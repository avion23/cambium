# Cambium

Cambium is a Python-native coding-agent harness, run directly from source. The
`cambium` CLI starts a supervisor, workers edit isolated Git worktrees over
NDJSON stdio, and a clean worker whose envelope reports `succeeded` publishes to
`refs/heads/main`. Production pre-merge gates were removed by product decision:
this is a local development tool, so the worker verdict alone decides merge
eligibility and tools execute without an approval gate.

## Current shape

- `cambium.supervisor.run_plan` accepts a flat supplied task list and supervises
  it concurrently. There is no worker-count semaphore: an 11-task canary
  observed 11 concurrent supervisions. `resource_thresholds` only checks host
  health. There is no pre-merge gate and no `CompileGate` concurrency bound:
  a clean worker whose envelope reports `succeeded` is merged. Events persist
  in `.cambium/events.db`; publication does not refresh a checkout.
- `worker.do_work` has deterministic marker mode and a bounded custom provider
  and tool loop. Provider calls go through `Diffundo`; strict actions dispatch
  validated tools, emit checkpoints, and end in one worker commit.
- `Diffundo` has tier, priority, cooldown, configured-RPM request-rate buckets,
  and retry behavior. Rate-limited providers report `RATE_LIMITED`; HTTP 429
  `Retry-After` is honored.
- `tasktree` validates and snapshots dependency specs, but `run_plan` does not
  schedule that tree. Architectus, dynamic decomposition, and the conversation
  store are not wired into `run_plan`.
- Target scheduling starts with a harness-owned validated tree and static
  ready-node waves. Each child gets a fresh bounded context and returns only a
  strict envelope; dynamic child admission follows that slice. Prompt-prefix
  stability and provider cache-hit metrics are acceptance requirements.
- The package exports only `__version__`; use the CLI or module-level functions.
  The example module includes deterministic `decide` and `evaluate` operations.

See [`docs/research/v2-1-status.md`](docs/research/v2-1-status.md) for the live
capability and gap table. Source and tests are the evidence; target contracts
are in [`docs/architecture/architecture.md`](docs/architecture/architecture.md).

## Quickstart

Cambium is a local development tool run directly from source. Requires Python
3.14 and the project dependencies installed for pytest. There is no wheel or
package delivery: point `PYTHONPATH` at `src` and run the module.

```sh
PYTHONPATH=src python3.14 -m pytest -q
```

Run the deterministic demo:

```sh
PYTHONPATH=src python3.14 -m cambium.supervisor --session-dir demo
python3.14 -m cambium.cli --help
```

Plan publication advances `refs/heads/main` only. Read that ref or explicitly
update a consumer checkout before building or testing it.

## Documentation authority

- [`agents.md`](agents.md) — operating contract and current module map.
- [`docs/architecture/architecture.md`](docs/architecture/architecture.md) —
  canonical current-versus-target contract.
- [`docs/research/v2-1-status.md`](docs/research/v2-1-status.md) — sole detailed
  live capability/gap table.
- [`implementation-plan.md`](implementation-plan.md) — ordered work only.
- [`docs/research/README.md`](docs/research/README.md) — research authority and
  index. Drafts provide context, not runtime proof.
