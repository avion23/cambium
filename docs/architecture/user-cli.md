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
`--provider` and `--model` pin a selection: the named provider is the assigned
primary and is tried first; same-tier providers with a usable credential can
serve as fallback after it fails. Provider ids are validated against
`[a-z0-9](?:[a-z0-9._-]{0,62}[a-z0-9])?`. Without either option, the default
cascade uses enabled configured providers that have stored credentials;
admission selects a provider/model/tier from that set. `--auto` uses recorded
usage to balance that cascade. The default turn limit is 50. `--max-wall-s`
defaults to `300`, `--max-tokens` to `200000`.

One-shot runs create a fresh session under `<repo>/.cambium/sessions` unless
`--session-dir` names a new leaf. A used leaf is rejected; there is no
one-shot resume. Each run uses an isolated worktree. Exit codes are 0 for
success, 1 for run failure, 2 for input or configuration errors, 75 for a busy
session lock, and 130 for interruption.

Without `--provider`/`--model`, the run builds a cascade from every enabled
provider in the trusted user config that has a usable API-key or OAuth
credential. Admission selects a provider/model/tier from that set. `--auto`
uses the same credential-backed set and explicitly enables usage-balanced
routing. The marker task (`target_file` plus `marker`) is an internal
`OneShotConfig` path only; the CLI does not expose those fields, so a plain
run never becomes a marker task. Provider runs use the bounded Diffundo
worker loop. A provider run may complete successfully with a
conversational/read-only answer and no file/commit: when the agent changed no
files, no empty commit or merge occurs and nothing is published, and the
rendered output still carries the summary. A run that changed files commits
once and merges normally.

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

Each provider entry requires `name`, `tier` (`fast|balanced|strong|reasoning`),
and the tagged `auth`/`protocol` mode. `auth: "api_key"` +
`protocol: "chat_completions"` (the default) requires `base_url` (http(s);
plaintext http only for loopback hosts) and `api_key_env`, which must equal the
derived canonical name `CAMBIUM_PROVIDER_<PROVIDER>_API_KEY`. An
`auth: "codex_chatgpt"` entry is pinned to the `CODEX_CHATGPT_PROFILE` module
constants, must use `protocol: "codex_responses"`, and must not carry
`base_url` or `api_key_env`; optional `reasoning_effort` rides the request
body. Unknown fields, duplicate names, colliding env names, and invalid values
are rejected by `provider_config.load_providers`. The file contains
environment-variable names only, never key values.

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

The store is the fixed path `<effective-user-home>/.local/share/cambium/
auth.json` (the passwd home of the effective uid, not `HOME`). The directory
is `0700`, the file `0600`, and updates are atomic temp-file + rename under an
exclusive directory lock; symlinks, foreign owners, and wrong modes are
rejected. Keys are kept in memory only long enough to validate, serialize, or
build a launch environment; representations and exceptions never expose them.

For a provider run, `oneshot._stored_provider_environment` builds an in-memory
mapping `{derived_env_name: key_value}`. The canonical environment credential
wins; otherwise the value is read from the `AuthStore`. `run_oneshot` passes
that mapping directly to `supervisor.run_plan(..., provider_environment=...)`.
It is not part of the plan: `plan.json` carries only the `provider_env_keys`
names and the `provider_config_path`. Key values never enter `plan.json`,
`result.json`, events, argv, logs, or diagnostics.

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

One session leaf contains `plan.json` (mode `0600`, names never credential
values), `.cambium/result.json`, `.cambium/events.db`, `.cambium/session.lock`
(one live supervisor per session), and `wt/` (the worker worktree, removed
when clean after a terminal task).

## Supervisor

The unified `supervisor` command is session and supervision driven:

```sh
cambium supervisor --session-dir DIR [--conversations]
```

It does not select providers and does not accept `--plan` or `--task-spec`.
Those plan-runtime inputs belong to `python -m cambium.supervisor` and are used
by session resume. `--conversations` persists worker conversations in the
session directory.