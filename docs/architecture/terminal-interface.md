# Terminal interface

**Status:** implemented display/input contract. Session persistence and context
continuation are documented in [interactive lifecycle](interactive-tui.md).

## Responsibilities

The frontend displays evidence and accepts operator input. It does not own worker
state, provider scheduling, quota reservations, or artifact publication.
`observability.py` reduces events; `tui.py` controls the interactive session;
`tui_screen.py` renders the linear timeline and transient rows. Keep terminal
escape handling at that boundary rather than embedding it in the worker
protocol.

The important information is the current task, useful output, current activity,
provider/model, resource consumption, and whether the result was accepted.
Diagnostic event names and full checkpoints belong behind inspection commands,
not repeated in every status row.

## Layout

The interactive TUI is one chronological timeline in the terminal's normal
buffer. User messages, Cambium responses, child-agent activity, useful tool
output, failures, and inspection-command results all append to that timeline.
The terminal owns scrollback; Cambium does not maintain a second scrollable
conversation viewport or switch to an alternate screen. Rendered cells,
viewport positions, and screenshots are never session data and are never
persisted; replay starts from durable events and response chunks only.

Only two rows are transient: one compact status row and the input row. The
status row names the current owner/resource first (provider wait, thinking,
streaming, tool, children, cooldown), then provider/model and compact usage when
space permits. `/detail` adds more metadata to this same row; it does not open a
pane. `/agents`, `/context`, `/quota`, `/status`, and `/inspect` append explicit
event-derived data projections to the timeline on demand instead of permanently
consuming columns or rows. `SessionSnapshot` is an immutable observability read
model, not a saved screen or viewport.

### Timeline event policy

The timeline is the durable child-progress view. It keeps bounded, sanitized
identity for these lifecycle edges when the corresponding events exist:

- child admission (accepted or rejected), followed by child start with the
  bounded task text and resolved provider/model;
- child tool start/completion and output, labeled with the child, tool, and
  bounded command/path identity;
- child terminal success or failure with its bounded result or cause; and
- the parent waiting for child results and resuming after the join.

These entries are ordinary timeline rows, not a second live window. Heartbeats
only refresh the transient status row and never enter scrollback. Private
reasoning, raw action JSON, credentials, and unbounded tool payloads are never
rendered. Inspection commands remain the place for deeper recorded evidence.

A resize reflows only future/tail rendering and the transient rows; it does not
replay old history into scrollback. While the operator has scrolled upward, the
terminal remains authoritative for viewport behavior. New output continues to
append normally without Cambium forcing a private scroll position.

The output rate is generated output divided by call wall time, not total
prompt-plus-output tokens. Missing output counts are unknown. `cache HIT` and
`cache MISS` come only from provider usage; the aggregate cache percentage is a
separate token ratio. Zero estimated cost is displayed numerically unless an
explicit billing classification supports a free/subscription label. Neither
label describes remaining token quota.

## Live-state vocabulary

Cambium should not collapse all nonterminal time into a spinner. The status row
names the resource currently producing or blocking progress:

| State | Meaning |
| --- | --- |
| `ORCHESTRATING` | local controller work before a provider/tool boundary |
| `ROUTING` | choosing the next hard-feasible provider lane |
| `PROVIDER · P/M · waiting` | a concrete provider/model attempt is in flight; no model output yet |
| `THINKING · P/M` | that provider reports reasoning-phase progress; reasoning text is not shown |
| `STREAMING · P/M` | provider response tokens are arriving, with output tokens/s; internal action JSON is not shown |
| `TOOL` | one tool or a parallel read batch is in flight; the row names it and elapsed time |
| `CHILDREN · waiting` | parent is suspended on child results |
| `COOLDOWN` | provider capacity/rate limit is delaying the next attempt |
| `stalled` / `silent` / `no output` | the phase revision or output stopped changing for the stall threshold |

Provider/model identity is published when an attempt starts rather than inferred
from the eventual result. Fallback therefore changes the live owner row as it
happens. Cache `HIT`/`MISS` appears only after provider usage establishes it; an
unknown cache result stays unknown. The worker heartbeat carries a monotonic
phase revision so active reasoning or response streaming does not look stalled.
Private reasoning and the model's internal JSON action fragments are not copied
into the timeline. Tool output and stream-phase changes reset the progress clock.

## Markdown and color

Rich is a normal Cambium dependency and is the single Markdown parser/theme for
one-shot/REPL output and the timeline. The shared renderer uses a no-background
extended palette when the terminal supports 256 colors and degrades to the
standard palette or plain text. Headings, links, quotes, inline code, code blocks,
tables, user/model/tool roles, routing, provider waits, reasoning-phase labels,
streaming, children, cache hits/misses, cooldowns and failures have distinct
semantic styles. Code blocks use compact bordered panels and narrow tables retain the
width-safe fallback so cells are not silently clipped.

The renderer sanitizes model/provider text before Rich sees it. Cambium keeps
Markdown in process rather than invoking a pager subprocess. The terminal owns
scrollback; the TUI owns only width-aware rendering, the input draft, focus, and
live-event synchronization.

## Input and controls

A resize keeps the unfinished input draft intact. Unicode wide and
combining characters count by terminal cells, not Python string length.
Bracketed paste and multi-line input must not accidentally execute embedded
control sequences as commands.

Ctrl-C cancels a running turn and exits when idle. `/cancel` also works while a
turn is active. Operator commands must not be deferred behind a queued model
prompt when they are intended to interrupt or inspect running work.

On POSIX, `terminal_input.py` owns input on the same asyncio loop as the timeline.
It keeps the draft and cursor in Python data, reads terminal keys with
`add_reader`, and restores the saved terminal mode on exit. No libedit buffer or
resize signal is shared with a background input thread. A resize can redraw
immediately while preserving the unfinished draft and cursor.

Enter submits the draft; Alt-Enter inserts a newline. Bracketed paste retains
pasted newlines in one prompt, including CRLF split across input reads. Paste
chunks are inserted together rather than recopying the growing draft for every
character. In a multiline draft, Up/Down move between its lines rather than
replace it with history. Home/End and Ctrl-A/E move within the current line;
single-line Up/Down still navigate history. Deletion and Ctrl-U/K/W edit the
same buffer. The horizontal viewport shows the current line with a `2/3`-style
line indicator; it is not a full multirow editor. Explicit `<<<`/`>>>` blocks
and backslash continuation also remain available.

F6 cycles agent focus without submitting or clearing the draft. `/focus TASK`
selects a known task; `/inspect` uses the same bounded recorded-state projection
as the model's `inspect_state` tool. It prioritizes live children, shows actual
provider/model and result information, and points to history for omitted detail.
The conversation still contains the session's combined transcript.
Injected streams and non-POSIX terminals keep the existing line-reader path;
POSIX PTY results are not evidence of equal behavior on every platform.

Output synchronization prevents concurrent status, tool, and input writes from
interleaving escape sequences. Provider/tool text is sanitized before reaching
the terminal; retain this concrete display boundary even while simplifying
other harness policy.

`NO_COLOR` disables styling; it does **not** by itself make a capable terminal
non-interactive or disable cursor motion. Terminal capabilities and whether the
stream is a TTY determine the appropriate display path. Piped output must remain
readable without timeline escape sequences.

## Commands

| Command | Purpose |
| --- | --- |
| `/help` | Available interactive commands |
| `/status` | Session, branch, context, agents, and usage |
| `/usage` | Cumulative usage including the live turn, without double counting |
| `/agents` | Agent lifecycle, provider/model, and per-task counts |
| `/focus TASK` | Select a task; F6 cycles focus while retaining the draft |
| `/inspect [TASK]` | Shared recorded state for a task, defaulting to the focused task |
| `/context` | Current context, checkpoint, epoch, and trunk/raw sizes |
| `/quota` | Explicit read-only account-wide quota inspection |
| `/session` | Persistent session identity |
| `/model [provider:model]` | List eligible choices or set a subsequent preference |
| `/branches`, `/fork`, `/compact` | Inspect or change conversation continuation |
| `/events` or `/tail` | Recent bounded events |
| `/detail` | Toggle additional metadata on the single status row |
| `/cancel` | Cancel the active turn |
| `/exit` | Leave the frontend |

CLI `--help` and the controller's command handling remain authoritative for
arguments. The live timeline and status row are the dashboard; there is no
second dashboard mode.

## Read-only resource projection

Normal rendering consumes the supplied snapshot only. It does not open a quota
database, change its permissions, initialize WAL, or wait on write retries.
Recorded session quota is reduced from `usage_event.quota_windows` by provider
and window. Replaying yesterday's session must not display today's unrelated
global ledger as if it belonged to that session.

`/quota` deliberately reads the current account ledger, independently of replay.
An unavailable ledger reports unavailable; a missing allowance is unknown, not
unbounded. The same read-only path serves `cambium quota status`.

## Test the terminal, not only strings

Pure row tests cover cell width, terminal states, resource visibility, and
replay. PTY tests cover input, immediate resize while editing, paste, F6 with an
unfinished draft, Ctrl-C, and active `/cancel` through a real process boundary. Provider-backed tests run actual coding and read-only
follow-up tasks and check events plus Git artifacts. A canned provider is useful
for deterministic rendering, not proof that the agent can code.

The overhead profiler primes a long session through the production event
projection and `LinearTimeline`/`Transcript` path. It then measures one durable
event draw, one status-only draw, resize, and retained timeline bookkeeping
separately. Those samples measure incremental live work after retained history
exists; they never persist or replay a rendered screen image.

Run focused checks before broader changes:

```sh
python -m pytest -o addopts='' tests/scenarios/test_tui_screen.py tests/scenarios/test_resource_projection.py
python -m pytest -o addopts='' tests/scenarios/test_tui_live_pty.py
python -m pytest -o addopts='' -m acceptance tests/acceptance/test_live_frontends.py
```

The last command uses configured provider credentials and consumes real tokens.
Its scratch repository is outside the project checkout. It uses ordinary
provider selection and credential handling, not a copied single-provider store.

## Source

[Controller](../../src/cambium/tui.py),
[renderer](../../src/cambium/tui_screen.py),
[terminal capabilities](../../src/cambium/terminal.py),
[event projection](../../src/cambium/observability.py),
[quota reader](../../src/cambium/provider_scheduler.py).
