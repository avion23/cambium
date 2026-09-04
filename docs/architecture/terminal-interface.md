# Terminal interface

**Status:** persistent interactive branch and event-sourced dashboard implemented.
Source and tests remain authoritative.

Interactive-session lifecycle: see [`interactive-tui.md`](interactive-tui.md).
Subagent semantics: see [`subagents.md`](subagents.md).

## Architecture

Cambium separates command ownership from observability:

```text
user prompt
    |
    v
InteractiveSession single writer
    |
    +--> isolated supervisor leaf turn-0001
    |        |
    |        +--> immutable context checkpoint C1
    |
    +--> isolated supervisor leaf turn-0002, seeded from C1
             |
             +--> immutable context checkpoint C2

turn-0001/.cambium/events.db ─┐
turn-0002/.cambium/events.db ─┼─> supervisor.read_events(root)
root/.cambium/events.db      ─┘       |
                                     v
                         stable session timeline -> TUI / monitor / JSON
```

One TUI invocation owns one semantic branch. Each prompt still runs through the
canonical isolated supervisor/worktree boundary. The newest immutable checkpoint
is copied into the next turn leaf. Compatible continuation uses an exact
`context_fork`; an incompatible provider may receive the same checkpoint as a
provider-neutral `summary_trunk_ref`.

The private branch manifest is:

```text
<interactive-root>/.cambium/interactive.json
```

Reopening the root restores the current checkpoint, provider/model preference,
branch generation, completed-turn usage, and latest operator projection.

Each turn keeps a local event sequence. `supervisor.read_events(root)` orders the
root and turn stores and renumbers them into one session-level timeline. The TUI
and monitor share this read boundary. Aggregation never rewrites archived
results.

## Running it

```bash
PYTHONPATH=src python -m cambium tui --repo . --auto
PYTHONPATH=src python -m cambium tui --repo . -c --auto
PYTHONPATH=src python -m cambium tui --repo . -c SESSION --auto
```

A stable explicit root can be reopened later:

```bash
PYTHONPATH=src python -m cambium tui \
  --repo . \
  --session-dir ~/.local/state/cambium/my-project \
  --auto
```

## Layout

At twelve or more rows, the cockpit paints one deterministic two-pane frame in
the terminal primary buffer.

```text
┌ conversation / status / activity ┬ OPERATOR RAIL ┐
│                                  │ LANES         │
│                                  │ CONTEXT       │
│                                  │ USAGE         │
│                                  │ QUOTA         │
│ input                            │ RECENT        │
└──────────────────────────────────┴───────────────┘
```

The operator rail is responsive:

| Width | Rail |
| --- | --- |
| `>=100` | Full 32-column text and glyph tree |
| `80-99` | Compact six-column glyph overview |
| `<80` | Hidden; conversation gets the width |

A terminal shorter than twelve rows uses bounded unframed output. A resize
invalidates geometry and repaints the full frame, then restores the input row.

The full rail shows subagent parentage, provider/model, lifecycle, context
lineage, usage, quota, and recent events. The compact rail is deliberately an
overview; detailed inspection remains available through `/agents`, `/events`,
and the full layout.

The standalone monitor uses the same event projection but a separate full-frame
renderer. Its boxes and fixed columns are measured in terminal cells, so wide
CJK characters and combining marks do not shift borders or neighbouring fields.
It enters alternate-screen mode only on a TTY whose `TERM` is not `dumb`.

## TUI best-practice checklist

| Practice | Cambium status | Contract |
| --- | --- | --- |
| Preserve normal terminal scrollback | Applied | Primary buffer, not alternate-screen ownership, in the interactive cockpit |
| Keep input location predictable | Applied | Stable input row after event updates and resize |
| Use deterministic layout breakpoints | Applied | Three width bands; no content-dependent geometry |
| Degrade conversation-first | Applied | Rail shrinks, then disappears |
| Measure terminal cells, not code points | Applied | Cockpit and monitor clip/pad wide and combining Unicode by display width |
| Separate color preference from cursor capability | Applied | `NO_COLOR` suppresses styling; standalone screen mode depends on TTY/`TERM` capability |
| Do not encode state by color alone | Applied | Text and glyphs duplicate semantic color |
| Sanitize untrusted terminal text | Applied | One shared boundary removes complete CSI/OSC sequences, exposes bidi-format controls, and normalizes Unicode line separators |
| Bound every dynamic region | Applied | Rail, activity, recent events, and transcript rows |
| Make resize atomic | Applied | Full-frame repaint rather than incremental geometry edits |
| Drive UI from durable state | Applied | Event reducer and replay, not widget-local authority |
| Support reconnect | Applied | Persistent interactive manifest and turn stores |
| Provide non-TTY and no-color output | Applied | Deterministic line/JSON output; styling is optional |
| Make cancellation observable | Applied | Active-turn cancel acknowledgement and return to input |
| Make subagents inspectable | Applied, compact view limited | Full rail and `/agents`; glyph rail is overview only |
| Avoid layout jitter | Applied | Fixed section ordering and integer duration formatting |
| Keep commands discoverable | Applied | `/help`, command aliases, and explicit multiline mode |

This checklist describes intended behavior and focused coverage, not a claim
that the interface is finished. The six-column rail remains hard to discover;
glyphs cannot carry full text detail. The TUI also still uses its own event
projection rather than sharing every semantic field with BranchState. Text
inspection commands are the accessible fallback. Real-provider coding and
follow-up history retrieval are covered by `test_live_frontends.py`, separately
from renderer unit tests.

## Activity ticker

Heartbeat phase is one of `waiting`, `thinking`, or `streaming`. The latest
sanitized tail is bounded and replaces the previous tail instead of appending
unbounded output.

Displayed durations are regular:

- below one second: integer milliseconds;
- one second and above: whole seconds;
- rates: one decimal place.

This keeps columns stable while activity changes.

## Commands

```text
/help       command help
/status     compact branch status
/dashboard  repaint the dashboard
/events     recent durable events
/usage      completed usage plus the active turn
/agents     main/subagent lifecycle and provider/model rows
/context    checkpoint, epoch, summary trunk, and raw tail
/session    interactive root, branch generation, lease, and checkpoint
/branches   list persistent branches
/fork       fork the current context branch
/compact    compact the active context
/model      inspect or change provider/model preference
/quota      provider quota state
/cancel     cancel the active turn
/new        start a fresh semantic branch without deleting old artifacts
/clear      clear visible transcript
/exit       close the frontend
```

While a turn is running, `/status`, `/usage`, `/agents`, `/events` and the other
read-only inspection commands execute immediately. `/cancel`, `!cancel` and
Ctrl-C cancel the active turn. Ordinary prompts and branch/model mutations are
queued. Explicit inspection forces a repaint so the live-only rendering path
cannot hide its output.

Live usage is completed totals plus the current snapshot, not repeated additions
to the accumulator. A finished turn is counted once. This is covered by
`test_tui_live_usage.py`; blocked-provider inspection/cancellation is exercised
through a real PTY in `test_tui_live_pty.py`.

Multiline input:

```text
<<<
first line
second line
>>>
```

Bracketed paste and trailing-backslash continuation preserve multiline input
without turning pasted lines into commands.

## Dashboard fields

Each agent row can expose:

- main or subagent role and parent;
- queued, starting, active, merging, succeeded, failed, cancelled, or exited;
- generation, turn, and context epoch;
- provider/model and latest tool;
- exact, semantic, fresh, or unknown lineage;
- calls and summary calls;
- input, output, cached, and total tokens;
- latest output tokens per second;
- estimated cost.

The context row exposes provider-reported prompt tokens when available, plus
summary-trunk bytes/estimated tokens, segment count, raw-tail size, message
count, checkpoint, epoch, and collapsed tool-error count. Byte-derived token
estimates are marked approximate.

Colors aid scanning but are redundant. `NO_COLOR` disables styling without
disabling interactive cursor handling. Non-TTY and JSON output remain free of
ANSI controls. The standalone monitor treats `TERM=dumb` as lacking screen
capabilities and uses line-oriented frames rather than alternate-screen control
sequences.

## Persistence and crash behavior

The frontend is a single writer over the branch manifest:

```text
prepare turn
    -> observe durable checkpoint/result events
    -> complete turn
    -> atomically publish manifest
```

A crash cannot partially replace the manifest. A turn directory allocated
before a crash is skipped rather than reused. A failed turn keeps the previous
valid context seed. `/new` advances the branch generation without deleting
earlier artifacts.

## Standalone monitoring

`cambium monitor [SESSION]` is a read-only projection over a supervisor leaf or
interactive root. It can render a live frame, static text, or JSON. Closing a
monitor never mutates or cancels the runtime.

## Correctness properties

- Frontends never mutate worker or provider state directly.
- Checkpoint publication is ordered by one interactive-session writer.
- Exact provider reuse and cold semantic reuse are explicitly distinguished.
- Previous checkpoint files are immutable.
- Replaying the same event sequence yields the same operator view.
- Cached tokens remain a subset of input and are not added twice.
- Terminal control sequences from model/provider output are removed as complete
  sequences at one shared boundary.
- Bidirectional-format controls remain visible as escaped text rather than
  reordering neighbouring paths, diagnostics, or source snippets.
- Framed rows have deterministic terminal-cell widths.
- Non-TTY behavior remains deterministic and line-oriented.

## Source and executable proof

- Shared sanitation, cursor capability, cell width, clipping, and padding:
  `src/cambium/terminal.py`
- Frame construction and breakpoints: `_cockpit_frame_lines`, `_rail_width`,
  `_rail_rows`, and `_side_sections` in `src/cambium/tui_screen.py`
- Standalone cell-aware frame and alternate-screen gate:
  `render_dashboard` and `AnsiDashboard` in `src/cambium/monitor.py`
- Heartbeat phase state and emission: `AgentProgress` and `_heartbeat_loop` in
  `src/cambium/worker.py`
- Activity heartbeat and ticker rendering: `ActivityState._observe_heartbeat`,
  `_activity_status`, `_activity_row`, and `render_cockpit` in
  `src/cambium/tui_screen.py`
- Session command loop and input handling: `src/cambium/tui.py`
- Durable operator reducer: `src/cambium/observability.py`
- Interactive manifest ownership: `src/cambium/interactive.py`
- Width proof: `test_side_sections_are_width_safe` in
  `tests/scenarios/test_tui_screen.py`
- Shared terminal/monitor capability proof:
  `tests/scenarios/test_terminal_presentation.py`
- Resize, scrollback, paste, cancellation, shutdown, and reconnect proofs:
  named `test_*` functions in `tests/scenarios/test_tui_*.py`
