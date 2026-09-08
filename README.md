# Cambium

Cambium is a local multi-provider coding-agent runtime. Its unit of work is a
branch: a task running in an isolated Git worktree with a provider lease,
cache-friendly context, durable checkpoints, and supervised Git publication.

The model proposes plans, tool calls, child tasks, and finish verdicts. Cambium
owns credentials, provider admission, process lifecycle, budgets, checkpoints,
context forks, child joins, Git publication, cancellation, and recovery.

## Current capabilities

- **Durable execution:** line-based supervisor/worker protocol, generation
  fencing, restart budget, immutable checkpoints, worktree salvage, and
  ref-only Git publication (`supervisor.py`, `worker.py`, `store.py`).
- **CAST context:** stable system/tool head, append-only semantic trunk,
  bounded raw tail, deterministic K0 rollover, and exact versus semantic cache
  lineage (`summary_trunk.py`, `context_policy.py`).
- **Multi-provider operation:** capability/credential admission, provider/model
  leases, durable quota reservations, usage accounting, effort-aware deadlines,
  and bounded call-time failover (`routing.py`, `provider_scheduler.py`,
  `diffundo.py`).
- **Recursive branches:** per-parent fan-out bounded at 8 and depth at 3, isolated
  child worktrees, per-child context/placement with deterministic defaults,
  serialized integration, and conflict-resolver support.
- **Worker tools:** the active schema and dispatch expose `write_file`,
  `edit_file`, `git_op`, `run_shell`, `read_batch`, `repo_query`,
  `branch_history`, `inspect_state`, and `delegate` (`schemas.py`, `tools.py`).
- **Persistent terminal cockpit:** one interactive branch across prompts,
  reconnect, queued follow-up, cancellation, live usage/quota projection, and
  child-agent lanes, event-loop-owned POSIX input, and F6 focus while editing
  (`interactive.py`, `tui.py`, `tui_screen.py`, `terminal_input.py`).
- **Branch state inspection:** model `inspect_state`, TUI `/inspect`, and CLI
  `inspect-state` share the recorded `BranchState` reader (`state_view.py`).
  A bounded local SituationFrame supplies current worker context without an
  additional model call.
- **Optimization path:** `cambium optimize prompts` runs real repository
  benchmarks with GEPA or a zero baseline and installs a measurably better
  coding/summary policy automatically (`optimize.py`, `prompt_optimize.py`).

An evidence-linked WorkLedger and versioned ResultCapsule remain proposals.
Checkpointing a finish or exact/fresh delegation does not force a summary call;
CAST folds on working-set pressure or a semantic child's need for new context.

## Delegation model

There is one worker implementation; a child is an ordinary worker task owned by
its parent. The model decides to delegate inside its ordinary action call, and
each delegate spec declares `context_mode` (`trunk`, `semantic`, `fresh`) and
`placement` (`inherit`, `spread`), with deterministic defaults when omitted: a
single child defaults to `trunk+inherit`, a multi-child batch to
`semantic+spread`, and a read-only `investigation` delegate to `fresh+inherit`.
`trunk+spread` is rejected. Children run in isolated
worktrees; the supervisor integrates accepted child commits into the parent
before the parent resumes.

## Quick start

Requirements:

- Python 3.12+
- Git
- `uv` recommended

```bash
uv sync --extra dev
uv run cambium --help
```

Run the persistent terminal cockpit against a repository:

```bash
uv run cambium tui --repo .
```

With no `--provider` or `--model`, Cambium uses the credential-ready provider
pool and its normal routing/resource state; no separate automatic-routing flag
is needed.

Continue the latest interactive branch:

```bash
uv run cambium tui --repo . --continue
```

Run a static plan:

```bash
uv run cambium supervisor --session-dir /tmp/cambium-session --plan plan.json
```

Inspect provider and session state:

```bash
uv run cambium doctor
uv run cambium quota status
uv run cambium monitor /path/to/session
uv run cambium inspect-state /path/to/session
```

## Verification

```bash
uv run ruff check src tests
uv run pytest -q
uv run pytest -m "slow and not acceptance" -q
```

Credential-gated acceptance checks use real provider configuration/accounts and
are intentionally separate from hermetic CI.

## Documentation

Start with:

1. [`docs/architecture/architecture.md`](docs/architecture/architecture.md) —
   current runtime map, module ownership, and executable checks.
2. [`docs/architecture/agent-operating-model.md`](docs/architecture/agent-operating-model.md)
   — design rationale for the harness and its unit of work.
3. [`agents.md`](agents.md) — coding-agent/contributor operating contract.
4. [`docs/README.md`](docs/README.md) — complete documentation map and status
   language.

Focused documents:

- [`docs/architecture/context-engine.md`](docs/architecture/context-engine.md)
  — CAST trunking, folds, and K0 rollover.
- [`docs/architecture/context-branches.md`](docs/architecture/context-branches.md)
  — delegation decisions and context/placement defaults.
- [`docs/architecture/subagents.md`](docs/architecture/subagents.md) — child
  lifecycle, admission, joins, and failure recovery.
- [`docs/architecture/provider-routing.md`](docs/architecture/provider-routing.md)
  — admission, routing, quota, and usage semantics.
- [`docs/architecture/terminal-interface.md`](docs/architecture/terminal-interface.md)
  — cockpit layout, input handling, and commands.
- [`docs/architecture/interactive-tui.md`](docs/architecture/interactive-tui.md)
  — durable interactive turns, reconnect, and replay.
- [`docs/architecture/events.md`](docs/architecture/events.md) — event-kind
  glossary for the durable event store.
- [`docs/reference/agent-state.md`](docs/reference/agent-state.md) — shared
  `BranchState`/`SituationFrame` inspection and remaining state proposals.
- [`docs/how-to/agent-driving-loop.md`](docs/how-to/agent-driving-loop.md) —
  driving sessions from another coding agent.

## License

**All rights reserved.** This project is published without an open-source
license: you may read and reference the code, but redistribution, derivative
works, and commercial use require the author's explicit permission.
