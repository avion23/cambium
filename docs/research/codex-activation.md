# Codex OAuth activation

Use Cambium's OAuth commands rather than storing Codex tokens in provider
configuration.

## Login or import an existing Codex session

```sh
PYTHONPATH=src python -m cambium auth oauth login codex
```

The installed-console equivalent is `cambium auth oauth login codex`. The
command prints the verification URL and one-time code to the controlling TTY
and stores the resulting OAuth record in Cambium's secured auth store.

If the Codex CLI is already logged in, import that local session instead:

```sh
PYTHONPATH=src python -m cambium auth oauth import-codex-cli
```

`import-codex-cli` is a subcommand, not an option to `login`.

## Routing contract

A trusted Codex provider uses `auth=codex_chatgpt`; the trusted profile owns its
issuer and endpoint. Do not add a custom `base_url` or `api_key_env` to that
entry.

Normal automatic routing skips an enabled optional Codex provider when no usable
OAuth record exists. Explicitly selecting that unavailable provider fails with a
credential error instead of silently pretending it is runnable. At worker spawn,
the supervisor refreshes an eligible record when required and injects only the
worker credential material; refresh tokens remain outside the worker process.

## Verify without exposing credentials

```sh
PYTHONPATH=src python -m cambium auth oauth status codex
CAMBIUM_PROVIDERS="$HOME/.config/cambium/providers.json" \
  PYTHONPATH=src python -m cambium doctor
```

Use `--oauth-live` only when a real issuer reachability/refresh probe is needed:

```sh
CAMBIUM_PROVIDERS="$HOME/.config/cambium/providers.json" \
  PYTHONPATH=src python -m cambium doctor --oauth-live
```

That probe can refresh account state and consume quota. Ordinary `doctor` is the
preferred local configuration check.

## Disable or remove local access

Disable the trusted provider entry to keep the local OAuth record while removing
Codex from routing. To remove only Cambium's local Codex OAuth record, run:

```sh
PYTHONPATH=src python -m cambium auth oauth logout codex
```

This removes local state; it does not claim to revoke the issuer-side session.
