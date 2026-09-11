# CAST: Cache-Aligned Semantic Trunking

**Status:** current context model and implementation. The worker and
`summary_trunk.py` implement normal folds, checkpoint forks and K0 rollover.
A typed work ledger and a globally optimized rollover schedule are not implied.

## The model

CAST keeps a compact working context without repeatedly rewriting its useful
past. The model is stateless; the harness owns context and history. A provider
cache makes an unchanged prefix cheaper to process, but losing that cache must
not lose the task.

A branch's active request has this shape:

```text
H  M  S1  S2  ...  Sn  R
│  │  └──────┬──────┘  └─ recent raw actions and observations
│  │         └─ immutable semantic entries
│  └─ branch task message
└─ stable system instructions and tool definitions
```

`H + M` is the stable two-message head in the current implementation. The
system message includes the model identity and tool catalogue. `M` is task
data, not an instruction interpolated into the system head. An exact child
keeps that head and appends its own task in the continuation.

The semantic trunk is an **accumulation of useful changes in knowledge**, not a
running prose summary rewritten on every turn. Preserve decisions, findings,
changed artifacts, check outcomes, relevant failed approaches and open work.
Discard routine noise from the working prompt, not from durable history.

## One ordinary fold

Suppose the branch has accumulated raw range `R1`:

```text
before       H M S1 S2 R1
summary call H M S1 S2 R1 <summary-control>
after        H M S1 S2 S3
next work    H M S1 S2 S3 R2
```

The summary call sees the existing trunk for background, but **only `R1` is the
new source range**. `S3` records its semantic delta. Neither `S1` nor `S2` is
rewritten or treated as new raw evidence. The next fold covers `R2`, not `R1`.
The published prefix through `S2` therefore remains byte-identical. The old
request's raw suffix is replaced, so reuse is of that common prefix, not a
promise that the entire next request is cached.

An epoch number identifies a published checkpoint snapshot. Ordinary folds
advance that number too; it does not mean that every epoch change rewrites the
trunk. Distinguish an **append-only fold** from a **prefix-replacing K0 rollover**.
Do not infer cache invalidation or a cache hit from the epoch counter alone.

The worker supplies sequence, source digest, source-message count and covered
turn; the model supplies the semantic fields. A successful fold appends one
entry, removes its covered raw range from the active request, and publishes a
successor epoch. Merely appending a summary after the entire transcript would
not reduce the working set and is not CAST compaction.

Normal work appends actions and observations to `R`. The worker folds when the
raw working set crosses its threshold, or when a semantic child needs the new
knowledge. Finishing a task, spawning an exact-trunk child, and spawning a fresh
child do **not** force a summary. They save an exact checkpoint with its recent
raw continuation; a later consumer can compact when necessary. One semantic
batch shares one fold, not one summary request per child. The conditions live in
`worker._bound_context_continuation` and `worker._run_agent_loop`.

Checkpointing is local persistence. Summarizing is a paid model operation.
Keeping them separate means a completed short task does not fail merely because
an unnecessary summary provider is unavailable. Both a one-call finish and a
blocking exact child can retain useful history without a single summary call.

Malformed summary output leaves the prior trunk and raw tail intact while the
existing bounded deferral path applies. A failed fold cannot erase its source.
The current worker can still fail a task on an unrecoverable compaction error;
CAST does not yet make every summary failure transparent.

## What remains outside the prompt

Events and checkpoints retain the exact evidence. `branch_history` lists calls
and reopens one action/observation or a bounded transcript window. Reading
history produces a new observation; it never re-executes the old command or
rewrites an old summary.

Distinguish the storage objects:

| Object | Purpose |
| --- | --- |
| Event log | Ordered execution and usage observations |
| Ordinary turn checkpoint | Resumable transcript/workspace snapshot; may be updated while a batch finishes |
| Immutable epoch checkpoint | Published provider prefix, continuation and compatibility identity |
| Git commit/worktree | Actual artifact state, separate from remembered conclusions |
| Provider cache | Optional reuse of request processing, not durable agent memory |

An automatically generated SituationFrame is a transient view, not new evidence.
An explicit `inspect_state` call is different: its returned view is a recorded
tool observation and remains in history and the raw tail. Checkpoint cleanup
removes only the generated loop-state frame; it must not delete similarly
formatted tool results or user text. The same bounded renderer serves model and
operator inspection without creating a second state store.

A retained summary is not proof that a check passed. A passing check describes
the code it ran against, not a later merged tree. The current runtime retains
check observations but does not claim that a successful shell command proves
arbitrary task correctness. Repository benchmarks check the accepted artifact
with an external executable criterion.

## Corrections and K0 are different

Within an ordinary trunk, corrections append `decisions_superseded` or
`facts_invalidated` together with new conclusions. Old entries stay intact.

Eventually even semantic entries can fill the working set. K0 is the separate
rollover operation:

```text
old epoch    H M S1 S2 ... Sn       retained unchanged in history
new epoch    H M K0                 new cache lineage
later        H M K0 S1' S2' ... R
```

The K0 compiler is deterministic Python, **not a model recursively summarizing
summaries**. Semantic entries can use stable labels: `D1` for a decision, `F1`
for a fact, `O1` for an obligation, and `V1` for a check. Unlabelled items use
exact text identity. These are identifiers within the existing string lists,
not another ledger or database.

For example, a later delta can replace `F1: strings concatenate` with
`F1: strings now raise TypeError`, put `O1` in `open_items_resolved`, and put
`V1` in `verification_invalidated` when its earlier Git head is obsolete. K0
removes those old active values and retains the replacement, unresolved work,
and still-applicable checks. Invalidations apply before additions in the same
delta, so a replacement is not accidentally deleted. Repeated closure of an
already absent label is harmless; it must not block compaction.

Original entries remain unchanged and accessible through history. The model
must identify a correction or completion from actual evidence. K0 does not infer
semantic contradiction, automatically prove an obligation complete, or turn a
shell exit code into proof of task correctness. Its active state is only as good
as those recorded deltas; it is not a lossless transcript or a proof system.

Manual `/compact` materializes available semantic entries into K0 and keeps any
recent raw tail unchanged. K0 describes the summarized range, not a claim that
nothing has happened since: a child join or passing check in the later raw tail
can already have resolved an item still listed there. Its outcome states that
boundary explicitly. The next semantic fold records the corresponding closure;
compaction does not guess it from arbitrary prose or add a paid summary call.
With no semantic entries it reports that fact rather than inventing a model-free
summary. Both manual and automatic rollover reset the prompt-accounting baseline
because the active prefix changed.

`context_policy.CastPolicy` currently rolls over after more than 16 segments by
default. Its optional trunk-token bound is disabled at zero. The economic
helpers are not the live policy owner. A rollover must restore the configured
bounds; large irreducible semantic state can still exceed them.

Because K0 replaces the active semantic prefix, the next request can be cold.
The worker resets the prompt-accounting baseline rather than pretending it is
another append to the previous cached prefix. Earlier epoch files remain
unchanged. The active trunk grows by immutable additions between rollovers; every
published checkpoint remains immutable even after a rollover. These are the
two meanings of append-only here, not a requirement to carry all old summaries
in every future request.

## Branches

CAST defines what a child can inherit: exact trunk, semantic trunk, or fresh
context. Delegation defaults and provider placement belong to
[context branches](context-branches.md); child admission and artifact joins
belong to [subagents](subagents.md). Exact values are in the
[context-branch reference](../reference/context-branches.md).

An exact fork requires compatible request identity and an unredacted checkpoint;
that proves only semantic compatibility, not a provider cache hit. A redacted
checkpoint is never an exact fork. Semantic reuse exports only validated summary
entries from the persisted checkpoint, so already-redacted summaries may be
reused under a fresh provider head. Semantic context is provider-neutral: it
does not carry cache identity or claim a provider-cache hit, and it never sends
unredacted checkpoint material. Semantic/fresh forks do not transfer KV cache
state.

## Resource economics

CAST optimizes a working set, not the number of tokens produced. Measure the
whole task: action calls, summary calls, retries, input/cache/output usage,
child startup, provider queues, integration, verification and elapsed time.

For an estimated `N` remaining calls, the break-even comparison is:

```text
summary call + cache-disruption cost
    < N × (cost of the raw range − cost of its semantic entry)
```

This is a decision model, not an implemented optimizer or exact token formula.
Use the provider's actual cache tariff and account accounting when known.
Measure cold and warm requests separately. A final fold with no future consumer
cannot repay itself through replay savings alone, which is why ordinary finish
only checkpoints. The current threshold policy does not forecast `N`; further
changes need task-level measurements, not a compulsory economic-model call.

Provider quota, request capacity and placement are separate from CAST and are
documented in [provider routing](provider-routing.md). For CAST decisions, use
provider-reported cache/input/output evidence; a matching prefix alone is not a
cache hit. The TUI labels byte-derived context sizes as estimates.

## Reading a trace

Inspect `usage_event.call_kind` to separate action calls from summary calls.
Inspect `context_checkpoint` and `context_epoch_advanced` for the published
projection, and `branch_history` for the raw tool evidence it covers. A tool
proposal, child admission, accepted child artifact and passing combined check
are different events; do not fold them all into "the child finished".

A context checkpoint, cache descriptor, or compatible exact fork proves only
that a prefix is eligible for reuse. Count a cache outcome only when a
`usage_event` carries provider-reported `provider_cache_hit` or cached-token
evidence; leave the outcome unknown when those fields are absent.

For example, a failed check followed by a corrected edit should retain the
current result, the relevant failure constraint and any check still owed.
A summary that says only "tests run" has lost that distinction. The model must
supply useful evidence references today; the string-based K0 compiler cannot
infer which obligation a later check discharged. Inspect such losses before
expanding the schema or adding more prompt instructions.

## Prompt experiments

Summary policy is supplied only when a fold is requested. Offline GEPA can
replace coding or summary policy for new sessions; it does not change CAST
semantics or add a runtime classifier. See [optimization](optimization.md).

## Implementation anchors

`worker._bound_context_continuation`, `worker._write_epoch_checkpoint`,
`summary_trunk.partition_summary_trunk`, `append_summary_entry`,
`compile_k0_projection`, `rollover_summary_trunk`, and `context_policy.CastPolicy`
own this path. They use existing events, checkpoints and Git; CAST needs no
vector database, second memory service, or permanent planner/reviewer hierarchy.
