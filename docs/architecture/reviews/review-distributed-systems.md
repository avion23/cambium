# Cambium — Distributed Systems Review

**Reviewer:** Distributed Systems Perspective
**Date:** 2026-08-10
**Document reviewed:** `SYSTEM_DESIGN.md` v0.1.0-draft

---

## CRITICAL FLAWS

### C1. Synchronous file I/O inside the asyncio event loop — backpressure cascade kill-chain

**Location:** `Supervisor._log_event()` (M4, line ~439)

```python
def _log_event(self, event: dict):
    event["timestamp"] = time.time()
    self.event_log.append(event)
    with open(self.log_path, "a") as f:   # <-- blocking open()
        f.write(json.dumps(event) + "\n") # <-- blocking write()
```

This is the single most dangerous flaw in the design. Every call to `_log_event` performs **synchronous** `open()` + `write()` + `close()` directly inside the asyncio event loop. There is no `await`, no `asyncio.to_thread`, no `aiofiles`. This blocks the event loop thread for the full duration of each disk operation.

Because the event loop is single-threaded and cooperatively scheduled, while it is blocked on disk I/O:

- **No stdout pipes are drained.** Every `async for line in proc.stdout` reader across every worker is stalled.
- **Workers block on stdout writes.** The OS pipe buffer is 64 KB on Linux. When the supervisor stops reading, the buffer fills, and the worker's `sys.stdout.write()` + `.flush()` (inside `emit()`) **blocks** the worker process.
- **Heartbeats stop arriving.** Workers are frozen on stdout writes, so they cannot emit heartbeat messages.
- **Heartbeat monitors fire.** After 60s of stalled I/O, every heartbeat monitor independently decides its worker is dead and calls `handle.proc.kill()`.
- **All workers die simultaneously**, then all restart with identical backoff delays (no jitter — see C4), then all re-spawn, re-emit events, re-flood the log, and the cycle repeats.

This is a **positive-feedback cascade**: high event volume → slow disk → event loop stalls → pipes fill → workers stall → false heartbeat timeouts → mass kill → mass restart → more events → repeat. The system has no backpressure mechanism and no circuit breaker. A single burst of tool events from a chatty worker, or a momentary disk hiccup (NFS, EBS, SSD GC pause), can bring down the entire supervisor.

**The fix is not optional.** Event logging must be off the event loop critical path:
- Use `aiofiles` or `asyncio.to_thread(self._write_event, event)`.
- Batch writes: buffer events in memory, flush periodically or when buffer reaches N entries.
- Keep the file handle open (opening the file per-event is also a performance disaster — see M3).
- Critically: separate the "fast path" (in-memory append for ordering) from the "durable path" (batched fsync on a timer or background thread).

The design claims "Temporal-style durable execution." Temporal uses a database with WAL, dedicated writer threads, and async replication. A synchronous append to a file inside the event loop has **none** of Temporal's durability or performance properties.

---

### C2. "stdout EOF = worker dead" is unsound — at least four failure modes violate this invariant

**Location:** M1 Protocol Rules §5, M4 `_read_worker_output`

The design asserts: *"stdout closes = worker dead. EOF on stdout = process exit. Supervisor detects immediately."* This is presented as an axiom (line 622: "When stdout closes (EOF), the worker is dead. Period."). It is not an axiom; it is a heuristic with at least four known failure modes.

**(a) Grandchild processes hold the pipe open — zombie workers.**

When the worker spawns subprocesses via `run_shell` (shell=True) or `git_op`, Python's subprocess module sets `close_fds=True` by default (Python 3+), which closes inherited FDs in the child. **However**, the `capture_output=True` path redirects the subprocess's stdout/stderr to new pipes — it does not inherit the worker's stdout. So for captured subprocesses, this is fine.

The real danger is **any code path that spawns a process without `capture_output` or with `pass_fds`**, or any C extension / shared library that forks a daemon (language servers, LSP daemons, telemetry agents, IDE integration). If a grandchild inherits the worker's stdout FD, the worker process can exit but the pipe remains open. The supervisor blocks on `proc.stdout` read forever — **the worker is dead but EOF never arrives**. The heartbeat monitor is the only backstop, and as shown in C1, it can be defeated by event-loop stalls.

This is a classic Unix supervision gotcha. s6 and systemd explicitly handle it (s6 uses a notification FD + `PR_SET_PDEATHSIG`; systemd uses `notify` + cgroup tracking). Cambium has neither.

**(b) Python stdout buffering — silent data loss on SIGKILL.**

Python's default buffering mode for stdout when it is **not a TTY** (which it isn't — it's a pipe) is **block-buffered** (4 KB or 8 KB blocks), not line-buffered. The `emit()` function does call `sys.stdout.flush()`, so messages emitted via `emit()` are pushed to the OS. But:

- Any stray `print()` call, any library that writes to stdout directly (DSPy, LiteLLM, warning modules, `logging` misconfigured to stdout), or any C-level `printf` / `puts` in an extension writes to the same block buffer and is **lost on SIGKILL / segfault / OOM-kill**. The data sits in Python's userspace buffer, never reaches the OS, and is destroyed when the process is killed.
- If the worker is killed between `sys.stdout.write(json_str)` and `sys.stdout.flush()` inside `emit()` (they are two separate statements, not atomic), the message is lost — including potentially the `result` message. The supervisor sees EOF, assumes crash, and restarts a task that had actually completed.

**Fix:** set `PYTHONUNBUFFERED=1` or reopen stdout with `buffering=1` (line-buffered) in the worker. Better: use a dedicated FD (FD 3) for the protocol, leaving stdout free for library noise. The design explicitly says "stdout is never used for debug logging" but provides **no enforcement** — a Python convention with no mechanism behind it.

**(c) Partial writes corrupt the stream framing.**

If a worker is killed (SIGKILL, segfault) mid-`write()` syscall — after the kernel has accepted some bytes into the pipe buffer but the JSON line is incomplete — the supervisor reads a partial line. `json.loads()` fails, the line is logged and skipped (line 541). Then EOF arrives. This is handled gracefully **for the skipped line**, but:

- If the partial write was the `result` message, the result is silently dropped. The supervisor sees only EOF + a parse error, treats it as a crash, and restarts. The completed work (commits in the worktree) is lost from the supervisor's perspective — it doesn't know the task succeeded.
- There is no message-level checksum, no length prefix, no acknowledgment protocol. Newline-delimited JSON is fragile precisely because a truncated final line is indistinguishable from a corrupt line.

**(d) The supervisor itself can stall the pipe and create a false "worker is slow" signal.**

Per C1, if the event loop stalls, the supervisor stops reading stdout. The worker blocks on `write()`. From the supervisor's perspective, no messages arrive. This is indistinguishable from a hung worker. The heartbeat timeout fires. The worker is killed — not because it was unhealthy, but because **the supervisor failed to drain its own pipe**. The IPC protocol has no flow control, no backpressure signal, no "I'm alive but blocked on output" channel.

**Verdict on this claim:** EOF-on-stdout is a **necessary but not sufficient** liveness signal. It detects process exit but not process health, not grandchild FD leaks, and not supervisor-induced stalls. The design should add: (1) `SOCK_CLOEXEC` / `close_fds` enforcement on all worker-spawned subprocesses, (2) `PYTHONUNBUFFERED=1`, (3) a dedicated protocol FD separate from stdout, and (4) a "drain deadline" — if the supervisor hasn't read from a worker's pipe in N seconds, log a supervisor-side stall warning rather than blaming the worker.

---

### C3. Heartbeat granularity is far too coarse — false-positive kills on long-running tools

**Location:** M4 `_heartbeat_monitor`, M5 heartbeat emission

The heartbeat monitor kills workers that miss the 60s heartbeat timeout (line 580-591). Heartbeats are emitted "after each tool call" via `on_step_end_callback` (line 741-746). The problem: **tools can run far longer than 60 seconds**, and no heartbeat is emitted during tool execution.

Concrete examples from the design's own tool definitions:
- `run_shell`: timeout = **120 seconds** (line 674). A worker running `cargo build --release` or `pytest` will not heartbeat for up to 120s.
- `git_op`: timeout = 30s (line 682). Usually fine, but `git gc` or large rebases can exceed.
- DSPy LLM calls via FanOut: `timeout = 30s` per provider (line 199), but in cascade mode with 4 providers, worst case is 4 × 30s = 120s.

A worker that starts a 90-second `cargo build` at turn 3 will be killed at the 60s mark by the heartbeat monitor. The worktree may be left with a locked `index.lock` (see C5), partial build artifacts, or a half-written git operation. The supervisor then restarts the worker, which resumes from the last checkpoint — and may immediately re-attempt the same long build, creating a **restart loop on long tools** that the intensity/period limiter may or may not catch (see C4).

**This is an architectural mismatch:** the liveness signal (heartbeat per tool call) has a coarser granularity than the operation granularity (tools that run for minutes). The fix is to emit heartbeats **from within long-running tools** — e.g., `run_shell` should heartbeat every 15s while the subprocess runs, or the worker should run a background heartbeat thread. The design does neither.

---

### C4. Restart policy: no jitter → thundering herd; rate-window gaming → unbounded restarts

**Location:** M4 `RestartPolicy` (line 403-420)

Two distinct flaws:

**(a) No jitter — deterministic thundering herd.**

`get_delay()` returns `restart_delay * (backoff_base ** len(crash_times))` = `1.0 * 2^n` = {1, 2, 4, 8, 16, 32} seconds, with **zero randomization**. If a systemic event kills all workers simultaneously (OOM killer under memory pressure, shared dependency failure, disk-full event, network partition to LLM provider), every worker's `_supervise_worker` loop independently computes the same delay and restarts at the same instant.

Consequences of synchronized restart:
- **N concurrent `git worktree` / process spawns** — git lock contention on `.git/config` and `.git/worktrees/`.
- **N concurrent LLM API calls** — if the crash was caused by provider rate-limiting (FanOut cooldown), all workers hit the provider simultaneously after the cooldown window, re-triggering the rate limit.
- **N concurrent event-log writes** — exacerbating C1's event-loop stall.

Every production system that uses exponential backoff adds jitter (AWS recommends "full jitter" — `random.uniform(0, delay)`). The design omits it entirely. This is a textbook distributed-systems mistake.

**(b) Intensity/period window is defeated by slow crashes — no absolute restart cap.**

`should_restart()` filters `crash_times` to crashes within the last `max_period` (60s), then checks `len >= max_restarts` (5). A worker that crashes **once every 61 seconds** will always have `crash_times` trimmed to length ≤ 1 after filtering, so `should_restart()` always returns `True`. The worker restarts **forever**. There is no absolute cap on total restarts, only a burst-rate cap.

For a coding agent, a worker with a subtle bug (e.g., always crashes on a specific file pattern encountered once per minute) will restart indefinitely, consuming resources and generating noise. Erlang's intensity/period is designed for transient failures, not persistent ones. The design needs either an absolute restart ceiling or a total-cost/time budget per task.

---

### C5. Worktree locks survive worker crashes — stale locks block restarts and pollute the repo

**Location:** M3 `WorktreeManager`, M4 `_spawn_worker`

When a worker is killed mid-git-operation (SIGKILL from heartbeat timeout, OOM kill, or the false-positive kills from C3), it leaves behind:

- `worktree/.git/index.lock` — git index lock from an interrupted `git add` / `commit`.
- `repo_root/.git/worktrees/<id>/locked` — worktree lock from an interrupted `git worktree` operation.
- `repo_root/.git/refs/...` temporary lock files from interrupted ref updates.
- `.git/REBASE_HEAD`, `.git/rebase-merge/`, or `.git/rebase-apply/` from an interrupted rebase in the MergeSequencer.

On restart, `_spawn_worker` reuses `handle.worktree_path` (set once in `run_task`, never updated on restart). It spawns a new worker process in the **same worktree** with the **same stale locks**. The new worker's first git operation will fail with `fatal: Unable to create '<path>/.git/index.lock': File exists.`

The `WorktreeManager.remove()` method has a `force` parameter, but:
1. It's never called on restart — `_supervise_worker` loops directly back to `_spawn_worker` without cleanup.
2. `git worktree remove --force` doesn't clear `.git/index.lock` inside the worktree — it only forces removal of the worktree itself.
3. There is **no `git worktree prune`** call anywhere in the design. Stale worktree administrative entries in `.git/worktrees/` accumulate indefinitely.

**Fix:** Before re-spawning a worker into an existing worktree, the supervisor must run a recovery sequence: remove all `*.lock` files in the worktree's `.git`, abort any in-progress rebase/merge/cherry-pick, and reset the worktree to `base_commit`. This should be a dedicated `_recover_worktree(handle)` method called before every `_spawn_worker` on restart.

---

### C6. "Temporal-style durability" is unsupported — supervisor crash orphans all workers

**Location:** M4 `_log_event`, §6 "Crash recovery = replay the log"

The design claims event-log replay provides Temporal-style crash recovery. It does not. The event log is append-only metadata; it records **what happened**, not **how to reconnect to live state**. The critical gap:

**When the supervisor process crashes, all worker pipes break.** The supervisor's side of every stdin/stdout pipe is closed by the kernel on process exit. Workers writing to stdout receive `SIGPIPE` (default action: terminate) or `BrokenPipeError` on next write. Workers reading stdin get EOF.

Outcomes after supervisor crash:
- Workers that were mid-LLM-call try to emit a heartbeat or tool_event on stdout → `BrokenPipeError` → worker crashes (no handler shown) or hangs.
- Workers become **orphans** (reparented to PID 1). They may continue running for minutes, consuming CPU and memory, modifying their worktrees.
- On supervisor restart, "replay the log" cannot reattach to these orphaned workers — the pipe FDs are gone, the PIDs may have been recycled.
- The supervisor respawns workers from checkpoint into the **same worktrees** that the orphans are still modifying. **Two processes now write to the same git worktree simultaneously** → index corruption, conflicting commits, torn writes.

This is a **split-brain** condition: the supervisor believes it owns the worktree exclusively (it spawned a new worker there), but an orphaned worker is still making changes. Git's file-level locking (`index.lock`) may partially protect against simultaneous index writes, but uncommitted working-tree changes, untracked files, and build artifacts will conflict unpredictably.

Temporal avoids this because activities are stateless functions retried by the workflow engine — there is no long-lived process to orphan. Cambium's workers are long-lived stateful processes (ReAct loops with checkpoints), which is fundamentally different.

**Fix options (all significant):**
1. **Fencing token / generation counter:** Each worker spawn gets a monotonically increasing generation number. Workers check their generation against a shared file before every git operation; if mismatched, they self-terminate. The supervisor increments generation on respawn.
2. **Process group kill on startup:** Supervisor kills any process in the worktree's process group before respawning. Requires workers to be in their own process group (`os.setsid` / `start_new_session=True` in `create_subprocess_exec`).
3. **Fresh worktree per restart:** Don't reuse worktrees across restarts. Create a new worktree from `base_commit` (or from checkpoint). Higher disk cost but eliminates split-brain. The current design's reuse-for-restart choice optimizes for a property (continuity) that crash recovery invalidates.

---

## MODERATE ISSUES

### M1. Merge sequencer is a serialization bottleneck — parallel workers, serial merge

**Location:** M7 `MergeSequencer.merge_worker`

The merge sequencer processes worker branches **one at a time**: `git checkout main` → `git rebase main <branch>` → `git merge --ff-only` → **run tests (timeout 300s)** → get diff. For N workers, the wall-clock time is:

```
T_merge = N × (T_rebase + T_merge + T_test + T_diff)
```

With `cargo test` taking even 30s, 10 workers = 5+ minutes of serialized merging. This is the system's throughput ceiling — parallelism in the worker fan-out is entirely negated by serial merge. The system's effective throughput is `1 / T_merge_per_worker`, regardless of worker count.

The design runs the full test suite after **each individual merge**. This is correct for blame attribution (you know exactly which merge broke tests) but O(N) in test executions. Alternatives the design doesn't consider:
- **Batch-then-test:** Merge all branches, run tests once. If pass, done. If fail, bisect. Amortizes test cost but adds bisect complexity.
- **Speculative merge tree:** Merge branches in a binary tree, test at each level. O(log N) test runs in the best case.
- **Pre-merge fast checks:** Run only fast tests (lint, type-check) per merge; run full suite once at the end.

Additionally, `merge_worker` operates on `main` in `repo_root`. Every `git checkout main` and `git merge --ff-only` mutates `refs/heads/main`. If any worker is concurrently doing a git operation that reads `main` (e.g., `git log main`, `git merge-base main HEAD`), there's a TOCTOU window. Git refs are not transactional.

**Bug:** Line 980 references `self.root`, but the attribute is `self.repo_root` (line 937). This will raise `AttributeError` at runtime — `git show --stat` never executes.

---

### M2. Race conditions on shared `WorkerHandle` state — logical TOCTOU, not memory-level

**Location:** M4 `_supervise_worker`, `_read_worker_output`, `_heartbeat_monitor`

In single-threaded asyncio, there are no memory-level data races (no preemptive thread switching). However, there are **logical races at `await` boundaries** that produce incorrect behavior:

**Race: heartbeat monitor kills a worker that has already emitted its result.**

```
Timeline:
  T=0   Worker starts a 70s LLM call (cascade through 3 providers)
  T=0   Worker emits heartbeat before the call
  T=10  Heartbeat monitor wakes, elapsed=10, OK
  T=20  Heartbeat monitor wakes, elapsed=20, OK
  ...
  T=60  LLM call completes. Worker writes result to stdout pipe buffer.
        Worker also writes a final heartbeat (turn N).
  T=60  Worker exits cleanly (after emit result).
  T=70  Heartbeat monitor wakes (sleep was 10s, started at T=60).
        But wait — handle.last_heartbeat was updated by the stdout reader
        when it read the heartbeat at T=60... IF the stdout reader ran
        before the heartbeat monitor.
```

The interleaving depends on which task the event loop schedules first after both wake. If the heartbeat monitor runs before the stdout reader processes the buffered messages:

1. Monitor checks `handle.last_heartbeat` — still the pre-LLM-call timestamp (T=0).
2. elapsed = 70 > 60 → monitor calls `handle.proc.kill()`.
3. Worker is already dead (exited at T=60). `kill()` on a dead process: `ProcessLookupError` — **uncaught** in the heartbeat monitor. The exception propagates, the monitor task dies, the `_supervise_worker`'s `await heartbeat_task` (line 478) raises an unexpected exception.

Actually, re-examining: `handle.proc.kill()` on an already-dead process in asyncio raises `ProcessLookupError`. The heartbeat monitor (line 591) calls `handle.proc.kill()` without a try/except. This is a latent crash bug.

**Race: state transitions observed mid-update.**

The heartbeat monitor checks `handle.state in (DONE, FAILED, DEAD)` to decide whether to bail. But `_read_worker_output` sets `handle.state = RUNNING` when it reads a heartbeat, then later the supervisor sets `DONE`. If the heartbeat monitor reads state between a `result` message being received (which returns from `_read_worker_output`) and the supervisor setting `handle.state = DONE` (line 483), the monitor sees `RUNNING` and continues its timeout check. Minor, but illustrates that the state machine has no atomic transitions.

**These are not showstoppers** (asyncio's cooperative scheduling limits the damage), but they confirm the design hasn't thought through the interleaving semantics. The fix: use an explicit state machine with guarded transitions, or route all `handle` mutations through a single serialized function.

---

### M3. Event log has no durability guarantees on crash — "append" is not "fsync"

**Location:** M4 `_log_event` (line 439)

```python
with open(self.log_path, "a") as f:
    f.write(json.dumps(event) + "\n")
```

Three problems:

**(a) No `fsync`.** `write()` copies data to the OS page cache. `close()` flushes the Python buffer to the OS but does **not** force a disk sync. On power loss or kernel panic, data in the page cache is lost. The last seconds-to-minutes of events vanish. For a system claiming crash-recovery-via-replay, this means the recovery log itself is not crash-safe.

**(b) Partial line on crash.** If the supervisor is killed mid-`write()` (the JSON string is partially written to the page cache, or the OS write is torn across a block boundary), the log file ends with a truncated JSON line. On replay, `json.loads` fails on that line. The design says parse errors are "logged but don't crash the supervisor" (line 171) — but this means the **last event before crash is silently lost**, and if that event was a `checkpoint` or `result`, recovery is incomplete.

**(c) Open/close per event.** Opening the file for every event is a syscall-heavy pattern (`open` + `fcntl` + `write` + `close` × N events). Under the chatty event volume this design generates (heartbeat + tool_event + checkpoint per turn, per worker), this is hundreds of syscalls per second. Combined with C1 (synchronous I/O in the event loop), this is a performance cliff.

**Fix:** Keep the file handle open. Use a dedicated writer thread with a queue. Batch writes and `fsync` on a configurable interval (e.g., every 1s or every N events). Write a length-prefixed or checksummed format so truncated final lines are detectable and skippable on replay. Consider SQLite (WAL mode) — it's stdlib, has atomic commits, crash recovery, and is faster than manual file management for this workload.

---

### M4. FanOut cache and provider state are shared mutable global state — not thread-safe, not async-safe

**Location:** M2 `FanOut` (line 204)

`FanOut` maintains `self.cache` (a dict), `self.providers` (list of mutable `Provider` dataclasses with `cooldown_until`, `total_calls`, `total_errors`, `rate_limit_remaining`). These are mutated inside `_try_provider`, which runs via `asyncio.to_thread`.

In `race` mode, multiple `asyncio.to_thread(self._try_provider, ...)` calls run **concurrently in real OS threads** (the thread pool). They all mutate the same `Provider` objects (`provider.total_calls += 1`, `provider.cooldown_until = ...`). Under Python's GIL, `+=` is not atomic (it's a read-modify-write). With free-threaded Python (the design's target), these are genuine data races with no synchronization.

The cache dict is also mutated concurrently: `self.cache[key] = (result, time.time())` from multiple threads. Dict operations in CPython are atomic under the GIL due to internal locking, but under free-threaded Python (no GIL), concurrent dict writes can corrupt internal state.

The design makes concurrent-multi-thread claims (race mode, `asyncio.to_thread`, "Python 3.14 free-threaded") but provides **no locks, no atomics, no thread-safe data structures** anywhere in FanOut. This will manifest as lost counter increments, corrupted cache entries, and (under no-GIL) potential crashes.

---

### M5. Python 3.14 free-threaded claim provides no benefit to this architecture — and adds risk

**Location:** §0 TL;DR, §2.1, M9

The design targets "Python 3.14 (free-threaded, no-GIL)" and states "3.12+ works without true parallelism." This framing implies the system needs true multi-threading. It does not:

- **Workers are separate processes**, not threads. The GIL / no-GIL distinction is irrelevant to inter-worker parallelism.
- **The supervisor is single-threaded asyncio.** No CPU-bound threading.
- **The orchestrator is single-threaded async.** No CPU-bound threading.
- **FanOut uses `asyncio.to_thread`** for LLM calls, but LLM calls are **I/O-bound** (network requests to APIs). asyncio already handles I/O concurrency without threads; `to_thread` is used here presumably because DSPy's `LM.__call__` is synchronous. But this is I/O concurrency, not CPU parallelism — the GIL is released during I/O anyway.
- **SIMBA optimization (`num_threads=4`)** is the only CPU-bound multi-threaded code, and it runs offline, not in the supervisor.

The no-GIL build provides **zero benefit** to this system's runtime characteristics. Meanwhile, it adds:
- **10-40% single-threaded performance overhead** (documented for CPython 3.13t free-threaded build).
- **C extension compatibility risk** — DSPy, LiteLLM, and any native deps (e.g., `tiktoken`, `httpx` C extensions, `orjson`) may not be tested under free-threaded Python.
- **No production track record** — free-threaded Python is experimental as of 3.13/3.14.

The design provides **no benchmarks** showing free-threaded Python helps any workload in Cambium. The claim should either be backed by data or dropped. A standard CPython 3.12+ build with asyncio is the correct choice for this architecture.

---

### M6. Orchestrator has no cycle detection and a broken task-ID counter

**Location:** M6 `Orchestrator.execute` (line 851)

The orchestrator dispatches subtasks based on `depends_on` lists. If the LLM-generated dependency graph contains a cycle (A depends on B, B depends on A — entirely possible from an LLM decomposer), the `while ready:` loop will never schedule the cyclic tasks. They remain in `pending` forever. `ready` becomes empty, the loop exits, and the cyclic tasks are silently dropped — no error, no timeout, just missing results.

Additionally, `SubTask.task_id` generation is referenced via `self.__task_id_counter` (line 843) which is a syntactically broken line (`def __task_id_counter` with no body) — task IDs are never actually generated by the orchestrator. Tasks must come with pre-assigned IDs from the decomposer, but the decomposer returns `list[SubTask]` from an LLM call with no ID assignment logic shown.

**Fix:** Topological sort with cycle detection before dispatch. Reject cyclic decomposition graphs and re-prompt the decomposer.

---

### M7. No isolation between FanOut failure and worker liveness — but the design claims there is

**Location:** M6 design decision box (line 889-912)

The design claims: "If all providers are down, the supervisor keeps existing workers running and just can't spawn new tasks." This is **half-true**:

- The **supervisor** (M4) indeed never calls an LLM. Existing workers keep running. ✓
- But **workers** (M5) call the LLM on every ReAct step via DSPy, which goes through FanOut. If all providers are down, the worker's `agent.forward()` call will raise `AllProvidersFailed`. The worker's `except Exception` handler catches it and emits an `error` message (line 760-762). The supervisor treats `error` as failure (line 567: `return msg` → triggers restart logic). So a provider outage causes **every worker to fail and restart** — the exact cascade the design claims to prevent.

The isolation is between the *orchestrator* and the *supervisor*, not between *provider failure* and *worker stability*. A provider outage kills all in-flight worker tasks. The restart policy (C4) will then thundering-herd restart all of them, all hitting the still-down provider, all failing again.

**Fix:** Workers should distinguish "provider temporarily down" (retry with backoff inside the worker, don't emit error) from "task failed" (emit error). Or: FanOut's cooldown should be long enough (60s) that restarted workers don't immediately re-hit the provider — but with no jitter and the thundering herd, they will.

---

## MINOR NOTES

### N1. Code-level bugs (will fail at runtime)

These are implementation errors, not architecture, but they indicate the code hasn't been executed:

| Line | Issue |
|------|-------|
| 668 | `Path(path).write_content(content)` — `write_content` doesn't exist; should be `write_text` |
| 692 | `grep_code` has `result.stdout + result.stderr` with no `return` — returns `None` silently |
| 730 | `os.getpid()` — `os` is never imported in the worker script |
| 761 | `"type": " M5: error"` — malformed type string (leading space, module label leaked in) |
| 806 | `SubTask.depends_on: list[str] = None` — should be `Optional[list[str]] = None` or use `field(default=None)` |
| 843 | `def __task_id_counter` — broken syntax, incomplete line |
| 853 | `await self.decompose(...)` — `TaskDecomposer.forward` is not async; cannot be awaited |
| 980 | `self.root` — should be `self.repo_root`; raises `AttributeError` |
| 1091 | `len(true_bugs) polymorphism` — syntax error, should be `len(true_bugs)` |
| 1116 | `f".c flywheel/data/optimized/{node_name}.json"` — stray space, broken path |
| 1133, 1140 | ASCII art diagram has stray characters (`┌`, `┏`) breaking box alignment |

### N2. `collect_commits` assumes at least 5 commits exist

`git log --oneline HEAD~5..HEAD` (line 767) fails if the worktree has fewer than 5 commits (e.g., a fresh worktree with one commit). Should use `git log --oneline -5` (max 5, not range).

### N3. Sandbox (M8) is incomplete and references undefined `sys`

`_sandbox_command` uses `sys.executable` but doesn't import `sys`. The `Sandbox.wrap()` method returns a command list but `_spawn_worker` in M4 doesn't use it — workers are spawned with `[sys.executable, self.worker_script]` directly. The sandbox is not wired into the supervisor.

### N4. `grep_code` is a shell-injection vector

`grep -rn '{pattern}' {path}` (line 690) with `shell=True` — a pattern containing `'` breaks out of the quotes. Since workers run LLM-generated tool calls, a model hallucination or adversarial input could inject shell commands. Same for `run_shell` and `git_op` (by design, but `grep_code` should be safer).

### N5. Event log grows unbounded

No rotation, no compaction, no size limit. For a long-running harness generating hundreds of events per minute, `events.jsonl` will grow to GBs within hours. Replay becomes impractical. Need rotation + a separate "current state" snapshot mechanism.

### N6. The "Kahn process network" and "CSP" labels are name-dropping, not architecture

The design cites Kahn process networks and CSP as foundational patterns. The actual implementation is standard asyncio with subprocess pipes — which is neither Kahn (no fixed-point process network) nor CSP (no channel-select beyond `asyncio.select`). These labels add no constraint and may mislead reviewers into assuming guarantees (determinism, freedom from deadlock) that the code does not provide.

### N7. `shutdown()` kills pending procs but doesn't clean up worktrees

`shutdown()` (line 594) terminates/kills workers but never calls `worktree_mgr.remove()` on any worktree. Stale worktrees and branches persist after every supervisor shutdown. Combined with C5 (no prune), the repo accumulates `.cambium/worktrees/*` directories and `cambium/*` branches indefinitely.

---

## VERDICT: Fix First

The architecture has a **sound core** — process-isolated workers, stdin/stdout JSON IPC, Erlang-style supervision, git worktree isolation, and the deterministic/LLM layer separation are all good design choices that avoid real problems seen in competitor systems. The DSPy optimization flywheel is a genuine differentiator.

However, the design is **not ready to build** in its current form. The critical flaws are not edge cases — they are **guaranteed failure modes under normal operation**:

1. **C1 (sync I/O in event loop)** will cause cascading worker kills under any event burst. This is a "works in demo, dies in production" flaw.
2. **C2 (EOF ≠ dead)** means the liveness model is unsound — false negatives (zombie workers) and false positives (killed-while-healthy) will both occur.
3. **C3 (heartbeat granularity)** guarantees false-positive kills on any tool running >60s, which includes the design's own `run_shell` with a 120s timeout.
4. **C4 (no jitter)** guarantees thundering herd on any correlated failure.
5. **C5 (worktree locks)** guarantees restart failures after any crash mid-git-op.
6. **C6 (supervisor crash)** causes split-brain worktree corruption — the "Temporal-style durability" claim is unsupported.

These six issues are **interdependent**: C1 causes the pipe stalls that trigger C2's false positives, which trigger C3's heartbeat kills, which trigger C4's thundering herd, which trigger C5's worktree lock races, and C6 means even a clean supervisor restart doesn't recover safely. Fixing them in isolation is insufficient — the supervision layer needs a holistic redesign of its I/O model, liveness model, and crash-recovery model.

**Recommended path forward:**

1. **Redesign the event-loop I/O model:** All disk I/O off the event loop (dedicated writer thread or `aiofiles`). Bounded in-memory event buffer with explicit flush points.
2. **Redesign the liveness model:** Separate "process alive" (PID poll / `waitpid`) from "worker healthy" (heartbeat) from "worker making progress" (tool-event stream). Use different timeouts for each. Add per-tool heartbeats for long-running tools.
3. **Add jitter to all timers and backoffs.** This is a one-line fix with outsized impact.
4. **Implement worktree recovery** (lock cleanup + rebase abort + hard reset to base) before every worker respawn.
5. **Implement fencing/generation counters** to prevent split-brain after supervisor crash.
6. **Drop the Python 3.14 free-threaded target** unless benchmarks justify it. Use standard CPython 3.12+.
7. **Fix the ~11 runtime bugs** (N1) — the code as written will not execute.

The design document is thoughtful about *what patterns to borrow* but insufficiently rigorous about *how those patterns fail*. The competitor analysis (§6) is strong; the adversarial review of the design's own mechanisms is what this review attempts to provide, and what the design needs before it can be handed to a coding agent.
