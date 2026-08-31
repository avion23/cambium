# Agent operating model

**Status:** target architecture. This document defines the system Cambium is
converging toward. Current source, executable tests, durable records, and
accepted Git state remain the authority for landed behavior.

## 1. Purpose

Cambium is a cognitive control system for a language model operating on a real
repository under partial information, finite context, finite time, provider
constraints, and irreversible effects.

The objective is:

```text
maximize
    accepted progress
  + useful information gained
  + reusable knowledge accumulated

subject to hard correctness and authority constraints

while minimizing
    wall time
  + uncached context and generated tokens
  + cash and quota use
  + context churn
  + delegation, join, and verification work
  + stale or incorrect actions
```

Hard constraints are never scores. Provider feasibility, worktree ownership,
generation fencing, result correlation, checkpoint identity, and Git
publication are validated before optimization.

## 2. Design laws

### 2.1 One canonical branch state, many projections

Each active branch has one derived `BranchState` rebuilt from durable sources.
The model prompt, TUI, monitor, inspection commands, child admission, and
recovery project it rather than reconstructing independent versions of reality.

```text
events + checkpoints + Git + quota
                |
                v
          BranchState
          /    |    \
         /     |     \
SituationFrame TUI   supervisor decisions
```

A projection may be discarded and rebuilt. It is never another source of truth.

### 2.2 The branch is the unit of agency

The root and every child use the same conceptual record:

```text
branch
├── task contract and authority
├── accepted semantic and artifact state
├── current observations and raw tail
├── provider/model lease and resources
├── plan, obligations, and blockers
├── child branches
├── verification state
└── bounded result capsule
```

Task, authority, tools, context policy, resources, and done criteria specialize
a branch. Do not create root, research, review, or sub-main runtime classes.

### 2.3 State is explicit at the decision boundary

Before every model decision, Cambium should expose the current mission,
authority, accepted state, recent delta, obligations, blockers, children,
resources, and exact evidence anchors. The model should not recover these facts
from transcript archaeology or UI-only state.

### 2.4 Expensive knowledge accretes; noise does not

Decision-relevant observations, test results, failed approaches, decisions, and
open work survive compaction. Routine command noise, duplicate reads, malformed
actions, and superseded scratch plans do not.

Accretion is append-only and evidence-linked. Corrections invalidate or
supersede prior items; they never rewrite durable history.

### 2.5 Progressive disclosure beats replay

```text
0  SituationFrame: current mission and control state
1  branch or result capsule
2  exact tool, check, file, event, or commit reference
3  bounded transcript or source window
4  raw durable artifact for recovery
```

The common path pays only for levels 0–2. Deeper history is loaded when a
specific uncertainty requires it.

### 2.6 Effects and claims are different

A model proposes intent, claims, and decisions. Tools observe or mutate the
environment. The supervisor accepts process, checkpoint, child, and publication
state. Git identifies accepted artifacts. No model sentence becomes an accepted
environmental fact by being summarized.

### 2.7 Resource control belongs in the loop

The model receives a bounded harness-computed `ResourceEnvelope`: remaining
turns and wall time, context pressure, provider lease, cache affinity, quota and
cash pressure, alternative lanes, and relative delegation or migration cost.
Credentials and scheduler internals remain hidden.

### 2.8 Human and model views agree

The operator rail and SituationFrame render the same BranchState fields. The
human view may be wider; the meaning of shared fields cannot differ.

## 3. Tower of ownership

```text
L7  evaluation and policy promotion
                              ^
L6  SituationFrame, inspection, TUI, monitor
                              ^
L5  branch controller and decision loop
                              ^
L4  canonical BranchState
                              ^
L3  CAST, result, artifact, and provider views
                              ^
L2  events, checkpoints, manifests, quota, Git objects
                              ^
L1  tools, workers, transports, merge sequencer
                              ^
L0  repository, providers, clocks, operator intent
```

Observations flow upward. Validated commands flow downward. A higher layer never
mutates a lower layer outside its explicit interface. Every lossy projection
keeps stable references to the durable evidence that can reconstruct or
challenge it.

## 4. Control loops

Cambium contains four linked loops:

```text
work          orient -> act -> observe -> update
context       raw tail -> semantic delta -> trunk -> K0
orchestration admit -> lease -> run -> join -> publish/recover
learning      record -> compare -> canary -> promote/reject
```

The model chooses intent in the work loop. The harness owns validation and
effects. The learning loop may improve prompts and routing only from held-out
outcomes; it never weakens runtime invariants.

## 5. BranchState

`BranchState` is a pure materialized view over durable events, immutable
checkpoints, validated Git state, and provider/quota records. It does not require
a new database.

It contains:

```text
identity       session, task, parent, generation, lifecycle
mission        objective, constraints, done criteria, verification contract
authority      repo, worktree, branch, write scope, tools, provider allowlist
context        epoch, checkpoint, lineage, semantic head, raw-tail shape
artifacts      base, worktree, accepted integration heads, dirty state
control        plan, current step, obligations, blockers, last delta
knowledge      observations, claims, decisions, invalidations, verification
children       admission order, policy, scope, lifecycle, result, artifact state
resources      turns, wall, context, lease, cache, quota, cash
anchors        stable refs to tools, files, checks, events, branches, commits
```

Field ownership stays explicit. Git owns commit identity; the supervisor owns
accepted integration; providers own cache-hit evidence; the model owns only its
proposals.

## 6. SituationFrame

The SituationFrame is a deterministic bounded rendering of BranchState appended
as a late control message:

```text
MISSION
AUTHORITY
ACCEPTED
DELTA
OPEN
CHILDREN
RESOURCES
ANCHORS
```

Properties:

1. Equal durable input and Git state produce equal bytes.
2. Unknown, inferred, accepted, and stale values are distinct.
3. Each section and the whole frame have hard byte/item limits.
4. Truncation identifies the omitted section and a drill-down cursor.
5. The frame is rebuilt immediately before dispatch.
6. It never rewrites the stable system/tool head or prior CAST entries.
7. The event log stores only version, watermark, identity, digest, size, and
   truncation metadata when the frame is reproducible.
8. It contains no credentials, hidden reasoning, or full transcript.

CAST answers which semantic history remains active. SituationFrame answers what
is true and controllable at the current decision boundary.

## 7. Agent control protocol

```text
ORIENT       read mission, accepted state, open work, and resources
LOCATE       find the smallest evidence region that resolves uncertainty
ACT          batch independent reads; serialize effects; delegate separable work
OBSERVE      distinguish direct evidence from inference
VERIFY       run the narrowest decisive check, then affected integration checks
EXTERNALIZE  preserve decisions, expensive findings, failures, and obligations
FINISH       require done criteria, accepted artifacts, and current verification
```

The agent emits short plans, typed actions, claims, evidence references, and
summaries. It is not required to expose hidden reasoning.

## 8. Accretive knowledge

The semantic projection distinguishes:

```text
Observation   direct tool, provider, event, file, or artifact result
Claim         model interpretation with observed/inferred/hypothesis basis
Decision      selected course with supporting refs
Obligation    unfinished work with owner and completion condition
Verification  check tied to an artifact/context state
```

Transitions are append-only:

```text
claim        proposed -> accepted | invalidated
decision     active -> superseded
obligation   open -> satisfied | cancelled | blocked
verification passed -> failed | stale
```

A changed accepted artifact stales overlapping verification. Numeric confidence
never replaces evidence. K0 preserves current facts, decisions, constraining
failed approaches, verification, obligations, and compact anchors—not every
historical item.

Canonical tool evidence references are:

```text
tool:<percent-encoded-task-id>:<generation>:<turn>:<batch-index>
```

The batch index is zero-based and mandatory. New history tooling must not add an
omission-based compatibility form.

## 9. Tool surface

The target model tool set remains small and orthogonal:

```text
inspect_state   current BranchState sections
branch_history  prior branches, tools, and bounded transcript windows
repo_query      bounded repository location and navigation
read_batch      exact bounded file reads
write_file      complete-file creation or replacement
edit_file       one exact local replacement
git_op          inspection and narrowly allowlisted Git operations
run_shell       checks that lack a typed tool
delegate        one scoped child proposal
```

`inspect_state` answers what is authoritative now. `branch_history` answers what
happened before. `repo_query` answers where relevant code is. Do not hide these
questions behind one universal query tool.

A target tool does not justify a standalone implementation. Schema, dispatcher,
prompt text, provider-tool hash, bounded result, durable observation, and one
public scenario land together. Repository navigation starts with the smallest
portable actions; a one-shot LSP boundary is added only when measured need
justifies it.

## 10. Delegation and ResultCapsule

Delegate only when expected critical-path or information benefit exceeds
context construction, spawn, provider queue, join, verification, and conflict
cost.

The parent declares objective, ownership, completion criteria, verification,
context mode, placement, and budget. The supervisor resolves hard feasibility;
prose never selects credentials.

A child returns one bounded versioned ResultCapsule containing status, outcome,
evidence-linked claims and decisions, changed artifacts and head, verification
and tested head, open obligations, blockers, usage, and recommended parent
action.

Semantic acceptance and artifact integration are separate. A parent resumes
with write authority only after required integration is accepted and its
worktree matches the accepted head. Completion order never determines join
order.

## 11. ResourceEnvelope

The model sees decision-relevant facts, not raw telemetry:

```text
remaining_turns and remaining_wall_s
context and uncached-token pressure
provider/model lease
cache affinity and warm estimate
request, concurrency, quota, and cash pressure
relative delegation overhead
alternative feasible lane availability
```

Each value is measured, configured, or `unknown`. Request rate, concurrency,
tokens, time windows, and cash remain separate. Provider selection first builds
the hard-feasible set, then ranks it from recorded evidence. Root migration is
an explicit checkpointed state transition, not a retry detail.

## 12. Shared control plane

`BranchState` supports two projections:

```text
model  SituationFrame + inspect_state
human  TUI + monitor + status commands
```

Both expose the same mission, accepted artifact, context lineage, children,
blockers, verification, and resources. Operator steering is a validated durable
event applied at one branch boundary and shown in the next frame. Frontends
remain renderers and command sources, never runtime authority.

## 13. Failure and recovery

```text
failure
  -> classify the owning boundary
  -> preserve salvage and last safe checkpoint
  -> stale affected claims and verification
  -> rebuild BranchState
  -> emit a fresh SituationFrame with the failure delta
  -> retry, migrate, delegate, or stop within budget
```

Recovery must restore cognitive and artifact state together. Checkpoint reuse
requires matching workspace identity. Provider migration records lost cache
affinity. Merge conflict evidence is bounded and ordered. Failed verification
remains an obligation rather than becoming “tests run.”

## 14. Current implementation truth

Current `main` provides:

- durable events, immutable checkpoints, isolated/fenced workers, salvage, and
  ref-only Git publication;
- append-only CAST summaries and K0 rollover;
- provider leases, usage debt, quota/cache capability values, and routing;
- persistent interactive sessions and an event-sourced operator projection;
- explicit child context/placement behavior when declared;
- automatic compatibility behavior when child policy is omitted;
- six active model tools: `write_file`, `edit_file`, `git_op`, `run_shell`,
  `read_batch`, and `delegate`.

It does not yet provide:

- a canonical BranchState shared by supervisor, model, and TUI;
- an automatically injected SituationFrame;
- `inspect_state`, `branch_history`, or `repo_query` as model tools or production
  implementations;
- evidence-linked knowledge identities and a versioned ResultCapsule;
- a model-visible ResourceEnvelope and critical-path child view;
- named decision/history prompt components with live callers;
- strict removal or explicit naming of omitted-policy compatibility.

The repository `implementation-plan.md` orders those target slices.

## 15. Non-goals

This architecture does not require:

- hidden-chain-of-thought storage;
- a vector database or second memory service;
- global mutable singleton agent state;
- provider-native orchestration;
- automatic replay of all history;
- one monolithic tool;
- role-specific worker runtimes;
- trusting model prose as Git or test evidence;
- placeholder economics presented as measurement.

## 16. Acceptance properties

```text
[ ] one event prefix produces one deterministic BranchState
[ ] model and operator projections agree on shared fields
[ ] every action is tied to its SituationFrame watermark and digest
[ ] obligations survive summary flush and K0 rollover
[ ] exact evidence reopens without whole-transcript replay
[ ] batched tool calls have distinct canonical refs
[ ] repository navigation precedes broad reads or shell search
[ ] a child capsule cannot imply artifact acceptance
[ ] artifact changes stale affected verification
[ ] resources are visible without exposing secrets
[ ] delegation improves held-out efficiency
[ ] no new memory database is required
[ ] source, schemas, prompts, tests, reference docs, and UI agree
```

## 17. Related documents

- [`architecture.md`](architecture.md) — current runtime map
- [`context-engine.md`](context-engine.md) — CAST and checkpoint lineage
- [`context-branches.md`](context-branches.md) — branch rationale
- [`subagents.md`](subagents.md) — current delegation mechanics
- [`provider-routing.md`](provider-routing.md) — provider ownership
- [`terminal-interface.md`](terminal-interface.md) — operator projection
- [`../reference/agent-state.md`](../reference/agent-state.md) — target schemas
- [`../how-to/agent-driving-loop.md`](../how-to/agent-driving-loop.md) — agent loop
- [`../research/agent-system-evaluation.md`](../research/agent-system-evaluation.md) — evaluation
- [`../../implementation-plan.md`](../../implementation-plan.md) — ordered work
