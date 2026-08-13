# Cambium user CLI

**Status:** current behavior. The command parser and scenario tests are the authority.

## Entry point

Run the unified CLI with:

```sh
PYTHONPATH=src python3.14 -m cambium.cli --help
```

The unified commands are `auth`, `supervisor`, `doctor`, `bench`, `module-test`,
`version`, `run`, `repl`, `tui`, `session`, and `architectus`. Prompts require the
explicit `run` command. Unknown commands are never interpreted as prompts.

Task-tree validation has a separate module interface only:

```sh
PYTHONPATH=src python3.14 -m cambium.tasktree [PLAN]
```

## Agent commands

`run`, `repl`, and `tui` share repository, session, provider, routing, and budget
options:

```text
--repo PATH --session-dir DIR
--provider PROVIDER[:MODEL] --model [PROVIDER/]MODEL --auto
--max-wall-s SECONDS --max-tokens N --max-turns N
```

`run` requires one prompt and supports `--json`. `tui` also supports `--quiet`.
`--provider` and `--model` pin a selection. Without either option, the default
cascade uses enabled configured providers that have credentials. `--auto` uses
recorded usage to balance that cascade. The default turn limit is 50.

One-shot runs create a fresh session under `<repo>/.cambium/sessions` unless
`--session-dir` names a new leaf. A used leaf is rejected. Each run uses an
isolated worktree. Exit codes are 0 for success, 1 for run failure, 2 for input
or configuration errors, 75 for a busy session lock, and 130 for interruption.

REPL and TUI process one prompt per line. Operational failures use the prefixes
`cambium repl:` and `cambium tui:`. Broken output pipes exit successfully;
unexpected programming errors terminate the interface. TUI suppresses only an
absent usage record and reports malformed or inaccessible statistics.

## Provider and credential forms

`run`, `repl`, `tui`, and `architectus` accept `--provider NAME:MODEL` and
`--model PROVIDER/MODEL`. Architectus also accepts `--tier`, `--waves`, `--task`,
and deterministic `--dry-run`/`--scripted` execution.

Provider configuration comes from `CAMBIUM_PROVIDERS` when set, otherwise from
`<effective-home>/.config/cambium/providers.json`. Target-repository provider
files are not trusted automatically. Credential values do not appear in argv,
plans, results, events, or diagnostics.

## Authentication

```text
cambium auth set PROVIDER [--stdin]
cambium auth remove PROVIDER
cambium auth list
cambium auth oauth login PROVIDER [--client-id ID]
cambium auth oauth status PROVIDER
cambium auth oauth logout PROVIDER
cambium auth oauth import-codex-cli
cambium auth run supervisor --session-dir DIR [--conversations]
```

`auth set` accepts only an enabled provider in the trusted provider config.
Removal is idempotent: a missing credential prints `no change` and exits 0.
OAuth status is local and does not refresh. Logout removes only the local
session. Import reads the Codex CLI session and stores it as provider `codex`.

## Sessions

The default session root is `<cwd>/.cambium/sessions`; `--session-dir DIR`
overrides it for read commands.

- `session list` prints completed session paths.
- `session latest` prints the latest completed session.
- `session show SESSION` prints the stored result as JSON.
- `session status SESSION` renders current task state from the event log.
- `session usage SESSION` renders token and estimated-cost totals by task and
  provider.
- `session resume SESSION` requires the leaf's persisted `plan.json` and invokes
  the supervisor resume path. Completed reconciled tasks remain skipped and
  interrupted tasks restart from their persisted bases.

Read failures and missing artifacts exit 1 with a `cambium session:` diagnostic.
An interrupted resume exits 130.

## Supervisor

The unified `supervisor` command is session and supervision driven:

```sh
cambium supervisor --session-dir DIR [--conversations]
```

It does not select providers and does not accept `--plan` or `--task-spec`.
Those plan-runtime inputs belong to `python -m cambium.supervisor` and are used
by session resume. `--conversations` persists worker conversations in the
session directory.
