# CAST: Cache-Aligned Semantic Trunking

**Status:** implemented flat semantic trunk and interactive fork/resume protocol;
adaptive epoch rollover remains an evaluation target.

## Abstract

Long-running coding agents need more history than should remain in every model
request. Raw trajectories contain useful decisions, but also repeated reads,
failed tool syntax, abandoned plans, superseded observations, and intermediate
repository states. Rewriting one global summary removes that noise but changes a
large prompt prefix and can destroy provider-side prefix-cache reuse.

**Cache-Aligned Semantic Trunking (CAST)** keeps the complete raw event history
outside the model while presenting an append-only semantic projection:

```text
H + S1 + S2 + ... + Sn + Wn
```

`H` is the stable instruction and tool head. Each `Si` is an immutable semantic
delta over one disjoint range of raw events. `Wn` is the small unsummarized
frontier tail. A flush summarizes only `Wn`, appends `S(n+1)`, and removes the
covered raw range from the active projection without deleting it from durable
history. Earlier summary bytes remain unchanged, which aligns the logical data
structure with exact-prefix prompt caches.

CAST-FJ extends the trunk with fork-join subagents. A cache-compatible child
reuses the exact provider/model prefix. An opportunistic child on another
provider starts cold from the provider-neutral semantic summaries. Semantic
result admission and Git artifact integration remain separate operations.

## 1. Context is a graph

The durable session is an append-only directed graph, not one mutable chat
array. Turns, tool observations, summaries, checkpoints, forks, joins, and
artifact heads are nodes. Parent, continuation, fork, supersession,
invalidation, and integration relationships are edges.

```text
Raw turn graph

 t1 --> t2 --> t3 --> t4 --> t5 --> t6
                    \                 \
                     \                 +--> checkpoint C2
                      +--> checkpoint C1

Active semantic projection at C2

 [ stable head H ][ S1 covers t1..t3 ][ S2 covers t4..t5 ][ raw t6 ]
```

The **frontier** is the set of resumable leaf nodes. Every active frontier owns
one bounded model projection. A frontier may compact its new raw tail, fork a
child, join child evidence, or roll into a new epoch. Historical graph nodes do
not change.

```text
                              child A frontier
                             /
 H--S1--S2--W  --> checkpoint C7
                             \
                              child B frontier

 child A result -----------\
                            +--> deterministic join --> parent frontier C8
 child B result -----------/
```

## 2. Append-only semantic segments

Let `E` be the durable ordered event log and let `R_i` be disjoint raw ranges.
The active trunk at step `n` is:

```text
T_n = H || S_1 || S_2 || ... || S_n || W_n
```

with:

```text
coverage(S_i) = R_i
R_i intersection R_j = empty, for i != j
R_i precedes R_(i+1)
```

A semantic flush is:

```text
Before:
[ H ][ S1 ][ S2 ][ raw r17 ][ raw r18 ][ raw r19 ]
                    \----------- W3 -----------/

Summary request:
[ H ][ S1 ][ S2 ][ r17 ][ r18 ][ r19 ][ summarize only W3 ]

After validation:
[ H ][ S1 ][ S2 ][ S3 ]
```

The raw events remain in the session database. Only the active model projection
changes.

A segment is a delta, not a repeated snapshot. Changed conclusions are explicit:

```text
S1.decisions_added:
  D12: use SQLite for the local event store

S5.decisions_added:
  D31: use PostgreSQL for distributed deployment
S5.decisions_superseded:
  D12: no longer the distributed deployment choice
```

The model owns semantic fields such as decisions, facts, changed symbols,
verification results, relevant failed approaches, and open work. The runtime
owns sequence numbers, source hashes, source message counts, checkpoint
identity, and graph edges. The model is never trusted to compute bookkeeping
identity.

## 3. Why the structure is cache-aligned

Provider prompt caches commonly depend on an exact initial token or byte prefix.
CAST preserves the previous semantic trunk when a new segment is appended:

```text
Request 1: [ H ][ S1 ][ S2 ][ working suffix A ]
Request 2: [ H ][ S1 ][ S2 ][ working suffix B ]
Request 3: [ H ][ S1 ][ S2 ][ S3 ][ working suffix C ]
           \---- reusable prefix ----/
```

A repeatedly rewritten global summary instead changes most of the semantic
prefix on every compaction.

Cache retention and minimum cacheable block size are provider capabilities, not
correctness assumptions. Cambium treats an approximately 60-second warm horizon
as a configurable scheduling hint only. A cache miss must produce the same
semantic request and result contract as a hit.

Small tails should normally be batched until one of these boundaries:

- a provider/tool turn completed and the tail crosses its high-water mark;
- delegation requires a clean immutable branch point;
- terminal completion requires a reusable continuation checkpoint;
- the expected cache horizon is about to expire;
- the active context needs space for its output reserve.

Provider adapters should eventually expose:

```text
minimum_cacheable_tokens
cache_block_granularity_tokens
cache_horizon_s
supports_explicit_breakpoints
cache_read_price
cache_write_price
```

## 4. Same-provider cached child

A child can reuse the exact parent prefix only when the provider, model,
protocol, reasoning configuration, tool schemas, and serialized messages are
compatible.

```text
Parent, provider A/model M

 [ H_A ][ S1 ][ S2 ][ S3 ]
                |
                +--> child task

Child, provider A/model M

 [ H_A ][ S1 ][ S2 ][ S3 ][ child contract ]
 \--------- exact shared prefix -----------/
```

The child receives an immutable checkpoint descriptor and a private continuation.
It cannot mutate the parent frontier directly.

## 5. Fresh opportunistic child on another provider

A weaker, free, or otherwise available provider may be useful for bounded
research, indexing, summarization, or test triage even though it cannot reuse the
parent provider's KV cache.

```text
Cached parent on provider A

 [ H_A ][ S1 ][ S2 ][ S3 ]
                |
                | export provider-neutral summaries
                v
Fresh child on provider B

 [ H_B ][ S1 ][ S2 ][ S3 ][ scoped child contract ]
          \ semantic reuse / cold provider cache /
```

This is a **semantic fork**, not a cache hit. The child starts with a fresh
provider-specific head and the immutable summary history. It should receive only
the capabilities and evidence required for its task. Weak or free lanes can be
restricted to read-only task classes, and their claims can require review before
parent admission.

This allows Cambium to consume idle token capacity without rotating the main
long-lived branch away from its incumbent provider.

## 6. Branch-back protocol

Children return bounded result envelopes rather than complete transcripts:

```text
ChildResult {
  source_checkpoint
  status
  claims + evidence references
  changed artifacts
  verification results
  failed checks
  open questions
  usage
}
```

Admission order is deterministic and independent of wall-clock completion.
Monotone observations may be joined as a set. Code edits and contradictory
choices require coordination.

```text
                    parent checkpoint C7
                   /                    \
              child A                child B
                 |                       |
             envelope A              envelope B
                 \                       /
                  \-- deterministic join --/
                              |
                         parent C8
```

Semantic join and artifact integration are distinct:

```text
Context graph                         Git graph

C7 -- envelope A --\                  M -- child A commit --\
                    +--> C8                                  +--> M'
C7 -- envelope B --/                  M -- child B commit --/
```

Before a mutating parent resumes, the required invariant is:

```text
post_join_parent_HEAD == accepted_integration_HEAD
```

A merge conflict should become a structured conflict envelope with conflicted
paths and bounded evidence. The parent can resolve it or spawn an explicit
resolver child; it must not receive a false success summary for code it cannot
see.

## 7. When to start a new epoch

Appending summaries forever eventually creates segment sprawl and
lost-in-the-middle pressure. A **trunk epoch rollover** starts a new cache lineage
without deleting the session history.

```text
Old epoch E0
[ H0 ][ S1 ][ S2 ][ ... ][ S18 ][ W ]
                     |
                     | compile currently active state
                     v
New epoch E1
[ K0 ][ W' ]
```

`K0` is grounded from authoritative repository state, active decisions,
unresolved obligations, verification evidence, and the raw event graph. It
permanently removes superseded facts only from the active projection.

Let:

- `O` be old trunk tokens;
- `K` be new snapshot tokens;
- `p_c` and `p_u` be effective cached and uncached token prices;
- `C_r` be snapshot generation and validation cost;
- `Q_o - Q_n` be per-call quality improvement;
- `N` be expected remaining calls.

Rollover becomes economical when:

```text
N * [p_c * (O - K) + (Q_o - Q_n)]
    > C_r + (p_u - p_c) * K
```

or:

```text
N* = [C_r + (p_u - p_c) * K]
     / [p_c * (O - K) + (Q_o - Q_n)]
```

For subscriptions, the effective price includes shadow prices for five-hour,
weekly, and monthly windows. A hard context limit, trust-policy change,
provider/model migration, tool-schema change, poisoned summary, or failed
quality canary forces rollover regardless of the estimate.

A completely new session is rarer. It is appropriate for an unrelated task,
repository or customer boundary, independent evaluation, or untrusted/corrupt
history. Context size alone normally calls for a new epoch inside the same
session.

## 8. Current Cambium implementation

Implemented:

- append-only, disjoint semantic summary segments;
- runtime-stamped segment identity;
- durable raw history and immutable checkpoints;
- exact cache-compatible child forks;
- cold cross-provider semantic-summary forks;
- a persistent interactive TUI branch composed from isolated supervisor leaves;
- per-agent usage, throughput, provider/model, trunk, tail, and epoch views.

Still empirical or future work:

- provider-specific cache-block calibration;
- adaptive `K0` rollover and held-out quality canaries;
- structured automatic resolver children for every Git conflict class;
- workload-specific measurement of the break-even policy.

## 9. Research questions

| RQ | Hypothesis | Principal metrics |
| --- | --- | --- |
| Cache efficiency | Append-only segments improve reusable-prefix ratio | cache tokens, TTFT, cost |
| Contradiction avoidance | explicit invalidation reduces regressions | contradictions, failed tests |
| Fork accuracy | exact checkpoint children improve coupled edits | success, merge conflicts |
| Opportunistic capacity | cold semantic children use idle providers safely | useful tokens, review cost |
| Optimal rollover | online break-even beats fixed resets | cost-success frontier |

Suggested title:

> **CAST: Cache-Aligned Semantic Trunking for Long-Horizon Autonomous Coding Agents**
