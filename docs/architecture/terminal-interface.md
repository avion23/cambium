# Terminal interface

**Status:** persistent interactive branch and event-sourced dashboard implemented.
Source and tests remain authoritative.

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

all leaves --> durable event logs --> ObservabilityState --> TUI / monitor / JSON
```

One TUI invocation owns one semantic branch. Each prompt still runs through the
canonical isolated supervisor/worktree boundary, but the newest immutable
checkpoint is copied into the next turn leaf. A compatible continuation receives
an exact `context_fork`; the same checkpoint is supplied as
`summary_trunk_ref` for a cold provider-neutral fallback.

The interactive root stores a private atomic manifest at:

```text
<interactive-root>/.cambium/interactive.json
```

Reopening the same explicit `--session-dir` restores the current checkpoint,
provider/model lease, branch generation, completed-turn usage, and latest
operator view.

## Running it

```bash
PYTHONPATH=src python -m cambium tui --repo . --auto
```

Use a stable root to reopen the same branch later:

```bash
PYTHONPATH=src python -m cambium tui \
  --repo . \
  --session-dir ~/.local/state/cambium/my-project \
  --auto
```

The normal terminal scrollback contains completed Markdown answers and compact
usage/context lines. While a turn is active, an alternate-screen dashboard
shows live workers and subagents. Leaving the dashboard restores the terminal
before the final answer is printed.

## Commands

```text
/help       command help
/usage      cumulative usage for the current semantic branch
/agents     main/subagent lifecycle and provider/model rows
/context    checkpoint, epoch, summary trunk, and raw tail
/session    interactive root, branch generation, lease, and checkpoint
/new        start a fresh semantic branch without deleting old turn artifacts
/clear      clear the terminal
/exit       exit the frontend
```

Multiline input uses explicit delimiters:

```text
<<<
first line
second line
>>>
```

## Dashboard fields

Each agent row exposes:

- main or subagent role;
- queued, starting, active, merging, succeeded, failed, cancelled, or exited;
- parent, generation, turn, and context epoch;
- provider/model and latest tool;
- calls and summary calls;
- input, output, cached, and total tokens;
- latest output tokens per second;
- estimated cost.

The context row exposes exact prompt tokens when reported, plus summary-trunk
bytes/estimated tokens, segment count, raw-tail size, message count, checkpoint,
and epoch. Byte-derived token estimates are marked approximate.

Colors are semantic: main/cyan, subagents/magenta, active/yellow,
success/green, failure/red, and structural borders/dim cyan. `NO_COLOR`, a dumb
terminal, non-TTY output, and JSON output remain free of ANSI styling.

## Persistence and crash behavior

The frontend is a single writer over the branch manifest. The sequence is:

```text
prepare turn -> observe checkpoint events -> complete turn -> atomically publish manifest
```

A crash cannot partially replace the manifest. A turn directory allocated
before a crash is skipped on the next submission rather than reused. A failed
turn keeps the previous valid context seed. `/new` advances the branch
generation and excludes earlier turn leaves from restored branch totals.

## Standalone monitoring

`cambium monitor [SESSION]` remains a read-only projection over one supervisor
leaf. It can render a live frame, a static text frame, or JSON. Closing a
monitor never mutates or cancels the runtime.

## Correctness properties

- frontends never mutate worker or provider state directly;
- checkpoint publication is ordered by one interactive-session writer;
- exact provider reuse and cold semantic reuse are explicitly distinguished;
- previous checkpoint files are copied immutably, never edited;
- replaying the same event sequence yields the same operator view;
- cached tokens remain a subset of input and are not added twice;
- terminal control characters from model output are sanitized;
- non-TTY behavior remains deterministic and line-oriented.
