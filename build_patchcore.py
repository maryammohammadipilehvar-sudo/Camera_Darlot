#!/usr/bin/env python3
"""
build_patchcore.py — collect normal-scene patches and save memory bank.

Run BEFORE deploying. Point at ~10 minutes of "normal" footage for a scene.

Usage:
    python build_patchcore.py --source rtsp://... --frames 500
    python build_patchcore.py --source normal_clip.mp4 --frames 500
"""
import argparse, os, logging
import cv2, numpy as np

logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")
log = logging.getLogger("patchcore_build")

IMAGENET_MEAN = np.array([0.485, 0.456, 0.406], dtype=np.float32)
IMAGENET_STD  = np.array([0.229, 0.224, 0.225], dtype=np.float32)

PATCH_SIZE = 16
PATCH_STRIDE = 8
IMG_SIZE = 224
SUBSAMPLE = 0.1   # keep 10 % of patches to cap memory


def extract_patches(frame_bgr: np.ndarray) -> np.ndarray:
    img = cv2.resize(frame_bgr, (IMG_SIZE, IMG_SIZE))
    img = cv2.cvtColor(img, cv2.COLOR_BGR2RGB).astype(np.float32) / 255.0
    img = (img - IMAGENET_MEAN) / IMAGENET_STD   # (H,W,3)

    patches = []
    for y in range(0, IMG_SIZE - PATCH_SIZE + 1, PATCH_STRIDE):
        for x in range(0, IMG_SIZE - PATCH_SIZE + 1, PATCH_STRIDE):
            p = img[y:y+PATCH_SIZE, x:x+PATCH_SIZE].flatten()   # 3*16*16=768
            patches.append(p)

    patches = np.array(patches, dtype=np.float32)   # (N_patches, 768)
    # L2-normalise each patch
    norms = np.linalg.norm(patches, axis=1, keepdims=True) + 1e-6
    return patches / norms


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--source", required=True, help="RTSP URL or video file")
    parser.add_argument("--frames", type=int, default=500, help="Number of frames to sample")
    parser.add_argument("--out",    default="models/patchcore_memory.pt")
    args = parser.parse_args()

    os.environ["OPENCV_FFMPEG_CAPTURE_OPTIONS"] = "rtsp_transport;tcp|stimeout;5000000"
    cap = cv2.VideoCapture(args.source, cv2.CAP_FFMPEG)
    if not cap.isOpened():
        log.error(f"Cannot open: {args.source}"); return

    total   = int(cap.get(cv2.CAP_PROP_FRAME_COUNT)) or 999999
    step    = max(1, total // args.frames)
    bank    = []
    sampled = 0
    n       = 0

    log.info(f"Sampling {args.frames} frames from '{args.source}'...")

    while sampled < args.frames:
        ret, frame = cap.read()
        if not ret: break
        n += 1
        if n % step != 0: continue

        patches = extract_patches(frame)
        # random subsample to keep memory manageable
        idx = np.random.choice(len(patches),
                               max(1, int(len(patches) * SUBSAMPLE)),
                               replace=False)
        bank.append(patches[idx])
        sampled += 1

        if sampled % 50 == 0:
            log.info(f"  Sampled {sampled}/{args.frames} frames…")

    cap.release()

    if not bank:
        log.error("No frames collected."); return

    memory = np.vstack(bank)   # (total_patches, 768)
    log.info(f"Memory bank: {memory.shape[0]} patches × {memory.shape[1]} dims")

    # Greedy coreset subsampling — keep at most 10 k patches
    MAX_PATCHES = 10_000
    if memory.shape[0] > MAX_PATCHES:
        log.info(f"Subsampling to {MAX_PATCHES} coreset patches…")
        idx = np.random.choice(memory.shape[0], MAX_PATCHES, replace=False)
        memory = memory[idx]

    import torch
    os.makedirs(os.path.dirname(args.out) or ".", exist_ok=True)
    torch.save({
        "memory_bank": torch.from_numpy(memory),
        "mean":        IMAGENET_MEAN,
        "std":         IMAGENET_STD,
        "patch_size":  PATCH_SIZE,
        "patch_stride":PATCH_STRIDE,
    }, args.out)

    log.info(f"✓ PatchCore memory bank saved: {args.out}")
    log.info("  Next step: run detect.py — anomaly detection is now active.")


if __name__ == "__main__":
    main()
