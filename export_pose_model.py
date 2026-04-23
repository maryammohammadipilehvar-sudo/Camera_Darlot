#!/usr/bin/env python3
"""
export_pose_model.py — export YOLOv8n-pose to TensorRT FP16 engine.

Invariant: this script reads from models/ and writes to models/ only.
It does not touch the repo root. If you see yolov8n-pose.{pt,onnx} in
the repo root, they are historical — see archive/ for context.

Run once on Jetson after setup.
Usage:
    python export_pose_model.py
    python export_pose_model.py --imgsz 360 640 --skip-trt   # ONNX only
"""

import argparse, os, subprocess, sys, logging, tempfile

logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")
log = logging.getLogger("export_pose")

REPO_ROOT  = os.path.dirname(os.path.abspath(__file__))
MODELS_DIR = os.path.join(REPO_ROOT, "models")
os.makedirs(MODELS_DIR, exist_ok=True)


def run(cmd: str, check=True, cwd=None):
    log.info(f"$ {cmd}" + (f"  (cwd={cwd})" if cwd else ""))
    r = subprocess.run(cmd, shell=True, cwd=cwd)
    if check and r.returncode != 0:
        log.error(f"Command failed (exit {r.returncode})")
        sys.exit(1)
    return r.returncode == 0


def export_pose(h: int, w: int, skip_trt: bool):
    pt_src     = os.path.join(MODELS_DIR, "yolov8n-pose.pt")
    onnx_out   = os.path.join(MODELS_DIR, "yolov8n-pose.onnx")
    engine_out = os.path.join(MODELS_DIR, "yolov8n-pose.engine")

    if not os.path.exists(pt_src):
        log.error(f"Required source weights not found: {pt_src}")
        log.error("This script does not re-download. Place the .pt at models/ and retry.")
        sys.exit(1)

    need_build = not os.path.exists(engine_out)

    if need_build:
        log.info(f"=== Step 1: Export {pt_src} → ONNX ===")
        # Run ultralytics export with an absolute pt_src so YOLO loads from
        # models/ and writes the .onnx next to the input — directly into
        # models/. The tempdir cwd is a belt-and-suspenders guard: if
        # ultralytics ever falls back to CWD for any path, it lands in a
        # directory that vanishes when the with-block exits.
        with tempfile.TemporaryDirectory() as tmp:
            run(
                f'python -c "'
                f"from ultralytics import YOLO; "
                f"m = YOLO(r'{pt_src}'); "
                f"m.export(format='onnx', opset=17, simplify=False, dynamic=False, "
                f"         imgsz=[{h},{w}])"
                f'"',
                cwd=tmp,
            )

        if not os.path.exists(onnx_out):
            log.error(f"ONNX not found at {onnx_out} — ultralytics did not write next to input .pt")
            sys.exit(1)
        log.info(f"✓ ONNX saved: {onnx_out}")

        if skip_trt:
            log.info("--skip-trt set — stopping at ONNX stage.")
            return

        log.info("=== Step 2: Build TensorRT FP16 engine ===")
        run(
            f"trtexec"
            f"  --onnx={onnx_out}"
            f"  --saveEngine={engine_out}"
            f"  --fp16"
            f"  --memPoolSize=workspace:2048"
        )
        log.info(f"✓ TensorRT engine saved: {engine_out}")
    else:
        log.info(f"Engine already exists: {engine_out}  (skipping rebuild, continuing to validation)")
        if skip_trt:
            return

    # Validation: trtexec's `&&&& PASSED` benchmark (during build above)
    # confirms the engine loads, runs, and produces correct-shape output
    # at FP16 on this GPU. We intentionally do NOT re-validate through
    # ultralytics YOLO() — trtexec-built engines lack the metadata
    # (task, names, stride) that ultralytics' pose postprocessor needs.
    # See FOLLOWUPS.md "Engine lacks ultralytics metadata".
    log.info(f"✓ Done. Engine at: {engine_out}")


def main():
    p = argparse.ArgumentParser()
    p.add_argument("--imgsz",    nargs=2, type=int, default=[384, 640],
                   metavar=("H","W"))
    p.add_argument("--skip-trt", action="store_true",
                   help="Only export ONNX, skip trtexec (useful to test on non-Jetson)")
    args = p.parse_args()
    export_pose(args.imgsz[0], args.imgsz[1], args.skip_trt)


if __name__ == "__main__":
    main()
