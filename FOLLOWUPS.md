# Follow-ups

Deferred work captured from audits and verification runs. Items here are
intentionally not blocking the current change; each entry notes urgency and
sufficient context to pick up in a future session.

### Migrate MQTT callbacks to paho CallbackAPIVersion.VERSION2

- Current state: using VERSION1 compat (paho 2.x deprecation warning)
- Affected: `detect.py` `_on_connect`, `dashboard_server.py` `_on_connect`
- VERSION2 signature: `on_connect(client, userdata, flags, reason_code, properties)` — `reason_code` replaces `rc`, `properties` added
- Low urgency; do before paho 3.x release.

### ByteTrack import broken in current boxmot

- Observed in Q4 foreground run: `cannot import name 'ByteTrack' from 'boxmot'`
- Effect: multi-object tracking is disabled in production right now; Risk #3 (zone loitering track_id=0 collapse) is active, not theoretical
- Current boxmot in `.venv`: run `pip show boxmot` to confirm version
- boxmot restructured tracker imports in recent versions; likely need `from boxmot.trackers.bytetrack.bytetrack import ByteTrack` or similar
- High urgency — tracking underpins behavior analysis, zone dwell, and alert dedup
- Candidate for next session after Q4/Q7 wrap up
