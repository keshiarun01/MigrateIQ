"""Tests for complexity scoring and path routing."""

from __future__ import annotations

import copy
import json
from pathlib import Path
from typing import Any

import pytest

from migrateiq.ir.models import MigrationPath, MigrationUnit, UnsupportedConstruct
from migrateiq.parsers.catalog import Catalog
from migrateiq.parsers.complexity import CORE_WEIGHTS, score
from migrateiq.parsers.registry import parse_artifact

GOLDEN = Path(__file__).resolve().parents[1] / "golden" / "Load_DimPolicyholder.ir.json"

EXPECTED_PATHS = {
    "ssis/Load_DimPolicyholder.dtsx": MigrationPath.TEMPLATE,
    "ssis/Load_DimPolicy.dtsx": MigrationPath.LLM,
    "informatica/m_LOAD_FACT_CLAIMS.xml": MigrationPath.LLM,
    "tsql/usp_Load_FactPremium.sql": MigrationPath.LLM,
    "tsql/trg_Claim_StatusAudit.sql": MigrationPath.MANUAL,
}


@pytest.fixture(scope="module")
def catalog() -> Catalog:
    return Catalog.from_ddl(Path(__file__).resolve().parents[2] / "sample_artifacts/schema/insurance_ddl.sql")


def _golden() -> dict[str, Any]:
    return json.loads(GOLDEN.read_text(encoding="utf-8"))


def test_score_reproduces_golden_complexity() -> None:
    doc = _golden()
    expected = doc["complexity"]
    doc["complexity"] = None
    got = score(MigrationUnit.model_validate(doc))
    assert got.model_dump(mode="json") == expected


@pytest.mark.parametrize(("rel", "path"), sorted(EXPECTED_PATHS.items()))
def test_recommended_paths(artifacts_dir: Path, catalog: Catalog, rel: str, path: MigrationPath) -> None:
    unit = parse_artifact(artifacts_dir / rel, catalog)
    assert unit.complexity is not None
    assert unit.complexity.recommended_path == path


def test_pending_expressions_do_not_change_path() -> None:
    doc = _golden()
    for mapping in doc["steps"][0]["mappings"]:
        mapping["expression"].update(spark_sql=None, translation_status="pending")
    got = score(MigrationUnit.model_validate(doc))
    assert got.factors["pending_expressions"] == 4
    assert got.score == 19
    assert got.recommended_path == MigrationPath.TEMPLATE


def test_unsupported_without_pattern_is_manual() -> None:
    unit = MigrationUnit.model_validate(_golden())
    unit.unsupported_constructs = [
        UnsupportedConstruct(kind="cursor", original_snippet="DECLARE c CURSOR", reason="x")
    ]
    got = score(unit)
    assert got.recommended_path == MigrationPath.MANUAL
    assert got.factors["unsupported_constructs"] == 1


def test_trigger_is_manual_even_with_pattern() -> None:
    unit = MigrationUnit.model_validate(_golden())
    unit.unsupported_constructs = [
        UnsupportedConstruct(
            kind="trigger", original_snippet="CREATE TRIGGER", reason="x", suggested_pattern="CDC"
        )
    ]
    assert score(unit).recommended_path == MigrationPath.MANUAL


def test_score_capped_and_large_units_manual() -> None:
    doc = _golden()
    base = copy.deepcopy(doc["steps"][0]["mappings"][0])
    doc["steps"][0]["mappings"] = [{**copy.deepcopy(base), "target_column": f"c{i}"} for i in range(60)]
    got = score(MigrationUnit.model_validate(doc))
    assert got.score == 100
    assert got.recommended_path == MigrationPath.MANUAL


def test_zero_factors_omitted_except_core() -> None:
    got = score(MigrationUnit.model_validate(_golden()))
    assert set(got.factors) == set(CORE_WEIGHTS)
