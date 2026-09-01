# Decompose work with context branches

**Status:** current delegate-policy guide with target inspection steps labelled.
Exact values are in
[`../reference/context-branches.md`](../reference/context-branches.md).

## 1. Decide whether to stay local

Continue in the current branch when the next step:

- depends on the current unsummarized observation;
- edits the same small ownership region;
- can be answered by one direct tool call;
- must complete before any other useful work;
- costs less than child context, spawn, join, interpretation, and verification.

Delegate when the work has an independent objective, ownership boundary,
definition of done, and enough expected benefit to repay coordination.

```text
next work
   |
   +-- coupled / tiny / one tool ----------> continue
   |
   +-- separable and useful in parallel ---> choose child policy
```

## 2. Always declare context and placement

Current model-originated `delegate` calls require both policy dimensions.

### `trunk + inherit`

Use when the child needs the exact parent checkpoint/raw tail and same-provider
cache affinity.

```json
{
  "name": "delegate",
  "arguments": {
    "child_task_id": "implement-parser-window",
    "kind": "feature",
    "spec": {
      "task": "Own src/parser.py and focused parser tests. Add bounded offset/limit reads. Do not edit provider routing. Done when focused and existing parser scenarios pass.",
      "context_mode": "trunk",
      "placement": "inherit"
    }
  }
}
```

An explicit trunk request fails closed when exact compatibility cannot be
proved. It never silently becomes semantic or fresh.

### `semantic + inherit`

Use when accepted decisions/facts are enough and provider affinity remains
valuable.

```json
{
  "name": "delegate",
  "arguments": {
    "child_task_id": "document-routing-contract",
    "kind": "docs",
    "spec": {
      "task": "Own docs/architecture/provider-routing.md only. Align it to accepted source facts and verify links and git diff --check.",
      "context_mode": "semantic",
      "placement": "inherit"
    }
  }
}
```

### `semantic + spread`

Use for separable work that needs project decisions and can benefit from another
hard-feasible provider lane.

```json
{
  "name": "delegate",
  "arguments": {
    "child_task_id": "audit-tui-resize",
    "kind": "investigation",
    "spec": {
      "task": "Read-only audit of resize and narrow-terminal behavior. Return concrete defects and reproductions; make no production edits.",
      "context_mode": "semantic",
      "placement": "spread",
      "model_candidates": ["model-a", "model-b"]
    }
  }
}
```

Spread prefers another feasible lane, then falls back to the complete feasible
set. It does not bypass credentials, authorization, capability, quota, cash, or
context limits.

### `fresh + spread`

Use when independence is the purpose: blind review, clean reproduction, or
assumption isolation.

```json
{
  "name": "delegate",
  "arguments": {
    "child_task_id": "blind-review-routing",
    "kind": "investigation",
    "spec": {
      "task": "Review routing from source and executable tests only. Do not rely on parent conclusions. Report evidence and make no edits.",
      "context_mode": "fresh",
      "placement": "spread"
    }
  }
}
```

`fresh + inherit` is also valid when the same provider/model is required but the
child must not inherit semantic assumptions.

Invalid:

```text
trunk + spread
```

## 3. Assign ownership before parallelism

Unsafe:

```text
several children may edit supervisor.py, schemas.py, README, and the same tests
```

Safer:

```text
root
├── one implementation owner
├── one read-only routing reviewer
├── one read-only TUI reviewer
└── one documentation owner after source decisions settle
```

Use one writer per semantic area. A reviewer may overlap read authority. When
edits depend on one another, serialize them.

## 4. Make the task self-contained

State:

```text
objective
owned files/symbols or read-only area
forbidden scope
observable done criteria
verification/evidence required
```

Do not assume the child has hidden parent reasoning. Even a trunk child receives
an explicit task message; semantic and fresh children need a complete contract.

## 5. Interpret child results correctly

```text
semantic result   claims/outcome for parent reasoning
artifact result   commits that may be integrated
verification      checks tied to child or combined artifact state
```

A summary or `files_changed` list does not prove that the parent contains the
child artifact. After an accepted code join, confirm the parent worktree head
and run required combined-tree checks.

The target inspection ladder is:

```text
ResultCapsule
    -> inspect_state(children)
    -> branch_history branches/tools
    -> one exact tool:<task>:<generation>:<turn>:<batch-index>
    -> bounded transcript window
```

`branch_history.py` already implements bounded history projection, while
`code_index.py` and `lsp_query.py` provide navigation libraries. They are not
active model tools. The examples become live only when schema, dispatch,
prompt/tool hash, durable observation, and public scenarios are wired.

```json
{
  "name": "branch_history",
  "arguments": {"action": "tool", "ref": "tool:routing-review:1:6:0"}
}
```

The final coordinate identifies one call in a batched model action. A historical
read never re-executes the tool or rewrites old CAST entries.

## 6. Promote corrected knowledge

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
append invalidation / supersession / verification / obligation
```

Do not edit old summary text. Preserve the corrected conclusion and exact next
action in a new semantic delta.

## 7. Recursive delegation

The same rules apply at every depth:

```text
root
└── routing-review        semantic + spread
    └── quota-reproducer  trunk + inherit relative to routing-review
        └── header-check  fresh + spread
```

Every branch remains bounded by task-tree depth/width, parent lifetime, resource
budget, provider authority, isolated worktree, and ordered join rules.

## 8. Current internal automatic path

Harness-originated static `proposed_children` can still omit both policy fields
and enter internal automatic compatibility logic. Model calls cannot. Do not
rely on omission in new code or examples; remaining work is to assign that
internal mode an explicit schema/event value or remove it.

## 9. File-effect boundary

`write_file` and `edit_file` mutate only normal paths inside the assigned
worktree. They reject absolute external paths, `..` traversal, `.git`,
`.cambium`, and symlink escapes. Do not work around this ownership boundary.

`read_batch` remains a bounded inspection tool and may read permitted external
paths.

## 10. Common mistakes

- **Delegating a tiny local edit:** coordination cost dominates.
- **Using semantic mode when raw tail matters:** use trunk or keep work local.
- **Using trunk only for hoped-for savings:** exact compatibility is a hard
  requirement and couples the child to the parent provider.
- **Creating overlapping writers:** parallel execution becomes conflict and
  repeated verification.
- **Treating child checks as parent checks:** integration may stale them.
- **Reading a whole transcript first:** start from bounded result/current state
  and exact refs.
- **Creating another memory database:** derive state/history from events,
  checkpoints, CAST, and Git.
