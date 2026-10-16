# IR v1.2 proposal

Status: **draft, collecting gaps**. Not submitted for approval yet. `src/migrateiq/ir/models.py` stays at 1.1.0.
Gap ids continue from `docs/ir_v1_1_proposal.md` (G1–G10).

## G11 Conditional uniqueness for SCD2 targets (T7)
**Gap.** `edw.dim_policy` (Load_DimPolicy) needs "at most one current row per business key": `policy_id` is unique
where `is_current = 1`. Today the SSIS parser emits no `unique` rule for it. The only PK, `policy_key`, is an IDENTITY
column that isn't in the target projection, and `DataQualityRule` cannot scope a rule to a subset of rows.

**Why not `custom_sql`.** `DQRuleType.CUSTOM_SQL` has no defined contract: nothing says what the SQL must return,
what counts as a pass, or which dialect it uses. Emitting one would invent a convention that DQ codegen would then
depend on.

**Proposal.**
```python
class DataQualityRule:
    condition: Optional[Expression] = Field(
        None, description="G11: the rule applies only to rows where this holds, e.g. is_current = 1"
    )
```
The SSIS parser would then emit, for an `scd2` target:
`unique` on `load_strategy.business_keys`, with `condition` = `<current_flag_column> = 1` and
`derived_from="SCD2 current version"`.

**Workaround until approved.** None. No rule is emitted. The DimPolicy parser golden (step 8) will not contain
one.

## G12 Values for insert-only / update-only columns (FactPremium)
**Gap.** IR v1.1 (G3) records *which* columns are insert-only or update-only, not *what* they are set to.
In `usp_Load_FactPremium` the MERGE sets `dw_load_ts = @RunTs` on INSERT, `dw_update_ts = @RunTs` on UPDATE, and
`dw_update_ts = NULL` on INSERT. Today these values exist only as a `business_rules` sentence, so codegen would have
to read prose.

A related wrinkle: the `row_counts` `PostLoadAction` has `target_table="@MergeOut"`, which is a T-SQL table variable,
not a real table. The counts only feed the job_log row.

**Proposal.**
```python
class ColumnAssignment(_Base):
    column: str
    on: Literal["insert", "update"]
    value: Expression  # e.g. '@RunTs' (tsql) -> run_ts parameter, or 'NULL'


class LoadStrategy:
    column_assignments: list[ColumnAssignment] = Field(default_factory=list)
```
`insert_only_columns` / `update_only_columns` stay as they are, for compatibility. `PostLoadAction(kind="row_counts")`
gets `target_table: Optional[str]` (None when the counts are internal), or is folded into the job_log action's `columns`.

**Workaround until approved.** The `business_rules` line
`dw_load_ts = run_ts on INSERT; dw_update_ts = run_ts on UPDATE and NULL on INSERT.` plus G3's column lists.

## G13 Target column mapping (FACT_CLAIMS, X5)
**Gap.** `Target` has no column mapping, so a target column fed by a port with a different name can't be
expressed. In `m_LOAD_FACT_CLAIMS`, `fact_claims.agent_key` comes from `o_agent_key`, `dw_load_ts` from
`o_dw_load_ts`, and so on (11 columns), and `err_claims.error_reason` comes from `o_error_reason`. Because the stream
carries every input column (`keep_input_columns=True`), the raw `claim_status` and the derived `o_claim_status` both
reach the target step. Only a mapping says which one is written.

**Proposal.**
```python
class Target:
    column_map: dict[str, str] = Field(
        default_factory=dict, description="G13: {target_column: input stream column}; identity when absent"
    )
```
Columns missing from `column_map` are taken by the same name. The Informatica parser fills it from explicit connectors
and name-inferred `X` / `O_X` links. The SSIS parser fills it from `externalMetadataColumnId` when the names differ.

**Workaround until approved.** One `business_rules` line per target:
`Target '<T>' columns fed by differently named ports (IR targets carry no column mapping): a <- o_a, ...`.
