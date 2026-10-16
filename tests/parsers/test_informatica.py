"""Tests for the deterministic Informatica PowerCenter parser."""

from __future__ import annotations

import json
import logging
from pathlib import Path

import pytest

from migrateiq.ir.io import dump_unit
from migrateiq.ir.models import LoadMode, MigrationUnit
from migrateiq.parsers.catalog import Catalog
from migrateiq.parsers.informatica import InformaticaParser

DDL = Path("sample_artifacts/schema/insurance_ddl.sql")
FIXTURE = Path("sample_artifacts/informatica/m_LOAD_FACT_CLAIMS.xml")

EXPECTED_STEPS = [
    "sq_claim",
    "sq_claim_payment",
    "agg_payments",
    "jnr_claim_payments",
    "lkp_dim_policy",
    "lkp_dim_agent",
    "exp_claim_metrics_v_indemnity",
    "exp_claim_metrics_v_expense",
    "exp_claim_metrics_v_total_paid",
    "exp_claim_metrics",
    "rtr_policy_check",
    "exp_error_reason",
]


@pytest.fixture
def parser(repo_root: Path, monkeypatch: pytest.MonkeyPatch) -> InformaticaParser:
    monkeypatch.chdir(repo_root)
    return InformaticaParser(Catalog.from_ddl(DDL))


def _without_parsed_at(unit: MigrationUnit) -> str:
    doc = json.loads(dump_unit(unit))
    doc["source_artifact"].pop("parsed_at")
    return json.dumps(doc, indent=2, sort_keys=True)


def test_fixture_parses_to_valid_unit(parser: InformaticaParser) -> None:
    unit = parser.parse(FIXTURE)
    MigrationUnit.model_validate_json(dump_unit(unit))
    assert unit.unit_id == "informatica.m_LOAD_FACT_CLAIMS"
    assert unit.name == "m_LOAD_FACT_CLAIMS"
    assert unit.source_artifact.object_name == "m_LOAD_FACT_CLAIMS"
    assert unit.source_artifact.file_path == FIXTURE.as_posix()
    assert unit.complexity is None
    assert unit.unsupported_constructs == []
    assert [s.alias for s in unit.sources] == ["src_claim", "src_claim_payment"]
    assert [r.alias for r in unit.references] == ["ref_dim_policy", "ref_dim_agent"]
    assert [(t.dataset.alias, t.input) for t in unit.targets] == [
        ("tgt_fact_claims", "rtr_policy_check.valid"),
        ("tgt_err_claims", "exp_error_reason"),
    ]
    assert all(t.dataset.system == "InsuranceDW" and t.dataset.schema_name == "edw" for t in unit.targets)
    assert all(s.system == "PolicyAdmin" and s.schema_name == "dbo" for s in unit.sources)
    assert [p.legacy_refs for p in unit.parameters] == [["SESSSTARTTIME"]]
    assert [(r.dataset_alias, r.column, r.rule_type.value, r.params) for r in unit.dq_rules] == [
        ("tgt_fact_claims", "claim_id", "unique", {}),
        ("tgt_fact_claims", "is_large_loss", "accepted_values", {"values": ["Y", "N"]}),
    ]


def test_absolute_path_under_cwd_is_relative(parser: InformaticaParser, repo_root: Path) -> None:
    unit = parser.parse(repo_root / FIXTURE)
    assert unit.source_artifact.file_path == FIXTURE.as_posix()


def test_deterministic_output(repo_root: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.chdir(repo_root)
    first = InformaticaParser(Catalog.from_ddl(DDL)).parse(FIXTURE)
    second = InformaticaParser(Catalog.from_ddl(DDL)).parse(FIXTURE)
    assert _without_parsed_at(first) == _without_parsed_at(second)


def test_step_order(parser: InformaticaParser) -> None:
    assert [s.id for s in parser.parse(FIXTURE).steps] == EXPECTED_STEPS


def test_inferred_links_and_router_reassignment_logged(
    parser: InformaticaParser, caplog: pytest.LogCaptureFixture
) -> None:
    with caplog.at_level(logging.WARNING, logger="migrateiq.parsers.informatica"):
        unit = parser.parse(FIXTURE)
    messages = [r.getMessage() for r in caplog.records if r.levelno == logging.WARNING]
    links = [m for m in messages if m.startswith("inferred link ")]
    assert "inferred link CLAIM.CLAIM_NUMBER -> SQ_CLAIM.CLAIM_NUMBER (abridged connectors)" in links
    assert (
        "inferred link RTR_POLICY_CHECK.O_DW_LOAD_TS -> ERR_CLAIMS.DW_LOAD_TS (abridged connectors)" in links
    )
    assert "inferred link CLAIM.CLAIM_ID -> SQ_CLAIM.CLAIM_ID (abridged connectors)" not in links  # explicit
    assert len(links) == len(set(links))
    assert any("POLICY_ID3" in m and "POLICY_NOT_FOUND" in m for m in messages)
    assert (
        "Router 'RTR_POLICY_CHECK': connector suffix '3' (POLICY_ID3) would select the default group "
        "DEFAULT1 under PowerCenter numbering, which receives no rows; wired to group POLICY_NOT_FOUND per "
        "the mapping description (claims without a current policy go to ERR_CLAIMS)."
    ) in unit.business_rules


# ---------------------------------------------------------------------------
# Synthetic mappings
# ---------------------------------------------------------------------------
_TEMPLATE = """<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE POWERMART SYSTEM "powrmart.dtd">
<POWERMART>
<REPOSITORY NAME="R">
<FOLDER NAME="F">
  <SOURCE DBDNAME="PolicyAdmin" NAME="CLAIM" OWNERNAME="dbo">
    <SOURCEFIELD DATATYPE="int" NAME="CLAIM_ID" NULLABLE="NOTNULL" KEYTYPE="PRIMARY KEY"/>
    <SOURCEFIELD DATATYPE="varchar" NAME="CLAIM_NUMBER" NULLABLE="NOTNULL" KEYTYPE="NOT A KEY" PRECISION="20"
      SCALE="0"/>
  </SOURCE>
  <TARGET NAME="T_ONE" OWNERNAME="edw">
    <TARGETFIELD DATATYPE="int" NAME="CLAIM_ID" NULLABLE="NOTNULL" KEYTYPE="NOT A KEY"/>
    <TARGETFIELD DATATYPE="varchar" NAME="CLAIM_NUMBER" NULLABLE="NOTNULL" KEYTYPE="NOT A KEY" PRECISION="20"
      SCALE="0"/>
  </TARGET>
  <TARGET NAME="T_TWO" OWNERNAME="edw">
    <TARGETFIELD DATATYPE="int" NAME="CLAIM_ID" NULLABLE="NOTNULL" KEYTYPE="NOT A KEY"/>
  </TARGET>
  {mappings}
  <SESSION NAME="s_m_SYN" MAPPINGNAME="m_SYN">
    <SESSIONEXTENSION NAME="Relational Writer" SINSTANCENAME="T_ONE" TYPE="WRITER">
      <ATTRIBUTE NAME="Truncate target table option" VALUE="NO"/>
      <ATTRIBUTE NAME="Insert" VALUE="YES"/>
    </SESSIONEXTENSION>
    <SESSIONEXTENSION NAME="Relational Writer" SINSTANCENAME="T_TWO" TYPE="WRITER">
      <ATTRIBUTE NAME="Truncate target table option" VALUE="YES"/>
      <ATTRIBUTE NAME="Insert" VALUE="YES"/>
    </SESSIONEXTENSION>
    <ATTRIBUTE NAME="Treat source rows as" VALUE="{treat}"/>
  </SESSION>
</FOLDER>
</REPOSITORY>
</POWERMART>
"""

_MAPPING = """<MAPPING NAME="m_SYN" DESCRIPTION="synthetic">
    <TRANSFORMATION NAME="SQ_CLAIM" TYPE="Source Qualifier">
      <TRANSFORMFIELD DATATYPE="integer" NAME="CLAIM_ID" PORTTYPE="INPUT/OUTPUT" PRECISION="10" SCALE="0"/>
      <TRANSFORMFIELD DATATYPE="string" NAME="CLAIM_NUMBER" PORTTYPE="INPUT/OUTPUT" PRECISION="20" SCALE="0"/>
      <TABLEATTRIBUTE NAME="Sql Query" VALUE=""/>
      <TABLEATTRIBUTE NAME="Source Filter" VALUE=""/>
    </TRANSFORMATION>
    {transformation}
    <INSTANCE NAME="CLAIM" TRANSFORMATION_NAME="CLAIM" TRANSFORMATION_TYPE="Source Definition" TYPE="SOURCE"/>
    <INSTANCE NAME="SQ_CLAIM" TRANSFORMATION_NAME="SQ_CLAIM" TRANSFORMATION_TYPE="Source Qualifier"/>
    {instance}
    <INSTANCE NAME="T_ONE" TRANSFORMATION_NAME="T_ONE" TRANSFORMATION_TYPE="Target Definition" TYPE="TARGET"/>
    <INSTANCE NAME="T_TWO" TRANSFORMATION_NAME="T_TWO" TRANSFORMATION_TYPE="Target Definition" TYPE="TARGET"/>
    <CONNECTOR FROMINSTANCE="CLAIM" FROMFIELD="CLAIM_ID" TOINSTANCE="SQ_CLAIM" TOFIELD="CLAIM_ID"/>
    <CONNECTOR FROMINSTANCE="SQ_CLAIM" FROMFIELD="CLAIM_ID" TOINSTANCE="T_ONE" TOFIELD="CLAIM_ID"/>
    {connectors}
  </MAPPING>"""

_CUSTOM = (
    """<TRANSFORMATION NAME="CT_X" TYPE="Custom Transformation">
      <TRANSFORMFIELD DATATYPE="integer" NAME="CLAIM_ID" PORTTYPE="INPUT/OUTPUT" PRECISION="10" SCALE="0"/>
    </TRANSFORMATION>""",
    '<INSTANCE NAME="CT_X" TRANSFORMATION_NAME="CT_X" TRANSFORMATION_TYPE="Custom Transformation"/>',
    """<CONNECTOR FROMINSTANCE="SQ_CLAIM" FROMFIELD="CLAIM_ID" TOINSTANCE="CT_X" TOFIELD="CLAIM_ID"/>
    <CONNECTOR FROMINSTANCE="CT_X" FROMFIELD="CLAIM_ID" TOINSTANCE="T_TWO" TOFIELD="CLAIM_ID"/>""",
)

_BAD_LOOKUP = (
    """<TRANSFORMATION NAME="LKP_X" TYPE="Lookup Procedure">
      <TRANSFORMFIELD DATATYPE="integer" NAME="IN_ID" PORTTYPE="INPUT" PRECISION="10" SCALE="0"/>
      <TRANSFORMFIELD DATATYPE="integer" NAME="ID" PORTTYPE="LOOKUP" PRECISION="10" SCALE="0"/>
      <TRANSFORMFIELD DATATYPE="integer" NAME="CLAIM_ID" PORTTYPE="LOOKUP/OUTPUT" PRECISION="10" SCALE="0"/>
      <TABLEATTRIBUTE NAME="Lookup table name" VALUE="NO_SUCH_TABLE"/>
      <TABLEATTRIBUTE NAME="Lookup condition" VALUE="ID = IN_ID"/>
      <TABLEATTRIBUTE NAME="Lookup policy on multiple match" VALUE="Use Any Value"/>
      <TABLEATTRIBUTE NAME="Connection Information" VALUE="$Target"/>
    </TRANSFORMATION>""",
    '<INSTANCE NAME="LKP_X" TRANSFORMATION_NAME="LKP_X" TRANSFORMATION_TYPE="Lookup Procedure"/>',
    """<CONNECTOR FROMINSTANCE="SQ_CLAIM" FROMFIELD="CLAIM_ID" TOINSTANCE="LKP_X" TOFIELD="IN_ID"/>
    <CONNECTOR FROMINSTANCE="LKP_X" FROMFIELD="CLAIM_ID" TOINSTANCE="T_TWO" TOFIELD="CLAIM_ID"/>""",
)


def _write(
    tmp_path: Path, extra: tuple[str, str, str] = ("", "", ""), *, treat: str = "Insert", copies: int = 1
) -> Path:
    mapping = _MAPPING.format(transformation=extra[0], instance=extra[1], connectors=extra[2])
    path = tmp_path / "m_SYN.xml"
    path.write_text(_TEMPLATE.format(mappings="\n".join([mapping] * copies), treat=treat), encoding="utf-8")
    return path


def _parse(path: Path) -> MigrationUnit:
    return InformaticaParser(Catalog.from_ddl(Path(__file__).resolve().parents[2] / DDL)).parse(path)


def test_synthetic_baseline(tmp_path: Path) -> None:
    unit = _parse(_write(tmp_path))
    # T_TWO has no incoming connector in the baseline mapping.
    assert [(u.kind, u.reason) for u in unit.unsupported_constructs] == [
        ("target", "Target T_TWO needs exactly one upstream instance")
    ]
    assert unit.steps == []
    assert [(t.dataset.alias, t.input, t.load_strategy.mode) for t in unit.targets] == [
        ("tgt_t_one", "src_claim", LoadMode.APPEND),
    ]


def test_custom_transformation_is_unsupported(tmp_path: Path) -> None:
    unit = _parse(_write(tmp_path, _CUSTOM))
    kinds = [u.kind for u in unit.unsupported_constructs]
    assert kinds == ["custom_transformation"]
    construct = unit.unsupported_constructs[0]
    assert 'NAME="CT_X"' in construct.original_snippet
    assert construct.reason == "Unsupported Informatica transformation"
    assert [t.dataset.alias for t in unit.targets] == ["tgt_t_one"]


def test_data_driven_session_is_unsupported(tmp_path: Path) -> None:
    with pytest.raises(ValueError, match="session_writer"):
        _parse(_write(tmp_path, treat="Data driven"))


def test_lookup_without_catalog_match_is_unsupported(tmp_path: Path) -> None:
    unit = _parse(_write(tmp_path, _BAD_LOOKUP))
    lookups = [u for u in unit.unsupported_constructs if u.kind == "lookup"]
    assert len(lookups) == 1
    assert "NO_SUCH_TABLE" in lookups[0].reason
    assert [t.dataset.alias for t in unit.targets] == ["tgt_t_one"]


def test_two_mappings_raise(tmp_path: Path) -> None:
    with pytest.raises(ValueError, match="exactly one MAPPING"):
        _parse(_write(tmp_path, copies=2))
