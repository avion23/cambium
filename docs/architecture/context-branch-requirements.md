# Context-branch requirements

**Status:** normative. `MUST`, `MUST NOT`, `SHOULD`, and `MAY` have their usual
requirements meaning. The architecture rationale is in
[`context-branches.md`](context-branches.md).

## 1. Terminology

- **Task tree:** parent/child ownership and scheduling structure.
- **Conversation branch:** one LM task's transcript, tool calls, and checkpoints.
- **Git graph:** artifact commits and integration order.
- **Cache lineage:** exact provider-request-prefix compatibility.
- **Trunk:** append-only semantic history plus a bounded raw tail.
- **Branch capsule:** bounded child result returned to the parent.
- **Tool reference:** `tool:<task-id>:<generation>:<turn>`.

These structures MUST remain distinct in code, events, documentation, and UI.

## 2. Recursive branch model

1. The root and every child MUST use the same worker/session abstraction.
2. A child MAY create children subject to the same depth, width, budget, and
   lifecycle bounds as the root.
3. A child proposal MUST contain a self-contained `task` and explicit
   `context_mode` and `placement` values.
4. There MUST be no implicit default, alias, or silent downgrade for those two
   policy fields.
5. A proposal that requests an impossible combination MUST be rejected before
   durable child admission.
6. `context_mode=trunk` MUST require `placement=inherit`.

## 3. Context modes

### 3.1 Trunk

- A trunk child MUST receive the complete immutable parent checkpoint prefix.
- Provider, model, protocol, reasoning mode, tool schema, system prompt,
  authorization identity, and prefix hashes MUST be exact-compatible.
- A failed compatibility check MUST reject the proposal; it MUST NOT silently
  become a semantic or fresh child.
- The parent provider/model lease MUST be inherited.

### 3.2 Semantic

- A semantic child MUST receive the immutable semantic trunk under a fresh
  provider-specific head.
- It MUST NOT claim an exact provider-cache hit merely because summary bytes
  match.
- The parent checkpoint MUST be present and suitable for semantic projection.
- `placement=spread` SHOULD remove the inherited provider/model pin and allow
  normal admission to prefer another feasible lane.

### 3.3 Fresh

- A fresh child MUST receive no parent checkpoint or summary-trunk reference.
- Its task contract and stable system/tool head MAY still include ordinary
  repository instructions and tool definitions.
- Fresh mode SHOULD be used for blind review, independent reproduction, or
  deliberate assumption isolation.

## 4. Placement

1. `inherit` MUST preserve parent provider affinity when a parent provider is
   known and feasible.
2. `spread` MUST remove an inherited hard provider pin.
3. `spread` SHOULD prefer a different credential-ready provider when one
   satisfies all hard task constraints.
4. `spread` MUST fall back to any feasible provider rather than fail solely
   because another provider is unavailable.
5. Natural-language prompt text MUST NOT bypass provider feasibility or select
   credentials directly.

## 5. Trunk and raw history

1. The semantic trunk MUST remain append-only within an epoch.
2. Existing trunk bytes MUST NOT be rewritten merely to inspect historical
   detail.
3. Raw tool calls, observations, and branch transcripts MUST remain outside the
   normal trunk unless summarized through the normal trunk protocol.
4. A history read MUST be appended as a temporary tool observation after the
   stable prefix.
5. A durable conclusion learned from history SHOULD be promoted through a later
   summary entry; the raw read itself MUST NOT mutate old entries.
6. No second evidence, memory, or search database may be introduced for this
   feature. Existing event logs and immutable checkpoints are authoritative.

## 6. Historical tool references

1. Every retrievable tool call MUST be identified by task branch, worker
   generation, and LM turn.
2. Listing tool calls MUST expose the stable reference.
3. Reopening a reference MUST return the corresponding durable tool-event
   metadata and, when available, the assistant tool action plus tool
   observation from the matching checkpoint.
4. A history read MUST NOT re-execute the historical tool call.
5. A malformed or missing reference MUST fail explicitly.
6. History queries MUST have deterministic row and byte bounds.
7. All task branches in the current session are readable; this feature has no
   per-branch access-control model.

## 7. Child result and artifact join

1. A branch capsule MUST remain bounded.
2. A parent MAY inspect the child's branch history after receiving the capsule.
3. Accepting a semantic result MUST NOT imply that the child's Git artifacts
   were integrated.
4. Parent resume after child code changes MUST verify the accepted integration
   head separately.
5. Completion order MUST NOT change deterministic admission or integration
   order.

## 8. Prompt ownership and optimization

1. Model-facing policy text MUST have one authoritative source in
   `src/cambium/prompts.py`.
2. Branch-decision and history-recall guidance MUST be named prompt components,
   not duplicated strings in worker code.
3. Each component SHOULD be independently replaceable by the DSPy optimization
   pipeline while the tool schemas and runtime invariants remain fixed.
4. Promotion of an optimized component MUST require held-out and canary
   evaluation.
5. Prompt optimization MUST NOT alter hard runtime validation.

## 9. Observability

The durable `child_admitted` and `context_fork` events MUST expose:

```text
parent_task_id
child_task_id
child_kind
context_mode
placement
```

When available they SHOULD also expose:

```text
provider/model
checkpoint epoch
exact compatibility
semantic reuse
```

The TUI SHOULD display task-tree parentage independently from context lineage
and provider placement.

## 10. Acceptance scenarios

### R1 — exact cached branch

```text
Given a parent with a compatible checkpoint
When it delegates trunk + inherit
Then the child receives context_fork
And assigned_provider equals the parent provider
And the full old prefix is byte-identical
```

### R2 — semantic spread branch

```text
Given a parent with an immutable semantic checkpoint
When it delegates semantic + spread
Then the child receives summary_trunk_ref
And it receives no context_fork
And inherited provider pinning is removed
And normal routing may choose another feasible provider
```

### R3 — fresh spread branch

```text
When a parent delegates fresh + spread
Then the child receives neither context_fork nor summary_trunk_ref
And inherited provider pinning is removed
```

### R4 — impossible request

```text
When a proposal declares trunk + spread
Then admission rejects it before child_admitted
And no worker is spawned
```

### R5 — branch-local tool recall

```text
Given a tool_event and matching checkpoint for task T, generation G, turn N
When branch_history lists tools
Then it emits tool:T:G:N
When that reference is reopened
Then the original assistant action and observation are returned
And the tool is not executed again
```

### R6 — recursive use

```text
Given a child branch within the configured depth bound
When that child delegates another explicit branch policy
Then the same admission, routing, checkpoint, and join rules apply
```

### R7 — cache-stable drill-down

```text
Given an existing trunk prefix P
When branch_history returns historical detail D
Then the next request is P + current-tail + D
And no byte inside P changes
```

## 11. Definition of done

```text
[ ] explicit child-policy parser has no defaults
[ ] delegate schema requires task/context_mode/placement
[ ] supervisor rejects impossible policy before spawn
[ ] trunk/semantic/fresh paths have focused tests
[ ] spread removes inherited provider pinning
[ ] branch_history lists branches and stable tool refs
[ ] branch_history reopens one tool action/observation
[ ] branch_history pages a task transcript
[ ] history uses only current event/checkpoint artifacts
[ ] prompt components are named and DSPy-ready
[ ] architecture/reference/how-to/evaluation docs agree with source
[ ] fast and slow CI suites pass
[ ] final remote main SHA is independently fetched and verified
```
