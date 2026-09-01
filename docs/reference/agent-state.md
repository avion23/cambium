# Agent state reference

**Status:** target interface reference. Current source schemas remain
 authoritative until the corresponding implementation-plan slice lands.

Rationale is in
[`../architecture/agent-operating-model.md`](../architecture/agent-operating-model.md).

## 1. Identity and authority

Every target state object is scoped by:

```text
session_id
branch_id          normally task_id
parent_branch_id
generation         worker ownership/fencing generation
turn               model decision turn within generation
context_epoch      immutable CAST epoch
artifact_head      accepted Git commit
source_watermark   latest durable event sequence used
projection_version
```

`generation`, `context_epoch`, and `artifact_head` are independent.

| Field class | Authority |
| --- | --- |
| task objective, constraints, done criteria | caller or admitted parent contract |
| branch lifecycle, generation, child admission | supervisor |
| tool observation | tool boundary and durable event |
| provider usage/cache hit | provider response normalized by transport |
| checkpoint identity | worker writer, validated by supervisor |
| accepted artifact head | Git plus supervisor join/publication invariant |
| claim or recommendation | model proposal, explicitly labelled |
| SituationFrame | deterministic projection engine |
| TUI row | renderer over canonical state |

## 2. Stable references

```text
branch:<percent-encoded-task-id>
tool:<percent-encoded-task-id>:<generation>:<turn>:<batch-index>
event:<session-id>:<sequence>
checkpoint:<percent-encoded-task-id>:<epoch>
commit:<object-id>
file:<path>#L<start>-L<end>@<blob-or-worktree-hash>
check:<percent-encoded-task-id>:<generation>:<name>
claim:<percent-encoded-task-id>:<sequence>
decision:<percent-encoded-task-id>:<sequence>
obligation:<percent-encoded-task-id>:<sequence>
verification:<percent-encoded-task-id>:<sequence>
```

The tool batch index is zero-based and mandatory for new history tooling. An
evidence reference grants no authority and never re-executes an effect. Missing
or stale references fail explicitly.

The existing `branch_history.py` library accepts a legacy tool form without the
batch index for compatibility. The future active model tool should emit the
canonical form and decide explicitly whether that library compatibility remains.

## 3. BranchState

Target illustrative shape:

```json
{
  "version": 1,
  "source_watermark": 481,
  "identity": {
    "session_id": "/state/cambium/run-42",
    "branch_id": "root",
    "parent_branch_id": null,
    "generation": 2,
    "lifecycle": "active"
  },
  "mission": {
    "objective": "Repair paging and prove the regression",
    "constraints": ["Do not edit provider transports"],
    "done_when": ["focused regression passes", "parser suite passes"],
    "verification_contract": ["python -m pytest tests/test_parser.py -q"]
  },
  "authority": {
    "repo": "/work/repo",
    "worktree": "/state/run-42/root-wt",
    "branch": "cambium/root",
    "writable_scope": ["src/parser.py", "tests/test_parser.py"],
    "tools": ["repo_query", "read_batch", "edit_file", "run_shell", "delegate"],
    "authorized_providers": ["provider-a", "provider-b"]
  },
  "context": {
    "epoch": 4,
    "checkpoint_ref": "root/epoch-004-...json",
    "lineage": "exact",
    "summary_segments": 6,
    "raw_tail_tokens": 1320
  },
  "artifacts": {
    "base_head": "abc123...",
    "worktree_head": "def456...",
    "accepted_integration_head": "def456...",
    "dirty": false
  },
  "control": {
    "plan": ["locate", "reproduce", "repair", "verify"],
    "current_step": 2,
    "last_meaningful_delta": "offset=500 truncates before slicing",
    "blockers": []
  },
  "knowledge": {
    "claims": ["claim:root:7"],
    "decisions": ["decision:root:4"],
    "obligations": ["obligation:root:9"],
    "verifications": ["verification:root:3"]
  },
  "children": [
    {
      "branch_id": "review-boundary",
      "admission_index": 0,
      "lifecycle": "succeeded",
      "context_mode": "fresh",
      "placement": "spread",
      "critical": false,
      "result_ref": "branch:review-boundary"
    }
  ],
  "resources": {
    "remaining_turns": 27,
    "remaining_wall_s": 1180,
    "context_pressure": "medium",
    "provider_lease": "provider-a/model-a",
    "cache_affinity": "exact",
    "quota_pressure": "low"
  },
  "anchors": ["tool:root:2:6:0", "file:src/parser.py#L70-L105@def456"]
}
```

Public lifecycle values:

```text
queued
starting
active
suspended
joining
verifying
publishing
succeeded
failed
cancelled
rejected
```

Internal phases may be richer, but model and operator projections use one public
vocabulary.

## 4. SituationFrame

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

Frame metadata:

```text
version
source_watermark
frame_sha256
branch_id
generation
context_epoch
artifact_head
```

Rules:

1. Keep mandatory section headers even when values are empty.
2. Sort children and obligations by stable identity/admission order.
3. Render missing values as `unknown` rather than plausible defaults.
4. Mark stale verification and evidence explicitly.
5. Bound the whole frame and every section; expose `inspect_state` continuation
   when items are omitted.
6. Include no credentials, hidden reasoning, or full transcript.
7. Persist only version, watermark, identity, digest, byte count, and
   truncation metadata when the frame is reproducible.

Initial target bounds, subject to measurement:

```text
whole frame             12 KiB
mission + authority      2 KiB
accepted + delta         3 KiB
open work                3 KiB / 12 items
children                 2 KiB / 8 items
resources + anchors      2 KiB / 12 refs
```

## 5. ResourceEnvelope

```json
{
  "remaining_turns": 27,
  "remaining_wall_s": 1180,
  "context_pressure": "low|medium|high|critical|unknown",
  "uncached_token_pressure": "low|medium|high|unknown",
  "provider_lease": {
    "provider": "provider-a",
    "model": "model-a",
    "migration_required": false
  },
  "cache_affinity": "exact|semantic|fresh|unknown",
  "cache_warmth": "warm_estimate|cold|unknown",
  "quota_pressure": "low|medium|high|blocked|unknown",
  "cash_pressure": "low|medium|high|unknown",
  "delegation_overhead": "low|medium|high|unknown",
  "alternative_lane_available": true
}
```

`warm_estimate` is not a cache-hit claim. Pressure thresholds are versioned and
inspectable. Missing inputs yield `unknown`.

## 6. Knowledge items

### Observation

```json
{
  "id": "observation:root:18",
  "kind": "tool|event|file|provider|artifact",
  "summary": "offset case failed before repair",
  "evidence_refs": ["tool:root:2:6:0"],
  "source_watermark": 420
}
```

### Claim

```json
{
  "id": "claim:root:7",
  "text": "read_batch caps before offset slicing",
  "basis": "observed|inferred|hypothesis",
  "status": "proposed|accepted|invalidated",
  "evidence_refs": ["file:src/pager.py#L80-L96@abc123"],
  "supersedes": []
}
```

### Decision

```json
{
  "id": "decision:root:4",
  "text": "slice lines before response-byte cap",
  "status": "active|superseded",
  "evidence_refs": ["claim:root:7"],
  "supersedes": []
}
```

### Obligation

```json
{
  "id": "obligation:root:9",
  "text": "run parser scenarios",
  "owner": "root",
  "done_when": "command exits 0 at accepted head",
  "status": "open|blocked|satisfied|cancelled",
  "evidence_refs": []
}
```

### Verification

```json
{
  "id": "verification:root:3",
  "name": "parser scenarios",
  "status": "passed|failed|stale",
  "artifact_head": "def456...",
  "context_epoch": 4,
  "evidence_refs": ["check:root:2:parser-scenarios"]
}
```

Verification applies only to the artifact and configuration tested.

## 7. WorkLedger projection

The first implementation derives current items conservatively from existing
`SummaryEntry` fields:

| Existing field | Derived item |
| --- | --- |
| `facts_added` | Claim, inferred unless linked direct evidence exists |
| `facts_invalidated` | invalidation transition |
| `decisions_added` | active Decision |
| `decisions_superseded` | supersession transition |
| `open_items` | open Obligation |
| `verification_results` | Verification parsed conservatively |
| `relevant_failed_approaches` | negative evidence/constraint |
| `files_and_symbols_changed` | model-reported observation, not accepted Git proof |

The harness assigns identities and source-entry refs. It must not fuzzy-merge
similar strings. A richer model schema requires a versioned migration.

## 8. ResultCapsule

```json
{
  "version": 2,
  "branch_id": "review-boundary",
  "parent_branch_id": "root",
  "status": "succeeded",
  "outcome": "No second paging defect found",
  "claims": ["claim:review-boundary:2"],
  "decisions": [],
  "artifacts": {
    "changed": false,
    "head": null,
    "files": []
  },
  "verification": ["verification:review-boundary:1"],
  "open_obligations": [],
  "blockers": [],
  "usage": {
    "calls": 4,
    "input_tokens": 12000,
    "cached_tokens": 0,
    "output_tokens": 2100,
    "estimated_cost_usd": 0.0
  },
  "recommended_parent_action": "continue root repair"
}
```

The capsule is bounded and immutable once admitted. Artifact fields do not prove
parent integration; join state remains supervisor-owned.

## 9. Target tools

### `inspect_state`

```json
{
  "name": "inspect_state",
  "arguments": {
    "section": "mission|authority|accepted|open|children|resources|knowledge|anchors|all",
    "branch_id": "optional task branch",
    "cursor": "optional opaque continuation",
    "limit": 20
  }
}
```

It reads BranchState only and never executes effects.

### `branch_history`

Existing library actions:

```text
branches
tools
tool
transcript
```

The library reads existing events/checkpoints only and never re-executes a tool.
It is not yet wired into the active model schema/dispatcher/prompt/tool hash.

### `repo_query`

```json
{
  "name": "repo_query",
  "arguments": {
    "action": "tree|symbols|definition|references|search|window|diagnostics",
    "query": "optional text or symbol",
    "path": "optional repository-relative path",
    "line": 1,
    "offset": 0,
    "limit": 20
  }
}
```

Portable actions can reuse `code_index.py`. Optional LSP actions can reuse
`lsp_query.py`. Both remain repository-local and must not silently perform a
different fallback operation.

No target tool is current until schema, dispatcher, prompt/tool hash, bounded
result, durable observation, scenario, and documentation land together.

## 10. Planned projection events

```text
branch_state_projected
situation_frame_built
knowledge_delta_admitted
knowledge_item_invalidated
obligation_updated
verification_recorded
verification_staled
result_capsule_admitted
provider_lease_migrated
operator_steer_admitted
```

Events contain identities, versions, counts, hashes, and bounded summaries.
Large reconstructible payloads are not duplicated without need.

## 11. Current migration state

Current model-originated `delegate` calls require `task`, `context_mode`, and
`placement` in the schema and are validated again at tool and supervisor
boundaries.

Harness-originated static `proposed_children` can still omit both policy fields
and enter an internal automatic compatibility path. That mode must receive an
explicit schema/event value or be removed; it is not a public model default.

Current `write_file` and `edit_file` effects are confined to normal paths inside
the assigned worktree. Parent paths, `.git`, `.cambium`, and symlink escapes are
rejected.

Current strict result envelopes remain the migration base for ResultCapsule.
Current CAST `SummaryEntry` remains the migration base for WorkLedger.
`branch_history.py`, `code_index.py`, and `lsp_query.py` are implemented library
boundaries awaiting model-tool wiring. BranchState, SituationFrame, and
`inspect_state` do not yet exist as production runtime surfaces.
