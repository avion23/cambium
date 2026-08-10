# agents.md - Cambium agent control plane

Read this file before work. It is the operating contract for agents in this repository.
Use source, tests, and recorded commands for current behavior.

## Authority and orientation

### Authority order

1. The task request controls scope and required behavior.
2. This file controls process and reporting.
3. Source and tests establish current behavior.
4. Architecture describes target behavior, not proof.
5. Research and history provide context and recorded decisions.

Record scope, authority, entry points, baseline, and reproduction before work.
Search from route registration, command tables, and imports.
Trace callers and tests; a failed name search is not proof of absence.

### Current truth

Current code is a deterministic Python harness. `src/cambium/__init__.py` exports only `__version__`; no public `Cambium`/`Session`/`Result` API exists.
`worker.do_work` is a deterministic marker/commit seed; no DSPy ReAct loop is present.
There is no example eval harness, smoke module, or example `__main__.py`; these are targets, and those attempted module commands exit 1 and are not checks.
Custos, Opifex, Nuntius, Surculus, and Unio are architecture target names mapped to current supervisor, worker, ipc, worktree, and merge portions and symbols.
Matching role modules are not proof. No TUI exists.

## Invariants

- Keep scope tight. Report required file-scope expansion before editing.
- Work in an isolated worktree. Verify `git rev-parse --show-toplevel` and `git worktree list`; never work or commit on `main` or in a shared integration checkout.
- Children never commit to the integration branch. Root may merge verified child commits.
- Read-only reports state `files changed: none, commit: none`.
- Do not force-push, rewrite shared history, reset another worktree, or delete work to hide a failure.
- Secrets are environment-only. Never log or send credentials.
- Reproduce first with a deterministic check. Make the smallest causal change; do not hide causes with a fallback, retry, default, or catch-all path.
- Preserve protocol, schema, worktree, approval, export, and module boundaries.
- Worker stdout is NDJSON protocol only. Send diagnostics to logging or stderr.
- Keep blocking disk and subprocess I/O off the event loop and use existing boundary helpers.
- Use existing dependencies and vocabulary. Tests are offline and deterministic with fixed fixtures and fake workers.
- Run the narrowest check, then an affected package or integration check when a change crosses a boundary.
- Use same-version dataset, canary, schema, and baseline evidence. If the baseline moves, stop and record a new anchor before comparing results; never silently re-anchor.
- Report VERIFIED only with command, cwd, exit status, and evidence. Use UNVERIFIED for an unrun claim and BLOCKED for an external blocker.
- Protocol-order violations fail the worker; malformed advisory lines are logged and skipped; model parsing follows the module's bounded failure policy.
- Use enums for domain alternatives. Cite only `Decision` in `src/cambium/modules/example/decide.py` and `NodeStatus` in `src/cambium/tasktree.py`.

Fail-open warnings are REQUIRED: DLQ writes records unchanged (no redaction); supervisor passes near-full host env after name filtering; no strict spawn allowlist is integrated.
redaction and strict env allowlisting are NOT integrated; never place credentials/sensitive content in task specs, events, gate commands/output, or DLQ records.

`supervisor.run_plan` starts supplied tasks concurrently in one `asyncio.TaskGroup`.
`tasktree.py` validates but does not schedule the DAG. The architecture DAG is target only.

## Module map

Generate the inventory from current tracked files with `git ls-files`; do not copy planned names from architecture.
Current source is under `src/cambium/`; current tests are under `tests/` and `src/cambium/modules/example/tests/`.
CLI: `src/cambium/cli.py:main`; version: `src/cambium/__init__.py`.
Runtime: `src/cambium/ipc.py`, `src/cambium/worker.py`, `src/cambium/supervisor.py`, `src/cambium/tasktree.py`, and `src/cambium/worker_pool.py`.
State and control: `src/cambium/store.py`, `src/cambium/merge.py`, `src/cambium/dlq.py`, `src/cambium/events.py`, `src/cambium/conversations.py`, `src/cambium/approval.py`, and `src/cambium/provider_config.py`.
Tool changes cover `src/cambium/schemas.py`, `src/cambium/tools.py` (`TOOL_DISPATCH`), and `src/cambium/approval.py` together.
Decision module: `src/cambium/modules/example/`; harness scenarios are in `tests/scenarios/`.
Current data is `{train,eval,canaries}.jsonl`; `example_pairs.jsonl` is legacy fallback only.
Fallback references: `src/cambium/modules/example/dataset.py:59-77` and `src/cambium/bench.py:215-255`.
`pyproject.toml` has `dependencies = []` and no `[dspy]` extra yet; `requires-python` is `>=3.14` with no packaging upper bound.
Architecture's `>=3.14,<3.15` claim is open packaging work, not a fact.
The docs tree is `docs/architecture/...` and `docs/research/...`.
`implementation-plan.md` and `docs/research/v2-1-status.md` are stale-baseline snapshots, not current truth.
Coding principles pointer: `docs/research/coding-constitution.md`.

## Commands

Run from `/home/ubuntu/cambium-wt-agents-condense`. Use only real checks:

- `uv run --python 3.14.7 --extra test pytest -q`
- `uv run --python 3.14.7 --extra test pytest --collect-only -q`
- Focused scenarios: `uv run --python 3.14.7 --extra test pytest -q tests/scenarios/test_tasktree.py tests/scenarios/test_supervisor_fanout.py`
- `uv run --python 3.14.7 --extra dev ruff check src tests`
- `python -m compileall src/cambium`
- `cambium --help`
- `cambium version`
- `git diff --check`

A module CLI is allowed only when its `__main__.py` exists. None exists for the example package now.

## Workflow

Before editing, state scope, authority, entry points, baseline, and the check that distinguishes the diagnosis from alternatives.
Search the execution path before concluding that a symbol or command does not exist.
Keep child worktrees disjoint. The root owns orchestration, integration, verification, and cleanup.
Merge only verified child commits; children never commit to the integration branch.
After editing, inspect the diff, run the narrowest real check, then run required boundary checks.
Report facts separately from inferences.
Before committing, verify the worktree path and `git diff --check`. Stage only intended files and leave the tree clean.
Every handoff uses this block:
- Scope:
- Authority and target:
- Entry points read:
- Baseline and reproduction: command, cwd, result
- Files in scope:
- Change and preserved boundary:
- Checks: command, cwd, exit status, evidence
- Status: VERIFIED | UNVERIFIED | BLOCKED
- Next action:

## Forbidden

- Do not claim the architecture public API, DSPy ReAct worker, or architecture DAG scheduler is implemented without source and test proof.
- Do not put credentials or sensitive content in task specs, events, gate commands or output, or DLQ records.
- Do not invent enum types, module paths, or checks for a package with no `__main__.py`.
- Do not add an editorial constitution block or patch-history prose; retain only the one-line constitution pointer above.
- Do not add reports, summaries, transient branch or SHA claims, test counts, or stale Vim swap text to this orientation file.
