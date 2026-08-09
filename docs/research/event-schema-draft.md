# Cambium Event-Log Schema — DRAFT

**Status:** DRAFT. Research-stage proposal for the durable event log of the Cambium supervisor/worker lifecycle.
**Will be reconciled with:** the final architecture (`/tmp/opencode/cambium-arch/docs/architecture.md`, v2.0.0, build-ready) before any schema is frozen in code.
**Authoritative sources today:** architecture v2.0.0 §3.6 (Event record), §5 (liveness + IPC), §6 (event log + durability contract), §7 (lifecycle); the merged scaffold `src/cambium/events.py`; dataset versioning convention `docs/module-template/dataset-format.md` §5.
**Deviations from the architecture are explicitly flagged in §8 (Reconciliation Notes).**

---

## 1. Scope

This draft defines the complete event catalog for the Cambium supervisor/worker lifecycle:

- task submission and decomposition (`Architectus`),
- worker lifecycle (`Custos` + `Opifex`): spawn, ready, heartbeat, stdout/stderr, checkpoint, finish, fail, kill, restart,
- merge sequencing (`Unio`): start / success / failure,
- supervisor boot and shutdown,
- error and advisory events (parse errors, stalls, drops, recovery gaps, EOF advisories).

It covers the durable record (`events.db`, SQLite WAL), the optional JSON-Lines mirror, and the subscriber-facing `Session.events()` stream — all three are views over the same event shape (arch §6.1, §6.2, §16.2; `docs/research/tui-best-practices.md` line 268: the event stream *is* the interface).

Non-goals (kept out of this draft, matching arch §6.4): checkpoints are a **separate store** from the event log. The event log records *what happened*; checkpoints record *where to resume*. This draft defines only the event log.

---

## 2. Canonical envelope (field set)

Every event, in every store, is one JSON object with the same envelope. This is the single source of truth for field naming across this draft.

| Field | Type | Nullable | Meaning | Arch source | events.py seed |
|---|---|---|---|---|---|
| `event_id` | string (ULID) | no | Unique event identifier; correlation key across the SQLite DB, the JSONL mirror, and subscriber streams. **Draft addition** — see D2 in §8. | none (arch has `seq` only) | — |
| `kind` | string (enum, §3) | no | Event type from the catalog. | arch §3.6 `kind`; §6.3 `kind` | `type` |
| `seq` | integer | no | Per-session, gap-free, monotonic sequence number. Assigned by the sole event-log writer thread. | arch §6.3 `seq` | — |
| `ts` | float (epoch seconds) | no | Wall-clock timestamp, `time.time()`. | arch §3.6 `timestamp`, §6.3 `wall_ts` | `timestamp` |
| `monotonic_ms` | integer | no | `time.monotonic_ns() // 1_000_000`. Ordering is by `seq`; this is the drift-free monotonic clock for interval math. | arch §3.6 `monotonic_ms`, §6.3 | — |
| `task_id` | string | yes | Task this event belongs to; `null` for supervisor-only events (boot/shutdown, drops). | arch §3.6, §6.3 | `task_id` |
| `worker_id` | string | yes | **Derived** worker identity `"{task_id}#{generation}"`. **Draft addition** — see D3 in §8. | arch §7.3 (identity = task_id + generation) | `pid` (see §3) |
| `request_id` | string | yes | ULID correlation key from the Nuntius RPC framing; echoed from the initiating request. | arch §5 (framing), §3.6 | — |
| `generation` | integer | yes | Fencing token for the worker that produced this event. | arch §3.6, §7.3 | — |
| `payload` | object | no | Kind-specific fields. Redacted at enqueue time (arch §12.3, §9.3). | arch §3.6 `payload`, §6.3 | per-dataclass fields |

Field mapping to the seed scaffold (`src/cambium/events.py`), which is **normative for field naming**:

| events.py symbol | draft field | notes |
|---|---|---|
| `Event.type` | `kind` | The seed's three `type` strings (`"worker_started"`, `"worker_finished"`, `"log"`) are preserved as `kind` values in this catalog. |
| `Event.timestamp` | `ts` | Same source: `time.time()`. |
| `WorkerStarted.task_id`, `.pid` | `task_id`, `payload.pid` | Draft's `worker_started` (`phase="spawned"`) carries `pid` in payload; `worker_id` is the derived `task_id#generation` (the seed's `pid` is kept as an ephemeral observation, not an identity). |
| `WorkerFinished.task_id`, `.status`, `.exit_code` | `task_id`, `payload.status`, `payload.exit_code` | Seed default `status="finished"` ↔ draft `worker_finished`; non-default statuses ↔ `worker_failed` / `worker_killed`. |
| `LogEvent.level`, `.message` | `worker_stdout_line.payload.level`, `.line` | The seed's `log` type maps to the draft's advisory `worker_stdout_line` (and `worker_error` for worker-reported errors). |

Envelope JSON (example — full record for a heartbeat, §3.3):

```jsonc
{
  "event_id": "01JXKQZ9X2F4H1A6B3C8D0E5F7",
  "kind": "worker_heartbeat",
  "seq": 42,
  "ts": 1754212800.123,
  "monotonic_ms": 481234567890,
  "task_id": "wt-abc-001",
  "worker_id": "wt-abc-001#3",
  "request_id": null,
  "generation": 3,
  "payload": { "turn": 4, "tool": "run_shell", "status": "editing src/dry_run.rs" }
}
```

---

## 3. Event catalog

**Tier legend (durability):** **C** = critical (fsync before ack and before subscriber yield; zero loss window on supervisor crash), **NC** = non-critical (loss window ≤ `fsync_interval_s`, default 1 s). Tier assignments in the tier tables match arch §6.5 for every kind the architecture enumerates; tier assignments for draft-proposed kinds are flagged (D7, D8 in §8).

| # | `kind` | Tier | Emitted by | Arch kind / message mapping |
|---|---|---|---|---|
| 1 | `submitted` | **C** | Custos (on dispatch from Architectus) | arch `task_assigned` (§6.5 critical set) |
| 2 | `worker_started` | NC | Custos | arch `worker_spawned` + `worker_ready` (§6.5 non-critical set), folded via `phase` (D5) |
| 3 | `worker_heartbeat` | NC | Opifex | arch `heartbeat` (§6.5 non-critical set) |
| 4 | `worker_stdout_line` | NC | Custos (reader) | arch `log` (§13 stderr→log) + `parse_error` tagging (§5.4c); see D6 |
| 5 | `worker_checkpoint` | **C** | Opifex | arch `checkpoint` (§6.5 critical set) |
| 6 | `worker_finished` | **C** | Custos | arch `result` + `worker_exit` (critical set), `exit` msg `reason="done"` |
| 7 | `worker_failed` | **C** | Custos | arch `task_failed` (critical set), `error` msg, `exit` msg `reason="crash"|"fatal"` |
| 8 | `worker_killed` | **C** | Custos | arch `worker_exit` (critical set); watchdog/ping/no-pong/killpg paths (§5.3); draft kind (D7) |
| 9 | `restart_scheduled` | NC | Custos | restart policy (§7.4); draft kind (D7) |
| 10 | `task_decomposed` | NC | Architectus | `TaskDecomposer` (§4, §17.1); draft kind (D7) |
| 11 | `merge_started` | **C** | Unio | arch `merge_progress` (critical set), phase="started" |
| 12 | `merge_succeeded` | **C** | Unio | arch `merge_committed` (critical set) + `merge_reconciled` |
| 13 | `merge_failed` | **C** | Unio | arch `merge_progress` failure + `NonFastForward` (§7.8); draft kind (D7) |
| 14 | `supervisor_started` | **C** | Custos | boot marker; draft kind (D7) |
| 15 | `supervisor_shutdown` | **C** | Custos | §7.7 shutdown; draft kind (D7) |
| 16 | `worker_error` | NC | Opifex → Custos | arch `error` message (§5.2), `recoverable` flag (§7.4) |
| 17 | `parse_error` | NC | Custos (reader) | arch §5.1 inv. 4 / §5.4c parse-error tagging |
| 18 | `supervisor_stall` | NC | Custos | arch §5.3 drain-deadline watchdog |
| 19 | `drop` | NC | Custos (writer) | arch §6.2 overflow drop marker |
| 20 | `recovery_gap` | **C** | Custos (on replay) | arch §6.5 gap detection |
| 21 | `eof_seen` | NC | Custos (reader) | arch §5.3 layer-4 EOF advisory; draft kind (D7) |

### 3.1 `submitted` — task enters the session

Fields: `task_id` (target), payload `{spec, base_branch, parent_task_id?, depends_on?, budget.max_wall_s?, priority?}`.
The durable proof that a task was enqueued. For decomposed tasks, emitted once per subtask by Custos on `await supervisor.run_task(spec)` (arch §3.3, §7.1 PENDING→SPAWNING). `parent_task_id` links subtasks to the decomposition (§4).

```json
{ "event_id": "01JX0000...", "kind": "submitted", "seq": 5, "ts": 1754212800.001, "monotonic_ms": 481234567800,
  "task_id": "wt-abc-002", "worker_id": null, "request_id": "01H0000...", "generation": null,
  "payload": { "spec": "Refactor dry_run.rs to remove global state", "base_branch": "main",
               "parent_task_id": "wt-abc-001", "depends_on": ["wt-abc-001"], "budget": { "max_wall_s": 1800 } } }
```

### 3.2 `worker_started` — spawn, then readiness

Fields: `worker_id` = `task_id#generation`; payload `{phase, pid?, worktree?, base_commit?, resume_from_checkpoint?, ready_timeout_s?}`.
Emitted **twice per generation**: `phase="spawned"` on `create_subprocess_exec` (payload carries `pid`, `worktree`, `base_commit`, `resume_from_checkpoint` if resuming from a checkpoint, arch §6.4/§7.5), and `phase="ready"` when the worker's `ready` message arrives (arch §5.2, §7.2 — the supervisor waits for `ready` before RUNNING). The `ready` record carries `liveness.ipc_ready=true` (§6). `ready_timeout_s` (default 60) governs the cold-start kill path (arch §7.2, IMPL-M2).

```json
{ "event_id": "01JX0001...", "kind": "worker_started", "seq": 8, "ts": 1754212801.204, "monotonic_ms": 481234568400,
  "task_id": "wt-abc-002", "worker_id": "wt-abc-002#1", "request_id": "01H0001...", "generation": 1,
  "payload": { "phase": "ready", "pid": 20471, "worktree": "/abs/.cambium/worktrees/wt-abc-002",
               "base_commit": "a1b2c3d", "resume_from_checkpoint": "checkpoints/wt-abc-002/turn-003.json",
               "ready_timeout_s": 60 },
  "liveness": { "process_alive": true, "ipc_ready": true, "checkpoint_seen": "checkpoints/wt-abc-002/turn-003.json",
                "exit_message": null, "eof_seen": false, "watchdog_armed": { "interval_s": 15, "timeout_s": 90 } } }
```

### 3.3 `worker_heartbeat` — progress, not just liveness

Fields: payload `{turn, tool (string|null), status}`.
Emitted by the worker at `interval_s` (default 15) and **from inside long-running tool wrappers** so no default tool can run silently past the watchdog (arch §7.6). Three missed beats (90 s default) is the supervisor's kill trigger — but a heartbeat is never proof of death and never terminates anything by itself; it is the layer-3 signal in §6.

```json
{ "event_id": "01JX...", "kind": "worker_heartbeat", "seq": 42, "ts": 1754212800.123, "monotonic_ms": 481234567890,
  "task_id": "wt-abc-002", "worker_id": "wt-abc-002#1", "request_id": null, "generation": 1,
  "payload": { "turn": 4, "tool": "run_shell", "status": "editing src/dry_run.rs" } }
```

### 3.4 `worker_stdout_line` — advisory output, bounded

Fields: payload `{stream ("stdout"|"stderr"), level, line, line_no, truncated}`.
Only **non-protocol** bytes go here: all stderr (arch §13 — forwarded as `log` events with `level`/`module` parsed from common prefixes; unparseable lines stored verbatim at `INFO`) and stdout lines that fail JSON parse (arch §5.1 inv. 4, §5.4c — logged with their line number and skipped). **Valid protocol lines are never echoed raw**; they become their own typed events (D6). `line` is capped (default 4 KiB) and full output spilled to a managed directory per `docs/research/opencode.md` §4.6; `truncated` records the cap so replay tools know the tail is missing.

```json
{ "event_id": "01JX...", "kind": "worker_stdout_line", "seq": 43, "ts": 1754212800.140, "monotonic_ms": 481234567910,
  "task_id": "wt-abc-002", "worker_id": "wt-abc-002#1", "request_id": null, "generation": 1,
  "payload": { "stream": "stderr", "level": "WARNING", "line": "DeprecationWarning: x", "line_no": 12, "truncated": false } }
```

### 3.5 `worker_checkpoint` — durable resume point

Fields: payload `{turn, state_ref, commits_so_far}`.
Emitted after every tool call that produces or modifies durable state (file writes, commits); `state_ref` points at `${session_dir}/cambium/checkpoints/${task_id}/turn-${N}.json`, written atomically (arch §6.4). **Critical**: a caller may wait on this event as proof the state is safe to resume from. On restart, Custos re-injects the latest checkpoint via `init.resume_from_checkpoint` (§6.4, §7.5). Not a substitute for the event log.

```json
{ "event_id": "01JX0002...", "kind": "worker_checkpoint", "seq": 60, "ts": 1754212830.011, "monotonic_ms": 481234570200,
  "task_id": "wt-abc-002", "worker_id": "wt-abc-002#1", "request_id": null, "generation": 1,
  "payload": { "turn": 3, "state_ref": "checkpoints/wt-abc-002/turn-003.json", "commits_so_far": ["a1b2c3d"] } }
```

### 3.6 `worker_finished` — clean completion

Fields: payload `{status: "done", exit_code: 0, commits, files_changed, summary, metric_score, metric_breakdown, exit_message: {reason: "done"}}`.
Emitted when the worker sends `result` **and** the authoritative `exit` message with `reason="done"` (arch §5.2, §5.3 layer 2). `exit_message` is the layer-2 encoding from §6. The supervisor cross-checks: a worker that sent `result` but exited without `exit` is treated as crashed (arch §5.2). Metric fields mirror `Result` (arch §3.4).

```json
{ "event_id": "01JX0003...", "kind": "worker_finished", "seq": 95, "ts": 1754212900.500, "monotonic_ms": 481234580700,
  "task_id": "wt-abc-002", "worker_id": "wt-abc-002#1", "request_id": "01H0002...", "generation": 1,
  "payload": { "status": "done", "exit_code": 0, "commits": ["c9f8e7d"], "files_changed": ["src/dry_run.rs"],
               "summary": "Removed 3 global statics.", "metric_score": 0.84,
               "metric_breakdown": { "tests": 1.0, "spec_adherence": 0.9, "diff_quality": 0.7, "canaries": 1.0 },
               "exit_message": { "reason": "done", "generation": 1 } } }
```

### 3.7 `worker_failed` — permanent failure

Fields: payload `{status: "failed", exit_code, error_type, message, partial_commits, recoverable, failure_reason, exit_message: {reason: "crash"|"fatal"}}`.
Emitted on: non-recoverable worker error (`recoverable: false`, arch §7.4), restart budget exhausted (`burst_max` in `burst_window_s` or `absolute_max`), or wall-time budget exceeded. `failure_reason` is drawn from the `Result` vocabulary (`failed`/`timeout`/`rejected`/`cancelled`, arch §3.4). This is the critical event a caller waits on for "the task cannot complete."

```json
{ "event_id": "01JX0004...", "kind": "worker_failed", "seq": 120, "ts": 1754213000.900, "monotonic_ms": 481234590100,
  "task_id": "wt-abc-002", "worker_id": "wt-abc-002#3", "request_id": "01H0003...", "generation": 3,
  "payload": { "status": "failed", "exit_code": 1, "error_type": "build_failure",
               "message": "cargo build failed: 3 errors", "partial_commits": ["a1b2c3d"],
               "recoverable": false, "failure_reason": "max_restarts_exceeded",
               "exit_message": { "reason": "crash", "generation": 3 } } }
```

### 3.8 `worker_killed` — supervisor-initiated termination

Fields: payload `{exit_code, reason, elapsed_s?, exit_message: {reason: "cancelled"|"fatal"}}`, `reason ∈ {watchdog_timeout, ready_timeout, ping_no_pong, cancelled, shutdown, generation_mismatch}`.
Emitted whenever Custos terminates a worker: heartbeat watchdog trip (3 missed beats, §5.3 layer 3), `ready_timeout` cold-start kill (§7.2), the ping/no-pong process-group kill after EOF (§5.3 — the "EOF is not death" escalation), graceful shutdown cancel (§7.7), or a generation-mismatch fence hit (§7.3). The `generation_mismatch` case is the worker terminating **itself** after reading `.cambium/generation`; Custos records it as a kill for uniformity. Critical because it closes a worker's liveness story (D7).

```json
{ "event_id": "01JX...", "kind": "worker_killed", "seq": 118, "ts": 1754212995.400, "monotonic_ms": 481234589600,
  "task_id": "wt-abc-002", "worker_id": "wt-abc-002#2", "request_id": null, "generation": 2,
  "payload": { "exit_code": -9, "reason": "ping_no_pong", "elapsed_s": 15.2,
               "exit_message": null } }
```

### 3.9 `restart_scheduled` — policy decision, not a state change

Fields: payload `{attempt, generation_next, delay_s, jittered_delay, burst_restarts, burst_window_s, absolute_restarts, absolute_max, budget_remaining_s, resume_from_checkpoint}`.
Emitted when Custos decides to restart a crashed/terminated worker, with the full-jitter delay from arch §7.4 (`delay = random.uniform(0, base_delay_s * backoff_base ** n)`). It records the **intent**; the next `worker_started` (with `generation+1`) records the **effect**. Non-critical: reconstructible from the `worker_killed`/`worker_failed` + next `worker_started` pair (D7/D8).

```json
{ "event_id": "01JX...", "kind": "restart_scheduled", "seq": 119, "ts": 1754212995.450, "monotonic_ms": 481234589650,
  "task_id": "wt-abc-002", "worker_id": "wt-abc-002#2", "request_id": null, "generation": 2,
  "payload": { "attempt": 2, "generation_next": 3, "delay_s": 2.0, "jittered_delay": 1.37,
               "burst_restarts": 2, "burst_window_s": 60, "absolute_restarts": 2, "absolute_max": 10,
               "budget_remaining_s": 1550, "resume_from_checkpoint": "checkpoints/wt-abc-002/turn-003.json" } }
```

### 3.10 `task_decomposed` — Architectus output

Fields: payload `{parent_task_id, subtasks: [{task_id, spec, depends_on, model, base_branch}], cycle_detected}`.
Emitted by the orchestrator when `TaskDecomposer` (arch §4, §17.1) splits a spec; DAG cycle detection runs before dispatch (arch §18.1 DS-M6) and its result is recorded. Non-critical: the causal links consumers need are already in each subtask's `submitted` record via `parent_task_id` (D7/D8).

```json
{ "event_id": "01JX0005...", "kind": "task_decomposed", "seq": 2, "ts": 1754212799.500, "monotonic_ms": 481234567100,
  "task_id": null, "worker_id": null, "request_id": "01H0004...", "generation": null,
  "payload": { "parent_task_id": "wt-abc-000", "cycle_detected": false,
               "subtasks": [ { "task_id": "wt-abc-001", "spec": "Add kalman_fusion", "depends_on": [], "model": "deepcode/v4-flash", "base_branch": "main" },
                             { "task_id": "wt-abc-002", "spec": "Refactor dry_run.rs", "depends_on": ["wt-abc-001"], "model": "deepcode/v4-flash", "base_branch": "main" } ] } }
```

### 3.11 `merge_started` — Unio acquires the lock

Fields: payload `{task_ids, phase: "started", base_ref, verified_tip_prev?}`.
Emitted when Unio begins the serialized merge pipeline (verify in throwaway worktree under the `asyncio.Lock`, arch §4/§7/DS-M1). Part of the arch's critical `merge_progress` family — a caller can wait on it for "the main ref is about to move."

```json
{ "event_id": "01JX...", "kind": "merge_started", "seq": 200, "ts": 1754213100.000, "monotonic_ms": 481234600200,
  "task_id": null, "worker_id": null, "request_id": null, "generation": null,
  "payload": { "task_ids": ["wt-abc-001", "wt-abc-002"], "phase": "started",
               "base_ref": "refs/heads/main", "verified_tip_prev": "9d8c7b6" } }
```

### 3.12 `merge_succeeded` — main published

Fields: payload `{old_sha, new_sha, commits, files_changed, test_exit_code, canary_pass}`.
Emitted by `Unio.publish_merge` **after** the atomic `git update-ref` of `refs/heads/main` and **before** `publish_merge` returns (arch §7.8). It is the critical event a caller waits on for "the work is on main." `merge_reconciled` (arch §7.8 — emitted on recovery when `refs/heads/main` is ahead of the last `merge_committed` event) is a second `merge_succeeded`-shaped record with `payload.reconciled=true`.

```json
{ "event_id": "01JX...", "kind": "merge_succeeded", "seq": 210, "ts": 1754213200.000, "monotonic_ms": 481234610000,
  "task_id": null, "worker_id": null, "request_id": null, "generation": null,
  "payload": { "old_sha": "9d8c7b6", "new_sha": "f0e1d2c", "commits": ["c9f8e7d"],
               "files_changed": ["src/dry_run.rs", "src/kalman.rs"], "test_exit_code": 0, "canary_pass": true } }
```

### 3.13 `merge_failed` — merge cannot publish

Fields: payload `{reason ("conflict"|"test_failure"|"non_fast_forward"), conflicts, test_exit_code?, old_sha, new_sha?, quarantined_task?}`.
Emitted when the rebase/test gate fails (conflict or failing tests) or when `NonFastForward` aborts the publish (arch §7.8 — main moved during verification; orchestrator re-merges). Critical: it is the durable record of "merge did not happen," which replay must distinguish from "merge happened" (§4). Draft kind folding the arch's `merge_progress` failure semantics (D7/D9).

```json
{ "event_id": "01JX...", "kind": "merge_failed", "seq": 215, "ts": 1754213210.000, "monotonic_ms": 481234611000,
  "task_id": null, "worker_id": null, "request_id": null, "generation": null,
  "payload": { "reason": "conflict", "conflicts": ["src/dry_run.rs"], "test_exit_code": null,
               "old_sha": "9d8c7b6", "new_sha": null } }
```

### 3.14 `supervisor_started` — session epoch begins

Fields: payload `{session_id, host, pid, cambium_version, event_schema_version, config_hash, worktrees_pruned}`.
Emitted once per session boot, after `Surculus.prune()` (arch §7.5). Marks the start of a replay epoch and records `event_schema_version` (§7) for the store. Critical so a replay always has a boot anchor (D7/D8).

```json
{ "event_id": "01JX...", "kind": "supervisor_started", "seq": 1, "ts": 1754212799.200, "monotonic_ms": 481234566900,
  "task_id": null, "worker_id": null, "request_id": null, "generation": null,
  "payload": { "session_id": "s-20260809-001", "host": "build-01", "pid": 19801,
               "cambium_version": "0.2.0-dev", "event_schema_version": 1,
               "config_hash": "sha256:9f86d0...", "worktrees_pruned": 2 } }
```

### 3.15 `supervisor_shutdown` — session epoch ends

Fields: payload `{reason ("graceful"|"host"|"crash"), workers_terminated, pending_events_flushed, exit_code}`.
Emitted at the end of the arch §7.7 shutdown sequence, after the writer queue is flushed and the DB closed. `reason="crash"` is written by the **next** boot's replay when the previous epoch has no shutdown record (the tail-loss heuristic of §4). Critical: it is the boundary between replay epochs (D7/D8).

```json
{ "event_id": "01JX...", "kind": "supervisor_shutdown", "seq": 511, "ts": 1754213600.000, "monotonic_ms": 481234680000,
  "task_id": null, "worker_id": null, "request_id": null, "generation": null,
  "payload": { "reason": "graceful", "workers_terminated": 2, "pending_events_flushed": 0, "exit_code": 0 } }
```

### 3.16 Error and advisory events

All six carry the envelope unchanged; `payload` is listed per kind. `worker_error` and `parse_error` are worker-stream-derived; `supervisor_stall`, `drop`, `recovery_gap`, `eof_seen` are supervisor-generated.

**`worker_error`** (NC) — raw error report from the worker (`recoverable` flag, arch §5.2/§7.4). Never terminal by itself; the terminal decision is the critical `worker_failed`. payload `{error_type, message, partial_commits, recoverable, turn}`.

```json
{ "event_id": "01JX0006...", "kind": "worker_error", "seq": 88, "ts": 1754212870.000, "monotonic_ms": 481234578000,
  "task_id": "wt-abc-002", "worker_id": "wt-abc-002#1", "request_id": "01H0005...", "generation": 1,
  "payload": { "error_type": "build_failure", "message": "cargo build failed: 3 errors",
               "partial_commits": [], "recoverable": true, "turn": 6 } }
```

**`parse_error`** (NC) — a stdout/stderr line failed JSON parse; recorded with its line number and skipped (arch §5.1 inv. 4, §5.4c). payload `{stream, line_no, line_snippet, reason}`. Distinct from `worker_stdout_line` because it is a *protocol* hygiene signal, not content.

```json
{ "event_id": "01JX...", "kind": "parse_error", "seq": 44, "ts": 1754212800.145, "monotonic_ms": 481234567920,
  "task_id": "wt-abc-002", "worker_id": "wt-abc-002#1", "request_id": null, "generation": 1,
  "payload": { "stream": "stdout", "line_no": 13, "line_snippet": "{\"type\":\"heartbeat\"...", "reason": "truncated" } }
```

**`supervisor_stall`** (NC) — the drain-deadline watchdog fired: Custos has not drained this worker's stdout in >30 s; heartbeat enforcement is **suspended** for that worker until draining resumes (arch §5.3). This is the event that keeps "supervisor-induced stall" from being blamed on the worker. payload `{task_id, stall_s, drain_deadline_s, suspended_until_ms}`.

```json
{ "event_id": "01JX...", "kind": "supervisor_stall", "seq": 100, "ts": 1754212910.000, "monotonic_ms": 481234581000,
  "task_id": "wt-abc-003", "worker_id": "wt-abc-003#1", "request_id": null, "generation": 1,
  "payload": { "stall_s": 31.2, "drain_deadline_s": 30.0, "suspended_until_ms": 481234612000 } }
```

**`drop`** (NC) — the writer's bounded queue overflowed (default 10 000); the oldest non-critical events were dropped and a counter incremented (arch §6.2 inv. 2). Critical events are **never** dropped (they block up to 100 ms instead). payload `{dropped_count, kind, queue_max}`.

```json
{ "event_id": "01JX...", "kind": "drop", "seq": 300, "ts": 1754213300.000, "monotonic_ms": 481234630000,
  "task_id": null, "worker_id": null, "request_id": null, "generation": null,
  "payload": { "dropped_count": 3, "kind": "worker_heartbeat", "queue_max": 10000 } }
```

**`recovery_gap`** (C) — on replay, a gap in `seq` was detected (non-critical tail lost to a crash inside `fsync_interval_s`); the writer documents the lost range (arch §6.5). Critical because consumers must treat the range as absent. payload `{first_seq, last_seq, count, source}`.

```json
{ "event_id": "01JX...", "kind": "recovery_gap", "seq": 512, "ts": 1754213600.100, "monotonic_ms": 481234680100,
  "task_id": null, "worker_id": null, "request_id": null, "generation": null,
  "payload": { "first_seq": 505, "last_seq": 509, "count": 5, "source": "crash" } }
```

**`eof_seen`** (NC) — EOF on stdout observed. **Advisory only, never death** (arch §5.3 layer 4): the reader records `proc_poll` and the scheduled escalation, then proceeds through §6's grace sequence. A subsequent `worker_finished`/`worker_failed`/`worker_killed` carries the authoritative outcome. payload `{proc_poll ("alive"|"exited"|"unknown"), grace_s, pending_ping}`.

```json
{ "event_id": "01JX...", "kind": "eof_seen", "seq": 116, "ts": 1754212990.000, "monotonic_ms": 481234589000,
  "task_id": "wt-abc-002", "worker_id": "wt-abc-002#2", "request_id": null, "generation": 2,
  "payload": { "proc_poll": "alive", "grace_s": 5.0, "pending_ping": true } }
```

---

## 4. Sequence numbers and causal order

### 4.1 `seq` — per-session, gap-free, writer-assigned

- `seq` is a per-session `INTEGER PRIMARY KEY AUTOINCREMENT` (arch §6.3), assigned by the **sole writer thread** at dequeue time, immediately before `INSERT` (arch §6.2 inv. 1–3). The writer is a single consumer, so dequeue order = insert order = subscriber publish order; there is exactly one total order per session.
- **Gap-free invariant** (arch §6.3): `seq` is contiguous within a session; a gap on replay is *defined as* data loss. Gaps can only exist at the tail — non-critical events committed to the WAL but lost when the WAL is truncated by a crash before the next checkpoint (arch §6.5). On replay the writer emits `recovery_gap` documenting the lost range (§3.16).
- The in-memory ring buffer (last 10 000 events, arch §6.2) preserves the tail's `seq` values across a crash, so a fresh subscriber can be caught up without re-reading the DB; the `snapshots` table provides compaction points (arch §6.1).

### 4.2 Three clocks

| Clock | Field | Purpose |
|---|---|---|
| `seq` | integer | Total order; the only ordering that matters for state reconstruction. |
| `monotonic_ms` | integer | Interval math (watchdog deadlines, stall durations). Drift-free per host. |
| `ts` | float wall | Human correlation and cross-session comparisons; not used for ordering. |

### 4.3 Causal order

- **Within a worker's life:** `(task_id, seq)` is indexed (arch §6.3 `events_task_idx`); a worker's event sequence for one `worker_id` is a linear, causally ordered chain (`worker_started` → `worker_heartbeat`* → `worker_checkpoint`* → terminal).
- **Across restarts of the same task:** `generation` is the causal barrier (arch §7.3). All events of generation *N* are causally before all events of generation *N+1*; `restart_scheduled` (with `generation_next`) is the boundary marker (§3.9). Replay never mixes generations: a `worker_started` with `generation=N` supersedes every earlier generation for that task.
- **Across tasks:** subtask causality runs through `submitted.parent_task_id` (§3.1) and the orchestrator's DAG (arch §18.1 DS-M6). Merge causality is a **fork/join**: the parallel worker chains (forked at `task_decomposed`/`submitted`) converge at `merge_started`/`merge_succeeded`/`merge_failed`. `seq` makes the join point well-defined.
- **Across supervisor epochs:** `supervisor_started` (seq *a*) ... `supervisor_shutdown` (seq *b*) brackets one epoch. Recovery replays from the last snapshot; a missing `supervisor_shutdown` for the previous epoch, plus `recovery_gap`, signals an unclean end (arch §6.5, §16.3 "Kill ... event log may be truncated").

### 4.4 Replay reconstruction

1. Open `events.db` (SQLite replays the WAL automatically, arch §6.5). Read `meta.event_schema_version`; apply migration functions if older (§7).
2. Bootstrap from the last `snapshots` row, then read `events` where `seq > snapshot.seq` (arch §6.1).
3. Detect `seq` gaps → emit `recovery_gap`; treat the range as absent.
4. Apply events in `seq` order, maintaining per-`worker_id` chains and per-task terminal state (`worker_finished`/`worker_failed`/`worker_killed`).
5. Cross-check `refs/heads/main` against the latest `merge_succeeded`; on mismatch, emit `merge_reconciled` (arch §7.8) and record it.
6. Re-publish to fresh `Session.events()` subscribers in `seq` order (arch §6.2 inv. 5, §6.5).

---

## 5. Durability classes

Two classes, exactly as the architecture's normative contract (arch §6.5). Every catalog event is assigned exactly one tier (§3 table).

| Tier | Promise | Mechanism |
|---|---|---|
| **Critical** | Fsync-d to disk **before** the writer acks the producer and **before** the event reaches any `Session.events()` subscriber. Loss window on supervisor crash: zero (modulo simultaneous kernel/page-cache loss, covered by WAL + `synchronous=NORMAL`). | The writer thread enters critical-immediate mode: `PRAGMA wal_checkpoint(TRUNCATE)` + `os.fsync(wal_fd)` + `os.fsync(db_fd)` before processing the next event (arch §6.2 inv. 4, §6.5). |
| **Non-critical** | Appended to the WAL at the batching cadence (`fsync_interval_s`, default 1.0 s). Loss window on supervisor crash: at most `fsync_interval_s` of the most recent non-critical events. | Timer-driven `_fsync_now` runs even with no critical events, bounding the worst-case loss (arch §6.5). |

Catalog tier table (kind → tier, with arch §6.5 cross-reference):

| Critical | Non-critical |
|---|---|
| `submitted` (= `task_assigned`) | `worker_started` (= `worker_spawned`, `worker_ready`) |
| `worker_checkpoint` | `worker_heartbeat` |
| `worker_finished` (= `result`, `worker_exit`) | `worker_stdout_line` (= `log`) |
| `worker_failed` (= `task_failed`) | `restart_scheduled` (draft) |
| `worker_killed` (draft; = `worker_exit` path) | `task_decomposed` (draft) |
| `merge_started` (= `merge_progress`) | `worker_error` (draft) |
| `merge_succeeded` (= `merge_committed`) | `parse_error` |
| `merge_failed` (draft) | `supervisor_stall` |
| `supervisor_started` (draft) | `drop` |
| `supervisor_shutdown` (draft) | `eof_seen` (draft) |
| `recovery_gap` (draft) | |

**Caller rule (arch §6.5):** a caller that needs proof a thing happened must wait for the matching **critical** event — `worker_finished` for completion, `worker_checkpoint` for resumability, `merge_succeeded` for the main ref. Heartbeats and tool/output streams cannot prove anything across a supervisor crash, by design.

---

## 6. Four-layer liveness model — event encoding

The architecture's liveness model (arch §5.3) makes EOF advisory, not terminal. This section defines how the event log *encodes* that model so that a replay, a subscriber, or the host can always tell which liveness signal a given record attests to.

### 6.1 The four layers (from arch §5.3, descending authority)

| # | Signal | Source | Authority | Latency |
|---|---|---|---|---|
| 1 | Process exit (`proc.wait()` returns) | kernel | Definitive | immediate |
| 2 | `{"type":"exit"}` message | worker | Definitive (matches #1 within 100 ms) | immediate |
| 3 | Heartbeat watchdog (default 90 s) | supervisor | Strong — kills on trip | up to `timeout_s` |
| 4 | EOF on stdout | kernel | **Advisory only** | immediate |

### 6.2 `liveness` encoding object

Worker-lifecycle events may carry an optional `liveness` object in `payload`. It makes the layer provenance explicit:

```jsonc
"liveness": {
  "process_alive": true,                      // layer 1 state, from proc.poll()/proc.wait()
  "ipc_ready": true,                          // ready message received (arch §7.2 readiness gate)
  "checkpoint_seen": "checkpoints/.../turn-003.json",  // last durable checkpoint (arch §6.4)
  "exit_message": { "reason": "done", "generation": 1 }, // layer 2: the authoritative exit msg (§5.2)
  "eof_seen": false,                          // layer 4 advisory
  "watchdog_armed": { "interval_s": 15, "timeout_s": 90 }  // layer 3 parameters (arch §5.2 init.heartbeat)
}
```

| Signal | Meaning | Carried by |
|---|---|---|
| `process_alive` | Layer 1 truth: is the process observed alive at record time. `false` **only** after `proc.wait()` returns (arch §5.3 rule 1). | `worker_started`, `worker_finished`, `worker_failed`, `worker_killed`, `eof_seen` |
| `ipc_ready` | Readiness, not aliveness: "process alive" ≠ "ready"; Python import takes time (arch §2.1, §7.2). | `worker_started` (`phase="ready"`) |
| `checkpoint_seen` | Durable resume point the worker has reached; the checkpoint store, not the log (arch §6.4). | `worker_checkpoint`, `worker_started` (resume), terminal events |
| `exit_message` | Layer 2: the worker's authoritative `exit` line with `reason ∈ {done, crash, cancelled, fatal}` (arch §5.2). Present on terminal events; absent = no `exit` was seen (supervisor cross-checks, §5.2). | `worker_finished`, `worker_failed`, `worker_killed` |
| `eof_seen` | Layer 4 advisory. **Never sufficient for death.** | `eof_seen` events, terminal events that followed an EOF escalation |

### 6.3 "EOF is never death" — the encoded escalation

Per arch §5.3, the reader's exact sequence, each step represented in the log:

```
EOF observed                 → eof_seen {proc_poll, grace_s: 5, pending_ping: true}
5 s grace timer, then poll:
  process exited             → worker_finished/worker_failed with exit_message (layers 1+2 agree)
  process still alive        → ping sent (supervisor→worker, arch §5.2)
     pong within 10 s        → worker alive; stream restored; no death recorded
     no pong in 10 s         → killpg the process group (spawned start_new_session=True, §7.2)
                              → worker_killed {reason: "ping_no_pong"}   (layer 3 escalation)
```

Heartbeats (layer 3) never fire during normal tool execution because tools emit heartbeats from inside their wrappers (arch §7.6); three missed beats (90 s) kill, recorded as `worker_killed {reason: "watchdog_timeout"}`. `supervisor_stall` (§3.16) suspends layer-3 enforcement during supervisor-induced stalls (arch §5.3), so the log never records a heartbeat timeout that was actually the supervisor's own drain failure.

### 6.4 Liveness ↔ state machine (arch §7.1)

| State transition | Event(s) |
|---|---|
| PENDING → SPAWNING | `submitted` |
| SPAWNING → RUNNING | `worker_started` `phase="ready"` (`ipc_ready=true`) |
| RUNNING → RUNNING | `worker_heartbeat` (loop), `worker_stdout_line`, `worker_error` |
| RUNNING → CHECKPOINTING→RUNNING | `worker_checkpoint` |
| RUNNING → DONE | `worker_finished` (result + `exit reason="done"`) |
| RUNNING → CRASHED | `eof_seen` + `worker_exit` absence, or `worker_killed` (watchdog/ready_timeout/ping_no_pong) |
| CRASHED → SPAWNING | `restart_scheduled` → `worker_started` (generation+1) |
| CRASHED → FAILED | `worker_failed` (budget exhausted, arch §7.4) |
| RUNNING → FAILED | `worker_failed` (non-recoverable error / timeout) |
| any → REJECTED | `worker_failed` with `failure_reason="rejected"` (reviewer verdict, arch §7.1) |

---

## 7. Schema versioning and migration policy

Pattern follows the in-repo dataset convention (`docs/module-template/dataset-format.md` §5) adapted to an **append-only** log: the log is never rewritten in place; versioning is a read/replay concern.

### 7.1 Version identity

- `events.db` carries the event schema version in `PRAGMA user_version` **and** in a `meta` table row (belt-and-braces; `PRAGMA user_version` is not transactional, so the `meta` row is the authoritative copy, written in the same transaction as the first insert).
- The JSON-Lines mirror carries `"schema_version": N` **on every record**, so mirror files are self-describing and migratable independently of the DB (mirror is off by default, arch §6.1).
- `supervisor_started.payload.event_schema_version` (§3.14) is the human-readable anchor in the event stream itself.

### 7.2 Migration policy

| Change kind | Example | Requires version bump? |
|---|---|---|
| Add a new `kind` | `eof_seen`, `merge_failed` | **No.** The `kind` set is open-ended (arch §3.6 kind list ends in `"..."`; §7 "every state transition emits an event"). Unknown kinds are passed through uninterpreted by old readers. |
| Add a payload key | `payload.tool` on a heartbeat | **No.** `payload` is a JSON dict; additive keys are backward-compatible. |
| Change a field's meaning | `payload.status` semantics | **Yes** — breaking. |
| Rename / remove a field | `payload.summary` → `payload.result_summary` | **Yes** — breaking. |
| Change the envelope | new required column | **Yes** — breaking. |

- Breaking changes bump `schema_version` by 1 and ship a **pure, tested migration** `migrate_event(record: dict, from_v: int, to_v: int) -> dict` (one function per step, mirroring `dataset-format.md` §5). Migrations run **lazily at read/replay time**; committed rows are never mutated (append-only invariant). The writer writes only current-version rows; old rows stay as archived bytes under the host's `session_dir` (arch §16.2).
- Migration tests must be committed in the same change as the bump; a reader that encounters `user_version` higher than it understands refuses to open rather than guessing.
- **Snapshots** (§4.1) are re-derived under the new schema on replay; a snapshot written under `schema_version=N` is read under `schema_version=N` and migrated on the fly.

### 7.3 Proposal for schema v1

Envelope as §2, SQLite DDL as arch §6.3 plus the two draft columns:

```sql
CREATE TABLE events (
    event_id      TEXT    NOT NULL UNIQUE,   -- draft: ULID (D2)
    seq           INTEGER PRIMARY KEY AUTOINCREMENT,  -- arch §6.3
    monotonic_ms  INTEGER NOT NULL,
    wall_ts       REAL    NOT NULL,
    kind          TEXT    NOT NULL,
    task_id       TEXT,
    worker_id     TEXT,                       -- draft: "task_id#generation" (D3)
    request_id    TEXT,
    generation    INTEGER,
    payload       TEXT    NOT NULL            -- redacted JSON
);
CREATE INDEX events_task_idx ON events(task_id, seq);
CREATE INDEX events_kind_idx ON events(kind, seq);
CREATE INDEX events_worker_idx ON events(worker_id, seq);   -- draft

CREATE TABLE snapshots (
    seq           INTEGER PRIMARY KEY,
    taken_at      REAL    NOT NULL,
    schema_version INTEGER NOT NULL,          -- draft: schema of state_summary
    state_summary TEXT    NOT NULL
);

CREATE TABLE meta (
    key   TEXT PRIMARY KEY,
    value TEXT NOT NULL
);
-- meta rows: ('event_schema_version','1'), ('session_id', 's-...'), ('created_at', '...')
```

---

## 8. Reconciliation notes — deviations from the architecture doc

Everything in this draft is consistent with the architecture's *behavioral* contract (§6 durability, §5.3 liveness, §7 lifecycle). Where the draft *names* or *adds* things the architecture does not name, it is flagged here so the final reconciliation is a diff against this list, not a re-review.

| # | Deviation | Detail | License in arch |
|---|---|---|---|
| D1 | **Kind vocabulary differs from arch §6.5 tables.** | The draft uses lifecycle-oriented names (`submitted`, `worker_started`, `merge_succeeded`, …); the arch's durable kind set is `task_assigned`, `worker_spawned`, `worker_ready`, `heartbeat`, `tool_event`, `checkpoint`, `result`, `worker_exit`, `task_failed`, `merge_progress`, `merge_committed`, `log`, … The mapping is in §3's catalog table; tier assignments for the mapped kinds are unchanged. | §3.6 kind list is explicitly open (`"..."`); §7 "every state transition emits an event." |
| D2 | **`event_id` (ULID) added to the envelope.** | Arch Event has no `id`; the durable identity is `seq`. The draft adds `event_id` purely as a correlation key across the DB, the optional JSON-Lines mirror, and subscriber streams (same shape as the protocol's `request_id`, arch §5). | No arch conflict — additive. |
| D3 | **`worker_id` added (derived `task_id#generation`).** | Arch identity is `task_id` + `generation` fencing token (§7.3); `pid` is explicitly rejected as identity (system-design §2.1). The draft materializes the compound as a column for indexes and joins. `pid` survives as an ephemeral observation in `worker_started.payload`. | Composes arch §7.3 without changing it. |
| D4 | **Clock fields consolidated.** | `ts` = arch's `wall_ts`/`timestamp`; `monotonic_ms` retained unchanged. events.py seed's `timestamp` maps to `ts`. | Naming only. |
| D5 | **`worker_started` folds arch's `worker_spawned` + `worker_ready`** via a `phase` discriminator. | Arch §6.5 treats both as separate non-critical kinds. The draft keeps one kind, two records. If the final architecture requires the two arch kind strings verbatim, split them back — the tier and payloads are unchanged. | Behavioral behavior preserved (§7.2 readiness gate). |
| D6 | **`worker_stdout_line` covers only non-protocol bytes.** | Arch reserves stdout for the protocol (§5.1 inv. 2) and forwards stderr to the log (§13); valid protocol lines become typed events and are never raw-echoed. The draft names that residual stream `worker_stdout_line` instead of arch's `log`. | §5.1 inv. 4 / §5.4c (`parse_error` tagging); §13. |
| D7 | **Draft-proposed kinds absent from arch §6.5:** `worker_killed`, `restart_scheduled`, `task_decomposed`, `merge_failed`, `supervisor_started`, `supervisor_shutdown`, `worker_error`, `eof_seen`, `recovery_gap`, `drop`, `parse_error`. | Each maps to a *behavior* the arch specifies but never names as an event kind (§5.2 error/exit messages, §5.3 escalation, §6.2 drop marker, §6.5 gap recovery, §7.4 restart, §7.7 shutdown, §4 Architectus). | §3.6 open kind set; §7 "every state transition emits an event." |
| D8 | **Tier assignments for draft kinds.** | `worker_killed`, `merge_failed`, `supervisor_started`, `supervisor_shutdown`, `recovery_gap` → **critical** (rare, terminal, or replay-semantics-changing). `restart_scheduled`, `task_decomposed`, `worker_error`, `eof_seen` → **non-critical** (reconstructible/advisory). The arch tier tables do not mention these kinds. | §6.5 tiering rule ("derived from kind") applies to whatever the final architecture names. |
| D9 | **Merge event naming.** | Draft `merge_started`/`merge_failed` are `merge_progress` phases; `merge_succeeded` = `merge_committed` (+ `merge_reconciled` as `payload.reconciled=true`). | §7.8 defines the behaviors; naming is draft-proposed. |
| D10 | **`liveness` payload object is a draft encoding.** | Arch §5.3 defines the four-layer model but no event-level encoding. The draft's `liveness` sub-object (`process_alive`, `ipc_ready`, `checkpoint_seen`, `exit_message`, `eof_seen`, `watchdog_armed`) is a proposal for how to expose it; it does not alter any arch behavior. | Encoding-only. |
| D11 | **DDL additions:** `event_id`, `worker_id`, `events_worker_idx`, `meta` table, `snapshots.schema_version`. | Supersets of arch §6.3. The `meta` table holds `event_schema_version` (§7). | Additive. |
| D12 | **`tool_event` is not a catalog member.** | The arch lists `tool_event` as a non-critical kind. This draft omits it from the 21-kind catalog because the task's requested catalog did not include it and the behavioral content (`tool`, `cmd`, `exit_code`, `duration_ms`) is already carried by `worker_stdout_line`/heartbeat `tool` and the checkpoint trail. **Reconciliation should decide whether to restore `tool_event` as a first-class kind** — the architecture's tier table explicitly lists it, so the default position is to keep it. | Flag for explicit reconciliation, not silently dropped. |
| D13 | **`merge_reconciled` naming.** | Arch §7.8 names a `merge_reconciled` event; the draft emits it as `merge_succeeded` with `payload.reconciled=true` (D9). Restore the arch kind string if the final architecture insists. | Naming only. |

---

## 9. Open questions for reconciliation

1. **`tool_event`** (D12): restore as its own non-critical kind, or fold into the output/checkpoint trail as the draft does?
2. **Kind strings:** adopt the draft's lifecycle vocabulary wholesale, or keep the arch §6.5 strings and treat the draft's names as display labels?
3. **`event_id`/`worker_id` columns** (D2/D3): keep in the durable DDL, or derive at read time?
4. **`restart_scheduled` tier** (D8): the draft says non-critical; if the host needs an auditable trail of restart-policy decisions across a crash, it should be promoted to critical (it is rare — volume is not an argument either way).
5. **`supervisor_shutdown` `reason="crash"` heuristic** (§3.15): acceptable to synthesize this on the next boot, or should unclean shutdown be recorded only via `recovery_gap`?

---

## Appendix A — events.py seed ↔ draft mapping (verification)

Every field in `src/cambium/events.py` has an exact draft home:

| events.py | draft |
|---|---|
| `Event.type` | `kind` (values preserved for the three seed types) |
| `Event.timestamp` | `ts` |
| `WorkerStarted.task_id` | `task_id` |
| `WorkerStarted.pid` | `worker_started` `phase="spawned"` `payload.pid` |
| `WorkerStarted.type="worker_started"` | `kind="worker_started"` |
| `WorkerFinished.task_id` | `task_id` |
| `WorkerFinished.status` | `worker_finished`/`worker_failed` `payload.status` |
| `WorkerFinished.exit_code` | terminal event `payload.exit_code` |
| `WorkerFinished.type="worker_finished"` | `kind="worker_finished"` |
| `LogEvent.level` | `worker_stdout_line` `payload.level` |
| `LogEvent.message` | `worker_stdout_line` `payload.line` |
| `LogEvent.type="log"` | `worker_stdout_line` / `worker_error` |

## Appendix B — catalog summary

21 kinds total: 15 lifecycle (submitted, worker_started, worker_heartbeat, worker_stdout_line, worker_checkpoint, worker_finished, worker_failed, worker_killed, restart_scheduled, task_decomposed, merge_started, merge_succeeded, merge_failed, supervisor_started, supervisor_shutdown) + 6 error/advisory (worker_error, parse_error, supervisor_stall, drop, recovery_gap, eof_seen).

Tier totals: 11 critical, 10 non-critical.
