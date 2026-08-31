# Agent state reference

**Status:** target interface reference. These values define the intended
agent-facing control model. Current wire schemas remain authoritative until the
corresponding implementation-plan phase lands.

Rationale is in
[`../architecture/agent-operating-model.md`](../architecture/agent-operating-model.md).

## 1. Identity and authority

Every state object is scoped by stable runtime identity:

```text
session_id
branch_id       normally the task_id
parent_branch_id
generation      worker ownership/fencing generation
turn            model decision turn within the generation
context_epoch   immutable CAST epoch
artifact_head   accepted Git commit
source_watermark latest durable event sequence used by the projection
projection_version
```

`generation`, `context_epoch`, and `artifact_head` are independent. Equality of
one does not imply equality of the others.

Field ownership:

| Field class | Authority |
| --- | --- |
| task objective, constraints, done criteria | caller or admitted parent contract |
| branch lifecycle, generation, child admission | supervisor |
| tool observation | tool boundary and durable event |
| provider usage/cache hit | provider response normalized by transport |
| checkpoint identity | worker checkpoint writer, validated by supervisor |
| accepted artifact head | Git plus supervisor publication/join invariant |
| claim or recommendation | model proposal, explicitly labelled |
| SituationFrame | deterministic projection engine |
| TUI row | renderer over canonical state |

## 2. Stable references

A lossy state item should point back to one or more stable references.
Recommended printable forms:

```text
branch:<percent-encoded-task-id>
tool:<percent-encoded-task-id>:<generation>:<turn>:<batch-index>
event:<session-id>:<sequence>
checkpoint:<percent-encoded-task-id>:<epoch>
commit:<40-or-64-hex-object-id>
file:<path>#L<start>-L<end>@<blob-or-worktree-hash>
check:<percent-encoded-task-id>:<generation>:<name>
claim:<percent-encoded-task-id>:<sequence>
decision:<percent-encoded-task-id>:<sequence>
obligation:<percent-encoded-task-id>:<sequence>
verification:<percent-encoded-task-id>:<sequence>
```

Tool batch index is zero-based. New history tooling must emit and accept the
canonical five-part form; omission must not acquire compatibility semantics.

A reference identifies evidence; it does not grant authority or re-execute an
effect. Missing or stale references fail explicitly.

## 3. BranchState

`BranchState` is a pure read model reconstructed from durable sources.
Illustrative JSON shape:

```json
{
  "version": 1,
  "source_watermark": 481,
  "identity": {
    "session_id": "/state/cambium/project/run-42",
    "branch_id": "root",
    "parent_branch_id": null,
    "generation": 2,
    "lifecycle": "active"
  },
  "mission": {
    "objective": "Repair paging and prove the regression",
    "constraints": ["Do not edit provider transports"],
    "done_when": ["focused regression passes", "existing parser suite passes"],
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
    "plan": ["locate paging boundary", "add regression", "repair", "verify"],
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

### Lifecycle values

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

A reducer may preserve more detailed internal phases, but model and operator
projections should use the same public vocabulary.

## 4. SituationFrame

The SituationFrame is a bounded text rendering of `BranchState`, appended as a
normal late request message.

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

Every frame header carries:

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

1. Omit empty optional rows, never mandatory section headers.
2. Sort obligations, children, and anchors by stable identity/admission order,
   not completion time.
3. Label unknown values `unknown`; do not invent defaults.
4. Mark stale verification or evidence explicitly.
5. Include at most the next few critical obligations and children. Supply an
   `inspect_state` cursor when more exist.
6. Do not include secrets, raw credentials, hidden reasoning, or the full
   transcript.
7. A frame is not persisted as another truth object. Persist its projection
   version, source watermark, digest, byte count, and truncation metadata.

Suggested hard initial bounds, subject to measurement:

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

`warm_estimate` is based on configured TTL and elapsed time, not a cache-hit
claim. `provider_cache_hit` remains provider evidence after a request.

Pressure classes are harness-computed policy outputs. Their thresholds are
versioned and inspectable. Unknown inputs produce `unknown`, not a low-pressure
assumption.

## 6. Epistemic items

### Observation

```json
{
  "id": "observation:root:18",
  "kind": "tool|event|file|provider|artifact",
  "summary": "test_parser_offset_500 failed before the repair",
  "evidence_refs": ["tool:root:2:6:0", "check:root:2:parser-offset"],
  "source_watermark": 420
}
```

An Observation reports what a boundary returned. It should not contain a model
conclusion disguised as a direct fact.

### Claim

```json
{
  "id": "claim:root:7",
  "text": "read_batch slices after applying the byte cap",
  "basis": "observed|inferred|hypothesis",
  "status": "proposed|accepted|invalidated",
  "evidence_refs": ["file:src/pager.py#L80-L96@abc123", "tool:root:2:6:0"],
  "supersedes": []
}
```

### Decision

```json
{
  "id": "decision:root:4",
  "text": "slice lines before enforcing the response-byte cap",
  "status": "active|superseded",
  "evidence_refs": ["claim:root:7"],
  "supersedes": []
}
```

### Obligation

```json
{
  "id": "obligation:root:9",
  "text": "run the existing parser scenario suite",
  "owner": "root",
  "done_when": "command exits 0 at the accepted artifact head",
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

Verification is valid only for the artifact and relevant configuration it
tested. A later overlapping artifact change marks it stale until rerun.

## 7. WorkLedger projection

The first implementation should derive a ledger from existing `SummaryEntry`
fields:

| Existing field | Derived state |
| --- | --- |
| `facts_added` | Claim with `basis=inferred` unless linked direct evidence exists |
| `facts_invalidated` | append invalidation transition |
| `decisions_added` | active Decision |
| `decisions_superseded` | supersession transition |
| `open_items` | open Obligation |
| `verification_results` | Verification, parsed conservatively |
| `relevant_failed_approaches` | constraint/negative evidence |
| `files_and_symbols_changed` | artifact-related observation, not accepted Git proof |

The harness assigns identities and preserves source-entry references. It must
not guess that two similar strings are the same item. Explicit future IDs can
replace conservative identity only through a versioned schema migration.

## 8. ResultCapsule

Target versioned shape:

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
  "recommended_parent_action": "continue the root repair"
}
```

The capsule is bounded, immutable once admitted, and linked to branch history.
It does not carry the complete child transcript. `artifacts.changed=true` does
not mean the parent accepted the artifact; join state remains supervisor-owned.

## 9. inspect_state tool

Target model-facing schema:

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

The tool reads the current canonical projection only. It does not read raw
transcripts, execute tools, or mutate state. Historical detail remains in
`branch_history`.

## 10. repo_query tool

Target model-facing schema:

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

Portable actions require a bounded repository-local implementation. Rich
definition/reference/diagnostic actions may use an optional one-shot LSP
boundary. Both must land with their schema, dispatcher, prompt, provider-tool
hash, and live scenario; no standalone implementation is part of the current
runtime. An unavailable LSP returns an explicit unsupported result and may fall
back only to a semantically equivalent portable query.

## 11. Projection events

Planned event vocabulary:

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

Events should contain identifiers, versions, hashes, counts, and bounded
summaries. Large frame or knowledge payloads should remain reconstructible from
existing checkpoints/events rather than duplicated without need.

## 12. Compatibility and migration

Current child-policy source accepts explicit `context_mode`/`placement`, while
the model schema also permits omission and automatic compatibility resolution.
The target interface removes that ambiguity: every model-originated child
proposal declares both fields. A separate harness-originated compatibility mode,
if retained, must have an explicit name and event value; omission must not carry
hidden semantics.

Current result envelopes remain valid during migration. Version 2 capsules are
added behind an adapter that preserves the current strict envelope until all
supervisor, worker, TUI, and history readers consume the new version.
