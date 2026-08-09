# Cambium — System Design Document

**Version:** 0.1.0-draft
**Date:** 2026-08-09
**Status:** Pre-implementation, adversarial-review-ready

---

## 0. TL;DR

Cambium is a Python-native multi-agent coding harness. A persistent **supervisor** process manages N **worker** processes. Workers run DSPy ReAct loops in isolated git worktrees. IPC is stdin/stdout JSON lines. One worker dies — the rest survive. The supervisor is both a deterministic process manager and an LLM-driven orchestrator. Every node in the system is a DSPy module that can be independently hill-climbed.

**Primary CS patterns:** Erlang/OTP supervisor tree (one\_for\_one, transient) + Temporal-style durable execution (idempotent activities, heartbeats, checkpoints) + Kahn process network IPC (blocking reads, deterministic ordering) + CSP event loop (asyncio selecting over channels).

**Language:** Python 3.14 (free-threaded, no-GIL). Target; 3.12+ works without true parallelism.

**Zero external runtime dependencies** beyond Python stdlib + DSPy + git.

---

## 1. Naming

**Cambium** — the growth layer between bark and wood in trees. It's where new cells form. The supervisor is the cambium layer: it doesn't do the visible work (wood = workers), but it generates and sustains it. Short, memorable, unclaimed in the coding-agent space.

### Module Naming Convention — Latin Botanical Terms

All modules carry Latin names following the tree-growth metaphor. Each name reflects the module's role in the system.

| Code | Latin Name | Meaning | Role |
|---|---|---|---|
| M1 | **Nuntius** | Messenger | IPC protocol — carries messages between supervisor and workers |
| M2 | **Diffundo** | I spread out / I pour forth | FanOut — spreads LLM calls across providers |
| M3 | **Surculus** | Shoot / sucker | Worktree Manager — each worktree is a new shoot from the trunk |
| M4 | **Custos** | Guardian / overseer | Supervisor — watches over workers, never does the work itself |
| M5 | **Opifex** | Craftsman / worker | Worker Runtime — does the actual coding work |
| M6 | **Architectus** | Master builder / planner | Orchestrator — plans and decomposes tasks |
| M7 | **Unio** | Union / becoming one | Merge Sequencer — fuses worker branches back together |
| M8 | **Septum** | Enclosure / partition | Sandbox — isolates each worker from the rest |
| M9 | **Ascensus** | Ascent / climbing | Optimization — hill-climbing the DSPy modules |
| M10 | **Janus** | God of transitions / doors | CLI/TUI — the interface to the outside world |

The tree metaphor is consistent throughout:

```
Cambium (growth layer — the whole system)
├── Custos (root — supervises everything)
│   ├── Architectus (plans the growth)
│   ├── Diffundo (draws water/nutrients = LLM calls)
│   └── Nuntius (vascular tissue = IPC channels)
├── Opifex × N (leaves — each does work)
│   ├── Surculus (each has its own shoot = worktree)
│   └── Septum (bark = sandbox isolation)
├── Unio (fuses shoots together = merge)
├── Ascensus (grows upward = optimization)
└── Janus (the gate to the outside = CLI)
```

---

## 2. CS Foundations

### 2.1 Pattern Inventory

| Concern | Pattern | Source | Why |
|---|---|---|---|
| Worker lifecycle | one\_for\_one, transient restart, intensity/period | Erlang/OTP | Workers are independent; only crashed worker restarts |
| Worker identity | Stable task ID, not PID | Unix supervision (s6) | PIDs are volatile; task IDs are stable references |
| Worker readiness | `{"type":"ready"}` signal before accepting work | s6 notification FD | "Process alive" ≠ "ready"; Python import takes time |
| Crash recovery | Checkpoint ReAct state after each tool call | Temporal heartbeats | Resume from last checkpoint, not from scratch |
| Side-effect safety | Idempotency keys for git operations | Temporal activities | Retries must be safe; deterministic keys prevent double-commits |
| IPC semantics | Blocking reads, non-blocking writes, unique writer/reader per pipe | Kahn process networks | Deterministic message delivery; no races |
| Supervisor internals | asyncio event loop selecting over result/timeout/control channels | CSP | Race-free multiplexing; explicit backpressure |
| Restart safety | Mandatory 1s+ delay between restarts; backoff | s6 + Erlang | Prevents busy-looping on a crashing worker |
| Failure handling | Let it crash; don't defensively program inside workers | Erlang philosophy | Workers stay simple; supervisor handles recovery |
| Git rollback | Saga pattern: register compensation before each step | Temporal Saga | Multi-step git operations need rollback |

### 2.2 What We Explicitly Avoid

| Anti-pattern | Why | Seen in |
|---|---|---|
| one\_for\_all restart for independent workers | Nukes all in-flight work on single failure | (Erlang misuse) |
| `.pid` files for worker identity | Race-prone; supervisor is the parent, knows PID | Prime Agent |
| Unix socket + lock-file supervision | Socket path limits (macOS), stale locks, snapshot corruption | Prime Agent |
| Shared mutable state between workers | Data races, undefined behavior | (Generic) |
| Defensive try/except everywhere in workers | Hides bugs; let it crash + supervisor restart | (Erlang warns against this) |
| `uuid4()` for idempotency keys | Must be deterministic across retries | (Temporal anti-pattern) |
| Bidirectional agent-to-agent messaging | Degenerates into ACK loops | OpenCode community |
| Prompt-instructed concurrency policy | Model may not follow "parallelize reads, serialize writes" | Claude Code |
| Sequential subagent dispatch | Kills parallelism entirely | OpenCode (#29638) |
| Subagent without timeout | Hangs 20-30 min silently | OpenCode (#11865) |

---

## 3. System Architecture

### 3.1 High-Level Diagram

```
┌─────────────────────────────────────────────────────────────┐
│                     CAMBIUM SUPERVISOR                       │
│                                                              │
│  ┌──────────────┐  ┌──────────────────┐  ┌────────────────┐ │
│  │  Deterministic│  │   Architectus    │  │   Diffundo     │ │
│  │   (Custos)    │  │   (LLM, DSPy)    │  │   (LLM Access) │ │
│  │               │  │                  │  │                │ │
│  │ • spawn/kill  │  │ • decompose task │  │ • cascade      │ │
│  │ • heartbeat   │  │ • route subtasks │  │ • race         │ │
│  │ • crash reco- │  │ • evaluate result│  │ • cache        │ │
│  │   very        │  │ • retry / reject │  │ • quota track  │ │
│  │ • merge seq   │  │                  │  │                │ │
│  │ • event log   │  │  (DSPy module,   │  │  (shared by    │ │
│  │ • worktree mgmt│ │   hill-climbable)│  │   both layers) │ │
│  └──────┬────────┘  └────────┬─────────┘  └───────┬────────┘ │
│         │                    │                     │         │
│  ┌──────┴────────────────────┴─────────────────────┴───────┐ │
│  │                   Event Loop (asyncio)                   │ │
│  │  select over: [worker stdout pipes, timers, ctrl channel]│ │
│  └──────┬─────────────┬─────────────┬─────────────┬────────┘ │
│         │             │             │             │          │
└─────────┼─────────────┼─────────────┼─────────────┼──────────┘
          │             │             │             │
     ┌────▼────┐   ┌────▼────┐   ┌────▼────┐   ┌────▼────┐
     │ Worker 1│   │ Worker 2│   │ Worker 3│   │ Worker N│
     │ (Opifex)│   │ (Opifex)│   │ (Opifex)│   │ (Opifex)│
     │         │   │         │   │         │   │         │
     │ DSPy    │   │ DSPy    │   │ DSPy    │   │ DSPy    │
     │ ReAct   │   │ ReAct   │   │ ReAct   │   │ ReAct   │
     │         │   │         │   │         │   │         │
     │ worktree│   │ worktree│   │ worktree│   │ worktree│
     │  A      │   │  B      │   │  C      │   │  N      │
     │         │   │         │   │         │   │         │
     │ stdin ► │   │ stdin ► │   │ stdin ► │   │ stdin ► │
     │◄ stdout │   │◄ stdout │   │◄ stdout │   │◄ stdout │
     └─────────┘   └─────────┘   └─────────┘   └─────────┘
```

### 3.2 Module Decomposition (Parallelizable Implementation)

Each module has a **clear interface boundary**, a **testable contract**, and can be **built independently**.

| Module | Latin Name | Responsibility | Dependencies | Est. Lines | Priority |
|---|---|---|---|---|---|
| **M1: IPC Protocol** | **Nuntius** | JSON-lines message types, serialization, framing | None (stdlib) | ~300 | P0 |
| **M2: FanOut** | **Diffundo** | Multi-provider LLM access with cascade/race/cache | DSPy, LiteLLM | ~500 | P0 |
| **M3: Worktree Manager** | **Surculus** | `git worktree add/remove`, path allocation, cleanup | git CLI | ~200 | P0 |
| **M4: Supervisor (Deterministic)** | **Custos** | Process lifecycle, heartbeat, crash recovery, event log | M1, M3 | ~800 | P0 |
| **M5: Worker Runtime** | **Opifex** | DSPy ReAct loop, tool execution, checkpointing | M1, M2 | ~600 | P0 |
| **M6: Orchestrator** | **Architectus** | LLM-driven task decomposition, routing, evaluation | M2, DSPy | ~400 | P1 |
| **M7: Merge Sequencer** | **Unio** | Sequential rebase merge, conflict detection, test gate | git CLI, M3 | ~300 | P1 |
| **M8: Sandbox Wrapper** | **Septum** | namespace process wrapping, per-task policy | namespace wrapper, firejail | ~150 | P2 |
| **M9: DSPy Optimization Harness** | **Ascensus** | Record trajectories, define metrics, run SIMBA/GEPA | DSPy, M5 | ~400 | P2 |
| **M10: CLI / TUI** | **Janus** | User interface, task submission, status monitoring | M4, M6 | ~500 | P2 |

**Total estimate:** ~4,150 lines of Python. With DSPy handling the LLM abstraction and git handling isolation, this is lean.

---

## 4. Module Specifications

### M1: Nuntius — IPC Protocol

The simplest possible protocol that is correct. Newline-delimited JSON over stdin/stdout pipes.

#### Message Types

```jsonl
// Supervisor → Worker (via worker stdin)
{"type":"init","task_id":"wt-abc-001","worktree":"/path/to/wt","spec":"Refactor dry_run.rs to remove global state","max_turns":20,"tools":["read_file","write_file","run_shell","git_op","grep"],"model":"deepcode/v4-flash","permissions":{"network":true,"shell":true}}

{"type":"context","context":"Previous task in this series added kalman_fusion. The AGENTS.md is at repo root."}

{"type":"cancel","reason":"timeout"}

// Worker → Supervisor (via worker stdout)
{"type":"ready","task_id":"wt-abc-001","pid":12345}

{"type":"heartbeat","task_id":"wt-abc-001","turn":3,"status":"editing dry_run.rs"}

{"type":"tool_event","task_id":"wt-abc-001","tool":"run_shell","cmd":"cargo check","exit_code":0,"duration_ms":1200}

{"type":"checkpoint","task_id":"wt-abc-001","turn":3,"state_ref":"checkpoints/wt-abc-001/turn-003.json"}

{"type":"result","task_id":"wt-abc-001","status":"done","commits":["a1b2c3d"],"files_changed":["src/dry_run.rs","src/config.rs"],"summary":"Removed 3 global statics, replaced with worker-local config."}

{"type":"error","task_id":"wt-abc-001","error_type":"build_failure","message":"cargo build failed: 3 errors","partial_commits":[]}
```

#### Protocol Rules (Kahn Process Network Semantics)

1. **stdin = one writer (supervisor), one reader (worker).** Supervisor writes, worker blocks on read.
2. **stdout = one writer (worker), one reader (supervisor).** Worker writes, supervisor reads.
3. **Blocking reads.** Worker blocks on `stdin.readline()` until a message arrives. Never poll.
4. **Non-blocking writes.** Supervisor enqueues to worker stdin buffer; OS pipe handles buffering.
5. **stdout closes = worker dead.** EOF on stdout = process exit. Supervisor detects immediately.
6. **No shared memory.** All communication is through the pipes.
7. **One message per line.** Newline-delimited JSON. No binary, no framing protocols.

#### Framing & Safety

- Every line must be valid JSON. Worker must flush stdout after each message (`flush=True`).
- Worker stdout is **never** used for debug logging — debug goes to stderr (unstructured, advisory only).
- Supervisor reads stdout line-by-line. If a line fails JSON parse, it's logged but doesn't crash the supervisor.

---

### M2: Diffundo — Multi-Provider LLM Access

Solves: "one provider overloaded = whole harness stops." The user has multiple cheap subscriptions (DeepCode v4 Flash, Gemini Flash, OpenAI Mini, Claude Haiku, etc.). FanOut cascades between them.

```python
from dataclasses import dataclass, field
from typing import Optional, Any
import hashlib, time, asyncio

@dataclass
class Provider:
    name: str
    model: str
    api_key: str
    base_url: Optional[str] = None
    priority: int = 0          # lower = tried first
    rate_limit_remaining: int = -1  # -1 = unknown
    cooldown_until: float = 0.0
    total_calls: int = 0
    total_errors: int = 0

@dataclass
class FanOutConfig:
    mode: str = "cascade"      # "cascade" | "race"
    timeout: float = 30.0      # per-provider timeout
    cache_ttl: int = 3600      # seconds
    cache_max_size: int = 10000
    race_redundancy: int = 2   # number of providers to race in race mode

class FanOut:
    """
    Every LLM call in the harness goes through FanOut.
    Cache check → provider selection → call → cache write.
    """
    def __init__(self, config: FanOutConfig, providers: list[Provider]):
        self.config = config
        self.providers = sorted(providers, key=lambda p: p.priority)
        self.cache: dict[str, tuple[Any, float]] = {}  # key -> (result, timestamp)

    def _cache_key(self, prompt: str, model: str, temperature: float) -> str:
        return hashlib.sha256(f"{model}:{temperature}:{prompt}".encode()).hexdigest()

    def _get_cached(self, key: str) -> Optional[Any]:
        if key in self.cache:
            result, ts = self.cache[key]
            if time.time() - ts < self.config.cache_ttl:
                return result
            del self.cache[key]
        return None

    def _try_provider(self, provider: Provider, prompt: str, **kwargs) -> Any:
        """Call a single provider. Raises on failure."""
        if time.time() < provider.cooldown_until:
            raise RateLimited(f"{provider.name} in cooldown")
        provider.total_calls += 1
        try:
            # DSPy LM call — provider abstraction handled by LiteLLM
            import dspy
            lm = dspy.LM(
                model=provider.model,
                api_key=provider.api_key,
                base_url=provider.base_url,
            )
            result = lm(prompt, **kwargs)
            provider.rate_limit_remaining = -1  # reset on success
            return result
        except Exception as e:
            provider.total_errors += 1
            provider.cooldown_until = time.time() + 60  # 60s cooldown
            raise

    async def call(self, prompt: str, model: str = None, temperature: float = 0.0, **kwargs) -> Any:
        # 1. Cache check
        resolved_model = model or self.providers[0].model
        key = self._cache_key(prompt, resolved_model, temperature)
        cached = self._get_cached(key)
        if cached is not None:
            return cached

        # 2. Provider selection
        if self.config.mode == "cascade":
            return await self._cascade(prompt, resolved_model, temperature, **kwargs)
        elif self.config.mode == "race":
            return await self._race(prompt, resolved_model, temperature, **kwargs)

    async def _cascade(self, prompt, model, temperature, **kwargs):
        last_error = None
        for provider in self.providers:
            if model and provider.model != model:
                continue  # skip if specific model requested
            try:
                result = await asyncio.to_thread(
                    self._try_provider, provider, prompt, **kwargs
                )
                self.cache[self._cache_key(prompt, model, temperature)] = (result, time.time())
                return result
            except Exception as e:
                last_error = e
                continue  # try next provider
        raise AllProvidersFailed(f"All {len(self.providers)} providers failed. Last: {last_error}")

    async def _race(self, prompt, model, temperature, **kwargs):
        candidates = [p for p in self.providers if not model or p.model == model]
        candidates = candidates[:self.config.race_redundancy]
        tasks = [
            asyncio.to_thread(self._try_provider, p, prompt, **kwargs)
            for p in candidates
        ]
        done, pending = await asyncio.wait(tasks, return_when=asyncio.FIRST_COMPLETED)
        for t in pending:
            t.cancel()
        winner = done.pop()
        result = winner.result()  # may raise
        self.cache[self._cache_key(prompt, model, temperature)] = (result, time.time())
        return result
```

#### Design Rationale

- **Cascade mode (default):** Tries cheap subscriptions first. DeepCode Flash → Gemini Flash → OpenAI Mini → Claude Haiku. First success wins, rest skipped.
- **Race mode:** Fire to N providers simultaneously, first response wins. Good for latency-sensitive steps (planning, evaluation). Costs more but guarantees fastest path.
- **Cache:** Identical prompts hit cache instead of API. Hash key = `(model_id, prompt_hash, temperature)`. TTL-based eviction.
- **Cooldown:** Failed provider enters 60s cooldown. Prevents hammering a down provider.
- **Provider-agnostic:** Uses DSPy's LM abstraction (backed by LiteLLM for 100+ providers). Adding a provider = adding a `Provider` dataclass entry.

---

### M3: Surculus — Worktree Manager

```python
import subprocess
import uuid
from pathlib import Path
from dataclasses import dataclass

@dataclass
class Worktree:
    task_id: str
    path: Path
    branch: str
    base_commit: str

class WorktreeManager:
    def __init__(self, repo_root: str, worktree_root: str = None):
        self.repo_root = Path(repo_root)
        self.worktree_root = Path(worktree_root or f"{repo_root}/.cambium/worktrees")
        self.worktree_root.mkdir(parents=True, exist_ok=True)

    def create(self, task_id: str, base_branch: str = "main") -> Worktree:
        branch = f"cambium/{task_id}"
        path = self.worktree_root / task_id

        subprocess.run(
            ["git", "worktree", "add", "-b", branch, str(path), base_branch],
            cwd=self.repo_root, check=True, capture_output=True
        )

        base_commit = subprocess.check_output(
            ["git", "rev-parse", "HEAD"], cwd=path
        ).decode().strip()

        return Worktree(task_id=task_id, path=path, branch=branch, base_commit=base_commit)

    def remove(self, worktree: Worktree, force: bool = False):
        flag = "--force" if force else ""
        subprocess.run(
            ["git", "worktree", "remove", str(worktree.path)] + ([flag] if flag else []),
            cwd=self.repo_root, check=True, capture_output=True
        )
        subprocess.run(
            ["git", "branch", "-D", worktree.branch],
            cwd=self.repo_root, check=True, capture_output=True
        )

    def list_active(self) -> list[Worktree]:
        output = subprocess.check_output(
            ["git", "worktree", "list", "--porcelain"],
            cwd=self.repo_root
        ).decode()
        return self._parse_worktree_list(output)

    def has_uncommitted_changes(self, worktree: Worktree) -> bool:
        result = subprocess.run(
            ["git", "status", "--porcelain"],
            cwd=worktree.path, capture_output=True
        )
        return bool(result.stdout.strip())
```

---

### M4: Custos — Supervisor (Deterministic Layer)

The core. This is what Prime Agent gets wrong and we must get right.

```python
import asyncio
import json
import signal
import sys
import time
from dataclasses import dataclass, field
from pathlib import Path
from enum import Enum
from typing import Optional

class WorkerState(Enum):
    SPAWNING = "spawning"
    READY = "ready"
    RUNNING = "running"
    CHECKPOINTING = "checkpointing"
    DONE = "done"
    DEAD = "dead"
    FAILED = "failed"

@dataclass
class WorkerHandle:
    task_id: str
    proc: Optional[asyncio.subprocess.Process] = None
    state: WorkerState = WorkerState.SPAWNING
    worktree_path: str = ""
    last_heartbeat: float = 0.0
    restart_count: int = 0
    base_spec: dict = field(default_factory=dict)
    result: Optional[dict] = None
    # Erlang-style intensity tracking
    crash_times: list[float] = field(default_factory=list)

class RestartPolicy:
    """Erlang/OTP-style restart limits."""
    max_restarts: int = 5       # MaxR
    max_period: float = 60.0    # MaxT (seconds)
    restart_delay: float = 1.0  # s6-style mandatory minimum
    backoff_base: float = 2.0   # exponential backoff base

    def should_restart(self, handle: WorkerHandle) -> bool:
        now = time.time()
        # Keep only crashes within the period window
        handle.crash_times = [t for t in handle.crash_times if now - t < self.max_period]
        if len(handle.crash_times) >= self.max_restarts:
            return False  # Over intensity limit — escalate
        return True

    def get_delay(self, handle: WorkerHandle) -> float:
        """Exponential backoff: 1s, 2s, 4s, 8s..."""
        return self.restart_delay * (self.backoff_base ** len(handle.crash_times))

class Supervisor:
    """
    The deterministic process manager.
    NEVER calls an LLM. NEVER crashes. Manages processes, heartbeats, and recovery.
    """

    def __init__(self, config: dict):
        self.config = config
        self.workers: dict[str, WorkerHandle] = {}  # task_id -> handle
        self.event_log: list[dict] = []               # Temporal-style durable log
        self.log_path = Path(config.get("event_log_path", ".cambium/events.jsonl"))
        self.restart_policy = RestartPolicy()
        self.worker_script = config.get("worker_script", "worker.py")
        self.worktree_mgr = None  # injected
        self.fanout = None        # injected (FanOut module)
        self._shutdown = False

    def _log_event(self, event: dict):
        """Append-only event log. Temporal-style durability."""
        event["timestamp"] = time.time()
        self.event_log.append(event)
        with open(self.log_path, "a") as f:
            f.write(json.dumps(event) + "\n")

    async def run_task(self, task_spec: dict) -> dict:
        """Submit a task. Returns when the task completes or fails permanently."""
        task_id = task_spec["task_id"]
        wt = self.worktree_mgr.create(task_id, base_branch=task_spec.get("base_branch", "main"))
        handle = WorkerHandle(
            task_id=task_id,
            worktree_path=str(wt.path),
            base_spec=task_spec,
        )
        self.workers[task_id] = handle
        self._log_event({"type": "task_assigned", "task_id": task_id, "worktree": str(wt.path)})

        return await self._supervise_worker(handle)

    async def _supervise_worker(self, handle: WorkerHandle) -> dict:
        """Supervise a single worker through its lifecycle."""
        while not self._shutdown:
            # Spawn worker process
            proc = await self._spawn_worker(handle)
            handle.proc = proc
            handle.state = WorkerState.SPAWNING

            # Start heartbeat monitor (concurrent task)
            heartbeat_task = asyncio.create_task(
                self._heartbeat_monitor(handle)
            )

            # Read stdout until process exits or result received
            result = await self._read_worker_output(handle)

            heartbeat_task.cancel()
            try:
                await heartbeat_task
            except asyncio.CancelledError:
                pass

            if result is not None:
                handle.state = WorkerState.DONE
                handle.result = result
                self._log_event({"type": "task_done", "task_id": handle.task_id, "result": result})
                return result

            # Process died without result — check restart policy
            handle.crash_times.append(time.time())
            if not self.restart_policy.should_restart(handle):
                handle.state = WorkerState.FAILED
                self._log_event({"type": "task_failed", "task_id": handle.task_id,
                                "reason": "max_restarts_exceeded",
                                "crash_count": len(handle.crash_times)})
                return {"status": "failed", "reason": "max_restarts_exceeded"}

            delay = self.restart_policy.get_delay(handle)
            handle.state = WorkerState.DEAD
            self._log_event({"type": "worker_restart", "task_id": handle.task_id,
                            "delay": delay, "attempt": handle.restart_count})
            await asyncio.sleep(delay)
            handle.restart_count += 1

        return {"status": "cancelled"}

    async def _spawn_worker(self, handle: WorkerHandle) -> asyncio.subprocess.Process:
        """Spawn a worker subprocess."""
        spec = handle.base_spec.copy()
        spec["worktree"] = handle.worktree_path

        proc = await asyncio.create_subprocess_exec(
            sys.executable, self.worker_script,
            stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            cwd=handle.worktree_path,
        )
        # Send init message
        init_msg = json.dumps({"type": "init", **spec}) + "\n"
        handle.proc = proc
        if proc.stdin:
            proc.stdin.write(init_msg.encode())
            await proc.stdin.drain()
        self._log_event({"type": "worker_spawned", "task_id": handle.task_id, "pid": proc.pid})
        return proc

    async def _read_worker_output(self, handle: WorkerHandle) -> Optional[dict]:
        """Read stdout line by line. Return result dict or None (crash)."""
        proc = handle.proc
        if not proc or not proc.stdout:
            return None

        try:
            async for line in proc.stdout:
                line = line.strip()
                if not line:
                    continue
                try:
                    msg = json.loads(line)
                except json.JSONDecodeError:
                    self._log_event({"type": "log_parse_error", "task_id": handle.task_id, "line": line[:200]})
                    continue

                msg_type = msg.get("type")
                if msg_type == "ready":
                    handle.state = WorkerState.READY
                    handle.last_heartbeat = time.time()
                    self._log_event({"type": "worker_ready", "task_id": handle.task_id, "pid": msg.get("pid")})

                elif msg_type == "heartbeat":
                    handle.last_heartbeat = time.time()
                    handle.state = WorkerState.RUNNING
                    self._log_event({"type": "heartbeat", "task_id": handle.task_id, "turn": msg.get("turn")})

                elif msg_type == "checkpoint":
                    handle.state = WorkerState.CHECKPOINTING
                    self._log_event({"type": "checkpoint", "task_id": handle.task_id,
                                    "turn": msg.get("turn"), "state_ref": msg.get("state_ref")})

                elif msg_type == "tool_event":
                    self._log_event({"type": "tool_event", "task_id": handle.task_id, **msg})

                elif msg_type == "result":
                    return msg

                elif msg_type == "error":
                    return msg  # treated as failure, triggers restart logic

            # stdout EOF — process exited
            exit_code = await proc.wait()
            self._log_event({"type": "worker_exit", "task_id": handle.task_id, "exit_code": exit_code})
            return None  # trigger restart logic

        except asyncio.CancelledError:
            proc.kill()
            raise

    async def _heartbeat_monitor(self, handle: WorkerHandle):
        """Watchdog: kill workers that miss heartbeats."""
        heartbeat_timeout = self.config.get("heartbeat_timeout", 60.0)
        while True:
            await asyncio.sleep(10)  # check every 10s
            if handle.state in (WorkerState.DONE, WorkerState.FAILED, WorkerState.DEAD):
                return
            if handle.last_heartbeat > 0:
                elapsed = time.time() - handle.last_heartbeat
                if elapsed > heartbeat_timeout:
                    self._log_event({"type": "heartbeat_timeout", "task_id": handle.task_id,
                                    "elapsed": elapsed})
                    if handle.proc:
                        handle.proc.kill()
                    return

    async def shutdown(self):
        """Graceful shutdown: SIGTERM all workers, wait, SIGKILL stragglers."""
        self._shutdown = True
        for task_id, handle in self.workers.items():
            if handle.proc and handle.state in (WorkerState.RUNNING, WorkerState.READY, WorkerState.SPAWNING):
                self._log_event({"type": "shutdown_worker", "task_id": task_id})
                try:
                    handle.proc.terminate()
                except ProcessLookupError:
                    pass

        # Wait up to 10s for graceful exit
        _, pending = await asyncio.wait(
            [h.proc.wait() for h in self.workers.values() if h.proc],
            timeout=10
        )
        for proc in pending:
            proc.kill()
```

#### Key Design Decisions

1. **Erlang one\_for\_one**: When worker\_2 dies, workers 1 and 3 don't even notice. They're independent processes with independent worktrees.
2. **Transient restart**: Clean exit (result received) → no restart. Abnormal exit (crash) → restart with backoff.
3. **Intensity/period**: Max 5 restarts in 60s. Exceeded → task marked failed, escalated to orchestrator for re-decomposition. Prevents infinite crash loops (the Prime Agent problem).
4. **Event log**: Every state transition is appended to `.cambium/events.jsonl`. Crash recovery = replay the log.
5. **Heartbeat watchdog**: Separate asyncio task per worker. Kills workers that go silent for >60s. This is the "watchdog checks tool activity, not just process liveness" lesson from OpenCode.
6. **No lock files**: The supervisor is the parent process. It knows the PID directly. No `.pid` files to go stale.
7. **No Unix sockets**: stdin/stdout pipes. When stdout closes (EOF), the worker is dead. Period.
8. **Mandatory restart delay**: 1s minimum, exponential backoff. Prevents busy-looping.

---

### M5: Opifex — Worker Runtime

Each worker is a standalone Python script. It reads JSON from stdin, runs a DSPy ReAct loop, writes JSON to stdout.

```python
#!/usr/bin/env python3
"""Cambium Worker — standalone script spawned by the supervisor."""

import sys
import json
import time
import asyncio
from pathlib import Path

# DSPy for the agent loop
import dspy
from dspy import ReAct, Tool, Signature

class WorkerAgent(dspy.Module):
    """The core agent loop. Hill-climbable via SIMBA/GEPA."""

    def __init__(self, tools: list, max_iters: int = 20):
        # DSPy signature: task + context → action
        signature = Signature(
            "task, context -> action",
            "You are a coding agent. Read the task, use tools to implement it."
        )
        self.react = ReAct(signature, tools=tools, max_iters=max_iters)

    def forward(self, task: str, context: str = ""):
        return self.react(task=task, context=context)

# ─── Tools ───────────────────────────────────────────────────────

def read_file(path: str) -> str:
    """Read a file from the worktree."""
    return Path(path).read_text()

def write_file(path: str, content: str) -> str:
    """Write content to a file in the worktree."""
    Path(path).write_content(content)
    return f"Wrote {len(content)} bytes to {path}"

def run_shell(cmd: str) -> str:
    """Run a shell command in the worktree. Returns combined output."""
    import subprocess
    result = subprocess.run(
        cmd, shell=True, capture_output=True, text=True, timeout=120
    )
    return result.stdout + result.stderr

def git_op(op: str, args: str = "") -> str:
    """Run a git operation in the worktree."""
    import subprocess
    result = subprocess.run(
        f"git {op} {args}", shell=True, capture_output=True, text=True, timeout=30
    )
    return result.stdout + result.stderr

def grep_code(pattern: str, path: str = ".") -> str:
    """Search for a pattern in files."""
    import subprocess
    result = subprocess.run(
        f"grep -rn '{pattern}' {path}", shell=True, capture_output=True, text=True, timeout=30
    )
    result.stdout + result.stderr

# ─── Tool Registry ───────────────────────────────────────────────

TOOLS = [read_file, write_file, run_shell, git_op, grep_code]

# ─── Heartbeat / Checkpoint ──────────────────────────────────────

def emit(msg: dict):
    """Write a JSON line to stdout. Always flush."""
    msg["timestamp"] = time.time()
    sys.stdout.write(json.dumps(msg) + "\n")
    sys.stdout.flush()

def heartbeat(turn: int, status: str = "working"):
    emit({"type": "heartbeat", "turn": turn, "status": status})

def checkpoint(turn: int, state: dict):
    """Persist ReAct state for crash recovery."""
    state_dir = Path("checkpoints")
    state_dir.mkdir(exist_ok=True)
    state_file = state_dir / f"turn-{turn:03d}.json"
    state_file.write_text(json.dumps(state, indent=2))
    emit({"type": "checkpoint", "turn": turn, "state_ref": str(state_file)})

# ─── Main Loop ───────────────────────────────────────────────────

async def main():
    # 1. Read init message from stdin
    init_line = sys.stdin.readline()
    init_msg = json.loads(init_line)
    task_id = init_msg["task_id"]
    worktree = init_msg["worktree"]
    spec = init_msg["spec"]
    max_turns = init_msg.get("max_turns", 20)
    model = init_msg.get("model", "deepcode/v4-flash")

    # 2. Signal ready
    emit({"type": "ready", "task_id": task_id, "pid": os.getpid()})

    # 3. Configure DSPy LM
    dspy.configure(lm=dspy.LM(model=model))

    # 4. Build agent
    agent = WorkerAgent(tools=TOOLS, max_iters=max_turns)

    #  standalone agent
    turn = 0

    def on_step_end_callback(trajectory_state):
        """Hook into DSPy ReAct after each tool call."""
        nonlocal turn
        turn += 1
        heartbeat(turn)
        checkpoint(turn, {"trajectory": trajectory_state})

    # 5. Run agent loop
    try:
        result = agent.forward(task=spec, context=init_msg.get("context", ""))

        # 6. Collect results
        commits = collect_commits()
        files_changed = collect_changed_files()

        emit({"type": "result", "task_id": task_id, "status": "done",
              "commits": commits, "files_changed": files_changed,
              "summary": str(result)})

    except Exception as e:
        emit({"type": " M5: error", "task_id": task_id,
              "error_type": type(e).__name__, "message": str(e)})

def collect_commits() -> list[str]:
    import subprocess
    out = subprocess.check_output(
        ["git", "log", "--oneline", "HEAD~5..HEAD"],  # last 5 commits
        text=True, stderr=subprocess.DEVNULL
    ).strip()
    return [line.split()[0] for line in out.splitlines() if line]

def collect_changed_files() -> list[str]:
    import subprocess
    out = subprocess.check_output(
        ["git", "diff", "--name-only", "HEAD"], text=True
    ).strip()
    # Also check uncommitted changes vs base
    return [line for line in out.splitlines() if line]

if __name__ == "__main__":
    asyncio.run(main())
```

---

### M6: Architectus — Orchestrator (LLM-Driven Task Decomposition)

The orchestrator is an LLM-driven DSPy module that sits **above** the deterministic supervisor. It decides:

1. **Decompose**: Break a complex task into independent subtasks
2. **Route**: Assign subtasks to workers with appropriate configs
3. **Evaluate**: Assess whether merged results meet the original spec
4. **Retry/Reject**: If results are insufficient, send feedback and reassign

```python
import dspy
from dataclasses import dataclass
from typing import Optional

@dataclass
class SubTask:
    task_id: str
    spec: str
    model: str = "deepcode/v4-flash"
    base_branch: str = "main"
    depends_on: list[str] = None  # task IDs this depends on

class TaskDecomposer(dspy.Module):
    """Break a complex spec into parallelizable subtasks."""

    def __init__(self):
        self.decompose = dspy.ChainOfThought(
            "spec, repo_context -> subtasks: list[SubTask]"
        )

    def forward(self, spec: str, repo_context: str = "") -> list[SubTask]:
        pred = self.decompose(spec=spec, repo_context=repo_context)
        return pred.subtasks

class ResultEvaluator(dspy.Module):
    """Evaluate whether worker results meet the original spec."""

    def __init__(self):
        self.evaluate = dspy.ChainOfThought(
            "spec, diff, test_results -> verdict: Literal['approve', 'reject'], "
            "feedback: str, confidence: float"
        )

    def forward(self, spec: str, diff: str, test_results: str) -> dict:
        pred = self.evaluate(spec=spec, diff=diff, test_results=test_results)
        return {
            "verdict": pred.verdict,
            "feedback": pred.feedback,
            "confidence": pred.confidence,
        }

class Orchestrator:
    """
    The LLM brain of the supervisor.
    Uses FanOut for all LLM calls.
    """

    def __task_id_counter

    def __init__(self, fanout, supervisor):
        self.fanout = fanout
        self.supervisor = supervisor
        self.decomposer = TaskDecomposer()
        self.evaluator = ResultEvaluator()

    async def execute(self, spec: str, repo_context: str = "") -> dict:
        # 1. Decompose
        subtasks = await self.decompose(spec, repo_context)

        # 2. Dispatch independent subtasks in parallel
        results = {}
        ready = [st for st in subtasks if not st.depends_on]
        pending = [st for st in subtasks if st.depends_on]

        while ready:
            # Dispatch all ready tasks concurrently
            coros = [self.supervisor.run_task(st.__dict__) for st in ready]
            batch_results = await asyncio.gather(*coros)
            for st, result in zip(ready, batch_results):
                results[st.task_id] = result

            # Check dependencies
            ready = []
            for st in pending[:]:
                if all(dep in results for dep in (st.depends_on or [])):
                    ready.append(st)
                    pending.remove(st)

        # 3. Merge
        merged = await self.merge(results)

        # 4. Evaluate
        evaluation = await self.evaluate(spec, merged.diff, merged.test_results)

        # 5. Retry if needed
        if evaluation["verdict"] == "reject":
            feedback = evaluation["feedback"]
            # Re-dispatch failed subtasks with feedback
            ...

        return merged
```

#### Key Design Decision: Is the Supervisor an LLM or Pure Code?

**Both, but strictly separated:**

```
┌─────────────────────────────────────────┐
│  Orchestrator (LLM, DSPy, can fail)     │
│  • decompose, route, evaluate, retry    │
│  • Uses FanOut for LLM calls            │
│  • If FanOut fails → can't decompose,   │
│    but existing workers keep running    │
└────────────────┬────────────────────────┘
                 │ dispatch(run_task)
┌────────────────▼────────────────────────┐
│  Supervisor (pure Python, never fails)  │
│  • spawn, heartbeat, kill, restart      │
│  • Event log, worktree management       │
│  • NEVER calls LLM                      │
│  • If orchestrator fails → keeps        │
│    existing workers alive               │
└─────────────────────────────────────────┘
```

The deterministic layer **does not depend on the LLM layer**. If all providers are down, the supervisor keeps existing workers running and just can't spawn new tasks. This is critical: the failure mode of "LLM API is down" should never cascade into "workers get killed."

---

### M7: Unio — Merge Sequencer

```python
import subprocess
from dataclasses import dataclass
from typing import Optional

@dataclass
class MergeResult:
    success: bool
    commits: list[str] = None
    conflicts: list[str] = None
    diff: str = ""
    test_results: str = ""

class MergeSequencer:
    """
    Sequential merge of worker branches back to main.
    Rebase each worker branch onto current main, run tests, commit.
    """

    def __init__(self, repo_root: str, test_cmd: str = None):
        self.repo_root = repo_root
        self.test_cmd = test_cmd or "cargo test --lib 2>&1 | tail -5"  # default

    def merge_worker(self, task_id: str, branch: str) -> MergeResult:
        """Merge a single worker branch onto main."""
        try:
            # 1. Checkout main
            subprocess.run(["git", "checkout", "main"], cwd=self.repo_root, check=True)

            # 2. Rebase worker branch onto main
            result = subprocess.run(
                ["git", "rebase", "main", branch],
                cwd=self.repo_root, capture_output=True, text=True
            )
            if result.returncode != 0:
                conflicts = self._extract_conflicts(result.stderr + result.stdout)
                subprocess.run(["git", "rebase", "--abort"], cwd=self.repo_root)
                return MergeResult(success=False, conflicts=conflicts)

            # 3. Fast-forward main
            subprocess.run(["git", "checkout", "main"], cwd=self.repo_root, check=True)
            subprocess.run(["git", "merge", "--ff-only", branch], cwd=self.repo_root, check=True)

            # 4. Run tests
            test_output = subprocess.run(
                self.test_cmd, shell=True, cwd=self.repo_root,
                capture_output=True, text=True, timeout=300
            )
            test_results = test_output.stdout + test_output.stderr

            if test_output.returncode != 0:
                # Tests failed — revert
                subprocess.run(["git", "reset", "--hard", "HEAD~1"], cwd=self.repo_root)
                return MergeResult(
                    success=False,
                    commits=[],
                    diff="",
                    test_results=test_results
                )

            # 5. Get diff
            diff = subprocess.check_output(
                ["git", "show", "--stat"], cwd=self.root
            ).decode()

            return MergeResult(
                success=True,
                commits=subprocess.check_output(
                    ["git", "log", "--oneline", "-3"], cwd=self.repo_root
                ).decode().strip().splitlines(),
                diff=diff,
                test_results=test_results
            )

        except subprocess.CalledProcessError as e:
            return MergeResult(success=False, conflicts=[str(e)])
```

---

### M8: Septum — Sandbox Wrapper

```python
import subprocess
from typing import Optional

class Sandbox:
    """
    namespace-based isolation per worker.
    Workers can only see their worktree + read-only system dirs.
    """

    def __ sandbox_command(self, worktree_path: str, allow_network: bool = False) -> list[str]:
        cmd = [
            "sandbox", "--die-with-parent",
            "--ro-bind", "/usr", "/usr",
            "--ro-bind", "/lib", "/lib",
            "--ro-bind", "/lib64", "/lib64",
            "--ro-bind", "/bin", "/bin",
            "--bind", worktree_path, worktree_path,
            "--dev", "/dev",
            "--proc", "/proc",
            "--tmpfs", "/tmp",
        ]
        if not allow_network:
            cmd.append("--unshare-net")
        cmd.extend([sys.executable, "worker.py"])
        return cmd

    def wrap(self, worktree_path: str, allow_network: bool = False) -> list[str]:
        """Return the command list to run a sandboxed worker."""
        return self._sandbox_command(worktree_path, allow_network)
```

---

### M9: Ascensus — DSPy Optimization Harness

This is the **moat**. Every node in the tree can be hill-climbed independently.

```python
import dspy
import json
from pathlib import Path
from datetime import datetime

class TrajectoryRecorder:
    """Records worker trajectories for offline optimization."""

    def __init__(self, storage_path: str = ".cambium/trajectories"):
        self.path = Path(storage_path)
        self.path.mkdir(parents=True, exist_ok=True)

    def record(self, task_id: str, trajectory: list[dict], success: bool, metric_score: float = None):
        record = {
            "task_id": task_id,
            "timestamp": datetime.now().isoformat(),
            "trajectory": trajectory,
            "success": success,
            "metric_score": metric_score,
            "model": self._detect_model(),
        }
        file = self.path / f"{task_id}.json"
        file.write_text(json.dumps(record, indent=2))

# ─── Per-Node Metrics ────────────────────────────────────────────

def worker_metric(trajectory: list[dict], reference: dict = None) -> float:
    """Did the worker complete its task? How efficiently?"""
    if not trajectory[-1].get("success"):
        return 0.0
    # Efficiency: fewer tool calls = better
    num_tool_calls = sum(1 for step in trajectory if step.get("type") == "tool_call")
    max_expected = reference.get("expected_tool_calls", 20) if reference else 20
    efficiency = 1.0 - (num_tool_calls / max_expected) * 0.3  # weight 30%
    return min(1.0, max(0.0, 0.7 + efficiency))  # base 0.7 for success

def decomposer_metric(subtasks: list, original_spec: str, actual_results: dict) -> float:
    """Did the decomposition lead to successful completion?"""
    completed = sum(1 for r in actual_results.values() if r.get("status") "done")
    total = len(subtasks)
    if total == 0:
        return 0.0
    return completed / total

def reviewer_metric(reviewer_pred: dict, ground_truth: dict) -> float:
    """Did the reviewer correctly identify bugs?"""
    # precision: predicted bugs that were real
    # recall: real bugs that were found
    pred_bugs = set(reviewer_pred.get("bugs", []))
    true_bugs = set(ground_truth.get("bugs", []))
    tp = len(pred_bugs & true_bugs)
    precision = tp / len(pred_bugs) if pred_bugs else 0.0
    recall = tp / len(true_bugs) if true_bugs polymorphism
    return 2 * precision * recall / (precision + recall) if (precision + recall) > 0 else 0.0

# ─── Optimization Loop ───────────────────────────────────────────

async def optimize_node(node_name: str, trainset: list, metric_fn, auto="medium"):
    """Hill-climb a single node using SIMBA or GEPA."""
    if node_name == "worker":
        module = WorkerAgent(tools=TOOLS)
    elif node_name == "decomposer":
        module = TaskDecomposer()
    elif node_name == "reviewer":
        module = ResultEvaluator()
    else:
        raise ValueError(f"Unknown node: {node_name}")

    optimizer = dspy.SIMBA(
        metric=metric_fn,
        max_steps=12,
        max_demos=10,
        num_threads=4,  # parallel evaluation
    )

    optimized = optimizer.compile(module, trainset=trainset)
    # Save optimized module
    save_path = f".c flywheel/data/optimized/{node_name}.json"
    optimized.save(save_path)
    return optimized
```

#### The Data Flywheel

```
    ┌──────────────────────────────────────────────┐
    │              PRODUCTION RUN                   │
    │  Workers execute tasks, record trajectories   │
    └──────────────────┬───────────────────────────┘
                       │
    ┌──────────────────▼───────────────────────────┐
    │              COLLECT                          │
    │  Trajectories + success metrics stored to     │
    │  .cambium/trajectories/                       │
    └──────────────────┬───────────────────────────┌
                       │
    ┌──────────────────▼───────────────────────────┐
    │              OPTIMIZE (offline)               │
    │  Run SIMBA/GEPA on DeepCode v4 Flash (free)  │
    │  Hill-climb each node independently           │
    │  Ship optimized prompts back to production    │
    └──────────────────┬───────────────────────────┏
                       │
    ┌──────────────────▼───────────────────────────┐
    │              DEPLOY                           │
    │  Workers load optimized DSPy modules          │
    │  Better tool selection, better decomposition  │
    │  → Better results → Better training data      │
    └──────────────────────────────────────────────┘
```

---

## 5. Binding Summary

### Supervisor ↔ Worker

| Direction | Transport | Format | Semantics |
|---|---|---|---|
| Supervisor → Worker | stdin pipe | JSON line | Blocking read (Kahn) |
| Worker → Supervisor | stdout pipe | JSON line | Non-blocking write |
| Worker debug | stderr pipe | Free text | Advisory only |

**No other bindings exist.** No sockets, no lock files, no shared memory, no shared filesystem state.

### Supervisor ↔ Orchestrator

| Direction | Transport | Format |
|---|---|---|
| Orchestrator → Supervisor | Python function call (`await supervisor.run_task(spec)`) | `dict` |
| Supervisor → Orchestrator | Return value of `run_task()` | `dict` |

The orchestrator and supervisor run in the **same process**. The orchestrator is an async client of the supervisor. Clean function-call boundary.

### Both ↔ FanOut

| Direction | Transport | Format |
|---| LLM call | `dspy.LM(...)` |

FanOut is injected into both the supervisor's orchestrator layer and the workers. It's a shared module that all LLM calls route through.

---

## 6. Competitor Analysis Summary

### What We Copy

| Feature | From | Why |
|---|---|---|
| Async generator agent lifecycle | Claude Code | Streaming, cancellation, composition fall out naturally |
| Tool removal for isolation | OpenCode | Model can't call what it can't see |
| Doom loop detector (3 heuristics) | Claude Code | Turn counting + repetition detection + convergence checks |
| Per-subagent timeout + watchdog | OpenCode (lesson) | Stuck subagents should bail, not hang 30 min |
| Ephemeral teams by default | OpenCode community | No persistent team state to recover |
| Unidirectional messaging | OpenCode community | Prevents ACK loops |
| Append-only JSONL sessions | Prime Agent / Codex | Crash recovery + audit trail |
| Single-attempt compaction guard | Claude Code | Prevents infinite compaction loops |
| Explicit truncation detection | OpenCode (lesson) | Don't doom-loop on truncated tool calls |
| BM25 tool search | Codex | When MCP servers expose hundreds of tools |
| OS-native sandboxing | Codex | namespace-based isolation + seccomp |
| Two-phase memory | Codex | Light model extracts, strong model consolidates |
| RLM context-as-variables | Prime Agent | Long sessions without losing access to past info |
| Continual harness self-improvement | Prime Agent | Agent CRUD on its own prompts/skills |

### What We Do Differently

| Area | Everyone Else | Cambium |
|---|---|---|
| **Supervisor** | In-process async (Claude Code, OpenCode) or fragile socket IPC (Prime Agent) | Process isolation + stdin/stdout pipes. Best of both. |
| **Provider resilience** | Single provider; whole system breaks on overload | FanOut cascade through N providers |
| **Prompt optimization** | Static hand-written prompts | DSPy SIMBA/GEPA per-node hill climbing |
| **Language** | TypeScript/Node.js or Go+TS | Python 3.14 (no-GIL) |
| **Crash recovery** | Lock files, snapshots, complex cleanup | PID-based + pipe EOF + event log replay |
| **Cache** | None | FanOut-level prompt caching |
| **Optimization data** | None recorded | Per-node trajectories → optimization flywheel |

---

## 7. Implementation Priority & Parallelization

Modules can be built **in parallel**. Dependency graph:

```
M1 (IPC Protocol) ──────────────────────────────┐
                                                │
M2 (FanOut) ──────────────────────────────┐    │
                                          │    │
M3 (Worktree Manager) ────────────────────┤    │
                                          │    │
M4 (Supervisor) ◄── depends on M1, M3 ────┤    │
                                          │    │
M5 (Worker Runtime) ◄── depends on M1, M2 ┤    │
                                          │    │
M6 (Orchestrator) ◄── depends on M2, M5 ──┘    │
                                                │
M7 (Merge Sequencer) ──────────────────────────┤
                                                │
M8 (Sandbox) ───────────────────────────────────┤
                                                │
M9 (DSPy Optimization) ◄── depends on M5 ───────┘
                                                │
M10 (CLI/TUI) ◄── depends on M4, M6 ────────────┘
```

### Build Phases (parallelizable within each phase)

**Phase 1 (P0 — Core, ~2 weeks):**
- M1 + M2 + M3 can be built **simultaneously** (no inter-dependencies)
- M4 starts after M1 + M3 land
- M5 starts after M1 + M2 land
- M4 + M5 can be built in **parallel** once their deps land

**Phase 2 (P1 — Intelligence, ~1 week):**
- M6 (Orchestrator) after M2 + M5
- M7 (Merge) after M3

**Phase 3 (P2 — Hardening & Optimization, ~1 week):**
- M8 (Sandbox), M9 (Optimization), M10 (CLI)
- All independent of each other

### Parallel Build Assignment

| Builder | Module(s) | Priority |
|---|---|---|
| Builder A | M1 (Nuntius), M4 (Custos) | P0 |
| Builder B | M2 (Diffundo), M5 (Opifex) | P0 |
| Builder C | M3 (Surculus), M7 (Unio) | P0 |
| Builder D | M6 (Architectus) | P1 |
| Builder E | M8 (Septum), M9 (Ascensus) | P2 |
| Builder F | M10 (Janus) | P2 |

---

##  SYSTEM_DESIGN.md Status

**Status:** Draft v0.1.0 — ready for adversarial review.

**Next steps:**
1. Adversarial review (3 perspectives: distributed systems, LLM design, implementation risks)
2. Incorporate feedback
3. Finalize as build-ready spec
4. Hand to coding agent

**File:** `/home/ubuntu/cambium/SYSTEM_DESIGN.md`

---

## 9. Adversarial Review Summary (v0.2.0-pending)

Three independent reviewers analyzed the v0.1.0 draft. Full reviews: `review-distributed-systems.md`, `review-llm-design.md`, `review-implementation.md`.

### Consensus Verdict: "Sound bones, not build-ready yet."

All three agree: the architecture is correct, the CS foundations are well-chosen, the competitor analysis is thorough. But the code samples have bugs and several critical design gaps must be fixed before handing to a coding agent.

### Critical Issues to Fix (Must Fix Before Implementation)

| # | Issue | Source | Fix |
|---|---|---|---|
| **F1** | **Sync file I/O in event loop** — `_log_event()` blocks the entire asyncio loop on every event write. This cascades: pipe buffers fill → workers stall on stdout → heartbeats stop → heartbeat monitor kills everyone. | DS-C1 | Move to dedicated writer thread or `aiofiles`. Never block the event loop. |
| **F2** | **FanOut cache ignores repo state** — `(model, temp, prompt)` hash will serve stale codegen when the same prompt is issued against different file contents. Silently produces wrong edits. | LLM-C1 | Add `git rev-parse HEAD` + worktree ID to cache key. Or disable cache for worker calls. |
| **F3** | **Merge sequencer has no mutex** — two concurrent merges race on the shared repo, corrupting HEAD/index. | IMPL-C1 | Serialize merges via `asyncio.Lock`. Operate on throwaway worktree, not main checkout. |
| **F4** | **Workers bypass FanOut** — worker.py creates `dspy.LM(model=model)` directly. Provider cascade/race/cache doesn't protect the workers (which are the majority of LLM calls). | IMPL-C12 | Inject FanOut config into worker via init message. Worker constructs FanOut locally. |
| **F5** | **FanOut cascade is a no-op across models** — `if provider.model != model: continue` skips every provider that doesn't match the exact model string. The headline multi-provider feature literally doesn't work. | LLM-C2 | Remove the model-match guard. Cascade should try ANY available provider, not just exact-match. |
| **F6** | **Heartbeat timeout (60s) < tool timeout (120s)** — `run_shell` has a 120s timeout. A worker running `cargo build` sends no heartbeat for up to 120s → killed at 60s by the watchdog. | DS-C3 | Per-tool heartbeat (long-running tools emit heartbeats mid-execution). Raise default timeout to 180s. Add jitter. |
| **F7** | **~12 syntax errors in code samples** — `os.getpid()` without importing `os`, `self.root` instead of `self.repo_root`, `write_content()` instead of `write_text()`, broken `__task_id_counter`, `__ sandbox_command` typo, missing `import asyncio` in orchestrator, etc. | IMPL-C3-C9 | These are draft bugs. The coding agent will write correct code. But they prove the code hasn't been smoke-tested. |
| **F8** | **No structured edit tool** — only `write_file` (full overwrite) and `run_shell`. Every production agent (Claude Code, Codex, OpenCode) has a diff/patch tool. Without it, the agent is strictly weaker. | LLM-C3 | Add `edit_file(path, old_string, new_string)` tool. Consider structured patch parsing (Lark grammar, per Codex). |
| **F9** | **Independent hill-climbing claim is overstated** — worker optimization data depends on decomposer quality. Coupled nodes can't truly be optimized independently. | LLM-C4 | Document as hypothesis to validate. Start with worker-only optimization (most data, clearest metric). |
| **F10** | **No automatic coding metric for SIMBA/GEPA** — "did tests pass" is necessary but insufficient. Gameable (empty patches pass). | LLM-C5 | Multi-signal metric: tests pass (floor) + LLM-judge quality score + diff size reasonableness + behavioral checks. |
| **F11** | **Event log writes are not crash-safe** — `open(path, "a")` + write is not atomic. Supervisor crash mid-write corrupts the log. | DS-C6 | Use `os.fsync()` after each write. Or use SQLite WAL mode (atomic by default). |
| **F12** | **Sandbox backend is Linux-only** — user has a macOS build machine. No fallback documented. | IMPL-M4 | Platform abstraction: Linux namespace tool, `sandbox-exec` (macOS), none (Windows/CI). |

### Moderate Issues (Fix During Implementation)

| # | Issue | Fix |
|---|---|---|
| M1 | No jitter in timers/backoffs → thundering herd | Add ±20% random jitter to all sleep/restart values |
| M2 | stdout EOF ≠ "dead worker" — partial writes, Python buffering | Use `proc.wait()` + EOF as combined signal. Set `PYTHONUNBUFFERED=1` in worker env. |
| M3 | Python 3.14 free-threaded is experimental | Default to 3.12/3.13. Free-threading optional, not required. |
| M4 | No secrets management for API keys | `.cambium/providers.yaml` with `env:` references, never hardcoded |
| M5 | No test strategy for the harness itself | Phase 1 must include smoke test: spawn 1 worker with mock LLM, verify IPC + merge |
| M6 | No orchestrator atomicity escape hatch | Add "single-task" mode that bypasses decomposition for atomic tasks |
| M7 | No tool-retrieval layer (BM25/semantic search) | Defer to Phase 3. Important for MCP integration but not blocking. |
| M8 | No logging framework | Use `structlog` or stdlib `logging` with JSON formatter |

### What the Reviewers Liked

- ✅ "Sound core" — process isolation, stdin/stdout IPC, Erlang supervision
- ✅ "Genuine differentiator" — DSPy optimization flywheel
- ✅ "Thoughtfully derived from Erlang/OTP"
- ✅ "The strongest part of the document" — process supervision layer
- ✅ Competitor analysis is "genuinely thoughtful"
- ✅ "The bones are good"

### Revised Build Priority

**Phase 0 (Pre-build, 2-3 days):**
- Fix all F1-F12 critical issues in the spec
- Write a minimal smoke test (mock LLM, 1 worker, verify spawn → IPC → result → merge)
- Get the smoke test to pass

**Phase 1 (Core, ~2 weeks):**
- M1 (IPC) + M2 (FanOut, fixed) + M3 (Worktree) — parallel
- M4 (Supervisor, fixed) after M1+M3
- M5 (Worker, with FanOut injection + edit_file tool) after M1+M2

**Phase 2 (Intelligence, ~1 week):**
- M6 (Orchestrator, with atomic-task mode)
- M7 (Merge, serialized + worktree-isolated)

**Phase 3 (Hardening, ~1 week):**
- M8 (Sandbox, cross-platform)
- M9 (DSPy optimization, worker-only first)
- M10 (CLI)

**Total revised estimate: ~4-5 weeks** (up from 3, accounting for review findings).

---

## 10. Appendix: Files

| File | Description |
|---|---|
| `SYSTEM_DESIGN.md` | This document (v0.1.0 draft) |
| `review-distributed-systems.md` | Adversarial review: race conditions, I/O, crash recovery (391 lines) |
| `review-llm-design.md` | Adversarial review: caching, optimization, tools, metrics (242 lines) |
| `review-implementation.md` | Adversarial review: syntax errors, deps, cross-platform, missing pieces (326 lines) |
| `../supervisor-worker-patterns.md` | CS foundations research (373 lines) |
| `../multi-agent-architecture-research.md` | Competitor architecture analysis (200 lines) |
