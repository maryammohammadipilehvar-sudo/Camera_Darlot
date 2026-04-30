"""Predicted-intrusion rule (Phase 2).

Real predictive security: extrapolate each tracked person's recent
trajectory ``horizon_s`` seconds into the future and fire a
``predicted_intrusion`` event when the predicted position lands inside
a forbidden zone — *before* the person actually enters.

The rule is pure math. No ML, no model file, no training data. It uses
the last few frames of bbox-centroid history to estimate velocity, then
projects forward by a configurable horizon.

Design notes
- Velocity is averaged over the most recent ~8 samples to absorb
  bbox-jitter without lagging slow turns.
- A minimum speed gate (``predict_min_velocity_px_s``) suppresses
  noise from a stationary person whose centroid drifts a few pixels
  per frame.
- A consecutive-breach counter (``predict_breach_consecutive``) avoids
  firing on a single noisy frame; the predicted breach must hold for
  N straight frames before the alert lands.
- Already-inside-zone tracks are skipped — that case belongs to
  ``ForbiddenZoneRule``; we don't want both rules firing on the same
  frame for the same person.
- Per-track history lives on the rule instance and is pruned when a
  track has been silent for longer than ``predict_track_ttl_s``. The
  global track-loss observer in ``detect.py`` handles the dedup
  ledger separately; this dict is rule-local state only.
"""

from __future__ import annotations

import collections
import math
from typing import Deque, Dict, Iterable, Tuple

from rules.base import Rule, RuleContext, RuleResult


# COCO person class id.
_PERSON_CLS = 0

# Number of recent samples used for velocity averaging. Short enough to
# react to direction changes, long enough to absorb 1-2 frames of jitter.
_VELOCITY_WINDOW = 8

# History deque cap — needs to comfortably exceed _VELOCITY_WINDOW so we
# always have enough samples even when the engine drops a frame.
_HISTORY_MAXLEN = 16


class PredictedIntrusionRule(Rule):
    """Fires when a tracked person's projected path enters a forbidden zone.

    Constructor takes the same ``ForbiddenZoneEngine`` instance that
    ``ForbiddenZoneRule`` uses; both rules share the operator-drawn
    polygons. The rule self-disables when the engine is ``None``.

    Per-track state held on the instance:
        _history:        track_id → deque of (cx, cy, ts).
        _last_seen:      track_id → most-recent ts; used for TTL pruning.
        _breach_streak:  (track_id, zone_id) → consecutive-breach count.
    """

    name = "predicted_intrusion"

    def __init__(self, engine) -> None:
        self.engine = engine
        if engine is None:
            self.enabled = False
        self._history: Dict[int, Deque[Tuple[int, int, float]]] = {}
        self._last_seen: Dict[int, float] = {}
        self._breach_streak: Dict[Tuple[int, int], int] = {}

    def evaluate(self, ctx: RuleContext) -> Iterable[RuleResult]:
        if self.engine is None:
            return

        cfg = ctx.cfg
        horizon = float(cfg.get("predict_horizon_s", 2.5))
        min_frames = int(cfg.get("predict_min_frames", 5))
        min_v = float(cfg.get("predict_min_velocity_px_s", 30.0))
        consec_required = int(cfg.get("predict_breach_consecutive", 3))
        track_ttl = float(cfg.get("predict_track_ttl_s", 3.0))

        active_track_ids = set()

        for t in ctx.tracks:
            if t.cls != _PERSON_CLS or t.track_id < 0:
                continue
            tid = t.track_id
            active_track_ids.add(tid)

            history = self._history.setdefault(
                tid, collections.deque(maxlen=_HISTORY_MAXLEN),
            )
            history.append((t.cx, t.cy, ctx.ts))
            self._last_seen[tid] = ctx.ts

            if len(history) < min_frames:
                continue

            # Skip tracks that are already inside any zone — the
            # ForbiddenZoneRule owns that case.
            if self.engine.check(t.cx, t.cy) is not None:
                self._clear_streaks_for_track(tid)
                continue

            # Compute average velocity over the last _VELOCITY_WINDOW frames.
            recent = list(history)[-_VELOCITY_WINDOW:]
            if len(recent) < 2:
                continue
            dx = recent[-1][0] - recent[0][0]
            dy = recent[-1][1] - recent[0][1]
            dt = recent[-1][2] - recent[0][2]
            if dt <= 0:
                continue
            vx = dx / dt
            vy = dy / dt
            speed = math.hypot(vx, vy)

            if speed < min_v:
                # Stationary or near-stationary — don't predict and don't
                # accumulate breach streaks (otherwise a small jitter at
                # the edge of a zone could pile up over time).
                self._clear_streaks_for_track(tid)
                continue

            px = t.cx + vx * horizon
            py = t.cy + vy * horizon

            zone = self.engine.check(int(px), int(py))
            if zone is None:
                self._clear_streaks_for_track(tid)
                continue

            streak_key = (tid, int(zone["id"]))
            count = self._breach_streak.get(streak_key, 0) + 1
            self._breach_streak[streak_key] = count

            if count >= consec_required:
                # Reset the streak so we don't re-yield every frame; the
                # central dedup smooths from here.
                self._breach_streak[streak_key] = 0
                yield RuleResult(
                    kind="predicted_intrusion",
                    detail={
                        "track_id":      tid,
                        "zone":          zone["name"],
                        "zone_id":       int(zone["id"]),
                        "bbox":          list(t.bbox),
                        "current":       [int(t.cx), int(t.cy)],
                        "predicted":     [int(px), int(py)],
                        "velocity_px_s": round(speed, 1),
                        "horizon_s":     horizon,
                        "label": (
                            f"Predicted entry into {zone['name']} "
                            f"in ~{horizon:.1f}s"
                        ),
                    },
                )

        # TTL prune: drop history for tracks that have been silent longer
        # than predict_track_ttl_s. Cheap O(n) scan; n is small (number
        # of distinct ids the rule has ever seen in the last few seconds).
        if track_ttl > 0:
            stale = [
                tid for tid, last in self._last_seen.items()
                if ctx.ts - last > track_ttl
            ]
            for tid in stale:
                self._history.pop(tid, None)
                self._last_seen.pop(tid, None)
                self._clear_streaks_for_track(tid)

    def _clear_streaks_for_track(self, track_id: int) -> None:
        """Drop every (track_id, *zone_id*) streak entry for this track."""
        keys = [k for k in self._breach_streak if k[0] == track_id]
        for k in keys:
            self._breach_streak.pop(k, None)
