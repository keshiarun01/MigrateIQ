"""Tests for the deterministic SSIS expression rules."""

from __future__ import annotations

from pathlib import Path

import pytest
from lxml import etree

from migrateiq.ir.models import ExpressionDialect, TranslationStatus
from migrateiq.parsers.base import run_ts_note
from migrateiq.parsers.expr_rules import ssis_expr
from migrateiq.parsers.expr_rules.ssis_expr import (
    PARSE_FAILURE_NOTE,
    is_bare_runtime_value,
    is_literal_only,
    parse_expression,
    resolve_lineage_refs,
    string_literal_domain,
    translate,
)

AGE_BAND = (
    'ISNULL(date_of_birth) ? "UNKNOWN" : DATEDIFF("yy", date_of_birth, GETDATE()) < 25 ? "<25" : '
    'DATEDIFF("yy", date_of_birth, GETDATE()) < 45 ? "25-44" : DATEDIFF("yy", date_of_birth, GETDATE()) < 65 '
    '? "45-64" : "65+"'
)
STATUS_DESC = (
    'status_code == "A" ? "ACTIVE" : status_code == "C" ? "CANCELLED" : '
    'status_code == "L" ? "LAPSED" : "UNKNOWN"'
)
CSPL = (
    "status_code != old_status_code || annual_premium != old_annual_premium || deductible != old_deductible"
)


def _friendly_expressions(artifacts_dir: Path) -> list[str]:
    found: list[str] = []
    for dtsx in sorted((artifacts_dir / "ssis").glob("*.dtsx")):
        tree = etree.parse(str(dtsx))
        found.extend(p.text or "" for p in tree.iter("property") if p.get("name") == "FriendlyExpression")
    return found


def test_every_fixture_friendly_expression_parses(artifacts_dir: Path) -> None:
    expressions = _friendly_expressions(artifacts_dir)
    assert len(expressions) >= 9
    for text in expressions:
        parse_expression(text)
        assert not (translate(text).notes or "").startswith(PARSE_FAILURE_NOTE), text


@pytest.mark.parametrize(
    ("original", "spark_sql"),
    [
        ("ISNULL(email)", "email IS NULL"),
        (
            CSPL,
            "status_code <> old_status_code OR annual_premium <> old_annual_premium "
            "OR deductible <> old_deductible",
        ),
        ('UPPER(TRIM(x)) == "A"', "upper(trim(x)) = 'A'"),
        ('a == "it\'s"', "a = 'it''s'"),
        ("!(a == b) && (c || d)", "NOT (a = b) AND (c OR d)"),
        ("(a || b) && c", "(a OR b) AND c"),
        ("a >= 10 && b < 2.5", "a >= 10 AND b < 2.5"),
        ("!ISNULL(a)", "NOT (a IS NULL)"),
    ],
)
def test_deterministic(original: str, spark_sql: str) -> None:
    expr = translate(original)
    assert expr.translation_status == TranslationStatus.DETERMINISTIC
    assert expr.dialect == ExpressionDialect.SSIS_EXPR
    assert expr.spark_sql == spark_sql
    assert expr.original == original


@pytest.mark.parametrize(
    "original",
    [
        'TRIM(first_name) + " " + TRIM(last_name)',
        'ISNULL(email) || TRIM(email) == "" ? NULL(DT_WSTR,100) : LOWER(TRIM(email))',
        STATUS_DESC,
        "LOWER(x)",
        'DATEDIFF("yy", a, b) < 25',
        "(DT_BOOL)1",
        '(DT_DBTIMESTAMP2,7)"9999-12-31 00:00:00"',
        "NULL(DT_WSTR,100)",
        "TRUE",
        "@[User::SomethingElse] == a",
        '"a\\\\b"',
    ],
)
def test_pending(original: str) -> None:
    expr = translate(original)
    assert expr.translation_status == TranslationStatus.PENDING
    assert expr.spark_sql is None
    assert expr.original == original


def test_bare_getdate() -> None:
    expr = translate("GETDATE()")
    assert expr.translation_status == TranslationStatus.DETERMINISTIC
    assert expr.spark_sql == "current_timestamp()"
    assert expr.notes == "Non-deterministic; excluded from reconciliation checksums."
    assert expr.referenced_columns == []
    assert is_bare_runtime_value("GETDATE()")


def test_bare_package_variable() -> None:
    expr = translate("@[User::RunStartTime]")
    assert expr.translation_status == TranslationStatus.DETERMINISTIC
    assert expr.spark_sql == "current_timestamp()"
    assert (
        expr.notes == "Package start time (User::RunStartTime); codegen pins it to the run_ts job argument."
    )
    assert expr.referenced_columns == []
    assert translate("@[System::StartTime]").spark_sql == "current_timestamp()"
    assert is_bare_runtime_value("@[User::RunStartTime]")
    assert not is_bare_runtime_value("@[User::Other]")


def test_nested_runtime_value_is_pending() -> None:
    for original in (AGE_BAND, 'DATEADD("dd", 1, GETDATE())', "a < @[User::RunStartTime]"):
        expr = translate(original)
        assert expr.translation_status == TranslationStatus.PENDING
        assert expr.spark_sql is None
        assert expr.notes == run_ts_note(original)
    assert "@[User::RunStartTime]" not in translate("a < @[User::RunStartTime]").referenced_columns


def test_unparseable() -> None:
    for original in ("a +", "foo bar", "#{Package\\X.Outputs[O].Columns}", '"open'):
        expr = translate(original)
        assert expr.translation_status == TranslationStatus.PENDING
        assert expr.spark_sql is None
        assert expr.notes == PARSE_FAILURE_NOTE


def test_unparseable_with_runtime_value_gets_run_ts_note() -> None:
    for original, spelling in (
        ('DATEDIFF("yy", dob, GETDATE()) <', "GETDATE()"),
        ("a < @[User::RunStartTime] +", "@[User::RunStartTime]"),
    ):
        expr = translate(original)
        assert expr.translation_status == TranslationStatus.PENDING
        assert expr.notes == f"{PARSE_FAILURE_NOTE} {run_ts_note(original)}"
        assert ssis_expr.runtime_spellings(original) == [spelling]
    assert translate("@[User::Other] +").notes == PARSE_FAILURE_NOTE


def test_referenced_columns_sorted_without_variables() -> None:
    assert translate(CSPL).referenced_columns == [
        "annual_premium",
        "deductible",
        "old_annual_premium",
        "old_deductible",
        "old_status_code",
        "status_code",
    ]
    assert translate(AGE_BAND).referenced_columns == ["date_of_birth"]
    assert translate("zeta == @[User::X] || alpha == 1").referenced_columns == ["alpha", "zeta"]


def test_string_literal_domain() -> None:
    assert string_literal_domain(AGE_BAND) == ["UNKNOWN", "<25", "25-44", "45-64", "65+"]
    assert string_literal_domain(STATUS_DESC) == ["ACTIVE", "CANCELLED", "LAPSED", "UNKNOWN"]
    assert string_literal_domain("ISNULL(email) ? NULL(DT_WSTR,100) : LOWER(email)") is None
    assert string_literal_domain('"A"') is None
    assert string_literal_domain('a ? b : "c"') is None


def test_is_literal_only() -> None:
    assert is_literal_only(AGE_BAND)
    assert is_literal_only("(DT_BOOL)1")
    assert is_literal_only('(DT_DBTIMESTAMP2,7)"9999-12-31 00:00:00"')
    assert is_literal_only('"x"')
    assert not is_literal_only('TRIM(a) + " " + TRIM(b)')
    assert not is_literal_only("GETDATE()")
    assert not is_literal_only('a ? b : "c"')


def test_literal_value_and_not_equal_pairs() -> None:
    assert ssis_expr.literal_value('(DT_DBTIMESTAMP2,7)"9999-12-31 00:00:00"') == "9999-12-31 00:00:00"
    assert ssis_expr.literal_value("(DT_BOOL)1") is None
    assert ssis_expr.not_equal_pairs(CSPL) == [
        ("status_code", "old_status_code"),
        ("annual_premium", "old_annual_premium"),
        ("deductible", "old_deductible"),
    ]
    assert ssis_expr.not_equal_pairs("a != b && c != d") is None


def test_resolve_lineage_refs() -> None:
    text = (
        "TRIM(#{Package\\DFT Load Policyholder\\SRC Policyholder.Outputs[OLE DB Source Output]"
        ".Columns[first_name]})"
        ' + " " + TRIM(#{Package\\DFT\\SRC.Outputs[OLE DB Source Output].Columns[last_name]})'
    )
    assert resolve_lineage_refs(text) == 'TRIM(first_name) + " " + TRIM(last_name)'
