# Runtime architecture

**Status:** current runtime map. Source and executable tests take precedence.
Design rationale is in [agent operating model](agent-operating-model.md).

## Execution path

```text
CLI / TUI
  -> oneshot or persistent interactive session
  -> supervisor: admit task, allocate worktree, assign provider, start worker
  -> worker: build context, request action, execute tools, checkpoint
  -> Diffundo: provider transport, deadlines, retry/fallback, usage
  -> worker result
  -> supervisor: verify artifact boundary, publish, join children, resume parent
  -> durable events -> operator display / inspection
```

A provider call does not publish code. A successful tool does not finish a task.
A child result does not by itself put the child's code into the parent's tree.
The supervisor owns these transitions and their ordering.

## Module ownership

| Owner | Responsibility |
| --- | --- |
| `cli.py` | Command parsing and frontend selection |
| `oneshot.py`, `interactive.py` | One request or a persistent branch across operator turns |
| `supervisor.py` | Task/process lifetime, admission, isolated worktrees, child joins, publication |
| `worker.py` | Model/action loop, context assembly, verification tracking, checkpoints and summaries |
| `schemas.py`, `tools.py` | Model-facing tool contract and actual dispatch |
| `diffundo.py` | Call-time transport, deadlines, retries, fallback and provider usage |
| `provider_config.py`, `routing.py`, `selection.py` | Declared capabilities, task admission and candidate ordering |
| `provider_scheduler.py` | Lease/cache values and quota reservations; not another task scheduler |
| `store.py` | Durable execution records |
| `summary_trunk.py` | Immutable summary entries and their content contract |
| `branch_state.py` | Replay-derived branch state and `inspect-state` support |
| `branch_history.py` | Read-only historical tool/transcript retrieval |
| `code_index.py`, `lsp_query.py` | Bounded source scans and optional configured LSP queries |
| `observability.py`, `tui.py`, `tui_screen.py` | Incremental operator projection, input handling and rendering |
| `prompts.py` | Versioned coding and summary instructions |
| `optimize.py`, `prompt_optimize.py`, `benchmark.py` | Offline DSPy experiments, real rollouts and automatic policy replacement |

Paths in this table are relative to `src/cambium/`. Large ownership modules,
especially worker and supervisor, still contain too much policy. Extract only a
cohesive operation with real callers; do not replace them with a class hierarchy.

## Current agent tools

The active schema and dispatch contain:

```text
read_batch   write_file   edit_file   git_op   run_shell   delegate
repo_query   branch_history
```

Permissions still determine which effects a worker may perform. Navigation and
history are read-only and can share the independent-read batch path. Tool calls
can use direct `name`/`arguments` or a `calls` batch without a redundant type tag;
actual tool arguments still follow their schema.

The model chooses delegation in its ordinary action call. Omitted child policy
and workspace fields are filled by the worker/supervisor; see
[automatic delegation](context-branches.md). No separate classifier runs first.

`prompts.py` loads the deployed coding/summary policies for new sessions.
Interactive sessions pin that text, so replacement does not rewrite a live
CAST prefix. [Optimization](optimization.md) describes GEPA and deployment.
Budget pressure does not disable tools, and a successful shell command is not a
universal completion certificate. A run without a terminal verdict is incomplete.

`repo_query` exposes the existing tree/search/symbol/reference/window scans and
optional LSP adapter. The portable reference scan is lexical, not a semantic
language-service answer. A missing LSP configuration is reported as unavailable.

`branch_history` lists branches or calls, reopens a returned call reference, or
pages a checkpoint transcript. It reads existing artifacts and never reruns a
tool. Interactive references include the enclosing operator turn so counters
repeated in later turns cannot resolve to unrelated evidence.

Exact navigation examples are in [agent-state reference](../reference/agent-state.md);
history and delegation formats are in
[context-branch reference](../reference/context-branches.md).

## State: implemented versus proposed

`BranchState` and the `cambium inspect-state` command are implemented. It was
incorrect for earlier docs to describe their existence as future work.
`observability.py` still owns the TUI's separate event projection; adding
`BranchState` did not automatically make every consumer share one reducer.

The base runtime does not yet implement the complete model-facing
`SituationFrame`/`inspect_state` proposal, an evidence-linked WorkLedger, or a
new versioned ResultCapsule protocol. The existing summary entries, child
result envelope, verification state and Git joins already work. Extend those
paths where needed rather than introducing parallel mutable stores.

The current source/tests for a feature must land before its status changes from
proposed to implemented. Architecture diagrams and data types alone do not
establish that a worker can use it.

## Important boundaries

* Task ownership, conversation history, accepted Git state and provider cache
  lineage are distinct. See [context branches](context-branches.md).
* Provider feasibility is checked before ranking; leases, call-time health and
  account quotas have different owners. See [provider routing](provider-routing.md).
* Published context prefixes stay immutable. Raw history survives projection
  changes; cache availability never changes the semantic request.
* Model text proposes actions and claims. Tool observations, actual checks and
  accepted Git heads establish effects. Worktree isolation protects parallel
  changes and is not an optional prompt instruction.
* Frontends render events and send operator input; widget memory is not recovery
  state. See [interactive TUI](interactive-tui.md).

## Executable checks

Use focused tests for the owner being changed. The regular suite excludes
`slow` tests; a green regular run does not prove a real terminal or provider ran.

```sh
python -m pytest -n 4 -q
python -m pytest -n 0 -m slow tests/scenarios/test_tui_live_pty.py -q
python -m pytest -n 0 -m acceptance tests/acceptance/test_live_frontends.py -q
python -m pytest -n 0 -m acceptance tests/acceptance/test_live_coding_gate.py -q
```

The live frontend tests exercise CLI publication and a real PTY coding turn
followed by exact historical retrieval. They use disposable repositories and
configured provider credentials. Report model/provider failures as failures,
not as evidence that the harness path passed.

Open work belongs only in [implementation-plan.md](../../implementation-plan.md),
not in another copy of the runtime map.
