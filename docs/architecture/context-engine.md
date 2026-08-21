# Cache-first context engine

**Status:** target contract for context reuse, branching, compaction, and cache
accounting. Source and tests remain authoritative for current behavior. This
document replaces the design authority previously split across
`docs/research/cache-first-context-reuse-plan.md`,
`docs/research/rolling-context-and-agent-reuse.md`, and
`docs/research/compaction-design.md`.

## 1. Decision

Cambium may treat a long-lived agent context as a reusable trunk, but it must
not treat an API model as a stateful process. The reusable object is an
**immutable, versioned context projection**. A provider's KV/prompt cache is an
optional acceleration of replaying that projection; it is never correctness
state.

The source of truth is an append-only event/history log. Each active model
request is assembled from one materialized context epoch. Child agents fork an
immutable checkpoint, append a private continuation, and return a typed result
envelope. Compaction publishes a new epoch containing a validated structured
checkpoint plus an explicit recent tail. It never deletes the raw history.

A concise summary must **replace the covered region in the active projection**.
Appending a summary after the complete old transcript does not reduce context,
does not bound attention cost, and usually destroys the intended cache/cost
benefit.

## 2. Distinct mechanisms

Do not collapse these into a single `cache=true` concept:

| Mechanism | Key | Correctness role | Typical lifetime |
|---|---|---|---|
| Provider prefix/KV cache | Provider-defined exact request prefix | None; performance only | Minutes to provider-defined persistence |
| Local exact-response cache | Canonical request digest | Optional deterministic replay | Local policy |
| Semantic response cache | Similarity + policy/version | Approximate; unsafe by default for code edits | Local policy |
| Context checkpoint | Content digest + parent/version | Durable replay and branch identity | Session or retained history |
| Active context epoch | Checkpoint + continuation projection | Model input state | Until compaction/fork/close |
| Semantic memory | Typed facts with provenance and validity | Recalled evidence, not transcript replacement | Explicit retention policy |

A cache hit and a cache miss for the same semantic request must produce the
same request bytes and differ only in latency/accounting. Provider-specific
cache controls belong in provider adapters and capability metadata, not in the
semantic task description.

## 3. State model

Let `E = (e0, e1, ... en)` be the immutable ordered event log. An active context
is a materialized view:

```text
P = project(E[from:to], projection_version, policy, capabilities)
```

A context epoch is:

```text
Epoch = {
  epoch_id,
  parent_epoch_id,
  source_range,
  projection_version,
  immutable_prefix,
  recent_tail,
  unresolved_items,
  evidence_refs,
  digest
}
```

`digest` is over the canonical serialized epoch descriptor and its referenced
artifacts. The model request has a stricter cache identity:

```text
RequestIdentity = H(
  provider,
  model,
  endpoint/protocol,
  reasoning settings,
  cache namespace/policy,
  ordered tool names and schemas,
  system/developer messages,
  ordered conversation messages,
  multimodal asset identities,
  serialization/tokenization version
)
```

A context digest proves Cambium replay identity. It does **not** prove that a
provider retained or reused KV state. Provider-reported usage is the only
direct cache evidence; latency changes are supporting evidence only.

This structure is a persistent data structure and a Merkle DAG: epochs share
immutable ancestors rather than copying or mutating them. A child branch is an
MVCC-style snapshot plus a private continuation. Publishing a new active epoch
uses compare-and-swap against the expected parent/generation so duplicate or
stale completions cannot advance the branch.

## 4. Invariants

### C1. Cold-path equivalence

Provider cache availability is not observable in the semantic request. A cold
retry remains valid and receives the same request content.

### C2. Immutable replay prefix

After publication, an epoch's prefix bytes, ordered tools, settings, artifacts,
and descriptor never change. Corrections create a child epoch.

### C3. Content/provenance binding

Every checkpoint and child result names its source epoch, source event range,
projection version, and referenced artifacts. Descriptor-to-artifact mismatch
fails closed.

### C4. Raw history remains authoritative

Compaction is a materialized view, not destructive history rewriting. Operators
and evaluators can recover the exact covered events.

### C5. A summary is lossy and non-authoritative

An LLM summary is not assumed deterministic, idempotent, associative, or
commutative. Idempotency applies to publication: the same compaction operation
ID may publish at most once. Re-running the model may produce different text.

### C6. Bounded active context

Each model call has a declared context budget and output reserve. The active
projection must fit before dispatch; provider fallback cannot silently select a
smaller context window.

### C7. Least-authority inheritance

A child receives only the stable task contract, relevant evidence projection,
required tool schemas, and explicit parent state. It does not automatically
inherit unrelated sibling transcripts, secrets, or the complete parent
scratch history.

### C8. Deterministic admission and merge

Child completion order cannot decide the parent state. The parent admits result
envelopes in a deterministic order and uses explicit conflict rules. Facts may
join monotonically; code edits and contradictory decisions require
coordination, validation, or rejection.

### C9. Exact compatibility before cache locality

Provider/model/protocol/tool/schema/reasoning compatibility and task budgets are
hard constraints. Cache locality, stickiness, cost, and latency are soft
objectives only inside the feasible equivalence class.

### C10. Accounting dimensions stay separate

Record at least input, output, cache-read, cache-write, total, current-turn,
cumulative, latency, and estimated cost. Cached tokens are normally a subset
of input tokens and must not be added to total tokens again.

## 5. Turn, fork, merge, and compaction protocol

### 5.1 Normal turn

1. Read the branch's expected active epoch and generation.
2. Build the canonical request from the epoch plus the new user/tool events.
3. Check hard provider/model/context/tool constraints.
4. Dispatch and persist the provider usage record independently of success.
5. Append response/tool events to the raw log.
6. Publish the continuation checkpoint with compare-and-swap.

### 5.2 Child fork

A child descriptor contains:

```text
child_id
parent_epoch_id
parent_generation
context_projection_id
task_contract
tool_capabilities
model/provider constraints
budget
merge_slot
```

The descriptor is immutable. A child can use the same exact prefix only when
provider, model, endpoint, settings, tools, and prefix bytes remain compatible.
Otherwise Cambium reuses the semantic checkpoint while accepting a cold
provider cache.

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

### 5.5 Compaction

Compaction runs only between provider/tool turns:

1. Freeze the covered event range and expected parent epoch.
2. Deterministically extract obligations, decisions, file/symbol identities,
   failed checks, open questions, and evidence references.
3. Ask the model for a schema-constrained synthesis of those extracted items.
4. Validate required fields, source references, unresolved items, and size.
5. Optionally run a held-out continuation/equivalence canary.
6. Publish a new epoch with the structured checkpoint plus a bounded recent
   verbatim tail, using compare-and-swap.
7. Keep the full covered history unchanged and auditable.

The next request uses the new epoch. It does not contain both the entire old
transcript and its summary.

Compaction trades a one-time cache rebuild and summarization cost for lower
recurring input/attention cost. This is an online optimal-stopping/ski-rental
problem. A policy should compact only when estimated future savings and quality
benefit exceed rebuild cost:

```text
summary_call + expected_cache_rebuild
    < expected_remaining_calls * per_call_context_saving + quality_benefit
```

No universal token threshold is correct. Measure the workload's remaining-turn
distribution and provider cache lifetime.

## 6. Provider cache capability contract

A provider adapter should expose typed capabilities instead of a generic flag:

```text
supports_implicit_prefix_cache
supports_explicit_breakpoints
minimum_cacheable_tokens
cache_granularity_tokens
cache_ttl_modes
cache_namespace/key support
cache_read_usage_path
cache_write_usage_path
input_price
cache_read_price
cache_write_price
cache_isolation_scope
cache_identity_fields
```

Official APIs differ materially. OpenAI caching is based on exact prefixes and
may expose cache keys/retention controls; Anthropic uses explicit cache control
breakpoints and separate read/write accounting; Gemini has explicit context
cache resources and TTLs. Provider adapters normalize evidence into Cambium's
usage record without pretending the protocols are identical.

An absent provider cache field is `unknown`, not a proved miss. A positive
provider-reported cache-read count is a hit. A documented zero is a miss. This
three-valued state must not be collapsed before routing evidence is updated.

## 7. Routing interaction

The context engine supplies a cache-affinity identity to routing, but does not
choose providers. Routing first constructs a feasible set from authorization,
exact model constraints, protocol/tool support, context/output capacity,
budget, health, and rate limits. It then applies configured priority. Only
inside that class may it optimize expected quality, cost, latency, and the
switching cost of losing cache affinity.

This is a constrained non-stationary bandit/scheduling problem with switching
costs. Exploration cannot violate hard constraints. Deterministic weighted
rendezvous hashing is a suitable baseline for stable assignment among truly
equivalent providers because it minimizes churn when membership changes.

See [`provider-routing.md`](provider-routing.md).

## 8. User-interface contract

Every interactive frontend attaches to a durable branch rather than inventing
a new unrelated conversation for each line. It displays current-turn and
cumulative usage, including cache reads/writes and context utilization. The UI
is a projection over the canonical event stream; it does not own session or
agent state.

See [`terminal-interface.md`](terminal-interface.md).

## 9. Current implementation map (2026-08-20)

Verified in source/tests at the review base:

- immutable context epochs and parent/child fork/resume machinery exist;
- context descriptors bind to artifacts and reject mismatches;
- terminal checkpoints and rolling transcript compaction exist;
- provider usage events and token aggregation exist;
- REPL/TUI operator entry points enable context reuse for each one-shot run.

Open deltas:

- REPL/TUI prompts still create separate one-shot session leaves instead of
  continuing one durable interactive branch;
- current rolling folds are deterministic transcript projections, not the
  structured extraction + model synthesis protocol above;
- provider cache capability, identity, TTL, and read/write pricing are not
  modeled as one typed contract;
- `prompt_prefix_bytes` measures the leading system-message bytes, not the
  complete provider request prefix;
- one-sample cache observations are insufficient acceptance evidence;
- strict model pinning, rate/concurrency modeling, and cache-aware cost routing
  still have implementation gaps described in `provider-routing.md`.

## 10. Verification

A cache/context change is accepted only with frozen configuration and repeated
trials:

1. **Replay identity:** canonical request digest equality for hit/cold paths.
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

## 11. Computer-science foundations

- **Persistent functional data structures / Merkle DAGs:** immutable shared
  ancestry and content-addressed checkpoints.
- **Event sourcing and materialized views:** raw events are authoritative;
  active context is a rebuildable projection.
- **MVCC / snapshot isolation:** children read a stable parent version and
  publish against an expected generation.
- **Write-ahead logging and idempotency keys:** crash-safe append before state
  advancement; duplicate completion suppression.
- **CALM theorem / semilattices:** monotone facts can merge without ordering;
  non-monotone edits require coordination.
- **Online algorithms / ski rental:** decide when cache rebuild and compaction
  amortize over future calls.
- **Bandits with switching costs:** provider quality exploration under cache
  affinity and changing evidence.
- **Little's Law:** request rate is not concurrency; safe in-flight capacity
  depends on observed service time.
- **Information retrieval and long-context results:** retrieval/projection is
  required because relevant information can be lost in the middle of long
  prompts.
- **Typed recursive language-model runtimes:** recursion needs explicit
  operators, budgets, and termination conditions rather than unconstrained
  self-calls.

## 12. References

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
