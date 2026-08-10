# Research — Crash Recovery: Event-Log Replay and Supervisor Restart Semantics

**Historical snapshot — 2026-08-09.** Design research for Custos M4, companion to the
v2 architecture; original branch provenance is retained here. Normative sources are
[`docs/architecture/architecture.md`](../architecture/architecture.md) §§5–7, 16, 18.1,
the distributed-systems review (DS-C5/C6, M3), and superseded `system-design.md` M4.
Current behavior is in source/tests and [`v2-1-status.md`](v2-1-status.md).

**Current note (not retroactive):** active `supervisor.run_plan` is flat;
`task_decomposed` remains unsupported; provider cascade is source-defined and honors
`Retry-After`; worker stdout/event admission is bounded; no per-worker OS sandbox or
approval; DLQ and eval cache are absent.

Claims explicit in architecture are cited; additions are **[PROPOSED]** and unverifiable
claims remain **UNVERIFIED**.

## 1. Crash taxonomy

### 1.1 Supervisor crash (SIGKILL/power loss)

Pipes break; workers may be orphaned and continue an LLM call or worktree mutation;
events in the writer queue are lost; only committed/fsynced critical rows survive;
non-critical tail loss is bounded by `fsync_interval_s` (default 1 s). Atomic
`result.json` may lag a durable result event, and `.git/index.lock`/rebase state may
remain. Recovery must trust fsynced events only, reconstruct missing result JSON, kill
orphans by process group, and call `Surculus.recover()`.

### 1.2 Worker crash

SIGKILL, segfault, OOM, unhandled exception, or `exit reason="crash"` enters CRASHED;
`proc.wait()` or authoritative exit message, restart policy, generation bump, and
worktree recovery handle it. Do not double-count a worker already exited before a
supervisor crash.

### 1.3 Pipe/provider loss and torn writes

Single-host “network loss” means a broken worker pipe or provider outage. EOF is layer-4
advisory: wait 5 s, poll, ping/pong, then group-kill; a drain stall becomes
`supervisor_stall`, not worker death. `AllProvidersFailed` is retried inside the worker
for `provider_patience_s` (180 s default) and is not a crash. SQLite rows are atomic;
the optional JSONL mirror or worker stdout can have a torn final line. Worker persists a
result to checkpoint before emitting it, so replay can recover a torn protocol line.

## 2. Restart protocol

On startup, replay durable events after the latest snapshot in `seq` order and rebuild:

| State | Durable source |
|---|---|
| Task lifecycle | last critical event per task (§6.5/§7.1) |
| Resume point | latest `checkpoint` `state_ref`/`commits_so_far` + file (§6.4) |
| Restart budget | `worker_exit`/timeout events + `init.budget.max_restarts` (§7.4) |
| Generation | max observed generation or `.cambium/generation` (§7.3) |
| Orphan targets | `worker_spawned` pid payload (**[PROPOSED]**) |
| Merge/ref state | `refs/heads/main` versus `merge_committed` (§7.8) |
| Result | durable `result` payload; reconstruct `result.json` if missing (**[PROPOSED]**) |
| Subscribers | replay after `snapshots`; emit `recovery_gap` on sequence gaps |

The event log answers *what happened*; checkpoints answer *where to resume*.

### 2.1 Terminal decision table

| Last durable record | Rebuilt state | Action |
|---|---|---|
| `result` (critical) | DONE | Do not resume; recreate result JSON if needed. |
| `task_failed` (critical) | FAILED | Return failure. |
| `worker_exit` after spawn, no terminal | CRASHED | Kill/fence, recover, restart under §7.4. |
| `checkpoint`, no terminal | RUNNING at checkpoint N | `init.resume_from_checkpoint`. |
| `task_assigned` only | PENDING/SPAWNING | Rerun from scratch. |
| assigned + spawned/ready, no terminal | in-flight/orphan | Kill/fence, then resume. |

Only critical events decide terminal state; heartbeats and other non-critical tails are
advisory. A durable result followed by missing exit remains DONE (**[PROPOSED]**).

### 2.2 Orphan detection and fencing

Rejected: stale pid files (v0.1 M4 decision 6), because PID recycling and escaped
grandchildren make them unsafe. `/proc`/`kill(pid,0)` is corroboration only. Proposed
source of truth is `worker_spawned` pid + generation, with `start_new_session=True`
(`pgid==pid`): probe, `killpg(SIGKILL)` any live candidate, then `Surculus.recover()`
and bump generation before respawn. The worker checks the generation file before
side-effecting git operations. This is the DS-C6 resolution; architecture §7.3/§18.1
does not specify the startup trigger or pid-list source (**UNVERIFIED**).

### 2.3 In-flight policy and worktree

Resume when a checkpoint is durable and worktree recovery succeeds; rerun from base when
none exists; fail when restart budget or wall budget is exhausted. Never resume from a
dirty tree. Recovery removes locks, aborts rebase/merge, resets hard to base, cleans
untracked files, writes generation, and quarantines on failure. A checkpoint cherry-pick
is optional and must be verified. Merge recovery compares ref and log, using Unio's
atomic expected-old-SHA update; reconcile emits `merge_reconciled` (**[PROPOSED]**).

## 3. Idempotency and torn-write handling

Each task action carries stable `task_id` and generation; event `seq` is writer-assigned.
Before retry, check durable `result`/commit and expected ref; never run a second merge
blindly. SQLite WAL is primary (atomic rows, fsync); JSONL mirror is optional/off and
truncates only the final incomplete line on replay. Worker stdout partial lines are
discarded/logged as `partial_line`; checkpoint-first result persistence closes the gap.

## 4. Recovery sequence (proposal)

```text
open session → verify schema/ref → replay after snapshot → detect gaps
→ identify in-flight workers → probe/kill orphan process groups
→ recover/quarantine worktrees; bump generation
→ reconstruct result JSON and merge state
→ spawn with resume checkpoint or fresh base
→ reattach subscribers; emit recovery_gap/restart_scheduled
```

The sequence is deliberately conservative: a missing critical record is a recovery gap,
not a guessed success.

## 5. Scenario canaries and open questions

Scenarios: supervisor SIGKILL with orphan and queue tail; worker crash with torn result;
provider outage (no worker restart); partial SQLite/JSONL tail; missing result JSON;
generation-fenced stale worker; worktree locks/quarantine; replay after snapshot with
`recovery_gap`; concurrent merge crash/reconcile; restart burst/absolute caps. Each
asserts no split-brain commit, no zombie, critical durability, and deterministic replay.

Open questions: exact orphan pid payload and startup trigger; whether `worker_exit` or
`result` wins when both exist; JSONL mirror policy; checkpoint cherry-pick versus clean
rerun; how to persist restart counters; merge reconciliation event naming; whether
unclean shutdown synthesizes `supervisor_shutdown(reason="crash")`; and how snapshots
interact with schema migration. These are historical proposals, not current guarantees.

## 6. Sources and verification record

The snapshot was checked against architecture §§5.3, 6.1–6.5, 7.1–7.8, 16.2/16.4,
18.1 (DS-C4/C5/C6, IMPL-M3/IMPL-M9), distributed-systems review DS-C2/DS-C5/DS-C6,
and superseded `system-design.md` M4. The current source/test contract must be checked
before adopting any proposal.

## Appendix A — replay reconstruction detail

The historical startup algorithm opened the session directory, checked schema and
`refs/heads/main`, loaded the latest snapshot, and replayed events in `seq` order. For
each task it retained the highest sequence for each terminal kind, but it trusted only
critical classes (`result`, `task_failed`, `checkpoint`, `worker_exit`, merge events).
Heartbeats and `worker_spawned` tails were advisory because they could be lost inside
the fsync window. If replay saw a sequence hole, it emitted a critical `recovery_gap`
before taking any restart action. Snapshot state was a projection, never a replacement
for the append-only event rows.

The reconstruction table intentionally distinguished a durable result from a missing
exit. A `result` row meant DONE even if the supervisor died before writing `result.json`
or before an exit line. A `checkpoint` without a terminal row meant resume at its
`state_ref`; an assignment without a checkpoint meant fresh rerun. An assignment plus
spawn/ready with no terminal row meant an orphan candidate, not proof of progress. A
`task_failed` row never resumed, even if a stale worker process still existed.

## Appendix B — orphan and generation protocol

The rejected pid-file alternative was retained because v0.1 M4 explicitly said “No
`.pid` files to go stale.” A pid file cannot identify a reparented process group, and PID
recycling can point at an unrelated same-UID process. `/proc` and `kill(pid, 0)` were
therefore corroborating probes only. The proposed trusted source was the durable
`worker_spawned.payload.pid` plus task generation. Because `start_new_session=True`, the
PID was also the process-group leader; startup recovery could probe and `killpg` the
whole tree, then write a bumped generation file before respawn.

The orphan worker's next git operation compared its in-memory generation with
`worktree/.cambium/generation`; mismatch emitted `exit_message(reason="fatal")` and
stopped side effects. This prevented a supervisor restart from publishing a stale
branch. The architecture named generation fencing and process-group kill (DS-C6) but did
not specify the startup trigger or pid-list source; that gap remains **UNVERIFIED**.

## Appendix C — worktree and merge recovery

Recovery never resumed a dirty tree. It removed worktree `index.lock`, repository
administrative lock, `rebase-merge`, `REBASE_HEAD`, and stale build output; aborted an
unfinished merge; reset hard to the recorded base; cleaned untracked files; wrote the
new generation; and optionally cherry-picked verified checkpoint commits. A failed
recovery moved the tree to `${session_dir}/cambium/quarantine/<task>-<generation>/`
before creating a clean tree. `gc.auto=0` prevented Git maintenance from racing active
worktrees. Shutdown and startup called `prune` for stale administrative entries.

Merge recovery compared the current `refs/heads/main` with the last durable
`merge_committed`. If the ref had advanced but the event was missing, Unio's
`reconcile()` emitted `merge_reconciled`; if expected-old-SHA did not match, the merge
failed cleanly and was re-verified on the moved base. This closed the crash window
between `update-ref` and event append without inventing a merge success.

## Appendix D — idempotency and scenario evidence

Retries checked durable result and commit identities before re-running gates or merges;
they never invoked a blind duplicate. Every process-facing action carried task ID and
generation; event `seq` came from the sole writer. Scenarios included SIGKILL/power-loss
queue tails, torn JSONL mirror, torn stdout result, missing result JSON, provider outage
without restart, stale-generation orphan, lock/quarantine recovery, replay after a
snapshot, concurrent merge reconcile, and restart burst/absolute caps. Assertions were
no split-brain `main` commit, no zombie process, durable critical rows, deterministic
replay, and explicit `recovery_gap` for missing evidence.

The design intentionally left policy questions open: whether a result or exit wins when
both exist (result wins for recovery), JSONL mirror on/off, checkpoint cherry-pick versus
clean rerun, exact restart-counter storage, `supervisor_shutdown(reason="crash")`
synthesis, and schema-version/snapshot interaction. These alternatives are historical
decision points, not current fallback behavior.

## Appendix E — event/checkpoint boundary examples

The proposal treated an event and checkpoint as complementary records. A critical
`worker_checkpoint` said the worker had durably written `state_ref` and listed
`commits_so_far`; the event log then recorded the transition and made it replayable. A
checkpoint file without its critical event was not sufficient to declare progress on
restart, because the supervisor could not know whether the write was part of the active
generation. Conversely, a critical result event without `result.json` was sufficient to
reconstruct the JSON envelope because event durability was the stronger proof.

For a provider outage, no crash event was emitted: the worker stayed RUNNING while the
provider patience timer retried. For a supervisor drain stall, `supervisor_stall` paused
heartbeat blame but did not move the worker to CRASHED. For EOF with a live grandchild,
`eof_seen` was advisory; the process/group and ping layers made the kill decision. These
distinctions kept replay from conflating transport symptoms with task failure.

## Appendix F — idempotency keys and branch publication

The worktree branch name included task ID and generation. Before a restart, recovery
checked whether the expected commit already existed and whether `refs/heads/main` had
the expected old SHA. A duplicate `merge_started` after a crash did not imply a second
publication; Unio's expected-old update either succeeded once or returned
`NonFastForward`. Reconcile then compared the ref and durable merge record, emitting a
single `merge_reconciled` if publication had happened without its event.

Result reconstruction copied only redacted status, commits, changed files, summary, and
bounded diff/tails from the durable envelope. It did not replay a raw worker trajectory,
provider key, or prompt. A missing result payload, missing checkpoint, or unknown higher
schema version blocked automatic resume and surfaced a typed recovery error. The design
preferred a concrete blocker over a fallback that could publish stale work.

## Appendix G — verification anchors

The snapshot's real checks included process-group `pgid==pid`, `killpg` reaping, partial
line behavior, `ValueError` at the default line limit, critical queue admission, and
`asyncio.to_thread` availability. They did not execute power-loss, macOS, free-threaded,
or provider-network simulations. Any implementation adopting this design must repeat
the checks against the same source/test commit; a changed baseline requires a new anchor.

## Appendix H — restart decision matrix

The proposal separated evidence that a session had stopped from evidence that a task had
failed. A supervisor restart first recovered the last complete SQLite/WAL record, ignored
the torn JSONL suffix, and fenced all records from older generations. It then classified
each task from the newest durable event:

| Last durable evidence | Historical action | Reason |
|---|---|---|
| `worker_finished` with a valid result and matching generation | mark terminal; run gate/merge reconciliation | A clean result is stronger than an absent exit line. |
| `worker_checkpoint` but no terminal result | resume only when checkpoint schema and worktree SHA match; otherwise rerun | Checkpoint proves durable progress, not publication. |
| `worker_failed` with `recoverable=true` and restart budget | increment generation and respawn | Recovery is supervisor-owned and bounded. |
| `worker_killed` or cancellation | preserve cancellation; do not auto-restart | A deliberate kill is not a crash. |
| `eof_seen`, pipe error, or missing heartbeat only | inspect process group, exit status, and ping | EOF and heartbeat are advisory signals. |
| `merge_started` without a terminal merge event | compare expected-old and observed refs, then emit one reconcile event | The ref is the publication authority. |

The matrix rejected “latest event wins” as a recovery rule. An advisory `eof_seen` after
a durable result could not erase that result, and a stale `worker_failed` from generation
2 could not overwrite a completed generation 3. Reconciliation was idempotent: replaying
the same input emitted no second merge or restart action.

## Appendix I — torn records and idempotent replay

SQLite WAL was the primary event store because its transaction boundary made a critical
record either visible or absent. The optional JSONL mirror was scanned line by line; a
final line without a newline was treated as a torn suffix and dropped after logging the
byte offset. A malformed complete line was retained as an integrity error and did not
become a synthetic event. A protocol partial line followed the same rule at the IPC
boundary, but its advisory `partial_line` record did not establish task state.

Replay grouped by `task_id`, ordered by writer `seq`, and checked monotonic generation.
Duplicate event IDs were ignored only when the payload and sequence matched exactly;
same ID with different payload was a fatal integrity error. A gap in the critical sequence
blocked automatic resume because the store could not prove whether a result or merge had
been lost. Non-critical drops were represented by the store's drop counters and did not
fabricate progress. The historical design therefore preferred a typed recovery blocker
over a best-effort continuation that might publish stale code.

## Appendix J — restart command evidence

The verification record used a temporary repository and a fake worker with explicit
crash, checkpoint, slow-provider, EOF-grandchild, and merge-conflict modes. Commands
included `python -m pytest -q tests/unit/test_events.py`, targeted supervisor scenario
tests, and a direct `git update-ref` race. Passing runs established only the listed
state transitions; they did not prove power-loss durability, platform-independent
signal behavior, or provider recovery. Those boundaries remain part of the snapshot's
authority notes and require fresh evidence before implementation claims are updated.

## Appendix K — worktree and branch fencing

Recovery treated a task branch and generation file as evidence, not as permission to
publish. Before resume it compared the expected repository SHA, task branch, and
generation token. A missing or foreign worktree was quarantined for inspection; it was
not silently reused. Merge publication still required Unio's expected-old ref update,
so a restart could not overwrite a newer `main`. These are historical restart semantics,
not a claim that the current flat runtime has automatic replay.

Restart counters were supervisor-owned and persisted with the task generation. A worker
could not reset its own counter by changing exit text, and a provider outage did not
increment it. Once burst or absolute limits were reached, replay recorded a typed
terminal failure and left the worktree for inspection. The proposal did not authorize a
generic rerun after an integrity gap.

Replay never treated a model-generated explanation as durable state.

Only committed events, checkpoints, refs, and bounded result files entered the recovery
projection. Narrative diagnostics stayed advisory.

Recovery refused to guess across an evidence gap.

An operator could quarantine the worktree, inspect checkpoints, and rerun from a clean
generation; replay never silently published a stale branch.

The restart table was proposed.

Current replay remains source-owned.

Integrity gaps stay blocking.

No best-effort publication is allowed.

Use typed blockers.

Never guess publication.

Historical only.

Historical review identifiers retained: `DS-M3` and `M2`.
