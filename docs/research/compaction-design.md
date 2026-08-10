# Cambium v2.1 — Context-Compaction Protocol (evidence-backed, never silent)

**Historical snapshot — 2026-08-09.** **DRAFT — docs only, non-normative.** Research
task `wt-doc-compaction`, worktree `/tmp/opencode/cambium-doc-compaction`, base
`main@6109a6a`. It proposes a worker protocol and store behavior; final authority is
[`docs/architecture/architecture.md`](../architecture/architecture.md), source/tests,
and [`v2-1-status.md`](v2-1-status.md).

**Current note (not retroactive):** active `supervisor.run_plan` is flat;
`task_decomposed` remains unsupported; provider cascade is source-defined and honors
`Retry-After`; worker stdout/event admission is bounded; no per-worker OS sandbox or
approval; DLQ and eval cache are absent.

The proposal assumes explicit-tree admission: a validated static DAG selects a child,
then the child receives a fresh bounded context. Compaction never turns sibling sessions
into one implicit recursive context; only the strict upward result envelope crosses the
boundary. Prefix-cache effects remain measurement targets.

**Design driver:** lossy compaction must be explicit, evidence-backed, and never silent;
OpenCode's named context-compaction and safe-provider-turn discussions describe the
hidden pass rejected here (`docs/research/opencode.md`). History is append-only and never
deleted.

## 1. Boundary and triggers

Compaction runs in the worker's own node context, writes a new row to the shared
`conversations.db`, and never enters a parent context. This follows I2.4 (own bounded
log + parent summary + subtree envelopes) and I2.7/D8b (child never sends scratchpad,
CoT, or trajectory upward). The summary is richer than the ≤2k result envelope, so it
stays node-local. D8g's store is one SQLite WAL with `node_id`, `(node_id, turn_seq)` and
`(node_id, kind, turn_seq)` indexes; Opifex owns trajectory, turn, generation, and log
(M5).

At the snapshot, `src/cambium/conversations.py` was absent (v2-1-review §1.3 gap 11)
and `worker.py:356-494` was single-shot: no `context`, checkpoint, or store binding.
These are **UNVERIFIED implementation gaps**, not silently assumed behavior.

Three triggers:

1. **Token threshold (worker advisory):** compact when
   `contextTokens > contextWindow − reserveTokens`; config carries
   `reserve_tokens`, `keep_recent_tokens`, and `threshold`. `Custos` still enforces
   supervisor-owned `init.budget.max_tokens` (D4).
2. **Steer:** supervisor sends a focus hint (`"keep failing test names"`) to a live
   NodeSession, mirroring Prime Agent `/compact [instructions]`.
3. **Checkpoint cadence:** after a configured number of critical checkpoints or before
   resume into a fresh worker; a checkpoint must precede compaction.

Every decision records threshold, used, and freed tokens in the critical checkpoint;
there is no silent background action.

## 2. Wire message and safe boundary

Add request `compact` to `ipc-protocol-draft.md` §2.2 (the six existing requests are
`init`, `context`, `run_task`, `check_health`, `cancel`, `shutdown`; adoption makes
seven). It is additive within `proto` (§5), carries a ULID `request_id`, and responds
`ok` with the same ID:

```jsonc
{"type":"compact","request_id":"01J…",
 "reason":"token_threshold|steer|checkpoint_cadence|supervisor",
 "instructions":"optional focus hint","max_summary_tokens":2000,
 "reserve_tokens":16384,"keep_recent_tokens":20000}
```

Only one compact may be pending (`PROTO_OUT_OF_ORDER`). A rejected summary returns
`error_type="compaction_canary_failed"`, `recoverable=true`; retry exhaustion is a
durable error. The worker may self-trigger; the resulting `checkpoint` carries the
summary, just as `run_task` completes via `result_envelope` event rather than response.

Run only between provider turns: never mid-tool or mid-LLM call. Pause the ReAct loop,
summarize, write checkpoint, then resume. This mirrors Prime Agent's turn-end
`compact.run()` and OpenCode's safe-provider-turn boundary. The adopted alternative is
**in-process** compaction; the proposed spawned “GC agent” is rejected: it would copy
raw history across I2.7, add a process/lifecycle class, and worsen the Prime Agent OOM
case (`docs/research/prime-agent.md`, **Relevant lessons**). In-process work blocks the worker but stays
within `max_summary_tokens` and supervisor wall budget.

## 3. Summary envelope and canary

The `checkpoint` payload extension is:

```jsonc
{"compact_summary":{"summary":"…","covered_from":"msg-id",
 "covered_to":"msg-id","claims":[{"text":"…","evidence":["msg-id"]}],
 "open_questions":["…"],"todo_paths":["src/x.py"],
 "tokens_before":12345,"tokens_after":4567,
 "canary":{"pass":true,"missing_claims":[],"missing_todos":[]},
 "instructions":"…"}}
```

`covered_from/to` are message IDs, each claim names a message-id range, and the
deterministic canary requires every open question and TODO path from that range to
survive. A failure rejects the summary and retries with a larger budget; no history is
deleted. Store writes add the node row; the critical checkpoint/event is the replay
anchor. The exact token column is **UNVERIFIED** (D8g does not define one).

Anti-patterns: silent compaction; deletion; parent-context compaction; compaction before
checkpoint; upward scratchpad leakage. `compact_summary` does not refresh a parent
summary; only the fixed result envelope does.

## 4. Falsifiable acceptance

1. Mean `(tokens_before−tokens_after)/tokens_before` meets a frozen threshold (proposal
   ≥60%, every sample above floor), measured from store rows.
2. Canary pass rate is 100% after retries; no accepted record has `canary.pass=false`.
3. Paired held-out eval with/without compaction has no more than −1 point metric delta
   (M9 posture; `should_decompose_metric` or Opifex metric).
4. Record scenario count, commit SHA, pinned model, threshold, and retry config (M1).

## 5. Open questions (retained IDs)

| ID | Question / historical default |
|---|---|
| Q1 | Worker trigger versus supervisor token authority; worker triggers, Custos enforces. |
| Q2 | Event payload, store row, or both; default both, with double-write review (D8g.3). |
| Q3 | Cascade windows differ; default smallest eligible window. |
| Q4 | Canary exhaustion fail-open (uncompacted, durable error) versus fail-closed task failure; default fail-open. |
| Q5 | Summary call not a ReAct turn but consumes token/wall budget and cost. |
| Q6 | Compose M9 static tree-sitter context and dynamic compaction in disjoint regions (D8c). |
| Q7 | Never refresh parent summary. |
| Q8 | Add usage metadata or deterministic estimator for token accounting. |

## 6. Verification and adoption record

Verified sources at the snapshot: I2.4/I2.7 (`architecture.md` §3.7), D8b/D8g,
M5/M6/M7, supervisor-owned D4 budgets (§7.4), provider capability filtering (§9.2),
IPC catalogue §2.1–2.2 and error taxonomy §4.1–4.2, checkpoint durability §6.4–6.5,
canary metric gate §10/D5, Prime Agent `docs/compaction.md` (formula, defaults,
carry-forward `CompactionEntry`), `README.md` (history preserved), and
`skills/compact/src/compact/__init__.py` (host request, turn boundary). **UNVERIFIED:**
the claimed spawned GC agent, a store token column, and absent `ConversationStore`/
worker wiring at this base.

On adoption, update `ipc-protocol-draft.md` §2.2 and §7, architecture §5.2/§6.4/§6.6,
`src/cambium/ipc.py`, worker wire loop, new `conversations.py`, Custos cadence/metering,
and tests. This list records future work, not a current implementation claim.

## Appendix A — retained protocol detail

The summary node was designed as an append-only conversation row with `node_id`, a
`parent_id` pointing at the last covered message, `first_kept_entry_id`, and
`tokens_before`. A replay reader could therefore show both the compact summary and the
full original JSONL/SQLite history; it never rewrote or deleted covered messages. The
summary template carried Goal, Constraints, Progress, Key Decisions, and Next Steps,
then machine-checkable `claims[].refs`, `open_questions`, and `todo_paths`.

The compact request reason was intentionally typed. `token_threshold` meant the worker's
provider usage crossed the reserve formula; `steer` carried a parent focus hint;
`checkpoint_cadence` was a supervisor defensive request; `supervisor` was an explicit
host action. `max_summary_tokens` bounded the summarization call, while
`reserve_tokens` and `keep_recent_tokens` controlled the portion left verbatim. A
worker could self-trigger at a threshold, but the resulting checkpoint had to use the
same envelope and canary as a supervisor-triggered request.

Safe-boundary sequencing was explicit: finish the current tool; flush its checkpoint;
queue compaction; pause before the next provider call; summarize; validate claims/TODOs;
write the critical checkpoint; reload context; then continue. A canary failure never
silently substituted a shorter summary. Retry budget was bounded; default policy was
fail-open after exhaustion with a durable `compaction_canary_failed` error, leaving the
full un-compacted context available.

### A.1 Evidence and data flow

For covered messages `[first_kept_entry_id, last_covered_entry_id]`, the worker built a
deterministic evidence set. Every claim named one or more IDs; every open question and
TODO path was extracted from the covered range and compared to summary arrays. The
canary returned `missing_claims` and `missing_todos`, not just a boolean, so an operator
could diagnose a lossy summary. The durable checkpoint included `tokens_before` and
`tokens_after` even though D8g did not yet define where row-level token estimates came
from. This accounting gap remained Q8.

Compaction reduced only dynamic node history. M9 tree-sitter compression reduced static
AST/symbol context. The proposal allowed both adapters in disjoint regions and forbade
the supervisor from applying either directly; the worker owned context assembly. A child
summary never refreshed a parent's summary, preserving I2.7 information hiding.

### A.2 Why the spawned-GC alternative was rejected

The rejected alternative gave a separate GC agent the worker's history. It required a
second process, a second context copy, a second admission/restart state, and a channel
for raw scratchpad data that I2.7 explicitly forbids. Prime Agent's evidence showed
context, rather than fixed process overhead, was its OOM driver (`docs/research/prime-agent.md`,
**Relevant lessons**), so duplicating the context amplified the risk. In-process summarization blocked
the worker for one bounded provider call but kept ownership, redaction, generation, and
checkpoint ordering local.

### A.3 Acceptance run record

Each frozen corpus run was to record scenario count, commit SHA, pinned model/provider,
`reserve_tokens`, `keep_recent_tokens`, reduction threshold, and retry count. A mean
reduction target (proposal 60%) was not enough: every sample had to clear its floor, the
canary rate had to remain 100%, and paired module metrics had to stay within the
pre-registered −1 point bound. The design specifically rejected accepting a summary that
passed the canary while failing the reduction threshold or a summary that improved a
training metric by hiding an open question.

### A.4 Source notes retained

The snapshot also checked Prime Agent `docs/compaction.md` (formula and defaults),
`docs/session-format.md` (`CompactionEntry` fields), `README.md` (single-file history and
lossy warning), `skills/compact/src/compact/__init__.py` (`host_request`, no mid-cell),
OpenCode `CONTEXT.md` safe provider boundary, `feedback-2-deltas.md` D8b/D8g, and
`test-strategy.md` §8 canary policy. The alleged “spawned GC agent” had no source and
remained **UNVERIFIED**; it was not rewritten as a fact.

## Appendix E — trigger and store matrix

| Trigger | Initiator | Durable evidence | Safety condition |
|---|---|---|---|
| Token threshold | worker | checkpoint `compact_summary` with used/freed tokens | worker advisory; Custos owns max tokens. |
| Steer | supervisor/parent | request ID on `ok`, instructions in summary | turn boundary; no sibling raw history. |
| Checkpoint cadence | supervisor | preceding critical checkpoint + compact checkpoint | no compaction before checkpoint. |
| Resume preparation | supervisor | summary row + state ref | fresh worker receives bounded summary. |

The shared row was expected to carry `kind="compact_summary"`, `node_id`, `parent_id`,
`covered_from`, `covered_to`, and its envelope. The event log carried a redacted audit
copy; the conversation store carried the node-local `context_for(node_id)` copy. Q2 left
open whether to reduce this double representation; if both writes remained, each boundary
kept one owner.

The wire request was not a checkpoint command. A worker could compact on threshold;
`compact` was a host request with an ACK. Its summary rode the critical checkpoint path,
like `run_task`'s terminal result event. `PROTO_OUT_OF_ORDER` prevented a second pending
compact; `recoverable=true` canary errors allowed bounded retry. The worker never received
permission to delete history.

## Appendix F — falsification and measurement discipline

Reduction was measured from the covered store range, not a model's claimed token count.
The corpus was frozen before comparing thresholds; a moved dataset/model/branch required
a new anchor. Every sample cleared its floor, not only the mean. Paired module evaluation
used the same split/provider config with compaction toggled. Canary pass without
reduction was a config failure; reduction with a canary miss was a compaction failure;
metric gain with canary regression was rejected as reward hacking.

No consensus, 90% discount, universally cheap branching, or mandatory MCTS was inferred
from this protocol. Static prefix caching and latency claims required direct measurement;
the design only said byte-stable prefixes could be cacheable.

## Appendix G — compaction failure matrix

| Failure | Worker behavior | Supervisor/replay behavior |
|---|---|---|
| Summary call timeout | abort summary before next provider turn | retain prior context; count wall/tokens; no checkpoint replacement. |
| Missing claim reference | canary returns `missing_claims` | retry with larger summary budget; durable error on exhaustion. |
| Missing TODO/open question | canary returns `missing_todos` | same retry/fail-open policy; never silently accept. |
| Token reduction below floor | summary remains valid but threshold fails | mark acceptance sample invalid; keep full history. |
| Store write failure | worker does not advance compaction cursor | task follows store-failure policy; replay uses previous checkpoint. |
| Worker crash after summary | critical checkpoint may be durable | restart from checkpoint; summary row and old messages both replay. |
| Parent asks for raw history | no sibling/session read | return only strict result envelope; I2.7 remains authoritative. |

The failure matrix was intended to prevent a generic catch-all (“use the last summary”)
from hiding causal failures. A summary is useful only when its evidence range is known,
its canary passes, and its checkpoint is durable. On fail-open exhaustion the worker
continues with an un-compacted bounded context; this trades token relief for information
retention and is recorded as `compaction_canary_failed`.

The proposal also kept a bounded `keep_recent_tokens` tail verbatim. The tail was not a
second summary and did not change the covered range; it was the explicit freshness window
for the next provider turn. `reserve_tokens` protected response headroom, while
`max_summary_tokens` protected the summarizer call. These values were configuration
inputs, not evidence-backed universal constants.
the design only said byte-stable prefixes could be cacheable.

## Appendix H — conversation-store query contract

The proposed store exposed three pure reads: `last_turns(node_id, n)` for bounded own
history; `cost_by_node(node_id)` for token/cost accounting; and
`context_for(node_id)` for composition. Each query was indexed by `node_id` and turn
sequence. A summary row did not replace the rows it covered, so a reader could inspect
the full history, the summary, or both. On restart, the worker received a state reference
and compact summary, not a parent or sibling transcript.

The event log and conversation store had different ownership: Custos persisted protocol
transcripts and critical event envelopes, while the node worker appended conversation
turns. The blackboard proposal introduced a third owner only by using a separate
`shared.db`; it did not put `_shared` rows into `conversations.db`. This one-writer-per-
database rule was the same DS-C1/DS-M3 discipline used for events.

The compaction request did not alter task-tree admission, sibling routing, or merge
policy. It was a context adapter at a safe turn boundary. Static context compression
(M9/tree-sitter) and dynamic history compaction were independent: either could be
disabled without changing the upward envelope or the validated DAG. Any claimed token
benefit therefore required paired measurement under the same provider/corpus.

## Appendix I — summary shape and replay canary

The carry-forward summary was a bounded object, not an unconstrained prose answer. It
recorded goal, constraints, progress, key decisions, next steps, covered message IDs,
claim evidence, open questions, TODO paths, token counts, and canary result. A reader
could display the object without opening the raw transcript, while a debugger could
follow every claim back to its source range. The covered range was immutable after the
checkpoint; a later turn started a new range rather than extending an old summary in
place.

The replay canary deleted the in-memory context projection, reopened the store, replayed
from the last durable checkpoint, and compared the resulting bounded context byte-for-
byte (apart from timestamps). It then checked that an open question and TODO path from
the covered range still appeared. A failed comparison blocked adoption of the summary
format; a provider/model change required a new frozen corpus. This made compaction a
measured context adapter, not an implicit recursion or universal memory discount.

Source pointers use stable named sections: OpenCode's **Context Epoch / Safe Provider
Turn Boundary** discussion in `docs/research/opencode.md`, and Prime Agent's **Relevant
lessons** section plus its compaction format/source
notes (rather than an assumed “GC agent” subsection). Those sources support turn-boundary
and history-preservation observations only; they do not prove a Cambium scheduler,
provider discount, or process-per-child isolation.

The child-session boundary was therefore a state boundary, not merely prompt text. A
fresh child context started with its validated spec and bounded parent summary; compaction
could shorten that context but could not import a sibling's raw transcript. Static DAG
admission happened before the child was created, and a dynamic decomposition proposal
had to wait for a validated next wave. This preserved tree ownership while allowing
node-local history to remain append-only.

## Appendix J — compact request and checkpoint example

The draft `compact` request carried `request_id`, `node_id`, `generation`,
`covered_from`, `covered_to`, `max_summary_tokens`, `keep_recent_tokens`, and a
`reason` (`threshold`, `steer`, or `checkpoint`). The worker acknowledged the request
at a safe provider-turn boundary, wrote the summary, and emitted one critical
`worker_checkpoint` containing the summary reference and covered range. An ACK without
that checkpoint was not enough for resume. A duplicate request for the same range was
idempotent; a request with a stale generation returned a typed error.

Validation required every claim reference to resolve to an event/turn ID, every open
question and TODO path in the covered range to remain represented, and token counts to
be measured from the frozen serialized corpus. The summary could include a bounded
verbatim tail, but it could not include hidden scratchpad, sibling transcript, provider
credentials, or raw chain-of-thought. A store failure left the prior cursor active and
did not delete or overwrite covered rows.

The proposal kept a compact-summary event separate from the normal result envelope.
On replay, the event identified the range and state reference; the node-local store
supplied the bounded context. A parent saw only the strict upward envelope. This made
lossy compaction auditable and preserved the explicit-tree boundary without implying a
current compaction implementation.

## Appendix K — rejected deletion and GC alternatives

The design rejected destructive history replacement. A garbage-collector agent that
rewrote or deleted prior turns would make claim references, replay, and crash recovery
ambiguous. Rejected variants also included silent provider-side summarization, compacting
mid-tool call, and using a parent or sibling transcript as the child's context. The
adopted alternative was append-only node-local history plus a bounded summary/checkpoint
at a safe turn boundary. The proposal did not claim that a separate process-per-child
sandbox, universal branching discount, or mandatory MCTS policy follows from this

The canary compared claims, TODOs, open questions, and covered IDs before and after
compaction. It rejected a summary that reduced tokens but dropped a claim reference or
changed a decision. The worker could continue with the prior bounded context after a
canary failure, but it recorded the failure and did not replace durable history. This
fail-open choice was a proposal tradeoff, not a silent fallback in current source.

Compaction never changed task IDs, generation tokens, or event sequence ownership.

The summary cursor advanced only after the critical checkpoint committed. A crash before
commit left the previous cursor and full append-only rows available for replay.

No compaction step deleted source turns.

The summary retained covered IDs and token counts so a replay reader could verify its
range without opening a model response.

The format remained a draft.

Source/tests own current compaction behavior.

No universal savings claim is made.

Prefix behavior is provider-qualified.

Measure before adoption.

Keep history append-only.

Historical only.

Historical identifiers retained: `D2`, `D3`, and `Q8g`.
