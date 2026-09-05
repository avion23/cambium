# Drive Cambium as an agent

**Status:** guide to the current coding loop. Proposed SituationFrame and
`inspect_state` model interfaces are not prerequisites for this path.

## Orient and locate

Read the task, its ownership boundary and completion criteria. For multi-step
work, make a short plan with observable outcomes. A simple task can begin with
a tool call; there is no mandatory planning round trip.

Use a bounded repository query before guessing filenames or dumping a tree:

```json
{"type":"tool_call","name":"repo_query","arguments":{"action":"symbols","query":"read_lines"}}
```

Then read the selected source region or a batch of relevant files. `symbols`
and `references` are portable scans; only the explicitly configured LSP method
provides language-service results. Exact arguments and limits are in
[agent-state reference](../reference/agent-state.md).

Independent reads can share one action. Mutations run in listed order, not in
parallel. Keep the batch focused on one question.

## Act on evidence

Distinguish what a tool returned from what you infer. A failed command may be a
wrong invocation, missing prerequisite or provider failure rather than a source
bug. Diagnose that cause instead of adding a fallback to hide it.

Read the owning code, make the smallest useful change and run the relevant
check. A targeted regression should fail before the fix and pass afterward when
a reproduction is available. Use the affected suite when a shared boundary
changes. Do not run the whole suite repeatedly while a focused failure remains.

Work only in the assigned worktree. Cambium owns the publication commit; do not
run `git commit`, `merge` or `push` from an agent tool call.

## Delegate and recall selectively

Keep small or coupled work local. Delegate only an independent scope with its
own completion check and explicit `context_mode`/`placement`. Child lifetime,
result admission and Git integration remain supervisor-owned. Use
[the delegation guide](context-branches.md) for the handoff, rather than a second
planner/reviewer hierarchy.

Start with the child's result envelope. For a missing detail, list its tools
and reopen the returned reference:

```json
{"type":"tool_call","name":"branch_history","arguments":{"action":"tools","task_id":"review-parser"}}
```

An interactive reference may end in `@turn-0003`; preserve that suffix. It
identifies the operator turn, separately from the model-call counter. Historical
retrieval does not execute the command again. Use transcript paging only when
one exact observation is insufficient.

## Preserve useful conclusions

Keep decisions, concrete findings, changed files, verification evidence and
unfinished work available to the summarizer. Correct obsolete conclusions
explicitly. Do not preserve routine syntax mistakes and repeated reads unless
they reveal a reusable constraint.

After cancellation, restart or a child join, check the actual resumed artifact
and context state. A past passing test may no longer apply after another edit.
Do not infer accepted Git state from an old model summary.

## Finish honestly

A complete read-only investigation needs no edit. A code change needs the
required verification after the change. Set `objective_met` accordingly and
summarize the result, checks actually run and remaining limitations:

```json
{"type":"finish","summary":"Repaired the paging boundary; focused and affected parser tests passed.","objective_met":true}
```

When resources run short, prioritize completion and verification over another
exploratory branch. Do not call a provider's unknown quota "available" or confuse
cached input with free account capacity. Current accounting limitations are in
[provider routing](../architecture/provider-routing.md).
