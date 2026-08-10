# Cambium — Distributed Systems Review

> **Historical snapshot — pre-implementation review.** Findings below apply to
> `SYSTEM_DESIGN.md` v0.1.0-draft, not to runtime behavior. For current behavior,
> see [`docs/architecture/architecture.md`](../architecture.md) and
> [`docs/research/v2-1-status.md`](../../research/v2-1-status.md).

**Reviewer:** Distributed Systems Perspective
**Date:** 2026-08-10
**Document reviewed:** `SYSTEM_DESIGN.md` v0.1.0-draft

## CRITICAL FLAWS

### C1. Synchronous file I/O inside the asyncio event loop — backpressure cascade kill-chain

`Supervisor._log_event()` opened, wrote, and closed `events.jsonl` on every
event. No `await`, `asyncio.to_thread`, or `aiofiles` existed. A disk pause blocks
the single event-loop thread; stdout readers stop, 64-KB worker pipes fill,
workers block in `emit()`, heartbeats stop, the 60-second watchdog kills all
workers, and synchronized restarts generate more events. This positive-feedback
cascade invalidates the proposed Temporal-style durability. Move writes off the
loop, keep a handle, batch, and separate in-memory ordering from durable flush.

The reviewed code performed this operation for heartbeats, tool events,
checkpoints, spawns, exits, and errors. Thus the busiest worker controlled the
supervisor's scheduling latency. Even a local SSD pause was enough to stop all
pipe readers; NFS, EBS, or an SSD garbage-collection pause made the failure more
likely. A bounded queue must define overflow behavior—drop only advisory events,
never result or ownership records—or C1 simply moves the unbounded memory
problem to another boundary.

### C2. “stdout EOF = worker dead” is unsound

EOF detects process exit but is not a liveness invariant. Grandchildren or
daemons can inherit the descriptor and hold it open; Python/library stdout
buffers can lose messages on SIGKILL; a kill during `write()` can leave a
truncated JSON line (possibly the result); and a supervisor-side stall can make a
healthy worker appear silent. Enforce `close_fds`, set `PYTHONUNBUFFERED=1`,
reserve a dedicated protocol FD, and distinguish process exit, health, and
progress. EOF is necessary, not sufficient.

The proposed `emit()` used two operations, `sys.stdout.write(json_line)` and
`flush()`. A kill between them can lose a complete result even though Git
changes remain in the worktree. A partial kernel write is read as one line and
`json.loads()` skips it; without a checksum, length prefix, or result
acknowledgement, the supervisor cannot tell a truncated result from arbitrary
noise. Conversely, a language server or telemetry child can keep the descriptor
open after its parent exits. The fix must enforce descriptor ownership, not just
document “stdout is protocol-only.”

### C3. Heartbeat granularity is too coarse

The watchdog allowed 60 seconds, but `run_shell` allowed 120 seconds, a Git
operation 30 seconds, and a four-provider cascade up to about 120 seconds. A
90-second build therefore looked dead, left locks or partial state, and entered
a restart loop. Emit heartbeats during long tools (for example every 15s) or
use independent process, health, and progress deadlines.

The draft's `on_step_end_callback` emitted only after a tool returned. It could
not report a compile, test, or LLM call in progress. Killing at 60 seconds can
leave `.git/index.lock`, a half-written file, or a rebase sequencer; the restart
then repeats the same operation. A per-tool deadline alone is insufficient: the
worker should send progress while the tool runs and the supervisor should retain
the last successful checkpoint.

### C4. Restart policy has no jitter and no absolute cap

The delay sequence `{1,2,4,8,16,32}` seconds is identical for every worker, so a
shared outage causes a thundering herd of Git operations, provider calls, and
event writes. `max_restarts=5` within 60 seconds does not stop a crash every 61
seconds; such a task restarts forever. Add full jitter and a total restart/time
or cost budget.

The intensity window is a rate limit, not a termination guarantee. A worker that
crashes at seconds 0, 61, 122, and so on never reaches five crashes in one
window. A total wall-clock or restart budget is needed for a deterministic
failure, and jitter must be applied independently per task so a provider outage
does not create simultaneous worktree and API pressure.

### C5. Worktree locks survive crashes

A crash can leave `worktree/.git/index.lock`, rebase state, or ref lock files.
`_spawn_worker` reuses the same path, so the next Git command sees
`Unable to create .../.git/index.lock: File exists`. Stale worktrees and branches
also accumulate. Recover before respawn (remove safe stale locks, abort in-flight
operations, reset to `base_commit`) or create a fresh worktree per restart.

The historical worktree manager also removed branches with `git branch -D`; a
stale branch may be the target of a still-running rebase. Cleanup must first
prove the old process is gone, abort Git sequencers, and retain forensic state
when recovery is uncertain. “Remove all `*.lock` files” is safe only after that
ownership check; deleting a live lock can corrupt another process.

### C6. “Temporal-style durability” is unsupported

On supervisor crash, child workers can continue mutating their worktrees while a
new supervisor replays an incomplete JSONL log and respawns from checkpoints.
Those two supervisors can write the same worktree: a split brain with torn
files, index conflicts, and duplicate commits. There is no generation/fencing
token, parent-death signal, or durable ownership record. Add a supervisor epoch
and worker fencing, kill or reap orphans, and use a fresh worktree when recovery
cannot prove exclusive ownership.

The design had no parent-death signal, PID file, lease, or epoch in its JSONL
messages. Replaying a JSONL suffix could therefore respawn a worker while the
original child still held stdin or a worktree. A generation field checked at
every result and commit boundary, plus a process-group kill on supervisor loss,
would distinguish a legitimate restart from split brain. These controls were
recommended, not present in the draft.

## MODERATE ISSUES

### Causal evidence and recovery alternatives

The six critical findings form one failure graph, not six unrelated checklist
items. A chatty worker produces many `tool_event` records; C1 makes the event
loop stop reading pipes; C2's EOF/heartbeat heuristic then classifies a blocked
but live process as dead; C3 makes any tool longer than 60 seconds a false
positive even without a disk stall; C4 restarts all victims at the same fixed
times; C5 makes those restarts inherit Git locks; and C6 allows an old supervisor
or orphan process to keep writing while a replacement believes it owns the same
worktree. The result can be mass restart, duplicate commits, and no trustworthy
answer to whether a task completed.

The review distinguished three signals that the draft conflated: **process
liveness** (`waitpid`/poll and exit code), **protocol health** (valid
ready/heartbeat and a drained pipe), and **task progress** (tool events and
checkpoint advancement). A process can be alive while stdout is blocked; a
healthy worker can be quiet during a long compile; and a dead process can leave
a grandchild holding a descriptor. Each signal needs its own timeout and event.

For C1/C3, alternatives were a dedicated writer thread with a bounded queue,
`asyncio.to_thread`, or `aiofiles`; long tools could emit progress from a thread
or use a background heartbeat. The preferred shape was a fast in-memory ordering
path plus a durable writer that keeps the handle open and fsyncs on a bounded
interval. For C2, a dedicated protocol FD (FD 3) would leave stdout available
for redirected library output; `PYTHONUNBUFFERED=1`, `close_fds`, and a drain
deadline would make the contract enforceable. For C4, AWS-style full jitter was
the concrete example: `random.uniform(0, delay)` rather than a shared schedule.

For C5/C6, recovery choices were: clean stale locks and abort rebase state in
place; reset to `base_commit`; tear down and create a fresh worktree; or use a
separate bare/merge repository. Fresh worktrees cost disk but remove split-brain
risk. A supervisor epoch or fencing token in every message would let a new
owner reject stale workers. None of these mechanisms appeared in the reviewed
draft, so “Temporal-style” was an analogy rather than durability evidence.

### M1. Merge sequencer is a serialization bottleneck

`git checkout main` → rebase → fast-forward → 300-second test is O(N) for N
workers and mutates shared refs. Alternatives recorded in the draft were
batch-then-test with bisect, and a speculative binary merge tree with O(log N)
tests. If sequential merge remains the policy, isolate it in a dedicated
throwaway worktree and state the throughput trade-off.

The serialization cost is not only wall-clock time. A merge can change the
meaning of another worker's `base_commit` between its rebase and test. A test
failure followed by `reset --hard HEAD~1` can undo a different worker's commit
if another merge interleaved. A queue/actor gives one owner of refs and makes
the ordering explicit; a tree or batch strategy needs conflict attribution and
bisect rules before it can be called safe.

### M2. `WorkerHandle` has a logical TOCTOU race

The stdout reader mutates `last_heartbeat`, `status`, and `proc` while the
watchdog reads them. Even on one asyncio thread, an `await` between read and
decision can observe a half-transition: the watchdog kills a worker immediately
after a heartbeat or restarts one whose result was already accepted. Funnel
updates through one state-machine task or version state transitions.

For example, `_read_worker_output` can set `state=DONE` by returning a result
while the monitor is between its 10-second sleep and elapsed-time check. The
monitor then sees an old `last_heartbeat`, kills the process, and the caller may
record both `task_done` and `heartbeat_timeout`. An enum alone does not make the
transition atomic; a serialized event reducer or monotonic generation number is
needed.

### M3. Event append is not durable

`open(..., "a")` plus `write()` can leave a truncated final line on power loss;
the in-memory list is not a recovery log. Keep one writer, batch, fsync on a
bounded interval, and use checksums/length prefixes so replay can skip a torn
record. SQLite WAL was a viable stdlib alternative.

The distinction matters because the draft used replay as its crash-recovery
story. A record can be visible in `event_log` but absent or partial on disk; a
replayed `worker_spawned` without `worker_ready` cannot prove whether the child
is dead, slow, or still owns a pipe. A checksum can detect the final torn record
but cannot recover a missing result; fencing and an external ownership record
are still required.

### M4. FanOut state is not thread-safe

`asyncio.to_thread` race calls mutate provider counters, cooldowns, and a shared
cache dict. The draft's free-threaded Python target makes these true data races,
not merely GIL assumptions. Protect state with locks or confine each provider and
cache to an event-loop owner.

The race is concrete: two `to_thread` calls can both read
`provider.total_calls == 3` and write 4, or one can clear a cooldown while the
other sets it. A cache write can also win after a newer provider response. The
draft's “free-threaded” target removes the accidental GIL serialization that
might hide these bugs in ordinary CPython; an explicit lock or actor is required
even if the target is later changed.

### M5. Python 3.14 free-threaded adds risk without a demonstrated benefit

Workers are processes; the supervisor and orchestrator are single-threaded
asyncio; provider calls are I/O-bound. No workload or benchmark needs no-GIL,
while DSPy/LiteLLM native dependencies may not be safe. Standard CPython 3.12+
was the safer default; free-threading should be optional and evidence-backed.

The only cited parallel workload was SIMBA `num_threads=4`, an offline
optimization job that can use processes. The production path uses separate
worker processes and network I/O. The review found no benchmark, compatibility
matrix, or fallback to a standard build, so “free-threaded” was an unsupported
requirement rather than a measured design choice.

### M6. Orchestrator lacks cycle detection and a sound task-ID counter

An LLM-generated A↔B dependency cycle empties `ready` and silently drops both
tasks. The draft also showed a broken `def __task_id_counter`. Validate a DAG
with topological sort, reject cycles and failed dependencies, and assign IDs in
deterministic code.

The failure is silent: `pending` retains A and B, `ready` becomes empty, the
loop exits, and the reviewer can see a partial result set. A topological sort
should reject cycles before any worktree is allocated; a failed dependency
should mark dependants skipped rather than leaving them pending. IDs generated
by the LLM should be normalized or replaced with deterministic IDs before
dispatch.

### M7. Provider failure is not isolated from worker liveness

The prose said existing workers survive an all-provider outage, but workers call
FanOut on every ReAct step. `AllProvidersFailed` becomes a worker `error`; the
supervisor restarts every worker, recreating the outage cascade. Distinguish
temporary provider unavailability from task failure, park new dispatch, and use
an aggregate circuit breaker.

The prose's isolation claim applied only to an in-process Architectus call. In
the sample, Opifex catches a provider exception, emits `error`, and Custos
returns it as a worker failure. Five workers therefore repeat the same provider
failure under one restart policy. The safe state machine needs “provider
unavailable; keep task alive,” “task rejected,” and “worker crashed” as distinct
outcomes.

## MINOR NOTES

- **N1:** Sample defects: `write_content` (should be `write_text`), missing
  `return` in `grep_code`, missing `os`, malformed error type, broken task-ID
  declaration, invalid `await`, `self.root`, corrupted metric tokens, bad
  flywheel path, and broken box drawing.
- **N2:** `collect_commits` uses `HEAD~5..HEAD`; it fails with fewer than five
  commits and can report unrelated commits. Use `git log -5` and a task boundary.
- **N3:** Septum (M8) references undefined `sys` and is not wired into worker
  spawn; M9's optimization boundary was likewise only a target in the draft.
- **N4:** `grep_code` interpolates an LLM pattern into `shell=True`; quote or use
  argument arrays, and treat `run_shell`/`git_op` as explicit capability gates.
- **N5:** The event log has no rotation, compaction, or size bound.
- **N6:** “Kahn process network” and “CSP” are labels unless their actual channel
  and determinism guarantees are implemented; ordinary asyncio pipes do not prove
  those properties.
- **N7:** `shutdown()` kills processes but never removes worktrees or branches.

The review intentionally stayed at the distributed-systems boundary. It did not
claim that every sample bug was an architectural defect: N1/N3/N4 were listed
as implementation hazards because they alter liveness or safety at a boundary.
Likewise, the recommendation to use SQLite WAL was an alternative to the
JSONL durability design, not evidence that the historical draft used SQLite.
The source names (Erlang/OTP, s6, Temporal, Kahn, CSP, AWS) identify the
comparison points used on 2026-08-10; they are not current dependency claims.

## VERDICT: Fix First

The review found a sound core—process-isolated workers, JSON IPC, Erlang-style
supervision, Git worktrees, and deterministic/LLM separation—and a genuine DSPy
optimization differentiator. It still judged the draft **not ready to build**:
C1–C6 are normal-operation failure chains, not edge cases. Recommended order:
(1) off-loop durable I/O; (2) separate process, health, and progress signals;
(3) jitter; (4) worktree recovery; (5) fencing/generation counters; (6) drop
free-threaded as a default; and (7) fix N1 samples. The competitor analysis was
strong; the draft needed this failure-model redesign before coding.
