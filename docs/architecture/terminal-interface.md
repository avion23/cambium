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
output tokens and end-to-end output rate, input and cached tokens, calls, and
estimated cost. Known provider windows appear under **QUOTA** when space allows.
Resource rows retain space in a short full-width terminal; omitted detail points
to `/agents`, `/context`, or `/quota`.

While an agent is active, selected-run detail keeps a stable row allocation so
activity/tool changes do not make context jump around. Terminal states collapse
the empty live-detail rows to one status line. Conversation history and narrow
layouts must remain usable without color or the rail.

The output rate is generated output divided by call wall time, not total
prompt-plus-output tokens. Missing output counts are unknown. Zero estimated
cost is displayed numerically unless an explicit billing classification supports
a free/subscription label. Neither label describes remaining token quota.

## Input and controls

A resize keeps the native editor's live input line intact. Unicode wide and
combining characters count by terminal cells, not Python string length.
Bracketed paste and multi-line input must not accidentally execute embedded
control sequences as commands.

Ctrl-C cancels a running turn and exits when idle. `/cancel` also works while a
turn is active. Operator commands must not be deferred behind a queued model
prompt when they are intended to interrupt or inspect running work.

Native line editing owns its resize signal on the input thread. On POSIX the
controller masks SIGWINCH outside that owner and restores the original mask at
exit; the asyncio wakeup path still updates the cockpit. This avoids libedit
resizing global editor buffers concurrently with character insertion. The
renderer also never reads another thread's live libedit buffer: even its string
conversion can race with typing. Conversation and status cells update in place;
destructive full-frame/input repaint waits for the line to complete. This can
defer the full geometry refresh while editing, but preserves native input and
keeps cancellation/results visible. The PTY suite stresses typing and repeated
resizes, not just idle repaint.

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
replay. PTY tests cover input, resize, Ctrl-C, and active `/cancel` through a real
process boundary. Provider-backed tests run actual coding and read-only
follow-up tasks and check events plus Git artifacts. A canned provider is useful
for deterministic rendering, not proof that the agent can code.

Run focused checks before broader changes:

```sh
python -m pytest -o addopts='' tests/scenarios/test_tui_screen.py tests/scenarios/test_tui_rail_detail.py tests/scenarios/test_resource_projection.py
python -m pytest -o addopts='' tests/scenarios/test_tui_live_pty.py
python -m pytest -o addopts='' -m acceptance tests/acceptance/test_live_tui_coding.py
```

The last command uses configured provider credentials and consumes real tokens.
Its scratch repository and session config are outside the project checkout.

## Source

[Controller](../../src/cambium/tui.py),
[renderer](../../src/cambium/tui_screen.py),
[terminal capabilities](../../src/cambium/terminal.py),
[event projection](../../src/cambium/observability.py),
[quota reader](../../src/cambium/provider_scheduler.py).
