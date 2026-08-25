# Persistent interactive terminal cockpit

**Status:** implemented operator contract.

`cambium tui` owns one user-visible CAST lineage across many prompts. Each
prompt still receives an isolated supervisor transaction and worktree, but the
newest immutable context checkpoint becomes the next prompt's branch head. The
frontend is a single writer: a successful turn advances that head, while a
failed or cancelled leaf remains durable without becoming the next seed. The
interactive root is also locked, so two frontends cannot steer the same branch
at once. On a sufficiently tall TTY, the live view is a two-pane cockpit in
the terminal's normal primary buffer; short terminals use the bounded fallback
described below.

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

Each launch without an explicit session target allocates a fresh interactive
root. Continue the latest durable root explicitly with `-c`:

```bash
PYTHONPATH=src python -m cambium tui --repo . -c --auto
```

Continue one session by id (under the repository's session root) or by
directory path:

```bash
PYTHONPATH=src python -m cambium tui --repo . -c SESSION_ID --auto
```

Use a stable root to reopen the same lineage directly:

```bash
PYTHONPATH=src python -m cambium tui \
  --repo . \
  --session-dir ~/.local/state/cambium/my-project \
  --auto
```

`-c` without a value selects the newest reconnectable interactive root. A
missing or non-reconnectable target is an error; it never silently starts a
fresh session. On continuation, Cambium replays the durable turn event stores,
restores the latest valid checkpoint/branch head, and shows the resumed turn,
epoch, and checkpoint. A `session.lock` under the interactive `.cambium`
directory prevents concurrent owners; a kernel-released or stale lock can be
reclaimed, but a live owner fails closed.

The frontend stores content-free lineage metadata at:

```text
<interactive-root>/.cambium/interactive.json
```

Raw events, results, worktrees, and checkpoints remain inside the individual
turn leaves; `supervisor.read_events` and `monitor` aggregate those event
stores into one session timeline with stable read sequence numbers. A corrupt
or missing checkpoint fails closed instead of silently starting an unrelated
context. Reconnect requires durable events or a checkpoint, so an abandoned
empty allocation is not mistaken for a session.

## Cockpit layout and scrollback

For a TTY with at least twelve rows, the fixed frame is arranged top-to-bottom
as conversation, live status, and input:

```text
┌ Cambium · conversation · running ┐
│ YOU / CAMBIUM / TOOL transcript   │  conversation pane
│ streamed or completed Markdown    │
├───────────────────────────────────┤
│ provider=codex · model=gpt-5.6    │
│ turn=2 · tokens=42k · cost=$...   │  live status pane
│ agents=1 active · ...             │
│ STREAMING · tool errors=0 · ckpt  │
│ input ›                           │  input row
└───────────────────────────────────┘
```

The status pane reports provider/model, turn, tokens (including input/output
and cached counts when available), throughput, cost, active agents and their
states, the collapsed tool-error count, session/checkpoint identity, and
epoch. While a turn runs, activity is `WAITING` while thinking, `STREAMING`
when assistant output arrives, or `IDLE` between turns; tool/cooldown details
can replace the live activity text. The final status is `DONE` or `ERROR`, and
cancelled turns return to `IDLE`.

The conversation renderer is width-bounded and sanitizes model text. Its safe
Markdown subset renders headings, lists, quotes, rules, pipe-delimited tables,
inline **bold**/**italic**/`code`, and fenced code blocks. Long words and wide
Unicode characters are wrapped by display width rather than allowed to break
the pane border. Consecutive successful calls to one tool use a compact count;
routine failed tool events collapse into one per-turn
`tool errors: N (last: TOOL …)` counter. `v` expands available tool
command/output details, while a terminal task failure is shown as one
consolidated failure block with its useful context.

The fixed frame is written to the normal primary buffer, never an alternate
screen. Conversation changes append a fresh frame so terminal scrollback keeps
the interaction; status-only changes repaint the bottom pane in place. While
readline owns the input row, draws are coalesced so they cannot overwrite
edited text. Turn completion forces the final frame draw and flush even when
input is still pending. `/clear` clears Cambium's bounded local transcript for
future draws; it cannot erase scrollback already owned by the terminal.

If the terminal is shorter than twelve rows, the framed borders are removed and
the cockpit falls back to width-bounded conversation rows followed by the
status rows. Input remains a normal prompt rather than a fixed framed row.
Both renderers are pure views of immutable frontend state and the event-sourced
`ObservabilityState` projection; the TUI never reaches into live workers.

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
  estimated cost;
- streamed assistant/tool text as bounded Markdown-safe output while a turn is
  in flight, followed by the final Markdown response;
- successful consecutive calls to the same tool as one compact line with a
  repeat count and last duration; routine failed tool events use a per-turn
  collapsed counter, and `v` expands available command/output details;
- one consolidated failure block per failed task/turn, containing the task,
  cause, and the most useful preceding tool/provider/timeout context.

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
/model      show the current lease; `/model MODEL` persists a configured model preference
/branches   list durable branch heads, epochs, and checkpoint references
/fork       start a branch from the current successful checkpoint
/compact    flush semantic context and perform a CAST K0 rollover when eligible
/session    persistent root and lease
/context    trunk, tail, checkpoint, and epoch
/usage      cumulative tokens, throughput, calls, and cost
/agents     main/sub-agent lifecycle table
/new        begin a fresh context lineage; old artifacts remain durable
/clear      clear only the local transcript (terminal scrollback remains)
/cancel     report whether a turn is active (active cancellation uses `!cancel`)
/exit       close the frontend

v           toggle expanded command/output details for tool entries
```

Multiline prompts start with `<<<` on a line by itself and end with `>>>`.
Native terminals retain readline editing and private history at
`<interactive-root>/.cambium/tui_history` (1,000 entries, mode `0600`). Cambium
owns only the appended presentation rows and the submitted immutable prompt;
the terminal owns the scrollback buffer.

## Cancellation

The input reader stays live while a turn is active. A normal prompt entered
during that turn is appended to a FIFO queue, appears as `queued: ...`, and is
started only after the current turn completes. `!cancel` is the immediate
steering command; it cancels the active canonical supervisor task rather than
being queued. Ctrl-C has the same active-turn behavior. `!cancel` or Ctrl-C
while waiting for input retains ordinary terminal interrupt behavior.

Cancellation propagates through the canonical supervisor task. The cancelled
leaf remains auditable but cannot advance the branch head, so the next prompt
resumes from the last successfully published checkpoint. Queued prompts are
not lost when a turn is cancelled; they run from that last successful head.

`/compact` only rolls a summary-only checkpoint: it refuses a checkpoint with a
raw tail, folds immutable semantic segments into one CAST K0 entry, writes the
next content-addressed epoch, and records the rollover without mutating the
old checkpoint.

## Non-TTY behavior

Pipes, redirected output, and injected test streams use the deterministic
line-oriented adapter. They do not receive cursor movement, colors, or an
alternate screen. This preserves scripting compatibility and keeps the TUI a
presentation concern rather than a second runtime.

## Correctness boundary

1. `InteractiveSession` is the single writer for the user-visible branch head.
2. Every prompt is one isolated supervisor leaf and publication transaction.
3. Reconnect is selected from a durable manifest/checkpoint and guarded by one
   session lock; it never invents a missing context.
4. The adapter adds only validated `context_fork` and `summary_trunk_ref`
   descriptors.
5. Provider resolution, OAuth, quotas, workers, tools, events, and Git
   publication remain owned by the canonical runtime.
6. The cockpit folds events and snapshots; it cannot mutate runtime state.
7. `/new` advances the frontend branch generation but does not delete old
   artifacts.
8. `/fork` changes only the visible branch generation; `/branches` discovers
   durable checkpoint heads; `/model` validates and persists a configured
   preference for subsequent turns.
9. Closing the TUI leaves completed durable session data and native terminal
   scrollback intact without entering or leaving an alternate screen.
