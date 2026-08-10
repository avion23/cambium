# Cambium

Cambium is a Python-native coding-agent harness. The supervisor starts worker
subprocesses in isolated Git worktrees, speaks JSON Lines over stdio, runs a
gate, and publishes successful worker commits to `refs/heads/main`.

The repository contains a deterministic runtime plus a provider-backed worker
path. The implementation and tests are authoritative; architecture and
research documents label targets separately.

## Current status

- `src/cambium/worker.py` has a deterministic marker mode and a bounded
  provider worker loop. Provider mode calls Diffundo, parses strict tool or
  finish actions, dispatches validated tools through the worker context, emits
  checkpoints, and commits the result.
- `src/cambium/lm.py` contains the merged optional DSPy-compatible `CambiumLM`
  adapter (and `ArchitectusLM`). Integration tests cover the adapter and a
  loopback provider. Real external-provider acceptance is not yet evidenced.
- `cambium.supervisor.run_plan` validates a **flat task list** and supervises
  supplied tasks concurrently with `asyncio.TaskGroup`. It does not schedule a
  DAG.
- `tasktree.build_tree` validates dependency graphs, but the current
  `run_plan` path does not call it for scheduling. `ArchitectusCore` and the
  `worker_pool` state machine are tested standalone and are not connected to
  the supervisor. The orchestrator remains a skeleton.
- The package has no public library API: `src/cambium/__init__.py` exports only
  `__version__`. Use the CLI or the module-level supervisor functions.
- Canonicalization is incomplete. The supervisor still contains the one-task
  `run_session` slice, `EventLog`, and fallback store/sequencer paths; canonical
  store, redaction, and root-result wiring still need one integrated path.

The `modules/example` decision module is deterministic and has train, eval, and
canary data plus a DSPy seam. It is an example module, not the supervisor's
planner.

## Quickstart

Requires Python 3.14 (regular build) and [uv](https://docs.astral.sh/uv/).

```sh
uv sync --extra test --python 3.14.7
uv run --python 3.14.7 --extra test pytest -q
```

Run the deterministic one-worker demo:

```sh
uv run --python 3.14.7 --extra test python -m cambium.supervisor --session-dir demo
```

The installed CLI exposes `auth`, `supervisor`, `doctor`, `bench`, `tasktree`,
`module-test`, and `version`:

```sh
uv run --python 3.14.7 --extra test cambium --help
```

In plan mode, a successful publication advances `refs/heads/main` only. It
does not refresh a checkout; materialize the published ref in a separate
worktree before building or testing it.

## Documentation authority

- [`agents.md`](agents.md) — agent process and current-truth orientation.
- [`docs/architecture/architecture.md`](docs/architecture/architecture.md) —
  current/target architecture split and behavioral invariants.
- `src/cambium/` and `tests/` — implementation and behavior evidence.
- [`docs/research/README.md`](docs/research/README.md) — research index;
  drafts are historical unless an authoritative document cites them.
- [`docs/research/v2-1-status.md`](docs/research/v2-1-status.md) — concise
  capability/gap tracker.
- [`implementation-plan.md`](implementation-plan.md) — ordered implementation
  work, not a merge log.

## Repository layout

```
cambium/
├── agents.md
├── docs/architecture/architecture.md
├── docs/research/
├── src/cambium/        runtime, controls, providers, and modules
├── tests/scenarios/    runtime and integration scenarios
└── pyproject.toml
```

Read `agents.md` before changing code. Work in an isolated `wt-*` branch and
verify claims with the narrowest relevant check.
