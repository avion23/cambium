# Terminal interface

**Status:** implemented display/input contract. Session persistence and context
continuation are documented in [interactive lifecycle](interactive-tui.md).

## Responsibilities

The frontend displays evidence and accepts operator input. It does not own worker
state, provider scheduling, quota reservations, or artifact publication.
`observability.py` reduces events; `tui.py` controls the interactive session;
`tui_screen.py` renders bounded terminal rows. Keep terminal escape handling at
that boundary rather than embedding it in the worker protocol.

The important information is the current task, useful output, current activity,
provider/model, resource consumption, and whether the result was accepted.
Diagnostic event names and full checkpoints belong behind inspection commands,
not repeated in every status row.

## Layout

The conversation and input remain primary. At 100 columns or more the cockpit
uses a full 32-column operator rail. At 80–99 columns it uses a compact lineage
rail; below that it omits the rail. Terminal height also limits visible rows.

The full rail shows agent/context state and a compact **RESOURCES** block:
output tokens and end-to-end output rate, cached/input share, the latest
provider-reported cache `HIT`/`MISS`, calls, and estimated cost. Known provider
windows appear under **QUOTA** when space allows.
Resource rows retain space in a short full-width terminal; omitted detail points
to `/agents`, `/context`, or `/quota`.

When space permits, each full-width lane shows provider/model and one compact
activity/status row. Crowded rails keep the selected parent and live children
before older terminal rows, collapse detail before hiding lanes, and show an
explicit omitted count with `/agents`. The duplicate footer usage row is hidden
by default; `/detail` reveals it without changing the agent state.
A suspended parent says `waiting for children` and becomes active on resume.
The rail does not repeat streamed response text or reserve empty phase/tail/tool/
duration rows. CAST includes a proportional block strip:

```text
H██ S▓▓▓▓▓ R░░
```

`H` is the stable head, `S` the semantic-only trunk, and `R` the raw tail. The
bar is a size visualization, not a cache-hit claim. Exact byte counts,
checkpoint paths, and segment details remain available through `/context`.
Conversation history and narrow layouts remain usable without color or the rail.

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
| `PROVIDER · waiting` | request sent; no model output yet |
| `THINKING` | provider is reporting reasoning-phase progress; reasoning text is not shown |
| `STREAMING` | user-visible model output is arriving, with output tokens/s |
| `TOOL` | a tool is in flight; the row names it and its elapsed time |
| `CHILDREN · waiting` | parent is suspended on child results |
| `COOLDOWN` | provider capacity/rate limit is delaying the next attempt |
| `stalled` / `silent` / `no output` | the phase revision or output stopped changing for the stall threshold |

The worker heartbeat carries a monotonic phase revision so active reasoning does
not look stalled even though private reasoning text is intentionally absent.
Tool output and visible streaming text also reset the progress clock.

## Markdown and color

Rich is a normal Cambium dependency and is the single Markdown parser/theme for
one-shot/REPL output and the cockpit. Headings use distinct cyan/blue/magenta/
green/yellow levels; code is yellow/cyan, tool output magenta, provider waits
blue, reasoning magenta, streaming/success green, and failures red. Code blocks
use compact bordered panels and block quotes use a colored rule. Narrow tables
retain the existing width-safe fallback so cells are not silently clipped.

The renderer sanitizes model/provider text before Rich sees it. Cambium does not
shell out to a second Markdown TUI; the live cockpit needs in-process width,
scrollback, input, and event ownership.

## Input and controls

A resize keeps the unfinished input draft intact. Unicode wide and
combining characters count by terminal cells, not Python string length.
Bracketed paste and multi-line input must not accidentally execute embedded
control sequences as commands.

Ctrl-C cancels a running turn and exits when idle. `/cancel` also works while a
turn is active. Operator commands must not be deferred behind a queued model
prompt when they are intended to interrupt or inspect running work.

On POSIX, `terminal_input.py` owns input on the same asyncio loop as the cockpit.
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
readable without cockpit escape sequences.

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
| `/detail` | Toggle additional cockpit detail |
| `/cancel` | Cancel the active turn |
| `/exit` | Leave the frontend |

CLI `--help` and the controller's command handling remain authoritative for
arguments. `/dashboard` is a compatibility response: the persistent cockpit is
already the live dashboard, not a second UI mode.

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

Run focused checks before broader changes:

```sh
python -m pytest -o addopts='' tests/scenarios/test_tui_screen.py tests/scenarios/test_tui_rail_detail.py tests/scenarios/test_resource_projection.py
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
