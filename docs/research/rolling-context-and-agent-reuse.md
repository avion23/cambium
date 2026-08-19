# Rolling context compaction and branchable agent reuse — future work, docs only

**Status: DRAFT — future work, non-normative.** Snapshot base `main@4e39b1a`
(`test: move transcript extractor scenarios out of example module`), written
2026-08-19, worktree `/tmp/opencode/cambium-rolling-context`, branch
`docs/rolling-context-and-agent-reuse`. This note proposes two mechanisms the
runtime does not have today:

1. **Rolling context compaction** — a stable compact state plus the current
   raw turn; after a verified task result, compress only the new delta into
   the state, replace the active model context, and keep the raw transcript
   durable for recovery and audit.
2. **Branchable/reusable agent context** — an immutable parent context
   checkpoint from which a child task is spawned in its own worktree and
   permission boundary, and to which the parent resumes with only a structured
   child-result message appended. This is context reuse, not shared mutable
   agent state.

Neither mechanism exists in `src/cambium/`. This note supersedes nothing. It
does not override [`compaction-design.md`](compaction-design.md) (historical
v2.1 protocol draft), `docs/architecture/architecture.md`, `agents.md`,
`src/cambium/`, or `tests/`. It extends the historical record with a
delta-only, cursor-based alternative and a parent/child reuse design; where
they disagree, `compaction-design.md` stays authoritative as the historical
protocol draft and this note records a proposed alternative.

## 1. Status and scope

- **Scope:** two new Markdown files only: this note plus one index line in
  `docs/research/README.md`. No production code, tests, or generated files
  are changed.
- **Authority:** the existing order in `docs/research/README.md` applies:
  task request > `agents.md` > `src/cambium/` and `tests/` >
  `docs/architecture/architecture.md` > research files. Research files do
  not define the runtime.
- **Historical counterpart:** [`compaction-design.md`](compaction-design.md)
  is retained unchanged. That draft proposed a worker-local `compact` wire
  request, a full-message-range summary checkpoint, and a canary envelope.
  This note records findings that change two of those decisions: (a) folding
  must be delta-only and gated on supervisor-verified publication, and (b) a
  new IPC request type is the wrong first step — the worker protocol does not
  currently route arbitrary request types safely.
- **Claims:** every "current" statement cites a repository-relative file and
  symbol. Anything without a citation is proposed behavior. No feature is
  claimed to exist.

## 2. Idea

One session context is a list of messages. The worker re-sends that list on
every turn (`_run_agent_loop` re-builds the prompt from `transcript` each
iteration, `src/cambium/worker.py:1701-1736`), so context grows monotonically
until it is truncated. The current truncation is destructive: `_summarize_transcript`
drops messages and injects a synthetic marker, mutating the only transcript
the worker keeps. The proposal separates three things the worker currently
collapses into one list:

| Object | Role | Durability |
|---|---|---|
| Raw record | append-only log of every message, tool observation, and verified envelope | durable; never mutated or deleted |
| Active projection | the bounded message list actually sent to the provider | derived; rebuilt from raw record + compact state |
| Compaction cursor | the position in the raw record already folded into the compact state | durable checkpoint |

Compaction becomes a **fold**: copy the compact state, compress the raw
messages strictly between the cursor and the current head into that copy,
advance the cursor, atomically persist the new state, then rebuild the active
projection from the new state plus the fresh tail. The raw record is never
rewritten.

For sub-tasks, the same cursor idea generalizes to branches. A parent running
a long session may need to delegate one slice to a child without re-paying
the parent's whole context. The proposal keeps an immutable snapshot of the
parent context at the branch point, appends the child's task description as a
single structured message, spawns the child with its own worktree and
permissions, and later resumes the parent from that same snapshot with the
child's strict result envelope appended. The child's raw trace is stored
separately, keyed to its own node id.

## 3. Terminology

- **Raw record** — append-only per-node message log (the current
  `transcript` list in the worker, and/or `ConversationStore` rows).
- **Active projection** — the exact message list sent to the provider this
  turn. Rebuildable from raw record + compact state; never edited in place.
- **Compact state** — a versioned, immutable summary object covering a
  contiguous range of the raw record (`covered_from`..`covered_to` row ids).
  A rebuildable derived cache, never the source of truth.
- **Compaction cursor** — `covered_to` of the newest folded summary. Rows
  after the cursor are unfolder; rows before it are folded.
- **Fold** — the deterministic operation `new_state = fold(state, delta)`
  that compresses only the delta range `[cursor+1 .. head]`.
- **Checkpoint** — an immutable snapshot of the parent's context state at a
  branch point. A checkpoint is content-addressed or versioned; nothing is
  ever mutated in place.
- **Result envelope** — the strict upward key set (`tasktree._ENVELOPE_KEYS`,
  `src/cambium/tasktree.py:55-65`): `parent_task_id`, `unified_diff`,
  `diff_truncated`, `summary`, `metric_score`, `metric_breakdown`, `commits`,
  `files_changed`, `status`. No scratchpad or chain-of-thought.
- **Context reuse vs. shared mutable state** — a child inherits a *copy* of
  the parent's checkpoint plus its own task message. Parent and child never
  mutate a common context object.

## 4. Current evidence

### 4.1 Destructive transcript bounding

`_summarize_transcript` (`src/cambium/worker.py:1132-1163`) is called every
turn in `_run_agent_loop` (`worker.py:1730`) before `_build_agent_prompt`
(`worker.py:1192-1245`). When the transcript exceeds
`config.max_transcript_chars` (default `MAX_TRANSCRIPT_CHARS = 120_000`,
`worker.py:167`), it:

1. keeps the plan message and the most recent `TRANSCRIPT_KEEP_TURNS = 6`
   turns (`worker.py:168`),
2. **drops** all older messages and inserts a synthetic `user` marker
   (`worker.py:1150-1157`),
3. proportionally truncates retained user observations via
   `_fit_transcript_to_budget` (`worker.py:1104-1129`).

This is destructive in three ways: the dropped messages are gone from the
only in-memory transcript; the synthetic marker is a lossy stand-in; and the
truncation edits messages in place. There is no durable raw record on this
path (checkpoints written by `_write_checkpoint_file`, `worker.py:1377-1400`,
are per-task redacted snapshots, not an append-only log).

### 4.2 The store already provides most of the substrate

`ConversationStore` (`src/cambium/conversations.py:107-310`) is a SQLite WAL
store with one writer thread. It already has:

- append-only rows with `node_id`, `parent_id`, `turn`, `role`, `content`,
  `ts`, `seq`, optional `tokens`, `kind`, and JSON `meta`
  (`_CREATE_TABLE`, `conversations.py:48-60`),
- `branch(node_id, from_id)` for cheap parent links that do not copy the
  prefix (`conversations.py:220-230`),
- `add_summary(node_id, content, covers_from, covers_to, tokens_before,
  tokens_after)` which appends a `kind="summary"` row with the covered range
  and token envelope in `meta` and does **not** delete the covered rows
  (`conversations.py:181-218`),
- `history`, `path`, and `token_accounting` reads, including the latest
  summary's `covered_range` and `reduction` (`conversations.py:232-310`).

The current supervisor writes one `kind="system"` row per admitted/rejected
child revision through `_record_revision_conversation`
(`src/cambium/supervisor.py:1789-1827`), redacted via the session redactor,
keyed by child task id. `run_plan` opens the store only when
`conversations=True` (`supervisor.py:3874-3878, 3940-3947`); the flag
defaults off. This note treats the store as the raw-record substrate and the
summary row as a rebuildable derived cache — not as the active projection the
provider sees.

### 4.3 Verification and publication belong to the supervisor

Workers emit a `result_envelope` (`worker.py:2321-2342`) and an authoritative
`exit_message`. The supervisor decides publishability: `_worker_success_integrity`
(`supervisor.py:2933-2967`) rejects a detached head, wrong branch, or dirty
worktree; `MergeSequencer.publish_merge` (`src/cambium/merge.py:884-...`)
atomically advances `refs/heads/main` under a strict fast-forward contract;
`run_plan` (`supervisor.py:3830-4018`) drives the whole session. Consequence:
a worker's own `finish` claim ("emit finish only when the task is complete
and verified", `worker.py:1224-1225`) is *advisory*. Folding raw turn data
into compact state must therefore be gated on the supervisor's published
result, not on worker success alone. The worker does not know whether its
branch was merged.

### 4.4 Per-node context is already the isolation unit

`TaskTree` enforces structural isolation: `subtree_of` gives a node only its
own subtree, never a sibling's (`src/cambium/tasktree.py:18-19, 427-471`);
`upward_result` returns exactly the envelope key set so a parent can never
receive scratchpad (`tasktree.py:474-499`). The supervisor admits dynamic
children with context "limited to its own spec plus the parent's envelope —
never sibling context or a parent transcript" (`supervisor.py:1678-1686`),
and `_strict_parent_envelope` projects results into that strict key set
(`supervisor.py:3706-3729`). Parallel work therefore needs per-node context;
a single global summary would silently merge sibling state and violate I2.4.

### 4.5 Provider prefix caching is measured, not assumed

`Diffundo` is a stateless router with no local response cache (design delta
D1; `src/cambium/diffundo.py:8-32`, `cache=False` in `src/cambium/lm.py`).
`validate_prompt_structure` lints the leading message for volatile tokens
(`diffundo.py:364-385`), and `prompt_prefix_bytes` reports the stable byte
prefix (`diffundo.py:388-399`). The usage event forwards
`prompt_prefix_bytes` and `provider_cache_hit` (`supervisor.py:211-226`), and
`cached_tokens` is an accepted provider usage field (`supervisor.py:511-520`).

Measured evidence in `diffundo.py:70-93`:

- The codex `responses` endpoint reports `cached_tokens` on only **7/56 calls
  (12.5%)** with sparse, non-monotonic per-turn hits (4/24 in one session),
  despite a byte-stable in-session prefix (`prompt_prefix_bytes` constant,
  e.g. 24/24 calls at 5385).
- Chat providers hit "essentially every call after the first" on the same
  byte-stable prefix: opencode-go 109/110, zai 48/49.
- Codex rejects `prompt_cache_options` and `prompt_cache_breakpoint` with
  400s; `prompt_cache_key` reports `cached_tokens: 0` even for a
  byte-identical 24k-token prefix re-sent three times.

Cross-session smoke evidence (`docs/research/v2-1-status.md`, "Providers"
row): zai measured a ~2.6× higher provider-reported cache-hit rate than codex
(0.929 vs 0.357) and ~2.2× lower mean latency; neither transport reports
cost, so cost-weighted routing has no data yet.

## 5. Architecture and state model

Proposed three-layer model, replacing the single mutable `transcript` list:

```text
raw record (append-only, per node_id)
   rows: turn | summary | system | branch
   never updated in place, never deleted

compact state (versioned, immutable)
   one row kind="summary" with meta {covers_from, covers_to,
   tokens_before, tokens_after, state_version, folded_from}
   rebuildable from raw record by replaying the covered range

compaction cursor
   = covers_to of the newest summary on the node's active head
   persisted with the summary row; a checkpoint records it

active projection (derived, ephemeral)
   rendered only when building the provider prompt
   = [compact-state render] + raw rows (cursor, head]
   never stored, never shared between nodes
```

State machine per node:

```text
  idle --init--> running --turn boundary--> running
     running --verified published result--> folding
     folding --fold ok--> idle(compacted)
     folding --canary fail or budget exhausted--> idle(uncompacted, durable error)
     running --failure/cancel--> idle(failed, no fold)
```

Invariants:

- **Raw record immutable.** Folding writes one new summary row; it never
  rewrites or deletes covered rows. Recovery replays from the last durable
  checkpoint plus the raw record.
- **Derived state only.** The compact state is a derived cache; deleting it
  costs a replay, not a loss of truth.
- **Fold only after verified publication.** The supervisor emits a published
  event after `publish_merge` (or the no-change success path); only then may
  the worker fold. Worker-side success alone never triggers a fold.
- **Per-node state.** Every fold, cursor, and projection is keyed by
  `node_id`. Sibling rows never enter a projection.
- **Never revert repository or worktree changes.** Compaction touches only
  context state. It never invokes `git reset`, `git checkout`, branch
  deletion, or worktree mutation. Publication stays in the supervisor/merge
  layer.

## 6. Rolling fold lifecycle

### 6.1 Trigger and admission

Initial slice, default-off: an `init` flag such as
`"rolling_compact": true` (modeled on the existing `worker_reuse` opt-in,
`supervisor.py:2340-2344`, `worker.py:2470-2474`). When the flag is absent,
the worker loop is byte-for-byte the current one. Do **not** add a new
`compact` IPC request type first: the worker dispatch loop
(`worker.py:2480-2610`) special-cases `init`, `run_task`, `cancel`, and EOF,
and a new request type would require new ordering, out-of-order, and fatal
handling before it is safe (`docs/research/ipc-protocol-draft.md` catalogues
six request types). The fold therefore starts as a **worker self-trigger at a
turn boundary**, exactly like the current `_summarize_transcript` call site
(`worker.py:1730`), gated on:

1. `rolling_compact` init flag,
2. the supervisor having reported a verified published result for the node,
3. a token threshold with hysteresis: enter above `threshold_high`, do not
   leave until below `threshold_low` (prevents fold thrash),
4. a per-fold cost budget (`max_fold_cost_usd`) and wall budget.

### 6.2 Delta fold algorithm

```text
def maybe_fold(node, cursor, head, verified_published):
    if not verified_published(node):        # §4.3 supervisor gate
        return NO_OP
    if token_estimate(node, cursor, head) < threshold_high:
        return NO_OP
    if not within_cost_wall_budgets():      # per-fold
        return DEFER_AND_RECORD
    delta = raw_rows(node, cursor+1, head)  # exact covered row range
    state  = load_compact_state(node)       # immutable, versioned
    new_state = fold(state, delta)          # deterministic; see §8.2
    if not canary(new_state, delta):        # claims/TODOs survive
        return FAIL_OPEN_DURABLE            # keep old state, record error
    write_summary_row(node, new_state, covers_from=cursor+1,
                      covers_to=head, tokens_before, tokens_after)
    persist_checkpoint(node, cursor=head, state_version+1)
    rebuild_active_projection(node, new_state, rows_after=head)
    emit_compaction_event(...)              # structured usage event §11
```

Key properties:

- **Delta-only compression.** Only `(cursor, head]` is compressed. The old
  compact state is copied, not re-summarized, so every fold is idempotent and
  the cost is proportional to the delta, not the whole session.
- **Idempotent checkpoints.** Re-applying a fold with the same
  `covers_from/covered_to` produces the same summary row (same
  `state_version`); a duplicate is a no-op. Stale `covers_to` vs. the current
  head is a normal condition, not an error.
- **Exact ranges.** The summary row's `meta.covers_from/covers_to` are row
  ids of the raw record (the store already does this in `add_summary`,
  `conversations.py:181-218`).
- **Versioned summaries.** Each fold increments `state_version`; readers can
  tell which state a projection was built from, and a stale checkpoint is
  detectable by version, not by content sniffing.
- **Raw-range retrieval.** When the summary is insufficient, a reader
  fetches `raw_rows(node, from, to)` directly from the store. The summary is
  a hint and a token saver, never a deletion.

### 6.3 Replacing the active model context

After a fold the worker's next prompt is built from `rebuild_active_projection`
instead of the mutated `transcript`. The current `_build_agent_prompt`
(`worker.py:1192-1245`) appends a single system message from `_parent_envelope_lines`
for the parent envelope; the same seam appends the rendered compact state as
**delimited user-role/data content, not as system instructions** (see §8.2).
The provider call then carries the new projection. The raw record continues
to grow append-only for recovery, audit, and debugging.

### 6.4 Turn-boundary only

Fold runs between provider turns — after the current tool result is appended
and before the next prompt build — never mid-tool or mid-LLM-call. This
matches the existing safe boundary the historical draft chose
(`compaction-design.md` §2) and the "only at Safe Provider-Turn Boundary"
lesson recorded in `docs/research/opencode.md` (Context Epoch section).

## 7. Parent-child context branching lifecycle

Goal: reuse parent context when delegating one slice of work, without copying
a mutable session into the child and without shared state.

### 7.1 Lifecycle

```text
parent running
   |  at a safe turn boundary, parent requests a child task
   v
1. checkpoint = snapshot(parent_context_state, state_version, node_id)
       immutable; content-addressed; durable; no rows copied
2. child_context = checkpoint + child_task_message (one structured append)
3. child spawned with:
       own git worktree (existing per-task worktree path, supervisor.py:1601)
       own permission boundary (existing init permissions block,
                                supervisor.py:2323)
       own node_id (child task id), own raw record and compaction cursor
4. child runs independently; may itself fold or branch one level
5. child terminates with result_envelope (strict key set) + exit_message
6. parent resumes from the SAME checkpoint, with the child's result
   envelope appended as one structured data message
7. child raw trace is retained separately under the child node_id
   (branch row + child rows via ConversationStore.branch/history)
```

### 7.2 What is reused and what is not

Reused: the parent's compact state and the bounded context built from it —
so the child does not re-derive the parent's accumulated state from scratch.

Not reused:

- the parent's raw transcript (child gets the projection/checkpoint render,
  never the raw record),
- the parent's worktree (child gets its own),
- the parent's permissions or credentials beyond the strict
  `provider_env_keys` allowlist (`supervisor.py:2324-2325`),
- any mutable parent state (the checkpoint is immutable; parent and child
  never share a writable context object).

This is **context reuse, not shared mutable agent state**. It is the
existing strict-envelope boundary (`tasktree.upward_result`,
`supervisor.py:1647-1668`) applied at a deeper level: instead of a child
starting from a fresh prompt plus the parent summary, it starts from a fresh
prompt plus the parent's checkpoint render.

### 7.3 Ordering, concurrency, cancellation, retries

- **Ordering:** the child result message carries the checkpoint version it
  was spawned from. If the parent has since folded or branched again, the
  child's result is still valid (it references the older checkpoint) but must
  be appended to the current parent head with the checkpoint version recorded
  — the parent never rewinds to make room for the child.
- **Concurrent children:** multiple children may run from the same
  checkpoint; each writes its own node rows. The parent appends results in
  child completion order, each as its own message. Sibling state never
  merges (I2.4).
- **Cancellation:** cancelling a child (existing `cancel` request,
  `ipc-protocol-draft.md` §2.1) never cancels the parent or siblings. The
  checkpoint remains valid for reuse.
- **Stale checkpoints:** a checkpoint whose `state_version` is not the
  parent's current version is still reusable for spawn (it is immutable) but
  is refused as a resume point if the parent advanced past it; resuming
  always uses the newest checkpoint at or before the current head.
- **Retries and cache invalidation:** re-running a child produces a new
  child node id (new generation); its rows never overwrite the old child's.
  Provider prefix caches are invalidated by any byte change in the prefix;
  because the parent's compact state is rendered at the *bottom* of the
  projection (§8.2), a child's own session keeps its own stable prefix
  (`diffundo.py:364-385` lints this ordering).
- **Model/provider compatibility:** a child may run a different model or
  provider than the parent; the checkpoint is plain rendered content and does
  not carry provider state. The existing provider cascade and
  `assigned_provider` admission balancing (`supervisor.py:2348-2351`) apply
  to the child independently.

## 8. Cache economics

### 8.1 The cost question, answered directly

Cached input is **not** universally 10x or 100x cheaper. The multiple depends
on the provider's pricing table and how the cache-hit discount is applied:

- **~2x input savings is common.** Most providers that expose a prompt cache
  bill cached input tokens at roughly half the uncached input rate.
- **~10x can occur** only where the provider advertises a ~90% cached-input
  discount (e.g. cached reads near 0.1× the input price). That discount
  applies to the *cached input tokens only*.
- **100x is not a safe expectation.** No current provider in this repository's
  measured surface prices cached input near 1% of uncached input, and a
  100x total-request claim requires a workload that is ~99% cached prefix —
  which contradicts any growing transcript.

Overall request cost drops by **less** than the input multiple, because:

1. output/completion tokens are still billed at the full rate,
2. tool-call overhead and any orchestrator call (e.g. a fold/summarize call)
   are billed at full rates,
3. the first request that *writes* the cache (or the uncached prefix on any
   request) is billed at the full input rate,
4. latency is not cost; a cache hit shortens time-to-first-token but the
   billing is per token class.

So a cache with a 2x input discount and a 50% cached-input fraction saves
~25% of the *input* bill, and a smaller fraction of the total request bill
once outputs and tool calls are included. Claim a measured number, not the
provider's headline multiple.

### 8.2 Repository evidence for the active providers

The measured surface in `diffundo.py:70-93` says: chat providers
(opencode-go 109/110, zai 48/49) hit on essentially every call after the
first, while the codex `responses` endpoint reports `cached_tokens` on only
7/56 calls (12.5%), sparse and non-monotonic, and rejects the cache-control
fields. `docs/research/design-deltas.md` D1 and `feedback-5-assessment.md`
record the same posture: a ~0.1× cached-input price "does not prove 0.1×
total request cost," and "Explicit trees yield a 90% cache discount" was
rejected as UNVERIFIED. **Do not promise savings for the active codex
provider**; its sparse caching is backend behavior and cannot be forced from
the request shape.

Consequence for the design: byte-stable system prefixes keep the cache
address stable (`diffundo.py:364-385`), so the compact-state render and the
current raw turn must live in the **mutable suffix**, below the stable
prefix. A fold changes only the suffix, which the chat providers tolerate
(the prefix stays byte-identical while the transcript grows); the codex
endpoint's sparse caching is a measurement input, not a design failure.

### 8.3 Measurement plan

Every fold-enabled run records a paired cache/cost table. Fields:

| Field | Source |
|---|---|
| paired runs (compact on/off, same corpus) | `docs/research/bench-harness-design.md` approach |
| prompt-prefix bytes per call | `prompt_prefix_bytes` usage event (`supervisor.py:222`, `diffundo.py:388`) |
| compaction count per node | new compaction event |
| input / output tokens | `usage` fields `input_tokens`/`output_tokens` (`supervisor.py:511-520`) |
| cache-read tokens | provider `cached_tokens` (`supervisor.py:518`) |
| cache-write/uncached input tokens | inferred: `input_tokens - cached_tokens` |
| provider and model per call | usage event `provider`/`model` (`supervisor.py:214-215`) |
| total cost per completed task | `estimated_cost_usd` when the provider reports it; otherwise omit (v2-1-status: "neither transport reports cost") |

Report the ratio `total_cost(compact) / total_cost(baseline)` per task and
per provider, with the corpus commit SHA and pinned model, per the discipline
in `docs/research/compaction-design.md` Appendix F and `design-deltas.md`
Q1.2/Q8c.2.

## 9. Safety and security

- **Compaction can launder prompt injection.** Summarizing untrusted
  narrative (tool output, web content, file contents) and then inserting the
  summary as a *system instruction* gives the summarized content system-level
  authority it never had. The compact-state render is therefore appended as
  **delimited data content under the `user` role** (like the existing parent
  envelope block, `worker.py:1166-1189`, which is a data block, not a policy
  directive). System instructions stay byte-stable in the prefix.
- **Fold structured verified envelopes deterministically; only compress a
  bounded untrusted narrative.** Verified result envelopes (strict key set,
  §3) are folded verbatim into structured fields. Free-form tool observations
  are the only compressed prose, and they are compressed as data, with the
  range of origin recorded per claim so a reader can recover the raw text
  (`raw-range retrieval`, §6.2).
- **Redact before persistence.** Every fold runs the session redactor before
  any summary row is written (the existing redaction path is
  `supervisor.py:1829-1837` and `worker.py:1397-1398`); raw rows keep their
  existing redaction treatment.
- **Never revert repository/worktree changes.** Folding operates on context
  state only. No code path in the design invokes git reset/checkout, branch
  deletion, or worktree mutation; the "never revert" property is an explicit
  invariant (§5).
- **Canary validation.** Before a new state replaces the old, a deterministic
  canary checks that every open question, TODO path, and claim reference from
  the covered range survives in the new state (the historical draft's
  envelope, `compaction-design.md` §3, is the reference shape).
- **Fail-open with durable error.** A canary failure or fold-budget
  exhaustion leaves the prior compact state and the full raw record in place,
  records a durable `compaction_failed` error event, and does not block the
  task. It never silently substitutes a shorter state.

## 10. Failure, recovery, and concurrency

| Failure | Behavior |
|---|---|
| Worker crash before fold checkpoint | previous cursor and full raw rows remain; restart replays from last durable checkpoint |
| Summary row written, checkpoint write fails | fold is retried; duplicate fold of the same range is idempotent (§6.2) |
| Canary fails | keep prior state; durable `compaction_failed`; retry with larger budget per hysteresis, then fail-open |
| Store write failure | fold does not advance the cursor; task follows the existing store-failure policy (`conversations.py` raises, supervisor wraps as `ConversationAppendError`, `supervisor.py:1826-1827`) |
| Provider/model change between folds | state is plain rendered content; a new frozen corpus anchors each acceptance run (compaction-design Appendix F) |
| Stale child checkpoint | spawn from it is allowed (immutable); resume refuses a version behind the current head (§7.3) |
| Concurrent children from one checkpoint | each writes its own node rows; parent appends results in completion order; sibling state never merges (I2.4) |
| Child fails/crashes | child node records failure; parent resumes with a bounded `child_failed` envelope message; no parent state is rolled back |
| Warm-pool/rebind with fold state | a pooled worker (`worker_reuse`, `supervisor.py:2340-2366`) rebuilds all per-task state from the new init (§4.1 of this design: `_run_agent_loop` already rebuilds from config on rebind, `worker.py:2517-2527`); the fold cursor and raw record are re-read from the store, never carried in the process |
| Raw-record corruption | summary remains usable; full-history reconstruction falls back to earlier checkpoints; integrity is checked like the event store's WAL discipline (`docs/research/sqlite-wal-durability.md`) |

Concurrency rule: one writer per node — the ConversationStore's single writer
thread already serializes appends (`conversations.py:107-128`). Folding
reads the node's head, computes the delta, and appends one row; two folds of
the same node cannot interleave because appends are serialized and the
checkpoint is appended after the summary row in the same writer.

## 11. Required software changes by component

Docs-only today; each row lists what a future implementation must touch.

- **`src/cambium/conversations.py`** — extend `add_summary`/`meta` with
  `state_version` and a `folded_from` marker; add a `raw_range(node_id, from,
  to)` reader; add a `rebuild_projection(node_id, cursor)` helper (or keep
  rendering in the worker and add only the range reader). Store remains
  append-only; no schema migration needed for the version field if `meta`
  JSON carries it.
- **`src/cambium/worker.py`** — replace the in-place `_summarize_transcript`
  call (`worker.py:1730`) with a gated fold path (`maybe_fold`, §6.2) that
  renders the compact state into the active projection; add the
  `rolling_compact` init flag to `AgentConfig` (`worker.py:632` area);
  emit structured `compaction` events; keep `_summarize_transcript` only as a
  legacy bounded fallback while the feature is default-off.
- **`src/cambium/supervisor.py`** — emit a `published` event after the merge
  path decides a node's fate (`_worker_success_integrity` + `publish_merge`
  caller, `supervisor.py:2933-3161`); forward the verified state to the
  worker so the fold gate is supervisor-owned; add a `rolling_compact`
  option to `run_plan` (next to `conversations`/`warm_pool_size`,
  `supervisor.py:3842-3844`); for reuse, snapshot parent context checkpoints
  and spawn children from them (reusing `_admit_child`,
  `supervisor.py:1670-1727`, and `_child_spec`, `supervisor.py:3509-3534`).
- **`src/cambium/ipc.py` / protocol** — no new request type in phase 1. The
  fold is a self-trigger; reuse uses the existing `init`/`run_task`/
  `result_envelope`/`reuse_ready` surface. A later `context`-style message
  (`ipc-protocol-draft.md` §2.1) is a separate, gated step.
- **`src/cambium/lm.py`, `src/cambium/diffundo.py`** — no change to cache
  policy (D1); measurement-only additions such as caching `cached_tokens`
  breakdown in the usage event.
- **`src/cambium/redact.py`** — reused as-is for fold persistence and child
  checkpoint rendering.
- **`src/cambium/tasktree.py`** — unchanged; its `_ENVELOPE_KEYS` and
  `upward_result` define the child-result message shape.
- **`src/cambium/cli.py`** — surface `--rolling-compact` and
  `--context-reuse` flags next to `--conversations` (`cli.py:177-180`).
- **`docs/research/README.md`** — index line for this note (this task).

## 12. Phased implementation plan

1. **Phase 0 — measurement only.** Enable `prompt_prefix_bytes` +
   `provider_cache_hit` + `cached_tokens` accounting end-to-end (already in
   the usage event surface, `supervisor.py:211-226`); run the §8.3 paired
   cost table on a frozen corpus. No code changes to context handling.
2. **Phase 1 — raw record + projection split.** Wire the worker's `transcript`
   through `ConversationStore` when a new init flag is set (default off);
   prove replay = rebuild. Keep `_summarize_transcript` untouched. Gate on
   `conversations=True`-style plumbing.
3. **Phase 2 — rolling fold, self-trigger only.** Implement `maybe_fold` at
   the existing `worker.py:1730` seam with the supervisor `published` gate,
   hysteresis thresholds, canary, and durable errors. No new IPC message.
4. **Phase 3 — parent/child reuse.** Add checkpoints, child spawn from a
   checkpoint, and resume-with-envelope; keep child raw traces under child
   node ids. Reuse the existing worktree and permission plumbing.
5. **Phase 4 — adoption gates.** Cost-gate the feature per provider using the
   §8.3 table; only enable by default where measured savings clear the bar.

## 13. Tests and acceptance metrics

Existing test seams: `ConversationStore` unit tests in `tests/` (branch,
summary, token accounting), worker loop scenarios, supervisor
merge/publish scenarios, and the warm-pool rebind tests (`worker_reuse`).
New tests:

- Fold idempotency: applying the same fold twice yields the same
  `state_version` and no duplicate rows; the cursor is unchanged.
- Cursor correctness: raw rows before the cursor are only readable via
  `raw_range`; rows after the cursor appear verbatim in the projection.
- Supervisor gate: a worker "succeeded" without a published merge produces no
  fold (fail-closed on the gate).
- Canary: dropping an open question from the covered range rejects the state;
  exhaustion records `compaction_failed` and leaves the prior state.
- Redaction: no secret-pattern content from the covered range appears in the
  summary row.
- Injection: a tool observation containing instruction-shaped text appears in
  the projection only under the `user` role, never in system messages.
- Prefix stability: `prompt_prefix_bytes` is unchanged by a fold; the mutable
  suffix alone changes.
- Reuse: a child spawned from a checkpoint cannot mutate the parent's state;
  two concurrent children from one checkpoint keep disjoint node rows; a
  stale checkpoint is refused as a resume point; child failure produces a
  bounded `child_failed` envelope and no parent rollback; warm-pool rebind
  rebuilds fold state from the store.

Acceptance metrics (frozen corpus, pinned model, recorded commit SHA):

- reduction = `(tokens_before − tokens_after) / tokens_before` per fold,
  every sample above floor (compaction-design §4 precedent);
- canary pass rate 100% after retries; no accepted row has a failed canary;
- paired metric delta within the pre-registered bound (no more than −1 point);
- measured cost ratio per task and per provider from the §8.3 table; the
  feature is not enabled by default where the ratio is not below the
  pre-registered bar;
- reuse: zero tests show sibling state merging or parent rollback on child
  failure.

## 14. Open questions

| ID | Question | Proposed default |
|---|---|---|
| RQ1 | Worker self-trigger vs. supervisor-issued fold | worker self-trigger at turn boundary, supervisor `published` gate, default-off flag (this note) |
| RQ2 | Where the active projection is rendered | worker, from store range + summary (store stays a raw/derived cache, not a prompt builder) |
| RQ3 | Hysteresis thresholds and per-fold budgets | configurable; per-provider from §8.3 measurements |
| RQ4 | Canary exhaustion policy | fail-open with durable `compaction_failed`, never silent substitution |
| RQ5 | Fold cost call vs. no-call compression | prefer deterministic no-call compression of structured envelopes; an LLM fold call is budgeted like compaction-design Q5 |
| RQ6 | Checkpoint retention and GC | raw rows and checkpoints are append-only; retention policy is separate and out of scope here |
| RQ7 | Child depth | one level in phase 3; deeper reuse re-uses the same mechanism (tree depth stays bounded by `tasktree.MAX_DEPTH`) |
| RQ8 | Provider cache measurement granularity | per provider/model/task from `cached_tokens` + `prompt_prefix_bytes`; no cost field yet from codex or zai transports |

## 15. Non-goals

- **Not superseding anything.** `compaction-design.md` stays as the
  historical protocol draft; `docs/architecture/architecture.md`, `agents.md`,
  and source/tests keep authority over runtime behavior.
- **No deletion.** Neither fold nor reuse deletes or rewrites raw rows,
  checkpoints, branches, commits, or worktree state.
- **No shared mutable agent state.** Reuse is context reuse only.
- **No new IPC request type in the first slice.**
- **No guaranteed cache savings**, especially for the codex provider.
- **No repository/worktree mutation from compaction.**
- **No single global summary.** Compaction is per-node; sibling state is
  never merged.
- **No change to publication, merge, or verification policy.** The
  supervisor keeps those; compaction only consumes a `published` signal.

## Appendix A — relationship to `compaction-design.md`

`compaction-design.md` (2026-08-09 draft) proposed a worker-local `compact`
request, full-range summary checkpoint, and canary envelope, and it explicitly
rejected silent compaction, parent-context compaction, and upward scratchpad
leakage. This note keeps those rejections and the canary discipline, and it
records three changes grounded in the current source:

1. **Delta fold, not full-range re-summary.** `compaction-design.md` §2
   summarized from `covered_from` to `covered_to`; this note folds only
   `(cursor, head]` into the previous state (§6.2), making folds idempotent
   and proportional to the delta.
2. **Supervisor `published` gate.** `compaction-design.md` §1 let the worker
   compact on its own threshold; `worker.py` cannot know whether its branch
   was published (§4.3), so the fold waits for the supervisor's verified
   publication signal.
3. **No new `compact` request first.** `compaction-design.md` §2 added a
   seventh request type; the current dispatch loop (`worker.py:2480-2610`)
   has no generic request handler, so the first slice is a self-trigger at
   the existing `worker.py:1730` seam, default-off (§6.1).

Both documents are drafts; neither claims the feature exists.