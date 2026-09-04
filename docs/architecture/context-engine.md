# Cache-first context engine

**Status:** implemented CAST summaries, checkpoints, context forks, and
configured K0 rollover. Richer work-ledger/result-capsule proposals are not
implied by those mechanisms.

## Durable history, disposable projection

The reusable object is an immutable context projection, not a stateful API
model. Provider prompt/KV cache is an acceleration of sending that projection;
it is never the authority for task correctness or recovery.

The ordinary active request is:

```text
stable system/tool head + S1 + S2 + ... + Sn + small raw working tail
```

Each `Si` is a published semantic summary of one new disjoint raw range. A flush
appends one summary entry and removes only its covered raw tail from the active
request. Earlier summary bytes remain unchanged. Appending a summary after the
entire old transcript without removing the covered raw region is not compaction.

Events and checkpoints retain the raw evidence outside the active prompt.
`branch_history` can reopen a recorded tool action/observation without executing
it again. A summary is lossy and may be wrong; its conclusions do not turn into
verified tool results merely by surviving several turns.

## Separate identities

| Object | Identity / purpose |
| --- | --- |
| Provider cache | Provider-owned prefix reuse; performance evidence only |
| Checkpoint | Immutable request/continuation and metadata for recovery |
| Active context epoch | The current projection and its lineage |
| Worker generation | A particular process attempt, including restart fencing |
| Git head | The actual code artifact being edited or accepted |

A warm worker does not prove a warm provider cache. Equal context hashes do not
prove a hit. A code merge does not automatically update a remembered conclusion.
These identities must be carried explicitly where they affect a transition.

## Checkpoint and prefix contract

Epoch files contain provider messages and a continuation suffix, with metadata
for task/generation, usage, deadlines, and context compatibility. The cache key
includes provider/model/protocol/reasoning identity, system/tool hashes, exact
prefix/suffix/full hashes, prefix bytes, and the provider boundary.

Loaders verify the stored content against its descriptor. Redacted or
incompatible state cannot be treated as an exact byte-identical fork. A cold
provider must still receive a correct request; cache availability cannot change
semantic request content.

An exact child deliberately receives the complete compatible parent prefix,
including its raw tail. A semantic child receives immutable summary state under
a fresh provider head. A fresh child receives neither. The same distinction
applies to continuation across provider changes; see
[context policy](context-branches.md).

## Summary flush

At an eligible completed-turn boundary, the worker freezes the new raw range,
computes source identity, and asks for a bounded summary. The model supplies
semantic fields; the harness supplies sequence, digest, count, and covered-turn
metadata. Publication validates the result and creates an immutable successor
checkpoint before advancing the active projection.

The semantic fields preserve objective/outcome, decisions and supersessions,
facts and invalidations, changed files/symbols, verification results, relevant
failed approaches, and open work. These are the current `SummaryEntry` fields,
not a separately persisted WorkLedger. The canonical schema and limits live in
[summary_trunk.py](../../src/cambium/summary_trunk.py); the prompt should not
repeat the same long schema in several places.

A malformed summary can be deferred within the existing bounded allowance,
leaving the raw tail and accepted checkpoint intact. Other errors follow the
worker's explicit compaction-failure path. A failed fold must not destroy the
only copy of the evidence. Summary calls consume real provider requests,
tokens, latency, wall budget, and quota, just like action calls.

## K0 rollover

A configured `CastPolicy` can trigger rollover by segment/token thresholds.
The worker compiles the active semantic state into a bounded K0 entry, records
rollover provenance, and publishes a new epoch. Old source entries remain in
durable history. This starts a **new cache lineage**: prompt accounting resets
rather than subtracting the old prefix's length.

Normal append-only folds and K0 rollover are different operations. Do not call
rollover a free append to the old prefix, or silently rewrite old epoch files.
The library's economic decision helpers do not mean a measured global
cache/throughput optimizer already controls every rollover.

## Child results and artifacts

The current supervisor joins bounded worker results and validated Git artifacts
using the [child lifecycle](subagents.md). It does not currently expose every
field of the proposed versioned, evidence-linked `ResultCapsule` in
[agent-state reference](../reference/agent-state.md).

The parent must distinguish accepting a child's conclusion from accepting its
commit. Its worktree must match the accepted integration head before resuming
from a code join. Checks run on the child alone are not automatically checks of
the combined tree. Do not invent an additional merge authority or memory store
to express this distinction.

## Resource consequences

An exact warm prefix can save repeated prompt processing, but retaining every
old token also has a cost. Use raw-tail limits and bounded summaries; measure
uncached input, cache reads, generated output, summary overhead, and wall time.
Provider capability/TTL describes what may be cached; provider usage supplies
actual hit evidence. Cache savings never turn an incompatible checkpoint into
a valid one.

Routing and quota ownership are described in
[providers as resources](provider-routing.md). Context code supplies compatibility
and affinity; it does not run a second provider scheduler. Different providers
are useful parallel resources when the task can use semantic or fresh context.

## Verification proportional to the change

Checkpoint/fold changes need focused identity, raw-tail retention, and recovery
regressions. Branch changes need exact/semantic/fresh and ordered-join checks.
A rendering change should not require a new model approval layer.

Claims of better cache economics require repeated provider-reported warm/cold
measurements, not one matching hash or a single cached call. Claims of better
summaries require held-out tasks that retain obligations, reopen exact evidence,
and verify the final artifact. Keep negative results and resource use visible.

## Source and related design

[Worker context loop](../../src/cambium/worker.py),
[summary/K0 projection](../../src/cambium/summary_trunk.py),
[cache policy and quota values](../../src/cambium/provider_scheduler.py),
[provider calls](../../src/cambium/diffundo.py),
[history](../../src/cambium/branch_history.py).

The broader [operating model](agent-operating-model.md) and
[evaluation proposals](../research/agent-system-evaluation.md) preserve future
ideas. They are not evidence that a canonical model/operator state projection,
typed work ledger, or optimized prompt deployment is already complete.
