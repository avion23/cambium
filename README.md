# Cambium

Cambium is a Python-native multi-agent coding-agent harness. A deterministic
supervisor manages isolated worker subprocesses, each running a coding agent
in its own git worktree, speaking JSON-Lines-on-stdio IPC with `request_id`
RPC framing. An LLM-driven orchestrator decomposes tasks, routes work, and
evaluates results; all state is recorded in an event-sourced log. Design
philosophy: **LLM plans, deterministic code executes.**

## Status

What exists now:

- **Scaffold** — the public API skeleton (`Orchestrator`, event log) and the
  `Module` base pattern every decision module follows.
- **Example module** `should_decompose` (`src/cambium/modules/example/`) — a
  rule-engine `decide()` with a DSPy seam, an exact-match `metric()`, and
  dataset v1 (the combined `example_pairs.jsonl` plus the seeded
  `train`/`eval`/`canaries` split) with canary records.
- **Deterministic runtime foundations** — SQLite WAL storage (`store.py`), the
  atomic merge sequencer (`merge.py`), NDJSON framing (`ipc.py`), the worker
  runtime (`worker.py`), task-tree validation (`tasktree.py`), and diagnostics
  (`doctor.py`).
- **Vertical-slice milestone** — supervisor to worker to gate to merge,
  end-to-end: a real supervisor subprocess-spawns a real worker, runs the
  task's gate, and merges the worker branch with `git merge --ff-only`.
- **Scenario coverage** — 108 tests are collected from `tests/scenarios/`.
  Current main reports 108 passed, and the source ruff gate is clean.
- **Not in this main snapshot** — `diffundo.py`, `bench.py`, and `redact.py`
  remain in implementation worktrees and are not current modules in
  `src/cambium/`.

Next:

- **Hardening pass** — clear the current source gate, complete the implementation
  audit actions, and verify the supervisor, store, IPC, worker, task-tree, and
  merge paths together.
- **v2.1** — exercise the DSPy seam and optimization/eval path described in
  `docs/architecture/module-template/example-spec.md`, then land the remaining
  provider, benchmark, redaction, and recovery work. The roadmap is in
  `docs/research/v2-1-review.md`.

## Quickstart

Requires Python 3.14 (regular build) and [uv](https://docs.astral.sh/uv/).

```sh
uv sync --extra test --python 3.14.7
uv run --python 3.14.7 --extra test pytest -q
```

Vertical-slice demo (no LLM, no network — stdlib + git only):

```sh
uv run --python 3.14.7 python -m cambium.supervisor --session-dir /tmp/opencode/slice-run
```

The supervisor reads `<session-dir>/task.json` if present (otherwise uses
built-in defaults), bootstraps a scratch git repo, spawns the worker, runs the
gate, and merges. See `docs/research/vertical-slice-report.md` for the wire
protocol and the full manual-run transcript.

## Documentation

- `docs/architecture/` — canonical design:
  - `architecture.md` — authoritative v2 architecture.
  - `system-design.md` — the v0.1 design draft (superseded; kept as the origin record).
  - `module-template/` — the per-module pattern: `architecture.md`, `dataset-format.md`, `example-spec.md`.
  - `reviews/` — the three adversarial reviews that shaped v2.
- `docs/research/` — 33 evidence docs (historical, never pruned). Key reads:
  `python-3.14.md`, `tui-best-practices.md`, `sqlite-wal-durability.md`,
  `worktree-concurrency.md`, `design-deltas.md`.
- `agents.md` — orientation for agents landing in this repo. Read first.

## Design decisions

- Python >= 3.14, regular build (GIL present; free-threaded build optional).
- Headless-first: the public interface is JSON-Lines on stdio; the TUI is an optional view.
- No local LLM cache — provider-side caching only.
- No sandboxing in the harness: containment via git worktrees + permission allowlists + approval gates.
- Task tree with information hiding: decomposition produces a tree; subtasks see only what they need.
- Per-module DSPy seam, datasets with canaries, frozen metric + held-out eval.
- SQLite WAL event log on a single writer thread.
- Stdlib + git only; DSPy is the one seam.

## Repository layout

```
cambium/
├── agents.md                  orientation for agents
├── docs/
│   ├── architecture/          architecture.md, system-design.md (v0.1), module-template/, reviews/
│   └── research/              33 evidence docs
├── scripts/                   dataset tooling + the fake worker
├── src/cambium/               events.py, orchestrator.py, supervisor.py, doctor.py,
│                              ipc.py, merge.py, store.py, tasktree.py, worker.py, modules/
├── tests/scenarios/           108 example/dataset/runtime scenario tests
└── pyproject.toml
```

## Contributing

Read `agents.md`, then the onboarding checklist at
`docs/research/onboarding-checklist-draft.md` (the module-template onboarding
reference). Work in an isolated git worktree on a `wt-*` branch, commit
frequently, and verify every claim with the narrowest check that catches your
change.
