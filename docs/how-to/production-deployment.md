# Deploy Cambium to production

Use an immutable wheel, a non-secret provider configuration, and a disposable
canary before admitting production work. Keep OAuth state, API keys and other
secrets outside Git and outside the deployment artifact.

## Build and install

Build in a clean release checkout, then publish the wheel through the normal
artifact channel. Install that exact wheel in the production virtual
environment; do not run production from a source checkout.

```sh
python -m venv .venv
. .venv/bin/activate
python -m pip install --upgrade pip build
python -m build --wheel --outdir dist
python -m pip install --force-reinstall dist/<wheel-file>
cambium version
cambium --help
```

The package requires Python 3.12 or newer. Keep the wheel filename and its
artifact metadata with the deployment record so rollback can reinstall the
known-good build.

## Configure the Codex provider

Point the installed process at a provider file managed by the deployment
system. The file contains routing metadata only. For the production Codex
profile, its non-secret entry is:

```json
{
  "providers": [
    {
      "name": "codex",
      "tier": "reasoning",
      "model": "gpt-5.6-luna",
      "auth": "codex_chatgpt",
      "protocol": "codex_responses",
      "reasoning_effort": "max",
      "supports_native_tools": true,
      "enabled": true,
      "required": true,
      "priority": 0
    }
  ]
}
```

Set the same path in the service environment. The default path is
`$HOME/.config/cambium/providers.json`.

```sh
export CAMBIUM_PROVIDERS="$HOME/.config/cambium/providers.json"
chmod 600 "$CAMBIUM_PROVIDERS"
```

Do not add `base_url`, `api_key_env` or a token to a `codex_chatgpt` entry.
Cambium pins the Codex endpoint and OAuth flow for that auth mode. The native
tool declaration belongs in this provider configuration; it is not a command
line option.

Import an existing Codex CLI session under the same operating-system account
that will run Cambium:

```sh
cambium auth oauth import-codex-cli
cambium auth oauth status codex
```

If no Codex CLI session exists, use the device flow instead:

```sh
cambium auth oauth login codex
```

Neither command puts a token in an argument. Do not print, copy or commit the
OAuth store. For an explicitly approved API-key provider, feed the key through
protected stdin instead of an argument or a config value:

```sh
cambium auth set PROVIDER --stdin
```

## Preflight

Run the local checks first. A live OAuth check performs a real refresh-token
exchange, can consume quota, and never makes a model call.

```sh
cambium doctor
cambium doctor --oauth-live
```

Require no `FAIL` result before launch. Resolve warnings that affect the
deployment rather than treating them as proof that a provider is runnable.
Run the live check again after a credential or provider-file change.

## Run a disposable canary

Never use a production checkout for the first model call. Create a temporary
Git repository with one harmless read-only task:

```sh
CANARY_ROOT="$(mktemp -d)"
mkdir -p "$CANARY_ROOT/repo"
git -C "$CANARY_ROOT/repo" init
git -C "$CANARY_ROOT/repo" config user.name "Cambium canary"
git -C "$CANARY_ROOT/repo" config user.email "cambium-canary@invalid"
printf '%s\n' '# Cambium canary' > "$CANARY_ROOT/repo/README.md"
git -C "$CANARY_ROOT/repo" add README.md
git -C "$CANARY_ROOT/repo" commit -m "Seed canary"

cambium run \
  --repo "$CANARY_ROOT/repo" \
  --session-dir "$CANARY_ROOT/session" \
  --provider codex:gpt-5.6-luna \
  --max-wall-s 120 \
  --max-tokens 4000 \
  --max-turns 6 \
  --max-workers 1 \
  --json \
  "Read README.md and report its title. Do not modify files."
```

Stop if the canary exits non-zero or changes the canary checkout. Inspect the
persisted result and event store, not only the command's stdout:

```sh
export CANARY_SESSION="$CANARY_ROOT/session"
python -m json.tool "$CANARY_SESSION/.cambium/result.json"
cambium session show "$CANARY_SESSION"
cambium session status "$CANARY_SESSION"
cambium session usage "$CANARY_SESSION"
cambium monitor "$CANARY_SESSION" --once --json
cambium doctor --session-dir "$CANARY_SESSION"
```

The canonical root result is `.cambium/result.json`. Check its `status`,
`exit_code`, `session_id`, `provider`, `fell_back_from` and `event_log_ref`.
The `event_log_ref` must be a `sqlite:` reference to that session's
`.cambium/events.db`; do not substitute a worker or child result. The durable
event kinds and their meanings are listed in the [event glossary](../architecture/events.md).
At minimum, inspect the assignment, usage, terminal result and session-ended
events before promoting the deployment.

## Monitor and roll back

Use the monitor during a live session and keep the session directory for
incident review. `--once --json` is a machine-readable snapshot; omit `--once`
for a continuous operator view.

```sh
cambium monitor SESSION --once --json
cambium session status SESSION
cambium session usage SESSION
```

Alert on failed doctor checks, non-zero session results, missing event stores,
provider boundary failures, and an unexpected non-null `fell_back_from`.

For rollback:

1. Stop new admissions with the deployment's process manager and let active
   sessions finish or cancel them according to the service policy.
2. Preserve the affected session directories and their `.cambium` artifacts.
3. Reinstall the previously approved wheel with `python -m pip install
   --force-reinstall <known-good-wheel-file>`.
4. Restore the last approved provider file from the secret-managed deployment
   store. Do not restore credentials from Git or shell history.
5. Run `cambium doctor`, `cambium doctor --oauth-live`, and the disposable
   canary again before admitting work.

Do not roll back OAuth by copying token files. If the OAuth record is invalid,
reauthenticate with `cambium auth oauth import-codex-cli` or the device flow.

## Make fallbacks explicit

An enabled provider is eligible for runtime fallback. A single-entry Codex
configuration therefore makes the no-fallback policy visible. Pinning the
initial canary with `--provider codex:gpt-5.6-luna` does not authorize an
undeclared provider.

If a fallback is approved, add it as a separate provider entry with its own
name, model, auth mode, protocol, capability declarations and credential
source. Provision its API key with `cambium auth set PROVIDER --stdin` or its
own OAuth flow, and test the fallback as a separate canary. Record both the
serving `provider` and `fell_back_from` fields from the canonical result. Do not
depend on the shipped sample, provider order, or a missing credential to choose
the fallback policy.
