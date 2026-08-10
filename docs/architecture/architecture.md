# Cambium architecture

**Status:** design and implementation map. This file separates the runtime that
exists from contracts that are still targets. It does not declare a release
readiness state.

`agents.md`, the source under `src/cambium/`, and tests under `tests/` establish
current behavior. This document describes target boundaries and invariants;
research drafts are context, not implementation proof.

## 1. Current runtime

Cambium is a Python 3.14 coding-agent harness. Its checked-in package exports
only `__version__`; there is no public `Cambium`, `Session`, `Result`, or
`Instance` library API.

### Plan execution

`cambium.supervisor.run_plan` is the current plan entry point. It:

1. accepts a mapping with `tasks` or a task list;
2. rejects malformed task records, duplicate IDs, and unsafe worktree paths;
3. starts one supervisor runtime and supervises each supplied task in an
   `asyncio.TaskGroup`;
4. runs each worker's gate and publishes successful merges by advancing
   `refs/heads/main` with an expected-old check; publication does not refresh a
   checkout; and
5. returns a `PlanResult` and stores events in `.cambium/events.db` when the
   canonical store is available.

`run_session` is an older one-task slice that remains in the supervisor. The
supervisor also retains `EventLog`, `_FallbackEventStore`, and
`_FallbackSequencer` paths. These are compatibility paths, not a second
architecture. Canonicalization is incomplete until the slice and fallbacks are
removed and the canonical store/redaction/result paths are wired together.

The plan path has one session-admission guard. It opens the event store, starts
the runtime, reconciles the supplied task specifications, and waits for the
task group to finish. Cancellation shuts the runtime down with a cancelled
session status. A task result records status, exit code, gate result, merge
SHA (when published), restart count, and a failure reason when applicable.

The plan input is deliberately smaller than a task-tree contract. Each task
must provide an ID, non-empty task text, repository, worktree path, and branch.
The worktree must be below the session directory. Provider environment names
are copied as a list, and default marker-mode fields are filled at this
boundary. This validation prevents unsafe setup; it does not infer
dependencies or decompose task text.

### Worker and provider paths

`cambium.worker` has two explicit modes:

- Without `fanout_config`, `do_work` runs the deterministic marker-edit worker.
- With `fanout_config`, it loads provider configuration, calls the Diffundo
  router once per bounded turn, parses a strict `tool_call` or `finish` action,
  checks permissions, dispatches a tool, emits `tool_event` and `checkpoint`
  messages, and commits the worker result.

`src/cambium/lm.py` contains the optional DSPy-compatible `CambiumLM` adapter
and `ArchitectusLM`; their integration tests use a fake Diffundo. A loopback
provider scenario proves the worker-loop-to-gate-to-merge path. A real external
provider release proof is still a target.

The provider loop is bounded by worker configuration. It records cumulative
provider usage and latency, keeps a bounded transcript, and stops on a strict
finish action, invalid response, tool failure policy, or configured turn/wall
limits. A provider response is not a worker result until the worker has
produced its result envelope and commit. Provider metadata is safe summary
data; credentials stay in the worker environment and are not copied into event
payloads.

### Task trees and disconnected modules

`tasktree.build_tree` validates task IDs, dependency references, depth/width
bounds, and cycles. `topological_order` and `ready_tasks` are pure helpers.
`supervisor.run_plan` currently dispatches the supplied list directly; it does
not use these helpers to schedule a DAG.

`architectus.ArchitectusCore` is a pure scheduling core with an injected LLM
port. Tests cover its decisions and topological waves, but no supervisor module
imports it. `orchestrator.py` remains a submit/drain skeleton.

Persistent worker reuse is not implemented.

### Implemented supporting modules

The repository contains independently tested modules for:

- JSON-Lines framing and request IDs (`ipc.py`);
- SQLite WAL event storage (`store.py`) and serialized Git publication
  (`merge.py`);
- worker fencing, worktree cleanup, gates, resource admission, tool schemas,
  approval, provider configuration, redaction, DLQ, conversations, and result
  records; and
- the `cambium` CLI and the `modules/example` decision module.

Presence of a module or a passing unit/scenario test does not mean that
`run_plan` wires every module into one production path.

### Current event and result boundaries

The canonical store writes append-only event rows to SQLite WAL from a writer
thread. Critical events wait for admission; non-critical events are handed to
the runtime queue and can be dropped according to the store policy. The
supervisor currently creates the store without a session redactor and also
keeps an unbounded in-process non-critical handoff, so the end-to-end redaction
and backpressure contract is not complete.

Worker result messages and root/session results are different boundaries. The
worker emits the strict child envelope defined by `results.py`; the root result
record has its own fields and exit-code mapping. `write_result` can atomically
write `.cambium/result.json`, but the current supervisor does not call it for
the plan lifecycle. Do not treat a worker `result` event as proof that a root
result file exists.

Merge publication uses a throwaway staging worktree and `git update-ref` with
an expected old value. A worker branch is not rebased in place. Conflicts,
non-fast-forward races, unsafe quarantine state, and cleanup failures stop
publication or preserve forensic state; they do not silently advance `main`.

## 2. Runtime sequence and ownership

The current `run_plan` path has these ownership boundaries:

1. The caller owns the session directory and supplies task records.
2. The supervisor validates records and owns worker handles, generations,
   restart decisions, task admission, and event emission on one event loop.
3. A worker owns its process group, worktree edits, provider calls, tool
   transcript, and worker commit. It cannot publish `main` directly.
4. The gate runs in the worker worktree under a finite deadline. A failed gate
   ends the task before the merge step.
5. The merge sequencer owns staging, expected-old checks, ref publication, and
   worktree cleanup. It does not refresh the caller's checkout.
6. The event store owns durable event rows and its writer thread. Observers
   receive copies of event records and cannot mutate the persisted object.

This separation is a runtime fact only for paths that call the corresponding
canonical module. The retained slice and fallback classes are the reason the
canonicalization step remains open.

## 3. Target contracts

The following are targets, not current public interfaces or completion claims:

- expose a small host-facing session/result API from `cambium.__init__`;
- schedule a validated, fixed task tree through an integrated Architectus
  runtime; dynamic replanning is out of scope for the first integration;
- connect one canonical supervisor/store/sequencer path, redaction at event
  admission, and atomic root-result publication;
- provide bounded transport and runtime queues, deadline-bound subprocess and
  gate waits, and durable overflow handling; and
- evaluate persistent worker reuse only after the canonical path and provider
  vertical proof are accepted.

Targets must be demonstrated by source and tests before being described as
implemented.

### Target host boundary

A future host-facing API may expose session creation, status polling, event
consumption, cancellation, and a typed root result. Its names and signatures
are not fixed by this document. Until exports and tests exist, callers should
use the CLI or the module-level functions that are present in source.

The target result boundary is atomic: a completed plan writes one typed root
record only after event persistence and publication decisions are final. A
worker envelope is an input to that record, not a substitute for it. Exit code
and status mappings must remain stable once a public API is introduced.

### Target fixed-tree boundary

The first scheduler integration will accept a validated tree, compute ready
nodes from completed dependencies, and dispatch only those nodes. It will not
let a provider response mutate the live tree in place. A later revision protocol
may be evaluated after the fixed-tree path has deterministic tests and durable
checkpoint semantics.

### Target control boundary

Transport limits, queue bounds, subprocess deadlines, event-store admission,
redaction, approval, resource admission, fencing, and merge publication must be
composed at the supervisor boundary. A helper module is not evidence that its
control is active in the plan path. Each control needs a caller and a focused
failure test before it moves from target to current.

## 4. Boundaries and invariants

These behavioral rules apply to current code and to the target integration.

### Process and protocol

- Workers run in separate Git worktrees and process groups. A worker's stdout is
  the NDJSON protocol; diagnostics go to stderr/logging.
- Each protocol message is one UTF-8 JSON object per newline. Blank lines and
  malformed advisory lines are skipped. A line over `ipc.MAX_LINE_BYTES` is a
  fatal framing error after the reader resynchronizes. EOF is an end-of-stream
  condition, not a protocol message.
- Correlated requests carry a request ID. A result, pong, or ready message is
  accepted only for the active task and generation; stale or wrong-correlated
  messages cannot advance the task.
- Blocking Git, file, and store work stays outside the event loop. The event
  store has a bounded writer queue, while the supervisor's non-critical event
  handoff remains an integration gap until it is bounded end to end.

The worker protocol is intentionally narrow. Initialization carries the task
and generation, task dispatch carries a request ID, and the worker returns
status/result/exit records for that request. Heartbeats and checkpoints carry
progress but do not change task ownership. A malformed advisory line may be
logged and skipped; malformed framing, an oversized line, a wrong request ID
at a fatal handshake point, a missing correlated result, or a non-zero worker
exit fails the task according to supervisor policy.

### Validation, gate, and publication

- Plan validation rejects missing required fields, duplicate IDs, unknown task
  dependencies, malformed dependency graphs, and cycles where `build_tree` is
  used.
- A non-zero gate result or gate timeout fails a task before publication.
- Merge conflicts, non-fast-forward updates, quarantine violations, and stale
  expected-old refs do not publish `main`.
- Publication is ref-only and atomic. It never updates a checkout or index.

The task-tree validator and the plan validator are separate boundaries. The
plan validator protects the current flat entry point. The tree validator is a
pure DAG check that can be invoked by a future scheduler. Neither validator
turns free-form provider output into an accepted task without explicit schema
validation.

### Controls and secrets

- Tool schemas reject malformed calls. Git reset/checkout and shell execution
  pass through the injected approval gate; approval denies by default when no
  callback is available. `fail_open` is an explicit opt-in and is not a
  mandatory production control.
- Worker environments are built from an allowlist. Provider credentials are
  environment values only and must not enter task specs, events, gate output,
  or DLQ records. Redaction is implemented at store/DLQ boundaries, but the
  canonical supervisor wiring is still pending.
- Cambium provides worktree isolation and command controls. It does not claim
  an in-harness OS sandbox.

The process environment is rebuilt for worker and Git subprocesses. Provider
key names may be selected by task configuration, but secret values are read
only by the provider boundary. Gate commands, event observers, logs, and DLQ
records must receive redacted data. The current supervisor wiring is the gap to
close; adding another redact call at a leaf does not close it.

### Provider and cache policy

`Diffundo` routes provider calls by configured tier and priority. It has no
local response cache; repeated calls remain provider calls. Provider-side
caching is outside this repository. Cascade behavior is an implementation
contract only where covered by `diffundo.py` and its tests; research drafts do
not add policy.

The provider adapter is optional at import time. Core modules must not import
DSPy eagerly, and the example module can run offline. A provider failure is a
worker/task failure or a router outcome; it is not a reason for the
deterministic supervisor to invent a fallback result. Local response caching
is not part of the runtime contract.

## 5. Failure policy by boundary

| Boundary | Current behavior | Required invariant |
| --- | --- | --- |
| Plan | Reject malformed task records and duplicate IDs before task setup. | No task side effect before structural validation. |
| Task tree | `build_tree` rejects duplicate IDs, missing dependencies, bounds violations, and cycles. | A scheduler never dispatches an unvalidated graph. |
| IPC | Skip advisory malformed lines; reject oversized frames; enforce request/generation correlation at task checks. | A stale worker cannot complete a newer generation. |
| Worker | Restart or fail on missing results, fatal protocol checks, non-zero exits, and deadline exhaustion. | A worker result is accepted only for its live request. |
| Gate | Non-zero exit or timeout returns a failed task. | No failed gate reaches publication. |
| Merge | Conflict, non-fast-forward, quarantine, or cleanup failure prevents unsafe publication. | `main` advances only through the expected-old ref contract. |
| Approval | Unsafe Git operations require an approval gate; missing callback denies by default. | `fail_open` is explicit configuration, not an assumption. |
| Schema | Invalid tool-call shapes return validation errors. | Tools receive only validated arguments. |
| Store | Critical event admission waits for the writer; store death raises. | Durable boundaries fail closed; no silent success after writer failure. |

The table describes existing checks where the source path calls them. It does
not mark the full runtime as complete; the current supervisor still has the
slice/fallback and queue/redaction wiring gaps listed above.

## 6. Source map

| Concern | Current source | Current status |
| --- | --- | --- |
| CLI and version | `src/cambium/cli.py`, `src/cambium/__init__.py` | CLI exists; package export is version-only |
| Plan supervisor | `src/cambium/supervisor.py` | Flat concurrent `run_plan`; slice/fallback paths remain |
| Worker | `src/cambium/worker.py` | Marker mode and bounded provider tool loop |
| Task validation | `src/cambium/tasktree.py` | Pure validation/order helpers; not run-plan scheduling |
| Architectus | `src/cambium/architectus.py`, `src/cambium/orchestrator.py` | Pure core; not wired |
| IPC | `src/cambium/ipc.py` | NDJSON framing and request IDs |
| Store and merge | `src/cambium/store.py`, `src/cambium/merge.py` | Canonical modules used by the plan runtime when available |
| Controls | `approval.py`, `resources.py`, `fencing.py`, `tools.py`, `schemas.py` | Independently tested; integration is partial |
| Providers and LM | `diffundo.py`, `provider_config.py`, `lm.py` | Provider router and `CambiumLM` merged; external proof pending |
| Evidence | `tests/scenarios/` and `src/cambium/modules/example/tests/` | Behavior tests are authoritative for implemented paths |

## 7. Evolution order

Implementation work follows the short plan in `implementation-plan.md`:
canonical runtime and controls, a thin real-provider vertical proof, fixed-tree
scheduling, then measured experiments. Any new contract must name its source
entry point and a distinguishing test.
