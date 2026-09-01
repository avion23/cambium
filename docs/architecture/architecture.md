# Cambium architecture

**Status:** current runtime map plus target integration direction. Source,
executable scenarios, durable records, and accepted Git state are authoritative
for landed behavior.

The target system model is in
[`agent-operating-model.md`](agent-operating-model.md). This document maps that
model to current repository ownership.

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

The model proposes intent. Cambium owns authority, execution, persistence,
resources, process lifecycle, child admission, context publication, Git
integration, and recovery.

A plausible final message is not sufficient. Success requires agreement between:

```text
semantic result
accepted repository artifact
required current verification
supervisor terminal verdict
```

## 2. Tower and orthogonal structures

```text
evaluation and policy promotion
        ^
model SituationFrame + human operator view
        ^
branch controller and policy
        ^
canonical BranchState               target integration layer
        ^
CAST + artifact + lease projections
        ^
events + checkpoints + Git + quota
        ^
tools + workers + transports + merge
        ^
repository + providers + operator intent
```

Observations flow upward. Validated commands flow downward. Higher layers do not
mutate lower-layer state outside explicit interfaces.

Cambium keeps these structures separate:

| Structure | Question | Current authority |
| --- | --- | --- |
| task tree | who owns which work and child lifetime? | supervisor/tasktree |
| conversation branches | what did each model branch see and do? | events/checkpoints |
| Git artifact DAG | which repository states were accepted? | Git/merge/supervisor |
| provider-cache lineage | which request prefixes are exactly compatible? | checkpoint/cache-key/provider evidence |
| provider topology | where does each branch run and what capacity exists? | routing/scheduler/provider config |
| semantic projection | which facts, decisions, failures, checks, and obligations are current? | CAST today; target WorkLedger |

Task ancestry is not cache compatibility. Provider placement is not parentage. A
semantic result is not accepted Git state.

## 3. Current runtime flow

### 3.1 Admission

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

Static dependencies use validated ready waves. Dynamic children cross the same
supervisor-owned task-tree, authority, budget, and provider checks.

### 3.2 Worker decision loop

```text
supervisor init/run_task
        |
        v
worker builds stable prompt head + CAST + recent tail
        |
        v
provider call through Diffundo
        |
        v
plan | tool_call batch | finish
        |
        v
validate and execute tools, append observations, checkpoint, repeat
```

Current active model tools:

```text
write_file
edit_file
git_op
run_shell
read_batch
delegate
```

`write_file` and `edit_file` resolve their targets and permit mutations only to
normal paths inside the assigned worktree. Parent paths, `.git`, `.cambium`, and
symlink escapes are rejected at the effect boundary. `read_batch` remains a
bounded inspection capability and may read permitted external paths.

The repository contains implemented library boundaries for branch-history
projection, portable code indexing, and optional one-shot LSP queries. They are
not active model tools because schema, dispatch, prompt/tool hash, durable
observation, and public scenario wiring are still missing.

### 3.3 Context loop

```text
stable system/tool head
+ immutable semantic summary entries
+ bounded raw working tail
```

A summary flush covers one disjoint raw range. Existing entries are immutable.
K0 rollover materializes active semantic state while source records remain
durable. Provider cache is a measured performance outcome, never correctness
state.

See [`context-engine.md`](context-engine.md).

### 3.4 Child fork and join

```text
model delegate proposal
        |
        v
schema + call-time tool validation
        |
        v
supervisor tree/authority/budget/policy validation
        |
        v
persist child_admitted before spawn
        |
        +--> trunk/inherit: exact compatible checkpoint
        +--> semantic: immutable summary state under fresh head
        +--> fresh: task only
        |
        v
child worker + isolated worktree
        |
        v
bounded semantic result + optional artifact
        |
        v
ordered integration + combined verification
        |
        v
parent HEAD == accepted integration HEAD
```

Model-originated children must declare `context_mode` and `placement`.
`trunk+spread` is rejected and explicit policy never silently downgrades.
Harness-originated static `proposed_children` can still enter an internal
automatic compatibility path when both fields are absent. That path is a known
gap: it must receive an explicit schema/event value or be removed.

A parent owns child lifetime under structured concurrency. Completion order does
not determine result or artifact join order.

See [`context-branches.md`](context-branches.md) and
[`subagents.md`](subagents.md).

### 3.5 Provider flow

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

There is one scheduler ownership path. Configuration describes capabilities and
credentials; routing admits and ranks; Diffundo executes attempts; quota and
usage records supply evidence. Prompt prose does not choose credentials.

### 3.6 Publication and recovery

A successful worker owns at most one fenced commit. The supervisor validates
branch attachment, clean state, envelope/HEAD agreement, and expected-old
publication before advancing a ref. Child integration and root publication are
serialized.

On interruption, Cambium preserves bounded salvage and the latest valid
checkpoint. Resume is legal only when workspace identity matches; otherwise the
old worktree is fenced and recovery starts from accepted state.

## 4. Control surfaces

### Human surface

The persistent TUI and monitor reduce durable events into operator views of
agents, context, usage, quota, and recent activity. Frontends are not runtime
authority.

### Model surface

Current model context contains the coding prompt, task, optional parent context,
CAST entries, recent observations, and six tool schemas.

Target model control adds a deterministic late SituationFrame and three
orthogonal read surfaces:

```text
inspect_state    current accepted branch state
branch_history   prior branch/tool/transcript evidence
repo_query       repository location/navigation
```

The target model and TUI project the same BranchState fields at one watermark.
The existing history/index/LSP libraries are inputs to these future slices, not
proof that the model surfaces already exist.

## 5. Durable data and ownership

| Data | Writer | Readers | Mutation rule |
| --- | --- | --- | --- |
| event log | supervisor/worker through EventStore | replay, TUI, monitor, history, target BranchState | append only |
| ordinary checkpoint | worker | restart/recovery/history | immutable file |
| context epoch | worker, supervisor validates | fork/resume/CAST/history | immutable; successor only |
| interactive manifest | InteractiveSession single writer | reconnect/TUI | atomic replace after successful turn |
| routing debt | usage-event fold | admission/doctor | transactional merge |
| quota ledger | reservation owner | admission/operator | transactional reserve/reconcile |
| worker branch | fenced worker | supervisor/merge | one generation owner |
| integration ref | merge sequencer/supervisor | artifact consumers | expected-old atomic advance |
| semantic summary | model proposal, worker validates | active context/target WorkLedger | append only; invalidate/supersede by delta |

## 6. Module map

```text
src/cambium/
  cli.py                    command routing
  interactive.py            persistent interactive branch ownership
  tui.py / tui_screen.py     terminal input and deterministic rendering
  observability.py           current event-sourced operator projection

  supervisor.py             task/worker/child lifecycle, admission, join, publication
  worker.py                 model/tool/context loop and checkpoints
  tasktree.py               hierarchy bounds and readiness
  merge.py                  serialized ref-only publication and recovery
  fencing.py                generation authority

  prompts.py                coding and semantic-summary prompt text
  schemas.py                model action/tool schemas
  tools.py                  active effects and call-time validation
  child_policy.py           context/placement values and parsing
  branch_history.py         bounded history projection; model wiring pending
  code_index.py             bounded portable navigation; model wiring pending
  lsp_query.py              optional one-shot LSP boundary; model wiring pending

  summary_trunk.py          semantic deltas and K0 projection
  context_policy.py         hard CAST bounds
  conversations.py          optional raw conversation rows

  routing.py                provider/model admission and usage debt
  provider_scheduler.py     leases, quota/cache values and reservations
  provider_config.py        provider capabilities/auth/protocol config
  diffundo.py               call-time execution and typed failure
  oauth.py / auth.py        credential stores and worker handoff

  store.py                  durable events
  results.py                canonical terminal result
  modules/                  optimizable deterministic decisions
```

Target additions remain small and require live consumers:

```text
branch_state.py             pure canonical reducer/value objects
situation.py                bounded SituationFrame projection
inspect_state wiring        current-state model tool
branch_history wiring       existing history projection into active tools
repo_query wiring           existing index/LSP boundaries into active tools
```

## 7. Invariants

### Authority

- A worker mutates only its assigned worktree under the current generation.
- File effects cannot mutate parent paths or reserved Git/Cambium metadata.
- A child cannot widen parent filesystem, tool, credential, or provider
  authority.
- A frontend or model response cannot mutate supervisor state directly.
- Publication uses expected-old atomic ref advancement.

### Context

- Raw history remains durable outside active projection.
- Published summary/checkpoint bytes are immutable.
- Exact fork compatibility uses all relevant identity fields.
- Provider cache hits come only from provider evidence.
- Compaction cannot silently lose current obligations or verification.

### Branching

- Model child policy is explicit before proposal registration.
- Admission is durable before spawn.
- Parent lifetime bounds children.
- Completion order does not determine join order.
- Semantic result acceptance cannot imply artifact acceptance.
- Parent write authority resumes only at accepted integration state.

### State and resources

- Accepted facts, model claims, unknowns, and stale values remain distinct.
- Model and operator projections agree at one watermark in the target design.
- Verification is tied to the artifact/configuration tested.
- Request rate, concurrency, tokens, wall time, cash, quota, and cache state
  remain separate.
- Missing evidence remains unknown.

## 8. Current versus target

Current runtime has durable execution, CAST, provider routing, recursive child
branches, confined file mutations, persistent interactive sessions, six active
model tools, and implemented but unwired history/navigation libraries.

Target work includes canonical BranchState, automatic SituationFrame,
state/history/repository model-tool wiring, evidence-linked semantic identities,
versioned ResultCapsule, model-visible ResourceEnvelope, and removal or explicit
naming of the harness-only automatic child policy.

The ordered path is [`../../implementation-plan.md`](../../implementation-plan.md).

## 9. Architectural coherence

```text
one branch identity across process, context, artifacts, resources, and UI
one durable replay authority
one derived branch state for model and operator
one scheduler ownership path
one explicit effect boundary per tool/process/ref update
one bounded semantic accumulation path
one deterministic child admission and join order
one evidence-linked explanation of accepted action and result
```
