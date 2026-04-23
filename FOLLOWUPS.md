# Follow-ups

Deferred work captured from audits and verification runs. Items here are
intentionally not blocking the current change; each entry notes urgency and
sufficient context to pick up in a future session.

### Migrate MQTT callbacks to paho CallbackAPIVersion.VERSION2

- Current state: using VERSION1 compat (paho 2.x deprecation warning)
- Affected: `detect.py` `_on_connect`, `dashboard_server.py` `_on_connect`
- VERSION2 signature: `on_connect(client, userdata, flags, reason_code, properties)` — `reason_code` replaces `rc`, `properties` added
- Low urgency; do before paho 3.x release.

### ByteTrack import broken in current boxmot — RESOLVED (2026-04-23, `5a1b935`)

- Observed in Q4 foreground run: `cannot import name 'ByteTrack' from 'boxmot'`
- Effect: multi-object tracking is disabled in production right now; Risk #3 (zone loitering track_id=0 collapse) is active, not theoretical
- Current boxmot in `.venv`: run `pip show boxmot` to confirm version
- boxmot restructured tracker imports in recent versions; likely need `from boxmot.trackers.bytetrack.bytetrack import ByteTrack` or similar
- High urgency — tracking underpins behavior analysis, zone dwell, and alert dedup
- Candidate for next session after Q4/Q7 wrap up
- **Resolution:** Session 2. Switched to `from boxmot.trackers.bytetrack.bytetrack import ByteTrack`; split init into import vs construction try blocks with loud `TRACKING DISABLED` logging. Pinned `boxmot>=10.0.43,<18.1` in requirements.txt. Verified live: `track_id=1` persisted across 41 frames with evolving coordinates.

### Engine lacks ultralytics metadata

- `trtexec` confirms the engine is valid at the hardware level: 188 qps throughput, correct output shape `1×56×5040`, `&&&& PASSED` benchmark during the Q7 rebuild on 2026-04-23.
- The engine **cannot currently be loaded via the ultralytics `YOLO()` wrapper** because it is missing embedded metadata (task, names, stride, etc.). Loading via `YOLO('models/yolov8n-pose.engine', task='pose')` and invoking inference fails inside ultralytics NMS postprocessing with `RuntimeError: Trying to create tensor with negative dimension`.
- If/when we flip `cfg["pose_engine"]` from `.pt` to `.engine`, we'll need to either:
  - (a) export via `m.export(format='engine', ...)` instead of the `trtexec` path (produces an ultralytics-compatible engine with metadata embedded), or
  - (b) load the engine via `tensorrt + pycuda` (or `cuda-python`) directly, bypassing the ultralytics wrapper.
- Current status: engine is a build artifact, not in production. `cfg["pose_engine"]` still points at `models/yolov8n-pose.pt` (`detect.py:64`). Not blocking.

### `export_pose_model.py` subprocess uses bare `python`

- Observed while wiring up Q7: the subprocess commands in `export_pose_model.py` invoke `python -c "..."` with the bare interpreter name, relying on `.venv/bin` being first on `PATH`.
- Works only when the venv is activated at call time. In other contexts (a tool running the script with a different PATH, `systemd-run`, cron) this could resolve to a different Python or fail.
- Fix: switch to `sys.executable` (the absolute path to the same Python running the parent script) or explicit `python3`.
- Low urgency; expected invocation is always `source .venv/bin/activate && python export_pose_model.py`.
