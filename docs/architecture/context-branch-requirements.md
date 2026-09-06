# Branch contracts

**Status:** cross-cutting runtime invariants. Rationale belongs in
[context branches](context-branches.md); exact public values belong in the
[reference](../reference/context-branches.md).

## Ownership

The supervisor owns task lifetime, worker generations, child admission,
worktrees, joins, and Git publication. The worker owns its model/action loop and
checkpoint construction. Provider transports own call outcomes and usage. Model
text proposes work; it does not directly change those owners' state.

Keep these identities separate:

```text
task ancestry
conversation/context lineage
accepted Git state
provider/cache identity
```

A successful child result does not prove its commit was integrated. A compatible
checkpoint does not prove the provider reported a cache hit.

## Context

Published summary entries and checkpoint prefixes are immutable. One semantic
fold covers one new raw range; earlier summaries remain background rather than
being recursively rewritten as new evidence.

Child modes are distinct:

- `trunk`: validated exact parent checkpoint, same provider affinity;
- `semantic`: accepted semantic trunk under a fresh provider head;
- `fresh`: self-contained task with no inherited parent context.

The worker fills omitted context/placement defaults before admission. Explicit
contradictions such as `trunk+spread` fail instead of silently changing meaning.

## Actions and evidence

A normal model response is one plan, tool action, or finish verdict. Planning is
optional. Independent calls can be batched; mutations execute in declared order.
Malformed or failed tools become observations for the next decision.

Repository/history/state inspection is read-only. `repo_query`,
`branch_history`, and `inspect_state` use existing source/session artifacts and
do not re-execute historical effects.

A successful shell exit is evidence, not universal proof of task correctness.
A check applies to the artifact/configuration it actually tested. Budget or turn
exhaustion without a terminal verdict is incomplete, not fabricated success.

## Children and publication

A parent owns every admitted child until completion or cancellation. Child code
is accepted separately from the child's semantic result. Resuming after a child
code change requires the parent's worktree to match the supervisor's accepted
integration head.

Parallel writers use isolated worktrees. Publication must not overwrite
unrelated local changes or make completion order determine join order.

## Resources

Provider feasibility precedes preferences. Request rate, concurrent capacity,
quota windows, token use, cash, cache affinity, and wall time are separate
signals. Unknown quota stays unknown. Output throughput uses generated tokens,
not prompt tokens.

The shared `BranchState`/SituationFrame inspection path reports recorded state;
it is not another scheduler or mutable memory service. WorkLedger and richer
ResultCapsule ideas remain research proposals, not prerequisites for ordinary
execution.

## Executable evidence

Use focused regressions for the owner being changed. Representative failure
classes include interactive-history collisions, cancellation during provider
calls, child join/publication mismatches, stale context evidence, quota/capacity
selection, and PTY resize/paste/focus behavior. Real-provider frontend exercises
are in `tests/acceptance/test_live_frontends.py`.
