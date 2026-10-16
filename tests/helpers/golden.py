"""Compare a ``MigrationUnit`` with a golden JSON file."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from migrateiq.ir.models import MigrationUnit

IGNORED = (("source_artifact", "sha256"), ("source_artifact", "parsed_at"))

# Fields added in IR v1.1. A v1.0 golden cannot contain them, so they are dropped from both sides
# when the golden is v1.0.x (see docs/ir_v1_1_proposal.md).
V1_1_UNIT_FIELDS = ("ir_version", "parameters", "post_load_actions")
V1_1_LOAD_STRATEGY_FIELDS = (
    "update_condition",
    "insert_only_columns",
    "update_only_columns",
    "watermark",
    "end_date_sentinel",
    "surrogate_key_strategy",
)


def _normalize(doc: dict[str, Any], golden_version: str) -> dict[str, Any]:
    """Drop fields that legitimately differ between runs or between IR versions."""
    for parent, key in IGNORED:
        doc.get(parent, {}).pop(key, None)
    if golden_version.startswith("1.0."):
        for key in V1_1_UNIT_FIELDS:
            doc.pop(key, None)
        for target in doc.get("targets", []):
            for key in V1_1_LOAD_STRATEGY_FIELDS:
                target.get("load_strategy", {}).pop(key, None)
    return doc


def diff(expected: Any, actual: Any, path: str = "") -> list[str]:
    """Return a readable list of differences between two JSON values, by path."""
    if isinstance(expected, dict) and isinstance(actual, dict):
        out: list[str] = []
        for key in sorted(set(expected) | set(actual)):
            sub = f"{path}.{key}" if path else key
            if key not in actual:
                out.append(f"{sub}: missing (expected {expected[key]!r})")
            elif key not in expected:
                out.append(f"{sub}: unexpected {actual[key]!r}")
            else:
                out.extend(diff(expected[key], actual[key], sub))
        return out
    if isinstance(expected, list) and isinstance(actual, list):
        out = []
        if len(expected) != len(actual):
            out.append(f"{path}: length {len(actual)} != expected {len(expected)}")
        for i, (e, a) in enumerate(zip(expected, actual, strict=False)):
            out.extend(diff(e, a, f"{path}[{i}]"))
        return out
    return [] if expected == actual else [f"{path}: {actual!r} != expected {expected!r}"]


def golden_diff(unit: MigrationUnit, golden: MigrationUnit) -> list[str]:
    """Differences between ``unit`` and ``golden`` after normalization (empty list = match)."""
    version = golden.ir_version
    expected = _normalize(golden.model_dump(mode="json"), version)
    actual = _normalize(unit.model_dump(mode="json"), version)
    return diff(expected, actual)


def assert_matches_golden(unit: MigrationUnit, golden_path: Path) -> None:
    """Assert ``unit`` equals the golden, ignoring ``source_artifact.sha256`` and ``parsed_at``.

    Both sides are validated through ``MigrationUnit`` first so defaults are filled the same way.
    If the golden is IR v1.0.x, ``ir_version`` and v1.1-only fields are compared on neither side.
    """
    golden = MigrationUnit.model_validate(json.loads(golden_path.read_text(encoding="utf-8")))
    problems = golden_diff(unit, golden)
    assert not problems, f"IR differs from {golden_path.name}:\n  " + "\n  ".join(problems)
