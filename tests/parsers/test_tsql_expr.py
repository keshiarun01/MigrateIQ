"""Tests for the deterministic T-SQL expression rules (expr_rules/tsql_expr.py)."""

from __future__ import annotations

import pytest

from migrateiq.ir.models import DataType, TranslationStatus
from migrateiq.parsers.base import run_ts_note
from migrateiq.parsers.expr_rules.tsql_expr import (
    COLLATION_NOTE,
    PARSE_FAILURE_NOTE,
    RUNTIME_NOTE,
    UNKNOWN_TYPE_NOTE,
    WATERMARK_NOTE,
    translate,
)

TYPES: dict[str, DataType] = {
    "c.rn": DataType.BIGINT,
    "c.txn_type": DataType.STRING,
    "c.fees": DataType.DECIMAL,
    "t.load_ts": DataType.TIMESTAMP,
    "tgt.written_premium": DataType.DECIMAL,
    "src.written_premium": DataType.DECIMAL,
    "tgt.fees": DataType.DECIMAL,
    "src.fees": DataType.DECIMAL,
    "tgt.txn_type": DataType.STRING,
    "src.txn_type": DataType.STRING,
}


def test_row_number_filter_is_deterministic() -> None:
    expression = translate("c.rn = 1", TYPES)
    assert expression.translation_status == TranslationStatus.DETERMINISTIC
    assert expression.spark_sql == "rn = 1"
    assert expression.referenced_columns == ["rn"]


def test_numeric_not_equal_chain_keeps_angle_brackets() -> None:
    original = "tgt.written_premium <> src.written_premium\n   OR tgt.fees != src.fees"
    expression = translate(original, TYPES)
    assert expression.translation_status == TranslationStatus.DETERMINISTIC
    assert expression.spark_sql == "written_premium <> written_premium OR fees <> fees"
    assert expression.original == original
    assert "<=>" not in (expression.spark_sql or "")
    assert "!=" not in (expression.spark_sql or "")


def test_string_literal_comparison_is_pending_x2() -> None:
    expression = translate("c.txn_type = 'CANCEL'", TYPES)
    assert expression.translation_status == TranslationStatus.PENDING
    assert expression.spark_sql is None
    assert expression.notes == COLLATION_NOTE


def test_string_column_comparison_is_pending_x2() -> None:
    expression = translate("tgt.txn_type <> src.txn_type", TYPES)
    assert expression.translation_status == TranslationStatus.PENDING
    assert expression.notes is not None and COLLATION_NOTE in expression.notes
    assert expression.original == "tgt.txn_type <> src.txn_type"


def test_isnull_two_args_is_never_coalesce_deterministic() -> None:
    expression = translate("ISNULL(c.fees, 0)", TYPES)
    assert expression.translation_status == TranslationStatus.PENDING
    assert expression.spark_sql is None
    assert expression.referenced_columns == ["fees"]


@pytest.mark.parametrize("text", ["SYSDATETIME()", "GETDATE()", "@RunTs"])
def test_bare_runtime_value_is_current_timestamp(text: str) -> None:
    expression = translate(text, TYPES)
    assert expression.translation_status == TranslationStatus.DETERMINISTIC
    assert expression.spark_sql == "current_timestamp()"
    assert expression.notes == RUNTIME_NOTE


def test_watermark_variable_is_pending() -> None:
    expression = translate("t.load_ts > @LastValue", TYPES, watermark_vars=["@LastValue"])
    assert expression.translation_status == TranslationStatus.PENDING
    assert expression.notes == WATERMARK_NOTE.format(var="@LastValue")
    assert expression.referenced_columns == ["load_ts"]


def test_runtime_and_watermark_notes_are_joined() -> None:
    original = "t.load_ts > @LastValue AND t.load_ts < SYSDATETIME()"
    expression = translate(original, TYPES, watermark_vars=["@LastValue"])
    assert expression.notes == f"{run_ts_note(original)} {WATERMARK_NOTE.format(var='@LastValue')}"


def test_nested_runtime_value_is_pending_with_run_ts_note() -> None:
    original = "CAST(SYSDATETIME() AS DATE)"
    expression = translate(original, TYPES)
    assert expression.translation_status == TranslationStatus.PENDING
    assert expression.spark_sql is None
    assert expression.notes == run_ts_note(original)


def test_unparseable_text_is_pending() -> None:
    expression = translate("c.fees +* (", TYPES)
    assert expression.translation_status == TranslationStatus.PENDING
    assert expression.notes == PARSE_FAILURE_NOTE
    assert expression.original == "c.fees +* ("


def test_unparseable_text_with_runtime_value_adds_run_ts_note() -> None:
    expression = translate("SYSDATETIME() +* (", TYPES)
    assert expression.notes == f"{PARSE_FAILURE_NOTE} {run_ts_note('SYSDATETIME() +* (')}"


def test_unknown_column_type_in_comparison_is_pending() -> None:
    expression = translate("x.mystery = 1", TYPES)
    assert expression.translation_status == TranslationStatus.PENDING
    assert expression.notes == UNKNOWN_TYPE_NOTE.format(columns="x.mystery")


def test_referenced_columns_sorted_without_variables() -> None:
    expression = translate("c.txn_type = @JobName OR c.fees > @Limit OR c.rn = 1", TYPES)
    assert expression.referenced_columns == ["fees", "rn", "txn_type"]


def test_allowed_functions_and_null_tests() -> None:
    expression = translate("UPPER(LTRIM(RTRIM(c.txn_type))) IS NOT NULL AND NOT (c.rn > 1)", TYPES)
    assert expression.translation_status == TranslationStatus.DETERMINISTIC
    assert expression.spark_sql == "NOT UPPER(LTRIM(RTRIM(txn_type))) IS NULL AND NOT (rn > 1)"


def test_resolver_callable_is_accepted() -> None:
    expression = translate("a.flag = 1", lambda qualifier, name: DataType.INT)
    assert expression.spark_sql == "flag = 1"


@pytest.mark.parametrize(
    "text",
    [
        "c.rn = 1",
        "tgt.fees <> src.fees",
        "tgt.txn_type <> src.txn_type",
        "ISNULL(c.fees, 0) <> 0",
        "c.fees != 1 AND c.rn <> 2",
        "CASE WHEN c.txn_type = 'CANCEL' THEN -1 ELSE 1 END",
    ],
)
def test_no_null_safe_equality_anywhere(text: str) -> None:
    expression = translate(text, TYPES)
    assert "<=>" not in expression.model_dump_json()
