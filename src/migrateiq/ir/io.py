"""Load and dump ``MigrationUnit`` documents with stable formatting."""

from __future__ import annotations

import json
from pathlib import Path

from migrateiq.ir.models import MigrationUnit


def load_unit(path: Path) -> MigrationUnit:
    """Read and validate a ``MigrationUnit`` JSON file."""
    return MigrationUnit.model_validate_json(path.read_text(encoding="utf-8"))


def dump_unit(unit: MigrationUnit) -> str:
    """Serialize a unit to JSON with sorted keys, 2-space indent, and a trailing newline.

    Only object keys are sorted. List order (steps, columns, mappings) carries meaning and is kept.
    """
    return json.dumps(unit.model_dump(mode="json"), indent=2, sort_keys=True, ensure_ascii=False) + "\n"


def write_unit(unit: MigrationUnit, path: Path) -> None:
    """Write ``dump_unit(unit)`` to ``path``, creating parent directories."""
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(dump_unit(unit), encoding="utf-8")
