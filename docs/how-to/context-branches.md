# Decompose work with context branches

Use this guide when a root task or `plan.md` contains several separable
problems. The exact schema is in
[`../reference/context-branches.md`](../reference/context-branches.md).

## 1. Decide whether to stay in the current branch

Continue locally when the next step:

- depends on the current unsummarized tool result;
- edits the same small ownership region;
- takes fewer calls than a child would need to reconstruct and verify;
- must be ordered before any other useful work.

Delegate when the work has an independent objective, ownership boundary, and
definition of done.

```text
current step
   |
   +-- tightly coupled / tiny ----------> continue
   |
   +-- separable and worth parallelism --> choose child policy
```

## 2. Choose the child policy

### Default: complete cached trunk

Use this for most substantial children that need project/task context.

```json
{
  "type": "tool_call",
  "name": "delegate",
  "arguments": {
    "child_task_id": "implement-parser-window",
    "kind": "feature",
    "spec": {
      "task": "Own src/parser.py and focused parser tests. Add bounded offset/limit reads. Do not edit provider routing. Done when the new focused tests and existing parser scenarios pass.",
      "context_mode": "trunk",
      "placement": "inherit"
    }
  }
}
```

Why: the full trunk is already small and information-dense; on the same
provider its old prefix is usually the cheapest and most complete context.

### Independent semantic child on another subscription

Use this when the summaries contain enough state and the task can run in
parallel on another feasible provider.

```json
{
  "type": "tool_call",
  "name": "delegate",
  "arguments": {
    "child_task_id": "audit-tui-resize",
    "kind": "investigation",
    "spec": {
      "task": "Inspect only TUI resize and narrow-terminal behavior. Own no production files. Produce concrete defects, reproducing tests, and recommended minimal fixes.",
      "context_mode": "semantic",
      "placement": "spread",
      "model_candidates": ["model-a", "model-b"]
    }
  }
}
```

Why: summary continuity is enough, while provider spreading increases total
throughput and consumes otherwise idle subscription capacity.

### Fresh independent review

Use this to avoid inherited assumptions.

```json
{
  "type": "tool_call",
  "name": "delegate",
  "arguments": {
    "child_task_id": "blind-review-routing",
    "kind": "investigation",
    "spec": {
      "task": "Review src/cambium/routing.py from first principles. Do not read parent conclusions. Report only defects supported by source or executable tests.",
      "context_mode": "fresh",
      "placement": "spread"
    }
  }
}
```

## 3. Split a large `plan.md`

Suppose the plan contains:

```text
- add branch history reads
- improve provider spreading
- redesign TUI branch display
- document the architecture
```

Do not create four writers that all edit the same files. Assign ownership:

```text
root
├── history-runtime
│   owns: branch_history.py, tools.py, history tests
├── routing-review
│   owns: child_policy.py, supervisor routing tests
├── tui-review
│   read-only: reports changes to root
└── documentation
    owns: docs/context-branch documents
```

A safe wave is:

```text
wave 1: history-runtime || routing-review || tui-review
wave 2: root integrates findings and fixes overlap
wave 3: documentation reflects the accepted implementation
wave 4: root runs full verification
```

The root should delegate `trunk+inherit` to a child that needs the complete
architecture context. Use `semantic+spread` for a clearly bounded review or
documentation task. Use `fresh+spread` for an independent second opinion.

## 4. Inspect a child after it returns

Start with the child's bounded result. Drill down only when necessary.

### List branches

```json
{
  "type":"tool_call",
  "name":"branch_history",
  "arguments":{"action":"branches"}
}
```

### List one branch's calls

```json
{
  "type":"tool_call",
  "name":"branch_history",
  "arguments":{"action":"tools","task_id":"routing-review","limit":20}
}
```

### Reopen the suspicious call

```json
{
  "type":"tool_call",
  "name":"branch_history",
  "arguments":{"action":"tool","ref":"tool:routing-review:1:6"}
}
```

### Read a broader transcript window

```json
{
  "type":"tool_call",
  "name":"branch_history",
  "arguments":{
    "action":"transcript",
    "task_id":"routing-review",
    "offset":12,
    "limit":8
  }
}
```

The returned detail becomes a normal tool observation at the end of the active
request. It does not rewrite the cached trunk.

## 5. Promote a recovered conclusion

Suppose a reopened test call shows that an earlier child summary was wrong.
The root should:

1. inspect the exact call and relevant transcript window;
2. reproduce the result in the current accepted worktree if it affects code;
3. state the corrected conclusion;
4. let the normal semantic flush add `facts_invalidated` or
   `decisions_superseded` and the new fact;
5. keep the raw old branch unchanged.

```text
old branch evidence ----------- immutable
             |
             v
branch_history read ----------- temporary suffix
             |
             v
new verified conclusion ------- next trunk delta
```

## 6. Recursive delegation

A child can use the same rules:

```text
root
└── routing-review        semantic + spread
    └── quota-reproducer  trunk + inherit relative to routing-review
        └── header-check  fresh + spread
```

Each branch is bounded by the same supervisor depth, fan-out, token, wall, and
join rules. There is no special “sub-main” class.

## 7. Common mistakes

### Delegating a tiny local edit

The context/spawn/join overhead dominates. Continue locally.

### Using semantic mode merely because another provider exists

Use it only when the child can work from summary state. Otherwise use the full
trunk and preserve correctness.

### Asking `transcript` before `tool`

A whole transcript is usually much larger. List calls and reopen the exact one
first.

### Treating a child summary as artifact proof

After child code changes, verify the accepted Git head and run combined-tree
tests.

### Writing retrieved history into the system prompt

Do not rebuild the stable instruction prefix. The history tool already returns
it as an ordinary suffix observation.

### Creating another memory/index database

Do not. Branch history is reconstructed from existing event and checkpoint
artifacts.
