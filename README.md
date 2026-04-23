# Security Pipeline

Production AI surveillance for a single RTSP camera, running on a Jetson
AGX Orin. It fuses object detection, multi-object tracking, pose-based
behavior analysis, face recognition, audio event classification, and
visual anomaly scoring into a single real-time stream, publishes alerts
over MQTT, serves an MJPEG preview and a JSON `/health` endpoint, and
persists every event to a SQLite store that backs a web dashboard.

## Hardware target

This is a fixed-target deployment — the entire stack is tuned for one
machine and will not run unmodified anywhere else.

- NVIDIA Jetson AGX Orin 64GB (aarch64)
- JetPack 6.2 (L4T R36 Rev 5.0, build date 2026-01-16)
- CUDA 12.6 (`nvcc 12.6.r12.6`)
- Python 3.10.12 inside the repo-local `.venv/`

Several of the critical dependencies (PyTorch, torchvision,
onnxruntime-gpu, tflite-runtime, faiss) must come from NVIDIA's JetPack
wheels, not PyPI. The PyPI versions either lack CUDA support on
aarch64 or are x86_64-only, and installing them silently disables GPU
acceleration. See *Detailed setup* below before touching dependencies.

## Architecture

```
RTSP camera ──► capture ──► resize ──► YOLOv9-C (TRT) ──► ByteTrack
                                            │                │
                                            ▼                ▼
                                      class filter      zones / dwell
                                            │                │
                                            └──► BehaviorAnalyzer (pose)
                                                             │
                                                 ┌───────────┼───────────┐
                                                 ▼           ▼           ▼
                                              [face]*    [anomaly]*   [audio]*
                                                 │           │           │
                                                 └──── emit_alert ──────┘
                                                             │
                                              ┌──────────────┼──────────────┐
                                              ▼              ▼              ▼
                                          MQTT queue    annotate/MJPEG   SQLite
                                          (worker)      (port 8080)      (dashboard)
                                                                         + /health :8081
```

Stages marked `*` are currently disabled in production. See *Current
capabilities vs disabled features* below for the status of each, and
`AUDIT.md` §3 for the re-enable checklists.

## Quick start

From a fresh clone, with the Jetson already flashed to JetPack 6.2:

```
bash setup.sh
source .venv/bin/activate
python detect.py
```

Then open `http://<jetson-ip>:8080/stream` in a browser to confirm the
pipeline is live, and `http://<jetson-ip>:8081/health` for a JSON
status snapshot.

If you want the full dashboard UI, run `python dashboard_server.py` in
a second terminal and browse to `http://<jetson-ip>:8888`.

## Detailed setup

`setup.sh` is the source of truth for first-time install. It handles
apt packages, the Mosquitto MQTT broker, the JetPack-wheel installs
for PyTorch / onnxruntime-gpu / tflite-runtime, the PyPI packages, and
a dry-run validation at the end. Read it before running — it does use
`sudo` for the apt and systemd steps.

Model exports are a separate, slow, one-time step:

```
python export_models.py          # 30+ minutes; builds all TRT engines
```

This downloads YOLOv9-C, SCRFD, AdaFace, and YAMNet, and runs `trtexec`
to produce `models/yolov9c.engine`. It only needs to run once per
machine (or after a model version bump). See `export_pose_model.py`
for the pose-specific export path — it is run separately to keep the
pose artifacts under `models/` only.

### JetPack-wheel hazards

The following packages **must not** come from PyPI. `setup.sh` handles
them correctly; if you ever install or upgrade one by hand, stop and
verify the source first.

- `torch` — JetPack wheel; PyPI version lacks CUDA on aarch64
- `torchvision` — JetPack wheel; must match the `torch` version
- `onnxruntime-gpu` — JetPack wheel; PyPI is x86_64-only
- `tflite-runtime` — TensorFlow-Jetson wheel
- `faiss-cpu` (or `faiss-gpu` if available for your JetPack)

`paho-mqtt` is pinned to `>=2.1,<3` in `requirements.txt`. The code
uses `mqtt.CallbackAPIVersion.VERSION1` for compatibility with the
existing v1-style callback signatures; the planned migration to
VERSION2 is tracked in `FOLLOWUPS.md`.

## Running it

### Development

Activate the venv and run `detect.py` in the foreground:

```
source .venv/bin/activate
python detect.py
```

Logs stream to the terminal. Ctrl-C triggers a graceful shutdown —
the signal handler flips the `running` flag, daemon threads (MQTT,
MJPEG, health, thermal, behavior) wind down with the process, and a
final stats log line is emitted.

### Production (systemd)

Two units run the production stack:

- `security_pipeline.service` — runs `detect.py` as user `quarero`
  from the repo-local venv. Binds MJPEG on `:8080` and `/health` on
  `:8081`. `Restart=always` with a 5-second backoff.
- `sentinel_dashboard.service` — runs `dashboard_server.py` on
  `:8888`. Declares `Requires=security_pipeline.service`, so it only
  comes up once the pipeline is running.

Both are under the user's control via `systemctl`:

```
sudo systemctl status security_pipeline sentinel_dashboard
sudo systemctl restart security_pipeline
sudo systemctl restart sentinel_dashboard
```

The `.service` files are committed to the repo but **are not modified
by the agent**. Any change to a unit file requires explicit approval
(CLAUDE.md rule #5).

## Observability

| Surface | Where | What it gives you |
|---|---|---|
| Health endpoint | `http://<host>:8081/health` | JSON: status, MQTT up, camera up, fps, frames_total, alerts_total, thermal_c, behavior_ready, uptime_s |
| MJPEG stream | `http://<host>:8080/stream` | Annotated live preview (boxes, track IDs, behavior labels, HUD) |
| Dashboard UI | `http://<host>:8888` | Event timeline, live stream proxy, alert filters |
| MQTT topic | `security/alerts` | Deduplicated, cooldown-gated alert JSON |
| Event store | `sentinel_events.db` (SQLite) | All events; schema defined in `dashboard_server.py:init_db` |

Live logs for either service:

```
journalctl -u security_pipeline  -f
journalctl -u sentinel_dashboard -f
```

## Configuration

Runtime configuration lives in the `CFG` dict at the top of `detect.py`
(lines 58–110). There is no separate config file — edit the dict
and restart the process. Per CLAUDE.md, there should be no hardcoded
paths, thresholds, or topic names outside `CFG`.

The toggles most worth knowing about:

- `rtsp_url`, `camera_id` — input source and identifier
- `yolo_conf`, `face_conf`, `audio_conf`, `anomaly_thresh` — per-stage
  confidence / threshold gates
- `inference_fps`, `resize_w`, `resize_h` — main-loop pacing and
  pre-resize dimensions
- `face_every_n`, `behavior_every_n`, `anomaly_every_n` — run the
  heavier stages every Nth frame (behavior is on; face and anomaly are
  currently gated to `999999`, i.e. never)
- `alert_cooldown_sec`, `object_ttl_sec`, `summary_min_gap` — alert
  dedup parameters
- `zones` — polygon list for loitering detection with per-zone
  `dwell_sec`
- `mqtt_host`, `mqtt_port`, `mqtt_topic` — broker coordinates
- `thermal_warn_c`, `thermal_crit_c` — Jetson thermal alert thresholds

When you re-enable a currently disabled feature, put it behind a
dedicated config toggle so it can be turned off again without code
changes (CLAUDE.md rule #13).

## Current capabilities vs disabled features

A snapshot of what the pipeline actually does today. Full details and
re-enable checklists live in `AUDIT.md` §3.

| Stage | Status | Notes |
|---|---|---|
| Object detection (YOLOv9-C) | ✅ Running | Currently via the `.pt` runtime, not the `.engine`; switching to TRT is pending (FOLLOWUPS.md "Engine lacks ultralytics metadata"). |
| Multi-object tracking (ByteTrack) | ⚠️ Broken | Live `ImportError` from the installed `boxmot` version (FOLLOWUPS.md #2). Pipeline runs, but `track_id` collapses to `0` for every object, which degrades behavior analysis, zone dwell accuracy, and alert deduplication (AUDIT.md Risk #3). |
| Behavior / pose analysis | ✅ Running | YOLOv8n-pose on the PyTorch path; TRT engine exists but is not yet wired (FOLLOWUPS.md). |
| Zone dwell / loitering | ✅ Running | Affected by the tracking break above. |
| MQTT alerts, MJPEG, `/health`, thermal, SQLite | ✅ Running | The alerting, observability, and persistence surfaces all work. |
| Face recognition (SCRFD + AdaFace + Faiss) | ❌ Disabled | Hard-coded `None` in `detect.py:710–712`, main-loop branch commented out. Gated on ORT stability on this Jetson. |
| Visual anomaly (PatchCore) | ❌ Disabled | Memory bank not yet built via `build_patchcore.py`; run-loop branch is a placeholder. |
| Audio events (YAMNet) | ❌ Disabled | `start_audio_thread` is defined but never called from `run()`; `CFG` is missing its required keys. |

The practical consequence: detection, tracking (degraded), behavior,
zones, and alerting are live. Face ID, anomaly, and audio are not.
Treat the three disabled rows as known gaps, not regressions.

## Related docs

- `CLAUDE.md` — agent instructions and the strict rules that govern
  automated edits to this repo.
- `AUDIT.md` — full codebase audit as of 2026-04-23, covering entry
  point resolution, the pipeline map, disabled features with
  re-enable steps, a risk register, and a recommended test surface.
- `FOLLOWUPS.md` — deferred work items with context: paho v2
  migration, boxmot `ByteTrack` import break, ultralytics engine
  metadata, `export_pose_model.py` subprocess PATH fragility.
- `PROJECT_STRUCTURE.md` — every file in the repo mapped to its role,
  with a "where to look when…" navigation index.
- `archive/pose_model_20260423/README.md` — pose-artifact provenance
  after the Q7 cleanup that stopped `export_pose_model.py` from
  polluting the repo root.

## Contributing

Branch strategy:

- Work on `agent/auto-dev` or a feature branch. Never commit directly
  to `main`.
- Prefer small, reviewable commits over large ones. When a change is
  risky, commit the current state first, then edit.

The strict rules in CLAUDE.md apply to human contributors too, not
just automated agents. The short version:

- Never create `.bak`, `.before_*`, or `.auto_fix_*` files — git is
  the backup. Old ones live in `_old_backups/` and `backups/` and are
  archival / read-only.
- Never install or upgrade the JetPack-wheel packages listed above
  without confirming the wheel source first.
- Never modify the systemd `.service` files or the
  `sentinel_events.db` schema without approval / a migration plan.
- After editing any Python file, run `python3 -m py_compile <file>`
  and, for substantial edits, `python3 -c "import detect"` (or the
  relevant module) to catch import-time breakage.
- When re-enabling a disabled feature, put it behind a config toggle.

## License

License not yet assigned. Until one is added, default copyright law
applies — external use requires explicit permission from the author.
