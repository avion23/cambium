# Documentation map

Start with the [runtime map](architecture/architecture.md). It states what is
implemented and which module owns each effect. Source and executable checks are
the final authority.

## Runtime contracts

| Topic | Owner |
| --- | --- |
| Runtime flow and module ownership | [architecture](architecture/architecture.md) |
| CAST working context and K0 | [context engine](architecture/context-engine.md) |
| Delegation and context/placement defaults | [context branches](architecture/context-branches.md) |
| Child admission, suspension, joins and failure | [subagents](architecture/subagents.md) |
| Provider capacity, quota and routing | [provider routing](architecture/provider-routing.md) |
| Persistent interactive sessions | [interactive TUI](architecture/interactive-tui.md) |
| Terminal input, focus and timeline layout | [terminal interface](architecture/terminal-interface.md) |
| Durable events | [events](architecture/events.md) |
| Recovery and publication | [operations](architecture/operations.md) |
| DSPy/GEPA prompt experiments | [optimization](architecture/optimization.md) |
| Design rationale | [agent operating model](architecture/agent-operating-model.md) |

## Exact interfaces

- [Agent state](reference/agent-state.md): `BranchState`, `SituationFrame`,
  `inspect_state`, and state/reference semantics.
- [Context branches](reference/context-branches.md): `delegate`, `repo_query`,
  `branch_history`, context modes, placement values, and stable history refs.
- CLI `--help` and `schemas.py` remain authoritative for command/tool arguments.

## Workflows and operations

- [Agent driving loop](how-to/agent-driving-loop.md)
- [Delegation workflow](how-to/context-branches.md)
- [Production deployment](how-to/production-deployment.md)
- [Codex OAuth activation](research/codex-activation.md)

`WorkLedger` and `ResultCapsule-v2` are proposals. Shared recorded inspection
through `BranchState`/`SituationFrame` is implemented for the model, CLI and TUI.
Do not infer runtime support from a proposed type name.

## Editing rule

One contract gets one owner. Architecture explains rationale and ownership;
reference gives exact values; how-to gives sequences. Keep dated run logs and
completed implementation plans out of the active documentation tree. Link
instead of copying schemas, command tables, defaults or status prose into
several documents.
