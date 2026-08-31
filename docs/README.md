# Documentation map

Cambium documentation is organized by the question a reader or agent is trying
to answer. Source and tests remain authoritative for landed behavior.

## Status language

Every design document should use one of these labels:

- **implemented contract** — describes behavior backed by current source and
  executable tests;
- **current runtime map** — maps landed modules and explicitly labels target
  integration;
- **target architecture** — normative or aspirational design, not an
  implementation claim;
- **target reference** — exact values/schemas intended for a future phase;
- **experimental protocol** — hypotheses, measurements, and promotion gates;
- **historical** — retained for provenance only and not part of active design.

A target document must not be cited as proof that a feature exists. An active
implemented document must not depend on a branch, workflow, or symbol that no
longer exists.

## Start here

1. [`architecture/agent-operating-model.md`](architecture/agent-operating-model.md)
   — the synthetic system: branch state, SituationFrame, accretion, control,
   resources, and linked abstraction tower.
2. [`architecture/architecture.md`](architecture/architecture.md) — current
   runtime/module map and ownership boundaries.
3. [`../implementation-plan.md`](../implementation-plan.md) — ordered open work
   only.
4. [`../agents.md`](../agents.md) — repository operating contract for coding
   agents and contributors.

## Architecture

- [`architecture/agent-operating-model.md`](architecture/agent-operating-model.md)
  — target agent control system and design laws.
- [`architecture/architecture.md`](architecture/architecture.md) — current
  runtime map, tower, data ownership, and invariants.
- [`architecture/context-engine.md`](architecture/context-engine.md) — CAST,
  immutable checkpoints, cache lineage, K0 rollover, and context economics.
- [`architecture/context-branches.md`](architecture/context-branches.md) —
  recursive branch rationale and exact/semantic/fresh modes.
- [`architecture/context-branch-requirements.md`](architecture/context-branch-requirements.md)
  — target normative agent/context requirements and current compatibility gaps.
- [`architecture/subagents.md`](architecture/subagents.md) — current child
  admission, worktree isolation, result, and join mechanics.
- [`architecture/provider-routing.md`](architecture/provider-routing.md) —
  admission versus call-time provider ownership and target resource projection.
- [`architecture/interactive-tui.md`](architecture/interactive-tui.md) —
  persistent interactive branch lifecycle.
- [`architecture/terminal-interface.md`](architecture/terminal-interface.md) —
  operator rendering and command contract.
- [`architecture/events.md`](architecture/events.md) — current durable event
  glossary.
- [`architecture/operations.md`](architecture/operations.md) — executable
  operational behavior and commands.
- [`architecture/optimization.md`](architecture/optimization.md) — DSPy data,
  evaluation, and promotion mechanics.

## Reference

Use reference documents for exact values and interfaces, not rationale.

- [`reference/agent-state.md`](reference/agent-state.md) — target BranchState,
  SituationFrame, ResourceEnvelope, epistemic item, ResultCapsule,
  `inspect_state`, and `repo_query` contracts.
- [`reference/context-branches.md`](reference/context-branches.md) — current and
  target child-policy/history vocabulary and explicit compatibility notes.

Provider/CLI/Python/security behavior is currently documented by the owning
source modules, `agents.md`, the focused architecture documents, and `--help`.
Add separate reference pages only when they can be generated or checked against
those executable interfaces.

## How-to guides

- [`how-to/agent-driving-loop.md`](how-to/agent-driving-loop.md) — target
  orient/locate/act/verify/accrete workflow for a model branch.
- [`how-to/context-branches.md`](how-to/context-branches.md) — decompose work,
  assign ownership, choose context/placement, and inspect child evidence.

Operational setup remains in the root README, `agents.md`, CLI `--help`, and the
focused architecture documents until dedicated guides exist.

## Research and evaluation

- [`research/agent-system-evaluation.md`](research/agent-system-evaluation.md) —
  target whole-system experiments for orientation, retrieval, accretion,
  delegation, resources, and recovery.
- [`research/codex-activation.md`](research/codex-activation.md) — provider
  activation research; verify conclusions against current transports/config.

## Documentation rules

1. Put rationale and ownership in `architecture/`.
2. Put exact public/target shapes in `reference/`.
3. Put recommended sequences in `how-to/`.
4. Put hypotheses and metrics in `research/`.
5. Keep only open ordered work in `implementation-plan.md`.
6. Link to source/tests for implemented claims; do not freeze rotating line
   numbers when a symbol name is sufficient.
7. Use the same names across source, schemas, prompts, events, TUI, and docs.
8. When source and docs disagree, state the disagreement and fix the source or
   the document; do not explain it away with an implicit compatibility rule.
9. Do not link to a planned page as though it exists. Create the page in the
   same change or describe the owning source/current document directly.
