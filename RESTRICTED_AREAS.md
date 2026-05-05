# Restricted Areas — what to do, step by step

This is the practical guide for the warehouse owner. Plain language, in
order. Every line is something you do or check.

The code for everything below is already on the `agent/cam2-dev` branch
(commits `a53d6c2`, `7a925f2`, `6c3bb7a`, `de62bf4`). It has not been
deployed yet. Step 1 deploys it.

---

## Step 1 — Restart the services (once)

Open a terminal on the Jetson and run:

```
sudo systemctl restart sentinel_dashboard security_pipeline
```

That picks up Phases 1, 2a, 2b, and 3 in one go. The dashboard runs the
database migration on startup (additive only — no data is rewritten).

## Step 2 — Verify it came up

Wait about 15 seconds for the pipeline to load the model, then run:

```
journalctl -u sentinel_dashboard --since "1 minute ago" | grep "added column"
curl -s http://localhost:8081/health | python3 -m json.tool | head -30
```

What you want to see:

- A handful of `forbidden_zones: added column …` lines on the FIRST
  restart only. Subsequent restarts will not show them.
- `behavior_ready: false` (we turned that off earlier — expected).
- `clip_count`, `clip_errors`, `clip_active` fields appearing in the
  health output.

Open the dashboard in a browser:

```
http://<jetson-ip>:8888
```

You should see three tabs at the top: **Live**, **History**, **Reports**.

## Step 3 — Draw your first zone

Use the Live tab.

1. Click **Draw zone** (top-left of the live feed).
2. Click each corner of the area you want to restrict on the video. Five
   corners, six, however many you need. Double-click to close the polygon.
3. The configuration modal opens. Fill it in:
   - **Template** — pick the closest match (chemical / electrical /
     mezzanine / dock / fall / custom). It pre-fills sensible defaults
     for that kind of zone. You can still edit any field.
   - **Zone name** — what you call this zone in real life. e.g.
     `Chem Cage – Aisle 7`. Use real names, not generic IDs.
   - **Severity** — CRITICAL for life-safety zones, HIGH/MEDIUM for
     others. Severity decides whether the alert pages Telegram or just
     hits the dashboard.
   - **Dwell (s)** — how many seconds someone must be inside before the
     alert fires. Set this to **5** for chemical/storage zones so
     walking past doesn't trigger. Set to **0** for fall hazards
     (mezzanine, dock edges) so any entry fires immediately.
   - **Active hours** — `*` for always. For a no-go-after-8pm dock,
     write `20:00-06:00`. The hours wrap midnight automatically.
   - **Authorized roles** — write the role names (comma-separated) that
     should be allowed in this zone. Stored now, not yet enforced — see
     "What I need from you" below.
   - **Shadow mode** — pick **14 days (recommended for new zones)**.
     Read the next step before saving.
4. Click **Save**.

## Step 4 — Live with shadow mode for two weeks (the most important step)

Every zone you create should start in **14-day shadow mode**. The system
will detect intrusions and write them to the dashboard, but it will not
send any Telegram alerts during this window.

Why: this is how we hit the "<2 false alarms per camera per week"
target. You spend two weeks looking at what the system *would* have
fired on. Every event that's wrong (pallet shadow, forklift turn,
shoulder clipping the line), you click **False alarm** on. The system
counts those clicks as the false-positive metric.

You'll see a small `SHADOW` pill on every event card during the
shadow window so you know nothing was paged. After the expiry date the
pill disappears, the zone goes live, and Telegram starts firing.

You can re-enable shadow mode any time by clicking the zone's polygon
and choosing **Edit**.

## Step 5 — Daily use: the three action buttons

Every event card in the Live tab has three buttons under it:

- **Acknowledge** — "I saw it, it was a real event, no further action."
- **Dispatch** — "I'm sending a supervisor."
- **False alarm** — "The system was wrong. Don't count this against me,
  and use it as evidence to tune the zone."

Click one. The card flips to a small footer ("Acknowledged · 3s ago"
in green, "False alarm · 5s ago" in red). The buttons disappear so you
can't double-act on the same event. If you click one by accident, you
can re-classify from the History tab in Phase 4 (not yet built).

The numbers you click here drive the FP metric in the Reports tab. Be
honest with the False alarm button — it's the single most important
input the system has for self-tuning.

## Step 6 — Weekly review: the Reports tab

Open **Reports** every Monday morning (or whenever you do your weekly
review).

The window defaults to **Last 7 days**. The four tiles at the top tell
you what happened:

- **Total events** — how many alerts fired across all cameras and zones.
- **Shadow events** — how many fired silently (zones in their
  shake-down window). Expect this to be high in the first two weeks
  after deployment, then drop to zero.
- **After-hours** — forbidden_zone events that fired between 18:00 and
  06:00. These are the ones most worth reviewing in detail because
  they're the ones that should never have legitimate cause.
- **False alarms** — total times you clicked False alarm in the
  window. The tile turns RED if any camera is over the 2/week budget.

Below the tiles:

- **By kind** — what mix of events the system saw.
- **By camera** — which camera is loudest. Often the busiest camera is
  also the highest false-alarm one.
- **Forbidden-zone breaches by zone** — which zones are firing most.
  If one zone dominates, look at its config (dwell too low? polygon
  too aggressive?).
- **False-alarm budget** — per-camera "within budget" or
  "⚠ over budget". This is the spec target made visible.
- **Sample events for spot-check** — clickable chips. Click one, the
  modal opens, the 5-second clip plays. Use this to confirm zone
  config is correct without scrolling through history.

## Step 7 — When a camera is over its FP budget

The Reports tab shows the camera in red. Find its biggest source of
false alarms:

1. In **Forbidden-zone breaches by zone**, find the zone with the
   highest count.
2. Click a sample event chip from that zone. Watch the clip.
3. If it's clearly wrong (shadow / forklift / clipped shoulder), Edit
   the zone:
   - Bump **Dwell (s)** from 0 → 3 → 5. Most pallet-shadow false alarms
     disappear at 5 seconds.
   - Pull the polygon corners IN if shoulders or forks are clipping
     the boundary. Click the zone, Delete, redraw with more margin.
   - Tighten **Active hours**. If a zone only matters at night,
     `20:00-06:00` halves the daytime noise floor.
4. Re-enable 7-day shadow mode on the zone after every retune so the
   change isn't paging anyone while you confirm it's better.

That's the whole loop. Tune, shadow, observe, un-shadow.

---

## What I still need from you (decisions, not code)

Phase 4 is heatmaps + incident export + the false-alarm feedback loop.
None of those need decisions. Two items from your original spec do:

1. **Badge / access-control system.** What make and model do you have
   on site, and does it expose an API or webhook? If you don't have one
   yet, I can ship a pure-camera substitute: per-zone vest-colour
   detection (operator picks a colour swatch, the system suppresses
   alerts when the bbox is mostly that colour). It's not as solid as a
   badge system but it works at warehouse lighting and needs no extra
   hardware. Tell me which you want.
2. **On-site speaker for life-safety zones.** The spec asks for an
   audible "you are approaching a fall hazard" warning. I need to know
   what speaker hardware the warehouse will have. Options ranked by
   effort:
   - IP speaker (Axis, 2N, similar) with HTTP API — half a day's work.
   - MQTT-controlled speaker — half a day, since the pipeline already
     publishes MQTT.
   - Generic USB speaker plugged into the Jetson — works but only audible
     near the Jetson box, not near the cameras.
   - PA system tied into a relay — needs an electrician.
   Tell me which (or "decide later, ship Phase 4 first" — that's also a
   valid answer).

When you have answers to those, paste them in chat and I'll start
Phase 4.

---

## Quick reference

| Task                              | Where                              |
|-----------------------------------|------------------------------------|
| Draw or edit a zone               | Live tab → Draw zone, or click polygon → Edit |
| Watch the live feed               | Live tab                           |
| Acknowledge / dispatch / FP       | Buttons under each event card      |
| Look at past events               | History tab                        |
| Weekly metrics                    | Reports tab                        |
| Pipeline health                   | `curl http://localhost:8081/health` |
| Pipeline logs                     | `journalctl -u security_pipeline -f` |
| Dashboard logs                    | `journalctl -u sentinel_dashboard -f` |
| Restart everything                | `sudo systemctl restart sentinel_dashboard security_pipeline` |
