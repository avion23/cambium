# Cache-first context reuse — corrected research record

**Status:** research record, not runtime authority. The active design contract is
[`../architecture/context-engine.md`](../architecture/context-engine.md). Source
and tests establish current behavior.

**Reviewed:** 2026-08-21 against current `main` and the verified append-only
summary-trunk implementation.

## 1. Research question

Can Cambium lower repeated-context cost and latency by making one stable context
trunk reusable across turns, forks, restarts, and child agents?

Yes, with one correction: the reusable object is an immutable context
projection, not a persistent or recursively mutable LLM. The provider may reuse
its internal prefix/KV state when it sees a compatible request prefix, but that
state is opaque, optional, provider-specific, and evictable. Cambium must remain
correct when every request is cold.

The useful decomposition is:

```text
append-only history
    -> immutable context epoch / checkpoint
    -> canonical provider request
    -> optional provider prefix-cache reuse
```

A cache hit accelerates replay. It does not advance Cambium's logical state.

## 2. What is now implemented

The earlier prototype has been replaced by an append-only semantic trunk:

- `SummaryEntry` binds every segment to an exact raw source digest, message
  count, sequence number, covered turn, bounded semantic fields, and canonical
  entry digest;
- the active provider request is `stable head + S1..Sn + raw tail`;
- threshold, delegation, and terminal boundaries make an additional provider
  summary call, validate it, append one immutable entry, and clear only the
  covered raw tail;
- earlier summaries are never summarized again or rewritten; tests assert exact
  prefix and `S1` byte stability when `S2` is appended;
- compatible children reuse the exact trunk prefix, while incompatible
  providers receive the same semantic entries under a fresh provider-specific
  head;
- legacy transcript-heavy checkpoints migrate at their next summary boundary;
- raw events, ordinary turn checkpoints, and immutable epoch artifacts remain
  the external audit/recovery record;
- summary calls participate in token, request-debt, latency, cache, cancellation,
  wall-clock, and cost accounting;
- invalid summaries, redacted/corrupt checkpoints, and publication failures fail
  closed without advancing the trunk.

This establishes the mechanism and cold-path correctness. It does not by itself
prove provider cache retention, cost savings, or task-quality improvement; those
remain empirical questions under the verification protocol below.

## 3. Corrections to the earlier plan

### 3.1 “The main LLM is the cache” is the wrong abstraction

An API model has no Cambium-owned durable identity or mutable process state.
The model can be upgraded, routed elsewhere, lose its cache, or tokenize a
request differently. Cambium owns only the canonical history, projection,
request, and publication state.

Use these terms:

- **context epoch:** Cambium-owned immutable materialized view;
- **branch:** parent epoch plus a private continuation;
- **provider prefix cache:** an optional optimization for an exact compatible
  request prefix;
- **summary/checkpoint:** a lossy or structured successor projection;
- **semantic memory:** typed, provenance-bearing facts recalled separately.

Do not use “cached agent state” unless the state is a Cambium-owned artifact.

### 3.2 Structural equality and provider cache evidence are different

A digest or byte comparison can prove that Cambium generated an identical
prefix. It cannot prove that the provider retained the corresponding KV state.
The strongest direct evidence is the provider's usage field. Latency is only
supporting evidence because queueing, batching, network, and model load also
change latency.

The current `prompt_prefix_bytes` metric describes only the measured leading
system-message bytes. It must not be presented as proof that the complete
request prefix, tools, settings, or provider cache key are identical.

### 3.3 Cache identity is larger than message text

At minimum it includes provider, model, endpoint/protocol, reasoning settings,
ordered tool schemas, system/developer messages, ordered conversation items,
multimodal assets, serialization/tokenization version, and any provider cache
namespace/key or breakpoint configuration.

Provider adapters differ materially:

- some caches are automatic after a minimum prefix length;
- some require explicit breakpoints or cache-control annotations;
- some expose cache-read and cache-write tokens separately;
- TTL, granularity, key scope, and retention differ;
- a missing usage field may mean “unknown,” not “zero cache reuse.”

A provider-neutral boolean such as `cache_enabled` is therefore not a complete
capability model.

### 3.4 Appending a summary to the complete old trunk is not compaction

This sequence is wrong:

```text
full old transcript + concise summary + next turn
```

It keeps all old tokens, adds new tokens, and leaves the model attending over
both representations. The correct sequence preserves raw history externally
and replaces the covered region only in the next active projection:

```text
structured checkpoint + unresolved evidence + recent verbatim tail + next turn
```

The first request on the successor epoch may be a provider-cache miss. That is
the rebuild cost paid to reduce every later request in the epoch.

### 3.5 LLM summaries are not idempotent

Repeated summary calls can produce different valid text. The operation can be
idempotent only at the publication layer: one operation ID and expected parent
may publish at most one successor epoch. Summary content needs a schema,
provenance, required-field checks, and quality canaries; it is not authoritative
merely because it was produced by the same model that consumed the trunk.

### 3.6 One successful fork and one successful resume are smoke tests

The old 1/1 observations establish that those paths can work once. They do not
estimate cache-hit probability, latency improvement, cost reduction, quality,
or reliability. A routing policy must not learn a strong preference from such
a sample.

## 4. Revised hypothesis

For workloads with repeated, sufficiently long, exact-compatible prefixes,
immutable epochs plus provider affinity should reduce median billed uncached
input and/or latency without changing semantic results. The effect depends on:

- provider/model/cache protocol;
- exact request construction and minimum cacheable length;
- TTL and time between calls;
- whether tools/reasoning settings remain stable;
- how often routing fails over;
- branch depth and shared-prefix length;
- compaction cadence;
- the provider's cache tariffs and accounting fields.

This is falsifiable. A provider or workload may show no benefit, and Cambium
must then retain the correctness architecture without paying extra complexity
for unobserved cache locality.

## 5. Measurement protocol

### 5.1 Freeze the experiment

Record repository revision, provider, model, protocol, reasoning settings,
tool schemas, request serializer version, cache controls, account/project,
context length, branch operation, and wall-clock spacing. Do not change any of
these inside a paired run.

### 5.2 Paired warm/cold trials

For each frozen prompt family:

1. create a long stable prefix above the provider's documented minimum;
2. issue a seed request;
3. alternate compatible continuations and intentionally cache-busting controls;
4. include fork, resume, same-worker, fresh-worker, and provider-failover cases;
5. collect at least enough independent trials to show a distribution rather
   than a 1/1 anecdote;
6. randomize ordering where provider load can bias latency.

Persist raw normalized usage and the original provider usage object. Report
median and tail latency, input/output/cache-read/cache-write tokens, estimated
cost, success/quality metric, and cache-hit confidence interval. Keep “usage
field absent” separate from zero.

### 5.3 Correctness gate

Cache-on and cache-busted requests must use the same semantic task and pass the
same deterministic checks. For code tasks, compare changed artifacts, tests,
and result-envelope claims. A cheaper run that degrades the held-out task metric
is not accepted.

### 5.4 Compaction gate

Measure the one-time summary call and cache rebuild against future savings:

```text
compact when
  summary_cost + expected_rebuild_cost + expected_quality_loss
    < expected_remaining_calls * per_call_saving
```

This is an online optimal-stopping/ski-rental decision. Register thresholds
before the evaluation set is inspected; otherwise the compaction policy is
being fit to its own benchmark.

## 6. Implementation decisions promoted to architecture

The active context-engine contract now requires:

1. append-only raw history and immutable epochs;
2. cold-path semantic equivalence;
3. content/provenance binding and compare-and-swap publication;
4. least-authority child projections and bounded typed result envelopes;
5. deterministic, conflict-aware parent merge;
6. provider-specific cache capability metadata;
7. exact compatibility before cache locality;
8. separate input/output/cache-read/cache-write accounting;
9. summary replacement of the covered active projection, never destructive
   deletion of raw history;
10. empirical acceptance instead of assumed cache benefits.

## 7. Open implementation gaps

- The complete provider request identity is not represented as one typed value.
- Cache capability/TTL/breakpoint/key semantics are not modeled per provider.
- Structural evidence measures only a partial prefix.
- Usage normalization must preserve unknown versus zero for routing evidence.
- Estimated cost does not yet model every provider's cache-read/cache-write
  tariff.
- Routing debt is provider-wide rather than keyed by provider/model/protocol/
  cache identity.
- The REPL and TUI still create fresh one-shot sessions per prompt; they do not
  yet expose one durable interactive branch.
- The end-to-end compaction quality experiment described above has not been run
  on a frozen representative coding corpus.

## 8. Computer-science interpretation

- **Persistent data structures / Merkle DAGs:** branches share immutable
  ancestors and publish successor nodes.
- **Event sourcing and MVCC:** history is authoritative; epochs are projections;
  workers operate on snapshots and publish with expected-version checks.
- **Memoization:** exact provider replay is valid only under a complete cache
  key; eviction changes performance, not semantics.
- **Optimal stopping / ski rental:** decide when one-time compaction/rebuild is
  worth recurring savings.
- **CALM theorem:** monotone facts can merge without coordination; code edits
  and contradictory decisions require coordination.
- **Statistical decision theory:** tiny samples carry high uncertainty and must
  not dominate routing.

## 9. References

- OpenAI prompt caching: <https://platform.openai.com/docs/guides/prompt-caching>
- Anthropic prompt caching: <https://docs.anthropic.com/en/docs/build-with-claude/prompt-caching>
- Gemini context caching: <https://ai.google.dev/gemini-api/docs/caching>
- Prompt Cache: <https://arxiv.org/abs/2311.04934>
- SGLang / RadixAttention: <https://arxiv.org/abs/2312.07104>
- Lost in the Middle: <https://arxiv.org/abs/2307.03172>
- CALM theorem: <https://arxiv.org/abs/1901.01930>
