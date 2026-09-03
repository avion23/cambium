# Cambium

Cambium is a local multi-provider coding-agent runtime built around durable,
cache-friendly context and isolated Git execution.

Its unit of work is a branch:

```text
task contract
+ CAST context
+ provider/model lease
+ fenced worker and isolated worktree
+ durable events/checkpoints
+ child branches
+ verification
+ semantic result
+ accepted artifact head
```

The model proposes plans, tool calls, children, and finish verdicts. Cambium owns
credentials, provider admission, process lifecycle, budgets, checkpoints,
context forks, child joins, Git publication, cancellation, and recovery.

## Current capabilities

- **Durable execution:** JSON-lines supervisor/worker protocol, generation
  fencing, immutable checkpoints, worktree salvage, restart, and ref-only Git
  publication.
- **CAST context:** stable system/tool head, append-only semantic deltas, bounded
  raw tail, deterministic K0 rollover, and exact versus semantic cache lineage.
- **Multi-provider operation:** capability/credential admission, provider/model
  leases, usage debt, quota reservations, effort-aware deadlines, typed failure,
  and bounded call-time failover.
- **Recursive branches:** static and dynamic task trees, isolated child
  worktrees, explicit context/placement behavior when declared, deterministic
  join barriers, and conflict-resolver support.
- **Persistent terminal session:** one interactive semantic branch across
  prompts, reconnect, queued follow-up, event replay, model/tool activity,
  context, usage, quota, and child-agent projection.
- **Optimization path:** reviewed trajectory extraction, split discipline, DSPy
  evaluation, canaries, and promotion gates.

## Architecture direction

Cambium is being made agent-intuitive around one rule:

```text
one canonical branch state, many projections
```

The target system derives a compact current operating picture from events,
checkpoints, Git, and provider records. The model receives it as a bounded
`SituationFrame`; the human sees the same semantics in the TUI; precise current
state, historical evidence, and repository location use separate inspection
surfaces.

```text
durable events + checkpoints + Git + quota
                    |
                    v
              canonical BranchState
             /          |           \
    SituationFrame      TUI      supervisor policy
          |
          v
orient -> locate -> act -> observe -> verify -> accrete -> finish
```

See
[`docs/architecture/agent-operating-model.md`](docs/architecture/agent-operating-model.md)
and the ordered [`implementation-plan.md`](implementation-plan.md).

## Current truth and target gaps

Current source consumes declared child `context_mode` and `placement`: the
model schema rejects omission before admission. A supervisor-side automatic
exact/semantic resolution remains only for harness-originated specs that reach
it without a declared policy; it is a current compatibility gap, not a public
contract.

The repository also contains implementations of branch-history projection,
bounded code indexing, and optional one-shot LSP queries. They are not yet part
of the active worker tool roster, which currently exposes:

```text
write_file
edit_file
git_op
run_shell
read_batch
delegate
```

The automatic SituationFrame, shared BranchState reducer, `inspect_state`,
evidence-linked WorkLedger, versioned ResultCapsule, and model-visible
ResourceEnvelope are target work, not current implementation claims.

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
uv run cambium tui --repo . --auto
```

Continue the latest interactive branch:

```bash
uv run cambium tui --repo . --continue --auto
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
```

## Verification

```bash
uv run ruff check src tests
uv run pytest -m "not slow" -q
uv run pytest -m slow -q
```

Credential-gated acceptance checks use real provider configuration/accounts and
are intentionally separate from hermetic CI.

## Documentation

Start with:

1. [`docs/architecture/agent-operating-model.md`](docs/architecture/agent-operating-model.md)
   — synthetic agent control model and linked abstraction tower.
2. [`docs/architecture/architecture.md`](docs/architecture/architecture.md) —
   current runtime map and ownership.
3. [`implementation-plan.md`](implementation-plan.md) — ordered open work.
4. [`agents.md`](agents.md) — coding-agent/contributor operating contract.
5. [`docs/README.md`](docs/README.md) — complete documentation map and status
   language.

Focused documents:

- [`docs/architecture/context-engine.md`](docs/architecture/context-engine.md)
- [`docs/architecture/context-branches.md`](docs/architecture/context-branches.md)
- [`docs/architecture/subagents.md`](docs/architecture/subagents.md)
- [`docs/architecture/provider-routing.md`](docs/architecture/provider-routing.md)
- [`docs/architecture/terminal-interface.md`](docs/architecture/terminal-interface.md)
- [`docs/reference/agent-state.md`](docs/reference/agent-state.md)
- [`docs/how-to/agent-driving-loop.md`](docs/how-to/agent-driving-loop.md)
- [`docs/research/agent-system-evaluation.md`](docs/research/agent-system-evaluation.md)

## License

**All rights reserved.** This project is published without an open-source
license: you may read and reference the code, but redistribution, derivative
works, and commercial use require the author's explicit permission.
