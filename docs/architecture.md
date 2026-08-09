# Cambium — Architecture (v2)

**Version:** 2.0.0
**Date:** 2026-08-09
**Status:** Build-ready. Supersedes `system-design.md` v0.1.0.
**Scope:** This document is authoritative for behavior, interfaces, and failure semantics. Where it conflicts with `system-design.md`, this document wins.

---

## 0. TL;DR

Cambium is a **Python 3.14 multi-agent coding-agent harness**, shipped as an embeddable library (headless-first) with an optional TUI. A deterministic supervisor (`Custos`) manages N isolated worker processes (`Opifex`). Each worker runs a DSPy ReAct loop in a private git worktree under a sandbox. Workers communicate with the supervisor over **JSON-Lines on stdio with `request_id` RPC framing**. The orchestrating layer (`Architectus`) decomposes, routes, and evaluates via DSPy modules, each with its own dataset and metric. A serialized merge sequencer (`Unio`) fuses worker branches back onto `main`.

Cambium is a **leaf module** of a larger system: a host process spawns instances, owns persistence, and reads structured `Result` records. Cambium itself is stateless across sessions.

**What changed since v0.1.0.** Three adversarial reviews (`docs/reviews/`) catalogued ~25 CRITICAL flaws. v2 resolves every one. The headline fixes: (a) liveness is no longer "stdout EOF = dead" — there is an explicit four-layer liveness model with `request_id` framing, generation fencing tokens, and per-tool heartbeats; (b) all disk I/O is off the asyncio event loop on dedicated writer threads; (c) restart policy has full jitter plus an absolute ceiling; (d) worktrees are recovered (lock cleanup + hard reset) before every respawn and may be fenced by generation; (e) the FanOut cache is opt-in, keyed on task + context + model with a TTL, and lives upstream of workers; (f) the provider cascade actually cascades across models of a declared tier; (g) the merge sequencer holds an `asyncio.Lock` and operates in a throwaway worktree; (h) every DSPy module ships with its own frozen dataset, metric, and held-out eval — the "independently hill-climbable" claim is restated as a hypothesis validated under pinned siblings; (i) secrets are env-only and redacted; (j) logging is stdlib, structured, non-blocking, rotated.

**Primary patterns kept from v0.1:** Erlang/OTP one-for-one transient supervision; git worktree isolation; deterministic/LLM layer separation; DSPy optimization flywheel; subprocess-per-worker with stdio IPC.

**Primary patterns dropped:** free-threaded Python (irrelevant for subprocess design); `.pid` files; Unix sockets; lock files; "stdout EOF is death"; prompt-only cache; the literal `cascade`/`race` implementation in v0.1 M2 (rewritten).

---

## 1. Goals & Non-Goals

### Goals
1. **Embeddable.** `import cambium; await cambium.session(...).run(spec)` works in any async Python program. No daemon required.
2. **Headless-first.** The TUI is a view over the same JSON-Lines event stream the host reads. Nothing is TUI-only.
3. **Sound under failure.** Liveness, restart, merge, and crash-recovery have explicit, tested semantics. No "works in demo, dies in production."
4. **Per-module optimizable.** Each DSPy module has its own dataset, metric, and held-out evaluation. Sibling modules are pinned during optimization.
5. **Bounded everything.** Restarts, wall time, memory, cache size, log size, and worker counts all have explicit ceilings.
6. **Proto-AGI-friendly.** A host system can spawn/stop/poll/query Cambium instances through a stable contract.

### Non-Goals
1. **Not a coding agent itself.** Cambium is the harness. The DSPy programs do the coding.
2. **Not distributed.** Single host. Workers are local subprocesses on a single machine.
3. **Not free-threaded.** Standard CPython 3.14. The subprocess design does not need no-GIL.
4. **Not a TUI app.** The TUI is a thin, optional consumer.
5. **No new frameworks.** Stdlib + DSPy + git. Structured logging via stdlib `logging`.

---

## 2. Layering

```
┌──────────────────────────────────────────────────────────────────────┐
│  UPPER SYSTEM (proto-AGI host; not part of Cambium)                  │
│  Owns persistence, spawns Cambium instances, reads Result records.   │
└──────────────────────────────────────┬───────────────────────────────┘
                                       │ Control plane: spawn / stop / poll / query
                                       │ Data plane:    Result envelope, session_dir
┌──────────────────────────────────────▼───────────────────────────────┐
│  CAMBIUM  (leaf module; embeddable Python library)                   │
│                                                                       │
│  ┌────────────────────────────────────────────────────────────────┐  │
│  │  PUBLIC API:  Cambium · Session · Result · Instance · Event    │  │
│  │  Headless-first. TUI is a consumer of the event stream.        │  │
│  └──────────────────────────────┬─────────────────────────────────┘  │
│                                 │                                      │
│  ┌──────────────────────────────▼─────────────────────────────────┐  │
│  │  ORCHESTRATION LAYER  (LLM-driven; may fail; retryable)         │  │
│  │  Architectus = ShouldDecompose → TaskDecomposer → TaskRouter    │  │
│  │               → ResultEvaluator                                 │  │
│  │  Each is a DSPy module with its OWN frozen dataset + metric.    │  │
│  │  Diffundo (FanOut): upstream cache, tier-based provider         │  │
│  │                      cascade, opt-in per-call caching.          │  │
│  └──────────────────────────────┬─────────────────────────────────┘  │
│                                 │ await run_task(spec) -> Result       │
│  ┌──────────────────────────────▼─────────────────────────────────┐  │
│  │  DETERMINISTIC LAYER  (pure Python; never LLM; never crashes)   │  │
│  │  Custos    — supervisor; lifecycle; watchdog; restart policy;   │  │
│  │              worktree recovery; durable event log.              │  │
│  │  Unio      — merge sequencer (asyncio.Lock + throwaway wt).     │  │
│  │  Surculus  — worktree manager (lock recovery + prune).          │  │
│  │  Septum    — sandbox (Linux bwrap / macOS sandbox-exec / noop). │  │
│  │  Nuntius   — IPC protocol (JSON-Lines + request_id framing).    │  │
│  └──────────────────────────────┬─────────────────────────────────┘  │
│                                 │ stdin/stdout pipes (one pair/worker) │
│  ┌──────────────────────────────▼─────────────────────────────────┐  │
│  │  WORKER LAYER  (N independent processes)                         │  │
│  │  Opifex    — DSPy ReAct; tools; per-tool heartbeat; checkpoint. │  │
│  │  Each worker: isolated worktree, own process group, generation  │  │
│  │  counter (fencing token), bounded tool set, stdout reserved.    │  │
│  └────────────────────────────────────────────────────────────────┘  │
└──────────────────────────────────────────────────────────────────────┘
```

**Invariants of the layering:**
- The Deterministic Layer **never calls an LLM** and **never imports a DSPy module**. A total LLM/provider outage leaves existing workers running and the supervisor healthy.
- The Orchestrator depends on the Deterministic Layer (calls `Custos.run_task`); the reverse is false.
- Workers depend only on `Nuntius` (protocol) and `Diffundo` (LLM access, injected by config). They never call `Custos` directly.
- The upper system depends only on the **Public API**. It does not import any module below.
- `Diffundo` is owned by the Orchestrator. Workers receive a `DiffundoConfig` over the protocol and instantiate their own `Diffundo` client; cache state is **never shared across worker processes** (each worker has its own opt-in cache; see §8).

---

## 3. Public API

The library is **headless-first**. There is no required UI. The TUI (when present) consumes the same `events()` async iterator the host can.

### 3.1 Entry points

```python
# cambium/__init__.py — public surface
from cambium import Cambium, Session, Result, Instance, Event, Config, load_config
__all__ = ["Cambium", "Session", "Result", "Instance", "Event", "Config", "load_config"]
```

### 3.2 `Cambium` — harness entry point

```python
from pathlib import Path
from dataclasses import dataclass

@dataclass(frozen=True)
class Config:
    repo_root: Path
    session_root: Path            # parent of per-session dirs
    fanout: FanOutConfig
    supervisor: SupervisorConfig
    worker: WorkerConfig
    sandbox: SandboxConfig
    providers: tuple[ProviderConfig, ...]   # never serialized to logs
    # See §11 for full schema.

class Cambium:
    def __init__(self, config: Config): ...
    async def __aenter__(self) -> "Cambium": ...
    async def __aexit__(self, *exc): ...    # graceful shutdown

    def session(self, *, spec: str, base_branch: str = "main",
                session_id: str | None = None,
                session_dir: Path | None = None) -> Session: ...
    async def spawn(self, *, spec: str, session_dir: Path,
                    base_branch: str = "main") -> Instance: ...
```

`Cambium` is the long-lived object. It owns the deterministic supervisor, the orchestrator, the event log writer, and the worktree manager. **It holds no per-task mutable state.**

### 3.3 `Session` — one task execution, headless API

```python
class Session:
    @property
    def session_id(self) -> str: ...
    @property
    def session_dir(self) -> Path: ...

    async def run(self) -> Result:
        """Block until the task completes, fails permanently, or is cancelled."""

    async def events(self) -> AsyncIterator[Event]:
        """Stream every Event. Durability: every event is fsync-d before yielded."""

    async def cancel(self, reason: str = "user") -> None: ...
```

`Session.run()` is the synchronous-feeling entry point for callers who just want a result. `Session.events()` is for callers who want to observe or stream (the TUI uses this).

### 3.4 `Result` — structured record (data plane)

```python
@dataclass(frozen=True)
class Result:
    status: Literal["done", "failed", "rejected", "timeout", "cancelled"]
    exit_code: int                      # 0 done, 1 failed, 2 rejected,
                                        # 3 timeout, 4 cancelled
    commits: tuple[str, ...]            # SHAs produced
    files_changed: tuple[str, ...]
    summary: str                        # worker-authored, ≤2k chars
    metric_score: float                 # 0.0..1.0, multi-signal (§10)
    metric_breakdown: dict[str, float]  # per-signal scores
    event_log_ref: str                  # "sqlite:<session_dir>/cambium/events.db"
    session_id: str
    started_at: float
    ended_at: float
    failure_reason: str | None          # populated when status != "done"
```

`Result` is JSON-serializable and is the **only** contract the upper system consumes from a finished run. It is written atomically to `${session_dir}/cambium/result.json` before `Session.run()` returns.

### 3.5 `Instance` — proto-AGI leaf handle (control plane)

```python
class Instance:
    @property
    def instance_id(self) -> str: ...
    @property
    def session_dir(self) -> Path: ...
    @property
    def status(self) -> Literal["pending","running","done","failed","cancelled"]: ...

    async def poll(self) -> InstanceStatus: ...
    async def wait(self, timeout: float | None = None) -> Result: ...
    async def stop(self, reason: str = "host") -> None: ...    # graceful
    async def kill(self) -> None: ...                          # immediate
    def query(self, field: str) -> object: ...
        # Read-only accessor for session state: "events_summary",
        # "workers_alive", "current_phase", etc. Never blocks.
```

`Instance` is what a proto-AGI host holds. The host owns `session_dir` lifecycle; Cambium owns everything under `${session_dir}/cambium/`.

### 3.6 `Event` — typed stream record

```python
@dataclass(frozen=True)
class Event:
    kind: str               # "task_assigned" | "worker_spawned" | "heartbeat" |
                            # "tool_event" | "checkpoint" | "merge_progress" |
                            # "result" | "worker_exit" | "log" | ...
    task_id: str | None
    request_id: str | None
    timestamp: float        # time.time()
    monotonic_ms: int       # time.monotonic_ns() // 1_000_000
    generation: int | None
    payload: dict           # type-specific; redacted of secrets (§9)
```

The `Event` stream is the **machine interface**. The TUI renders it; the host system can also subscribe. Every `Event` has been fsync-d to the durable log before it is yielded to subscribers.

---

## 4. Module Catalog

Each row maps to one self-contained module with its own `architecture.md` (see `docs/module-template/`). Latin names retained for continuity with v0.1; **no module requires the Latin name to be used in code**.

| Code | Name | Layer | Responsibility | State owned |
|---|---|---|---|---|
| M1 | Nuntius | Deterministic | IPC protocol: JSON-Lines framing, `request_id` RPC, message schema. | None (pass-through). |
| M2 | Diffundo | Orchestrator | Multi-provider LLM access: tier-based cascade, opt-in cache, cooldown. | Cache (bounded, opt-in); per-provider cooldown timers. |
| M3 | Surculus | Deterministic | `git worktree` lifecycle: create, recover, prune, list. | None (state lives in git). |
| M4 | Custos | Deterministic | Supervisor: lifecycle, watchdog, restart, event log writer. | WorkerHandle table; event log handle; restart counters. |
| M5 | Opifex | Worker | DSPy ReAct loop; tools; checkpoint; heartbeat. | Per-worker: trajectory, turn counter, generation token. |
| M6 | Architectus | Orchestrator | DSPy modules: `ShouldDecompose`, `TaskDecomposer`, `TaskRouter`, `ResultEvaluator`. | DSPy program versions (read-only at runtime). |
| M7 | Unio | Deterministic | Merge sequencer: serialized, throwaway worktree, test gate. | None (operates on a temp worktree). |
| M8 | Septum | Deterministic | Sandbox wrapper: `bwrap` (Linux), `sandbox-exec` (macOS), noop. | None. |
| M9 | Ascensus | Tooling (offline) | Optimization harness: per-module dataset, metric, held-out eval. | Optimized prompt artifacts under `.cambium/optimized/`. |
| M10 | Janus | View | TUI: subscribes to `Session.events()`. Read-only. | None. |

**Module interface contracts are normative.** Each module's `architecture.md` defines its inputs, outputs, state, failure modes, DSPy program, metric, dataset, and test strategy, per the template in `docs/module-template/architecture.md`.

---

## 5. IPC Protocol — Nuntius

**Transport:** stdin/stdout pipes, one pair per worker. JSON-Lines (one JSON object per `\n`-terminated line, UTF-8). stderr is free-form advisory log only.

**Framing:** every request carries a `request_id` (ULID, monotonic-ish). Every response that completes a request echoes the same `request_id`. This makes the protocol RPC-shaped, supports future multiplexing, and gives the host a correlation key for the event log.

### 5.1 Channel invariants

1. **stdin = one writer (supervisor), one reader (worker).** Worker blocks on `readline()` between messages. No polling.
2. **stdout = one writer (worker), one reader (supervisor).** Stdout is **reserved for the protocol**. Library noise that would write to stdout (DSPy progress bars, LiteLLM warnings, deprecation warnings, torch banners) is redirected to stderr at worker startup via `sys.stdout = ...` reshim and `logging` reconfiguration. **No `print()` is permitted in worker code or its dependencies.** A pre-flight check redirects `sys.stdout` for known chatty libraries; see §11.
3. **`PYTHONUNBUFFERED=1`** is set in the worker env so partial-line buffering never loses a message on SIGKILL.
4. **One JSON object per line.** Each line is parsed independently. A line that fails JSON parse is **logged with its line number** and skipped; the stream is not corrupted.
5. **stderr is unstructured.** Supervisor reads stderr opportunistically and writes it to the event log under `kind="log"`, level-tagged. It is advisory only; no protocol semantics depend on it.
6. **No shared FDs.** Worker-spawned subprocesses use `pass_fds=()` and `close_fds=True`. The worker's stdout FD is **never** inherited by grandchildren. The supervisor enforces this by spawning workers with `start_new_session=True` so the entire worker subtree can be killed via process group.

### 5.2 Message schema (authoritative)

```jsonc
// ── Supervisor → Worker ──────────────────────────────────────────
{"type":"init",
 "request_id":"01HXXXX...",            // ULID; echoed in result/error/exit
 "task_id":"wt-abc-001",
 "generation":3,                        // fencing token (§7.3)
 "worktree":"/abs/path",
 "base_commit":"a1b2c3d...",
 "spec":"Refactor dry_run.rs to remove global state",
 "max_turns":20,
 "tools":["read_file","write_file","edit_file","run_shell","git_op","grep_code"],
 "fanout_config":{ /* DiffundoConfig, no api keys */ },
 "provider_env_keys":["DEEPCODE_API_KEY","GEMINI_API_KEY"],  // names only; values from env
 "permissions":{"network":true,"shell":true},
 "heartbeat":{"interval_s":15,"timeout_s":90},
 "budget":{"max_wall_s":1800,"max_restarts":10}}

{"type":"context","request_id":"...","context":"Previous task added kalman_fusion."}
{"type":"cancel", "request_id":"...","reason":"timeout"}
{"type":"ping", "request_id":"..."}    // liveness probe; worker must echo via "pong"

// ── Worker → Supervisor ──────────────────────────────────────────
{"type":"ready",
 "request_id":"01HXXXX...",            // echoes the init request_id
 "task_id":"wt-abc-001",
 "pid":12345,
 "generation":3,                        // worker has accepted this generation
 "monotonic_ms":...}

{"type":"pong","request_id":"...","task_id":"...","monotonic_ms":...}

{"type":"heartbeat",
 "task_id":"wt-abc-001",
 "turn":3,
 "tool":"run_shell",                    // currently-executing tool, or null
 "status":"editing dry_run.rs",
 "monotonic_ms":...}

{"type":"tool_event",
 "task_id":"wt-abc-001",
 "tool":"run_shell",
 "cmd":"rg 'fn dry_run' src/",
 "exit_code":0,
 "duration_ms":1200}

{"type":"checkpoint",
 "task_id":"wt-abc-001",
 "turn":3,
 "state_ref":"checkpoints/wt-abc-001/turn-003.json",
 "commits_so_far":["a1b2c3d"]}

{"type":"result",
 "request_id":"01HXXXX...",            // echoes init
 "task_id":"wt-abc-001",
 "status":"done",
 "commits":["a1b2c3d"],
 "files_changed":["src/dry_run.rs"],
 "summary":"Removed 3 global statics; replaced with worker-local config.",
 "metric_score":0.84,
 "metric_breakdown":{"tests":1.0,"spec_adherence":0.9,"diff_quality":0.7,"canaries":1.0}}

{"type":"error",
 "request_id":"01HXXXX...",
 "task_id":"wt-abc-001",
 "error_type":"build_failure",
 "message":"cargo build failed: 3 errors",
 "partial_commits":[],
 "recoverable":true}                    // hint to supervisor; see §7.4

{"type":"exit",
 "task_id":"wt-abc-001",
 "generation":3,
 "reason":"done" | "crash" | "cancelled" | "fatal",
 "monotonic_ms":...}
```

The `exit` message is **the authoritative termination signal**. Workers emit it as the final line before process exit. A worker that exits without emitting `exit` is treated as having crashed — even if `result` was already sent (the supervisor cross-checks).

### 5.3 Liveness model (resolves DS-C2)

v0.1 conflated "EOF on stdout" with "worker dead." It is not. Cambium uses a **four-layer liveness model**, listed in descending authority:

| # | Signal | Source | Authority | Latency |
|---|---|---|---|---|
| 1 | Process exit (`proc.wait()` returns) | kernel | Definitive | immediate |
| 2 | `{"type":"exit"}` message on stdout | worker | Definitive (matches #1 inside 100 ms) | immediate |
| 3 | Heartbeat watchdog (default 90 s, configurable per task) | supervisor | Strong — kills worker if tripped | up to `timeout_s` |
| 4 | EOF on stdout | kernel | **Advisory only** — triggers investigation, not automatic kill | immediate |

Rules:

- **EOF alone is not death.** When the supervisor sees EOF, it does **not** immediately conclude the worker is dead. It schedules a 5 s grace timer and then calls `proc.poll()`. If the process has exited, the worker is dead (rules 1+2 agree). If the process is still alive (grandchild holding the pipe — DS-C2 mode a), the supervisor escalates: it sends `{"type":"ping"}`. If no `pong` within 10 s, it kills the **process group** (worker was spawned with `start_new_session=True`).
- **Heartbeat watchdog never fires during normal tool execution.** Long-running tools (`run_shell`, `git_op`, LLM calls inside Diffundo) emit heartbeats from inside the tool wrapper (§7.6). The default 90 s timeout therefore represents **3 missed 15 s beats**, not "90 s of inactivity."
- **Supervisor-induced stalls are flagged, not blamed on the worker.** A "drain deadline" timer tracks when the supervisor last called `readline()` on each worker's stdout. If the supervisor hasn't drained a worker in >30 s, it emits a `supervisor_stall` event and **suspends heartbeat enforcement** for that worker until draining resumes (resolves DS-C2 mode d).
- **Python buffering cannot lose messages.** `PYTHONUNBUFFERED=1` is set; worker startup re-opens `sys.stdout` with `line_buffering=True` as belt-and-braces. A pre-flight check (§11) asserts stdout is a pipe and not a TTY.

### 5.4 Failure modes of EOF that v2 explicitly handles

| DS-C2 mode | Cause | v2 handling |
|---|---|---|
| (a) Grandchild holds the pipe | inherited FD leak | `close_fds=True` + `pass_fds=()` on every subprocess from the worker; workers spawned with `start_new_session=True` so the supervisor can `killpg` the whole subtree. EOF + still-alive process triggers the ping/process-group kill sequence above. |
| (b) Python stdout buffering | block-buffered pipe | `PYTHONUNBUFFERED=1` + `line_buffering=True` reopen. |
| (c) Partial write of `result` | SIGKILL mid-`write()` | `result` is persisted to the checkpoint store **before** the message is emitted. On crash-during-emit, the supervisor recovers the result from the checkpoint store at the next watchdog tick. Length-prefixed framing is **not** used (we accept the rare torn-line case for parser simplicity) — instead, lines that fail JSON parse are tagged with `parse_error` and skipped; the result-recovery path picks up from checkpoints. |
| (d) Supervisor-induced stall | event-loop block (DS-C1) | All supervisor disk I/O off the event loop (§6); drain-deadline watchdog suspends heartbeat enforcement during supervisor stalls. |

---

## 6. Event Log & Feedback Contract

The event log is the **durable feedback channel**: it is how the orchestrating LLM (in `Architectus`) gets evidence from finished workers, how the host reconstructs state, and how offline optimization (`Ascensus`) reads trajectories.

### 6.1 Store

- **Primary store:** SQLite in **WAL mode** at `${session_dir}/cambium/events.db`. Stdlib only; atomic commits; crash-safe by construction (resolves DS-C6/M3).
- **Optional mirror:** JSON-Lines at `${session_dir}/cambium/events.jsonl` for streaming consumers and human inspection. Off by default; enable via config.
- **Retention:** per-session DB; the host archives or deletes the session dir. Within a session, an `events` table is append-only; a `snapshots` table stores periodic compaction points. Replay = read `events` since the last `snapshot`.

### 6.2 Writer architecture (resolves DS-C1, DS-M3, IMPL-M7)

```
                       event loop                      dedicated writer thread
                  ┌────────────────┐                ┌────────────────────────┐
asyncio tasks ──► │ supervisor     │  queue.Queue   │ single consumer:       │
                  │ enqueues Event ├───────────────►│ - dequeue               │
                  │ (non-blocking) │   (bounded,    │ - BEGIN; INSERT; COMMIT│
                  │                │    backpressure│ - fsync WAL every 1s   │
                  └────────────────┘    or drop+log)│ - publish to in-proc   │
                          │                            │   subscriber set      │
                          ▼                            └───────────┬────────────┘
                  in-memory ring buffer                            │
                  (last 10 000 events)                            ▼
                                                              asyncio.Queue
                                                                  │
                                                                  ▼
                                                          Session.events()
                                                          (async iterator)
```

Invariants:

1. The supervisor **never** performs disk I/O on the event-loop thread. Every event goes through `queue.Queue.put_nowait()` (non-blocking).
2. The queue is **bounded** (default 10 000). On overflow, the writer drops the oldest non-critical event, logs a `drop` marker, and increments a counter. Critical events (`result`, `worker_exit`, `task_failed`, `merge_progress`) are **never** dropped — they block the producer for up to 100 ms (acceptable, because these are rare).
3. The writer thread is the **sole process** that holds the SQLite write connection. There is no concurrency on the write path. The in-memory ring buffer is a `collections.deque(maxlen=10000)` protected by a `threading.Lock`.
4. **fsync cadence:** the writer calls `PRAGMA wal_checkpoint(PASSIVE)` and `os.fsync(db_fd)` at most once per second, or immediately on critical events. The default fsync cadence is configurable.
5. **Subscribers** (`Session.events()` consumers) receive events via an `asyncio.Queue` fed from the writer thread through `loop.call_soon_threadsafe`. Subscribers are guaranteed to see events in monotonic order, fsync-d before delivery.
6. **Redaction** is applied at enqueue time, before the event ever reaches disk (§9.3).

### 6.3 Event schema (durable)

```sql
CREATE TABLE events (
    seq          INTEGER PRIMARY KEY AUTOINCREMENT,  -- monotonic, gap-free
    monotonic_ms INTEGER NOT NULL,
    wall_ts      REAL    NOT NULL,
    kind         TEXT    NOT NULL,
    task_id      TEXT,
    request_id   TEXT,
    generation   INTEGER,
    payload      TEXT    NOT NULL                     -- redacted JSON
);
CREATE INDEX events_task_idx ON events(task_id, seq);
CREATE INDEX events_kind_idx ON events(kind, seq);

CREATE TABLE snapshots (
    seq           INTEGER PRIMARY KEY,
    taken_at      REAL    NOT NULL,
    state_summary TEXT    NOT NULL                     -- redacted JSON
);
```

`seq` is gap-free within a session; gaps signal data loss on replay.

### 6.4 Checkpoint / restart semantics

A worker emits `{"type":"checkpoint", "state_ref":"...", "commits_so_far":[...]}` after every tool call that produces or modifies durable state (file writes, commits). The `state_ref` points to `${session_dir}/cambium/checkpoints/${task_id}/turn-${N}.json`, written atomically (write-temp + `os.rename`).

On restart (§7.4), `Custos` loads the latest checkpoint for the task and re-injects it into the new worker via the `init` message as `resume_from_checkpoint`. Workers that opt out of checkpointing (e.g., read-only tasks) accept a fresh start.

**Checkpoint is not a substitute for the event log.** The event log records *what happened* (for replay, audit, training); checkpoints record *where to resume* (for crash recovery). They are distinct stores.

---

## 7. Lifecycle

This section normatively defines how a task moves through the system. Every state transition emits an event.

### 7.1 State machine (per task)

```
                ┌──────────┐
                │ PENDING  │  task enqueued by Architectus
                └────┬─────┘
                     │ worktree created (Surculus)
                     ▼
                ┌──────────┐
                │ SPAWNING │  Custos.create_subprocess_exec(...)
                └────┬─────┘
                     │ ready received
                     ▼
                ┌──────────┐  heartbeat ─┐
            ┌──►│ RUNNING  │◄────────────┘
            │   └────┬─────┘
            │        │ result  ──────────────►  ┌──────────┐
            │        │                          │  DONE    │
            │        │ timeout/fatal error ───► │ FAILED   │
            │        │ reviewer-rejected ─────► │ REJECTED │
            │        ▼
            │   ┌──────────┐
            │   │ CRASHED  │  EOF + no result, OR watchdog kill
            │   └────┬─────┘
            │        │ restartable & under budget?
            │        ├─yes─► recover worktree (§7.5), increment generation
            └────────┘        back to SPAWNING
                     │no
                     ▼
                ┌──────────┐
                │ FAILED   │  max_restarts reached OR budget exhausted
                └──────────┘
```

### 7.2 Spawn

```python
proc = await asyncio.create_subprocess_exec(
    *sandbox.wrap([sys.executable, "-X", "utf8", "-u", worker_script]),
    stdin=PIPE, stdout=PIPE, stderr=PIPE,
    cwd=worktree_path,
    env={**os.environ, "PYTHONUNBUFFERED": "1",
         "CAMBIUM_TASK_ID": task_id, "CAMBIUM_GENERATION": str(generation)},
    start_new_session=True,         # process group for killpg
    pass_fds=(), close_fds=True,
)
```

After spawn, the supervisor sends `init` and **waits for `ready`** before considering the worker RUNNING. There is a `ready_timeout` (default 60 s) covering cold-start cost (IMPL-M2). On timeout, the worker is killed and the restart policy engages.

### 7.3 Fencing tokens (resolves DS-C6)

Every spawn carries a `generation` integer, monotonically increasing per task. The generation is:

- Sent in the `init` message.
- Echoed by the worker in `ready`, `heartbeat`, `checkpoint`, `result`, `error`, and `exit`.
- Written to a file `${worktree}/.cambium/generation` after `worktree create`/`recover`.

Before **every** git operation (and before writing to `state_ref`), the worker reads `.cambium/generation` and compares to its in-memory value. On mismatch, the worker emits `{"type":"exit","reason":"fatal","message":"generation mismatch"}` and terminates.

This prevents the split-brain scenario of DS-C6: if the supervisor crashes, restarts, and spawns a fresh worker into the same worktree, it bumps the generation. Any orphaned worker from the previous supervisor instance detects the mismatch on its next git op and dies. Combined with **process-group kill at startup** (below), this makes worktree split-brain detectable rather than silent.

### 7.4 Restart policy (resolves DS-C4)

```python
@dataclass(frozen=True)
class RestartPolicy:
    burst_max: int = 5                # MaxR
    burst_window_s: float = 60.0      # MaxT
    absolute_max: int = 10            # hard ceiling per task
    base_delay_s: float = 1.0
    backoff_base: float = 2.0
    jitter: float = 1.0               # full-jitter coefficient (0..1)
```

- **Burst cap (Erlang-style):** ≥`burst_max` crashes within `burst_window_s` → escalate.
- **Absolute cap:** ≥`absolute_max` total restarts → mark FAILED. Closes the DS-C4(b) "crash once per 61 s forever" hole.
- **Per-task wall-time budget** (from `init.budget.max_wall_s`, default 1800 s): exceeded → mark FAILED.
- **Full jitter:** `delay = random.uniform(0, base_delay_s * backoff_base ** n)`. No deterministic thundering herd (DS-C4(a)). Jitter is also applied to the heartbeat watchdog interval, the fsync timer, and the heartbeat emission.
- **Recoverable errors:** workers set `recoverable: false` on `error` messages that should not be retried (e.g., `invalid_spec`, `unknown_tool`). Non-recoverable errors skip the restart budget and fail immediately.
- **Provider outage is not a worker failure** (resolves DS-M7): `Diffundo.AllProvidersFailed` raised inside the worker is caught at the worker's tool boundary, logged, and converted to a backoff retry **inside the worker** for up to `provider_patience_s` (default 180 s). Only if the outage persists past that does the worker emit `error` with `recoverable: true`. This isolates provider flapping from the supervisor's restart policy.

### 7.5 Worktree recovery (resolves DS-C5, IMPL-M9)

Before **every** respawn (not just first-spawn), `Surculus.recover(worktree, base_commit)` runs:

1. Remove every `*.lock` file under `${worktree}/.git` and `${repo}/.git/worktrees/${id}`.
2. Abort in-progress git operations: `git rebase --abort`, `git merge --abort`, `git cherry-pick --abort`, `git revert --abort` (each best-effort, log if it fails).
3. `git reset --hard ${base_commit}` — drop the failed attempt's working-tree changes.
4. `git clean -fd` — remove untracked files (build artifacts, stray files).
5. Write `.cambium/generation` with the new generation number.
6. Optionally (default on): if a checkpoint exists for the task, **restore** the checkpoint's commits by cherry-picking `commits_so_far` onto `base_commit`. If cherry-pick fails, fall back to a fresh start.

After M3-style recovery, the worktree is in a known-good state. The new worker inherits no corruption.

If recovery fails (step 3 returns non-zero), the worktree is **quarantined** to `${session_dir}/cambium/quarantine/${task_id}-${generation}/` and a fresh worktree is created from `base_commit`. The quarantined tree is preserved for forensics and pruned after `${session_dir}` cleanup.

`Surculus.prune()` is called on supervisor startup and shutdown to clean stale `git worktree` administrative entries.

### 7.6 Per-tool heartbeat (resolves DS-C3)

Long-running tools heartbeat from inside the tool wrapper:

```python
async def run_shell(cmd: str, timeout: float = 120.0) -> ToolResult:
    deadline = time.monotonic() + timeout
    proc = await asyncio.create_subprocess_shell(
        cmd, stdout=PIPE, stderr=PIPE, start_new_session=True)
    try:
        while True:
            try:
                stdout, stderr = await asyncio.wait_for(
                    proc.communicate(), timeout=HEARTBEAT_INTERVAL_S)
                return ToolResult(stdout + stderr, proc.returncode)
            except asyncio.TimeoutError:
                if time.monotonic() > deadline:
                    proc.kill()
                    return ToolResult("timeout", -1)
                heartbeat_emit(tool="run_shell", status=cmd[:120])
    finally:
        if proc.returncode is None:
            proc.kill()
```

The default tool timeouts (§11) are all `<` heartbeat timeout (90 s) **or** the tool wraps a heartbeat as shown. There is no tool in the default set that can run silently past the watchdog. (If a user-added custom tool needs to run longer, it must call `heartbeat_emit` itself or accept watchdog termination.)

### 7.7 Shutdown

```python
async def shutdown(self):
    self._shutdown = True
    # 1. Stop accepting new sessions.
    # 2. Send cancel to every running worker; wait up to graceful_s (default 10s).
    # 3. SIGTERM the process groups of workers still alive.
    # 4. After term_grace_s (default 5s), SIGKILL the process groups.
    # 5. Run worktree_mgr.prune() to clean .git/worktrees/ entries.
    # 6. Flush the event-log writer queue and close the DB.
```

Process groups are used (not bare PIDs), so grandchildren die with the worker. The supervisor's own parent (host system) can additionally `killpg` the supervisor's PID for hard shutdown.

---

## 8. Caching & Transparency Policy

A recurring v0.1 flaw was conflating **pass-through modules** (which carry data without interpreting it) with **state-owning modules** (which make decisions based on accumulated state). v2 makes the split explicit.

| Module | Transparency | State owned | Notes |
|---|---|---|---|
| **Nuntius** | Pass-through | None | Carries bytes; never interprets payload. No cache. |
| **Surculus** | Pass-through | None | Delegates to git; state lives in git itself. |
| **Septum** | Pass-through | None | Wraps a command list; no inspection. |
| **Unio** | Pass-through | None | Operates on a throwaway worktree; no in-memory state between merges. |
| **Custos** | Owns (process) | WorkerHandle table, event log | Process state. No LLM cache. |
| **Opifex** | Owns (per-worker) | Trajectory, turn counter, generation | Per-process; dies with the worker. No cross-worker sharing. |
| **Diffundo** | Owns (cache) | Per-instance cache, per-provider cooldown | **Cache lives here, upstream of workers**; see §8.1. |
| **Architectus** | Owns (program versions) | DSPy program versions (read-only) | Each submodule has its own dataset; see §9. |
| **Ascensus** | Owns (offline) | Optimized artifacts | Not on the hot path. |

### 8.1 Diffundo cache policy (resolves LLM-C1, LLM-M5)

- **Cache is opt-in per call.** Default is `cache=False`. The caller passes `cache=True` and a `cache_namespace` to enable. Workers do not cache codegen by default; the orchestrator caches genuinely stateless calls (e.g., `ShouldDecompose` classifier outputs, given a fixed spec).
- **Cache key is `sha256(namespace || model || temperature || prompt || context_hash)`.** `context_hash` is caller-supplied and **must** include any world state the answer depends on — for code-aware calls, callers pass `git rev-parse HEAD` plus a hash of the relevant file contents. Calls that omit `context_hash` are rejected when `cache=True`.
- **TTL:** default 300 s (5 minutes), not the v0.1 3600 s. Configurable per namespace.
- **Bound:** LRU, default 10 000 entries, per `Diffundo` instance.
- **No cross-worker sharing.** Each worker process has its own `Diffundo` instance with its own cache. Shared caching is the host system's job (it can subscribe to the event log and replay cache-populating calls if it wants). Cross-process caches invite coherence bugs that the v0.1 reviews rightly flagged.
- **Transparency:** every cached response is tagged `"cache_hit": true` in the result envelope, with the original generation timestamp. Optimization harnesses can filter cache hits out of trajectory datasets.
- **Cache upstream of workers.** The orchestrator-side `Diffundo` instance is where shared cross-task caching would live (if ever added). Workers' caches are private and short-lived.

### 8.2 Why "transparent" Nuntius matters

`Nuntius` is the only module that sees every byte of every protocol message. Keeping it strictly pass-through means:

- It has no parse-and-branch logic that can become a bug surface.
- It is independently testable (frame in / frame out).
- It cannot violate the layering (e.g., caching) because it does not retain state.

This is the Kahn-process-network property the v0.1 doc name-dropped: a true pass-through channel.

---

## 9. Diffundo — Provider Cascade (resolves LLM-C2, LLM-C3, IMPL-C10)

The v0.1 cascade was dead code: the `if provider.model != model: continue` guard, combined with `model` always being resolved, meant only the first provider was ever tried. v2 replaces it.

### 9.1 Provider model

```python
@dataclass(frozen=True)
class ProviderConfig:
    name: str                       # "deepcode", "gemini", "openai", ...
    model: str                      # provider-specific model id
    tier: Literal["fast", "balanced", "strong", "reasoning"]
    api_key_env: str                # NEVER the key itself; env var name
    base_url: str | None = None
    priority: int = 0               # within tier, lower tried first
    context_window: int = 200_000   # for routing decisions
    supports_tools: bool = True     # native function calling?
    cooldown_s: float = 60.0
    max_retries: int = 2
```

### 9.2 Cascade (default mode)

```python
async def call(self, *, prompt: str, tier: str = "fast",
               model: str | None = None, temperature: float = 0.0,
               cache: bool = False, cache_namespace: str | None = None,
               context_hash: str | None = None,
               require_tools: bool = False,
               min_context_window: int = 0) -> LLMResponse:
    # 1. Cache check (only if cache=True and context_hash present)
    # 2. Filter providers: tier match; tool support if require_tools;
    #    context window if min_context_window; not in cooldown.
    # 3. Sort by priority.
    # 4. Try each in order; on exception, mark cooldown, continue.
    # 5. If all fail -> raise AllProvidersFailed(providers_tried, last_error).
```

Key changes from v0.1:

- **`tier` is the primary key.** A request for `"fast"` matches DeepCode v4 Flash, Gemini Flash, OpenAI Mini, Claude Haiku interchangeably. **No exact-model filter except when caller explicitly passes `model=`** (rare; used by optimization to pin a model).
- **Capability filtering.** `require_tools=True` skips providers with `supports_tools=False`. `min_context_window=600_000` skips Haiku. These are *explicit*, *documented* tradeoffs — not magic.
- **`AllProvidersFailed` is a real exception class**, defined in `cambium.diffundo.errors`, carrying the list of tried providers and the last error. The orchestrator catches it and parks dispatch (resolves IMPL-M5).
- **No per-call `dspy.LM` construction.** LMs are cached per provider on first use (resolves IMPL-N10).
- **Race mode** is removed from the default config (it was unsafe per LLM-M6 — fastest-typically-weakest bias, cancelled metered requests). If a caller genuinely needs "first of N," they get it by configuring N providers at the same priority; cascade returns the first success.

### 9.3 Worker-side Diffundo integration (resolves IMPL-C12)

Workers do **not** construct `dspy.LM` directly. They construct a `Diffundo` from the `fanout_config` field of `init`, with provider `api_key_env` names resolved from the inherited environment. The DSPy integration is via a custom `dspy.LM` subclass that routes calls through `Diffundo.call`:

```python
class CambiumLM(dspy.LM):
    def __init__(self, diffundo: Diffundo, tier: str, **kw):
        self._diffundo = diffundo; self._tier = tier; ...
    def __call__(self, prompt, **kw):
        return self._diffundo.call(prompt=prompt, tier=self._tier, ...)
```

Workers `dspy.configure(lm=CambiumLM(diffundo, tier="fast"))`. Every DSPy call — `ReAct`, `ChainOfThought`, raw `dspy.LM` — flows through `Diffundo`. The headline provider-failover benefit reaches workers.

---

## 10. Coding Metric (resolves LLM-C5)

There is no single number that captures "did the agent write good code." v2 uses a **multi-signal metric**, computed by the orchestrator's `ResultEvaluator` (LLM-assisted) plus deterministic checks (run by `Unio` at merge time and by `Ascensus` offline). All signals are in `[0, 1]`.

| Signal | Source | Weight (default) | Gameability mitigation |
|---|---|---|---|
| `tests` | `Unio` runs the test command (no `\| tail`, no `set -o pipefail` issue — raw exit code, see §11) | 0.30 (floor) | **Tests are a floor, not a ceiling.** A run that fails tests scores 0.0 overall regardless of other signals. |
| `spec_adherence` | LLM-judge (`ResultEvaluator`) using a fixed rubric, scored 1–5 normalized to [0,1] | 0.30 | Rubric is **pre-registered per task** in the dataset; judge sees only the spec + diff + test output, not the worker's summary. |
| `diff_quality` | Deterministic heuristics: diff size in expected range, no test-file deletion, no `# noqa`/`# type: ignore` additions, no commented-out code, no large generated files | 0.20 | Heuristics are versioned in the metric module; changes require dataset re-eval. |
| `behavioral_checks` | Pre-registered assertions per task ("function X exists", "no `print()` statements", "config files unchanged") | 0.15 | Authored at dataset construction time; not visible to the worker. |
| `canaries` | Trap assertions that should **not** pass under reward hacking (e.g., "the worker did not delete the failing test", "the worker did not add `assert True` to inflate pass rate", "no `.cambium/` writes from worker") | 0.05 (gate) | A failed canary **zeroes the entire score** regardless of other signals. |

Final score: `score = (tests × w1 + spec_adherence × w2 + diff_quality × w3 + behavioral_checks × w4) × canaries`. Weights are per-task-type in config; defaults above.

**Held-out evaluation set.** `Ascensus` ships with 20+ reference tasks (in `datasets/eval/`) with gold diffs and pre-registered rubrics. The held-out set is **never** used for training; it is the gate for shipping optimized prompts to production.

**Reward-hacking canaries.** Each held-out task ships with 3–5 canary assertions designed to detect the failure modes the metric would otherwise incentivize (deleting failing tests, no-op patches, `# noqa` additions, etc.). A prompt variant that improves the training metric while regressing the canary rate is **rejected** by the optimization harness, even if its score went up.

---

## 11. Worker Tool Set (resolves LLM-M2, IMPL-C4, IMPL-C5, IMPL-N4)

| Tool | Implementation | Notes |
|---|---|---|
| `read_file(path)` | `Path.read_text(encoding="utf-8")` | Rejects paths outside the worktree. |
| `write_file(path, content)` | `Path.write_text(content, encoding="utf-8")` | **NOT `write_content`** (the v0.1 bug). Atomic via temp-file + `os.rename`. |
| `edit_file(path, old_string, new_string)` | Search-and-replace with **uniqueness check**: errors if `old_string` matches 0 or >1 locations. | **New.** Closes the "agent must rewrite the whole file" gap. Matches Claude Code / Aider conventions. |
| `run_shell(cmd, timeout=120)` | `asyncio.create_subprocess_shell`, wrapped in per-tool heartbeat loop (§7.6). | `shell=True` is allowed because the worker runs in a sandbox with a bounded tool set and process-group kill; the alternative (parsing shell) is worse. **Every** command is logged in the event log. |
| `git_op(op, args)` | `subprocess.run(["git", op, *shlex.split(args)])` — **list form, no shell** | Eliminates the v0.1 shell-injection vector. `op` is allowlisted (`add`, `commit`, `status`, `diff`, `log`, `stash`); others rejected. |
| `grep_code(pattern, path)` | `subprocess.run(["rg", "-n", pattern, path])` — **uses ripgrep, list form** | Eliminates the `grep -rn '{pattern}'` injection vector (IMPL-N4). Falls back to stdlib `re` if `rg` not on PATH. **Always `return`s the result** (fixes IMPL-C5). |

**Tools that are deliberately absent** at v2:

- No `fetch_url` / `curl` tool. Network egress is gated by sandbox policy (off by default).
- No structured-edit patch tool. The `edit_file` search-and-replace primitive covers the common case; full diff/patch parsing is deferred to v2.1.
- No AST/symbol search. Planned for v2.1.

**All tool calls** are wrapped by the heartbeat-emitting tool runner, so even `run_shell(cmd, timeout=300)` cannot trip the watchdog.

---

## 12. Secrets Management (resolves IMPL-M6)

### 12.1 Threat model

- **At rest:** no API key is ever written to disk by Cambium. Keys live in the host process's environment.
- **In transit to workers:** keys are inherited via the subprocess environment; they never appear in protocol messages.
- **In logs:** every event passes through a redaction filter before it reaches the writer thread.
- **In sandbox:** the sandbox wrapper injects only the env keys the worker is authorized to receive via `--setenv`.

### 12.2 Loading

```python
# cambium/config.py
def load_providers(config_path: Path) -> tuple[ProviderConfig, ...]:
    """Parse providers.toml; resolve api_key_env to env-var NAMES (not values)."""
    raw = tomllib.loads(config_path.read_text())
    providers = []
    for name, spec in raw["providers"].items():
        env_var = spec["api_key_env"]   # e.g. "DEEPCODE_API_KEY"
        if env_var not in os.environ:
            raise ConfigError(f"Provider {name}: env var {env_var} not set")
        providers.append(ProviderConfig(
            name=name, model=spec["model"], tier=spec["tier"],
            api_key_env=env_var, ...))
    return tuple(providers)
```

`providers.toml` contains **only env-var names**, never key values. The host is responsible for setting the environment (systemd `EnvironmentFile`, k8s secrets, shell export, etc.).

### 12.3 Redaction filter

```python
REDACT_KEYS = re.compile(r"(api[_-]?key|token|secret|password|auth)", re.I)
REDACT_VALUES = re.compile(r"(sk-[A-Za-z0-9]{20,}|AIza[A-Za-z0-9_-]{35}|...)")

def redact(payload: dict) -> dict:
    return {k: ("***" if REDACT_KEYS.search(k) else
                REDACT_VALUES.sub("***", str(v)) if isinstance(v, str) else v)
            for k, v in payload.items()}
```

Applied at enqueue time (before the writer thread sees the event). Belt-and-braces: the writer thread applies it again before INSERT.

---

## 13. Logging (resolves IMPL-M7)

- **stdlib `logging`** with a `JsonFormatter` (no third-party logging lib). One formatter, defined in `cambium.logging`.
- **Non-blocking:** every logger is wired with a `logging.handlers.QueueHandler` that feeds a single `QueueListener` running on a background thread. The listener writes to a `logging.handlers.RotatingFileHandler` (100 MB × 5 files) at `${session_dir}/cambium/cambium.log`.
- **Per-module loggers:** `cambium.nuntius`, `cambium.diffundo`, ..., `cambium.opifex.<task_id>`. Levels configurable per module in config.
- **Correlation:** every record carries `task_id`, `request_id`, `generation`, ` monotonic_ms`. Set via `logging.LoggerAdapter` per task.
- **Redaction:** a `logging.Filter` applies the same redaction as §12.3.
- **stderr from workers** is captured by the supervisor and forwarded to the event log as `kind="log"` events with `level` and `module` fields parsed from common prefixes (`WARNING`, `ERROR`, etc.). Unparseable stderr lines are stored verbatim at level `INFO`.

---

## 14. Python Stance

- **`requires-python = ">=3.14,<3.15"`** in `pyproject.toml`. Pinned to 3.14, as required by the task.
- **Standard CPython build.** Free-threaded (`python3.14t`) is **not** required, **not** the default, and **not** recommended for v2. Rationale:
  - Workers are separate **processes**, not threads; the GIL is irrelevant to inter-worker parallelism.
  - The supervisor is single-threaded asyncio.
  - The only multi-threaded code is `Ascensus` (offline SIMBA fan-out); when needed, it can opt into free-threading via `pyproject.toml` extras.
  - Free-threading adds 10–40% single-threaded overhead and C-extension risk (DSPy, LiteLLM, torch).
- **Free-threading is an opt-in extra** for users who want SIMBA parallelism:
  ```toml
  [project.optional-dependencies]
  free_threaded = []  # marker only; document that user supplies python3.14t
  ```
- **`asyncio.to_thread`** is used for the rare synchronous, CPU-light blocking call inside the supervisor (e.g., `git` invocations). It runs on the default thread pool, which is fine because no shared mutable state is touched inside those calls.
- **Subprocess-per-worker** design means each worker is a fresh Python interpreter. Cold-start cost is documented (IMPL-M2) and mitigated by `ready_timeout`. A persistent worker pool is **deferred to v2.1** — it requires a different IPC model (multiple init messages per process) and is not needed for v2 correctness.

---

## 15. TUI Policy

- **The TUI (`Janus`, M10) is a view, not a controller.** It subscribes to `Session.events()` and renders `Event` objects. It does not call `Custos` directly.
- **Headless-first.** Every feature reachable from the TUI is reachable from the public API. If a feature is TUI-only, that's a bug.
- **The TUI is optional at runtime.** `pip install cambium` does not require the TUI's dependencies; `pip install cambium[tui]` adds them. The TUI lives in `cambium.tui` behind the extra.
- **The machine interface is JSON-Lines events.** A host system that wants to render its own UI reads `Session.events()` exactly as the TUI does. There is no second API.
- **TUI is NOT in scope for v2 P0.** It is P2 and depends only on the Public API.

---

## 16. Proto-AGI Integration — Cambium as a Leaf Module

A proto-AGI host treats Cambium the way Cambium treats a worker: as a subprocess with a structured contract. This section defines that contract.

### 16.1 Control plane vs data plane

- **Control plane** (lifecycle): `spawn`, `poll`, `wait`, `stop`, `kill`, `query`. Owned by `Instance` (§3.5). Transport: in-process function calls if Cambium is embedded as a library, or process signals/stdin if Cambium runs as a standalone subprocess wrapping `cambium.cli`.
- **Data plane** (work): the `Result` envelope (§3.4) and the event log. The host reads `Result` from `${session_dir}/cambium/result.json` after `Instance.wait()` returns, or subscribes to `Session.events()` for live observation.

### 16.2 Session directory contract

The host owns `${session_dir}/`. Cambium owns **only** `${session_dir}/cambium/`:

```
${session_dir}/
├── host-controlled files         # upper system's state
└── cambium/                       # Cambium owns everything below
    ├── events.db                  # SQLite WAL
    ├── events.jsonl               # optional mirror
    ├── cambium.log                # rotated logs
    ├── result.json                # written atomically on completion
    ├── status.json                # written on every state change (read by poll())
    ├── worktrees/                 # one subdir per active worktree
    ├── checkpoints/               # one subdir per task
    ├── quarantine/                # worktrees that failed recovery
    └── optimized/                 # DSPy artifacts loaded by Ascensus
```

### 16.3 Lifecycle

```
HOST                                CAMBIUM (Instance)
────                                ─────────────────
spawn(spec, session_dir, ...)   ──► [PENDING]
                                    [running]
poll()                         ◄──   status="running", workers_alive=3, phase="merging"
wait(timeout)                  ◄──   blocks
                                    [done|failed|rejected|timeout|cancelled]
                                  Result written to result.json
                                  ◄── returns Result
```

- **Spawn** is async; returns an `Instance` immediately. The host can `poll()` for status or `await wait()`.
- **Stop** is graceful: Cambium cancels in-flight workers, runs the merge sequencer to a safe point, flushes the event log, writes `result.json` with `status="cancelled"`, and exits with code 4.
- **Kill** is immediate: SIGTERM the supervisor's process group, then SIGKILL after 5 s. The event log may be truncated; `result.json` is **not** written.
- **Query** is a read-only accessor for non-final state. Supported fields: `events_summary`, `workers_alive`, `current_phase`, `commits_so_far`. Never blocks; returns `"unknown"` if not yet available.

### 16.4 Stable contract guarantees

The host may rely on the following invariants across v2.x:

1. **`result.json` is written atomically** (temp + rename) before `Instance.wait()` returns. The host can poll for its presence to detect completion without `wait()`.
2. **Exit codes are stable:** `0` done, `1` failed, `2` rejected, `3` timeout, `4` cancelled, `>100` supervisor crash.
3. **`events.db` is always recoverable.** SQLite WAL is crash-safe by construction; replay = open the DB and read `events` since the last `snapshot`.
4. **The `${session_dir}/cambium/` layout is stable.** The host can archive it without parsing.
5. **No implicit global state.** Two Cambium instances in two different `session_dir`s do not interfere. No `/tmp/cambium-*` files; no `~/.cambium`; no shared caches.

---

## 17. DSPy-Per-Module Strategy (resolves LLM-C4)

### 17.1 The coupling problem, restated

`Architectus` has four DSPy modules: `ShouldDecompose`, `TaskDecomposer`, `TaskRouter`, `ResultEvaluator`. `Opifex` has its own worker ReAct module. v0.1 claimed all five were "independently hill-climbable." They are not: the worker metric depends on the decomposer's output, the decomposer metric depends on the worker's competence, etc. SIMBA on one module with the others held fixed is a moving-target optimization.

### 17.2 Decoupling via pinned siblings and held-out eval

Each module is optimized against **frozen references** of its siblings, not their live co-adapted versions.

| Module | Optimization input | Sibling pinning | Held-out metric |
|---|---|---|---|
| `ShouldDecompose` | `spec → bool` | None needed (input is just the spec). | Accuracy + F1 + calibration on a frozen 200-spec dataset. |
| `TaskDecomposer` | `spec → list[SubTask]` | **Stub Worker** that returns canned results per subtask ID. | Subtask-completion rate on a frozen 50-spec dataset with pre-registered gold decompositions. |
| `TaskRouter` | `subtask, worker_profiles → route` | Stub Worker pool with declared tiers. | Routing accuracy vs gold routing on 100 cases. |
| `ResultEvaluator` | `spec, diff, test_results → verdict` | None (input is post-hoc). | Verdict accuracy + F1 on 100 hand-labeled (spec, diff, verdict) triples. |
| `Opifex` (worker ReAct) | `task, context → action` | **Stub Decomposer** that returns the canonical decomposition for each task. | Multi-signal metric (§10) on 50 reference coding tasks. |

### 17.3 Per-module artifacts

Each module ships, under `src/cambium/modules/<name>/`:

```
src/cambium/modules/<name>/
├── architecture.md           # per-module design (template: docs/module-template/architecture.md)
├── program.py                # the DSPy Module subclass
├── metric.py                 # metric function
├── eval.py                   # eval harness entry point
├── datasets/
│   ├── train.jsonl           # versioned (see docs/module-template/dataset-format.md)
│   ├── eval.jsonl            # frozen held-out
│   └── canaries.jsonl        # reward-hacking traps
└── siblings-stub.yaml        # which sibling versions this module was last optimized against
```

### 17.4 Optimization loop (in `Ascensus`)

```
1. Pick a module M to optimize.
2. Load its current production version: M_v.
3. Load the pinned siblings declared in siblings-stub.yaml.
4. Load train.jsonl.
5. Run SIMBA (or GEPA) on M_v against train.jsonl using metric.py.
6. Produce M_v+1.
7. Score M_v+1 on the frozen eval.jsonl.
8. Score M_v+1 on canaries.jsonl. If any canary regresses below threshold → REJECT.
9. If eval improved AND canaries OK AND a human approves → promote M_v+1 to production.
10. Update siblings-stub.yaml in OTHER modules if M's interface changed.
```

Steps 8 and 9 are the **brakes** the v0.1 flywheel lacked.

### 17.5 Independence claim, restated

> Modules are *per-module optimizable* against frozen references of their siblings. They are **not** jointly optimized. Joint optimization is a v2.1 research question that requires solving the non-stationarity explicitly; v2 ships without it.

---

## 18. Resolution Matrix — Every CRITICAL Flaw

For each CRITICAL item in the three reviews, the mechanism v2 uses to resolve it. "Status" is **resolved** (mechanism in this document) or **rejected** (the design choice is intentional and the critique does not apply, with reasoning).

### 18.1 Distributed Systems review (`review-distributed-systems.md`)

| ID | Flaw | Resolution | Section |
|---|---|---|---|
| DS-C1 | Sync file I/O in event loop → cascade kill-chain | Event log writer moved to a dedicated single-consumer thread; bounded queue; supervisor only does `queue.put_nowait()`. | §6.2 |
| DS-C2 | "stdout EOF = worker dead" unsound (4 failure modes) | Four-layer liveness model; `exit` message authoritative; EOF is advisory; process-group kill; `PYTHONUNBUFFERED=1`; drain-deadline watchdog. | §5.3, §5.4 |
| DS-C3 | Heartbeat granularity (60 s < 120 s tool timeout) | Per-tool heartbeat loop inside long-running tool wrappers; default heartbeat interval 15 s, timeout 90 s. | §7.6 |
| DS-C4 | Restart: no jitter + rate-window gaming | Full jitter; burst cap + absolute cap (10) + per-task wall budget. | §7.4 |
| DS-C5 | Stale worktree locks survive crashes | `Surculus.recover()` runs before every respawn: lock cleanup, rebase/merge abort, `reset --hard`, `clean -fd`, optionally cherry-pick checkpoint commits. | §7.5 |
| DS-C6 | Supervisor crash orphans workers / split-brain | Generation fencing token; `start_new_session=True` + process-group kill on startup; quarantine-and-fresh-create fallback. | §7.3, §7.5 |
| DS-M1 | Merge sequencer serialization bottleneck | `asyncio.Lock`; throwaway worktree; batch-then-test mode (configurable); fast pre-merge checks, full suite once. | §4, §7 (Unio) |
| DS-M2 | Race on `WorkerHandle` state | State machine with guarded transitions; mutations serialized through the supervisor's single event-loop task; `ProcessLookupError` caught in watchdog. | §7.1 |
| DS-M3 | Event log no durability (no fsync) | SQLite WAL (atomic by construction); explicit fsync cadence. | §6.1, §6.2 |
| DS-M4 | FanOut cache/provider state unsafe under threads | Cache is per-instance and only mutated from the owning process; cascade no longer uses `asyncio.to_thread` for shared-state mutation (LM construction cached per provider; cooldown tracked in a `threading.Lock`-protected structure when needed). | §8.1, §9 |
| DS-M5 | Python 3.14 free-threaded unnecessary | Standard CPython 3.14; free-threading is opt-in extra; documented. | §14 |
| DS-M6 | Orchestrator cycle detection / broken task-ID counter | DAG validation in `Architectus`: topological sort with cycle detection before dispatch; cyclic graphs rejected and re-prompted; ULID-based `request_id` and `task_id` from `Custos`. | §4 (Architectus) |
| DS-M7 | No isolation between FanOut failure and worker liveness | Workers retry provider outage inside the tool boundary for `provider_patience_s` before emitting `error`; provider outage no longer kills workers. | §7.4 |
| DS-N1 | Code-level runtime bugs (write_content, missing return, missing import, etc.) | All bugs fixed in v2 tool spec; see §11 and the implementation contract docs per module. |
| DS-N4 | `grep_code` shell-injection vector | Uses **ripgrep** with list-form `subprocess.run`; no shell. | §11 |
| DS-N5 | Event log grows unbounded | Per-session SQLite DB with snapshot compaction; rotation for JSON-Lines mirror; host owns session dir cleanup. | §6 |
| DS-N6 | Kahn/CSP labels are name-dropping | v2 retains "Kahn-style pass-through" only for `Nuntius` where it is structurally true; drops CSP label. | §8.2 |
| DS-N7 | `shutdown()` doesn't clean worktrees | Shutdown explicitly calls `worktree_mgr.prune()` and removes (or quarantines) active worktrees. | §7.7 |

### 18.2 LLM Design review (`review-llm-design.md`)

| ID | Flaw | Resolution | Section |
|---|---|---|---|
| LLM-C1 | FanOut cache ignores repo state | Opt-in per call; key includes `context_hash` (caller-supplied); default TTL 300 s; cache hits tagged. | §8.1 |
| LLM-C2 | Cascade not cascading across models | `tier` field; cascade tries all providers in tier; no exact-model filter except when caller explicitly pins. | §9.2 |
| LLM-C3 | Provider/model transparency assumed | Capability metadata on `ProviderConfig` (`supports_tools`, `context_window`); `require_tools` and `min_context_window` filters; tradeoffs documented. | §9.1, §9.2 |
| LLM-C4 | "Independently hill-climbable" is false | Claim restated: "per-module optimizable against pinned siblings"; held-out eval per module; canary rejection. | §17 |
| LLM-C5 | No automatic metric for coding tasks | Multi-signal metric: tests (floor) + spec-adherence (LLM judge) + diff-quality (heuristics) + behavioral checks + canaries (gate). | §10 |
| LLM-C6 | No "do not decompose" path | `ShouldDecompose` classifier module; single-task fast path bypasses decomposition. (Spec'd as the reference example module — `docs/module-template/example-spec.md`.) | §4 (Architectus), §17 |
| LLM-M1 | Default test command broken (`\| tail`) | `Unio` uses raw `subprocess.run` exit code; no shell pipe; full output captured, truncated in Python. | §10, §11 |
| LLM-M2 | Worker tool set inadequate | Adds `edit_file` (search-and-replace with uniqueness); fixes `write_file`/`grep_code`; structured-edit tool documented as v2.1. | §11 |
| LLM-M3 | Optimization flywheel coupled, no stability | Held-out eval, canaries, human gate, rollback. | §10, §17.4 |
| LLM-M4 | `ReAct` checkpoint callback doesn't exist in DSPy | v2 implements checkpointing via a `ReAct` subclass (`OpifexReAct`) that overrides the step loop to call `checkpoint()` between steps; documented in `Opifex` architecture. | §6.4, module template |
| LLM-M5 | Cache per-instance nearly useless | Cache is **upstream of workers** (orchestrator-side, for genuinely stateless calls); worker caches are private and opt-in. The "shared cross-worker cache" benefit is the host's job. | §8.1 |
| LLM-M6 | Race mode unsafe (cancellation, weak-model bias) | Race mode removed from default; same-priority providers in cascade give equivalent latency behavior without the pathologies. | §9.2 |

### 18.3 Implementation review (`review-implementation.md`)

| ID | Flaw | Resolution | Section |
|---|---|---|---|
| IMPL-C1 | Merge sequencer no concurrency guard | `asyncio.Lock` around the sequencer; operates in throwaway worktree. | §4 (Unio), §7 |
| IMPL-C2 | `self.root` undefined | v2 normative spec uses `repo_root`; reviewed at module-template level. | §4 |
| IMPL-C3 | `os.getpid()` without importing `os` | Fixed in the worker spec (`Opifex` imports `os`). | §11 |
| IMPL-C4 | `write_content` nonexistent | v2 spec uses `Path.write_text`. | §11 |
| IMPL-C5 | `grep_code` no return | v2 spec returns the combined output. | §11 |
| IMPL-C6 | `def __task_id_counter` syntax error | Removed; task IDs assigned by `Custos` via ULID. | §4 (Architectus) |
| IMPL-C7 | Sandbox space in identifier + undefined `sys` | v2 `Septum` spec uses valid identifiers and imports `sys`. | §4 (Septum) |
| IMPL-C8 | Orchestrator awaits sync methods / undefined merge/evaluate | `Architectus` interface normatively defined; sync vs async decided per method. | §4 (Architectus) |
| IMPL-C9 | Metric syntax errors (`polymorphism`, missing `==`) | v2 metric module is syntactically valid Python; tested before merge. | §10 |
| IMPL-C10 | Cascade no-op when model resolved | Resolved by LLM-C2 mechanism. | §9.2 |
| IMPL-C11 | `shutdown()` calls `.kill()` on Tasks | v2 shutdown uses process objects directly; awaiting `proc.wait()` via wrapped tasks; correct API. | §7.7 |
| IMPL-C12 | Worker bypasses FanOut | Workers construct `Diffundo` from `fanout_config` and route DSPy through `CambiumLM`. | §9.3 |
| IMPL-M1 | Python 3.14 free-threaded experimental | Standard 3.14; free-threading opt-in. | §14 |
| IMPL-M2 | Subprocess cold-start unbounded | Documented; `ready_timeout` (default 60 s); persistent pool deferred to v2.1. | §14 |
| IMPL-M3 | Git worktree concurrency / `gc.auto` | `Surculus` sets `gc.auto=0` on the cambium-managed repo; retries `worktree add` on lock contention; never mutates `main` from worker code. | §7 (Surculus) |
| IMPL-M4 | bubblewrap Linux-only | `Septum` has `BwrapSandbox` (Linux), `SandboxExecSandbox` (macOS, best-effort), `NoopSandbox` (dev/CI). | §4 (Septum) |
| IMPL-M5 | `AllProvidersFailed` undefined / unhandled | Defined in `cambium.diffundo.errors`; orchestrator catches it and parks dispatch. | §9.2 |
| IMPL-M6 | No secrets management | Env-only; redaction filter; never in JSON init; sandbox `--setenv` per-worker key allowlist. | §12 |
| IMPL-M7 | No real logging | stdlib `logging` + `JsonFormatter`; `QueueHandler` + `QueueListener`; rotation; correlation IDs. | §13 |
| IMPL-M8 | No test strategy | Module template requires test strategy; `Ascensus` ships with fake-LLM and fake-worker harnesses. | §17, module template |
| IMPL-M9 | Restart reuses corrupted worktree | Fixed by `Surculus.recover()` (DS-C5). | §7.5 |
| IMPL-M10 | Heartbeat timing coarse / readiness gap | Configurable interval/timeout; supervisor **waits for `ready`** before sending further messages. | §7.2 |
| IMPL-N1..N14 | Various code-quality bugs | All fixed by the v2 normative specs; verified by the module-template's "smoke test passes" gate. | per-module |

### 18.4 Consensus items (`system-design.md` §9 table)

Every F1–F12 item in the v0.1 consensus table is resolved by the matrix above:
F1=§6.2, F2=§8.1, F3=§7(Unio), F4=§9.3, F5=§9.2, F6=§7.6, F7=per-module specs, F8=§11, F9=§17, F10=§10, F11=§6, F12=§4(Septum).

---

## 19. Why Projects Succeed or Fail

Concrete factors, and how this design addresses each. Ordered roughly by observed frequency of failure in comparable systems.

1. **Testability without TDD.** TDD is not required, but every module ships with a test strategy (template field) and a smoke test that must pass before the module is marked P0-complete. The v0.1 reviews identified ~12 syntax bugs that a single dry run would have caught — that gate now exists. Addressed: §17, `docs/module-template/architecture.md` (test strategy field).

2. **Verifiable metrics.** Every DSPy module has a metric that runs without human-in-the-loop scoring, against a frozen held-out set. Without this, optimization hill-climbs toward a proxy. Addressed: §10, §17.

3. **Canaries against reward hacking.** Every held-out task ships with 3–5 trap assertions designed to detect the failure mode the metric would otherwise incentivize. A prompt variant that improves training metric while regressing canary rate is rejected. Addressed: §10, §17.4 step 8.

4. **Incremental milestones.** Each module is independently buildable and independently testable. The build phases (P0/P1/P2) have explicit entry conditions, not just exit conditions. Addressed: §4; per-module `architecture.md`.

5. **Adversarial review gates.** Every module passes an adversarial review before merge; integration reviews re-run on every cross-module contract change. The three v0.1 reviews are the template for what a review looks like. Addressed: `docs/reviews/` are now first-class artifacts; `agents.md` documents the review gate.

6. **No hidden global state.** Config is explicit (`Config` dataclass, frozen). No module-level mutables. All runtime state lives under `${session_dir}/cambium/`. Two Cambium instances in two session dirs do not interfere. Addressed: §16.2 invariant 5.

7. **Fail-fast on invariant violations.** Generation mismatches, parse errors, lock files, missing providers, missing env vars — all cause explicit failure with a typed event in the log, never silent corruption. The system tells you when it is broken. Addressed: §5, §7.3, §12.

8. **Honest claims.** The v0.1 "independently hill-climbable" claim is restated as a hypothesis (§17). The "Temporal-style durability" claim is backed by SQLite WAL (§6), not by append-only file writes. The "zero dependencies" claim is dropped (DSPy pulls LiteLLM et al.). Honest claims survive contact with production; marketing claims don't.

9. **I/O off the hot path.** Every disk write is on a dedicated thread or in a subprocess. The supervisor's event loop never blocks on disk. Addressed: §6.2, §13.

10. **Explicit concurrency guards.** Merge sequencer is locked. FanOut cooldown is locked. Event log is single-writer. Worker subprocesses are in their own process group. Nothing races implicitly. Addressed: §6.2, §7, §8, §9.

11. **Cross-platform from day 1.** The sandbox has Linux, macOS, and noop backends. The design does not assume bubblewrap. macOS is a first-class dev platform. Addressed: §4 (Septum).

12. **Secrets handled once, correctly.** Env-only at rest, inherited via subprocess env, never in protocol messages, redacted at the log boundary, sandboxed per-worker via `--setenv`. Documented threat model. Addressed: §12.

13. **Real logging.** stdlib `logging`, structured (JSON), non-blocking (QueueHandler/QueueListener), rotated (100 MB × 5), redacted, correlated (task_id + request_id + generation). No `print()` in worker code. Addressed: §13.

14. **Bounded everything.** Restarts (10 absolute), wall time (1800 s/task), memory (event ring buffer 10 000; queue 10 000), cache (LRU 10 000), log size (rotation), worker count (config). No resource grows without bound. Addressed: §6.2, §7.4, §8.1.

15. **Smoke test as gate.** No module is marked complete until the end-to-end smoke test (fake LLM + 1 worker + 1 merge) passes against it. This is the single highest-leverage practice the v0.1 reviews identified. Addressed: `agents.md` documents the gate; the example module spec (`docs/module-template/example-spec.md`) demonstrates it.

**Failure modes this design does not yet address (honest gaps):**
- Cold-start latency for subprocess-per-worker (mitigation documented; persistent pool deferred).
- Cross-model prompt transfer during optimization (documented; mitigation is per-model optimization, deferred to v2.1).
- Macos sandbox is weaker than Linux bwrap (documented as best-effort).
- The "doom loop detector" pattern from Claude Code is on the v2.1 list, not in v2.

---

## 20. References

- `docs/system-design.md` — v0.1 draft (superseded).
- `docs/reviews/review-distributed-systems.md` — DS review (391 lines).
- `docs/reviews/review-llm-design.md` — LLM review (242 lines).
- `docs/reviews/review-implementation.md` — implementation review (326 lines).
- `docs/module-template/architecture.md` — per-module design template.
- `docs/module-template/dataset-format.md` — dataset JSONL schema, versioning, splits, canaries.
- `docs/module-template/example-spec.md` — reference module (`ShouldDecompose`) for first implementation.
- `agents.md` — repo-root orientation for new agents.
