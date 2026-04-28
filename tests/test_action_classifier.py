"""Unit tests for ActionClassifier — focused on the fighting false-positive
fix in Phase 3.

The classifier consumes a TrackWindow (deques of bboxes + keypoints) and
returns one of the Action.* string labels. These tests synthesise small
windows that target specific decision branches.

Most importantly: a single person making fast wrist movements at a desk
must NOT be classified as FIGHTING. Operator-reported false positive
2026-04-28.
"""

from __future__ import annotations

import sys
from pathlib import Path

import numpy as np
import pytest


REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))


from behavior import (  # noqa: E402
    Action,
    ActionClassifier,
    KP_L_HIP, KP_R_HIP,
    KP_L_KNEE, KP_R_KNEE,
    KP_L_SHOULDER, KP_R_SHOULDER,
    KP_L_WRIST, KP_R_WRIST,
    TrackWindow,
)


def _make_kp(positions, confidences=None) -> np.ndarray:
    """Build a (17, 3) keypoint array from a dict of {index: (x, y)}.

    Missing indices default to (0, 0) with confidence 0.0.
    """
    arr = np.zeros((17, 3), dtype=np.float32)
    for idx, (x, y) in positions.items():
        arr[idx, 0] = x
        arr[idx, 1] = y
        arr[idx, 2] = (confidences or {}).get(idx, 0.9)
    return arr


def _seated_keypoints(t: float) -> np.ndarray:
    """Generate keypoints for a person seated at a desk.

    - Shoulders + hips at fixed positions in the bbox.
    - Knees at hip level (occluded by desk → low confidence).
    - Wrists ARE confidently tracked (hands on desk surface) and
      jiggle erratically, simulating typing / reaching for a mug.
      Returns array (17, 3) with confidence in column 2.
    """
    # Wrists move ~5 px each frame in random-ish patterns.
    lw_x = 100 + 5 * np.sin(t * 7)
    lw_y = 200 + 4 * np.cos(t * 5)
    rw_x = 200 + 5 * np.sin(t * 6 + 1)
    rw_y = 200 + 4 * np.cos(t * 8)

    return _make_kp(
        positions={
            KP_L_SHOULDER: (110, 120),
            KP_R_SHOULDER: (190, 120),
            KP_L_HIP:      (110, 240),
            KP_R_HIP:      (190, 240),
            KP_L_KNEE:     (110, 250),  # near hip — knees hidden under desk
            KP_R_KNEE:     (190, 250),
            KP_L_WRIST:    (lw_x, lw_y),
            KP_R_WRIST:    (rw_x, rw_y),
        },
        confidences={
            KP_L_SHOULDER: 0.9, KP_R_SHOULDER: 0.9,
            KP_L_HIP: 0.7,      KP_R_HIP: 0.7,
            KP_L_KNEE: 0.2,     KP_R_KNEE: 0.2,  # low conf — desk occludes
            KP_L_WRIST: 0.85,   KP_R_WRIST: 0.85,
        },
    )


def _seated_window(n_frames: int = 8) -> TrackWindow:
    """Build a TrackWindow representing 8 frames of seated activity."""
    w = TrackWindow(track_id=1)
    bbox = (50, 80, 250, 320)  # tall bbox, mostly upper body visible
    for i in range(n_frames):
        kp = _seated_keypoints(t=i * 0.1)
        w.push(kps=kp, bbox=bbox)
    return w


def _aggressive_two_person_window(n_frames: int = 8) -> TrackWindow:
    """Window for one of two people fighting:
       - Body bbox is moving (not stationary).
       - Both wrists are confidently tracked AND moving fast each frame.

    Wrist excursions of ~120 px in a 240-px-tall bbox simulate
    punching/flailing — normalised speed ≈ 0.5, well above the 0.35 gate.
    """
    w = TrackWindow(track_id=1)
    for i in range(n_frames):
        # Move the bbox by ~10 px per frame — body is in motion.
        bbox = (50 + i * 10, 80, 250 + i * 10, 320)
        # Wrists alternate ±60 px from a midline — net frame-to-frame
        # delta is 120 px, normalised by h=240 → 0.5 per-frame velocity.
        lw_y = 150 + (i % 2) * 120
        rw_y = 250 - (i % 2) * 120
        kp = _make_kp(
            positions={
                KP_L_SHOULDER: (110 + i * 10, 120),
                KP_R_SHOULDER: (190 + i * 10, 120),
                KP_L_HIP:      (110 + i * 10, 240),
                KP_R_HIP:      (190 + i * 10, 240),
                KP_L_KNEE:     (110 + i * 10, 290),
                KP_R_KNEE:     (190 + i * 10, 290),
                KP_L_WRIST:    (60 + i * 10, lw_y),
                KP_R_WRIST:    (240 + i * 10, rw_y),
            },
            confidences={
                KP_L_WRIST: 0.85, KP_R_WRIST: 0.85,
                KP_L_SHOULDER: 0.9, KP_R_SHOULDER: 0.9,
                KP_L_HIP: 0.8, KP_R_HIP: 0.8,
                KP_L_KNEE: 0.7, KP_R_KNEE: 0.7,
            },
        )
        w.push(kps=kp, bbox=bbox)
    return w


# ───────────────────────── tests ──────────────────────────────────────────────


def test_solo_seated_never_fights():
    """Operator-reported regression: sitting at desk → fighting.

    Even with wrists wiggling fast, multi_person=False must block fighting.
    """
    classifier = ActionClassifier()
    w = _seated_window(8)
    result = classifier.classify(w, multi_person=False)
    assert result != Action.FIGHTING, (
        f"Solo seated person classified as {result!r}; should never be FIGHTING"
    )


def test_solo_with_fast_wrists_still_not_fighting():
    """Even an aggressive-looking single person isn't fighting alone."""
    classifier = ActionClassifier()
    w = _aggressive_two_person_window(8)
    result = classifier.classify(w, multi_person=False)
    assert result != Action.FIGHTING


def test_two_persons_fighting_does_fire():
    """When multi_person=True AND all gates pass, FIGHTING fires."""
    classifier = ActionClassifier()
    w = _aggressive_two_person_window(8)
    result = classifier.classify(w, multi_person=True)
    assert result == Action.FIGHTING


def test_low_confidence_wrists_block_fighting():
    """Bad keypoints → fake velocity. Confidence gate stops the false fire."""
    classifier = ActionClassifier()
    w = TrackWindow(track_id=1)
    for i in range(8):
        bbox = (50 + i * 10, 80, 250 + i * 10, 320)
        kp = _make_kp(
            positions={
                KP_L_SHOULDER: (110, 120), KP_R_SHOULDER: (190, 120),
                KP_L_HIP: (110, 240),      KP_R_HIP: (190, 240),
                KP_L_KNEE: (110, 290),     KP_R_KNEE: (190, 290),
                # Wrists "jump" 100 px each frame → fast on paper —
                # but confidence is only 0.3, below the 0.5 gate.
                KP_L_WRIST: (60 + (i % 2) * 100, 150),
                KP_R_WRIST: (240 - (i % 2) * 100, 150),
            },
            confidences={
                KP_L_WRIST: 0.3, KP_R_WRIST: 0.3,
                KP_L_SHOULDER: 0.9, KP_R_SHOULDER: 0.9,
                KP_L_HIP: 0.8, KP_R_HIP: 0.8,
                KP_L_KNEE: 0.7, KP_R_KNEE: 0.7,
            },
        )
        w.push(kps=kp, bbox=bbox)
    assert classifier.classify(w, multi_person=True) != Action.FIGHTING


def test_one_wrist_fast_other_resting_not_fighting():
    """Typing pattern: one wrist on mouse moves, other is still — not fighting."""
    classifier = ActionClassifier()
    w = TrackWindow(track_id=1)
    for i in range(8):
        bbox = (50 + i * 8, 80, 250 + i * 8, 320)  # body moving
        kp = _make_kp(
            positions={
                KP_L_SHOULDER: (110, 120), KP_R_SHOULDER: (190, 120),
                KP_L_HIP: (110, 240),      KP_R_HIP: (190, 240),
                KP_L_KNEE: (110, 290),     KP_R_KNEE: (190, 290),
                # Right wrist moves 80 px/frame, left wrist barely moves.
                KP_L_WRIST: (60 + i, 200),       # ~1 px/frame
                KP_R_WRIST: (240 + (i % 2) * 80, 200),  # 80 px alternating
            },
            confidences={
                KP_L_WRIST: 0.85, KP_R_WRIST: 0.85,
                KP_L_SHOULDER: 0.9, KP_R_SHOULDER: 0.9,
                KP_L_HIP: 0.8, KP_R_HIP: 0.8,
                KP_L_KNEE: 0.7, KP_R_KNEE: 0.7,
            },
        )
        w.push(kps=kp, bbox=bbox)
    assert classifier.classify(w, multi_person=True) != Action.FIGHTING


def test_stationary_body_blocks_fighting_even_with_fast_wrists():
    """Two stationary people waving hands isn't fighting."""
    classifier = ActionClassifier()
    w = TrackWindow(track_id=1)
    for i in range(8):
        bbox = (50, 80, 250, 320)  # NOT moving
        kp = _make_kp(
            positions={
                KP_L_SHOULDER: (110, 120), KP_R_SHOULDER: (190, 120),
                KP_L_HIP: (110, 240),      KP_R_HIP: (190, 240),
                KP_L_KNEE: (110, 290),     KP_R_KNEE: (190, 290),
                KP_L_WRIST: (60, 150 + (i % 2) * 80),
                KP_R_WRIST: (240, 250 - (i % 2) * 80),
            },
            confidences={
                KP_L_WRIST: 0.85, KP_R_WRIST: 0.85,
                KP_L_SHOULDER: 0.9, KP_R_SHOULDER: 0.9,
                KP_L_HIP: 0.8, KP_R_HIP: 0.8,
                KP_L_KNEE: 0.7, KP_R_KNEE: 0.7,
            },
        )
        w.push(kps=kp, bbox=bbox)
    assert classifier.classify(w, multi_person=True) != Action.FIGHTING


def test_multi_person_default_is_false_backward_compat():
    """Old call sites that pass no multi_person kwarg behave as solo (no fight)."""
    classifier = ActionClassifier()
    w = _aggressive_two_person_window(8)
    # No kwarg — uses default False.
    assert classifier.classify(w) != Action.FIGHTING


def test_fallen_still_works_with_landscape_bbox():
    """Sanity: the FALLEN rule (aspect > 1.6) is unaffected by Phase 3."""
    classifier = ActionClassifier()
    w = TrackWindow(track_id=1)
    # Two frames of a landscape bbox (a person on the floor).
    w.push(kps=None, bbox=(50, 200, 350, 280))
    w.push(kps=None, bbox=(50, 200, 350, 280))
    assert classifier.classify(w, multi_person=False) == Action.FALLEN
