# Codex activation note

## Login

The exact device-flow command from the checkout is:

```sh
PYTHONPATH=src python3 -m cambium auth oauth login codex
```

The installed-console equivalent is `cambium auth oauth login codex`. The
optional `--client-id ID` (or `CAMBIUM_CODEX_CLIENT_ID`) is not needed: the
trusted Codex public client id is pinned in the provider profile. The command
prints the verification URL and one-time code only to the controlling TTY.
Open the URL, enter the code, and approve the ChatGPT subscription login.
On success it stores the secured OAuth record in the effective user's
`~/.local/share/cambium/oauth.json`; it does not put tokens in
`providers.json`.

If the Codex CLI is already logged in, the non-interactive alternative is:

```sh
PYTHONPATH=src python3 -m cambium auth oauth import-codex-cli
```

That imports `~/.codex/auth.json`. `import-codex-cli` is a subcommand (not an
`--import-codex-cli` option).

## Provider and eligibility

The trusted `codex` entry is enabled and loads as:

```text
auth=codex_chatgpt protocol=codex_responses model=gpt-5.6-luna reasoning_effort=max
```

Codex entries must not specify `base_url` or `api_key_env`; the pinned profile
owns the issuer and endpoint. `required` defaults to `false` and is doctor
metadata: a missing optional credential is a warning, while `required=true`
makes that doctor check fail. It is not the routing credential gate.

Normal one-shot cascade routing uses the following gates:

1. `oneshot._is_codex_oauth_provider` identifies the entry from
   `auth=codex_chatgpt`.
2. `oneshot._oauth_doc_present` performs a local OAuth-store read. A missing
   document makes the provider ineligible; a corrupt or insecure store raises
   rather than being hidden. Presence is checked here, not token freshness.
3. The authorized set includes only enabled providers with a ready credential,
   so an unauthenticated enabled Codex entry is skipped when other providers
   are available. Explicitly selecting `--provider codex` without a session
   instead fails with a clear unavailable-credential error.
4. Once a task references Codex, supervisor preflight requires an
   unexpired-or-refreshable, non-disabled OAuth record. `TokenManager` refreshes
   at worker spawn; only the access token and optional account id are injected.
   Diffundo receives those through `CredentialSource`; it fails closed if that
   source is absent or empty. The refresh token never enters the worker.

## Verification after login

These checks do not print OAuth material:

```sh
PYTHONPATH=src python3 -m cambium auth oauth status codex
CAMBIUM_PROVIDERS="$HOME/.config/cambium/providers.json" \
  PYTHONPATH=src python3 -m cambium doctor
```

Use `--oauth-live` only when an issuer reachability and refresh probe is
wanted; it performs a real refresh and can consume quota:

```sh
CAMBIUM_PROVIDERS="$HOME/.config/cambium/providers.json" \
  PYTHONPATH=src python3 -m cambium doctor --oauth-live
```

With the trusted config, the expected local result is provider-env showing
`codex(model=gpt-5.6-luna)=set`, and provider-runnable listing `codex` among
the runnable providers. Other local warnings may remain. In this environment
the overall doctor exit is also affected by the host Python-version check and
an unrelated secrets-hygiene warning; those are not Codex activation failures.

## Acceptance expectations

Offline, before enabling mutation, the requested check passed as skips:

```text
PYTHONPATH=src python3 -m pytest tests/acceptance -q -k "codex"
8 skipped, 0 failed
```

The eight Codex cases are fresh login, valid stored token, expired-access
refresh, rotated refresh, revoked refresh, concurrent child startup,
account-id propagation, and restart/reuse. The disposable fixture is inactive
unless `CAMBIUM_ACCEPTANCE_ALLOW_MUTATION=1` (or the legacy
`CAMBIUM_ACCEPTANCE_ALLOW_OAUTH_MUTATIONS=1`) is set. With that guard, the
seven non-fresh cases can run after a usable read-only source and the required
config/provider variables are supplied. Fresh-login additionally requires an
operator-supplied `CAMBIUM_ACCEPTANCE_CODEX_LOGIN_COMMAND` that writes the
new record to the disposable target; a normal login to the production store
does not satisfy that test. All such cases make live requests and mutation
cases can rotate or disable disposable-account state.

## Rollback

To remove Codex from normal routing while retaining the OAuth record, change
only the trusted provider entry's flag back to:

```json
"enabled": false
```

The local session can be removed separately with
`PYTHONPATH=src python3 -m cambium auth oauth logout codex`; this removes only
the local record and does not claim to revoke the issuer session.
