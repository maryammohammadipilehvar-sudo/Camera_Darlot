"""Rules package — declarative event detection on top of the perception stack.

Each rule reads a normalised ``RuleContext`` (built once per frame by the
main loop) and yields zero or more ``RuleResult`` objects. The
``RulesEngine`` orchestrates evaluation and surfaces a per-rule failure
log so one bad rule cannot kill the others.

Public API:
    - Rule              — abstract base; implement ``evaluate(ctx)``.
    - RuleContext       — per-frame inputs for evaluate().
    - RuleResult        — what a rule emits; routed to emit_alert.
    - TrackedObject     — normalised single track record.
    - RulesEngine       — drives a list of Rule instances per frame.
"""

from rules.base import Rule, RuleContext, RuleResult, TrackedObject
from rules.engine import RulesEngine

__all__ = [
    "Rule",
    "RuleContext",
    "RuleResult",
    "TrackedObject",
    "RulesEngine",
]
