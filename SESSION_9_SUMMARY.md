# What changed this session — explained simply

This file explains, in plain language, what was changed on the branch
`feat/rules-engine` during Session 9. No jargon if I can help it. If you
want the technical version, read the commit messages on the branch
(`git log feat/rules-engine`).

---

## The big picture

Imagine the security pipeline like a **guard standing in front of a wall
of camera screens**. The cameras see things. The guard decides which
things to call out, and to whom.

Until this session, the guard had three problems:

1. **He stayed quiet too long after a single alert.** If a person walked
   into a forbidden zone and stayed there for 5 minutes, the guard
   called it out *once* and then said nothing for the next 5 minutes —
   even if the person was clearly still trespassing.
2. **He had only one switch: "tell me about forbidden-zone problems
   only."** No nuance. You couldn't say "tell me about forbidden zones
   AND fights AND gunshots" without changing the code.
3. **All his rules were tangled together inside one big function.** Hard
   to add new rules (like "no hardhat in the warehouse"). Hard to
   change one rule without risking another.

This session fixed all three. Below is what changed, one piece at a
time, in the order it shipped.

---

## Change 1 — The guard re-alerts faster for serious things

**File:** `detect.py`. Tests in `tests/test_dedup.py`.

**What was wrong.** The guard used the same "wait time" for every kind
of alert. If a critical event (an intruder!) happened, the guard
re-checked at the same slow pace as a low-importance event (someone
walking in a normal area).

**What now happens.** Different waits for different severities:

- **CRITICAL** events (intruder, fire, fight): re-check every 30 seconds.
- **HIGH** events: every 60 seconds.
- **MEDIUM** events: every 3 minutes.
- **LOW** events: every 5 minutes.

**Plus a clever trick — "track loss":** if the camera was watching person
A, and person A walks out of view, then a new person B walks in — the
guard now treats person B as fresh news and alerts about them
immediately. Before this change, the guard would silently lump them
together and say nothing about person B.

**Why it matters.** A real intruder who lingers for 5 minutes used to
generate 1 alert and 419 silent "I already saw this" notes. Now they
generate ~10 alerts. You'll actually know they're still there.

---

## Change 2 — The guard's "what to tell me" switch is smarter

**File:** `detect.py`. Tests in `tests/test_allowlist.py`.

**What was wrong.** There was a single yes/no switch: "tell me about
forbidden zones only." That was it. No way to say "tell me about
forbidden zones AND fights" without editing code.

**What now happens.** The switch is a *list*:

- Set the list to `["forbidden_zone"]` — guard tells you only about
  forbidden zones (same as before).
- Set the list to `["forbidden_zone", "audio:critical", "fighting"]` —
  guard tells you about all three.
- Set the list to `None` — guard uses the full rules and tells you
  about everything important.
- Set the list to `[]` (empty) — guard tells you nothing (silent mode).

**Backward compatibility.** The old switch (`alerts_only_forbidden_zone`)
still works for now — the code translates it into the new list and
prints a warning saying "please update your config." If you set both
the old switch and the new list and they disagree, the code refuses
to start (loud failure beats silent confusion).

---

## Change 3 — The guard now tells you when someone uses a phone

**Files:** `detect.py` (CFG and one helper). No new files.

**What was wrong.** The system already had cell-phone detection wired
in — YOLO classifies phones (COCO class 67) and there was a check
asking "is the phone overlapping a person?". But three things blocked
the alert from reaching you:

1. The "what to tell me" allowlist (Change 2) didn't include
   `phone_use`, so it was filtered out.
2. The severity for `phone_use` was set to LOW during business hours,
   which only writes to the dashboard — no Telegram push.
3. The overlap check used **IoU** (Intersection over Union). For tiny
   objects like phones this is geometrically broken — a held phone is
   ~1% of a person's bounding box area, so even when the phone is
   *fully inside* the person box, IoU caps at about 0.01. The
   threshold was 0.05. **Mathematically impossible to ever fire.**

**What now happens.**

- `phone_use` is in the allowlist.
- Severity bumped to HIGH during business hours and after-hours. That
  routes to Telegram and benefits from the 60-second re-fire window
  (Change 1) so you get periodic reminders, not just one and silence.
- The IoU check is replaced by a **containment** check: "what fraction
  of the phone's bounding box is inside the person's bounding box?"
  This is naturally close to 1.0 when held in front of the body.
  Default threshold: 30% overlap.
- The person's bounding box is **padded by 40 pixels** before the
  check, because YOLO's person box covers torso/legs cleanly but
  often clips arms extended forward — so a phone held up at chest
  level lands just outside the bare box. Padding reclaims that area.

Three operator-tunable knobs in CFG, with sensible defaults:

```
phone_use_containment   = 0.3   # 30% overlap is enough
phone_use_person_pad_px = 40    # forgive 40px of arm reach
yolo_conf               = 0.40  # was 0.55; lowered to catch small objects
```

**Why it matters.** Walk in front of the camera holding a phone, and
within 1–2 seconds you get a Telegram message with a snapshot. While
you keep using it, you get a reminder ping every minute. Stop using
it, and the alerts stop.

---

## Change 4 — The guard now uses a "rule book"

**Files:** new folder `rules/` with `base.py`, `engine.py`,
`forbidden_zone.py`, `loitering.py`. Tests in `tests/test_rules_engine.py`,
`tests/test_rule_forbidden_zone.py`, `tests/test_rule_loitering.py`.

**What was wrong.** All the guard's rules ("if a person enters this
zone, alert"; "if someone stays here 2 minutes, alert"; "if there's a
phone in someone's hand, alert") were written in one giant function in
`detect.py`. Adding a new rule meant editing that function. Mistakes
in one rule could break others.

**What now happens.** Every rule is its own small file in a new folder
called `rules/`. They all follow the same simple shape:

> *"Given what's happening on screen right now, list any alerts you'd
> like to fire."*

A new piece called the **rules engine** runs every rule once per video
frame, collects what each rule wants to alert about, and routes it
all through the existing alert pipeline.

Two rules have been moved into this new system so far:

- **`forbidden_zone`** — "person inside an operator-drawn polygon".
  Same behaviour as before, just lives in `rules/forbidden_zone.py`
  now. Easier to test in isolation.
- **`loitering`** — "track stays in a zone longer than X seconds".
  Same behaviour. The internal helper (`ZoneEngine`) had to be
  slightly reshaped — it used to call the alert system directly;
  now it just *returns* what would be alerted, and the rule decides
  what to do with it. Cleaner.

**Failure isolation.** If one rule has a bug, the rules engine logs the
error and keeps the other rules running. Before, a bug in one place
could crash the whole thing.

**What's next on this rule-book.** The remaining inline rules in
`detect.py` (phone-use detection, generic detection events) will move
into the rules folder in a future session. The headline new rule —
**PPE compliance** ("no hardhat / no hi-vis vest in the warehouse") —
is paused waiting for you to choose which AI model to use. See
`FOLLOWUPS.md` → "Phase 1d" for the three options.

---

## How to verify it all still works

Open a terminal in the project folder, then:

```
source .venv/bin/activate
pytest tests/ -v
```

You should see **47 passed, 4 skipped**. The 4 skipped are pre-existing
"replay corpus" tests that need real video clips in `tests/corpus/` —
they're skipped because those clips aren't checked into git.

To check that the live pipeline still imports cleanly:

```
python3 -c "import detect; print('OK')"
```

To run the live pipeline against the camera (same as before):

```
python3 detect.py
```

You'll see a new line in the startup log:
`RULES engine ready: forbidden_zone, loitering`. That's the rule book
announcing which rules it has loaded.

---

## What was NOT changed (intentionally)

- **No new dependencies.** No new pip installs, no new model downloads.
- **No database schema changes.** The events table, audit log, and
  forbidden-zones table are untouched.
- **No systemd service file changes.** Production startup is identical.
- **No notification channel changes.** Telegram, MQTT, dashboard all
  behave the same from the operator's point of view.
- **No deletion of disabled features.** Face recognition, audio events,
  and PatchCore anomaly are all still disabled (same status as before
  this session — see `AUDIT.md` §3).

---

## How to back out

If anything breaks, the changes can be reversed cleanly:

```
git checkout agent/auto-dev    # leaves feat/rules-engine alone for review
```

The new branch hasn't been pushed to a remote, so there's no
broadcast undo to worry about. The Session 8 work-in-progress changes
that were stashed before this session are preserved — recover them with
`git stash pop` when you're back on `agent/auto-dev`.
