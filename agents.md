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
`worker.do_work` is a deterministic marker/commit seed in default mode and invokes the Diffundo provider router in provider-fanout mode; no DSPy ReAct loop is present.
The example module has a split evaluation harness (`modules/example/metric.py`; `__main__.py` `evaluate` operation) and a module CLI entry point (`python -m cambium.modules.example`).
Custos, Opifex, Nuntius, Surculus, and Unio are architecture target names mapped to current supervisor, worker, ipc, worktree, and merge portions and symbols.
Matching role modules are not proof. No TUI exists.

## Invariants

- Keep scope tight. Report required file-scope expansion before editing.
- Work in an isolated worktree. Verify `git rev-parse --show-toplevel` and `git worktree list`; never work or commit on `main` or in a shared integration checkout.
- Plan-mode `run_plan` publication is ref-only: it advances `refs/heads/main` and never refreshes a checkout, so the runtime primary checkout is not a consumer workspace.
- Children never merge other branches into their own worktree. Committing to the integration branch and merging child branches are forbidden for children; all merges are root-owned, verified, and ordered.
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
- Protocol handling is boundary-specific; malformed advisory lines are logged/skipped, while fatal cases are listed under IPC below; model parsing follows the module's bounded failure policy.
- Use enums for domain alternatives. Cite enums verified in current source.
- Regular GIL build is the target; synchronization remains required.
- `asyncio.Queue` is loop-local; cross-thread handoff uses thread-safe primitives; external ingress is bounded.
- Finite subprocess waits are deadline-bound; long-lived workers use lifecycle budgets.

Current hazards: DLQ writes records unchanged when `cambium.redact` is absent; the supervisor uses a fail-closed environment allowlist; worker message queues and the supervisor event queue are unbounded `asyncio.Queue`s (`supervisor.py`). Never place credentials/sensitive content in task specs, events, gate commands/output, or DLQ records.

`supervisor.run_plan` concurrently fans out supplied tasks under one `asyncio.TaskGroup`; `tasktree.py` validates but does not schedule. The architecture DAG is target only.
Boundary failure policy:
- `PLAN` (`tasktree.build_tree`): task-tree validation rejects malformed, duplicate, or cyclic plans.
- `IPC` (`_Runtime._drive_generation`; framing `ipc.read_message`, worker `worker.run`): handling is per-boundary, not universally fail/restart; malformed frames, stale pongs, and oversized lines are fatal at their protocol checks, while duplicate task IDs are rejected by `tasktree.build_tree`. A wrong-request-id `ready` is fatal: the worker is killed before task dispatch. Missing correlated results, nonzero exits, and timeouts fail/restart workers; malformed advisory lines are logged/skipped.
- `GATE` (`_Runtime._run_gate`): a nonzero exit or timeout fails before merge.
- `MERGE` (`_Runtime._merge_task`): a conflict or non-fast-forward emits `merge_failed`; nothing is published.
- `APPROVAL` (`ApprovalGate.is_approved` in `src/cambium/approval.py`): approval is fail-closed by default; `fail_open` configuration permits execution without a reviewer — verify configuration. With `fail_open=True`, approval returns true without a callback for a command requiring approval.
- `SCHEMA` (`validate_tool_call` in `src/cambium/schemas.py`): malformed tool calls return validation errors.

## Module map

Generate the inventory from current tracked files with `git ls-files`; do not copy planned names from architecture.
Current source is under `src/cambium/`; current tests are under `tests/` and `src/cambium/modules/example/tests/`.
CLI: `src/cambium/cli.py:main`; version: `src/cambium/__init__.py`.
Runtime: `src/cambium/ipc.py`, `src/cambium/worker.py`, `src/cambium/supervisor.py`, `src/cambium/tasktree.py`, and `src/cambium/worker_pool.py`.
State and control: `src/cambium/store.py`, `src/cambium/merge.py`, `src/cambium/dlq.py`, `src/cambium/events.py`, `src/cambium/conversations.py`, `src/cambium/approval.py`, and `src/cambium/provider_config.py`.
Tools: `src/cambium/schemas.py`, `src/cambium/tools.py` (`TOOL_DISPATCH`), and `src/cambium/approval.py`; keep the map complete across all three.
Decision module: `src/cambium/modules/example/`; harness scenarios are in `tests/scenarios/`.
Current data is the split `{train,eval,canaries}.jsonl`; combined `example_pairs.jsonl` is legacy fallback only.
Fallback references: `src/cambium/modules/example/dataset.py:59-77` and the `bench.py` fallback path.
`pyproject.toml` requires `pytest>=9` and ships a `[dspy]` extra; `requires-python` is `>=3.14` with no packaging upper bound.
Architecture's `>=3.14,<3.15` claim is open packaging work, not a fact.
The docs tree is `docs/architecture/...` and `docs/research/...`.
Milestone status lives in `docs/research/v2-1-status.md`; re-check it against `main` before relying on it.
Coding principles pointer: `docs/research/coding-constitution.md` (historical, non-normative).

## Commands

Run from the repository root. The IPC fuzz test is load-sensitive; if it fails, check machine load before treating it as a regression. Use only real checks:

| Check | Command |
|---|---|
| Full suite | `uv run --python 3.14.7 --extra test pytest -q` |
| Collect tests | `uv run --python 3.14.7 --extra test pytest --collect-only -q` |
| Focused scenario | `uv run --python 3.14.7 --extra test pytest -q tests/scenarios/test_supervisor_fanout.py` |
| Lint | `uv run --python 3.14.7 --extra dev ruff check src tests` |
| Syntax | `uv run --python 3.14.7 python -m compileall src tests` |
| CLI help | `uv run --python 3.14.7 cambium --help` |
| CLI version | `uv run --python 3.14.7 cambium version` |
| Patch check | `git diff --check` |

A module CLI is allowed only when its `__main__.py` exists; the example package has one (`python -m cambium.modules.example`).

## Workflow

Before editing, state scope, authority, entry points, baseline, and the check that distinguishes the diagnosis from alternatives.
Search the execution path before concluding that a symbol or command does not exist.
Keep child worktrees disjoint. The root owns orchestration, integration, verification, and cleanup.
The root merges only verified child commits, in order; children do not merge branches or commit to the integration branch.
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
