"""Tests for the golden comparison helper."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from migrateiq.ir.models import MigrationUnit
from tests.helpers.golden import assert_matches_golden

GOLDEN = Path(__file__).resolve().parent.parent / "golden" / "Load_DimPolicyholder.ir.json"


def _golden_doc() -> dict:
    return json.loads(GOLDEN.read_text(encoding="utf-8"))


def test_ignores_sha256_and_parsed_at() -> None:
    doc = _golden_doc()
    doc["source_artifact"]["sha256"] = "deadbeef"
    doc["source_artifact"]["parsed_at"] = "2030-01-01T00:00:00Z"
    assert_matches_golden(MigrationUnit.model_validate(doc), GOLDEN)


def test_reports_changed_field_by_path() -> None:
    doc = _golden_doc()
    doc["steps"][0]["mappings"][2]["expression"]["spark_sql"] = "months_between(current_date(), dob) / 12"
    with pytest.raises(AssertionError, match=r"steps\[0\]\.mappings\[2\]\.expression\.spark_sql"):
        assert_matches_golden(MigrationUnit.model_validate(doc), GOLDEN)


def test_reports_length_mismatch() -> None:
    doc = _golden_doc()
    doc["dq_rules"].pop()
    with pytest.raises(AssertionError, match=r"dq_rules: length 3 != expected 4"):
        assert_matches_golden(MigrationUnit.model_validate(doc), GOLDEN)


def test_v1_0_golden_ignores_v1_1_fields() -> None:
    doc = _golden_doc()
    doc["ir_version"] = "1.1.0"
    doc["parameters"] = [{"name": "run_ts", "kind": "run_ts", "legacy_refs": ["GETDATE()"]}]
    doc["targets"][0]["load_strategy"]["insert_only_columns"] = ["dw_load_ts"]
    assert_matches_golden(MigrationUnit.model_validate(doc), GOLDEN)


def test_v1_1_golden_compares_v1_1_fields(tmp_path: Path) -> None:
    doc = _golden_doc()
    doc["ir_version"] = "1.1.0"
    v11 = tmp_path / "v11.ir.json"
    v11.write_text(json.dumps(doc), encoding="utf-8")
    doc["parameters"] = [{"name": "run_ts", "kind": "run_ts"}]
    with pytest.raises(AssertionError, match="parameters: length 1 != expected 0"):
        assert_matches_golden(MigrationUnit.model_validate(doc), v11)
