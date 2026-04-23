#!/usr/bin/env python3
"""
export_models.py — run once on the Jetson to build all TensorRT engines.

Usage:
    python export_models.py [--yolo-weights yolov9c.pt] [--skip-scrfd] [--skip-adaface]
"""
import argparse, os, subprocess, sys, logging

logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")
log = logging.getLogger("export")

MODELS_DIR = "models"
os.makedirs(MODELS_DIR, exist_ok=True)


def run(cmd: str, check=True):
    log.info(f"$ {cmd}")
    result = subprocess.run(cmd, shell=True, capture_output=False)
    if check and result.returncode != 0:
        log.error(f"Command failed (exit {result.returncode})")
        sys.exit(1)
    return result.returncode == 0


# ─── 1. YOLOv9-C → TensorRT FP16 engine ──────────────────────────────────────
def export_yolo(weights: str):
    engine_out = os.path.join(MODELS_DIR, "yolov9c.engine")
    if os.path.exists(engine_out):
        log.info(f"Engine already exists: {engine_out}")
        return

    log.info("=== Exporting YOLOv9-C → ONNX → TensorRT FP16 ===")

    # Download weights if not present
    if not os.path.exists(weights):
        log.info(f"Downloading {weights}...")
        run(f"python -c \""
            f"from ultralytics import YOLO; YOLO('yolov9c.pt')\"")
        weights = "yolov9c.pt"

    # Export to ONNX via ultralytics
    run(f"python -c \""
        f"from ultralytics import YOLO; "
        f"m = YOLO('{weights}'); "
        f"m.export(format='onnx', opset=17, simplify=True, dynamic=False, "
        f"         imgsz=[360,640])\"")

    onnx_path = weights.replace(".pt", ".onnx")

    # Build TensorRT engine (FP16)
    run(f"trtexec "
        f"  --onnx={onnx_path} "
        f"  --saveEngine={engine_out} "
        f"  --fp16 "
        f"  --workspace=4096 "
        f"  --minShapes=images:1x3x360x640 "
        f"  --optShapes=images:1x3x360x640 "
        f"  --maxShapes=images:1x3x360x640")

    log.info(f"✓ YOLOv9-C engine saved: {engine_out}")

    # Validate
    run(f"python -c \""
        f"from ultralytics import YOLO; import numpy as np; "
        f"m = YOLO('{engine_out}', task='detect'); "
        f"r = m(np.zeros((360,640,3), dtype='uint8'), device='cuda:0', verbose=False); "
        f"print('YOLOv9 engine OK, boxes:', len(r[0].boxes))\"")


# ─── 2. SCRFD-10G face detector (ONNX — runs via OnnxRuntime-GPU) ─────────────
def download_scrfd():
    out = os.path.join(MODELS_DIR, "scrfd_10g.onnx")
    if os.path.exists(out):
        log.info(f"SCRFD already present: {out}")
        return
    log.info("=== Downloading SCRFD-10G ===")
    run(f"pip install -q insightface onnxruntime-gpu")
    run(f"python -c \""
        f"from insightface.model_zoo import get_model; "
        f"import shutil; "
        f"m = get_model('scrfd_10g_bnkps', download=True, download_zip=True); "
        f"src = m.model_file; "
        f"import shutil; shutil.copy(src, '{out}'); "
        f"print('SCRFD copied to', '{out}')\"")
    log.info(f"✓ SCRFD saved: {out}")


# ─── 3. AdaFace IR-50 ONNX ────────────────────────────────────────────────────
def download_adaface():
    out = os.path.join(MODELS_DIR, "adaface_ir50.onnx")
    if os.path.exists(out):
        log.info(f"AdaFace already present: {out}")
        return
    log.info("=== Downloading AdaFace IR-50 ONNX ===")
    # HuggingFace mirror — adjust if network restricted
    run(f"pip install -q huggingface_hub")
    run(f"python -c \""
        f"from huggingface_hub import hf_hub_download; "
        f"p = hf_hub_download('hbldh/adaface-ir50-ms1mv2', 'adaface_ir50_ms1mv2.onnx'); "
        f"import shutil; shutil.copy(p, '{out}'); "
        f"print('AdaFace copied to', '{out}')\"")
    log.info(f"✓ AdaFace saved: {out}")


# ─── 4. YAMNet TFLite + class CSV ─────────────────────────────────────────────
def download_yamnet():
    out     = os.path.join(MODELS_DIR, "yamnet.tflite")
    out_csv = os.path.join(MODELS_DIR, "yamnet_classes.csv")
    if os.path.exists(out) and os.path.exists(out_csv):
        log.info("YAMNet already present")
        return
    log.info("=== Downloading YAMNet TFLite ===")
    run(f"wget -q -O {out} "
        f"https://storage.googleapis.com/download.tensorflow.org/models/tflite/task_library/audio_classification/android/yamnet_1.tflite")
    run(f"wget -q -O {out_csv} "
        f"https://raw.githubusercontent.com/tensorflow/models/master/research/audioset/yamnet/yamnet_class_map.csv")
    log.info(f"✓ YAMNet saved: {out}")


# ─── 5. Validate all models ───────────────────────────────────────────────────
def validate_all():
    log.info("=== Validating model files ===")
    required = [
        ("models/yolov9c.engine", "YOLOv9 TensorRT engine"),
        ("models/scrfd_10g.onnx", "SCRFD face detector"),
        ("models/adaface_ir50.onnx", "AdaFace embedder"),
        ("models/yamnet.tflite",  "YAMNet audio"),
    ]
    all_ok = True
    for path, name in required:
        exists = os.path.exists(path)
        status = "✓" if exists else "✗ MISSING"
        log.info(f"  {status}  {name}: {path}")
        if not exists:
            all_ok = False
    if all_ok:
        log.info("All models ready.")
    else:
        log.warning("Some models missing — check errors above.")


def main():
    p = argparse.ArgumentParser()
    p.add_argument("--yolo-weights", default="yolov9c.pt")
    p.add_argument("--skip-scrfd",    action="store_true")
    p.add_argument("--skip-adaface",  action="store_true")
    p.add_argument("--skip-yamnet",   action="store_true")
    args = p.parse_args()

    export_yolo(args.yolo_weights)
    if not args.skip_scrfd:    download_scrfd()
    if not args.skip_adaface:  download_adaface()
    if not args.skip_yamnet:   download_yamnet()
    validate_all()

if __name__ == "__main__":
    main()
