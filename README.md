# Cambium

Cambium is a Python-native multi-agent coding-agent harness. A deterministic
supervisor manages isolated worker subprocesses, each running a coding agent
in its own git worktree, speaking JSON-Lines-on-stdio IPC with `request_id`
RPC framing. The current main provides deterministic plan execution, task-tree
validation, durable event storage, and atomic merge sequencing. LLM-driven
decomposition, provider calls, and result evaluation remain roadmap work. Design
philosophy: **LLM plans, deterministic code executes.**

## Status

What exists now:

- **Example decision module** `should_decompose`
  (`src/cambium/modules/example/`) — a deterministic rule engine with a DSPy
  seam, `Decision` enum, exact-match metric, train/eval/canaries splits, and a
  committed bench baseline. The current dataset version is `1.1.0`; the JSON
  wire format remains unchanged.
- **Integrated deterministic runtime** — the multi-worker supervisor plan path,
  SQLite WAL event store, atomic merge sequencer, Nuntius NDJSON framing,
  worker runtime, task-tree validation, Architectus scheduling core, and
  doctor diagnostics.
- **Runtime controls and tooling** — approval gates, generation fencing,
  compile-gate resource budgets, a bounded dead-letter queue, conversation
  storage, Diffundo provider routing, anchored edits, executable tools,
  provider configuration validation, JSON tool schemas, host health probes,
  Ruff diagnostics, AST search, the benchmark plugin, and the unified `cambium`
  CLI. These modules are not all wired into one production runtime.
- **Vertical-slice proof** — a real supervisor subprocess-spawns a real fake
  worker, runs the task gate, and merges the worker branch. The multi-worker
  plan path also has end-to-end scenario coverage.
- **Scenario and module coverage** — the committed module baseline records 307
  timed node IDs (not yet module-scoped; the module-test gate is blocked on 278
  foreign scenario IDs). The verified full run on `b709375` reports **647 passed
  and 4 skipped**; the source Ruff gate is clean.
- **Still unmerged** — `CambiumLM` (the real-provider LM adapter) is branch-local
  in `wt-dspy-cambiumlm`. Real provider execution and DSPy optimization are not
  verified.

Next:

- **M1 — Canonical runtime and audit baseline: in progress.** The canonical
  store, merge, IPC, worker, doctor, and multi-worker paths exist, but the
  supervisor still retains the slice `EventLog`, both fallback classes, and the
  slice entry path, and redaction is not yet wired at enqueue/INSERT.
- **M2 — Protocol and pipe hardening: in progress.** Framing limits, worker
  controls, the durable DLQ, and stdin write deadlines exist; the roadmap's
  FD-3 transport, bounded runtime queues, and production overflow contract are
  not complete.
- **M3 — Security boundary and fencing: in progress.** Fencing, approval gates,
  and the canonical redaction module are merged; strict spawn-time environment
  allowlisting (the `_worker_environment` provider-key leak), durable approval,
  ref validation, and runtime fencing remain open.
- **M4 — Gate/resource hardening and deep budgets: in progress.** Resource
  controls landed; GateRunner extraction, deep turn/process deadlines, and
  bounded store backpressure remain open.
- **M5 — Architectus/task-tree execution and conversations: in progress.** The
  task tree, Architectus execution core, plan runtime, and conversation store
  exist; the core is not wired into the supervisor and the orchestrator is still
  a skeleton.
- **M6 — First real LLM task: in progress.** Diffundo, provider configuration,
  provider-environment coverage, the fake-provider staging test, and the
  M6-hygiene quota-fallback and exact-publish-scope assertions are merged. The
  staging path is verified with a loopback fake provider; `CambiumLM` is
  branch-local, and real-provider execution and M6 acceptance remain
  unverified.
- **M7 — Persistent worker pool: blocked.** Only a pure state-machine seed is
  merged (explicitly not acceptance); its M2–M6 prerequisites are not accepted.
- **M8 — DSPy `should_decompose` refinement: in progress.** The Decision enum
  migration and dataset bump to `1.1.0` are merged; the package rename and
  DSPy SIMBA refinement gates remain open.
- **M9 — Tree-sitter context compression: research done; runtime deferred.**
  The feasibility research is merged, but the M6-dependent paired provider
  trials remain unverified.

M9's research deliverable is complete; no production/runtime v2.1 milestone
meets all acceptance criteria in this snapshot. The full roadmap is in
`docs/research/v2-1-review.md` and the current tracker is
`docs/research/v2-1-status.md`.

## Quickstart

Requires Python 3.14 (regular build) and [uv](https://docs.astral.sh/uv/).

```sh
uv sync --extra test --python 3.14.7
uv run --python 3.14.7 --extra test pytest -q
```

Vertical-slice demo (no LLM, no network — stdlib + git only):

```sh
uv run --python 3.14.7 --extra test python -m cambium.supervisor --session-dir demo
```

The supervisor reads `<session-dir>/task.json` if present (otherwise uses
built-in defaults), bootstraps a scratch git repo, spawns the worker, runs the
gate, and merges. See `docs/research/vertical-slice-report.md` for the wire
protocol and the full manual-run transcript.

The installed command exposes the same entry points plus the plan-oriented
adapters:

```sh
uv run --python 3.14.7 --extra test cambium --help
```

Available subcommands are `auth`, `supervisor`, `doctor`, `bench report|gate`,
`tasktree`, `module-test`, and `version`.

## Documentation

- `docs/architecture/` — canonical design:
  - `architecture.md` — authoritative v2 architecture.
  - `system-design.md` — the v0.1 design draft (superseded; kept as the origin record).
  - `module-template/` — the per-module pattern: `architecture.md`, `dataset-format.md`, `example-spec.md`.
  - `reviews/` — the three adversarial reviews that shaped v2.
- `docs/research/` — 44 research docs plus a tiered README index (historical,
  never pruned). Key reads:
  `python-3.14.md`, `tui-best-practices.md`, `sqlite-wal-durability.md`,
  `worktree-concurrency.md`, `design-deltas.md`.
- `agents.md` — orientation for agents landing in this repo. Read first.

## Design decisions

- Python >= 3.14, regular build (GIL present; free-threaded build optional).
- Headless-first: the public interface is JSON-Lines on stdio; the TUI is an optional view.
- No production local LLM cache — `EvalCache` is opt-in and limited to the
  frozen evaluation harness; provider-side caching remains the production
  policy.
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
│   └── research/              44 research docs + README index
├── scripts/                   dataset tooling + the fake worker
├── src/cambium/               approval.py, architectus.py, ast_tools.py, auth.py,
│                              bench.py, cli.py, conversations.py, diffundo.py,
│                              dlq.py, doctor.py, edits.py, eval_cache.py, events.py,
│                              fencing.py, ipc.py, lint_diag.py, merge.py,
│                              module_conformance.py, orchestrator.py, process_env.py,
│                              provider_config.py, redact.py, resources.py, results.py,
│                              schemas.py, store.py, supervisor.py, system_health.py,
│                              tasktree.py, tools.py, worker.py, worker_pool.py, modules/
│   └── modules/example/       should_decompose decision module, datasets, tests, baseline
├── tests/scenarios/           runtime, tooling, and integration scenarios
└── pyproject.toml
```

## Contributing

Read `agents.md`, then the onboarding checklist at
`docs/research/onboarding-checklist-draft.md` (the module-template onboarding
reference). Work in an isolated git worktree on a `wt-*` branch, commit
frequently, and verify every claim with the narrowest check that catches your
change.
