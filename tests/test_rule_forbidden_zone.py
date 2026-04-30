"""Unit tests for ForbiddenZoneRule (Phase 1b).

Stub the engine — we're testing the rule's filter logic, not
ForbiddenZoneEngine's SQL/geometry. The engine has its own integration
in production and the existing replay corpus.
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest


REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))


from rules import RuleContext, TrackedObject  # noqa: E402
from rules.forbidden_zone import ForbiddenZoneRule  # noqa: E402


class _FakeEngine:
    """Returns a fixed zone for any (cx, cy) lookup."""
    def __init__(self, zone=None):
        self._zone = zone
        self.calls: list = []

    def check(self, cx, cy):
        self.calls.append((cx, cy))
        return self._zone


def _ctx(tracks):
    return RuleContext(
        camera_id="cam_01",
        ts=1.0,
        n_frame=1,
        mode="OCCUPIED",
        cfg={},
        tracks=tracks,
        has_tracker=True,
        action_by_track={},
        frame=None,
    )


def _person(track_id=1, cls=0, cx=100, cy=100):
    return TrackedObject(
        track_id=track_id,
        bbox=(cx-10, cy-10, cx+10, cy+10),
        cls=cls,
        conf=0.9,
        cx=cx,
        cy=cy,
    )


def test_yields_one_result_per_person_inside_zone():
    engine = _FakeEngine(zone={"id": 7, "name": "Loading dock"})
    rule = ForbiddenZoneRule(engine)

    out = list(rule.evaluate(_ctx([_person(1), _person(2)])))
    assert len(out) == 2
    assert out[0].kind == "forbidden_zone"
    assert out[0].detail["zone"] == "Loading dock"
    assert out[0].detail["zone_id"] == 7
    assert out[0].detail["track_id"] == 1
    assert out[1].detail["track_id"] == 2


def test_skips_non_person_classes():
    engine = _FakeEngine(zone={"id": 7, "name": "X"})
    rule = ForbiddenZoneRule(engine)
    # cls=2 (car), cls=67 (cell phone) — both should be ignored.
    out = list(rule.evaluate(_ctx([_person(cls=2), _person(cls=67)])))
    assert out == []
    assert engine.calls == []


def test_yields_nothing_when_engine_returns_none():
    engine = _FakeEngine(zone=None)
    rule = ForbiddenZoneRule(engine)
    out = list(rule.evaluate(_ctx([_person(1), _person(2)])))
    assert out == []
    # But the engine WAS asked twice — the rule still queries every person.
    assert len(engine.calls) == 2


def test_self_disables_when_engine_is_none():
    rule = ForbiddenZoneRule(engine=None)
    assert rule.enabled is False
    # And evaluate() yields nothing even if accidentally called.
    out = list(rule.evaluate(_ctx([_person(1)])))
    assert out == []


def test_track_id_minus_one_normalised_to_none():
    engine = _FakeEngine(zone={"id": 1, "name": "z"})
    rule = ForbiddenZoneRule(engine)
    out = list(rule.evaluate(_ctx([_person(track_id=-1)])))
    assert len(out) == 1
    assert out[0].detail["track_id"] is None
