#!/usr/bin/env bash
# cam2.sh — one-stop launcher for cam2 dev tasks.
# Usage: ./cam2.sh        (interactive menu)
#        ./cam2.sh ptz    (run PTZ self-test only)
#        ./cam2.sh run    (start the detection pipeline only)

set -u

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
cd "$SCRIPT_DIR" || { echo "could not cd to $SCRIPT_DIR"; exit 1; }

if [[ ! -d .venv ]]; then
  echo "ERROR: .venv/ not found in $SCRIPT_DIR"
  echo "Run setup.sh first (or check you're in the right project)."
  exit 1
fi

# shellcheck disable=SC1091
source .venv/bin/activate

if [[ ! -f cam2.env ]]; then
  echo "ERROR: cam2.env not found. Camera credentials are missing."
  exit 1
fi

LAN_IP="$(ip -4 -o addr show scope global 2>/dev/null \
  | awk '{print $4}' | cut -d/ -f1 | head -1)"
LAN_IP="${LAN_IP:-localhost}"

ptz_test() {
  echo "──────── PTZ self-test ────────"
  echo "Watch the camera (or its web UI live view) for movement."
  echo
  echo "[1/5] login-only..."
  python3 cam2_login.py login-only || { echo "login failed"; return 1; }
  echo
  echo "[2/5] zoom-in (1.5s)..."
  python3 cam2_login.py zoom-in --duration 1.5
  sleep 1
  echo "[3/5] zoom-out (1.5s)..."
  python3 cam2_login.py zoom-out --duration 1.5
  sleep 1
  echo "[4/5] pan-right (0.8s)..."
  python3 cam2_login.py pan-right --duration 0.8
  sleep 1
  echo "[5/5] pan-left (0.8s)..."
  python3 cam2_login.py pan-left --duration 0.8
  echo
  echo "PTZ self-test done."
}

start_pipeline() {
  echo "──────── starting detection pipeline ────────"
  echo "Live MJPEG stream: http://${LAN_IP}:8083/stream"
  echo "Health endpoint:   http://${LAN_IP}:8082/health"
  echo
  echo "Press Ctrl+C in this terminal to stop the pipeline."
  echo "(First start takes 10-30s while YOLO loads.)"
  echo

  if command -v xdg-open >/dev/null 2>&1; then
    (sleep 12 && xdg-open "http://${LAN_IP}:8083/stream" >/dev/null 2>&1) &
  fi

  python3 detect_cam2.py
}

case "${1:-}" in
  ptz)
    ptz_test
    exit $?
    ;;
  run|start|detect)
    start_pipeline
    exit $?
    ;;
  "")
    ;;
  *)
    echo "unknown subcommand: $1"
    echo "use: ./cam2.sh [ptz|run]   (or no args for menu)"
    exit 2
    ;;
esac

while true; do
  echo
  echo "════════════════════ cam2 launcher ════════════════════"
  echo "  1) PTZ self-test  (login + zoom in/out + pan left/right)"
  echo "  2) Start detection pipeline  (RTSP -> YOLO -> MJPEG)"
  echo "  q) Quit"
  echo "═══════════════════════════════════════════════════════"
  read -rp "choice: " choice
  case "$choice" in
    1) ptz_test ;;
    2) start_pipeline ;;
    q|Q|"") echo "bye"; exit 0 ;;
    *) echo "unknown choice: $choice" ;;
  esac
done
