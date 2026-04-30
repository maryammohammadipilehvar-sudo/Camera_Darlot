"""Unit tests for LoiteringRule + ZoneEngine.observe (Phase 1c).

Covers both the rule's per-track iteration and the ZoneEngine's dwell
math against a synthetic single-zone setup. No GPU, no MQTT, no DB.
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest


REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))


from rules import RuleContext, TrackedObject  # noqa: E402
from rules.loitering import LoiteringRule  # noqa: E402


def _import_detect():
    import detect  # noqa: F401
    return detect


def _zone_engine(dwell_sec: float = 5.0):
    """Build a ZoneEngine with a single 100x100 square zone at origin."""
    detect = _import_detect()
    return detect.ZoneEngine([{
        "name": "test_zone",
        "polygon": [[0, 0], [100, 0], [100, 100], [0, 100]],
        "dwell_sec": dwell_sec,
    }])


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


def _track(track_id=1, cx=50, cy=50, cls=0):
    return TrackedObject(
        track_id=track_id,
        bbox=(cx-5, cy-5, cx+5, cy+5),
        cls=cls,
        conf=0.9,
        cx=cx,
        cy=cy,
    )


# ──────────────────── ZoneEngine.observe directly ─────────────────────────────


def test_observe_no_alert_before_dwell_threshold(monkeypatch):
    detect = _import_detect()
    eng = _zone_engine(dwell_sec=5.0)

    fake_now = [1000.0]
    monkeypatch.setattr(detect.time, "time", lambda: fake_now[0])

    # First entry — dwell starts at 1000.
    out = list(eng.observe(track_id=1, cx=50, cy=50))
    assert out == []

    # 4s later — still under 5s threshold.
    fake_now[0] += 4
    out = list(eng.observe(track_id=1, cx=50, cy=50))
    assert out == []


def test_observe_yields_alert_after_dwell_threshold(monkeypatch):
    detect = _import_detect()
    eng = _zone_engine(dwell_sec=5.0)

    fake_now = [2000.0]
    monkeypatch.setattr(detect.time, "time", lambda: fake_now[0])

    list(eng.observe(track_id=1, cx=50, cy=50))  # dwell starts
    fake_now[0] += 6
    out = list(eng.observe(track_id=1, cx=50, cy=50))
    assert len(out) == 1
    detail = out[0]
    assert detail["zone"] == "test_zone"
    assert detail["track_id"] == 1
    assert detail["dwell_sec"] >= 5.0
    assert "Loitering in test_zone" in detail["label"]


def test_observe_clears_dwell_when_track_leaves_zone(monkeypatch):
    detect = _import_detect()
    eng = _zone_engine(dwell_sec=5.0)

    fake_now = [3000.0]
    monkeypatch.setattr(detect.time, "time", lambda: fake_now[0])

    # Inside.
    list(eng.observe(track_id=1, cx=50, cy=50))
    # Outside (200, 200) — well outside the 100x100 zone.
    list(eng.observe(track_id=1, cx=200, cy=200))
    # Back inside, 6s later — dwell starts FRESH; no alert yet.
    fake_now[0] += 6
    out = list(eng.observe(track_id=1, cx=50, cy=50))
    assert out == []


def test_observe_re_alerts_on_sustained_presence(monkeypatch):
    """Two alerts within dwell_sec * 2 — verifies the timer reset on fire."""
    detect = _import_detect()
    eng = _zone_engine(dwell_sec=5.0)

    fake_now = [4000.0]
    monkeypatch.setattr(detect.time, "time", lambda: fake_now[0])

    list(eng.observe(track_id=1, cx=50, cy=50))   # dwell start
    fake_now[0] += 6
    out1 = list(eng.observe(track_id=1, cx=50, cy=50))  # alert 1
    fake_now[0] += 6
    out2 = list(eng.observe(track_id=1, cx=50, cy=50))  # alert 2

    assert len(out1) == 1
    assert len(out2) == 1


# ──────────────────── LoiteringRule integration ───────────────────────────────


def test_rule_yields_one_result_per_engine_yield(monkeypatch):
    detect = _import_detect()
    eng = _zone_engine(dwell_sec=5.0)
    rule = LoiteringRule(eng)

    fake_now = [5000.0]
    monkeypatch.setattr(detect.time, "time", lambda: fake_now[0])

    list(rule.evaluate(_ctx([_track(1, 50, 50)])))
    fake_now[0] += 6
    out = list(rule.evaluate(_ctx([_track(1, 50, 50)])))

    assert len(out) == 1
    assert out[0].kind == "loitering"
    assert out[0].detail["track_id"] == 1


def test_rule_collapses_untracked_to_id_zero(monkeypatch):
    """Legacy contract: track_id=-1 (no tracker) becomes id=0 in the dwell ledger."""
    detect = _import_detect()
    eng = _zone_engine(dwell_sec=5.0)
    rule = LoiteringRule(eng)

    fake_now = [6000.0]
    monkeypatch.setattr(detect.time, "time", lambda: fake_now[0])

    list(rule.evaluate(_ctx([_track(track_id=-1, cx=50, cy=50)])))
    fake_now[0] += 6
    out = list(rule.evaluate(_ctx([_track(track_id=-1, cx=50, cy=50)])))

    assert len(out) == 1
    assert out[0].detail["track_id"] == 0


def test_rule_self_disables_when_engine_is_none():
    rule = LoiteringRule(engine=None)
    assert rule.enabled is False
    out = list(rule.evaluate(_ctx([_track(1)])))
    assert out == []


def test_rule_handles_empty_track_list():
    eng = _zone_engine()
    rule = LoiteringRule(eng)
    out = list(rule.evaluate(_ctx([])))
    assert out == []
