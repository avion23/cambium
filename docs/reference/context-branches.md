# Context branch reference

This document defines exact model-facing values. Rationale is in
[`../architecture/context-branches.md`](../architecture/context-branches.md).

## Delegate child policy

Every `delegate` call requires:

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
| `semantic` | Immutable summary trunk under a fresh provider head |
| `fresh` | No parent checkpoint or semantic history |

### `placement`

| Value | Meaning |
| --- | --- |
| `inherit` | Preserve parent provider/model affinity |
| `spread` | Prefer another feasible provider through normal admission |

Valid combinations:

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

There is no implicit default and no downgrade. A requested `trunk` branch that
cannot prove exact compatibility is rejected.

### Optional child constraints

A child spec may also contain the normal task fields, including:

```json
{
  "requirements": {
    "quality": "strong",
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

These constrain supervisor admission. They do not select credentials directly.

## Branch history tool

```json
{
  "name": "branch_history",
  "arguments": {
    "action": "branches | tools | tool | transcript",
    "task_id": "optional branch id",
    "ref": "optional tool reference",
    "offset": 0,
    "limit": 20
  }
}
```

### Actions

#### `branches`

Lists task branches discovered from durable session events.

```json
{"action":"branches","limit":20}
```

Example result:

```text
branches=3
branch:root parent=- status=succeeded provider=provider-a context=- placement=- tools=4 turn=12
branch:routing parent=root status=active provider=provider-b context=semantic placement=spread tools=2 turn=4
branch:tests parent=root status=succeeded provider=provider-a context=trunk placement=inherit tools=5 turn=8
```

#### `tools`

Lists stable historical tool references globally or for one branch.

```json
{"action":"tools","task_id":"routing","limit":20}
```

Example result:

```text
tool_calls=2
tool:routing:1:2 branch=branch:routing tool=read_batch ok=true duration_ms=7 cmd=read_batch {...}
tool:routing:1:3 branch=branch:routing tool=run_shell ok=true duration_ms=91 cmd=python -m pytest ...
```

#### `tool`

Reopens one historical tool action and its matching observation when a turn
checkpoint is available.

```json
{"action":"tool","ref":"tool:routing:1:2"}
```

Example result:

```text
tool:routing:1:2
branch=branch:routing generation=1 turn=2
tool=read_batch ok=true
assistant_action:
{"type":"tool_call","name":"read_batch","arguments":{"paths":["src/routing.py"]}}
tool_observation:
tool read_batch ok=True
...
```

Reading the reference never executes the tool again.

#### `transcript`

Pages the latest checkpoint transcript for one task branch.

```json
{
  "action":"transcript",
  "task_id":"routing",
  "offset":20,
  "limit":10
}
```

Use this only when branch summaries and specific tool references are
insufficient. It is deliberately more expensive than reopening one call.

## Stable references

### Branch reference

```text
branch:<percent-encoded-task-id>
```

### Tool reference

```text
tool:<percent-encoded-task-id>:<generation>:<turn>
```

Examples:

```text
branch:review-routing
tool:review-routing:1:7
tool:parser%3Awindows:2:11
```

Generation is part of the identity because a restarted worker can repeat the
same logical turn number.

## Source data

`branch_history` reads existing artifacts only:

```text
<session>/.cambium/events.db
<session>/.cambium/checkpoints/<task>/turn-NNN.json
interactive-root/turn-NNNN/.cambium/events.db
```

It does not create a database, index, embedding, summary, or cache entry.

## Prompt components

`src/cambium/prompts.py` exposes independently testable components:

```text
BRANCH_DECISION_POLICY
BRANCH_HISTORY_POLICY
SEMANTIC_SUMMARIZER
CODING_AGENT
```

`CODING_AGENT` composes the first three. DSPy experiments may optimize the
named policy strings, but tool schemas and runtime validation remain fixed.
