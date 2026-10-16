# MigrateIQ — 6-day build plan

Rules for this plan: see `AGENTS.md`. Trap ids (T1–T22) refer to `sample_artifacts/README.md`; X1–X5 are
extra traps found during review (README is read-only, so they live here). Every acceptance criterion
is a test or command that can pass or fail.

## Fixed decisions
- Two golden tiers. `tests/golden/Load_DimPolicyholder.ir.json` is the end-to-end golden (final state,
  statuses `verified`/`deterministic`). Parser goldens live in `tests/golden/parser/<unit_id>.ir.json`
  (statuses `pending`/`deterministic`). `tests/helpers/golden.py` ignores `source_artifact.sha256` and
  `source_artifact.parsed_at`. New goldens are shown to the user and written only after approval.
- Oracles: `tests/oracles/<unit_id>.duckdb.sql`, written by a person, never by an LLM. They emulate
  legacy quirks explicitly (case-insensitive collation, Informatica NULL concatenation, DATEDIFF yy).
- IR changes go through `docs/ir_v1_1_proposal.md` on Day 1. Approved and implemented 2026-10-11 (IR 1.1.0).
- Local table format: Delta Lake (`delta-spark`). MERGE and SCD2 targets are Delta tables.
- Pinned run timestamp: `MIGRATEIQ_RUN_TS=2026-01-01T00:00:00` (matches `User::RunStartTime` in Load_DimPolicy).

## Directory tree
```
migrateiq/
  AGENTS.md  CLAUDE.md  README.md  pyproject.toml  uv.lock  .env.example  .gitignore
  docs/
    PLAN.md  ir_v1_1_proposal.md  architecture.md  demo.md
  sample_artifacts/                     READ-ONLY
  src/migrateiq/
    cli.py                              Typer: parse | gen-data | run | report
    config.py                           env settings, model price table
    logging_setup.py
    ir/        models.py (FROZEN)  io.py
    inventory/ scanner.py
    parsers/   CLAUDE.md  base.py  ssis.py  informatica.py  tsql.py  complexity.py
               expr_rules/ ssis_expr.py  informatica_expr.py
    agents/    CLAUDE.md  llm.py  schemas.py  translator.py  codegen.py  reporter.py
               prompts/ registry.toml  translator/v1.md  codegen/v1.md  reporter/v1.md
    codegen/   render.py  templates/ base_job.py.j2  full_overwrite.py.j2  append.py.j2
                                     merge_upsert.py.j2  scd2_merge.py.j2
    validate/  lint.py  compile.py  spark_run.py
    reconcile/ oracle.py  compare.py  metrics.py
    graph/     state.py  nodes.py  build.py
    datagen/   generator.py  scenarios.py
  tests/
    conftest.py  test_ir_models.py
    helpers/ golden.py
    golden/  Load_DimPolicyholder.ir.json (READ-ONLY)  parser/<unit_id>.ir.json
    oracles/ <unit_id>.duckdb.sql       human-written
    parsers/ agents/ codegen/ validate/ reconcile/ graph/ datagen/
    traps/   test_traps.py              one test_T<NN> / test_X<N> per trap
  data/synthetic/                       gitignored
  out/                                  gitignored
```

## IR v1.0 gaps -> `docs/ir_v1_1_proposal.md`
| # | Gap | Traps | Proposed change | Workaround until approved |
|---|---|---|---|---|
| G1 | No runtime parameters | T3, T19 | `MigrationUnit.parameters: list[RunParameter{name, kind: run_ts\|watermark, legacy_ref}]` | `run_ts` convention + `business_rules` note |
| G2 | MERGE update condition missing | T20 | `LoadStrategy.update_condition: Expression` | condition text in `business_rules`, template reads it |
| G3 | Insert-only / update-only columns | FactPremium | `LoadStrategy.insert_only_columns`, `update_only_columns` | `business_rules` note |
| G4 | Watermark storage + advance rule | T19 | `WatermarkSpec{store_table, job_name, column, advance_only_if_rows}` | `LoadStrategy.watermark_column` + note |
| G5 | Lookup redirect has no output stream | T5 | `LookupStep` emits `<id>.match` / `<id>.no_match` when `on_no_match=redirect` | `return_null` lookup + Router on key IS NULL |
| G6 | Post-load actions (job_log, OUTPUT counts) | FactPremium | `post_load_actions: list[PostLoadAction]` | `unsupported_constructs` with pattern |
| G7 | JoinStep cannot take a reference alias | T17 | allow `references` aliases in `JoinStep.inputs` | model dim table as a source |
| G8 | SCD2 error text says "and ok" | — | fix message | none needed |
| G9 | `is_ready_for_codegen()` true for manual units | T21 | also require `recommended_path != manual` | check in graph router |
| G10 | No sentinel / surrogate key generation spec for SCD2 | T7 | `LoadStrategy.end_date_sentinel`, `surrogate_key_strategy` | derived columns + note |

## Milestones

### Day 1 — scaffolding, IR proposal, parsers
Work: `pyproject.toml` (uv, ruff, pytest markers `live`), `cli.py parse`, inventory, `base.py`,
`ssis.py`, `informatica.py`, `tsql.py`, `complexity.py`, `tests/helpers/golden.py`, `docs/ir_v1_1_proposal.md`.
Acceptance:
- `uv sync --all-extras && uv run pytest -q tests/test_ir_models.py` passes (existing contract tests).
- `uv run migrateiq parse <file>` emits a valid `MigrationUnit` for all 5 artifacts.
- 5 parser goldens approved by the user and matched by `uv run pytest -q tests/parsers`.
- Structural trap tests pass: `uv run pytest -q tests/traps -k "T01 or T04 or T05 or T06 or T07 or T08 or
  T09 or T10 or T11 or T12 or T13 or T15 or T16 or T17 or T20 or T21 or T22"`. Specifically:
  T4 `on_no_match=fail`; T5 redirect workaround present; T6 `tracked_columns == [annual_premium, deductible,
  status_code]`; T7 `load_strategy.mode == scd2`; T8 `'P'` filter in `sql_override.original`; T9 `join_type
  == left` with claims input first; T10 `is_unconnected=True`; T11 variable ports chained in order; T13
  `first_match_only is False`; T15 FACT_CLAIMS `full_overwrite`, ERR_CLAIMS `append`; T16 `DeduplicateStep`
  `order_by == ["load_ts DESC"]`; T17 non-equi condition kept; T20 `<>` in update condition, no `<=>`;
  T21 `UnsupportedConstruct(kind="trigger")` with `suggested_pattern`; T22 BR-8 in `business_rules`.
- Load_DimPolicyholder `recommended_path == template`; trigger `recommended_path == manual`.
- `tests/parsers/test_no_llm_imports.py` and per-parser determinism tests pass.
- `docs/ir_v1_1_proposal.md` covers G1–G10 and is sent for approval.

### Day 2 — synthetic data, oracle harness, reconciliation, validator
Work: `datagen/`, `cli.py gen-data`, `reconcile/`, `validate/`, oracle stubs (user writes the SQL).
Acceptance:
- `uv run migrateiq gen-data --seed 42 --out data/synthetic` is deterministic (same seed -> same file hashes).
- `uv run pytest -q tests/datagen` asserts every scenario row in the spec below exists (one test per trap).
- `reconcile.compare` reports row count, per-column checksum, null count, and distinct count, with an
  `exclude_columns` list; `dw_load_ts` is excluded for T3.
- Validator returns a structured result for ruff, `py_compile`, and a local Spark + Delta run of a fixture job.
- Once the user has written `tests/oracles/ssis.Load_DimPolicyholder.duckdb.sql`: a hand-written reference
  PySpark job reconciles with it. T1 email rows and T2 boundary DOB rows match the legacy values at the pinned `run_ts`.

### Day 3 — LLM client, translator agent, template codegen
Work: `agents/llm.py`, `schemas.py`, `translator.py`, prompt registry, `codegen/render.py`,
`full_overwrite.py.j2`, `graph/` skeleton (parse -> translate -> codegen -> validate -> reconcile).
Acceptance:
- `get_chat_model()` returns Bedrock or Anthropic chat model based on `MIGRATEIQ_LLM_PROVIDER`; temperature 0 (unit test).
- Translator, with fake LLM, fills every pending expression with `translation_status="llm"` and leaves
  `original` unchanged (assert byte-equal).
- Prompt-leak test: no prompt contains `sample_artifacts/`, `<DTS:`, `<POWERMART`, or `CREATE PROCEDURE`.
- `uv run migrateiq run sample_artifacts/ssis/Load_DimPolicyholder.dtsx --out out/` reconciles and the final
  IR matches `tests/golden/Load_DimPolicyholder.ir.json` via the golden helper (T1, T2, T3).
- Informatica expressions for T12 and T14 translate and pass the expression-level tests
  (`CASE WHEN`, `concat_ws`/`coalesce` keeps NULL-as-empty).
- Token log records written for each call, with `prompt_version` and `usd`.
- The graph re-scores `complexity` (`parsers/complexity.score`) after reconciliation, so the final IR's
  factors (e.g. `pending_expressions: 0`) match the end-to-end golden.

### Day 4 — LLM-filled codegen, retry loop: DimPolicy and FACT_CLAIMS
Work: `scd2_merge.py.j2`, `append.py.j2`, `agents/codegen.py`, retry node, `RetryFeedback`.
Acceptance:
- T4: on dataset `unknown_product`, both the oracle and the PySpark job fail (job raises, no rows written).
- T5–T7: on dataset `base`, new-version count, expired-version count, and unchanged count match the oracle;
  the generated job uses one Delta `MERGE`, not per-row updates (assert no `.collect()`/loop over rows).
- T6: policies with only `policy_number`/`expiration_date` changes produce no new version.
- T8: `status_code='P'` rows absent from output.
- T9–T12, T14, X1, X5 reconcile for `edw.fact_claims` and `edw.err_claims`.
- T15: pre-existing `err_claims` rows still present after the run; `fact_claims` contains only this run's rows.
- Retry: a fault-injected codegen response fails validation; graph retries exactly up to 3 times, feeds
  `RetryFeedback` each time, then marks expressions `needs_review` and reaches the approval node (`tests/graph`).

### Day 5 — FactPremium, trigger path, full graph, approval, report
Work: `merge_upsert.py.j2`, watermark handling, manual route, `interrupt()` approval, `agents/reporter.py`, LangSmith.
Acceptance:
- T16: re-sent txns load only the latest `load_ts` copy.
- T17: boundary txns (`txn_date == row_effective_from`, `== row_effective_to`) join to the same version as the oracle.
- T18: rows dropped by the inner join are excluded, and the report shows the dropped-row count.
- T19: running `premium_batch_1` then `premium_batch_2_empty` leaves the watermark unchanged after batch 2;
  X4 scenario matches the oracle (rows lost by legacy are also lost by PySpark, and the report flags them).
- T20: identical re-sent txns cause no update (`dw_update_ts` stays NULL); changed ones update.
- X2: lower-case `cancel` txns get the sign the oracle gives them (collation emulated).
- T21/T22: the trigger unit skips codegen, its report contains a CDC / Delta Change Data Feed + audit job
  pattern, and `out/` has no generated `.py` for it.
- Approval: the graph pauses at `interrupt()`, resumes from the checkpoint with approve/reject, and reject
  routes to report with status `rejected`.
- LangSmith runs carry `unit_id` and `prompt_version` metadata (asserted with a tracing stub).

### Day 6 — README, architecture diagram, demo
Acceptance:
- `docs/architecture.md` has a Mermaid diagram whose nodes match `graph/build.py` node names (test parses both).
- `README.md` quickstart, run in a fresh clone, works: `uv sync --all-extras && uv run pytest -q &&
  uv run migrateiq run --all --out out/`.
- `uv run migrateiq run --all --out out/` writes `out/scorecard.md` listing T1–T22 and X1–X5 with pass/fail.
- `docs/demo.md` is a scripted walkthrough that runs in under 10 minutes.
- Final check: `uv run pytest -q`, ruff clean, `git diff --stat -- sample_artifacts tests/golden` empty.

## Synthetic insurance data generator spec
Command: `uv run migrateiq gen-data --seed 42 --out data/synthetic [--scenario <name>]`.

Output:
- One Parquet file per table under `data/synthetic/<scenario>/<schema>.<table>.parquet`, and one
  `data/synthetic/<scenario>/legacy.duckdb` with the same rows. The oracle and Spark read identical data.
- `manifest.json` per scenario: seed, `run_ts`, row counts, and a `trap -> [row keys]` index used by tests.
- Schemas follow `sample_artifacts/schema/insurance_ddl.sql` exactly (types, nullability).

Scenarios:
| Scenario | Purpose |
|---|---|
| `base` | Every source table plus prior target state; hits all row-level traps |
| `unknown_product` | `base` plus one policy with `product_id=999` (T4 whole-run failure) |
| `premium_batch_1` | First premium increment after watermark |
| `premium_batch_2_empty` | Increment where every row is filtered or join-dropped (T19, X4) |
| `dedupe_ties` | Opt-in: duplicate `txn_id` with equal `load_ts` (X3; non-deterministic in legacy, excluded from reconciliation) |

Prior target state in `base`: `edw.dim_product`, `edw.dim_agent` (no -1 member), `edw.dim_policy` with current
and expired versions, `edw.fact_claims` rows from a "previous run", `edw.err_claims` rows from a previous run,
`edw.fact_premium` rows, and `etl.watermark` for `usp_Load_FactPremium` at `2025-12-31T00:00:00`.

Base volumes (small enough for local Spark, each trap row is extra and tagged): 200 policyholders,
20 agents, 6 products, 300 policies, 400 claims, 900 claim payments, 1200 premium transactions.

Trap rows (each row tagged in `manifest.json`):
| Trap | Rows to generate | Expected legacy outcome |
|---|---|---|
| T1 | `email` NULL, `''`, `'   '`, `' Mixed@Case.COM '` | NULL, NULL, NULL, `'mixed@case.com'` |
| T2 | DOB `2001-12-31`, `2001-01-01`, `1981-12-31`, `1961-12-31`, NULL | yy diff 25 -> `'25-44'` (true age 24); NULL -> `'UNKNOWN'` |
| T3 | all rows; `run_ts` pinned | `age_band` stable; `dw_load_ts` excluded from checksums |
| T4 | (`unknown_product`) policy with `product_id=999` | run fails; no rows written |
| T5 | 20 policies not in `dim_policy` | inserted as new current version |
| T6 | existing policies with only `policy_number` or `expiration_date` changed | no new version |
| T6 | existing policies with `status_code`, `annual_premium`, or `deductible` changed | old expired at `run_ts`, new current version |
| T7 | 10 changed policies in one run | one set-based MERGE gives same counts as oracle |
| T8 | `status_code='P'` new policies; existing current version whose source is now `'P'` | not loaded; existing version untouched |
| T9 | claims with no payments; claims whose payments are all `is_void=1`; payments on `is_deleted=1` claims | claim kept with totals 0; deleted claims absent |
| T10 | `handling_agent_id` NULL; id not in `dim_agent`; valid id | `agent_key` -1, -1, real key |
| T11 | claims with NULL aggregated totals | `total_paid = 0` via variable-port order |
| T12 | `total_paid+reserve` = 100000.00 and 99999.99; LIABILITY reserve 50000.00 and 49999.99; FIRE reserve 50000 | Y, N, Y, N, N |
| T13 | none (groups are mutually exclusive) | structural test only |
| T14 | claims with NULL `policy_id`; `policy_id` with no current `dim_policy` | `'POLICY_NOT_FOUND:'`, `'POLICY_NOT_FOUND:<id>'` |
| T15 | prior-run rows in `fact_claims` and `err_claims` | `fact_claims` replaced, `err_claims` appended |
| T16 | `txn_id` re-sent 2–3 times with different amounts and later `load_ts` | latest copy wins |
| T17 | txns on `row_effective_from` exactly, 1 day before `row_effective_to`, on `row_effective_to` exactly | `>=` from, `<` to |
| T18 | txns for unknown `policy_id`; txns dated before the first `dim_policy` version | dropped; count reported |
| T19 | (`premium_batch_2_empty`) only `source_system='OTHER'` or `load_ts <= watermark` rows | 0 rows; watermark unchanged |
| T20 | re-sent txn identical to the target row; re-sent txn with changed `fees` | no update; update with `dw_update_ts=run_ts` |
| T21 | `claim_status_events` change feed (claim_id, old, new, ts, user) | used by the proposed CDC audit pattern demo only |
| T22 | CLOSED/DENIED claims with NULL `closed_date`; REOPENED claims with a `closed_date` | `days_open` NULL / per legacy; no warehouse-side mutation |

Extra traps (not in the README catalog):
| Id | Source | Trap | Data |
|---|---|---|---|
| X1 | FACT_CLAIMS `O_DAYS_OPEN` | `IN(CLAIM_STATUS, ...)` tests the raw value, output uses `UPPER` | claims with status `'closed'`, `' CLOSED'` -> `days_open` NULL |
| X2 | FactPremium | SQL Server default collation is case-insensitive; DuckDB and Spark are not | `txn_type='cancel'` -> negative premium in legacy |
| X3 | FactPremium | `ROW_NUMBER` ties on `load_ts` are non-deterministic | `dedupe_ties` scenario only |
| X4 | FactPremium | watermark = MAX(load_ts) after the inner join, so join-dropped rows with lower `load_ts` are lost for good | batch with a dropped row older than a kept row |
| X5 | FACT_CLAIMS | `ERR_CLAIMS` needs `CLAIM_ID`, `CLAIM_NUMBER`, `DW_LOAD_TS` but `EXP_ERROR_REASON` does not output them; connectors are abridged | parser passes ports through by name; err rows have all NOT NULL columns |

Generator rules:
- Pure function of `(seed, scenario)`; uses `random.Random(seed)`, no global RNG, no clock reads.
- Respects all DDL NOT NULL and FK constraints, except where a trap requires a missing FK target (T10, T14, T18).
- `datagen` never imports from `parsers/` or `agents/`; the trap rows are written from this spec, not from the IR.
