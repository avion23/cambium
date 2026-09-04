# Subagents: admission, lifetime and joins

**Status:** current runtime mechanics. Context/placement rationale belongs in
[context branches](context-branches.md); exact policy fields belong in
[the reference](../reference/context-branches.md).

A child is a normal supervised task doing delegated work. There is no separate
subagent or sub-main worker class. Provider-native tools may transport a
`delegate` action, but providers do not own worker processes or Git publication.

```text
model proposal -> generation-local buffer -> supervisor admission
    -> worker + isolated worktree -> bounded result -> ordered artifact join
```

## Creation paths

A static plan can express `depends_on`. The supervisor validates the task tree,
dispatches ready work in a defined order and prevents failed dependencies from
silently turning into successful descendants.

A model uses `delegate` with a stable child ID, task kind and `spec`. The schema
requires `context_mode` and `placement`. The call proposes a child; it does not
confirm that the child was admitted or completed. Admission resolves task-tree
bounds, ownership, provider feasibility, context compatibility and budget.

The optional Architectus port can also propose children after a parent result.
It is another decision source through the same admission path, not a second
scheduler. A merge conflict may produce a scoped resolver child; it still uses
the same worker/publication boundary.

Task `kind` is task-tree metadata, not a hierarchy of specialized agent classes.

## Give the child a sufficient task

State its objective, file/symbol ownership or read-only scope, completion check,
constraints and necessary context. Current wire representation carries most of
that in `spec.task`, plus explicit policy and budget fields. Do not create a
second structured contract until a consumer actually needs fields that cannot
be represented clearly there.

Parallel writers should own disjoint changes. Read-only investigations can
overlap. For a shared mutation area, serialize edits or use one writer and a
read-only reviewer. A vague "improve this" task creates interpretation and join
cost rather than useful parallelism.

The context choices are `trunk`, `semantic` and `fresh`; placement is `inherit`
or `spread`. The reference owns the exact valid combinations. An explicit exact
request does not silently become semantic when compatibility fails.

## Parent and child lifetime

```text
parent runs and proposes
  -> admission persisted
  -> parent-owned completion future registered
  -> child spawned in isolated worktree
  -> terminal result represented
  -> semantic result and artifact integration resolved
  -> parent HEAD agrees with accepted integration head
  -> parent resumes
```

Registering completion before spawn avoids losing an immediately finishing
child. A suspended parent releases its worker slot rather than occupying the
capacity its child needs. The parent's wall budget still bounds waiting and
resumption.

Cancellation, restart, failure and admission rollback must resolve parent-owned
child lifetime. A child must not accidentally become an unowned background task.
Result or integration order must not depend on which worker finishes first.

## Semantic result is not artifact acceptance

The current bounded supervisor/worker result envelope contains status, summary,
diff/commit/file evidence, metrics and parent identity. It is not a complete
transcript and it is not the proposed ResultCapsule-v2 protocol.

A child saying "fixed" does not put code into the parent worktree. Publication
and join must establish the actual Git state. A child passing a check at its own
head does not prove the combined parent tree; run the required combined checks
when integration changes what was tested.

Richer evidence-linked claims, obligations and verification are proposals in
[agent-state reference](../reference/agent-state.md). Extend the existing result
path rather than adding another independently mutable result store.

## Inspect a result progressively

Read the result envelope first. When detail is missing, use the active
`branch_history` tool to list that branch's calls and reopen the exact returned
reference. Page a transcript only when an individual action/observation cannot
answer the question.

Interactive references include an operator-turn suffix. Keep it when reopening
evidence; model-call counters can repeat in later turns. Historical retrieval
never reruns a command or rewrites the stable prompt head. A new conclusion from
retrieved evidence becomes a later semantic delta.

The automatic SituationFrame and model `inspect_state` proposal are not required
to use current result/history inspection.

## Failure handling

Invalid identity, contradictory policy, unavailable required capacity or
conflicting ownership must not spawn a different job than the one requested.
Children cannot widen the parent boundary. A failed child remains a failed
result; timeout does not turn unfinished work into completion. Conflict evidence
must identify the actual integration attempt and stay bounded.

Do not add a separate review or approval agent to every child merely because a
review classifier exists. Delegate when independent work repays handoff,
queueing, join and verification cost; the reasoning is in
[context branches](context-branches.md).

## Sources and observability

`schemas.py`, `tools.py` and `child_policy.py` define proposals. `worker.py`
handles the agent loop and suspension. `supervisor.py` owns admission, context
materialization, lifetime and joins; `tasktree.py` owns tree structure.
`routing.py` handles admission preferences and `diffundo.py` actual calls.

Durable events already expose child identity, lifecycle, context, provider
usage, results and joins. `observability.py` renders them for the operator.
`BranchState` exists as an additional replay-derived view; complete agreement
with all model/TUI state proposals remains [open work](../../implementation-plan.md).
