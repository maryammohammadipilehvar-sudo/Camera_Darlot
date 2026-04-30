"""Rules engine — evaluates a list of Rule instances per frame.

The engine is intentionally thin: it iterates the configured rules,
calls ``evaluate(ctx)`` on each enabled one, and yields
``(rule, result)`` pairs back to the caller. The caller is responsible
for routing each result through ``emit_alert``.

Per-rule failures are caught and logged with a greppable
``RULE %s FAILED`` prefix so one buggy rule never takes the pipeline
down. The greppable prefix matches the convention used elsewhere in
``detect.py`` (``TRACKING DISABLED``, ``BEHAVIOR DISABLED``,
``THERMAL MONITOR error``).
"""

from __future__ import annotations

import logging
from typing import Iterable, Iterator, List, Tuple

from rules.base import Rule, RuleContext, RuleResult


log = logging.getLogger("rules")


class RulesEngine:
    """Drive a list of Rule instances per frame.

    The engine is stateless beyond the rule list itself — all per-rule
    state lives inside the rule instance. Adding/removing rules at
    runtime is supported but not required; the main loop builds the
    engine at startup and never touches it again.

    Attributes:
        rules: Ordered list of registered Rule instances.
    """

    def __init__(self, rules: "List[Rule] | None" = None) -> None:
        self.rules: List[Rule] = list(rules or [])
        # Track rules that have already raised so the log doesn't keep
        # repeating the same traceback every frame; the rule itself
        # stays enabled (transient failures recover) but we down-grade
        # to log.warning after the first traceback.
        self._first_failure_logged: set = set()

    def add(self, rule: Rule) -> None:
        """Append a rule to the evaluation order."""
        self.rules.append(rule)

    def evaluate(self, ctx: RuleContext) -> Iterator[Tuple[Rule, RuleResult]]:
        """Yield one (rule, result) pair for every result every rule emits.

        Disabled rules are skipped. Per-rule exceptions are caught,
        logged, and do not affect subsequent rules. Rules that return
        ``None`` (instead of an iterable) are tolerated — common
        mistake; the engine treats it as no results.

        Args:
            ctx: Per-frame context; same instance handed to every rule.

        Yields:
            Tuples of ``(rule, result)`` for the caller to dispatch.
        """
        for rule in self.rules:
            if not rule.enabled:
                continue
            try:
                results = rule.evaluate(ctx)
            except Exception:
                self._log_rule_failure(rule)
                continue
            if results is None:
                continue
            try:
                for result in results:
                    yield (rule, result)
            except Exception:
                # Iteration itself can raise (generator rules with bugs).
                self._log_rule_failure(rule)

    def shutdown(self) -> None:
        """Call shutdown() on every rule. Errors logged, never raised."""
        for rule in self.rules:
            try:
                rule.shutdown()
            except Exception:
                log.exception("RULE %s SHUTDOWN failed — continuing", rule.name)

    # ── internals ────────────────────────────────────────────────────────────
    def _log_rule_failure(self, rule: Rule) -> None:
        if rule.name in self._first_failure_logged:
            log.warning("RULE %s FAILED (repeat)", rule.name)
        else:
            log.exception("RULE %s FAILED", rule.name)
            self._first_failure_logged.add(rule.name)
