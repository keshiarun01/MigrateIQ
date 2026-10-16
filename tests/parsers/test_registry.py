"""Tests for the parser registry."""

from __future__ import annotations

from pathlib import Path

import pytest

from migrateiq.ir.models import SourceTechnology
from migrateiq.parsers.catalog import Catalog
from migrateiq.parsers.registry import parse_artifact


@pytest.fixture(scope="module")
def catalog() -> Catalog:
    return Catalog.from_ddl(Path(__file__).resolve().parents[2] / "sample_artifacts/schema/insurance_ddl.sql")


@pytest.mark.parametrize(
    ("rel", "technology", "unit_id"),
    [
        ("ssis/Load_DimPolicyholder.dtsx", SourceTechnology.SSIS, "ssis.Load_DimPolicyholder"),
        ("ssis/Load_DimPolicy.dtsx", SourceTechnology.SSIS, "ssis.Load_DimPolicy"),
        (
            "informatica/m_LOAD_FACT_CLAIMS.xml",
            SourceTechnology.INFORMATICA,
            "informatica.m_LOAD_FACT_CLAIMS",
        ),
        ("tsql/usp_Load_FactPremium.sql", SourceTechnology.TSQL, "tsql.usp_Load_FactPremium"),
        ("tsql/trg_Claim_StatusAudit.sql", SourceTechnology.TSQL, "tsql.trg_Claim_StatusAudit"),
    ],
)
def test_dispatch(
    artifacts_dir: Path, catalog: Catalog, rel: str, technology: SourceTechnology, unit_id: str
) -> None:
    unit = parse_artifact(artifacts_dir / rel, catalog)
    assert unit.source_artifact.technology == technology
    assert unit.unit_id == unit_id
    assert unit.complexity is not None


@pytest.mark.parametrize("rel", ["README.md", "schema/insurance_ddl.sql"])
def test_unknown_file_raises(artifacts_dir: Path, catalog: Catalog, rel: str) -> None:
    with pytest.raises(ValueError, match="not a recognized legacy artifact"):
        parse_artifact(artifacts_dir / rel, catalog)
