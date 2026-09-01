# Context branch reference

**Status:** current delegate values plus target state/history interfaces.
Rationale is in
[`../architecture/context-branches.md`](../architecture/context-branches.md).

## 1. Model-originated delegate policy

Current required shape:

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

The model schema requires `task`, `context_mode`, and `placement`.
`src/cambium/tools.py` validates policy again before registering the proposal,
and the supervisor validates it before durable admission.

### `context_mode`

| Value | Meaning |
| --- | --- |
| `trunk` | complete exact parent checkpoint prefix and raw tail |
| `semantic` | immutable semantic state under a fresh provider-specific head |
| `fresh` | task contract only; no parent checkpoint, trunk, or result context |

### `placement`

| Value | Meaning |
| --- | --- |
| `inherit` | preserve parent provider/model affinity when known and feasible |
| `spread` | prefer another hard-feasible lane, then use the complete feasible set |

Valid model combinations:

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

### Harness-only automatic compatibility

Harness-originated static `proposed_children` can currently omit both policy
fields. The internal path then attempts exact compatibility, otherwise semantic
reuse when suitable, otherwise no fork. Missing only one dimension is invalid.

This path is not part of the model tool contract. Open work must either remove
it or assign it an explicit schema/event value; omission should not remain a
long-term behavior carrier.

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
bytes, and provider boundary. The parent lease is inherited.

### Semantic

The child receives `summary_trunk_ref` and no exact `context_fork`. `inherit`
preserves the parent lease; `spread` removes inherited pinning before normal
hard-feasible provider admission.

### Fresh

The child receives neither `context_fork`, `summary_trunk_ref`, nor parent result
context. Placement is resolved independently.

## 3. Mutation authority

Current active file effects:

```text
write_file   create or replace one UTF-8 file
edit_file    replace exactly one occurrence in one UTF-8 file
```

Both resolve the target path and require it to remain a normal file path inside
the assigned worktree. The following are rejected:

```text
absolute path outside worktree
../ parent escape
.git or .cambium metadata
symlink resolving outside worktree
```

`read_batch` is a separate bounded inspection capability and may read permitted
external paths. It refuses the worker's own active session internals except for
normal files in its assigned worktree.

## 4. Current prompt exports

`src/cambium/prompts.py` exports:

```text
CODING_AGENT
SEMANTIC_SUMMARIZER
SUMMARY_PROTOCOL_LINES
PROMPTS_VERSION
```

The coding prompt states the mutation boundary and requires explicit model child
policy. Named orientation, history-recall, and repository-location prompt
components remain target work until they have live callers and evaluation.

## 5. History and navigation libraries

Current implemented library boundaries:

```text
src/cambium/branch_history.py   bounded event/checkpoint history projection
src/cambium/code_index.py       bounded portable repository navigation
src/cambium/lsp_query.py        optional one-shot LSP queries
```

They are not active model tools. End-to-end wiring still requires schema,
dispatch, prompt/tool hash, bounded durable observation, and a public scenario.

Canonical new tool reference:

```text
tool:<percent-encoded-task-id>:<generation>:<turn>:<batch-index>
```

Generation distinguishes restarts. The zero-based batch index distinguishes
several calls in one model action. New tooling must not create an omission-based
legacy form.

Target branch reference:

```text
branch:<percent-encoded-task-id>
```

A history read never re-executes an effect.

## 6. Target state interfaces

```text
SituationFrame   automatic bounded current operating picture
inspect_state    deeper current BranchState sections
branch_history   existing history projection as an active model tool
repo_query       existing index/LSP boundaries as an active model tool
ResultCapsule    versioned child result
ResourceEnvelope model-visible resource pressure and lease state
```

Current state, old evidence, and repository location remain separate tools. Each
must land with schema, dispatch, prompt/tool hash, bounded result, durable
observation, scenario coverage, and documentation in one vertical slice.

Exact target state shapes are in [`agent-state.md`](agent-state.md).

## 7. Current source map

```text
src/cambium/child_policy.py   context/placement values and parsers
src/cambium/schemas.py        model-facing delegate schema
src/cambium/tools.py          call-time validation and effects
src/cambium/prompts.py        current model instructions
src/cambium/worker.py         proposal buffering and suspension
src/cambium/supervisor.py     admission, fork materialization, join, publication
src/cambium/branch_history.py history projection library
src/cambium/code_index.py     portable navigation library
src/cambium/lsp_query.py      optional LSP boundary
```
