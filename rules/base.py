"""Rule contract — base class plus the dataclasses every rule consumes/emits.

Rules operate on a per-frame ``RuleContext`` (already-normalised tracks +
config + behavior labels) and yield ``RuleResult`` objects describing
events to surface. A Rule is *not* responsible for severity, dedup, or
notification routing — those happen downstream in
``detect.emit_alert``. A Rule is purely "does this frame match my
condition".

Rules are intentionally small. Anything that needs persistent state
across frames (e.g. dwell timers, hot-reloaded zone polygons) is held
inside the Rule instance, *not* the context — the context is rebuilt
every frame.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from typing import Any, Iterable, Mapping, Optional, Sequence


@dataclass(frozen=True)
class TrackedObject:
    """Single tracked detection in pixel coordinates of the inference frame.

    Attributes:
        track_id: ByteTrack id, or ``-1`` when tracking is unavailable.
        bbox:     ``(x1, y1, x2, y2)`` in inference-frame pixels (ints).
        cls:      COCO class id (e.g. 0 = person, 67 = cell phone).
        conf:     Detection confidence in [0, 1].
        cx:       Centroid x (precomputed; rules use it for zone tests).
        cy:       Centroid y.
    """

    track_id: int
    bbox: tuple
    cls: int
    conf: float
    cx: int
    cy: int


@dataclass(frozen=True)
class RuleContext:
    """Per-frame inputs handed to every Rule.evaluate() call.

    The context is treated as immutable from the rule's perspective. The
    main loop is the single writer; rules read.

    Attributes:
        camera_id:        Source camera identifier (``cfg["camera_id"]``).
        ts:               ``time.time()`` captured at frame-grab time.
        n_frame:          Monotonic frame counter (1-indexed).
        mode:             Resolved operating mode (``OCCUPIED`` /
                          ``CLOSED`` / ``MAINTENANCE``).
        cfg:              Pipeline config dict — rules read their own
                          slice from ``cfg["rules"][rule_name]``.
        tracks:           Normalised tracks for this frame.
        has_tracker:      ``True`` when ByteTrack is wired; ``False``
                          means every ``track_id`` is ``-1`` and rules
                          that need persistent ids should noop.
        action_by_track:  Behavior labels keyed by track_id, e.g.
                          ``{4: {"action": "sitting", "next_action":
                          "standing", "track_id": 4}}``.
        frame:            Raw inference frame (BGR, ``np.ndarray``)
                          for rules that need pixels (PPE crops, etc.).
                          ``None`` when rules can't expect it.
    """

    camera_id: str
    ts: float
    n_frame: int
    mode: str
    cfg: Mapping[str, Any]
    tracks: Sequence[TrackedObject]
    has_tracker: bool
    action_by_track: Mapping[int, Mapping[str, Any]] = field(default_factory=dict)
    frame: Optional[Any] = None  # numpy array; Any to avoid hard numpy import here


@dataclass
class RuleResult:
    """One event to surface, returned by a rule's evaluate() call.

    The ``kind`` and ``detail`` are passed straight through to
    ``emit_alert(camera_id, kind, detail)``. Severity is computed
    downstream from the kind/detail/mode triple — rules do not assign it.

    Attributes:
        kind:   Event kind string (e.g. ``"forbidden_zone"``,
                ``"loitering"``, ``"ppe_violation"``).
        detail: Free-form payload. Conventional keys: ``track_id``,
                ``bbox``, ``zone``, ``label``. The orchestrator strips
                operator-supplied severity hints (computed wins).
    """

    kind: str
    detail: dict


class Rule(ABC):
    """Abstract base for all rules.

    Subclasses set ``name`` and implement ``evaluate(ctx)``. Rules are
    instantiated once at startup and reused across frames. State stored
    on ``self`` persists across frames (use a lock if the same instance
    is touched from multiple threads — currently the main loop is the
    only caller, so rules can assume single-threaded ``evaluate``).

    Attributes:
        name:     Human-readable rule name; used in logs and audit.
        enabled:  When ``False`` the engine skips ``evaluate``. Rules
                  may also self-disable on init failure (set
                  ``enabled=False`` in __init__ and log a reason) so the
                  pipeline keeps running with a missing dependency.
    """

    name: str = "unnamed"
    enabled: bool = True

    @abstractmethod
    def evaluate(self, ctx: RuleContext) -> Iterable[RuleResult]:
        """Yield zero or more RuleResult objects for this frame.

        Implementations should be fast (the main loop budget is ~100ms
        per frame). Long-running setup belongs in ``__init__`` or a
        background thread the rule owns.
        """
        ...

    def shutdown(self) -> None:
        """Hook for rules that own background threads. Default no-op."""
        return None
