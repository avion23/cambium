# Decompose work with context branches

**Status:** current delegation and historical-inspection guide. Exact
current values and separately proposed extensions are in
[`../reference/context-branches.md`](../reference/context-branches.md).
The complete agent decision loop is in
[`agent-driving-loop.md`](agent-driving-loop.md).

## 1. Decide whether to stay in the current branch

Continue locally when the next step:

- depends on the current unsummarized observation;
- edits the same small ownership region;
- can be answered by one direct tool call;
- must complete before any other useful work;
- would cost less than child context, spawn, join, and verification.

Delegate when the work has an independent objective, ownership boundary,
definition of done, and enough expected benefit to repay coordination.

```text
next work
   |
   +-- coupled / tiny / one tool ----------> continue
   |
   +-- separable and useful in parallel ---> choose child policy
```

## 2. Choose context separately from placement

### Exact full context: `trunk + inherit`

Use when the child needs current project decisions, exact parent tail, or the
complete compatible provider prefix.

```json
{
  "type": "tool_call",
  "calls": [{
    "name": "delegate",
    "arguments": {
      "child_task_id": "implement-parser-window",
      "kind": "feature",
      "spec": {
        "task": "Own src/parser.py and focused parser tests. Add bounded offset/limit reads. Do not edit provider routing. Done when the focused regression and existing parser scenarios pass.",
        "context_mode": "trunk",
        "placement": "inherit"
      }
    }
  }]
}
```

An explicit trunk request fails closed if the parent epoch is not exactly
compatible. Use it for correctness, not merely because cache reuse sounds
cheap.

### Summary continuity on the same provider: `semantic + inherit`

Use when accepted decisions/facts are enough but provider affinity is still
valuable.

```json
{
  "name": "delegate",
  "arguments": {
    "child_task_id": "document-routing-contract",
    "kind": "docs",
    "spec": {
      "task": "Own docs/architecture/provider-routing.md only. Align it to accepted source facts and report any unresolved discrepancy. Verify links and git diff --check.",
      "context_mode": "semantic",
      "placement": "inherit"
    }
  }
}
```

### Summary continuity on another lane: `semantic + spread`

Use for separable work when another provider may improve throughput, capacity,
cost, or diversity.

```json
{
  "name": "delegate",
  "arguments": {
    "child_task_id": "audit-tui-resize",
    "kind": "investigation",
    "spec": {
      "task": "Read-only audit of TUI resize and narrow-terminal behavior. Return concrete defects, exact evidence, reproductions, and no production edits.",
      "context_mode": "semantic",
      "placement": "spread",
      "model_candidates": ["model-a", "model-b"]
    }
  }
}
```

Spread prefers another hard-feasible lane and falls back to the full feasible
set. It does not bypass credentials, authorization, capability, quota, or cash
constraints.

### Independent review: `fresh + spread`

Use when inherited assumptions are a liability.

```json
{
  "name": "delegate",
  "arguments": {
    "child_task_id": "blind-review-routing",
    "kind": "investigation",
    "spec": {
      "task": "Review src/cambium/routing.py from source and executable tests only. Do not rely on parent conclusions. Report claims with exact evidence and no edits.",
      "context_mode": "fresh",
      "placement": "spread"
    }
  }
}
```

`fresh + inherit` is also valid when the same provider/model is required but the
child must not inherit semantic assumptions.

## 3. Assign ownership before parallelism

Suppose a plan contains:

```text
wire history reads
improve provider spreading
redesign branch display
update documentation
```

Unsafe:

```text
four children may edit supervisor.py, schemas.py, README, and the same tests
```

Safer:

```text
root
├── history-runtime
│   owns: branch_history integration, tool schema/dispatch, focused tests
├── routing-review
│   read-only: reports policy/routing defects to root
├── tui-review
│   read-only: reports projection/layout changes
└── documentation
    owns: named documents after source decisions are accepted
```

A useful sequence:

```text
wave 1  independent implementation/reviews
wave 2  root integrates evidence and resolves overlap
wave 3  one documentation owner updates accepted behavior
wave 4  root runs combined verification
```

One writer owns a semantic area. Another child may review it without write
authority. When two edits truly depend on one another, serialize them.

## 4. Make the child task self-contained

A child task should state:

```text
objective
owned files/symbols or read-only area
forbidden scope
observable done criteria
verification/evidence required
```

Do not assume the child has the parent's hidden reasoning. Even a trunk child
receives an explicit task message; semantic and fresh children need enough
contract to work without the raw parent transcript.

## 5. Interpret the child result correctly

A child has two products:

```text
semantic result   claims/outcome for parent reasoning
artifact result   commits that may be integrated
```

The parent must not treat a summary or `files_changed` list as proof that its
worktree contains the child artifact. After an accepted code join, verify the
parent worktree head and run the required combined-tree checks.

Start from the existing bounded child result:

```text
child result envelope
    -> branch_history branches/tools
    -> one exact tool ref
    -> bounded transcript window
```

`branch_history` is an active worker tool. ResultCapsule-v2 and model
`inspect_state` are separate proposals, not prerequisites for these queries.
The examples below show tool invocations; wrap them in a normal `tool_call`
action when producing a text-protocol response.

### List branches

```json
{
  "name": "branch_history",
  "arguments": {"action": "branches"}
}
```

### List one branch's calls

```json
{
  "name": "branch_history",
  "arguments": {"action": "tools", "task_id": "routing-review", "limit": 20}
}
```

### Reopen one call

```json
{
  "name": "branch_history",
  "arguments": {"action": "tool", "ref": "tool:routing-review:1:6:0"}
}
```

The final numeric coordinate is the zero-based index within a batched action.
Interactive references also include `@turn-<number>`; preserve that suffix from
the listing to distinguish repeated counters across operator turns. Legacy
references without a batch index resolve to index zero, but an ambiguous
unscoped reference is rejected rather than returning unrelated evidence.

### Broader transcript window

```json
{
  "name": "branch_history",
  "arguments": {
    "action": "transcript",
    "task_id": "routing-review",
    "offset": 12,
    "limit": 8
  }
}
```

A historical read appends a normal bounded observation. It never rewrites an
old CAST entry or re-executes the tool.

## 6. Promote corrected knowledge

When exact history disproves a prior child or parent conclusion:

```text
old evidence remains immutable
        |
        v
reopen exact reference
        |
        v
reproduce against current accepted artifact when relevant
        |
        v
append fact invalidation / decision supersession / new obligation
```

Do not edit old summary text. Preserve the corrected conclusion and precise
next action in the next semantic delta.

## 7. Recursive delegation

The same rules apply at every depth:

```text
root
└── routing-review        semantic + spread
    └── quota-reproducer  trunk + inherit relative to routing-review
        └── header-check  fresh + spread
```

Each branch remains bounded by task-tree depth/width, parent lifetime, resource
budget, provider authority, isolated worktree, and ordered join rules.

## 8. Common mistakes

### Delegating a tiny local edit

Spawn/context/join cost dominates. Continue locally.

### Using semantic mode when raw tail matters

Semantic state cannot reconstruct omitted unsummarized detail. Use exact trunk
or keep the step in the parent.

### Using trunk only for hoped-for cache savings

Exact compatibility is a hard requirement and the child remains coupled to the
parent provider. Use it because complete state is needed.

### Creating overlapping writers

Parallel execution does not help when the root must untangle conflicts and
reverify both changes.

### Asking for a whole transcript first

Use the result, current state, and exact evidence refs before a broad transcript
window.

### Treating child verification as parent verification

A check run on the child head may be stale or incomplete after integration.
Verify the accepted combined artifact.

### Creating another memory database

Do not. Branch history and current state are derived from existing events,
checkpoints, CAST, and Git.

### Relying on omitted policy fields

The model schema already rejects omission at the delegate proposal boundary; a
supervisor-side automatic compatibility path remains only for harness-originated
specs. Declare policy explicitly either way.
