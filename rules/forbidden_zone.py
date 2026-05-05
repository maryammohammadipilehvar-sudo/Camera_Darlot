"""Forbidden-zone rule (Phase 1b + Restricted Areas Phase 1).

Wraps the existing ``ForbiddenZoneEngine`` (which owns the SQL-backed
polygon table + 5-second hot-reload thread) as a rule. The rule keeps
per-(track_id, zone_id) entry timestamps so the per-zone ``dwell_s``
threshold can be enforced — walking past a zone shouldn't fire if the
zone's dwell is non-zero, but standing inside long enough should.

Per-zone configuration consumed (loaded by the engine from the DB):
  * ``dwell_s``: seconds the track must be inside before firing
                 (0 = fire on first hit, legacy behavior).
  * ``time_window``: ``"*"`` (always-on) or ``"HH:MM-HH:MM"`` (24h,
                     wraps midnight when start > end).
  * ``severity_tier``: stamped into ``detail`` so the central severity
                       table can pick the right row via
                       ``forbidden_zone:<tier>``.

Person-only by design: only ``cls == 0`` (COCO person) tracks are
checked. The engine handles geometry; the rule just iterates tracks
and routes the answer.
"""

from __future__ import annotations

import datetime
from typing import Iterable

from rules.base import Rule, RuleContext, RuleResult


# COCO person class id. Pulled from detect.py's ALLOWED_CLASSES.
_PERSON_CLS = 0

# How long after a track stops appearing before we drop its dwell entry.
# Tracks come back with the same id within this window — keeping the
# entry alive avoids restarting dwell every time tracking briefly drops.
_TRACK_TTL_S = 5.0


def _hhmm_to_minutes(s: str) -> int:
    h, m = s.split(":")
    return int(h) * 60 + int(m)


def _time_window_active(window: str, now_local: datetime.time) -> bool:
    """Return True if the zone is active right now.

    Accepts ``"*"`` (always) or ``"HH:MM-HH:MM"`` (24h). When start <=
    end the window is the half-open interval [start, end). When start >
    end (e.g. ``"20:00-06:00"``) the window wraps midnight; both
    ``[start, 24:00)`` and ``[00:00, end)`` are active.
    """
    if not window or window == "*":
        return True
    try:
        start_s, end_s = window.split("-")
        start = _hhmm_to_minutes(start_s)
        end = _hhmm_to_minutes(end_s)
    except Exception:
        # Malformed window — fail open so the operator notices alerts
        # rather than silently losing them.
        return True
    cur = now_local.hour * 60 + now_local.minute
    if start == end:
        return False  # zero-length window: never active
    if start < end:
        return start <= cur < end
    return cur >= start or cur < end


class ForbiddenZoneRule(Rule):
    """Yield a ``forbidden_zone`` event for every person inside a polygon.

    The rule does not own the engine's lifecycle — the main loop is
    expected to construct ``ForbiddenZoneEngine`` and call
    ``start_reload_thread`` itself. The rule keeps a borrowed reference
    so it can call ``check`` per track per frame.

    Self-disables when the engine is ``None`` (graceful degradation —
    the pipeline keeps running, the rule just emits nothing).

    Attributes:
        name:    ``"forbidden_zone"``.
        engine:  ``ForbiddenZoneEngine`` instance.
    """

    name = "forbidden_zone"

    def __init__(self, engine) -> None:
        self.engine = engine
        if engine is None:
            self.enabled = False
        # Per-(track_id, zone_id) entry timestamp. Used to enforce
        # dwell_s. Pruned each frame for tracks not seen this frame.
        self._entry_ts: dict[tuple[int, int], float] = {}
        # Per-(track_id, zone_id) last-fire timestamp — keeps the rule
        # from re-emitting every frame once the dwell threshold has
        # tripped (central dedup smooths further). Re-fires every
        # max(dwell_s, 1.0) seconds while the track is still inside.
        self._last_fire_ts: dict[tuple[int, int], float] = {}

    def _prune(self, ts: float, alive_keys: set) -> None:
        """Drop entries for tracks that haven't been seen for TTL."""
        stale_cutoff = ts - _TRACK_TTL_S
        for store in (self._entry_ts, self._last_fire_ts):
            dead = [k for k, v in store.items()
                    if k not in alive_keys and v < stale_cutoff]
            for k in dead:
                del store[k]

    def evaluate(self, ctx: RuleContext) -> Iterable[RuleResult]:
        if self.engine is None:
            return
        now = ctx.ts
        now_dt = datetime.datetime.fromtimestamp(now)
        now_local = now_dt.time()
        now_hour = now_dt.hour
        alive_keys: set = set()

        for t in ctx.tracks:
            if t.cls != _PERSON_CLS:
                continue
            zone = self.engine.check(t.cx, t.cy)
            if zone is None:
                continue

            # Time-window gate: outside the active window, the zone is
            # silent and no dwell timer accrues. Reset any in-progress
            # dwell so a track that's inside when the window opens has
            # to dwell again.
            if not _time_window_active(zone.get("time_window", "*"), now_local):
                continue

            zone_id = int(zone.get("id", -1))
            # Phase 4a — learned suppression. A False-alarm click on a
            # past event seeds a (zone, cx, cy, hour) rejection. If this
            # hit matches one, drop it silently — central audit log
            # still records the suppression decision via emit_alert
            # downstream when the operator consults it.
            check_rej = getattr(self.engine, "check_rejection", None)
            if callable(check_rej) and check_rej(zone_id, t.cx, t.cy, now_hour):
                continue
            # Vest-colour authorization (Option A). If this zone has
            # authorized_roles configured AND the operator has set up
            # role colours, run the cheap HSV check against the
            # person's upper torso. A match means "authorized — don't
            # fire". Falls through (returns False) when ctx.frame is
            # None or no role colour is configured for the zone's roles.
            authorized_roles = zone.get("authorized_roles", []) or []
            check_auth = getattr(self.engine, "check_authorized", None)
            if (authorized_roles and ctx.frame is not None and
                    callable(check_auth) and
                    check_auth(ctx.frame, t.bbox, authorized_roles)):
                continue
            tid = t.track_id if t.track_id >= 0 else -1
            key = (tid, zone_id)
            alive_keys.add(key)

            # Untracked detections (no ByteTrack) can't dwell — fire
            # immediately so legacy behavior is preserved when tracking
            # is unavailable.
            dwell_s = float(zone.get("dwell_s", 0.0) or 0.0)

            if tid < 0 or dwell_s <= 0.0:
                # Legacy fast-path: fire every frame; central dedup
                # handles repeat suppression.
                yield self._make_result(t, zone)
                continue

            entry = self._entry_ts.get(key)
            if entry is None:
                self._entry_ts[key] = now
                continue  # not yet dwelled long enough

            elapsed = now - entry
            if elapsed < dwell_s:
                continue

            # Dwell tripped. Re-fire every dwell_s seconds while inside
            # (operator gets fresh telegrams when someone is camped in
            # a zone, central dedup smooths bursts).
            last_fire = self._last_fire_ts.get(key, 0.0)
            if (now - last_fire) < max(dwell_s, 1.0):
                continue
            self._last_fire_ts[key] = now
            yield self._make_result(t, zone, dwell_elapsed_s=elapsed)

        self._prune(now, alive_keys)

    def _make_result(self, t, zone, dwell_elapsed_s: float = 0.0) -> RuleResult:
        detail = {
            "track_id":      t.track_id if t.track_id >= 0 else None,
            "zone":          zone["name"],
            "zone_id":       zone["id"],
            "bbox":          list(t.bbox),
            "label":         f"Person in forbidden zone: {zone['name']}",
            "severity_tier": str(zone.get("severity_tier", "CRITICAL")).upper(),
            "template_kind": str(zone.get("template_kind", "custom")),
            "time_window":   str(zone.get("time_window", "*")),
            "dwell_s":       float(zone.get("dwell_s", 0.0) or 0.0),
        }
        if dwell_elapsed_s > 0:
            detail["dwell_elapsed_s"] = round(dwell_elapsed_s, 2)
        # Shadow mode: zones in their post-deploy shake-down window stamp
        # the event so emit_alert downgrades it to dashboard-only
        # routing (no Telegram). The dashboard uses this flag to render
        # a SHADOW badge so the operator sees nothing was paged.
        shadow_until = int(zone.get("shadow_until", 0) or 0)
        import time as _t
        if shadow_until and _t.time() < shadow_until:
            detail["shadow"] = True
            detail["shadow_until"] = shadow_until
        return RuleResult(kind="forbidden_zone", detail=detail)
