# Cambium

Cambium is a Python-native multi-agent coding-agent harness. The `cambium`
CLI runs directly from source with the system interpreter:

```sh
PYTHONPATH=src python3.14 -m cambium.cli --help
```

## User CLI

The entry point (`src/cambium/cli.py`) dispatches these commands:

- `run PROMPT` — run one prompt against a repository (`--repo`,
  `--session-dir`, `--provider`, `--model`, `--auto`, `--max-wall-s`,
  `--max-tokens`, `--max-turns`, `--json`).
- `repl` — interactive line loop; each input line is one prompt.
- `tui` — line-oriented front end with a `cambium> ` prompt.
- `session list | latest | show SESSION` — read completed sessions from their
  `.cambium/result.json` and `.cambium/events.db` artifacts.
- `supervisor --session-dir DIR` — run one supervisor session from a plan, a
  task spec, or the built-in deterministic demo.
- `auth set|remove PROVIDER` — manage stored provider credentials; `set` reads
  the key from stdin with `--stdin`. Only `set` and `remove` take a
  `PROVIDER` positional; `auth list` takes none.
- `doctor`, `bench`, `tasktree`, `module-test`, `version` — diagnostics and
  tooling.

A multi-word command line that is not a known command is a prompt:
`cambium make the change` runs `cambium run "make the change"`.

Provider-backed `run`, `repl`, and `tui` prompts may complete successfully
with a conversational/read-only answer and no file change: no commit is made
and nothing is merged or published (no empty commit or merge occurs), and the
rendered output carries the summary. A prompt that changes files commits once
and merges as before.

See [`docs/architecture/user-cli.md`](docs/architecture/user-cli.md) for the
exact run, bare-prompt, repl, tui, and session workflows, provider selection
from the trusted user config (`~/.config/cambium/providers.json`), and how
stored credentials are handed to workers in memory.

## Quickstart

```sh
PYTHONPATH=src python3.14 -m cambium.cli supervisor --session-dir demo
PYTHONPATH=src python3.14 -m cambium.cli session show --session-dir . demo
PYTHONPATH=src python3.14 -m cambium.cli --help
```

The supervisor demo runs a deterministic worker against a seeded repository
inside `demo/` and publishes its branch onto that repository's
`refs/heads/main`.

## Documentation authority

- [`agents.md`](agents.md) — operating contract and current module map.
- [`docs/architecture/architecture.md`](docs/architecture/architecture.md) —
  canonical current-versus-target contract.
- [`docs/architecture/user-cli.md`](docs/architecture/user-cli.md) — user CLI
  workflows and credential handoff.
- [`docs/research/v2-1-status.md`](docs/research/v2-1-status.md) — capability
  and gap table.
- [`implementation-plan.md`](implementation-plan.md) — ordered work only.
- [`docs/research/README.md`](docs/research/README.md) — research authority
  and index.
