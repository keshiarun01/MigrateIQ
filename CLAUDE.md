@AGENTS.md

# Claude Code workflow

## Before changing code
- Use plan mode for any change that touches more than one file, a graph-state field, or a prompt version.
- Before editing a component, read its trap rows in `sample_artifacts/README.md` and its section in
  `docs/PLAN.md`. Name the trap ids you are working on in the plan.
- Check `docs/PLAN.md` for the current milestone. Do not start work from a later day.

## While changing code
- After each edit, run the narrowest test that covers it, e.g. `uv run pytest -q tests/parsers/test_ssis.py`.
- Before saying a task is done, run `uv run pytest -q` and `uv run ruff check . && uv run ruff format --check .`
  and paste the summary lines.
- If a test fails, fix the code, not the test. A test is changed only when the user agrees the test is wrong.

## Subagents
- Parser work: one subagent per technology (SSIS, Informatica, T-SQL). Give it only `src/migrateiq/parsers/`,
  its artifact(s), its trap rows, its parser golden, and `ir/models.py` (read-only).
- Agent work (translator, codegen, reporter): give the subagent `src/migrateiq/agents/`, `codegen/`,
  sample IR JSON, and prompts. Never give it `sample_artifacts/`.
- Use the Explore subagent for read-only searches across the repo.
- Never mix parser and agent edits in one subagent. The main session merges results and runs the full suite.

## Stop and ask before
- Any change to `src/migrateiq/ir/models.py`. Write `docs/ir_v<version>_proposal.md` instead.
- Creating any golden file. Show the full JSON and wait for approval before writing it.
- Writing anything in `tests/oracles/*.sql`. Create the harness and an empty stub, then ask.
- Adding, removing, or upgrading a dependency.
- A trap whose correct behaviour is unclear from the README and the artifact.
- A failing test you did not cause.
- Running live LLM tests, Bedrock, or any AWS call.
- Deleting or renaming files.
- Changing the retry limit, temperature, approval gate, or prompt registry.

## Reporting back
End each task with:
- Files changed (paths).
- Test and lint commands run, with their result lines.
- Trap ids affected, and whether each one's test passes.
- Open questions or IR gaps hit, with a pointer to the proposal doc if one was written.
