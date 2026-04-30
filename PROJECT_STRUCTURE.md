# Project Structure

This document maps every file in the repo to its role. For operational
docs — setup, running, observability — see `README.md`. For the full
code audit (entry-point resolution, pipeline map, risks, test surface)
see `AUDIT.md`.

Dead, historical, or read-only paths are marked inline. Anything
tagged `DEAD`, `HISTORICAL`, or `read-only per CLAUDE rule #4` should
not be edited — they exist for provenance, not for use.

## Annotated tree

```
security_pipeline/
├── detect.py                       ── canonical production entry; runs the main pipeline loop
├── detect_v2.py                    ── DEAD: older snapshot pre-autoclean patch (see AUDIT §1)
├── behavior.py                     ── pose-based behavior analyzer; spawned as a thread by detect.py
├── dashboard_server.py             ── FastAPI dashboard backend; MQTT → SQLite → WebSocket bridge
├── build_patchcore.py              ── offline script: build the PatchCore anomaly memory bank from normal footage
├── export_pose_model.py            ── offline script: export YOLOv8n-pose → ONNX → TRT engine into models/
├── export_models.py                ── offline script: build all TRT engines (YOLOv9, SCRFD, AdaFace, YAMNet)
├── manage_watchlist.py             ── CLI: add / add-dir / list / remove entries in the Faiss watchlist
├── apply_upgrades.sh               ── HISTORICAL: one-shot v2 upgrade runner; already applied, do not re-run
├── setup.sh                        ── bootstrap: apt deps, JetPack wheels, PyPI deps, model export, dry-run validation
├── requirements.txt                ── dependency manifest; see setup.sh for Jetson-specific install order
├── security_pipeline.service       ── systemd unit → runs detect.py (MJPEG :8080, /health :8081)
├── sentinel_dashboard.service      ── systemd unit → runs dashboard_server.py (dashboard :8888)
├── sentinel_events.db              ── SQLite event store; schema in dashboard_server.py:init_db (line 62)
├── index.html                      ── DEAD: leftover from the v2 upgrade source set; the live dashboard page is dashboard_static/index.html
├── CLAUDE.md                       ── agent instructions + strict rules (apply to humans too)
├── AUDIT.md                        ── 2026-04-23 codebase audit: entry point, pipeline map, disabled features, risk register, test surface
├── FOLLOWUPS.md                    ── deferred work items: paho VERSION2, boxmot ByteTrack break, engine metadata, export PATH fragility
├── README.md                       ── project entry-point doc (front door for new contributors)
├── PROJECT_STRUCTURE.md            ── this file
├── .gitignore                      ── ignores .venv/, model binaries (*.pt/*.onnx/*.engine), .bak/.before_*/.auto_fix_* patterns, backups/
├── models/                         ── model weights (.pt, .onnx, .engine, .tflite); git-ignored, produced by export_models.py / export_pose_model.py
├── dashboard_static/               ── dashboard frontend assets (index.html, images/) served by dashboard_server.py
├── archive/                        ── durable archives with per-folder READMEs; git-tracked for provenance
│   └── pose_model_20260423/        ── archived pose artifacts + rollback engine from the Q7 cleanup; see its README.md
├── backups/                        ── HISTORICAL: pre-v2 snapshot (20260417_115133/); read-only per CLAUDE rule #4
├── _old_backups/                   ── HISTORICAL: .bak/.before_*/.auto_fix_* files from earlier iterations; read-only per CLAUDE rule #4
│   └── =0.1.71                     ── DEAD: stray file from a botched `pip install` argument (version spec got written as a filename)
├── upgrade/                        ── HISTORICAL: source files apply_upgrades.sh copied into place; kept for reference only
├── .venv/                          ── Python 3.10.12 virtual environment; git-ignored
└── __pycache__/                    ── Python bytecode cache; git-ignored
```

## Where to look when…

- **How does detection work?** → `detect.py:run()` (main loop, ~line 750 onward).
- **How are alerts emitted?** → `detect.py:emit_alert` (line 308) pushes to `_alert_q`; drained by `_mqtt_worker` thread (line 253).
- **How is behavior classified?** → `behavior.py:ActionClassifier.classify` (line 110); next-action prediction uses `_TRANSITIONS` (line 61) via `predict_next` (line 72).
- **How does the MJPEG stream work?** → `detect.py:_FrameBus` (line 144) holds the latest JPEG; `_MJPEGHandler` (line 164) serves `multipart/x-mixed-replace`; `start_mjpeg` (line 187) binds the thread.
- **How does `/health` work?** → `detect.py:_HealthHandler` (line 195) returns the shared `_health` dict as JSON; `start_health` (line 211) binds it; `_hset` (line 137) is the thread-safe mutator.
- **Where are behavior labels drawn?** → `detect.py:draw_tracks` (line 640) renders boxes + cleaned behavior strings; `behavior.py:draw_behavior` (line 465) renders the skeleton overlay.
- **Where are model weights?** → `models/` (git-ignored). Pose-artifact provenance: `archive/pose_model_20260423/README.md`.
- **Where's the web UI?** → frontend in `dashboard_static/index.html` + `dashboard_static/images/`; backend in `dashboard_server.py`.
- **Which systemd units run what?** → `security_pipeline.service` runs `detect.py`; `sentinel_dashboard.service` runs `dashboard_server.py` and declares `Requires=security_pipeline.service`.
- **What events exist in SQLite?** → schema in `dashboard_server.py:init_db` (line 62); UI-side formatting in `format_event_for_ui` (line 173).
- **How is the watchlist managed?** → `manage_watchlist.py` (`add` / `add-dir` / `list` / `remove`); index persisted at `watchlist.index` with metadata in `watchlist_meta.json`.
- **How is the anomaly memory bank built?** → `build_patchcore.py` samples normal footage and writes `models/patchcore_memory.pt` (not yet wired into `detect.py:run()`).
- **Why are face / audio / anomaly off?** → `AUDIT.md` §3 (each has a dedicated re-enable checklist).
- **What's deferred right now?** → `FOLLOWUPS.md` covers the paho VERSION2 migration, the `boxmot` `ByteTrack` import break (active production issue), the ultralytics engine-metadata gap, and the `export_pose_model.py` subprocess PATH fragility.
- **What rules govern edits to this repo?** → `CLAUDE.md` "STRICT RULES FOR THE AGENT" (items 1–13); they apply to human contributors too.
