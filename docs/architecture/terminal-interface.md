# Interactive terminal interface

**Status:** target contract plus current UI audit. Source and tests remain
authoritative for current behavior.

## 1. Decision

Cambium's terminal interface should behave like a durable coding session, not a
loop that launches unrelated one-shot jobs. The frontend is a renderer and
controller over a session actor/event stream. It does not own provider calls,
context state, branches, or token accounting.

The default experience should combine the useful properties seen in pi-like
inline interfaces and OpenCode-like dashboards:

- preserve normal terminal scrollback and stream the transcript;
- keep a compact live status/footer for active work;
- expose branch/session/agent state and token/cost usage;
- support attach, resume, fork, compact, cancel, and model/provider inspection;
- degrade cleanly to line-oriented output and machine-readable NDJSON.

A full-screen alternate-buffer UI may be optional. It must not be the only way
to inspect a session.

## 2. Current audit at `main@877e4a7`

### Existing strengths

- `render.py` contains pure deterministic renderers rather than provider logic.
- TUI and REPL consume the same event records and final result objects.
- The TUI already displays live/final input, output, cached, total tokens,
  calls, elapsed time, provider/model, active subagents, and token rate.
- Event persistence allows post-run `session show/status/usage` inspection.
- REPL history is repository-local and permission-restricted.

### Current gaps

- `repl.py` creates a fresh immutable one-shot config and session leaf for each
  prompt. `tui.py` follows the same one-prompt execution boundary. The operator
  sees a loop, but the context engine sees separate jobs rather than one
  durable branch.
- The TUI repaints per event: known event kinds render as concise human lines
  (`render_event_line` formatter table); unknown kinds keep the raw-JSON
  fallback. On TTY streams both frontends draw a bottom status bar
  (`render_status_bar`: session/elapsed/task left, tok/s · in/out/cached ·
  cost · subagents right), refreshed per event while live and frozen after
  terminal events; non-TTY output stays byte-identical line-oriented NDJSON.
- Model summaries render as markdown on TTY streams via `render_markdown_if_tty`
  (honors `NO_COLOR`, `TERM=dumb`, injected-stream `isatty`); interpolated event
  fields are control-char sanitized before display.
- The REPL previously discarded aggregate token usage after rendering a prompt;
  this change set adds a line after every result with cumulative calls, input,
  output, cached, and total tokens for the REPL process.
- There is no shared interactive command protocol for `/resume`, `/fork`,
  `/compact`, `/agents`, `/usage`, `/model`, or `/cancel`.
- Input editing, multiline composition, queued steering, attach/detach, and
  branch navigation are not modeled as session operations.
- Provider cache-write tokens, context-window utilization, and exact cache
  affinity are not yet available to the UI.

## 3. Session actor boundary

Introduce one headless interactive session object:

```text
InteractiveSession {
  session_id
  branch_id
  active_epoch_id
  generation
  status
  provider/model policy
  cumulative_usage
  command(request) -> acknowledgement
  subscribe(cursor) -> ordered event stream
}
```

The actor serializes branch-mutating commands. Provider/tool work remains
asynchronous, but every accepted command receives an operation ID and an
expected generation. A duplicate operation ID is idempotent; a stale generation
is rejected or explicitly rebased.

Frontends may reconnect using `(session_id, event_cursor)`. Durable events are
the canonical transcript; in-memory queues are only delivery mechanisms.

## 4. Event stream

Every event has a stable envelope:

```text
seq
session_id
branch_id
generation
operation_id
kind
timestamp
payload
```

Required event classes:

- user input accepted/rejected;
- assistant text delta/final message;
- tool start/progress/result;
- child-agent admitted/started/checkpointed/completed/failed;
- provider dispatch/retry/rate-limit/result;
- context fork/compaction/epoch publication;
- usage delta and cumulative usage;
- branch/session lifecycle;
- warning/error/cancel acknowledgement.

The renderer must tolerate unknown additive event kinds. Critical state changes
remain durable before they are shown as committed.

## 5. Backpressure and rendering

Use a bounded queue between event production and each frontend. Never allow a
slow terminal to block worker stdout/event persistence indefinitely.

- Text deltas may be coalesced.
- Repeated progress/usage updates may replace an older undrawn update.
- Tool results, errors, checkpoints, branch changes, and final messages are not
  droppable.
- The durable store remains complete even when the visual stream coalesces.

The default inline renderer appends transcript events to normal scrollback and
uses ANSI cursor save/restore to refresh a small footer only when the terminal
supports it. Non-TTY output emits stable lines without control sequences.

## 6. Command surface

Commands are parsed by the frontend but executed by the session actor:

| Command | Meaning |
|---|---|
| `/new` | create and attach to a new session/branch |
| `/resume [id]` | attach to an existing durable session |
| `/fork [name]` | create a child branch from the active epoch |
| `/branches` | show branch heads and status |
| `/compact [focus]` | request safe-boundary compaction |
| `/agents` | show child-agent tree and budgets |
| `/usage` | show current-turn and cumulative accounting |
| `/model` | show exact provider/model/protocol and pin/substitution policy |
| `/provider` | show lane health, rate state, and cache affinity |
| `/cancel` | cancel the active operation with acknowledgement |
| `/detach` | leave work running only when explicitly supported |
| `/exit` | close the frontend; session close policy is explicit |

Free-form text is a user-message command against the active branch, not a new
unrelated session.

## 7. Token and cost display

The always-visible footer should distinguish:

```text
turn:      input / output / cache-read / cache-write / total / cost
session:   input / output / cache-read / cache-write / total / cost
context:   active tokens / model window / reserved output / utilization
runtime:   elapsed / first-token latency / tokens per second
identity:  provider / model / protocol / branch / epoch
agents:    active / queued / completed / failed
```

Cached tokens are a subset of input unless a provider defines otherwise. Do not
sum them into total a second time. Unknown values render as `?`, not zero. The
usage event keeps raw provider data plus normalized fields so accounting can be
reconstructed.

The final response block repeats the turn totals, while `/usage` shows the full
session and provider/task breakdown. Machine-readable mode emits the same
numbers as structured events.

## 8. Input behavior

- Multiline input has an explicit key binding and paste-safe mode.
- Submitted input is immutable and receives an operation ID.
- Steering while a turn is active is either queued for the next safe boundary
  or sent through an explicit steer operation; it is never spliced into a
  provider request mid-flight.
- Ctrl-C first requests cancellation of the active operation; a second Ctrl-C
  may terminate the frontend. The UI displays whether cancellation was merely
  requested or acknowledged.
- History stores user input only, not secrets, provider tokens, or expanded
  system context.

## 9. Architecture layering

```text
CLI argument parsing
    -> InteractiveSession control API
        -> context/routing/supervisor state machines
        -> durable event store
    -> event subscription
        -> pure view model
        -> inline renderer | full-screen renderer | NDJSON renderer
```

No renderer imports provider transports or mutates branch state directly. No
session logic depends on terminal dimensions or ANSI support.

## 10. Delivery sequence

1. Keep current one-shot commands intact and expose a reusable
   `InteractiveSession` control/event API.
2. Make REPL attach to one durable branch across prompts.
3. Add commands and cumulative usage/context footer.
4. Convert TUI to a renderer over the same actor, preserving scrollback by
   default.
5. Add reconnect/attach and deterministic transcript replay.
6. Add optional full-screen panes after the headless contract is stable.

This order prevents a polished TUI from freezing the wrong one-shot session
model.

## 11. Acceptance properties

- Two prompts in one REPL instance share one branch and advance generations in
  order.
- Reconnecting from an event cursor reproduces the same transcript and footer.
- A slow renderer cannot block durable event admission or worker progress.
- Randomized event chunking/coalescing does not change the final rendered
  semantic transcript.
- Token totals equal `stats.py` and durable `session usage` output.
- Cached token shapes from supported providers normalize without double count.
- Non-TTY output contains no cursor-control escape sequences.
- Ctrl-C cancellation has an observable request and terminal acknowledgement.
- Branch/compaction commands operate only at valid context-engine boundaries.

## 12. Computer-science mapping

- **Model-view-update / unidirectional data flow:** canonical events update a
  pure view model rendered by multiple frontends.
- **Actor model:** one session actor serializes branch mutations while workers
  run concurrently.
- **Event sourcing:** replayable durable transcript and reconnect cursor.
- **Reactive streams/backpressure:** bounded delivery with typed coalescing.
- **Linearizability at command admission:** operation IDs and generations make
  accepted branch mutations unambiguous.
- **CQRS:** commands mutate session state; event/read models drive UI queries.
