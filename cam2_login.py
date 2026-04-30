"""cam2_login.py — autonomous login + PTZ client for cam2 (INP-54M2812M0A).

Replicates the camera's web-UI login handshake so the pipeline can drive PTZ
without a browser-captured session. Reverse-engineered from the camera's
JS bundle (module 138 / "encrypt.js" / "mycrypto").

Protocol summary (default `base_x_public` / X25519 mode):

    1. POST /API/Login/TransKey/Get  {data:{type:["base_x_public"]}}
       -> {data:{Key_lists:[{type:"base_x_public", key:"0<b64>", seq:N}]}}
       The "0" prefix is a literal marker; the rest is base64(server_pub_32).

    2. Generate ephemeral X25519 keypair. shared = ECDH(eph_priv, server_pub).

    3. KDF (HKDF-Expand only, no Extract — non-standard but matches JS):
         key = HMAC-SHA256(shared, b"expand key" + 0x01)[:32]
         iv  = HMAC-SHA256(shared, b"expand iv"  + 0x01)[:12]

    4. AES-256-GCM encrypt password.
       ciphertext = ct_bytes || iv || tag  (concatenated)
       cipher_str = "0" + base64(ciphertext)
       peer_key   = "0" + base64(eph_pub_32)

    5. POST /API/Web/Login {data:{user, base_enc_password:{seq, peer_key, cipher}}}
       -> 200 OK, Set-Cookie: session=<hex>;  body has token (CSRF), user, ...

    6. For all subsequent /API/* calls: send Cookie: session=... AND
       X-csrftoken: <token from login response>.

The session expires on inactivity (~5-15 min). A periodic /API/Login/Heartbeat
ping keeps it alive.

This module is intentionally self-contained — no torch/cv2 imports — so it
can be imported by `detect_cam2.py` without bloating its startup.
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import json
import logging
import os
import re
import secrets
import ssl
import threading
import time
import urllib.error
import urllib.request
from datetime import datetime
from pathlib import Path
from typing import Any, Optional

from cryptography.hazmat.primitives.asymmetric.x25519 import (
    X25519PrivateKey, X25519PublicKey,
)
from cryptography.hazmat.primitives.ciphers.aead import AESGCM
from cryptography.hazmat.primitives import serialization

log = logging.getLogger("cam2.login")


# ─────────────────────────── crypto helpers ────────────────────────────────────

def _b64_strip0(s: str) -> bytes:
    """Decode the camera's "0<base64>" wire format into raw bytes.

    The leading "0" is a literal marker the JS prepends. The remainder is
    standard or url-safe base64 (the JS accepts both via regex replacement).

    Args:
        s: A "0"-prefixed base64 string from the camera.

    Returns:
        Decoded bytes.

    Raises:
        ValueError: If ``s`` does not start with "0" or is not valid base64.
    """
    if not s or s[0] != "0":
        raise ValueError(f"expected leading '0' marker, got: {s[:5]!r}")
    body = s[1:].replace("-", "+").replace("_", "/")
    pad = (-len(body)) % 4
    return base64.b64decode(body + "=" * pad)


def _wrap0_b64(data: bytes) -> str:
    """Encode bytes as the camera's "0<base64>" wire format."""
    return "0" + base64.b64encode(data).decode("ascii")


def _hkdf_expand_no_extract(
    prk: bytes, info: bytes, length: int
) -> bytes:
    """HKDF-Expand-only with SHA-256 — matches the camera's JS KDF.

    Standard HKDF first runs Extract; the camera skips it and uses the
    ECDH shared secret directly as PRK.

    Args:
        prk: Pseudo-random key (the ECDH shared secret).
        info: Context info string. Becomes part of every HMAC input.
        length: Desired output length in bytes (must be <= 255 * 32).

    Returns:
        ``length`` bytes of derived material.
    """
    if length > 255 * 32:
        raise ValueError("requested length exceeds HKDF max (255*32)")
    blocks: list[bytes] = []
    prev = b""
    n_blocks = (length + 31) // 32
    for i in range(1, n_blocks + 1):
        h = hmac.new(prk, prev + info + bytes([i]), hashlib.sha256)
        prev = h.digest()
        blocks.append(prev)
    return b"".join(blocks)[:length]


def _x25519_encrypt_password(server_pub_wire: str, password: str) -> dict:
    """Encrypt a password using the camera's X25519 + AES-256-GCM scheme.

    Args:
        server_pub_wire: The "0"-prefixed base64 server public key from
            ``/API/Login/TransKey/Get``.
        password: Plaintext password (UTF-8 string).

    Returns:
        Dict ``{"peer_key": "0...", "cipher": "0..."}`` ready to put in
        ``base_enc_password`` (caller still needs to set ``seq``).
    """
    server_pub_bytes = _b64_strip0(server_pub_wire)
    if len(server_pub_bytes) != 32:
        raise ValueError(
            f"server pub must be 32 bytes (got {len(server_pub_bytes)})"
        )

    eph_priv = X25519PrivateKey.generate()
    server_pub = X25519PublicKey.from_public_bytes(server_pub_bytes)
    shared = eph_priv.exchange(server_pub)

    aes_key = _hkdf_expand_no_extract(shared, b"expand key", 32)
    iv = _hkdf_expand_no_extract(shared, b"expand iv", 12)

    aesgcm = AESGCM(aes_key)
    # cryptography returns ciphertext || tag (16-byte tag appended).
    ct_and_tag = aesgcm.encrypt(iv, password.encode("utf-8"), None)
    ct = ct_and_tag[:-16]
    tag = ct_and_tag[-16:]

    # Camera's wire layout: ct || iv || tag
    blob = ct + iv + tag

    eph_pub_bytes = eph_priv.public_key().public_bytes(
        encoding=serialization.Encoding.Raw,
        format=serialization.PublicFormat.Raw,
    )

    return {
        "peer_key": _wrap0_b64(eph_pub_bytes),
        "cipher": _wrap0_b64(blob),
    }


# ─────────────────────────── HTTP Digest (RFC 7616) ───────────────────────────

def _parse_digest_challenge(www_auth: str) -> dict:
    """Parse a ``WWW-Authenticate: Digest ...`` challenge into a dict.

    Args:
        www_auth: Raw header value (with or without leading "Digest ").

    Returns:
        Dict of lowercased keys to string values (quotes stripped).
    """
    s = www_auth.strip()
    if s.lower().startswith("digest "):
        s = s[7:]
    out: dict = {}
    for m in re.finditer(
        r'(\w+)\s*=\s*(?:"((?:[^"\\]|\\.)*)"|([^,]+))', s
    ):
        k = m.group(1).lower()
        v = m.group(2) if m.group(2) is not None else m.group(3).strip()
        out[k] = v
    return out


def _build_digest_auth_header(
    user: str,
    password: str,
    method: str,
    uri: str,
    challenge: dict,
    nc: str = "00000001",
) -> str:
    """Build an ``Authorization: Digest ...`` header value (RFC 7616).

    Supports algorithms MD5 and SHA-256 (with or without -sess), qop=auth,
    and ``userhash="true"``.

    Args:
        user: Username (plaintext).
        password: Plaintext password.
        method: HTTP method (e.g. "POST").
        uri: Request URI (path + query string).
        challenge: Parsed challenge dict from ``_parse_digest_challenge``.
        nc: Nonce-count (8 hex digits, default "00000001").

    Returns:
        Full header value beginning with "Digest ".

    Raises:
        ValueError: For unsupported algorithm or qop.
    """
    realm = challenge["realm"]
    nonce = challenge["nonce"]
    qop = challenge.get("qop", "auth")
    if "auth" not in qop.split(","):
        raise ValueError(f"unsupported qop: {qop!r}")
    qop = "auth"
    algorithm = challenge.get("algorithm", "MD5")
    userhash = challenge.get("userhash", "false").lower() == "true"
    cnonce = secrets.token_hex(8)
    opaque = challenge.get("opaque")

    algo = algorithm.upper().replace("-SESS", "")
    if algo == "MD5":
        H = lambda s: hashlib.md5(s.encode()).hexdigest()
    elif algo in ("SHA-256", "SHA256"):
        H = lambda s: hashlib.sha256(s.encode()).hexdigest()
    else:
        raise ValueError(f"unsupported digest algorithm: {algorithm}")

    HA1 = H(f"{user}:{realm}:{password}")
    if algorithm.upper().endswith("-SESS"):
        HA1 = H(f"{HA1}:{nonce}:{cnonce}")
    HA2 = H(f"{method}:{uri}")
    response = H(f"{HA1}:{nonce}:{nc}:{cnonce}:{qop}:{HA2}")

    sent_user = H(f"{user}:{realm}") if userhash else user

    parts = [
        f'username="{sent_user}"',
        f'realm="{realm}"',
        f'nonce="{nonce}"',
        f'uri="{uri}"',
        f"algorithm={algorithm}",
        f"qop={qop}",
        f"nc={nc}",
        f'cnonce="{cnonce}"',
        f'response="{response}"',
    ]
    if opaque:
        parts.append(f'opaque="{opaque}"')
    if userhash:
        parts.append("userhash=true")
    return "Digest " + ", ".join(parts)


# ─────────────────────────── HTTP transport ────────────────────────────────────

def _ssl_ctx_no_verify() -> ssl.SSLContext:
    """SSL context that skips cert verification (camera ships self-signed)."""
    ctx = ssl.create_default_context()
    ctx.check_hostname = False
    ctx.verify_mode = ssl.CERT_NONE
    return ctx


def _ts_query() -> str:
    """The cache-buster the JS appends to /API/* URLs (e.g. ?2026-04-30@13:42:42)."""
    return datetime.now().strftime("%Y-%m-%d@%H:%M:%S")


def _maybe_json(raw: str) -> dict:
    """Parse ``raw`` as JSON, falling back to ``{"raw": raw[:500]}``."""
    try:
        return json.loads(raw)
    except json.JSONDecodeError:
        return {"raw": raw[:500]}


def _extract_session_cookie(set_cookie_header: str) -> Optional[str]:
    """Pull the ``session=...`` value out of a Set-Cookie header.

    Handles both single-cookie and comma-joined multi-cookie headers.
    """
    if not set_cookie_header:
        return None
    for piece in set_cookie_header.split(","):
        for part in piece.split(";"):
            p = part.strip()
            if p.startswith("session="):
                return p.split("=", 1)[1]
    return None


# ─────────────────────────── Cam2Session ───────────────────────────────────────

class Cam2Session:
    """Live session against the camera's REST API.

    Owns the session cookie and CSRF token, refreshes on expiry, and exposes
    PTZ helpers. Thread-safe (per-instance lock around login + state mutation).

    Args:
        host: Camera host (without scheme, with optional :port).
        username: Login user.
        password: Login password.
        timeout_s: Per-request timeout.
    """

    def __init__(
        self,
        host: str,
        username: str,
        password: str,
        timeout_s: float = 6.0,
    ) -> None:
        self.host = host
        self.username = username
        self._password = password
        self.timeout_s = timeout_s

        self._session_cookie: Optional[str] = None
        self._csrf: Optional[str] = None
        self._user_token: Optional[str] = None  # echoed by login response
        self._lock = threading.Lock()
        self._hb_thread: Optional[threading.Thread] = None
        self._hb_stop = threading.Event()

    # ---- low-level HTTP -----------------------------------------------------

    def _request(
        self,
        method: str,
        path: str,
        body: Optional[dict] = None,
        with_auth: bool = True,
    ) -> tuple[int, dict, dict]:
        """Send one request. Returns (status, parsed_body, response_headers)."""
        url = f"https://{self.host}{path}?{_ts_query()}"
        data = json.dumps(body).encode() if body is not None else None
        req = urllib.request.Request(url, data=data, method=method)
        req.add_header("Content-Type", "application/json")
        req.add_header("Accept", "application/json; charset=utf-8")
        req.add_header("Origin", f"https://{self.host}")
        req.add_header("Referer", f"https://{self.host}/")
        if with_auth and self._session_cookie:
            req.add_header("Cookie", f"session={self._session_cookie}")
            if self._csrf:
                req.add_header("X-csrftoken", self._csrf)

        try:
            with urllib.request.urlopen(
                req, timeout=self.timeout_s, context=_ssl_ctx_no_verify()
            ) as resp:
                raw = resp.read().decode("utf-8", errors="replace")
                status = resp.status
                hdrs = dict(resp.headers.items())
        except urllib.error.HTTPError as e:
            raw = e.read().decode("utf-8", errors="replace")
            status = e.code
            hdrs = dict(e.headers.items()) if e.headers else {}
        except (urllib.error.URLError, TimeoutError) as e:
            return -1, {"error": f"network: {e}"}, {}

        try:
            parsed = json.loads(raw)
        except json.JSONDecodeError:
            parsed = {"raw": raw[:500]}
        return status, parsed, hdrs

    # ---- login --------------------------------------------------------------

    def _fetch_transkey(self) -> tuple[str, int]:
        """Fetch the server's X25519 public key. Returns (key_str, seq)."""
        status, body, _ = self._request(
            "POST", "/API/Login/TransKey/Get",
            body={"version": "1.0", "data": {"type": ["base_x_public"]}},
            with_auth=False,
        )
        if status != 200 or body.get("result") != "success":
            raise RuntimeError(f"TransKey/Get failed: {status} {body}")
        keys = body["data"]["Key_lists"]
        for k in keys:
            if k.get("type") == "base_x_public":
                return k["key"], int(k.get("seq", 0))
        raise RuntimeError(f"no base_x_public key in response: {body}")

    def _post_login_with_digest(
        self, payload: dict
    ) -> tuple[int, dict, dict]:
        """POST /API/Web/Login, performing the RFC 7616 Digest dance if asked.

        The camera answers the first POST with 401 +
        ``WWW-Authenticate: Digest ...``. We compute the response and replay
        the same body with an ``Authorization`` header, keeping the same URI
        (cache-buster timestamp) across both attempts.
        """
        path = "/API/Web/Login"
        # Use ONE timestamp for both attempts so the URI used in the digest
        # response matches the URI of the second request.
        url = f"https://{self.host}{path}?{_ts_query()}"
        uri = url[len(f"https://{self.host}"):]
        body_bytes = json.dumps(payload).encode()

        def _send(extra_headers: dict) -> tuple[int, dict, dict]:
            req = urllib.request.Request(url, data=body_bytes, method="POST")
            req.add_header("Content-Type", "application/json")
            req.add_header("Accept", "application/json; charset=utf-8")
            req.add_header("Origin", f"https://{self.host}")
            req.add_header("Referer", f"https://{self.host}/")
            for k, v in extra_headers.items():
                req.add_header(k, v)
            try:
                with urllib.request.urlopen(
                    req, timeout=self.timeout_s,
                    context=_ssl_ctx_no_verify(),
                ) as resp:
                    raw = resp.read().decode("utf-8", errors="replace")
                    return resp.status, dict(resp.headers.items()), _maybe_json(raw)
            except urllib.error.HTTPError as e:
                raw = e.read().decode("utf-8", errors="replace")
                return (
                    e.code,
                    dict(e.headers.items()) if e.headers else {},
                    _maybe_json(raw),
                )

        status, hdrs, body = _send({})
        if status == 401:
            www = hdrs.get("WWW-Authenticate") or hdrs.get("www-authenticate")
            if not www:
                raise RuntimeError(f"401 without WWW-Authenticate: {hdrs}")
            challenge = _parse_digest_challenge(www)
            auth_hdr = _build_digest_auth_header(
                self.username, self._password, "POST", uri, challenge,
            )
            status, hdrs, body = _send({"Authorization": auth_hdr})
        return status, body, hdrs

    def login(self) -> None:
        """Run the full login handshake. Updates session cookie + CSRF token.

        The camera enforces both:
          - An RFC 7616 HTTP Digest challenge on /API/Web/Login (proves
            knowledge of the password via challenge-response).
          - A custom encrypted-payload body (X25519 + AES-256-GCM), which
            establishes the application-level session.

        Raises:
            RuntimeError: On any handshake or HTTP failure.
        """
        with self._lock:
            server_pub, seq = self._fetch_transkey()
            enc = _x25519_encrypt_password(server_pub, self._password)
            payload = {
                "version": "1.0",
                "data": {
                    "user": self.username,
                    "base_enc_password": {
                        "seq": seq,
                        "peer_key": enc["peer_key"],
                        "cipher": enc["cipher"],
                    },
                },
            }
            status, body, hdrs = self._post_login_with_digest(payload)
            if status != 200 or body.get("result") != "success":
                raise RuntimeError(f"login failed: {status} {body}")

            # Session cookie comes from Set-Cookie. CSRF token comes from
            # the X-csrftoken RESPONSE header (the JS reads it from there).
            session = _extract_session_cookie(
                hdrs.get("Set-Cookie") or hdrs.get("set-cookie") or ""
            )
            if not session:
                raise RuntimeError(
                    f"login: no session cookie in headers: {hdrs}"
                )
            token = (
                hdrs.get("X-csrftoken") or hdrs.get("x-csrftoken") or ""
            ).strip()
            if not token:
                raise RuntimeError(
                    f"login: no X-csrftoken in response headers: {hdrs}"
                )

            self._session_cookie = session
            self._csrf = token
            self._user_token = token
            log.info(
                "logged in as %s (token=%s..., session=%s...)",
                self.username, token[:8], session[:8],
            )

    def ensure_logged_in(self) -> None:
        """Login if we don't already have a session cookie."""
        if self._session_cookie is None:
            self.login()

    # ---- PTZ ----------------------------------------------------------------

    def ptz_control(
        self,
        cmd: str,
        state: str,
        speed: int = 10,
        zoom_step: int = 1,
        channel: str = "CH1",
    ) -> tuple[int, dict]:
        """Send one Ptz_Cmd_* control frame.

        Args:
            cmd: e.g. ``"Ptz_Cmd_ZoomPlus"``, ``"Ptz_Cmd_PanLeft"``.
            state: ``"Start"`` or ``"Stop"``.
            speed: 1-N motor speed.
            zoom_step: Discrete step size (passed through; not always used).
            channel: Camera channel id.

        Returns:
            (http_status, parsed_body). On expired session this auto-relogs in
            once and retries.
        """
        self.ensure_logged_in()
        body = {
            "version": "1.0",
            "data": {
                "channel": channel,
                "cmd": cmd,
                "speed": speed,
                "state": state,
                "zoom_step": zoom_step,
            },
        }
        status, parsed, _ = self._request(
            "POST", "/API/PreviewChannel/PTZ/Control", body=body,
        )
        if (
            isinstance(parsed, dict)
            and parsed.get("error_code") in ("expired", "no_heartbeat")
        ):
            log.info(
                "session %s; re-logging in", parsed.get("error_code"),
            )
            self._session_cookie = None
            self._csrf = None
            self.login()
            status, parsed, _ = self._request(
                "POST", "/API/PreviewChannel/PTZ/Control", body=body,
            )
        return status, parsed

    def ptz_pulse(
        self,
        cmd: str,
        duration_s: float,
        speed: int = 10,
        zoom_step: int = 1,
    ) -> bool:
        """Send Start, sleep ``duration_s``, send Stop. Returns True on success."""
        s1, b1 = self.ptz_control(cmd, "Start", speed=speed, zoom_step=zoom_step)
        if s1 != 200:
            log.warning("ptz Start failed: %s %s", s1, b1)
            return False
        time.sleep(duration_s)
        s2, b2 = self.ptz_control(cmd, "Stop", speed=speed, zoom_step=zoom_step)
        if s2 != 200:
            log.warning("ptz Stop failed: %s %s", s2, b2)
            return False
        return True

    # ---- session keepalive --------------------------------------------------

    def heartbeat(self) -> bool:
        """Ping /API/Login/Heartbeat. Returns True on 200 success."""
        if self._session_cookie is None:
            return False
        status, parsed, _ = self._request(
            "POST", "/API/Login/Heartbeat",
            body={"version": "1.0"},
        )
        if (
            isinstance(parsed, dict)
            and parsed.get("error_code") in ("expired", "no_heartbeat")
        ):
            return False
        return status == 200

    def start_heartbeat(self, interval_s: float = 30.0) -> None:
        """Start a daemon thread that pings /API/Login/Heartbeat periodically.

        Without this, the camera kills the session after a short idle window
        and subsequent PTZ commands fail with ``error_code: no_heartbeat``.
        Idempotent — calling twice on the same session is a no-op.

        Args:
            interval_s: Seconds between heartbeats. Default 30s; the camera
                seems to want at most ~60s between pings on this firmware.
        """
        if self._hb_thread is not None and self._hb_thread.is_alive():
            return

        def _loop() -> None:
            while not self._hb_stop.wait(interval_s):
                try:
                    if not self.heartbeat():
                        log.info("heartbeat failed; re-logging in")
                        try:
                            self._session_cookie = None
                            self._csrf = None
                            self.login()
                        except Exception as e:
                            log.warning("heartbeat re-login failed: %s", e)
                except Exception as e:
                    log.warning("heartbeat error: %s", e)

        self._hb_stop.clear()
        self._hb_thread = threading.Thread(
            target=_loop, name="cam2-heartbeat", daemon=True,
        )
        self._hb_thread.start()
        log.info("heartbeat thread started (every %.0fs)", interval_s)

    def stop_heartbeat(self) -> None:
        """Signal the heartbeat thread to exit."""
        self._hb_stop.set()


# ─────────────────────────── env loader ────────────────────────────────────────

def _load_env_file(path: Path) -> None:
    """Load KEY=VALUE lines from a .env file into os.environ (setdefault)."""
    if not path.exists():
        return
    for raw in path.read_text().splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        k, _, v = line.partition("=")
        os.environ.setdefault(k.strip(), v.strip().strip('"').strip("'"))


def session_from_env() -> Cam2Session:
    """Build a Cam2Session from cam2.env (CAM2_HOST/USER/PASS).

    The session is not yet logged in; call ``.login()`` or ``.ensure_logged_in()``
    on the returned object.
    """
    _load_env_file(Path(__file__).parent / "cam2.env")
    host = os.getenv("CAM2_HOST", "192.168.2.117")
    user = os.environ["CAM2_USER"]
    pw = os.environ["CAM2_PASS"]
    return Cam2Session(host=host, username=user, password=pw)


# ─────────────────────────── CLI ───────────────────────────────────────────────

def _main() -> int:
    """Quick CLI: ``python3 cam2_login.py [zoom-in|zoom-out|...] [--duration]``."""
    import argparse
    # Verified valid command names on firmware V1.4.1.1025_250331:
    # zoom uses Add/Minus (NOT Plus/Minus); pan/tilt are bare directionals.
    actions = {
        "zoom-in":    "Ptz_Cmd_ZoomAdd",
        "zoom-out":   "Ptz_Cmd_ZoomMinus",
        "pan-left":   "Ptz_Cmd_Left",
        "pan-right":  "Ptz_Cmd_Right",
        "tilt-up":    "Ptz_Cmd_Up",
        "tilt-down":  "Ptz_Cmd_Down",
        "focus-near": "Ptz_Cmd_FocusMinus",
        "focus-far":  "Ptz_Cmd_FocusAdd",
        "login-only": "",
    }
    p = argparse.ArgumentParser()
    p.add_argument("action", choices=sorted(actions.keys()))
    p.add_argument("--duration", type=float, default=0.5)
    p.add_argument("--speed", type=int, default=10)
    p.add_argument("--zoom-step", type=int, default=1)
    args = p.parse_args()

    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s [%(name)s] %(message)s",
    )

    sess = session_from_env()
    sess.login()
    if args.action == "login-only":
        print("login OK")
        return 0
    cmd = actions[args.action]
    ok = sess.ptz_pulse(
        cmd, duration_s=args.duration,
        speed=args.speed, zoom_step=args.zoom_step,
    )
    print(f"{args.action}: {'OK' if ok else 'FAIL'}")
    return 0 if ok else 1


if __name__ == "__main__":
    raise SystemExit(_main())
