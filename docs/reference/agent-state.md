# Agent state reference

**Status:** current `BranchState`, SituationFrame, and `inspect_state` contracts.
Future WorkLedger/ResultCapsule ideas are research topics, not runtime wire
formats. Rationale belongs in the
[agent operating model](../architecture/agent-operating-model.md).

## Identity and authority

One branch state is scoped by independent runtime identities:

```text
session_id
branch_id          normally task_id
parent_branch_id
generation         worker ownership/fencing generation
turn               model decision turn in that generation
context epoch      accepted CAST checkpoint epoch
artifact head      accepted Git integration head
source watermark   latest durable event sequence folded into this state
```

A matching generation does not imply a matching context epoch or Git head.
Likewise, a child result does not imply that its artifact was accepted.

Authority is split by owner:

| Fact | Owner |
| --- | --- |
| task objective and declared constraints | caller / admitted parent contract |
| lifecycle, generation, child admission | supervisor |
| tool observation | tool boundary + durable event |
| provider usage/cache hit | provider response normalized by transport |
| checkpoint identity | worker writer, validated by supervisor |
| accepted artifact | Git + supervisor publication/join |
| SituationFrame | deterministic projection of branch state |
| TUI row | renderer over recorded/projected state |

## BranchState

`src/cambium/branch_state.py` folds durable events into immutable
`BranchState(schema_version=1)`. It does not inspect Git or execute tools while
reducing state.

The current top-level groups are:

| Group | Current contents |
| --- | --- |
| `identity` | session/branch/parent ids, generation, lifecycle, turn |
| `mission` | objective, constraints, done criteria, verification contract |
| `authority` | repo, worktree, branch, writable scope, tools, allowed providers |
| `context` | epoch, checkpoint, lineage, cache descriptor, summary/raw-tail facts |
| `artifacts` | base/worktree/accepted heads and dirty state |
| `control` | plan, current step, open obligations, blockers, last delta |
| `knowledge` | recorded observation/claim/decision/obligation/verification refs |
| `children` | ordered child lifecycle, context/placement, provider, result and usage |
| `resources` | remaining budgets, provider/model, cache/quota/cash pressure |
| `usage` | calls, summaries, failures, tokens, cache hits, latency and output rate |
| `tool_events` | bounded durable tool invocations/results |
| `result` | bounded worker/supervisor result projection when one exists |
| `anchors` | stable evidence references retained by the reducer |

Use CLI `inspect-state` for the exact JSON serialization instead of copying a
second schema into documentation:

```sh
cambium inspect-state /path/to/session
cambium inspect-state /path/to/session child-task-id
```

Public lifecycle values are:

```text
unknown queued starting active suspended joining verifying publishing
succeeded failed cancelled rejected
```

Terminal values do not move backward during replay.

## Shared state reader

`src/cambium/state_view.py` is the read-only session adapter used by the model,
CLI, and TUI.

It reads the latest relevant interactive turn rather than sorting sequence
numbers from different turn-local event stores together. When a `task_id` is
supplied it replays that branch plus its descendants. A running task with no
result remains a valid active state; absence of a terminal result is not an
error.

The consumers are:

```text
model tool inspect_state  -> bounded SituationFrame text
TUI /inspect [TASK]       -> the same bounded SituationFrame text
CLI inspect-state         -> full BranchState JSON
```

Inspection is read-only. It does not re-run historical tools or mutate quota,
Git, checkpoints, or the session. Use `branch_history` when exact earlier tool
evidence is needed.

## SituationFrame

`src/cambium/situation.py` renders a bounded text projection of `BranchState`.
The canonical section order is:

```text
MISSION
AUTHORITY
ACCEPTED
DELTA
OPEN
CHILDREN
RESOURCES
ANCHORS
```

Default limits are 12 KiB for the whole frame, 2 KiB per section, and 12 items
per section. Callers may provide smaller explicit limits. Unknown values remain
`unknown`; the renderer does not invent optimistic defaults.

The frame header identifies the projection with:

```text
version
source_watermark
frame_sha256
branch_id
generation
context_epoch
artifact_head
```

Live children are retained ahead of completed children when a bounded frame
cannot show every child. Omitted detail points to existing history operations
rather than adding another state API.

The worker also builds a local SituationFrame for its late loop-state message.
That local frame uses worker-local observations and therefore has a different
watermark meaning from the durable event-store view. Epoch checkpoints retain
the exact provider-sent prefix required for context identity; generated
loop-state frames are not a second mutable memory database.

## Stable history references

The implemented printable branch/tool references are owned by
[context/navigation reference](context-branches.md):

```text
branch:<percent-encoded-task-id>
tool:<percent-encoded-task-id>:<generation>:<turn>:<batch-index>
tool:<percent-encoded-task-id>:<generation>:<turn>:<batch-index>@turn-NNNN
```

A reference identifies recorded evidence. It does not grant authority or
execute the referenced operation. Reopen a tool reference with
`branch_history action=tool`.

## `inspect_state` tool

The model-facing call is deliberately small:

```json
{"name":"inspect_state","arguments":{}}
```

or for a known branch:

```json
{"name":"inspect_state","arguments":{"task_id":"review-routing"}}
```

`task_id` is optional and defaults to the current branch. The result is the same
bounded SituationFrame text used by TUI inspection. There is no section/filter/
cursor vocabulary; historical detail stays in `branch_history`.

## Navigation references

Exact `repo_query`, `branch_history`, delegation arguments, and history-ref
syntax live in [context/navigation reference](context-branches.md). Keeping
those shapes there avoids maintaining two copies of the tool contract.

## Not runtime contracts

A typed evidence-linked **WorkLedger**, richer versioned **ResultCapsule**, and
additional knowledge-transition event vocabulary remain design/evaluation ideas.
Existing CAST semantic entries, `BranchState`, child result envelopes, tool
observations, and Git joins already own the current data. Do not add a second
state database or mandatory per-turn event family merely to match a proposal.

Future experiments belong in
[agent-system evaluation](../research/agent-system-evaluation.md). A proposal
moves into this reference only after source and an executable consumer land.

## Implementation anchors

- `src/cambium/branch_state.py` — reducer and JSON serialization
- `src/cambium/state_view.py` — session/task reader shared by inspection clients
- `src/cambium/situation.py` — bounded SituationFrame renderer
- `src/cambium/schemas.py`, `src/cambium/tools.py` — `inspect_state` schema/dispatch
- `src/cambium/branch_history.py` — stable history references and exact evidence
- `tests/scenarios/test_state_view.py`, `tests/scenarios/test_situation_frame.py` — projection regressions
