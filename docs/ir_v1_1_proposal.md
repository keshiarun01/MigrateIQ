# IR v1.1 proposal

Status: **approved 2026-10-11**; implemented in `src/migrateiq/ir/models.py` (`IR_VERSION = "1.1.0"`).
Date: 2026-10-11. Gap ids G1–G10 match `docs/PLAN.md` §IR v1.0 gaps.

## Compatibility rule
Every change is additive: new fields are optional or have defaults, and no existing field is renamed, retyped
or removed. Every v1.0 document, including `tests/golden/Load_DimPolicyholder.ir.json`, still validates under v1.1.
`IR_VERSION` becomes `"1.1.0"`. The only behaviour changes are G8 (error text) and G9 (`is_ready_for_codegen`).

Until approval, parsers use the workaround listed for each gap and say so in the relevant `notes` / `business_rules` entry.

---

## G1 Runtime parameters (T3, T19)
**Gap.** The IR has no place to declare run-time inputs. `GETDATE()` (DimPolicyholder), `User::RunStartTime`
(DimPolicy, line 19), `@RunTs` and the watermark `@LastValue` (FactPremium) all appear only inside expression text.

**Proposal.**
```python
class RunParameterKind(str, Enum):
    RUN_TS = "run_ts"
    WATERMARK = "watermark"


class RunParameter(_Base):
    name: str  # job argument name, e.g. "run_ts"
    kind: RunParameterKind
    data_type: DataType = DataType.TIMESTAMP
    legacy_refs: list[str] = Field(default_factory=list)  # ["GETDATE()", "@[User::RunStartTime]"]


class MigrationUnit:
    parameters: list[RunParameter] = Field(default_factory=list)
```
**Workaround.** Expressions that use a runtime value stay `pending` with the shared note from
`parsers/base.py::run_ts_note()`. The unit gets a `business_rules` line naming the legacy references.

## G2 MERGE update condition (T20)
**Gap.** `LoadStrategy` cannot hold `WHEN MATCHED AND (tgt.written_premium <> src.written_premium OR ...)`.
Leaving it out changes which rows update.

**Proposal.** `LoadStrategy.update_condition: Optional[Expression] = None`. Only valid for `merge_upsert`.
The original text keeps the plain `<>` verbatim; a test asserts no `<=>` appears in `spark_sql`.

**Workaround.** The condition text goes verbatim into `business_rules` with the prefix `MERGE update condition:`.
The `merge_upsert` template reads it from there.

## G3 Insert-only / update-only columns (FactPremium)
**Gap.** `dw_load_ts` is set only on INSERT and `dw_update_ts` only on UPDATE. `txn_id` is never updated.

**Proposal.** `LoadStrategy.insert_only_columns: list[str] = []` and `update_only_columns: list[str] = []`.

**Workaround.** `business_rules` note listing both sets.

## G4 Watermark storage and advance rule (T19, X4)
**Gap.** `watermark_column` names the column but not where the value is stored, which job key it uses, how it is
read when missing (`'1900-01-01'`), or the rule that it advances only when rows were processed and is computed
*after* the inner join (X4).

**Proposal.**
```python
class WatermarkSpec(_Base):
    store_table: str  # "etl.watermark"
    job_name: str  # "usp_Load_FactPremium"
    column: str  # "load_ts"
    initial_value: str  # "1900-01-01"
    computed_from: str  # step id whose output MAX(column) is taken, e.g. the post-join step
    advance_only_if_rows: bool = True


class LoadStrategy:
    watermark: Optional[WatermarkSpec] = None
```
**Workaround.** `LoadStrategy.watermark_column = "load_ts"` plus `business_rules` lines giving the store table, job
name, initial value, the step it is computed from, and the empty-batch rule.

## G5 Lookup redirect has no output stream (T5)
**Gap.** `LKP Current dim_policy` has `NoMatchBehavior=1` (line 103). The no-match rows are the NEW-policy insert
path, but a `LookupStep` has a single output.

**Proposal.** When `on_no_match == redirect`, the step exposes `<id>.match` and `<id>.no_match`
(like `RouterStep.output_names()`), and `_validate_dag` adds both names to `available`.

**Workaround** (also in `parsers/CLAUDE.md`). Use a `return_null` lookup, then a `RouterStep` with groups `match`
(`existing_policy_key IS NOT NULL`) and `no_match` (`existing_policy_key IS NULL`), `first_match_only=True`,
`default_group=None`. The notes say this stands in for the redirect.

## G6 Post-load actions (FactPremium)
**Gap.** `OUTPUT $action INTO @MergeOut` and the `etl.job_log` writes for both SUCCESS and FAILED cases have no
place in the IR.

**Proposal.**
```python
class PostLoadAction(_Base):
    kind: Literal["job_log", "row_counts"]
    on: Literal["success", "failure", "always"]
    target_table: str
    columns: dict[str, str]  # {target_column: source description}
    original_snippet: str


class MigrationUnit:
    post_load_actions: list[PostLoadAction] = Field(default_factory=list)
```
**Workaround.** One `UnsupportedConstruct(kind="job_log")` with `suggested_pattern` set to "write a job-log row from
the Delta MERGE metrics (numTargetRowsInserted / numTargetRowsUpdated); on exception, write FAILED and re-raise".

## G7 JoinStep cannot take a reference alias (T17)
**Gap.** `_validate_dag` only counts sources and step ids as available join inputs. The FactPremium point-in-time
join to `edw.dim_policy` has to put the dimension in `sources`, even though it is reference data.

**Proposal.** Allow `references` aliases in `JoinStep.inputs` (only the right-hand input).

**Workaround.** Model `edw.dim_policy` as a source with `system="InsuranceDW"`, and say so in its join step's
`description`.

## G8 SCD2 error message
**Gap.** When only `tracked_columns` is missing, `LoadStrategy` raises `"scd2 requires tracked_columns and ok"`.

**Proposal.** Build the message from the actual list of missing fields, e.g. `"scd2 requires: tracked_columns"`.

**Workaround.** None needed. Tests match on `"scd2 requires"` only.

## G9 `is_ready_for_codegen()` is true for manual units (T21)
**Gap.** The trigger unit has a `suggested_pattern` on its `UnsupportedConstruct`, so the method returns True even
though `complexity.recommended_path == manual`.

**Proposal.** Also require `self.complexity is None or self.complexity.recommended_path != MigrationPath.MANUAL`.

**Workaround.** The graph router checks `recommended_path` before calling the method (AGENTS.md §Routing).

## G10 SCD2 sentinel and surrogate key generation (T7)
**Gap.** DimPolicy sets `row_effective_to = "9999-12-31 00:00:00"` (line 175) and `is_current = 1` for new versions,
and leaves `policy_key` to an IDENTITY column. None of this can be expressed in `LoadStrategy`.

**Proposal.**
```python
class LoadStrategy:
    end_date_sentinel: Optional[str] = None  # "9999-12-31 00:00:00"
    surrogate_key_strategy: Optional[Literal["identity", "max_plus_row_number", "hash"]] = None
```
**Workaround.** `DER SCD2 Columns` stays a `DerivedColumnStep`. The sentinel is a deterministic literal and
`row_effective_from` is a pending run_ts expression. A `business_rules` note says the target uses an IDENTITY key.

---

## Open questions
1. **Golden vs the run_ts rule.** `tests/golden/Load_DimPolicyholder.ir.json` (read-only) has
   `age_band.spark_sql` using `current_date()` and `dw_load_ts.spark_sql = current_timestamp()`. AGENTS.md says
   runtime values resolve to one pinned `run_ts`.
   Proposed resolution: the golden stays as is. Codegen replaces `current_date()` / `current_timestamp()` in IR
   expressions with the `run_ts` job argument, so the generated job is pinned even though the IR text is not.
   A codegen test asserts no `current_date()` / `current_timestamp()` survives in generated code.
   *Needs a decision before Day 3.*
2. **G1 vs G4 overlap.** The watermark could be one `RunParameter(kind=watermark)` plus a `WatermarkSpec`, or only
   the spec. Proposal: keep both. The parameter is the job argument; the spec says how it is read and advanced.
