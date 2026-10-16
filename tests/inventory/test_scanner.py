"""Tests for artifact discovery."""

from __future__ import annotations

from pathlib import Path

from migrateiq.inventory.scanner import detect, scan
from migrateiq.ir.models import SourceTechnology
from migrateiq.parsers.base import sha256_file


def test_scan_sample_artifacts(artifacts_dir: Path) -> None:
    found = {a.path.name: (a.technology, a.object_type) for a in scan(artifacts_dir)}
    assert found == {
        "Load_DimPolicy.dtsx": (SourceTechnology.SSIS, "package"),
        "Load_DimPolicyholder.dtsx": (SourceTechnology.SSIS, "package"),
        "m_LOAD_FACT_CLAIMS.xml": (SourceTechnology.INFORMATICA, "mapping"),
        "trg_Claim_StatusAudit.sql": (SourceTechnology.TSQL, "trigger"),
        "usp_Load_FactPremium.sql": (SourceTechnology.TSQL, "procedure"),
    }


def test_scan_is_sorted_and_hashed(artifacts_dir: Path) -> None:
    found = scan(artifacts_dir)
    assert [a.path for a in found] == sorted(a.path for a in found)
    assert all(a.sha256 == sha256_file(a.path) for a in found)


def test_ddl_and_readme_skipped(artifacts_dir: Path) -> None:
    assert detect(artifacts_dir / "schema" / "insurance_ddl.sql") is None
    assert detect(artifacts_dir / "README.md") is None


def test_plain_xml_skipped(tmp_path: Path) -> None:
    other = tmp_path / "other.xml"
    other.write_text("<root/>", encoding="utf-8")
    assert detect(other) is None
