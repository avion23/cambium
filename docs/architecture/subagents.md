# Subagents and workload delegation

**Status:** implemented runtime contract. Source and tests remain authoritative.

Cambium does not use a provider-native “spawn agent” feature. Every subagent is
a normal Cambium task admitted by the supervisor, executed by a worker process,
and isolated in its own Git worktree. Provider-native tool calls are only a
transport for emitting the typed `delegate` action.

This distinction matters:

```text
provider-native tool call
        |
        v
typed delegate proposal
        |
        v
Cambium supervisor admission
        |
        v
worker process + isolated worktree
```

The provider proposes work. Cambium owns process creation, provider admission,
filesystem isolation, cancellation, result validation, and publication.

## 1. Computer-science model

Patterns: bounded task DAG, fork-join, fencing token, write-ahead admission.

A child receives authority through its task spec. It does not inherit arbitrary
parent memory, sibling state, credentials, or write access.

## 2. Ways a child can be created

Cambium has four creation paths. They converge on the same supervisor admission
and worker runtime.

### Static plan child

A submitted plan can contain tasks with `depends_on`. The supervisor validates
the DAG, admits ready tasks in deterministic order, and runs independent tasks
in parallel up to the configured width and provider capacity.

### Model-requested child

A coding worker can call `delegate` with:

```json
{
  "child_task_id": "review-routing",
  "kind": "investigation",
  "spec": {
    "task": "Inspect provider routing only. Own no files. Report concrete violations and the tests that reproduce them."
  }
}
```

The tool call does not spawn immediately. The proposal is buffered under the
parent generation. The supervisor admits it only at a permitted parent
lifecycle boundary, after validating identity, depth, width, ownership, paths,
provider authority, and the task spec.

### Architectus child

The optional Architectus decision port can emit typed child proposals from a
parent result. Architectus is a decision source, not a second scheduler. Its
proposal still crosses the normal admission boundary.

### Conflict-resolver child

A merge conflict can create a narrowly scoped resolver task. The resolver owns
only the conflict envelope and the affected integration attempt. Its result is
validated and joined through the same publication path.

## 3. How workload is distributed

The workload contract is `spec.task`. A useful child task states four things:

1. **Objective** — the result to produce.
2. **Ownership** — files, symbols, tests, or investigation area the child owns.
3. **Definition of done** — observable completion criteria.
4. **Verification** — commands or evidence required before success.

Bad delegation:

```text
Improve the code.
```

Good delegation:

```text
Inspect src/cambium/routing.py and its scenario tests only.
Do not edit provider transports.
Find violations of the single-owner routing invariant.
For each real defect, add the smallest failing scenario and repair it.
Run the focused routing tests and report the exact commands.
```

Parallel children should have non-overlapping write ownership. Read-only
investigations may overlap. When two children must edit the same semantic area,
serialize them or assign one owner and make the other a reviewer.

The task `kind` is structural metadata for the task tree. It does **not** select
a different system prompt. Current coding children use the same coding-agent
prompt and tool catalogue. Their behavior differs because of the task,
parent-result envelope, provider constraints, permissions, and checkpoint
lineage.

## 4. Prompt construction

Cambium keeps instructions and untrusted workload data separate.

```text
system role
  stable coding-agent contract
  tool protocol
  summary protocol
  available tool schemas

user role
  <cambium-task>
  child workload
  </cambium-task>

user role, when present
  <cambium-parent-context>
  strict bounded parent result
  </cambium-parent-context>
```

A child does not receive the parent transcript or hidden reasoning. The parent
result is a strict bounded envelope containing status, summary, diff evidence,
commits, files changed, metrics, and parent identity.

For an exact context fork, Cambium appends one child-task user message after the
immutable checkpoint. The leading provider messages remain byte-identical.

For cross-provider semantic reuse, Cambium builds a fresh provider-specific
system/tool head and imports only immutable semantic summary entries. This is
semantic continuity, not a provider-cache hit.

## 5. Provider modes

“Native subagent” is ambiguous and should not be used for Cambium children.
Use these terms instead.

| Mode | Provider/model | Context | Cache claim |
| --- | --- | --- | --- |
| **Cache-affine child** | Same compatible provider and model | Exact checkpoint prefix plus child task | May reuse provider prefix cache |
| **Semantic-reuse child** | Independently admitted provider/model | Fresh head plus immutable summaries | Cold on the new provider |
| **Fresh child** | Independently admitted provider/model | New prompt without reusable summary | No reuse |
| **Pinned child** | Explicitly constrained provider/model | Depends on compatibility | Only evidence-backed cache claims |

The supervisor, not prompt prose, selects the provider. A task constrains
admission with fields such as:

```json
{
  "requirements": {
    "quality": "strong",
    "min_context_window": 100000,
    "allow_paid": true
  },
  "model_candidates": ["gpt-5.6", "claude-opus"],
  "authorized_providers": ["openai", "anthropic"],
  "authorized_providers_explicit": true
}
```

An exact compatible child inherits the parent provider/model lease and a
`context_fork` descriptor. An incompatible but non-redacted checkpoint yields
`summary_trunk_ref`; the child is then admitted independently and starts cold
with semantic history. If neither form is legal, the child starts fresh or is
rejected.

The provider JSON field `supports_native_tools` is an opt-in transport
capability; when absent it is `false`. A declared `true` provider receives
typed function/tool wire fields, while a declared `false` or absent provider
receives the identical messages without those fields and uses the universal
textual-JSON action protocol. Native capability never filters the cascade: all
enabled providers remain eligible, and `needs_native_tools` is retained only
as a backward-compatible task field rather than a hard admission filter.
Provider “native tools” means that the provider transports typed function/tool
calls. It does not mean the provider owns subagent orchestration.

## 6. Lifecycle

```text
parent worker
    |
    | delegate proposal
    v
generation-local proposal buffer
    |
    | parent reaches permitted boundary
    v
validate task-tree revision and child spec
    |
    | durable child_admitted event
    v
spawn child worker in isolated worktree
    |
    | strict result envelope
    v
validate child result
    |
    +--> semantic result join
    |
    +--> private Git artifact integration
              |
              v
       verify parent HEAD == accepted integration HEAD
              |
              v
          parent resume
```

Admission is durable before spawn. The child completion future belongs to the
parent. The parent releases its worker slot while suspended and resumes only
after the bounded child wait and join invariant.

## 7. Semantic join and artifact join

A child returns two different products:

- **Semantic result:** bounded evidence for the parent prompt.
- **Artifact result:** commits integrated into the parent workspace.

These joins must agree. A parent must never resume with a child summary while
its worktree still points to pre-child code.

```text
semantic child result accepted
            AND
parent worktree HEAD == accepted integration HEAD
            AND
combined tree passes required verification
```

Git integration is serialized. Conflict evidence is bounded and can be routed
to a resolver child. The resumed parent is responsible for verification of the
combined tree when child integration changed the workspace.

## 8. Failure and cancellation

- Invalid, duplicate, cyclic, too-deep, or too-wide proposals are rejected and
  spawn nothing.
- A child cannot widen the parent provider allowlist or credential authority.
- A failed child produces a bounded failure envelope for the parent.
- Parent timeout does not silently convert an unfinished child into success.
- Cancellation is generation-fenced; stale workers cannot publish.
- A provider failure changes only the directly observed provider health state.
- A semantic child result cannot authorize a Git publication by itself.

## 9. Choosing whether to delegate

Delegate when at least one condition is true:

- the work decomposes into independent ownership regions;
- a read-only investigation can run concurrently;
- another provider/model has a materially better capability/cost fit;
- a conflict requires a separate resolver authority;
- the child can reuse a large compatible prefix.

Do not delegate when:

- the task is a small local edit;
- children would contend on the same files;
- coordination cost exceeds the saved wall time;
- the child cannot be given a precise definition of done;
- a direct tool call is sufficient.

## 10. Source map

- Tool schema and preflight validation: `src/cambium/schemas.py`
- Worker prompt and delegate suspension: `src/cambium/worker.py`
- Admission, provider pinning, join, and publication: `src/cambium/supervisor.py`
- Task-tree bounds: `src/cambium/tasktree.py`
- Provider feasibility and debt: `src/cambium/routing.py`
- Call-time provider health and failover: `src/cambium/diffundo.py`
- Operator projection: `src/cambium/observability.py`
- TUI rendering: `src/cambium/tui_screen.py`
