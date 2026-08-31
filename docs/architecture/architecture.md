# Cambium architecture

**Status:** current runtime map plus target integration direction. Source and
tests are authoritative for landed behavior. Target-only interfaces are labelled
and ordered in `implementation-plan.md`.

The synthetic system model is defined in
[`agent-operating-model.md`](agent-operating-model.md). This document maps that
model to the repository and its current runtime boundaries.

## 1. System definition

Cambium is a local multi-provider coding-agent runtime. Each task executes as a
bounded branch with:

```text
task contract
+ context state
+ provider/model lease
+ isolated Git worktree
+ durable events/checkpoints
+ tools and children
+ verification
+ semantic result
+ accepted artifact head
```

The model proposes actions. Cambium owns authority, execution, persistence,
resource limits, process lifecycle, child admission, context publication, Git
integration, and recovery.

A correct final message is not sufficient. Success requires agreement between:

```text
semantic result
accepted repository artifact
required verification
supervisor terminal verdict
```

## 2. Tower of abstractions

```text
Evaluation / optimization
        ^
Human + model projections
        ^
Branch controller and policy
        ^
Canonical BranchState             target integration layer
        ^
CAST + artifact + lease views
        ^
Events + checkpoints + Git + quota
        ^
Workers + tools + transports + merge
        ^
Repository + providers + operator intent
```

Each layer owns one class of fact. A higher layer may derive or display lower
state but cannot mutate it outside the lower layer's validated interface.

The architectural rule is:

```text
one canonical branch state, many projections
```

Today the operator projection is implemented in `observability.py`; the target
is to factor its shared semantics into a pure BranchState reducer consumed by
both the TUI and the model SituationFrame.

## 3. Orthogonal structures

Cambium must not collapse these structures into one vague “agent tree.”

| Structure | Question it answers | Current authority |
| --- | --- | --- |
| Task tree | Who owns which work and child lifetime? | supervisor/tasktree |
| Conversation branches | What did each model branch see and do? | events/checkpoints |
| Git artifact graph | Which filesystem states were accepted? | Git/merge/supervisor |
| Provider-cache lineage | Which exact request prefixes are compatible? | checkpoint cache key/provider evidence |
| Epistemic state projection | Which claims, decisions, obligations, and checks are current? | CAST summary today; target WorkLedger projection |

The first four have distinct identity and failure modes. The fifth is a derived
semantic view over durable records, not another database.

## 4. Branch model

The root and every child share one conceptual entity:

```text
ContextBranch
├── identity: session/task/parent/generation
├── mission: objective/constraints/done criteria
├── authority: repo/worktree/branch/tools/providers
├── context: checkpoint/epoch/trunk/raw tail/lineage
├── artifacts: base/head/accepted integration
├── control: plan/open work/blockers
├── resources: turns/wall/context/quota/cache/lease
├── children: deterministic admission and join order
├── verification: checks tied to artifact state
└── result: bounded semantic capsule
```

Current source represents these fields across supervisor state, worker
checkpoints, event records, Git, quota/routing values, and TUI reducers. The
target BranchState composes them without changing their owners.

## 5. Runtime flow

### 5.1 Session and task admission

```text
CLI / interactive session / plan
        |
        v
validate task, repo, authority, dependencies, provider feasibility
        |
        v
persist plan and task_assigned
        |
        v
create isolated worktree + generation fence
```

Static dependency plans use validated ready waves. Dynamic children enter as
typed proposals and are durably admitted before their coroutine/process exists.

### 5.2 Worker decision loop

```text
supervisor init/run_task
        |
        v
worker builds stable prompt head + CAST state + recent tail
        |
        v
provider call through Diffundo
        |
        v
plan | tool_call batch | finish
        |
        v
execute bounded tools, append observations, checkpoint, repeat
```

Current model tools are `write_file`, `edit_file`, `git_op`, `run_shell`,
`read_batch`, and `delegate`. Branch history and repository navigation remain
target capabilities; they have no current schema, dispatcher, or production
implementation. Phase 3 must land each capability end to end rather than
keeping standalone code that only its own tests consume.

### 5.3 Context loop

```text
stable system/tool head
+ immutable semantic summary entries
+ bounded raw working tail
```

A summary flush covers one disjoint raw range. Existing summary entries are not
rewritten. K0 rollover materializes current active semantic state under policy
bounds while source entries remain durable. Provider cache is an optional
performance outcome, never correctness state.

See [`context-engine.md`](context-engine.md).

### 5.4 Child fork and join

```text
parent delegate proposal
        |
        v
validate task tree + authority + budget + child policy
        |
        +--> trunk/inherit: exact compatible checkpoint
        +--> semantic: immutable summary state under a fresh head
        +--> fresh: task-only parent-independent context
        |
        v
child worker + isolated worktree
        |
        v
bounded semantic result + artifact integration
        |
        v
verify parent HEAD == accepted integration HEAD
        |
        v
parent resume
```

Current supervisor source consumes declared context/placement policy. For
compatibility, omitted policy still enters automatic exact/semantic resolution;
the target public contract removes or explicitly names that ambiguity.

See [`context-branches.md`](context-branches.md),
[`context-branch-requirements.md`](context-branch-requirements.md), and
[`subagents.md`](subagents.md).

### 5.5 Provider flow

```text
supervisor admission
  hard feasibility + credentials + authorization + capacity + task constraints
        |
        v
provider/model lease
        |
        v
Diffundo attempt execution
  deadline + retry/cooldown + typed failure + bounded cascade
        |
        v
transport and provider evidence
```

There is one scheduler ownership path. Provider configuration describes
capabilities; routing admits and ranks; Diffundo executes attempts; quota and
usage stores supply evidence. Prompt prose does not select credentials.

See [`provider-routing.md`](provider-routing.md).

### 5.6 Publication and recovery

A successful worker owns at most one fenced commit. The supervisor verifies
branch attachment, clean state, envelope/HEAD consistency, and expected-old
publication before a ref advance. Child integration and root publication are
serialized.

On interruption, Cambium preserves bounded salvage and the latest valid
checkpoint. Resume is legal only when workspace identity matches. Otherwise the
worktree is fenced, salvaged, and recovered from the accepted base.

## 6. Control surfaces

### Human surface

The persistent TUI and monitor reduce durable events into an operator view with
agents, context, usage, quota, and recent activity. The frontend is not runtime
authority.

See [`interactive-tui.md`](interactive-tui.md) and
[`terminal-interface.md`](terminal-interface.md).

### Model surface

Current model context contains the coding-agent prompt, task, optional parent
context, CAST summaries, recent observations, and tool schemas.

Target model control adds a deterministic late `SituationFrame` and three
separate read surfaces:

```text
inspect_state   current accepted branch state
branch_history  prior branch/tool/transcript evidence
repo_query      repository location and code navigation
```

The model and TUI then share BranchState fields rather than learning different
versions of the session.

## 7. Durable data and ownership

| Data | Writer | Reader | Mutation rule |
| --- | --- | --- | --- |
| event log | supervisor/worker through EventStore | replay, TUI, monitor, target history/BranchState | append only |
| ordinary checkpoint | worker | restart/recovery, target history | immutable file |
| context epoch | worker, supervisor validates | fork/resume/CAST, target history | immutable; successor epoch only |
| interactive manifest | InteractiveSession single writer | reconnect/TUI | atomic replace after successful turn |
| routing debt | usage-event fold | admission/doctor | transactional merge |
| quota ledger | reservation owner | admission/operator | transactional reservation/reconcile |
| worker branch | fenced worker | supervisor/merge | one generation owner |
| main/parent integration ref | merge sequencer/supervisor | all artifact consumers | expected-old atomic advance |
| semantic summary | model proposal, worker validates | active context/target WorkLedger | append only; invalidate/supersede by delta |

## 8. Module map

```text
src/cambium/
  cli.py                   command routing
  interactive.py           persistent interactive branch ownership
  tui.py                    terminal command/input loop
  tui_screen.py             deterministic rendering
  observability.py          current event-sourced operator projection

  supervisor.py             task/worker/child lifecycle, admission, join, publication
  worker.py                 model/tool/context loop and checkpoints
  tasktree.py               static/dynamic hierarchy bounds
  merge.py                  ref-only serialized publication and recovery
  fencing.py                generation authority

  prompts.py                current coding and summary prompt text
  schemas.py                model action/tool schemas
  tools.py                  active executable worker tools

  summary_trunk.py          semantic deltas and K0 projection
  context_policy.py         hard CAST bounds
  child_policy.py           child context/placement values
  conversations.py          optional raw conversation rows

  routing.py                provider/model admission and usage debt
  provider_scheduler.py     leases, cache/quota values and reservations
  provider_config.py        provider capability/auth/protocol configuration
  diffundo.py               call-time execution and failure handling
  oauth.py / auth.py        credential stores and safe worker handoff

  store.py                  durable events
  results.py                canonical terminal result
  modules/                  optimizable decision modules
```

Target additions should remain small and must land with a live consumer:

```text
branch_state.py            pure canonical reducer/value objects
situation.py               bounded SituationFrame projection
branch_history             bounded durable-history tool
repo_query                 bounded repository-navigation tool
```

Names may change, but ownership must not spread across another scheduler or
frontend-local state machine.

## 9. Invariants

### Authority

- A worker may mutate only its assigned worktree under the current generation.
- A child cannot widen parent credential/provider or filesystem authority.
- A frontend or model response cannot mutate supervisor state directly.
- Publication uses expected-old atomic ref advancement.

### Context

- Raw history remains durable outside the active projection.
- Published summary/checkpoint bytes are immutable.
- Exact fork compatibility is proven from all relevant identity fields.
- Provider cache hits come only from provider evidence.
- Compaction never silently loses current obligations or required verification.

### Branching

- Admission is validated and durable before spawn.
- Parent lifetime bounds children through structured concurrency.
- Child completion order does not determine join order.
- Semantic result acceptance cannot imply artifact acceptance.
- Parent resume after code changes requires the accepted integration head.

### Agent state

- Current accepted facts, model claims, unknowns, and stale values are distinct.
- Model and operator projections agree at the same source watermark.
- Every lossy current item can expose a stable evidence/source reference or an
  explicit inferred/hypothesis label.
- A verification is tied to the artifact state it tested.

### Resources

- Hard provider/task constraints precede scoring.
- Request rate, concurrency, token windows, wall time, cash, and cache state are
  separate dimensions.
- Missing evidence remains unknown.
- Retries, children, summaries, and verification consume the parent/session
  budget explicitly.

## 10. Documentation and truth

Documentation types have different authority:

```text
architecture  rationale, ownership, invariants, current/target boundary
reference     exact public or target values and schemas
how-to        recommended workflows
research      hypotheses and evaluation protocols
implementation-plan ordered open work only
```

A target document must say target. An implemented-contract document must point
to executable source/tests. Historical branch status belongs in Git history,
not the active plan.

## 11. Definition of architectural coherence

```text
one branch identity across process, context, artifacts, resources, and UI
one durable record for replay
one derived branch state for model and operator
one scheduler ownership path
one explicit effect boundary per tool/process/ref update
one bounded semantic accumulation path
one deterministic child admission/join order
one evidence-linked explanation of why an action and final result were valid
```

The ordered path from the current runtime to that state is
[`../../implementation-plan.md`](../../implementation-plan.md).
