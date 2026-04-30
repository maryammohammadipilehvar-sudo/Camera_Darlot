# Pose model artifact archive — 2026-04-23

Historical pose-model artifacts moved here on 2026-04-23 as part of the
Q7 cleanup (rebuild pose TRT engine; patch `export_pose_model.py` to
stop writing repo-root artifacts).

## Contents

- `yolov8n-pose.pt` — Root-level duplicate of `models/yolov8n-pose.pt`.
  MD5 verified byte-identical to the in-tree copy at archive time
  (`fce9c3a495cc42f597c8191798b1445b`). Originated from
  `export_pose_model.py` invoking ultralytics with a bare filename in
  the repo root CWD, which ultralytics then cached/downloaded there.

- `yolov8n-pose.onnx` — Root-level duplicate of `models/yolov8n-pose.onnx`.
  MD5 verified byte-identical at archive time
  (`9527df96fb6e2ff86cc5501ba568ca17`). Same CWD-pollution origin.

- `yolov8n-pose.engine.prev` — Previous TensorRT FP16 engine built on
  2026-04-17 12:23. Superseded by the fresh rebuild in Q7. Retained for
  rollback in case the new engine regresses.

## Why keep these instead of deleting

- The `.pt` / `.onnx` duplicates are byte-identical to canonical
  `models/` copies, but relocating (rather than deleting) preserves
  their timestamps and lets us confirm provenance if a question arises.
- The `.engine.prev` is the only filesystem rollback path for the
  engine build without rerunning `export_pose_model.py`.

## Restore procedure

Engine rollback (from repo root):

    cp archive/pose_model_20260423/yolov8n-pose.engine.prev \
       models/yolov8n-pose.engine

The `.pt` / `.onnx` files should not be restored to the repo root —
the patched `export_pose_model.py` reads from `models/` via an
absolute path and writes outputs only to `models/`. Restoring the
root-level copies would only reintroduce the pollution problem.

## .gitignore note

Model binary artifacts (`*.pt`, `*.onnx`, `*.engine`) in this folder
are git-ignored (ignore patterns live in the repo-root `.gitignore`).
Only this README is tracked, to keep durable context in git history
without bloating the repo with binary blobs.
