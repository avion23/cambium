# Nuntius IPC Protocol — Message Catalogue (DRAFT)

**Historical snapshot — proto 1, 2026-08-09.** Docs-only draft by research task
`wt-ipc` (M1); sources included superseded `system-design.md` §4.1 and the merged
`orchestrator.py`/`events.py` scaffold. It is not normative; architecture §5 wins.
Current authority is [`docs/architecture/architecture.md`](../architecture/architecture.md),
source/tests, and [`v2-1-status.md`](v2-1-status.md).

**Current note (not retroactive):** active `supervisor.run_plan` is flat;
`task_decomposed` remains unsupported; provider cascade is source-defined and honors
`Retry-After`; worker stdout/event admission is bounded; no per-worker OS sandbox or
approval; DLQ and eval cache are absent.

The explicit-tree boundary is historical design intent: the harness validates a static
DAG and admits only dependency-ready tasks; each admitted child receives a fresh bounded
context, and only the strict upward result envelope is returned. The wire does not model
implicit single-context recursion. Any prefix-cache claim is a measurement target, not a
discount or guarantee.

## 1. Framing and invariants

NDJSON over UTF-8 stdio: one JSON object per newline, one writer/reader per pipe,
supervisor→worker stdin and worker→supervisor stdout. stdout is protocol-only,
diagnostics go to advisory stderr; workers flush each message, use
`PYTHONUNBUFFERED=1`, `start_new_session=True`, `pass_fds=()`, and `close_fds=True`.

Blank lines are skipped; invalid UTF-8/JSON is logged with line number and skipped;
there is no length prefix. A partial line is buffered mid-stream, discarded/logged as
`partial_line` at EOF, and never fabricated. EOF is advisory, not death; checkpoint-first
result persistence recovers a torn `result` line. The draft adds `MAX_LINE_BYTES=1_048_576`:
over-limit lines resync at the next newline, log `line_too_long`, and are discarded;
sender caps are heartbeat status ≤200 chars, progress message ≤200, command ≤512.
stderr has no protocol semantics and is mirrored as advisory `worker_stdout_line`.

## 2. Message classes and envelopes

Requests carry sender-assigned ULID `request_id`; responses echo exactly one ID. Events
are fire-and-forget. Draft count: six requests (`init`, `context`, `run_task`,
`check_health`, `cancel`, `shutdown`), specialized `ready` plus generic `ok`/`error`,
and six events (`heartbeat`, `progress`, `checkpoint`, `result_envelope`, `fatal_error`,
`exit_message`) — 15 kinds. There are no ACK loops.

```jsonc
{"type":"<request>","request_id":"01J…",…}
{"type":"ok","request_id":"01J…",…}
{"type":"error","request_id":"01J…|null",
 "error":{"code":"PROTO_…|WORKER_…","message":"…","recoverable":false}}
```

Unparseable/oversized input has `request_id:null`; unknown response IDs are
`PROTO_UNKNOWN_REQUEST_ID`, logged/dropped.

### 2.1 Requests (orchestrator → worker)

**`init`** is first and configures one task:

| Field | Type / definition |
|---|---|
| `type` | `"init"` |
| `request_id` | ULID; echoed by ready/result/error; exit echo is draft extension (#7). |
| `proto` | int draft version (1). |
| `task_id` | stable string, never PID. |
| `generation` | monotonic fencing token. |
| `worktree`, `base_commit` | absolute worktree path and starting SHA. |
| `spec`, `context?` | task text and optional context. |
| `max_turns` | ReAct loop bound. |
| `tools` | allowlist; unknown tool is nonrecoverable. |
| `fanout_config` | Diffundo config, no API keys. |
| `provider_env_keys` | names only (values are environment-bound). |
| `permissions` | e.g. `{"network":false,"shell":true}`. |
| `heartbeat` | `{"interval_s":15,"timeout_s":90}`. |
| `budget` | `{"max_wall_s":1800,"max_restarts":10}`. |
| `resume_from_checkpoint?` | checkpoint `state_ref` on restart. |

Response is `ready`; default `ready_timeout=60 s`. Unsupported proto or unknown tool is
`error`, `recoverable:false`.

**`context`** (`request_id`, `context`) is a best-effort push with no required response.
**`run_task`** (`request_id`, `task_id`, `spec`, `max_turns?`, `context?`) is a draft
persistent-worker extension; `ok` acknowledges start, terminal state is an event.
**`check_health`** (`request_id`) is the draft name for arch `ping`; `ok` carries
`task_id`, `monotonic_ms` (pong), deadline 10 s. **`cancel`** (`request_id`, `reason?`)
returns `ok`, then cancelled result/exit; **`shutdown`** (`request_id`, `reason?`) is a
cooperative draft extension, with signal escalation still authoritative.

### 2.2 Responses and events (worker → orchestrator)

`ready` echoes `task_id`, `pid`, `generation`, `proto`, `monotonic_ms`; no further request
is sent before it. Generic `ok` covers run/health/cancel/shutdown; `error` uses §4 codes.

| Event | Fields | Semantics |
|---|---|---|
| `heartbeat` | `task_id`, `generation`, `turn`, `tool|null`, `status≤200`, `monotonic_ms` | progress/liveness, not death proof. |
| `progress` | `task_id`, `generation`, `turn`, `phase=tool|reasoning|llm`, `message≤200`, optional `tool/cmd≤512/exit_code/duration_ms` | draft generalization of arch `tool_event`; D6 says valid line is never raw output. |
| `checkpoint` | `task_id`, `generation`, `turn`, atomic `state_ref`, `commits_so_far` | durable resume point. |
| `result_envelope` | request/task/generation plus §3 | terminal report. |
| `fatal_error` | request/task/generation, `error_type`, `message`, `partial_commits`, `recoverable` | arch `error`; followed by exit. |
| `exit_message` | task/generation, `reason=done|crash|cancelled|fatal`, `monotonic_ms` | mandatory authoritative terminal signal; request echo is draft extension. |

`progress(phase="tool")` is deferred `tool_event` (events D12); reconstruct from
heartbeat/checkpoint until reconciled. Missing exit means CRASHED even if result arrived;
generation mismatch emits fatal exit (optionally `fatal_error`).

## 3. Result envelope

```jsonc
{"type":"result_envelope","request_id":"01J…","task_id":"wt-abc-001","generation":3,
 "status":"succeeded","exit_code":0,"commits":["a1b2c3d"],
 "files_changed":["src/dry_run.rs"],"diff":"…","stdout_tail":"","stderr_tail":"…",
 "summary":"Removed 3 global statics.",
 "metrics":{"metric_score":0.84,"metric_breakdown":{"tests":1.0,"spec_adherence":0.9,"diff_quality":0.7,"canaries":1.0}},
 "failure_reason":null,"started_at":1786147200.0,"ended_at":1786147800.0}
```

| Field | Definition |
|---|---|
| `status` | `succeeded|failed|timeout|cancelled` draft vocabulary; maps succeeded→arch `done`; `rejected` is supervisor-only. |
| `exit_code` | 0 done, 1 failed, 3 timeout, 4 cancelled; 2 rejected and >100 supervisor crash are not worker outcomes. |
| `commits`, `files_changed` | Produced SHAs and changed paths. |
| `diff` | Draft `git diff base..worktree`, capped 64 KiB; evaluator input. |
| `stdout_tail`, `stderr_tail` | Optional ≤200-line diagnostics; stdout protocol remains clean. |
| `summary` | Worker-authored, ≤2k chars. |
| `metrics` | Draft nesting of `metric_score`/`metric_breakdown`; flatten when writing `Result`. |
| `failure_reason` | Set when status is not succeeded. |
| `started_at`, `ended_at` | Wall-clock bounds; ended is envelope timestamp. |

Terminal mapping (events seam 2): succeeded→`worker_finished`/done; failed→`worker_failed`;
timeout→failed/watchdog kill; cancelled→`worker_killed`. Custos adds `session_id` and
`event_log_ref` when writing `result.json`.

## 4. Error and liveness policy

### 4.1 Protocol errors

| Code | Condition | Handling |
|---|---|---|
| `PROTO_BAD_JSON` | invalid UTF-8/object | log line number, skip |
| `PROTO_LINE_TOO_LONG` | >1 MiB | log, resync, discard |
| `PROTO_UNKNOWN_TYPE` | missing/unknown type | log/ignore; parseable request gets error |
| `PROTO_MISSING_REQUEST_ID` | missing correlation | log/drop |
| `PROTO_OUT_OF_ORDER` | before ready, before init, duplicate init, after exit | log/ignore; repeated violation may kill/restart |
| `PROTO_UNKNOWN_REQUEST_ID` | unsent correlation | log/drop |

### 4.2 Worker errors

`build_failure`, `test_failure`, `command_error` are recoverable; `unknown_tool`,
`invalid_spec`, `tool_not_allowed`, internal exceptions, and `generation_mismatch` are
nonrecoverable. `AllProvidersFailed` is recoverable after worker patience (180 s), not a
worker restart. `partial_commits` is retained in fatal errors.

### 4.3 Exit code/liveness distinction

Process code alone is insufficient. Task/session codes are 0 done, 1 failed, 2 rejected,
3 timeout, 4 cancelled, >100 supervisor crash. Clean requires exit message plus
`proc.wait()` within 100 ms. Missing exit is crash/restart. EOF starts 5 s grace + poll,
then ping/pong (10 s) and process-group kill; three missed heartbeats at 90 s kills.
A 30 s drain deadline emits `supervisor_stall` and suspends heartbeat enforcement.

## 5. Versioning, timers, reconciliation

`proto=1` is a draft addition on init/ready. Breaking message/required-field/framing
changes bump proto; optional fields/events are compatible. Draft timers: ready 60 s,
pong 10 s, heartbeat 15/90 s, wall budget 1,800 s, EOF grace 5 s, drain 30 s,
graceful/term grace 10/5 s. Events have no response deadlines.

Reconciliation (all retained): (1) run_task extends arch init-only process; (2) shutdown
extends signal-only shutdown; (3) check_health aliases ping/pong; (4) generic ok/error
generalizes per-message responses; (5) progress aliases `tool_event`; (6) fatal_error
aliases error + recoverable; (7) exit request echo is draft-only; (8) status maps to
`Result` vocabulary; (9) proto field is additive; (10) 1 MiB cap/truncation is additive;
(11) diff/tails/nested metrics are draft additions; (12) context is both init field and
request; (13) PROTO taxonomy fills an unspecified gap; (14) tails summarize captured
output. Architecture §5.2 wins unresolved rows. The event mapping remains ready→started,
result→finished, heartbeat/checkpoint direct, fatal→worker_error, exit reason→terminal
kind, stderr→worker_stdout_line; `orchestrator.py` remains a placeholder at this snapshot.

Historical trace IDs/refs are retained: init request `01JWCKQN2E1Z9K5Y3M8P7R4T1`, task
`wt-abc-001`, generation `3`, worker PID `12345`, tool `grep_code`, and `M1 Nuntius`.
Open questions: ping naming, generic envelopes, run_task retention, cap sizes, and proto
string versus int. This draft does not claim any of these proposals are current.

## Appendix A — wire traces and boundary examples

The historical happy-path trace used a stable request ID and generation:

```jsonl
{"type":"init","request_id":"01JWCKQN2E1Z9K5Y3M8P7R4T1","task_id":"wt-abc-001",
 "proto":1,"generation":3,"worktree":"/abs/worktrees/wt-abc-001",
 "base_commit":"a1b2c3d","spec":"Refactor dry_run.rs","max_turns":20,
 "tools":["read_file","write_file","edit_file","run_shell","git_op","grep_code"],
 "fanout_config":{},"provider_env_keys":["DEEPCODE_API_KEY"],
 "permissions":{"network":false,"shell":true},
 "heartbeat":{"interval_s":15,"timeout_s":90},"budget":{"max_wall_s":1800,"max_restarts":10}}
{"type":"ready","request_id":"01JWCKQN2E1Z9K5Y3M8P7R4T1","task_id":"wt-abc-001",
 "pid":12345,"generation":3,"proto":1,"monotonic_ms":100}
{"type":"heartbeat","task_id":"wt-abc-001","generation":3,"turn":1,
 "tool":"grep_code","status":"locating global statics","monotonic_ms":200}
{"type":"progress","task_id":"wt-abc-001","generation":3,"turn":1,"phase":"tool",
 "message":"locating global statics","tool":"grep_code","cmd":"rg 'static' src/",
 "exit_code":0,"duration_ms":1200}
{"type":"checkpoint","task_id":"wt-abc-001","generation":3,"turn":3,
 "state_ref":".../checkpoints/wt-abc-001/turn-003.json","commits_so_far":["a1b2c3d"]}
{"type":"result_envelope","request_id":"01JWCKQN2E1Z9K5Y3M8P7R4T1",
 "task_id":"wt-abc-001","generation":3,"status":"succeeded","exit_code":0,
 "commits":["a1b2c3d"],"files_changed":["src/dry_run.rs"],"diff":"…",
 "summary":"Removed 3 global statics.","metrics":{"metric_score":0.84},
 "failure_reason":null,"started_at":1.0,"ended_at":2.0}
{"type":"exit_message","request_id":"01JWCKQN2E1Z9K5Y3M8P7R4T1",
 "task_id":"wt-abc-001","generation":3,"reason":"done","monotonic_ms":300}
```

The request echo on `exit_message` is explicitly a draft extension: strict architecture
parity can drop it while keeping `exit` authoritative. If the worker dies between the
result and exit lines, the supervisor marks CRASHED even if it already read the result;
checkpoint/result durability is the recovery path. An EOF with a live grandchild runs
the 5-second grace, `proc.poll()`, ping/pong, then process-group kill.

## Appendix B — response and error examples

`ready` is the only response that admits RUNNING. Generic `ok` bodies are request
specific:

```json
{"type":"ok","request_id":"01J…","task_id":"wt-abc-001","monotonic_ms":123}
{"type":"error","request_id":"01J…","error":{"code":"WORKER_UNKNOWN_TOOL",
 "message":"tool not in init.tools","recoverable":false}}
```

Protocol errors never crash the supervisor: `PROTO_BAD_JSON` records line number and
skips; `PROTO_LINE_TOO_LONG` consumes through newline and resyncs; unknown type/request
ID is logged/dropped; out-of-order input is ignored and repeated violations may cause a
worker kill/restart. Domain classes remain separate:

| Class | Example | Recoverable | Historical action |
|---|---|:---:|---|
| Tool | `build_failure`, `test_failure`, `command_error` | yes | bounded restart/backoff. |
| Spec/config | `unknown_tool`, `invalid_spec`, `tool_not_allowed` | no | fail immediately. |
| Provider | `AllProvidersFailed` | yes | worker patience 180 s, then recoverable error. |
| Internal | unhandled exception | no | fatal error + fatal exit; stderr advisory. |
| Fencing | `generation_mismatch` | no | fatal exit; no retry. |

`partial_commits` remains attached to fatal errors so recovery knows what survived.

## Appendix C — timer and state-machine matrix

| Timer/state | Draft value | Trigger and action |
|---|---:|---|
| `ready_timeout` | 60 s | init without ready → kill/restart. |
| `pong_deadline` | 10 s | ping without pong during EOF escalation → group kill. |
| `heartbeat.interval_s` | 15 s | worker heartbeat cadence. |
| `heartbeat.timeout_s` | 90 s | three missed beats → watchdog kill. |
| `budget.max_wall_s` | 1,800 s | hard supervisor wall budget. |
| `eof_grace_s` | 5 s | EOF then poll before escalation. |
| `drain_deadline_s` | 30 s | read stall → `supervisor_stall`, suspend blame. |
| `graceful_s/term_grace_s` | 10/5 s | cancel/shutdown → SIGTERM then SIGKILL. |

Clean exit requires authoritative `exit_message` and `proc.wait()` within 100 ms.
Process return code alone cannot distinguish a worker that emitted result then crashed.
Task/session codes remain 0 done, 1 failed, 2 rejected, 3 timeout, 4 cancelled, >100
supervisor crash; code 2 is never worker-generated.

## Appendix D — reconciliation and scaffold boundaries

The fourteen draft/architecture rows are retained: run_task persistent shape versus
init-only worker; cooperative shutdown versus signal-only architecture; check_health
versus ping; generic envelopes versus direct per-message responses; progress versus
tool_event; fatal_error versus error; exit request echo; status succeeded versus Result
done/rejected; proto field; one-MiB line cap; diff/tail/metrics fields; context in init
plus separate request; PROTO taxonomy; and advisory output tails. Framing, init fields,
ready gate, heartbeat/fencing/restart/checkpoint/exit authority and task codes align with
architecture. Unresolved rows remain proposal-only.

At the snapshot `orchestrator.py` queued specs and emitted seed
`WorkerStarted`/`WorkerFinished`; it did not read wire messages, apply restart policy, or
gate ready. Intended mapping was ready→started, result→finished, heartbeat/checkpoint
direct, fatal→worker_error, exit reason→terminal event, stderr→worker_stdout_line. This
mapping is historical and does not assert the scaffold owns Nuntius today.

## Appendix E — protocol admission and bounded output

The receiver state machine was intentionally strict at the boundary:

```text
NEW → INIT_SENT → READY → RUNNING → TERMINAL → EXITED
             ↘ ERROR (recoverable or fatal)
```

`run_task`, `context`, `cancel`, and `shutdown` before `ready` produced
`PROTO_OUT_OF_ORDER`; a duplicate `init` or any message after `exit_message` was also
out of order. A worker response with an unknown request ID was logged and dropped rather
than delivered to a later request. Events were not acknowledged, so a malformed advisory
line could not create an ACK loop.

The 1 MiB cap protected the supervisor against a malicious or accidental unbounded
summary/diff. The receiver consumed bytes through the next newline before resuming, so a
large line could not poison all following messages. Sender-side caps remained part of
the contract: heartbeat/status 200 chars, progress message 200, command 512, summary
2,000, diff 64 KiB, stdout/stderr tails 200 lines. These caps were draft additions to
an architecture that deliberately used newline framing; they did not claim length-
prefixed transport or atomic pipe writes.

## Appendix F — worker/session exit mapping

The wire and host code spaces were kept separate. Worker process `returncode=0` was only
clean when an exit message arrived; `returncode<0` exposed a signal but still required
the liveness state machine. Host task codes were 0 done, 1 failed, 2 rejected, 3 timeout,
4 cancelled, >100 supervisor crash. A worker never emitted rejected (2), and the
supervisor never treated a worker's `exit_code` as authoritative over a failed gate.

Mapping by exit reason was deterministic: `done` plus wait→worker_finished; `cancelled`
plus wait→worker_killed; `fatal`→worker_failed/nonrecoverable; `crash` or missing exit→
restart policy. A result envelope followed by no exit remained a crash in the live path,
even though replay used a durable critical result as DONE. This apparent difference was
intentional: live liveness needed process evidence, while crash recovery needed durable
evidence.

`provider_env_keys` always carried names, never values. `fanout_config` could include
provider tiers and capabilities but no API key. Permissions were descriptive in this
draft; current source/tests, not this proposal, decide whether shell/network controls
are enforced. stdout remained protocol-only even when a library attempted to print; such
bytes were parse errors/advisory diagnostics, never debug semantics.

## Appendix G — request field ownership and redaction

`init.spec` and `context` were task data, not diagnostics; implementations were to log
only size/checksum and correlation IDs. `fanout_config` carried routing choices but no
API keys; `provider_env_keys` carried names and the host boundary resolved values. The
permissions object described network/shell intent, while actual enforcement belonged to
source-owned controls. The protocol itself never promised an OS sandbox or per-worker
approval.

`resume_from_checkpoint` was accepted only after generation fencing and worktree
recovery. A stale checkpoint from another task or generation was a protocol/domain
error, not a fresh context fallback. `request_id` linked the request to ready/result/error
but did not identify the worker; task ID plus generation did. `pid` was an observation
for event logs and orphan recovery, not an identity key.

The result envelope was bounded before serialization: summary ≤2k, diff ≤64 KiB,
stdout/stderr tails ≤200 lines, and metrics contained scalar score/breakdown fields only.
The supervisor added session/result references after the worker event; it did not trust a
worker's status over gate/merge verdict. These limits were proposal checks for a future
Nuntius implementation, not a claim that a current worker emits this exact envelope.

## Appendix H — explicit-tree wire boundary

The draft intentionally had no implicit child-recursion message. Static DAG validation
and admission happened in the harness; a dynamic `task_decomposed` proposal required a
new validated wave. Each admitted child received a fresh bounded context, and only the
strict upward result envelope was allowed back. A future `steer` or `shared_update` event
was a distinct control-plane surface, not a way to send raw sibling history. Prefix
stability could be measured for cache behavior, but this protocol made no provider-
independent cost or latency claim.

## Appendix I — framing corner cases

Whitespace-only lines were skipped before `json.loads`, so a worker's final newline did
not create a protocol error. Non-UTF-8 bytes were logged as `PROTO_BAD_JSON`; the reader
did not attempt replacement decoding because a replacement character could change a
request field. A partial line in the middle of a stream stayed buffered; a partial line
at EOF was discarded and tagged. Complete lines after an otherwise valid message were
not “trailing garbage”—NDJSON permits any number of messages until pipe close.

When a line exceeded 1 MiB, the receiver stopped retaining bytes but continued consuming
until newline to resynchronize. It then emitted a bounded advisory event with the byte
count. The sender caps were applied before JSON serialization, so a large diff or stderr
tail could not defeat the reader limit. This was a memory-safety draft addition to a
newline protocol, not a promise that every source path currently enforces the cap.

## Appendix J — request/response deadlines

`init` required `ready` within 60 s; failure killed the process group and entered restart
policy. `check_health`/`ping` required pong within 10 s during EOF escalation. `cancel`
and `shutdown` had no strict RPC deadline because the host's 10 s/5 s signal ladder was
the authority. `run_task` and `context` had no response deadline; wall budget and
heartbeat watchdog bounded them. Events had no acknowledgement or deadline. Timer values
were carried in init heartbeat/budget objects, and jitter was applied by the supervisor,
not self-reported by the worker.

The draft kept all request IDs sender-assigned and all response IDs echoed. A response
cannot be matched by task ID alone because one persistent-worker shape could have several
requests in flight; the current one-task-per-process architecture deferred that shape.
This is why `run_task` was marked an explicit extension instead of silently treated as
implemented.

## Appendix K — typed error and reconciliation rules

Protocol errors were transport facts. `PROTO_BAD_JSON` carried the stream and line
number; `PROTO_TOO_LARGE` carried the discarded byte count; `PROTO_OUT_OF_ORDER` carried
the observed state and message type; `PROTO_UNKNOWN_REQUEST` carried the request ID.
They were never converted to a successful worker result. Domain errors were separate:
`AllProvidersFailed` was recoverable during provider patience, a missing required field
was nonrecoverable, and a generation mismatch fenced the sender. The host added gate,
merge, timeout, cancellation, and supervisor errors after it had process evidence.

The request/response examples preserved one important distinction: an `ok` response
acknowledged receipt, while `worker_finished` or `result_envelope` carried the durable
task outcome. A worker could return `ok` for `shutdown` and still fail to exit; the host
then used the signal ladder and process-group wait. Similarly, a `checkpoint` event
proved a resume point but did not prove that `result.json` or `refs/heads/main` had been
published. Reconciliation consumed the authoritative store/ref evidence rather than
guessing from a final line.

The protocol's redaction boundary applied before logging, queue admission, and observer
publication. API-key values, prompts, scratchpad text, and raw trajectories were never
wire fields. `provider_env_keys` and permissions were names/intent only; this historical
draft did not promise a per-worker OS sandbox or approval callback. A future explicit-tree
harness could add control messages, but it would still require fresh context, bounded
payloads, and strict upward envelopes.

## Appendix L — handshake and generation examples

The intended sequence was `init(request_id, task_id, generation)` → `ready(echo_id)` →
`run_task`/events → terminal result → `exit_message` → process wait. A `ready` with the
wrong request ID, a missing protocol version, duplicate init, or a result from an older
generation was rejected and logged with its typed code. A new generation reused the task
ID but changed `worker_id`; PID reuse therefore could not join old and new event streams.
These checks were draft protocol tests, not evidence that every current worker path
enforces them.

The draft kept one writer per pipe. A supervisor request was serialized before write and
flushed; a worker event was serialized by the worker and flushed before the next turn.
Concurrent writes, shell diagnostics on stdout, and replacement decoding were rejected
because they could corrupt request IDs or field boundaries. stderr remained advisory and
bounded. These rules were protocol invariants, not a claim that a current process obeys
them on every code path.

The draft did not add a length prefix, binary side channel, or implicit child session.

Every wire payload was bounded before serialization. Oversized advisory text was marked
truncated; oversized critical fields caused a typed failure rather than silent growth.

The worker could not enlarge a supervisor-owned limit.

EOF triggered liveness inspection, not immediate failure. Process state and ping/group
kill supplied the authoritative decision.

This EOF rule was historical.

Current framing is source-owned.

No sandbox is implied.

Approval is not promised.

Source tests decide enforcement.

NDJSON framing remains historical.

Historical only.

Historical review identifier retained: `M4`.
