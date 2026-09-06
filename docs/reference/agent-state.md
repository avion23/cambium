# Agent state reference

**Status:** implemented `BranchState`, shared `SituationFrame` rendering, and
model/CLI/TUI inspection. WorkLedger and ResultCapsule-v2 remain proposals.
Rationale belongs in the [agent operating model](../architecture/agent-operating-model.md).

## Identity

Recorded state keeps these identities separate:

```text
session_id       persistent session or turn store
branch_id        normally task_id
parent_branch_id parent task when delegated
generation       worker ownership/fencing generation
turn             model turn inside one generation
context_epoch    published CAST checkpoint epoch
artifact_head    accepted Git commit
source_watermark last durable event sequence used by the projection
```

Equality of one does not imply equality of another. In particular, a context
checkpoint is not an accepted Git artifact, and matching context identity is not
proof of a provider cache hit.

Authority follows the runtime owners:

| State | Authority |
| --- | --- |
| objective/constraints | caller or admitted parent task |
| lifecycle, generation, child admission | supervisor |
| tool result | tool boundary and recorded event |
| provider usage/cache hit | provider response normalized by transport |
| checkpoint | worker writer, validated by supervisor |
| accepted artifact | Git plus supervisor publication/join |
| model claim/recommendation | model output, not direct evidence |

## Stable references

Implemented printable references include:

```text
branch:<percent-encoded-task-id>
tool:<percent-encoded-task-id>:<generation>:<turn>:<batch-index>
tool:<percent-encoded-task-id>:<generation>:<turn>:<batch-index>@turn-NNNN
```

Use the exact refs returned by `branch_history`. The interactive suffix prevents
repeated generation/turn counters in different operator turns from colliding.
A reference identifies recorded evidence; it grants no authority and does not
re-execute the effect.

Other evidence IDs such as semantic `D1`/`F1`/`O1`/`V1` labels are CAST summary
identities, not a second persistent ledger. See [CAST](../architecture/context-engine.md).

## BranchState

`src/cambium/branch_state.py` is the pure reducer over durable events. It tracks:

- identity and lifecycle;
- mission and authority;
- context/checkpoint lineage;
- base, worktree and accepted artifact state;
- plan/open work/blockers;
- child branches and results;
- provider/model/resource observations;
- recent claims, decisions, obligations and verification facts when recorded.

CLI inspection emits the full current dataclass serialization:

```sh
cambium inspect-state /path/to/session
cambium inspect-state /path/to/session child-task-id
```

`state_view.py` chooses the latest relevant interactive turn before replay. It
does not sort turn-local sequence numbers from separate event stores into one
false global sequence. A focused child view includes its descendants.

## SituationFrame

`src/cambium/situation.py` renders a deterministic bounded text view of
`BranchState`. The same renderer is used by model `inspect_state` and TUI
`/inspect`; the worker also renders its local current state into the normal
`<cambium-loop-state>` message without another provider request.

Canonical section order is:

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

The header includes projection version, source watermark, branch/generation,
context epoch and artifact identity. Unknown values remain `unknown`. Live
children are kept before completed children when the frame is bounded. Omitted
detail points to `branch_history` instead of inventing pagination arguments.

The frame is a projection, not a second truth store. Recorded `inspect_state`
output is a normal tool observation and survives checkpoint cleanup. Generated
loop-state frames are transient request material.

## `inspect_state`

Model tool:

```json
{"name":"inspect_state","arguments":{}}
{"name":"inspect_state","arguments":{"task_id":"review-routing"}}
```

`task_id` is optional and defaults to the current task. The tool is read-only.
It returns the same bounded SituationFrame format used by TUI `/inspect`.
A running task has an unknown/pending result rather than an exception.

Use `branch_history` when exact historical action/observation evidence is needed.
Inspection does not rerun tools, mutate the session, or require a preparatory
planner/classifier call.

## `repo_query`

`repo_query` is a separate read-only navigation tool. Exact schema values are in
`schemas.py`; implementation uses `code_index.py` plus the optional configured
LSP adapter.

| Action | Main arguments | Result |
| --- | --- | --- |
| `tree` | optional `path`, `limit` | bounded source-file listing |
| `search` | `query`, optional `path`, `limit` | literal matches |
| `symbols` | `query`, optional `path`, `exact`, `limit` | declarations |
| `references` | `query`, optional `path`, `limit` | lexical identifier uses |
| `window` | `path`, `line`, optional `limit` | nearby source lines |
| `lsp` | `path`, `method`, optional position | configured language-server result |

Portable references are lexical and are not relabelled as semantic LSP results.
Cambium does not install a language server automatically.

## Proposed state only

The following names are design proposals, not worker wire contracts:

- **WorkLedger:** evidence-linked structured claims/decisions/obligations/checks
  derived from data that currently lives in CAST entries and recorded events.
- **ResultCapsule-v2:** richer immutable child result with explicit claims,
  verification and recommended parent action.
- **ResourceEnvelope:** a richer normalized resource-policy object beyond the
  resource fields already projected by `BranchState`.

Do not add these merely to mirror a document. Introduce a field or event only
when an observed consumer cannot use the existing state, summary, result or
history record. Current child results and strict envelopes remain authoritative.

## Sources

- `src/cambium/branch_state.py` — reducer and full state
- `src/cambium/state_view.py` — shared recorded-state reader
- `src/cambium/situation.py` — bounded frame
- `src/cambium/schemas.py`, `src/cambium/tools.py` — model-facing inspection
- [context-branch reference](context-branches.md) — delegation/history formats
- [runtime architecture](../architecture/architecture.md) — ownership and flow
