# Cambium Logging Design (resolves IMPL-M7)

**Historical snapshot — 2026-08-09.** Design-only record from the M6/M7 work, based on
CPython 3.14.7 in branch `wt-logging`; `IMPL-M6` redaction and `IMPL-M7` logging were
not merged by this document. Current behavior is owned by
[`docs/architecture/architecture.md`](../architecture/architecture.md), source/tests,
and [`v2-1-status.md`](v2-1-status.md).

**Current note (not retroactive):** active `supervisor.run_plan` is flat;
`task_decomposed` remains unsupported; provider cascade is source-defined and honors
`Retry-After`; worker stdout/event admission is bounded; there is no per-worker OS
sandbox or approval; DLQ and eval cache are absent.

## 1. Requirements

- Keep the async hot path non-blocking: `QueueHandler.put_nowait` into a bounded queue;
  one `QueueListener` writer thread owns file I/O.
- Emit structured JSON with `ts`, `level`, logger/module, message, process/thread,
  and correlation (`session_id`, `task_id`, `worker_id`, `request_id`).
- Redact at emit (`IMPL-M6` + arch §12): prompts, outputs, provider keys, tokens,
  PII, environments, auth headers, and worker `init` bodies never appear verbatim.
- Bound disk and memory: `RotatingFileHandler` and `queue.Queue(maxsize=10_000)`;
  advisory records drop/count on full, with a rate-limited warning. This is distinct
  from the event store, where only non-critical events drop and critical events wait.
- Keep logs separate from the durable event log. Paths are session-absolute because
  workers run with `cwd=worktree`; `.cambium/` is gitignored and survives recovery.

## 2. Design

### 2.1 Components and path layout

`LoggingService` creates a `QueueHandler` on each process, a bounded queue, a
`ShutdownSafeQueueListener`, `JsonFormatter`, `RedactFilter`, and `ContextFilter`
(`contextvars` + `bind_correlation`). The supervisor writes
`.cambium/logs/cambium.log` (`maxBytes=100 MB`, `backupCount=5`, ≤600 MB); each worker
writes `.cambium/logs/workers/<task_id>.log` (`10 MB`, `3`, ≤40 MB). Worker stderr is
captured and mirrored as advisory event `kind="log"`; stdout remains JSON-lines IPC.

### 2.2 Queue and writer mechanics

`DropQueueHandler` catches `queue.Full`, increments `dropped`, and returns without
`handleError` traceback. `JsonFormatter` uses `default=str` and safe `getattr` so a
record cannot kill the writer. `QueueListener.stop()` is replaced by
`ShutdownSafeQueueListener`: enqueue the sentinel with `put(block=True, timeout=5.0)`
then drain before closing handlers. `LoggingService.flush_and_stop()` is the only
shutdown API; no `atexit` or signal dependency. The queue is advisory, unlike critical
event records (`§6.5`) which have their own bounded admission.

`RedactFilter` runs at emit and masks both structured fields and message arguments.
`REDACT_KEYS` includes `api_key`, `token`, `secret`, `authorization`, `cookie`, and
similar names; config logging exposes `api_key_env` names only. Correlation fields are
injected from context vars, not process-global mutable state.

### 2.3 Levels and non-logging policy

`DEBUG` is development-only; `INFO` records lifecycle; `WARNING` records retries,
drops, malformed advisory lines; `ERROR` records failed tasks/provider calls; `CRITICAL`
records process/session failure. Never log prompts or raw model output; use token count,
module, checksum, or bounded summaries. Never log keys, env/config dumps, PII, auth
headers/cookies, or the `init` body.

## 3. Scenarios

All scenarios use the real logging module and a temporary directory (no mocks), matching
the repository scenario convention.

1. `test_logging_nonblocking_under_writer_lag` (S1): slow writer (`0.002 s`/record),
   2,000 records complete under 1,000 ms; listener thread owns output; no logging-error
   spam; `0 < dropped < total`.
2. `test_logging_redacts_secrets_and_prompts` (S2): JSON lines parse, fake `sk-...`
   values and `api_key` fields are absent or `***`, and `prompt ***` is stored.
3. `test_logging_rotation_bounds_size` (S3): at most `backupCount+1` files and total
   size ≤ `maxBytes*(backupCount+1)`.
4. `test_logging_shutdown_safe_and_correlated`: a full queue does not raise
   `queue.Full`; concurrent bindings preserve each task/worker pair.

## 4. Verification record (CPython 3.14.7)

Commands were run in `wt-logging` with `uv run --python 3.14.7 python …`:

```text
from logging.handlers import QueueHandler, QueueListener, RotatingFileHandler
ok: QueueHandler, QueueListener, RotatingFileHandler import
[PASS] listener writes on its own thread; correlation attrs survive the queue
[PASS] rotation: files=['cambium.log','cambium.log.1','cambium.log.2','cambium.log.3'] total=6642B max=8000B
[FAIL] vanilla queue-full producer 2148.4ms/2000 (cause: QueueHandler handleError spam)
[PASS] DropQueueHandler 203.3ms/5000; dropped=3977; kept=1023; no handleError spam
[PASS] shutdown-safe sentinel; [PASS] vanilla QueueListener.stop() raises queue.Full
[PASS] contextvars/filter correlation: task-1→w-1, task-2→w-2
```

The source facts checked in 3.14.7 were `QueueHandler.enqueue=put_nowait`, producer-
thread `prepare`, daemon `QueueListener` monitor, idempotent `stop`, and
`enqueue_sentinel=put_nowait`; the design overrides only the queue-full and sentinel
footguns.

## 5. Unverified and adoption files

Unverified: free-threaded builds, real-disk contention with the event writer,
`cambium doctor` drop-counter wiring, and contextvars propagation across threads
(the thread case above passed). Proposed adoption files are
`src/cambium/logging.py`, `tests/scenarios/test_logging.py`, and supervisor/worker
entry-point setup/flush calls. The historical `src/cambium/events.py` seed supplied
field names for this design; it is not the live event contract. Live persistence uses
`cambium.store.EventStore` with redacted dictionary/envelope records at its append/read
boundary.

## Appendix A — formatter, filter, and rotation details

The proposed JSON record shape was:

```json
{"ts":1754212800.123,"level":"INFO","logger":"cambium.supervisor",
 "message":"worker ready","process":1234,"thread":"Thread-1 (_monitor)",
 "session_id":"s-1","task_id":"task-1","worker_id":"task-1#2",
 "request_id":"01J…"}
```

`JsonFormatter` copied stable record fields and known correlation extras; unknown extras
were stringified with `default=str`, never allowed to raise. `ContextFilter` filled
absent IDs from `contextvars` at producer time so a `QueueListener` thread did not need
to inherit async context. `RedactFilter` applied key masks and value patterns to both
`record.msg`/args and structured extras; it ran at handler emit to cover records that
bypassed the helper. Redaction happened before output, not as a post-hoc file scrub.

Rotation was size-based: a write that would exceed `maxBytes` renamed current file to
`.1`, shifted backups, and opened a fresh file. The bound formula was
`maxBytes*(backupCount+1)`, so supervisor logs were ≤600 MB and worker logs ≤40 MB each.
Worker logs used session-absolute paths; relative `.cambium/logs` would land in the
worker worktree because spawn `cwd` differs from the session root.

The queue-full footgun mattered: stdlib `QueueHandler` reaches `handleError`, which
emits a traceback per dropped record and consumes producer CPU. `DropQueueHandler`
caught `queue.Full` directly, counted drops, and emitted one rate-limited warning. The
shutdown footgun was symmetric: vanilla `enqueue_sentinel()` used `put_nowait` and raised
`queue.Full` on a saturated queue; `ShutdownSafeQueueListener` used a bounded blocking
put and joined after the sentinel, so earlier records drained first.

## Appendix B — scenario fixture details

S1 used a 2 ms/record slow writer and 2,000 producer records; the historical reference
was <1,000 ms producer wall time, with listener thread names in every JSON record,
nonzero but incomplete drops, and no `--- Logging error ---` stderr. S2 emitted a fake
`sk-` key in `extra` plus a secret-like argument and asserted parsed JSON, no literal
value, `api_key:"***"`, and `message:"prompt ***"`. S3 wrote until two rotations and
calculated total size rather than trusting names. The bonus filled the queue before
`flush_and_stop()` and bound two correlation contexts concurrently.

The design separated advisory log loss from event-log guarantees: losing a log line was
acceptable and surfaced as `logs_dropped`; losing a critical `result`/`checkpoint` event
was not. `cambium doctor` was expected to expose the counter, but that integration stayed
UNVERIFIED. No prompt, model output, API key, cookie, environment dump, or worker init
body could be recovered from a compliant log.

## Appendix C — source verification notes

CPython 3.14.7 checks confirmed `QueueHandler.enqueue=put_nowait`, producer-thread
`prepare`, daemon listener monitor, idempotent `QueueListener.stop`, optional
`respect_handler_level`, and `enqueue_sentinel=put_nowait` (`logging/handlers.py`). The
proposal changed only the two unsafe defaults. Free-threaded 3.14t, real disk
contention, doctor plumbing, and async-task context propagation were not run. These are
historical command/results, not claims about a current `cambium.logging` module.

## Appendix D — operational invariants

The service was intended to be configured once per process, with supervisor and worker
paths kept separate. A worker's logger inherited the session correlation context but not
the supervisor's file handler. Every handler wrote UTF-8 JSON with one record per line;
newline escaping happened in the formatter so a model message could not inject a second
record. Logger propagation was disabled after the service attached its queue handler,
avoiding duplicate writes through the root logger.

Shutdown was lifecycle-owned: supervisor `finally` called `flush_and_stop()` after child
reaping and before result publication; workers called it in their `main()` `finally`.
The listener's sentinel was inserted after all producer records already admitted, then
the monitor joined before file descriptors closed. A writer failure was visible on
stderr in development (`logging.raiseExceptions=True`); it never propagated into the
async supervisor as an unbounded exception, and a producer could still count/drop an
advisory record if the queue saturated.

Redaction was deliberately not a promise that arbitrary secrets are discoverable. The
filter masked known key names and configured value patterns; provider-specific formats
required adding patterns and tests. It never logged environment values, prompts, raw
completions, cookies, auth headers, or full init context. This historical design therefore
kept secret handling at the emit boundary while leaving source integration and doctor
plumbing explicitly UNVERIFIED.

## Appendix E — event/log separation

The event store and diagnostic log intentionally had different loss policies. Event
records carried lifecycle state and replay evidence; critical rows were fsynced before
ack and subscriber yield. Diagnostic logs carried stack traces, provider timing, and
operator hints; advisory records could drop under queue pressure. A `logs_dropped`
counter was a health signal, not a replay gap, and no DLQ was implied by the drop policy.

Worker stdout was excluded from the logging path because it was NDJSON protocol. Stderr
could be mirrored into `worker_stdout_line` events and also written to a worker log, but
the two copies were independently bounded/redacted. A malformed stdout line was an IPC
parse event, not a diagnostic print shortcut. This separation prevented a verbose model
or library from corrupting request correlation while retaining enough evidence for a
human to diagnose a failure.

## Appendix F — correlation and backpressure checks

Each log record carried task/worker/request correlation even when produced on a writer
thread. The context filter filled missing values with `null`, not a guessed global ID.
Two concurrent tasks bound different IDs and emitted the same message; the resulting
JSON lines had the correct pair for each task. A record with an unserializable extra was
stringified rather than killing the listener. A full queue dropped advisory records and
incremented a counter; it never blocked an async worker or emitted a traceback per
record. The event store's critical admission remained a separate contract.

## Appendix G — file and level matrix

The historical level policy was intentionally narrow. `cambium.supervisor` and
`cambium.store` emitted `INFO` for lifecycle milestones, `WARNING` for bounded drops or
provider pauses, and `ERROR` for a failed gate, merge, or writer. Worker tool chatter
was `DEBUG`; a terminal worker failure was `ERROR` with a typed reason. The policy did
not authorize logging prompts, full model output, environment snapshots, or raw IPC
lines. A correlation ID and a bounded summary were enough to join a diagnostic record to
the durable event stream.

Rotation limits were per process rather than global: the supervisor file was 100 MB
with five backups and worker files were 10 MB with three backups. The documented upper
bounds (600 MB and 40 MB) assumed a process did not retain old file descriptors after
rotation. A cleanup task could remove stale worker files only after the corresponding
session was terminal; deletion was not a substitute for event retention. The proposed
`logs_dropped` counter was advisory and did not create a dead-letter queue.

The source checks also recorded two stdlib behaviors that future implementations must
retest: `QueueHandler.prepare` runs on the producer thread, so redaction and formatting
must be safe there, and `QueueListener.stop` depends on a sentinel that can itself block
when the queue is full. The custom handler/listener pair addressed those boundaries but
did not claim to solve disk failure, free-threaded scheduling, or context propagation
outside the documented `contextvars` path.

## Appendix H — verification boundaries

The logging checks used CPython 3.14.7 and fake secrets, not live credentials or model
prompts. They verified queue behavior, redaction, rotation, correlation, and shutdown
ordering. They did not verify a current `cambium.logging` module, free-threaded Python,
real disk contention, crash-safe rotation, or `doctor` integration. A future source
change must rerun those checks and retain the command, interpreter, branch, and result
with the corresponding design ID.

The design kept log correlation orthogonal to event sequencing. `seq` belonged to the
durable event writer; log records used session/task/worker/request IDs and never invented
a sequence that replay could trust. A dropped diagnostic line therefore reduced human
detail but did not create a replay gap. This distinction remained important when stdout
was protocol data and stderr was mirrored advisory data.

The design did not make logs a source of truth for replay.

Operators joined logs to events by correlation IDs and inspected the durable event for
state. A missing advisory line was therefore diagnosable, not a state transition.

No log path implied a DLQ.

The bounded queue exposed a drop counter and rate-limited warning. It did not block the
async hot path or create a second durable store.

The current logging path may differ.

The record is a snapshot.

No live credential was used.

Redaction patterns need source tests.

Do not log secrets.

Advisory loss is counted.

Historical only.
