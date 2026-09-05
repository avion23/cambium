# Branch contracts

**Status:** runtime invariants and explicitly identified gaps. This document
collects checks that protect actual state; it is not a specification for a
second approval or policy framework.

Rationale belongs in [context branches](context-branches.md), exact public
shapes in [the reference](../reference/context-branches.md), and future work in
[the implementation plan](../../implementation-plan.md).

## Ownership

The supervisor owns task lifetime, worker generations, child admission,
worktrees and Git publication. The worker owns its model/action loop and
checkpoint construction. Provider transports report usage and call outcomes.
A model response can propose an action or claim, not directly change any of
those owners' state.

Task ancestry, conversation context, accepted Git state and provider-cache
compatibility are distinct identities. In particular:

- A semantic child result does not prove that its artifact was integrated.
- A valid exact checkpoint does not prove that the provider served cached tokens.
- Worker turn counters are not globally unique across interactive sessions.

## Context

Published summary entries and checkpoint prefixes are immutable. A summary
covers one disjoint raw range; existing summaries are not recursively rewritten
as if they were fresh evidence. Keep the raw source outside the working prompt.

An exact child gets the validated parent prefix. A semantic child gets summaries
under a fresh provider head. A fresh child gets its task without the parent
checkpoint or result envelope. The worker completes omitted context/placement
before recording proposals. Contradictory or incompatible explicit requests do
not silently change meaning.

All five supported combinations and the internal harness-originated compatibility
path are documented once in the [reference](../reference/context-branches.md).

## Tools and actions

A normal response is one plan, tool call or finish action. A plan is optional;
small tasks must not spend a call merely to satisfy a first-action ritual.
Independent reads may run concurrently. Mutations execute in the declared order.
Tool errors are observations for the next decision, not reasons to repeat the
same malformed call indefinitely.

Repository queries are bounded and worktree-relative. Portable symbol/reference
results must not masquerade as semantic LSP results. Missing language-service
configuration is reported rather than replaced with a guessed answer.

History queries read existing events and checkpoints without re-execution.
Returned references include batch identity and, for interactive archives, the
operator-turn scope. An unscoped ambiguous reference must not select unrelated
"latest" evidence. Detail belongs in a requested observation, not in a rewritten
system prompt.

## Results and resources

The worker records tool observations and the model's finish verdict. It does
not certify correctness merely because a shell command returned zero, or demand
an unrelated shell call before completion. Read-only completion needs no edit.
Actual correctness claims require checks against the relevant artifact; passing
a check before the last edit does not prove the edited state. Budget exhaustion
without a finish verdict is incomplete, not fabricated success.

The parent owns child lifetime and joins results in a defined order. Resuming
from a child code change requires the accepted Git integration, not merely the
child's summary. Parallel work stays isolated; publication must not overwrite
unrelated local changes.

Provider feasibility precedes preferences. Request rate, concurrent capacity,
account windows, token usage, cash and wall time remain different dimensions.
Only provider-reported output counts measure generation speed. Aging evidence
must not change the measured rate. Unknown quota and cache evidence must not be
replaced with persuasive invented values.

## Known gaps, not implemented contracts

`BranchState` and CLI inspection exist, but the TUI and model do not yet share
all of its semantics. The full SituationFrame, WorkLedger and ResultCapsule-v2
shapes in [agent-state reference](../reference/agent-state.md) remain proposals.
Their existence in a document is not evidence of model tool support.

Routing's default token-window normalizer is a load-spreading heuristic, not a
real weekly entitlement. Root lease migration, unified resource-aware ranking,
and evidence-linked verification across all projections remain open. See
[provider routing](provider-routing.md) and the single open implementation plan.

## Representative executable regressions

| Failure to prevent | Exercise |
| --- | --- |
| Mandatory planning/finalization crowds out verification | Three-turn edit, verify, finish scenario |
| Historical calls collide across interactive turns | Repeated task/generation/turn with distinct archived observations |
| Operator cancel or inspection is queued behind a provider | PTY with blocked provider; inspect, cancel, return to prompt |
| Active usage is missing or double-counted | Completed totals plus live snapshot, repeated repaint |
| Long input appears to be fast output generation | Total-only provider usage does not create a speed sample |
| Aging makes a provider artificially faster | Decay sample weight while preserving its mean |
| A wired schema has no executable consumer | Call navigation/history through `run_tool`, then a real frontend |

Relevant tests are under `tests/scenarios/`; real-provider CLI/TUI exercises are
in `tests/acceptance/test_live_frontends.py`. Use additional fault/replay tests
for the owner being changed, not a mandatory unrelated suite before every tool
call.
