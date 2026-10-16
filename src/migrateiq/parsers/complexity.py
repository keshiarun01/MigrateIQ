"""Score a ``MigrationUnit`` and choose its migration path.

The score is a pure function of the IR, so the graph can re-score after translation and reconciliation.
The read-only end-to-end golden records the final state, e.g. ``pending_expressions: 0``.
"""

from __future__ import annotations

import logging

from migrateiq.ir.models import (
    AggregateStep,
    Complexity,
    DeduplicateStep,
    DerivedColumnStep,
    FilterStep,
    JoinStep,
    LoadMode,
    LookupStep,
    MigrationPath,
    MigrationUnit,
    RouterStep,
    UnionStep,
)

logger = logging.getLogger(__name__)

#: Always reported, even when zero (they appear in the end-to-end golden).
CORE_WEIGHTS: dict[str, int] = {"derived_columns": 2, "lookups": 8, "joins": 10, "pending_expressions": 1}
#: Reported only when non-zero.
EXTRA_WEIGHTS: dict[str, int] = {
    "routers": 6,
    "aggregates": 6,
    "dedupes": 6,
    "filters": 2,
    "unions": 3,
    "scd2_targets": 15,
    "merge_targets": 12,
    "watermarks": 5,
    "post_load_actions": 2,
    "unsupported_constructs": 20,
}
BASE_SCORE = 5
PER_TARGET = 2
TEMPLATE_MAX_STRUCTURAL = 24
MANUAL_MIN_SCORE = 85
MANUAL_KINDS = frozenset({"trigger"})


def _counts(unit: MigrationUnit) -> dict[str, int]:
    steps = unit.steps
    modes = [t.load_strategy.mode for t in unit.targets]
    return {
        "derived_columns": sum(len(s.mappings) for s in steps if isinstance(s, DerivedColumnStep)),
        "lookups": sum(isinstance(s, LookupStep) for s in steps),
        "joins": sum(isinstance(s, JoinStep) for s in steps),
        "pending_expressions": len(unit.pending_expressions()),
        "routers": sum(isinstance(s, RouterStep) for s in steps),
        "aggregates": sum(isinstance(s, AggregateStep) for s in steps),
        "dedupes": sum(isinstance(s, DeduplicateStep) for s in steps),
        "filters": sum(isinstance(s, FilterStep) for s in steps),
        "unions": sum(isinstance(s, UnionStep) for s in steps),
        "scd2_targets": modes.count(LoadMode.SCD2),
        "merge_targets": modes.count(LoadMode.MERGE_UPSERT),
        "watermarks": sum(t.load_strategy.watermark is not None for t in unit.targets),
        "post_load_actions": len(unit.post_load_actions),
        "unsupported_constructs": len(unit.unsupported_constructs),
    }


def score(unit: MigrationUnit) -> Complexity:
    """Compute the complexity score, factors, and recommended path of a unit.

    ``score = min(100, 5 + 2 * targets + sum(weight * count))``. The path is decided by structure, not by
    the number of pending expressions: ``manual`` for triggers or unsupported constructs without a suggested
    pattern, ``template`` when the structural score is at most 24, ``manual`` when it is 85 or more, otherwise
    ``llm``. The structural score is the score without the pending-expression contribution.

    Args:
        unit: The unit to score. It is not modified.

    Returns:
        A ``Complexity``. ``factors`` always holds the core factors, and other factors only when non-zero.
    """
    counts = _counts(unit)
    weights = {**CORE_WEIGHTS, **EXTRA_WEIGHTS}
    total = BASE_SCORE + PER_TARGET * len(unit.targets) + sum(weights[k] * v for k, v in counts.items())
    structural = total - CORE_WEIGHTS["pending_expressions"] * counts["pending_expressions"]
    value = min(100, total)

    if any(u.kind in MANUAL_KINDS or not u.suggested_pattern for u in unit.unsupported_constructs):
        path = MigrationPath.MANUAL
    elif structural <= TEMPLATE_MAX_STRUCTURAL:
        path = MigrationPath.TEMPLATE
    elif structural >= MANUAL_MIN_SCORE:
        path = MigrationPath.MANUAL
    else:
        path = MigrationPath.LLM

    factors = {k: counts[k] for k in CORE_WEIGHTS}
    factors.update({k: counts[k] for k in EXTRA_WEIGHTS if counts[k]})
    logger.debug("complexity %s: score=%d structural=%d path=%s", unit.unit_id, value, structural, path.value)
    return Complexity(score=value, factors=factors, recommended_path=path)
