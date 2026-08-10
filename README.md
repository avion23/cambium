# Cambium

Cambium is a Python-native coding-agent harness. The `cambium` CLI starts a
supervisor, workers edit isolated Git worktrees over NDJSON stdio, gates run in
the worker worktree, and successful commits publish to `refs/heads/main`.

## Current shape

- `cambium.supervisor.run_plan` accepts a flat supplied task list and supervises
  it concurrently. There is no worker-count semaphore: an 11-task canary
  observed 11 concurrent supervisions. `resource_thresholds` only checks host
  health; `CompileGate` limits heavy gate commands, not workers. Events persist
  in `.cambium/events.db`; publication does not refresh a checkout.
- `worker.do_work` has deterministic marker mode and a bounded custom provider
  and tool loop. Provider calls go through `Diffundo`; strict actions dispatch
  validated tools, emit checkpoints, and end in one worker commit.
- `Diffundo` has tier, priority, cooldown, configured-RPM request-rate buckets,
  and retry behavior. Rate-limited providers report `RATE_LIMITED`; HTTP 429
  `Retry-After` is honored. A bench canary still leaks the `\u005c` escape in
  mixed raw/Unicode-escaped free-form stderr; fix redaction before live use.
- `tasktree` validates and snapshots dependency specs, but `run_plan` does not
  schedule that tree. Architectus, dynamic decomposition, and the conversation
  store are not wired into `run_plan`.
- Target scheduling starts with a harness-owned validated tree and static
  ready-node waves. Each child gets a fresh bounded context and returns only a
  strict envelope; dynamic child admission follows that slice. Prompt-prefix
  stability and provider cache-hit metrics are acceptance requirements.
- External live use is blocked until bounded worker admission, escaped
  free-form redaction, credentials, and OS containment are verified. Loopback
  fixtures do not prove them.
- The package exports only `__version__`; use the CLI or module-level functions.
  The example module includes deterministic `decide` and `evaluate` operations.

See [`docs/research/v2-1-status.md`](docs/research/v2-1-status.md) for the live
capability and gap table. Source and tests are the evidence; target contracts
are in [`docs/architecture/architecture.md`](docs/architecture/architecture.md).

## Quickstart

Requires Python 3.14 and the project dependencies installed for pytest.

```sh
PYTHONPATH=src python3.14 -m pytest -q
```

Run the deterministic demo:

```sh
PYTHONPATH=src python3.14 -m cambium.supervisor --session-dir demo
PYTHONPATH=src python3.14 -m cambium.cli --help
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
