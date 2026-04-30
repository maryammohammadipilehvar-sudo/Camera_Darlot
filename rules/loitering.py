"""Loitering rule (Phase 1c).

Wraps the existing ``ZoneEngine``, which holds per-track-per-zone dwell
timers and yields a detail dict every time a track's dwell exceeds the
configured threshold for any zone. The rule iterates ctx.tracks and
routes each yielded detail through ``RuleResult``.

Behaviour-preserving port: legacy ``zones.update(track_id_or_0, cx, cy,
camera_id)`` collapsed every untracked detection to ``track_id=0``
(AUDIT Risk #3). That collapse is preserved here for parity until
ByteTrack-availability is universal in production. ``track_id == -1``
(no tracker) becomes ``0`` for the dwell ledger, matching the legacy
``track_id if track_id >= 0 else 0`` semantics.
"""

from __future__ import annotations

from typing import Iterable

from rules.base import Rule, RuleContext, RuleResult


class LoiteringRule(Rule):
    """Yield ``loitering`` events for every per-track-per-zone dwell trip.

    The rule does not own zone state — ``ZoneEngine`` does. The rule is
    a thin per-frame iterator that hands each track to the engine and
    yields the engine's detail dicts unchanged.

    Attributes:
        name:    ``"loitering"``.
        engine:  ``ZoneEngine`` instance (duck-typed; we only call
                 ``.observe(track_id, cx, cy) -> Iterable[dict]``).
    """

    name = "loitering"

    def __init__(self, engine) -> None:
        self.engine = engine
        if engine is None:
            self.enabled = False

    def evaluate(self, ctx: RuleContext) -> Iterable[RuleResult]:
        if self.engine is None:
            return
        for t in ctx.tracks:
            # Preserve legacy contract: untracked detections collapse to
            # id 0. AUDIT Risk #3 documents the false-loitering risk
            # this produces when ByteTrack is unavailable; resolution
            # tracked separately and out of scope for this port.
            tid = t.track_id if t.track_id >= 0 else 0
            for detail in self.engine.observe(tid, t.cx, t.cy):
                yield RuleResult(kind="loitering", detail=detail)
