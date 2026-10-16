"""Tests for IR load/dump helpers."""

from __future__ import annotations

import json
from pathlib import Path

from migrateiq.ir.io import dump_unit, load_unit, write_unit

GOLDEN = Path(__file__).resolve().parent.parent / "golden" / "Load_DimPolicyholder.ir.json"


def test_roundtrip(tmp_path: Path) -> None:
    unit = load_unit(GOLDEN)
    out = tmp_path / "nested" / "unit.json"
    write_unit(unit, out)
    assert load_unit(out) == unit


def test_dump_is_stable_and_sorted() -> None:
    unit = load_unit(GOLDEN)
    first = dump_unit(unit)
    assert first == dump_unit(load_unit(GOLDEN))
    assert first.endswith("\n")
    doc = json.loads(first)
    assert list(doc) == sorted(doc)


def test_list_order_preserved() -> None:
    unit = load_unit(GOLDEN)
    doc = json.loads(dump_unit(unit))
    names = [m["target_column"] for m in doc["steps"][0]["mappings"]]
    assert names == ["full_name", "email_normalized", "age_band", "dw_load_ts"]
