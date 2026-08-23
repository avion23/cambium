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
/session    persistent root path
/context    current lineage/checkpoint/provider
/usage      cumulative frontend usage
/agents     latest main/sub-agent table
/new        fresh context lineage; old artifacts remain durable
/exit       close the frontend
```

Multiline prompts start with `<<<` on a line by itself and end with `>>>`.

## TUI contents

While a turn is active the alternate-screen dashboard displays:

- main and sub-agent lifecycle;
- provider and model per agent;
- input, output, cached, and total tokens;
- output tokens per second;
- summary-call count and estimated cost;
- context epoch, immutable summary-trunk size, and raw-tail size;
- current tool and recent durable events.

After a turn, Cambium leaves the alternate screen, renders the final model
summary as terminal Markdown, prints turn usage, and prints cumulative usage for
the interactive frontend. Normal terminal scrollback is therefore retained for
completed results.

## Correctness boundary

The adapter is deliberately thin:

1. first turns use the ordinary `oneshot.run_oneshot` path;
2. continuation turns use the same oneshot provider resolution and plan builder;
3. the adapter adds only the already-validated `context_fork` and
   `summary_trunk_ref` descriptors;
4. execution still belongs to the canonical supervisor;
5. every turn has its own event store and publication transaction.

This avoids weakening the existing session-reuse guard merely to obtain a chat
loop. The user sees one long branch, while the supervisor retains isolated,
replayable turn transactions.
