# Nuntius IPC Protocol — Message Catalogue (DRAFT)

**STATUS: DRAFT — docs only, not normative.** This document is a design draft of the
M1 Nuntius wire contract. It is **not** authoritative for behavior: the authoritative
specification is `docs/architecture.md` §5 (IPC Protocol), which this draft must be
reviewed against before any implementation. Divergences from the architecture doc are
flagged inline and collected in §7.

**Version:** proto 1 (draft proposal, see §5)
**Date:** 2026-08-09
**Author:** research task `wt-ipc` (docs-only)
**Sources read (read-only):**
- `/home/ubuntu/cambium/docs/system-design.md` §4.1 (M1 Nuntius — v0.1, superseded) — cited as `system-design §4.1`.
- `docs/architecture.md` §5 (IPC: JSON-Lines on stdio, `request_id` RPC framing, authoritative `exit` message), plus §3.4 (`Result`), §6.4 (checkpoints), §7 (lifecycle), §16.4 (exit codes) — cited as `arch §5`, `arch §3.4`, etc.
- `/home/ubuntu/cambium/src/cambium/orchestrator.py`, `/home/ubuntu/cambium/src/cambium/events.py` (merged scaffold) — cited as `orchestrator.py`, `events.py`.

**Purpose:** a complete, implementation-ready message catalogue for the Nuntius module:
framing, per-direction message list, result-envelope schema, error taxonomy, versioning,
timeouts, and a flagged reconciliation against `arch §5`.

---

## 1. Framing

Transport is **NDJSON over stdio**: exactly one pair of pipes per worker (supervisor
writes worker stdin, worker writes supervisor stdout), UTF-8, one JSON object per
`\n`-terminated line. This is the v0.1 transport carried into v2 unchanged
(`system-design §4.1` "Newline-delimited JSON over stdin/stdout pipes"; `arch §5`
"JSON-Lines (one JSON object per `\n`-terminated line, UTF-8)").

### 1.1 Channel invariants (from `arch §5.1`)

1. **One writer, one reader per pipe.** Supervisor is the only writer of worker stdin;
   worker is the only writer of supervisor stdout. Worker blocks on `readline()`
   between messages; no polling.
2. **stdout is reserved for the protocol.** Worker debug output goes to **stderr**,
   which is unstructured and advisory only (`arch §5.1.5`; `system-design §4.1`
   "Worker stdout is never used for debug logging"). No `print()` in worker code or its
   dependencies; stdout is re-shim'd and `PYTHONUNBUFFERED=1` is set (`arch §5.1.2`,
   `§5.1.3`).
3. **No shared FDs.** Worker-spawned subprocesses use `pass_fds=()` and `close_fds=True`;
   workers are spawned with `start_new_session=True` so the whole subtree can be killed
   via process group (`arch §5.1.6`, `§7.2`).
4. **Blocking reads, buffered writes.** Supervisor writes to worker stdin through
   `asyncio` pipe buffering; the worker blocks on read (`arch §5.1.1`). Worker flushes
   stdout after every message (`flush=True`) — implied by `system-design §4.1` "Worker
   must flush stdout after each message".

### 1.2 Line discipline

- Each line must be a **single JSON object** (`json.loads`). A line that fails to parse
  is **logged with its line number and skipped**; the stream is not corrupted
  (`arch §5.1.4`). It is a protocol error, not a connection failure (see §4.1).
- **Empty and whitespace-only lines are skipped, not errors.** `json.loads` of a blank
  line is a parse failure, so receivers skip blank lines *before* parsing (this matches
  the v0.1 supervisor loop: `line = line.strip(); if not line: continue` in
  `system-design §4.1` M4 `_read_worker_output`).
- **Encoding:** UTF-8 both ways. Non-UTF-8 bytes in a line are a `bad_json` protocol
  error (decoding failure), logged and skipped.
- **Message boundaries are the newline only.** No length prefixes, no binary framing.
  This is a deliberate trade: rare torn lines are accepted and handled (below) in
  exchange for parser simplicity (`arch §5.4(c)` "Length-prefixed framing is not used").

### 1.3 Partial lines

- **Mid-stream:** a partial line is simply buffered by the reader until the terminating
  newline arrives. No action is needed; NDJSON framing is self-synchronizing at the
  newline.
- **At EOF:** a partial line (bytes after the last newline, or a line torn by SIGKILL
  mid-`write()`) is **discarded and logged** (`partial_line` protocol event). The
  receiver must not fabricate a message from it. Consequence risk (a torn `result`
  envelope lost) is mitigated by the architecture, not by framing: the worker persists
  its result to the checkpoint store **before** emitting the envelope, and the
  supervisor recovers from checkpoints (`arch §5.4(c)`).
- **EOF itself is not death** — see §4.4. EOF triggers the escalation sequence, not an
  immediate "worker dead" conclusion (`arch §5.3`).

### 1.4 Max line length policy

The architecture doc specifies no line-length cap; this is a **draft addition**
(flagged in §7).

- **Default cap: 1 MiB per line** (`MAX_LINE_BYTES = 1_048_576`). All lines in the
  normal catalogue are far below this: `summary` is capped at 2k chars (`arch §3.4`),
  `status`/`message` strings are bounded by sender-side truncation (see below). The cap
  exists to bound reader memory against a pathological or malicious peer.
- **On exceed:** the receiver stops buffering the line, continues consuming bytes until
  the next `\n` (resync), logs a `line_too_long` protocol event with the byte count, and
  discards the line. The stream remains usable.
- **Sender-side truncation (draft):** free-text fields that can grow are truncated at
  emit time to bound line size — `heartbeat.status` ≤ 200 chars, `progress.message` ≤
  200 chars, `progress.cmd` ≤ 512 chars. `result_envelope` tails are already capped (§3).

### 1.5 Trailing garbage

NDJSON requires **nothing after the final newline** except stream close (EOF).

- Trailing bytes that do not form a complete line are a *partial line* (handled in
  §1.3): discarded and logged at EOF.
- A trailing empty line (bare newline) is skipped (§1.2).
- Trailing bytes that do form complete lines are legitimate protocol messages — there is
  no concept of "extra" lines in NDJSON. The stream ends only when the pipe closes.

### 1.6 stderr

stderr is free-form advisory log output. The supervisor reads it opportunistically and
writes it to the event log as `kind="log"` events, level-tagged. **No protocol semantics
depend on stderr** (`arch §5.1.5`, `§13`). The `result_envelope.stderr_tail` (§3) is the
only place stderr content enters the protocol, and it is advisory.

---

## 2. Message catalogue

Two directions, three message classes:

| Class | Who sends | Semantics |
|---|---|---|
| **Request** | orchestrator → worker | Carries `request_id`; expects a response (ok/error envelope) |
| **Response** | worker → orchestrator | Correlates to a request via the echoed `request_id` |
| **Event** | worker → orchestrator | Fire-and-forget; no response; `request_id` only where it echoes `init` for correlation |

`request_id` is a ULID (monotonic-ish), assigned by the sender; every response echoes it
(`arch §5` "Every request carries a request_id (ULID, monotonic-ish). Every response
that completes a request echoes the same request_id"). Wire messages are flat JSON
objects (`type`, then `request_id` for requests/responses, then type-specific fields)
matching the flat style of `arch §5.2` examples.

Message count in this catalogue: **6 orchestrator→worker request types, 3 worker→
orchestrator response kinds, 6 worker→orchestrator event types — 15 message kinds**
(counting the optional retained `context` request; the abstract `ok`/`error` envelopes
cover the request types).

### 2.1 Envelope shapes

Request envelope (abstract; `request_id` required):

```jsonc
{"type":"<request_type>", "request_id":"01J…", /* type-specific fields */}
```

Response envelopes (worker → orchestrator):

```jsonc
{"type":"ok",    "request_id":"01J…", /* echoes the request's id; type-specific fields */}

{"type":"error", "request_id":"01J…|null", "error":{
    "code":"PROTO_…|WORKER_…", "message":"human-readable", "recoverable":bool}}
```

- `request_id` in an `error` is the echoed id when the request was parseable, else
  `null` (bad-JSON/oversized lines cannot be correlated; the supervisor logs them with a
  line number instead — `arch §5.1.4`).
- **Correlation rule:** the worker sends exactly one response per request (see the
  per-request tables; `run_task` completes with a `result_envelope` *event*, not a
  response). A response whose `request_id` the orchestrator does not recognize is a
  `PROTO_UNKNOWN_REQUEST_ID` protocol error (§4.1), logged and dropped.
- **No ACK loops.** Events never produce acknowledgements (matches the v0.1 anti-pattern
  "bidirectional agent-to-agent messaging degenerates into ACK loops",
  `system-design §2.2`).

### 2.2 Orchestrator → worker: requests

**`init`** — spawn-time configuration. Sent once, first, over stdin; the worker must
not process any other request before `init`.

| Field | Type | Notes / source |
|---|---|---|
| `type` | `"init"` | |
| `request_id` | ULID | echoed by `ready`, `result_envelope`, `fatal_error`, `exit_message` (`arch §5.2`) |
| `proto` | int | draft protocol version (§5) |
| `task_id` | str | stable task id, not PID (`system-design §2.1`; `arch §5.2`) |
| `generation` | int | fencing token, monotonically increasing per task (`arch §7.3`) |
| `worktree` | abs path | the worker's private worktree (`arch §5.2`) |
| `base_commit` | sha | worktree starts here (`arch §5.2`, `§7.5`) |
| `spec` | str | the task spec (`arch §5.2`) |
| `context` | str? | optional; draft folds the arch `context` message here (see below) |
| `max_turns` | int | ReAct loop bound (`arch §5.2`) |
| `tools` | list[str] | tool allowlist; a tool outside this set is `unknown_tool` (`arch §5.2`, `§7.4`) |
| `fanout_config` | object | `DiffundoConfig`, **no API keys** (`arch §5.2`, `§9.3`) |
| `provider_env_keys` | list[str] | env-var **names only**; values from inherited env (`arch §5.2`, `§12`) |
| `permissions` | object | e.g. `{"network":false,"shell":true}` (`arch §5.2`) |
| `heartbeat` | object | `{"interval_s":15,"timeout_s":90}` (`arch §5.2`, `§7.6`) |
| `budget` | object | `{"max_wall_s":1800,"max_restarts":10}` (`arch §5.2`, `§7.4`) |
| `resume_from_checkpoint` | str? | checkpoint `state_ref` re-injected on restart (`arch §6.4`) |

Response: `ready` (specialized `ok`, §2.3). **Deadline:** `ready_timeout`, default 60 s
(§6). Error paths: unsupported `proto` → `error` `recoverable:false` (§5); `unknown_tool`
in `tools` → `error` `recoverable:false` (`arch §7.4`).

**`context`** — additional context push, retained from `arch §5.2`
(`{"type":"context","request_id":"…","context":"…"}`). Best-effort; may be sent any time
after `init` and before the worker finishes. **No required response** — the arch schema
shows none, so the draft treats it as a request-with-request_id but no response deadline
(flagged in §7).

**`run_task`** — task dispatch. In the architecture, one worker process executes exactly
one task delivered entirely by `init` (`arch §14` defers a persistent pool to v2.1
"because it requires a different IPC model (multiple init messages per process)"). This
draft nevertheless defines `run_task` as the **task-enqueue request** so the catalogue
covers the persistent-worker shape the arch defers; for v2 the supervisor sends `init`
followed by `run_task` with the same spec body, and the worker responds `ok` once its
ReAct loop starts. This is a **draft extension**, flagged in §7.

| Field | Type |
|---|---|
| `type` | `"run_task"` |
| `request_id` | ULID |
| `task_id` | str |
| `spec` | str |
| `max_turns` | int? |
| `context` | str? |

Response: `ok` (loop started). **No RPC deadline** — the run is bounded by
`budget.max_wall_s` and the heartbeat watchdog (§6). Terminal outcome arrives as the
`result_envelope` event (§2.4).

**`check_health`** — liveness probe. Draft name for the arch `ping` request
(`{"type":"ping","request_id":"…"}`, `arch §5.2`); semantics identical (flagged in §7).
Used during the EOF-escalation sequence (§4.4) and on demand.

| Field | Type |
|---|---|
| `type` | `"check_health"` |
| `request_id` | ULID |

Response: `ok` (with `task_id`, `monotonic_ms` — the arch `pong` body, `arch §5.2`).
**Deadline:** 10 s pong deadline (§6). Silence → escalation sequence (§4.4).

**`cancel`** — cooperative cancellation of the current task.

| Field | Type |
|---|---|
| `type` | `"cancel"` |
| `request_id` | ULID |
| `reason` | str? (e.g. `"timeout"`, `"user"`, `"host"`) |

Response: `ok` (acknowledged). The worker then terminates, emitting `result_envelope`
(status `cancelled`) and/or `exit_message` (`reason:"cancelled"`) (§2.4, §4.4). **No
strict RPC deadline**: the orchestrator waits `graceful_s` (default 10 s) before SIGTERM,
then `term_grace_s` (default 5 s) before SIGKILL, on the process group (`arch §7.7`).

**`shutdown`** — graceful worker termination. **Draft extension**: the arch has no wire
shutdown message — the supervisor terminates via process-group signals (`arch §7.7`).
Draft retains it as the cooperative path; on receipt the worker must finish its current
step and exit with `exit_message` `reason:"cancelled"` (flagged in §7).

| Field | Type |
|---|---|
| `type` | `"shutdown"` |
| `request_id` | ULID |
| `reason` | str? |

Response: `ok`. Deadline: `graceful_s` (10 s) before SIGTERM escalation (`arch §7.7`).

### 2.3 Worker → orchestrator: responses

**`ready`** — the `ok` specialization for `init` (arch-listed message, `arch §5.2`):

```jsonc
{"type":"ready","request_id":"01J…","task_id":"wt-abc-001","pid":12345,
 "generation":3,"proto":1,"monotonic_ms":…}
```

`generation` confirms the worker accepted this fencing generation (`arch §7.3`); `proto`
echoes the negotiated protocol version (§5). The orchestrator must not send further
requests until `ready` (`arch §7.2` "waits for ready before considering the worker
RUNNING").

**`ok`** — generic success response for `run_task`, `check_health`, `cancel`, `shutdown`
(abstract shape in §2.1; body is request-specific).

**`error`** — generic failure response (shape in §2.1; codes in §4). Sender-side protocol
errors from the worker (unparseable request, unknown request type, out-of-order) use
`request_id:null` unless the request was parseable.

### 2.4 Worker → orchestrator: events (fire-and-forget)

All events carry `task_id`; `heartbeat`, `progress`, `checkpoint` additionally echo
`generation` (`arch §7.3` requires generation on heartbeat/checkpoint; the draft extends
it to `progress`). `result_envelope`, `fatal_error`, and `exit_message` echo the `init`
`request_id` (as in `arch §5.2`). Events are never acknowledged.

| Event | Purpose | Key fields | Source |
|---|---|---|---|
| `heartbeat` | Liveness: "I am alive and working" | `turn`, `tool` (currently-running tool or null), `status` (≤200 chars), `monotonic_ms` | `arch §5.2`, `§7.6` |
| `progress` | Work-in-progress detail (draft generalization of arch `tool_event`) | `turn`, `phase` (`"tool"` \| `"reasoning"` \| `"llm"`), `tool`/`cmd`/`exit_code`/`duration_ms` when `phase=="tool"` | draft; arch `tool_event` `arch §5.2` |
| `checkpoint` | Durable resume point | `turn`, `state_ref` (atomic write), `commits_so_far` | `arch §5.2`, `§6.4` |
| `result_envelope` | Terminal outcome | full schema in §3 | arch `result` `arch §5.2` |
| `fatal_error` | Terminal error | `error_type`, `message`, `partial_commits`, `recoverable` | arch `error` `arch §5.2` |
| `exit_message` | **The authoritative death signal — the ONLY death signal per the liveness model** | `reason` ∈ {`done`,`crash`,`cancelled`,`fatal`}, `monotonic_ms` | `arch §5.2`, `§5.3` |

Notes on the event set:

- **`progress` subsumes `tool_event`** (draft reconciliation, flagged in §7): a
  tool-level `tool_event` (`arch §5.2`) is a `progress` event with `phase:"tool"` and the
  `tool`/`cmd`/`exit_code`/`duration_ms` fields populated. `cmd` is truncated to ≤512
  chars at emit.
- **`exit_message` is mandatory.** It is the final line before process exit. A worker
  that exits without emitting `exit_message` is treated as crashed, **even if
  `result_envelope` was already sent** — the supervisor cross-checks (`arch §5.2`,
  `§5.3`).
- **`fatal_error` covers the arch `error` message**, including `recoverable`. The draft
  names it `fatal_error` because it always precedes `exit_message` (`reason:"fatal"` for
  `recoverable:false`, `reason:"crash"` for `recoverable:true`).
- **Generation-mismatch termination** (`arch §7.3`) is `exit_message` `reason:"fatal"`
  directly, optionally preceded by `fatal_error` with `error_type:"generation_mismatch"`.

---

## 3. Result envelope schema

The `result_envelope` event is the worker's terminal report. It maps onto the public
`Result` dataclass (`arch §3.4`) and the scaffold's `WorkerFinished` (`events.py`); the
supervisor enriches it with session-level fields when writing `result.json` (§8.2).

```jsonc
{
  "type": "result_envelope",
  "request_id": "01J…",              // echoes init
  "task_id": "wt-abc-001",
  "generation": 3,
  "status": "succeeded",             // succeeded | failed | timeout | cancelled
  "exit_code": 0,                    // arch §16.4 mapping: 0/1/3/4 (see §4.3)
  "commits": ["a1b2c3d"],
  "files_changed": ["src/dry_run.rs"],
  "diff": "diff --git a/src/dry_run.rs b/src/dry_run.rs …",   // draft, capped 64 KiB
  "stdout_tail": "",                 // draft, capped (see below)
  "stderr_tail": "WARNING …",        // draft, capped (see below)
  "summary": "Removed 3 global statics.",    // ≤2k chars (arch §3.4)
  "metrics": {
    "metric_score": 0.84,
    "metric_breakdown": {"tests":1.0,"spec_adherence":0.9,"diff_quality":0.7,"canaries":1.0}
  },
  "failure_reason": null,            // populated when status != succeeded (arch §3.4)
  "started_at": 1786147200.0,
  "ended_at": 1786147800.0
}
```

| Field | Type | Notes |
|---|---|---|
| `status` | enum | `succeeded` \| `failed` \| `timeout` \| `cancelled`. Draft names; **flag:** arch `Result.status` uses `done` and adds `rejected` (§7). `timeout` is normally supervisor-assigned (§2.4/§6), not self-reported. |
| `exit_code` | int | Task-level code following `arch §16.4`: 0 done, 1 failed, 3 timeout, 4 cancelled (2 `rejected` and >100 supervisor crash are not worker outcomes). |
| `commits` | list[str] | SHAs produced (`arch §3.4` `Result.commits`). |
| `files_changed` | list[str] | Paths changed (`arch §3.4` `Result.files_changed`). |
| `diff` | str | Draft addition — `arch §3.4` `Result` has no diff field, but the `ResultEvaluator` consumes a `diff` (`arch §10`, `§17.3`). Draft: `git diff base_commit..worktree`, capped at 64 KiB; empty when no work was produced. |
| `stdout_tail` | str | Draft addition. Last ≤200 lines of tool output the worker chooses to attach (worker stdout itself is protocol-only, `arch §5.1.2`). Optional; empty when unset. |
| `stderr_tail` | str | Draft addition. Last ≤200 lines of the worker's own stderr record; the supervisor independently retains its own capture as `kind="log"` events (`arch §5.1.5`). |
| `summary` | str | Worker-authored, ≤2k chars (`arch §3.4`). |
| `metrics` | object | Draft nests the arch-flat `metric_score`/`metric_breakdown` (`arch §5.2` `result`) under a `metrics` object; the supervisor flattens when writing `Result`. Multi-signal metric per `arch §10`. |
| `failure_reason` | str? | Set when `status != "succeeded"` (`arch §3.4`). |
| `started_at` / `ended_at` | float | Wall-clock bounds (`arch §3.4`); `ended_at` doubles as the envelope timestamp. |

Supervisor-side enrichment (not on the wire): `session_id`, `event_log_ref` — `Result`
fields (`arch §3.4`) assembled by Custos when writing
`${session_dir}/cambium/result.json` (`arch §16.2`).

---

## 4. Error taxonomy

### 4.1 Protocol errors (wire-level; detectable by either side)

| Code | Condition | Handling | Source |
|---|---|---|---|
| `PROTO_BAD_JSON` | Line is not a valid JSON object, or not UTF-8 | Log with **line number**, skip line; stream unaffected | `arch §5.1.4` |
| `PROTO_LINE_TOO_LONG` | Line exceeds `MAX_LINE_BYTES` (1 MiB, draft) | Log, resync to next `\n`, discard | draft §1.4 |
| `PROTO_UNKNOWN_TYPE` | `type` missing or not in the catalogue | Receiver logs and ignores; a worker that received a parseable request responds `error` (`request_id` echoed) | draft; consistent with `arch §5.1.4` skip-and-continue |
| `PROTO_MISSING_REQUEST_ID` | Request/response without `request_id` | Log and drop; a worker may ignore the request | draft |
| `PROTO_OUT_OF_ORDER` | Message violates the state machine (result before `ready`; `run_task` before `init`; duplicate `init`; anything after `exit_message`) | Log and ignore; repeated violations on a worker → treat as crash (kill + restart) | draft; state machine is `arch §7.1` |
| `PROTO_UNKNOWN_REQUEST_ID` | Response/event correlates to a request the supervisor never sent | Log and drop | draft; correlation rule §2.1 |

Protocol errors never kill the supervisor and never corrupt the stream
(`arch §5.1.4`). They are recorded as event-log entries; the draft maps them to the
scaffold's `LogEvent(level="error")` (`events.py`) or a future `kind="parse_error"`
event (v0.1 used `log_parse_error`, `system-design §4.1` M4).

### 4.2 Worker errors (domain)

Sent as `fatal_error` (terminal) or `error` (response):

| Class | Example `error_type` | `recoverable` | Handling |
|---|---|---|---|
| Tool failure | `build_failure`, `test_failure`, `command_error` | `true` | Restart policy engages (burst cap, backoff, wall budget) — `arch §7.4` |
| Spec/config error | `unknown_tool`, `invalid_spec`, `tool_not_allowed` | `false` | **No retry**; task fails immediately — `arch §7.4` "Non-recoverable errors skip the restart budget and fail immediately" |
| Provider outage | `AllProvidersFailed` | `true` | Worker retries inside the tool boundary for `provider_patience_s` (180 s) before emitting; only then is it a worker failure — `arch §7.4` |
| Internal error | unhandled exception type | `false` | Worker emits `fatal_error`, then `exit_message` `reason:"fatal"`; stderr traceback captured as `kind="log"` |
| Fencing violation | `generation_mismatch` | `false` | `exit_message` `reason:"fatal"` — `arch §7.3` |

`partial_commits` accompanies `fatal_error` so the supervisor knows what survived
(`arch §5.2` `error.partial_commits`).

### 4.3 Exit codes and their meaning

Two distinct code spaces; do not conflate:

**Worker process exit code** (observed via `proc.wait()`): the wire contract is the
presence of `exit_message`, not the code (`arch §5.3` "matches #1 inside 100 ms"). Draft
convention: `0` = clean exit with `exit_message` emitted as the final line; non-zero or
signal-death (`returncode < 0`) = abnormal, with signal number available. The supervisor
must not rely on the code alone — see §4.4.

**Task/session exit codes** (host contract, `arch §16.4`, `arch §3.4`):

| Code | Meaning |
|---|---|
| 0 | done (draft: `succeeded`) |
| 1 | failed |
| 2 | rejected (reviewer verdict; not a worker outcome) |
| 3 | timeout |
| 4 | cancelled |
| >100 | supervisor crash |

The `result_envelope.exit_code` field (§3) uses this space. `rejected` (2) is assigned by
the orchestrator's `ResultEvaluator` after merge (`arch §7.1` REJECTED state), never by a
worker.

### 4.4 How the supervisor distinguishes crash vs clean exit

The liveness model is authoritative (`arch §5.3`, four layers in descending authority):
(1) process exit, (2) `exit_message`, (3) heartbeat watchdog, (4) EOF (advisory).

- **Clean exit:** `exit_message` received **and** `proc.wait()` agrees inside 100 ms.
  `reason:"done"` → task DONE, no restart. `reason:"cancelled"` → task CANCELLED, no
  restart. `reason:"fatal"` → task FAILED, no restart. `reason:"crash"` → restart policy
  engages (`arch §7.4`).
- **Crash:** process exited **without** `exit_message` — even if `result_envelope` was
  already sent (supervisor cross-checks, `arch §5.2`). Restart policy engages
  (`arch §7.4`); task FAILED once the absolute cap (10) or wall budget is exhausted.
- **Ambiguous (EOF but process alive):** EOF alone is **not** death. The supervisor
  schedules a 5 s grace timer, then `proc.poll()`. If still alive (e.g., a grandchild
  holds the pipe), it escalates with `check_health` (`ping`); no `pong` within 10 s →
  kill the **process group** (`arch §5.3`, `§5.4(a)`). Workers are spawned with
  `start_new_session=True` for exactly this (`arch §7.2`).
- **Watchdog kill:** 3 missed heartbeats (> `heartbeat.timeout_s`, default 90 s) → the
  supervisor kills the worker; the kill path also handles grandchild pipe-holders
  (`arch §5.3`, `§7.6`).
- **Supervisor-induced stalls** are flagged, not blamed on the worker: a 30 s drain
  deadline suspends heartbeat enforcement while the supervisor is stalled
  (`arch §5.3`).

---

## 5. Versioning

- **Draft addition:** a `proto` integer field (draft value `1`), carried on `init`
  (supervisor→worker) and echoed on `ready` (worker→supervisor). The architecture doc
  defines no version field in §5.2 (flagged in §7).
- **Semantics:** `proto` bumps on breaking changes — message removed/renamed, a required
  field added, framing changed. Additive changes (new optional field, new event type)
  are backward-compatible within a `proto`.
- **Negotiation at init:** the supervisor sends `init` with its `proto`. If the worker
  understands it, it echoes the same `proto` in `ready` — negotiation is then complete,
  and the worker must not emit protocol messages before that. If the worker cannot
  support the `proto`, it responds `error` with `recoverable:false` and
  `error_type:"proto_unsupported"`, then exits; the supervisor marks the task FAILED
  (per the `arch §7.4` non-recoverable rule). Because both ends ship in the same harness
  release today, this path is defensive.
- **Future:** a minor/major split or feature-negotiation handshake is deferred; the
  draft deliberately keeps a single integer for v1.

---

## 6. Timeouts

Which messages require response deadlines, and the liveness timers.

| Timer | Value | Applies to | Source |
|---|---|---|---|
| `ready_timeout` | 60 s (default) | `init` → `ready` response | `arch §7.2` |
| `pong_deadline` | 10 s | `check_health` (`ping`) → `ok` (`pong`) response | `arch §5.3` |
| `heartbeat.interval_s` | 15 s (default, per task in `init`) | worker → `heartbeat` events | `arch §5.2`, `§7.6` |
| `heartbeat.timeout_s` | 90 s (default; 3 missed beats) | watchdog kill | `arch §5.2`, `§7.6` |
| `budget.max_wall_s` | 1800 s (default, per task in `init`) | `run_task` wall clock | `arch §5.2`, `§7.4` |
| `eof_grace_s` | 5 s | EOF → `proc.poll()` before escalation | `arch §5.3` |
| `drain_deadline_s` | 30 s | supervisor read-loop stall (suspends heartbeat enforcement) | `arch §5.3` |
| `graceful_s` / `term_grace_s` | 10 s / 5 s | `cancel`/`shutdown` → SIGTERM → SIGKILL on process group | `arch §7.7` |

Response-deadline summary:

- **`init` requires a response deadline** (`ready_timeout`). On expiry the worker is
  killed and the restart policy engages (`arch §7.2`).
- **`check_health` requires a response deadline** (`pong_deadline`). Silence feeds the
  §4.4 escalation path.
- **`cancel` and `shutdown` have no strict RPC deadline**; they are bounded by the
  `graceful_s`/`term_grace_s` escalation ladder (`arch §7.7`).
- **`run_task` and `context` have no response deadline**; the run is bounded by
  `budget.max_wall_s` and the heartbeat watchdog.
- **Events have no deadlines** — fire-and-forget by definition. Liveness is enforced by
  the heartbeat watchdog, not by per-event responses.
- Timer values are sent to the worker in `init.heartbeat`/`init.budget` so both sides
  agree (`arch §5.2`). The architecture applies full jitter to the watchdog interval,
  the fsync timer, and heartbeat emission (`arch §7.4`); the draft inherits that.

---

## 7. Reconciliation vs architecture doc (flagged)

This draft is a **proposal**. Where it names or structures messages differently from
`arch §5.2` (the normative spec), the divergence is flagged here. Until a review
resolves each row, the architecture doc wins.

| # | Draft | Architecture doc (`arch §5.2` unless noted) | Flag |
|---|---|---|---|
| 1 | `run_task` request | No wire dispatch message; one task per process, delivered entirely by `init`. Persistent pool deferred to v2.1 ("requires a different IPC model", `arch §14`). | Draft extension — keep only if/when the persistent-worker IPC shape lands; harmless for v2 (send `init`+`run_task`). |
| 2 | `shutdown` request | No wire shutdown message; supervisor terminates via process-group SIGTERM/SIGKILL (`arch §7.7`). | Draft extension — cooperative path is optional; signals remain the enforcement. |
| 3 | `check_health` request | `ping` request (`arch §5.2`, `§5.3`). | Draft name only; wire semantics identical. Suggest adopting `ping`/`pong` verbatim or resolving names in review. |
| 4 | Generic `ok`/`error` response envelopes | No generic envelope; each response message carries the echoed `request_id` directly (`arch §5.2` `ready`/`result`/`error`/`exit`). | Draft generalization. `ready` is kept as the concrete `init` response so the arch's message survives; `ok`/`error` are syntactic sugar over the same correlation rule. |
| 5 | `progress` event | `tool_event` message (`arch §5.2`). | Draft generalization (tool_event = progress with `phase:"tool"`). Retain `tool_event` as an alias in v1 to avoid breaking the event-log contract (`arch §3.6` lists `tool_event` as an event kind). |
| 6 | `fatal_error` event | `error` message with `recoverable` flag (`arch §5.2`, `§7.4`). | Draft rename emphasizing terminality; must keep the `recoverable` flag semantics. |
| 7 | `exit_message` event | `exit` message, `reason` ∈ {done,crash,cancelled,fatal} (`arch §5.2`, `§5.3`). | Draft name only; the message itself is authoritative and identical. |
| 8 | `result_envelope.status` ∈ {succeeded,failed,timeout,cancelled} | `Result.status` ∈ {done,failed,rejected,timeout,cancelled} (`arch §3.4`, `§16.4`). | Draft maps `succeeded`→`done`. `rejected` (2) is an orchestrator/merge outcome, never a worker status — the draft intentionally omits it from the wire envelope; `Result` still carries it. |
| 9 | `proto` version field on `init`/`ready` | No version field defined in `arch §5.2`. | Draft addition — required by the versioning requirement; low risk since both ends ship together. |
| 10 | `MAX_LINE_BYTES` = 1 MiB; sender-side truncation caps | No line-length cap specified; torn lines accepted for parser simplicity (`arch §5.4(c)`). | Draft addition — the 1 MiB cap does not weaken the torn-line handling; a torn line over the cap is still dropped and resynced. |
| 11 | `result_envelope` gains `diff`, `stdout_tail`, `stderr_tail`, nested `metrics` | arch `result` carries `status`, `commits`, `files_changed`, `summary`, `metric_score`, `metric_breakdown` (`arch §5.2`); `Result` has no diff/tails (`arch §3.4`). | Draft additions; `diff` is justified by `ResultEvaluator` input (`arch §10`). Tails are advisory diagnostics. Flatten `metrics` when writing `Result`. |
| 12 | `context` folded into `init` (optional) and kept as separate request | Separate `context` request (`arch §5.2`). | Draft keeps both shapes; no behavioral change. |
| 13 | Error taxonomy codes (`PROTO_*`) | `arch §5.1.4` covers parse failures only; other wire errors are unspecified. | Draft fills the gap consistently with skip-and-continue. |
| 14 | `stdout_tail`/`stderr_tail` in result envelope | Worker stdout reserved for protocol; stderr advisory (`arch §5.1.2`, `§5.1.5`). | Draft additions do not write to stdout; they summarize already-captured content. |

Not flagged (aligned): framing/NDJSON (`§5`), channel invariants (`§5.1`), `init` field
set (`§5.2`), `ready` as the handshake response (`§7.2`), heartbeat semantics (`§7.6`),
fencing (`§7.3`), restart policy (`§7.4`), checkpoint `state_ref` protocol (`§6.4`),
`exit_message` authority (`§5.3`), task exit codes (`§16.4`).

---

## 8. Consistency with the merged scaffold

Verification rule: the catalogue must not contradict `events.py` or the `orchestrator.py`
loop as merged. Read them; the following mapping is what the supervisor (Custos) will
produce when it implements the Nuntius wire loop.

### 8.1 Wire message → `events.py` event mapping

| Wire message | Scaffold event (`events.py`) | Notes |
|---|---|---|
| `ready` (init response) | `WorkerStarted(task_id, pid)` | `pid` and `task_id` come straight from the `ready` body; `type="worker_started"`. |
| `result_envelope` | `WorkerFinished(task_id, status, exit_code)` | `status` (any string is legal — the draft's `succeeded`/`failed`/`timeout`/`cancelled` assign cleanly) and `exit_code` map 1:1. |
| `heartbeat`, `progress`, `checkpoint`, `exit_message`, `fatal_error` | no dedicated type yet | The scaffold comment says these four types "are the seed; the contract will grow with the architecture doc" (`events.py` docstring). `Event.kind` strings in `arch §3.6` (`heartbeat`, `tool_event`, `checkpoint`, `worker_exit`, `result`, …) are the intended growth. `LogEvent` is the catch-all for protocol errors (`level="error"`) and stderr (`level` parsed from prefixes, `arch §13`). |
| protocol errors (§4.1) | `LogEvent(level="error")` | advisory; never affects control flow (`arch §5.1.4`). |

The scaffold `Event` base (`type`, `timestamp`) and `WorkerStarted`/`WorkerFinished`
carry the same correlation fields the wire does (`task_id`; `request_id`/`generation`
can be added as fields later, matching `arch §3.6` `Event` schema).

### 8.2 Relationship to `orchestrator.py` (the scaffold loop)

- `src/cambium/orchestrator.py` is an **orchestration-layer placeholder** (`Architectus`
  territory): `submit(task_spec)` enqueues and `run()` drains, emitting
  `WorkerStarted`/`WorkerFinished` to a caller callback. It contains **no wire code**.
- Per the layering (`arch §2`, `§4`), the wire endpoint is **Nuntius + Custos** in the
  Deterministic Layer: Custos spawns the worker, sends `init` (the v0.1 supervisor
  already does exactly this: `json.dumps({"type": "init", **spec})` in
  `system-design §4.1` M4 `_spawn_worker`), and reads the worker's stdout line loop
  (`system-design §4.1` M4 `_read_worker_output`). The draft's messages are the bytes on
  that pipe; the scaffold's `WorkerStarted`/`WorkerFinished` events are emitted from the
  `ready` and `result_envelope`/`exit_message` outcomes respectively.
- The scaffold's `run()` loop is "placeholder lifecycle only" (`orchestrator.py`
  docstring): it does not yet gate on `ready`, apply restart policy, or read the wire.
  Nothing in this draft conflicts with its public surface; the wire loop lands in Custos.

### 8.3 Example session trace

```jsonl
// supervisor → worker (stdin)
{"type":"init","request_id":"01JWCKQN2E1Z9K5Y3M8P7R4T1","task_id":"wt-abc-001",
 "proto":1,"generation":3,"worktree":"/abs/worktrees/wt-abc-001","base_commit":"a1b2c3d",
 "spec":"Refactor dry_run.rs to remove global state","max_turns":20,
 "tools":["read_file","write_file","edit_file","run_shell","git_op","grep_code"],
 "fanout_config":{...},"provider_env_keys":["DEEPCODE_API_KEY"],
 "permissions":{"network":false,"shell":true},
 "heartbeat":{"interval_s":15,"timeout_s":90},
 "budget":{"max_wall_s":1800,"max_restarts":10}}

// worker → supervisor (stdout)
{"type":"ready","request_id":"01JWCKQN2E1Z9K5Y3M8P7R4T1","task_id":"wt-abc-001",
 "pid":12345,"generation":3,"proto":1,"monotonic_ms":...}

{"type":"heartbeat","task_id":"wt-abc-001","generation":3,"turn":1,
 "tool":"grep_code","status":"locating global statics","monotonic_ms":...}

{"type":"progress","task_id":"wt-abc-001","generation":3,"turn":1,"phase":"tool",
 "tool":"grep_code","cmd":"rg 'static' src/","exit_code":0,"duration_ms":1200}

{"type":"checkpoint","task_id":"wt-abc-001","generation":3,"turn":3,
 "state_ref":".../checkpoints/wt-abc-001/turn-003.json","commits_so_far":["a1b2c3d"]}

{"type":"result_envelope","request_id":"01JWCKQN2E1Z9K5Y3M8P7R4T1","task_id":"wt-abc-001",
 "generation":3,"status":"succeeded","exit_code":0,"commits":["a1b2c3d"],
 "files_changed":["src/dry_run.rs"],"diff":"...","summary":"Removed 3 global statics.",
 "metrics":{"metric_score":0.84,"metric_breakdown":{...}},
 "failure_reason":null,"started_at":...,"ended_at":...}

{"type":"exit_message","request_id":"01JWCKQN2E1Z9K5Y3M8P7R4T1","task_id":"wt-abc-001",
 "generation":3,"reason":"done","monotonic_ms":...}
// EOF; proc.wait() == 0 within 100 ms → clean exit, task DONE (arch §5.3)
```

Crash counter-example: the worker dies (SIGKILL) between the two lines above —
`exit_message` never arrives → supervisor marks CRASHED regardless of the
`result_envelope` already read (`arch §5.2` "treated as having crashed — even if result
was already sent").

---

## 9. Open questions for review (draft-only)

1. Adopt `ping`/`pong` or keep `check_health` (Reconciliation #3)? Recommend `ping`/
   `pong` verbatim for zero divergence.
2. Keep the generic `ok`/`error` envelopes, or go fully per-type responses (#4)?
3. Is `run_task` worth keeping in v1 (#1), or should v2 ship `init`-only dispatch?
4. Are the draft caps sane: `MAX_LINE_BYTES` 1 MiB, `diff` 64 KiB, `status` 200 chars,
   `cmd` 512 chars, tails 200 lines (#10, #11)?
5. Should `proto` be a string like `"1"` for JSON-compat, or stay int (#9)?




