# Subagents and workload delegation

**Status:** current runtime contract with target control/capsule evolution clearly
labelled. Source and tests remain authoritative.

Cambium children are normal supervised tasks. Provider-native tool calls may
transport a typed `delegate` action, but providers never own child process,
credential, filesystem, budget, or publication authority.

```text
model delegate action
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

The root and every child use the same conceptual branch:

```text
task + authority + context + lease + resources + worktree
     + children + verification + result + artifact state
```

There is no provider-native subagent runtime and no special “sub-main” class.
A task's `kind` is structural task-tree metadata; current children use the same
coding worker/prompt/tool roster and differ through their task, authority,
context policy, placement, budgets, and parent result.

## 2. Creation paths

### Static plan child

A plan may contain `depends_on`. The supervisor validates one rooted task tree,
dispatches deterministic ready waves, bounds width, and prevents descendants of
a failed dependency from spawning.

### Model-requested child

Current accepted explicit shape:

```json
{
  "child_task_id": "review-routing",
  "kind": "investigation",
  "spec": {
    "task": "Read-only review of routing. Return concrete defects, evidence refs, and reproductions. Own no production files.",
    "context_mode": "semantic",
    "placement": "spread"
  }
}
```

The call only proposes. Admission occurs at a permitted parent lifecycle
boundary after validation of identity, task-tree bounds, paths, branch/worktree
ownership, provider authority, policy, and budget.

Current compatibility note: the active schema still permits `context_mode` and
`placement` to be omitted. The supervisor then performs automatic exact versus
semantic compatibility resolution. The target public model contract makes both
fields explicit; see `implementation-plan.md` Phase 0.

### Architectus child

The optional Architectus port may propose children after a parent result. It is
a decision source, not a second scheduler; every proposal crosses the normal
admission boundary.

### Conflict-resolver child

A structured merge conflict may create a bounded resolver branch with explicit
write authority over one integration attempt. Its result still passes worker
integrity and publication/join checks.

## 3. Child task contract

A useful child task makes these facts explicit:

```text
objective       result to produce
ownership       files/symbols or read-only investigation area
done_when       observable completion criteria
verification    commands or evidence required
constraints     boundaries it must not cross
context_mode    trunk | semantic | fresh
placement       inherit | spread
budget          turns/wall and normal task/provider constraints
```

Current wire carries most of this inside `spec.task` plus policy/budget fields.
The target evolves it into a typed task contract only after the versioned result
capsule is stable; avoid adding parallel free-text and structured owners that
can disagree.

Bad:

```text
Improve the code.
```

Good:

```text
Own src/cambium/routing.py and focused routing scenarios only.
Reproduce the lane-release defect, repair the owning transition, and run the
focused scenarios. Do not edit provider transports or documentation.
```

Parallel writers should have disjoint ownership. Read-only investigations may
overlap. When two branches must touch one semantic area, use one writer plus a
reviewer or serialize them.

## 4. Context policy

The model chooses context representation separately from provider placement.

| Context mode | Context supplied | Cache claim |
| --- | --- | --- |
| `trunk` | Exact immutable parent checkpoint prefix plus child task | May reuse provider prefix only when all compatibility checks pass |
| `semantic` | Fresh provider-specific head plus immutable semantic trunk | Cold semantic continuity, not a cache hit |
| `fresh` | New prompt with no parent checkpoint, semantic trunk, or parent envelope | No reuse |

| Placement | Meaning |
| --- | --- |
| `inherit` | Preserve parent provider/model affinity when known and feasible |
| `spread` | Remove inherited hard pinning, prefer another hard-feasible lane, then fall back to all feasible lanes |

Valid explicit combinations:

```text
trunk + inherit
semantic + inherit
semantic + spread
fresh + inherit
fresh + spread
```

`trunk + spread` is contradictory and rejected. An explicit trunk request that
cannot prove exact compatibility is rejected; it is not silently downgraded.

## 5. Prompt construction

Current branch context is composed from:

```text
stable system/tool head
+ task user message
+ optional bounded parent-result context
+ immutable CAST summary entries
+ bounded raw working tail
+ exact fork/resume suffix when applicable
```

A child does not receive hidden reasoning or sibling context. Exact fork bytes
remain byte-identical before the new child task. Semantic mode rebuilds a fresh
provider-specific head and imports only semantic entries.

Target integration adds a deterministic late SituationFrame derived from the
canonical BranchState. It exposes current mission, accepted context/artifact,
open obligations, children, resources, and evidence anchors without rewriting
the stable prefix. See [`agent-operating-model.md`](agent-operating-model.md).

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
rollback must resolve that ownership exactly once. A child never escapes into a
session-global background task by accident.

## 7. Semantic result versus artifact result

A child produces two different things:

- **semantic result:** bounded evidence/conclusions for parent reasoning;
- **artifact result:** commits that may be integrated into the parent workspace.

They require separate acceptance:

```text
child terminal result represented
AND artifact integration accepted when changed
AND parent worktree HEAD == accepted integration HEAD
AND required combined-tree verification applies
```

A summary cannot authorize publication. `files_changed` cannot prove the parent
contains the changes. Verification performed on the child head is not
necessarily sufficient for the combined parent tree.

## 8. Current result and target ResultCapsule

Current supervisor/worker exchange uses a strict bounded result envelope with
status, summary, diff evidence, commits, files, metrics, and parent identity.
Branch history remains available for drill-down.

Target version 2 is a `ResultCapsule` containing:

```text
status and concise outcome
claims and decisions with evidence refs
changed artifact/head information
verification and tested artifact head
open obligations and blockers
resource usage
recommended parent action
```

The migration must be versioned and preserve current readers. A capsule remains
much smaller than a transcript and never claims accepted parent integration.
Exact target fields are in
[`../reference/agent-state.md`](../reference/agent-state.md).

## 9. Delegation control law

A child is justified only when expected benefit exceeds coordination cost.

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

Delegate when:

- work has an independent objective and ownership region;
- a read-only audit can run concurrently;
- another feasible provider/model is materially better or idle;
- independent assumptions are valuable;
- a conflict needs separate resolver authority;
- exact compatible context makes a substantial child cheap.

Continue locally when:

- the edit is small and cohesive;
- the next step depends on the unsummarized tail;
- children would contend on the same files;
- a direct tool call resolves the uncertainty;
- the child cannot be given observable done criteria;
- spawn/join/verification cost dominates the work.

The target SituationFrame exposes critical-path child state and relative
delegation overhead so this decision does not depend on prompt folklore.

## 10. Progressive child inspection

The normal parent path is:

```text
ResultCapsule
    -> inspect_state(children)
    -> branch_history(branches/tools)
    -> one exact historical tool ref
    -> bounded transcript window only when necessary
```

Historical reads do not execute the tool again or rewrite the parent's CAST
trunk. A durable correction discovered through history is appended later as a
new fact/decision/invalidation delta.

Current `branch_history.py` implements the read-only projection, but it is not
yet in the active worker tool roster. Until wiring lands, do not describe the
model as able to call it.

## 11. Failures

- Invalid, duplicate, cyclic, too-deep, too-wide, unauthorized, or
  path-conflicting proposals spawn nothing.
- An explicit impossible context policy is rejected.
- A child cannot widen credential/provider or filesystem authority.
- A failed child produces a bounded failure result and remains visible to the
  parent.
- A critical child failure remains a blocking obligation unless the parent
  changes the plan.
- Parent timeout does not convert unfinished child work into success.
- Cancellation is generation-fenced and acknowledged durably.
- Merge conflict evidence is bounded and ordered through resolver authority.
- Child completion order does not determine final integration order.

## 12. Observability

Current durable events expose task/child identity, lifecycle, context fork,
provider usage, result, merge, recovery, and session end. The operator TUI
projects child parentage, lifecycle, lineage, provider/model, usage, and quota.

Target BranchState/SituationFrame adds shared semantics:

```text
admission index and critical-path flag
objective/ownership/done criteria
open obligations/blockers
resolved context mode and placement
semantic-result status
artifact-integration status and accepted head
verification status and tested head
resource contribution
```

The model and operator must agree on shared fields at one event watermark.

## 13. Source map

- Tool schema and validation: `src/cambium/schemas.py`
- Active tool dispatch: `src/cambium/tools.py`
- Worker prompt and delegation suspension: `src/cambium/prompts.py`,
  `src/cambium/worker.py`
- Child policy: `src/cambium/child_policy.py`
- Admission, policy materialization, lifetime, join, and publication:
  `src/cambium/supervisor.py`
- Task-tree bounds: `src/cambium/tasktree.py`
- Provider admission: `src/cambium/routing.py`
- Call-time provider execution: `src/cambium/diffundo.py`
- Current operator projection: `src/cambium/observability.py`
- Historical branch projection: `src/cambium/branch_history.py`
- Target system: [`agent-operating-model.md`](agent-operating-model.md)
- Ordered work: [`../../implementation-plan.md`](../../implementation-plan.md)
