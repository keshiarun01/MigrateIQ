# Sample legacy artifacts (insurance domain)

Read-only fixtures. Parsers and agents must never modify these files.

| Artifact | Tech | Target | Complexity | Expected path |
|---|---|---|---|---|
| `ssis/Load_DimPolicyholder.dtsx` | SSIS | `edw.dim_policyholder` (full reload) | Low | template |
| `ssis/Load_DimPolicy.dtsx` | SSIS | `edw.dim_policy` (SCD2) | Medium-High | llm |
| `informatica/m_LOAD_FACT_CLAIMS.xml` | Informatica | `edw.fact_claims` + `edw.err_claims` | High | llm |
| `tsql/usp_Load_FactPremium.sql` | T-SQL proc | `edw.fact_premium` (incremental MERGE) | Medium-High | llm |
| `tsql/trg_Claim_StatusAudit.sql` | T-SQL trigger | `audit.claim_status_history` | Not portable | manual (pattern proposal) |

Schema for all source and target tables: `schema/insurance_ddl.sql`.

## Semantic traps (each one should become an eval case)

These are deliberate. A naive LLM conversion gets them wrong, and reconciliation should catch every one.

| # | Artifact | Trap | Correct Spark behaviour |
|---|---|---|---|
| T1 | DimPolicyholder | SSIS `ISNULL(x)` is a boolean test, unlike T-SQL `ISNULL(x, y)` | `x IS NULL` |
| T2 | DimPolicyholder | `DATEDIFF("yy", ...)` counts year boundaries, not actual age | `year(a) - year(b)`, not `months_between/12` |
| T3 | DimPolicyholder | `GETDATE()` is non-deterministic | Pin run date in reconciliation; exclude `dw_load_ts` from checksums |
| T4 | DimPolicy | Lookup 1 `NoMatchBehavior=0` fails the whole package | `on_no_match=fail`: raise if any product is unmatched |
| T5 | DimPolicy | Lookup 2 no-match output means NEW policy, not an error | `on_no_match=redirect` → insert path |
| T6 | DimPolicy | Changes to `policy_number`/`expiration_date` are ignored for existing policies | Only `status_code, annual_premium, deductible` are tracked |
| T7 | DimPolicy | Row-by-row OLE DB Command UPDATE | Set-based `MERGE` on Delta/Iceberg |
| T8 | DimPolicy | Source filter `status_code <> 'P'` lives in the SQL, not a component | Captured in `sql_override` |
| T9 | FACT_CLAIMS | "Master Outer Join" keeps all DETAIL rows | `claims LEFT JOIN payments_agg` (not right join) |
| T10 | FACT_CLAIMS | Unconnected lookup `:LKP.LKP_DIM_AGENT(...)` inside an expression | `LookupStep` with `is_unconnected=True`, then `coalesce(agent_key, -1)` |
| T11 | FACT_CLAIMS | Variable ports (`V_*`) evaluated before outputs | Inline or chain derived steps in order |
| T12 | FACT_CLAIMS | `DECODE(TRUE, cond1, v1, cond2, v2, default)` | `CASE WHEN cond1 THEN v1 WHEN cond2 THEN v2 ELSE default END` |
| T13 | FACT_CLAIMS | Router evaluates ALL groups, not first match | `first_match_only=False` |
| T14 | FACT_CLAIMS | `'A' \|\| NULL` = `'A'` in Informatica, but `concat('A', NULL)` = NULL in Spark | `concat_ws('', ...)` or `coalesce` |
| T15 | FACT_CLAIMS | Load behaviour (truncate vs append) lives in the SESSION, not the MAPPING | Parser must read `<SESSION>` too |
| T16 | FactPremium | Re-sent rows: dedupe with `ROW_NUMBER ... load_ts DESC` | `DeduplicateStep` |
| T17 | FactPremium | Point-in-time SCD2 join (range condition, not equality) | Non-equi join on `row_effective_from/to` |
| T18 | FactPremium | `INNER JOIN` silently drops txns with no policy version | Preserve the behaviour, but surface the dropped-row count in the report |
| T19 | FactPremium | Watermark advances only when rows were processed | Same; do not advance on empty batches |
| T20 | FactPremium | `MERGE ... WHEN MATCHED AND (a <> b ...)`: `<>` is NULL-unsafe | Keep plain `<>` (Spark has the same NULL semantics). Do NOT "improve" it to null-safe `<=>`, because that changes which rows update |
| T21 | Trigger | No row-level triggers in Spark | `UnsupportedConstruct` + CDC/change-feed pattern proposal |
| T22 | Trigger | BR-8 mutates the source row (`closed_date`) | Flag as an OLTP-side rule; it is not part of the warehouse migration |
