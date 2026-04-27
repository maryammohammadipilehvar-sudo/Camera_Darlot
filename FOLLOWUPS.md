# Follow-ups

Deferred work captured from audits and verification runs. Items here are
intentionally not blocking the current change; each entry notes urgency and
sufficient context to pick up in a future session.

### alerts_only_forbidden_zone gate suppresses everything else (MEDIUM — review when re-tuning rules)

`CFG.alerts_only_forbidden_zone = True` (Session 7) silences every
event kind except `forbidden_zone`. Phone-use, action-captioned
detections, behavior alerts, thermal warnings, audio events,
detection summaries — all build audit rows but never reach the
events table, dashboard, or Telegram.

This was the operator's explicit ship-it call after the v2 rules
session. Audit log still captures the decisions
(`decision='suppressed_threshold'`,
`reason_detail='alerts_only_forbidden_zone'`) so operators can
later query "what would have fired?" and unfreeze on a per-kind
basis.

To relax: flip the CFG flag to `False`. Severity table + threshold
table take over again — every kind routed per Session 5 rules.
Pair the flip with a fresh foreground test, the previous-tuning
session's results don't transfer once the gate moves.

Review trigger: any operator request that surfaces non-forbidden-
zone information (e.g., "I want a daily report of detection
counts" or "we need phone-use back").

### phone_use IoU threshold tuning (LOW)

`CFG.phone_use_iou = 0.05` is loose for production. Phone-on-desk
near a sitting person triggers without the phone being held. Tune
to 0.15–0.30 after observing real warehouse traffic.

May also need pose-based "phone in hand" refinement (wrist keypoint
near phone bbox center) to distinguish phone-on-desk from
phone-being-used. Adds dependency on YOLOv8n-pose keypoint flow
into a new classifier; only worth it if IoU tuning alone isn't
enough.

### forbidden_zones lacks camera_id column (MEDIUM)

Today fine — single-camera deployment. When multi-camera support
lands, schema migration needed:

```sql
ALTER TABLE forbidden_zones ADD COLUMN camera_id TEXT NOT NULL DEFAULT 'cam_01';
```

Then update:
- API endpoints to filter by `?camera=` and require it on POST
- Dashboard drawing UI to scope polygons to the selected camera tile
- `ForbiddenZoneEngine.check(cx, cy, camera_id)` signature change
- Engine reload thread to load only its own camera's zones

Block on the multi-site architecture entry (separate FOLLOWUP) so
both schema changes ship together.

### Forbidden-zone drawing UI is mouse-only (LOW)

Canvas listeners are click / mousemove / dblclick only. Touch
events (touchstart / touchmove / touchend) not wired. Operators
on iPads or touch-enabled monitors can't draw polygons; the
"Draw zone" button works but tapping the canvas does nothing.

Add when there's a real iPad operator request — desktop-only is
acceptable for warehouse deployment today. Implementation is
straightforward (~30 lines): map the touch event coordinate
through the same `_zoneEvtToNorm` helper, with `touches[0]` as
the position source, and a synthetic dblclick on rapid-tap.

### Behavior pipeline duplicates the central dedup (LOW)

`behavior.py:should_alert` runs a per-track, per-action cooldown
(60/60/120s as of 2026-04-27) that gates whether `emit_alert` is
called at all. The central three-layer dedup in `detect.py`
(`_dedup_filter`) only runs on the telegram path inside
`emit_alert`, so behavior emits that route to dashboard_only bypass
the central system entirely.

Two dedup systems for the same noise problem. Consolidate so all
throttling lives in `detect.py`'s `_dedup_filter` once the behavior
emit path is unified with detection — likely means moving the
should_alert gate either out of behavior.py or into the central
dedup with a "kind=behavior, dashboard_only" allowance.

Low urgency — works correctly today, just two places to reason
about when tuning rates.

### Track dedup over-suppresses sustained presence (HIGH)

Session 5 foreground test (2026-04-27, 13:05–13:08, CLOSED mode):
1 person walking in frame for 3 minutes produced 1 fired_telegram +
419 suppressed_dedup_track. A real intruder lingering for 5+ minutes
would only generate one alert under the current 5-minute track-aware
window.

Rules tuning options to evaluate before customer deployment with
real warehouse traffic:

- Severity-aware dedup windows (e.g., CRITICAL=30s, HIGH=60s,
  LOW=300s) so high-severity tracks re-fire faster than low ones.
- Track-loss-aware re-firing: when a track_id is dropped and
  recovered (or a new track_id appears in the same zone), reset
  the dedup state for that kind.
- Movement / zone-change re-firing: significant displacement in
  bbox or polygon transitions triggers a re-alert even within the
  dedup window.

Decide before customer deployment with real warehouse traffic.
The 5-minute window was a deliberate noise-reduction choice for
the bring-up; production behavior needs the tuning above.

### First-event snapshot race (LOW)

The very first emit_alert after process startup fires before the
snapshot writer thread is warm. Result observed in Session 5
foreground test: `photo=False` on the first Telegram message
(text-only). Subsequent events ship with photos correctly.

Mitigations: (a) prime the snapshot writer at startup with a
one-shot warm frame, (b) extend the first-event 150ms wait
specifically (e.g., 1s grace for event_id=1), (c) ignore — the
pipeline runs continuously in production and only loses a photo
on cold restart, which is operator-visible already via systemd
logs.

Cosmetic in production. Worth fixing alongside any other
snapshot-pipeline tuning.

### ffmpeg H264 'Overread VUI by 8 bits' decoder noise (LOW)

Pre-existing, harmless H264 decoder warnings from the camera RTSP
stream — typically 2 lines per cold start, more if the encoder
restarts. Doesn't affect decoding correctness. Adds clutter to
log greps for `error` / `warning` and trips reflexive concern
during incident review.

Fix: pipe ffmpeg stderr through a filter that drops known-benign
warnings, OR set `OPENCV_FFMPEG_LOGLEVEL=quiet` (verify it doesn't
mask real errors first).

### alert_audit retention policy (LOW)

Currently grows unbounded at ~876k rows/year (estimate from
Session 5 audit math). Manageable on the Jetson SSD for years,
but not forever. Add daily prune or rolling-window archive
before the audit table exceeds 10M rows or 1GB.

Approach options: (a) DELETE rows older than 90 days on a
cron-style timer, (b) weekly export to a compressed archive
file before delete, (c) partition by month into separate
tables. (a) is simplest; (b) preserves forensic value; (c) is
overkill at this scale.

### Dashboard surfaces alert_audit (MEDIUM)

Operators currently can't see why an alert was suppressed —
the dashboard reads `events`, but suppressed-INFO and dedup-
suppressed decisions never write events rows; they exist only
in `alert_audit`. Without dashboard visibility, operators
cannot tune dedup windows from observed behavior.

Build a "Suppressed Alerts" view in the dashboard that queries
alert_audit with a small set of canned filters (last hour, by
camera, by reason). Read-only; no actions. Pair with the dedup-
tuning work in the HIGH FOLLOWUPS entry above so operators can
see the impact of any window change.

Later session — depends on production traffic to be useful.

### Build real remote notification channel (HIGH)

**RESOLVED (Session 4, commits 5bca47a + 5f9cb1f + c091b41)**

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

**RESOLVED (Session 4, commit 5f9cb1f)**

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

### Alert fatigue — dedup, per-kind cooldowns, quiet hours (HIGH)

Session 4 Phase 5 foreground test (2026-04-24, 120s) delivered ~24
Telegram messages, roughly 6 of which were repeat "person detected"
alerts for the same two people standing in frame. Under the current
threshold (CRITICAL/HIGH/MEDIUM — HIGH includes person-class
detections via the JS-parity severity upgrade), this will not scale
to a real deployment.

Required work:

- **Audit `CFG["alert_cooldown_sec"]` (currently 30.0).** Is it
  actually applied in the detection/loitering emit paths? Is it
  track-aware (per track_id) or global? Grep for references;
  verify behavior against repeat-emit observed in the Phase 5
  test.
- **Per-track dedup for detection events.** Track_id-keyed cooldown:
  don't re-alert for the same track within N seconds. The existing
  `active_objects` dict around `detect.py:720` looks structurally
  right — needs verification that it actually gates notification,
  not just MQTT publication.
- **Per-kind cooldowns.** A gunshot and a person-detection should
  have independent cooldowns. Currently a single scalar.
- **Quiet-hours gate.** Operator-configurable window where MEDIUM
  and below are suppressed (CRITICAL always shipping). Off by
  default; opt-in per site.
- Consider a "burst digest" for summary-kind events: N alerts in
  T seconds collapse into one "5 person-detections in 60s" message.

Product impact: without this, Telegram notifications become noise
and admins turn them off — defeating the whole Session 4 build-out.
Session 5 candidate, probably the highest-value next task.

### Behavior classifier mislabels standing as sitting (MEDIUM — partial fix)

**PARTIAL FIX (Session 8).** Three structural problems flagged
here have been addressed:

1. The unreachable duplicate sitting block at `behavior.py:167-177`
   has been deleted.
2. `Action.SITTING` is now a member of the enum, registered in
   `_TRANSITIONS`, `ACTION_COLORS`, and `_ROUTINE_ACTIONS` (so the
   overlay correctly skips it as routine).
3. Strict-sitting `knee_hip_gap` threshold tightened from 0.22 →
   0.15 to reduce the standing-misclassified-as-sitting rate that
   was observed when keypoint detection was incomplete.

What remains: a quantitative threshold sweep against ground-truth
labels. The replay harness now captures per-frame action labels
via `--emit-actions` (Session 8); next iteration is a labeled
corpus + accuracy regression test that drives further tuning.
Possibly a learned classifier (small MLP on keypoint statistics)
if the rule-based approach plateaus.

### Debug overlay leak in behavior.py / draw_behavior (LOW)

Session 4 Phase 5 Telegram snapshots show raw Python dict text
drawn on every annotated frame, e.g.:

    {'action': 'sitting', 'next_action': 'unknown', 'track_id': 4}

Appears to be forgotten debug instrumentation inside
`behavior.py`'s `draw_behavior` (or one of its helpers). Not a
Phase 5 regression — behavior of this function didn't change in
Session 4 — just made newly visible because we're now shipping
annotated frames to Telegram as evidence.

Fix: find the raw-dict draw call and either remove it or put it
behind a `cfg["behavior_debug_overlay"] = False` toggle (per
CLAUDE.md #13).  Small, self-contained, high-visual-impact change.

### Audit severity-upgrade logic (MEDIUM)

**RESOLVED.** `_severity_of()` in `detect.py` was deleted in
Session 5 (replaced by table-driven `compute_severity`). The JS
`severityOf()` upgrade (high→critical, medium→high) is also
retired now that `emit_alert` stamps the computed severity
directly into the events row. Both surfaces read the same
truth from the severity table; what the operator configures is
what they see.

### Port _summary_of() coverage to dashboard_server.py (LOW)

Session 4 Phase 5 added `_summary_of()` in `detect.py` with
explicit cases for thermal and face_match kinds. The dashboard's
`format_event_for_ui()` in `dashboard_server.py:173` still falls
through to "Unknown event" for those kinds.

Straightforward parity port: copy the thermal and face_match
branches from `detect.py:_summary_of` into
`format_event_for_ui`. Small, low-risk change, improves admin UX
for thermal-zone and identity-match events without touching the
detection path.
