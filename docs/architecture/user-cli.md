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

Top-level and subcommand `--help` exits 0. Missing required arguments, unknown
commands, and unknown options are argparse errors and exit 2.

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
`--repo` defaults to the current directory. `--provider` accepts `NAME` or
`NAME:MODEL`; `--model` accepts `MODEL` or `PROVIDER/MODEL`. Either option can
pin a selection: the named provider is the assigned primary and is tried first;
same-tier providers with an available credential can serve as fallback after it
fails. Provider ids are validated against
`[a-z0-9](?:[a-z0-9._-]{0,62}[a-z0-9])?`. Without either option, the default
cascade uses enabled configured providers with an available API-key value or
stored OAuth session; an API key may come from its canonical environment
variable or the auth store. Admission selects a provider/model/tier from that
set. `--auto` uses recorded usage to balance that cascade instead of pinning a
provider or model. The defaults are `--max-wall-s 300`, `--max-tokens 200000`,
and `--max-turns 50`; `--json` and `--quiet` are off unless supplied.

One-shot runs create a fresh session under `<repo>/.cambium/sessions` unless
`--session-dir` names a new leaf. A used leaf is rejected; there is no
one-shot resume. Each run uses an isolated worktree. Exit codes are 0 for
success, 1 for run failure, 2 for input or configuration errors, 75 for a busy
session lock, and 130 for interruption.

Context reuse and rolling transcript compaction are on by default. They use
the existing run/supervisor path and have no public enable/disable options.
Compaction replaces the active projection with a new immutable context epoch;
older content-addressed epochs remain unchanged and valid for audit or pinned
forks.

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
absent usage record and reports malformed or inaccessible statistics. REPL
returns 0 when every prompt succeeds, 1 when a prompt fails, and 130 on
interruption. TUI returns 0 on successful EOF, 1 when a prompt fails or its
backend cannot be imported, and 130 on interruption.

## Provider and credential forms

`run`, `repl`, `tui`, and `architectus` accept `--provider NAME` or
`--provider NAME:MODEL`, and `--model MODEL` or `--model PROVIDER/MODEL`.
Architectus also accepts `--tier`, `--waves`, `--task`, and the aliases
`--dry-run` and `--scripted` for deterministic execution.

Agent and Architectus provider configuration comes from `CAMBIUM_PROVIDERS` when
set, otherwise from `<effective-home>/.config/cambium/providers.json`. A
relative `CAMBIUM_PROVIDERS` path is resolved from the current directory.
Target-repository provider files are not trusted automatically. Credential
values do not appear in argv, plans, results, events, or diagnostics.

For an API-key provider, `api_key_env` must be the derived name
`CAMBIUM_PROVIDER_<NORMALIZED_PROVIDER>_API_KEY`; a non-empty inherited value
or a matching auth-store entry makes the key available to agent runs. OAuth
login takes its client id from `--client-id` or `CAMBIUM_CODEX_CLIENT_ID`.
There is no client-id default: login without either source exits 1.

Each provider entry requires `name` and `tier` (`fast|balanced|strong|reasoning`);
absent `auth` and `protocol` fields default to `api_key` and
`chat_completions`. `auth: "api_key"` +
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
cambium auth run supervisor --session-dir DIR (--plan PATH | --task-spec PATH | --demo)
  [--warm-pool-size N] [--conversations]
```

`auth set` accepts only an enabled provider in the trusted provider config.
The config path follows `CAMBIUM_PROVIDERS` or the effective-home config path
above. Removal is idempotent: a missing credential prints `no change` and exits
0. OAuth status is local and does not refresh; it exits 1 when no session is
stored. Logout removes only the local session and exits 0 when it is already
absent. Import reads the Codex CLI session and stores it as provider `codex`.

The OAuth store is `<effective-user-home>/.local/share/cambium/oauth.json`,
using the same effective-home ownership and private-mode rules as the API-key
store. `auth oauth login` exits 130 when canceled or interrupted and 1 for
other device-flow failures. API-key auth failures exit 1; invalid input or
provider configuration exits 2.

The store is the fixed path `<effective-user-home>/.local/share/cambium/
auth.json` (the passwd home of the effective uid, not `HOME`). The directory
is `0700`, the file `0600`, and updates are atomic temp-file + rename under an
exclusive directory lock; symlinks, foreign owners, and wrong modes are
rejected. Keys are kept in memory only long enough to validate, serialize, or
build a launch environment; representations and exceptions never expose them.

For an API-key provider run, `oneshot._stored_provider_environment` builds an
in-memory mapping `{derived_env_name: key_value}`. The canonical environment
credential wins; otherwise the value is read from the `AuthStore`.
`run_oneshot` passes that mapping directly to
`supervisor.run_plan(..., provider_environment=...)`. OAuth access tokens are
handed to workers separately at spawn. Credentials are not part of the plan:
`plan.json` carries only the `provider_env_keys` names and the
`provider_config_path`. Key values never enter `plan.json`, `result.json`,
events, argv, logs, or diagnostics.

## Sessions

The default session root is `<cwd>/.cambium/sessions`; `--session-dir DIR`
overrides it for `list`, `latest`, `show`, `status`, and `usage`. `resume`
takes the session leaf path as its positional argument.

- `session list` prints completed session paths; completion is determined from
  a valid `.cambium/result.json`.
- `session latest` prints the latest completed session.
- `session show SESSION` reads `.cambium/result.json` and prints the stored
  result as JSON. It does not require `events.db`.
- `session status SESSION` requires `.cambium/events.db` and renders current
  task state from the event log.
- `session usage SESSION` requires the event log and renders token and
  estimated-cost totals by task and provider.
- `session resume SESSION` requires the leaf's persisted `plan.json` and invokes
  the supervisor resume path. Completed reconciled tasks remain skipped and
  interrupted tasks restart from their persisted bases.

Required-artifact read failures exit 1 with a `cambium session:` diagnostic.
`latest` also exits 1 when no completed session exists. An interrupted resume
exits 130.

One session leaf contains the immutable submission manifest `plan.json` (mode
`0600`, names never credential values), `.cambium/result.json`,
`.cambium/events.db`, `.cambium/session.lock`
(one live supervisor per session), and `wt/` (the worker worktree, removed
when clean after a terminal task).

## Supervisor

The unified `supervisor` command is session and supervision driven:

```sh
cambium supervisor --session-dir DIR (--plan PATH | --task-spec PATH | --demo)
  [--warm-pool-size N] [--conversations]
```

It does not select providers. The unified command delegates these same plan
inputs to the `cambium.supervisor` runtime. The direct
`python -m cambium.supervisor` entry remains a lower-level module interface;
it uses the same plan inputs and is used by session resume. `--conversations`
persists worker conversations in the `<session-dir>/.cambium/conversations.db`.
`--warm-pool-size` is a non-negative integer and defaults to `0` (disabled);
`CAMBIUM_WARM_POOL_SIZE` is not read.
The demo, plan, and task-spec modes return 0 when all tasks succeed and 1 when
a task fails; input/configuration errors return 2 and interruption returns 130.

## Architectus

```text
cambium architectus [--dry-run|--scripted]
  [--provider PROVIDER[:MODEL]] [--model [PROVIDER/]MODEL]
  [--tier TIER] [--waves N] [--task TASK]
```

`--dry-run` and `--scripted` are aliases for one deterministic step and require
no provider credentials. `--waves` defaults to `1`; `--task` defaults to
`Add a docstring to the build_tree function in src/cambium/tasktree.py`; and
`--tier` defaults to the selected provider's configured tier. A live API-key
call requires the selected provider's canonical API-key environment variable;
an OAuth provider uses its local OAuth session. Successful decisions exit 0,
provider/configuration errors exit 2, and decision-step failures exit 1.

## Diagnostics and tooling

### Doctor

```text
cambium doctor [--session-dir DIR] [--oauth-live]
```

`--session-dir` is optional and checks `<session-dir>/.cambium/events.db` and
`<session-dir>/.cambium/conversations.db` when they exist. Without
`CAMBIUM_PROVIDERS`, doctor checks `<cwd>/.cambium/providers.json`, falling
back to the shipped provider sample; this differs from agent provider
selection, which uses the trusted effective-home config. `--oauth-live` is
opt-in: it checks issuer reachability and, when `CAMBIUM_CODEX_CLIENT_ID` is
set, performs real refresh-token exchanges for configured Codex providers.
Without that variable, refresh is skipped with a warning. The probe can
consume quota but never makes a model call. Doctor exits 0 when no check fails
and 1 when any check fails.

### Bench

```text
cambium bench report
cambium bench gate
cambium bench re-anchor
cambium bench quality
```

Each mode also accepts `--full`, `--drift-report`, `--bench-root PATH`,
`--bench-metric-delta FLOAT`, and `--bench-wall-ratio FLOAT`. `--bench-root`
defaults to `.cambium/baselines/` for report, gate, and re-anchor, and to
`.cambium/quality-repo/` for quality. Report writes a baseline, gate checks
drift without changing its anchor, re-anchor records a new baseline over an
existing anchor, and quality measures the fixed task-success fixture. With no
provider credentials, quality skips and exits 0. The standalone metric-drop
threshold defaults to `0.05`; its wall threshold defaults to a ratio of `3.0`
plus `0.5` seconds of absolute slack. Successful runs exit 0; drift or a
tooling failure exits 1.

### Module test and version

```text
cambium module-test NAME
cambium version
```

`module-test` has no options besides `--help`; a conformance pass exits 0, a
conformance failure exits 1, and an unknown module or input error exits 2.
`version` has no arguments and exits 0 after printing the version.
