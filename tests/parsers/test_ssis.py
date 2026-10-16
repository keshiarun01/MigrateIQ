"""Tests for the deterministic SSIS parser."""

from __future__ import annotations

import json
import logging
from pathlib import Path

import pytest

from migrateiq.ir.io import dump_unit
from migrateiq.ir.models import (
    DerivedColumnStep,
    LoadMode,
    MigrationUnit,
    TranslationStatus,
)
from migrateiq.parsers.catalog import Catalog
from migrateiq.parsers.ssis import LINEAGE_NOTE, SsisParser, slug
from tests.helpers.golden import golden_diff

DDL = Path("sample_artifacts/schema/insurance_ddl.sql")
POLICYHOLDER = Path("sample_artifacts/ssis/Load_DimPolicyholder.dtsx")
POLICY = Path("sample_artifacts/ssis/Load_DimPolicy.dtsx")


@pytest.fixture
def parser(repo_root: Path, monkeypatch: pytest.MonkeyPatch) -> SsisParser:
    monkeypatch.chdir(repo_root)
    return SsisParser(Catalog.from_ddl(DDL))


def _without_parsed_at(unit: MigrationUnit) -> str:
    doc = json.loads(dump_unit(unit))
    doc["source_artifact"].pop("parsed_at")
    return json.dumps(doc, indent=2, sort_keys=True)


@pytest.mark.parametrize("path", [POLICYHOLDER, POLICY])
def test_fixture_parses_to_valid_unit(parser: SsisParser, path: Path) -> None:
    unit = parser.parse(path)
    MigrationUnit.model_validate_json(dump_unit(unit))
    assert unit.source_artifact.file_path == path.as_posix()
    assert unit.unit_id == f"ssis.{path.stem}"
    assert unit.complexity is None
    assert unit.unsupported_constructs == []


@pytest.mark.parametrize("path", [POLICYHOLDER, POLICY])
def test_deterministic_output(repo_root: Path, monkeypatch: pytest.MonkeyPatch, path: Path) -> None:
    monkeypatch.chdir(repo_root)
    first = SsisParser(Catalog.from_ddl(DDL)).parse(path)
    second = SsisParser(Catalog.from_ddl(DDL)).parse(path)
    assert _without_parsed_at(first) == _without_parsed_at(second)


def test_absolute_path_under_cwd_is_relative(parser: SsisParser, repo_root: Path) -> None:
    unit = parser.parse(repo_root / POLICYHOLDER)
    assert unit.source_artifact.file_path == POLICYHOLDER.as_posix()


def test_golden_dim_policyholder(parser: SsisParser, repo_root: Path) -> None:
    unit = parser.parse(POLICYHOLDER)
    golden = json.loads((repo_root / "tests/golden/Load_DimPolicyholder.ir.json").read_text(encoding="utf-8"))
    golden["complexity"] = None
    step = unit.steps[0]
    assert isinstance(step, DerivedColumnStep)
    parsed = {m.target_column: m.expression for m in step.mappings}
    for mapping in golden["steps"][0]["mappings"]:
        expression = parsed[mapping["target_column"]]
        if expression.translation_status == TranslationStatus.PENDING:
            mapping["expression"].update(spark_sql=None, translation_status="pending", notes=expression.notes)
    golden_unit = MigrationUnit.model_validate(golden)
    assert golden_diff(unit, golden_unit) == []
    # The deterministic dw_load_ts mapping and the source query were compared as-is, not patched.
    assert parsed["dw_load_ts"].translation_status == TranslationStatus.DETERMINISTIC
    assert unit.sources[0].sql_override is not None
    assert unit.sources[0].sql_override.translation_status == TranslationStatus.DETERMINISTIC


def test_policy_structure(parser: SsisParser) -> None:
    unit = parser.parse(POLICY)
    assert [s.id for s in unit.steps] == [
        "lkp_dim_product",
        "der_status_desc",
        "lkp_current_dim_policy",
        "cspl_detect_change",
        "uall_new_and_changed",
        "der_scd2_columns",
    ]
    assert [r.alias for r in unit.references] == ["ref_dim_product", "ref_dim_policy"]
    assert unit.references[1].system == "InsuranceDW"
    assert any("inferred from 'LKP dim_product'" in r for r in unit.business_rules)
    assert (
        "Conditional Split 'CSPL Detect Change' output 'Unchanged' is not connected: "
        "those rows are discarded." in unit.business_rules
    )
    target = unit.targets[0]
    assert target.input == "der_scd2_columns"
    assert target.dataset.alias == "tgt_dim_policy"
    assert target.load_strategy.mode == LoadMode.SCD2
    assert target.load_strategy.business_keys == ["policy_id"]
    assert target.load_strategy.effective_from_column == "row_effective_from"
    assert target.load_strategy.effective_to_column == "row_effective_to"
    assert target.load_strategy.current_flag_column == "is_current"
    assert target.load_strategy.surrogate_key_strategy == "identity"
    assert unit.parameters[0].legacy_refs == [
        "@[System::StartTime]",
        "@[User::RunStartTime]",
        "User::RunStartTime",
    ]
    assert [(r.rule_type.value, r.column) for r in unit.dq_rules] == [("accepted_values", "status_desc")]
    src = unit.sources[0].sql_override
    assert src is not None and src.referenced_columns[0] == "policy_id"


def test_slug() -> None:
    assert slug("SRC Policyholder") == "src_policyholder"
    assert slug("DER Policyholder Attributes") == "der_policyholder_attributes"
    assert slug("  LKP -- Current  dim_policy ") == "lkp_current_dim_policy"


# ---------------------------------------------------------------------------- synthetic packages
_PACKAGE = """<?xml version="1.0"?>
<DTS:Executable xmlns:DTS="www.microsoft.com/SqlServer/Dts" DTS:refId="Package" DTS:ObjectName="Synthetic"
  DTS:ExecutableType="Microsoft.Package">
  <DTS:ConnectionManagers>
    <DTS:ConnectionManager DTS:refId="Package.ConnectionManagers[PolicyAdmin]" DTS:ObjectName="PolicyAdmin" />
    <DTS:ConnectionManager DTS:refId="Package.ConnectionManagers[InsuranceDW]" DTS:ObjectName="InsuranceDW" />
  </DTS:ConnectionManagers>
  <DTS:Executables>
    <DTS:Executable DTS:refId="Package\\DFT" DTS:ObjectName="DFT" DTS:ExecutableType="Microsoft.Pipeline">
      <DTS:ObjectData><pipeline version="1"><components>
        <component componentClassID="Microsoft.OLEDBSource" name="SRC">
          <properties>
            <property name="SqlCommand">SELECT policyholder_id, first_name FROM dbo.policyholder</property>
          </properties>
          <connections>
            <connection connectionManagerRefId="Package.ConnectionManagers[PolicyAdmin]" />
          </connections>
          <outputs><output name="OLE DB Source Output"><outputColumns>
            <outputColumn name="policyholder_id" dataType="i4" />
            <outputColumn name="first_name" dataType="wstr" length="50" />
          </outputColumns></output></outputs>
        </component>
        {middle}
        <component componentClassID="Microsoft.OLEDBDestination" name="DST">
          <properties><property name="OpenRowset">[edw].[dim_policyholder]</property></properties>
          <connections>
            <connection connectionManagerRefId="Package.ConnectionManagers[InsuranceDW]" />
          </connections>
          <inputs><input name="OLE DB Destination Input"><inputColumns>
            <inputColumn cachedName="policyholder_id" externalMetadataColumnId="policyholder_id" />
            <inputColumn cachedName="{dst_col}" externalMetadataColumnId="full_name" />
          </inputColumns></input></inputs>
        </component>
      </components><paths>{paths}</paths></pipeline></DTS:ObjectData>
    </DTS:Executable>
  </DTS:Executables>
</DTS:Executable>
"""

_MULTICAST = """
        <component componentClassID="Microsoft.Multicast" name="MC">
          <outputs><output name="Multicast Output 1" /><output name="Multicast Output 2" /></outputs>
        </component>"""

_MC_PATHS = """
  <path startId="SRC.Outputs[OLE DB Source Output]" endId="MC.Inputs[Multicast Input]" />
  <path startId="MC.Outputs[Multicast Output 1]" endId="DST.Inputs[OLE DB Destination Input]" />
  <path startId="MC.Outputs[Multicast Output 2]" endId="{side}.Inputs[{side_input}]" />"""


def _write(tmp_path: Path, middle: str, paths: str, dst_col: str = "first_name") -> Path:
    path = tmp_path / "Synthetic.dtsx"
    path.write_text(
        _PACKAGE.replace("{middle}", middle).replace("{paths}", paths).replace("{dst_col}", dst_col)
    )
    return path


def test_script_component_is_unsupported(tmp_path: Path, repo_root: Path) -> None:
    middle = (
        _MULTICAST
        + """
        <component componentClassID="Microsoft.ScriptComponent" name="SCR Custom">
          <properties><property name="ScriptLanguage">CSharp</property></properties>
        </component>"""
    )
    paths = _MC_PATHS.replace("{side}", "SCR Custom").replace("{side_input}", "Input 0")
    unit = SsisParser(Catalog.from_ddl(repo_root / DDL)).parse(_write(tmp_path, middle, paths))
    assert [u.kind for u in unit.unsupported_constructs] == ["scriptcomponent"]
    construct = unit.unsupported_constructs[0]
    assert construct.reason == "Unsupported SSIS component"
    assert "SCR Custom" in construct.original_snippet and len(construct.original_snippet) <= 2000
    assert unit.targets[0].input == "src"
    assert unit.targets[0].load_strategy.mode == LoadMode.APPEND


def test_inline_unsupported_component_raises_clear_error(tmp_path: Path, repo_root: Path) -> None:
    middle = '<component componentClassID="Microsoft.ScriptComponent" name="SCR" />'
    paths = """
  <path startId="SRC.Outputs[OLE DB Source Output]" endId="SCR.Inputs[Input 0]" />
  <path startId="SCR.Outputs[Output 0]" endId="DST.Inputs[OLE DB Destination Input]" />"""
    with pytest.raises(ValueError, match="no target could be built.*scriptcomponent"):
        SsisParser(Catalog.from_ddl(repo_root / DDL)).parse(_write(tmp_path, middle, paths))


def test_stray_ole_db_command_is_unsupported(tmp_path: Path, repo_root: Path) -> None:
    sql = "UPDATE dbo.policyholder SET email = ? WHERE policyholder_id = ?"
    middle = (
        _MULTICAST
        + f"""
        <component componentClassID="Microsoft.OLEDBCommand" name="CMD Stray">
          <properties><property name="SqlCommand">{sql}</property></properties>
        </component>"""
    )
    paths = _MC_PATHS.replace("{side}", "CMD Stray").replace("{side_input}", "OLE DB Command Input")
    unit = SsisParser(Catalog.from_ddl(repo_root / DDL)).parse(_write(tmp_path, middle, paths))
    assert len(unit.unsupported_constructs) == 1
    construct = unit.unsupported_constructs[0]
    assert construct.kind == "ole_db_command"
    assert construct.original_snippet == sql
    assert construct.suggested_pattern is None
    assert unit.targets[0].load_strategy.mode == LoadMode.APPEND
    assert not any("OLE DB Command" in r for r in unit.business_rules)


def test_derived_column_without_friendly_expression(tmp_path: Path, repo_root: Path) -> None:
    lineage = "#{Package\\DFT\\SRC.Outputs[OLE DB Source Output].Columns[first_name]}"
    middle = """
        <component componentClassID="Microsoft.DerivedColumn" name="DER Name">
          <outputs><output name="Derived Column Output"><outputColumns>
            <outputColumn name="full_name" dataType="wstr" length="101"><properties>
              <property name="Expression">UPPER(TRIM(LINEAGE))</property>
            </properties></outputColumn>
          </outputColumns></output></outputs>
        </component>""".replace("LINEAGE", lineage)
    paths = """
  <path startId="Package\\DFT\\SRC.Outputs[OLE DB Source Output]"
    endId="Package\\DFT\\DER Name.Inputs[Derived Column Input]" />
  <path startId="DER Name.Outputs[Derived Column Output]" endId="DST.Inputs[OLE DB Destination Input]" />"""
    unit = SsisParser(Catalog.from_ddl(repo_root / DDL)).parse(_write(tmp_path, middle, paths, "full_name"))
    step = unit.steps[0]
    assert isinstance(step, DerivedColumnStep)
    expression = step.mappings[0].expression
    assert expression.original == "UPPER(TRIM(first_name))"
    assert expression.spark_sql == "upper(trim(first_name))"
    assert expression.notes == LINEAGE_NOTE
    assert expression.referenced_columns == ["first_name"]
    assert [r.rule_type.value for r in unit.dq_rules] == ["unique", "not_null"]


def test_missing_catalog_keeps_defaults(
    repo_root: Path, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    monkeypatch.chdir(repo_root)
    with caplog.at_level(logging.WARNING):
        unit = SsisParser().parse(POLICYHOLDER)
    assert all(c.nullable and not c.is_key for c in unit.sources[0].columns)
    assert [c.name for c in unit.targets[0].dataset.columns][0] == "policyholder_id"
    assert any("catalog" in r.message for r in caplog.records)
