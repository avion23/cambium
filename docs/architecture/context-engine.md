# Cache-first append-only context engine

**Status:** active contract for the implemented CAST (Cache-Aligned Semantic
Trunking) projection, checkpoints, provider evidence, and fork/resume paths.
Source and tests remain authoritative for current behavior.

## 1. Decision

Cambium may treat a long-lived agent context as a reusable trunk, but it must
not treat an API model as a stateful process. The reusable object is an
**immutable, versioned context projection**. A provider's KV/prompt cache is an
optional acceleration of replaying that projection; it is never correctness
state.

The model is a transition oracle at this boundary:
`(model request, tool observations) -> proposed action/result`. It may be
nondeterministic, but it does not own branch state; Cambium validates
transitions and owns persistence, budgets, tool effects, and publication.

The source of truth is an append-only event/history log. The implemented active
request projection is:

```text
H + S1 + S2 + ... + Sn + small raw working tail
```

`H` is the stable system/tool head. Every `Si` is an immutable semantic summary
entry covering one new, disjoint raw message range. A flush makes one additional
provider call over the existing trunk plus the current raw tail, validates the
strict result, appends exactly one new summary entry, and removes only that
covered raw tail from the active prompt. Earlier summary bytes are never edited
or included in a later summary source.

Raw events, ordinary checkpoints, and immutable epoch files remain the audit and
recovery authority outside the active prompt. Child agents fork an immutable
checkpoint and append a private continuation. Exact-context-eligible children
reuse the exact trunk prefix; non-redacted incompatible checkpoints can supply
the same semantic summary entries under a fresh provider-specific head. Neither
implies a cache hit.

Appending a summary after the complete old transcript is still not compaction.
The covered raw region must leave the active projection while remaining durable
in the external history.

## 2. Distinct mechanisms

Do not collapse these into a single `cache=true` concept:

| Mechanism | Key | Correctness role | Typical lifetime |
|---|---|---|---|
| Provider prefix/KV cache | Provider-owned request prefix | None; performance only | Provider-defined |
| Epoch checkpoint | Content address plus `cache_key` | Durable replay and branch identity | Retained session history |
| Active context epoch | `provider_messages` plus continuation | Model input state | Until compaction/fork/close |
| Raw history | Append-only event log | Audit and recovery authority | Session retention policy |

Warm worker reuse is separate from provider cache and context epochs: the three
mechanisms have independent keys, lifetimes, and failure modes. Keeping a worker
alive never proves provider KV-cache retention; provider failover must preserve
semantic checkpoint correctness even when its cache is cold.

Cambium has no local response cache: `Diffundo` is a stateless router
(`src/cambium/diffundo.py:8-10`). A cache hit and a miss for the same request
must produce the same request bytes; only provider-reported usage supplies hit
evidence (`src/cambium/diffundo.py:1226-1237`).

## 3. State model

Let `E = (e0, e1, ... en)` be the immutable ordered event log. An active context
is a materialized view:

```text
P = project(E[from:to], projection_version, policy, capabilities)
```

An epoch checkpoint is persisted as `{schema, content:{provider_messages,
continuation_suffix}, meta:{identity/cache/loop/budget keys}}`. `content` holds
only the two provider message lists; `meta` holds identity, cache, loop, budget,
usage, and wall-deadline state
(`src/cambium/worker.py:203-234`). Legacy flat epoch files with top-level
`provider_messages` are accepted on load and can resume, including schema-4 files
(`src/cambium/worker.py:3728-3758`).

The implemented summary segment is:

```text
SummaryEntry = {
  type,
  sequence,
  source_sha256,
  source_message_count,
  through_turn,
  objective,
  outcome,
  decisions_added,
  decisions_superseded,
  facts_added,
  facts_invalidated,
  files_and_symbols_changed,
  verification_results,
  relevant_failed_approaches,
  open_items
}
```

Sequence, source digest/count, turn coverage, and the canonical entry fields are
validated before publication. Semantic arrays are bounded and remain user-role
data; they do not acquire system authority
(`src/cambium/summary_trunk.py:49-59`, `src/cambium/summary_trunk.py:85-103`).

The epoch `cache_key` is:

```text
provider, model, protocol, reasoning_effort
system_sha256, tools_sha256
prefix_sha256, suffix_sha256, full_sha256
prefix_bytes, message_count, redacted, provider_boundary
```

`system_sha256` hashes the system content and `tools_sha256` hashes the tool
schema; prefix, suffix, and full hashes cover the exact message lists.
`prefix_bytes` and `message_count` describe the provider prefix. `redacted`
records persisted redaction; a redacted checkpoint is not exact-fork eligible.
`provider_boundary` carries validated
non-secret provider, endpoint, auth, model, protocol, tier, environment, and
authorization identity
(`src/cambium/worker.py:3455-3472`, `src/cambium/worker.py:406-461`,
`src/cambium/worker.py:2469-2470`).

These fields prove checkpoint identity and exact-context compatibility, not
provider-cache eligibility or a cache hit. The loader recomputes the hashes and
prefix size before use
(`src/cambium/worker.py:3793-3852`).

## 4. Invariants

### C1. Cold-path equivalence

Provider cache availability is not observable in the semantic request. A cold
retry remains valid and receives the same request content. Matching a
`cache_key`, model, or prefix can authorize an exact-context fork; it does not
infer provider-cache eligibility or a hit. Only
`usage_event.provider_cache_hit`, copied from
provider-reported usage, is cache evidence
(`src/cambium/diffundo.py:1226-1237`, `src/cambium/worker.py:2990-3005`).

### C2. Immutable replay prefix

After publication, an epoch's message bytes, cache key, and checkpoint file never
change. Corrections create a child epoch.

### C3. Content/provenance binding

Checkpoint loaders recompute message hashes, prefix size, and message count;
descriptor or payload mismatch fails closed before a prompt is seeded
(`src/cambium/worker.py:3793-3852`).

### C4. Raw history remains authoritative

Compaction is a materialized view, not destructive history rewriting. Operators
and evaluators can recover the exact covered events.

### C5. A summary is lossy, append-only, and non-authoritative

An LLM summary is not assumed deterministic, associative, or commutative. The
published entry is immutable: `S1` must remain byte-identical when `S2` is
appended, and a later flush may summarize only the new raw tail. Publication is
fail-closed through sequence, source digest/count, checkpoint identity, and
exclusive immutable file creation. No invalid or duplicate proposal advances the
trunk (`src/cambium/worker.py:4455-4473`, `src/cambium/worker.py:4657-4674`).

### C6. Bounded active context

Each model call has a declared context budget and output reserve. The active
projection must fit before dispatch; provider fallback cannot silently select a
smaller context window.

### C7. Least-authority inheritance

A child receives only the stable task contract, relevant evidence projection,
required tool schemas, and explicit parent state. It does not automatically
inherit unrelated sibling transcripts, secrets, or the complete parent
scratch history.

Recursive reuse is typed and bounded, not process cloning. A child is admitted
against an immutable parent epoch/generation with a scoped task/capability
projection, budget, and deterministic merge slot; supervisor-owned depth,
descendant, concurrency, retry, output, and cancellation limits plus an
explicit terminal condition prevent unbounded self-delegation.

### C8. Deterministic admission and merge

Child completion order cannot decide the parent state. The parent admits result
envelopes in a deterministic order and uses explicit conflict rules. Facts may
join monotonically; code edits and contradictory decisions require
coordination, validation, or rejection.

### C9. Exact compatibility before cache locality

Provider/model/protocol/tool/schema/reasoning compatibility and the validated
`provider_boundary` are hard constraints. Cache locality is a soft outcome
inside that feasible class; credential readiness is checked before provider
admission (`src/cambium/worker.py:2571-2618`,
`src/cambium/supervisor.py:7553-7585`).

### C10. Accounting dimensions stay separate

Record at least input, output, cache-read, cache-write, total, current-turn,
cumulative, latency, and estimated cost. The budget prompt baseline is
`max(0, prompt_tokens - cached_tokens)`; the charged prompt delta is that
baseline minus the previous baseline, clamped at zero. Missing cache data treats
the full prompt as uncached, and completions remain billable
(`src/cambium/worker.py:1889-1926`).

## 5. Turn, fork, merge, and compaction protocol

### 5.1 Normal turn

1. Read the branch's expected active epoch and generation.
2. Build the canonical request from the epoch plus the new user/tool events.
3. Check hard provider/model/context/tool constraints.
4. Dispatch and persist the provider usage record independently of success.
5. Append response/tool events to the raw log.
6. Publish the immutable checkpoint with exclusive creation so an existing file
   cannot be replaced (`src/cambium/worker.py:3266-3299`).

### 5.2 Child fork

A child descriptor contains:

```text
checkpoint_ref
provider
model
system_sha256
tools_sha256
prefix_sha256
suffix_sha256
full_sha256
prefix_bytes
provider_boundary
```

The context-fork descriptor is immutable. An exact-context child is eligible only
when the provider boundary, model/protocol/reasoning settings, tool schema,
hashes, and prefix bytes match. Otherwise Cambium can reuse semantic summaries
under a new provider head. Neither path claims a provider cache hit; only
provider usage can make that claim (`src/cambium/worker.py:2487-2533`).

### 5.3 Child result

The child returns a bounded envelope, not its full transcript:

```text
status
claims[] { text, evidence_refs[] }
changed_artifacts[] { path, old_id, new_id }
open_questions[]
failed_checks[]
usage
source_epoch_id
child_checkpoint_id
```

The parent validates source identity, generation, artifact state, and declared
checks before admission.

### 5.4 Merge

Use a deterministic merge slot/order independent of wall-clock completion.
Monotone observations can be set-unioned when their identity and provenance are
stable. Mutating code results are not CRDT updates: overlapping edits, stale
base artifacts, and contradictory decisions require an explicit merge or
re-evaluation. The CALM result is the useful dividing line: coordination-free
composition is available only for monotonic knowledge.

### 5.5 Append-only semantic-summary flush

A flush runs only between completed provider/tool turns:

1. Freeze the current raw working tail. Existing summary entries are not part of
   the source range.
2. Compute its canonical source digest, message count, next sequence number, and
   covered turn.
3. Make a bounded summary call containing the immutable trunk, the raw tail, and
   a delimited summary-control request; a content flag permits one transformed
   retry.
4. Account for this call exactly like every other provider call: usage, request
   debt, latency, cache evidence, token budget, cost, cancellation, and wall
   deadline all apply.
5. Parse and validate the strict `SummaryEntry`: exact sequence and source
   metadata, bounded semantic fields, canonical schema, and no unknown fields.
6. Append the entry as one user-role trunk message, clear the covered raw tail,
   write a new immutable checkpoint, and publish the epoch transition only after
   durable creation succeeds.
7. Leave every prior summary message byte-stable. The next summary request begins
   with the complete existing trunk but summarizes only its newly supplied raw
   source block.
8. Retain the full raw history externally for replay, audit, recovery, and future
   re-projection.

Cambium forces a flush at delegation and terminal boundaries and performs a
threshold flush when the raw tail crosses the configured high-water mark. A
legacy transcript-heavy checkpoint is migrated by summarizing its unsummarized
continuation at the next flush; it is not recursively compacted.

An invalid summary entry emits `compaction_deferred`, skips the fold, and leaves
the active checkpoint and raw tail unchanged; the loop continues. After two
consecutive deferrals, the next invalid entry becomes `compaction_failed`.
Other flush errors emit `compaction_failed` and the boundary caller fails the
task (`src/cambium/worker.py:183`, `src/cambium/worker.py:4590-4604`,
`src/cambium/worker.py:4657-4670`, `src/cambium/worker.py:5217-5225`).

At the 90% token soft cap, the worker adds a forced-finalization instruction and
allows bounded headroom for a terminal `finish` rather than cutting off the
loop (`src/cambium/worker.py:237-247`, `src/cambium/worker.py:4301-4370`).
The live flush policy uses configured raw-tail thresholds plus delegation and
terminal boundaries (`src/cambium/worker.py:4435-4448`).

## 6. Provider cache capability contract

Provider configuration carries a normalized `CacheCapability` with:

```text
minimum_cacheable_tokens
cache_ttl_s
cache_granularity_tokens
cache_read_price
cache_write_price
```

This is provider capability and tariff metadata, not cache evidence
(`src/cambium/provider_scheduler.py:56-124`,
`src/cambium/provider_config.py:215`, `src/cambium/diffundo.py:624`). Cache
namespace and isolation are not modeled.

`provider_cache_hit` is true only for a positive normalized cached-token count,
false for present usage with no positive count, and absent when usage is absent;
prefix equality never fills this field (`src/cambium/diffundo.py:1226-1237`).

## 7. Routing interaction

The context engine supplies a cache-affinity identity to routing, but does not
choose providers. Routing first constructs a feasible set from authorization,
exact model constraints, protocol/tool support, context/output capacity,
budget, health, rate limits, and credential readiness. An explicit authorized
set with no credential-ready provider raises `NoCredentialFeasibleProvidersError`
before spawn (`src/cambium/supervisor.py:2030-2031`,
`src/cambium/supervisor.py:7553-7585`).

Each provider attempt gets the smaller of the call deadline and
`base * _REASONING_EFFORT_MULTIPLIERS.get(effort, 1.0)` (the `max` effort is
2x).
`CONTENT_FLAGGED` lets the caller transform context once before normal cascade
recovery; it leaves provider health unchanged and consumes no retry backoff
(`src/cambium/diffundo.py:161-164`, `src/cambium/diffundo.py:695-697`,
`src/cambium/diffundo.py:2683-2690`, `src/cambium/diffundo.py:2759-2763`).

The first successful provider binds a `ProviderLease`; child/context binding can
inherit its root and cache identity. A pinned incumbent timeout or real-death
result clears that lease before fallback; a successful fallback becomes the new
lease (`src/cambium/diffundo.py:2062-2115`,
`src/cambium/diffundo.py:2193-2255`).

See [`provider-routing.md`](provider-routing.md).

## 8. User-interface contract

Every interactive frontend attaches to a durable branch rather than inventing
a new unrelated conversation for each line. It displays current-turn and
cumulative usage, including cache reads/writes and context utilization. The UI
is a projection over the canonical event stream; it does not own session or
agent state. The TUI rail marks exact, semantic, and fresh context lineage as
`=`, `~`, and `∅` (`src/cambium/observability.py:285-295`,
`src/cambium/tui_screen.py:2325-2341`,
`src/cambium/tui_screen.py:2447-2457`).

See [`terminal-interface.md`](terminal-interface.md).

## 10. Verification

A cache/context change is accepted only with frozen configuration and repeated
trials:

1. **Replay identity:** canonical checkpoint identity and compatibility equality;
   provider hit evidence is checked separately.
2. **Provider evidence:** repeated warm/cold trials with provider-reported cache
   read/write tokens; report sample count and distribution, not `1/1`.
3. **Compaction quality:** paired held-out task evaluation before/after
   compaction, including unresolved obligations and edit correctness.
4. **Atomicity:** duplicate, stale, timeout, and crash tests around epoch CAS.
5. **Branch determinism:** randomized child completion schedules produce the
   same admitted parent state.
6. **Security:** children cannot inherit excluded secrets/evidence; redaction
   remains effective in events and checkpoints.
7. **Economics:** input/output/cache-read/cache-write costs are calculated from
   the provider's actual pricing fields.

## 11. References

- Git object model: <https://git-scm.com/book/en/v2/Git-Internals-Git-Objects>
- CALM theorem: <https://arxiv.org/abs/1901.01930>
- Event sourcing empirical study: <https://arxiv.org/abs/2104.01146>
- Prompt Cache: <https://arxiv.org/abs/2311.04934>
- SGLang / RadixAttention: <https://arxiv.org/abs/2312.07104>
- Lost in the Middle: <https://arxiv.org/abs/2307.03172>
- RAPTOR: <https://arxiv.org/abs/2401.18059>
- Recursive Language Models: <https://arxiv.org/abs/2512.24601>
- OpenAI prompt caching: <https://platform.openai.com/docs/guides/prompt-caching>
- Anthropic prompt caching: <https://docs.anthropic.com/en/docs/build-with-claude/prompt-caching>
- Gemini context caching: <https://ai.google.dev/gemini-api/docs/caching>
