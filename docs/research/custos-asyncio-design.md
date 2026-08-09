# Custos — Event-Loop Architecture

**Author:** csp research worktree
**Date:** 2026-08-09
**Status:** Design spec. Resolves the asyncio scaffolding gap for the deterministic supervisor (M4 Custos).
**Scope:** Event-loop vs thread vs subprocess split, I/O strategy, shared-state discipline, coordinated shutdown, and the delta from the merged `src/cambium/orchestrator.py` skeleton.

## 0. What this doc settles

Three review findings drive this design:

| ID | Finding | Resolution |
|---|---|---|
| DS-C1 | Synchronous file I/O in the asyncio event loop → backpressure cascade (pipes fill, workers stall, heartbeat monitors false-kill, thundering herd) | The event loop never performs disk I/O. Event persistence lives on a single dedicated writer thread; the loop only does `queue.put_nowait()`. All pipe reads are loop-native `StreamReader` awaits (verified non-blocking). |
| DS-M2 | Logical races on `WorkerHandle` at `await` boundaries (heartbeat monitor kills a worker that already emitted `result`; `ProcessLookupError` uncaught in the watchdog) | `WorkerHandle` is loop-affine: only loop tasks mutate it. Each state transition is a guard-check + mutate in one synchronous block with no `await` between. The watchdog catches `ProcessLookupError` (verified: `proc.kill()` on a reaped process raises it). |
| DS-M3 / IMPL-C8 | Event log has no durability; "append" is not "fsync"; orchestrator `await`s sync methods | Single-writer SQLite WAL on a dedicated thread with explicit fsync cadence (architecture §6.2). Sync work (git, blocking calls) escapes the loop via `asyncio.to_thread`. |

**Verification rule:** every stdlib claim below was checked against real CPython 3.14.7
(`uv run --python 3.14.7 ...`). See §7 for the command log. Anything not verified is flagged `UNVERIFIED`.

---

## 1. Component map

Three execution contexts plus one library-internal helper:

| Context | Count | Owns | Never owns |
|---|---|---|---|
| **Event loop** (single thread) | 1 | All Custos orchestration: worker spawn, IPC read/write, heartbeats, liveness, restart, event enqueue, subscriber fan-out, shutdown choreography | Disk writes, blocking syscalls, cross-`await` shared state |
| **Event-log writer thread** | exactly 1 | The sole SQLite WAL connection, fsync cadence, publish-to-subscribers handoff | `WorkerHandle` state, process objects, any loop object |
| **Worker subprocesses** | N (config) | Their own ReAct loop, worktree, generation token | Nothing inside the supervisor process |
| **asyncio child watcher** | library | SIGCHLD/pidfd reaping of workers | — |

### 1.1 On the event loop

- **Worker spawn / termination**: `asyncio.create_subprocess_exec(...)`; `proc.kill()` / `proc.terminate()` / `await proc.wait()`.
- **IPC readers**: one task per worker for stdout (`readline()` on `proc.stdout`) and one for stderr.
- **IPC writer**: supervisor→worker via `proc.stdin.write(...)` + `await proc.stdin.drain()`.
- **Timers / heartbeat**: one watchdog task per worker (periodic sleep + synchronous check-and-act), plus the EOF grace timer, the drain-deadline monitor, and the jittered restart backoff.
- **Task scheduling**: per-worker supervise tasks under an `asyncio.TaskGroup`; `Unio` merge sequencing under its `asyncio.Lock`.
- **Event enqueue**: `queue.Queue.put_nowait()` (non-blocking) into the writer thread's queue; critical-event overflow handled with a timeout-bounded put.
- **Subscriber fan-out**: one `asyncio.Queue` per `Session.events()` consumer.
- **Worktree recovery orchestration** (`Surculus`): the *decision* sequence runs here; the *git* calls are delegated to `asyncio.to_thread`.

### 1.2 On dedicated threads

- **Event-log writer thread** — single consumer of the bounded `queue.Queue`. Opens the SQLite connection, applies redaction, INSERTs, runs the batched `PRAGMA wal_checkpoint(TRUNCATE)` + `os.fsync(wal_fd)` cadence, and forwards events to loop subscribers via `loop.call_soon_threadsafe`.
- **Logging `QueueListener` thread** (stdlib `logging`, architecture §13) — out of scope of the event store; noted for completeness.

### 1.3 Where the task-template split differs (and why)

The task template suggested `stdout/stderr pipe readers` and `subprocess wait` live on dedicated threads. This design places them on the loop, with verification:

| Template suggestion | This design | Verified evidence |
|---|---|---|
| stdout pipe readers on a thread | **On the loop** via `StreamReader` | `asyncio.create_subprocess_exec` returns loop-native `StreamReader`s; reading is a transport-fed await, never a blocking syscall. `asyncio.create_subprocess_exec` does **not** expose the raw pipe fd to hand to a thread (no such accessor), so a thread-based reader would require abandoning `StreamReader` entirely. |
| stderr pipe readers on a thread | **On the loop**, rate-limited + size-capped, advisory-only | Same `StreamReader` mechanism; verified stderr lines and a torn tail are delivered like stdout. Keeping it on the loop preserves the single-I/O-discipline and avoids fd extraction. It is strictly advisory (architecture §5.1.5): overflow is dropped and counted, never allowed to stall anything. |
| subprocess wait on a thread | **On the loop** via the asyncio child watcher | Linux 3.14 default child watcher is `_PidfdChildWatcher` (verified): reaping is pidfd/loop-driven, `proc.wait()` is a coroutine and never blocks the loop. A user wait thread (`os.waitpid` in a thread) is redundant work. |

The thread budget is therefore: **1 user thread (event-log writer) + N subprocesses**. Everything else is the loop.

### 1.4 Worker spawn contract (normative)

```python
proc = await asyncio.create_subprocess_exec(
    *septum.wrap([sys.executable, "-X", "utf8", "-u", worker_script]),
    stdin=asyncio.subprocess.PIPE,
    stdout=asyncio.subprocess.PIPE,
    stderr=asyncio.subprocess.PIPE,
    limit=1_048_576,                       # StreamReader line limit (see §2.2)
    cwd=worktree_path,
    env={**os.environ, "PYTHONUNBUFFERED": "1",
         "CAMBIUM_TASK_ID": task_id, "CAMBIUM_GENERATION": str(generation)},
    start_new_session=True,                # own process group -> killpg
    pass_fds=(), close_fds=True,
)
```

`start_new_session=True` is verified to make the child its own process-group leader (pgid == pid), which is what makes `os.killpg(pgid, ...)` in the shutdown sequence and the C2-(a) grandchild-handling possible.

---

## 2. I/O strategy

### 2.1 Pipe readers never block the loop

- All reads go through `StreamReader` on the loop. `readline()`/`readuntil()` are awaits driven by the transport as data arrives; the loop is not blocked while a pipe is idle.
- **Partial-line buffering for NDJSON** (DS-C2 mode c): a worker killed mid-`write()` leaves a torn final line. Verified: `readline()` delivers the partial tail at EOF as a final line (no trailing `\n` required). `json.loads` failure on that line → log a `parse_error` event, skip, count it. The `result` case is not lost: `result` is persisted to the checkpoint store *before* it is emitted (architecture §5.4-c), so a torn `result` line is recovered from the checkpoint at the next watchdog tick.
- **Pipe starvation**: a `StreamReader` that fills its transport buffer stops reading; the worker then blocks on `write()` — which is exactly the DS-C1 cascade trigger. The loop cannot stall on disk (writer thread owns all disk I/O), so the only starvation source left is a CPU-bound loop task; the per-worker **drain-deadline monitor** (architecture §5.3) flags supervisor-side stalls and suspends heartbeat enforcement instead of blaming the worker.

### 2.2 Line limits (verified: the default limit bites)

The internal `StreamReader` limit defaults to 64 KiB (`_DEFAULT_LIMIT == 65536`, verified). A line that exceeds the limit raises `ValueError` (`"Separator is not found, and chunk exceed the limit"`) — an unguarded `readline()` would crash the reader task.

Mitigations (both are required):

1. **Raise the limit at spawn**: `asyncio.create_subprocess_exec` accepts a `limit` kwarg (verified in the 3.14.7 signature, before `**kwds`), which flows through to the internal `StreamReader`. Verified: a 200 000-byte line reads cleanly with `limit=1_000_000`. Custos passes `limit=1_048_576`.
2. **Cap payloads in the protocol contract** (`Nuntius`): `cmd` is truncated to 120 chars, `summary` to 2 k chars, stderr lines size-capped at the reader. An over-limit line is caught (`ValueError`), logged, skipped, counted — never fatal.

### 2.3 Sync work escapes the loop

- `asyncio.to_thread` is verified present on 3.14.7 and executes its callable on a different OS thread. It is used for the rare blocking calls: `git` invocations in `Surculus` (worktree create/recover/prune), `Unio` merge git ops, and any synchronous `Path` I/O the deterministic layer needs. Invariant: nothing inside a `to_thread` callable touches `WorkerHandle` or any shared mutable state (architecture §14).
- The event loop never calls `open()`, `write()`, `fsync()`, `sqlite3`, or blocking `git` directly. DS-C1 is structurally impossible.

### 2.4 Single-writer discipline for the event store

The architecture §6.2 invariants are adopted verbatim and made precise:

1. The loop enqueues with `queue.Queue.put_nowait()` — non-blocking, always.
2. **Non-critical events** (`heartbeat`, `tool_event`, `log`, ...): on `queue.Full`, the writer drops the oldest non-critical event, logs a `drop` marker, and increments a counter. The loop never blocks.
3. **Critical events** (`result`, `checkpoint`, `worker_exit`, `task_failed`, `merge_progress`, `task_assigned`, `merge_committed`): never dropped. On overflow the producer performs a timeout-bounded blocking put: `await asyncio.to_thread(partial(q.put, event, timeout=0.1))`. These are rare; a bounded 100 ms wait is acceptable.
4. The writer thread is the only holder of the SQLite write connection. `PRAGMA synchronous=NORMAL`; batched `wal_checkpoint(TRUNCATE)` + `os.fsync(wal_fd)` every `fsync_interval_s` (default 1 s); the same sequence runs synchronously when a critical event is dequeued (architecture §6.5).
5. The writer thread publishes to subscribers through `loop.call_soon_threadsafe(subscriber_q.put_nowait, event)` — verified to deliver across threads. Subscribers see events in monotonic order; critical events are fsync-d before they are published.
6. Redaction (§12) is applied at enqueue time on the loop, before the event crosses into the writer thread.

Queue inventory:

| Queue | Type | Producer | Consumer | Bounded? |
|---|---|---|---|---|
| Event store | `queue.Queue` | loop (`put_nowait`) | writer thread | yes (10 000) |
| Subscriber streams | `asyncio.Queue` | writer thread (via `call_soon_threadsafe`) | loop (`Session.events()`) | yes (config) |
| Worker stdin | pipe (`StreamWriter`) | loop | worker process | OS pipe |

The `queue.Queue` ↔ `asyncio.Queue` handoff point is precisely the writer thread's publish step (row 2). No other cross-thread queue exists in Custos.

---

## 3. Shared-state discipline

### 3.1 `WorkerHandle` state machine — loop-affine only

States (architecture §7.1): `PENDING → SPAWNING → RUNNING → (DONE | FAILED | REJECTED | CRASHED → restartable → SPAWNING)`.

Rules:

1. **Loop-affinity**: only event-loop tasks may read or mutate `WorkerHandle` fields (`state`, `last_heartbeat`, `proc`, `crash_times`, `generation`, `result`). No thread ever receives a handle reference; threads only receive immutable `Event` values.
2. **Atomic transitions**: every transition is a guard-check + mutation executed in a single synchronous block with **no `await` between check and set**. asyncio has no preemptive switching between awaits, so two loop tasks cannot interleave a check-and-set pair. This is the mechanism that makes DS-M2's logical races structurally impossible rather than merely rare.
3. **One mutation site per field**: `last_heartbeat` and `state` are updated together by the stdout reader (a heartbeat implies `RUNNING`); the supervise task and the watchdog read them but do not set them except through the same transition helpers.

### 3.2 DS-M2, resolved at the source

The reviewed race was: heartbeat monitor wakes, reads a stale `last_heartbeat`, and kills a worker that has already exited. With loop-affine state this remains *possible* as a cooperative interleaving (reader task may be scheduled after the monitor), so the design adds three guards:

- **Re-check before kill**: the watchdog calls `proc.poll()` and, if the process has already exited, skips the kill and lets the supervise task complete.
- **Catch `ProcessLookupError`**: verified that `proc.kill()` on an already-reaped asyncio process raises `ProcessLookupError`. The watchdog wraps the kill in `try/except ProcessLookupError` and treats it as "already dead" (this was the latent crash in the v0.1 `_heartbeat_monitor`, line 591).
- **The reader wins the tie**: the reader sets `last_heartbeat` + `state = RUNNING` in one synchronous step; the monitor's guard reads both fields in one synchronous step. No await between either read or write, so a torn observation is impossible.

### 3.3 Messages between loop and threads

| Direction | Mechanism | Verified? |
|---|---|---|
| loop → writer thread | `queue.Queue.put_nowait` (non-critical); `asyncio.to_thread(q.put, ev, timeout=0.1)` for critical overflow | yes (`queue.Queue`, `queue.Full`, `asyncio.QueueFull` present; `to_thread` runs off-loop) |
| writer thread → loop | `loop.call_soon_threadsafe(sub_q.put_nowait, event)` | yes |
| loop → worker | `proc.stdin` | yes |
| worker → loop | `StreamReader` on stdout/stderr | yes |

Only immutable, already-redacted `Event` dataclasses cross any thread boundary. There are no locks on Custos state (the only `asyncio.Lock` in the deterministic layer is `Unio`'s merge lock, architecture §7.8).

---

## 4. Cancellation / shutdown — precise sequence

Coordinated shutdown, steps 1–10. The choreographer is a single loop task; every worker-facing step is bounded by a timeout so shutdown cannot hang.

1. **Stop intake.** Set `self._shutdown = True`. The orchestrator stops dispatching new tasks; `submit()` returns a rejected result for anything after this point. No new worktrees are created.
2. **Send `cancel` to live workers.** For every handle in `RUNNING | READY | SPAWNING`, write `{"type":"cancel","request_id":...,"reason":...}` to `proc.stdin` (best-effort; swallow `BrokenPipeError` for already-dead workers). Start the per-worker grace window.
3. **Wait for cooperative exits.** For each worker, `await asyncio.wait_for(worker_exit_event, timeout=graceful_s)` (default 10 s). A healthy worker emits `exit` (authoritative termination, architecture §5.3) and its supervise task completes normally. Workers that comply never see a signal.
4. **SIGTERM the stragglers' process groups.** For each worker still alive: `os.killpg(handle.pgid, signal.SIGTERM)`. Killing the *group* (not just the pid) is mandatory — `start_new_session=True` means pgid == child pid, and grandchildren die with the worker (DS-C2 mode a). `await asyncio.wait_for(proc.wait(), timeout=term_grace_s)` (default 5 s).
5. **SIGKILL what remains, then reap.** For each worker still alive: `os.killpg(handle.pgid, signal.SIGKILL)`; then `proc.kill()` (verified method) as belt-and-braces; then `await proc.wait()` — the `wait()` reaps the zombie and sets `returncode`. Catch `ProcessLookupError` (verified: raised on a reaped process). Every subprocess is reaped; nothing is left to PID 1.
6. **Cancel loop tasks.** Cancel and drain the remaining per-worker tasks (stdout/stderr readers, watchdogs, EOF-grace timers) with `task.cancel()` + `await asyncio.gather(..., return_exceptions=True)`, or by exiting the `TaskGroup` scope. `CancelledError` is expected and swallowed here.
7. **Drain subscriber queues.** Stop feeding the per-consumer `asyncio.Queue`s; flush what remains to attached `Session.events()` consumers (best-effort, bounded), then close them. No consumer is left waiting forever.
8. **Flush the event log and stop the writer.** Enqueue a final **critical** `session_ended` event, set the writer's shutdown flag, and wait for the writer to drain and ack (via a `call_soon_threadsafe`-delivered done event). The writer performs a final `wal_checkpoint(TRUNCATE)` + `os.fsync(wal_fd)` and closes the SQLite connection. Postcondition: every critical event is durable (architecture §6.5).
9. **Worktree cleanup.** `Surculus.prune()` removes stale `.git/worktrees/` entries (DS-N7); active worktrees are removed or quarantined per policy; `gc.auto=0` is already set on the managed repo. This runs before the writer thread stops only if it must be observed in the log; otherwise it runs now, after the log is durable.
10. **Close the session contract.** Write `result.json` atomically (temp + rename) with `status="cancelled"`, exit code 4, and `failure_reason`; write final `status.json`; close the logging `QueueListener`; return from `shutdown()`.

Verification hooks: after step 5, a smoke assertion checks every handle's `proc.returncode is not None` (all reaped). After step 8, a smoke assertion checks the DB has the `session_ended` row.

---

## 5. The scaffolding gap — deltas to the merged skeleton

Read: `src/cambium/orchestrator.py` (59 lines) and `src/cambium/events.py` (47 lines). The skeleton is a placeholder: a single `Orchestrator` class with an `asyncio.Queue` of task specs, a counter-based `task_id`, a serial `submit → run → _emit` loop, and no supervisor, no subprocesses, no event store, no shutdown. Concrete deltas to reach this design (spec only, no code):

1. **Split facade from supervisor.** Introduce a `Custos` component that owns the `WorkerHandle` table, the event-log writer thread, and the restart policy. `Orchestrator` (Architectus) keeps decomposition/routing/evaluation and calls `Custos.run_task(spec) -> Result`. The skeleton's single class conflates both layers.
2. **Replace the counter task-id scheme.** `self._next_task_id` + `task-{n}` is replaced by ULID task ids issued by Custos, plus a per-task monotonic `generation` (fencing token, architecture §7.3). Counter-based ids cannot fence and invite the DS-M2 class of identity confusion.
3. **Replace the serial drain loop.** `run()`'s `while not self._queue.empty(): ... await _emit(...)` becomes concurrent supervision: each task runs its own supervise task inside an `asyncio.TaskGroup`. A crashed task must not cancel siblings (Erlang one-for-one). `run()` returns the aggregated `Result` and writes `result.json` atomically.
4. **Route events to the writer, not to callbacks.** `_emit` currently awaits a caller callback on the loop. Callbacks become subscribers of `Session.events()`; the loop's event path becomes `queue.Queue.put_nowait` (critical events via the bounded put). No event path may touch disk.
5. **Upgrade the event schema.** `events.py` ships four flat types; the design needs the architecture §3.6 `Event` (`kind`, `task_id`, `request_id`, `timestamp`, `monotonic_ms`, `generation`, `payload`), frozen, with a critical/non-critical tier classifier and enqueue-time redaction. `WorkerStarted`/`WorkerFinished` map onto `worker_spawned` / `worker_exit` kinds.
6. **Add the event-log writer.** A single consumer thread holding the SQLite WAL connection, fsync cadence, and `call_soon_threadsafe` publish path (§2.4). The skeleton has no persistence at all.
7. **Add worker spawn + the init handshake.** `create_subprocess_exec` with the §1.4 contract (pipes, `limit`, `start_new_session`, `pass_fds=()`, `close_fds=True`, `PYTHONUNBUFFERED=1`), then `init` → wait for `ready` under `ready_timeout` (default 60 s). The skeleton spawns nothing.
8. **Add the supervision machinery.** stdout reader (partial-line-safe, §2.1), stderr reader (rate-limited, size-capped), heartbeat watchdog with `ProcessLookupError` guard and `poll()` re-check, EOF-grace timer, drain-deadline monitor, and the jittered restart policy (burst cap + absolute cap + per-task wall budget, architecture §7.4). None of this exists in the skeleton.
9. **Add `Surculus` integration.** Before every spawn: worktree create (first) or recover (restart: lock cleanup, rebase/merge abort, `reset --hard`, `clean -fd`, generation bump, optional checkpoint cherry-pick; quarantine on failure), each `git` call via `asyncio.to_thread`. Shutdown calls `prune()`. The skeleton has no worktree concept.
10. **Add `shutdown()` and the async context surface.** `__aenter__`/`__aexit__` running the §4 sequence; `cancel(reason)`, `events()`, `query()`. The skeleton has no shutdown and no context-manager entry points.
11. **Wire `Unio` after results.** On task completion, submit the branch to the merge sequencer (single `asyncio.Lock`, throwaway worktree, test gate, atomic `update-ref` publish, architecture §7.8). The skeleton emits `WorkerFinished` and stops.
12. **Keep the smoke gate.** The scaffold's `tests/` discipline (a real dataset, no mocking, one end-to-end run) is extended with the scenario tests in §6. The scenario suite is the v0.1 lesson made into a CI gate: the reviewed code samples had ~12 syntax/name bugs because nothing was ever executed (review-implementation.md verdict).

---

## 6. Test scenarios

There is **no test-strategy document in `main`** at this commit (`docs/` contains only `research/`, `reviews/`, `system-design.md`). The harness strategy lives in the un-merged arch worktree: `docs/architecture/module-template/architecture.md` §9 (unit tests, eval harness, canary suite, integration smoke test, sibling pinning) and the scaffold's `tests/scenarios/test_example_module.py` precedent (real fixtures, no mocks). The scenarios below slot into that strategy's §9.4 integration tier and extend the smoke test.

All scenarios use a **fake worker** (`tests/fixtures/fake_worker.py`): a script that speaks canned NDJSON over stdout at a scripted pace, controllable via env vars — no DSPy, no network, deterministic.

1. **Partial-line and parse-error recovery** (DS-C2 mode c). Fake worker emits `ready`, a `heartbeat`, then a torn `result` line without a trailing newline, and exits 0. Assert: the reader delivers the partial tail at EOF (verified behavior), the supervisor logs a `parse_error` event, does **not** crash, and the supervise task completes `DONE` with `exit` message cross-check.
2. **Over-limit line guard** (§2.2). Fake worker emits a single 200 000-byte line. Assert with `limit=1 MiB` at spawn: the line is read whole and processed. Assert the guard path: an unguarded default-limit reader raises `ValueError`; Custos catches it, logs, skips, counts, and the loop stays responsive (the heartbeat watchdog still fires on schedule).
3. **Event-loop non-blocking regression** (DS-C1). High-volume `tool_event` burst (e.g., 5 000 events) from one fake worker while a second fake worker runs a long tool emitting heartbeats. Assert: the second worker's heartbeat never false-trips, no pipe ever fills (the writer queue bounds and drops non-critical events with a `drop` counter), and wall-clock latency of a probe `ping` stays under a threshold.
4. **Watchdog vs exited worker** (DS-M2). Fake worker exits cleanly at T+0 immediately after `ready`. The watchdog's next tick computes a stale `last_heartbeat`. Assert: `poll()` re-check prevents the kill, `ProcessLookupError` (if the kill is attempted) is caught, the monitor task does **not** die, and the supervise task completes `DONE`.
5. **Coordinated shutdown** (§4). Start a task with a slow fake worker, call `shutdown()` mid-run. Assert the 10 steps in order: worker receives `cancel` or group SIGTERM/SIGKILL within bounds, every `proc.returncode` is set (all reaped — no zombies), the `session_ended` critical row is durable in the DB, `worktree prune` ran, `result.json` says `cancelled`, and `shutdown()` returns in bounded time.
6. **Thread→loop handoff** (§2.4). Fake worker emits a `result` (critical) followed by a burst of `tool_event`s. Assert: a `Session.events()` subscriber receives events in monotonic order; the `result` event is present in the SQLite DB *before* the subscriber's copy is observed (fsync-before-yield, architecture §6.5).
7. **Restart policy with jitter and caps** (DS-C4). Fake worker crashes (`exit 1`) N times with a deterministic seed. Assert: restart delays are full-jitter within `[0, base * 2^n]`, the burst cap (5 in 60 s) and absolute cap (10) both engage, and the task ends `FAILED` with the correct `failure_reason`.
8. **Generation fencing across restart** (DS-C6). First worker exits mid-task; on respawn Custos bumps `generation`. The fake worker is scripted to read its generation and refuse to run with a stale token. Assert: the restarted worker accepts the new generation, and a simulated orphan (an extra subprocess holding the worktree's `.cambium/generation`) self-terminates on mismatch.

Each scenario asserts loop liveness (a concurrent probe task completes), worker reaping, and event-log durability — the three invariants this design exists to protect.

---

## 7. Verification appendix (real commands, CPython 3.14.7)

Run from `/tmp/opencode/cambium-csp` with `uv run --python 3.14.7`. `uv` resolved `cpython-3.14.7-linux-aarch64-gnu` (the pinned GIL build).

### 7.1 Required checks

```
$ uv run --python 3.14.7 python -c "import asyncio; print(hasattr(asyncio, 'create_subprocess_exec'))"
True

$ uv run --python 3.14.7 python -c "import asyncio; print(hasattr(asyncio, 'to_thread'))"
True
```

### 7.2 Behavior checks (script: `/tmp/opencode/verify_custos.py`)

| Behavior | Result |
|---|---|
| `readline()` delivers a partial final line at EOF (no trailing `\n`) | lines = `['{"type":"ready"}', '{"type":"heartbeat","turn":1}', '{"type":"tool_event","cmd":"sleep"}']`, rc = 0 |
| `readline()` on a line > 64 KiB default limit | `ValueError: Separator is not found, and chunk exceed the limit` |
| `create_subprocess_exec(..., limit=1_000_000)` | 200 000-byte line read whole (200 001 bytes incl. `\n`) |
| `proc.terminate()` → `await proc.wait()` | returncode = −15 (SIGTERM visible) |
| `proc.kill()` on an already-reaped process | `ProcessLookupError` raised |
| `start_new_session=True` | child pgid == child pid (own process-group leader) |
| `os.killpg(pgid, SIGKILL)` | process group dies; `wait()` returns −9 |
| `asyncio.wait_for(proc.wait(), timeout=0.5)` | `TimeoutError` after 0.50 s; `returncode` stays `None` until `wait()` completes |
| `asyncio.Queue(maxsize=2).put_nowait(3)` | `asyncio.QueueFull` raised |
| thread → loop handoff | `loop.call_soon_threadsafe(sub_q.put_nowait, ev)` delivers; loop drains `queue.Queue` via `run_in_executor(None, q.get)` |
| stderr `StreamReader` | lines + torn tail delivered exactly like stdout; rc = 0 |
| default child watcher (Linux 3.14) | `_PidfdChildWatcher` — reaping is loop-driven, `proc.wait()` is a coroutine |
| `os.killpg` | present on Linux |

### 7.3 UNVERIFIED / platform notes

- **macOS process-group semantics** (`os.killpg`, `start_new_session`) — `UNVERIFIED` (this box is Linux; `Septum` owns platform abstraction; macOS behavior must be re-verified on a Mac).
- **`_PidfdChildWatcher` fallback** on kernels < 5.3 — `UNVERIFIED` here (modern kernel). Custos must not depend on pidfd specifics; the `proc.wait()` API is identical under the threaded-watcher fallback.
- **Free-threaded build (3.14t)** — `UNVERIFIED`; the architecture pins the GIL build (architecture §14), and this design targets it.
- **Windows Proactor loop** — out of scope (Linux/macOS dev targets).

---

## 8. Sources

- `/home/ubuntu/cambium/docs/architecture/reviews/review-distributed-systems.md` — DS-C1 (§1, §2), DS-M2 (§3), DS-M3 (§2.4).
- `/home/ubuntu/cambium/docs/architecture/system-design.md` §M4 (lines 397–643) — the reviewed `Supervisor` (this doc replaces its I/O and liveness model).
- `/tmp/opencode/cambium-arch/docs/architecture/architecture.md` (v2.0.0, arch worktree, pending merge into main) — §5.3 liveness model, §6 event-log writer (§6.2) and durability contract (§6.5), §7.1 state machine, §7.2 spawn, §7.3 fencing, §7.4 restart, §7.5 worktree recovery, §7.7 shutdown, §7.8 Unio publish, §13 logging, §14 Python stance (`asyncio.to_thread`).
- `/tmp/opencode/cambium-arch/docs/architecture/module-template/architecture.md` §9 — the test-strategy template this doc's §6 extends (not yet in `main`).
- `/tmp/opencode/cambium-csp/src/cambium/orchestrator.py`, `src/cambium/events.py` — the merged skeleton this design must grow into (§5).
