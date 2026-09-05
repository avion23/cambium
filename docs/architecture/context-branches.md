# Context branches and automatic delegation

**Status:** current worker/supervisor behavior. [CAST](context-engine.md) owns
context representation; [the reference](../reference/context-branches.md) owns
exact arguments; [child lifecycle](subagents.md) owns admission and joins.

## Who decides to delegate?

The current coding model decides, during its ordinary action call. There is no
mandatory extra planner request, no online DSPy classifier, and no fixed rule
that splits every request mentioning two files.

`prompts.CODING_POLICY` asks the model to keep tiny or coupled work local and
batch independent workstreams with disjoint ownership and completion checks.
A GEPA experiment can change that policy text. The `should_decompose` decision
module is an offline experiment, not a hidden step in the worker loop.

```text
general request
  -> normal model action
       -> direct tool call / plan / finish
       -> one delegate or a batch of delegates
            -> complete omitted context/placement policy
            -> supervisor derives workspace and execution configuration
            -> choose eligible provider lanes, start children
            -> join results and artifacts, resume parent
```

This is model-directed delegation with deterministic execution defaults. It is
not a measured critical-path optimizer, and appropriateness is not guaranteed.
The benchmark should measure whether delegation actually improves completion
and resource use, not reward child count by itself. `--auto` concerns provider
selection; it does not force decomposition.

## Defaults without configuration rituals

`worker._complete_delegates` counts delegate calls in the current action batch.
`child_policy.complete_child_policy` fills omitted policy fields before the
proposal is recorded:

| Proposal | Default context and placement |
| --- | --- |
| One child, neither field supplied | `trunk + inherit` |
| Several children in one batch, neither field supplied | `semantic + spread` |
| `placement=spread`, context omitted | `semantic + spread` |
| `context_mode=semantic` or `fresh`, placement omitted | `spread` |
| `context_mode=trunk`, placement omitted | `inherit` |

Explicit fields remain explicit. `trunk + spread` is contradictory. A requested
exact fork that cannot establish compatibility is rejected, not silently made
lossy. A batch is important: emitting one delegate on each later turn can
serialize work because the parent suspends at a successful delegation boundary.

The supervisor supplies repository, private worktree, branch and inherited
execution settings. Models supply the actual workload, not internal filesystem
paths or credential plumbing. Existing complete supervisor plans remain usable.

## What does spreading guarantee?

For independent children, admission removes incidental inherited model/provider
pinning and ranks eligible capacity. It prefers unused lanes and alternatives
to the parent where feasible. Explicit task requirements and allowed providers
still apply. A suspended parent releases its provider-lane reservation.

Spreading is a preference, not an assurance of distinct providers. When no
alternative is usable, the original provider can serve the task. Call-time
failure can move an assigned child to another provider, including the parent's.
Compare `task_assigned` with actual `usage_event.provider`; assignment is not
proof of where tokens were generated.

For a single blocking child needing the exact current context, keeping the
parent provider/model avoids a new semantic head and preserves prefix affinity.
For independent work, `semantic` transfers accepted summary state; `fresh`
transfers only a self-contained task. Neither transfers a provider's KV cache.

## One worker, several relationships

A root and a child run the same worker. **Child** names parent-owned lifetime;
**subagent** is the ordinary name for delegated work. Neither denotes a separate
runtime, permanent researcher/reviewer role, cheaper model, or stateless mode.

Task ancestry, conversation history, accepted Git artifacts and provider-cache
lineage are separate. A successful child summary does not prove its code was
integrated. A cache-compatible checkpoint does not prove a cache hit.

The parent receives bounded results, inspects missing details with
`branch_history`, checks the combined artifact, and continues. On failure or
cancellation it ends its owned children before removing their integration
workspace. No detached child should attempt to merge into a deleted parent.

## Cost and evidence

Delegate when independent work can repay startup, repeated input, summary,
provider queueing, joins and combined-tree checks. Keep one writer per code
region. A model can be wrong about this tradeoff; do not hide unsuccessful
trials or claim a speedup from one successful run.

The ordinary defaults need no additional flags. Runtime observations belong in
the existing events/checkpoints, and real rollouts in the benchmark reports.
Keep open state/optimization proposals in [the plan](../../implementation-plan.md),
not in a second worker hierarchy or duplicate scheduler.
