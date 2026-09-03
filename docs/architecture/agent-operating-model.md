# Agent operating model

**Status:** target architecture. This document defines the system Cambium is
converging toward. It distinguishes landed behavior from planned behavior; it
is not evidence that every interface below already exists.

## 1. Purpose

Cambium is not primarily an agent launcher. It is a cognitive control system
for a language model operating on a real repository under partial information,
finite time, finite context, provider constraints, and irreversible effects.

The system succeeds when the accepted repository state is correct, the model
understood enough of the situation to choose good actions, and expensive work
remains reusable. It fails when the model acts on stale or ambiguous state,
repeats discoveries that were already paid for, loses obligations during
compaction, confuses a semantic claim with an accepted artifact, or spends more
resources coordinating than the work is worth.

The design objective is therefore:

```text
maximize
    expected accepted progress
  + expected information gain
  + reusable knowledge created

subject to hard correctness and authority constraints

minus
    wall time
  + uncached context
  + generated tokens
  + cash and quota scarcity
  + context churn
  + spawn and join overhead
  + verification cost
  + risk of an incorrect or stale action
```

Hard constraints are never converted into scores. Provider feasibility,
worktree ownership, generation fencing, result correlation, checkpoint
identity, and publication invariants are checked before optimization begins.

## 2. Design laws

### 2.1 One canonical branch state, many projections

Cambium MUST have one derived state for each active branch. The model prompt,
operator TUI, monitor, session inspection, child admission, and recovery logic
MUST project that state rather than assembling independent stories about the
same task.

```text
                         durable sources
                events + checkpoints + Git + quota
                               |
                               v
                       canonical BranchState
                     /          |           \
                    /           |            \
          SituationFrame    operator view   control decisions
             for LM           for human      for supervisor
```

A projection may be discarded and rebuilt. It is never a second source of
truth.

### 2.2 The branch is the unit of agency

The root and every child use the same conceptual record:

```text
branch
├── task contract and authority
├── accepted semantic state
├── accepted artifact state
├── current observations and raw tail
├── provider/model lease
├── resource envelope
├── plan and open obligations
├── child branches
├── verification state
└── bounded result capsule
```

There are no privileged “sub-main,” “research,” or “review” runtimes. Task,
authority, tools, context policy, resources, and done criteria specialize a
branch without creating another orchestration implementation.

### 2.3 State must be explicit at the decision boundary

Before asking a model for another action, Cambium should state the current
mission, accepted state, recent delta, open obligations, blockers, child state,
resources, and exact drill-down anchors. The model should not reconstruct these
facts from hundreds of transcript tokens or infer them from UI-only events.

### 2.4 Expensive knowledge must accrete; noise must not

Tool observations, test results, failed approaches, decisions, and unresolved
questions must survive when they are expensive or decision-relevant. Routine
command noise, duplicate reads, malformed actions, and superseded scratch plans
must not grow the active context forever.

Accretion is append-only and evidence-linked. Corrections supersede or
invalidate prior items; they do not rewrite history.

### 2.5 Progressive disclosure beats automatic replay

The common path should contain a small operating picture. More detail is loaded
only when a decision requires it:

```text
0  SituationFrame: current mission and control state
1  branch/result capsule: bounded outcome and evidence anchors
2  exact tool, check, file, event, or commit reference
3  bounded transcript or source window
4  raw durable artifact for forensic recovery
```

The model should pay for level 3 or 4 only after levels 0–2 prove insufficient.

### 2.6 Effects and claims are different

A model may propose a claim or decision. Only tools observe the environment;
only the supervisor accepts process, checkpoint, child, and publication state;
only Git identifies accepted repository artifacts. The prompt must make these
authority boundaries visible.

### 2.7 Resource control belongs in the loop

The agent cannot minimize resources it cannot see. Cambium should expose a
small, harness-computed `ResourceEnvelope`: remaining turns and wall time,
context pressure, provider lease, cache-affinity state, quota pressure, and the
relative cost of delegation or provider switching. Credentials and internal
scheduler machinery remain hidden.

### 2.8 Human and model control surfaces must agree

The operator rail and the model's SituationFrame are two renderers over the
same `BranchState`. A human should be able to see what the model was told, and
the model should not be missing a critical fact already visible in the TUI.

## 3. Tower of linked abstractions

Each layer owns one kind of truth and exposes a bounded interface upward.
Higher layers may not bypass lower-layer ownership.

```text
L7  Evaluation and policy improvement
    paired outcomes, prompt/routing experiments, promotion gates
                              ^
L6  Human and agent control surfaces
    TUI, monitor, SituationFrame, inspect_state, steering
                              ^
L5  Branch controller
    orient -> decide -> act -> observe -> verify -> externalize -> finish
                              ^
L4  Canonical BranchState
    mission, authority, accepted state, plan, obligations, children, resources
                              ^
L3  Materialized semantic and artifact views
    CAST/K0, result capsules, worktree/commit heads, provider leases
                              ^
L2  Durable record
    events, immutable checkpoints, manifests, usage ledger, Git objects
                              ^
L1  Effect boundaries
    tools, worker processes, provider transports, merge sequencer
                              ^
L0  Reality
    repository, filesystem, tests, provider accounts, clocks, operator intent
```

The tower is deliberately asymmetric:

- observations flow upward;
- validated commands flow downward;
- no upper layer mutates a lower layer's state directly;
- every lossy projection retains references to the durable source that can
  reconstruct or challenge it.

## 4. Four closed loops

Cambium is one system containing four nested feedback loops.

### 4.1 Work loop

```text
orient -> choose action -> execute -> observe -> update state
```

This is the normal model/tool loop. The model chooses intent; the harness
validates and executes it.

### 4.2 Context loop

```text
raw observations -> semantic delta -> append-only trunk -> K0 rollover
                         ^                         |
                         +---- precise recall -----+
```

CAST keeps the active working set bounded while raw history remains available
through stable references.

### 4.3 Orchestration loop

```text
admit -> lease -> run -> suspend/fork -> join -> verify -> publish/recover
```

The supervisor owns branch lifetime, resource reservations, generations,
children, artifact integration, cancellation, and publication.

### 4.4 Learning loop

```text
record outcome -> compare policies -> optimize -> canary -> promote or reject
```

The outer loop improves prompts and routing only from held-out evidence. It
never weakens hard runtime validation.

## 5. Canonical BranchState

`BranchState` is a pure materialized view. It is reconstructed from durable
events, immutable checkpoints, accepted Git state, and provider/quota records.
It does not require a new database.

At minimum it contains:

```text
identity
  session_id, task_id, parent_task_id, generation, lifecycle

mission
  objective, constraints, done_when, verification contract

authority
  repository, worktree, branch, writable scope, tools, provider allowlist

accepted context
  epoch, checkpoint, lineage, semantic head, raw-tail shape

accepted artifacts
  base head, worktree head, accepted integration head, dirty/clean state

control state
  active plan, current step, open obligations, blockers, last meaningful delta

knowledge state
  observations, claims, decisions, constraints, verification, invalidations

children
  admission order, policy, owner scope, lifecycle, result, artifact status

resources
  turns, wall, token/context pressure, provider lease, cache state, quota/cost pressure

anchors
  stable refs for exact history, source, checks, events, branches, and commits
```

Fields have explicit owners. For example, Git owns commit identity, the
supervisor owns accepted integration state, the provider owns cache-hit
evidence, and the model may only propose semantic claims. A reducer must not
turn a model sentence into an accepted environmental fact.

## 6. SituationFrame

The `SituationFrame` is the small model-facing projection of `BranchState`. It
is generated by Cambium, placed at the end of the normal request, and bounded
independently from the semantic trunk.

```text
<cambium-situation version="1">
MISSION
  objective: ...
  done_when: ...

AUTHORITY
  worktree: ...
  writable_scope: ...
  available_effects: ...

ACCEPTED
  context_epoch: ...
  artifact_head: ...
  provider_lease: ...
  verification: ...

DELTA
  what changed since the previous decision: ...

OPEN
  obligations: ...
  blockers: ...
  uncertainties: ...

CHILDREN
  critical-path and terminal child summaries: ...

RESOURCES
  turns/wall/context/quota pressure: ...

ANCHORS
  exact refs worth inspecting next: ...
</cambium-situation>
```

Properties:

1. **Deterministic.** The same event prefix and repository state produce the
   same frame.
2. **Harness-authored.** It labels model proposals, accepted facts, unknowns,
   and stale data distinctly.
3. **Bounded.** Each section and the whole frame have byte/item caps and a
   visible truncation marker plus drill-down reference.
4. **Recent.** It is regenerated immediately before dispatch, after all
   completed tool and child events have been reduced.
5. **Cache-aligned.** It is a short changing suffix. It never rewrites the
   stable system/tool head or prior CAST entries.
6. **Auditable.** A projection version, source watermark, and frame digest are
   recorded so an action can be replayed against what the model saw.
7. **Non-duplicative.** It contains current control state, not a second narrative
   summary of the whole session.

The SituationFrame solves a different problem from CAST. CAST answers “what
semantic history should remain active?” The frame answers “what is true and
controllable at this exact decision boundary?”

## 7. Agent control protocol

The intended model behavior is a small closed-loop protocol:

```text
ORIENT
  read mission, accepted state, open obligations, resource pressure

LOCATE
  find the smallest source/evidence region that can answer the next uncertainty

ACT
  batch independent reads; serialize effects; delegate only separable work

OBSERVE
  distinguish tool evidence from interpretation and update the plan

VERIFY
  test the affected layer, then the combined accepted artifact when needed

EXTERNALIZE
  preserve decisions, expensive findings, failed approaches, and open work

FINISH
  report objective_met only when done criteria and required verification agree
```

The agent should not be asked to narrate private reasoning. It should emit a
short plan, typed actions, and externally useful state changes. A plan is a
control hypothesis, not an immutable promise; it changes when evidence changes.

## 8. Accretive epistemic model

Cambium's durable semantic state should distinguish five item types:

```text
Observation   direct result of a tool, provider, event, or artifact read
Claim         model interpretation linked to observations
Decision      selected course of action and its supporting refs
Obligation    unfinished work with owner and completion condition
Verification  check performed against a particular artifact/context state
```

Every item has a stable identity, source branch, creation watermark, status,
and evidence references. Status transitions are append-only:

```text
claim:        proposed -> accepted | invalidated
decision:     active   -> superseded
obligation:   open     -> satisfied | cancelled | blocked
verification: passed   -> failed | stale
```

A verification becomes stale when the accepted artifact head changes outside
its tested scope. A claim without evidence remains explicitly inferred or
hypothetical. Numeric confidence is optional and never substitutes for source
references.

Stable evidence refs include all coordinates needed to disambiguate a recorded
boundary. The canonical historical tool form is:

```text
tool:<percent-encoded-task-id>:<generation>:<turn>:<batch-index>
```

The zero-based batch index distinguishes multiple calls in one model action.
Legacy history refs without that final coordinate resolve to index zero only.

The current `SummaryEntry` already carries decisions, facts, failed approaches,
verification results, and open items. The first implementation should assign
stable harness identities and derive a `WorkLedger` from those fields rather
than introducing a new store or immediately replacing the summary wire schema.
A later typed schema is justified only if evaluation shows that the compatible
projection is insufficient.

K0 rollover materializes the current accepted semantic state; it must preserve
open obligations, active decisions, verified facts, constraining failed
approaches, and evidence anchors. It must not preserve every historical item in
the active prompt; superseded and invalidated items remain in durable history.

## 9. Tool surface

The model-facing tool set should remain small and orthogonal:

```text
inspect_state   current BranchState sections and exact anchors
branch_history  historical branches, tool calls, and bounded transcript windows
repo_query      tree/symbol/definition/reference/search/window/diagnostic navigation
read_batch      exact bounded file reads once locations are known
write_file      complete-file creation/replacement
edit_file       one exact local replacement
git_op          inspection and narrowly allowlisted Git operations
run_shell       tests and commands that lack a typed tool
delegate        create one scoped child proposal
```

`inspect_state` answers “what is the current accepted situation?”
`branch_history` answers “what happened before or in another branch?”
`repo_query` answers “where in the repository is the relevant code?” These are
separate questions and should not be hidden behind one universal query tool.

Typed navigation should be preferred to shell `find`/`grep` because it produces
smaller, structured locations. `run_shell` remains the escape hatch, not the
first navigation primitive. Independent read-only calls may be batched;
mutating calls execute in declared order.

Tool results include stable evidence references. Large output is retained in a
bounded spill/artifact with a reference rather than silently discarded or
replayed in full.

## 10. Delegation and result capsules

Delegation is a resource decision, not a reflex. A child should be created only
when expected critical-path or information benefit exceeds:

```text
context construction + spawn + provider queue + join + verification + conflict risk
```

The parent declares:

```text
objective
ownership boundary
completion criteria
verification contract
context_mode
placement
budget
```

The supervisor resolves hard feasibility and exposes the resolved policy. The
agent expresses `inherit` or `spread` intent; it does not select credentials by
prose.

A child returns a versioned bounded `ResultCapsule`:

```text
status and concise outcome
claims with evidence refs
decisions made or recommended
artifacts changed and accepted head
verification performed and artifact head tested
open obligations and blockers
resource usage
recommended parent action
```

Semantic acceptance and artifact acceptance are separate. A parent resumes
only after child completion is represented, required artifact integration is
accepted, and the parent worktree matches the accepted integration head.
Completion order never determines join order.

## 11. ResourceEnvelope

The model should receive decision-relevant resource facts, not a dump of raw
provider telemetry:

```text
remaining_turns
remaining_wall_s
context_pressure: low | medium | high | critical
uncached_token_pressure: low | medium | high | unknown
provider_lease: provider/model identity or unknown
cache_affinity: exact | semantic | fresh | unknown
cache_warmth: warm_estimate | cold | unknown
quota_pressure: low | medium | high | blocked | unknown
cash_pressure: low | medium | high | unknown
delegation_overhead: low | medium | high | unknown
alternative_lane_available: true | false | unknown
```

Exact numeric values remain available through inspection when useful. The
summary classes keep the common frame stable and prevent the model from
micro-optimizing noisy estimates.

The supervisor owns provider selection. It first builds a hard-feasible set,
then ranks it using measured quality, throughput, cash cost, quota scarcity,
verification cost, and cache switching cost. Unknown evidence is labelled
unknown, not replaced by a persuasive placeholder.

Root continuation keeps a provider/model lease until explicitly infeasible.
Independent children may spread. Migration of a root lease is an observable
state transition tied to a safe checkpoint, never a silent retry detail.

## 12. Shared human/model control plane

`BranchState` should support two projections:

```text
model: SituationFrame + inspect_state
human: TUI/monitor/status commands
```

Both display the same mission, accepted artifact head, context lineage,
children, blockers, verification state, and resources. The human view can be
wider; the semantic meaning cannot differ.

Operator steering is an event, not an out-of-band prompt mutation. A steer can:

- append a new constraint or priority;
- cancel an active branch;
- queue a follow-up;
- request inspection of a branch or evidence reference;
- change a provider/model preference within authorization;
- start a new or forked branch.

The next SituationFrame exposes the accepted steering delta. Frontends remain
renderers and command sources; they do not mutate worker state directly.

## 13. Failure and recovery

A useful recovery path restores both artifact state and cognitive state.

```text
failure
  -> classify boundary
  -> preserve salvage and last safe checkpoint
  -> mark affected claims/verifications stale when necessary
  -> rebuild BranchState
  -> emit a fresh SituationFrame with the failure delta
  -> retry, migrate, delegate, or stop within budget
```

Examples:

- a worker restart may reuse a checkpoint only when workspace identity matches;
- a provider migration starts from a safe semantic/checkpoint state and records
  loss of cache affinity;
- a child merge conflict creates bounded conflict evidence and an ordered
  resolver path;
- a failed verification remains an open obligation and cannot be compacted into
  a generic “tests run” statement;
- a truncated observation retains a stable reference to the full bounded
  artifact when available.

## 14. Current implementation truth

At the time this design was written, current `main` provides:

- durable events, immutable checkpoints, isolated/fenced workers, salvage, and
  ref-only Git publication;
- append-only CAST summaries and K0 rollover;
- provider leases, usage debt, quota/cache capability values, and routing;
- persistent interactive sessions and an event-sourced operator projection;
- explicit child context/placement behavior when those fields are declared;
- automatic exact/semantic compatibility behavior when a harness-originated
  child omits them (the model contract requires both fields);
- implementations of branch-history projection, bounded code indexing, and
  optional LSP queries.

The following target layers are not yet a coherent runtime surface:

- a canonical `BranchState` shared by supervisor, model, and TUI;
- an automatically injected SituationFrame;
- `inspect_state` as a model tool;
- branch-history, code-index, and LSP access in the active worker tool roster;
- evidence-linked epistemic item identities and a versioned ResultCapsule;
- a model-visible ResourceEnvelope and critical-path child view;
- named branch-decision/history-recall prompt components matching current docs;
- strict removal or explicit naming of the omitted-policy compatibility path.

These gaps are ordered in the repository `implementation-plan.md`.

## 15. Non-goals

This architecture does not require:

- hidden-chain-of-thought storage or retrieval;
- a vector database or second memory service;
- a global mutable singleton agent state;
- provider-native agent orchestration;
- automatic replay of all history before every call;
- one monolithic “do anything” tool;
- role-specific worker runtimes;
- trusting a model summary as evidence of a Git or test result;
- optimizing undocumented placeholder economics.

## 16. Acceptance properties

The target system is coherent only when all of these hold:

```text
[ ] one event prefix produces one deterministic BranchState
[ ] model and operator projections agree on all shared fields
[ ] every action can be tied to the SituationFrame watermark/digest it saw
[ ] current obligations survive summary flush and K0 rollover
[ ] exact evidence can be reopened without replaying an entire transcript
[ ] batched tool calls have independently addressable evidence refs
[ ] repository navigation usually precedes broad file reads or shell search
[ ] a child capsule cannot imply artifact acceptance
[ ] a changed artifact head stales affected verification explicitly
[ ] resource pressure and cache affinity are visible without exposing secrets
[ ] delegation improves held-out critical-path or information efficiency
[ ] no new memory database is needed to reconstruct the accepted state
[ ] source, schemas, prompts, tests, reference docs, and UI use the same terms
```

## 17. Related documents

- [`architecture.md`](architecture.md) — runtime map and subsystem ownership
- [`context-engine.md`](context-engine.md) — CAST, checkpoints, and cache lineage
- [`context-branches.md`](context-branches.md) — recursive branch rationale
- [`subagents.md`](subagents.md) — current delegation and join mechanics
- [`provider-routing.md`](provider-routing.md) — admission and provider execution
- [`terminal-interface.md`](terminal-interface.md) — operator projection
- [`../reference/agent-state.md`](../reference/agent-state.md) — target state schemas
- [`../how-to/agent-driving-loop.md`](../how-to/agent-driving-loop.md) — practical agent loop
- [`../research/agent-system-evaluation.md`](../research/agent-system-evaluation.md) — evaluation protocol
- [`../../implementation-plan.md`](../../implementation-plan.md) — ordered open work
