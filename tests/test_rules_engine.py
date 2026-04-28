"""Unit tests for the rules engine scaffold (Phase 1a).

Exercises the contract — Rule + RuleContext + RuleResult + RulesEngine —
with synthetic in-memory rules. No detect.py side effects, no GPU, no
DB. Runs in well under one second.
"""

from __future__ import annotations

import sys
from pathlib import Path
from typing import Iterable

import pytest


REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))


from rules import Rule, RuleContext, RuleResult, RulesEngine, TrackedObject  # noqa: E402


def _ctx(tracks=(), **overrides):
    base = dict(
        camera_id="cam_01",
        ts=1000.0,
        n_frame=1,
        mode="OCCUPIED",
        cfg={},
        tracks=list(tracks),
        has_tracker=True,
        action_by_track={},
        frame=None,
    )
    base.update(overrides)
    return RuleContext(**base)


class _AlwaysFires(Rule):
    name = "always_fires"

    def __init__(self, kind: str = "test_kind") -> None:
        self._kind = kind

    def evaluate(self, ctx: RuleContext) -> Iterable[RuleResult]:
        yield RuleResult(kind=self._kind, detail={"n": ctx.n_frame})


class _PerTrack(Rule):
    name = "per_track"

    def evaluate(self, ctx: RuleContext) -> Iterable[RuleResult]:
        for t in ctx.tracks:
            yield RuleResult(
                kind="per_track_event",
                detail={"track_id": t.track_id, "cls": t.cls},
            )


class _Broken(Rule):
    name = "broken"

    def evaluate(self, ctx: RuleContext) -> Iterable[RuleResult]:
        raise RuntimeError("simulated rule bug")


class _BrokenIterator(Rule):
    name = "broken_iter"

    def evaluate(self, ctx: RuleContext) -> Iterable[RuleResult]:
        # Returns a fine value, then raises mid-iteration.
        yield RuleResult(kind="ok_then_break", detail={})
        raise RuntimeError("mid-iteration boom")


class _ReturnsNone(Rule):
    name = "returns_none"

    def evaluate(self, ctx: RuleContext):
        return None  # tolerated by the engine


class _Disabled(Rule):
    name = "disabled"
    enabled = False

    def evaluate(self, ctx: RuleContext) -> Iterable[RuleResult]:
        yield RuleResult(kind="should_not_appear", detail={})


def test_engine_yields_results_in_rule_order():
    engine = RulesEngine([_AlwaysFires("a"), _AlwaysFires("b")])
    out = list(engine.evaluate(_ctx()))
    assert [r.kind for _, r in out] == ["a", "b"]


def test_engine_skips_disabled_rules():
    engine = RulesEngine([_Disabled(), _AlwaysFires("after")])
    out = list(engine.evaluate(_ctx()))
    assert [r.kind for _, r in out] == ["after"]


def test_engine_swallows_rule_failure_and_continues(caplog):
    engine = RulesEngine([_Broken(), _AlwaysFires("survivor")])
    with caplog.at_level("ERROR", logger="rules"):
        out = list(engine.evaluate(_ctx()))
    assert [r.kind for _, r in out] == ["survivor"]
    assert any("RULE broken FAILED" in rec.message for rec in caplog.records)


def test_engine_swallows_iterator_failure(caplog):
    engine = RulesEngine([_BrokenIterator(), _AlwaysFires("survivor")])
    with caplog.at_level("ERROR", logger="rules"):
        out = list(engine.evaluate(_ctx()))
    # First yield passes through; mid-iter exception is logged; survivor still runs.
    kinds = [r.kind for _, r in out]
    assert "ok_then_break" in kinds
    assert "survivor" in kinds


def test_engine_repeat_failure_only_logs_traceback_once(caplog):
    engine = RulesEngine([_Broken()])
    with caplog.at_level("WARNING", logger="rules"):
        list(engine.evaluate(_ctx()))
        list(engine.evaluate(_ctx()))
        list(engine.evaluate(_ctx()))
    # One full ERROR (with traceback) + two WARNINGs (one per repeat call).
    errors = [r for r in caplog.records if r.levelname == "ERROR"]
    warnings_ = [r for r in caplog.records if r.levelname == "WARNING"]
    assert len(errors) == 1
    assert len(warnings_) == 2


def test_engine_tolerates_none_return():
    engine = RulesEngine([_ReturnsNone(), _AlwaysFires("after")])
    out = list(engine.evaluate(_ctx()))
    assert [r.kind for _, r in out] == ["after"]


def test_per_track_rule_sees_tracks():
    tracks = [
        TrackedObject(track_id=1, bbox=(0, 0, 10, 10), cls=0, conf=0.9, cx=5, cy=5),
        TrackedObject(track_id=2, bbox=(20, 20, 30, 30), cls=0, conf=0.8, cx=25, cy=25),
    ]
    engine = RulesEngine([_PerTrack()])
    out = list(engine.evaluate(_ctx(tracks=tracks)))
    assert [r.detail["track_id"] for _, r in out] == [1, 2]


def test_engine_add_appends_to_order():
    engine = RulesEngine()
    engine.add(_AlwaysFires("first"))
    engine.add(_AlwaysFires("second"))
    out = list(engine.evaluate(_ctx()))
    assert [r.kind for _, r in out] == ["first", "second"]


def test_shutdown_calls_every_rule_even_if_one_raises(caplog):
    calls = []

    class _A(Rule):
        name = "a"
        def evaluate(self, ctx): return ()
        def shutdown(self): calls.append("a")

    class _B(Rule):
        name = "b"
        def evaluate(self, ctx): return ()
        def shutdown(self): raise RuntimeError("shutdown bug")

    class _C(Rule):
        name = "c"
        def evaluate(self, ctx): return ()
        def shutdown(self): calls.append("c")

    engine = RulesEngine([_A(), _B(), _C()])
    with caplog.at_level("ERROR", logger="rules"):
        engine.shutdown()
    assert calls == ["a", "c"]
    assert any("RULE b SHUTDOWN failed" in rec.message for rec in caplog.records)
