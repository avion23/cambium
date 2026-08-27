# Cambium

Multi-provider coding harness — [`src/cambium/diffundo.py`](src/cambium/diffundo.py).<br>
Rotation, demotion, failover, lease-rotation, and CAST context caching — [`src/cambium/diffundo.py`](src/cambium/diffundo.py), [`src/cambium/worker.py`](src/cambium/worker.py).<br>
Live operator TUI — [`src/cambium/tui_screen.py`](src/cambium/tui_screen.py), [`src/cambium/observability.py`](src/cambium/observability.py).

## FEATURES

- Provider rotation, demotion, failover, lease-rotation, and recoverable `CONTENT_FLAGGED` moderation recovery — [`src/cambium/diffundo.py`](src/cambium/diffundo.py).
- CAST epoch checkpoints, summary trunk/raw tail, `cache_key` `prefix_sha256` hashing, and the provider-cache-evidence-only rule — [`src/cambium/worker.py`](src/cambium/worker.py), [`docs/architecture/cast.md`](docs/architecture/cast.md).
- Uncached-token budget charging and graceful forced finalization — [`src/cambium/worker.py`](src/cambium/worker.py).
- Checkpoint-bound stall resume and `salvage/<task>/<gen>/workspace.diff` — [`src/cambium/supervisor.py`](src/cambium/supervisor.py).
- Credential-feasible admission and the success invariant, including `requires_commit` — [`src/cambium/supervisor.py`](src/cambium/supervisor.py), [`src/cambium/worker.py`](src/cambium/worker.py).
- Operator rail with lineage glyphs and fold ticks — [`src/cambium/tui_screen.py`](src/cambium/tui_screen.py), [`src/cambium/observability.py`](src/cambium/observability.py).
- Integer-only duration stats — [`src/cambium/tools.py`](src/cambium/tools.py), [`src/cambium/worker.py`](src/cambium/worker.py), [`src/cambium/tui_screen.py`](src/cambium/tui_screen.py).
- Plan-mode parallel workers — [`src/cambium/supervisor.py`](src/cambium/supervisor.py), [`src/cambium/cli.py`](src/cambium/cli.py).

## INSTALL

```bash
pip install -e .
cambium --help
```

From a checkout:

```bash
PYTHONPATH=src python -m cambium --help
```

Top-level commands are defined in [`src/cambium/cli.py`](src/cambium/cli.py):

- `cambium run` — run one prompt.
- `cambium tui` — start the terminal dashboard.
- `cambium repl` — start an interactive prompt session.
- `cambium supervisor` — run a plan, task spec, or demo session.
- `cambium monitor` — attach to a durable session.
- `cambium session` — list, inspect, resume, or report on sessions.
- `cambium doctor` — run harness diagnostics.
- `cambium auth` — manage provider credentials and OAuth.
- `cambium bench` — run benchmark report, gate, re-anchor, or quality.
- `cambium module-test NAME` — run one module's conformance gate.
- `cambium quota` — inspect or update provider quota windows.
- `cambium optimize MODULE|extract|stats|eval` — optimize or inspect datasets.
- `cambium architectus` — run a live or scripted decision session.
- `cambium version` — print the version.

Providers use `~/.config/cambium/providers.json`; API-key entries use the
`api_key_env` convention `CAMBIUM_PROVIDER_<NAME>_API_KEY`; Codex OAuth uses
`cambium auth oauth login PROVIDER` — [`src/cambium/provider_config.py`](src/cambium/provider_config.py), [`src/cambium/cli.py`](src/cambium/cli.py), [`src/cambium/oauth.py`](src/cambium/oauth.py).

OPERATIONS: details in `docs/architecture/operations.md` (missing in this checkout); caching internals: [`docs/architecture/cast.md`](docs/architecture/cast.md) + [`docs/architecture/context-engine.md`](docs/architecture/context-engine.md).

## LIMITS

- Slow-tier tests (`-m slow`) carry known failures — [`src/cambium/supervisor.py`](src/cambium/supervisor.py).
- Worker-side moderation transform-retry is not yet implemented — [`src/cambium/worker.py`](src/cambium/worker.py).
- Nested orchestration needs `CAMBIUM_ALLOW_NESTED_EPHEMERAL=1` — [`src/cambium/oneshot.py`](src/cambium/oneshot.py).

## License

**All rights reserved.** This project is published without an open-source
license: you may read and reference the code, but redistribution, derivative
works, and commercial use require the author's explicit permission.
