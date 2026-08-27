# CAST: Cache-Aligned Semantic Trunking

**Status:** implemented flat semantic trunk, checkpoint fork/resume, and
thresholded K0 rollover; adaptive break-even selection remains an evaluation
target.

## Abstract

Long-running coding agents need more history than should remain in every model
request. Raw trajectories contain useful decisions, but also repeated reads,
failed tool syntax, abandoned plans, superseded observations, and intermediate
repository states. Rewriting one global summary removes that noise but changes a
large prompt prefix and can destroy provider-side prefix-cache reuse.

**Cache-Aligned Semantic Trunking (CAST)** keeps raw event history durable while
presenting an append-only semantic projection:

```text
H + S1 + S2 + ... + Sn + Wn
```

`H` is the stable instruction and tool head. Each `Si` is an immutable semantic
delta over one disjoint range of raw events. `Wn` is the small unsummarized
frontier tail. A flush summarizes only `Wn`, appends `S(n+1)`, and removes the
covered raw range from the active projection without deleting it from durable
history. Earlier summary bytes remain unchanged, which aligns the logical data
structure with exact-prefix prompt caches.

CAST-FJ extends the trunk with fork-join subagents. An exact-context-eligible
child reuses the exact provider/model prefix; a child on another provider starts
from provider-neutral semantic summaries. Neither is provider-cache evidence.
Semantic result admission and Git artifact integration remain separate
operations.

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
identity (`src/cambium/summary_trunk.py:49-59`,
`src/cambium/summary_trunk.py:85-103`).

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
correctness assumptions. `CacheCapability` normalizes minimum size, TTL,
granularity, and read/write prices; namespace and isolation are not modeled
(`src/cambium/provider_scheduler.py:56-124`). A cache miss must produce the same
semantic request and result contract as a hit.

The live worker flushes at the raw-tail high-water threshold, delegation, and
terminal boundaries (`src/cambium/worker.py:4435-4448`,
`src/cambium/worker.py:5217-5225`, `src/cambium/worker.py:5382-5397`). The
separate `breakpoint_due` horizon check is not wired to a worker flush
(`src/cambium/provider_scheduler.py:282-306`).

`provider_cache_hit` is copied from normalized provider usage; matching a
prefix or model makes a child eligible, but never proves a hit
(`src/cambium/diffundo.py:1226-1237`, `src/cambium/worker.py:2990-3005`).

## 4. Same-provider exact-context child

An exact-context child can reuse the parent message prefix only when the
provider boundary, model, protocol, reasoning configuration, tool schema,
serialized messages, hashes, and prefix size match. This is eligibility, not a
provider-cache eligibility or a hit (`src/cambium/worker.py:2487-2533`).

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

## 5. Cross-provider semantic child

An incompatible provider can reuse the immutable summary history but cannot
reuse the parent provider's exact prefix.

```text
Parent on provider A

 [ H_A ][ S1 ][ S2 ][ S3 ]
                |
                | export provider-neutral summaries
                v
Semantic child on provider B

 [ H_B ][ S1 ][ S2 ][ S3 ][ scoped child contract ]
          \ semantic reuse / no hit claim /
```

This is a **semantic fork**, not a cache hit. For a non-redacted checkpoint, the
child starts with a fresh provider-specific head and receives only the summary
history plus its scoped contract (`src/cambium/worker.py:2571-2618`).

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

The supervisor checks that head and emits `join_invariant_failed` on mismatch;
a successful worker that violates its result/worktree commit invariant becomes a
failure with cleanup deferred (`src/cambium/supervisor.py:6354-6382`,
`src/cambium/supervisor.py:4774-4810`).

Checkpoint-bound resume requires the current worktree hash to equal the latest
turn checkpoint hash. A dirty worktree that cannot resume is salvaged to
the 1 MB-bounded `salvage/<task>/<gen>/workspace.diff` and emits
`worktree_salvaged` (`src/cambium/supervisor.py:858-875`,
`src/cambium/supervisor.py:2847-2869`,
`src/cambium/supervisor.py:2758-2816`).

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

`K0` folds the active semantic fields from immutable summary entries and keeps
the source entries in a rollover manifest; superseded facts disappear only from
the active projection (`src/cambium/summary_trunk.py:670-735`,
`src/cambium/worker.py:4608-4630`). The worker rolls over after configured
segment/token bounds and resets the prompt baseline. `k0_rollover_decision`
computes an economic break-even decision, but it is a library-only path; the
live worker uses thresholds (`src/cambium/summary_trunk.py:769-818`).

## 8. Current Cambium implementation

Implemented:

- append-only, disjoint semantic summary segments;
- runtime-stamped segment identity;
- durable raw history and immutable checkpoints;
- exact-context-eligible child forks, with provider hits reported separately;
- cold cross-provider semantic-summary forks;
- thresholded K0 rollover with a durable source manifest;
- a persistent interactive TUI branch composed from isolated supervisor leaves;
- per-agent usage, throughput, provider/model, trunk, tail, epoch, and lineage
  views (`src/cambium/tui_screen.py:2325-2341`,
  `src/cambium/tui_screen.py:2447-2457`).

Still empirical or future work:

- provider-specific cache-block calibration;
- adaptive `K0` break-even selection and held-out quality canaries;
- structured automatic resolver children for every Git conflict class;
- workload-specific measurement of the library-only break-even policy.
