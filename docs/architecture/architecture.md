# Cambium architecture

**Status:** current-versus-target contract. Source and tests establish current
behavior. This document names targets but does not turn them into features.
See [`agents.md`](../../agents.md) for the operating contract and
[`docs/research/v2-1-status.md`](../research/v2-1-status.md) for the detailed
live gap table.

## 1. Current runtime

`pyproject.toml` installs one `cambium` script at `cambium.cli:main`. The CLI
routes `auth`, `supervisor`, `doctor`, `bench`, `tasktree`, `module-test`, and
`version`. `cambium.__init__` exports only `__version__`; there is no public
session API.

### Plan and publication

`cambium.supervisor.run_plan` accepts a mapping with `tasks` or a task list. It
validates supplied task records, rejects duplicate IDs and unsafe worktree
paths, then supervises the supplied tasks concurrently in one
`asyncio.TaskGroup`. A task worker runs in a Git worktree and process group; a
gate runs before merge. Successful publication uses an expected-old atomic
update of `refs/heads/main`. It is ref-only and never refreshes the caller's
checkout or index.

The plan runtime creates `store.EventStore` at `.cambium/events.db`, emits
records through it, and writes `.cambium/result.json` after shutdown. The
one-task `run_session` adapter remains for compatibility. It is not a DAG
scheduler. `EventStore` is the current event boundary; there is no current
`events.py` or dead-letter queue module.

The supervisor gives each worker a bounded decoded-stdout `asyncio.Queue` and
routes worker and runtime records through `EventStore`'s bounded writer queue.
Worker stdout remains protocol-only NDJSON; diagnostics use stderr/logging.
Non-critical store records can be dropped under the store overflow policy.

### Worker and providers

`worker.do_work` selects one of two explicit modes:

1. Without `fanout_config`, the deterministic marker worker edits and fences
   one commit.
2. With `fanout_config`, the worker loads the configured providers and runs a
   custom bounded loop. Each turn calls `Diffundo`, requires exactly one strict
   `tool_call` or `finish` action, validates permissions and tool arguments,
   dispatches the tool, emits a `tool_event` and checkpoint, and then creates
   one fenced result commit.

The loop bounds turns, tokens, wall time, transcript size, and summaries. It
returns cumulative provider usage and latency as redacted metadata. `lm.py`
contains optional DSPy-compatible `CambiumLM` and `ArchitectusLM` adapters;
they are not a supervisor planner.

`Diffundo` is a tiered provider router with health, token-bucket quota,
cooldown, circuit-breaker, and configured-priority ordering. It has no local
response cache. HTTP 429 responses carry a parsed `Retry-After` delay into the
same-provider retry path. Weighted routing and a production usage/quota
observability contract are not implemented.

### Trees, diagnostics, and modules

`tasktree.build_tree` validates one rooted dependency tree, cycle and
depth/width bounds, and deep-copies each input `spec` into frozen node records.
`topological_order` and `ready_tasks` are pure inspection/scheduling inputs.
`run_plan` currently bypasses them and fans out the supplied flat list.

`architectus.ArchitectusCore` is tested with injected LLMs but has no caller in
`run_plan`. Dynamic decomposition and the conversation store are not wired into
that path; `orchestrator.py` is a skeleton. Persistent worker reuse is absent.

`doctor` checks Python/Git and `uv`, worktree hygiene, provider environment and
auth coverage, optional event and conversation databases, module datasets, and
advisory host health. `resources.CompileGate` limits configured heavy gate
commands. There is no `ResourceBudget` class. `module_conformance` provides an
isolated module-test gate. `modules/example` has deterministic decision logic,
train/eval/canary data, split metrics, and a JSON CLI with `decide` and
`evaluate` operations. There is no `eval_cache.py`.

The tracked source does not contain `worker_pool.py`, `events.py`, or `dlq.py`.
Do not use those names as current architecture components.

## 2. Ownership and invariants

1. The caller owns the session directory and supplies plan records.
2. The supervisor owns validation, worker handles, generations, event
   admission, gates, restart decisions, and publication order.
3. A worker owns its worktree edits, provider calls, tool context, and commit;
   it cannot publish `main` directly.
4. The merge sequencer owns staging, expected-old checks, quarantine, and
   cleanup. A conflict, non-fast-forward, failed gate, or cleanup violation
   does not advance `main`.
5. The event store owns durable rows and its writer thread. Observer copies
   cannot mutate persisted records.

IPC is bounded and correlated by request ID and worker generation. Fatal
   framing, oversized lines, missing correlated results, non-zero exits, and
   deadline failures follow the boundary-specific supervisor policy; advisory
   malformed lines are logged or skipped. Tool schemas reject malformed calls.

Provider credentials are allowlisted environment values. They must not enter
task specs, prompts persisted as events, gate commands/output, logs, or result
artifacts. Worktree and process-group isolation is not an OS sandbox. An
`ApprovalGate` primitive exists in `tools.py`, but the `run_plan` worker
context does not provide a production approval service; `fail_open` is an
explicit dangerous policy option.

## 3. Target contracts and delivery order

These are open contracts, not current interfaces:

### Production hierarchy and admission

Integrate a validated `TaskTree` with the supervisor. A production
hierarchy must admit only ready nodes, enforce dependency and width limits, and
carry envelope-only child results. Dynamic decomposition may propose revisions,
but the supervisor must validate and durably admit each revision; a provider
response must not mutate the live tree in place. Wire the Architectus decision
port and conversation persistence only with callers and failure tests.

### Per-worker containment and approval

Add an explicit host boundary for per-worker OS containment, resource limits,
and process cleanup. Compose it with a production approval callback/policy at
the tool boundary. Prove denied commands, unavailable approval, containment
failure, and teardown behavior. A systemd or cgroup smoke wrapper is evidence
for that wrapper, not proof that every production worker is contained.

### Provider accounting before routing policy

Define durable usage events, provider and model identity, token/cost fields,
quota ownership, and privacy/redaction rules. Test Retry-After, quota
exhaustion, and accounting failure first. Only then evaluate weighted routing;
priority ordering remains the current policy.

### External-provider acceptance

Run a disposable, credentialed provider smoke through worker loop, tool event,
checkpoint, gate, and ref-only merge. Credentials stay in the environment and
the run is never a default network test. External credentials are not present
in this checkout, so external-provider acceptance remains open.

A root-confirmed loopback smoke has exercised the disposable path with a
bounded host scope, two provider requests, one commit, a passing gate, and a
ref publication. It is not external-provider acceptance or proof of
per-worker OS isolation.

## 4. Failure policy by boundary

| Boundary | Current check | Required outcome |
| --- | --- | --- |
| Plan | `run_plan` rejects malformed tasks, duplicate IDs, and unsafe paths before worker setup. | No worker side effect before structural validation. |
| Task tree | `build_tree` rejects missing dependencies, multiple roots/parents, cycles, and bounds. | A future scheduler dispatches only a validated graph with snapshotted specs. |
| IPC | Framing limits, request IDs, generations, heartbeat deadlines, and correlated result checks are enforced in `_Runtime._drive_generation`. | Stale or missing worker messages cannot complete a task. |
| Worker | Provider/tool failures, missing results, non-zero exits, and wall/token limits fail the generation; recoverable failures may restart it. | A worker verdict is accepted only for its active generation. |
| Gate | Non-zero exit, timeout, output overflow, or resource-acquire failure fails before merge. | A failed gate never reaches publication. |
| Merge | Conflict, non-fast-forward, unsafe quarantine, or cleanup failure stops publication. | `main` advances only through the expected-old ref contract. |
| Store | Critical event admission waits for the writer; writer death raises; non-critical overflow follows the bounded queue policy. | Durable failure is visible; no silent success after store failure. |

The table describes checks on paths that call these modules. A helper's
existence is not proof of integration: approval, redaction, resource admission,
hierarchy, and containment remain targets where the plan path has no caller.

## 5. Source map

| Concern | Current source | State |
| --- | --- | --- |
| CLI/version | `pyproject.toml`, `src/cambium/cli.py`, `__init__.py` | Installed CLI; version-only package export |
| Plan runtime | `src/cambium/supervisor.py` | Flat concurrent `run_plan`; one-task adapter retained |
| Worker/IPC | `src/cambium/worker.py`, `ipc.py` | Marker mode, custom provider loop, bounded NDJSON |
| Provider/LM | `diffundo.py`, `provider_config.py`, `lm.py` | Priority router and optional adapters; external proof open |
| Tree/planner | `tasktree.py`, `architectus.py`, `orchestrator.py` | Pure tree/core; no run-plan hierarchy wiring |
| Store/merge | `store.py`, `merge.py`, `results.py`, `fencing.py` | Current event, result, and ref-publication boundaries |
| Controls | `tools.py`, `schemas.py`, `approval.py`, `resources.py`, `redact.py` | Primitives; production approval/containment gaps |
| Diagnostics/evaluation | `doctor.py`, `module_conformance.py`, `bench.py`, `modules/example/` | CLI diagnostics and example evaluation exist |

Any target moves to current only after a caller and focused failure test
demonstrate it. Keep public names and status mappings stable once a host API is
introduced; a worker envelope is not a substitute for a typed root result.
