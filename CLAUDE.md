# Security Pipeline — Agent Instructions

## Project Identity
Production AI Security Pipeline v2 — real-time multi-model surveillance
running on Jetson AGX Orin 64GB. Integrates object detection, tracking,
pose/behavior analysis, face recognition, audio event detection, and
visual anomaly detection. Publishes alerts via MQTT, serves an MJPEG
stream and health endpoint, persists events to SQLite, and runs as a
systemd service.

## Pipeline Stack (end-to-end)
YOLOv9-C (TensorRT)
  → ByteTrack multi-object tracking
  → BehaviorAnalyzer (YOLOv8n-pose, TensorRT)
  → SCRFD face detection
  → AdaFace embeddings
  → Faiss 1:N identity search
  → YAMNet audio event classification
  → PatchCore visual anomaly
  → MQTT alerts (with dedup + per-kind cooldowns)

## Hardware & Runtime
- NVIDIA Jetson AGX Orin 64GB, aarch64
- JetPack 6.2 (L4T R36, Rev 5.0, build date 2026-01-16)
- CUDA 12.6 (nvcc 12.6.r12.6)
- Python 3.10.12
- Virtual env at .venv/ — always `source .venv/bin/activate` before Python work

## Key Files
- detect.py                        — Main pipeline (current production entry)
- detect_v2.py                     — Alternative/experimental entry; confirm which is canonical before editing
- behavior.py                      — Pose-based behavior analyzer (graceful degradation)
- build_patchcore.py               — Builds the PatchCore anomaly model
- export_pose_model.py             — Exports YOLOv8n-pose for TensorRT
- export_models.py                 — Exports detection/other models
- manage_watchlist.py              — Face-watchlist CRUD
- dashboard_server.py              — Web dashboard backend
- dashboard_static/                — Web dashboard frontend
- apply_upgrades.sh                — Upgrade runner
- setup.sh                         — Dependency bootstrap (read before suggesting pip installs)
- requirements.txt                 — Dependencies (Jetson-specific install order)
- security_pipeline.service        — systemd unit for the pipeline
- sentinel_dashboard.service       — systemd unit for the dashboard
- sentinel_events.db               — SQLite event store (do not rewrite schema without a migration plan)
- models/                          — Model weights (do not commit large files)
- yolov8n-pose.pt / .onnx          — Pose model artifacts
- _old_backups/                    — Archived old files, DO NOT TOUCH
- backups/                         — Existing project backup folder, treat as read-only

## Critical Jetson-Specific Dependency Rules
READ setup.sh BEFORE suggesting any pip/apt install. Several packages
MUST come from JetPack wheels, NOT from PyPI:
- onnxruntime-gpu      → JetPack wheel (PyPI x86_64 only, will break)
- torch                → JetPack wheel (PyPI version lacks CUDA on aarch64)
- torchvision          → JetPack wheel (must match torch)
- tflite-runtime       → tensorflow-jetson wheel
- faiss-cpu or faiss-gpu depending on JetPack availability

If the agent needs to install or upgrade any of these, STOP and ask
the user — wrong wheels silently break GPU acceleration.

## Known Incomplete / Disabled Areas
- Face detection path in detect.py — commented out, gated on ORT stability
- PatchCore anomaly path in detect.py — commented out until build_patchcore.py has been run and output integrated
- Unknown: whether detect.py or detect_v2.py is the intended production entry

## Build & Run
- Activate venv:       `source .venv/bin/activate`
- Run pipeline (dev):  `python3 detect.py`   (confirm with user which entry is canonical)
- Dashboard:           `python3 dashboard_server.py`
- Systemd (prod):      `sudo systemctl status security_pipeline sentinel_dashboard`
- Health check:        `curl http://localhost:8081/health`
- Logs (systemd):      `journalctl -u security_pipeline -f`
- Tests:               NONE YET — adding pytest is a valuable early task

## Coding Standards
- Python 3.10, PEP 8
- Type hints on all new or modified function signatures
- Google-style docstrings on every public function
- Use the `logging` module (already structured) — never `print()` for runtime logs
- Respect existing "Structured log fields (machine-parseable)" convention
- Config-driven: no hardcoded paths, thresholds, or topic names — put them in the config
- Thread-safe: this pipeline runs multiple threads (MQTT worker, thermal monitor,
  HTTP health, MJPEG annotate queue). New shared state needs locks or queues.

## STRICT RULES FOR THE AGENT
1. NEVER create .bak, .before_*, or .auto_fix_* files. Git is the backup.
   If a change is risky, commit the current state first, then edit.
2. NEVER commit to main. Only commit to agent/auto-dev or a feature branch.
3. NEVER run sudo without explicit approval in the conversation.
4. NEVER touch _old_backups/ or backups/ — treat as archival.
5. NEVER modify the systemd .service files without user approval.
6. NEVER modify sentinel_events.db schema without proposing a migration first.
7. NEVER install or upgrade the Jetson-specific packages listed above without asking.
8. ALWAYS run a syntax check after editing Python: `python3 -m py_compile <file>`.
9. ALWAYS verify the pipeline still imports after substantial edits:
   `python3 -c "import detect"` (or the relevant module).
10. PREFER small, reviewable commits over large ones.
11. When fixing a bug, add a regression test if reasonable.
12. If a task is ambiguous, ASK before guessing.
13. When disabled features are reactivated, put them behind a config toggle so
    they can be disabled again without code changes.

## First-Session Priorities (suggested order)
1. Decide canonical entry point: detect.py vs detect_v2.py — diff them, report.
2. Audit TODOs / FIXMEs / "disabled" comments across all .py files.
3. Introduce pytest scaffolding and tests for pure-logic helpers
   (e.g., _clean_behavior_label in detect.py, utilities in behavior.py).
4. Plan (do not execute yet) reactivation of face and PatchCore paths.
