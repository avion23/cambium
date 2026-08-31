# Context branch reference

**Status:** current values plus target public-contract notes. Rationale is in
[`../architecture/context-branches.md`](../architecture/context-branches.md)
and the integrated model is in
[`../architecture/agent-operating-model.md`](../architecture/agent-operating-model.md).

## 1. Delegate child policy

Current accepted explicit shape:

```json
{
  "child_task_id": "stable-child-id",
  "kind": "investigation",
  "spec": {
    "task": "Objective, ownership boundary, done criteria, and verification.",
    "context_mode": "trunk",
    "placement": "inherit"
  }
}
```

### `context_mode`

| Value | Meaning |
| --- | --- |
| `trunk` | Complete exact parent checkpoint prefix |
| `semantic` | Immutable semantic summary state under a fresh provider head |
| `fresh` | No parent checkpoint, semantic trunk, or parent-result context |

### `placement`

| Value | Meaning |
| --- | --- |
| `inherit` | Preserve parent provider/model affinity when known and feasible |
| `spread` | Prefer another hard-feasible provider, then fall back to all feasible providers |

Valid explicit combinations:

```text
trunk    + inherit
semantic + inherit
semantic + spread
fresh    + inherit
fresh    + spread
```

Invalid:

```text
trunk + spread
```

An explicit `trunk + inherit` request that cannot prove exact compatibility is
rejected. It does not silently become semantic or fresh.

### Current omission compatibility

The active model schema currently permits `context_mode` and `placement` to be
omitted. In that case the supervisor uses automatic compatibility behavior:

```text
exact compatible checkpoint -> exact inherited fork
otherwise suitable semantic checkpoint -> semantic reuse with inherited pin removed
otherwise -> no fork
```

This is current compatibility behavior, not the target public contract. Phase 0
of `../../implementation-plan.md` makes model-originated policy explicit or
assigns the automatic path a separate named internal policy. Do not describe
missing fields as an intentional model decision.

### Optional child constraints

```json
{
  "requirements": {
    "quality": "high",
    "min_context_window": 100000,
    "allow_paid": true
  },
  "model_candidates": ["model-a", "model-b"],
  "authorized_providers": ["provider-a", "provider-b"],
  "authorized_providers_explicit": true,
  "max_turns": 20,
  "max_wall_s": 900
}
```

These constrain supervisor admission. They do not select credentials directly
and cannot widen parent authority.

## 2. Resolved context behavior

### Trunk

The child receives an exact `context_fork` descriptor containing checkpoint,
provider/model, stable-prompt/tool hashes, prefix/suffix/full hashes, prefix
bytes, and provider boundary. The provider/model lease is inherited.

### Semantic

The child receives `summary_trunk_ref`, no exact `context_fork`, and a fresh
provider-specific head. `inherit` preserves the parent lease; `spread` removes
inherited pinning and lets normal admission prefer another feasible lane.

### Fresh

The child receives neither `context_fork`, `summary_trunk_ref`, nor the parent
result envelope. Placement is resolved independently.

## 3. Branch-history projection

`src/cambium/branch_history.py` implements a bounded read-only projection over
existing session events and checkpoints.

Query shape used by the library boundary:

```json
{
  "action": "branches | tools | tool | transcript",
  "task_id": "optional branch id",
  "ref": "optional tool reference",
  "offset": 0,
  "limit": 20
}
```

Current integration status: this implementation is **not yet in the active
worker tool schema/dispatch roster**. It is therefore not a current model tool
until Phase 3 wires schema, dispatch, worker init, prompt, provider tool hash,
and scenarios.

### `branches`

Lists branch identity, parent, lifecycle, provider, context mode, placement,
tool count, and last turn from durable events.

```text
branches=3
branch:root parent=- status=succeeded provider=provider-a context=- placement=- tools=4 turn=12
branch:routing parent=root status=active provider=provider-b context=semantic placement=spread tools=2 turn=4
```

### `tools`

Lists stable historical tool references globally or for one task branch. A
batched model action has one independently addressable reference per call.

```text
tool_calls=2
tool:routing:1:2:0 branch=branch:routing tool=read_batch ok=true duration_ms=7
tool:routing:1:3:0 branch=branch:routing tool=run_shell ok=true duration_ms=91
```

### `tool`

Reopens one matching tool event and, when present in a checkpoint, the original
assistant action and the corresponding batched tool observation. It never
executes the tool again.

```text
tool:routing:1:2:0
branch=branch:routing generation=1 turn=2 batch_index=0
tool=read_batch ok=true
assistant_action:
{"type":"tool_call","calls":[{"name":"read_batch","arguments":{"paths":["src/routing.py"]}}]}
tool_observation:
tool read_batch ok=True
...
```

### `transcript`

Returns one bounded checkpoint transcript window for a branch. Use it only when
the branch capsule and exact tool refs are insufficient.

## 4. Stable references

### Branch

```text
branch:<percent-encoded-task-id>
```

### Tool

Canonical current form:

```text
tool:<percent-encoded-task-id>:<generation>:<turn>:<batch-index>
```

Examples:

```text
branch:review-routing
tool:review-routing:1:7:0
tool:parser%3Awindows:2:11:3
```

Generation is part of identity because a restarted worker can repeat a logical
turn number. Batch index is zero-based and distinguishes calls emitted in one
batched action.

The legacy three-coordinate suffix remains accepted:

```text
tool:<percent-encoded-task-id>:<generation>:<turn>
```

It resolves to batch index zero. Events written before batch indexes existed are
treated the same way.

Target additional evidence-reference forms are defined in
[`agent-state.md`](agent-state.md).

## 5. Source artifacts

Branch-history projection reads only existing artifacts:

```text
<session>/.cambium/events.db
<session>/.cambium/checkpoints/<task>/turn-NNN.json
interactive-root/turn-NNNN/.cambium/events.db
```

It does not create an embedding, database, summary, index, or cache entry.

## 6. Current prompt exports

`src/cambium/prompts.py` currently exports:

```text
CODING_AGENT
SEMANTIC_SUMMARIZER
SUMMARY_PROTOCOL_LINES
PROMPTS_VERSION
```

Earlier documents named separate branch-decision and history-recall prompt
components that do not currently exist as exports. Phase 0 either adds real
independently testable components or removes those claims. Target optimization
components are listed in
[`../research/agent-system-evaluation.md`](../research/agent-system-evaluation.md).

## 7. Target state interfaces

The integrated target adds:

```text
SituationFrame   automatic bounded current operating picture
inspect_state    deeper current BranchState sections
branch_history   historical exact evidence
repo_query       bounded repository location/navigation
ResultCapsule    versioned child result
ResourceEnvelope model-visible resource pressure and lease state
```

Exact target shapes are in [`agent-state.md`](agent-state.md). None of those
target values should be inferred as current wire support solely from this
reference.
