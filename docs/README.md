# Cambium documentation

Source and tests define current behavior. Documentation is separated by reader
need rather than accumulated in one design file.

```text
architecture/   why the system has this shape
requirements/   normative invariants (kept beside architecture when tightly coupled)
reference/      exact schemas, values, commands, and source maps
how-to/         task-oriented examples
research/       hypotheses, comparisons, and evaluation protocols
```

## Architecture and requirements

- [`architecture/architecture.md`](architecture/architecture.md) — runtime,
  ownership, publication, and concurrency.
- [`architecture/context-branches.md`](architecture/context-branches.md) — the
  paper-like big vision: recursive branches, cache-aligned trunks, provider
  placement, historical tool recall, and the four distinct structures.
- [`architecture/context-branch-requirements.md`](architecture/context-branch-requirements.md)
  — normative MUST/SHOULD invariants and acceptance scenarios.
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

## Reference

- [`reference/context-branches.md`](reference/context-branches.md) — exact
  `delegate` policy and `branch_history` tool values, formats, and examples.

## How-to

- [`how-to/context-branches.md`](how-to/context-branches.md) — decide whether to
  continue, fork a cached child, spread a semantic/fresh child, and inspect a
  returned branch.

## Research and evaluation

- [`research/context-branch-evaluation.md`](research/context-branch-evaluation.md)
  — paired experiments, metrics, hypotheses, and DSPy promotion gates.
- [`research/README.md`](research/README.md) — retained research and provider
  setup notes.

Superseded reviews, roadmap snapshots, and run records remain available through
Git history rather than being mixed into active contracts.
