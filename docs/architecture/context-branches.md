# Recursive cache-aligned context branches

**Status:** architectural vision and implemented vertical-slice contract.

Cambium's central problem is not “how to create more agents.” It is how to
supply a stateless language model with enough context to act correctly while
keeping that context concise, cacheable, inspectable, and cheap to replay.

The design answer is a recursive branch:

```text
branch = task + trunk + raw tail + provider lease + children + result + artifact head
```

The root branch and every child branch use the same structure. A child is not a
specialized runtime class. It is another bounded Cambium branch that may work,
inspect its history, delegate again, and return a small result to its parent.

## 1. The two hard constraints

Cambium optimizes under two restrictions that pull in opposite directions.

```text
             enough context for correctness
                         ^
                         |
                         |
 concise working set <---+---> stable prefix for provider caching
                         |
                         |
                omitted detail must remain recoverable
```

1. **Context restriction.** Too much history dilutes attention and consumes the
   context window; too little removes facts needed for the next decision.
2. **Cache restriction.** Rewriting an old prefix may invalidate a provider's
   prompt/KV cache. A stable growing trunk gives higher throughput and lower
   effective cost.

The system therefore keeps a compact semantic trunk in every normal request and
stores raw branch trajectories outside that request. An LM can reopen the raw
trajectory only when the current decision requires it.

## 2. Big vision

```text
                              ROOT BRANCH
                  cached semantic trunk + recent tail
                                  |
              +-------------------+-------------------+
              |                                       |
       trunk / inherit child                  semantic / spread child
       same provider, full trunk              another feasible provider
       maximum cache reuse                    subscription throughput
              |                                       |
       recursive children                     recursive children
              |                                       |
              +-------------------+-------------------+
                                  |
                       bounded child capsules
                                  |
                         appended to parent trunk
                                  |
                   branch_history: optional drill-down
                                  |
                 old tool calls / transcript on demand
```

The parent normally sees only each child's small result capsule. It does not pay
to replay every child transcript on every later turn. If a result looks wrong or
lacks enough detail, the parent can list that branch's tool calls, reopen one
specific call, or page through the branch transcript.

This is not a second evidence database. The readable history is a projection of
the event log and immutable checkpoints Cambium already produces.

## 3. Four structures that must not be conflated

“Tree,” “branch,” and “trunk” are overloaded words. Cambium uses four different
structures for four different responsibilities.

```text
                           CAMBIUM SESSION
                                 |
          +----------------------+----------------------+
          |                      |                      |
          v                      v                      v
      TASK TREE          CONVERSATION BRANCHES      GIT GRAPH
   who owns which work    what each LM observed     what code changed
          |
          v
     CACHE LINEAGE
 which request prefixes
 may be provider-cache compatible
```

### 3.1 Task tree

The task tree answers **who does what**.

```text
root: improve harness
├── routing audit
│   ├── quota behavior
│   └── provider spread
├── context history tools
└── documentation
```

It is a bounded parent/child decomposition with deterministic admission,
depth/fan-out limits, structured completion, and fork-join lifetime. It exists
because parallel work needs explicit ownership and completion boundaries.

### 3.2 Conversation branch tree

The conversation tree answers **what an LM branch saw and did**.

```text
root transcript
├── child A transcript
│   └── grandchild A1 transcript
└── child B transcript
```

Each tool call belongs to exactly one task branch, worker generation, and turn.
Its stable reference is:

```text
tool:<task-id>:<generation>:<turn>
```

The branch tree is the natural place to reopen a historical tool call or a
whole branch transcript. It is not injected wholesale into other branches.

### 3.3 Git artifact graph

The Git graph answers **which filesystem state was accepted**.

```text
base commit
├── child A commit
├── child B commit
└── serialized integration onto main
```

A semantic result and a Git result are different. The statement “the child
fixed the parser” is not proof that the parent worktree contains the fix. The
join barrier must validate both the result capsule and the accepted artifact
head.

### 3.4 Provider-cache lineage

Cache lineage answers **which requests share an exact provider prefix**.

```text
provider + model + protocol + reasoning mode
+ system prompt + tool schemas + message bytes
```

It is not the task tree. Two sibling tasks may share a task ancestor but use
different providers and have no shared provider cache. Conversely, several
exact children can reuse one byte-identical parent prefix.

## 4. The trunk is normal history

The trunk grows. That is intentional.

```text
H + S1 + S2 + ... + Sn + small raw tail
```

- `H` is the stable system/tool head.
- `Si` is an immutable semantic delta covering one disjoint raw range.
- the tail contains the current unsummarized work.

“Do not put historical detail into the stable system prompt” does **not** mean
“do not retain history.” It means:

```text
correct:
    stable instructions | growing semantic trunk | recent work | requested old detail

wrong:
    rewrite system instructions every time old detail is inspected
```

The semantic trunk remains the compact cached history. A historical transcript
or tool result is returned as a normal temporary tool observation at the end of
the request. It therefore does not rewrite the old prefix. If that inspection
changes a durable conclusion, the normal summarizer records the new fact or
invalidates the old one in a later trunk segment.

## 5. Recursive branch entity

Every branch has the same conceptual record:

```text
ContextBranch
├── task_id
├── parent_task_id
├── task contract
├── context_mode
├── placement
├── immutable checkpoint / trunk head
├── current raw tail
├── provider/model lease
├── child branches
├── bounded result capsule
├── tool-call references
└── Git artifact head
```

This uniformity is important. It avoids a hierarchy of root-agent,
research-agent, review-agent, and sub-main-agent classes. Different behavior
comes from task text, available tools, budgets, and explicit branch policy—not
from separate orchestration implementations.

## 6. Child context modes

Every delegated child declares one context mode. There is no implicit alias or
fallback.

| Mode | Child starts with | Typical use |
| --- | --- | --- |
| `trunk` | Complete exact parent checkpoint | Default; rich context and provider-cache reuse |
| `semantic` | Immutable semantic summaries under a fresh provider head | Separable work on another provider |
| `fresh` | Task contract only | Blind review, clean reproduction, contamination control |

### 6.1 `trunk`

```text
parent request: [H][S1][S2][tail]
child request:  [H][S1][S2][tail][child task]
                ^^^^^^^^^^^^^^^^^^
                exact old prefix
```

This is the normal choice. In Cambium's observed workload, the trunk is small,
information-dense, already paid for, and often served from the provider cache.
Constructing a separate compressed prompt can cost more tokens and lose more
information than simply reusing the full trunk.

`trunk` requires `placement=inherit`; an exact prefix cannot simultaneously be
a different provider's prefix.

### 6.2 `semantic`

```text
provider A parent: [system A][S1][S2][tail]
provider B child:  [system B][S1][S2][child task]
                              semantic continuity
```

The semantic entries are reusable, but provider B starts with a cold,
provider-specific head. This is useful when the subproblem is sufficiently
independent and the primary goal is more aggregate throughput across
subscriptions.

### 6.3 `fresh`

```text
provider B child: [system B][child task]
```

Fresh context is deliberately information-poor. It is useful when independence
is the point: reproduce a bug without inherited assumptions, perform a blind
review, or test whether a conclusion is robust.

## 7. Provider placement

Context representation and provider placement are separate decisions.

| Placement | Meaning |
| --- | --- |
| `inherit` | Preserve the parent provider/model affinity where feasible |
| `spread` | Remove the inherited pin and prefer another feasible provider lane |

`spread` is a preference inside the hard-feasible set, not permission to use an
incapable provider. Credentials, model/context capacity, budget, quota, and
health remain hard constraints.

Why provider spreading is essential:

```text
single provider:
    subscription A: saturated
    subscription B: idle
    total throughput ~= A

spread children:
    root on A + independent child on B + another child on C
    total throughput ~= A + B + C - orchestration overhead
```

The scheduler may still select the parent's provider if no alternative is
feasible. Correct completion is more important than artificial distribution.

## 8. How the LM decides

The decision is explicit and prompt-guided; the harness validates it.

```text
Is the next work tightly coupled to the current raw tail?
├── yes -> continue current branch
└── no
    |
    Is the task large enough to repay spawn/join overhead?
    ├── no -> continue current branch
    └── yes
        |
        Does the child benefit from full parent state?
        ├── yes -> trunk + inherit
        └── no
            |
            Is inherited semantic state useful?
            ├── yes -> semantic + spread
            └── no  -> fresh + spread
```

Default policy:

```text
continue local work when small or tightly coupled
otherwise prefer trunk + inherit
use semantic + spread for truly separable work
use fresh + spread for independent verification
```

The provider is not selected by prose. The LM chooses the architectural intent;
the supervisor checks the requested mode and the router selects a feasible
provider/model.

## 9. Historical context interface

The target LM surface is one small read-only tool, `branch_history`, with four
actions. `branch_history.py` implements the read-only projection and these
actions, but the tool is not yet wired into the active worker tool roster
(`subagents.md` §10); do not describe the model as able to call it today.

```text
branches    list task branches and their context/placement/provider state
tools       list historical tool calls, optionally for one branch
tool        reopen one stable tool reference
transcript  page through one branch checkpoint transcript
```

Typical drill-down:

```text
1. branch_history(action="branches")
2. branch_history(action="tools", task_id="routing-audit")
3. branch_history(action="tool", ref="tool:routing-audit:1:7")
```

Only when one call is insufficient:

```text
4. branch_history(action="transcript", task_id="routing-audit", offset=20, limit=10)
```

This keeps the common path cheap and allows precise expansion when something
looks wrong.

## 10. Tool-call identity

A historical tool call is not a loose text blob. Its identity is derived from
the execution branch:

```text
ToolCallRef
├── task branch id
├── worker generation
└── LM turn
```

The event log records the tool, success, duration, and bounded arguments. The
corresponding checkpoint contains the assistant tool action and the tool
observation. `branch_history` joins those existing records at read time.

```text
tool event ------------------+
 task / generation / turn    |
                             +--> branch_history(tool ref)
turn checkpoint -------------+
 assistant action + result
```

There is no separate evidence index to synchronize and no second writer.

## 11. Why these concepts are necessary

| Concept | Why necessary | Why better than the obvious alternative |
| --- | --- | --- |
| Append-only trunk | Retain compact branch state without rewriting prior bytes | Recursive summary rewriting invalidates caches and compounds loss |
| Exact trunk child | Reuse rich parent state cheaply | Recompressing a small cached trunk spends tokens and loses detail |
| Semantic child | Move independent work to another provider | Full exact context cannot cross provider cache namespaces |
| Fresh child | Obtain an independent sample/reproduction | Shared conclusions can correlate errors |
| Recursive task tree | Bound ownership and parallelism | Unstructured agent spawning causes contention and unclear completion |
| Branch history tool | Recover omitted detail only when needed | Always replaying all transcripts overwhelms context |
| Stable tool refs | Reopen one exact historical action | Search by vague prose is ambiguous and expensive |
| Event/checkpoint projection | One existing authority | A new evidence database creates synchronization and migration work |
| Separate Git graph | Prove accepted code state | A semantic summary cannot prove filesystem publication |
| Separate cache lineage | Make cache claims precise | Task ancestry alone says nothing about provider prefix compatibility |
| Named prompt components | Optimize policy independently | One monolithic prompt hides which instruction improves behavior |

## 12. Computer-science grounding

The architecture reuses established ideas rather than inventing a new
coordination theory.

- **Persistent data structures / MVCC:** children share an immutable prefix and
  append private continuation state.
- **Event sourcing:** raw events and checkpoints remain the replay authority;
  the trunk is a materialized view.
- **Structured concurrency / fork-join:** a parent owns the lifetime and result
  join of its children.
- **Actor model:** each worker has private execution state and communicates via
  bounded messages.
- **Content addressing:** checkpoint hashes identify exact context state.
- **Affinity scheduling:** exact children stay on the provider that can reuse
  their prefix.
- **Load balancing with switching cost:** semantic/fresh children spread only
  when the throughput benefit exceeds context and coordination cost.
- **Amdahl's law:** delegation accelerates only the independent fraction after
  spawn, context, verification, and merge overhead.
- **CALM theorem:** monotone observations can merge without coordination;
  conflicting decisions and code edits require an ordered join.
- **Working-set / virtual-memory analogy:** the trunk is the hot working set;
  old branch transcripts are paged in on demand.

The linked *AI Agents in Depth* repository is useful background because it
frames an agent as LM + context + tools and distinguishes shared-context from
message-passing multi-agent systems. Cambium combines both modes explicitly:
exact trunk children share a cached prefix; semantic/fresh children communicate
through bounded results and persistent artifacts.

## 13. What Cambium does not build

This design intentionally excludes:

- a vector memory service;
- an evidence graph database;
- per-branch access-control capabilities;
- hidden-reasoning retrieval;
- role-specific agent subclasses;
- automatic retrieval inserted before every call;
- summary-of-summary rewriting that destroys source history.

All branches in the current Cambium session are visible to `branch_history`.
The tool still has deterministic row/byte limits so one read cannot consume the
entire context window.

## 14. Implementation map

```text
src/cambium/child_policy.py
    explicit context_mode + placement validation

src/cambium/branch_history.py
    read-only event/checkpoint projection and stable refs

src/cambium/schemas.py
    delegate model-facing contract

src/cambium/tools.py
    executable worker tool dispatch and policy validation

src/cambium/prompts.py
    separately named branch-decision and history-recall policies

src/cambium/supervisor.py
    admission, exact/semantic/fresh materialization, provider affinity
```

Normative requirements are separate in
[`context-branch-requirements.md`](context-branch-requirements.md). Exact tool
schemas and examples are in
[`../reference/context-branches.md`](../reference/context-branches.md). A
practical decomposition guide is in
[`../how-to/context-branches.md`](../how-to/context-branches.md). Evaluation is
specified in
[`../research/agent-system-evaluation.md`](../research/agent-system-evaluation.md).
