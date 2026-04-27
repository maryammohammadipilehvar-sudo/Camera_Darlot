# Test Clip Corpus

Three short MP4s the replay harness uses to assert pipeline behaviour
without involving a live camera, MQTT broker, dashboard, or Telegram.

Record once. The clips become regression-test fixtures for every future
rules-tuning, model-swap, or dedup-window edit.

## File names (must match exactly)

```
tests/corpus/clip_01_forbidden_breach.mp4
tests/corpus/clip_02_routine.mp4
tests/corpus/clip_03_dedup_sustained.mp4
```

## Required scenarios

The tests inject a forbidden polygon covering the **right third of the
frame**, normalized as `[[0.6, 0.0], [1.0, 0.0], [1.0, 1.0], [0.6, 1.0]]`.
Record clips that exercise it.

### `clip_01_forbidden_breach.mp4` — ~10 s

- Operator walks from the LEFT side of the frame across into the right
  third, stops briefly inside the polygon, walks out.
- Test asserts: at least one `fired_*` decision with `kind=forbidden_zone`
  and `computed_severity=CRITICAL`.

### `clip_02_routine.mp4` — ~15 s

- Operator walks around the LEFT two-thirds of the frame only. Never
  crosses into the right third.
- Test asserts: zero `forbidden_zone` fires; zero non-`forbidden_zone`
  fires (the `alerts_only_forbidden_zone` gate suppresses everything
  else).

### `clip_03_dedup_sustained.mp4` — ~15 s

- Operator walks INTO the right third and stays there, standing or
  shifting weight, for the full duration.
- Test asserts: exactly one `fired_*` for `forbidden_zone`; subsequent
  per-frame triggers all collapse into `suppressed_dedup_track` rows.

## Recording recipe

Pipe the existing RTSP stream straight to disk with ffmpeg:

```bash
ffmpeg -rtsp_transport tcp \
       -i 'rtsp://admin:QuareroRobotics_2025@192.168.2.148/Preview_01_sub' \
       -t 12 \
       -c copy \
       tests/corpus/clip_01_forbidden_breach.mp4
```

Replace `-t 12` with the duration you want and the filename with the
target. Use `-c copy` to avoid re-encoding (faster, byte-perfect).

If your camera publishes at a resolution different from the inference
frame (640x360), the pipeline resizes on read — no need to pre-resize
the clips.

## Running the tests

```bash
source .venv/bin/activate
pytest tests/ -v
```

- `test_replay_smoke` runs against a synthetic blank video — no clip
  needed, exercises the harness itself.
- The three clip-backed tests `pytest.skip()` if their MP4 is missing,
  so the suite stays green on a fresh checkout.

## When to re-record

- After changing camera placement or angle.
- After a major lighting shift (e.g., new fixtures installed).
- If the polygon convention changes (right third → some other shape).

Keep the clips under 20 seconds each. The pipeline runs flat-out in
replay mode (`infer_gap=0`), so a 15s clip typically completes in 5-8
seconds on the Jetson.

## What the tests catch

- Severity-table edits that drop or move `forbidden_zone`.
- Threshold-table edits that change CRITICAL routing.
- Dedup-engine regressions (`_dedup_filter` returning wrong reason).
- The `alerts_only_forbidden_zone` gate being accidentally bypassed.
- Mode resolution glitches (forced `--mode OCCUPIED`).
- Audit-log writer breakage (decisions only reach the JSON via
  `_audit_write`; if the writer dies silently, the dump is empty and
  tests fail).

What they don't catch:
- YOLO inference correctness (deterministic but not asserted on bbox
  coords).
- Snapshot file production (replay disables the snapshot writer).
- Telegram delivery (replay disables the notifier).
- Dashboard rendering (frontend is out of scope here; live tests via
  browser still required).
