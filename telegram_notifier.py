"""Telegram notification channel for security-pipeline alerts.

Pure standalone module (no detect.py imports). Sends HTML-formatted
alerts to a single Telegram chat via the Bot API using stdlib only
(urllib). Severity-filtered, rate-limited, retried with exponential
backoff, fail-soft (never raises to caller).

Credentials are loaded at import from a KEY=VALUE env file outside the
repo (default ~/.config/darlot/telegram.env, 0600). Missing file or
missing keys raise RuntimeError at import — fail loud.

Module-level state (last_success_ts, last_error, send_count,
drop_count) is read by detect.py's /health endpoint in Phase 5.
"""

import html
import json
import logging
import os
import time
import urllib.error
import urllib.parse
import urllib.request
from collections import deque
from pathlib import Path
from typing import Optional

log = logging.getLogger(__name__)

# ── Configuration ──────────────────────────────────────────────────────
# Severities that pass the filter. Tune by editing this set — no caller
# changes needed. Comparison is case-sensitive; callers must pass the
# canonical uppercase strings.
NOTIFY_LEVELS = frozenset({"CRITICAL", "HIGH", "MEDIUM"})

# Rate limit: if the oldest of the last RATE_MAX_SENDS timestamps was
# within RATE_WINDOW_S, drop. Keeps us well under Telegram's ~20/min
# group-chat ceiling and protects the API from dedup failures upstream.
RATE_MAX_SENDS = 20
RATE_WINDOW_S = 60

# 3 total send attempts. The tuple holds the sleep BEFORE each retry:
# 5s before attempt 2, then 30s before attempt 3. The kickoff spec also
# mentioned 120s as a third backoff — that would imply a 4th attempt
# which "3 attempts" forbids. If you want the longer tail, append 120
# here and bump MAX_ATTEMPTS.
RETRY_BACKOFF_S = (5, 30)
MAX_ATTEMPTS = 3

HTTP_TIMEOUT_S = 15

# Credentials path. Overrideable via env var so tests can redirect.
CREDENTIALS_PATH = Path(
    os.environ.get(
        "DARLOT_TELEGRAM_ENV",
        str(Path.home() / ".config" / "darlot" / "telegram.env"),
    )
)

_SEVERITY_EMOJI = {
    "CRITICAL": "🔴",
    "HIGH": "🟡",
    "MEDIUM": "🔵",
    "LOW": "⚪",
    "INFO": "⚫",
}

# ── Module state (read by /health) ─────────────────────────────────────
last_success_ts: int = 0
last_error: Optional[str] = None
send_count: int = 0
drop_count: int = 0

_recent_sends: "deque[float]" = deque(maxlen=RATE_MAX_SENDS)


# ── Credentials loader ─────────────────────────────────────────────────


def _load_credentials(path: Path) -> dict:
    """Parse a KEY=VALUE env-style file.

    Blank lines and '#' comment lines are skipped. Surrounding single or
    double quotes on values are stripped.

    Args:
        path: filesystem path to the credentials file.

    Returns:
        Dict mapping each KEY to its string value.
    """
    creds: dict = {}
    for raw in path.read_text().splitlines():
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        if "=" not in line:
            continue
        key, _, value = line.partition("=")
        creds[key.strip()] = value.strip().strip('"').strip("'")
    return creds


try:
    _creds = _load_credentials(CREDENTIALS_PATH)
    TELEGRAM_BOT_TOKEN = _creds["TELEGRAM_BOT_TOKEN"]
    TELEGRAM_CHAT_ID = _creds["TELEGRAM_CHAT_ID"]
except FileNotFoundError as e:
    raise RuntimeError(
        f"NOTIFIER credentials file missing: {CREDENTIALS_PATH}. "
        "Create it (mode 0600) with TELEGRAM_BOT_TOKEN and "
        "TELEGRAM_CHAT_ID."
    ) from e
except KeyError as e:
    raise RuntimeError(
        f"NOTIFIER credentials at {CREDENTIALS_PATH} missing "
        f"required key: {e.args[0]}"
    ) from e

_API_BASE = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}"


# ── Message formatting ────────────────────────────────────────────────


def _format_message(
    severity: str, site: str, camera: str, summary: str, event_id: int
) -> str:
    """Build the HTML-escaped Telegram message body.

    All dynamic fields are HTML-escaped because they may originate from
    ML output (summary) or operator-editable config (site, camera).

    Args:
        severity: level string (CRITICAL/HIGH/MEDIUM/LOW/INFO).
        site: site identifier.
        camera: camera identifier.
        summary: short human-readable description of the event.
        event_id: numeric id rendered as "Event #<id>".

    Returns:
        HTML-formatted message body ready for parse_mode=HTML.
    """
    emoji = _SEVERITY_EMOJI.get(severity, "⚫")
    ts = time.strftime("%H:%M:%S %Y-%m-%d", time.localtime())
    return (
        f"{emoji} <b>{html.escape(severity)} — {html.escape(summary)}</b>\n"
        f"\n"
        f"Site: {html.escape(str(site))}\n"
        f"Camera: {html.escape(str(camera))}\n"
        f"Time: {ts}\n"
        f"\n"
        f"Event #{html.escape(str(event_id))}"
    )


# ── Telegram API calls ─────────────────────────────────────────────────


def _check_ok(payload: dict) -> None:
    """Raise RuntimeError if Telegram returned a non-ok response."""
    if not payload.get("ok"):
        raise RuntimeError(f"Telegram API: {payload.get('description')}")


def _post_message(text: str) -> None:
    """POST /sendMessage. Raises on HTTP error or non-ok response."""
    url = f"{_API_BASE}/sendMessage"
    data = urllib.parse.urlencode(
        {
            "chat_id": TELEGRAM_CHAT_ID,
            "text": text,
            "parse_mode": "HTML",
            "disable_web_page_preview": "true",
        }
    ).encode()
    req = urllib.request.Request(url, data=data, method="POST")
    try:
        with urllib.request.urlopen(req, timeout=HTTP_TIMEOUT_S) as resp:
            payload = json.loads(resp.read())
    except urllib.error.HTTPError as e:
        body = e.read().decode(errors="replace") if e.fp else ""
        raise RuntimeError(f"Telegram HTTP {e.code}: {body}") from e
    _check_ok(payload)


def _post_photo(photo: Path, caption: str) -> None:
    """POST /sendPhoto with a multipart body. Raises on any error."""
    url = f"{_API_BASE}/sendPhoto"
    boundary = f"----DarlotNotifier{int(time.time() * 1000)}"

    parts: list = []

    def _field(name: str, value: str) -> None:
        parts.append(f"--{boundary}\r\n".encode())
        parts.append(
            f'Content-Disposition: form-data; name="{name}"\r\n\r\n'.encode()
        )
        parts.append(value.encode())
        parts.append(b"\r\n")

    _field("chat_id", TELEGRAM_CHAT_ID)
    _field("caption", caption)
    _field("parse_mode", "HTML")

    parts.append(f"--{boundary}\r\n".encode())
    parts.append(
        (
            f'Content-Disposition: form-data; name="photo"; '
            f'filename="{photo.name}"\r\n'
        ).encode()
    )
    parts.append(b"Content-Type: image/jpeg\r\n\r\n")
    parts.append(photo.read_bytes())
    parts.append(b"\r\n")
    parts.append(f"--{boundary}--\r\n".encode())

    body = b"".join(parts)
    req = urllib.request.Request(
        url,
        data=body,
        method="POST",
        headers={
            "Content-Type": f"multipart/form-data; boundary={boundary}",
        },
    )
    try:
        with urllib.request.urlopen(req, timeout=HTTP_TIMEOUT_S) as resp:
            payload = json.loads(resp.read())
    except urllib.error.HTTPError as e:
        err_body = e.read().decode(errors="replace") if e.fp else ""
        raise RuntimeError(f"Telegram HTTP {e.code}: {err_body}") from e
    _check_ok(payload)


# ── Public API ─────────────────────────────────────────────────────────


def notify(
    severity: str,
    site: str,
    camera: str,
    summary: str,
    event_id: int,
    snapshot_path: Optional[str] = None,
) -> bool:
    """Send a Telegram alert.

    Enforces the severity threshold and rate limit internally. Retries
    transient failures with exponential backoff. Never raises — callers
    treat a False return as "notification not delivered".

    Args:
        severity: one of CRITICAL/HIGH/MEDIUM/LOW/INFO (case-sensitive).
            Only NOTIFY_LEVELS pass; LOW/INFO are dropped.
        site: site identifier; HTML-escaped before send.
        camera: camera identifier; HTML-escaped before send.
        summary: short human description; HTML-escaped before send.
        event_id: numeric event id, rendered as "Event #<id>".
        snapshot_path: optional JPEG path. If the file exists, sent as a
            photo with the message as caption; otherwise text-only.

    Returns:
        True if Telegram accepted the send; False on severity drop,
        rate-limit drop, or failure after all retries.
    """
    global last_success_ts, last_error, send_count, drop_count

    if severity not in NOTIFY_LEVELS:
        log.debug("NOTIFIER skip: severity=%s below threshold", severity)
        return False

    now = time.time()
    if (
        len(_recent_sends) == RATE_MAX_SENDS
        and now - _recent_sends[0] < RATE_WINDOW_S
    ):
        drop_count += 1
        last_error = "rate limit"
        log.warning(
            "NOTIFIER dropped: rate limit — %d sends in last %ds",
            RATE_MAX_SENDS,
            RATE_WINDOW_S,
        )
        return False
    _recent_sends.append(now)

    message = _format_message(severity, site, camera, summary, event_id)

    photo: Optional[Path] = None
    if snapshot_path:
        p = Path(snapshot_path)
        if p.is_file():
            photo = p
        else:
            log.info(
                "NOTIFIER snapshot not on disk (%s) — sending text-only",
                snapshot_path,
            )

    for attempt in range(1, MAX_ATTEMPTS + 1):
        if attempt > 1:
            delay = RETRY_BACKOFF_S[attempt - 2]
            log.info(
                "NOTIFIER retry %d/%d after %ds (last_err=%s)",
                attempt,
                MAX_ATTEMPTS,
                delay,
                last_error,
            )
            time.sleep(delay)
        try:
            if photo is not None:
                _post_photo(photo, message)
            else:
                _post_message(message)
            last_success_ts = int(time.time())
            last_error = None
            send_count += 1
            log.info(
                "NOTIFIER sent: severity=%s event=%s attempt=%d photo=%s",
                severity,
                event_id,
                attempt,
                photo is not None,
            )
            return True
        except Exception as e:
            last_error = f"{type(e).__name__}: {e}"
            log.warning(
                "NOTIFIER attempt %d/%d failed: %s",
                attempt,
                MAX_ATTEMPTS,
                e,
            )

    log.error(
        "NOTIFIER failed after %d attempts: severity=%s event=%s last_err=%s",
        MAX_ATTEMPTS,
        severity,
        event_id,
        last_error,
    )
    return False
