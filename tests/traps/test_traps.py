"""Structural trap assertions (sample_artifacts/README.md). One test per trap id."""

from __future__ import annotations

import os
from pathlib import Path

import pytest

from migrateiq.ir.io import dump_unit
from migrateiq.ir.models import (
    DeduplicateStep,
    DerivedColumnStep,
    Expression,
    FilterStep,
    JoinStep,
    JoinType,
    LoadMode,
    LookupNoMatchPolicy,
    LookupStep,
    MigrationUnit,
    RouterStep,
    RunParameterKind,
    Target,
    TranslationStatus,
    UnionStep,
)
from migrateiq.parsers.catalog import Catalog
from migrateiq.parsers.expr_rules.ssis_expr import translate
from migrateiq.parsers.informatica import InformaticaParser
from migrateiq.parsers.ssis import SsisParser
from migrateiq.parsers.tsql import TsqlParser

REPO_ROOT = Path(__file__).resolve().parents[2]


def _parse(name: str) -> MigrationUnit:
    previous = Path.cwd()
    os.chdir(REPO_ROOT)
    try:
        parser = SsisParser(Catalog.from_ddl(Path("sample_artifacts/schema/insurance_ddl.sql")))
        return parser.parse(Path(f"sample_artifacts/ssis/{name}.dtsx"))
    finally:
        os.chdir(previous)


@pytest.fixture(scope="module")
def policyholder() -> MigrationUnit:
    return _parse("Load_DimPolicyholder")


@pytest.fixture(scope="module")
def policy() -> MigrationUnit:
    return _parse("Load_DimPolicy")


def _mapping(unit: MigrationUnit, column: str) -> Expression:
    for step in unit.steps:
        if isinstance(step, DerivedColumnStep):
            for mapping in step.mappings:
                if mapping.target_column == column:
                    return mapping.expression
    raise AssertionError(f"no derived mapping for {column}")


def _lookup(unit: MigrationUnit, step_id: str) -> LookupStep:
    step = next(s for s in unit.steps if s.id == step_id)
    assert isinstance(step, LookupStep)
    return step


def test_T01(policyholder: MigrationUnit) -> None:
    """SSIS ISNULL(x) is a boolean test: x IS NULL, never T-SQL ISNULL(x, y) / COALESCE."""
    assert "ISNULL(email)" in _mapping(policyholder, "email_normalized").original
    spark = translate("ISNULL(email)").spark_sql
    assert spark == "email IS NULL"
    assert "COALESCE" not in spark.upper() and "ISNULL(" not in spark.upper()


def test_T02(policyholder: MigrationUnit) -> None:
    """DATEDIFF("yy") counts year boundaries; the parser leaves it pending, never months_between/12."""
    age_band = _mapping(policyholder, "age_band")
    assert age_band.translation_status == TranslationStatus.PENDING
    assert age_band.spark_sql is None
    assert "months_between" not in dump_unit(policyholder).lower()


def test_T03(policyholder: MigrationUnit) -> None:
    """GETDATE() is pinned to run_ts and excluded from checksums."""
    dw_load_ts = _mapping(policyholder, "dw_load_ts")
    assert dw_load_ts.translation_status == TranslationStatus.DETERMINISTIC
    assert dw_load_ts.spark_sql == "current_timestamp()"
    assert dw_load_ts.notes == "Non-deterministic; excluded from reconciliation checksums."
    run_ts = [p for p in policyholder.parameters if p.kind == RunParameterKind.RUN_TS]
    assert len(run_ts) == 1 and run_ts[0].name == "run_ts"
    assert "GETDATE()" in run_ts[0].legacy_refs


def test_T04(policy: MigrationUnit) -> None:
    """Lookup 1 NoMatchBehavior=0 fails the package."""
    assert _lookup(policy, "lkp_dim_product").on_no_match == LookupNoMatchPolicy.FAIL


def test_T05(policy: MigrationUnit) -> None:
    """Lookup 2 no-match output means a NEW policy and feeds the insert path."""
    assert _lookup(policy, "lkp_current_dim_policy").on_no_match == LookupNoMatchPolicy.REDIRECT
    unions = [s for s in policy.steps if isinstance(s, UnionStep)]
    assert len(unions) == 1
    assert "lkp_current_dim_policy.no_match" in unions[0].inputs


def test_T06(policy: MigrationUnit) -> None:
    """Only status_code, annual_premium, deductible are tracked (not policy_number / expiration_date)."""
    assert policy.targets[0].load_strategy.tracked_columns == ["annual_premium", "deductible", "status_code"]


def test_T07(policy: MigrationUnit) -> None:
    """Row-by-row OLE DB Command UPDATE becomes a set-based SCD2 MERGE."""
    strategy = policy.targets[0].load_strategy
    assert strategy.mode == LoadMode.SCD2
    assert any("CMD Expire Current" in rule for rule in policy.business_rules)
    assert not [u for u in policy.unsupported_constructs if u.kind == "ole_db_command"]
    assert strategy.end_date_sentinel == "9999-12-31 00:00:00"
    assert strategy.surrogate_key_column == "policy_key"


def test_T08(policy: MigrationUnit) -> None:
    """The status_code <> 'P' filter stays in the source SQL; no FilterStep is lifted out."""
    sql = policy.sources[0].sql_override
    assert sql is not None and "'P'" in sql.original
    assert not [s for s in policy.steps if isinstance(s, FilterStep)]


# ---------------------------------------------------------------------------
# T-SQL traps (T16-T22, X2-X4)
# ---------------------------------------------------------------------------
def _parse_tsql(name: str) -> MigrationUnit:
    previous = Path.cwd()
    os.chdir(REPO_ROOT)
    try:
        parser = TsqlParser(Catalog.from_ddl(Path("sample_artifacts/schema/insurance_ddl.sql")))
        return parser.parse(Path(f"sample_artifacts/tsql/{name}.sql"))
    finally:
        os.chdir(previous)


@pytest.fixture(scope="module")
def fact_premium() -> MigrationUnit:
    return _parse_tsql("usp_Load_FactPremium")


@pytest.fixture(scope="module")
def claim_trigger() -> MigrationUnit:
    return _parse_tsql("trg_Claim_StatusAudit")


def _join(unit: MigrationUnit) -> JoinStep:
    joins = [s for s in unit.steps if isinstance(s, JoinStep)]
    assert len(joins) == 1
    return joins[0]


def test_T16(fact_premium: MigrationUnit) -> None:
    """Re-sent rows: ROW_NUMBER() ... load_ts DESC = 1 becomes a DeduplicateStep with the exact order."""
    dedups = [s for s in fact_premium.steps if isinstance(s, DeduplicateStep)]
    assert len(dedups) == 1
    assert dedups[0].partition_by == ["txn_id"]
    assert dedups[0].order_by == ["load_ts DESC"]


def test_T17(fact_premium: MigrationUnit) -> None:
    """Point-in-time SCD2 join keeps the full range condition, not just the equality."""
    join = _join(fact_premium)
    original = join.condition.original
    assert ">= d.row_effective_from" in original
    assert "<  d.row_effective_to" in original
    normalized = " ".join(original.split())
    assert "d.policy_id = c.policy_id" in normalized
    assert "< d.row_effective_to" in normalized
    assert join.inputs[1] == "ref_dim_policy"


def test_T18(fact_premium: MigrationUnit) -> None:
    """INNER JOIN silently drops txns without a policy version: keep INNER, count the drops."""
    assert _join(fact_premium).join_type == JoinType.INNER
    assert any("INNER JOIN" in r and "dropped-row count" in r for r in fact_premium.business_rules)


def test_T19(fact_premium: MigrationUnit) -> None:
    """The watermark advances only when rows were processed."""
    watermark = fact_premium.targets[0].load_strategy.watermark
    assert watermark is not None
    assert watermark.advance_only_if_rows is True
    assert watermark.computed_from == "stg_premium"
    assert watermark.initial_value == "1900-01-01"
    assert watermark.store_table == "etl.watermark"
    assert watermark.job_name == "usp_Load_FactPremium"
    assert any("(T19)" in r for r in fact_premium.business_rules)


def test_T20(fact_premium: MigrationUnit) -> None:
    """MERGE WHEN MATCHED AND (a <> b ...) keeps the NULL-unsafe <> verbatim; never <=>."""
    condition = fact_premium.targets[0].load_strategy.update_condition
    assert condition is not None
    assert "<>" in condition.original
    assert "<=>" not in dump_unit(fact_premium)


def test_T21(claim_trigger: MigrationUnit) -> None:
    """No row-level triggers in Spark: an UnsupportedConstruct with a CDC / Change Data Feed pattern."""
    triggers = [u for u in claim_trigger.unsupported_constructs if u.kind == "trigger"]
    assert len(triggers) == 1
    assert "Change Data Feed" in (triggers[0].suggested_pattern or "")
    assert claim_trigger.steps == []


def test_T22(claim_trigger: MigrationUnit) -> None:
    """BR-8 mutates the source row: OLTP-side business rule plus a source_row_mutation construct."""
    assert any(r.startswith("OLTP-side") and "BR-8" in r for r in claim_trigger.business_rules)
    mutations = [u for u in claim_trigger.unsupported_constructs if u.kind == "source_row_mutation"]
    assert len(mutations) == 1
    assert "c.closed_date" in mutations[0].original_snippet


def test_X2(fact_premium: MigrationUnit) -> None:
    """SQL Server compares strings case-insensitively; Spark does not: string compares stay pending."""
    written_premium = _mapping(fact_premium, "written_premium")
    assert written_premium.translation_status == TranslationStatus.PENDING
    assert written_premium.notes is not None and "case-insensitive" in written_premium.notes


def test_X3(fact_premium: MigrationUnit) -> None:
    """ROW_NUMBER() ties on load_ts are non-deterministic."""
    assert any("ties" in r and "(X3)" in r for r in fact_premium.business_rules)


def test_X4(fact_premium: MigrationUnit) -> None:
    """Watermark = MAX(load_ts) after the inner join: computed from the step after the join."""
    watermark = fact_premium.targets[0].load_strategy.watermark
    assert watermark is not None
    ids = [s.id for s in fact_premium.steps]
    assert watermark.computed_from == ids[ids.index(_join(fact_premium).id) + 1]
    assert any("(X4)" in r for r in fact_premium.business_rules)


# ---------------------------------------------------------------------------
# Informatica traps (T9-T15, X1, X5)
# ---------------------------------------------------------------------------
@pytest.fixture(scope="module")
def fact_claims() -> MigrationUnit:
    previous = Path.cwd()
    os.chdir(REPO_ROOT)
    try:
        parser = InformaticaParser(Catalog.from_ddl(Path("sample_artifacts/schema/insurance_ddl.sql")))
        return parser.parse(Path("sample_artifacts/informatica/m_LOAD_FACT_CLAIMS.xml"))
    finally:
        os.chdir(previous)


def _step_ids(unit: MigrationUnit) -> list[str]:
    return [s.id for s in unit.steps]


def _target(unit: MigrationUnit, alias: str) -> Target:
    return next(t for t in unit.targets if t.dataset.alias == alias)


def test_T09(fact_claims: MigrationUnit) -> None:
    """Master Outer Join keeps all DETAIL rows: LEFT join with the detail (claims) first."""
    joins = [s for s in fact_claims.steps if isinstance(s, JoinStep)]
    assert len(joins) == 1
    assert joins[0].join_type == JoinType.LEFT
    assert joins[0].inputs == ["sq_claim", "agg_payments"]


def test_T10(fact_claims: MigrationUnit) -> None:
    """:LKP. call inside an expression becomes an unconnected LookupStep placed before the expression."""
    lookup = _lookup(fact_claims, "lkp_dim_agent")
    assert lookup.is_unconnected is True
    assert lookup.return_columns == {"agent_key": "agent_key"}
    ids = _step_ids(fact_claims)
    first_exp = min(i for i, step_id in enumerate(ids) if step_id.startswith("exp_claim_metrics"))
    assert ids.index("lkp_dim_agent") < first_exp
    agent_key = _mapping(fact_claims, "o_agent_key")
    assert agent_key.translation_status == TranslationStatus.PENDING
    assert "agent_key" in agent_key.referenced_columns


def test_T11(fact_claims: MigrationUnit) -> None:
    """Variable ports evaluate top to bottom, before output ports: chained derived steps in that order."""
    ids = _step_ids(fact_claims)
    order = [
        ids.index("exp_claim_metrics_v_indemnity"),
        ids.index("exp_claim_metrics_v_expense"),
        ids.index("exp_claim_metrics_v_total_paid"),
        ids.index("exp_claim_metrics"),
    ]
    assert order == sorted(order)


def test_T12(fact_claims: MigrationUnit) -> None:
    """DECODE(TRUE, ...) stays pending with the CASE WHEN first-match note."""
    large_loss = _mapping(fact_claims, "o_is_large_loss")
    assert large_loss.translation_status == TranslationStatus.PENDING
    assert large_loss.spark_sql is None
    assert large_loss.notes is not None and "CASE WHEN" in large_loss.notes


def test_T13(fact_claims: MigrationUnit) -> None:
    """Informatica Router evaluates every group: first_match_only is False."""
    routers = [s for s in fact_claims.steps if isinstance(s, RouterStep)]
    assert len(routers) == 1
    assert routers[0].first_match_only is False


def test_T14(fact_claims: MigrationUnit) -> None:
    """'A' || NULL = 'A' in Informatica: O_ERROR_REASON stays pending with the NULL-as-empty note."""
    reason = _mapping(fact_claims, "o_error_reason")
    assert reason.translation_status == TranslationStatus.PENDING
    assert reason.notes is not None and "empty string" in reason.notes
    step = next(s for s in fact_claims.steps if s.id == "exp_error_reason")
    assert step.inputs == ["rtr_policy_check.policy_not_found"]


def test_T15(fact_claims: MigrationUnit) -> None:
    """Load behaviour comes from the SESSION writer attributes, not the mapping."""
    assert _target(fact_claims, "tgt_fact_claims").load_strategy.mode == LoadMode.FULL_OVERWRITE
    assert _target(fact_claims, "tgt_err_claims").load_strategy.mode == LoadMode.APPEND
    assert any("session" in r and "s_m_LOAD_FACT_CLAIMS" in r for r in fact_claims.business_rules)


def test_X1(fact_claims: MigrationUnit) -> None:
    """IN(CLAIM_STATUS, ...) tests the raw value while O_CLAIM_STATUS normalizes it."""
    days_open = _mapping(fact_claims, "o_days_open")
    assert days_open.notes is not None and "X1" in days_open.notes


def test_X5(fact_claims: MigrationUnit) -> None:
    """ERR_CLAIMS columns not output by EXP_ERROR_REASON pass through by name from upstream."""
    columns = [c.name for c in _target(fact_claims, "tgt_err_claims").dataset.columns]
    assert {"claim_id", "claim_number", "dw_load_ts"} <= set(columns)
    assert any("(X5)" in r for r in fact_claims.business_rules)
