# Cambium

Cambium is a stdlib-first multi-agent coding harness. It supervises isolated
worker processes, records durable session events, routes model calls across
configured providers, and publishes successful worker commits through a fenced
merge sequencer.

The current runtime is intentionally small:

- one asyncio supervisor,
- one worker implementation,
- one provider router,
- one task-tree model,
- one durable event store,
- one merge/publication path,
- one provider-admission policy (`cambium.routing`),
- one active tool catalogue (`cambium.schemas` + `cambium.tools`).

## Status

Cambium runs directly from source and currently requires Python 3.14.
`pyproject.toml` declares package metadata, dependencies, test extras, and the
`cambium` / `cambium-monitor` entry points, but a checked-out repository does
not need an editable install.

```bash
PYTHONPATH=src python -m cambium version
PYTHONPATH=src python -m cambium --help
```

Use [`agents.md`](agents.md) as the operating contract for coding agents.
Current architecture is documented in
[`docs/architecture/architecture.md`](docs/architecture/architecture.md), with
the single provider-routing ownership model in
[`docs/architecture/provider-routing.md`](docs/architecture/provider-routing.md).
Research documents under `docs/research/` are design evidence, not additional
runtime implementations.

## Architecture boundaries

Provider concerns are deliberately separated without duplicating ownership:

- `cambium.routing` owns supervisor admission, durable usage debt, capability
  checks, and lane-aware provider/model assignment.
- `cambium.selection` is the shared pure equal-priority quality-ordering
  primitive used by admission and Diffundo.
- `cambium.diffundo` owns call-time health, retry, cooldown, and transport
  execution after a task is assigned.
- `cambium.provider_scheduler` retains only immutable provider leases and the
  transactional quota ledger. It contains no competing scheduler actor.

The worker uses `cambium.schemas` as the schema source of truth and
`cambium.tools` as the sole executable tool dispatcher. There is no parallel
plugin registry.

## Quick start

Run the deterministic demo:

```bash
PYTHONPATH=src python -m cambium supervisor \
  --session-dir /tmp/cambium-demo \
  --demo
```

Run one prompt against the current repository:

```bash
PYTHONPATH=src python -m cambium run \
  --repo . \
  --provider openai:gpt-5.6 \
  "Inspect the current task-tree implementation"
```

For automatic selection across enabled providers with available credentials:

```bash
PYTHONPATH=src python -m cambium run \
  --repo . \
  --auto \
  "Inspect the current task-tree implementation"
```

Start an interactive session:

```bash
PYTHONPATH=src python -m cambium repl --repo . --auto
```

Start the live terminal dashboard:

```bash
PYTHONPATH=src python -m cambium tui --repo . --auto
```

Attach a monitor to an existing session:

```bash
PYTHONPATH=src python -m cambium monitor --session /path/to/session
```

Inspect or record provider quota windows:

```bash
PYTHONPATH=src python -m cambium quota status
PYTHONPATH=src python -m cambium quota observe openai five-hour \
  --reset-in-s 18000 \
  --allowance-tokens 1000000 \
  --remaining-tokens 750000
```

## Provider configuration

Cambium reads provider definitions from:

```text
~/.config/cambium/providers.json
```

Override the path with `CAMBIUM_PROVIDERS`. Provider files contain environment
variable names, never secret values. API keys are stored with:

```bash
PYTHONPATH=src python -m cambium auth set openai
PYTHONPATH=src python -m cambium auth list
PYTHONPATH=src python -m cambium doctor
```

For Codex/ChatGPT OAuth:

```bash
PYTHONPATH=src python -m cambium auth oauth login codex
PYTHONPATH=src python -m cambium auth oauth status codex
```

## Local validation

This repository does not use GitHub Actions or hosted continuous integration.
Run checks directly from the checkout:

```bash
PYTHONPATH=src python -m pytest -q
PYTHONPATH=src python -m ruff check src tests
PYTHONPATH=src python -m cambium doctor
```

The default pytest invocation excludes tests marked `slow`. Run the
process-boundary tier explicitly when needed:

```bash
PYTHONPATH=src python -m pytest -m slow -q
```

## Session artifacts

A session records its state below `<session-dir>/.cambium/`, including:

- `events.db` — durable event log,
- `result.json` — final session result,
- `conversations.db` — optional revision-boundary conversation history,
- `checkpoints/` — worker checkpoints and immutable context epochs.

The supervisor publishes through Git references. It does not refresh the
caller's working tree or index after advancing `main`.
