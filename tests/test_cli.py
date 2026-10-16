"""Tests for the ``migrateiq`` CLI."""

from __future__ import annotations

from pathlib import Path

import pytest
from typer.testing import CliRunner

from migrateiq.cli import app
from migrateiq.ir.models import MigrationUnit

ARTIFACTS = [
    "sample_artifacts/ssis/Load_DimPolicyholder.dtsx",
    "sample_artifacts/ssis/Load_DimPolicy.dtsx",
    "sample_artifacts/informatica/m_LOAD_FACT_CLAIMS.xml",
    "sample_artifacts/tsql/usp_Load_FactPremium.sql",
    "sample_artifacts/tsql/trg_Claim_StatusAudit.sql",
]


@pytest.fixture
def runner(monkeypatch: pytest.MonkeyPatch, repo_root: Path) -> CliRunner:
    monkeypatch.chdir(repo_root)
    monkeypatch.delenv("MIGRATEIQ_CATALOG", raising=False)
    return CliRunner()


@pytest.mark.parametrize("artifact", ARTIFACTS)
def test_parse_prints_valid_unit(runner: CliRunner, artifact: str) -> None:
    result = runner.invoke(app, ["parse", artifact])
    assert result.exit_code == 0, result.stderr
    unit = MigrationUnit.model_validate_json(result.stdout)
    assert unit.complexity is not None
    assert unit.source_artifact.file_path == artifact


def test_warnings_go_to_stderr_not_stdout(runner: CliRunner) -> None:
    result = runner.invoke(app, ["parse", "sample_artifacts/informatica/m_LOAD_FACT_CLAIMS.xml"])
    assert result.exit_code == 0
    assert "inferred link" in result.stderr
    MigrationUnit.model_validate_json(result.stdout)


def test_out_writes_same_json(runner: CliRunner, tmp_path: Path) -> None:
    artifact = ARTIFACTS[0]
    printed = runner.invoke(app, ["parse", artifact]).stdout
    out = tmp_path / "ir" / "unit.json"
    result = runner.invoke(app, ["parse", artifact, "--out", str(out)])
    assert result.exit_code == 0
    assert result.stdout == ""
    a = MigrationUnit.model_validate_json(out.read_text(encoding="utf-8"))
    b = MigrationUnit.model_validate_json(printed)
    assert a.model_dump(exclude={"source_artifact": {"parsed_at"}}) == b.model_dump(
        exclude={"source_artifact": {"parsed_at"}}
    )


@pytest.mark.parametrize(
    "artifact", ["sample_artifacts/README.md", "sample_artifacts/schema/insurance_ddl.sql"]
)
def test_unknown_file_exits_1(runner: CliRunner, artifact: str) -> None:
    result = runner.invoke(app, ["parse", artifact])
    assert result.exit_code == 1
    assert "not a recognized legacy artifact" in result.stderr


def test_missing_artifact_exits_1(runner: CliRunner) -> None:
    result = runner.invoke(app, ["parse", "nope.dtsx"])
    assert result.exit_code == 1
    assert "artifact not found" in result.stderr


def test_missing_catalog_from_env_exits_1(runner: CliRunner, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("MIGRATEIQ_CATALOG", "does/not/exist.sql")
    result = runner.invoke(app, ["parse", ARTIFACTS[0]])
    assert result.exit_code == 1
    assert "catalog not found" in result.stderr


def test_catalog_option(runner: CliRunner) -> None:
    result = runner.invoke(
        app, ["parse", ARTIFACTS[0], "--catalog", "sample_artifacts/schema/insurance_ddl.sql"]
    )
    assert result.exit_code == 0
