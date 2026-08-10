# Custos — Event-Loop Architecture

**Historical snapshot — 2026-08-09.** Design spec from the recorded
`/tmp/opencode/cambium-csp` worktree; the source record does not identify a branch ref.
It resolves the proposed M4 asyncio gap, not current implementation.
Current behavior is in [`docs/architecture/architecture.md`](../architecture/architecture.md),
source/tests, and [`v2-1-status.md`](v2-1-status.md).

**Current note (not retroactive):** active `supervisor.run_plan` is flat;
`task_decomposed` remains unsupported; provider cascade is source-defined and honors
`Retry-After`; worker stdout/event admission is bounded; no per-worker OS sandbox or
approval exists; DLQ and eval cache are absent.

## 1. Decisions and execution contexts

| ID | Historical finding | Proposed resolution |
|---|---|---|
| **DS-C1** | Sync disk I/O stalls the loop, fills pipes, and false-kills workers. | Loop only enqueues; one dedicated SQLite-WAL writer thread owns persistence. |
| **DS-M2** | `WorkerHandle` races across `await`; watchdog can kill an exited process. | Handles are loop-affine; guard + mutate with no `await`; `poll()` re-check and catch `ProcessLookupError`. |
| **DS-M3 / IMPL-C8** | Append is not fsync; sync methods are awaited. | Single writer with explicit fsync cadence; `asyncio.to_thread` for git/blocking calls. |

The single event-loop thread owns worker spawn/IPC, timers, supervision, event enqueue,
subscriber fan-out, shutdown, and policy decisions. The one writer thread owns the
SQLite connection, redaction, inserts, WAL checkpoint/fsync, and
`loop.call_soon_threadsafe` publication. N worker subprocesses own their ReAct loop,
worktree, and generation token. `Unio` keeps its own `asyncio.Lock` for merge order.
There are no shared mutable handles across threads: only immutable, already-redacted
events cross the boundary.

Spawn contract (proposal):

```python
await asyncio.create_subprocess_exec(
    *septum.wrap([sys.executable, "-X", "utf8", "-u", worker_script]),
    stdin=asyncio.subprocess.PIPE, stdout=asyncio.subprocess.PIPE,
    stderr=asyncio.subprocess.PIPE, limit=1_048_576,
    cwd=worktree_path,
    env={**os.environ, "PYTHONUNBUFFERED": "1",
         "CAMBIUM_TASK_ID": task_id, "CAMBIUM_GENERATION": str(generation)},
    start_new_session=True, pass_fds=(), close_fds=True)
```

`start_new_session=True` makes `pgid == pid`; group kill handles DS-C2 grandchild
inheritance. The proposed line cap is 1 MiB; protocol fields are separately bounded.

## 2. I/O and admission invariants

- `StreamReader.readline()`/`readuntil()` stays on the loop; partial tails at EOF are
  logged as `parse_error` and skipped. A checkpoint precedes a result emission, so a
  torn result line can be recovered.
- The default 64 KiB reader limit raises `ValueError`; pass `limit=1_048_576`, catch
  over-limit lines, consume/resync, log and count, never kill the loop.
- `asyncio.to_thread` handles git/worktree, merge, and blocking path operations; the
  loop never calls `open`, `write`, `fsync`, `sqlite3`, or blocking git directly.
- The bounded event queue is `queue.Queue(maxsize=10_000)`: non-critical records use
  `put_nowait` and may drop the oldest non-critical item with a `drop` marker;
  critical records (`result`, `checkpoint`, `worker_exit`, `task_failed`, merge and
  assignment events) use a timeout-bounded put (0.1 s). The writer fsyncs critical
  records before subscriber yield. Subscriber queues are bounded `asyncio.Queue`s;
  worker stdin remains an OS pipe.

`WorkerHandle` states are `PENDING → SPAWNING → RUNNING → DONE|FAILED|REJECTED|CRASHED`.
Only loop tasks mutate `state`, `last_heartbeat`, `proc`, `crash_times`, `generation`,
and `result`; one synchronous transition site updates each. The watchdog checks
`proc.poll()` before kill and catches `ProcessLookupError` (verified on a reaped
process), so DS-M2 is guarded at its source.

## 3. Cancellation and shutdown (proposal)

1. Set shutdown, stop intake, and reject new submissions.
2. Send `cancel` to live workers, swallowing `BrokenPipeError`.
3. Wait `graceful_s=10` s for authoritative `exit`.
4. SIGTERM each remaining process group; wait `term_grace_s=5` s.
5. SIGKILL, `proc.kill()` as belt-and-braces, `await proc.wait()`, catch
   `ProcessLookupError`; every child is reaped.
6. Cancel/drain reader, watchdog, EOF, and drain-deadline tasks.
7. Stop subscriber intake and boundedly flush queues.
8. Emit critical `session_ended`; drain writer; `wal_checkpoint(TRUNCATE)` + fsync.
9. `Surculus.prune()` stale `.git/worktrees/` entries (DS-N7); quarantine active trees
   according to policy.
10. Atomically write `result.json` (`cancelled`, exit code 4), close logging, return.

Smoke assertions check every `proc.returncode` after step 5 and durable
`session_ended` after step 8.

## 4. Scaffold delta (historical proposal)

The then-merged `src/cambium/orchestrator.py` (59 lines) and `events.py` (47 lines)
were placeholders. The intended delta was: split the facade from Custos; replace
counter IDs with ULID task IDs plus generation fencing (§7.3); use an
`asyncio.TaskGroup` one-for-one supervisor; route events to the writer/subscribers;
adopt the architecture envelope (`kind`, `task_id`, `request_id`, `timestamp`,
`monotonic_ms`, `generation`, `payload`) and tiers; add spawn/ready handshake,
heartbeat/EOF/drain watchdogs, jittered restart, Surculus recovery/prune, async
context/shutdown, and Unio's serialized gate/update-ref publication. The smoke gate
was retained because the reviewed v0.1 sample had ~12 syntax/name bugs.

## 5. Scenario canaries

Use deterministic fake workers (no DSPy/network):

1. DS-C2 mode c torn result: parse-error event, no reader crash, checkpoint recovery.
2. 200,000-byte line: 1 MiB path succeeds; default limit failure is caught/counts.
3. DS-C1 event burst: second worker's heartbeat survives; queue drop is bounded.
4. DS-M2 exited worker: `poll()`/`ProcessLookupError` guard leaves monitor alive.
5. Shutdown: cancel/group kill, all children reaped, durable `session_ended`, prune,
   cancelled result in bounded time.
6. Thread→loop handoff: critical DB row precedes subscriber delivery.
7. DS-C4 crashes: full-jitter delay, burst cap 5/60 s, absolute cap 10, then FAILED.
8. DS-C6 restart: generation increments; stale orphan self-terminates.

## 6. Verification appendix (historical commands)

Run from `/tmp/opencode/cambium-csp` with `uv run --python 3.14.7`; interpreter was
`cpython-3.14.7-linux-aarch64-gnu`.

```text
python -c "import asyncio; print(hasattr(asyncio,'create_subprocess_exec'))" → True
python -c "import asyncio; print(hasattr(asyncio,'to_thread'))" → True
readline partial tail → lines=['{"type":"ready"}', '{"type":"heartbeat","turn":1}', '{"type":"tool_event","cmd":"sleep"}'], rc=0
default >64KiB → ValueError; limit=1_000_000 reads 200,001 bytes
terminate/wait → -15; kill reaped process → ProcessLookupError
start_new_session → pgid==pid; killpg → -9; wait_for(0.5) → TimeoutError
asyncio.Queue(maxsize=2).put_nowait(3) → QueueFull; thread→loop handoff → delivered
stderr lines/torn tail match stdout; Linux watcher → _PidfdChildWatcher
```

UNVERIFIED at the snapshot: macOS group semantics, old-kernel watcher fallback,
free-threaded 3.14t, and Windows Proactor. Sources were architecture §5.3, §6.2/6.5,
§7.1–7.8, §13–14; the superseded `system-design.md` M4; distributed-systems review
DS-C1/DS-M2/DS-M3; and the scaffold paths above.

## Appendix A — queue handoff and state proof

The event-store queue was intentionally the only cross-context handoff:

| Queue | Producer | Consumer | Bound / policy |
|---|---|---|---|
| Event store | loop `put_nowait` | writer thread | 10,000; non-critical drop, critical timeout put. |
| Subscriber stream | writer via `call_soon_threadsafe` | loop consumer | configured `asyncio.Queue`. |
| Worker stdin | loop `StreamWriter` | subprocess | OS pipe; `drain()` awaited. |

The writer applied redaction before SQLite serialization, inserted a row, batched
`PRAGMA wal_checkpoint(TRUNCATE)` and `os.fsync(wal_fd)` every second, and performed the
same fsync synchronously for a dequeued critical event before publishing it. A subscriber
could therefore observe a critical record only after the DB row was durable. A queue-full
non-critical event emitted one bounded `drop` marker rather than recursively logging
every drop. Critical admission waited at most 100 ms through `asyncio.to_thread`; the
design treated that short wait as a backpressure valve, not an unbounded disk stall.

`WorkerHandle` had one mutation site per field. The stdout reader updated
`last_heartbeat` and `state=RUNNING` together; watchdogs read but did not mutate except
through transition helpers. The stale-observation sequence (watchdog wakes, process
already exits, reader has not run) was handled by `proc.poll()` immediately before kill;
`ProcessLookupError` from a reaped process was treated as already dead. This was the
source-level DS-M2 guard, not a timing assumption.

## Appendix B — shutdown ordering proof

The ten-step shutdown was designed to be observable and bounded:

1. Set `_shutdown` and stop Architectus admission.
2. Send cooperative `cancel` to RUNNING/READY/SPAWNING handles.
3. Wait `graceful_s` for authoritative `exit`.
4. SIGTERM remaining process groups and wait `term_grace_s`.
5. SIGKILL, `proc.kill()` belt-and-braces, wait/reap, catch `ProcessLookupError`.
6. Cancel and drain reader/watchdog/EOF tasks.
7. Stop subscriber intake and flush bounded queues.
8. Enqueue critical `session_ended`; drain writer and final WAL fsync.
9. Prune stale worktree administration and quarantine failures.
10. Atomically write cancelled `result.json`, final status, and stop logging.

The postconditions were every child reaped, `session_ended` in SQLite, no subscriber
left waiting, and cancellation represented as status `cancelled`, exit code `4`. The
worktree prune was allowed before step 8 only when its result was itself logged; the
default ordering kept the event durable first.

## Appendix C — scaffold replacement inventory

The 59-line orchestrator skeleton had a counter task ID and serial callback drain. The
proposed replacement introduced: ULID + generation identity; `TaskGroup` one-for-one
supervision; ready handshake (`ready_timeout=60`); partial-line/over-limit stdout and
stderr readers; heartbeat, EOF-grace, and drain-deadline monitors; jittered restart;
Surculus create/recover/prune; `__aenter__`/`__aexit__`, `cancel`, `events`, `query`; Unio
gate/merge/update-ref; and canonical event envelope fields. This was a design delta, not
an assertion that the scaffold already had those components.

## Appendix D — verification command log

Historical checks ran from `/tmp/opencode/cambium-csp` with
`uv run --python 3.14.7` (`cpython-3.14.7-linux-aarch64-gnu`):

```text
hasattr(asyncio,'create_subprocess_exec') → True
hasattr(asyncio,'to_thread') → True
partial final readline → ready, heartbeat, tool_event; rc=0
default 64KiB line → ValueError; limit=1_000_000 → 200,001 bytes read
terminate/wait → -15; kill reaped process → ProcessLookupError
start_new_session → pgid==pid; killpg → -9; wait_for(0.5) → TimeoutError
Queue(maxsize=2).put_nowait → QueueFull; thread→loop callback → delivered
stderr torn tail → same behavior as stdout; Linux child watcher → _PidfdChildWatcher
```

UNVERIFIED platform notes were macOS group semantics, old-kernel watcher fallback,
free-threaded 3.14t, and Windows Proactor. Those notes and all numeric defaults remain
historical evidence, not current source claims.

## Appendix G — focused scenario assertions

The historical fake-worker suite also tested a reader receiving a 200,000-byte line,
then the same line with the default 64 KiB limit. The first path proved the explicit
spawn limit; the second proved `ValueError` was caught and the reader remained alive.
Another canary sent a valid result followed by a burst of non-critical tool events and
asserted that the critical DB row existed before a `Session.events()` subscriber saw it.
This tied queue admission, writer fsync, and subscriber publication into one observable
contract.

The watchdog race canary exited immediately after `ready`, leaving a stale heartbeat.
It asserted `poll()` prevented a kill of an already-dead process, and that an attempted
`proc.kill()` raising `ProcessLookupError` did not terminate the monitor task. The
shutdown canary called `shutdown()` mid-tool and checked cancel/group signals, reaping,
critical `session_ended`, pruning, and cancelled result JSON within a bound. A restart
canary crashed deterministically enough times to exercise both the five-in-60 burst cap
and ten-attempt absolute cap, then checked the typed failure reason.

## Appendix E — liveness escalation and reader behavior

Reader behavior was deliberately layered. `StreamReader.readline()` buffered a partial
line until newline; at EOF it returned a torn tail, which JSON parsing classified as
`parse_error`. A line over the configured 1 MiB limit was consumed to the next newline
and dropped. stderr was read separately, capped and rate-limited, and could never block
stdout protocol or determine task state. A worker's stdout EOF started a grace timer,
then a process poll; only a live process with no pong escalated to group kill. A process
that emitted an authoritative exit was not restarted merely because a reader saw EOF.

The drain-deadline monitor watched the supervisor side, not a worker heartbeat. If CPU,
subscriber backpressure, or a stalled callback prevented the loop from draining a pipe,
it emitted `supervisor_stall` and suspended heartbeat enforcement for that worker. This
was the causal fix for DS-C1/DS-C2's false-kill cascade: workers should not be blamed for
the supervisor failing to read.

Restart delays used full jitter over `[0, base*2**attempt]`, bounded by a five-in-60
second burst cap and absolute ten-attempt cap. A generation bump happened before every
respawn; a stale worker's process group was killed at startup and worktree generation
file checked before any git operation. Provider outage (`AllProvidersFailed`) stayed
inside the worker patience path and never consumed this restart budget.

## Appendix F — thread ownership checklist

The writer thread owned the SQLite connection, WAL checkpoint, fsync, and event publish;
the loop owned process handles, timers, and `WorkerHandle` fields. `asyncio.to_thread`
callables were forbidden from touching handles or shared mutable state. Only immutable
event values crossed the boundary, already redacted. The logging QueueListener was a
separate advisory thread and never shared the event-store connection. These ownership
rules were intended to make race review mechanical: every field had one mutation context,
and every durable record had one writer.

## Appendix H — worktree boundary and task-group behavior

Every spawn used a private worktree path and process group. Git create/recover/prune
calls ran through `asyncio.to_thread`; a thread never received `WorkerHandle`. The
supervisor's `TaskGroup` owned one supervise task per worker, but a child failure was
handled one-for-one: sibling tasks stayed alive while the failed tree consumed its own
restart policy. A gate or merge failure therefore could not cancel unrelated workers.

The loop's task admission was bounded before process creation. A worker stdout flood
could fill its pipe only if the loop stopped reading; the drain-deadline monitor flagged
that as a supervisor stall. Event queue drops were counted and bounded; no DLQ or durable
overflow sink was assumed. A critical event that could not be admitted within its bound
entered a fatal store path rather than being silently lost.

These constraints were intended to be checked with fake workers and a real temporary
Git repository. They do not assert an OS sandbox, per-worker approval callback, or
dynamic task decomposition in the current source.

Custos admission was deliberately separate from Architectus planning. Architectus
submitted a validated node and received an admission token; Custos then created/recovered
the worktree, started the process group, sent `init`, and waited for `ready`. A process
could not self-admit through a stdout message, and a `task_decomposed` line could not
create a worker without a new tree validation wave. This was the deterministic boundary
that kept dynamic model output from changing process ownership.

## Appendix I — cancellation and shutdown order

The proposed shutdown sequence was intentionally ordered. First stop new admission and
mark the session draining. Second send protocol `shutdown` to live workers, wait the
grace interval, then terminate their process groups and escalate to `SIGKILL` after the
term interval. Third drain stdout/stderr until the deadline, reap every process, and
fence the generation. Fourth flush critical events and close the writer thread. Only
after those steps could worktrees be pruned and `result.json` published. A cancellation
flag did not bypass reaping or durable `session_ended` evidence.

The loop treated `CancelledError` as control flow. A worker cancellation emitted a
typed cancelled result and preserved the supervisor task's cancellation for its caller;
it did not convert cancellation into a successful envelope. A callback failure in a
subscriber was isolated from process supervision, while a writer failure was fatal for
critical admission. These distinctions prevented cleanup exceptions from masquerading
as worker crashes.

The task-group plan was one-for-one at the tree boundary. A failed supervise task
reported its task and generation, consumed only that node's bounded restart budget, and
left siblings running. A root-level shutdown cancelled the group deliberately and
waited for all child tasks. This behavior was a proposal tied to DS-C1/DS-C2 and did not
claim that the current flat `run_plan` has task-tree scheduling.

## Appendix J — source gaps kept explicit

The draft identified missing or unverified seams instead of filling them with fallbacks:
the merged scaffold did not yet have a single writer, bounded event admission,
generation fencing, or complete shutdown choreography; `Session.events()` and worktree
recovery were design surfaces; and platform-specific child-watchers were untested.
The historical verification used CPython 3.14.7 on Linux only. A future implementation
must rerun line-limit, process-group, cancellation, and writer-failure checks on the
actual source revision before promoting any numeric default or portability claim.

## Appendix K — ownership of blocking operations

Git status, worktree creation, recovery, pruning, merge inspection, and WAL checkpoint
were all classified as blocking operations. The proposal routed them through
`asyncio.to_thread` and passed immutable paths/SHAs, never live process handles. Pipe
reads stayed on the loop with explicit byte limits; provider calls stayed in workers.
This split was the causal response to DS-C1: a synchronous disk or Git call could stop
reads, fill stdout, trigger a false heartbeat failure, and then cause an unnecessary
restart. The source and tests remained the authority for which calls still block.

The event loop also owned timer callbacks for ready, heartbeat, EOF grace, drain, wall,
and shutdown deadlines. Timer callbacks re-read process state before acting, so a late
heartbeat or stale callback could not kill a reaped generation. This was a proposed
loop-affinity invariant alongside single-writer persistence; it was not a guarantee that
the current runtime implements every timer.

No timer callback was allowed to call an LLM or make a workflow decision.

Timer callbacks emitted typed observations or requested deterministic process actions;
Architectus remained the owner of task-content policy.

Custos never inferred policy from model text.

The writer thread published only redacted immutable events. Subscribers could observe a
critical record only after its durable append boundary.

The current runtime remains the authority.

This document records proposed ownership only.

Platform claims remain unverified.

Numeric defaults are historical.

Recheck on source change.

Loop ownership stays narrow.

Historical only.

Historical review identifier retained: `C2`.
