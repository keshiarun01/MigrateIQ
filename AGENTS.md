# AGENTS.md — MigrateIQ

Source of truth for every AI coding agent (Claude Code, Copilot, others). Tool-specific files only add to it.

## Purpose
MigrateIQ migrates legacy ETL (SSIS packages, Informatica PowerCenter mappings, T-SQL procs/triggers) to
PySpark on AWS (Glue + Delta Lake) for an insurance data warehouse. Parsers turn each artifact into an IR;
agents generate PySpark from the IR only. A migration is correct when its output reconciles with the legacy
logic run in DuckDB on the same data. Legacy behaviour wins over "better" behaviour.

## Pipeline
```
inventory -> parser -> IR -> translator -> codegen -> validator -> reconcile -+-> approval -> report
                                              ^                               |
                                              +------ retry (max 3) <---------+ (fail)
```
| Stage | Module | Rule |
|---|---|---|
| inventory | `inventory/scanner.py` | Find artifacts, detect technology, compute sha256 |
| parser | `parsers/{ssis,informatica,tsql}.py` | Deterministic. No LLM. lxml (XML), sqlglot `read="tsql"` |
| translator | `agents/translator.py` | Only `MigrationUnit.pending_expressions()`; sets status `llm` |
| codegen | `codegen/render.py` + `agents/codegen.py` | Jinja skeleton from IR, LLM fills marked blocks only |
| validator | `validate/{lint,compile,spark_run}.py` | ruff, `py_compile`, run on local Spark + Delta |
| reconcile | `reconcile/{oracle,compare,metrics}.py` | DuckDB oracle vs PySpark: row counts, column checksums, null + distinct counts |
| retry | `graph/nodes.py` | Max 3 attempts; error text + recon diff fed back to codegen |
| approval | `graph/build.py` | LangGraph `interrupt()`; human approves or rejects |
| report | `agents/reporter.py` | Per-unit report: traps hit, dropped rows, cost, unsupported constructs |

Routing: if `complexity.recommended_path == "manual"` or any `UnsupportedConstruct` lacks
`suggested_pattern`, skip translator/codegen and go straight to report. Do not rely on
`is_ready_for_codegen()` alone: it returns True for the trigger once a pattern is suggested.

## Directory map
```
src/migrateiq/
  cli.py          Typer CLI (only place print/console output is allowed)
  config.py       Env settings, model price table
  ir/             models.py (FROZEN contract), io.py (load/dump helpers)
  inventory/      artifact discovery
  parsers/        one module per technology + expr_rules/ + complexity.py   (see parsers/CLAUDE.md)
  agents/         llm.py, schemas.py, translator, codegen, reporter, prompts/ (see agents/CLAUDE.md)
  codegen/        Jinja templates per LoadMode
  validate/       lint, compile, local Spark run
  reconcile/      DuckDB oracle runner + comparison metrics
  graph/          LangGraph state, nodes, graph build
  datagen/        synthetic insurance data generator
tests/            golden/ (RO), golden/parser/, oracles/ (human-written), traps/, one dir per module
sample_artifacts/ READ-ONLY legacy fixtures + trap catalog (README.md)
docs/             PLAN.md, ir_v*_proposal.md, architecture.md
```

## IR contract (`src/migrateiq/ir/models.py`)
- Frozen. To change it, write `docs/ir_v<version>_proposal.md` and wait for approval. Do not edit it.
- Every stage reads and writes `MigrationUnit`. No side-channel dicts between stages.
- `Expression.original` is verbatim legacy text and is never rewritten after parsing.
- Status ownership: parser sets `pending` or `deterministic`; translator sets `llm`; reconciliation sets
  `verified`; retry exhaustion sets `needs_review`. No stage sets another stage's status.
- Anything not understood goes to `unsupported_constructs` with `original_snippet` and `reason`.
- Steps are listed in topological order. Router outputs are `<step_id>.<group>`.
- String length goes in `Column.precision`. The target projection is `target.dataset.columns`.
- Runtime values (`GETDATE()`, `SYSDATETIME()`, `SESSSTARTTIME`, `@RunTs`, `User::RunStartTime`) resolve to
  one pinned `run_ts` (`MIGRATEIQ_RUN_TS`). Generated code takes `run_ts` as a job argument.
- IR v1.0 gaps and workarounds are listed in `docs/PLAN.md` §IR v1.1.

## Legacy semantics
- `sample_artifacts/README.md` (T1–T22) is the spec. Each trap has a test `tests/traps/test_traps.py::test_T<NN>`.
- Preserve behaviour that looks like a bug: NULL-unsafe `<>` (T20), `DATEDIFF("yy")` boundary counting (T2),
  inner-join row drops (T18). Surface them in the report; do not change them.
- Extra traps found after the catalog (X1–X5) are in `docs/PLAN.md` and get tests too.

## Conventions
- Python 3.11, full type hints on every function, `from __future__ import annotations`.
- Google-style docstrings on public functions and classes.
- `logger = logging.getLogger(__name__)`; no `print` outside `cli.py`.
- Pydantic v2 models for every payload that crosses a module boundary.
- No credentials, ARNs, account ids, or regions in code. Read from env; keep `.env.example` current.
- `pathlib.Path`, not `os.path`. Sort keys, columns, and lists in serialized output for stable diffs.
- Generated PySpark: `pyspark.sql.functions as F`, `F.expr(spark_sql)` for IR expressions, Delta for MERGE/SCD2.

## Environment variables
`MIGRATEIQ_LLM_PROVIDER` (`bedrock` | `anthropic`) · `MIGRATEIQ_MODEL_ID` · `AWS_PROFILE` · `AWS_REGION` ·
`ANTHROPIC_API_KEY` · `LANGSMITH_TRACING` · `LANGSMITH_API_KEY` · `LANGSMITH_PROJECT` ·
`MIGRATEIQ_RUN_TS` (default `2026-01-01T00:00:00`)

## Commands
```bash
uv sync --all-extras                                   # install
uv run pytest -q                                       # full suite (live tests excluded by default)
uv run pytest -q -m live                               # real LLM calls; costs money; only when asked
uv run pytest -q tests/traps                           # trap catalog
uv run pytest -q tests/test_ir_models.py               # IR contract
uv run ruff check . && uv run ruff format --check .    # lint + format
uv run migrateiq parse sample_artifacts/ssis/Load_DimPolicyholder.dtsx      # print IR JSON
uv run migrateiq gen-data --seed 42 --out data/synthetic                    # synthetic data
uv run migrateiq run sample_artifacts/ssis/Load_DimPolicyholder.dtsx --out out/   # full graph, one artifact
```

## Definition of done (every change)
1. `uv run pytest -q` passes with 0 failures and no new skips.
2. `uv run ruff check .` and `uv run ruff format --check .` are clean.
3. `git diff --stat -- sample_artifacts tests/golden tests/oracles src/migrateiq/ir/models.py` is empty,
   unless the user approved that change in this session.
4. New behaviour has a test; a bug fix has a regression test.
5. Every trap test touching the changed component still passes.
6. New dependencies were added with `uv add` after approval, and `uv.lock` is committed with them.

## NEVER
- Edit `sample_artifacts/`, `tests/golden/`, `tests/oracles/*.sql`, or `src/migrateiq/ir/models.py`.
- Import or call an LLM from `parsers/`.
- Give an agent raw legacy XML/SQL or artifact file paths. Agents get IR JSON only.
- Drop, stub, or fake a construct the parser cannot handle. It goes to `unsupported_constructs`.
- "Fix" legacy semantics: `<>` -> `<=>`, `DATEDIFF yy` -> `months_between/12`, INNER -> LEFT join, etc.
- Generate trigger-emulation code in Spark (T21). Propose a CDC / Delta Change Data Feed pattern.
- Use LLM temperature > 0, or parse free-text LLM output. Always `with_structured_output()`.
- Write or edit oracle SQL. Oracles are written by a human.
- Commit `.env`, keys, or tokens.
- Delete, skip, xfail, or loosen a test to make a change pass.
- Run `-m live` tests or call AWS without being asked.
