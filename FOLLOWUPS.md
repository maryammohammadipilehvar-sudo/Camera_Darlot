# Follow-ups

Deferred work captured from audits and verification runs. Items here are
intentionally not blocking the current change; each entry notes urgency and
sufficient context to pick up in a future session.

### Build real remote notification channel (HIGH)

The pipeline emits MQTT to `security/alerts` but no verified subscriber
exists. For an autonomous deployment this is THE critical gap: detection
without notification means the system tells no one.

Recommended starting channel: **ntfy.sh** (free, self-hostable, 30-minute
setup, phone push via app) **or** Telegram bot (free, supports rich media,
trusted platform, 1-2 hour setup).

NOT starting with: SMS/voice (Twilio cost + complexity), email (spam-filter
unreliability), mobile app (too much work).

This is the Session 4 candidate.

### Revisit modal action buttons for autonomous-first persona (MEDIUM)

Session 3 designed Acknowledge/Escalate around a security-guard workflow
that doesn't exist in the real product. The dashboard is an admin review
console, not a live monitoring surface. Candidate replacement actions:
"Mark as false alarm" (ML feedback), "Acknowledge" (admin review flag),
possibly remove Escalate entirely. Wait until the notification channel is
in place and real admin workflow is observed before redesigning.

### Multi-site architecture (MEDIUM)

Current system is single-camera, single-site. Real deployment target is
multi-site warehouses, which implies per-site config (cameras, contacts,
timezones), site-scoped admin access, and notification routing by site.
Not blocking current work, but every design decision from here should
ask "does this scale to 10 sites with different customers?"

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

### Daemon threads use `while True:` without shared stop-event (HIGH)

- Observed Session 2: `systemctl stop security_pipeline` ends in "failed" (SIGKILL after grace-period) rather than clean "inactive".
- Root cause: `thermal_monitor` (`detect.py:229`) and `_mqtt_worker` (at least) use `while True:` loops that don't observe the main loop's `running` flag. On SIGTERM, main exits cleanly, but daemon threads continue until systemd's SIGKILL.
- Related to AUDIT Risk #9 (`_mqtt_worker` initial-connect loop has no shutdown visibility either).
- Fix: introduce a shared `threading.Event` stop flag; each background `while`-loop checks it (e.g., `while not stop.is_set():`). Drain alert queue on shutdown.
- High urgency — clean shutdown is a prerequisite for reliable systemd restarts and for not losing in-flight alerts.

### SIGTERM handler non-responsive within systemd grace period (MEDIUM)

- Observed Session 2 during service stop: `security_pipeline` shows "failed" post-stop because systemd SIGKILL'd it after SIGTERM timed out.
- Signal handler at `signal.signal(SIGINT, SIGTERM → _stop)` flips `running=False`, but by the time any long-running call in the main loop (YOLO inference, `cam.read()` blocking on RTSP, `tracker.update`) returns, grace period may have elapsed.
- Investigation needed: what's the systemd default `TimeoutStopSec` for this unit, and which call in the main loop is holding the process past that limit?
- Likely related to the daemon-thread item above — full fix may be combined (stop-event + shorter blocking operations in main loop + raise systemd `TimeoutStopSec` if needed).
- Medium urgency — "failed" exit status pollutes `systemctl status` and complicates ops runbooks.

### Silent-degradation audit of remaining `except Exception` blocks in detect.py (MEDIUM)

- After Session 2's Fix A treatment of `_init_bytetrack`, the rest of detect.py still contains bare `except Exception` / `except Exception: pass` patterns that silently swallow failures.
- Known sites to review:
  - `detect.py:711-712` — behavior analyzer initialization (`log.warning` only, no traceback)
  - `detect.py:907-909` — `try/except Exception: pass` wrapping `draw_behavior` (the bug Fix B removed was hiding inside this very block)
  - Likely more in the main loop, annotate/stream path, and `emit_alert` fallthrough
- Apply the ByteTrack treatment: specific catches where possible; `log.error`/`log.exception` with greppable prefixes (e.g., "BEHAVIOR DISABLED", "ANNOTATE ERROR") for fail-open cases where graceful degradation is intentional.
- Medium urgency — these don't actively break anything today, but they're the pattern that hid Fix A for days.

### Capture event-time frame snapshot for modal evidence (MEDIUM)

Session 3 modal renders a placeholder ("No snapshot captured") because
the backend doesn't persist frames at event-emit time. Real work:

- Pipeline side (`detect.py`): on `emit_alert`, encode the current
  annotated frame to JPEG and write to `snapshots/<event_id>.jpg`
  (or a ring buffer keyed by ID). Disk-budget aware — cap size, prune
  oldest when over budget.
- Dashboard side (`dashboard_server.py`): new route `GET
  /api/events/{id}/snapshot` returns the JPEG, 404s if absent.
- Frontend (`dashboard_static/index.html`): modal evidence block
  swaps `<img src="/api/events/{id}/snapshot" onerror=fallback>`;
  current placeholder becomes the `onerror` fallback.

Open design questions: JPEG quality vs storage budget, whether to
store the raw frame or the annotated one, whether snapshots are
per-event or per-track. Defer until after notification channel lands.

### Add `Cache-Control: no-cache` to dashboard static responses (LOW)

`dashboard_server.py:452-457` serves `index.html` via
`HTMLResponse(read_text(...))` with no cache-control headers. Browser
caching bites during iteration — hard-reload required to see edits.

Fix: one-line addition at the response headers around lines 454-456:
`HTMLResponse(html.read_text(...), headers={"Cache-Control": "no-cache"})`.
Same treatment for any future static routes. Low urgency — a dev
inconvenience, not a production issue.

### Silent-degradation audit of `dashboard_server.py` except Exception blocks (MEDIUM)

Parallel to the `detect.py` silent-degradation audit above. Known
sites in `dashboard_server.py`:

- `_on_message` (line ~302-303): `except Exception as e: log.warning("MQTT parse error...")` — swallows everything from JSON decode errors to DB insert failures with the same generic log.
- `broadcast` (line ~241-247): appends failed clients to `dead` and drops them without distinguishing ConnectionClosed (expected) from actual send errors (worth logging).
- `proxy_stream._gen` (line ~343-369): `except Exception as e: log.debug(...)` — debug-level is invisible in production log config; a dead stream proxy leaves the dashboard showing "connecting…" forever with no ERROR line.
- `_mqtt_thread` retry loop (line ~311-317): similar to detect.py's `_mqtt_worker` — catches all, no differentiation between "broker down" and "protocol mismatch."

Apply the same ByteTrack treatment: specific catches where possible;
loud, greppable log prefixes ("MQTT PARSE", "WS BROADCAST",
"STREAM PROXY", "DASHBOARD MQTT") on failures that matter. Medium
urgency — same rationale as the detect.py item: these don't break
anything today, but they're where the next silent outage will hide.
