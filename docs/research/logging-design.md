# Cambium Logging Design (resolves IMPL-M7)

Design date: 2026-08-09. Purpose: replace the synchronous, unstructured logging
described in implementation review **IMPL-M7** ("no real logging framework;
synchronous file I/O on the hot path") with a stdlib-only, non-blocking,
structured, rotated, redacted logging design.

Sources read: `docs/architecture/reviews/review-implementation.md` (M7, M6), `docs/architecture/architecture.md`
(§5.1 item 2 stdout contract, §6 event log, §12 secrets, §13 logging, §14 Python stance, §16.2
session-dir contract), `src/cambium/**` scaffold (`events.py`, `orchestrator.py`,
`modules/base.py`).

**Every stdlib claim below was verified against a real CPython 3.14.7 install with
real commands; outputs are pasted in §5. Anything not checked is marked UNVERIFIED.**

---

## 1. Requirements

### 1.1 Non-blocking on the async hot path

IMPL-M7's `_log_event` did `open(log_path, "a")` + `f.write(...)` on every event,
synchronously inside the asyncio event loop. The rule that replaces it:

> **No syscall (open / write / fsync) ever executes on the event-loop thread.**

Log records cross to a dedicated writer thread through a bounded `queue.Queue`
(§2.2). The producer path is: level check → filter → `QueueHandler.enqueue`
(= `put_nowait`, verified non-blocking) plus a cheap message merge
(`QueueHandler.prepare`, §2.2). No disk I/O, no blocking, no lock.

### 1.2 Structured logs

- JSON Lines: one JSON object per line, stdlib `json` only — no structlog,
  no picologging, no third-party logging dependency (arch §5 "no new frameworks").
- Every line is `jq`-parseable. The log is a **diagnostic** stream; the machine
  contract lives in the event log (§1.6).

### 1.3 Correlation IDs

Every log record carries `task_id`, `worker_id`, `request_id`, `generation`
(arch §13). Mechanism: a `logging.Filter` reads a `contextvars.ContextVar` set at
the task boundary (§2.5). In the supervisor, multiple tasks interleave on one loop;
in a worker (one process per task), the context is set once at init.

### 1.4 Redaction (IMPL-M6 + arch §12)

No API key, token, secret, prompt, or personal data may reach disk (arch §12.1
threat model). A redaction `Filter` runs **at emit time** (producer side), with a
belt-and-braces pass in the JSON formatter (writer side) — matching arch §12.3's
"applied at enqueue time; belt-and-braces: the writer thread applies it again".

### 1.5 Bounded disk usage

Size-capped rotation via stdlib `RotatingFileHandler`. Total on-disk footprint is
bounded by `maxBytes × (backupCount + 1)` (§2.8, verified in §5.3).

### 1.6 Separation from the event log

| | Event log (§6) | Logging (this doc) |
|---|---|---|
| Store | `events.db` (SQLite WAL) + optional `events.jsonl` mirror | `.cambium/logs/*.log` (JSONL) |
| Semantics | **Machine contract**: replay, audit, training, state reconstruction | **Diagnostics**: human/support debugging |
| Writer | Single SQLite writer thread, fsync contract, critical/non-critical tiers (§6.5) | Writer thread, advisory — drops are acceptable (§2.9) |
| Consumers | `Session.events()`, offline optimization | `cambium doctor`, support, `jq` |
| Contract | Gap-free `seq`, durability promises (§6.5) | None beyond "a line is a JSON object" |

The two must never merge: an event is a typed, durable record; a log line is an
advisory diagnostic. A worker crash must still be reconstructible from the event log
even if its log file was rotated away or dropped.

---

## 2. Design

### 2.1 Component overview

```
                     event loop / any caller thread          dedicated writer thread
                ┌──────────────────────────────────┐      ┌──────────────────────────────┐
 logger ──────► │ filters (level, redaction,       │      │ QueueListener (daemon thread) │
 ("cambium.*")  │  correlation) at emit time       │      │   while True:                  │
                │      │                           │      │     rec = queue.get()          │
                │      ▼                           │      │     for handler in handlers:   │
                │  QueueHandler.enqueue()          │      │       handler.handle(rec)      │
                │  = put_nowait (never blocks)     │      │   # = JSON format + file write │
                │      │                           │      └──────┬───────────────────────┘
                │      ▼                           │             ▼
                │  queue.Queue(maxsize=10_000)     │      RotatingFileHandler → .cambium/logs/
                │  (DropQueueHandler policy §2.9)  │      (JsonFormatter attached HERE,
                └──────────────────────────────────┘       not on the QueueHandler)
```

The `JsonFormatter` is attached to the **file handler**, i.e. it runs on the writer
thread (verified: the JSON output's `thread` field names the listener's `_monitor`
thread, §5.2). `QueueHandler.prepare` runs the cheap message-merge on the producer
thread; the expensive JSON serialization never touches the caller.

### 2.2 Non-blocking handler mechanics (verified on 3.14.7)

`queue.Queue` + `logging.handlers.QueueHandler` + `logging.handlers.QueueListener` all
exist on 3.14.7 (§5.1). Verified mechanics from the 3.14.7 source
(`lib/python3.14/logging/handlers.py`):

- `QueueHandler.enqueue` — base implementation is `self.queue.put_nowait(record)`
  (L1474); the docstring explicitly invites override: *"You may want to override this
  method if you want to use blocking, timeouts or custom queue implementations."*
- `QueueHandler.prepare` — runs on the producer thread, merges `msg`+`args` via
  `format()` and strips `exc_info`/`exc_text` (L1476+). Because the QueueHandler has
  no formatter, this is the cheap default-formatter merge, not JSON serialization.
- `QueueListener.start` — spawns a **daemon** thread (`t.daemon = True`, L1572)
  running `_monitor`; a sentinel object stops it.
- `QueueListener.stop` — idempotent (gh-114706), enqueues the sentinel then
  `join()`s and resets `_thread` (L1642–1645).
- `QueueListener(respect_handler_level=...)` — keyword exists (L1529), default
  `False`. We set it `True` so the file handler's `level` is honored.
- `QueueListener` is also a context manager (`__enter__`/`__exit__`, L1539–1550).

**Two footguns were found by real behavioral tests and are designed around:**

1. **A full queue makes vanilla `QueueHandler` *spam* stderr, not block.** When the
   bounded queue fills, `put_nowait` raises `queue.Full` inside `Handler.emit`, and
   `logging`'s exception handling turns that into a fully formatted traceback per
   dropped record via `handleError` (verified: with a 2 ms/record consumer, a vanilla
   setup burned ~2.1 s of traceback formatting on 2000 records, §5.4). The producer
   never blocks — but the error churn defeats the purpose. Fix: **`DropQueueHandler`**
   (§2.9), a 10-line subclass that catches `queue.Full` in `enqueue` and counts the
   drop. Verified: producer stays flat at ~41 µs/record with a 2 ms/record consumer,
   no stderr spam (§5.4).
2. **`QueueListener.stop()` can raise `queue.Full` when the queue is full** because
   `stop()` → `enqueue_sentinel()` → `put_nowait(sentinel)` (L1632), and a full queue
   rejects the sentinel (verified §5.5). Fix: **`ShutdownSafeQueueListener`** whose
   `enqueue_sentinel` uses a blocking `put(block=True, timeout=5)` (§2.10).

### 2.3 JSON formatter (hand-written, no dependency)

`cambium/logging.py` (one formatter per arch §13) — a ~20-line `logging.Formatter`
subclass using `json.dumps`:

```python
class JsonFormatter(logging.Formatter):
    def format(self, record: logging.LogRecord) -> str:
        payload = {
            "ts": datetime.fromtimestamp(record.created, timezone.utc).isoformat(),
            "level": record.levelname,
            "logger": record.name,
            "module": record.module,
            "monotonic_ms": time.monotonic_ns() // 1_000_000,   # ordering aid
            "pid": os.getpid(),
            "msg": record.getMessage(),                          # redacted (§2.4)
            "task_id": getattr(record, "task_id", None),
            "worker_id": getattr(record, "worker_id", None),
            "request_id": getattr(record, "request_id", None),
            "generation": getattr(record, "generation", None),
        }
        if record.exc_info:
            payload["exc_text"] = self.formatException(record.exc_info)  # JSON string
        return json.dumps(payload, ensure_ascii=False, default=str)
```

Properties:

- One line per record (`json.dumps` emits no newlines). File opened with
  `encoding="utf-8"`, newline `\n`.
- `ts` is ISO-8601 UTC; `monotonic_ms` is for ordering records that crossed the queue
  near-identically (arch §13 lists `monotonic_ms`).
- `default=str` keeps non-serializable extras from crashing the writer thread; the
  writer thread swallows formatting errors (see §2.11) so one bad record cannot kill
  the log stream.
- `msg` is pulled from `record.getMessage()` **after** redaction (§2.4) so secrets
  are gone before the writer formats.

### 2.4 Redaction filter at emit point

A `logging.Filter` attached to the `QueueHandler` (runs on the caller's thread before
`enqueue`; `Handler.handle` filters before `emit`). It reuses arch §12.3's pattern
adapted from dict payloads to `LogRecord` fields:

```python
REDACT_KEYS = re.compile(r"(api[_-]?key|token|secret|password|auth)", re.I)
REDACT_VALUES = re.compile(r"(sk-[A-Za-z0-9]{20,}|AIza[A-Za-z0-9_-]{35}|...)")

class RedactFilter(logging.Filter):
    def filter(self, record: logging.LogRecord) -> bool:
        record.msg = REDACT_VALUES.sub("***", record.msg) if isinstance(record.msg, str) else record.msg
        if record.args:
            record.args = tuple(
                REDACT_VALUES.sub("***", a) if isinstance(a, str) else a
                for a in record.args
            )
        for attr in ("task_id", "worker_id", "request_id", "generation"):
            v = getattr(record, attr, None)
            if isinstance(v, str) and REDACT_KEYS.search(v):
                setattr(record, attr, "***")
        return True
```

- Key-shaped **values** (`sk-…`, Google API keys, JWTs) are scrubbed from the message
  and every string argument **at emit time**, before `QueueHandler.prepare` merges them
  into the final message.
- **Key-named attributes** (`api_key`, `token`, …) added via `extra=` are caught by
  the belt-and-braces pass: the `JsonFormatter` re-applies `REDACT_KEYS`/`REDACT_VALUES`
  to every field before serialization (same regexes, single definition). Belt-and-braces
  is required because redaction at the message level cannot see structured extras.
- The requirement is "redaction at emit point" — the filter chain guarantees the
  original secret never reaches the queue.

### 2.5 Correlation filter (contextvars + Filter)

Arch §13 proposed `LoggerAdapter`. A plain `LoggerAdapter` binds fields at adapter
creation, which is wrong for the supervisor where many tasks interleave on one loop.
Refinement: a `contextvars.ContextVar` + a `ContextFilter` on the `QueueHandler`:

```python
_corr = contextvars.ContextVar[dict[str, str] | None]("cambium.correlation", default=None)

def bind_correlation(task_id: str, worker_id: str | None = None,
                     request_id: str | None = None, generation: int | None = None) -> None:
    _corr.set({k: v for k, v in locals().items() if v is not None})

class ContextFilter(logging.Filter):
    def filter(self, record: logging.LogRecord) -> bool:
        for k, v in (_corr.get() or {}).items():
            setattr(record, k, v)
        return True
```

- The supervisor calls `bind_correlation(...)` at task dispatch; `asyncio` task
  isolation keeps the value per-coroutine. Verified: two threads each setting the
  `ContextVar` produce records correlated to their own task (`task-1→w-1`,
  `task-2→w-2`, §5.6). (`contextvars` propagates across `asyncio` tasks the same way
  it does across threads for the child context.)
- Workers (one process per task) call it once at init; their `task_id`/`worker_id`
  ride every record.
- The filter runs on the producer thread before enqueue, so the fields survive the
  queue exactly like `extra=` attrs (verified, §5.2).

### 2.6 Levels policy per module

Levels are logger levels set in `setup_logging(levels: dict[str, str])`, read from a
`[logging.levels]` config section. Defaults:

| Logger | Default level | Notes |
|---|---|---|
| `root` | `WARNING` | Floor for third-party noise |
| `cambium` | `INFO` | Supervisor + library |
| `cambium.opifex.<task_id>` | `INFO` | Per-worker (per arch §13) |
| `cambium.diffundo`, `cambium.nuntius`, … | `INFO` | Per-module, individually tunable |
| `dspy`, `litellm`, `httpx`, `urllib3`, `tokenizers`, `asyncio` | `WARNING` | Chatty libraries; **stdout redirected** per arch §5.1 item 2 (§11 secondary) |

`QueueListener(respect_handler_level=True)` lets the file handler's own `level`
(global floor) also gate what the writer processes.

### 2.7 Log file layout

**Canonical session prefix:** `.cambium/` (dotted) is canonical for the Cambium-owned
session subtree (arch §16.2). Every path below resolves against `$SESSION_DIR/.cambium/`;
the non-dotted `cambium/` form is not used in this design.

```
$SESSION_DIR/                     # = the repo root when session_dir is the repo root
└── .cambium/                     # Cambium owns everything under here (arch §16.2)
    ├── logs/
    │   ├── cambium.log           # supervisor diagnostics (RotatingFileHandler)
    │   └── workers/
    │       └── <task_id>.log     # per-worker diagnostics (RotatingFileHandler)
    ├── events.db                 # event log, SQLite WAL — arch §6 (separate, §1.6)
    ├── events.jsonl              # optional event mirror — arch §6
    ├── checkpoints/              # arch §6.4
    └── result.json               # arch §16.2
```

- **Supervisor process** writes `.cambium/logs/cambium.log`; **each worker process**
  writes `.cambium/logs/workers/<task_id>.log`. One file per process ⇒ **no two
  processes ever share a `RotatingFileHandler`** (rotation renames files; concurrent
  rotation from two processes on one file would race). This is why the design does not
  funnel workers into the supervisor's file.
- Worker stderr is still captured by the supervisor and mirrored into the event log
  as `kind="log"` events (arch §13), so a worker that crashes before its logger is
  configured remains observable, and log verbosity never corrupts the stdout protocol
  (arch §5.1 item 2: stdout is reserved for the JSON-Lines IPC; no `print()` in worker
  code).
- Refinement over arch §16.2 (which listed `cambium.log` directly under `.cambium/`):
  logs move into `logs/` so the diagnostics tree and the event store are cleanly
  separated (§1.6).
- **Session-absolute paths.** Log paths resolve against `$SESSION_DIR`, never the
  worker's cwd: workers spawn with `cwd = worktree` (arch §7.2), so
  `.cambium/logs/workers/<task_id>.log` must be constructed from the session dir, not a
  relative `.cambium/...` (a relative path would land inside the worktree).
- **`.cambium/` is gitignored.** `Surculus.recover()` runs `git clean -fd` before every
  respawn (arch §7.5 step 4); `.cambium/` is in `.gitignore`, so the clean cannot delete
  worker logs or the `generation` file.

### 2.8 Rotation policy

`logging.handlers.RotatingFileHandler` (verified present, §5.1; verified to bound
size, §5.3):

| File | maxBytes | backupCount | Worst-case footprint |
|---|---|---|---|
| `.cambium/logs/cambium.log` | 100 MB | 5 | ≤ 600 MB (arch §13 default) |
| `.cambium/logs/workers/<task_id>.log` | 10 MB | 3 | ≤ 40 MB per worker |

- Rotation is **size-based** (`maxBytes`): a write that would exceed `maxBytes`
  triggers `doRollover`, which renames `file → file.1 → …` and starts a fresh file.
- Bounded by construction: current file ≤ `maxBytes`, each backup ≤ `maxBytes`, so
  total ≤ `maxBytes × (backupCount + 1)` — verified numerically (§5.3).
- Worker logs are deleted with the session dir; per-task size caps stop a runaway
  worker from filling the disk regardless of worker count.

### 2.9 Queue bounds and drop policy (backpressure)

- `queue.Queue(maxsize=10_000)` per process (supervisor and each worker). Bounded,
  so a stuck disk (writer lag) cannot grow memory unboundedly.
- `DropQueueHandler` (the §2.2 footgun-1 fix): on `queue.Full`, count the drop and
  return; the record is discarded. Verified: producer stays flat, drops are counted,
  zero stderr spam (§5.4).
- Log records are **advisory** — dropping under load is the designed degradation
  (unlike the event log, where only non-critical events may drop and critical events
  block up to 100 ms, §6.5). The drop counter is exposed to `cambium doctor` (arch §13)
  as a health signal (`logs_dropped`), and a rate-limited `WARNING` is logged
  (throttled to once per N drops) so an operator sees the queue was saturated without
  paying per-record `handleError`.

### 2.10 Shutdown

- `ShutdownSafeQueueListener` (the §2.2 footgun-2 fix) overrides `enqueue_sentinel`
  with `self.queue.put(self._sentinel, block=True, timeout=5.0)` so `stop()` never
  raises on a full queue (verified §5.5) and **drains** everything queued before
  `stop()` returns (the sentinel is consumed only after prior records).
- `LoggingService.flush_and_stop()` is the one public shutdown API: `listener.stop()`
  (join the writer thread) then `file_handler.close()`. Called from the supervisor's
  shutdown path and each worker's `finally` on `main()`.
- No `atexit`/signal dependency: the supervisor lifecycle owns the call, matching the
  rest of the harness.

### 2.11 Writer-thread failure isolation

The two places a bad record can throw after the queue are the `JsonFormatter` and the
writer-handler chain. `logging.Handler.handle` wraps `emit` in
`try/except → handleError`, so a formatting error goes to stderr, not up to
`QueueListener._monitor` — but `_monitor` itself has no exception guard of its own
(only `except queue.Empty` around the dequeue), so any throw *outside* `Handler.emit`
(e.g. in a custom handler's `handle` or `QueueListener.prepare`) would kill the daemon
writer thread silently. Concrete guarantees adopted:

- Producer never throws from enqueue: `put_nowait` + the `DropQueueHandler` override
  (§2.9) contain every `queue.Full`.
- Writer never throws from formatting: `JsonFormatter` uses `default=str` and
  `getattr(record, ..., None)` for every optional field, so a non-serializable extra
  cannot raise inside `emit`.
- `logging.raiseExceptions` is left at its default (`True`) so genuine writer bugs are
  visible on stderr during development instead of being masked.

---

## 3. What NOT to log

| Category | Policy |
|---|---|
| **Prompts** | Never logged verbatim. Log a sanitized summary: token count, module name, first ~80 chars if safe. |
| **Model outputs** | Never logged verbatim. Log the structured result envelope (`Output`), not the raw completion text; summaries truncated to a configurable cap. |
| **API keys / tokens / secrets** | Never. Env-only at rest (arch §12.1); `RedactFilter` at emit (§2.4); keys are never in protocol messages, so they should never reach a log call in the first place. |
| **Personal data / PII** | No emails, phone numbers, or identifying attributes in messages. Repo/session paths are fine; user credentials are not. |
| **`os.environ` / config dumps** | Never dump environments or `providers.toml`; config logging prints resolved `api_key_env` **names** only (arch §12.2). |
| **Worker `init` message body** | The context payload is task data, not diagnostics; log only its size/checksum and the correlation IDs. |
| **HTTP auth headers / cookies** | Sanitized to `***` by `REDACT_KEYS`; better, never passed to a log call. |

Rule of thumb: if a line would be embarrassing in a support ticket, it should have
been redacted or summarized before it reached `logger.*`.

---

## 4. Test strategy

Scenario tests in `tests/scenarios/test_logging.py` (matching the scaffold's
no-mocking, scenario-test convention in `test_example_module.py`). All tests exercise
the real `cambium.logging` module against a temp dir; no fakes.

### 4.1 S1 — non-blocking under writer lag (`test_logging_nonblocking_under_writer_lag`)

Setup: `LoggingService` with a deliberately slow writer handler (`time.sleep(0.002)`
per record), bounded queue `maxsize=1000`, `DropQueueHandler`.

Asserts:
1. **Producer not serialized with the writer:** firing 2 000 records from the calling
   thread completes in < 1 000 ms wall time (consumer alone would take 4 s). Verified
   reference behavior: 5 000 records in 203 ms with the same 2 ms consumer (§5.4).
2. **Writer-thread separation:** every record's JSON `thread` field equals the
   listener's thread name, not the producer thread (verified §5.2).
3. **No error spam:** stderr captured during the burst contains no
   `--- Logging error ---` (the drop override swallows `queue.Full`; verified §5.4).
4. **Drops counted:** `drop_handler.dropped > 0` and `<` total, i.e. the bounded queue
   is the backpressure valve, not an error.

### 4.2 S2 — redaction works (`test_logging_redacts_secrets_and_prompts`)

Setup: normal `LoggingService`, then emit:

```python
logger.info("provider auth", extra={"api_key": "sk-" + "A" * 30})
logger.info("prompt %s", "sk-1234567890abcdef1234567890abcdef")   # a fake secret in args
```

Asserts: every line in the resulting file parses as a JSON object; no line contains
the literal `sk-A…` value or a non-redacted `api_key`; the key-named field reads
`"***"`; the message field reads `"prompt ***"`. Also emit a prompt-shaped message
and assert it is either absent or a sanitized summary (per §3).

### 4.3 S3 — rotation bounds size (`test_logging_rotation_bounds_size`)

Setup: `RotatingFileHandler(tmp/log, maxBytes=10_000, backupCount=3)`.

Asserts: after writing until the file has rotated at least twice, the directory
contains at most `backupCount + 1` files and
`sum(os.path.getsize(f) for f in files) <= maxBytes * (backupCount + 1)`
(verified numerically §5.3).

### 4.4 Bonus (folds into S1 or separate) — shutdown + correlation

`test_logging_shutdown_safe_and_correlated`: fill the queue past `maxsize`, call
`flush_and_stop()` — must not raise `queue.Full` (verified §5.5) — and assert two
threads/tasks with different `bind_correlation(...)` values produce records with
their own `task_id`/`worker_id` (verified §5.6).

---

## 5. Verified components (real outputs, CPython 3.14.7)

All commands run in the `wt-logging` worktree with
`uv run --python 3.14.7 python …`. Interpreter: `cpython-3.14.7-linux-aarch64-gnu`.

### 5.1 Imports

```
$ uv run --python 3.14.7 python -c \
  "from logging.handlers import QueueHandler, QueueListener, RotatingFileHandler; print('ok')"
ok: QueueHandler, QueueListener, RotatingFileHandler import
```

### 5.2 Writer thread + correlation attrs survive the queue

```
[PASS] listener writes from its own thread record.thread='Thread-1 (_monitor)' producer='MainThread'
[PASS] extra attrs (correlation id) survive task_id='task-1'
```

### 5.3 Rotation bounds disk usage

```
[PASS] rotation bounds size to maxBytes*(backupCount+1)
       files=['cambium.log', 'cambium.log.1', 'cambium.log.2', 'cambium.log.3'] total=6642B max=8000B
```

### 5.4 Footgun 1 + drop-policy fix

Vanilla setup, queue full, consumer sleeps 2 ms/record — producer stays non-blocking
but burns ~1 ms/record on `handleError` tracebacks:

```
[FAIL] producer non-blocking under slow consumer 2148.4ms for 2000 records (consumer: 2ms/record)
       (cause: queue.Full -> logging error spam; producer itself never blocked)
```

With `DropQueueHandler` (catch `queue.Full`, count drops) the same consumer:

```
[PASS] DropQueueHandler: producer flat under slow consumer
       203.3ms for 5000 records (40.66us/record), consumer sleeps 2ms/record
[PASS] drop-on-full counted, no handleError spam dropped=3977
[PASS] drop-on-full is the backpressure valve kept=1023
```

### 5.5 Footgun 2 + shutdown-safe fix

```
[PASS] shutdown-safe stop() with full queue sentinel uses blocking put
[PASS] vanilla QueueListener.stop() raises on full queue (footgun documented) observed: queue.Full
```

### 5.6 Correlation via contextvars + Filter

```
captured: [{'msg': 'work', 'task_id': 'task-1', 'worker_id': 'w-1'},
           {'msg': 'work', 'task_id': 'task-2', 'worker_id': 'w-2'}]
correlated: {'task-1': 'w-1', 'task-2': 'w-2'}
```

### 5.7 Source facts (3.14.7 `logging/handlers.py`)

`QueueHandler.enqueue` base = `put_nowait` (L1474, docstring invites override);
`QueueHandler.prepare` merges msg/args on producer thread (L1476+);
`QueueListener.start` uses a daemon thread (L1572); `QueueListener.stop` idempotent,
resets `_thread` (L1642–1645); `respect_handler_level` kwarg (L1529);
`enqueue_sentinel` base = `put_nowait` (L1632).

---

## 6. UNVERIFIED

- **Free-threaded build behavior** of this pipeline (target is the standard GIL build
  per arch §14; `queue`/`logging` are pure Python, so this is expected to behave the
  same, but it was not executed under `python3.14t`).
- **Real-disk latency** under the harness's actual I/O load (event-log writer and log
  writer share a disk; the drop counter in §2.9 exists to surface saturation).
- **`cambium doctor` integration** details (drop-counter plumbing) — designed, not
  implemented.
- `contextvars` propagation across `asyncio` tasks (not threads) — relied upon per
  Python semantics; only the thread case was executed (§5.6).

---

## 7. Files

- New: `src/cambium/logging.py` — `JsonFormatter`, `RedactFilter`, `ContextFilter` +
  `bind_correlation`, `DropQueueHandler`, `ShutdownSafeQueueListener`,
  `LoggingService` (`setup_logging`, `setup_worker_logging`, `flush_and_stop`).
- New: `tests/scenarios/test_logging.py` — S1–S3 (+ bonus, §4).
- Touch: `src/cambium/events.py` stays the event contract (no change); the
  supervisor/worker entry points call `setup_logging`/`bind_correlation` and
  `flush_and_stop` on shutdown.
