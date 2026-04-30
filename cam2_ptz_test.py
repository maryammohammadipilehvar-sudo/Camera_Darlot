"""cam2_ptz_test.py — manual PTZ command tester for cam2 (INP-54M2812M0A).

Sends a single Start/Stop pulse to the camera's PTZ control endpoint using a
session cookie + CSRF token captured from a logged-in browser session. Useful
to verify the API works before wiring auto-zoom into detect_cam2.py.

Usage:
    source .venv/bin/activate
    python3 cam2_ptz_test.py zoom-in              # 0.5s pulse, default speed
    python3 cam2_ptz_test.py zoom-out --duration 1.0
    python3 cam2_ptz_test.py pan-left  --speed 5
    python3 cam2_ptz_test.py stop                 # explicit stop (no pulse)

Tokens are loaded from cam2.env (gitignored). They are session-bound and
expire after ~5-15 min of inactivity — refresh from DevTools when the script
prints {"error_code": "expired"}.

This script intentionally has no heavy dependencies (no torch, no ultralytics)
so iteration is fast.
"""

import argparse
import json
import os
import ssl
import sys
import time
import urllib.error
import urllib.request
from datetime import datetime
from pathlib import Path
from typing import Optional


def _load_env_file(path: Path) -> None:
    """Load KEY=VALUE pairs from a .env file into os.environ (setdefault)."""
    if not path.exists():
        return
    for raw in path.read_text().splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        k, _, v = line.partition("=")
        os.environ.setdefault(k.strip(), v.strip().strip('"').strip("'"))


_load_env_file(Path(__file__).parent / "cam2.env")


# Mapping from friendly action names to the camera's Ptz_Cmd_* values.
# Derived from the captured /API/PreviewChannel/PTZ/Control request body.
_CMD_MAP = {
    "zoom-in":   "Ptz_Cmd_ZoomPlus",
    "zoom-out":  "Ptz_Cmd_ZoomMinus",
    "pan-left":  "Ptz_Cmd_PanLeft",
    "pan-right": "Ptz_Cmd_PanRight",
    "tilt-up":   "Ptz_Cmd_TiltUp",
    "tilt-down": "Ptz_Cmd_TiltDown",
    "focus-near": "Ptz_Cmd_FocusNear",
    "focus-far":  "Ptz_Cmd_FocusFar",
    "stop":      "Ptz_Cmd_Stop",
}


def _send(
    host: str,
    session: str,
    csrf: str,
    cmd: str,
    state: str,
    speed: int,
    zoom_step: int,
    channel: str = "CH1",
    timeout: float = 5.0,
) -> tuple[int, dict]:
    """Send a single PTZ control request.

    Args:
        host: Camera host or host:port (no scheme).
        session: Session cookie value (the part after "session=").
        csrf: X-csrftoken header value.
        cmd: Ptz_Cmd_* string (see ``_CMD_MAP`` values).
        state: "Start" or "Stop".
        speed: 1..N speed value (continuous-press only).
        zoom_step: Discrete step size; mostly relevant for zoom commands.
        channel: Camera channel id; "CH1" for this single-sensor model.
        timeout: HTTP timeout in seconds.

    Returns:
        Tuple of (http_status, parsed_json_body). Body is ``{}`` if not JSON.
    """
    ts = datetime.now().strftime("%Y-%m-%d@%H:%M:%S")
    url = f"https://{host}/API/PreviewChannel/PTZ/Control?{ts}"
    payload = {
        "data": {
            "channel": channel,
            "cmd": cmd,
            "speed": speed,
            "state": state,
            "zoom_step": zoom_step,
        },
        "version": "1.0",
    }
    body = json.dumps(payload).encode()

    req = urllib.request.Request(url, data=body, method="POST")
    req.add_header("Content-Type", "application/json")
    req.add_header("Accept", "application/json; charset=utf-8")
    req.add_header("Origin", f"https://{host}")
    req.add_header("Referer", f"https://{host}/")
    req.add_header("Cookie", f"session={session}")
    req.add_header("X-csrftoken", csrf)

    ctx = ssl.create_default_context()
    ctx.check_hostname = False
    ctx.verify_mode = ssl.CERT_NONE  # camera ships with self-signed cert

    try:
        with urllib.request.urlopen(req, timeout=timeout, context=ctx) as resp:
            raw = resp.read().decode("utf-8", errors="replace")
            status = resp.status
    except urllib.error.HTTPError as e:
        raw = e.read().decode("utf-8", errors="replace")
        status = e.code
    except (urllib.error.URLError, TimeoutError) as e:
        return -1, {"error": f"network: {e}"}

    try:
        parsed = json.loads(raw)
    except json.JSONDecodeError:
        parsed = {"raw": raw[:500]}
    return status, parsed


def main(argv: Optional[list] = None) -> int:
    """CLI entry. Returns exit code: 0 on apparent success, non-zero on error."""
    parser = argparse.ArgumentParser(description=__doc__.split("\n", 1)[0])
    parser.add_argument(
        "action",
        choices=sorted(_CMD_MAP.keys()),
        help="PTZ action to send",
    )
    parser.add_argument(
        "--duration", type=float, default=0.5,
        help="seconds between Start and Stop (default 0.5; ignored for 'stop')",
    )
    parser.add_argument(
        "--speed", type=int, default=10,
        help="motion speed (default 10; range depends on camera)",
    )
    parser.add_argument(
        "--zoom-step", type=int, default=1,
        help="discrete zoom step (default 1; matches captured payload)",
    )
    parser.add_argument(
        "--no-stop", action="store_true",
        help="skip the trailing Stop frame (debug only)",
    )
    args = parser.parse_args(argv)

    host = os.getenv("CAM2_HOST", "192.168.2.117")
    session = os.getenv("CAM2_SESSION_COOKIE", "").strip()
    csrf = os.getenv("CAM2_CSRF_TOKEN", "").strip()
    if not session or not csrf:
        sys.stderr.write(
            "missing CAM2_SESSION_COOKIE or CAM2_CSRF_TOKEN in cam2.env. "
            "Capture them from a logged-in browser session — see the comment "
            "block in cam2.env for steps.\n"
        )
        return 2

    cmd = _CMD_MAP[args.action]

    if args.action == "stop":
        status, body = _send(
            host, session, csrf, cmd, "Stop",
            args.speed, args.zoom_step,
        )
        print(f"[stop] HTTP {status} {json.dumps(body)}")
        return 0 if status == 200 else 1

    # Start phase
    status, body = _send(
        host, session, csrf, cmd, "Start", args.speed, args.zoom_step,
    )
    print(f"[start {args.action}] HTTP {status} {json.dumps(body)}")
    if status != 200:
        return 1

    # If the response indicates auth expired, don't even try Stop —
    # nothing's moving anyway.
    if isinstance(body, dict) and body.get("error_code") == "expired":
        sys.stderr.write(
            "session expired — refresh CAM2_SESSION_COOKIE / CAM2_CSRF_TOKEN "
            "in cam2.env\n"
        )
        return 1

    if args.no_stop:
        return 0

    time.sleep(args.duration)

    # Stop phase
    status, body = _send(
        host, session, csrf, cmd, "Stop", args.speed, args.zoom_step,
    )
    print(f"[stop  {args.action}] HTTP {status} {json.dumps(body)}")
    return 0 if status == 200 else 1


if __name__ == "__main__":
    sys.exit(main())
