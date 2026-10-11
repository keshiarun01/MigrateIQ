# parsers/ — local rules

## Deterministic only
- No imports from `langchain*`, `anthropic`, `boto3`, `botocore`, or `migrateiq.agents`.
  Enforced by `tests/parsers/test_no_llm_imports.py`.
- Same input -> byte-identical IR JSON (except `parsed_at`). Enforced by a determinism test per parser.
- No network, no randomness, no clock reads except `SourceArtifact.parsed_at`.

## Layout
- One module per technology: `ssis.py`, `informatica.py`, `tsql.py` (procedures and triggers).
- `base.py`: parser protocol `parse(path: Path) -> MigrationUnit`, sha256, legacy-type -> `DataType` maps.
- `expr_rules/`: exact-match translation tables per dialect. A rule applies only if it covers the whole
  expression; otherwise the expression stays `pending`, `spark_sql=None`.
- `complexity.py`: scores a `MigrationUnit` and sets `recommended_path`.
- XML: `lxml.etree` with explicit namespaces. T-SQL: `sqlglot.parse(sql, read="tsql")`.

## Expression capture
- `original` is copied verbatim. SSIS: use `FriendlyExpression`; if absent, use `Expression` with
  `#{...Columns[x]}` resolved to `x`, and record that in `notes`.
- Fill `referenced_columns` from the parsed expression, sorted.
- Never guess. If unsure, use `pending` and add a note.

## Traps each parser must respect (see `sample_artifacts/README.md`)
SSIS (`ssis.py`)
- T1 `ISNULL(x)` is `x IS NULL`. T3 `GETDATE()` -> `run_ts` parameter, with a note.
- T4 Lookup `NoMatchBehavior=0` -> `on_no_match=fail`. T5 `NoMatchBehavior=1` -> `redirect`. Until IR v1.1,
  model it as a `return_null` lookup followed by a Router on `<returned key> IS NULL`, and say so in `notes`.
- T6 tracked columns come from the Conditional Split expression, not from all columns.
- T7 OLE DB Command + Multicast expire path collapses into `LoadStrategy(mode=scd2)`. Keep `source_component`
  names and add a `business_rules` line describing the row-by-row update being replaced.
- T8 filters inside `SqlCommand` stay in `DatasetRef.sql_override`. Do not lift them into a `FilterStep`.
- Conditional Split: `first_match_only=True`. Unconnected outputs mean the rows are discarded; record that.

Informatica (`informatica.py`)
- T9 "Master Outer Join" -> `JoinType.LEFT`, DETAIL input first, MASTER second.
- T10 `:LKP.<name>(...)` inside an expression -> `LookupStep(is_unconnected=True)` before the expression step.
- T11 `LOCAL VARIABLE` ports evaluate in declared order, before output ports. Chain derived steps in that order.
- T12 `DECODE(TRUE, ...)` stays `pending` unless the rule table matches it exactly.
- T13 Router -> `first_match_only=False`. T14 `||` keeps NULL-as-empty semantics; note it on the expression.
- T15 read `<SESSION>` writer attributes for load mode per target. The mapping alone is not enough.
- Connectors are abridged in the fixture: pass ports through by name, and log each inferred link at WARNING.
- "Lookup Procedure" is used for both connected and unconnected lookups. Decide by `:LKP.` usage and connectors.

T-SQL (`tsql.py`)
- T16 `ROW_NUMBER() ... = 1` -> `DeduplicateStep` with the exact `order_by`.
- T17 range join conditions -> `JoinStep` with the full non-equi condition. T18 keep `INNER`.
- T19 watermark read/advance logic goes into `business_rules` (IR v1.1 adds `WatermarkSpec`).
- T20 `WHEN MATCHED AND (a <> b ...)`: keep `<>` verbatim. Never `<=>`.
- T21 a trigger -> `UnsupportedConstruct(kind="trigger")` with a `suggested_pattern` (CDC / Delta CDF + audit job),
  and `recommended_path=manual`.
- T22 BR-8 (source-row mutation) -> `business_rules` entry marked OLTP-side, plus an `UnsupportedConstruct`.
- Cursors, dynamic SQL, `@@ROWCOUNT` logic, TRY/CATCH, and job_log writes -> `unsupported_constructs`
  unless IR v1.1 covers them.

## Required tests per parser
- Golden: `tests/golden/parser/<unit_id>.ir.json`, compared through `tests/helpers/golden.py`
  (ignores `source_artifact.sha256` and `source_artifact.parsed_at`). New goldens need user approval.
- Structural trap assertions in `tests/traps/test_traps.py` for every trap listed above.
- Determinism (parse twice, compare JSON).
- Unsupported-construct test: a fixture snippet the parser cannot handle shows up in `unsupported_constructs`.
- Run: `uv run pytest -q tests/parsers tests/traps`.
