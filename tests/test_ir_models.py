"""Contract tests for the IR. Run: pytest tests/test_ir_models.py -v"""
import copy
import json
from pathlib import Path

import pytest
from pydantic import ValidationError

from migrateiq.ir.models import (
    Expression, ExpressionDialect, LoadMode, LoadStrategy, MigrationUnit,
    TranslationStatus,
)

GOLDEN = Path(__file__).parent / "golden" / "Load_DimPolicyholder.ir.json"


@pytest.fixture
def golden() -> dict:
    return json.loads(GOLDEN.read_text())


def test_golden_parses(golden):
    unit = MigrationUnit.model_validate(golden)
    assert unit.steps[0].type == "derived_column"
    assert unit.is_ready_for_codegen()


def test_golden_roundtrip(golden):
    unit = MigrationUnit.model_validate(golden)
    again = MigrationUnit.model_validate_json(unit.model_dump_json())
    assert again == unit


def test_pending_expressions_found(golden):
    g = copy.deepcopy(golden)
    expr = g["steps"][0]["mappings"][0]["expression"]
    expr["spark_sql"] = None
    expr["translation_status"] = "pending"
    unit = MigrationUnit.model_validate(g)
    assert len(unit.pending_expressions()) == 1
    assert not unit.is_ready_for_codegen()


def test_translated_without_spark_sql_rejected():
    with pytest.raises(ValidationError, match="spark_sql is empty"):
        Expression(original="UPPER(x)", dialect=ExpressionDialect.SSIS_EXPR,
                   translation_status=TranslationStatus.LLM)


def test_undefined_step_input_rejected(golden):
    g = copy.deepcopy(golden)
    g["steps"][0]["inputs"] = ["does_not_exist"]
    with pytest.raises(ValidationError, match="not defined before it"):
        MigrationUnit.model_validate(g)


def test_undefined_target_input_rejected(golden):
    g = copy.deepcopy(golden)
    g["targets"][0]["input"] = "ghost_step"
    with pytest.raises(ValidationError, match="undefined"):
        MigrationUnit.model_validate(g)


def test_extra_fields_forbidden(golden):
    g = copy.deepcopy(golden)
    g["steps"][0]["hallucinated_field"] = "x"   # LLM-invented keys must fail loudly
    with pytest.raises(ValidationError):
        MigrationUnit.model_validate(g)


def test_scd2_requires_columns():
    with pytest.raises(ValidationError):
        LoadStrategy(mode=LoadMode.SCD2, business_keys=["policy_id"])


def test_merge_requires_keys():
    with pytest.raises(ValidationError):
        LoadStrategy(mode=LoadMode.MERGE_UPSERT)
