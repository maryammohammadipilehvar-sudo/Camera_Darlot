#!/usr/bin/env bash
# =============================================================================
#  apply_upgrades.sh — Sentinel Security Pipeline v2 upgrade
#  Run from:  /home/quarero/Desktop/security_pipeline
#  Usage:     bash apply_upgrades.sh
# =============================================================================
set -e

ROOT="/home/quarero/Desktop/security_pipeline"
VENV="$ROOT/.venv"
PY="$VENV/bin/python"
PIP="$VENV/bin/pip install --quiet"
UPGRADE_SRC="$ROOT/upgrade"   # where you placed the downloaded files

echo "======================================================================"
echo " Sentinel v2 Upgrade Script"
echo " Root : $ROOT"
echo "======================================================================"

cd "$ROOT"

# ── 0. Sanity check ────────────────────────────────────────────────────────
if [ ! -f "$VENV/bin/python" ]; then
  echo "ERROR: venv not found at $VENV"; exit 1
fi
if [ ! -f "$ROOT/detect.py" ]; then
  echo "ERROR: detect.py not found in $ROOT"; exit 1
fi

echo ""
echo "── Step 1/7: Backup existing files ──────────────────────────────────"
STAMP=$(date +"%Y%m%d_%H%M%S")
cp detect.py                              "detect.py.bak_${STAMP}"
cp dashboard_static/index.html            "dashboard_static/index.html.bak_${STAMP}" 2>/dev/null || true
cp dashboard_server.py                    "dashboard_server.py.bak_${STAMP}"         2>/dev/null || true
echo "  ✓ Backups created (stamp: $STAMP)"

echo ""
echo "── Step 2/7: Install new Python dependencies ─────────────────────────"
# fastapi + uvicorn already installed; add new deps
$PIP "paho-mqtt>=1.6.1"
$PIP "fastapi>=0.110.0" "uvicorn[standard]>=0.27.0"
# psutil for optional resource stats (non-fatal)
$PIP "psutil" 2>/dev/null || true
echo "  ✓ Python dependencies OK"

echo ""
echo "── Step 3/7: Deploy upgraded source files ────────────────────────────"

# detect.py v2
if [ -f "$UPGRADE_SRC/detect_v2.py" ]; then
  cp "$UPGRADE_SRC/detect_v2.py" "$ROOT/detect.py"
  echo "  ✓ detect.py updated"
else
  echo "  SKIP: detect_v2.py not found in $UPGRADE_SRC"
fi

# behavior.py
if [ -f "$UPGRADE_SRC/behavior.py" ]; then
  cp "$UPGRADE_SRC/behavior.py" "$ROOT/behavior.py"
  echo "  ✓ behavior.py deployed"
else
  echo "  SKIP: behavior.py not found in $UPGRADE_SRC"
fi

# export_pose_model.py
if [ -f "$UPGRADE_SRC/export_pose_model.py" ]; then
  cp "$UPGRADE_SRC/export_pose_model.py" "$ROOT/export_pose_model.py"
  echo "  ✓ export_pose_model.py deployed"
fi

# index.html
if [ -f "$UPGRADE_SRC/index.html" ]; then
  cp "$UPGRADE_SRC/index.html" "$ROOT/dashboard_static/index.html"
  echo "  ✓ dashboard index.html updated"
fi

echo ""
echo "── Step 4/7: Export YOLOv8n-pose TRT engine ─────────────────────────"
if [ -f "$ROOT/models/yolov8n-pose.engine" ]; then
  echo "  ✓ yolov8n-pose.engine already exists — skipping"
else
  echo "  Exporting (this takes ~5–10 minutes on Jetson Orin)…"
  cd "$ROOT"
  $PY export_pose_model.py
  echo "  ✓ Pose engine built"
fi

echo ""
echo "── Step 5/7: Restart pipeline service ───────────────────────────────"
if systemctl is-active --quiet security_pipeline.service 2>/dev/null; then
  sudo systemctl restart security_pipeline.service
  sleep 3
  sudo systemctl status security_pipeline.service --no-pager -l | head -20
  echo "  ✓ security_pipeline.service restarted"
else
  echo "  INFO: security_pipeline.service not active; start manually:"
  echo "        cd $ROOT && source .venv/bin/activate && python detect.py"
fi

echo ""
echo "── Step 6/7: Restart dashboard service ──────────────────────────────"
if systemctl is-active --quiet sentinel_dashboard.service 2>/dev/null; then
  sudo systemctl restart sentinel_dashboard.service
  sleep 2
  sudo systemctl status sentinel_dashboard.service --no-pager -l | head -10
  echo "  ✓ sentinel_dashboard.service restarted"
else
  echo "  INFO: sentinel_dashboard.service not active; start manually:"
  echo "        cd $ROOT && source .venv/bin/activate && python dashboard_server.py"
fi

echo ""
echo "── Step 7/7: Smoke tests ─────────────────────────────────────────────"
sleep 2

echo -n "  Health endpoint: "
HEALTH=$(curl -sf http://localhost:8081/health 2>/dev/null | python3 -c "import sys,json;d=json.load(sys.stdin);print(d.get('status','?'))" 2>/dev/null || echo "unreachable")
echo "$HEALTH"

echo -n "  Dashboard API:   "
DASH=$(curl -sf http://localhost:8888/api/status 2>/dev/null | python3 -c "import sys,json;d=json.load(sys.stdin);print('ok' if d.get('ok') else 'err')" 2>/dev/null || echo "unreachable")
echo "$DASH"

echo -n "  MQTT broker:     "
MQTT=$(mosquitto_pub -h localhost -t test/sentinel -m ping -q 0 2>/dev/null && echo "ok" || echo "unreachable")
echo "$MQTT"

echo ""
echo "======================================================================"
echo " ✓ Upgrade complete"
echo ""
echo " Dashboard:    http://$(hostname -I | awk '{print $1}'):8888"
echo " Health:       http://$(hostname -I | awk '{print $1}'):8081/health"
echo " Stream:       http://$(hostname -I | awk '{print $1}'):8080/stream"
echo " MQTT:         mosquitto_sub -t security/alerts"
echo ""
echo " Logs:"
echo "   Pipeline:   sudo journalctl -u security_pipeline.service -f"
echo "   Dashboard:  sudo journalctl -u sentinel_dashboard.service -f"
echo "======================================================================"
