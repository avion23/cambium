# Rolling context and recursive agent reuse — corrected research record

**Status:** research record, not runtime authority. The active contracts are
[`../architecture/context-engine.md`](../architecture/context-engine.md),
[`../architecture/provider-routing.md`](../architecture/provider-routing.md),
and [`../architecture/terminal-interface.md`](../architecture/terminal-interface.md).

**Reviewed:** 2026-08-20 against `main@877e4a7`.

## 1. Answer to the proposed design

A long-lived main context can be the base for subagents and can be rolled
forward repeatedly. What cannot be reused is hidden model state. The durable
unit is a Cambium-owned immutable epoch:

```text
raw event history
  -> active epoch
  -> zero or more child snapshots
  -> typed child result envelopes
  -> deterministic parent merge
  -> optional validated successor epoch
```

The model is a pure-looking transition oracle at the architecture boundary:

```text
(model request, tool observations) -> proposed next action/result
```

It may be nondeterministic internally, but it does not own the branch. Cambium
owns transition validation, persistence, budgets, tool effects, and publication.

## 2. Correct rolling lifecycle

### 2.1 Establish a stable epoch

Build the active projection from stable instructions, tool schemas, repository
facts, parent checkpoint, unresolved obligations, and a recent continuation.
Canonicalize it and bind it to provider/model/protocol/settings/artifacts.

### 2.2 Continue the parent

Append user, assistant, and tool events to the raw log. Each call re-materializes
or incrementally extends the active projection. Provider prefix reuse is a
performance side effect when exact compatibility holds.

### 2.3 Fork children

A child receives an immutable parent epoch ID, expected generation, a scoped
context projection, task contract, tools, budget, and deterministic merge slot.
It does not receive every sibling transcript or every parent scratch detail.

This is snapshot isolation, not process cloning. A child may run on another
provider/model and still reuse the semantic checkpoint, but it should not be
counted as a provider KV-cache continuation unless the complete cache identity
matches.

### 2.4 Return typed envelopes

A child returns claims with evidence, changed artifact identities, failed
checks, open questions, usage, and source checkpoint. The full trajectory stays
child-local unless explicitly requested for audit. The parent validates the
envelope before admitting it.

### 2.5 Merge deterministically

Completion timing must not define meaning. Results are admitted by a stable
merge order and expected-version rules.

- Provenance-bound monotone observations may be joined as sets.
- Independent file edits may be admitted when bases and path sets prove
  independence.
- Overlapping edits, stale bases, contradictory decisions, and failed checks
  require coordination, explicit merge, or re-evaluation.

“Let the main model read all child transcripts and decide” is a useful fallback
for ambiguous synthesis, but it is not an inherently correct merge protocol.
It is nondeterministic, costly, and vulnerable to child-context poisoning.

### 2.6 Roll the epoch

When context pressure or expected future cost justifies it:

1. freeze a covered range;
2. deterministically extract decisions, constraints, file/symbol identities,
   failed checks, open questions, and evidence references;
3. ask for a schema-constrained synthesis;
4. validate it and publish one successor epoch with compare-and-swap;
5. keep a bounded verbatim recent tail;
6. retain the complete raw history outside the active projection.

The next turn starts from the successor epoch. It does not append the summary
behind the entire old epoch.

## 3. Recursion must be typed and bounded

“Recursive agent” should mean a small algebra of explicit operations, not an
unbounded model that can spawn arbitrary copies of itself:

```text
Inspect(scope)
Delegate(task, projection, capabilities, budget, merge_slot)
Join(child_results, merge_rule)
Compact(range, policy)
Finish(result)
```

Every operation debits a supervisor-owned budget. Required bounds include:

- maximum depth;
- total descendants;
- concurrent children;
- child wall/token/tool/output budget;
- parent reserve;
- retry/replan count;
- maximum result-envelope size;
- termination condition and cancellation propagation.

A recursive language model can decompose very large inputs, but the harness
must supply the operational semantics. Without typed operations and a ranking
function/budget that decreases, recursion has no liveness argument.

## 4. Information selection, not maximum context, is the objective

A larger trunk is not monotonically better. Long-context models can underuse
relevant information in the middle, and unrelated evidence raises both cost and
prompt-injection surface. The context engine should optimize the relevant,
trust-compatible projection under a token budget:

```text
maximize expected task utility(projection)
subject to context, output-reserve, trust, capability, and cost constraints
```

Useful layers are:

1. stable instructions and schemas;
2. current task/branch state;
3. unresolved obligations and failed checks;
4. symbol/file/evidence retrieval relevant to the next action;
5. bounded recent verbatim turns;
6. structured summaries of older ranges.

Repository-wide facts should be content-addressed and referenced rather than
copied into every child. Retrieval results need source identity and validity
bounds; a stale symbol summary is not a fact merely because it is cacheable.

## 5. Summary quality and drift

Repeated rolling summaries can compound omission. Required defenses are:

- deterministic extraction before synthesis;
- typed fields, not one prose blob;
- source-range and evidence references;
- explicit unresolved/failed items;
- a verbatim recent tail;
- raw-history recovery;
- periodic re-grounding from authoritative artifacts rather than summary of a
  summary only;
- held-out continuation tests across multiple roll generations.

Summary publication is idempotent; summary generation is not. A duplicated
operation may return the already-published successor, but rerunning the model is
not assumed to reproduce the same bytes.

## 6. Provider and worker reuse are independent

A warm worker reduces interpreter/import/startup cost. A provider prefix cache
reduces model-side repeated-prefix work. An immutable context epoch reduces
Cambium copying/reconstruction and defines replay. These mechanisms have
separate keys, lifetimes, failure modes, and measurements.

Keeping a worker alive does not guarantee provider KV-cache retention. Killing
a worker does not necessarily destroy a provider cache. Failing over to a new
provider should preserve semantic checkpoint correctness while accepting a
cold cache.

## 7. Routing consequences

Cache affinity is a switching cost in a constrained non-stationary routing
problem. It is useful only after hard compatibility and task requirements pass.
The selection order is:

1. authorization and provider health;
2. exact model/protocol/tool/reasoning/context compatibility;
3. task quality and policy requirements;
4. rate/concurrency/budget capacity;
5. then quality evidence, expected cost, latency, cache affinity, and stable
   load distribution.

A pinned model cannot silently fall back to another model. A provider cache hit
cannot justify violating priority or capability. Tiny cache samples need
uncertainty, not a permanent sticky preference.

## 8. Terminal-interface consequences

An OpenCode/pi-style surface should connect to one durable session actor rather
than launch one unrelated one-shot task per submitted line. The UI should show:

- current branch/epoch and active provider/model;
- current-turn and cumulative input/output/cache-read/cache-write tokens;
- estimated cost and budget remaining;
- running tools/children and their merge state;
- compaction/fork/resume events;
- a replayable transcript with reconnect cursor.

The UI is a projection of the canonical event stream. It must not own agent
state or block worker progress when rendering is slow.

## 9. What current Cambium proves and does not prove

Current source proves that parent-linked conversation storage, context epochs,
fork/resume metadata, terminal checkpoints, rolling folds, usage events, and
provider affinity exist and have scenario coverage.

It does not yet prove:

- that interactive REPL/TUI prompts share one durable branch;
- that all provider cache identities/capabilities are modeled;
- that folds preserve coding quality over many generations;
- that cache-aware routing beats a non-sticky baseline on a representative
  corpus;
- that arbitrary recursive child admission terminates under every schedule;
- that child result synthesis is conflict-free for overlapping edits;
- that a provider-reported cache field is always present or comparable across
  providers.

## 10. Evaluation plan

Use a frozen coding corpus with multi-turn tasks, independent subtasks,
overlapping-edit conflicts, restarts, provider failover, and at least several
compaction generations. Compare:

- fresh one-shot baseline;
- immutable epoch without provider affinity;
- epoch plus affinity;
- epoch plus scoped child forks;
- epoch plus rolling compaction.

Measure task success, deterministic checks, human/LLM rubric only where
necessary, total/input/output/cache-read/cache-write tokens, cost, latency,
context size, restart recovery, merge conflicts, omitted obligations, and
provider-switch count. Pre-register the acceptance margin. Report distributions
and confidence, not only averages or isolated examples.

Adversarial cases must include malicious child text, stale summaries, altered
tool schemas, expired cache TTL, missing usage fields, duplicate terminal
results, stale generations, and reordered child completion.

## 11. Computer-science interpretation

- **Persistent data structures / Merkle DAG:** cheap immutable forks with
  shared ancestors.
- **Event sourcing / CQRS:** history is the write model; epochs and UI are read
  projections.
- **MVCC / optimistic concurrency:** children read snapshots and publish only
  against expected versions.
- **CALM theorem:** coordination-free merge only for monotone information.
- **Structured concurrency:** child lifetime, cancellation, and failure stay
  nested under the parent.
- **Recursive language models:** decomposition is useful when recursion has
  explicit operators and budgets.
- **Hierarchical retrieval:** multi-resolution summaries and direct evidence
  references reduce irrelevant context.
- **Lost-in-the-middle results:** context selection and placement matter even
  when a model accepts a large window.

## 12. References

- Recursive Language Models: <https://arxiv.org/abs/2512.24601>
- RAPTOR: <https://arxiv.org/abs/2401.18059>
- Lost in the Middle: <https://arxiv.org/abs/2307.03172>
- CALM theorem: <https://arxiv.org/abs/1901.01930>
- Git object model: <https://git-scm.com/book/en/v2/Git-Internals-Git-Objects>
