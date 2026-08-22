# Cambium

Cambium is a Python 3.14 multi-agent coding harness with durable events,
provider routing, append-only context trunks, Git worktree isolation, and
replayable supervision.

Run it from the checkout:

```sh
uv sync --extra dev --python 3.14
uv run cambium --help
```

## Main commands

```sh
# One coding task
uv run cambium run "review and improve the repository" --repo .

# Interactive prompt loop; a live dashboard replaces the screen while a run is active
uv run cambium tui --repo .

# Attach a dashboard to any running or completed durable session
uv run cambium monitor /path/to/session
uv run cambium-monitor /path/to/session

# Inspect durable state without a full-screen terminal
uv run cambium monitor /path/to/session --once
uv run cambium monitor /path/to/session --json

# Session recovery and accounting
uv run cambium session status --session-dir ROOT SESSION
uv run cambium session usage --session-dir ROOT SESSION
uv run cambium session resume /path/to/session

# DSPy decision-module optimization
uv run cambium optimize should_decompose --dry-run
uv run cambium optimize should_decompose --optimizer bootstrap --budget-usd 2
```

Other command groups are `auth`, `supervisor`, `doctor`, `bench`,
`module-test`, `repl`, `architectus`, and `version`.

## Operator dashboard

The dashboard is an event-sourced read model. It shows:

- main and child-agent lifecycle state;
- provider/model, generation, turn, epoch, and current tool per agent;
- input, output, cached, and total tokens per agent;
- output tokens/second and estimated cost;
- exact latest prompt tokens when the provider reports them;
- append-only summary-trunk segments and byte-derived trunk/raw-tail estimates;
- recent durable events and failures.

The frontend does not inspect live worker objects. Replaying the same event log
and checkpoints produces the same dashboard state.

## Context model

Provider-backed agents use:

```text
stable head + S1 + S2 + ... + Sn + bounded raw tail
```

Each summary entry covers one disjoint raw range once. Earlier summary bytes are
never summarized or rewritten. Compatible children reuse the exact prefix;
incompatible providers receive the semantic summary history under a fresh
provider-specific head.

## Documentation

- [`agents.md`](agents.md) — repository operating contract.
- [`docs/architecture/architecture.md`](docs/architecture/architecture.md) —
  runtime and ownership boundaries.
- [`docs/architecture/context-engine.md`](docs/architecture/context-engine.md) —
  append-only context epochs and cache reuse.
- [`docs/architecture/provider-routing.md`](docs/architecture/provider-routing.md) —
  provider feasibility, routing, and accounting.
- [`docs/architecture/terminal-interface.md`](docs/architecture/terminal-interface.md) —
  implemented dashboard contract.
- [`docs/architecture/optimization.md`](docs/architecture/optimization.md) —
  DSPy data and evaluation contract.
- [`docs/security/threat-model.md`](docs/security/threat-model.md) — active
  no-sandbox threat model.
- [`docs/research/README.md`](docs/research/README.md) — retained measurements
  and research evidence.

Source and tests are authoritative when documentation disagrees.
