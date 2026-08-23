# Persistent interactive TUI

**Status:** implemented operator slice.

`cambium tui` owns one user-visible context lineage across many submitted
prompts. It does not reopen a completed supervisor session directory. Instead,
each prompt receives an isolated turn leaf while the newest immutable context
checkpoint is carried forward.

```text
interactive root
│
├── first turn/session leaf
│      └── checkpoint C1  (provider/model lease + CAST trunk)
│
├── turn-0002
│      ├── copied C1
│      └── checkpoint C2
│
└── turn-0003
       ├── copied C2
       └── checkpoint C3

Visible branch: C1 ──fork──> C2 ──fork──> C3
```

The exact checkpoint fork is attempted first. The same checkpoint is also
supplied as a semantic-summary fallback. Therefore a compatible provider/model
continues with the byte-stable cached trunk, while a compatibility change can
still reconstruct the semantic trunk under a fresh provider-specific head.
The adapter never mutates an old checkpoint.

## Running

```bash
PYTHONPATH=src python -m cambium tui --repo . --auto
```

Use an explicit root to reopen the same interactive lineage later:

```bash
PYTHONPATH=src python -m cambium tui \
  --repo . \
  --session-dir .cambium/my-interactive-session \
  --auto
```

The frontend stores only content-free lineage metadata at:

```text
<interactive-root>/.cambium/interactive.json
```

Raw events, results, worktrees, and checkpoints stay inside the individual turn
leaves. A corrupted or missing checkpoint fails closed rather than silently
starting from an unrelated prompt.

## Commands

```text
/help       command help
/session    persistent root path and branch identity
/model      current provider/model lease
/context    current lineage/checkpoint/provider and trunk shape
/usage      cumulative frontend usage
/agents     latest main/sub-agent table
/dashboard  copy the latest full dashboard into normal scrollback
/events     recent durable event summaries
/new        fresh context lineage; old artifacts remain durable
/clear      clear the terminal
/exit       close the frontend
```

Multiline prompts start with `<<<` on a line by itself and end with `>>>`.
Native terminals use the system readline implementation for editing and history.
History is stored privately at
`<interactive-root>/.cambium/tui_history`, limited to 1,000 entries and mode
`0600`.

## Cancellation

While a turn is active, Ctrl-C cancels only that turn and returns to the input
prompt. Cancellation propagates through the canonical supervisor task, so its
worker processes and child tasks receive normal structured cancellation. The
cancelled leaf remains auditable, but it cannot advance the interactive branch
head. The next prompt therefore resumes from the last successfully published
checkpoint rather than from a partial response.

Ctrl-C while the frontend is waiting for input retains the ordinary terminal
interrupt behavior. `/cancel` at an idle prompt explains the active-turn
shortcut rather than pretending that an operation is running.

## TUI contents

While a turn is active the alternate-screen dashboard displays:

- main and sub-agent lifecycle;
- provider and model per agent;
- input, output, cached, and total tokens;
- output tokens per second;
- summary-call count and estimated cost;
- context epoch, immutable summary-trunk size, and raw-tail size;
- current tool and recent durable events.

Semantic colors distinguish active, successful, failed, main-agent, and
sub-agent rows. `NO_COLOR`, `TERM=dumb`, pipes, and machine-readable output
remain free of ANSI decoration.

After a turn, Cambium leaves the alternate screen, renders the final model
summary as terminal Markdown, prints turn usage, and prints cumulative usage for
the interactive frontend. Normal terminal scrollback is therefore retained for
completed results. `/dashboard` provides a static copy of the latest dashboard
when the operator wants the complete view in scrollback.

## Correctness boundary

The adapter is deliberately thin:

1. first turns use the ordinary one-shot provider-resolution and supervisor path;
2. continuation turns use the same plan builder and execution path;
3. the adapter adds only the already-validated `context_fork` and
   `summary_trunk_ref` descriptors;
4. execution still belongs to the canonical supervisor;
5. every turn has its own event store and publication transaction;
6. only a successful turn with a durable context event may publish the next
   interactive branch head;
7. cancelled and failed turns leave the previous branch head unchanged.

This avoids weakening the existing session-reuse guard merely to obtain a chat
loop. The user sees one long branch, while the supervisor retains isolated,
replayable turn transactions.
