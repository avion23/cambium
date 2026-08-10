# Cambium Event-Log Schema — DRAFT

**Historical snapshot — 2026-08-09.** Research-stage proposal, not frozen code. It was
written against `/tmp/opencode/cambium-arch/docs/architecture/architecture.md`
v2.0.0, `src/cambium/events.py`, and the dataset format convention; deviations are D1–D13.
Current authority is [`docs/architecture/architecture.md`](../architecture/architecture.md),
source/tests, and [`v2-1-status.md`](v2-1-status.md).

**Current note (not retroactive):** active `supervisor.run_plan` is flat;
`task_decomposed` remains unsupported; provider cascade is source-defined and honors
`Retry-After`; worker stdout/event admission is bounded; no per-worker OS sandbox or
approval; DLQ and eval cache are absent.

For explicit-tree consumers, `submitted`/`task_decomposed` were intended to prove
static-DAG validation and admission before dynamic dispatch; each child starts from a
fresh bounded context, and only the strict I2.7 envelope is recorded upward. These
historical kinds do not imply implicit single-context recursion or a current scheduler.

## 1. Scope and envelope

The proposal covers submission/decomposition, worker lifecycle, stdout/stderr advisory
lines, checkpoints, merges, supervisor boot/shutdown, errors, liveness, drops, and
replay. Checkpoints remain a separate store: events say *what happened*, checkpoints
say *where to resume*. SQLite WAL is primary; JSONL mirror and `Session.events()` are
views of the same record.

Every record has this canonical field set:

| Field | Type | Nullable | Definition |
|---|---|---|---|
| `event_id` | ULID string | no | Durable correlation identity (D2). |
| `kind` | enum string | no | Catalog value below. |
| `seq` | integer | no | Per-session gap-free writer sequence. |
| `ts` | epoch float | no | Wall clock (`timestamp`/`wall_ts`). |
| `monotonic_ms` | integer | no | `time.monotonic_ns()//1_000_000`, interval clock. |
| `task_id` | string | yes | Null for supervisor-only records. |
| `worker_id` | string | yes | Derived `"{task_id}#{generation}"` (D3), never PID identity. |
| `request_id` | string | yes | NDJSON RPC correlation ULID. |
| `generation` | integer | yes | Worker fencing token. |
| `payload` | object | no | Kind-specific, redacted at enqueue. |

Seed mapping (`src/cambium/events.py`): `Event.type→kind`; `Event.timestamp→ts`;
`WorkerStarted.task_id/pid→task_id/payload.pid`; `WorkerFinished.status/exit_code`
map to terminal payload; `LogEvent.level/message→worker_stdout_line.payload.level/line`.
Seed `log` is renamed; valid protocol lines are never raw output.

Example (heartbeat):

```json
{"event_id":"01JXKQZ9X2F4H1A6B3C8D0E5F7","kind":"worker_heartbeat","seq":42,
 "ts":1754212800.123,"monotonic_ms":481234567890,"task_id":"wt-abc-001",
 "worker_id":"wt-abc-001#3","request_id":null,"generation":3,
 "payload":{"turn":4,"tool":"run_shell","status":"editing src/dry_run.rs"}}
```

## 2. Catalog and field definitions

Durability: **C** critical (fsync before ack/subscriber; zero intended loss), **NC**
non-critical (loss window ≤ `fsync_interval_s`, default 1 s).

| # | Kind | Tier | Payload fields and semantics |
|---:|---|:---:|---|
| 1 | `submitted` | C | `spec`, `base_branch`, optional `parent_task_id`, `depends_on`, `budget.max_wall_s`, `priority`; Custos admission proof. |
| 2 | `worker_started` | NC | `phase="spawned"|"ready"`; `pid?`, `worktree?`, `base_commit?`, `resume_from_checkpoint?`, `ready_timeout_s?`, `liveness`. Two records per generation. |
| 3 | `worker_heartbeat` | NC | `turn`, `tool` (string/null), `status`; default interval 15 s, watchdog timeout 90 s; heartbeat never proves death. |
| 4 | `worker_stdout_line` | NC | `stream="stdout"|"stderr"`, `level`, bounded `line` (4 KiB), `line_no`, `truncated`; only non-protocol bytes and parse failures. |
| 5 | `worker_checkpoint` | C | `state_ref`, `turn`, `commits_so_far`, optional `compact_summary`, `liveness`; durable resume point. |
| 6 | `worker_finished` | C | `status`, `exit_code`, `result`/`summary`, `commits`, `files_changed`, `diff_truncated`; clean terminal result. |
| 7 | `worker_failed` | C | `status`, `failure_reason`, `recoverable`, `exit_code`, bounded `stderr_tail`; permanent/nonrecoverable outcome. |
| 8 | `worker_killed` | C | `reason` (`watchdog|ready_timeout|ping_no_pong|cancelled|shutdown`), signal, `exit_code?`. |
| 9 | `restart_scheduled` | NC | `reason`, `delay_s`, `attempt`, `generation_next`; policy decision, not state transition. |
| 10 | `task_decomposed` | NC | `parent_task_id`, child task specs/IDs, dependencies, decomposition reason; **historical proposal only**. |
| 11 | `merge_started` | C | `task_id`, branch, `base_sha`, lock owner, phase `started`. |
| 12 | `merge_succeeded` | C | branch, `old_sha`, `merge_sha`, `reconciled`; maps to `merge_committed` + `merge_reconciled`. |
| 13 | `merge_failed` | C | `reason`, `old_sha`, `head_sha`, conflict paths, gate status; no publication. |
| 14 | `supervisor_started` | C | session id, pid, `event_schema_version`, recovered seq/gap info. |
| 15 | `supervisor_shutdown` | C | `reason` (`clean|cancelled|crash`), final seq, task counts. |
| 16 | `worker_error` | NC | error code/message, `recoverable`, request correlation; wire `fatal_error`. |
| 17 | `parse_error` | NC | stream, line number, bounded raw line, parse/encoding reason; reader skips. |
| 18 | `supervisor_stall` | NC | worker, drain deadline, elapsed, queue/reader state; suspends heartbeat blame. |
| 19 | `drop` | NC | dropped kind/count/reason/queue depth; non-critical overflow marker. |
| 20 | `recovery_gap` | C | missing seq range, last durable seq, source (`replay|snapshot`); changes replay semantics. |
| 21 | `eof_seen` | NC | stream, generation, grace deadline, process poll result; layer-4 advisory. |

`worker_started.liveness` encodes `{process_alive, ipc_ready, checkpoint_seen,
exit_message, eof_seen, watchdog_armed:{interval_s,timeout_s}}`; EOF starts escalation,
not immediate death. `worker_stdout_line` line is capped and full spill is managed outside
the record. `worker_checkpoint` is critical; summaries must remain redacted.

## 3. Ordering, durability, and liveness

`seq` is per-session, writer-assigned and gap-free for committed rows. Wall and monotonic
clocks are observations; causal order is `seq`, then `generation`/request links. Replay
uses the latest snapshot plus following events and treats a missing critical row as a
`recovery_gap`. Critical classes include submitted/result/checkpoint/exit/failure/merge/
boot/shutdown; advisory heartbeat/stdout/restart/drop/eof classes may be lost in the
bounded fsync window.

Schema version is in SQLite `PRAGMA user_version`, authoritative `meta` row, every JSONL
record (`schema_version`), and `supervisor_started.payload.event_schema_version`.
Adding a kind or payload key is additive; changing meaning, renaming/removing a field,
or changing the envelope bumps version. `migrate_event(record, from_v, to_v)` runs lazily
at replay; unknown higher versions refuse to open. Rows are append-only.

Proposed v1 DDL preserves unique columns/indexes:

```sql
CREATE TABLE events (
 event_id TEXT NOT NULL UNIQUE, seq INTEGER PRIMARY KEY AUTOINCREMENT,
 monotonic_ms INTEGER NOT NULL, wall_ts REAL NOT NULL, kind TEXT NOT NULL,
 task_id TEXT, worker_id TEXT, request_id TEXT, generation INTEGER,
 payload TEXT NOT NULL);
CREATE INDEX events_task_idx ON events(task_id, seq);
CREATE INDEX events_kind_idx ON events(kind, seq);
CREATE INDEX events_worker_idx ON events(worker_id, seq);
CREATE TABLE snapshots(seq INTEGER PRIMARY KEY, taken_at REAL NOT NULL,
 schema_version INTEGER NOT NULL, state_summary TEXT NOT NULL);
CREATE TABLE meta(key TEXT PRIMARY KEY, value TEXT NOT NULL);
```

## 4. Reconciliation decisions (D1–D13)

| ID | Historical deviation / retained alternative |
|---|---|
| **D1** | Lifecycle names differ from arch §6.5 (`submitted` vs `task_assigned`, etc.); mapping is the catalog. |
| **D2** | Add ULID `event_id` alongside `seq` for DB/mirror/subscriber correlation. |
| **D3** | Add derived `worker_id=task_id#generation`; PID remains payload observation. |
| **D4** | Consolidate `ts` naming for arch timestamp/wall_ts and seed timestamp. |
| **D5** | Fold spawn/ready into `worker_started.phase`; split if final arch requires two kinds. |
| **D6** | `worker_stdout_line` is non-protocol residual; valid wire lines become typed events. |
| **D7** | Draft-only kinds: killed, restart, decomposed, merge_failed, supervisor boot/shutdown, worker_error, recovery_gap, drop, parse_error, eof. |
| **D8** | Draft tier assignments: rare/terminal/replay-changing kinds critical; advisory/reconstructible kinds non-critical. |
| **D9** | `merge_started/failed/succeeded` map to `merge_progress`/`merge_committed`/reconciled. |
| **D10** | `liveness` object is a proposed encoding of the four-layer model. |
| **D11** | DDL adds `event_id`, `worker_id`, worker index, meta, snapshot schema version. |
| **D12** | `tool_event` omitted from the 21-kind draft; checkpoint/heartbeat reconstruct it; restore as a first-class kind if arch §6.5 wins. |
| **D13** | `merge_reconciled` is represented by `merge_succeeded.reconciled=true`; restore name if required. |

## 5. Open questions and source mapping

Reconcile `tool_event` (D12), kind vocabulary, D2/D3 columns, `restart_scheduled` tier,
and synthesized crash shutdown. Appendix mapping is retained above; source fields are
`src/cambium/events.py`. The architecture sources are §3.6, §5, §6.1–6.5, §7 and
module-template dataset §5. This draft remains a historical proposal: it does not claim
that `task_decomposed`, 21 kinds, or the DDL are current.

## Appendix A — catalog payload examples

The original draft kept examples to pin field names and replay semantics. They are
retained here in shortened form:

```json
{"kind":"submitted","task_id":"wt-abc-002","worker_id":null,
 "payload":{"spec":"Refactor parser","base_branch":"main",
            "parent_task_id":"wt-abc-001","depends_on":["wt-abc-001"],
            "budget":{"max_wall_s":1800}}}
{"kind":"worker_started","task_id":"wt-abc-002","worker_id":"wt-abc-002#1",
 "generation":1,"payload":{"phase":"ready","pid":20471,
            "worktree":"/abs/.cambium/worktrees/wt-abc-002",
            "base_commit":"a1b2c3d","ready_timeout_s":60,
            "liveness":{"process_alive":true,"ipc_ready":true,
                         "checkpoint_seen":"checkpoints/wt-abc-002/turn-003.json",
                         "exit_message":null,"eof_seen":false,
                         "watchdog_armed":{"interval_s":15,"timeout_s":90}}}}
{"kind":"worker_stdout_line","task_id":"wt-abc-002",
 "payload":{"stream":"stderr","level":"WARNING",
            "line":"DeprecationWarning…","line_no":17,"truncated":false}}
{"kind":"worker_checkpoint","task_id":"wt-abc-002","generation":1,
 "payload":{"state_ref":"checkpoints/wt-abc-002/turn-003.json",
            "turn":3,"commits_so_far":["a1b2c3d"]}}
{"kind":"merge_failed","task_id":"wt-abc-002",
 "payload":{"reason":"conflict","old_sha":"abc","head_sha":"def",
            "conflict_paths":["src/parser.py"],"gate_status":"passed"}}
```

`worker_started` appears twice per generation (`spawned`, then `ready`). A heartbeat
inside a long tool is progress, not death proof; three missed beats are a kill trigger
only when process/exit/EOF layers agree. A `drop` contains queue depth/count, while a
`recovery_gap` contains missing `seq` range and last durable sequence. The proposal kept
`worker_killed` separate from `worker_failed`: a supervisor signal is not worker error.

## Appendix B — terminal and replay rules

| Transition | Record | Replay consequence |
|---|---|---|
| PENDING→SPAWNING | `submitted`, `worker_started(spawned)` | prepare worker. |
| SPAWNING→RUNNING | `worker_started(ready)` | readiness gate satisfied. |
| RUNNING→CHECKPOINTING→RUNNING | `worker_checkpoint` | resume from `state_ref`. |
| RUNNING→DONE | `worker_finished` | result authoritative once critical row durable. |
| RUNNING→CRASHED | `eof_seen`/missing exit or `worker_killed` | restart + generation bump. |
| CRASHED→SPAWNING | `restart_scheduled` then started | rerun/reinject checkpoint. |
| RUNNING→FAILED | `worker_failed` | no descendant dispatch. |
| RUNNING→CANCELLED | `worker_killed(reason=cancelled)` | terminal; no restart. |
| Any→REJECTED | failed with `failure_reason=rejected` | typed plan/gate rejection. |

Only critical records determine terminal state; a non-critical heartbeat cannot turn a
missing result into success. Replay applies rows after a snapshot in `seq`; a missing
sequence produces `recovery_gap` and stops guessing. JSONL is self-describing via
`schema_version` per line; SQLite `meta` is authoritative for `user_version` because
`PRAGMA user_version` is not transactional. Unknown higher versions refuse to open.

## Appendix C — complete reconciliation rationale

- D1/D5/D9 are vocabulary/folding choices. If final architecture requires
  `task_assigned`, `worker_spawned`, `worker_ready`, `merge_progress`,
  `merge_committed`, and `merge_reconciled` verbatim, split/rename at the writer while
  preserving payload and tier.
- D2/D3/D11 are additive indexes. `event_id` correlates DB/mirror/subscriber;
  `worker_id` is derived; PID remains ephemeral to avoid recycling.
- D4 consolidates `timestamp`, `wall_ts`, and `ts`; it does not change ordering.
- D6/D12 protect stdout protocol purity. Restore `tool_event` if arch §6.5 is closed.
- D7/D8 propose lifecycle/error kinds and tiers. `recovery_gap` is critical because it
  changes replay; `eof_seen`/`restart_scheduled` are advisory and reconstructible.
- D10 encodes four-layer liveness without changing escalation.
- D13 carries `reconciled=true` until a final event kind decision.

These adopted/rejected alternatives are historical decisions, not hidden compatibility
paths. The final schema must choose one vocabulary and bump `schema_version` only for a
breaking field meaning or rename.

## Appendix D — source mapping and identifiers

`events.py` seed fields map `Event.type→kind`, timestamp→`ts`,
`WorkerStarted.pid→worker_started.payload.pid`, `WorkerFinished.status/exit_code` to
terminal payload, and `LogEvent.message→worker_stdout_line.payload.line`. The draft was
checked against architecture §3.6, §5.1/5.3, §6.1–6.5, §7, module-template
dataset-format §5, and the IPC draft. Historical IDs retained include D1–D13, DS-M1,
DS-M6, IMPL-M2, and heartbeat example `01JXKQZ9X2F4H1A6B3C8D0E5F7`. The proposed
`task_decomposed` kind remains a proposal even though listed for replay linkage.
## Appendix E — durability tiers and writer protocol

Critical records were acknowledged only after the writer committed them and completed
the configured fsync path. The critical set included task admission/submission,
checkpoint, terminal result/failure/exit, merge progress/commit, recovery gap, and
supervisor boot/shutdown. Non-critical heartbeat, stdout, restart, decomposition,
health, drop, and EOF records could be dropped or lost within the one-second window.
The `drop` marker itself was non-critical but carried a count so replay could explain a
missing advisory tail.

The writer assigned `seq`, not the producer. A loop event therefore had no durable
sequence until admission; a subscriber saw it only after the writer inserted it. If a
critical queue was full, the producer waited at most 100 ms for admission; if that
deadline failed, the session entered the store-failure path rather than silently dropping
the record. Non-critical overflow evicted an older non-critical item, never a critical
one. Redaction happened before either queue, so observers and SQLite received the same
sanitized object.

The JSONL mirror was optional and off by default. It carried `schema_version` per line,
so a torn final line could be discarded and the preceding rows replayed. SQLite WAL was
primary because a torn row is impossible after commit. Snapshot state was a projection;
deleting/rebuilding it from events was a required canary.

## Appendix F — field ownership and unknown-kind policy

`event_id` was a convenience correlation key, not a replacement for `seq`; two records
could never share it. `worker_id` was derived from task/generation to prevent PID reuse
from joining events across restarts. `request_id` linked wire calls, while `generation`
fenced stale workers. `payload` was a JSON object even for empty events so readers could
apply kind-specific defaults without changing the envelope.

Unknown kinds were passed through by old readers because the architecture kind list was
open-ended. Unknown required envelope fields, by contrast, were a schema-version bump;
unknown payload keys were additive. This policy let `shared_update`, `child_result`,
`subtree_failed`, and `replan` remain draft additions without making old replay code
guess their semantics.

`worker_stdout_line` never carried valid `progress`/`tool_event` content. It covered only
stderr and bytes that failed JSON parsing, with a stream tag and line number. This kept
stdout protocol admission distinct from advisory diagnostics and prevented a malformed
line from being reinterpreted as a typed event after the fact.
## Appendix G — event consumer and migration examples

The draft's subscriber contract exposed one redacted envelope regardless of storage
backend. A SQLite reader selected rows by `seq`; a JSONL reader parsed each line's
`schema_version`; a live subscriber received writer-published records in the same order.
No consumer was allowed to infer a missing heartbeat as a failure, and no consumer could
recover a dropped non-critical line as if it were durable. The `worker_id` index made
generation joins efficient, while `request_id` linked a wire error back to init/run.

An additive migration (new payload key or kind) left `schema_version` unchanged. A
breaking rename, changed status meaning, or required envelope field bumped it and added
a pure migration step. Migrations ran at read/replay, never in-place; the writer emitted
only the newest version. A reader seeing a future version refused to open instead of
silently dropping unknown required fields. Snapshot `state_summary` was re-derived under
the current version, so a stale summary could not override newer events.

The proposal's `recovery_gap` semantics were explicit: if sequence 41 was durable and
42 was absent while 43 existed, replay emitted one critical gap for `[42,42]`, exposed
the last durable sequence, and left task state at the last proven transition. It did
not synthesize `worker_finished` from a later `merge_succeeded`, because merge can be
reconciled independently. Operators could then inspect checkpoint files or quarantine a
worktree before resuming.

## Appendix H — event ownership cross-check

Custos emitted `submitted`, worker lifecycle, stdout, liveness, restart, errors, drops,
and supervisor markers. Opifex emitted heartbeat/checkpoint and worker errors. Architectus
emitted decomposition/replan bookkeeping. Unio emitted merge events. This ownership map
was a proposal for a future explicit tree; current `run_plan` source/tests remain the
authority. It avoided giving the worker authority over supervisor-only events such as
`recovery_gap`, `supervisor_started`, or `merge_succeeded`.

The strict envelope also bounded event admission: `payload` was redacted and size-capped
before entering either queue; oversized advisory lines were truncated with
`truncated=true`; critical payloads had fixed result/summary/diff limits. This was the
event-log analogue of bounded worker stdout and did not imply that a DLQ or durable
overflow store existed. The current note records DLQ absence explicitly.

## Appendix I — lifecycle payload constraints

`submitted` was the admission proof and carried `parent_task_id`/`depends_on` only after
tree validation; a raw worker could not emit it to bypass static-DAG checks.
`task_decomposed` was a proposal event and remained unsupported by the current flat
`run_plan`; readers must not treat its presence in this draft as dynamic scheduling.
`worker_started` carried the process PID as an observation and derived worker identity
from task/generation. `worker_finished` and `worker_failed` carried bounded summaries,
commits, changed files, status, and exit code, but never a scratchpad or credentials.

Merge records kept branch, expected old SHA, head SHA, gate status, and conflict paths so
replay could distinguish a failed publication from a task failure. `supervisor_started`
anchored schema/session epoch; `supervisor_shutdown` closed it. `recovery_gap` was the
only kind whose purpose was to say “evidence is missing”; it did not invent state.

The event catalog's open-kind policy allowed future `shared_update`, `child_result`, and
`replan` additions without making old readers understand their payloads. Their durable
decisions still belonged to Architectus/Custos boundaries, not to a worker stdout line.
This kept event definitions precise while leaving current source ownership to tests and
`docs/architecture/architecture.md`.

The machine-interface source for this draft is the named **“JSON-Lines on stdio —
RECOMMENDED primary”** section in `docs/research/tui-best-practices.md`, not a fragile
line-number citation. It supports the same one-object-per-newline boundary and
headless subscriber use; it does not establish current Cambium event kinds.

## Appendix J — advisory and terminal field inventory

`worker_error` payloads carried `error_type`, bounded `message`, `recoverable`, and
`partial_commits`; provider outage remained recoverable while generation mismatch was
fatal. `parse_error` carried UTF-8/JSON reason, stream, line number, and a bounded line
sample. `supervisor_stall` carried reader/queue elapsed time and the deadline that was
missed. `drop` carried queue name, dropped kind, count, and current depth. `eof_seen`
carried stream, generation, grace deadline, and `proc.poll()` observation. These fields
made advisory records diagnosable without making them liveness authority.

Merge payloads retained expected old SHA and candidate head SHA, gate verdict, conflict
paths, and reconciliation flag. `merge_started` was emitted after Unio acquired its
lock; `merge_succeeded` only after atomic `update-ref`; `merge_failed` before any
publication. `supervisor_started` anchored session epoch and schema version; shutdown
recorded clean/cancelled/crash reason and final sequence. A reader could therefore tell
“merge was never attempted,” “gate failed,” and “ref advanced but event was missing.”

The draft's catalog was additive but not permissive about envelope shape: unknown
`payload` keys could survive a version, while unknown required envelope keys could not.
The strict envelope and bounded payloads were the event-side counterpart to IPC framing;
they did not imply a DLQ or durable overflow queue.

## Appendix K — replay projection rules

Replay built a projection from the canonical envelope, not from log text. For each task,
the reducer tracked generation, lifecycle state, last heartbeat, checkpoint reference,
terminal result, gate verdict, merge verdict, and restart count. `worker_started` for a
new generation superseded advisory state from an older generation; a stale event was
retained for audit but could not move the projection backwards. `restart_scheduled`
changed policy evidence only and did not itself move a worker to `RUNNING`.

The reducer treated terminal events as idempotent by event ID and sequence. A duplicate
`merge_succeeded` with the same expected-old/head SHAs was harmless; a second success
with a different head was an integrity conflict. `merge_failed` retained conflict paths
and gate output even when a later reconcile proved that the ref had advanced. A missing
checkpoint or result stopped automatic resume rather than inventing a context. This was
the historical reason checkpoints and events remained separate records.

For the explicit-tree proposal, `submitted` established a validated parent/dependency
edge, while `child_result` and `subtree_failed` updated the parent only after strict
envelope validation. A `task_decomposed` payload without a tree revision was advisory
and could not authorize dispatch. The current flat scheduler does not consume that kind;
the rule is preserved here as a proposal boundary, not a current feature claim.

## Appendix L — durable-kind checklist

The proposed critical set included result, checkpoint, worker exit/failure, task
assignment, merge progress/commit, and supervisor session boundaries. Advisory kinds
included heartbeat, progress, stdout, provider health, parse errors, EOF, and drops.
Critical admission waited for the bounded queue and fsync path; if that path failed, the
session entered a store-failure state. Advisory loss was counted and surfaced, but it
never supplied liveness authority. This tiering is historical and must be checked against
the current `CRITICAL_KINDS` source before use.

The schema also reserved bounded `metrics` and `usage` objects for terminal results.
They accepted scalar scores, breakdown maps, token counts, and provider/model labels;
arbitrary nested trajectories were rejected or redacted. A result's `status` remained
advisory until gate and merge records agreed. This prevented a worker from declaring
success through an oversized or unbounded payload and preserved replay's strict result
envelope.

Field names and nullability in §2 remain the authoritative draft catalog for this file.

The draft kept `event_id` stable across storage views while `seq` remained the replay
order. A mirror could reorder bytes only by violating the proposal.

Unknown payload keys were additive; unknown required fields required a schema version.

Payload samples were bounded and redacted before queue admission, so storage and live
observers shared the same sanitized object.

The catalog remains a draft.

Current event kinds come from source/tests.

Unknown future kinds need migration review.

Schema ownership stays explicit.

Replay checks remain required.

Envelope fields stay bounded.

Historical only.
