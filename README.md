# Cambium

Cambium is a multi-provider coding harness: one bounded agent loop can use
many LLM providers through rotation, failover, and provider cache affinity.
It is for developers who want coding work to continue across provider failures
and resume from durable checkpoints.

## Quickstart

Requires Python 3.12+ and a Git repository with a checked-out branch.

```bash
python -m venv .venv && . .venv/bin/activate && python -m pip install -e .
mkdir -p ~/.config/cambium
cat > ~/.config/cambium/providers.json <<'JSON'
{
  "providers": [{
    "name": "openai",
    "tier": "strong",
    "base_url": "https://api.openai.com/v1",
    "api_key_env": "CAMBIUM_PROVIDER_OPENAI_API_KEY",
    "api_key": "replace-with-your-api-key",
    "model": "gpt-5.6"
  }]
}
JSON
cambium run "Explain this repository" --repo .
```

The `api_key` value is read from `providers.json`; replace the placeholder
with a key for the configured provider.

## Features

- Parallel plan execution for independent tasks and dependency waves.
- Delegate subagents as supervised children in isolated Git worktrees.
- Checkpoints and resume for interrupted or failed sessions.
- Moderation recovery retries a transformed flagged summary.
- Operator TUI for interactive turns and live task state.

## Limits

- Some non-scenario suites (bench/cli/lm/routing) are outside the enforced gate.
- Coding children share one coding-agent prompt; `kind` is task-tree metadata.
- Nested orchestration requires `CAMBIUM_ALLOW_NESTED_EPHEMERAL=1`.

## Docs

- [Architecture](docs/architecture/architecture.md)
- [Provider routing](docs/architecture/provider-routing.md)
- [Operations](docs/architecture/operations.md)
- [Subagents](docs/architecture/subagents.md)
- [Interactive TUI](docs/architecture/interactive-tui.md)

## License

**All rights reserved.** This project is published without an open-source
license: you may read and reference the code, but redistribution, derivative
works, and commercial use require the author's explicit permission.
