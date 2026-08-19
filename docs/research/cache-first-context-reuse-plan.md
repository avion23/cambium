# Cache-first context reuse — implementation plan for immutable cache epochs

**Status: DRAFT — Phase 1 implemented; Phase 2 measurement tooling implemented;
live provider evidence remains opt-in.** Snapshot base `main@a446345`
(`feat(jlens): calibrated layers from falsification (29,41,57,61)`), written
2026-08-19, worktree `/tmp/opencode/cambium-cache-first`, branch
`docs/cache-first-context-reuse`. This commit adds this file and one index
line to [`README.md`](README.md) only. **No production code, tests, or other
research notes change; no runtime behavior changes in this commit.**

This note extends [`rolling-context-and-agent-reuse.md`](rolling-context-and-agent-reuse.md)
with a changed order of objectives. That note treated rolling compaction as
mechanism 1 and parent/child reuse as mechanism 2. The clarified requirement
inverts this: **provider prompt-cache reuse is the primary application** —
parent context must be reused for child agents and for resumed parent turns —
and context compaction only supports a stable cacheable prefix. Where the two
notes disagree on priority, this note records the clarified objective; both
stay non-normative. Authority order is unchanged: task request > `agents.md`
> `src/cambium/` and `tests/` > `docs/architecture/architecture.md` >
research files.

## 1. Objective and order of precedence

1. **Primary: provider prompt-cache reuse.** A parent cuts an immutable,
   content-addressed context checkpoint at a safe provider-turn boundary. A
   child invocation reuses the exact cacheable prefix where
   provider/model/system/tool compatibility allows, appends only a bounded
   child task envelope, and runs in its own worktree and permission boundary.
   The parent later resumes from the same checkpoint with only bounded
   structured child-result envelopes appended.
2. **Secondary: compaction.** Compaction is an epoch transition that produces
   a new stable prefix only when context size requires it. Raw history stays
   durable and recovery stays independent of compaction. The first
   implementation does not compact at all.

The existing in-loop truncation (`_summarize_transcript`,
`src/cambium/worker.py:1132-1163`, called at `worker.py:1730`) continues to
bound the transcript in every phase below. It is a size guard, not the reuse
mechanism.

## 2. How this differs from passing a summary to a fresh subagent

Today's child path is summary-passing. Section 3 traces it. Three things are
lost, and the fork/resume mechanism exists to restore exactly these:

1. **The cacheable prefix.** A fresh child prompt shares zero provider-cache
   bytes with the parent beyond the static head before the `Task:` line
   (`tests/scenarios/test_worker_agent_loop.py:170-195` proves the head is
   stable only up to `rpartition("Task: ")`). Every child re-pays full input
   price for its context. A fork instead makes the child's first request
   byte-identical to a prefix the parent already sent on the same
   provider+model, so the provider's exact-prefix cache can serve it
   (measured chat-provider behavior, section 4).
2. **The parent's working state.** A summary is lossy and one-shot. The
   checkpoint is the parent's exact sent context — system head plus
   transcript projection — persisted and content-addressed. The child
   appends one structured envelope to it. The parent later resumes from the
   same bytes with only bounded child-result envelopes appended. Resume is a
   continuation of the same context, not a restart from a digest of it.
3. **Auditability with separated concerns.** Checkpoint, fork, and resume are
   durable events that carry cache-key descriptors. What the provider caches
   (unobservable, TTL-limited) stays strictly separate from what Cambium
   persists (checkpoints, envelopes, telemetry).

What this is not: shared mutable agent state. The checkpoint is immutable
once written. Children never write parent rows. Upward data stays the strict
nine-key envelope (`tasktree._ENVELOPE_KEYS`, `src/cambium/tasktree.py:55-65`).
A child's scratchpad or chain-of-thought never reaches the parent — the rule
is structural (`_validate_parent_envelope`, `src/cambium/worker.py:248-343`;
`_strict_envelope`, `src/cambium/supervisor.py:1647-1668`).

## 3. Current child path: summary-passing, not cache reuse

Traced execution path, all citations verified at `main@a446345`:

- A parent proposes a child either through the plan (`proposed_children`
  rendered by `_emit_proposed_children`, `src/cambium/worker.py:2195-2247`,
  emitted after the task body at `worker.py:2312`) or mid-loop through the
  `delegate` tool (`src/cambium/schemas.py:229-257`,
  `src/cambium/tools.py:761-782`, `_emit_delegated_child`,
  `src/cambium/worker.py:2250-2268`).
- The supervisor buffers each `propose_child` message
  (`src/cambium/supervisor.py:2731-2751`) and processes the set only when the
  parent's terminal envelope arrives (`src/cambium/supervisor.py:2685-2730`).
- `_admit_child` (`src/cambium/supervisor.py:1670-1784`) validates the
  revision with `tasktree.build_tree`, records it durably
  (`child_admitted`/`child_rejected`), and spawns the child through
  `supervise_task`.
- The child's context is its own spec plus the parent's strict envelope
  (`_child_spec`, `src/cambium/supervisor.py:3505-3535`). The envelope is
  bounded: `MAX_ENVELOPE_FIELD_CHARS = 2_000` and `MAX_ENVELOPE_ITEMS = 16`
  (`src/cambium/worker.py:169-170`).
- The worker renders that envelope into a freshly built system prompt:
  `_parent_envelope_lines` (`src/cambium/worker.py:1166-1189`) appends a
  "Parent task context:" block after the `Task:` line inside
  `_build_agent_prompt` (`src/cambium/worker.py:1192-1251`,
  `worker.py:1236-1237`).

Consequences:

- The child's system message differs from the parent's at the `Task:` line
  and everything below it. The provider's exact-prefix cache key covers the
  full leading message (`prompt_prefix_bytes`,
  `src/cambium/diffundo.py:388-404`), so the shared cacheable region is only
  the static head above `Task:`. The parent's accumulated transcript — the
  expensive part — is not reused at all.
- The parent itself is terminal (exited or pooled) when its children run.
  There is no suspension and no resume today; "parent continues after child
  results" does not exist as a mechanism.

## 4. Current cache evidence and limits

Measured and coded facts that bound the design:

- **Exact-prefix, content-addressed caching.** Providers cache an exact byte
  prefix. `validate_prompt_structure` (D8c) lints the leading message for
  volatile tokens (`src/cambium/diffundo.py:364-385`); the worker keeps the
  head byte-stable across tasks and transcript growth
  (`tests/scenarios/test_worker_agent_loop.py:170-216`). One byte of drift
  in system head, tools JSON (`json.dumps(tools, sort_keys=True)` inside the
  system message, `src/cambium/worker.py:1233`), model, or provider
  invalidates the cached prefix.
- **No local response cache, by design (D1).** `Diffundo` is a stateless
  router (`src/cambium/diffundo.py:1-32`); `CambiumLM` forces `cache=False`
  and strips `prompt_cache_key` and friends (`src/cambium/lm.py:53-54`,
  `lm.py:421-424`, `lm.py:809-810`). This stays unchanged.
- **Per-task provider stickiness exists.** The router binds a task to one
  provider (`_primary_provider` set on serve, `src/cambium/diffundo.py:1223-1226`),
  seeded per task (`rotation_seed = crc32(task_id)`,
  `src/cambium/worker.py:550-557`) or preset by the supervisor
  (`assigned_provider`, `src/cambium/worker.py:558-564`;
  `src/cambium/supervisor.py:2348-2351`; `_resolve_assignment`,
  `src/cambium/supervisor.py:1950-1967`).
- **Chat providers hit; codex does not.** zai 48/49 and opencode-go 109/110
  calls hit the cache on a byte-stable prefix; the codex responses endpoint
  reported `cached_tokens` on only 7/56 calls (12.5%), sparse and
  non-monotonic, and rejects `prompt_cache_options` /
  `prompt_cache_breakpoint` with 400s while `prompt_cache_key` reports zero
  (`src/cambium/diffundo.py:70-93`). Consequences: never emit cache-control
  fields; never gate the feature on codex hit rates; promise nothing for
  codex.
- **No cache IDs, no TTL contract, no cross-worktree sharing.** Nothing in
  the repo may assume a provider cache identifier, a minimum TTL, or that a
  cache entry is visible across accounts or worktrees. A miss is always a
  normal outcome. Eviction is silent and unobservable.
- **Model identity is strict.** The loop fails a turn when the response
  model differs from the configured model (`src/cambium/worker.py:1759-1765`);
  a live probe found the zai endpoint normalizes model slugs, so
  compatibility checks use the configured slug
  (`implementation-plan.md:112-117`).
- **Telemetry surface already exists.** `CallResult` carries
  `prompt_prefix_bytes` and `provider_cache_hit`
  (`src/cambium/diffundo.py:279-281`); `_provider_cache_hit` extracts
  `prompt_tokens_details.cached_tokens`, `cache_read_input_tokens`, and
  top-level `cached_tokens` (`src/cambium/diffundo.py:631-652`); codex usage
  is normalized by `_codex_usage` (`src/cambium/diffundo.py:955-983`). The
  worker emits one redacted `usage_event` per router call
  (`src/cambium/worker.py:1287-1374`); the supervisor validates and forwards
  an allowlist (`src/cambium/supervisor.py:211-226`, `:249-289`) and folds
  it into the debt ledger (`src/cambium/supervisor.py:2810-2814`;
  `ProviderDebt` including `cache_hit_count`,
  `src/cambium/routing.py:105-174`).

Cache economics discipline (from
[`rolling-context-and-agent-reuse.md`](rolling-context-and-agent-reuse.md) §8,
retained): a cached input discount applies to cached input tokens only;
overall request savings are strictly smaller than the headline multiple.
Claim measured numbers, not provider marketing.

## 5. Design: immutable checkpoint plus compatibility descriptor

### 5.1 Objects

| Object | Role | Durability |
|---|---|---|
| Epoch checkpoint | The exact message list (system head plus transcript projection) the parent last sent, with a cache-key descriptor; content-addressed; immutable once written | file under `<session_dir>/.cambium/checkpoints/<safe-task-id>/epoch-<n>-<sha16>.json`, written with `_atomic_json_write` (`src/cambium/worker.py:822-840`); durable `context_checkpoint` event |
| Cache-key descriptor | Compatibility contract for forking the epoch | embedded in the checkpoint and the event |
| Fork | A child invocation whose first prompt equals the checkpoint messages plus one child task envelope (user role) | derived; never stored as shared state |
| Resume | Parent continuation from the same checkpoint plus bounded child-result envelopes | durable `context_resume` event |
| Epoch transition (compaction) | A later fold that produces a new checkpoint; old epochs are never mutated | optional, phase 4 |
| Raw history | The append-only record of what was sent | existing per-tool-turn checkpoints (`src/cambium/worker.py:1377-1417`) and, when enabled, `ConversationStore` rows |

Separation rule: the provider may cache or evict at will; Cambium only (a)
makes prefix bytes reproducible, (b) persists checkpoints, envelopes, and
telemetry, (c) measures hits. A miss never fails anything.

### 5.2 Proposed structures (illustrative, frozen at implementation time)

```python
# worker.py additions, frozen dataclasses
@dataclass(frozen=True, slots=True)
class CacheKeyDescriptor:
    provider: str | None          # served provider (loop outcome, worker.py:1863)
    model: str                    # configured slug, never the response slug
    protocol: str                 # provider.protocol.value
    reasoning_effort: str | None
    system_sha256: str            # sha256 of messages[0] content bytes
    tools_sha256: str             # sha256 of the exact tools JSON in the head
    prefix_bytes: int             # prompt_prefix_bytes(prompt) at the boundary
    messages_sha256: str          # sha256 of canonical JSON of the message list
    message_count: int
    redacted: bool                # True when the session redactor altered bytes

@dataclass(frozen=True, slots=True)
class ContextCheckpoint:
    schema: int                   # = 2
    task_id: str
    epoch: int                    # per-task monotonic
    turn: int
    created_at: float
    cache_key: CacheKeyDescriptor
    system_message: dict[str, Any]
    transcript: list[dict[str, Any]]
    checkpoint_ref: str
```

If `Redactor.redact_mapping` changes any byte of the serialized checkpoint,
the writer sets `redacted: true`. A redacted checkpoint may still be forked
for context continuity, but the descriptor is marked incompatible for the
byte-guarantee purpose: cache reuse cannot be asserted, and telemetry records
that fact. Content addressing hashes the canonical pre-redaction
serialization, so the same logical checkpoint keeps one address.

### 5.3 Where epochs are cut (safe provider-turn boundary)

A checkpoint is cut only between provider turns — after the current tool
result is appended and the per-turn checkpoint persisted
(`src/cambium/worker.py:1897-1903`), and only when the action was
`delegate`; plus one terminal epoch next to the existing final checkpoint
write in `_finalize_worktree` (`src/cambium/worker.py:2132-2138`). The
checkpoint holds the prompt object just sent (`worker.py:1731-1734`), so by
construction `prefix_bytes` and the hashes match what the provider saw.
This is the same "Safe Provider-Turn Boundary" lesson recorded in
[`opencode.md`](opencode.md) (Context Epoch section): keep the baseline
context immutable during an epoch; admit changes only at the boundary.

### 5.4 Fork prompt construction

```python
def _build_forked_prompt(checkpoint, child_envelope) -> dict[str, Any]:
    messages = [checkpoint.system_message,
                *copy.deepcopy(checkpoint.transcript),
                {"role": "user", "content": _child_task_lines(child_envelope)}]
    return {"messages": messages}
```

`_child_task_lines` renders the child task plus the parent-envelope block
(reuse `_parent_envelope_lines`, `src/cambium/worker.py:1166-1189`) as one
**user-role data block** — bounded data, never a system directive (injection
posture, section 11). Build-time assertions, each failing closed to the
legacy fresh-prompt path with a durable `context_fork_skipped` reason:

- `prompt_prefix_bytes(forked) == checkpoint.cache_key.prefix_bytes`;
- `sha256(system message) == system_sha256`;
- the message list starts exactly with the checkpointed messages;
- `validate_prompt_structure` passes (it must — the head is the parent's
  already-linted head).

The neutral `"Begin."/"Continue."` tail (`src/cambium/worker.py:1240-1250`)
is unnecessary on the fork path: the fork's final message is user-role, which
is the shape the ZAI/GLM 1214 rejection requires.

### 5.5 Cache-key compatibility

`_fork_cache_compatible(child_spec, epoch, authorized_providers)` returns
`(compatible, reason)`:

- `epoch.cache_key.redacted is False`;
- `epoch.cache_key.provider` is in the child's authorized provider set
  (init injection `src/cambium/supervisor.py:2325`; child env-key
  intersection `_child_spec`, `src/cambium/supervisor.py:3528-3533`);
- the child's configured model equals `epoch.cache_key.model` (config slug;
  the response-slug normalization hazard is recorded above);
- protocol and `reasoning_effort` are equal;
- the child's `_exposed_tool_schemas` output (`src/cambium/worker.py:880-898`)
  hashes to `tools_sha256`. The supervisor sends uniform permissions for
  provider tasks (`src/cambium/supervisor.py:2323`), so sibling schemas are
  identical today — this is asserted, not assumed.

When compatible, the supervisor pins the child before admission resolution:
`spec["fanout_config"]["model"] = epoch.model` and
`spec["assigned_provider"] = epoch.provider`, set before
`_resolve_assignment` (`src/cambium/supervisor.py:1950-1967`). A pinned spec
is a no-op for batch pre-assignment (`_preassign_lanes`,
`src/cambium/supervisor.py:3456-3487`) and releases its lane on every exit
path (`_release_lane`, `src/cambium/supervisor.py:3489-3503`). When
incompatible, the child runs exactly today's summary-passing path with
`parent_envelope` only, and the `context_fork` event records the reason.

Invalidation is only what the descriptor can detect: any byte change in
system head, tools JSON, model, or provider. Providers evict silently; that
is a miss, not an invalidation. `created_at` is recorded so telemetry can
correlate age with hits; TTL is never predicted and never blocks a fork.

## 6. Fork and parent-resume lifecycle

### 6.1 Worker side

- After a `delegate` action's tool result is appended and the epoch
  checkpoint written and emitted, a task configured `context_reuse: true`
  returns a new provisional status `"suspended"` from `_run_agent_loop`
  (with `epoch` and `checkpoint_ref` in the loop outcome; children were
  already emitted at `src/cambium/worker.py:1881-1885` / `:2312`).
- `_EXIT_CODE_BY_STATUS` (`src/cambium/worker.py:195-199`) gains a distinct
  `SUSPENDED` code (proposed 3) so the supervisor can never misread a
  suspended parent as a plain failure.
- On resume, the supervisor sends the same `init` shape plus a
  `run_task` payload carrying `resume = {checkpoint_ref, epoch,
  child_results: [<strict nine-key envelopes>]}`. The worker seeds its
  transcript from the checkpoint, appends each child envelope as one bounded
  user message, and continues the normal loop. Budget accounting re-seeds
  `previous_prompt_tokens` from the epoch's last prompt-token count so the
  new-token budget stays proportional to the delta
  (`src/cambium/worker.py:1783-1800`).

### 6.2 Supervisor side

`_supervise` (`src/cambium/supervisor.py:2024-2232`) gains one new state:
suspended. On a clean, correlated envelope with `status == "suspended"`:

1. Children are admitted by the existing envelope-time path
   (`src/cambium/supervisor.py:2701-2730`); `_admit_child` additionally
   returns the admitted child task ids.
2. The supervisor does not write `_results` and does not prune the parent
   worktree. The generation fence is untouched — the suspended worker made
   no commit, and `_require_generation` keeps validating every resumed turn
   (`src/cambium/worker.py:1729`).
3. The supervisor awaits child terminality (a registry of per-task
   completion futures populated in `supervise_task`'s `finally`; bounded by
   the parent's remaining wall budget).
4. Strict envelopes come from `_child_envelopes[parent]`
   (`src/cambium/supervisor.py:2728-2730`) or are synthesized from the
   `child_failed` event for failed children
   (`src/cambium/supervisor.py:2014-2022`).
5. Emit `context_resume`, then drive a second `_drive_generation` for the
   same spec, same worktree, same generation, with `resume` in the payload.
6. The final (post-resume) envelope flows through the existing integrity
   check, merge, and no-op paths unchanged
   (`src/cambium/supervisor.py:2142-2193`, `_worker_success_integrity`
   `:2933-2967`, `_merge_task` `:3117-3209`).

Recursion depth stays bounded by the existing tree: delegation is a path,
never a diamond; `build_tree` enforces depth <= 3 and single-parent nodes
(`src/cambium/tasktree.py:51-52`, `:319-343`), so a suspended ancestor chain
cannot violate I2.2.

### 6.3 Isolation

Each child keeps what it already owns today: its own throwaway worktree and
branch (`_ensure_worktree` / `_recover_worktree`,
`src/cambium/supervisor.py:1368-1447`), its own generation fence
(`src/cambium/fencing.py:132`), its own permission boundary
(`src/cambium/supervisor.py:2323`), its own node context
(`tasktree.subtree_of`, `src/cambium/tasktree.py:427-471`), and its own
credential scope (child env keys are the intersection with the parent's,
`src/cambium/supervisor.py:3528-3533`). The fork adds exactly one shared
artifact — the immutable checkpoint file — and nothing writable.

## 7. Component work items

Grounded in the traced repo; each item names the file and symbols it touches.

- `src/cambium/worker.py` — add `CacheKeyDescriptor`, `ContextCheckpoint`,
  `_write_epoch_checkpoint`, `_build_forked_prompt`, `_child_task_lines`;
  parse `context_fork` / `resume` in `AgentConfig.from_init`
  (`worker.py:645-707`) and pass through `_merge_task_config`
  (`worker.py:710-759`); add the `suspended` status and exit code
  (`worker.py:173-199`); seed the loop from a checkpoint in
  `_run_agent_loop` (`worker.py:1669-1911`); emit `context_checkpoint`
  next to `_persist_checkpoint` (`worker.py:1420-1432`).
- `src/cambium/supervisor.py` — suspend handling in `_supervise`
  (`supervisor.py:2024-2232`); return admitted ids from `_admit_child`
  (`supervisor.py:1670-1784`); pin compatible children before
  `_resolve_assignment` (`supervisor.py:1950-1967`); forward the new event
  kinds in `_drive_generation` next to `checkpoint`
  (`supervisor.py:2786-2791`); accept and validate `resume` in
  `_run_payload` (`supervisor.py:1592-1624`); plumb `context_reuse` through
  `run_plan` (`supervisor.py:3830-3845`) and `_Runtime`
  (`supervisor.py:1100-1159`), next to `warm_pool_size`/`worker_reuse`
  (`supervisor.py:2340-2344`).
- `src/cambium/store.py` — add `context_checkpoint` to `CRITICAL_KINDS`
  (`store.py:76-81`) so the fork substrate is fail-closed durable.
- `src/cambium/supervisor.py` usage path — extend
  `_USAGE_EVENT_FORWARD_FIELDS` (`supervisor.py:211-226`) with optional
  `epoch` and `fork_of` (omitted when absent, the un-reported-field rule).
- `src/cambium/tasktree.py` — unchanged; `_ENVELOPE_KEYS` and
  `upward_result` already define the child-result message shape
  (`tasktree.py:55-65`, `:474-499`).
- `src/cambium/conversations.py` — unchanged in the first slice; phase 3
  wires raw-record rows (schema and `add_summary`/`branch` already exist,
  `conversations.py:48-60`, `:181-230`).
- `src/cambium/diffundo.py`, `src/cambium/lm.py` — no cache-policy change
  (D1 stands); measurement-only additions if any.
- `src/cambium/cli.py` / `src/cambium/oneshot.py` — surface
  `--context-reuse` next to the `--conversations` flag seam.
- `scripts/context_cache_evidence.py` — new aggregation script, sibling of
  `scripts/usage_evidence.py`.

## 8. Phases

### Phase 1 — no-LLM checkpoint/fork/resume slice (fake provider, default-off)

All of section 7's worker/supervisor/store items, tested with the scripted
fake router (the `_ScriptedRouter` pattern,
`tests/scenarios/test_worker_agent_loop.py:55-71`) and fixture workers.
No LLM calls anywhere in the new tests. No compaction, no
`ConversationStore` writes. Byte-for-byte current behavior when the flags
are absent.

### Phase 2 — real cache measurement (tooling implemented; live evidence pending)

Extend usage events with `epoch` / `fork_of`; add
`scripts/context_cache_evidence.py`; run one live paired session per chat
provider (zai, opencode-go; codex measured, never gated) using the
opt-in, non-loopback pattern of `scripts/external-provider-smoke.sh`.
Acceptance gates in section 12.

### Phase 3 (optional) — durable raw record

Route the worker transcript into `ConversationStore` when enabled; epochs
reference rows by id; recovery replays from rows plus checkpoints. No
prompt behavior change.

### Phase 4 (optional) — background compaction as an epoch transition

Deterministic (no-LLM first) delta fold at the existing
`worker.py:1730` seam behind a `rolling_compact` init flag, per
[`rolling-context-and-agent-reuse.md`](rolling-context-and-agent-reuse.md) §6
and the canary discipline of [`compaction-design.md`](compaction-design.md)
§3: the summary render goes into the mutable suffix under the user role; the
stable head never changes; failure is fail-open with a durable
`compaction_failed` event; the fold waits on a supervisor `published` signal
(the worker cannot know merge fate — `_worker_success_integrity` and the
merge path are supervisor-owned, `src/cambium/supervisor.py:2933-2967`,
`:3117-3209`). LLM-based folds come only after deterministic folds are
measured.

## 9. Data, event, and API changes; migration; flags

- New IPC event (worker to supervisor): `context_checkpoint`
  `{task_id, generation, epoch, turn, checkpoint_ref, cache_key{...}}`.
- New init field (supervisor to child): `context_fork`
  `{checkpoint_ref, provider, model, system_sha256, tools_sha256,
  prefix_bytes, messages_sha256}` — strict-validated in `from_init`; unknown
  keys are fatal (mirrors `_validate_parent_envelope`,
  `src/cambium/worker.py:248-343`).
- New run payload field (supervisor to resuming parent): `resume`
  `{checkpoint_ref, epoch, child_results, child_results_truncated}`. The
  list is bounded by `MAX_ENVELOPE_ITEMS` and the field cap; the whole
  message by `MAX_LINE_BYTES = 1 MiB` (`src/cambium/ipc.py:28`). Over
  limit: truncate the list and set the flag; never fail silently.
- New durable event kinds: `context_checkpoint`, `context_fork`
  `{parent_task_id, child_task_id, epoch, compatible, reason}`,
  `context_resume`, later `context_epoch_advanced` and
  `context_fork_skipped`. `emit` already accepts arbitrary kinds
  (`src/cambium/supervisor.py:1204-1244`).
- Usage event: optional `epoch: int`, `fork_of: str | null`.
- Feature flags: `context_reuse` (init/run_plan parameter, default `False`)
  modeled on `worker_reuse` (`src/cambium/worker.py:2470-2474`;
  `src/cambium/supervisor.py:2340-2344`); later `rolling_compact`. Absence
  is byte-for-byte today's behavior.
- **No new IPC request type.** The worker dispatch loop
  (`src/cambium/worker.py:2480-2673`) special-cases `init`, `run_task`,
  `cancel`, and EOF; a new request type would need new ordering and
  out-of-order handling before it is safe. Everything here rides existing
  messages as optional fields.
- Migration: none required. Checkpoint files are new artifacts under the
  existing checkpoints tree, which commits already exclude
  (`src/cambium/worker.py:2055-2060`; `is_cache_artifact_path`,
  `src/cambium/fencing.py:29`). Old plans and old stores validate
  unchanged; `ConversationStore` migration precedent
  (`conversations.py:503-534`) shows the additive path if phase 3 needs one.
  Rollback equals flipping the flag off.

## 10. Failure, retry, cancellation, concurrency, staleness

| Case | Behavior |
|---|---|
| Concurrent children from one epoch | All fork the same immutable checkpoint; identical prefix bytes; independent worktrees and nodes; results appended in completion order; sibling state never merges (I2.4 via `subtree_of`) |
| Child fails, then restarts | The existing crash-restart loop recovers the child worktree per generation (`src/cambium/supervisor.py:2219-2229`); a restart re-forks the same immutable checkpoint in a fresh process (the provider cache may still hold it); restart generations never come from the warm pool (`src/cambium/supervisor.py:2120-2135`) |
| Child finally fails | Parent resumes with a bounded failure envelope synthesized from `child_failed` (`src/cambium/supervisor.py:2014-2022`); no parent state rolls back |
| Child cancelled (`cancel`, `src/cambium/worker.py:2648-2651`) | The cancelled strict envelope resumes the parent; the epoch stays valid for further forks |
| Parent cancelled while suspended | TaskGroup cancellation tears down children with it (existing group semantics); the parent records terminal `cancelled`; prune path unchanged (`src/cambium/supervisor.py:1994-1995`) |
| Checkpoint missing or corrupt at fork/resume | Fail closed to the legacy fresh-prompt path; durable `context_fork_skipped` / `context_resume_failed`; never fabricate a prefix |
| Stale repo/worktree state | Worktree identity and recovery stay exactly the existing paths (`_ensure_worktree`, `_recover_worktree`, generation fence). The checkpoint carries context only, never repo state; the child's `base_commit` resolution is unchanged. A resumed parent re-validates its fence every turn (`src/cambium/worker.py:1729`) |
| Provider cache miss | Nothing; telemetry records `provider_cache_hit = false`; the run proceeds identically (D1 posture) |
| Resume payload over the wire cap | Truncate the child-result list; set `child_results_truncated` |
| Mid-forked-session truncation | `_summarize_transcript` fires as today; that is an epoch transition (a new checkpoint epoch is cut); the old prefix may be lost to the cache — acceptable and recorded |
| Warm pool | Pooled processes are never required: transcripts rebuild from durable checkpoints; resume spawns fresh. Pool identity (`_pool_env_key`, `src/cambium/supervisor.py:346-371`) is unchanged |

## 11. Redaction and prompt-injection controls

- Redaction before persistence, reusing the existing seams: the worker's
  checkpoint writer redacts through the session redactor exactly as
  `_write_checkpoint_file` does (`src/cambium/worker.py:1397-1398`); the
  supervisor redacts events before the store
  (`src/cambium/supervisor.py:1220-1224`) and envelopes before retention
  (`_redact_envelope`, `src/cambium/supervisor.py:1190-1196`). A redacted
  checkpoint is flagged incompatible for byte-guarantee reuse (section 5.2).
- Injection posture: the child task envelope and every compact-state render
  are appended as **delimited data under the `user` role**, never as system
  instructions — the same posture as the existing parent-envelope block.
  Summarized tool output must not gain system authority; system
  instructions stay byte-stable in the prefix. This is the laundering
  control from [`rolling-context-and-agent-reuse.md`](rolling-context-and-agent-reuse.md) §9.
- Upward closure stays structural: the strict nine-key envelope is the only
  child-to-parent channel (`tasktree.py:55-65`; `_strict_envelope`,
  `supervisor.py:1647-1668`); there is no transcript or scratchpad field to
  send (`supervisor.py:1647-1668` docstring; `worker.py:248-343`).
- No credential may ever appear in a checkpoint or event: provider secrets
  live only in the environment (`api_key_env`) or injected
  `CredentialSource` (`src/cambium/diffundo.py:203-257`), never in prompt
  content.

## 12. Test matrix and acceptance metrics

New `tests/scenarios/test_context_epochs.py`, plus additions to the
existing files. All phase-1 tests are deterministic and network-free.

- Prompt equality (fake router): the fork prompt's
  `prompt_prefix_bytes` equals `epoch.prefix_bytes`; the full message hash
  equals the checkpoint's; `validate_prompt_structure` passes; exactly one
  user-role child envelope is appended; no parent scratchpad beyond the
  strict block.
- Suspend/resume (fixture worker, pattern of
  `tests/scenarios/test_dynamic_child_admission.py`): suspend, two
  concurrent children, resume, final merge; `context_checkpoint`,
  `context_fork`, `context_resume` events present and redacted; child
  worktrees disjoint; parent worktree generation unchanged across resume.
- Negative matrix: incompatible provider pin (child authorized set excludes
  the parent provider) falls back to the legacy prompt with
  `context_fork.compatible = false`; a redacted checkpoint is incompatible;
  a corrupt or missing checkpoint falls back with a durable skip event; a
  child crash-then-restart re-forks; a finally-failed child resumes the
  parent with a failure envelope; a cancelled child resumes the parent; a
  parent cancelled while suspended cancels the children; an over-cap resume
  payload truncates with the flag set; unknown `context_fork` init keys are
  fatal.
- Pool interplay: resume spawns fresh even with `CAMBIUM_WARM_POOL_SIZE > 0`
  (pattern of `tests/scenarios/test_worker_pool.py:357`).
- Telemetry: `epoch` / `fork_of` forwarding and the omission rule
  (extensions to `tests/scenarios/test_usage_events.py`).
- Regression gate: with flags off, the full existing suite passes unchanged
  (`tests/scenarios/`, including `test_worker_agent_loop.py`,
  `test_dynamic_child_admission.py`, `test_worker_pool.py`,
  `test_supervisor_fanout.py`).

Acceptance metrics:

- **Structural (hard gate):** 100 percent prefix byte equality between the
  checkpoint and every fork/resume first turn, proven by tests.
- **Measured (phase 2):** child and resume first-turn `provider_cache_hit`
  rates at or above 80 percent of the parent-turn baseline on the chat
  providers (zai, opencode-go); codex reported, never gated; no regression
  in task success rate on the smoke fixtures. If chat-provider hits do not
  materialize, stop and report; do not proceed to phase 4.
- **Safety:** zero tests show sibling state merging, parent rollback on
  child failure, or an unredacted secret in any checkpoint or event.

## 13. Non-goals / what must not be implemented yet

1. No provider cache IDs; no `prompt_cache_key` or cache-control fields
   (stripped at `src/cambium/lm.py:53-54`; codex 400-probed,
   `src/cambium/diffundo.py:84-93`).
2. No assumption of cross-worktree, cross-account, or cross-session cache
   sharing; no minimum TTL.
3. No new IPC request type (section 9).
4. No live streaming parent: the parent suspends at a boundary; children run
   to terminal before resume.
5. No multi-parent or join tree nodes (I2.2 stands); no tree-model changes.
6. No LLM-based compaction; no deletion or GC of raw history, checkpoints,
   or branches; no rewinding the parent to receive a child result — resume
   is append-only.
7. No cache-aware provider switching: pinning only, for compatible children.
   No change to D1 (no local response cache), cascade order, or circuit
   breaker health machinery.
8. No background epoch writing in phase 1 (checkpoints are synchronous at
   the boundary, inside the existing checkpoint I/O budget).
9. No codex hit-rate gates or savings promises ahead of phase-2 measurement.
10. No change to merge, publication, or verification policy; the supervisor
    verdict stays authoritative over worker `finish` claims.

## 14. Open questions

| ID | Question | Proposed default |
|---|---|---|
| CQ1 | Suspend on `delegate` only, or also a supervisor-issued boundary? | delegate-only in phase 1 (the boundary is already safe); supervisor-issued later if measurement wants it |
| CQ2 | Resume one generation or N (multiple suspend/resume cycles per task)? | N, but only one level of concurrent children per epoch in phase 1; deeper chains ride existing depth bounds |
| CQ3 | Should `context_fork_skipped` events be critical? | non-critical first; promote if observers prove lossy |
| CQ4 | Do chat providers honor identical prefixes across processes within TTL? | open; phase 2 answers it — the design only makes it possible and measurable |
| CQ5 | Keep the terminal epoch (pre-merge) forkable? | yes, immutable; but children forked from it resolve their own `base_commit` as today |

## 15. This commit

Adds this file and the `README.md` index line. Touches no runtime code, no
tests, no other research notes. This historical docs commit did not implement
the runtime; later implementation commits now cover Phase 1 and the
measurement-only Phase 2 evidence tool.
