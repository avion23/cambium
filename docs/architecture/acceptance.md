# Live-provider acceptance

The acceptance suite is deliberately opt-in. It exercises real provider
accounts and can spend quota; it never invents a token and it never treats a
loopback fixture as external evidence.

## One command

From the repository root, after exporting the inputs below, run:

```sh
PYTHONPATH=src python -m pytest -m acceptance tests/acceptance -v -s
```

`-s` is needed only when the operator-supplied fresh-login command displays a
device code. A CI-less checkout with no acceptance environment runs the same
collection as skips:

```text
13 skipped, 0 failed
```

The suite is also included by an ordinary pytest collection, but the project
default still excludes only `slow`; use `-m acceptance` when a live run is
intended.

## Prerequisites

- Python and the test extra from `pyproject.toml` (`pytest`, the AST
  dependencies, and the optional DSPy dependency used by the broader suite).
- Network access to the real provider endpoints and permission to spend the
  selected account's quota.
- A validated `providers.json` for every provider under test. API-key entries
  must use their canonical `CAMBIUM_PROVIDER_<NAME>_API_KEY` environment name.
  Codex entries must use `auth: "codex_chatgpt"` and
  `protocol: "codex_responses"`; the pinned profile supplies the endpoint.
- An account-owned OAuth store for Codex. The store paths are explicit inputs
  so the suite never silently reads the developer's default store.
- A disposable account/store for every test that can refresh, rotate, disable,
  or create a session. Refresh-token rotation and revocation are provider
  state changes, not simulated test fixtures.

The suite reads token values only from the supplied OAuth stores and reads API
keys only from the `api_key_env` names in the supplied provider configuration.
Do not put a token or API key in JSON, a command argument, a commit, or a
shell history entry. Keep provider config files free of secret values and use
owner-only permissions for credential files and directories.

## Environment contract

Unset variables cause only the checks that need them to skip. A variable that
is set but names an invalid file or invalid provider configuration is a test
failure, so a typo cannot be mistaken for evidence.

### Codex OAuth

All Codex checks require these two routing inputs:

```sh
export CAMBIUM_ACCEPTANCE_CODEX_CONFIG=/absolute/path/providers.json
export CAMBIUM_ACCEPTANCE_CODEX_PROVIDER=codex
```

The following store variables are intentionally separate. Each must point at
a store containing the real account state named by the test:

| Check | Store variable |
| --- | --- |
| valid stored token, account-id propagation | `CAMBIUM_ACCEPTANCE_CODEX_OAUTH_STORE` |
| expired access plus valid refresh | `CAMBIUM_ACCEPTANCE_CODEX_EXPIRED_STORE` |
| rotated refresh | `CAMBIUM_ACCEPTANCE_CODEX_ROTATED_STORE` |
| revoked refresh | `CAMBIUM_ACCEPTANCE_CODEX_REVOKED_STORE` |
| concurrent child startup | `CAMBIUM_ACCEPTANCE_CODEX_CONCURRENT_STORE` |
| restart and reuse | `CAMBIUM_ACCEPTANCE_CODEX_RESTART_STORE` |
| fresh login output | `CAMBIUM_ACCEPTANCE_CODEX_FRESH_STORE` |

Mutation cases additionally require this deliberate confirmation:

```sh
export CAMBIUM_ACCEPTANCE_ALLOW_OAUTH_MUTATIONS=1
```

This is a guard, not a credential. The optional
`CAMBIUM_ACCEPTANCE_CODEX_CLIENT_ID` selects an explicitly supplied public
client id; when absent, the trusted pinned client id is used.

### Fresh login

Fresh login requires an operator-supplied executable command:

```sh
export CAMBIUM_ACCEPTANCE_CODEX_LOGIN_COMMAND='python /absolute/path/codex-login-wrapper.py'
```

The command is parsed as an argument vector, not as a shell pipeline. It must
perform a real device login and write the resulting record to the path in
`CAMBIUM_ACCEPTANCE_CODEX_FRESH_STORE`. A small wrapper outside the checkout
can construct `DeviceFlow` with `OAuthStore(Path(os.environ[...]))`; it should
display only the verification URL and one-time device code on the controlling
TTY. It must not print access tokens, refresh tokens, or the OAuth document.
The wrapper may use `CAMBIUM_ACCEPTANCE_CODEX_PROVIDER` and
`CAMBIUM_ACCEPTANCE_CODEX_CLIENT_ID`. The test refuses an existing target and
refuses the production default store, which prevents an accidental overwrite.

For the refresh matrix, prepare each disposable store through the provider's
normal account flow. An expired-store document may retain the real access and
refresh values while carrying an expired `expires_at`; it must not contain
hand-written token literals. A revoked-store document must contain a refresh
token the provider has actually revoked. The harness itself never truncates,
edits, or fabricates a credential to manufacture a failure.

### API-key providers and quota state

The skeleton checks use one explicit config/provider pair per provider family:

| Check | Config variable | Provider variable | Additional requirement |
| --- | --- | --- | --- |
| z.ai rolling windows | `CAMBIUM_ACCEPTANCE_ZAI_CONFIG` | `CAMBIUM_ACCEPTANCE_ZAI_PROVIDER` | entry name contains `zai`; non-empty `quota_windows`; `CAMBIUM_ACCEPTANCE_QUOTA_DB` |
| OpenRouter paid | `CAMBIUM_ACCEPTANCE_OPENROUTER_CONFIG` | `CAMBIUM_ACCEPTANCE_OPENROUTER_PAID_PROVIDER` | `billing_mode` is `metered` or `subscription` |
| OpenRouter free | `CAMBIUM_ACCEPTANCE_OPENROUTER_CONFIG` | `CAMBIUM_ACCEPTANCE_OPENROUTER_FREE_PROVIDER` | `billing_mode` is `free` |
| OpenCode Zen | `CAMBIUM_ACCEPTANCE_OPENCODE_ZEN_CONFIG` | `CAMBIUM_ACCEPTANCE_OPENCODE_ZEN_PROVIDER` | entry name contains `opencode` |
| provider cache tokens | `CAMBIUM_ACCEPTANCE_CACHE_CONFIG` | `CAMBIUM_ACCEPTANCE_CACHE_PROVIDER` | explicit `price_per_1m_cached_in` and `pricing_known: true` |

For each API-key entry, export the exact key environment variable named by
that entry's `api_key_env`; the test checks presence without printing its
value. `CAMBIUM_ACCEPTANCE_QUOTA_DB` must be a disposable path when a config
declares `quota_windows`; it is passed to Cambium as `CAMBIUM_QUOTA_DB` so the
probe does not write a developer's normal quota ledger.

## Checks and expected evidence

Each row below is one `@pytest.mark.acceptance` test. A passing live run is
evidence only for the account, provider, model, and configuration supplied to
that invocation.

| Test | What it does | Expected evidence |
| --- | --- | --- |
| `test_codex_fresh_login` | Runs the real device login, validates the new store, and sends one Codex request. | A new secure OAuth record, a successful real response, and usage with the configured provider/model. |
| `test_codex_valid_stored_token` | Ensures a fresh stored access token and sends one request. | The store bytes and access token remain unchanged; the real response reports provider/model/usage. |
| `test_codex_expired_access_with_valid_refresh` | Refreshes an expired access token through the real issuer, then sends one request. | A new unexpired access token, a fresh expiry, and a successful provider response. |
| `test_codex_rotated_refresh` | Refreshes a disposable account whose issuer rotates refresh tokens. | Both access and refresh values in the stored record change and the new expiry is usable. |
| `test_codex_revoked_refresh` | Presents an actually revoked refresh token. | The issuer rejects it and Cambium durably marks that provider record disabled. |
| `test_codex_concurrent_child_startup` | Starts two independent Python child processes against one expired store. | Both children succeed; the final store is one fresh usable record, with refresh serialized by the provider lock. |
| `test_codex_account_id_propagation` | Builds the real supervisor worker environment and sends one request with its injected credential. | The account-id environment field matches the stored account id, the refresh token is absent, and the provider request succeeds. |
| `test_codex_restart_and_reuse` | Starts, exits, and starts a separate child again with a valid store. | Both independent starts reuse the same access record and the store bytes remain unchanged. |
| `test_zai_rolling_windows` | Sends one z.ai request through a config with rolling quota windows. | The result includes future reset times and quota-window snapshots for every configured window. |
| `test_openrouter_paid` | Sends one OpenRouter request with paid-only routing requirements. | The configured paid lane serves and reports provider/model/usage. |
| `test_openrouter_free` | Sends one OpenRouter request with free-only routing requirements. | The configured free lane serves and reports provider/model/usage. |
| `test_opencode_zen` | Sends one OpenCode Zen request using the configured API-key lane. | The real response reports the configured provider/model and usage. |
| `test_provider_reported_cache_tokens` | Sends the same prompt twice when cached pricing is explicitly represented in config. | The second response reports a recognized cache-token field (positive hit or explicit zero miss) and a boolean cache-hit classification. |

The tests intentionally do not claim that a missing cache field is a cache
miss, and they do not convert a local prefix length into provider cache
evidence. Provider-reported usage is the authority.

## Safe evidence handling

Keep the pytest result, configured provider/model names, timestamps, and
redacted session or provider dashboards as the acceptance record. Do not save
the environment dump, command-line transcript containing credentials, OAuth
JSON, response bodies, authorization headers, or raw account identifiers. The
suite's assertions are designed to report names, booleans, counts, and state
transitions rather than token values.
