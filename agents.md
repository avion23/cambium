# agents.md — Cambium operating contract

Read this file before work. Use source, tests, and recorded commands for
current behavior. Do not infer implementation from a role name or a draft.

## Authority and workflow

Authority order is: task request; this contract; source and tests; architecture
targets; research history. Start at route registration, command tables, and
imports. Trace callers and tests. A failed name search is not proof of absence.
Record scope, entry points, baseline, reproduction, and the check that
distinguishes the diagnosis from alternatives.

- Keep the requested file scope. Report any required expansion before editing.
- Work in an isolated worktree. Verify `git rev-parse --show-toplevel`, branch,
  and `git worktree list`. Children do not merge branches; the root integrates,
  verifies, and cleans up.
- Reproduce before changing code. Remove the cause; do not mask it with a
  fallback, retry, default, or catch-all. Preserve protocol, schema, worktree,
  approval, export, and module boundaries.
- Use adversarial review for consequential changes. Report exact commands,
  working directories, exit statuses, and observed evidence. Use `UNVERIFIED`
  for an unrun check and `BLOCKED` only for an external blocker.
- Do not force-push, rewrite shared history, reset another worktree, or delete
  work to hide a failure. Secrets stay in the environment and never in task
  specs, events, logs, gate output, or commits.

## Current entry points and behavior

- `pyproject.toml` registers `cambium = "cambium.cli:main"`. The CLI exposes
  `auth`, `supervisor`, `doctor`, `bench`, `tasktree`, `module-test`, and
  `version`.
- `supervisor.run_plan` validates a flat list of supplied tasks, starts one
  runtime, and fans tasks out under an `asyncio.TaskGroup`. It creates
  `store.EventStore`, writes `.cambium/result.json`, runs gates, and publishes
  successful merges by an expected-old update of `refs/heads/main`. Publication
  is ref-only; it does not refresh a checkout.
- Each worker is a process group in a Git worktree. Its stdout is NDJSON only;
  diagnostics use stderr/logging. The supervisor bounds each worker's decoded
  stdout queue and routes emitted records through `EventStore`, whose writer
  queue is bounded. Non-critical store records may be dropped by its policy.
- `worker.do_work` selects deterministic marker mode unless `fanout_config` is
  present. Provider mode runs the custom bounded `Diffundo` loop: one provider
  call per turn, strict `tool_call`/`finish` parsing, schema and permission
  checks, tool events, checkpoints, and one fenced commit.
- `Diffundo` sorts eligible providers by configured priority and tracks health
  plus each provider's configured RPM request-rate bucket. A depleted bucket
  reports `RATE_LIMITED`; HTTP 429 `Retry-After` is honored before retrying. It
  has no local response cache. Provider token, cost, and account-quota usage
  remains a production observability contract gap.
- `tasktree.build_tree` validates roots, dependencies, cycles, and bounds, and
  deep-copies each input spec into its node as a snapshot. `topological_order` and
  `ready_tasks` are pure helpers; `run_plan` does not call them to schedule a
  DAG. `ArchitectusCore`, dynamic decomposition, and the conversation store are
  not wired into `run_plan`.
- `doctor` reports Python/Git/uv, worktree, provider environment and auth,
  optional event/conversation stores, dataset integrity, and advisory host
  health. `module_conformance` supplies the isolated `module-test` gate. The
  example module has deterministic `decide` and `evaluate` CLI operations and
  split evaluators in `metric.py`.

### Accepted target boundary

The first production hierarchy slice is harness-owned: it validates one
explicit tree, schedules static ready-node waves, gives each child a fresh
bounded context, and accepts only the strict upward envelope. Dynamic child
admission is a later validated revision step. Prompt-prefix stability and
provider cache-hit metrics are required acceptance evidence. These are targets,
not current `run_plan` behavior.

## Module and hazard map

Generate inventories with `git ls-files`; the current package is under
`src/cambium/` and tests are under `tests/` plus the example module tests.

| Concern | Current source |
| --- | --- |
| CLI and version | `cli.py`, `__init__.py`, `pyproject.toml` |
| Supervisor, worker, IPC | `supervisor.py`, `worker.py`, `ipc.py` |
| Plan tree and orchestration target | `tasktree.py`, `architectus.py`, `orchestrator.py` |
| Store, merge, fencing, results | `store.py`, `merge.py`, `fencing.py`, `results.py` |
| Providers and adapters | `diffundo.py`, `provider_config.py`, `lm.py` |
| Tools and controls | `tools.py`, `schemas.py`, `approval.py`, `resources.py`, `process_env.py`, `redact.py` |
| Diagnostics and modules | `doctor.py`, `module_conformance.py`, `bench.py`, `modules/example/` |

Do not cite `worker_pool.py`, `events.py`, `dlq.py`, `eval_cache.py`, or
`ResourceBudget` as current code: none is tracked at this revision. There is
no per-worker OS sandbox and no production approval gate in the `run_plan`
worker context. `approval.py:ApprovalGate` is a reusable command-policy primitive;
`fail_open` is an explicit dangerous option. Worktree/process-group isolation
is not OS containment.

## Boundary invariants

- IPC framing is bounded and request/generation-correlated. Malformed advisory
  lines are logged or skipped; fatal framing, missing correlated results,
  non-zero exits, and deadline failures fail or restart the task according to
  the boundary policy.
- A failed gate, merge conflict, non-fast-forward, quarantine violation, or
  stale expected-old ref never publishes `main`.
- Provider keys are allowlisted environment values. Never put credentials or
  sensitive content in task specs, event payloads, gate commands/output, or
  durable artifacts.
- Use enums for domain alternatives (`Decision`, `NodeStatus`). Keep blocking
  disk and subprocess I/O at existing thread/process boundaries.

## Checks and handoff

Run the narrowest real check, then the affected package or integration check
when a boundary changes. Useful system commands from the repository root:

```sh
python3.14 -m pytest -q tests/scenarios/test_supervisor_fanout.py
python3.14 -m compileall src tests
PYTHONPATH=src python3.14 -m cambium.cli --help
git diff --check
```

Before commit, inspect the diff, stage only intended files, and verify a clean
worktree. Every handoff states:

- Scope and files in scope.
- Authority and entry points read.
- Baseline/reproduction command, cwd, and result.
- Change and preserved boundary.
- Checks with command, cwd, exit status, and evidence.
- Status: `VERIFIED`, `UNVERIFIED`, or `BLOCKED`.
- Next action.
