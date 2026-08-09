# Cambium — Architecture (v2)

**Version:** 2.0.0
**Date:** 2026-08-09
**Status:** Build-ready. Supersedes `system-design.md` v0.1.0.
**Scope:** This document is authoritative for behavior, interfaces, and failure semantics. Where it conflicts with `system-design.md`, this document wins.

---

## 0. TL;DR

Cambium is a **Python 3.14 multi-agent coding-agent harness**, shipped as an embeddable library (headless-first) with an optional TUI. A deterministic supervisor (`Custos`) manages N isolated worker processes (`Opifex`). Each worker runs a DSPy ReAct loop in a private git worktree, contained by **worktree isolation + permission allowlists + approval gates** (no sandboxing in the harness — see §4/§7.2 and decision 10). Workers communicate with the supervisor over **JSON-Lines on stdio with `request_id` RPC framing**. The orchestrating layer (`Architectus`) decomposes, routes, and evaluates via DSPy modules, each with its own dataset and metric. A serialized merge sequencer (`Unio`) fuses worker branches back onto `main`.

Cambium is a **leaf module** of a larger system: a host process spawns instances, owns persistence, and reads structured `Result` records. Cambium itself is stateless across sessions.

**What changed since v0.1.0.** Three adversarial reviews (`docs/architecture/reviews/`) catalogued ~25 CRITICAL flaws. v2 resolves every one. The headline fixes: (a) liveness is no longer "stdout EOF = dead" — there is an explicit four-layer liveness model with `request_id` framing, generation fencing tokens, and per-tool heartbeats; (b) all disk I/O is off the asyncio event loop on dedicated writer threads; (c) restart policy has full jitter plus an absolute ceiling; (d) worktrees are recovered (lock cleanup + hard reset) before every respawn and may be fenced by generation; (e) the local LLM response cache is **deleted entirely** (D1) — `Diffundo` is a stateless router and caching is provider-side only, content-addressed and never stale (resolves review LLM-C1 by deletion); (f) the provider cascade actually cascades across models of a declared tier; (g) the merge sequencer holds an `asyncio.Lock` and operates in a throwaway worktree; (h) every DSPy module ships with its own frozen dataset, metric, and held-out eval — the "independently hill-climbable" claim is restated as a hypothesis validated under pinned siblings; (i) secrets are env-only and redacted; (j) logging is stdlib, structured, non-blocking, rotated.

**Primary patterns kept from v0.1:** Erlang/OTP one-for-one transient supervision; git worktree isolation; deterministic/LLM layer separation; DSPy optimization flywheel; subprocess-per-worker with stdio IPC.

**Primary patterns dropped:** free-threaded Python (irrelevant for subprocess design); `.pid` files; Unix sockets; lock files; "stdout EOF is death"; the v0.1 prompt-only cache and, per D1, **all local LLM response caching** (provider-side caching only); the literal `cascade`/`race` implementation in v0.1 M2 (rewritten); the in-harness sandbox module (Septum, M8 — removed from v2 scope per decision 10, D7).

---

## 1. Goals & Non-Goals

### Goals
1. **Embeddable.** `import cambium; await cambium.session(...).run(spec)` works in any async Python program. No daemon required.
2. **Headless-first.** The TUI is a view over the same JSON-Lines event stream the host reads. Nothing is TUI-only.
3. **Sound under failure.** Liveness, restart, merge, and crash-recovery have explicit, tested semantics. No "works in demo, dies in production."
4. **Per-module optimizable.** Each DSPy module has its own dataset, metric, and held-out evaluation. Sibling modules are pinned during optimization.
5. **Bounded everything.** Restarts, wall time, memory, log size, and worker counts all have explicit ceilings. (No cache size bound is needed: there is no local cache — D1.)
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
│  │  Diffundo (FanOut): stateless router, tier-based provider       │  │
│  │                      cascade, per-provider cooldown + token     │  │
│  │                      bucket, provider-side caching only (D1).   │  │
│  └──────────────────────────────┬─────────────────────────────────┘  │
│                                 │ await run_task(spec) -> Result       │
│  ┌──────────────────────────────▼─────────────────────────────────┐  │
│  │  DETERMINISTIC LAYER  (pure Python; never LLM; never crashes)   │  │
│  │  Custos    — supervisor; lifecycle; watchdog; restart policy;   │  │
│  │              worktree recovery; durable event log.              │  │
│  │  Unio      — merge sequencer (asyncio.Lock + throwaway wt).     │  │
│  │  Surculus  — worktree manager (lock recovery + prune).          │  │
│  │  Containment: worktree isolation + permission allowlists +      │  │
│  │              approval gates (no sandbox module; see §4 M8).     │  │
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
- `Diffundo` is owned by the Orchestrator. Workers receive a `DiffundoConfig` over the protocol and instantiate their own `Diffundo` client; `Diffundo` is **stateless across calls** (no local cache — D1), so there is no cache state to share or desynchronize across worker processes (see §8, §9).

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
        """Stream every Event. Durability: see §6.5 — critical events are
        fsync-d before yielded; non-critical events may be lost within the
        configured fsync interval (default 1 s) on supervisor crash."""

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
    unified_diff: str | None            # per-file diff vs base_commit, capped 64 KiB (D8b)
    diff_truncated: bool                # True when unified_diff overflowed the 64 KiB cap
    summary: str                        # worker-authored, ≤2k chars
    metric_score: float                 # 0.0..1.0, multi-signal (§10)
    metric_breakdown: dict[str, float]  # per-signal scores
    parent_task_id: str | None          # tree linkage (D2): None for the session root
    event_log_ref: str                  # "sqlite:<session_dir>/.cambium/events.db"
    session_id: str
    started_at: float
    ended_at: float
    failure_reason: str | None          # populated when status != "done"
```

`Result` is JSON-serializable and is the **only** contract the upper system consumes from a finished run. It is written atomically to `${session_dir}/.cambium/result.json` before `Session.run()` returns.

**Result envelopes flow up the Task Tree** (D2): a child node's terminal envelope is a **message** to the parent, not merely a terminal report. The upward envelope carries **exactly**: `parent_task_id`, `unified_diff` (with `diff_truncated` set on overflow), `summary`, `metric_score`, `metric_breakdown`, `commits`, `files_changed`, and terminal `status` — **never the child's scratchpad, chain-of-thought, or trajectory** (normative information-hiding rule, D8b/I2.7). The `Result` dataclass above adds session/root-level fields (`exit_code`, `event_log_ref`, `session_id`, `started_at`, `ended_at`, `failure_reason`) that are populated when the session result is finalized (§16.1); they are **not** part of the upward child envelope. `Nuntius`/`Custos` validate upward messages against this envelope schema and reject unknown top-level fields, so the rule is structural, not a prompt convention.

The `unified_diff` field is capped at 64 KiB and is included by default (the evaluator tier consumes it for merge-conflict context and result review; consuming design: `docs/research/architectus-design.md`). A per-task config flag `include_diff: false` omits the field for higher orchestrator tiers where the merge-conflict context is not needed (token savings); the diff remains available on demand when `merge_failed` resolution requires it (§7.8).

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

`Instance` is what a proto-AGI host holds. The host owns `session_dir` lifecycle; Cambium owns everything under `${session_dir}/.cambium/`.

### 3.6 `Event` — typed stream record

```python
@dataclass(frozen=True)
class Event:
    kind: str               # "submitted" | "task_decomposed" | "worker_spawned" |
                            # "heartbeat" | "tool_event" | "checkpoint" |
                            # "merge_progress" | "result" | "worker_exit" | "log" | ...
    task_id: str | None
    request_id: str | None
    timestamp: float        # time.time()
    monotonic_ms: int       # time.monotonic_ns() // 1_000_000
    generation: int | None
    payload: dict           # type-specific; redacted of secrets (§9)
```

The `Event` stream is the **machine interface**. The TUI renders it; the host system can also subscribe. Durability is tiered (critical vs non-critical events) — see §6.5 for the precise contract; in short, critical events (`submitted`, `result`, `checkpoint`, `worker_exit`, `task_failed`, `merge_progress`) are fsync-d before they are yielded to any subscriber, while non-critical events may be lost within the configured fsync interval (default 1 s) on supervisor crash.

**Event-kind vocabulary (reconciliation).** The canonical name for the task-entry event is **`submitted`** (critical); `task_assigned` is the v2.1-era name for the same event, per the events draft's catalog mapping (`docs/research/event-schema-draft.md` §3.1: `submitted` = arch `task_assigned`). Decomposition output is **`task_decomposed`** (non-critical, draft-proposed; `docs/research/event-schema-draft.md` §3.10). Tree linkage uses `payload.parent_task_id` on both (§3.7, §6.3). The §6.5 tier tables use the canonical names with the v2.1 names mapped inline, not silently divergent.

### 3.7 Task tree (D2)

The task structure is an explicit **`TaskTree`**, not a flat subtask list. Decomposition (`TaskDecomposer`) produces a **DAG with a single root per session**; nodes are sub-LLM sessions (workers), edges are parent/child delegation. The tree is validated by a deterministic helper (proposed `cambium.orchestrator.tasktree` — pure functions for validation, cycle detection, topological ordering) **before any dispatch**; it never imports DSPy (layering invariant, §2).

**Tree linkage in the event log — payload-first.** `parent_task_id` is a payload key on the `submitted` (critical) and `task_decomposed` (non-critical) events — **not** a new SQLite column — per the merged events draft (`docs/research/event-schema-draft.md` §3.1, §3.10). A new required column would be a breaking envelope change under the draft's migration policy (§7.2). Tree reconstruction joins `task_decomposed`/`submitted` records on `payload.parent_task_id`; this lets the host navigate the session tree (§16).

**Normative invariants (D2):**

- **I2.1 Single root.** One root node per session; every non-root node has exactly one parent (`parent_task_id`).
- **I2.2 No cycles.** The decomposition graph is a DAG — no cycles, no self-loops, no multi-parent in v2. Cycle detection = topological sort (Kahn) on the decomposition graph before dispatch; a cyclic decomposition is **rejected and the decomposer re-prompted** (bounded retries). Cyclic graphs can otherwise leave tasks `pending` forever (reviews DS-M6, LLM-N2).
- **I2.3 Depth/width bounds.** `max_depth` (default 3) and `max_width` (per-session parallel worker cap, config) are enforced by the supervisor at dispatch.
- **I2.4 Context composition.** A node's context = its own session log (bounded) + parent summary + subtree result envelopes. A node never reads a sibling's raw session; siblings communicate only through the parent.
- **I2.5 Tree-level completion.** A node reaches terminal state only when its own work is done **and** every child has returned an envelope (recursively). The §7.1 state machine is per-task; the D4 gate defines "work is done."
- **I2.6 Append-only session logs.** Nodes' logs are immutable history; steering writes new turns, never edits old ones.
- **I2.7 Information hiding (D8b).** A child **never** sends its scratchpad, chain-of-thought, reasoning trace, or trajectory upward. The child→parent envelope carries exactly: `parent_task_id`, `unified_diff` (≤64 KiB, `diff_truncated` flag on overflow), `summary` (≤2k chars), `metric_score`, `metric_breakdown`, `commits`, `files_changed`, terminal `status`. Enforcement is deterministic (schema validation at `Nuntius`/`Custos`), not a prompt convention.

**NodeSession terminology (D3).** The public `Session` (§3.3) is **one task execution** of the headless API. A node in the Task Tree owns a **`NodeSession`** — a sub-session identified by `session_id == task_id`, checkpointed and reloadable, with its own conversation/session store under `${session_dir}/.cambium/sessions/<node_id>/` (D2/D8g). The wire messages address `task_id`; `session_id` is the same value until the split is finalized (design-deltas D3 Q3.5).

---

## 4. Module Catalog

Each row maps to one self-contained module with its own `architecture.md` (see `docs/architecture/module-template/`). Latin names retained for continuity with v0.1; **no module requires the Latin name to be used in code**.

| Code | Name | Layer | Responsibility | State owned |
|---|---|---|---|---|
| M1 | Nuntius | Deterministic | IPC protocol: JSON-Lines framing, `request_id` RPC, message schema. | None (pass-through). |
| M2 | Diffundo | Orchestrator | Multi-provider LLM access: tier-based cascade, cooldown, token-bucket rate limiting. Stateless router. | None; per-provider cooldown timers + token buckets. |
| M3 | Surculus | Deterministic | `git worktree` lifecycle: create, recover, prune, list. | None (state lives in git). |
| M4 | Custos | Deterministic | Supervisor: lifecycle, watchdog, restart, event log writer, gate/budget enforcement. | WorkerHandle table; event log handle; restart counters; gate-verdict records. |
| M5 | Opifex | Worker | DSPy ReAct loop; tools; checkpoint; heartbeat. | Per-node: trajectory, turn counter, generation token, session log. |
| M6 | Architectus | Orchestrator | Decision modules: `should_decompose` (v2 rule engine; DSPy seam per `docs/architecture/module-template/example-spec.md`), `TaskDecomposer`, `TaskRouter`, `ResultEvaluator`. All subclass `cambium.modules.base.Module` (`decide()` + `metric()`). | Program versions (read-only at runtime). |
| M7 | Unio | Deterministic | Merge sequencer: serialized, throwaway worktree, test gate. | None (operates on a temp worktree). |
| M8 | Septum | — | Sandbox wrapper (Linux kernel namespace, `sandbox-exec` macOS, noop). | **Removed — out of scope (2026-08-09, decision 10).** Code retained for history; not renumbered (`agents.md` §6: module codes are stable vocabulary). Containment = worktree isolation + permission allowlists + approval gates (§7.2); research evidence retained in `docs/research/sandbox-options.md` (unprivileged user namespaces blocked by AppArmor on this host). |
| M9 | Ascensus | Tooling (offline) | Optimization harness: per-module dataset, metric, held-out eval, refinement loop. | Optimized prompt artifacts under `.cambium/optimized/`. |
| M10 | Janus | View | TUI: subscribes to `Session.events()`. Read-only. | None. |

**Module interface contracts are normative.** Each module's `architecture.md` defines its inputs, outputs, state, failure modes, DSPy program, metric, dataset, and test strategy, per the template in `docs/architecture/module-template/architecture.md`.

**Module CLI (D8a).** Every module MUST ship a CLI entry `python -m cambium.modules.<name>`: read one JSON object from stdin, write one JSON object to stdout, exit `0` on success (non-zero with a JSON `{"error": {…}}` object on failure); stderr is reserved for human diagnostics. The strict typed dataclasses (e.g., `TaskInput`/`DecomposeOutput`) are the CLI's schema. `decide()` is the pure function; the CLI is a thin adapter (~30 LOC). Distinct from the v2.1 eval entry `python -m cambium.modules.<name>.eval`.

**Ports and adapters (D8d).** A module's boundary is defined by typed ports (`typing.Protocol`), not concrete imports: v1 port set `LLMProvider` (`call(prompt, tier, temperature) -> response`), `EventSink`, `DatasetStore`. Adapters implement the ports (e.g., `DiffundoAdapter(LLMProvider)`). Module instances are built by constructor injection at a composition root from `Config` (proposed `cambium.container` or `cambium.orchestrator` wiring); a module never constructs a provider itself (except the worker-side `CambiumLM`, config-injected via `init.fanout_config`, §9.3).

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
 "task_id":"wt-abc-001",                // == NodeSession session_id (D2/D3)
 "parent_task_id":"wt-abc-000",        // tree linkage; null for the session root (D2)
 "generation":3,                        // fencing token (§7.3)
 "worktree":"/abs/path",
 "base_commit":"a1b2c3d...",
 "spec":"Refactor dry_run.rs to remove global state",
 "tools":["read_file","write_file","edit_file","run_shell","git_op","grep_code"],
 "fanout_config":{ /* DiffundoConfig, no api keys */ },
 "provider_env_keys":["DEEPCODE_API_KEY","GEMINI_API_KEY"],  // names only; values from env (D7)
 "permissions":{"network":true,"shell":true},                // allowlist; shell gates run_shell (D7)
 "heartbeat":{"interval_s":15,"timeout_s":90},
 "budget":{"max_wall_s":1800,"max_restarts":10,
           "max_turns":20,"max_tokens":200000,"timeout_ms":120000,   // supervisor-owned (D4)
           "gate_max_retries":2}}      // D4 gate bound; all budget fields enforced by Custos

{"type":"context","request_id":"...","context":"Previous task added kalman_fusion."}
{"type":"steer","request_id":"...","session_id":"wt-abc-001",      // (D3) parent direction to an
 "context":"<parent's follow-up / steering turn>"}                  // existing NodeSession; valid
                                                                   // only after `ready` (RUNNING)
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
 "parent_task_id":"wt-abc-000",        // child→parent linkage (D2/D3)
 "status":"done",
 "commits":["a1b2c3d"],
 "files_changed":["src/dry_run.rs"],
 "diff":"...",                          // unified_diff vs base_commit, ≤64 KiB, truncation
 "diff_truncated":false,                // flagged true on overflow (D8b) — envelope ONLY:
                                        // no scratchpad/CoT/trajectory may ride upward (I2.7)
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

**Admission is a supervisor-internal ack, NOT a wire message (D3).** When `Custos` accepts a spawn it returns admission to the orchestrator/host synchronously **before** the worker is RUNNING; it is a control-plane ack, not a worker→supervisor message. The wire handshake stays `init → ready` with `ready_timeout` unchanged (a pre-`ready` wire message would have no timer slot and would collide with the IPC draft's `PROTO_OUT_OF_ORDER` rule — `docs/research/ipc-protocol-draft.md` §4.1).

**Steering (D3).** After `ready`, the parent may direct a live NodeSession with repeatable `steer` turns; `Custos` routes messages by `session_id` (parent→child steer, child→parent result envelopes). Sibling→sibling messaging is parent-mediated only in v2. Routing is performed by the deterministic supervisor, never directly process-to-process.

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

- **Primary store:** SQLite in **WAL mode** at `${session_dir}/.cambium/events.db`. Stdlib only; atomic commits; crash-safe by construction (resolves DS-C6/M3).
- **Optional mirror:** JSON-Lines at `${session_dir}/.cambium/events.jsonl` for streaming consumers and human inspection. Off by default; enable via config.
- **Retention:** per-session DB; the host archives or deletes the session dir. Within a session, an `events` table is append-only; a `snapshots` table stores periodic compaction points. Replay = read `events` since the last `snapshot`.
- **Conversation store:** per-node session history is a separate queryable SQLite WAL store (`${session_dir}/.cambium/sessions/conversations.db`) — see §6.6 (D8g).

### 6.2 Writer architecture (resolves DS-C1, DS-M3, IMPL-M7)

```
                       event loop                      dedicated writer thread
                  ┌────────────────┐                ┌────────────────────────┐
asyncio tasks ──► │ supervisor     │  queue.Queue   │ single consumer:       │
                  │ enqueues Event ├───────────────►│ - dequeue               │
                  │ (non-blocking) │   (bounded,    │ - BEGIN; INSERT; COMMIT│
                  │                │    backpressure│ - fsync per §6.5:      │
                  └────────────────┘    or drop+log)│   • critical = now     │
                          │                            │   • other    ≤ 1 s    │
                          ▼                            │ - publish to in-proc   │
                  in-memory ring buffer                │   subscriber set      │
                  (last 10 000 events)                └───────────┬────────────┘
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
4. **fsync cadence:** the writer maintains two modes — *batched* and *critical-immediate*. In batched mode it flushes the SQLite WAL to disk at most once per `fsync_interval_s` (default 1.0) via `PRAGMA wal_checkpoint(TRUNCATE)` followed by `os.fsync(wal_fd)` on the WAL file's fd (not the main DB fd — in WAL mode recent commits live in the `-wal` file, so fsyncing the main DB fd alone is a no-op for durability). In critical-immediate mode (entered when a critical event is dequeued), it runs the same checkpoint+fsync before acking the producer. `PRAGMA synchronous=NORMAL` is set (WAL+NORMAL is crash-safe for the last committed transaction; FULL would add an fsync per commit and is unnecessary with our explicit WAL checkpoint). The default cadence is configurable.
5. **Subscribers** (`Session.events()` consumers) receive events via an `asyncio.Queue` fed from the writer thread through `loop.call_soon_threadsafe`. Subscribers see events in monotonic order. **Critical events** are guaranteed to be fsync-d before they reach a subscriber; **non-critical events** may reach a subscriber before they are fsync-d, so a supervisor crash within `fsync_interval_s` can drop the most recent non-critical events from a subscriber's view (but the in-memory ring buffer and the at-most-1s checkpoint catch-up close the gap on restart — see §6.5).
6. **Redaction** is applied at enqueue time, before the event ever reaches disk (§9.3).

### 6.3 Event schema (durable)

```sql
CREATE TABLE events (
    seq          INTEGER PRIMARY KEY AUTOINCREMENT,  -- monotonic, gap-free (by construction, §6.5)
    monotonic_ms INTEGER NOT NULL,
    ts           TEXT,                              -- ISO-8601 TEXT (canonical for v2; see note below)
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

`seq` is gap-free within a session **by construction**: the writer reserves `seq` at enqueue and commits in reservation order (single writer, FIFO queue — §6.2), so a gap cannot occur. Loss is bounded by the fsync-cadence crash window, not by gap detection (§6.5).

**Envelope reconciliation (`ts`, `event_id`).** The envelope timestamp is stored as **ISO-8601 TEXT** (`ts TEXT`); the events draft's float-epoch `ts` semantics (`docs/research/event-schema-draft.md` §2) are superseded for v2 — the code's TEXT form is canonical. The draft's ULID `event_id` correlation key (§2, D2) is **deferred to v2.1** and is absent from the v2 store DDL; **`seq` is the durable identity for v2**.

**Tree linkage is payload-first (D2).** `parent_task_id` is a payload key on `submitted` (critical) and `task_decomposed` (non-critical) — **no new column** is added to the `events` table (a new required column would be a breaking envelope change under the events draft's migration policy, `docs/research/event-schema-draft.md` §7.2). Tree reconstruction joins those records on `payload.parent_task_id`; an index over the payload field is a v2.1 optimization if query volume demands it. `task_decomposed.payload.subtasks[]` carries the decomposed child list and `cycle_detected` (I2.2).

### 6.4 Checkpoint / restart semantics

A worker emits `{"type":"checkpoint", "state_ref":"...", "commits_so_far":[...]}` after every tool call that produces or modifies durable state (file writes, commits). The `state_ref` points to `${session_dir}/.cambium/checkpoints/${task_id}/turn-${N}.json`, written atomically (write-temp + `os.rename`).

On restart (§7.4), `Custos` loads the latest checkpoint for the task and re-injects it into the new worker via the `init` message as `resume_from_checkpoint`. Workers that opt out of checkpointing (e.g., read-only tasks) accept a fresh start.

**Checkpoint semantics extend to session resume (D3).** The checkpoint `state_ref` plus the NodeSession's own session log (§6.6) are the reload state: on crash/restart the supervisor reloads the **session** (own log + DSPy trajectory + steering history) rather than starting a fresh task — answering Prime Agent's observed failure mode "children die mid-work; the isolated session worker stopped during in-flight work" (`docs/research/prime-agent.md` §3.3). Steering turns since the last checkpoint are replayable from the conversation store.

**Checkpoint is not a substitute for the event log.** The event log records *what happened* (for replay, audit, training); checkpoints record *where to resume* (for crash recovery). They are distinct stores.

### 6.5 Durability contract (precise)

The previous sections refer to "durability" in several places. This section states the contract once, exactly, and is normative.

**Event tiers.** Every event has a tier, derived from `kind`:

| Tier | Kinds | Promise |
|---|---|---|
| **Critical** | `submitted` (v2.1 name: `task_assigned`), `result`, `checkpoint`, `worker_exit`, `task_failed`, `merge_progress`, `merge_committed` | Fsync-d to disk **before** the writer returns the ack to the producer and **before** the event is yielded to any `Session.events()` subscriber. Loss window on supervisor crash: zero (last committed transaction may be lost only on simultaneous kernel/page-cache loss, which SQLite WAL + `synchronous=NORMAL` protects against). |
| **Non-critical** | `heartbeat`, `tool_event`, `log`, `worker_spawned`, `worker_ready`, `task_decomposed` (draft-proposed, §3.6) | Appended to the WAL subject to `fsync_interval_s` (default 1 s) checkpoint cadence. Loss window on supervisor crash: at most `fsync_interval_s` of the most recent non-critical events. |

**Mechanism.** The writer thread holds open the main DB fd **and** the WAL fd. The "fsync" operation is:

```python
def _fsync_now(self) -> None:
    cur = self._conn.execute("PRAGMA wal_checkpoint(TRUNCATE)")
    cur.close()
    os.fsync(self._wal_fd)        # fsync the WAL file (recent commits live here)
    os.fsync(self._db_fd)         # belt-and-braces; cheap after a TRUNCATE checkpoint
```

Notes:

- `PRAGMA synchronous=NORMAL` is set on connection open. Under WAL, NORMAL is crash-safe for the most recently committed transaction; FULL would add a per-commit fsync that defeats batching. Our explicit `_fsync_now` is the source of truth for cross-interval durability.
- We fsync the **WAL fd**, not just the main DB fd. In WAL mode, recent commits are appended to `${db}-wal`; fsyncing only the main DB fd is a no-op for those commits — this was the latent bug in the v2.0 draft's invariant 4 wording.
- Critical events trigger `_fsync_now` synchronously inside the writer's dequeue loop before the next event is read; non-critical events are buffered until the next timer-driven `_fsync_now` (every `fsync_interval_s`).
- The timer-driven `_fsync_now` runs even when no critical events arrive, so the worst-case non-critical loss on crash is bounded by `fsync_interval_s`.

**Recovery on supervisor restart.** Open the DB (SQLite replays the WAL automatically), read `events` since the last `snapshots` row, and re-publish to fresh subscribers. `seq` gaps cannot occur — `seq` is reserved at enqueue and the sole writer commits in reservation order (§6.2), so the gap-free invariant holds by construction and there is **no gap detection** to run. Loss is bounded by the fsync-cadence crash window: at most `fsync_interval_s` of the most recent non-critical events (§6.2 inv. 5), absorbed by the supervisor's own restart logic (re-read from the last snapshot, re-publish to fresh subscribers). The `recovery_gap` event kind is **superseded (unreachable by construction)** and is kept in the kind vocabulary only as a reserved marker for tooling compatibility; the writer never emits it.

**Phantom-read caveat (normative for callers).** A non-critical append returns a reserved `seq` whose row may not be durable yet: `events_after(seq)` may not observe it, and a supervisor crash inside `fsync_interval_s` can lose it. Callers must tolerate both — by polling `events_after`/re-publishing on restart and by treating a missing tail as loss within the crash window, never as corruption.

**What this means for callers.** A caller that needs proof a thing happened must wait for the matching **critical** event (e.g., a `result` event for task completion, a `merge_committed` event for a merge). A caller that observes only heartbeats or tool events cannot prove liveness across a supervisor crash — by design, since these are high-volume advisory signals.

### 6.6 Per-node conversation store (D8g)

The event log answers *"what happened system-wide"*; the **conversation store** answers *"what did this node see and decide"*. It is the queryable substrate the Task Tree (D2) needs for bounded context composition (I2.4) without forwarding scratchpads.

- **Storage:** per-node conversation/session history in **SQLite WAL** at `${session_dir}/.cambium/sessions/conversations.db` (separate from `events.db` — the event log is append-only history; conversations are mutable-queryable state). Same single-writer-thread discipline as §6.2 (one writer per DB; never disk I/O on the event-loop thread).
- **Content:** the node's protocol transcript — `init`/`steer`/`tool_event`/`checkpoint`/`result` message payloads per NodeSession. Queryable, e.g., `last_turns(node_id, n)`, `cost_by_node`, `context_for(node_id)` returning the bounded D2 I2.4 context. The event log keeps the same facts for cross-cutting audit.
- **JSONL is retained exactly where it already is:** IPC transport is JSON-Lines (§5.1) and the optional event mirror is JSON-Lines (§6.1). The conversation store is not IPC.
- **Growth bounds** mirror the event log: per-node snapshot/compaction, bounded retention; a node's store is pruned with its session dir (§16.2).
- SQLite WAL durability for this store is validated by the same machinery as the event log (`docs/research/sqlite-wal-durability.md` — reader never blocks, sees committed data immediately).

---

## 7. Lifecycle

This section normatively defines how a task moves through the system. Every state transition emits an event.

### 7.1 State machine (per task)

```
                ┌────────────┐
                │  PENDING   │   task enqueued by Architectus
                └──────┬─────┘
                       │ worktree created (Surculus)
                       ▼
                ┌────────────┐
                │  SPAWNING  │   Custos.create_subprocess_exec(...)
                └──────┬─────┘
                       │ ready received
                       ▼
                ┌────────────┐    heartbeat ─┐
            ┌──►│  RUNNING   │◄──────────────┘
            │   └──────┬─────┘
            │          │ work complete (result envelope received)
            │          │ cancel / shutdown (§7.7) ──────►  ┌────────────┐
            │          ▼                                   │ CANCELLED  │
            │   ┌────────────┐                             └────────────┘
            │   │  GATING    │    gate passes ──────►      ┌────────────┐
            │   └──────┬─────┘                             │    DONE    │
            │          │ gate fails                        └──────┬─────┘
            │          ▼                                          │ reviewer
            │   ┌────────────┐                                    │ rejection (§7.8)
            │   │GATE_FAILED │                                    ▼
            │   └──────┬─────┘                             ┌────────────┐
            │          │ retries left ──► back to RUNNING  │  REJECTED  │
            │          │ retries exhausted                 └────────────┘
            │          ▼
            │   ┌────────────┐
            │   │  FAILED    │   ◄─ timeout / fatal error (from RUNNING)
            │   └────────────┘
            │          ▲
            │          │ not restartable / budget exhausted
            │   ┌────────────┐
            │   │  CRASHED   │   EOF + no result, OR watchdog kill
            │   └──────┬─────┘
            │          │ restartable & under budget?
            │          ├─yes─► recover worktree (§7.5), increment generation ──► SPAWNING
            └──────────┘        back to SPAWNING
```

**GATING / GATE_FAILED (D4).** A task completes only when its **gate** passes. When the worker completes its work and `Custos` receives the result envelope (§3.4), the task enters `GATING` and `Custos` runs the task's gate command (e.g., the task's scenario test suite; `Unio`'s test gate at merge time, §7.8). Gate passes → `DONE` (with the gate verdict attached to the result envelope). Gate fails → `GATE_FAILED`: the worker receives the gate's failure evidence (command, output tail, failing assertion) as a steering turn (D3) and may retry up to `gate_max_retries` (default 2, supervisor-owned); retries exceed the bound → the task fails **with evidence** (`status="failed"`, `failure_reason` includes the gate command, exit code, and captured output). "Done" is therefore never self-reported by the worker; the transition into `GATING` is the deterministic result-envelope receipt, not a worker self-report. Skip-if-unchanged and gate-verdict content-addressing are in §7.9.

**Tree-level completion (D2 I2.5).** The state machine is per-task; a node is terminal only when its own work is done **and** every child has returned a result envelope (recursively). The root's envelope is the session result (§3.4, §3.7).

**Terminal result statuses (§3.4).** `DONE` — gate passed (§7.9); `FAILED` — timeout / fatal error, gate retries exhausted, or restart budget exhausted; `REJECTED` — reviewer rejection after the merge gate (§7.8); `CANCELLED` — cancel / shutdown (§7.7). Each is recorded as the terminal `Result.status`.

### 7.2 Spawn

```python
proc = await asyncio.create_subprocess_exec(
    sys.executable, "-X", "utf8", "-u", worker_script,   # direct spawn; no sandbox wrapper (D7)
    stdin=PIPE, stdout=PIPE, stderr=PIPE,
    cwd=worktree_path,
    env=_construct_worker_env(task_id, generation, worktree_path, provider_env_keys),  # D7 (R4)
    start_new_session=True,         # process group for killpg
    pass_fds=(), close_fds=True,
)
```

After spawn, the supervisor sends `init` and **waits for `ready`** before considering the worker RUNNING. There is a `ready_timeout` (default 60 s) covering cold-start cost (IMPL-M2). On timeout, the worker is killed and the restart policy engages. Spawn returns **admission** — a supervisor-internal ack to the orchestrator/host issued synchronously when the spawn is accepted, before the worker is RUNNING (D3; it is not a wire message — see §5.2).

**Least-privilege worker env (D7, resolves threat-model R4).** The spawn path does **not** pass `{**os.environ, ...}`. `_construct_worker_env` builds a scrubbed dict: `PATH` (minimal), `PYTHONUNBUFFERED=1`, `CAMBIUM_TASK_ID`, `CAMBIUM_GENERATION`, `CAMBIUM_SESSION_ID`, `HOME` (worktree-scoped, optional), **plus only the keys named in `init.provider_env_keys`** (names only; values resolved from the host env — §12.2). Everything else is dropped, so a compromised worker cannot `print(os.environ)` for unrelated secrets (this is the `--setenv` per-worker key-allowlist norm of the removed sandbox, repointed to spawn-time env construction — §18.3 IMPL-M6).

**Containment policy (D7).** With the sandbox module removed, containment is the stack:
- **Worktree isolation** — per-task throwaway worktrees, `Surculus` recovery, generation fencing, quarantine (§7.3, §7.5).
- **Permission allowlists** — the primary policy surface: per-task `init.permissions` (`network`, `shell`), the `git_op` op allowlist and list-form `grep_code` (§11), no `fetch_url`/`curl` tool (§11), and the least-privilege worker env above.
- **Approval gates** — a host-facing `approve(session_id, op)` callback for operations outside the pre-declared allowlist (first-time external-path writes, non-allowlisted network egress), wired through the supervisor.

**Deployment isolation is the host's job (D8e).** The worker is a plain stdio process (`python -m cambium.opifex`, JSON-Lines on stdin/stdout) whether run locally or inside a host-owned container (Docker) or microVM (Firecracker). Cambium neither builds nor assumes containers; a host wraps the process and connects the pipes — the IPC contract is transport-agnostic.

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
- **Supervisor-owned session budgets (D4):** `max_turns`, `max_tokens`, and `timeout_ms` are carried in `init.budget` and enforced by `Custos` — **never self-reported by the worker**. Exceeding any bound → task `FAILED` (or `timeout`, §3.4). `gate_max_retries` is a separate counter from `absolute_max`, but shares the wall-time budget.
- **Full jitter:** `delay = random.uniform(0, base_delay_s * backoff_base ** n)`. No deterministic thundering herd (DS-C4(a)). Jitter is also applied to the heartbeat watchdog interval, the fsync timer, and the heartbeat emission.
- **Recoverable errors:** workers set `recoverable: false` on `error` messages that should not be retried (e.g., `invalid_spec`, `unknown_tool`). Non-recoverable errors skip the restart budget and fail immediately.
- **Provider outage is not a worker failure** (resolves DS-M7): `Diffundo.AllProvidersFailed` raised inside the worker is caught at the worker's tool boundary, logged, and converted to a backoff retry **inside the worker** for up to `provider_patience_s` (default 180 s). Only if the outage persists past that does the worker emit `error` with `recoverable: true`. This isolates provider flapping from the supervisor's restart policy.
- **Queue-level pause on total provider exhaustion (D8f):** when the cascade exhausts every provider, the **dispatch queue pauses** — the orchestrator stops dispatching new tasks (IMPL-M5 "park dispatch" posture) and the supervisor does not respawn/retry-loop workers awaiting an LLM. A **recovery monitor** (proposed: `Custos` timer watching provider-health events) wakes dispatch when any provider's bucket/cooldown/breaker recovers. Workers in-flight await; they do not crash-loop.

### 7.5 Worktree recovery (resolves DS-C5, IMPL-M9)

> The worktree lifecycle rules below consolidate Codex desktop-app practice (per-task worktree, detached HEAD, snapshot before deletion, ~15-worktree GC cap, `.worktreeinclude` for ignored files like `.env`) as researched in `docs/research/codex.md` ("Worktree lifecycle engineering"). The lock-cleanup sequence addresses the specific failure modes Prime Agent exhibited locally (`docs/research/prime-agent.md` §3.2 — socket/lock-file supervision failures).

Before **every** respawn (not just first-spawn), `Surculus.recover(worktree, base_commit)` runs:

1. Remove every `*.lock` file under `${worktree}/.git` and `${repo}/.git/worktrees/${id}`.
2. Abort in-progress git operations: `git rebase --abort`, `git merge --abort`, `git cherry-pick --abort`, `git revert --abort` (each best-effort, log if it fails).
3. `git reset --hard ${base_commit}` — drop the failed attempt's working-tree changes.
4. `git clean -fd` — remove untracked files (build artifacts, stray files).
5. Write `.cambium/generation` with the new generation number.
6. Optionally (default on): if a checkpoint exists for the task, **restore** the checkpoint's commits by cherry-picking `commits_so_far` onto `base_commit`. If cherry-pick fails, fall back to a fresh start.

After M3-style recovery, the worktree is in a known-good state. The new worker inherits no corruption.

If recovery fails (step 3 returns non-zero), the worktree is **quarantined** to `${session_dir}/.cambium/quarantine/${task_id}-${generation}/` and a fresh worktree is created from `base_commit`. The quarantined tree is preserved for forensics and pruned after `${session_dir}` cleanup.

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

### 7.8 Merge terminal step — atomic update of `refs/heads/main`

The catalog (§4) and §7.1 establish that `Unio` operates in a throwaway worktree under an `asyncio.Lock`. This section normatively closes the loop: **how does `main` actually get updated?** Without an explicit terminal step, the v2.0 draft left the merge sequencer "verifying in a throwaway worktree" with no path to publishing the result.

**Single writer.** `Unio` is the **only** code path in Cambium permitted to mutate `refs/heads/main`. Workers never touch `main`; the orchestrator never touches `main`; only `Unio.publish_merge(...)` does, and it holds the `Unio` lock for the duration.

**Publish sequence (atomic fast-forward):**

```python
async def publish_merge(self, verified_tip: str) -> str:
    """Fast-forward refs/heads/main to verified_tip under the Unio lock.

    `verified_tip` is the SHA that the throwaway worktree reached after the
    test gate passed. This call must not run any test or build; that work is
    already done. It only publishes.
    """
    async with self._lock:                      # the only writer to main
        # 1. Refuse non-fast-forwards: this is a hard correctness invariant.
        #    A non-FF means main moved while we were verifying — abort and
        #    let the orchestrator re-merge against the new main.
        old_sha = self._read_ref("refs/heads/main")
        if not await self._is_ancestor(old_sha, verified_tip):
            raise NonFastForward(old=old_sha, new=verified_tip)

        # 2. Atomic ref update. `git update-ref` takes the ref lock inside
        #    .git/refs/heads/main.lock, writes the new SHA, renames — atomic
        #    at the filesystem level. We pass the expected old_sha so a
        #    concurrent update from outside Cambium (e.g., a human `git push`)
        #    is detected rather than silently overwritten.
        await self._git(["update-ref", "refs/heads/main", verified_tip, old_sha],
                        check=True)
        # 3. Emit the critical merge_committed event BEFORE returning, so
        #    subscribers see it only after it is durable (§6.5).
        await self._events.enqueue_critical(Event(
            kind="merge_committed",
            payload={"old": old_sha, "new": verified_tip},
            ...))
        return verified_tip
```

**Crash-safety story.**

- `git update-ref` is the atomic primitive: it takes `.git/refs/heads/main.lock`, writes the new SHA to a tempfile, and renames over the ref file. A crash before the rename leaves `main.lock` (recovered by `Surculus.recover()`, §7.5) and `main` unchanged. A crash after the rename has `main` pointing at the new SHA. There is no torn state.
- The expected-old-SHA argument (`update-ref <ref> <new> <old>`) makes the publish **fail loudly** if anything — including a human `git push` to `main` while Cambium is running — moved the ref between read and write. The orchestrator treats `NonFastForward` as "main moved; re-merge from the new main."
- The `merge_committed` event is **critical** (§6.5). It is fsync-d before `publish_merge` returns, so a supervisor crash immediately after `update-ref` is observable on recovery: the event log shows the new main SHA, and `result.json` can be written against it. A crash *between* `update-ref` and the event emit leaves the ref advanced but the event log unaware — on recovery, `Unio.reconcile()` reads `refs/heads/main`, compares to the latest `merge_committed` event, and emits a `merge_reconciled` event to close the gap.
- No working-tree checkout of `main` is performed by `Unio`. The publish is purely a ref update; the working tree of `main` (if any) is updated by the host system or a separate Cambium command, never automatically. This is the same lesson Codex's CLI applies ("create git checkpoints; do not auto-mutate the working tree") — see `docs/research/codex.md`.

**Lock scope.** The `Unio` lock is held across verify-in-throwaway-worktree **and** publish. This serializes the whole merge pipeline. Throughput impact is documented in DS-M1; the throwaway-worktree + batch-test mode mitigates wall-clock cost without weakening the single-writer invariant.

**Unio's test gate is the final gate (D4).** The merge-time test run in the throwaway worktree is the last gate a task's work passes before `main` moves; see §7.9 for the gate lifecycle and the content-addressed skip rule that covers the second run.

### 7.9 Autonomous gate and budgets (D4)

Task completion is gated, and budgets are supervisor-owned:

1. **Task completes only when a gate command passes.** The gate verifies the task's outcome (e.g., the task's scenario test suite; `Unio`'s test gate at merge, §7.8; the `tests` signal in §10). A task whose work is done but whose gate fails does **not** reach `DONE`; it enters `GATE_FAILED` (§7.1). "Done" cannot be self-reported by the worker.
2. **Failed gate → bounded evidence-backed retries.** On gate failure the worker receives the gate's failure evidence (command, output tail, failing assertion) as a steering turn (D3) and is allowed `gate_max_retries` (default 2). After the bound, the task **fails with evidence**: `status="failed"`, `failure_reason` includes the gate command, exit code, and captured output (§3.4).
3. **Skip-if-unchanged (content-addressed verdicts).** Gate verdicts are keyed by `sha256(tree-hash of worktree state || gate command || base_commit || gate input spec)` and stored per-session (in the events DB or a small `gate_verdicts` table). If a retry or a crash-restart re-derives an identical key, the prior verdict is reused instead of re-running the gate. This is the same content-addressing argument as D1: the key derives from the exact bytes that determine the outcome, so it cannot serve a verdict for different state. Per-session scope only — cross-session sharing would reintroduce the coherence question D1 removed.
4. **Budgets owned by the supervisor.** `max_turns`, `max_tokens`, and `timeout_ms` are carried in `init.budget` and enforced by `Custos`, never self-reported by the worker (§7.4). This is the "bounded everything" goal (§1, §19) applied at per-session granularity.

---

## 8. Caching & Transparency Policy

A recurring v0.1 flaw was conflating **pass-through modules** (which carry data without interpreting it) with **state-owning modules** (which make decisions based on accumulated state). v2 makes the split explicit.

| Module | Transparency | State owned | Notes |
|---|---|---|---|
| **Nuntius** | Pass-through | None | Carries bytes; never interprets payload. No cache. |
| **Surculus** | Pass-through | None | Delegates to git; state lives in git itself. |
| **Unio** | Pass-through | None | Operates on a throwaway worktree; no in-memory state between merges. |
| **Custos** | Owns (process) | WorkerHandle table, event log, gate-verdict records | Process state. No LLM cache. |
| **Opifex** | Owns (per-node) | Trajectory, turn counter, generation, session log | Per-process; dies with the worker. No cross-worker sharing. |
| **Diffundo** | Pass-through (stateless) | None; per-provider cooldown timers + token buckets | **No local cache (D1)** — provider-side caching only; see §8.1. |
| **Architectus** | Owns (program versions) | DSPy program versions (read-only) | Each submodule has its own dataset; see §9. |
| **Ascensus** | Owns (offline) | Optimized artifacts, harness state | Not on the hot path. |

*(Septum, M8, is removed from v2 scope — decision 10/D7; it no longer appears in the transparency table.)*

### 8.1 Diffundo cache policy — no local cache (resolves LLM-C1, LLM-M5 by deletion; D1)

- **There is no local LLM response cache.** Removed: the LRU store, the TTL, per-instance cache state, and the `cache` / `cache_namespace` / `context_hash` parameters on `Diffundo.call` (§9.2). `Diffundo` is a **stateless router**; its only state is per-provider cooldown timers and token buckets (§9).
- **Provider-side caching is the only caching.** A per-provider `cache_control` config (e.g., Anthropic `{"type":"ephemeral","ttl":"5m"|"1h"}`, OpenAI `prompt_cache_key`/`prompt_cache_breakpoint`/`prompt_cache_options.mode`, DeepSeek automatic — no client knob) replaces the local cache. Provider caches are **content-addressed** (exact-prefix KV) and **never cache the answer**: they store the prompt-prefix computation, so a changed input is a different prefix, the cache misses, and the response is freshly computed (OpenAI / Anthropic / DeepSeek caching docs, verified 2026-08-09 — see `docs/research/design-deltas.md` D1). There is no key under which an old prefix can be served against new content, and no stored response that could go stale. This deletes the review's core defect (LLM-C1: a response cache keyed on `(model, temperature, prompt)` with no repo state) by removing the cache, and makes the threat-model R8 "cache poisoning / stale cache" class structurally impossible.
- **The worker and orchestrator code do not manage any cache.** They may only place stable prefixes so the provider's cache hits — guidance, not a correctness mechanism (§9.3 prompt structure, D8c).
- **No `"cache_hit": true` tagging** in result envelopes; optimization harnesses cannot filter on a nonexistent cache.
- **Host-side cross-session caching is out of scope.** If a host system (outside Cambium) ever adds one, it must be content-addressed and repo-state-aware (D6 residue); shared cross-worker caching was already declared the host's job and is documented so the boundary is explicit.

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
    rpm: int = 60                   # token-bucket refill tokens/min (D8f)
    cache_control: dict | None = None   # provider-side caching knobs only (D1):
                                        #   {"type":"ephemeral","ttl":"5m"} etc.
```

### 9.2 Cascade (default mode)

```python
async def call(self, *, prompt: str, tier: str = "fast",
               model: str | None = None, temperature: float = 0.0,
               require_tools: bool = False,
               min_context_window: int = 0) -> LLMResponse:
    # 1. Filter providers: tier match; tool support if require_tools;
    #    context window if min_context_window; not in cooldown;
    #    token bucket non-empty (D8f).
    # 2. Sort by priority.
    # 3. Try each in order; on exception, mark cooldown and debit the
    #    provider's token bucket, continue.
    # 4. If all fail -> raise AllProvidersFailed(providers_tried, last_error)
    #    -> the dispatch queue pauses and a recovery monitor wakes it (D8f, §7.4).
```

*No cache check exists:* there is no local cache to check (D1).

Key changes from v0.1:

- **`tier` is the primary key.** A request for `"fast"` matches DeepCode v4 Flash, Gemini Flash, OpenAI Mini, Claude Haiku interchangeably. **No exact-model filter except when caller explicitly passes `model=`** (rare; used by optimization to pin a model).
- **Capability filtering.** `require_tools=True` skips providers with `supports_tools=False`. `min_context_window=600_000` skips Haiku. These are *explicit*, *documented* tradeoffs — not magic.
- **`AllProvidersFailed` is a real exception class**, defined in `cambium.diffundo.errors`, carrying the list of tried providers and the last error. The orchestrator catches it and parks dispatch (resolves IMPL-M5); under D8f the **dispatch queue pauses** and a recovery monitor wakes it when any provider recovers (§7.4).
- **Token-bucket rate limiting (D8f).** Each provider (optionally per tier) has a token bucket refilled at `rpm` tokens/min (`ProviderConfig.rpm`). Before each cascade attempt, `call` checks the provider's bucket; an empty bucket marks the provider `RATE_LIMITED` and the cascade skips it via the same selection-filter path as cooldown (step 1). The bucket bounds *throughput* — cooldown (`cooldown_s`) only bounds *failures*. Circuit breaker and capability tiers are unchanged (`docs/research/cascade-design.md` §2.3 sliding-window breaker: HEALTHY/COOLDOWN/OPEN/HALF_OPEN).
- **No per-call `dspy.LM` construction.** LMs are cached per provider on first use (resolves IMPL-N10).
- **Race mode** is removed from the default config (it was unsafe per LLM-M6 — fastest-typically-weakest bias, cancelled metered requests). If a caller genuinely needs "first of N," they get it by configuring N providers at the same priority; cascade returns the first success.

### 9.3 Worker-side Diffundo integration (resolves IMPL-C12)

Workers do **not** construct `dspy.LM` directly. They construct a `Diffundo` from the `fanout_config` field of `init`, with provider `api_key_env` names resolved from the inherited environment. The DSPy integration is via a custom `dspy.LM` subclass that routes calls through `Diffundo.call`:

```python
class CambiumLM(dspy.LM):
    def __init__(self, diffundo: Diffundo, tier: str, **kw):
        self._diffundo = diffundo; self._tier = tier; ...
    def __call__(self, prompt, **kw):
        # forwards tier/model/temperature only — no cache flags (D1)
        return self._diffundo.call(prompt=prompt, tier=self._tier, ...)
```

Workers `dspy.configure(lm=CambiumLM(diffundo, tier="fast"))`. Every DSPy call — `ReAct`, `ChainOfThought`, raw `dspy.LM` — flows through `Diffundo`. The headline provider-failover benefit reaches workers. `CambiumLM` passes **no cache flags**; `Diffundo.call` has none (D1).

**Prompt-structure convention (D8c).** Because provider-side caches are exact-prefix content-addressed, every `CambiumLM`/`Diffundo.call` caller follows a normative prompt layout: **static, byte-stable content at the TOP** (system prompt, AGENTS.md-derived guidelines, tool definitions, module instructions, task-independent few-shot context) and **dynamic content at the BOTTOM** (task spec, repo context, observations, tool results). Timestamps, `request_id`s, monotonic values, and per-call nonces are **never** placed at the top — they churn the exact-prefix key and destroy provider cache hits. This is guidance that enables upstream caching, **not** a correctness mechanism (consistent with §8.1); a prompt-lint check in the module test suite asserts static-before-dynamic ordering and no volatile tokens in the static prefix.

---

## 10. Coding Metric (resolves LLM-C5)

There is no single number that captures "did the agent write good code." v2 uses a **multi-signal metric**, computed by the orchestrator's `ResultEvaluator` (LLM-assisted) plus deterministic checks (run by `Unio` at merge time and by `Ascensus` offline). All signals are in `[0, 1]`.

| Signal | Source | Weight (default) | Gameability mitigation |
|---|---|---|---|
| `tests` | `Unio` runs the test command (no `\| tail`, no `set -o pipefail` issue — raw exit code, see §11) | 0.30 (floor) | **Tests are a floor, not a ceiling.** A run that fails tests scores 0.0 overall regardless of other signals. This is also the task's **D4 gate**: a task whose test gate fails never reaches `DONE` (§7.9). |
| `spec_adherence` | LLM-judge (`ResultEvaluator`) using a fixed rubric, scored 1–5 normalized to [0,1] | 0.30 | Rubric is **pre-registered per task** in the dataset; judge sees only the spec + diff + test output, not the worker's summary. |
| `diff_quality` | Deterministic heuristics: diff size in expected range, no test-file deletion, no `# noqa`/`# type: ignore` additions, no commented-out code, no large generated files | 0.20 | Heuristics are versioned in the metric module; changes require dataset re-eval. |
| `behavioral_checks` | Pre-registered assertions per task ("function X exists", "no `print()` statements", "config files unchanged") | 0.15 | Authored at dataset construction time; not visible to the worker. |
| `canaries` | Trap assertions that should **not** pass under reward hacking (e.g., "the worker did not delete the failing test", "the worker did not add `assert True` to inflate pass rate", "no `.cambium/` writes from worker") | 0.05 (gate) | A failed canary **zeroes the entire score** regardless of other signals. |

Final score: `score = (tests × w1 + spec_adherence × w2 + diff_quality × w3 + behavioral_checks × w4) × canaries`. Weights are per-task-type in config; defaults above.

**Held-out evaluation set.** `Ascensus` ships with 20+ reference coding tasks (in `src/cambium/modules/<name>/datasets/eval.jsonl`, schema in `docs/architecture/module-template/dataset-format.md`) with gold diffs and pre-registered rubrics. The held-out set is **never** used for training; it is the gate for shipping optimized prompts to production.

**Reward-hacking canaries.** Each held-out task ships with 3–5 canary assertions designed to detect the failure modes the metric would otherwise incentivize (deleting failing tests, no-op patches, `# noqa` additions, etc.). A prompt variant that improves the training metric while regressing the canary rate is **rejected** by the optimization harness, even if its score went up.

---

## 11. Worker Tool Set (resolves LLM-M2, IMPL-C4, IMPL-C5, IMPL-N4)

> The tool-set design and the per-tool heartbeat model below borrow concrete lessons from `docs/research/codex.md` ("Worktree lifecycle engineering", "Make git checkpoint/rollback implicit") and `docs/research/prime-agent.md` (children die mid-work — checkpoint early and often).

| Tool | Implementation | Notes |
|---|---|---|
| `read_file(path)` | `Path.read_text(encoding="utf-8")` | Rejects paths outside the worktree. |
| `write_file(path, content)` | `Path.write_text(content, encoding="utf-8")` | **NOT `write_content`** (the v0.1 bug). Atomic via temp-file + `os.rename`. |
| `edit_file(path, old_string, new_string)` | Search-and-replace with **uniqueness check**: errors if `old_string` matches 0 or >1 locations. | **New.** Closes the "agent must rewrite the whole file" gap. Matches Claude Code / Aider conventions. |
| `run_shell(cmd, timeout=120)` | `asyncio.create_subprocess_shell`, wrapped in per-tool heartbeat loop (§7.6). | `shell=True` remains a **deliberate, permission-gated** capability (D7): it is offered only when `init.permissions.shell == true`, every command is logged verbatim in the event log (§5.2 `tool_event.cmd`), and it runs under the per-task wall/timeout and heartbeat budget (§7.6). No shell where a list form exists — already realized for `git_op` and `grep_code`. It is the documented residual high-privilege tool, gated by the allowlist rather than by a (removed) sandbox. |
| `git_op(op, args)` | `subprocess.run(["git", op, *shlex.split(args)])` — **list form, no shell** | Eliminates the v0.1 shell-injection vector. `op` is allowlisted (`add`, `commit`, `status`, `diff`, `log`, `stash`); others rejected. |
| `grep_code(pattern, path)` | `subprocess.run(["rg", "-n", pattern, path])` — **uses ripgrep, list form** | Eliminates the `grep -rn '{pattern}'` injection vector (IMPL-N4). Falls back to stdlib `re` if `rg` not on PATH. **Always `return`s the result** (fixes IMPL-C5). |

**Tools that are deliberately absent** at v2:

- No `fetch_url` / `curl` tool. Network egress is gated by the permission allowlist (`init.permissions.network`, off by default) plus host approval gates for non-allowlisted egress (D7).
- No structured-edit patch tool. The `edit_file` search-and-replace primitive covers the common case; full diff/patch parsing is deferred to v2.1.
- No AST/symbol search. Planned for v2.1.

**All tool calls** are wrapped by the heartbeat-emitting tool runner, so even `run_shell(cmd, timeout=300)` cannot trip the watchdog.

---

## 12. Secrets Management (resolves IMPL-M6)

### 12.1 Threat model

- **At rest:** no API key is ever written to disk by Cambium. Keys live in the host process's environment.
- **In transit to workers:** keys are inherited via the subprocess environment; they never appear in protocol messages.
- **In logs:** every event passes through a redaction filter before it reaches the writer thread.
- **At spawn (D7, resolves threat-model R4):** the worker env is a **constructed least-privilege dict** — `PATH` (minimal), `PYTHONUNBUFFERED=1`, `CAMBIUM_TASK_ID`, `CAMBIUM_GENERATION`, `CAMBIUM_SESSION_ID`, optional worktree-scoped `HOME`, **plus only the keys named in `init.provider_env_keys`**; everything else is dropped (§7.2). This is the per-worker key allowlist the removed sandbox enforced via `--setenv`, repointed to spawn-time env construction.

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
- **Non-blocking:** every logger is wired with a `logging.handlers.QueueHandler` that feeds a single `QueueListener` running on a background thread. The listener writes to a `logging.handlers.RotatingFileHandler` (100 MB × 5 files) at `${session_dir}/.cambium/cambium.log`.
- **Per-module loggers:** `cambium.nuntius`, `cambium.diffundo`, ..., `cambium.opifex.<task_id>`. Levels configurable per module in config.
- **Correlation:** every record carries `task_id`, `request_id`, `generation`, ` monotonic_ms`. Set via `logging.LoggerAdapter` per task.
- **Redaction:** a `logging.Filter` applies the same redaction as §12.3.
- **stderr from workers** is captured by the supervisor and forwarded to the event log as `kind="log"` events with `level` and `module` fields parsed from common prefixes (`WARNING`, `ERROR`, etc.). Unparseable stderr lines are stored verbatim at level `INFO`.
- **Diagnostics command (`cambium doctor`).** A health check that validates log↔state consistency, reports dropped-event counters from §6.5, verifies worktree↔ref alignment, and flags stale `.lock` files. Modeled on `codex doctor` (see `docs/research/codex.md` "Persistence = append-only log + migrated SQLite, plus a diagnostics command"); Codex's local install shows the exact drift failure mode (rows pointing at missing rollout files) this command exists to surface early.

---

## 14. Python Stance

- **`requires-python = ">=3.14,<3.15"`** in `pyproject.toml`. Pinned to 3.14, as required by the task. Verified against a real 3.14.7 install: see `docs/research/python-3.14.md` ("Recommendation for Cambium").
- **Standard CPython (GIL) build.** Free-threaded (`python3.14t`) is **not** required, **not** the default, and **not** recommended for v2. Rationale (drawn from `docs/research/python-3.14.md`):
  - Workers are separate **processes**, not threads; the GIL is irrelevant to inter-worker parallelism. Process isolation already delivers multi-core parallelism.
  - The supervisor is single-threaded asyncio.
  - The only in-process multi-threaded code is `Ascensus` (offline SIMBA fan-out); `asyncio.to_thread` for LLM/`git` calls is I/O-bound and releases the GIL on blocking I/O regardless.
  - Free-threading adds ~5–10% single-threaded overhead on 3.14 (per `docs/research/python-3.14.md` §"Cost/benefit in 3.14"; the 10–40% figure measured on 3.13t is outdated), disables the JIT, and adds C-extension risk (DSPy, LiteLLM, tokenizers, torch, numpy FT-safety is **UNVERIFIED** per the same doc).
  - Free-threading is officially supported in 3.14 (PEP 779) but still optional and not the default; pinning plain 3.14 yields the GIL build for everyone (`docs/research/python-3.14.md` §"GIL / free-threading").
- **Free-threading is an opt-in extra** for users who want SIMBA thread-level CPU parallelism:
  ```toml
  [project.optional-dependencies]
  free_threaded = []  # marker only; user supplies python3.14t and the FT-safe wheel set
  ```
  Pair it with a documented fallback (`ProcessPoolExecutor` or 3.14's `concurrent.futures.InterpreterPoolExecutor`, both available in 3.14 and verified present in `docs/research/python-3.14.md`), as the implementation review's M1 asks. On the GIL build, `InterpreterPoolExecutor` is preferred for in-process parallelism — it gives real multi-core (per-interpreter GIL, PEP 684) without FT risk.
- **3.14 features the design relies on**, all verified in `docs/research/python-3.14.md`: `asyncio.to_thread`, `asyncio.timeout`, `asyncio.TaskGroup`; PEP 649/749 lazy annotations (lets DSPy/LiteLLM type-heavy code drop `from __future__ import annotations`); `multiprocessing`'s new `forkserver` default and `Process.interrupt()` for clean worker stop; `concurrent.interpreters` for future worker-pool work. **Migration note:** `forkserver` is now the default multiprocessing start method on Linux — any Cambium code that relied on `fork` semantics must be re-validated (per the same doc).
- **`asyncio.to_thread`** is used for the rare synchronous, CPU-light blocking call inside the supervisor (e.g., `git` invocations). It runs on the default thread pool, which is fine because no shared mutable state is touched inside those calls.
- **Subprocess-per-worker** design means each worker is a fresh Python interpreter. Cold-start cost is documented (IMPL-M2) and mitigated by `ready_timeout`, and now by **persistent NodeSessions within a task** (D3): the IPC model that v2.0 deferred ("multiple init messages per process") is adopted as repeatable `steer` turns over one session's lifetime (§5.2), amortizing the `import dspy` cold start (~2.1 s) across a session. The **cross-task persistent pool** is still deferred to v2.1, with the measured benchmark recorded in `docs/research/worker-coldstart.md` (branch `wt-coldstart`).

---

## 15. TUI Policy

> The TUI design lessons below are drawn from `docs/research/tui-best-practices.md` (which inspects opencode 0.0.0-dev-202608071959, Codex 0.146.1, and Claude Code locally) and `docs/research/codex.md`. Cited inline.

- **The TUI (`Janus`, M10) is a view, not a controller.** It subscribes to `Session.events()` and renders `Event` objects. It does not call `Custos` directly. This mirrors the `opencode run --format json` and `codex exec` headless surfaces — both ship a JSON event stream that any UI can render (`docs/research/tui-best-practices.md` §1, `docs/research/codex.md` "Headless exec is the harness surface").
- **Headless-first.** Every feature reachable from the TUI is reachable from the public API. If a feature is TUI-only, that's a bug. opencode and Codex both ship their TUI as a thin layer over the same protocol their headless modes speak; we follow the same rule.
- **The TUI is optional at runtime.** `pip install cambium` does not require the TUI's dependencies; `pip install cambium[tui]` adds them. The TUI lives in `cambium.tui` behind the extra.
- **The machine interface is JSON-Lines events.** A host system that wants to render its own UI reads `Session.events()` exactly as the TUI does. There is no second API.
- **TUI is NOT in scope for v2 P0.** It is P2 and depends only on the Public API. Build it last, after the headless contract is locked.

---

## 16. Proto-AGI Integration — Cambium as a Leaf Module

A proto-AGI host treats Cambium the way Cambium treats a worker: as a subprocess with a structured contract. This section defines that contract.

### 16.1 Control plane vs data plane

- **Control plane** (lifecycle): `spawn`, `poll`, `wait`, `stop`, `kill`, `query`. Owned by `Instance` (§3.5). Transport: in-process function calls if Cambium is embedded as a library, or process signals/stdin if Cambium runs as a standalone subprocess wrapping `cambium.cli`.
- **Data plane** (work): the `Result` envelope (§3.4) and the event log. The host reads `Result` from `${session_dir}/.cambium/result.json` after `Instance.wait()` returns, or subscribes to `Session.events()` for live observation.

### 16.2 Session directory contract

> **Naming note (canonical):** Cambium's per-session state directory is the **dotted** `.cambium/` — a hidden directory, consistent with the worktree-local `.cambium/generation` fencing file (§7.3) and common convention. `${session_dir}/.cambium/` is the canonical path everywhere in this document; the bare `cambium/` form is not used. (Research docs in `wt-logging` use `${SESSION_DIR}/.cambium/`; this document agrees.)

The host owns `${session_dir}/`. Cambium owns **only** `${session_dir}/.cambium/`:

```
${session_dir}/
├── host-controlled files         # upper system's state
└── .cambium/                      # Cambium owns everything below
    ├── events.db                  # SQLite WAL
    ├── events.jsonl               # optional mirror
    ├── cambium.log                # rotated logs
    ├── result.json                # written atomically on completion
    ├── status.json                # written on every state change (read by poll())
    ├── worktrees/                 # one subdir per active worktree
    ├── checkpoints/               # one subdir per task
    ├── sessions/                  # per-node session history (D2/D8g)
    │   ├── conversations.db       # SQLite WAL, queryable (§6.6)
    │   └── <node_id>/             # per-node store; pruned with the session dir
    ├── quarantine/                # worktrees that failed recovery
    └── optimized/                 # DSPy artifacts loaded by Ascensus
```

*(`sessions/<node_id>/` is introduced by D2 and stored SQLite-WAL-backed by D8g; the event-log `events.db` may additionally hold per-session `gate_verdicts` records, §7.9.)*

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
4. **The `${session_dir}/.cambium/` layout is stable.** The host can archive it without parsing.
5. **No implicit global state.** Two Cambium instances in two different `session_dir`s do not interfere. No `/tmp/cambium-*` files; no `~/.cambium`; no shared caches.

---

## 17. DSPy-Per-Module Strategy (resolves LLM-C4)

### 17.1 The coupling problem, restated

`Architectus` has four decision modules: `should_decompose` (v2 rule engine today, DSPy seam documented in `docs/architecture/module-template/example-spec.md` §5.1), `TaskDecomposer`, `TaskRouter`, `ResultEvaluator`. `Opifex` has its own worker ReAct module. All are `cambium.modules.base.Module` subclasses with `decide()` + `metric()` methods (see the scaffold at `src/cambium/modules/base.py`). v0.1 claimed all five were "independently hill-climbable." They are not: the worker metric depends on the decomposer's output, the decomposer metric depends on the worker's competence, etc. SIMBA on one module with the others held fixed is a moving-target optimization.

### 17.2 Decoupling via pinned siblings and held-out eval

Each module is optimized against **frozen references** of its siblings, not their live co-adapted versions.

| Module | Optimization input | Sibling pinning | Held-out metric |
|---|---|---|---|
| `ShouldDecompose` | `task, context → decompose` | None needed (input is just the spec). | Accuracy on a frozen 50-spec held-out set (train split = 200). |
| `TaskDecomposer` | `spec → TaskTree` (DAG, §3.7) | **Stub Worker** that returns canned results per node ID. | Tree-completion rate on a frozen 50-spec dataset with pre-registered gold decompositions (incl. cycle-free DAG validation, I2.2). |
| `TaskRouter` | `subtask, worker_profiles → route` | Stub Worker pool with declared tiers. | Routing accuracy vs gold routing on 100 cases. |
| `ResultEvaluator` | `spec, diff, test_results → verdict` | None (input is post-hoc). | Verdict accuracy + F1 on 100 hand-labeled (spec, diff, verdict) triples. |
| `Opifex` (worker ReAct) | `task, context → action` | **Stub Decomposer** that returns the canonical decomposition for each task. | Multi-signal metric (§10) on 50 reference coding tasks. |

### 17.3 Per-module artifacts

Each module ships, under `src/cambium/modules/<name>/`:

```
src/cambium/modules/<name>/
├── architecture.md           # per-module design (template: docs/architecture/module-template/architecture.md)
├── __init__.py               # public exports (module class, input/output, loader, metric)
├── decide.py                 # primary implementation (rule engine today) + the Module subclass;
│                             #   this file is also the DSPy seam — a future DSPy program
│                             #   replaces the engine behind `Module.decide`.
├── metric.py                 # metric function: (example_with_prediction) -> float in [0,1]
├── dataset.py                # DatasetLoader subclass (validates JSONL → Example records)
└── datasets/
    └── <name>_pairs.jsonl    # v2: single combined dataset; records may carry `canary: true`.
```

**v2 extensions (labeled, opt-in):** the scaffold ships a single combined `datasets/<name>_pairs.jsonl` with inline `canary: true` markers (see `src/cambium/modules/example/` for the reference). Splitting into `train.jsonl` / `eval.jsonl` / `canaries.jsonl` per `docs/architecture/module-template/dataset-format.md` is the planned v2.1 layout — until then, canaries are inlined in the single file and an `eval.py` harness entry point selects them by the `canary` flag. A `siblings-stub.yaml` is added when this module becomes siblings with another (the `should_decompose` reference has none — it is the first module in the pipeline).

**Harness state (D5).** Each module's optimizable surface is its **harness state** — the module's prompt/decide program **plus** skills/memories **plus** dataset **plus** metric — extending the artifact list above. Proposed store: `src/cambium/modules/<name>/harness/` (`prompts.yaml`, `skills/`, `memories/`, `meta.json` with refinement history), with promoted artifacts under `optimized/<name>/` (§16.2) and `meta.json.sibling_pins` tracking pinned sibling versions.

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

**Refinement loop (D5).** Each module's optimization is an **evidence-backed refinement loop over its own harness state**, making the plan/apply split above first-class:

- **Plan/apply split.** A refinement is first a **proposal** (a `refinement_id` + the planned edit to harness state + the evidence behind it, including a mandatory before/after eval delta table per signal). Only after the proposal passes its gates is it **applied** (promoted) via the versioned pointer swap (`optimized/<name>/v<N>/`).
- **Rollback by refinement ID.** Every applied refinement records a `refinement_id`; promotion is a symlink-swap and any refinement can be rolled back atomically by restoring the previous pointer.
- **Canary-gated.** The gate is the existing three-split evaluation: mean metric on frozen `eval.jsonl` ≥ threshold **and** canary pass rate 100% (§10, `docs/architecture/module-template/dataset-format.md` §6). A degraded canary score → the refinement is **rejected**, i.e., rolled back to the previous `refinement_id`. Frozen, dataset-time-authored canaries that are invisible to the refiner are the defense against reward-hacking refinements (e.g., a module teaching itself to game the metric — deleting failing tests, `assert True`, no-op patches).
- **Human approval for out-of-scope harness edits.** Edits to the module's **own** prompt/decide program are gateable by eval alone; edits that reach **beyond module scope** — the dataset (labels, splits, canaries), the metric, or sibling pins (`meta.json.sibling_pins`) — require **human approval** before apply (an approval-gate callback in the host, the same mechanism as D7's approval gates, or an explicit halt-and-queue state).

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
| DS-M4 | FanOut cache/provider state unsafe under threads | The cache is deleted (D1) — nothing left to race. What remains: LM construction cached per provider (per-process) and cooldown/token-bucket state tracked in a `threading.Lock`-protected structure when needed. | §8.1, §9 |
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
| LLM-C1 | FanOut cache ignores repo state | **Resolved by deletion (D1).** No local cache exists; provider-side caching is content-addressed and never stale. | §8.1 |
| LLM-C2 | Cascade not cascading across models | `tier` field; cascade tries all providers in tier; no exact-model filter except when caller explicitly pins. | §9.2 |
| LLM-C3 | Provider/model transparency assumed | Capability metadata on `ProviderConfig` (`supports_tools`, `context_window`); `require_tools` and `min_context_window` filters; tradeoffs documented. | §9.1, §9.2 |
| LLM-C4 | "Independently hill-climbable" is false | Claim restated: "per-module optimizable against pinned siblings"; held-out eval per module; canary rejection. | §17 |
| LLM-C5 | No automatic metric for coding tasks | Multi-signal metric: tests (floor) + spec-adherence (LLM judge) + diff-quality (heuristics) + behavioral checks + canaries (gate). | §10 |
| LLM-C6 | No "do not decompose" path | `ShouldDecompose` classifier module; single-task fast path bypasses decomposition. (Spec'd as the reference example module — `docs/architecture/module-template/example-spec.md`.) | §4 (Architectus), §17 |
| LLM-M1 | Default test command broken (`\| tail`) | `Unio` uses raw `subprocess.run` exit code; no shell pipe; full output captured, truncated in Python. | §10, §11 |
| LLM-M2 | Worker tool set inadequate | Adds `edit_file` (search-and-replace with uniqueness); fixes `write_file`/`grep_code`; structured-edit tool documented as v2.1. | §11 |
| LLM-M3 | Optimization flywheel coupled, no stability | Held-out eval, canaries, human gate, rollback. | §10, §17.4 |
| LLM-M4 | `ReAct` checkpoint callback doesn't exist in DSPy | v2 implements checkpointing via a `ReAct` subclass (`OpifexReAct`) that overrides the step loop to call `checkpoint()` between steps; documented in `Opifex` architecture. | §6.4, module template |
| LLM-M5 | Cache per-instance nearly useless | **Resolved by deletion (D1).** No local cache at all — the per-instance cache, its upstream variant, and the "shared cross-worker cache is the host's job" residue all disappear with it. | §8.1 |
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
| IMPL-C7 | Sandbox space in identifier + undefined `sys` | **Moot by removal (D7).** The Septum module no longer exists; there is no sandbox identifier to fix. | §4 (M8) |
| IMPL-C8 | Orchestrator awaits sync methods / undefined merge/evaluate | `Architectus` interface normatively defined; sync vs async decided per method. | §4 (Architectus) |
| IMPL-C9 | Metric syntax errors (`polymorphism`, missing `==`) | v2 metric module is syntactically valid Python; tested before merge. | §10 |
| IMPL-C10 | Cascade no-op when model resolved | Resolved by LLM-C2 mechanism. | §9.2 |
| IMPL-C11 | `shutdown()` calls `.kill()` on Tasks | v2 shutdown uses process objects directly; awaiting `proc.wait()` via wrapped tasks; correct API. | §7.7 |
| IMPL-C12 | Worker bypasses FanOut | Workers construct `Diffundo` from `fanout_config` and route DSPy through `CambiumLM`. | §9.3 |
| IMPL-M1 | Python 3.14 free-threaded experimental | Standard 3.14; free-threading opt-in. | §14 |
| IMPL-M2 | Subprocess cold-start unbounded | Documented; `ready_timeout` (default 60 s); persistent pool deferred to v2.1. | §14 |
| IMPL-M3 | Git worktree concurrency / `gc.auto` | `Surculus` sets `gc.auto=0` on the cambium-managed repo; retries `worktree add` on lock contention; never mutates `main` from worker code. | §7 (Surculus) |
| IMPL-M4 | Linux-only sandbox backend | **Moot by removal (D7).** Septum is removed from v2 scope; there is no kernel-namespace / `SandboxExecSandbox` / `NoopSandbox` backend. Containment = worktree isolation + permission allowlists + approval gates (§7.2). | §4 (M8) |
| IMPL-M5 | `AllProvidersFailed` undefined / unhandled | Defined in `cambium.diffundo.errors`; orchestrator catches it and parks dispatch; queue pauses + recovery monitor (D8f). | §9.2, §7.4 |
| IMPL-M6 | No secrets management | Env-only; redaction filter; never in JSON init; **per-worker key allowlist enforced at spawn-time env construction** — the removed sandbox's `--setenv` norm is retained, its enforcement mechanism repointed to the least-privilege worker env (D7). | §12, §7.2 |
| IMPL-M7 | No real logging | stdlib `logging` + `JsonFormatter`; `QueueHandler` + `QueueListener`; rotation; correlation IDs. | §13 |
| IMPL-M8 | No test strategy | Module template requires test strategy; `Ascensus` ships with fake-LLM and fake-worker harnesses. | §17, module template |
| IMPL-M9 | Restart reuses corrupted worktree | Fixed by `Surculus.recover()` (DS-C5). | §7.5 |
| IMPL-M10 | Heartbeat timing coarse / readiness gap | Configurable interval/timeout; supervisor **waits for `ready`** before sending further messages. | §7.2 |
| IMPL-N1..N14 | Various code-quality bugs | All fixed by the v2 normative specs; verified by the module-template's "smoke test passes" gate. | per-module |

### 18.4 Consensus items (`system-design.md` §9 table)

Every F1–F12 item in the v0.1 consensus table is resolved by the matrix above:
F1=§6.2, F2=§8.1 (cache deleted by D1), F3=§7(Unio), F4=§9.3, F5=§9.2, F6=§7.6, F7=per-module specs, F8=§11, F9=§17, F10=§10, F11=§6, F12=§4(M8 — Septum removed from v2 scope by D7).

---

## 19. Why Projects Succeed or Fail

Concrete factors, and how this design addresses each. Ordered roughly by observed frequency of failure in comparable systems.

1. **Testability without TDD.** TDD is not required, but every module ships with a test strategy (template field) and a smoke test that must pass before the module is marked P0-complete. The v0.1 reviews identified ~12 syntax bugs that a single dry run would have caught — that gate now exists. Addressed: §17, `docs/architecture/module-template/architecture.md` (test strategy field).

2. **Verifiable metrics.** Every DSPy module has a metric that runs without human-in-the-loop scoring, against a frozen held-out set. Without this, optimization hill-climbs toward a proxy. Addressed: §10, §17.

3. **Canaries against reward hacking.** Every held-out task ships with 3–5 trap assertions designed to detect the failure mode the metric would otherwise incentivize. A prompt variant that improves training metric while regressing canary rate is rejected. Addressed: §10, §17.4 step 8.

4. **Incremental milestones.** Each module is independently buildable and independently testable. The build phases (P0/P1/P2) have explicit entry conditions, not just exit conditions. Addressed: §4; per-module `architecture.md`.

5. **Adversarial review gates.** Every module passes an adversarial review before merge; integration reviews re-run on every cross-module contract change. The three v0.1 reviews are the template for what a review looks like. Addressed: `docs/architecture/reviews/` are now first-class artifacts; `agents.md` documents the review gate.

6. **No hidden global state.** Config is explicit (`Config` dataclass, frozen). No module-level mutables. All runtime state lives under `${session_dir}/.cambium/`. Two Cambium instances in two session dirs do not interfere. Addressed: §16.2 invariant 5.

7. **Fail-fast on invariant violations.** Generation mismatches, parse errors, lock files, missing providers, missing env vars — all cause explicit failure with a typed event in the log, never silent corruption. The system tells you when it is broken. Addressed: §5, §7.3, §12.

8. **Honest claims.** The v0.1 "independently hill-climbable" claim is restated as a hypothesis (§17). The "Temporal-style durability" claim is backed by SQLite WAL (§6), not by append-only file writes. The "zero dependencies" claim is dropped (DSPy pulls LiteLLM et al.). Honest claims survive contact with production; marketing claims don't.

9. **I/O off the hot path.** Every disk write is on a dedicated thread or in a subprocess. The supervisor's event loop never blocks on disk. Addressed: §6.2, §13.

10. **Explicit concurrency guards.** Merge sequencer is locked. FanOut cooldown is locked. Event log is single-writer. Worker subprocesses are in their own process group. Nothing races implicitly. Addressed: §6.2, §7, §8, §9.

11. **Cross-platform by construction.** v2 has **no sandbox backend to abstract** — containment (worktree isolation + permission allowlists + approval gates) is pure git + subprocess mechanics, identical on every platform (D7). Deployment-side isolation (containers/microVMs) is the host's choice, not a Cambium platform layer (D8e). macOS is a first-class dev platform. Addressed: §4 (M8), §7.2.

12. **Secrets handled once, correctly.** Env-only at rest, inherited via subprocess env, never in protocol messages, redacted at the log boundary, **per-worker env allowlist enforced at spawn** (the removed sandbox's `--setenv` norm, repointed — D7). Documented threat model. Addressed: §12, §7.2.

13. **Real logging.** stdlib `logging`, structured (JSON), non-blocking (QueueHandler/QueueListener), rotated (100 MB × 5), redacted, correlated (task_id + request_id + generation). No `print()` in worker code. Addressed: §13.

14. **Bounded everything.** Restarts (10 absolute), wall time (1800 s/task), turns/tokens/timeout (supervisor-owned per session, D4), gate retries (2), memory (event ring buffer 10 000; queue 10 000), log size (rotation), worker count (config). No resource grows without bound — and no cache-size bound is needed because there is no local cache (D1). Addressed: §6.2, §7.4.

15. **Smoke test as gate.** No module is marked complete until the end-to-end smoke test (fake LLM + 1 worker + 1 merge) passes against it. This is the single highest-leverage practice the v0.1 reviews identified. Addressed: `agents.md` documents the gate; the example module spec (`docs/architecture/module-template/example-spec.md`) demonstrates it. **Milestone 0 is the FIRST implementation milestone (D6 residue (b))**: "one worker, one file, one merge" — spawn, single `edit_file`, scenario test gate passes, branch merges via atomic `update-ref`, `result.json` written, clean exit. Nothing is P0-complete until it passes.

**Failure modes this design does not yet address (honest gaps):**
- No kernel-namespace boundary for `run_shell` — the accepted residual risk of the no-sandbox posture (threat-model R3 re-rated "accepted — out of scope"); the remaining controls are worktree isolation, permission allowlists, and approval gates (D7). Deployment-side containers/microVMs are the host's isolation vehicle (D8e).
- Cold-start latency for subprocess-per-worker (mitigation documented: persistent NodeSessions within a task amortize the `import dspy` cost — D3; the cross-task persistent pool is deferred to v2.1).
- Cross-model prompt transfer during optimization (documented; mitigation is per-model optimization, deferred to v2.1).
- The "doom loop detector" pattern from Claude Code is on the v2.1 list, not in v2.

---

## 20. References

### Review and prior-design artifacts
- `docs/architecture/system-design.md` — v0.1 draft (superseded).
- `docs/architecture/reviews/review-distributed-systems.md` — DS review (391 lines).
- `docs/architecture/reviews/review-llm-design.md` — LLM review (242 lines).
- `docs/architecture/reviews/review-implementation.md` — implementation review (326 lines).

### Research docs (in main; cited from this document)

The research docs live under `docs/research/` in main. They are not present on this branch (the orchestrator merges them); references here are by stable path so coherence is auditable when the merge lands.

- `docs/research/python-3.14.md` — verified Python 3.14 capabilities. Cited in §14 (free-threading cost ~5–10% on 3.14t, JIT not available on FT builds, `concurrent.interpreters` / `InterpreterPoolExecutor` available, `forkserver` default, PEP 649/749 lazy annotations). Used as the empirical basis for the `requires-python = ">=3.14,<3.15"` GIL-build pin.
- `docs/research/codex.md` — OpenAI Codex CLI local-install analysis. Cited in §7.5 (worktree lifecycle: per-task worktree, detached HEAD, snapshot-before-delete, ~15-worktree GC, `.worktreeinclude`), §7.8 (no auto-mutation of working tree; publish is ref-update only), §13 (`codex doctor` as the model for `cambium doctor`), §15 (headless exec is the harness surface; `codex exec`/`review`/`mcp-server`).
- `docs/research/tui-best-practices.md` — opencode / Codex / Claude Code TUI-surface analysis. Cited in §15 (headless-first; JSON event stream as machine interface; TUI as thin consumer).
- `docs/research/prime-agent.md` — Prime Agent local-install analysis. Cited in §7.5 (socket/lock-file supervision failure modes that motivate Surculus lock-cleanup), §11 (children die mid-work — checkpoint-early lesson).
- `docs/research/opencode.md`, `docs/research/cloud-code.md`, `docs/research/omp.md`, `docs/research/pi.md`, `docs/research/pydev.md` — additional competitive-analysis context informing §4 module decomposition and §19 success factors. Not cited inline but tracked as background.

### Templates and orientation
- `docs/architecture/module-template/architecture.md` — per-module design template.
- `docs/architecture/module-template/dataset-format.md` — dataset JSONL schema, versioning, splits, canaries.
- `docs/architecture/module-template/example-spec.md` — reference module (`ShouldDecompose`) for first implementation.
- `agents.md` — repo-root orientation for new agents.

### Adopted deltas (authoritative amendments — see §21)
- `docs/research/design-deltas.md` — D1–D7 (v1.0.0). D1 (no local cache), D2 (task tree), D3 (persistent sessions/steer), D4 (gate + budgets), D5 (refinement loop), D6 (honest status + smoke-test milestone), D7 (no sandboxing; Septum removed).
- `docs/research/feedback-2-deltas.md` — D8a–D8g (v1.0.0). D8a (module CLI), D8b (envelope-only info hiding), D8c (prompt prefix layout), D8d (ports/adapters + DI), D8e (deployment isolation outside the harness), D8f (token bucket + queue pause), D8g (SQLite WAL conversation store).
- `docs/research/event-schema-draft.md` — event-log schema draft; payload-first `parent_task_id` (cited by §3.7, §6.3).
- `docs/research/ipc-protocol-draft.md` — IPC protocol draft; `steer`, `ready` gating, `PROTO_OUT_OF_ORDER`, `result_envelope` (cited by §5.2, §7.2).
- `docs/research/sandbox-options.md` — superseded evidence record for decision 10 / D7 (unprivileged user namespaces blocked by AppArmor on this host).
- `docs/research/worker-coldstart.md` — fork-per-task vs persistent-pool benchmark (branch `wt-coldstart`; cited by §14).

---

## 21. Adopted deltas (fold record)

**Date:** 2026-08-09. **Source docs:** `docs/research/design-deltas.md` (D1–D7), `docs/research/feedback-2-deltas.md` (D8a–D8g), `docs/research/event-schema-draft.md`, `docs/research/ipc-protocol-draft.md`, `implementation-plan.md` (decisions 8–10). This document (v2.0.0) was amended in place to reflect the adopted deltas; the delta docs remain authoritative for the reasoning and open questions behind each fold.

| Delta | Folded into this document |
|---|---|
| D1 — No local LLM cache; Diffundo is a stateless router | §0, §1, §2 (Diffundo line + invariant), §4 (M2), §8, §8.1 (replaced), §9.1/§9.2/§9.3 (cache params removed, `cache_control` config), §18.2 (LLM-C1/LLM-M5 resolved by deletion), §18.4 (F2), §19.14 |
| D2 — Task tree | §3.4 (Result `parent_task_id`), §3.7 (new: task-tree invariants I2.1–I2.7), §4 (M6), §5.2 (`init.parent_task_id`, `result.parent_task_id`), §6.3 (payload-first linkage), §6.6 (§16.2 `sessions/`), §7.1 (tree-level completion), §16.2 |
| D3 — Persistent sessions: steer/admission, NodeSession | §3.3/§3.7 (NodeSession terminology), §5.2 (`steer`, admission = supervisor-internal ack, result messages), §6.4 (session resume), §7.2 (admission), §14 (pool re-scoped), §19 (honest gaps) |
| D4 — Gate + budgets | §5.2 (`init.budget` + `gate_max_retries`), §7.1 (GATING/GATE_FAILED), §7.4 (supervisor-owned budgets), §7.8 (final-gate note), §7.9 (new: gate lifecycle + skip-if-unchanged), §10 |
| D5 — Refinement loop | §4 (M9/Ascensus), §17.3 (harness state), §17.4 (refinement loop: plan/apply, rollback-by-id, canary gate, human approval) |
| D6 — Honest status residue | §8.1 (repo-state-aware dedup), §19.15 (smoke-test-first milestone) |
| D7 — No sandboxing; Septum removed | §0, §1, §2 (diagram + containment box), §3.2 (Config), §4 (M8 removed), §5.2, §7.2 (spawn env + containment policy), §7.4, §11 (run_shell), §12 (secrets at spawn), §18.3 (IMPL-M4/IMPL-C7 moot, IMPL-M6 repointed), §19.11/19.12/19.14 + honest gaps |
| D8a — Module CLI | §4 (module catalog) |
| D8b — Envelope-only info hiding | §3.4 (Result `unified_diff` + I2.7), §5.2 (`result.diff`) |
| D8c — Prompt prefix layout | §9.3 (prompt-structure convention) |
| D8d — Ports/adapters + DI | §4 (module catalog) |
| D8e — Deployment isolation outside the harness | §7.2 (stdio worker in containers), §19.11 + honest gaps |
| D8f — Token bucket + queue pause | §9.1 (`rpm`), §9.2 (bucket check, AllProvidersFailed → queue pause), §7.4 |
| D8g — SQLite WAL conversation store | §6.1, §6.6 (new), §16.2 (`sessions/` + `conversations.db`) |

**Posture statements (normative after this fold):** (1) **No local LLM cache** — caching is provider-side only, content-addressed and never stale (D1). (2) **No sandboxing in the harness** — containment = git worktree isolation + permission allowlists + approval gates; Septum (M8) is removed from v2 scope; deployment-side containers/microVMs are the host's job (D7, D8e). (3) **Task tree is first-class** — DAG with payload-first `parent_task_id`, envelope-only upward results (D2, D8b).
