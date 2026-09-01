# Drive Cambium as an agent

**Status:** target operating guide. Current runtime supports bounded planning,
six model tools, CAST, explicit model-originated child policy,
worktree-confined file mutations, and verification. Automatic SituationFrame,
`inspect_state`, and active history/navigation model tools remain ordered target
work; their underlying history/index/LSP libraries already exist.

## 1. Orient before searching

Target SituationFrame order:

```text
MISSION    required outcome and done criteria
AUTHORITY  worktree, write scope, tools, provider limits
ACCEPTED   context, artifact, lease, and verification state
DELTA      what changed since the previous decision
OPEN       obligations, blockers, unknowns, stale checks
CHILDREN   critical and terminal branch state
RESOURCES  turns, wall, context, quota, cash, lane pressure
ANCHORS    exact evidence worth reopening
```

Before the first effect, be able to state:

```text
objective
observable done criteria
write authority
accepted artifact head
largest uncertainty
cheapest decisive observation
required final verification
```

SituationFrame is target behavior. Until it lands, derive these facts from the
task, current source/worktree, durable observations, and bounded reads; do not
invent missing state.

## 2. Make a falsifiable plan

Bad:

```text
understand the code
make improvements
check everything
```

Better:

```text
1. locate the paging owner and focused scenario
2. reproduce offset=500 at the accepted head
3. repair the smallest owning boundary
4. run focused and affected checks
5. inspect diff and finish against done criteria
```

A plan is a control hypothesis, not authority. Change it when evidence changes.

## 3. Locate before reading broadly

Target precision ladder:

```text
repo_query tree/symbol/search
    -> exact source window
    -> read_batch for selected files
    -> run_shell only when typed inspection cannot answer
```

`code_index.py` and `lsp_query.py` already implement bounded navigation library
boundaries, but `repo_query` is not an active model tool. Today, use one bounded
shell search or one batched read instead of serial filename guesses.

```json
{
  "type": "tool_call",
  "calls": [{
    "name": "read_batch",
    "arguments": {
      "paths": ["src/cambium/routing.py", "tests/scenarios/test_routing.py"]
    }
  }]
}
```

Batch independent reads with one purpose. Serialize effects whose order or
failure semantics matter.

## 4. Treat tool output as evidence

After a meaningful observation, separate:

```text
observed   exact tool/file/provider result
inferred   what that result likely means
unknown    what remains untested
next       smallest action that discriminates explanations
```

A failed command may indicate a wrong invocation, missing dependency, stale
artifact, or genuine defect. Diagnose before editing. Truncated output is not
empty output; narrow the request or follow its retained reference.

## 5. Keep effect authority visible

Current file effects enforce:

```text
write_file / edit_file
    -> normal path inside assigned worktree only
    -> reject absolute external path
    -> reject parent traversal
    -> reject .git and .cambium
    -> reject symlink escape
```

`read_batch` is a bounded inspection capability and may read permitted external
paths. It refuses the worker's own session internals except normal files in its
assigned worktree.

Before an effect, confirm the target belongs to the branch, no child owns the
same write region, and the source evidence still matches current artifact state.
Model claims and child summaries do not update Git.

## 6. Delegate only when coordination pays

```text
benefit
  critical-path reduction
  + independent information gain
  + better provider/model fit
  + reusable context

cost
  context construction
  + provider queue and spawn
  + join/conflict risk
  + parent interpretation
  + combined verification
```

Every current model-originated `delegate` spec must contain a self-contained
task, `context_mode`, and `placement`.

### `trunk + inherit`

Use when the child needs the exact parent checkpoint/raw tail and same-provider
cache affinity.

```json
{
  "name": "delegate",
  "arguments": {
    "child_task_id": "repair-parser-window",
    "kind": "feature",
    "spec": {
      "task": "Own src/parser.py and focused parser tests. Reproduce and repair offset paging. Done when focused and existing parser tests pass. Do not edit routing.",
      "context_mode": "trunk",
      "placement": "inherit"
    }
  }
}
```

### `semantic + spread`

Use for separable work that needs accepted project decisions and can benefit
from another hard-feasible lane.

```json
{
  "name": "delegate",
  "arguments": {
    "child_task_id": "audit-terminal-resize",
    "kind": "investigation",
    "spec": {
      "task": "Read-only audit of terminal resize behavior. Return concrete defects and reproductions. Own no production files.",
      "context_mode": "semantic",
      "placement": "spread"
    }
  }
}
```

### `fresh + spread`

Use when independence is the objective: blind review, clean reproduction, or
assumption isolation.

```json
{
  "name": "delegate",
  "arguments": {
    "child_task_id": "blind-routing-review",
    "kind": "investigation",
    "spec": {
      "task": "Review routing from source and tests only. Do not rely on parent conclusions. Report evidence and make no edits.",
      "context_mode": "fresh",
      "placement": "spread"
    }
  }
}
```

`trunk + spread` is invalid. Explicit trunk requests fail closed when exact
compatibility is impossible. Do not create overlapping writers.

Harness-originated static proposals still have an internal automatic mode when
both policy fields are absent. Model calls do not. New agent work must never
rely on omission.

## 7. Read child results progressively

Target ladder:

```text
ResultCapsule
    -> inspect_state(children)
    -> branch_history branches/tools
    -> exact tool:<task>:<generation>:<turn>:<batch-index>
    -> bounded transcript window
```

`branch_history.py` already implements bounded projection over events and
checkpoints, but it is not an active model tool. When wired, history reads must
never re-run an effect.

Until then, use the bounded child result, accepted Git state, durable events,
and narrowly selected source/checks. Reproduce high-impact child observations
against the parent's accepted worktree when code may have changed.

## 8. Edit from a proven cause

Edit only after one of:

- a failing reproduction tied to current artifact state;
- a source invariant violation with exact locations;
- an unambiguous deterministic transformation requested by the task.

Prefer the smallest change at the owning boundary. Avoid fallback, broad catch,
retry, compatibility wrapper, or unrelated refactor that hides the cause.

After editing:

```text
inspect immediate tool/lint result
    -> inspect changed diff/region
    -> run focused regression
    -> run affected scenarios
    -> rerun any verification made stale by later edits
```

## 9. Verify in layers

```text
compile/static check
    -> focused reproduction
    -> affected module/scenario suite
    -> combined-tree integration
    -> full fast/slow gates when required
```

Record artifact head, command, result, and relevant configuration. Do not spend
full-suite cost before the focused defect is repaired; do not stop at one helper
test when a shared protocol or publication boundary changed.

## 10. Accrete what should survive

Before compaction, delegation, suspension, or finish, preserve:

```text
accepted decisions
expensive observations and exact refs
failed approaches that constrain future work
artifact changes
verification and tested state
open obligations with precise next action
invalidated or superseded conclusions
```

Do not preserve routine command mistakes, duplicate reads, or abandoned scratch
plans unless they expose a reusable constraint. Corrections append invalidation
or supersession; they do not edit old history.

## 11. Respond to resource pressure

- **context high:** use exact anchors and finish current reasoning; avoid broad
  transcript replay.
- **turns low:** stop speculative branches and prioritize done criteria.
- **wall low:** cancel non-critical work and run minimum decisive checks.
- **quota high:** preserve root affinity when feasible; spread only independent
  work.
- **cache warm estimate:** favor exact continuation for coupled work, but never
  call an estimate a cache hit.
- **delegation overhead high:** continue locally unless benefit is clear.

Unknown pressure is not low pressure.

## 12. Recover deliberately

After restart, provider migration, conflict, cancellation, or failed check:

1. establish accepted context epoch and artifact head;
2. identify stale claims and verification;
3. inspect salvage/history only where needed;
4. choose retry, migration, resolver, re-verification, or stop;
5. preserve failure classification and next action.

Recovery is incomplete if it restores code but loses obligations, or restores
semantic state on the wrong artifact.

## 13. Finish against the contract

Set `objective_met=true` only when:

```text
required outcomes exist
AND no blocking obligation remains
AND required verification applies to accepted artifact state
AND semantic result agrees with Git/publication state
```

A complete investigation that finds no defect may be complete. An edit with
unrun required checks is not. A critical child still running is not complete.

The final summary states what changed or was concluded, exact verification,
accepted branch/artifact outcome, and remaining concrete limitations.
