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

The model proposes plans, tool calls, child work, and finish verdicts. Cambium
owns credentials, provider admission, process lifecycle, budgets, checkpoints,
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
  worktrees, explicit model-originated context/placement policy, deterministic
  join barriers, and conflict-resolver support.
- **Confined file effects:** `write_file` and `edit_file` may change only normal
  files inside the assigned worktree; parent paths, `.git`, `.cambium`, and
  symlink escapes are rejected. `read_batch` remains a bounded inspection tool
  and may read permitted external paths.
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
`SituationFrame`; the human sees the same semantics in the TUI. Precise current
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

Model-originated `delegate` calls must provide both `context_mode` and
`placement`. The tool schema, prompt, parser, call-time validation, and
supervisor admission agree on that contract. Harness-originated static proposals
can still omit both fields and enter the internal automatic compatibility path;
removing that path or assigning it an explicit wire/event value remains target
work.

The repository contains implemented library boundaries for branch-history
projection, bounded code indexing, and optional one-shot LSP queries. They are
not active model tools. The current roster is:

```text
write_file
edit_file
git_op
run_shell
read_batch
delegate
```

The automatic SituationFrame, shared BranchState reducer, `inspect_state`,
evidence-linked WorkLedger, versioned ResultCapsule, model-visible
ResourceEnvelope, and end-to-end history/navigation tool wiring are target work,
not current implementation claims.

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
python -m compileall -q src tests
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
