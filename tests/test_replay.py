"""Replay-mode tests against the corpus clips.

Each test runs a clip through detect.py in --replay mode and asserts the
emitted decisions match expectations. Tests auto-skip if their clip is
missing — record clips per tests/corpus/README.md, then re-run.

The smoke test uses a synthetic blank video so the harness itself can
be exercised without operator intervention.
"""

from __future__ import annotations

import subprocess
from pathlib import Path

import numpy as np
import pytest

from conftest import CORPUS_DIR


# Right-third-of-frame polygon, matching the "Test Zone" docs/operator
# convention. Coords are normalized 0..1 in image space.
RIGHT_THIRD = [[0.6, 0.0], [1.0, 0.0], [1.0, 1.0], [0.6, 1.0]]


# ─────────────────────────── helpers ──────────────────────────────────────────

def _kinds(decisions: list) -> list:
    return [d["kind"] for d in decisions]


def _fired(decisions: list, kind: str) -> list:
    """Decisions of the given kind that resulted in any fired_* outcome."""
    return [
        d for d in decisions
        if d["kind"] == kind and d["decision"].startswith("fired_")
    ]


def _suppressed(decisions: list, prefix: str = "suppressed_") -> list:
    return [d for d in decisions if d["decision"].startswith(prefix)]


# ─────────────────────────── smoke ────────────────────────────────────────────

def test_replay_smoke(tmp_path, replay_runner):
    """Pipeline boots in replay mode against a synthetic blank clip and
    exits cleanly with a JSON dump.

    Catches: argparse breakage, replay-mode service-skip regressions, the
    EOF-exit path, the JSON dump path. No corpus clip required — runs in CI
    even on a fresh checkout.
    """
    try:
        import cv2
    except Exception:
        pytest.skip("cv2 unavailable in this environment")

    clip = tmp_path / "blank.mp4"
    # 30 frames of 320x180 black at 10 fps = 3 seconds. Small enough to
    # decode quickly; large enough that the pipeline goes through warmup.
    fourcc = cv2.VideoWriter_fourcc(*"mp4v")
    writer = cv2.VideoWriter(str(clip), fourcc, 10.0, (320, 180))
    blank = np.zeros((180, 320, 3), dtype=np.uint8)
    for _ in range(30):
        writer.write(blank)
    writer.release()
    assert clip.exists() and clip.stat().st_size > 0

    decisions = replay_runner(clip)
    # Blank frames → no detections → maybe zero decisions, maybe a couple
    # of detection_summary suppressions if the YOLO false-positives. Either
    # is acceptable; the smoke test only proves the harness ran end-to-end.
    assert isinstance(decisions, list)


# ─────────────────────────── corpus ──────────────────────────────────────────

def _require_clip(name: str) -> Path:
    """Return clip path or skip the test if missing."""
    clip = CORPUS_DIR / name
    if not clip.exists():
        pytest.skip(
            f"corpus clip missing: {clip.name} — see tests/corpus/README.md"
        )
    return clip


def test_clip_01_forbidden_breach(replay_runner):
    """Operator walks INTO the forbidden polygon. Must fire at least once."""
    clip = _require_clip("clip_01_forbidden_breach.mp4")
    decisions = replay_runner(clip, polygon=RIGHT_THIRD)

    fz = _fired(decisions, "forbidden_zone")
    assert fz, (
        f"forbidden_zone never fired in clip_01\n"
        f"all decisions: {[(d['kind'], d['decision']) for d in decisions]}"
    )
    # Severity must be CRITICAL per the rules table; no operator hint
    # should sneak through the upstream pipeline.
    assert all(d["computed_severity"] == "CRITICAL" for d in fz), fz


def test_clip_02_routine_outside_zone(replay_runner):
    """Operator walks around outside the polygon. forbidden_zone never
    fires; everything else is gated to suppressed by alerts_only_forbidden_zone.
    """
    clip = _require_clip("clip_02_routine.mp4")
    decisions = replay_runner(clip, polygon=RIGHT_THIRD)

    assert not _fired(decisions, "forbidden_zone"), (
        f"forbidden_zone fired unexpectedly during routine clip\n"
        f"firings: {_fired(decisions, 'forbidden_zone')}"
    )
    # Anything that DID get emitted should be suppressed (gate is on by
    # default in CFG). Empty-list is also fine.
    fired_anything = [d for d in decisions if d["decision"].startswith("fired_")]
    assert not fired_anything, (
        f"non-forbidden_zone fires leaked past the gate: {fired_anything}"
    )


def test_clip_03_dedup_sustained_breach(replay_runner):
    """Operator stays inside the polygon for 10+ seconds. Track-aware dedup
    (5 min window) must collapse repeated firings to ONE fired_telegram;
    the rest are suppressed_dedup_track.
    """
    clip = _require_clip("clip_03_dedup_sustained.mp4")
    decisions = replay_runner(clip, polygon=RIGHT_THIRD)

    fired_fz = _fired(decisions, "forbidden_zone")
    suppressed_fz = [
        d for d in decisions
        if d["kind"] == "forbidden_zone"
        and d["decision"] == "suppressed_dedup_track"
    ]
    assert len(fired_fz) == 1, (
        f"expected exactly 1 forbidden_zone fire under track dedup; "
        f"got {len(fired_fz)}: {fired_fz}"
    )
    assert suppressed_fz, (
        "expected suppressed_dedup_track rows for the sustained presence; "
        "got none — is the dedup engine wired in?"
    )
