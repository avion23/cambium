# Persistent interactive terminal cockpit

**Status:** implemented operator contract; source and tests remain authoritative.

## Interactive session

`cambium tui` keeps one persistent CAST branch across operator turns. Each turn
gets a fresh worker leaf under `turn-NNNN`; a successful immutable checkpoint
seeds the next turn. `-c`/`--continue` reopens the latest or a named durable
interactive root. Without it, Cambium allocates a new root.

The frontend is persistent. Worker execution remains isolated:

```text
interactive root
    |
    +--> turn-0001 / worker worktree / checkpoint C1
    |
    +--> turn-0002 / worker worktree / checkpoint C2
    |
    +--> turn-0003 / worker worktree / checkpoint C3
```

## Turn lifecycle

1. The single interactive-session writer allocates a fresh turn leaf.
2. The turn starts from the latest compatible checkpoint.
3. A worker executes inside the canonical supervisor/worktree boundary.
4. Checkpoint and result events are observed durably.
5. Successful finalization atomically advances the branch manifest.
6. Failure or cancellation keeps the previous valid context seed.

Checkpoint reuse requires compatible workspace and provider-cache identity. On
incompatibility, the runtime uses provider-neutral semantic summaries when
legal; otherwise it starts fresh. No frontend path edits an existing checkpoint.

## Subagents in the cockpit

Subagents are supervised worker tasks, not provider-native agents. The operator
rail projects their task-tree and lifecycle state from durable events:

```text
main
├─ review-routing   ~ active
├─ add-tests        = merging
└─ inspect-tui      ∅ succeeded
```

Lineage glyphs mean:

- `=` exact cache-affine checkpoint lineage;
- `~` semantic-summary reuse with a fresh provider head;
- `∅` fresh context;
- `?` lineage not yet known.

Lifecycle glyphs are redundant with text in the full rail so color is never the
only signal. The compact rail is an overview; `/agents`, `/events`, and the full
rail are the inspection surfaces.

Subagent workload, provider selection, prompt construction, and join behavior
are defined in [`subagents.md`](subagents.md).

## Layout contract

The cockpit uses the terminal primary buffer so normal scrollback remains
available. At twelve or more rows it renders conversation/status/input on the
left and an operator rail on the right.

| Width | Layout |
| --- | --- |
| `>=100` columns | Full 32-column operator rail |
| `80-99` columns | Compact six-column glyph rail |
| `<80` columns | Conversation-first single pane |

A resize invalidates geometry and repaints one complete deterministic frame.
Terminals shorter than twelve rows use bounded unframed output. Non-TTY output
is line-oriented. `NO_COLOR` suppresses styling; it does not disable cursor
handling on an interactive terminal.

The detailed layout and command behavior are in
[`terminal-interface.md`](terminal-interface.md).

## Input and commands

The input row remains at a stable location after updates and resizes. Bracketed
paste preserves embedded newlines. A trailing backslash continues input on the
next line. Explicit multiline mode is:

```text
<<<
first line
second line
>>>
```

Operator commands include:

```text
/help       command help
/status     compact session status
/dashboard  repaint the dashboard
/events     recent durable events
/usage      completed usage plus the active turn
/agents     main/subagent lifecycle
/context    checkpoint, epoch, trunk, and raw tail
/session    interactive root and branch lease
/branches   persistent branch list
/fork       fork the current checkpoint
/compact    compact the active context
/model      inspect or change provider/model preference
/quota      provider quota windows
/cancel     cancel the active turn
/new        start a fresh semantic branch
/clear      clear visible transcript
/exit       close the frontend
```

`/cancel`, `!cancel` and Ctrl-C cancel an active turn and return to the prompt.
Inspection commands such as `/status`, `/usage`, `/agents` and `/events` execute
immediately while the provider is running. Their output triggers a full repaint;
ordinary activity updates retain the lightweight repaint path. Other prompts and
branch/model changes are queued as follow-ups rather than lost.

Usage includes the active snapshot without adding it to completed totals on each
repaint. The completed turn is accumulated once. PTY coverage is in
`tests/scenarios/test_tui_live_pty.py`; real-provider coding and historical recall
are exercised in `tests/acceptance/test_live_frontends.py`.

## Correctness boundaries

- `InteractiveSession` is the single manifest writer.
- Each operator turn uses one isolated supervisor leaf.
- Durable events, not widget state, drive the operator projection.
- Exact cache reuse and semantic reuse are distinct states.
- Model/tool text is sanitized before terminal rendering.
- A failed turn cannot replace the last successful branch checkpoint.
- Monitor attachment is read-only and cannot cancel or mutate the session.

## Source map

- Interactive branch ownership: `src/cambium/interactive.py`
- Frontend command loop: `src/cambium/tui.py`
- Deterministic frame rendering: `src/cambium/tui_screen.py`
- Event reducer and agent snapshots: `src/cambium/observability.py`
- Session event aggregation: `src/cambium/supervisor.py`
- TTY and PTY behavior: `tests/scenarios/test_tui_*.py`
