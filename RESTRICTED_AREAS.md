# Restricted Areas — what the software does and how to use it

The Jetson runs at **192.168.2.108**. Open the dashboard from any
device on the same Wi-Fi:

> **http://192.168.2.108:8888**

Three tabs at the top: **Live**, **History**, **Reports**.
Header has a **Role colours** button.

---

## First-time setup (do this once, in this order)

### 1. Restart the services to deploy everything

On the Jetson, open a terminal and run:

```
sudo systemctl restart sentinel_dashboard security_pipeline
```

Wait ~15 seconds. Reload the browser.

### 2. Define your authorized roles and their vest colours

Click **Role colours** in the header. Two ways to add roles:

**Quick add — common warehouse roles.** Eight one-click presets with
standard hi-vis colours: `supervisor` (orange), `forklift_op` (yellow),
`safety_officer` (lime), `hazmat` (red), `maintenance` (blue),
`visitor` (green), `security` (black), `picker` (amber). Click a
preset, the role is added with that colour. If your actual vests are
slightly different, edit the colour swatch on the row afterwards.

**Custom role.** Type any role name, pick the colour with the native
picker, click **Add**. Useful for site-specific roles that don't fit
a preset (e.g. `night_shift`, `qa_inspector`).

This is what lets the system know "this person is allowed in this zone."
Use bright, high-contrast colours (orange, lime, yellow). Dim or
low-saturation clothing won't work reliably.

### 3. Draw your zones

Go to the **Live** tab. Click **Draw zone** above the live feed.

1. Click each corner of the no-go area on the video. Three corners
   minimum, more if needed.
2. Double-click to close the polygon.
3. The configuration modal opens.

In the modal:

| Field             | What it controls                                      |
|-------------------|-------------------------------------------------------|
| Template          | Pre-fills sensible defaults for chemical / electrical / mezzanine / dock / fall / custom |
| Zone name         | What you call this zone in real life ("Chem Cage – Aisle 7"). Used in alerts. |
| Severity          | CRITICAL pages Telegram immediately, lower tiers stay on the dashboard |
| Dwell (s)         | Seconds the person must be inside before alert fires. Set 0 for fall hazards, 5 for storage zones |
| Active hours      | `*` for always-on. `20:00-06:00` for after-hours-only |
| Authorized roles  | Comma-separated role names that match what you set up in step 2. Wearing the matching vest = no alert |
| Shadow mode       | **Pick "14 days" for every new zone.** The zone fires events to the dashboard but does not page anyone — gives you a window to tune false alarms |

Click **Save**. Repeat for each zone.

### 4. You are now operational

The pipeline is detecting people in your zones. Telegram is silenced
for the next 14 days because every zone is in shadow mode. Use this
window to look at what fires and click **False alarm** on anything
the system gets wrong.

---

## What the software does, function by function

### Live tab — what you watch all day

**Live feed.** The annotated camera view. Red boxes = forbidden zones
(the polygons you drew). Green / amber / orange overlays = detected
people, with severity colour cues.

**Event cards.** Each alert fires as a card on the right. Severity
sets the card colour: red bar = CRITICAL.

**Action buttons under each card.** Three options, every event:

- **Acknowledge** — "I saw it, normal-looking event, no further action."
- **Dispatch** — "I'm sending a supervisor to investigate."
- **False alarm** — "Wrong call. The system shouldn't have fired."

After clicking, the buttons collapse into a one-line footer
("Acknowledged · 5s ago"). The footer also appears in the History tab.

**Click any event card** — opens the detail modal:

- A 5-second video clip of the moment, scrubbable.
- Falls back to a still snapshot if the clip is still encoding
  (re-open a few seconds later to catch it).
- All event metadata.
- **↓ Export** button — download the incident as a ZIP (see below).
- Acknowledge / Escalate buttons.

**Click an existing zone polygon** — opens a popup with:

- Zone name + summary line (severity, hours, dwell, shadow days remaining).
- **Edit** — opens the same config modal in edit mode.
- **Suppressions** — appears when False-alarm clicks have created
  learned suppressions for this zone. Lists them; each row has a
  **Forget** button if the system over-learned from a wrong click.
- **Delete** — removes the zone (with a confirmation prompt).

### Authorization (vest colour)

When a person enters a zone whose Authorized roles list is non-empty:

1. The pipeline crops the upper torso of their bounding box.
2. Builds an HSV colour histogram.
3. Compares it to each authorized role's stored colour.
4. If a match crosses the threshold (≥ 25% of pixels match within
   ±15° hue tolerance), the alert is **suppressed silently**.

If your roles are well-coloured (bright high-vis, distinct from each
other and the warehouse background), this works well. If false-allows
happen (a random orange box gets a person past the gate), the
tolerances can be tightened — tell me and I'll wire a per-role
sensitivity slider into the UI.

### Shadow mode (the calibration window)

When a zone is in shadow mode:

- Events still fire to the dashboard with a **SHADOW** pill on each card.
- Telegram is suppressed regardless of severity.
- All your False-alarm clicks count toward the FP metric.
- After the expiry, the pill disappears and Telegram routing resumes.

Always shadow new zones for at least 7 days, ideally 14. Re-enable
shadow whenever you change a zone's geometry or dwell setting — your
tuning isn't validated yet.

### Learned suppressions (the false-alarm feedback loop)

When you click **False alarm** on a forbidden_zone event, three things
happen:

1. The False-alarm count goes up in the FP metric.
2. The system records *where* (zone + bounding-box centre) and *when*
   (hour of day) the wrong alert fired.
3. The next time a person triggers the same zone at the same hour
   within ~60 px of that location, the alert is suppressed silently.

The system learns from every False-alarm click. Each zone's popup
shows the count and a **Suppressions** button to review or forget
specific entries.

### Reports tab — what you check weekly

Pick the window: **Last 7 days**, **14 days**, or **30 days**. Filter
by camera if you have several.

**Tiles at the top:**

- **Zones configured** — how many zones currently exist. Red if zero.
- **Total events** — every alert that hit the events DB in the window.
- **Shadow events** — how many were silent (zones still in shadow mode).
- **After-hours** — forbidden_zone events between 18:00 and 06:00.
- **False alarms** — total False-alarm clicks. **Red if any camera
  exceeds 2 / week.**

**Tables:**

- **By kind** — what mix of events fired (forbidden_zone, phone_use, etc.).
- **By camera** — which camera is loudest.
- **Forbidden-zone breaches by zone** — split into:
  - **Currently-configured zones** — events from zones you still have drawn.
  - **Historical (zones you have deleted)** — events from zones you
    removed; shown separately so they don't skew your active view.
    Hidden when empty.
- **False-alarm budget** — per-camera "within budget" or "⚠ over
  budget". This is the spec target made visible.

**Hotspot heatmap.** Bright red cells show where forbidden-zone events
cluster in the camera frame. Use it to spot:
- Aisles you should restripe.
- Where to add or move signage.
- Whether a zone's polygon is too aggressive in one corner.

**Sample events for spot-check.** Click any chip — opens the modal
with the clip playing.

### Incident export (insurance / OSHA)

In any event modal, click **↓ Export**. A ZIP downloads named
`incident-{event_id}-{YYYYMMDD-HHMM}.zip` with:

- `README.txt` — plain-English: when, camera, type, zone, severity,
  shadow flag, full action timeline, file list.
- `event.json` — structured event data + zone config snapshot at the
  time + actions + audit slice ±60s.
- `snapshot.jpg` — event-time still frame.
- `clip.mp4` — 5-second video.

Hand it to insurance or OSHA exactly as-is. Open the README first;
everything else supports it.

---

## Daily / weekly rhythm

| Cadence  | Action                                                      |
|----------|-------------------------------------------------------------|
| Each shift | Open the dashboard. Watch the Live tab. Click action buttons on events as they come in. |
| Each event during the day | Acknowledge / Dispatch / False alarm. Be honest with False alarm — that's how the system tunes itself. |
| Monday morning | Open the **Reports** tab. Check the four top tiles. If any camera is over budget on FPs, dig into the hotspot heatmap and the by-zone breakdown. |
| Anytime a zone fires too many false alarms | Edit the zone: bump dwell, tighten polygon, narrow time window. Re-enable 7-day shadow until you confirm the change is good. |
| When something happens that needs filing | Open the event in the modal, click **↓ Export**, send the ZIP. |

---

## When something looks wrong

| Symptom                          | First thing to check                                  |
|----------------------------------|-------------------------------------------------------|
| Refresh button on Reports does nothing | Restart sentinel_dashboard — the new endpoint may not be live yet |
| Zone fires constantly on a forklift | Add the forklift driver's vest colour as an authorized role for that zone |
| Pallet shadow triggers a zone    | Bump the zone's **Dwell (s)** to 5 (chemical) or redraw the polygon away from the shadow line |
| Alert never reaches Telegram     | Check the zone is not in Shadow mode (look for the SHADOW pill on its events). After-hours zones (`20:00-06:00`) are silent during the day by design |
| Clip won't play in the modal     | Re-open the event 5–10 seconds later — the encoder may not have finished |
| Heatmap is empty                 | The window has no forbidden_zone events (good). Or no zones are configured (also possible). Check the **Zones configured** tile |

---

## What is still pending

Only one item from the original spec is open: an **on-site speaker**
that plays a "you are approaching a fall hazard, please step back"
warning when someone enters a life-safety zone. I need to know what
speaker hardware will be used:

- IP speaker (Axis, 2N, etc.) — easiest, half a day's work.
- MQTT-controlled speaker — also easy since the pipeline already speaks MQTT.
- Generic USB speaker on the Jetson — works but only audible near the box.
- PA system tied into a relay — needs an electrician.

When you have a hardware decision, I'll wire the audible warning.

---

## Quick reference

| Task                              | Where                                                  |
|-----------------------------------|--------------------------------------------------------|
| Open the dashboard                | `http://192.168.2.108:8888`                            |
| Set up role / vest colours         | Header → **Role colours**                              |
| Draw or edit a zone               | Live tab → **Draw zone**, or click an existing polygon → **Edit** |
| Acknowledge / dispatch / FP       | Buttons under each event card on the Live tab          |
| Watch a 5-second clip             | Click any event → modal opens with the player          |
| Export an incident                | Event modal → **↓ Export**                             |
| See historical events             | **History** tab                                        |
| Weekly metrics + heatmap          | **Reports** tab                                        |
| Pipeline health                   | `curl http://192.168.2.108:8081/health`                |
| Pipeline logs                     | `journalctl -u security_pipeline -f` on the Jetson     |
| Dashboard logs                    | `journalctl -u sentinel_dashboard -f` on the Jetson    |
| Restart everything                | `sudo systemctl restart sentinel_dashboard security_pipeline` |
