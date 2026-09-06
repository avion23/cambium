# Agent state reference

**Status:** `BranchState`, shared SituationFrame rendering, and model/CLI/TUI
inspection are implemented. WorkLedger and ResultCapsule-v2 are proposals.
Rationale belongs in the [agent operating model](../architecture/agent-operating-model.md).

## Identity and authority

State keeps these identities separate:

```text
session_id
branch_id
parent_branch_id
generation
turn
context_epoch
artifact_head
source_watermark
```

A matching context does not imply a matching Git artifact or provider cache hit.
The supervisor owns lifecycle and accepted artifacts; tools own observations;
provider responses own usage evidence; the worker owns checkpoints; model text
remains a proposal or claim until an effect boundary establishes it.

## BranchState

`src/cambium/branch_state.py` reduces durable events into the current immutable
read model. It tracks:

- identity, lifecycle, mission, and authority;
- context/checkpoint lineage and accepted artifact state;
- plan, open work, blockers, and recent semantic/evidence refs;
- child branches, provider/model/resource state, and usage;
- bounded tool observations and the current result envelope.

CLI inspection emits the full serialization:

```sh
cambium inspect-state /path/to/session
cambium inspect-state /path/to/session child-task-id
```

`state_view.py` chooses the latest relevant interactive turn before replay. It
does not mix turn-local sequence numbers into one false global order. A focused
child view includes descendants. A running task with no terminal result is valid
pending state.

Public lifecycle values are:

```text
unknown queued starting active suspended joining verifying publishing
succeeded failed cancelled rejected
```

## SituationFrame

`src/cambium/situation.py` renders a deterministic bounded text projection of
`BranchState`. Model `inspect_state` and TUI `/inspect` use the same renderer.
The worker also builds a local frame in its normal loop-state message without an
extra provider request.

Canonical section order:

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
per section. Unknown values stay `unknown`. Live children are kept ahead of
completed children when space is tight; omitted detail points to
`branch_history` instead of inventing another state API.

The header carries projection version, source watermark, frame digest,
branch/generation, context epoch, and artifact identity. The frame is a
projection, not a second truth store. Recorded `inspect_state` output is a normal
tool observation; generated loop-state frames are transient request material.

## `inspect_state`

Model tool:

```json
{"name":"inspect_state","arguments":{}}
{"name":"inspect_state","arguments":{"task_id":"review-routing"}}
```

`task_id` is optional and defaults to the current task. The tool is read-only and
returns the same bounded SituationFrame format used by TUI `/inspect`.
Inspection does not rerun tools, mutate the session, or require a preparatory
planner/classifier call.

Use `branch_history` for exact historical action/observation evidence. Exact
`repo_query`, `branch_history`, delegation arguments, and stable history-ref
syntax live in the [context/navigation reference](context-branches.md).

## Stable references

Implemented printable history references are:

```text
branch:<percent-encoded-task-id>
tool:<percent-encoded-task-id>:<generation>:<turn>:<batch-index>
tool:<percent-encoded-task-id>:<generation>:<turn>:<batch-index>@turn-NNNN
```

Use the refs returned by `branch_history`. A reference identifies recorded
evidence; it grants no authority and does not re-execute the effect. Semantic
`D1`/`F1`/`O1`/`V1` labels belong to CAST summary identity, not another
persistent ledger.

## Proposed state only

WorkLedger and ResultCapsule-v2 remain design/evaluation ideas. Existing CAST
entries, `BranchState`, child result envelopes, tool observations, and Git joins
already own the current data. Do not add a second state database or mandatory
event family merely to mirror a proposal.

Future experiments belong in
[agent-system evaluation](../research/agent-system-evaluation.md). A proposed
shape moves into this reference only after source and an executable consumer
land.

## Sources

- `src/cambium/branch_state.py` — reducer and full state
- `src/cambium/state_view.py` — shared recorded-state reader
- `src/cambium/situation.py` — bounded SituationFrame renderer
- `src/cambium/schemas.py`, `src/cambium/tools.py` — model-facing inspection
- `src/cambium/branch_history.py` — stable history refs and exact evidence
- [runtime architecture](../architecture/architecture.md) — ownership and flow
