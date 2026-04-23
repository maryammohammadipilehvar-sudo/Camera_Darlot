#!/usr/bin/env bash
# ─────────────────────────────────────────────────────────────────────────────
# setup.sh  — one-shot install + validate for Jetson Orin (JetPack 5.x / 6.x)
# Run as:  bash setup.sh
# ─────────────────────────────────────────────────────────────────────────────
set -e

PYTHON=python3
PIP="$PYTHON -m pip install --quiet"
JETPACK=$(cat /etc/nv_tegra_release 2>/dev/null | head -1 || echo "unknown")

echo "======================================================================"
echo " AI Security Pipeline — Jetson Setup"
echo " JetPack: $JETPACK"
echo "======================================================================"

# ── 1. System deps ──────────────────────────────────────────────────────────
echo "[1/7] System packages..."
sudo apt-get update -qq
sudo apt-get install -y -qq \
    python3-pip python3-dev \
    libgl1-mesa-glx libglib2.0-0 \
    portaudio19-dev \
    mosquitto mosquitto-clients \
    curl wget

# Start MQTT broker
sudo systemctl enable mosquitto
sudo systemctl start mosquitto
echo "  ✓ mosquitto MQTT broker running on localhost:1883"

# ── 2. PyTorch + torchvision (JetPack wheel) ────────────────────────────────
echo "[2/7] PyTorch (JetPack wheel)..."
JP_VER=$(python3 -c "
import re, subprocess
out = subprocess.check_output(['dpkg', '-l', 'nvidia-jetpack'], text=True, stderr='/dev/null') or ''
m = re.search(r'5\.\d+', out)
print('jp5' if m else 'jp6')
" 2>/dev/null || echo "jp6")

if [ "$JP_VER" = "jp5" ]; then
    TORCH_URL="https://developer.download.nvidia.com/compute/redist/jp/v512/pytorch/torch-2.1.0a0+41361538.nv23.06-cp38-cp38-linux_aarch64.whl"
else
    TORCH_URL="https://developer.download.nvidia.com/compute/redist/jp/v60/pytorch/torch-2.3.0a0+ebedce2.nv24.04-cp310-cp310-linux_aarch64.whl"
fi

if ! $PYTHON -c "import torch; assert torch.cuda.is_available()" 2>/dev/null; then
    echo "  Downloading PyTorch wheel..."
    wget -q -O /tmp/torch_jetson.whl "$TORCH_URL"
    $PIP /tmp/torch_jetson.whl
    echo "  ✓ PyTorch installed"
else
    echo "  ✓ PyTorch already present"
fi

# Validate
$PYTHON -c "import torch; print(f'  PyTorch {torch.__version__}, CUDA: {torch.cuda.is_available()}')"

# ── 3. onnxruntime-gpu (JetPack wheel) ──────────────────────────────────────
echo "[3/7] OnnxRuntime-GPU..."
if ! $PYTHON -c "import onnxruntime; p = onnxruntime.get_device(); assert p == 'GPU'" 2>/dev/null; then
    ORT_URL="https://nvidia.box.com/shared/static/zostg6agpy5b0oi7gu0gokf3qyynx1vb.whl"
    wget -q -O /tmp/ort_gpu.whl "$ORT_URL"
    $PIP /tmp/ort_gpu.whl
fi
$PYTHON -c "import onnxruntime as ort; print(f'  ORT {ort.__version__}, providers: {ort.get_available_providers()}')"

# ── 4. TFLite runtime (for YAMNet) ──────────────────────────────────────────
echo "[4/7] TFLite runtime..."
if ! $PYTHON -c "import tflite_runtime" 2>/dev/null; then
    TFL_URL="https://github.com/google-coral/pycoral/releases/download/v2.0.0/tflite_runtime-2.5.0-cp38-cp38-linux_aarch64.whl"
    $PIP "$TFL_URL" 2>/dev/null || \
        $PIP "tflite-runtime" 2>/dev/null || \
        echo "  WARNING: tflite-runtime install failed — audio will be disabled"
fi

# ── 5. Python packages ───────────────────────────────────────────────────────
echo "[5/7] Python packages..."
$PIP ultralytics>=8.2.0
$PIP boxmot>=10.0.43
$PIP insightface>=0.7.3
$PIP faiss-cpu>=1.7.4
$PIP sounddevice>=0.4.6
$PIP paho-mqtt>=1.6.1
$PIP numpy>=1.24.0 opencv-python-headless>=4.8.0 huggingface_hub>=0.20.0
echo "  ✓ Python packages installed"

# ── 6. Export / download models ─────────────────────────────────────────────
echo "[6/7] Exporting models..."
mkdir -p models
$PYTHON export_models.py
echo "  ✓ Models ready"

# ── 7. Validate full pipeline (dry run) ─────────────────────────────────────
echo "[7/7] Dry-run validation..."
$PYTHON - <<'EOF'
import sys, os, numpy as np

errors = []

# YOLO
try:
    from ultralytics import YOLO
    m = YOLO("models/yolov9c.engine", task="detect")
    r = m(np.zeros((360,640,3), dtype="uint8"), device="cuda:0", verbose=False)
    print(f"  ✓ YOLOv9 TRT engine — boxes: {len(r[0].boxes)}")
except Exception as e:
    errors.append(f"YOLO: {e}")

# OnnxRuntime
try:
    import onnxruntime as ort
    print(f"  ✓ OnnxRuntime GPU ready — providers: {ort.get_available_providers()}")
except Exception as e:
    errors.append(f"ORT: {e}")

# Faiss
try:
    import faiss
    idx = faiss.IndexFlatIP(512)
    idx.add(np.zeros((1,512), dtype="float32"))
    print(f"  ✓ Faiss — {idx.ntotal} vector(s)")
except Exception as e:
    errors.append(f"Faiss: {e}")

# MQTT
try:
    import paho.mqtt.client as mqtt
    print("  ✓ paho-mqtt available")
except Exception as e:
    errors.append(f"MQTT: {e}")

# TFLite
try:
    import tflite_runtime.interpreter as tflite
    print("  ✓ TFLite runtime available")
except Exception as e:
    errors.append(f"TFLite: {e}")

# boxmot
try:
    from boxmot import ByteTrack
    t = ByteTrack()
    print("  ✓ ByteTrack ready")
except Exception as e:
    errors.append(f"ByteTrack: {e}")

if errors:
    print("\n  ⚠ Warnings (non-fatal — those components will be skipped):")
    for err in errors:
        print(f"    - {err}")
else:
    print("\n  ✓ All components validated")

print("\nSetup complete. Next steps:")
print("  1. Build anomaly baseline:  python build_patchcore.py --source rtsp://... --frames 500")
print("  2. Add watchlist faces:     python manage_watchlist.py add --name 'Alice' --image alice.jpg")
print("  3. Start pipeline:          python detect.py")
EOF

echo ""
echo "======================================================================"
echo " Setup done. Run: python detect.py"
echo " Stream:   http://<jetson-ip>:8080/stream"
echo " Alerts:   mosquitto_sub -t security/alerts"
echo "======================================================================"
