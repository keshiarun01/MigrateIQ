"""Tests for the deterministic T-SQL procedure/trigger parser (parsers/tsql.py)."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from migrateiq.ir.io import dump_unit
from migrateiq.ir.models import (
    DeduplicateStep,
    DerivedColumnStep,
    FilterStep,
    JoinStep,
    JoinType,
    LoadMode,
    MigrationUnit,
    RunParameterKind,
    TranslationStatus,
)
from migrateiq.parsers.catalog import Catalog
from migrateiq.parsers.tsql import TsqlParser

PROC = Path("sample_artifacts/tsql/usp_Load_FactPremium.sql")
TRIGGER = Path("sample_artifacts/tsql/trg_Claim_StatusAudit.sql")
DDL = Path("sample_artifacts/schema/insurance_ddl.sql")

INSERT_TARGET = """
    INSERT INTO edw.err_claims (claim_id, claim_number, policy_id, error_reason, dw_load_ts)
    SELECT c.claim_id, c.claim_number, c.policy_id, 'NO POLICY', SYSDATETIME()
    FROM dbo.claim c
    WHERE c.policy_id IS NULL;
"""


@pytest.fixture
def parser(repo_root: Path, monkeypatch: pytest.MonkeyPatch) -> TsqlParser:
    monkeypatch.chdir(repo_root)
    return TsqlParser(Catalog.from_ddl(DDL))


def _without_parsed_at(unit: MigrationUnit) -> dict[str, object]:
    data = json.loads(dump_unit(unit))
    data["source_artifact"].pop("parsed_at")
    return data


def _proc(tmp_path: Path, body: str, name: str = "usp_Synthetic") -> Path:
    path = tmp_path / f"{name}.sql"
    path.write_text(f"CREATE PROCEDURE etl.{name}\nAS\nBEGIN\n{body}\nEND;\nGO\n", encoding="utf-8")
    return path


# ---------------------------------------------------------------------- sample artifacts
def test_procedure_parses_to_valid_unit(parser: TsqlParser) -> None:
    unit = parser.parse(PROC)
    MigrationUnit.model_validate_json(dump_unit(unit))
    assert unit.unit_id == "tsql.usp_Load_FactPremium"
    assert unit.name == "usp_Load_FactPremium"
    assert unit.source_artifact.object_name == "etl.usp_Load_FactPremium"
    assert unit.source_artifact.file_path == "sample_artifacts/tsql/usp_Load_FactPremium.sql"
    assert unit.complexity is None
    assert unit.unsupported_constructs == []
    assert [s.id for s in unit.steps] == ["flt_changed", "dedup_changed", "jn_dim_policy", "stg_premium"]
    assert [type(s) for s in unit.steps] == [FilterStep, DeduplicateStep, JoinStep, DerivedColumnStep]


def test_trigger_parses_to_valid_unit(parser: TsqlParser) -> None:
    unit = parser.parse(TRIGGER)
    MigrationUnit.model_validate_json(dump_unit(unit))
    assert unit.unit_id == "tsql.trg_Claim_StatusAudit"
    assert unit.source_artifact.object_name == "dbo.trg_Claim_StatusAudit"


@pytest.mark.parametrize("artifact", [PROC, TRIGGER])
def test_determinism(parser: TsqlParser, artifact: Path) -> None:
    first = _without_parsed_at(parser.parse(artifact))
    second = _without_parsed_at(parser.parse(artifact))
    assert json.dumps(first, sort_keys=True) == json.dumps(second, sort_keys=True)


def test_sources_and_references(parser: TsqlParser) -> None:
    unit = parser.parse(PROC)
    (source,) = unit.sources
    assert (source.alias, source.system, source.schema_name, source.table) == (
        "src_premium_transaction",
        "PolicyAdmin",
        "dbo",
        "premium_transaction",
    )
    assert source.sql_override is None
    assert [c.name for c in source.columns] == [
        "txn_id",
        "policy_id",
        "txn_type",
        "txn_date",
        "written_premium",
        "fees",
        "load_ts",
        "source_system",
    ]
    assert next(c for c in source.columns if c.name == "fees").nullable
    (ref,) = unit.references
    assert (ref.alias, ref.system, ref.schema_name, ref.table) == (
        "ref_dim_policy",
        "InsuranceDW",
        "edw",
        "dim_policy",
    )
    assert sorted(c.name for c in ref.columns) == [
        "policy_id",
        "policy_key",
        "row_effective_from",
        "row_effective_to",
    ]


def test_filter_condition_is_verbatim_and_pending(parser: TsqlParser) -> None:
    step = parser.parse(PROC).steps[0]
    assert isinstance(step, FilterStep)
    assert step.inputs == ["src_premium_transaction"]
    assert step.source_component == "changed"
    assert step.condition.original == (
        "t.load_ts > @LastValue\n              AND   t.source_system IN ('BILLING', 'LEGACY_BILL')"
    )
    assert step.condition.translation_status == TranslationStatus.PENDING
    assert "@LastValue" in (step.condition.notes or "")


def test_join_and_staging_projection(parser: TsqlParser) -> None:
    unit = parser.parse(PROC)
    join = unit.steps[2]
    assert isinstance(join, JoinStep)
    assert join.inputs == ["dedup_changed", "ref_dim_policy"]
    assert join.join_type == JoinType.INNER
    assert join.condition.translation_status == TranslationStatus.PENDING
    stg = unit.steps[3]
    assert isinstance(stg, DerivedColumnStep)
    assert stg.inputs == ["jn_dim_policy"]
    assert stg.keep_input_columns is False
    assert stg.source_component == "#stg_premium"
    assert [m.target_column for m in stg.mappings] == [
        "txn_id",
        "policy_key",
        "txn_date_key",
        "txn_type",
        "written_premium",
        "fees",
        "gross_premium",
        "load_ts",
    ]
    by_name = {m.target_column: m for m in stg.mappings}
    assert by_name["txn_date_key"].expression.original == "CONVERT(INT, FORMAT(c.txn_date, 'yyyyMMdd'))"
    assert by_name["fees"].expression.original == "ISNULL(c.fees, 0)"
    assert by_name["txn_id"].expression.spark_sql == "txn_id"
    assert by_name["txn_id"].expression.translation_status == TranslationStatus.DETERMINISTIC
    assert by_name["fees"].expression.translation_status == TranslationStatus.PENDING
    assert by_name["gross_premium"].expression.original.endswith(
        "ELSE c.written_premium END + ISNULL(c.fees, 0)"
    )
    assert by_name["txn_id"].target_type is not None and by_name["load_ts"].target_type is None


def test_merge_load_strategy(parser: TsqlParser) -> None:
    (target,) = parser.parse(PROC).targets
    assert target.input == "stg_premium"
    assert target.dataset.alias == "tgt_fact_premium"
    assert target.dataset.system == "InsuranceDW"
    assert [c.name for c in target.dataset.columns] == [
        "txn_id",
        "policy_key",
        "txn_date_key",
        "txn_type",
        "written_premium",
        "fees",
        "gross_premium",
        "dw_load_ts",
        "dw_update_ts",
    ]
    strategy = target.load_strategy
    assert strategy.mode == LoadMode.MERGE_UPSERT
    assert strategy.business_keys == ["txn_id"]
    assert strategy.insert_only_columns == ["dw_load_ts"]
    assert strategy.update_only_columns == ["dw_update_ts"]
    assert strategy.watermark_column == "load_ts"
    condition = strategy.update_condition
    assert condition is not None
    assert condition.original.startswith("tgt.written_premium <> src.written_premium")
    assert condition.original.endswith("OR tgt.policy_key      <> src.policy_key")
    assert condition.translation_status == TranslationStatus.PENDING


def test_parameters(parser: TsqlParser) -> None:
    params = parser.parse(PROC).parameters
    assert [(p.name, p.kind, p.legacy_refs) for p in params] == [
        ("run_ts", RunParameterKind.RUN_TS, ["@RunTs", "SYSDATETIME()"]),
        ("last_value", RunParameterKind.WATERMARK, ["@LastValue"]),
    ]


def test_post_load_actions(parser: TsqlParser) -> None:
    actions = parser.parse(PROC).post_load_actions
    assert sorted((a.kind, a.on) for a in actions) == [
        ("job_log", "failure"),
        ("job_log", "success"),
        ("row_counts", "success"),
    ]
    success = next(a for a in actions if a.kind == "job_log" and a.on == "success")
    assert success.target_table == "etl.job_log"
    assert success.columns == {
        "job_name": "@JobName",
        "rows_inserted": "MERGE OUTPUT $action count of INSERT",
        "rows_updated": "MERGE OUTPUT $action count of UPDATE",
        "run_ts": "run_ts",
        "status": "'SUCCESS'",
    }
    assert success.original_snippet.startswith("INSERT INTO etl.job_log")
    assert success.original_snippet.endswith("'SUCCESS');")
    counts = next(a for a in actions if a.kind == "row_counts")
    assert counts.target_table == "@MergeOut"
    assert counts.columns == {"action_type": "$action"}
    assert counts.original_snippet.startswith("SELECT @Inserted = SUM(")
    failure = next(a for a in actions if a.on == "failure")
    assert failure.columns == {
        "error_message": "ERROR_MESSAGE()",
        "job_name": "@JobName",
        "run_ts": "run_ts",
        "status": "'FAILED'",
    }
    assert "THROW" in failure.original_snippet
    assert failure.original_snippet.startswith("INSERT INTO etl.job_log")


def test_business_rules_and_dq(parser: TsqlParser) -> None:
    unit = parser.parse(PROC)
    rules = " | ".join(unit.business_rules)
    assert (
        "dw_load_ts = run_ts on INSERT; dw_update_ts = run_ts on UPDATE and NULL on INSERT."
        in unit.business_rules
    )
    assert "dbo -> PolicyAdmin" in rules
    assert "re-raised with THROW" in rules
    assert "@@ROWCOUNT = 0" in rules
    assert [(r.dataset_alias, r.column, r.rule_type.value, r.derived_from) for r in unit.dq_rules] == [
        ("tgt_fact_premium", "txn_id", "unique", "PRIMARY KEY")
    ]


def test_trigger_minimal_shell(parser: TsqlParser) -> None:
    unit = parser.parse(TRIGGER)
    assert unit.steps == []
    (source,) = unit.sources
    assert source.alias == "src_claim" and source.system == "PolicyAdmin"
    assert [c.name for c in source.columns][:2] == ["claim_id", "claim_number"]
    assert len(source.columns) == 12
    (target,) = unit.targets
    assert target.input == "src_claim"
    assert target.load_strategy.mode == LoadMode.APPEND
    assert target.dataset.alias == "tgt_claim_status_history"
    assert target.dataset.system == "InsuranceDW"
    assert [c.name for c in target.dataset.columns] == [
        "claim_id",
        "old_status",
        "new_status",
        "changed_at",
        "changed_by",
    ]
    assert unit.business_rules[0].startswith("BR-7:")
    assert unit.business_rules[1].startswith("OLTP-side (not migrated): BR-8:")
    assert unit.business_rules[2].startswith("IR wiring for this unit")
    assert len(unit.business_rules) == 3
    assert [u.kind for u in unit.unsupported_constructs] == ["trigger", "source_row_mutation"]
    trigger = unit.unsupported_constructs[0]
    assert trigger.original_snippet.startswith("CREATE OR ALTER TRIGGER dbo.trg_Claim_StatusAudit")
    assert trigger.original_snippet.endswith("END;")
    assert trigger.reason == "Spark has no row-level triggers (AFTER UPDATE on dbo.claim)."
    assert "SUSER_SNAME()" in (trigger.suggested_pattern or "")
    mutation = unit.unsupported_constructs[1]
    assert mutation.original_snippet.startswith("UPDATE  c\n    SET     c.closed_date = CASE")
    assert mutation.original_snippet.endswith("WHERE   d.claim_status <> i.claim_status;")
    assert [(p.name, p.legacy_refs) for p in unit.parameters] == [("run_ts", ["SYSDATETIME()"])]
    assert unit.dq_rules == [] and unit.complexity is None


# ---------------------------------------------------------------------- synthetic procedures
def test_cursor_is_unsupported(parser: TsqlParser, tmp_path: Path) -> None:
    body = (
        """
    SET NOCOUNT ON;
    DECLARE @id INT;
    DECLARE cur CURSOR FOR SELECT claim_id FROM dbo.claim;
    OPEN cur;
    FETCH NEXT FROM cur INTO @id;
    WHILE @@FETCH_STATUS = 0
    BEGIN
        UPDATE dbo.claim SET is_deleted = 0 WHERE claim_id = @id;
        FETCH NEXT FROM cur INTO @id;
    END;
    CLOSE cur;
    DEALLOCATE cur;
"""
        + INSERT_TARGET
    )
    unit = parser.parse(_proc(tmp_path, body))
    cursor = [u for u in unit.unsupported_constructs if u.kind == "cursor"]
    snippets = [u.original_snippet for u in cursor]
    assert snippets[0] == "DECLARE cur CURSOR FOR SELECT claim_id FROM dbo.claim;"
    assert snippets[1] == "OPEN cur;"
    assert snippets[2] == "FETCH NEXT FROM cur INTO @id;"
    assert (
        snippets[3].startswith("WHILE @@FETCH_STATUS = 0")
        and "UPDATE dbo.claim SET is_deleted" in snippets[3]
    )
    assert snippets[4:] == ["CLOSE cur;", "DEALLOCATE cur;"]
    (target,) = unit.targets
    assert target.dataset.alias == "tgt_err_claims"
    assert target.load_strategy.mode == LoadMode.APPEND
    assert [s.id for s in unit.steps] == ["flt_sel_err_claims", "sel_err_claims"]
    assert unit.sources[0].alias == "src_claim"


def test_dynamic_sql_and_unknown_statements_are_recorded(parser: TsqlParser, tmp_path: Path) -> None:
    body = (
        """
    DECLARE @sql NVARCHAR(MAX) = N'SELECT 1';
    EXEC(@sql);
    EXEC sp_executesql @sql;
    EXECUTE dbo.usp_Other;
    WAITFOR DELAY '00:00:01';
"""
        + INSERT_TARGET
    )
    unit = parser.parse(_proc(tmp_path, body))
    kinds = [(u.kind, u.original_snippet) for u in unit.unsupported_constructs]
    assert ("dynamic_sql", "EXEC(@sql);") in kinds
    assert ("dynamic_sql", "EXEC sp_executesql @sql;") in kinds
    assert ("procedure_call", "EXECUTE dbo.usp_Other;") in kinds
    assert ("unrecognized_statement", "WAITFOR DELAY '00:00:01';") in kinds


def test_no_target_raises_value_error(parser: TsqlParser, tmp_path: Path) -> None:
    path = _proc(tmp_path, "    WAITFOR DELAY '00:00:01';\n    EXEC(@sql);")
    with pytest.raises(ValueError, match=r"no target could be built.*unrecognized_statement.*dynamic_sql"):
        parser.parse(path)


def test_unrecognized_if_and_extra_parameter_are_recorded(parser: TsqlParser, tmp_path: Path) -> None:
    path = tmp_path / "usp_Param.sql"
    path.write_text(
        "CREATE PROCEDURE etl.usp_Param\n    @BatchId INT\nAS\nBEGIN\n"
        "    IF @BatchId > 0\n        PRINT 'x';\n" + INSERT_TARGET + "END;\nGO\n",
        encoding="utf-8",
    )
    unit = parser.parse(path)
    kinds = {u.kind: u.original_snippet for u in unit.unsupported_constructs}
    assert kinds["conditional_logic"] == "IF @BatchId > 0\n        PRINT 'x';"
    assert kinds["procedure_parameter"] == "@BatchId INT"
