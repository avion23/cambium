# Context branches

**Status:** implemented branch policies and their rationale. Exact fields and
examples belong in [the reference](../reference/context-branches.md).

## One worker, four different structures

Root and child tasks use the same worker. "Child" describes ownership;
"subagent" describes delegated work. Neither implies a different class or an
automatic choice of context. Avoid a second root/research/review/sub-main
hierarchy.

Keep these structures separate:

| Structure | Question it answers |
| --- | --- |
| Task tree | Who owns the work and the lifetime of each child? |
| Conversation history | What did this worker observe and do? |
| Git graph | Which artifact state was actually accepted? |
| Provider-cache lineage | Which exact request prefixes may share provider caching? |

A successful child claim is not an accepted Git change. Common task ancestry is
not proof of a common provider cache. An interactive operator turn is also not
the same coordinate as a worker's model-call turn.

## A branch carries a working set

```text
stable system/tool head | immutable semantic trunk | recent raw tail
```

A summary appends the durable conclusions of one new raw range. It does not
rewrite earlier summaries. Raw history stays in events and checkpoints, outside
the normal request, for precise later inspection.

This balances enough context to act correctly against the cost of replaying
irrelevant history. A stable prefix can reduce provider processing and cash
cost, but a cold replay must still work. Exact context compatibility is not a
claim that the provider returned a cache hit.

Persistence, summary validation and rollover mechanics are owned by
[context engine](context-engine.md). Do not duplicate them in delegation policy.

## Context and placement are independent choices

The child declares both. There is no model-facing implicit fallback.

| Context | Starting material | Placement |
| --- | --- | --- |
| `trunk` | Exact parent checkpoint, including its raw tail | `inherit` only |
| `semantic` | Immutable parent summaries under a fresh provider head | `inherit` or `spread` |
| `fresh` | Task contract, without parent checkpoint or result envelope | `inherit` or `spread` |

`inherit` preserves the parent provider/model affinity. `spread` removes the
inherited pin and prefers another feasible lane, falling back to the parent's
lane when necessary. It does not require a different provider at any cost.

`trunk + spread` is contradictory. An explicitly requested exact fork that is
incompatible is rejected, not silently changed to a lossy semantic fork. Exact
compatibility includes provider/model/protocol, prompt and tool schemas,
checkpoint identity and the provider boundary.

### Choosing without another planner

Continue locally for a small edit or work tightly coupled to the current tail.
Use `trunk + inherit` when a useful child needs the complete parent working set.
Use `semantic + spread` when a separable task benefits from another provider's
available capacity. Use `fresh` when independent reproduction or review is the
point; supply a sufficient task contract rather than an intentionally
underspecified job.

The exact prefix can be cheaper than composing a new compressed handoff, but
that depends on actual cache evidence and task size. It is not a universal
rule. Shared provider concurrency may also eliminate the expected parallelism.

Estimate the whole cost:

```text
child benefit = critical-path work avoided or independent evidence gained
child overhead = handoff + cold input + spawn + duplicated work + join + verification
```

Do not spawn when overhead dominates. Do not add an LLM decision call merely to
choose between a local three-line edit and a child.

## Results and artifact joins

The parent normally sees a bounded child envelope rather than its complete
transcript. The supervisor admits children, owns their lifetime, and joins
results in a defined order rather than whichever worker finished first.

After a child changes code, semantic acceptance and artifact integration must
both be resolved before the parent resumes against the new tree. A result that
says "fixed" cannot stand in for that Git transition. Failed verification and
unresolved critical work must remain visible.

Current mechanics are in [subagents](subagents.md). Richer evidence-linked
capsules are a proposal in [agent-state reference](../reference/agent-state.md),
not a new wire protocol already used by all workers.

## Historical detail is available on demand

`branch_history` is an active read-only worker tool backed by existing events
and checkpoints. It lists branches, lists calls, opens one returned reference,
or pages a checkpoint transcript. It does not create another evidence database
and never reruns a historical command.

Use the smallest useful retrieval: result envelope, call listing, exact call,
then a transcript window only if necessary. The returned observation is placed
at the end of the request; it does not edit the stable prefix.

Canonical call identity contains task, worker generation, model-call turn and
batch index. Interactive history also scopes it to the enclosing operator
turn, for example:

```text
tool:review-parser:1:7:0@turn-0003
```

Use the reference returned by the listing. Older unscoped references work when
unambiguous; ambiguity across interactive turns is an error, not permission to
return the latest unrelated call. Archives are replayed chronologically even
when inspected from the newest turn directory.

All branches in the current session are visible to this tool. That does not
mean sibling transcripts are automatically injected into every child request.
There is no per-branch access-control service, vector store, hidden-reasoning
archive, or automatic recall call before every model decision.

## Implementation and tests

`child_policy.py` defines the explicit combinations. `supervisor.py`
materializes context, resolves placement and joins children. `worker.py` builds
requests from immutable checkpoints. `schemas.py` and `tools.py` expose the
actual model contract; `branch_history.py` reads its historical evidence.

`test_branch_history.py` covers batched references, chronological interactive
history and cross-turn identity. `test_navigation_tools.py` exercises the
schema/dispatch boundary. `test_live_frontends.py` checks a real TUI follow-up
that reopens earlier edit evidence. Child admission/fork/join scenarios live
alongside these under `tests/scenarios/`.
