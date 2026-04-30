"""Forbidden-zone rule (Phase 1b).

Wraps the existing ``ForbiddenZoneEngine`` (which owns the SQL-backed
polygon table + 5-second hot-reload thread) as a rule. The rule itself
is stateless — every frame, it asks the engine "is this person inside
any drawn polygon?" and yields one ``RuleResult`` per hit.

Person-only by design: only ``cls == 0`` (COCO person) tracks are
checked. The engine handles the geometry; the rule just iterates
tracks and routes the answer.
"""

from __future__ import annotations

from typing import Iterable, Optional

from rules.base import Rule, RuleContext, RuleResult


# COCO person class id. Pulled from detect.py's ALLOWED_CLASSES.
_PERSON_CLS = 0


class ForbiddenZoneRule(Rule):
    """Yield a ``forbidden_zone`` event for every person inside a polygon.

    The rule does not own the engine's lifecycle — the main loop is
    expected to construct ``ForbiddenZoneEngine`` and call
    ``start_reload_thread`` itself. The rule keeps a borrowed reference
    so it can call ``check`` per track per frame.

    Self-disables when the engine is ``None`` (graceful degradation —
    the pipeline keeps running, the rule just emits nothing). The
    engine itself returns ``None`` from ``check`` when no zones are
    loaded, which also produces no events without any rule-side logic.

    Attributes:
        name:    ``"forbidden_zone"``.
        engine:  ``ForbiddenZoneEngine`` instance (duck-typed; we only
                 call ``.check(cx, cy) -> Optional[dict]``).
    """

    name = "forbidden_zone"

    def __init__(self, engine) -> None:
        self.engine = engine
        if engine is None:
            self.enabled = False

    def evaluate(self, ctx: RuleContext) -> Iterable[RuleResult]:
        if self.engine is None:
            return
        for t in ctx.tracks:
            if t.cls != _PERSON_CLS:
                continue
            zone = self.engine.check(t.cx, t.cy)
            if zone is None:
                continue
            yield RuleResult(
                kind="forbidden_zone",
                detail={
                    "track_id": t.track_id if t.track_id >= 0 else None,
                    "zone":     zone["name"],
                    "zone_id":  zone["id"],
                    "bbox":     list(t.bbox),
                    "label":    f"Person in forbidden zone: {zone['name']}",
                },
            )
