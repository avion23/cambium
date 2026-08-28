# Cambium documentation

Source and tests define current behavior.

## Active contracts

- [`architecture/architecture.md`](architecture/architecture.md) — runtime,
  ownership, publication, and concurrency.
- [`architecture/subagents.md`](architecture/subagents.md) — task decomposition,
  child prompts, same-provider cache-affine children, cross-provider semantic
  children, fork-join lifecycle, and artifact integration.
- [`architecture/context-engine.md`](architecture/context-engine.md) —
  append-only summary trunks, epochs, forks, resume, and accounting.
- [`architecture/events.md`](architecture/events.md) — durable event-kind
  reference.
- [`architecture/operations.md`](architecture/operations.md) — operator
  lifecycle, admission, recovery, and success behavior.
- [`architecture/provider-routing.md`](architecture/provider-routing.md) —
  provider feasibility, leases, child-provider selection, cache affinity, and
  debt.
- [`architecture/terminal-interface.md`](architecture/terminal-interface.md) —
  persistent interactive TUI, layout contract, event-sourced dashboard, and
  monitor.
- [`architecture/interactive-tui.md`](architecture/interactive-tui.md) —
  interactive branch lifecycle, subagent visibility, and commands.
- [`architecture/optimization.md`](architecture/optimization.md) — DSPy and
  OpenCode-data gates.

## Supporting notes

The small set of retained engineering standards and provider setup notes is
indexed in [`research/README.md`](research/README.md). Superseded reviews,
roadmap snapshots, and run records remain available through Git history.
