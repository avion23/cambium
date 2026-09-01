# Recursive cache-aligned context branches

**Status:** target architecture with current policy behavior identified
explicitly. Source, executable scenarios, durable records, and accepted Git
state remain authoritative for landed behavior.

Cambium's problem is not how to create more agents. It is how to give a
stateless language model enough context to act correctly while keeping context
bounded, cache-friendly, inspectable, and cheap to recover.

```text
branch = task + context + provider lease + resources
       + worktree + children + verification + result + artifact head
```

The root and every child use the same branch abstraction. Specialization comes
from task contract, authority, context mode, placement, tools, budget, and done
criteria—not separate root, research, review, or sub-main runtimes.

## 1. Structures that remain distinct

```text
TASK TREE
  work ownership, dependency, depth, width, lifetime, join order

CONVERSATION BRANCH TREE
  model messages, tool calls, checkpoints, summaries, recursive sessions

GIT ARTIFACT DAG
  repository states, commits, accepted integration order

PROVIDER-CACHE LINEAGE
  exact provider/model/protocol/prompt/tool-prefix compatibility

PROVIDER TOPOLOGY
  provider/model/subscription placement and available capacity

SEMANTIC STATE PROJECTION
  current facts, decisions, failed approaches, verification, obligations
```

Task ancestry is not cache compatibility. A semantic result is not accepted Git
state. Provider placement is not parentage. The semantic projection is rebuilt
from durable records rather than stored in another memory database.

## 2. Normal branch context

```text
stable system/tool head
+ append-only semantic trunk
+ bounded recent raw tail
```

Each semantic entry covers one exact disjoint raw range. Published entries are
immutable. K0 rollover materializes the current working set without erasing raw
history.

Historical detail is not copied into the stable system prompt. The target read
path is progressive:

```text
SituationFrame
    -> branch/result summary
    -> exact evidence reference
    -> bounded transcript or source window
    -> raw durable artifact only for recovery
```

A historical read is a temporary late observation. If it changes a durable
conclusion, the normal semantic path appends a fact, invalidation, decision,
supersession, verification result, or obligation. Old history is not rewritten.

## 3. ContextBranch

```text
ContextBranch
├── task and parent identity
├── task contract and authority
├── context checkpoint, epoch, trunk, and raw tail
├── provider/model lease and resource budget
├── isolated worktree and artifact heads
├── plan, blockers, and open obligations
├── recursive children
├── verification state
└── bounded semantic result
```

A child may recursively create a child subject to the same supervisor-owned
width, depth, budget, lifetime, and join rules.

## 4. Context mode and provider placement

Context representation and resource placement are separate decisions.

### Context modes

| Mode | Child starts with | Use |
| --- | --- | --- |
| `trunk` | complete exact parent checkpoint and raw tail | tightly coupled work that benefits from exact same-provider reuse |
| `semantic` | immutable semantic trunk under a fresh provider-specific head | separable work that needs project decisions but not the raw trajectory |
| `fresh` | task contract only | blind review, independent reproduction, assumption isolation |

### Placement modes

| Placement | Meaning |
| --- | --- |
| `inherit` | preserve parent provider/model affinity when feasible |
| `spread` | remove inherited hard pinning, prefer another hard-feasible lane, then use the complete feasible set |

Valid model-originated combinations:

```text
trunk + inherit
semantic + inherit
semantic + spread
fresh + inherit
fresh + spread
```

Invalid:

```text
trunk + spread
```

An exact `trunk + inherit` request either proves provider/model/protocol/
reasoning/prompt/tool/checkpoint compatibility or is rejected. It never silently
becomes semantic or fresh.

Provider spreading remains a preference inside the hard-feasible set. It cannot
bypass credentials, authorization, capabilities, context/output limits, quota,
cash, or task constraints.

## 5. Current model policy contract

Current model-facing `delegate` schema, prompt, parser, and call-time tool
validation require both `context_mode` and `placement`.

```json
{
  "child_task_id": "review-routing",
  "kind": "investigation",
  "spec": {
    "task": "Read-only routing review; report concrete defects and reproductions.",
    "context_mode": "semantic",
    "placement": "spread"
  }
}
```

The supervisor validates the proposal again before durable admission and spawn.
The model does not choose credentials by prose.

Harness-originated static `proposed_children` may still omit both fields and
enter the internal automatic compatibility path. This is not a model default.
The remaining target choice is to remove that path or give it an explicit
schema/event value so missing data carries no behavior.

## 6. Effect authority

A branch's file mutations are enforced by the active tool boundary:

```text
write_file / edit_file
    -> resolve target
    -> require normal path inside assigned worktree
    -> reject parent traversal
    -> reject .git and .cambium
    -> reject symlink escape
```

`read_batch` is a separate bounded inspection capability and may read permitted
external paths. This preserves worktree ownership in deterministic code rather
than relying on prompt compliance.

## 7. Fork and join

```text
parent delegate proposal
        |
        v
model schema + call-time validation
        |
        v
supervisor authority/tree/budget/policy validation
        |
        v
persist child_admitted
        |
        +--> trunk/inherit: exact compatible checkpoint
        +--> semantic: immutable semantic state under fresh head
        +--> fresh: task only
        |
        v
child worker + isolated worktree
        |
        v
bounded semantic result + optional artifact
        |
        v
ordered artifact integration and combined verification
        |
        v
parent worktree HEAD == accepted integration HEAD
        |
        v
parent resume
```

Admission is durable before spawn. A completion future is registered before a
fast child can finish. The parent owns child lifetime under structured
concurrency. Completion order never decides join order.

Semantic result, artifact integration, and verification are independent:

```text
child result represented
AND changed artifact accepted when present
AND parent worktree matches accepted integration head
AND required verification applies to the combined tree
```

No summary or `files_changed` field can prove publication.

## 8. Historical inspection

`branch_history.py` already implements a bounded projection over existing events
and immutable checkpoints. `code_index.py` and `lsp_query.py` provide bounded
navigation library boundaries. None is currently in the active model tool
roster.

Canonical future tool identity:

```text
tool:<percent-encoded-task-id>:<generation>:<turn>:<batch-index>
```

The zero-based batch index is mandatory for new references because several tool
calls may share one model turn. History, current state, and repository location
remain separate model interfaces:

```text
inspect_state    what is authoritative now?
branch_history   what happened before or in another branch?
repo_query       where is relevant repository code?
```

Each capability must land end to end: schema, dispatcher, prompt/tool hash,
bounded result, durable observation, public scenario, and documentation. A
historical read never re-executes a tool.

## 9. Computer-science grounding

- **Persistent data structures / MVCC:** children share immutable prefixes and
  append private continuation state.
- **Event sourcing:** events and checkpoints remain replay authority; semantic
  and branch state are materialized views.
- **Structured concurrency / fork-join:** a parent owns child lifetime and joins
  results deterministically.
- **Actor/process isolation:** each worker owns private execution state and
  communicates through bounded messages.
- **Content addressing:** checkpoint and prompt identities prove exact context
  compatibility.
- **Affinity scheduling:** exact children stay on the provider that can reuse
  the prefix.
- **Load balancing with switching costs:** semantic/fresh children spread only
  when throughput or information gain exceeds context and coordination cost.
- **CALM:** monotone observations can merge without coordination; conflicting
  decisions and code edits require an ordered join.
- **Working-set model:** the trunk is hot state; detailed history is paged in on
  demand.

## 10. Current source map

```text
src/cambium/child_policy.py   context/placement values and parsers
src/cambium/schemas.py        model-facing delegate contract
src/cambium/tools.py          call-time validation and file effects
src/cambium/prompts.py        current model instructions
src/cambium/worker.py         proposal buffering, suspension, checkpoints
src/cambium/supervisor.py     admission, materialization, lifetime, join, publication
src/cambium/tasktree.py       task-tree bounds and deterministic readiness
src/cambium/branch_history.py bounded history projection; model wiring pending
src/cambium/code_index.py     portable navigation; model wiring pending
src/cambium/lsp_query.py      optional LSP boundary; model wiring pending
```

Target BranchState, SituationFrame, and active inspection wiring are ordered in
[`../../implementation-plan.md`](../../implementation-plan.md).

## 11. Non-goals

This architecture does not require:

- a vector memory service or evidence database;
- hidden-reasoning storage;
- per-branch ACL architecture inside one session;
- provider-native orchestration;
- automatic replay of all history;
- role-specific worker subclasses;
- a universal query tool;
- compatibility behavior hidden behind absent public fields.
