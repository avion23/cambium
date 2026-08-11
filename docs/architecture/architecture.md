# Cambium architecture

**Status:** current-versus-target contract. Source and tests establish current
behavior. This document names targets but does not turn them into features.
See [`agents.md`](../../agents.md) for the operating contract and
[`docs/research/v2-1-status.md`](../research/v2-1-status.md) for the detailed
live gap table.

## 1. Current runtime

Cambium runs directly from source; no wheel is built and no install is
required or supported. The CLI routes `auth`, `supervisor`, `doctor`, `bench`,
`tasktree`, `module-test`, `version`, `run`, `repl`, `tui`, and `session`
(`session list/latest/show` reads completed session results).
`cambium.__init__` exports only `__version__`; there is no public session API.

### Plan and publication

`cambium.supervisor.run_plan` accepts a mapping with `tasks` or a task list. It
validates supplied task records, rejects duplicate IDs and unsafe worktree
paths, then supervises the supplied tasks concurrently in one
`asyncio.TaskGroup`. A task worker runs in a Git worktree and process group. A
clean worker whose envelope reports `succeeded` publishes; there is no
task-command pre-merge gate. A succeeded envelope proceeds to merge only after
the repository-integrity checks pass (worker success integrity, fencing,
expected-old ref publication, session admission, worktree confinement,
protocol/request correlation, and quarantine). Successful publication uses an
expected-old atomic update of `refs/heads/main`. It is ref-only and never
refreshes the caller's checkout or index.

There is no global worker-count semaphore. A flat plan (no `depends_on`)
fans out under one `asyncio.TaskGroup` with no concurrency cap — an 11-task
canary observed 11 concurrent supervisions. A plan that supplies
`depends_on` is built into one validated rooted `TaskTree` and dispatched in
static ready-node waves: only nodes whose dependencies finished are admitted
per wave, and each wave's concurrency is bounded by `max_width` (parameter,
then the plan field, then `tasktree.MAX_WIDTH`). A failed node cascades so
its descendants are never spawned. `resource_thresholds` remains the only
host-health pre-flight.

The plan runtime creates `store.EventStore` at `.cambium/events.db`, emits
records through it, and writes `.cambium/result.json` after shutdown. The
one-task `run_session` adapter remains for compatibility. `EventStore` is
the current event boundary; there is no current `events.py` or dead-letter
queue module.

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
   JSON action (`plan`, `tool_call`, or `finish`), validates permissions and
   tool arguments, dispatches the tool, emits a `tool_event` and checkpoint,
   and then creates one fenced result commit. The agent is instructed to emit a
   short `plan` action before any `tool_call`, and the plan is retained in the
   transcript. The transcript is summarized without an LLM call (dropping old
   turns plus a synthetic dropped-message marker, keeping the plan) whenever it
   exceeds a character budget, so it stays bounded within `max_turns`. A
   `read_batch` tool reads related files in one bounded call, and lint feedback
   from `write_file` reaches the agent as a tool observation.

The loop bounds turns, tokens, wall time, transcript size, and summaries. It
returns cumulative provider usage and latency as redacted metadata. `lm.py`
contains optional DSPy-compatible `CambiumLM` and `ArchitectusLM` adapters;
they are not a supervisor planner.

`Diffundo` is a tiered provider router with health, configured RPM request-rate
buckets, cooldown, circuit-breaker, and configured-priority ordering. A
depleted bucket reports `RATE_LIMITED`. It has no local response cache. HTTP 429
responses carry a parsed `Retry-After` delay into the same-provider retry path.
Weighted routing and a production provider token, cost, and account-quota
observability contract are not implemented.

The escaped-secret bench canary was deleted by product decision; it is no
longer a live blocker.

### Trees, diagnostics, and modules

`tasktree.build_tree` validates one rooted dependency tree, cycle and
depth/width bounds, and deep-copies each input `spec` into frozen node records.
`topological_order` and `ready_tasks` are pure inspection/scheduling inputs.
`run_plan` integrates them on the hierarchy path: a plan whose tasks carry
`depends_on` is built into a `TaskTree` and dispatched in static ready-node
waves; a flat plan keeps the unbounded one-`TaskGroup` fan-out.

`architectus.ArchitectusCore` is tested with injected LLMs but has no caller in
`run_plan`. Dynamic decomposition and the conversation store are not wired into
that path; `orchestrator.py` is a skeleton. Persistent worker reuse is absent.

`doctor` checks Python/Git and `uv`, worktree hygiene, provider environment and
auth coverage, optional event and conversation databases, module datasets, and
advisory host health. `resources.py` is deleted; there is no `CompileGate` and
no `ResourceBudget` class. `module_conformance` provides an
isolated module-test gate. `modules/example` has deterministic decision logic,
train/eval/canary data, split metrics, and a JSON CLI with `decide` and
`evaluate` operations. There is no `eval_cache.py`.

The tracked source does not contain `worker_pool.py`, `events.py`, or `dlq.py`.
Do not use those names as current architecture components.

## 2. Ownership and invariants

1. The caller owns the session directory and supplies plan records.
2. The supervisor owns validation, worker handles, generations, event
   admission, restart decisions, and publication order.
3. A worker owns its worktree edits, provider calls, tool context, and commit;
   it cannot publish `main` directly.
4. The merge sequencer owns staging, expected-old checks, quarantine, and
   cleanup. A conflict, non-fast-forward, or cleanup violation does not
   advance `main`.
5. The event store owns durable rows and its writer thread. Observer copies
   cannot mutate persisted records.

IPC is bounded and correlated by request ID (generation is not enforced for
   message correlation). Fatal framing, oversized lines, missing correlated
   results, non-zero exits, and deadline failures follow the boundary-specific
   supervisor policy; malformed lines that fail JSON parsing are counted and
   skipped up to a bound, and a valid JSON line that is not an object currently
   fails supervision (open defect). Tool schemas reject malformed calls.

Provider credentials are allowlisted environment values. They must not enter
task specs, prompts persisted as events, logs, or result
artifacts. Worktree and process-group isolation is not an OS sandbox.
`approval.py` and `resources.py` are deleted; `tools.py`
`run_shell`/`git_op` execute without `ApprovalGate` or `CompileGate`.

Live-use blockers were removed by product decision; this is a local development
tool run directly from source.

## 3. Target contracts and delivery order

These are open contracts, not current interfaces. Hierarchy and dynamic
admission are follow-on work; gates/containment are not prerequisites.

### Production hierarchy and admission

The smallest production slice is harness-owned: it receives one explicit,
validated `TaskTree`, computes static ready-node waves, and admits only nodes
whose dependencies and width limits are satisfied. Each child receives a fresh
bounded context derived from its task and allowed parent envelope. Upward flow
uses the strict envelope key set; sibling context and unbounded transcripts do
not cross the boundary.

Dynamic child admission follows the static slice. A parent may propose a typed
tree revision, but the supervisor must validate and durably admit it before
dispatch; a provider response cannot mutate the live tree in place. Wire the
Architectus decision port and conversation persistence only with callers and
failure tests. Prompt-prefix stability and provider cache-hit metrics are
required acceptance measures for the provider path.

### Per-worker containment and approval

Per-worker OS containment and production approval were removed by product
decision; worktree/process-group isolation is the only worker boundary.

### Provider accounting before routing policy

Define durable usage events, provider and model identity, token/cost fields,
request-rate status, account-quota ownership, and privacy/redaction rules. Test
Retry-After, `RATE_LIMITED`, token/cost accounting, and accounting failure
first. Measure prompt-prefix stability and provider-reported cache-hit metrics
on fixed prompt fixtures. Only then evaluate weighted routing; priority
ordering remains the current policy.

### External-provider acceptance

Run a disposable credentialed smoke through the worker loop, tool event,
checkpoint, and ref-only merge. Keep credentials in the environment and
network opt-in. Deployment credentials/configuration are external and
ephemeral; doctor currently reports no runnable configured provider.

## 4. Failure policy by boundary

| Boundary | Current check | Required outcome |
| --- | --- | --- |
| Plan | `run_plan` rejects malformed tasks, duplicate IDs, and unsafe paths before worker setup. | No worker side effect before structural validation. |
| Task tree | `build_tree` rejects missing dependencies, multiple roots/parents, cycles, and bounds. | A future scheduler dispatches only a validated graph with snapshotted specs. |
| IPC | Framing limits, request IDs, heartbeat deadlines, and request-correlated result checks are enforced in `_Runtime._drive_generation`. | Stale or missing worker messages cannot complete a task. |
| Worker | Provider/tool failures, missing results, non-zero exits, and wall/token limits fail the generation; recoverable failures may restart it. | A worker verdict is accepted only for its active generation. |
| Merge | Conflict, non-fast-forward, unsafe quarantine, or cleanup failure stops publication. | `main` advances only through the expected-old ref contract. |
| Store | Critical event admission waits for the writer; writer death raises; non-critical overflow follows the bounded queue policy. | Durable failure is visible; no silent success after store failure. |

The table describes checks on paths that call these modules. A helper's
existence is not proof of integration: Redaction, resource admission, and
hierarchy remain targets; approval and containment were removed by decision.

## 5. Source map

| Concern | Current source | State |
| --- | --- | --- |
| CLI/version | `pyproject.toml`, `src/cambium/cli.py`, `__init__.py` | Direct-source CLI; version-only package export |
| Plan runtime | `src/cambium/supervisor.py` | Flat concurrent `run_plan` for plans without `depends_on`; static ready-node waves with width-bounded admission for plans with `depends_on`; one-task adapter retained |
| Worker/IPC | `src/cambium/worker.py`, `ipc.py` | Marker mode, custom provider loop, bounded NDJSON |
| Provider/LM | `diffundo.py`, `provider_config.py`, `lm.py` | Priority router and optional adapters; external proof open |
| Tree/planner | `tasktree.py`, `architectus.py`, `orchestrator.py` | Pure tree/core; `build_tree`/`ready_tasks`/`topological_order` wired into `run_plan` for static waves; dynamic admission remains follow-on |
| Store/merge | `store.py`, `merge.py`, `results.py`, `fencing.py` | Current event, result, and ref-publication boundaries |
| Controls | `tools.py`, `schemas.py`, `redact.py` | `run_shell`/`git_op` run without `ApprovalGate`/`CompileGate`; `approval.py` and `resources.py` are deleted |
| Diagnostics/evaluation | `doctor.py`, `module_conformance.py`, `bench.py`, `modules/example/` | CLI diagnostics and example evaluation exist |

Any target moves to current only after a caller and focused failure test
demonstrate it. Keep public names and status mappings stable once a host API is
introduced; a worker envelope is not a substitute for a typed root result.
