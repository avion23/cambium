# Cambium

Cambium is a multi-provider coding and context-management harness. It keeps a
small append-only semantic trunk for normal LM calls, preserves exact provider
prefixes for caching, and executes separable work as recursive child branches.
Raw child sessions and historical tool calls remain retrievable without being
replayed into every prompt.

```text
                       cached semantic trunk
                               |
             +-----------------+-----------------+
             |                                   |
      trunk + inherit                    semantic/fresh + spread
      full cached context                another feasible provider
             |                                   |
             +---------- bounded result ----------+
                               |
                     branch_history drill-down
```

The task tree, conversation branches, Git artifact graph, and provider-cache
lineage are separate structures with separate owners. See the
[context-branch vision](docs/architecture/context-branches.md).

## Quickstart

Requires Python 3.12+ and a Git repository with `refs/heads/main`.

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

## Features

- Append-only CAST trunks with immutable checkpoints and provider-reported
  cache accounting.
- Recursive child branches with explicit `trunk`, `semantic`, or `fresh`
  context and `inherit` or `spread` provider placement.
- Branch-local stable tool references and on-demand historical transcript
  reads through existing event/checkpoint artifacts.
- Parallel plan execution, dependency waves, bounded fork-join, and isolated
  Git worktrees.
- Provider rotation, quota-aware admission, cooldown, and failure recovery.
- Checkpoint resume and transactional semantic/artifact joins.
- Operator TUI for interactive turns, branch lineage, provider state, and live
  activity.
- Named branch-decision, history-recall, and summarization prompt components
  suitable for DSPy evaluation.

## Documentation

- [Documentation map](docs/README.md)
- [Recursive context-branch vision](docs/architecture/context-branches.md)
- [Normative context-branch requirements](docs/architecture/context-branch-requirements.md)
- [Context-branch tool reference](docs/reference/context-branches.md)
- [Practical decomposition examples](docs/how-to/context-branches.md)
- [Evaluation and DSPy protocol](docs/research/context-branch-evaluation.md)
- [Core runtime architecture](docs/architecture/architecture.md)
- [Provider routing](docs/architecture/provider-routing.md)
- [Subagents](docs/architecture/subagents.md)
- [Interactive TUI](docs/architecture/interactive-tui.md)

## Limits

- Provider throughput/cache savings remain empirical and must be measured per
  workload and subscription.
- `spread` is a routing preference inside the hard-feasible provider set, not a
  guarantee that another provider is available.
- Historical recall uses deterministic event/checkpoint scans; it intentionally
  has no separate search or evidence database.
- Coding children share one base coding prompt; named policy components can be
  optimized independently without creating specialized agent classes.
- Nested orchestration requires `CAMBIUM_ALLOW_NESTED_EPHEMERAL=1`.

## License

**All rights reserved.** This project is published without an open-source
license: you may read and reference the code, but redistribution, derivative
works, and commercial use require the author's explicit permission.
