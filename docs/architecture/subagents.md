# Subagents and workload delegation

**Status:** current runtime contract with target control/result evolution labelled
explicitly. Source, executable scenarios, durable events, and accepted Git state
remain authoritative.

Cambium children are normal supervised branches. A provider may transport a
typed `delegate` action, but providers never own child process, credential,
filesystem, budget, context, join, or publication authority.

```text
model delegate action
        |
        v
schema + call-time tool validation
        |
        v
generation-local proposal buffer
        |
        v
supervisor validation and durable admission
        |
        v
worker process + isolated Git worktree
        |
        v
bounded result + ordered artifact join
```

## 1. One branch abstraction

The root and every child use the same conceptual record:

```text
task + authority + context + lease + resources + worktree
     + children + verification + result + artifact state
```

There is no provider-native subagent runtime and no special research, review, or
sub-main class. A task's `kind` is structural task-tree metadata; current
children use the same worker/prompt/tool roster and differ through task,
authority, context policy, placement, budgets, and parent relation.

## 2. Creation paths

### Static plan child

A plan may contain `depends_on`. The supervisor validates one rooted task tree,
dispatches deterministic ready waves, bounds width/depth, and prevents
descendants of a failed dependency from spawning.

### Model-requested child

Current model contract:

```json
{
  "child_task_id": "review-routing",
  "kind": "investigation",
  "spec": {
    "task": "Read-only review of routing. Return concrete defects and reproductions.",
    "context_mode": "semantic",
    "placement": "spread"
  }
}
```

Both policy fields are required by the model schema and validated again at tool
call and supervisor admission. The call only proposes. With context reuse, a
successful proposal may suspend the parent until the child result is joined.

Harness-originated static `proposed_children` may still omit both fields and use
the internal automatic compatibility path. This is not a model default. The
remaining target is to remove that path or expose an explicit internal
schema/event value.

### Architectus child

The optional Architectus port may propose children after a parent result. It is
a decision source, not a scheduler; every proposal crosses the normal admission
boundary.

### Conflict-resolver child

A structured merge conflict may create a bounded resolver branch with explicit
write authority over one integration attempt. Its result still passes worker
integrity, join, and publication checks.

## 3. Child task contract

A useful task states:

```text
objective       result to produce
ownership       files/symbols or read-only investigation area
done_when       observable completion criteria
verification    commands or evidence required
constraints     boundaries it must not cross
context_mode    trunk | semantic | fresh
placement       inherit | spread
budget          turns/wall and provider/task constraints
```

Current wire carries most of this inside `spec.task` plus policy and budget
fields. Avoid parallel free-text and structured owners that can disagree.

Parallel writers need disjoint ownership. Read-only investigations may overlap.
When two branches must touch one semantic area, use one writer plus a reviewer
or serialize the edits.

## 4. Context and placement

| Context mode | Context supplied | Cache claim |
| --- | --- | --- |
| `trunk` | exact immutable parent checkpoint prefix plus child task | exact provider prefix only after all compatibility checks pass |
| `semantic` | fresh provider-specific head plus immutable semantic trunk | semantic continuity, not a provider cache hit |
| `fresh` | task only, without parent checkpoint/trunk/result | no reuse |

| Placement | Meaning |
| --- | --- |
| `inherit` | preserve parent provider/model affinity when known and feasible |
| `spread` | remove inherited hard pinning, prefer another hard-feasible lane, then use the full feasible set |

Valid model pairs:

```text
trunk + inherit
semantic + inherit
semantic + spread
fresh + inherit
fresh + spread
```

`trunk + spread` is contradictory and rejected. An explicit trunk request that
cannot prove exact compatibility is rejected rather than downgraded.

## 5. Prompt and tool authority

Current branch context is composed from:

```text
stable system/tool head
+ task user message
+ optional bounded parent-result context
+ immutable CAST entries
+ bounded raw tail
+ exact fork/resume suffix when applicable
```

A child receives no hidden reasoning or sibling context. Exact fork bytes remain
byte-identical before the new child task. Semantic mode imports only immutable
semantic state under a fresh provider head.

Mutating file tools are confined to normal paths inside the assigned worktree.
`write_file` and `edit_file` reject parent paths, `.git`, `.cambium`, and symlink
escapes. This preserves branch ownership at the effect boundary rather than
relying on prompt compliance.

Target integration adds a deterministic late SituationFrame derived from
canonical BranchState. See
[`agent-operating-model.md`](agent-operating-model.md).

## 6. Lifecycle and structured concurrency

```text
parent running
    |
    | delegate proposal
    v
buffered under parent generation
    |
    | permitted boundary
    v
validate + persist child_admitted
    |
    v
register parent-owned completion future
    |
    v
spawn child in isolated worktree
    |
    v
validate terminal result and artifact
    |
    +--> semantic result join
    |
    +--> private ordered Git integration
              |
              v
      parent HEAD == accepted integration HEAD
              |
              v
          parent resume
```

The parent releases its worker slot while suspended. Parent wall budget still
bounds child wait/resume. A future is registered before spawn so a fast child
cannot finish in an unobservable gap.

A parent owns child lifetime. Cancellation, failure, restart, or admission
rollback resolves ownership exactly once. A child does not escape into a
session-global background task accidentally.

## 7. Semantic result, artifact, and verification

A child may produce:

- a **semantic result** for parent reasoning;
- an **artifact result** that may be integrated;
- **verification evidence** tied to the child or combined tree.

These require separate acceptance:

```text
child terminal result represented
AND artifact integration accepted when changed
AND parent worktree HEAD == accepted integration HEAD
AND required combined-tree verification applies
```

A summary cannot authorize publication. `files_changed` cannot prove parent
integration. A check run on the child head may become stale after combination.

## 8. Current result and target ResultCapsule

Current supervisor/worker exchange uses a strict bounded result envelope with
status, summary, diff evidence, commits, files, metrics, and parent identity.
Raw events/checkpoints remain the drill-down authority for recovery and history.

Target version 2 is a bounded `ResultCapsule` containing:

```text
status and concise outcome
evidence-linked claims and decisions
changed artifact and head
verification and tested head
open obligations and blockers
resource usage
recommended parent action
```

The migration must be versioned and preserve current readers. A capsule never
claims accepted parent integration.

## 9. Delegation control law

Delegate only when expected benefit exceeds coordination cost.

```text
benefit
  critical-path reduction
  + independent information gain
  + better provider/model fit
  + reusable context

cost
  context construction
  + spawn/queue
  + join/conflict risk
  + parent interpretation
  + combined verification
```

Continue locally when the edit is small, tightly coupled to the raw tail,
contends on the same files, or can be resolved by one direct tool call.

## 10. Historical inspection

`branch_history.py` already implements bounded projection over existing events
and immutable checkpoints. It is not yet an active model tool. `code_index.py`
and `lsp_query.py` are likewise implemented libraries awaiting `repo_query`
wiring.

The target normal path is:

```text
ResultCapsule
    -> inspect_state(children)
    -> branch_history branches/tools
    -> one exact tool:<task>:<generation>:<turn>:<batch-index>
    -> bounded transcript only when necessary
```

History reads must never re-execute a tool. Schema, dispatch, prompt/tool hash,
bounded output, durable observation, and public scenario land together.

## 11. Failures

- Invalid, duplicate, cyclic, too-deep, too-wide, unauthorized, or
  path-conflicting proposals spawn nothing.
- Missing model policy is rejected before proposal registration.
- Explicit impossible policy is rejected without downgrade.
- A child cannot widen filesystem, tool, credential, or provider authority.
- A failed child remains visible and a critical failure remains blocking.
- Parent timeout does not convert unfinished work into success.
- Cancellation is generation-fenced and durably acknowledged.
- Merge conflict evidence is bounded and ordered through one resolver owner.
- Completion order does not determine final integration order.

## 12. Source map

- Model schema: `src/cambium/schemas.py`
- Active tool validation/effects: `src/cambium/tools.py`
- Prompt and worker loop: `src/cambium/prompts.py`, `src/cambium/worker.py`
- Child policy values: `src/cambium/child_policy.py`
- Admission, lifetime, materialization, join, publication:
  `src/cambium/supervisor.py`
- Task-tree bounds: `src/cambium/tasktree.py`
- History/navigation libraries: `src/cambium/branch_history.py`,
  `src/cambium/code_index.py`, `src/cambium/lsp_query.py`
- Provider admission/execution: `src/cambium/routing.py`,
  `src/cambium/diffundo.py`
- Current operator projection: `src/cambium/observability.py`
- Target system: [`agent-operating-model.md`](agent-operating-model.md)
- Ordered work: [`../../implementation-plan.md`](../../implementation-plan.md)
