# Cambium user CLI

**Status:** current behavior. Source (`src/cambium/cli.py`, `oneshot.py`,
`repl.py`, `tui.py`, `session.py`, `auth.py`, `provider_config.py`) and the
scenario tests (`tests/scenarios/test_user_cli.py`, `test_cli.py`,
`test_auth.py`, `test_provider_selection.py`) are the authority for this
document. It covers only the implemented workflows, not targets.

## 1. Entry point and dispatch

`cambium.cli.main` is the single entry point. The parser exposes the fixed
command set `auth`, `supervisor`, `doctor`, `bench`, `tasktree`,
`module-test`, `version`, `run`, `repl`, `tui`, `session` (`session` further
requires `list`, `latest`, or `show`). The CLI runs directly from source with
the system interpreter:

```sh
PYTHONPATH=src python3.14 -m cambium.cli --help
```

Two dispatch rules precede normal parsing (`cli.main`):

1. `tasktree` delegates to `cambium.tasktree.main` before the argument parser.
2. A bare multi-word prompt dispatches to the `run` path (see §3).

The parser is `_SafeArgumentParser`, which rejects a `--` separator for `run`
and rewrites "unrecognized arguments" and "invalid choice" errors to
`invalid command arguments` without echoing the rejected tokens, so a token
that may be a credential never appears in diagnostics.

## 2. `cambium run`

```sh
PYTHONPATH=src python3.14 -m cambium.cli run --repo PATH \
    [--session-dir DIR] [--provider PROVIDER] [--model MODEL] \
    [--auto] [--max-wall-s SECONDS] [--max-tokens N] [--max-turns N] \
    [--json] PROMPT
```

Options:

- `--repo PATH` — repository to work in, default `.` (current directory).
  The path is expanded and resolved; `oneshot.preflight` requires it to be an
  existing directory whose `.git` exists and whose `refs/heads/main` resolves.
- `--session-dir DIR` — a fresh concrete session leaf. The leaf is used as-is
  and must not already contain run artifacts: `run_oneshot` rejects a leaf
  that already has `plan.json`, `.cambium/events.db`, or
  `.cambium/result.json` (`one-shot session directory has already been
  used`). There is no one-shot resume: a used or interrupted leaf is rejected,
  never continued. When omitted, a fresh leaf named `run-*` is created with
  mode `0700` under `<repo>/.cambium/sessions/` (one leaf per run; two
  default runs never share a directory).
- `--provider` / `--model` — select one configured provider (see §7).
  Provider ids are validated against `[a-z0-9](?:[a-z0-9._-]{0,62}[a-z0-9])?`.
- `--auto` — route the run through the usage-debt selector (solution C): the
  supervisor picks `(provider, model, tier)` from all enabled configured
  providers with stored credentials instead of pinning `--provider`/`--model`.
- `--max-wall-s SECONDS` — per-task wall-clock budget in seconds (default
  `300`, matching `cambium run --help` at this commit).
- `--max-tokens N` — total token budget across the run (default `200000`).
- `--max-turns N` — maximum agent-loop turns (default `20` at this commit;
  `cambium run --help` reports the same).
- `--json` — print the rendered result as JSON instead of the text line.

Pipeline (`oneshot.run_oneshot`): preflight the repo and prompt, resolve the
provider (§7), allocate or accept the session dir, build one task
(`task_id` `oneshot`, worktree `<session>/wt`, branch
`cambium-oneshot-<sha256(session_dir)[:16]>` — the first 16 hex digits of the
SHA-256 of the resolved session-directory path), and
call `supervisor.run_plan(session_dir, plan, provider_environment=...)`. The
accepted plan is written atomically as `<session>/plan.json` (mode `0600`)
before any worker starts; the event log is `.cambium/events.db`; the canonical
root result is `.cambium/result.json`.

Each prompt runs in a fresh, isolated worktree copy of the repository: the
supervisor creates `<session>/wt` with `git worktree add -b
cambium-oneshot-<sha256(session_dir)[:16]>` at the repo's `refs/heads/main`
base commit. `git diff` inside that worktree therefore shows only the agent's
changes, never the user's checkout state (uncommitted edits, a different
branch, or a moved HEAD).

The result is rendered by `cambium.render` from the supervisor `PlanResult`:
text output is one line such as `plan=tasks:1 plan_status={succeeded}`, JSON
output is the filtered `{"results": [...]}` record; both carry the worker
`summary` when present, including a successful conversational/read-only run
with no commits. Exit codes: `0` success,
`2` for config/preflight errors (`cambium run: <message>`), `75` when the
session admission lock is already held by another live supervisor
(`cambium run: session is already running: ...`, a temporary failure callers
may retry), `130` on `KeyboardInterrupt`, otherwise the result exit code.

Without `--provider`/`--model`, the run still resolves a provider: it
auto-selects the first enabled provider from the trusted user config (see
§7). The marker task (`target_file` plus `marker`) is an internal
`OneShotConfig` path only; the CLI does not expose those fields, so a plain
run never becomes a marker task. Provider runs use the bounded Diffundo
worker loop. A provider run may complete successfully with a
conversational/read-only answer and no file/commit: when the agent changed no
files, no empty commit or merge occurs and nothing is published, and the
rendered output still carries the summary. A run that changed files commits
once and merges normally.

## 3. Bare multi-word prompt

A command line whose first token is not a known command, not an option, and
either contains whitespace (a quoted sentence) or has more than one non-option
token among its first two tokens is treated as a prompt
(`cli._bare_prompt_allowed`). `_run_bare_prompt` joins the leading non-`--`
tokens into one prompt and parses everything from the first `--` token onward
as `run` arguments:

```sh
PYTHONPATH=src python3.14 -m cambium.cli make the change
# equivalent to: cambium run "make the change"
```

A single unknown token (`cambium not-a-command`) is not a prompt; the parser
exits `2` with `invalid command arguments`. Known command names are never
reinterpreted as prompts.

## 4. `cambium repl`

`repl.run_repl` reads lines from stdin. Empty lines are skipped; `/exit`
stops the loop; every other line is a prompt. Each prompt runs through
`oneshot.run_oneshot` with a fresh immutable config
(`dataclasses.replace(config, prompt=...)`), so a failed or mutated run never
changes the shared config. Each prompt also gets its own fresh session leaf
and therefore its own isolated worktree copy of the repo (§2), so a prompt
never works against another prompt's or the user's checkout. Each result is
printed as one rendered text line.
The exit code is `1` if any result failed (a nonzero result exit code or a
per-prompt exception), otherwise `0`; per-prompt failures print
`repl: <error>` to stderr and continue. A prompt answered conversationally
with no file/commit is a successful result (exit `0`), not a failure.

## 5. `cambium tui`

`tui.run_tui` is the same one-shot-per-line loop with a `cambium> ` prompt
written and flushed before each read. Blank lines are skipped; per-prompt
errors print `cambium: <error>` to stderr and continue. Exit codes: `0` on
EOF when every prompt succeeded, `1` on EOF if any result failed, `0` on
`BrokenPipeError`, `130` on `KeyboardInterrupt`, `1` if the backend modules
cannot be imported. A prompt answered conversationally with no file/commit
counts as a successful prompt (exit `0`), not a failure.

## 6. `cambium session list/latest/show`

Read-only views over completed sessions. The session root is
`--session-dir DIR` when given (expanded and resolved), otherwise
`session.session_root(Path.cwd())` = `<cwd>/.cambium/sessions` — the same root
that default `run` leaves are created under.

- `list` prints one resolved path per completed session. A directory is
  completed when its `.cambium/result.json` parses to a JSON object.
  Ordering is deterministic ascending `(ended_at, started_at, name)` read from
  each result record.
- `latest` prints the last entry of `list`; with no completed sessions it
  prints `cambium session: no completed sessions under <root>` to stderr and
  exits `1`.
- `show SESSION` reads one session's `.cambium/result.json` (must be a JSON
  object) and requires its `.cambium/events.db` artifact to exist (the event
  log is not materialized into the view; readers that need it stream it
  through `cambium.supervisor.read_events`). The view's JSON result is printed.
  `SESSION` may be an absolute path or a name resolved under the root. Missing
  or malformed artifacts print `cambium session: <error>` to stderr and exit
  `1`.

`session.py` never creates or opens artifacts for writing.

## 7. Provider selection and credential handoff

### Provider configuration selection

`oneshot._provider_config_path` resolves the provider file in this order:
`config.provider_config_path` (the library override, not exposed by the CLI),
then the `CAMBIUM_PROVIDERS` environment variable, then the trusted user
config `<effective-user-home>/.config/cambium/providers.json`. Relative paths
resolve against the current directory. The target repository's
`.cambium/providers.json` is never consulted: a default run auto-selects from
the trusted user config, and a repo-local provider file is not implicitly
trusted. `CAMBIUM_PROVIDERS` and the library `provider_config_path` are the
intentional overrides. The file is one JSON
object with a `providers` list; each entry has the required fields `name`,
`tier` (`fast|balanced|strong|reasoning`), `base_url` (http(s); plaintext http
only for loopback hosts), and `api_key_env` — which must equal the derived
canonical name `CAMBIUM_PROVIDER_<PROVIDER>_API_KEY`. Unknown fields,
duplicate names, colliding env names, and invalid values are rejected by
`provider_config.load_providers`. The file contains environment-variable
names only, never key values.

`select_provider` is a pure decision: an explicit `name` wins (it must be
enabled); otherwise the first enabled provider by ascending `priority` order
— optionally restricted by `model` when only `--model` was given. It never
reads the environment or a key value.

### `cambium auth`

- `cambium auth set PROVIDER` prompts for the key with `getpass`.
- `cambium auth set PROVIDER --stdin` reads the key from stdin's binary
  stream, stripping one conventional `\r\n`/`\n` line ending; the key never
  appears in argv.
- `cambium auth list` prints `provider<TAB>derived-env-name` per stored
  provider, never a key value.
- `cambium auth remove PROVIDER` deletes one stored key.
- `cambium auth run supervisor --session-dir DIR` execs
  `cambium.supervisor` with a scrubbed environment containing only the stored
  provider keys; keys travel in the environment, never in argv.

The store is the fixed path `<effective-user-home>/.local/share/cambium/
auth.json` (the passwd home of the effective uid, not `HOME`). The directory
is `0700`, the file `0600` with link count `1`, and updates are atomic
temp-file + `rename` under an exclusive directory lock; symlinks, foreign
owners, and wrong modes are rejected. Each key is kept in memory only long
enough to validate, serialize, or build a launch environment; representations
and exceptions never expose it.

### In-memory handoff, never in `plan.json`

For a provider run, `oneshot._stored_provider_environment` builds an in-memory
mapping `{derived_env_name: key_value}`. The canonical environment credential
wins: when the derived `CAMBIUM_PROVIDER_<PROVIDER>_API_KEY` variable is
already set in the process environment, that value is used; otherwise the
value is read from the `AuthStore`. `run_oneshot` passes that mapping directly
to `supervisor.run_plan(..., provider_environment=...)`. It is not part of the
plan: `plan.json` carries only the `provider_env_keys` names and the
`provider_config_path`; `_write_plan` writes it with mode `0600`. The
supervisor forwards only the declared names' values into each worker
subprocess environment (`_worker_environment`), and the session redactor
registers every forwarded value so it is redacted from durable events. Key
values never enter `plan.json`, `result.json`, events, argv, logs, or
diagnostics.

## 8. `cambium supervisor`

```sh
PYTHONPATH=src python3.14 -m cambium.cli supervisor --session-dir DIR \
    [--plan PATH | --task-spec PATH]
```

`--session-dir` is required and names the concrete session leaf. Without
`--plan` or `--task-spec`, the supervisor runs its built-in deterministic demo:
one marker worker (`task_id` `demo-001`) against a seeded repository under
`DIR/scratch` that appends a marker line to `hello.txt`. `--plan` is passed as
a plan when the installed supervisor exposes `run_plan`, otherwise as a
task-spec (compatibility with the slice supervisor). Publication advances the
working repository's `refs/heads/main` only; it does not refresh a checkout.

## 9. Session artifacts

One session leaf contains:

- `plan.json` — accepted plan, mode `0600` (names, never credential values).
- `.cambium/result.json` — canonical 15-field root result
  (`cambium.results.Result`).
- `.cambium/events.db` — durable redacted event log.
- `.cambium/session.lock` — admission lock (one live supervisor per session).
- `wt/` — the worker worktree; removed when clean after a terminal task.

Default `run` sessions are leaves under `<repo>/.cambium/sessions/`; the
`supervisor` command and an explicit `run --session-dir` write the leaf you
name. `session list/latest/show` enumerate the former root and require the
leaf directory for `show`.
