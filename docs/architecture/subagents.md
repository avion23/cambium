# Child lifecycle

**Status:** implemented supervisor/worker contract. Context rationale is in
[context branches](context-branches.md); exact model-facing fields are in
[the reference](../reference/context-branches.md).

## Vocabulary and ownership

There is one agent implementation. A **child** is an ordinary worker task owned
by a parent; **subagent** is an informal synonym for delegated work. It is not a
separate reduced worker or a provider category. A task's `kind` is structural
metadata, not a choice of system prompt.

The supervisor owns task admission, processes, worktrees, generations, child
lifetime, and publication. The worker owns the model/tool loop and proposes
children. Architectus can build a task tree, but does not replace the supervisor
as the process or publication owner.

A top-level flat plan can run independent roots. Recursive work additionally
obeys the task tree's width/depth constraints. `--max-workers` is an explicit
process concurrency cap; provider concurrency and quota are separate resources.
A suspended parent must not hold a worker slot while waiting for a child that
needs that slot.

## From proposal to running child

1. The model calls `delegate` with a stable child id, task kind and self-contained
   objective. The worker fills omitted context/placement using the
   [automatic delegation defaults](context-branches.md); explicit policy wins.
2. Tool validation checks the shape. A successful tool response means
   **proposed**, not admitted or completed.
3. The supervisor validates parent identity, tree limits, provider feasibility,
   and the requested context policy, then records admission before spawning.
4. The child gets an isolated Git worktree and the selected context projection.
   It runs the same action loop and may delegate recursively.
5. A context-reusing parent can checkpoint and suspend. Child results return in
   deterministic admission order, not whichever completion happened first.

The model-facing valid policies are `trunk+inherit`, `semantic+inherit`,
`semantic+spread`, `fresh+inherit`, and `fresh+spread`. Explicit exact requests
never silently downgrade. Internal harness-originated child specifications can
still use automatic compatibility resolution; this is not the public model
contract and should not be relied on in prompts.

## Two products, two acceptance decisions

A child produces a bounded semantic result and, for edits, Git artifacts.
These are related but not interchangeable.

The current worker result reports its terminal status and objective verdict,
commit requirement, commit/file evidence, and bounded provider/result metadata.
The supervisor checks the reported artifact against the actual worktree and
base. The richer evidence-linked **ResultCapsule** described in
[agent-state reference](../reference/agent-state.md) remains a design target;
it must not be documented as the current result wire schema.

For mutating work the accepted child commit is integrated through the existing
serialized merge path. Before the parent resumes, its worktree must match the
accepted integration head. A summary saying “implemented” or a `files_changed`
list cannot substitute for that join. Conflicts go through the existing ordered
resolver path, not a second independent merge authority.

A successful read-only task has `requires_commit=false`, a clean tree, and no
publication commit. Do not manufacture an edit or an empty commit just to make
a review look productive. Required checks on the combined tree still belong to
the integrating parent; child checks may refer to an earlier artifact state.

## Failure and recovery

Parent cancellation owns child lifetime. Worker generations fence stale
responses; restarts consume the configured restart allowance and restore only
compatible checkpoints. A dirty or inconsistent worktree is retained or salvaged
rather than silently published or discarded.

Task wall time, process slots, provider lanes, request rate, and token windows
remain separate. Summary and retry calls are real resource consumption, even
when a subscription makes their incremental cash price zero. Missing host
measurements are not themselves a reason to block work; optional host-resource
thresholds are applied only when configured.

These are practical correctness boundaries, not a reason to add model-facing
approval steps around ordinary reads, edits, or every child result.

## Inspect, do not replay work

Use `branch_history` to list the child's calls, then reopen a returned tool
reference when its bounded result is insufficient. The reference includes task,
generation, turn, and batch index. It reads recorded evidence and never runs
the historical command again. Use `repo_query` and `read_batch` to check the
currently accepted code.

The parent should continue directly when one local read answers the question.
A separate reviewer or child is useful only when it supplies independent work
or evidence worth its context and coordination cost.

## Source and tests

- [Supervisor admission and joins](../../src/cambium/supervisor.py),
  [task tree](../../src/cambium/tasktree.py),
  [merge owner](../../src/cambium/merge.py)
- [Worker actions/results](../../src/cambium/worker.py),
  [delegate schema](../../src/cambium/schemas.py)
- [History scenarios](../../tests/scenarios/test_branch_history.py),
  [real coding publication](../../tests/acceptance/test_live_coding_gate.py),
  [real TUI read-only continuation](../../tests/acceptance/test_live_tui_coding.py)
