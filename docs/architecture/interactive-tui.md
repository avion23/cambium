# Interactive branch lifecycle

**Status:** implemented. This page owns session/context behavior;
[terminal interface](terminal-interface.md) owns layout, input, and commands.

## One conversation, durable turns

`InteractiveSession` attaches successive prompts to a durable interactive root.
Each turn has its own supervisor run and event store, while the interactive
manifest records the accepted continuation checkpoint and branch identity.
The TUI is a frontend to that session, not a second agent runtime.

A successful turn can seed the next turn from its compatible checkpoint or
semantic continuation. A failed turn does not silently replace the accepted
head. Turn number, worker generation, context epoch, and Git head are different
identities; display and recovery code must not infer one from another.

The normal model loop can start directly with a useful tool call. A plan is
available when it helps, not a required extra model turn for every prompt.

## Code state and context state

The supervisor validates and publishes mutating work. A clean read-only result
needs no commit. Later prompts must see accepted artifacts as well as the
correct context continuation; remembering a claimed edit is not equivalent to
having the edit in the current Git tree.

`/fork` branches the conversation from the available durable state. `/branches`
inspects checkpoint heads. These are conversation operations, not arbitrary
Git branch mutation commands. `/compact` uses the existing context mechanism;
it does not make old raw evidence disappear.

`/model` lists or changes provider/model preference for subsequent work.
Existing context is subject to the explicit compatibility rules in
[context branches](context-branches.md). A changed provider cannot inherit an
incompatible exact prefix or claim the old provider's cache.

## Exit, cancellation, and reconnect

An interactive root has one active frontend owner. Reconnect reconstructs
accepted state from the manifest and durable turn artifacts, not widget memory.
The event stream remains usable by monitoring and history inspection.

During a running turn, Ctrl-C or `/cancel` requests cancellation. At idle,
Ctrl-C exits cleanly. Queued prompts are distinct from cancellation and from
status commands. Terminal resizing must not duplicate or swallow the input
line; neither rendering nor a quota query should block cancellation behind a
writable ledger retry.

## Observation

`ObservabilityState` reduces the current turn's events. The TUI combines that
snapshot with completed-turn cumulative usage without double counting.
`branch_history` can reopen recorded evidence across the interactive root's
turn directories. Its reads do not rerun tools or reconstruct hidden reasoning.

The canonical `branch_state.py` inspection projection also exists, but complete
agreement between all model/operator state surfaces is separate integration
work. Do not call a new frame or shared work ledger implemented solely because
this frontend retains a checkpoint.

## Source and checks

- [Interactive lifecycle](../../src/cambium/interactive.py),
  [TUI controller](../../src/cambium/tui.py)
- [Real coding, read-only continuation and history inspection](../../tests/acceptance/test_live_frontends.py),
  [PTY input/resize/cancel tests](../../tests/scenarios/test_tui_live_pty.py),
  [live usage projection tests](../../tests/scenarios/test_tui_live_usage.py)
