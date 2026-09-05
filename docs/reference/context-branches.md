# Context and navigation reference

**Status:** current model tools and context-policy values. Rationale belongs in
[context branches](../architecture/context-branches.md), lifecycle in
[child agents](../architecture/subagents.md), and proposed state shapes in
[agent-state](agent-state.md).

## Delegate policy

```json
{
  "name": "delegate",
  "arguments": {
    "child_task_id": "routing-review",
    "kind": "investigation",
    "spec": {
      "task": "Review routing.py without edits. Return a concrete defect, reproduction, and source evidence, or report none found.",
      "context_mode": "semantic",
      "placement": "spread"
    }
  }
}
```

Policy fields are optional. One delegate defaults to `trunk + inherit`; several
in one action batch default to `semantic + spread`. Explicit semantic/fresh
context defaults to spread; explicit spread defaults to semantic context.
The worker records the resolved policy before admission. The supervisor derives
repo, worktree, branch and inherited execution settings. See
[automatic delegation](../architecture/context-branches.md).

| `context_mode` | Meaning |
| --- | --- |
| `trunk` | Complete compatible parent checkpoint prefix; requires `inherit` |
| `semantic` | Immutable semantic summaries under a fresh provider head |
| `fresh` | Task only; no parent checkpoint, summaries, or result envelope |

| `placement` | Meaning |
| --- | --- |
| `inherit` | Preserve parent provider/model affinity when known and feasible |
| `spread` | Prefer another feasible provider; otherwise use the feasible set |

`trunk+spread` is invalid. An incompatible explicit `trunk+inherit` request is
rejected, not silently changed to another mode. Internal harness-originated
specifications retain an automatic compatibility path; it is not a model-tool
default.

Optional provider requirements and task budgets further constrain admission;
they cannot widen the parent's provider authority. Exact accepted fields are in
[schemas.py](../../src/cambium/schemas.py). `kind` is validated as a task-tree
kind, not a special prompt or separate worker implementation.

## `repo_query`

This is a live tool backed by `code_index.py` and the optional configured LSP
transport. Its required `action` is one of:

| Action | Useful arguments | Result |
| --- | --- | --- |
| `tree` | `path`, `limit` | Bounded source-file locations |
| `search` | `query`, `path`, `limit` | Literal text matches |
| `symbols` | `query`, `path`, `exact`, `limit` | Source declaration locations |
| `references` | `query`, `path`, `limit` | Identifier uses, not semantic references |
| `window` | `path`, `line`, `limit` | Nearby source lines |
| `lsp` | `method`, `path`, `line`, `column` | Configured language-server query |

Line and column are one-based; `limit` is 1–100. LSP methods are `definition`,
`references`, `hover`, `document_symbols`, and `diagnostics`. No configured server
means unavailable, not guessed semantic results or an automatic installation.

```json
{"name":"repo_query","arguments":{"action":"symbols","query":"select_lane","limit":10}}
```

Use `read_batch` to read exact relevant regions after locating them. Ordinary
repository navigation does not require an embedding service or a second index
store.

## `branch_history`

This is a live read-only worker tool. It reads existing session events and
checkpoints, including previous turns of an interactive session.

```json
{"name":"branch_history","arguments":{"action":"tools","task_id":"routing-review","offset":0,"limit":20}}
```

| Action | Required detail | Meaning |
| --- | --- | --- |
| `branches` | None | Branch identity, parent, lifecycle, provider and context policy |
| `tools` | Optional `task_id` | Recorded tool calls and stable refs |
| `tool` | `ref` | Reopen one recorded action/observation, without running it |
| `transcript` | `task_id` | Bounded checkpoint transcript window |

`offset` is zero-based. `limit` is 1–64. A batched action has an independently
addressable reference for each tool call. Start with a list or exact reference
rather than requesting the whole transcript.

## Stable history references

```text
branch:<percent-encoded-task-id>
tool:<percent-encoded-task-id>:<generation>:<turn>:<batch-index>
tool:<percent-encoded-task-id>:<generation>:<turn>:<batch-index>@turn-NNNN
```

Examples:

```text
branch:review-routing
tool:review-routing:1:7:0
tool:parser%3Awindows:2:11:3
tool:interactive:1:2:0@turn-0002
```

Generation distinguishes repeated turn numbers after restart. Batch index is
zero-based. Interactive sessions append the turn-directory identity because
task, generation, and model-turn counters can repeat across user turns. Reopen
the reference returned by `tools`; do not remove its suffix. An unscoped
reference matching several interactive turns is rejected as ambiguous rather
than returning whichever event happened to be last.

Older three-coordinate tool references remain readable as index zero when
unambiguous because existing durable sessions use them.

## Tools, prompts, and future state

The active schema exposes `write_file`, `edit_file`, `git_op`, `run_shell`,
`read_batch`, `repo_query`, `branch_history`, and `delegate`. A tool's internal
implementation filename is not another public tool name.

[prompts.py](../../src/cambium/prompts.py) exports `CODING_AGENT`,
`SEMANTIC_SUMMARIZER`, `SUMMARY_PROTOCOL_LINES`, and `PROMPTS_VERSION`. It does
not have separate branch-planner/history-policy prompt components. A small task
may start with a tool or a valid finish; a plan is optional.

`branch_state.py` and CLI `inspect-state` provide current inspection. A
model-facing `inspect_state`, unified SituationFrame/operator projection, typed
WorkLedger, and richer ResultCapsule are separate work tracked in
[the implementation plan](../../implementation-plan.md); do not infer their wire
support from target names in [agent-state](agent-state.md).

## Executable anchors

[Schema](../../src/cambium/schemas.py), [dispatch](../../src/cambium/tools.py),
[repository query](../../src/cambium/code_index.py),
[history query](../../src/cambium/branch_history.py),
[navigation scenarios](../../tests/scenarios/test_navigation_tools.py),
[history scenarios](../../tests/scenarios/test_branch_history.py).
