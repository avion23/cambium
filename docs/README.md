# Documentation map

Source and executable tests establish what currently runs. A proposed schema or
diagram does not establish that a worker or frontend can use it.

Start with [agent operating model](architecture/agent-operating-model.md) for the
purpose and tradeoffs, [runtime architecture](architecture/architecture.md) for
actual owners and interfaces, and [open implementation work](../implementation-plan.md)
for unfinished paths. Contributor instructions are in [agents.md](../agents.md).

## One home for each subject

| Question | Owning document |
| --- | --- |
| Why this harness, and what should stay lean? | [Agent operating model](architecture/agent-operating-model.md) |
| What runs, and which module owns it? | [Runtime architecture](architecture/architecture.md) |
| How do context, compaction and cache identity work? | [Context engine](architecture/context-engine.md) |
| What is a child, and when should context/provider placement differ? | [Context branches](architecture/context-branches.md) |
| How are children admitted, suspended and joined? | [Subagents](architecture/subagents.md) |
| Which state invariants must hold? | [Branch contracts](architecture/context-branch-requirements.md) |
| How are providers, throughput and account windows handled? | [Provider routing](architecture/provider-routing.md) |
| How does a persistent interactive session run? | [Interactive TUI](architecture/interactive-tui.md) |
| What does the terminal display and accept? | [Terminal interface](architecture/terminal-interface.md) |
| Which durable events exist? | [Events](architecture/events.md) |
| How is Cambium operated? | [Operations](architecture/operations.md) |
| What does DSPy actually optimize? | [Optimization](architecture/optimization.md) |

## Exact interfaces and practical guides

[Context-branch reference](reference/context-branches.md) owns current delegation
fields and historical reference formats. [Agent-state reference](reference/agent-state.md)
contains the current navigation interface and explicitly labelled state/capsule
proposals; illustrative future JSON is not the current CLI serialization.

[Agent driving loop](how-to/agent-driving-loop.md) describes the current
locate/edit/verify path. [Delegation guide](how-to/context-branches.md) gives
scoped handoffs and progressive historical inspection. CLI `--help`, source
schemas and their tests remain the exact command/tool contract.

## Research, not implementation claims

[Agent-system evaluation](research/agent-system-evaluation.md) retains broader
experiment proposals. [Codex activation](research/codex-activation.md) records
provider research whose conclusions must be checked against current transport
and configuration behavior. Neither is an additional runtime approval process.

## Editing these docs

Label current behavior, proposed extensions and historical findings distinctly.
Put rationale in architecture, exact shapes in reference, procedures in how-to,
and hypotheses in research. Keep unfinished work only in the implementation
plan. Link to the owner rather than copying its schema, phase checklist or
policy into another page.

For implemented claims name a real source owner and executable scenario. Prefer
symbols to rotating line numbers. When documentation and implementation disagree,
correct the claim or the owning path; do not invent a compatibility explanation.
Keep the names consistent across tools, prompts, events and the TUI.
