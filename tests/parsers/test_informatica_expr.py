"""Tests for the deterministic Informatica expression rules."""

from __future__ import annotations

from pathlib import Path

import pytest
from lxml import etree

from migrateiq.ir.models import DataType, ExpressionDialect, TranslationStatus
from migrateiq.parsers.base import run_ts_note
from migrateiq.parsers.expr_rules.informatica_expr import (
    BOOLEAN_NOTE,
    CONCAT_NOTE,
    PARSE_FAILURE_NOTE,
    Binary,
    aggregate_call,
    parse_expression,
    string_literal_domain,
    translate,
)

FIXTURE = Path("sample_artifacts/informatica/m_LOAD_FACT_CLAIMS.xml")


def _fixture_expressions(repo_root: Path) -> list[str]:
    parser = etree.XMLParser(resolve_entities=False, no_network=True, load_dtd=False)
    root = etree.parse(str(repo_root / FIXTURE), parser).getroot()
    return [e.get("EXPRESSION", "") for e in root.iter() if e.get("EXPRESSION") is not None]


def test_every_fixture_expression_parses(repo_root: Path) -> None:
    expressions = _fixture_expressions(repo_root)
    assert len(expressions) > 20
    for text in expressions:
        parse_expression(text)
        assert translate(text).notes != PARSE_FAILURE_NOTE


@pytest.mark.parametrize(
    ("original", "spark"),
    [
        ("ISNULL(POLICY_KEY)", "policy_key IS NULL"),
        ("NOT ISNULL(POLICY_KEY)", "NOT (policy_key IS NULL)"),
        ("LTRIM(RTRIM(UPPER(CLAIM_STATUS)))", "ltrim(rtrim(upper(claim_status)))"),
        ("a = 'X' AND b <> 2", "a = 'X' AND b <> 2"),
        ("A != 1 OR B ^= 'it''s'", "a <> 1 OR b <> 'it''s'"),
        ("(A = 1 OR B = 2) AND C = 3", "(a = 1 OR b = 2) AND c = 3"),
        ("CLAIM.LOSS_TYPE = 'FIRE'", "loss_type = 'FIRE'"),
    ],
)
def test_deterministic(original: str, spark: str) -> None:
    expression = translate(original)
    assert expression.translation_status == TranslationStatus.DETERMINISTIC
    assert expression.dialect == ExpressionDialect.INFORMATICA_EXPR
    assert expression.spark_sql == spark
    assert expression.original == original


def test_concatenation_is_pending_with_t14_note() -> None:
    expression = translate("'A' || B")
    assert expression.translation_status == TranslationStatus.PENDING
    assert expression.spark_sql is None
    assert expression.notes == CONCAT_NOTE
    assert expression.referenced_columns == ["b"]


def test_concatenation_is_never_or() -> None:
    root = parse_expression("a || b")
    assert isinstance(root, Binary) and root.op == "||"
    # || binds tighter than comparison and is not a logical operator
    root = parse_expression("a || b = c OR d = e")
    assert isinstance(root, Binary) and root.op == "OR"
    assert isinstance(root.left, Binary) and root.left.op == "="
    assert isinstance(root.left.left, Binary) and root.left.left.op == "||"
    assert "OR" not in (translate("a || b").spark_sql or "")


def test_decode_true_note() -> None:
    expression = translate("DECODE(TRUE, A >= 1, 'Y', B = 'L' AND C >= 5, 'Y', 'N')")
    assert expression.translation_status == TranslationStatus.PENDING
    assert expression.notes is not None
    assert "CASE WHEN" in expression.notes and "(T12)" in expression.notes
    assert "first matching condition wins" in expression.notes


def test_in_on_normalized_column_gets_x1_note() -> None:
    text = "IIF(IN(CLAIM_STATUS, 'CLOSED', 'DENIED'), 1, NULL)"
    plain = translate(text)
    assert plain.notes is None
    noted = translate(text, normalized_columns=frozenset({"claim_status"}))
    assert noted.translation_status == TranslationStatus.PENDING
    assert noted.notes is not None and "(X1)" in noted.notes and "claim_status" in noted.notes


def test_bare_session_start_time_is_deterministic() -> None:
    expression = translate("SESSSTARTTIME")
    assert expression.translation_status == TranslationStatus.DETERMINISTIC
    assert expression.spark_sql == "current_timestamp()"
    assert expression.referenced_columns == []
    assert expression.notes is not None and "SESSSTARTTIME" in expression.notes


def test_nested_session_start_time_is_pending() -> None:
    original = "TO_CHAR(SESSSTARTTIME, 'YYYY')"
    expression = translate(original)
    assert expression.translation_status == TranslationStatus.PENDING
    assert expression.notes == run_ts_note(original)


def test_boolean_vs_number_is_pending() -> None:
    expression = translate("CLAIM.IS_DELETED = 0", {"is_deleted": DataType.BOOLEAN})
    assert expression.translation_status == TranslationStatus.PENDING
    assert expression.notes == BOOLEAN_NOTE
    assert translate("CLAIM.IS_DELETED = 0", {"is_deleted": DataType.INT}).spark_sql == "is_deleted = 0"


def test_unparseable() -> None:
    expression = translate("IIF(A = 1, 'x'")
    assert expression.translation_status == TranslationStatus.PENDING
    assert expression.notes == PARSE_FAILURE_NOTE
    with_runtime = translate("IIF(SESSSTARTTIME > ")
    assert with_runtime.notes is not None
    assert with_runtime.notes.startswith(PARSE_FAILURE_NOTE) and "run_ts" in with_runtime.notes


def test_referenced_columns_sorted_lowercase() -> None:
    expression = translate("IIF(ZETA.Z_COL > $$LIMIT, Alpha, M_COL || Alpha)")
    assert expression.referenced_columns == ["alpha", "m_col", "z_col"]


def test_lookup_call_note_and_columns() -> None:
    text = "IIF(ISNULL(:LKP.LKP_DIM_AGENT(HANDLING_AGENT_ID)), -1, :LKP.LKP_DIM_AGENT(HANDLING_AGENT_ID))"
    expression = translate(text, lookups={"LKP_DIM_AGENT(HANDLING_AGENT_ID)": "lkp_dim_agent.agent_key"})
    assert expression.translation_status == TranslationStatus.PENDING
    assert expression.original == text
    assert expression.notes == (
        ":LKP.LKP_DIM_AGENT(HANDLING_AGENT_ID) is provided by step lkp_dim_agent as column agent_key."
    )
    assert expression.referenced_columns == ["agent_key", "handling_agent_id"]


def test_string_literal_domain() -> None:
    text = (
        "DECODE(TRUE, V_TOTAL_PAID + RESERVE_AMOUNT >= 100000, 'Y', "
        "LOSS_TYPE = 'LIABILITY' AND RESERVE_AMOUNT >= 50000, 'Y', 'N')"
    )
    assert string_literal_domain(text) == ["Y", "N"]
    assert string_literal_domain("IIF(A = 1, 'P', IIF(B = 2, 'Q', 'P'))") == ["P", "Q"]
    assert string_literal_domain("IIF(A = 1, 'P')") is None
    assert string_literal_domain("IIF(A = 1, 'P', B)") is None
    assert string_literal_domain("'P'") is None


def test_aggregate_call() -> None:
    text = "SUM(IIF(PAYMENT_TYPE = 'INDEMNITY', PAYMENT_AMOUNT, 0))"
    assert aggregate_call(text) == ("SUM", "IIF(PAYMENT_TYPE = 'INDEMNITY', PAYMENT_AMOUNT, 0)")
    assert aggregate_call("SUM(A) + 1") is None
    assert aggregate_call("UPPER(A)") is None
