# agents/ — local rules

## Prompts
- Prompts live in `agents/prompts/<agent>/v<N>.md` (agents: `translator`, `codegen`, `reporter`).
- Each file starts with front matter: `version`, `date`, `changelog` (one line per change).
- A published version never changes. To change a prompt, add `v<N+1>.md` and bump `agents/prompts/registry.toml`.
- No prompt text in Python files. Load prompts through the registry only.
- The active prompt version goes into LangSmith run metadata and into the unit report.

## LLM access
- The only entry point is `agents/llm.py::get_chat_model()`. It returns `ChatBedrockConverse`
  (`langchain-aws`) when `MIGRATEIQ_LLM_PROVIDER=bedrock`, and `ChatAnthropic` when it is `anthropic`.
- Temperature is 0. Model id comes from `MIGRATEIQ_MODEL_ID`. No model ids in agent code.
- Every call uses `model.with_structured_output(Schema, include_raw=True)`. Output schemas live in
  `agents/schemas.py` (Pydantic v2, `extra="forbid"`).
- A parse or `ValidationError` on the structured output counts as a failed attempt. Never repair it with regex.

## Inputs
- Agents receive `MigrationUnit.model_dump(mode="json")` (or a subset) plus `RetryFeedback`.
- Never pass file paths, raw XML, or raw SQL from `sample_artifacts/`. A test asserts prompts contain no
  `sample_artifacts/` path and no `<DTS:` / `<POWERMART` / `CREATE PROCEDURE` text.
- The translator gets only `unit.pending_expressions()` plus the columns each one references.
- The translator may set `spark_sql`, `translation_status="llm"`, and `notes`. Nothing else. `original` stays as is.
- Codegen fills only the `{# LLM: ... #}` blocks of the Jinja skeleton. The skeleton is fixed per `LoadMode`.

## Retries and error feedback
- Max 3 attempts per unit, counted in graph state `attempt`.
- After a failed attempt, codegen gets
  `RetryFeedback{attempt, stage: "lint"|"compile"|"spark_run"|"reconcile", error, recon_diff}`.
  `error` is truncated to 4000 chars (keep the tail). `recon_diff` lists failing metrics per column.
- The feedback replaces earlier feedback; prior attempts' code is not resent.
- After attempt 3 fails: set affected expressions to `needs_review` and route to approval with the last diff.

## Token and cost logging
- Every LLM call logs one record: `unit_id, agent, prompt_version, model_id, input_tokens, output_tokens,
  latency_ms, usd, attempt`. Token counts come from the raw response's usage metadata.
- `usd` uses the price table in `config.py`. Unknown model -> `usd=None` and a WARNING log.
- Records are appended to graph state and summed in the report.

## Tests
- Default suite uses `tests/agents/fakes.py` (recorded structured responses). No network.
- Real calls only in tests marked `@pytest.mark.live`. Run them only when asked: `uv run pytest -q -m live`.
- Run: `uv run pytest -q tests/agents tests/codegen`.
