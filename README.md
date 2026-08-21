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
- `session list | latest | show | status | resume | usage` - read and resume
  sessions. `list`, `latest`, and `show` read result records; `status` and
  `usage` read event logs; `resume` requires the leaf's `plan.json`.
- `supervisor --session-dir DIR (--plan PATH | --task-spec PATH | --demo)` -
  run one supervisor session from a plan, a task spec, or the built-in
  deterministic demo. `--warm-pool-size N` (default `0`) and
  `--conversations` are optional.
- `architectus [--dry-run|--scripted] [--provider PROVIDER[:MODEL]]
  [--model [PROVIDER/]MODEL] [--tier TIER] [--waves N] [--task TASK]` - run
  one live or scripted Architectus decision session: build one fixture
  TaskTree, run one or more decision waves through the pure core, and print
  the resulting action intents.
- `auth set|remove|list|oauth|run` - manage stored provider credentials and
  launch the authorized supervisor profile. `set` reads the key from stdin
  with `--stdin`; OAuth operations are `login`, `status`, `logout`, and
  `import-codex-cli`.
- `doctor [--session-dir DIR] [--oauth-live]` - run diagnostics.
- `bench {report,gate,re-anchor,quality}` - run benchmark or quality tooling;
  each mode also accepts `--full`, `--drift-report`, `--bench-root PATH`,
  `--bench-metric-delta FLOAT`, and `--bench-wall-ratio FLOAT`.
- `module-test NAME` - run one module's isolated conformance gate.
- `version` - print the Cambium version.

Unknown command lines are rejected; use `run PROMPT` for a prompt.

Context reuse and rolling transcript compaction are enabled by default for
operator-facing `run`, `repl`, `tui`, and supervisor commands. There is no
second public switch. At a compaction boundary Cambium writes a new immutable,
content-addressed context epoch and makes it active; it never rewrites the old
epoch.

Provider-backed `run`, `repl`, and `tui` prompts may complete successfully
with a conversational/read-only answer and no file change: no commit is made
and nothing is merged or published (no empty commit or merge occurs), and the
rendered output carries the summary. A prompt that changes files commits once
and merges as before.

See [`docs/architecture/user-cli.md`](docs/architecture/user-cli.md) for the
exact run, repl, tui, session, authentication, diagnostics, and tooling
workflows; provider selection from the trusted user config
(`~/.config/cambium/providers.json`) or `CAMBIUM_PROVIDERS`; and how stored
credentials are handed to workers in memory.

## Quickstart

```sh
PYTHONPATH=src python3.14 -m cambium.cli --help
PYTHONPATH=src python3.14 -m cambium.cli run "review the repository" --repo .
PYTHONPATH=src python3.14 -m cambium.cli supervisor --session-dir demo --demo
PYTHONPATH=src python3.14 -m cambium.cli session show --session-dir . demo
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
