"""Unit tests for PredictedIntrusionRule (Phase 2).

Synthetic trajectories drive the rule: we feed sequences of frames
where each frame is one ``RuleContext`` containing a single tracked
person. The fake ``ForbiddenZoneEngine`` returns a fixed zone for any
(cx, cy) lookup that lands inside a configurable rectangle.

These are pure unit tests — no GPU, no DB, no MQTT, no real pipeline.
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest


REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))


from rules import RuleContext, TrackedObject  # noqa: E402
from rules.predicted_intrusion import PredictedIntrusionRule  # noqa: E402


class _RectZoneEngine:
    """Trivial forbidden-zone engine: returns a zone iff (cx,cy) is inside rect.

    rect is (x1, y1, x2, y2). zone payload is {"id": 1, "name": "rect"}.
    """

    def __init__(self, rect):
        self.rect = rect
        self._zone = {"id": 1, "name": "rect"}

    def check(self, cx, cy):
        x1, y1, x2, y2 = self.rect
        if x1 <= cx <= x2 and y1 <= cy <= y2:
            return self._zone
        return None


def _ctx(track, ts, cfg=None):
    """Build a single-frame RuleContext with one TrackedObject."""
    return RuleContext(
        camera_id="cam_01",
        ts=ts,
        n_frame=int(ts * 10),
        mode="OCCUPIED",
        cfg=cfg or {},
        tracks=[track],
        has_tracker=True,
        action_by_track={},
        frame=None,
    )


def _person(track_id, cx, cy):
    return TrackedObject(
        track_id=track_id,
        bbox=(cx - 10, cy - 20, cx + 10, cy + 20),
        cls=0,
        conf=0.9,
        cx=cx,
        cy=cy,
    )


def _cfg(**overrides):
    base = dict(
        predict_horizon_s=2.5,
        predict_min_frames=5,
        predict_min_velocity_px_s=30.0,
        predict_breach_consecutive=3,
        predict_track_ttl_s=3.0,
    )
    base.update(overrides)
    return base


def _step_track(rule, track_id, start, velocity, n_frames, dt=0.1, cfg=None):
    """Step a track in a straight line and collect all RuleResults.

    Returns the list of (frame_index, RuleResult) emitted.
    """
    cx, cy = start
    vx, vy = velocity
    t0 = 1000.0
    out = []
    for i in range(n_frames):
        ts = t0 + i * dt
        person = _person(track_id, int(cx), int(cy))
        for r in rule.evaluate(_ctx(person, ts, cfg=cfg)):
            out.append((i, r))
        cx += vx * dt
        cy += vy * dt
    return out


def test_self_disables_when_engine_is_none():
    rule = PredictedIntrusionRule(engine=None)
    assert rule.enabled is False
    out = list(rule.evaluate(_ctx(_person(1, 100, 100), ts=1.0)))
    assert out == []


def test_no_fire_below_min_frames():
    """Need at least predict_min_frames history before any prediction."""
    rule = PredictedIntrusionRule(_RectZoneEngine((200, 200, 400, 400)))
    cfg = _cfg(predict_min_frames=5, predict_breach_consecutive=1)
    # Only 4 frames of history, heading straight at the zone.
    out = _step_track(rule, 1, start=(50, 250), velocity=(500, 0),
                      n_frames=4, cfg=cfg)
    assert out == []


def test_fire_when_velocity_projects_into_zone():
    """Person walking toward a forbidden zone fires before they enter."""
    rule = PredictedIntrusionRule(_RectZoneEngine((200, 200, 400, 400)))
    # Person at (50, 250) moving +x at 100 px/s. At horizon 2.5s they
    # would be at x = 50 + 100*2.5 = 300 — inside the zone.
    cfg = _cfg(
        predict_horizon_s=2.5,
        predict_min_frames=5,
        predict_min_velocity_px_s=30.0,
        predict_breach_consecutive=3,
    )
    out = _step_track(rule, 1, start=(50, 250), velocity=(100, 0),
                      n_frames=15, cfg=cfg)
    assert len(out) >= 1, "should fire at least once"
    _, first = out[0]
    assert first.kind == "predicted_intrusion"
    assert first.detail["track_id"] == 1
    assert first.detail["zone"] == "rect"
    assert first.detail["zone_id"] == 1
    assert first.detail["velocity_px_s"] >= 30


def test_no_fire_when_already_inside_zone():
    """Person already inside is the forbidden_zone rule's job, not ours."""
    rule = PredictedIntrusionRule(_RectZoneEngine((200, 200, 400, 400)))
    cfg = _cfg(predict_min_frames=2, predict_breach_consecutive=1)
    # Start INSIDE the zone, moving slowly.
    out = _step_track(rule, 1, start=(300, 300), velocity=(50, 0),
                      n_frames=10, cfg=cfg)
    assert out == [], "no predicted_intrusion when already inside"


def test_no_fire_for_stationary_track():
    """Stationary track: velocity below gate → no prediction."""
    rule = PredictedIntrusionRule(_RectZoneEngine((200, 200, 400, 400)))
    cfg = _cfg(predict_min_velocity_px_s=30.0, predict_breach_consecutive=1)
    # Standing still at (50, 250) — velocity is zero, no prediction.
    out = _step_track(rule, 1, start=(50, 250), velocity=(0, 0),
                      n_frames=20, cfg=cfg)
    assert out == []


def test_no_fire_for_path_that_misses_the_zone():
    """Walking parallel to the zone never produces a predicted entry."""
    rule = PredictedIntrusionRule(_RectZoneEngine((200, 200, 400, 400)))
    cfg = _cfg(predict_breach_consecutive=1)
    # Walking right but well above the zone (y=100 stays away from y∈[200,400]).
    out = _step_track(rule, 1, start=(50, 100), velocity=(200, 0),
                      n_frames=20, cfg=cfg)
    assert out == []


def test_consecutive_streak_required():
    """One-frame predicted breach should NOT fire — needs 3 consecutive."""
    eng = _RectZoneEngine((200, 200, 400, 400))
    rule = PredictedIntrusionRule(eng)
    cfg = _cfg(predict_breach_consecutive=3, predict_min_frames=2,
               predict_min_velocity_px_s=10.0)

    # Build up history with a path NOT heading into the zone.
    _step_track(rule, 1, start=(50, 100), velocity=(50, 0), n_frames=6, cfg=cfg)

    # Single noisy frame projecting into the zone (must iterate the
    # generator so the rule actually runs).
    list(rule.evaluate(_ctx(_person(1, 80, 250), ts=1010.0, cfg=cfg)))
    # Back to safe path — streak gets reset.
    list(rule.evaluate(_ctx(_person(1, 81, 100), ts=1010.1, cfg=cfg)))
    # Continue safe path — no fire.
    out = list(rule.evaluate(_ctx(_person(1, 82, 100), ts=1010.2, cfg=cfg)))
    assert out == []


def test_streak_reset_when_velocity_drops_below_gate():
    """Stopping mid-approach must reset the streak — important real case.

    A worker walks toward the zone (streak builds up), then pauses at
    the edge. The pause must NOT preserve the partial streak, or a
    later resumption could over-fire.
    """
    rule = PredictedIntrusionRule(_RectZoneEngine((200, 200, 400, 400)))
    cfg = _cfg(predict_breach_consecutive=3, predict_min_frames=2,
               predict_min_velocity_px_s=30.0)

    # Build 2 frames of breach streak by approaching.
    _step_track(rule, 1, start=(100, 250), velocity=(100, 0),
                n_frames=4, cfg=cfg)
    # Now stand still (10 frames of zero velocity).
    _step_track(rule, 1, start=(140, 250), velocity=(0, 0),
                n_frames=10, cfg=cfg)
    # Streak should have been cleared by the velocity-gate path.
    assert all(v == 0 or k[0] != 1 for k, v in rule._breach_streak.items()), (
        f"streak should be 0 for track 1, got {rule._breach_streak}"
    )


def test_track_ttl_prunes_stale_history():
    """Tracks not seen for predict_track_ttl_s get their history dropped."""
    rule = PredictedIntrusionRule(_RectZoneEngine((200, 200, 400, 400)))
    cfg = _cfg(predict_track_ttl_s=2.0)

    # Build history for track 1 at ts=1000. Must iterate the generator
    # for the body to execute and populate the history dict.
    for i in range(6):
        list(rule.evaluate(
            _ctx(_person(1, 50 + i, 100), ts=1000.0 + i * 0.1, cfg=cfg)
        ))
    assert 1 in rule._history

    # Jump time forward by 5 seconds with no track 1 reports. Drive a
    # different track to advance ctx.ts past the TTL.
    list(rule.evaluate(_ctx(_person(2, 50, 100), ts=1005.0, cfg=cfg)))

    # Track 1's history is older than TTL — should have been pruned.
    assert 1 not in rule._history
    assert 1 not in rule._last_seen


def test_skips_non_person_classes():
    """Only person tracks (cls=0) are projected — phones / vehicles ignored."""
    rule = PredictedIntrusionRule(_RectZoneEngine((200, 200, 400, 400)))
    cfg = _cfg(predict_min_frames=2, predict_breach_consecutive=1,
               predict_min_velocity_px_s=10.0)

    phone = TrackedObject(
        track_id=5, bbox=(40, 240, 60, 260), cls=67, conf=0.9, cx=50, cy=250,
    )
    out = []
    for i in range(8):
        ts = 1000.0 + i * 0.1
        # Move phone toward the zone.
        moved = TrackedObject(
            track_id=5, bbox=(40+i*30, 240, 60+i*30, 260),
            cls=67, conf=0.9, cx=50+i*30, cy=250,
        )
        for r in rule.evaluate(_ctx(moved, ts, cfg=cfg)):
            out.append(r)
    assert out == [], "non-person classes should be ignored"


def test_skips_untracked_objects():
    """track_id < 0 (no ByteTrack) cannot be predicted — needs continuity."""
    rule = PredictedIntrusionRule(_RectZoneEngine((200, 200, 400, 400)))
    cfg = _cfg(predict_min_frames=2, predict_breach_consecutive=1,
               predict_min_velocity_px_s=10.0)

    out = []
    for i in range(8):
        person = TrackedObject(
            track_id=-1, bbox=(40+i*30, 240, 60+i*30, 260),
            cls=0, conf=0.9, cx=50+i*30, cy=250,
        )
        for r in rule.evaluate(_ctx(person, 1000.0 + i * 0.1, cfg=cfg)):
            out.append(r)
    assert out == [], "untracked detections cannot be predicted"


def test_payload_contains_predicted_position_and_velocity():
    """Telegram + dashboard need the geometric details for an evidence card."""
    rule = PredictedIntrusionRule(_RectZoneEngine((200, 200, 400, 400)))
    cfg = _cfg(predict_breach_consecutive=1, predict_min_frames=2)
    out = _step_track(rule, 1, start=(50, 250), velocity=(100, 0),
                      n_frames=10, cfg=cfg)
    assert len(out) >= 1
    detail = out[0][1].detail
    assert "predicted" in detail
    assert "current" in detail
    assert "velocity_px_s" in detail
    assert "horizon_s" in detail
    assert detail["predicted"][0] >= 200, (
        f"predicted x should be inside zone, got {detail['predicted']}"
    )
