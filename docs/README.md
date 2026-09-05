# Documentation map

Start with [the current runtime map](architecture/architecture.md) and
[providers as resources](architecture/provider-routing.md). Source and executable
tests decide what is implemented. The [operating model](architecture/agent-operating-model.md)
preserves the wider design; it is not a claim that every proposed layer ships.

## Current runtime contracts

| Question | Owning document |
| --- | --- |
| Which module owns which effect? | [Runtime map](architecture/architecture.md) |
| What is a child/subagent, and why fork context? | [Context branches](architecture/context-branches.md) |
| How are children admitted, suspended, and joined? | [Child lifecycle](architecture/subagents.md) |
| How do summaries, checkpoints, and K0 rollover work? | [Context engine](architecture/context-engine.md) |
| How are provider capacity, throughput, and quota accounted? | [Provider routing](architecture/provider-routing.md) |
| What survives interactive turns and reconnect? | [Interactive lifecycle](architecture/interactive-tui.md) |
| How does terminal input, layout, and inspection work? | [Terminal interface](architecture/terminal-interface.md) |
| Which events are durable? | [Event glossary](architecture/events.md) |
| How do operational plans, recovery, and publication work? | [Operations](architecture/operations.md) |
| What does DSPy actually optimize and load? | [Offline optimization](architecture/optimization.md) |

The active navigation tools are `repo_query` and `branch_history`, backed by
existing source/session artifacts. `branch_state.py` and CLI `inspect-state`
also exist. A fully shared model/operator state projection, typed WorkLedger,
and richer ResultCapsule remain separate integration work; do not infer them
from target names or the existence of a library.

## Reference and usage

[Context/navigation reference](reference/context-branches.md) owns exact public
tool and policy values. [The driving loop](how-to/agent-driving-loop.md) and
[delegate and inspect work](how-to/context-branches.md) show the current workflow.
CLI `--help`, schemas, and source own exact command arguments and configuration
defaults.

The contributor contract is [agents.md](../agents.md).
[The implementation plan](../implementation-plan.md) contains open work only.

## Design proposals and experiments

[Agent operating model](architecture/agent-operating-model.md) explains the
rationale. [Branch contracts](architecture/context-branch-requirements.md)
distinguish current invariants from gaps, while
[agent-state reference](reference/agent-state.md) marks the remaining target
shapes. Use proposals to guide a small implemented slice, not as a checklist
requiring new per-turn control layers. Planning is optional for a small task.

[Agent-system evaluation](research/agent-system-evaluation.md) contains proposed
experiments and metrics. [Codex activation research](research/codex-activation.md)
is provider-specific research whose conclusions need checking against current
configuration and transports. Neither research page proves runtime support or
an optimization gain. The [2026-09-04 harness audit](research/harness-audit-2026-09-04.md)
records concrete failures, repairs, test outcomes, and remaining limitations.

## Editing rule

Give a contract one owner and link to it. Architecture explains rationale and
ownership; reference gives exact shapes; how-to shows a sequence; research keeps
hypotheses and results. Mark proposals explicitly, cite source symbols/files
rather than rotating line numbers, and remove completed work from the plan.
Do not repeat whole schemas, command tables, or mandatory gate lists across
all four categories.
