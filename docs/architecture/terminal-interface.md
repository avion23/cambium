# Terminal observability

**Status:** implemented contract. Source and tests are authoritative.

## Architecture

Cambium exposes one event-sourced operator read model:

```text
workers and supervisor
        │
        ▼
durable ordered events + immutable context checkpoints
        │
        ▼
ObservabilityState reducer
        ├── `cambium tui` live frame
        ├── `cambium monitor` attach/watch
        ├── `cambium monitor --once`
        └── `cambium monitor --json`
```

The reducer owns no provider, worker, branch, or context state. It folds ordered
facts into a projection. This is CQRS/event sourcing: runtime commands mutate
the system; the UI reads a replayable projection.

## Dashboard fields

Each agent row exposes:

- role: main or sub-agent;
- queued, starting, active, merging, succeeded, failed, cancelled, or exited;
- parent task, generation, turn, and context epoch;
- provider/model and latest tool;
- calls and summary calls;
- input, output, cached, and total tokens;
- latest output tokens/second;
- estimated cost.

The session header exposes aggregate lifecycle counts, usage, cost, output
throughput, elapsed time, event cursor, and recent failures.

The context line exposes:

- exact latest prompt tokens when provider usage supplies them;
- active prompt bytes and message count;
- immutable summary-trunk bytes and segment count;
- raw-tail bytes;
- byte-derived token estimates marked with `≈`.

Byte estimates are never presented as provider-tokenizer truth.

## Runtime instrumentation

Every provider call emits a validated `usage_event`. In addition to raw provider
usage, the worker records content-free prompt-shape metadata:

```text
call_kind
active_context_bytes
active_context_messages
summary_trunk_bytes
summary_segments
raw_tail_bytes
```

Unknown or invalid fields fail the usage event at the supervisor boundary.
Prompt contents never enter these metrics.

## Frontends

`cambium tui` remains the prompt-owning frontend. On a TTY it enters an
alternate screen only while a run is active and redraws the dashboard from
events. Non-TTY output stays line-oriented.

`cambium monitor [SESSION]` attaches to an existing session. With no explicit
path it uses `CAMBIUM_SESSION_ID` or the newest repository-local session.
`--once` renders one text frame; `--json` emits one machine-readable snapshot.

Closing a monitor never cancels the runtime. Cancellation remains owned by the
process that started the session.

## Correctness properties

- Replaying an event prefix is deterministic.
- A missing event cannot be reconstructed from live objects.
- The reducer is tolerant of unknown event kinds.
- Terminal lifecycle states are not overwritten by later nonterminal noise.
- Per-agent totals sum to session totals.
- Cached tokens are a subset of input and are not added to total twice.
- Output throughput uses output tokens, not prompt tokens.
- Checkpoint paths are confined to the session before inspection.
- The UI never renders arbitrary event payloads; only selected redacted fields
  enter recent-event summaries.


## Provider resource introspection

Durable usage records may include content-free quota-window snapshots. Operator
surfaces can display provider/model leases, reset times, remaining tokens and
requests, lane concurrency, cached/input/output tokens, output tokens/s, and
known marginal cost without reading worker memory.
