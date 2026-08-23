# Persistent interactive terminal cockpit

**Status:** implemented operator contract.

`cambium tui` owns one user-visible CAST lineage across many prompts and keeps
one full-screen operator cockpit alive for the complete terminal session.
Each prompt still receives an isolated supervisor transaction and worktree, but
the newest immutable context checkpoint becomes the next prompt's branch head.

```text
interactive root
│
├── turn-0001
│      └── checkpoint C1  (provider/model lease + CAST trunk)
│
├── turn-0002
│      ├── copied C1
│      └── checkpoint C2
│
└── turn-0003
       ├── copied C2
       └── checkpoint C3

Visible semantic branch: C1 ──fork──> C2 ──fork──> C3
```

The exact checkpoint fork is attempted first. The same checkpoint is also
supplied as a semantic-summary fallback. A compatible provider/model continues
with the byte-stable cached trunk. An incompatible provider can reconstruct the
semantic trunk under a fresh provider-specific head without claiming a KV-cache
hit.

## Running

```bash
PYTHONPATH=src python -m cambium tui --repo . --auto
```

Use a stable root to reopen the same lineage:

```bash
PYTHONPATH=src python -m cambium tui \
  --repo . \
  --session-dir ~/.local/state/cambium/my-project \
  --auto
```

The frontend stores content-free lineage metadata at:

```text
<interactive-root>/.cambium/interactive.json
```

Raw events, results, worktrees, and checkpoints remain inside the individual
turn leaves. A corrupt or missing checkpoint fails closed instead of silently
starting an unrelated context.

## Cockpit layout

On a normal wide terminal, the persistent alternate screen has two main panes:

```text
┌ Cambium · running ───────────────────────────────────────────────────────┐
│ session / branch / provider-model lease                                 │
├ transcript and Markdown ───────────────────┬ agents / context / usage ──┤
│ YOU                                        │ M root active               │
│   inspect the routing code                 │   codex/gpt-5.6             │
│                                            │   42k tok · 51 out/s        │
│ CAMBIUM                                    ├ CONTEXT                     │
│   ## Findings                              │ epoch 7 · segments 5        │
│   - ...                                    │ trunk ≈18k · raw ≈900       │
│                                            ├ SESSION USAGE               │
│ TOOL                                       │ calls / in / out / cache    │
│   read_batch: ok · 12ms                    │ cost / throughput           │
├────────────────────────────────────────────┴─────────────────────────────┤
│ /help commands · <<< multiline >>> · Ctrl-C cancels turn · /exit        │
│ ›                                                                         │
└──────────────────────────────────────────────────────────────────────────┘
```

Narrow terminals use a compact stacked view. Both layouts are pure renderings of
immutable frontend state and the event-sourced `ObservabilityState` projection.
The TUI never reaches into live workers.

The cockpit displays:

- the persistent branch generation, turn, provider/model lease, and epoch;
- user prompts and final model Markdown;
- selected tool, child, checkpoint, merge, and failure events;
- main and sub-agent state, provider/model, tokens, output tokens/second, and
  current tool;
- exact prompt tokens when reported by the provider;
- approximate serialized trunk and raw-tail token sizes otherwise;
- immutable summary-segment count and checkpoint identity;
- current-turn and cumulative calls, input/output/cache tokens, throughput, and
  estimated cost.

Semantic colors distinguish user, model, tool, system, active, successful, and
failed state. Color is disabled for `NO_COLOR`, `TERM=dumb`, non-TTY output, and
machine-readable interfaces. Provider-controlled escape sequences are stripped
before rendering.

## Commands

```text
/help       command help
/status     branch, context, agents, and usage
/dashboard  explain the visible live cockpit
/events     recent durable event summaries
/model      current provider/model lease
/session    persistent root and lease
/context    trunk, tail, checkpoint, and epoch
/usage      cumulative tokens, throughput, calls, and cost
/agents     main/sub-agent lifecycle table
/new        begin a fresh context lineage; old artifacts remain durable
/clear      clear only the visible cockpit transcript
/exit       close the frontend
```

Multiline prompts start with `<<<` on a line by itself and end with `>>>`.
Native terminals retain readline editing and private history at
`<interactive-root>/.cambium/tui_history` (1,000 entries, mode `0600`). Cambium
owns only the screen layout and submitted immutable prompt.

## Cancellation

While a turn is active, Ctrl-C cancels that turn and returns to the cockpit.
Cancellation propagates through the canonical supervisor task. The cancelled
leaf remains auditable but cannot advance the branch head, so the next prompt
resumes from the last successfully published checkpoint. Ctrl-C while waiting
for input retains ordinary terminal interrupt behavior.

## Non-TTY behavior

Pipes, redirected output, and injected test streams use the deterministic
line-oriented adapter. They do not receive cursor movement, colors, or an
alternate screen. This preserves scripting compatibility and keeps the TUI a
presentation concern rather than a second runtime.

## Correctness boundary

1. `InteractiveSession` is the single writer for the user-visible branch head.
2. Every prompt is one isolated supervisor leaf and publication transaction.
3. The adapter adds only validated `context_fork` and `summary_trunk_ref`
   descriptors.
4. Provider resolution, OAuth, quotas, workers, tools, events, and Git
   publication remain owned by the canonical runtime.
5. The cockpit folds events and snapshots; it cannot mutate runtime state.
6. `/new` advances the frontend branch generation but does not delete old
   artifacts.
7. Closing the TUI restores the terminal and leaves completed durable session
   data intact.
