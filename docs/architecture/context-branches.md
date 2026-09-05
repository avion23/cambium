# Context branches

**Status:** implemented branch/context policy, with remaining state projections
tracked in [the implementation plan](../../implementation-plan.md).

## One worker, several relationships

A root agent and a child run the same worker loop, tools, and action protocol.
**Child agent** names the parent/child lifetime relationship. **Subagent** is an
informal name for delegated work, not another runtime, permission class, or
cheaper kind of model. `kind` describes the task; it does not select a special
prompt. A child can delegate again within the task-tree limits.

Keep four different structures separate:

| Structure | What it answers |
| --- | --- |
| Task tree | Who owns this task, its children, and cancellation? |
| Conversation branch | Which messages, observations, and checkpoints belong to this task? |
| Git graph | Which code changes were actually integrated? |
| Cache lineage | Can this exact provider request prefix be reused? |

A child result does not imply a Git merge. A process restart does not imply a
new semantic branch. An exact checkpoint does not prove a provider cache hit.

## Choose context and provider placement independently

`context_mode` selects what the child knows. `placement` expresses provider
preference. Both are explicit in model-originated `delegate` calls.

| Context | Child receives | Placement |
| --- | --- | --- |
| `trunk` | Complete compatible checkpoint prefix, including its raw tail | `inherit` only |
| `semantic` | Immutable summary state under a fresh provider-specific head | `inherit` or `spread` |
| `fresh` | Self-contained task, without parent checkpoint or summaries | `inherit` or `spread` |

`inherit` preserves parent provider/model affinity. `spread` prefers another
feasible provider and can use the original provider when no alternative fits.
An explicit exact-context request is rejected when compatibility cannot be
established; it is not silently changed into a lossy branch. `trunk + spread`
is contradictory and rejected.

Use exact context when unsummarized observations matter. Use semantic context
when accepted conclusions are enough. Use fresh context for an independent
investigation or review. Neither a semantic branch on another provider nor a
new process transfers a provider's KV cache.

The exact fields and historical reference forms live in
[the reference](../reference/context-branches.md). Admission, suspension, and
joins live in [child lifecycle](subagents.md); request/checkpoint construction
lives in [the context engine](context-engine.md).

## Parallelism has a price

Delegate work that can make useful progress independently: a separate file
region, an investigation, or a review with an explicit question. Keep a small
local edit or the next dependent step in the current branch.

The relevant comparison is not “more agents are faster.” Compare the saved
critical-path time and useful information with child startup, repeated input,
summary, provider queueing, join, and combined-tree verification costs. A warm
parent can be cheaper than a fresh fast provider for a tiny task. An independent
child on an otherwise idle provider can be valuable for a large task even when
its prefix is cold.

A practical child contract states the objective, owned files or read-only area,
observable completion criteria, and relevant checks. Give overlapping code one
writer; another branch can review without writing. Do not create elaborate
role hierarchies to compensate for unclear ownership.

## Summaries are an index into evidence

The parent normally receives a bounded child result, not the complete child
transcript. It can use the live `branch_history` tool to list branches or calls
and reopen one exact action/observation. `repo_query` locates current source;
`read_batch` reads the relevant region. These tools read existing repository and
session artifacts rather than maintaining a second memory database.

Corrections append new facts or invalidations to the semantic trunk. Old
observations remain available. A check that passed on a child head is evidence
about that head, not proof that a later combined tree passes.

## Useful next steps, not additional runtime classes

`branch_state.py` and the `inspect-state` CLI already derive inspectable state.
The operator still has a separate reducer in `observability.py`; shared current
state and model-facing projections must be evaluated explicitly rather than
assumed complete. The proposed evidence-linked work ledger and richer result
capsule should reuse existing events, checkpoints, and Git identities.

Do not add a second scheduler, memory store, policy actor, or mandatory planning
turn to implement these ideas. Add the smallest projection or transition that
fixes a demonstrated loss of state or waste of resources.

## Executable anchors

- [Task structure](../../src/cambium/tasktree.py),
  [supervisor](../../src/cambium/supervisor.py),
  [worker](../../src/cambium/worker.py)
- [Tool contract](../../src/cambium/schemas.py),
  [history projection](../../src/cambium/branch_history.py)
- [Navigation scenarios](../../tests/scenarios/test_navigation_tools.py),
  [branch history scenarios](../../tests/scenarios/test_branch_history.py)
