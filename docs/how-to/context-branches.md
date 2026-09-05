# Delegate and inspect work

**Status:** current workflow. Exact fields/actions are in
[the reference](../reference/context-branches.md); rationale is in
[context branches](../architecture/context-branches.md).

## Keep small dependent steps local

Use `repo_query` to locate relevant code and `read_batch` to read exact regions.
Make the smallest useful change and run the relevant check. A plan is useful
for a multi-step task, not a mandatory first provider round trip.

Delegate when a child has a useful independent objective and enough work to
repay startup, context, and join costs. Prefer one writer per file region.
A second branch may investigate or review the same area without editing it.

## Give the child a complete task

```json
{
  "type": "tool_call",
  "name": "delegate",
  "arguments": {
    "child_task_id": "routing-review",
    "kind": "investigation",
    "spec": {
      "task": "Read-only review of src/cambium/routing.py. Reproduce one concrete provider selection or accounting bug if present. Return source locations, the reproduction, and its result. Do not edit files.",
      "context_mode": "semantic",
      "placement": "spread"
    }
  }
}
```

Use `trunk+inherit` when the complete current checkpoint/raw tail is needed.
Use `semantic` when summary conclusions are enough, and `fresh` for an
independent view without inherited assumptions. For semantic/fresh work,
`spread` can use another provider resource; `inherit` preserves affinity.
`trunk+spread` is invalid. Explicit exact context does not silently downgrade.

A successful delegate tool response means the task was proposed. The supervisor
still has to admit and execute it. The child is the same worker abstraction as
the parent, not a special lightweight model invocation.

## Read the result, then retrieve only missing evidence

Start with the child's bounded result. To inspect its actual calls:

```json
{"name":"branch_history","arguments":{"action":"tools","task_id":"routing-review","limit":20}}
```

Reopen a returned reference rather than inventing its coordinates:

```json
{"name":"branch_history","arguments":{"action":"tool","ref":"tool:routing-review:1:6:0"}}
```

The final coordinate identifies a call within a batch. A history read returns
recorded evidence; it does not run the command again. `branches` lists available
tasks. `transcript` with a task id and bounded offset/limit is available when
individual evidence is insufficient, not the default retrieval strategy.

## Verify accepted code, not only the report

For a mutating child, inspect the accepted Git head and the combined artifact.
The child saying “implemented” does not prove its change is in the parent
worktree. Run combined-tree checks that the integration needs. A read-only child
should return evidence without fabricating an edit or an empty commit.

When later evidence contradicts a conclusion, preserve the old evidence and
append the correction or invalidation in the next semantic delta. Do not rewrite
old summary entries. Retain an exact reference for expensive observations so a
later turn can reopen them without another provider/tool investigation.

## Spend parallel resources deliberately

Overlapping writers, tiny children, broad transcript replay, and redundant
reviews can consume more weekly capacity than they save in wall time. Compare
accepted outcomes, calls, input/output/cache tokens, and elapsed time. A cached
prefix is useful but not proof of free tokens; provider-specific quota rules
still apply.

The larger driving-loop/state proposals are in
[agent driving loop](agent-driving-loop.md). They are not extra mandatory
planning or approval steps in today's worker.
