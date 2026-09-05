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

Normal work appends actions and observations to `R`. The current worker checks
raw-tail thresholds between completed turns and requests a fold at delegation
and root terminal boundaries. These boundary folds cost a provider request;
they are not free bookkeeping. Forked exact children return their result rather
than publishing a competing parent trunk. The actual conditions are in
`worker._bound_context_continuation` and `worker._run_agent_loop`.

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

The current K0 compiler is deterministic Python, **not another model recursively
summarizing summaries**. It folds the semantic entries, removes matching
superseded/invalidated fact and decision strings, and deduplicates retained
constraints, verification strings and open items. The original segment set is
retained with rollover provenance.

There are real limits: identity is based on normalized text, not semantic
entailment. Differently worded contradictions are not automatically resolved.
Open items are deduplicated, not automatically marked completed. K0 is not a
lossless transcript, a proof system, or the proposed typed WorkLedger.

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

## Branches and provider placement

Context representation and execution placement answer different questions:

| Mode | Context at child start | Default placement |
| --- | --- | --- |
| `trunk` | Complete compatible parent checkpoint prefix and private continuation | Same provider/model (`inherit`) |
| `semantic` | Published semantic entries under a fresh branch/provider head | Another usable lane (`spread`) |
| `fresh` | Self-contained task, no parent checkpoint or semantic entries | `spread` |

Exact reuse needs matching provider/model/protocol/reasoning, instructions,
tools and checkpoint bytes. It is a correctness requirement for an exact fork,
not proof of a cache hit. An explicit impossible exact fork does not silently
become semantic. A semantic child on another provider transfers knowledge, not
KV-cache state. A fresh child deliberately transfers neither.

A child returns a bounded result. The supervisor separately integrates its code
and resumes the parent at the accepted artifact head. The parent adds new
observations and conclusions to its own continuation; it does not concatenate
complete sibling transcripts into its trunk.

[Context branches](context-branches.md) owns automatic delegation decisions and
placement defaults. [Child lifecycle](subagents.md) owns suspension and joins.
[The reference](../reference/context-branches.md) owns exact tool arguments.

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
cannot repay itself through replay savings alone; its purpose may instead be
preparing a reusable checkpoint. The current boundary policy does not forecast
`N`, so short tasks can still spend an avoidable extra summary call.

Cached input still consumes some provider resources and may consume account
quota. A provider's cash tariff, request limit, weekly allowance and generation
rate are separate quantities. Unknown quota is not unlimited quota; output
rate must not count prompt tokens. Only provider-reported usage establishes
cache hits. The TUI labels byte-derived context sizes as estimates.

Small local work often costs less than spawn, fold, join and re-verification.
Independent work can justify another provider when it shortens the critical
path or uses otherwise idle capacity. Exact same-trunk blocking work keeps the
parent provider. These are policies to evaluate, not a promise that every
additional child makes a task faster.

## Reading a trace

Inspect `usage_event.call_kind` to separate action calls from summary calls.
Inspect `context_checkpoint` and `context_epoch_advanced` for the published
projection, and `branch_history` for the raw tool evidence it covers. A tool
proposal, child admission, accepted child artifact and passing combined check
are different events; do not fold them all into "the child finished".

For example, a failed check followed by a corrected edit should retain the
current result, the relevant failure constraint and any check still owed.
A summary that says only "tests run" has lost that distinction. The model must
supply useful evidence references today; the string-based K0 compiler cannot
infer which obligation a later check discharged. Inspect such losses before
expanding the schema or adding more prompt instructions.

## Prompts and experiments

Coding and summary policies can be replaced by an offline GEPA run. A session
pins its chosen text; replacement affects new sessions or `/new`, not a live
prefix. Protocol/schema mechanics remain code-owned. See
[optimization](optimization.md) for deployment and experiment commands.

Compare prompt candidates on accepted artifacts and held-out tasks, including
corrections, long sessions, history recall and joins. A shorter summary that
loses an obligation is not a win. Neither one cache hit nor one passing task
establishes a general resource improvement.

## Implementation anchors

`worker._bound_context_continuation`, `worker._write_epoch_checkpoint`,
`summary_trunk.partition_summary_trunk`, `append_summary_entry`,
`compile_k0_projection`, `rollover_summary_trunk`, and `context_policy.CastPolicy`
own this path. They use existing events, checkpoints and Git; CAST needs no
vector database, second memory service, or permanent planner/reviewer hierarchy.
