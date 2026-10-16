"""Contract tests for the IR v1.1 additions (docs/ir_v1_1_proposal.md, G1–G10)."""

from __future__ import annotations

import copy
import json
from pathlib import Path
from typing import Any

import pytest
from pydantic import ValidationError

from migrateiq.ir.models import (
    IR_VERSION,
    Complexity,
    Expression,
    ExpressionDialect,
    LoadMode,
    LoadStrategy,
    LookupNoMatchPolicy,
    LookupStep,
    MigrationPath,
    MigrationUnit,
    PostLoadAction,
    RunParameter,
    RunParameterKind,
    UnsupportedConstruct,
    WatermarkSpec,
)

GOLDEN = Path(__file__).resolve().parent.parent / "golden" / "Load_DimPolicyholder.ir.json"


@pytest.fixture
def golden() -> dict[str, Any]:
    return json.loads(GOLDEN.read_text(encoding="utf-8"))


def _cond(text: str) -> Expression:
    return Expression(original=text, dialect=ExpressionDialect.TSQL)


def _lookup_unit(golden: dict[str, Any], policy: str) -> dict[str, Any]:
    """Golden plus a lookup step on a reference dataset, feeding the target."""
    g = copy.deepcopy(golden)
    g["references"] = [{"alias": "ref_state", "system": "EDW", "schema_name": "edw", "table": "dim_state"}]
    g["steps"].append(
        {
            "type": "lookup",
            "id": "lkp_state",
            "inputs": ["der_policyholder_attributes"],
            "reference": "ref_state",
            "join_keys": {"state_code": "state_code"},
            "return_columns": {"state_key": "state_key"},
            "on_no_match": policy,
        }
    )
    return g


def test_version_bumped() -> None:
    assert IR_VERSION == "1.1.0"


def test_v1_0_golden_still_validates(golden: dict[str, Any]) -> None:
    unit = MigrationUnit.model_validate(golden)
    assert unit.ir_version == "1.0.0"
    assert unit.parameters == []
    assert unit.post_load_actions == []


# G1
def test_parameters_roundtrip(golden: dict[str, Any]) -> None:
    g = copy.deepcopy(golden)
    g["parameters"] = [{"name": "run_ts", "kind": "run_ts", "legacy_refs": ["GETDATE()"]}]
    unit = MigrationUnit.model_validate(g)
    assert unit.parameters == [
        RunParameter(name="run_ts", kind=RunParameterKind.RUN_TS, legacy_refs=["GETDATE()"])
    ]
    assert MigrationUnit.model_validate_json(unit.model_dump_json()) == unit


# G2
def test_update_condition_on_merge() -> None:
    ls = LoadStrategy(
        mode=LoadMode.MERGE_UPSERT,
        business_keys=["txn_id"],
        update_condition=_cond("tgt.fees <> src.fees"),
    )
    assert ls.update_condition is not None
    assert ls.update_condition.original == "tgt.fees <> src.fees"


def test_update_condition_rejected_outside_merge() -> None:
    with pytest.raises(ValidationError, match="only valid for merge_upsert"):
        LoadStrategy(mode=LoadMode.APPEND, update_condition=_cond("a <> b"))


# G3
def test_insert_and_update_only_columns() -> None:
    ls = LoadStrategy(
        mode=LoadMode.MERGE_UPSERT,
        business_keys=["txn_id"],
        insert_only_columns=["dw_load_ts"],
        update_only_columns=["dw_update_ts"],
    )
    assert ls.insert_only_columns == ["dw_load_ts"]
    assert ls.update_only_columns == ["dw_update_ts"]


# G4
def _watermark(computed_from: str) -> dict[str, Any]:
    return WatermarkSpec(
        store_table="etl.watermark",
        job_name="job",
        column="load_ts",
        initial_value="1900-01-01",
        computed_from=computed_from,
    ).model_dump(mode="json")


def test_watermark_computed_from_must_exist(golden: dict[str, Any]) -> None:
    g = copy.deepcopy(golden)
    g["targets"][0]["load_strategy"]["watermark"] = _watermark("der_policyholder_attributes")
    unit = MigrationUnit.model_validate(g)
    assert unit.targets[0].load_strategy.watermark.advance_only_if_rows is True

    g["targets"][0]["load_strategy"]["watermark"] = _watermark("ghost_step")
    with pytest.raises(ValidationError, match="computed_from 'ghost_step' undefined"):
        MigrationUnit.model_validate(g)


# G5
def test_redirect_lookup_outputs(golden: dict[str, Any]) -> None:
    g = _lookup_unit(golden, "redirect")
    g["targets"][0]["input"] = "lkp_state.no_match"
    unit = MigrationUnit.model_validate(g)
    step = unit.steps[-1]
    assert isinstance(step, LookupStep)
    assert step.output_names() == ["lkp_state.match", "lkp_state.no_match"]


def test_redirect_lookup_has_no_plain_output(golden: dict[str, Any]) -> None:
    g = _lookup_unit(golden, "redirect")
    g["targets"][0]["input"] = "lkp_state"
    with pytest.raises(ValidationError, match="input 'lkp_state' undefined"):
        MigrationUnit.model_validate(g)


@pytest.mark.parametrize("policy", ["fail", "return_null", "default_value"])
def test_non_redirect_lookup_single_output(golden: dict[str, Any], policy: str) -> None:
    g = _lookup_unit(golden, policy)
    g["targets"][0]["input"] = "lkp_state"
    unit = MigrationUnit.model_validate(g)
    assert unit.steps[-1].output_names() == ["lkp_state"]
    assert unit.steps[-1].on_no_match == LookupNoMatchPolicy(policy)


# G6
def test_post_load_actions(golden: dict[str, Any]) -> None:
    g = copy.deepcopy(golden)
    action = PostLoadAction(
        kind="job_log",
        on="failure",
        target_table="etl.job_log",
        columns={"status": "'FAILED'"},
        original_snippet="INSERT INTO etl.job_log ...",
    )
    g["post_load_actions"] = [action.model_dump(mode="json")]
    assert MigrationUnit.model_validate(g).post_load_actions == [action]


# G7
def _join_unit(golden: dict[str, Any], inputs: list[str]) -> dict[str, Any]:
    g = copy.deepcopy(golden)
    g["references"] = [
        {"alias": "ref_dim_policy", "system": "EDW", "schema_name": "edw", "table": "dim_policy"}
    ]
    g["steps"].append(
        {
            "type": "join",
            "id": "jn_policy",
            "inputs": inputs,
            "join_type": "inner",
            "condition": {"original": "a = b", "dialect": "tsql"},
        }
    )
    return g


def test_join_right_input_may_be_reference(golden: dict[str, Any]) -> None:
    MigrationUnit.model_validate(_join_unit(golden, ["der_policyholder_attributes", "ref_dim_policy"]))


def test_join_left_input_may_not_be_reference(golden: dict[str, Any]) -> None:
    with pytest.raises(ValidationError, match="input 'ref_dim_policy' is not defined"):
        MigrationUnit.model_validate(_join_unit(golden, ["ref_dim_policy", "der_policyholder_attributes"]))


# G8
def test_scd2_message_lists_missing_fields() -> None:
    with pytest.raises(ValidationError) as only_tracked:
        LoadStrategy(
            mode=LoadMode.SCD2,
            business_keys=["policy_id"],
            effective_from_column="f",
            effective_to_column="t",
            current_flag_column="c",
        )
    assert "scd2 requires: tracked_columns" in str(only_tracked.value)
    assert "effective_from_column" not in str(only_tracked.value)
    with pytest.raises(ValidationError) as err:
        LoadStrategy(mode=LoadMode.SCD2, business_keys=["policy_id"])
    assert "and ok" not in str(err.value)
    assert "tracked_columns, effective_from_column, effective_to_column, current_flag_column" in str(
        err.value
    )


# G9
def test_manual_unit_not_ready_for_codegen(golden: dict[str, Any]) -> None:
    unit = MigrationUnit.model_validate(golden)
    assert unit.is_ready_for_codegen()
    unit.unsupported_constructs = [
        UnsupportedConstruct(
            kind="trigger", original_snippet="CREATE TRIGGER ...", reason="x", suggested_pattern="CDC"
        )
    ]
    unit.complexity = Complexity(score=90, recommended_path=MigrationPath.MANUAL)
    assert not unit.is_ready_for_codegen()


# G10
def test_scd2_sentinel_and_surrogate_key() -> None:
    ls = LoadStrategy(
        mode=LoadMode.SCD2,
        business_keys=["policy_id"],
        tracked_columns=["status_code"],
        effective_from_column="row_effective_from",
        effective_to_column="row_effective_to",
        current_flag_column="is_current",
        surrogate_key_column="policy_key",
        end_date_sentinel="9999-12-31 00:00:00",
        surrogate_key_strategy="identity",
    )
    assert ls.end_date_sentinel == "9999-12-31 00:00:00"
    with pytest.raises(ValidationError):
        LoadStrategy(mode=LoadMode.APPEND, surrogate_key_strategy="uuid")
