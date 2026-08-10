# Cambium agent orientation

This is a short map. The design and status documents remain authoritative.

## Current reality vs design target

- CURRENT on `main`: the worker is deterministic; it performs the current task, file edit, commit, and JSON-Lines path. The checked-in supervisor is Python runtime code, not an LLM.
- CURRENT on `main`: DSPy, ReAct, CambiumLM, and provider-backed worker execution are in-flight or planned, not active. No TUI implementation exists.
- DESIGN target: a headless JSON-Lines harness with DSPy/ReAct workers, an LLM orchestrator, and an optional TUI view. Do not claim a target is implemented without source and status evidence on `main`.

## Documents and status

- Authoritative behavior and interfaces: `docs/architecture/architecture.md`.
- Plan, milestone status, and research index: `implementation-plan.md`, `docs/research/v2-1-status.md`, `docs/research/README.md`.
- Historical only: `docs/architecture/system-design.md` is explicitly superseded by `docs/architecture/architecture.md`.
- An absent referenced path is planned. Do not invent it; check status first.

## Search before editing

Trace from entry points, imports, dispatch, and callers, not filenames. Start at:

- Package/CLI: `src/cambium/__init__.py`, `src/cambium/cli.py`.
- Runtime: `src/cambium/supervisor.py`, `src/cambium/worker.py`, `src/cambium/ipc.py`.
- DAG/orchestration: `src/cambium/tasktree.py`, `src/cambium/orchestrator.py`, `src/cambium/architectus.py`.
- Module contract/example: `src/cambium/modules/base.py`, `src/cambium/modules/example/`.

Discover files and entry points with:

```sh
git ls-files 'src/cambium/**/*.py' 'tests/**/*.py'
git grep -n -E 'def main|if __name__ == "__main__"' -- src tests
git grep -n -E 'import cambium|from cambium' -- src tests
```

`src/cambium/events.py` and the compatibility `orchestrator.py` submit/drain skeleton are M1 deletion work. Track this in the plan/status docs, not in a permanent module inventory or branch snapshot.

## Workflow

- Use an isolated worktree and branch for every non-trivial change; root owns integration. Parallel work uses disjoint file scopes.
- Same-file parallel work needs isolated worktrees and explicit merge order. Before commit, verify `git rev-parse --show-toplevel` and `git worktree list`.
- Never commit `main`. Commit frequently. No force-push, shared rebase, or reset/removal of another agent's work.
- Require adversarial review before merge. Report exact command, cwd, and exit status; an empty report is a failure.
- After three distinct failed hypotheses, stop and report all three. Never log secrets, credentials, prompts, or chain-of-thought. Normal tests use no network.

## Verification

Run from the repository root. Use these verified project commands, not bare `python` or guessed smoke/eval entry points:

```sh
uv run --python 3.14.7 --extra test pytest -q
uv run --python 3.14.7 --extra test pytest -q tests/scenarios/test_cli.py
uv run --python 3.14.7 --extra test pytest --collect-only -q tests/scenarios/test_cli.py
uv run --python 3.14.7 --extra dev ruff check src tests
uv run --python 3.14.7 python -m compileall src tests
uv run --python 3.14.7 cambium --help
uv run --python 3.14.7 cambium version
```

For a module JSON CLI, first run `git ls-files 'src/cambium/modules/**/__main__.py'`; only then use `uv run --python 3.14.7 python -m cambium.modules.<name>`. No module entry point is on current `main`; do not guess an eval module. Mark checks `VERIFIED`, `UNVERIFIED`, or `BLOCKED`, with the reason.

## Design-target invariants

- Headless JSON-Lines is the interface. Decomposition is a task tree/DAG; parents see no child scratchpad/reasoning, only the exact envelope: `parent_task_id`, `unified_diff`, `diff_truncated`, `summary`, `metric_score`, `metric_breakdown`, `commits`, `files_changed`, `status`.
- The supervisor owns concurrency, restarts, gates, and serialized merges. A worker executes task work and never manages the DAG.
- Production has no local LLM response cache and no in-harness sandbox.
- `events.db`, `conversations.db`, and `shared.db` are separate SQLite stores, each with one writer; `events.db` is the source of truth for event replay.
- IPC and events contain no secrets. Provider configuration stores environment variable names only, never key values.
- Current transport is JSON-Lines over stdio. FD 3 is pending M2; do not claim FD 3, the TUI, DSPy, ReAct, or CambiumLM is already implemented.

## Coding constitution

- Prefer flat control flow, guard clauses, exhaustive enums, and concrete code.
- Keep business logic pure and state/I-O at the edges; use frozen `slots=True` dataclasses for value records.
- Use enums for domain alternatives; booleans are predicates/API compatibility. No hidden mutable globals or singletons.
- Use stdlib-first libraries, Protocols, and plain functions. Measure before optimizing. Delete over add.
- Use list-form subprocess calls; never pass user input through `shell=True`. Worker stdout is protocol only; use logging or stderr for diagnostics.
- Offload event-loop disk I/O with `asyncio.to_thread` or a writer thread. Keep module tests beside modules and harness scenarios under `tests/scenarios/`.

## Where to look

| Need | Start here |
|---|---|
| Architecture and behavior | `docs/architecture/architecture.md` |
| Plan, status, research index | `implementation-plan.md`, `docs/research/v2-1-status.md`, `docs/research/README.md` |
| Package and CLI | `src/cambium/__init__.py`, `src/cambium/cli.py` |
| Supervisor and merge | `src/cambium/supervisor.py` |
| Worker and IPC | `src/cambium/worker.py`, `src/cambium/ipc.py` |
| DAG and orchestration | `src/cambium/tasktree.py`, `src/cambium/orchestrator.py`, `src/cambium/architectus.py` |
| Durable state | `src/cambium/store.py`, `src/cambium/conversations.py` |
| Module contract and dataset | `docs/architecture/module-template/architecture.md`, `src/cambium/modules/example/` |
| Providers and evidence/tests | `src/cambium/provider_config.py`, `tests/scenarios/`, `docs/architecture/reviews/` |

## Ask or act

- Act when a search, test, or document answers the question; record the assumption and continue.
- Ask only for an unresolved equal-priority conflict, an irreversible public/API/IPC choice, or evidence that cannot decide.
- Ask before leaving the assigned file scope.
- After three distinct failed hypotheses, stop and report; do not ask for a search or test that can answer the question.
