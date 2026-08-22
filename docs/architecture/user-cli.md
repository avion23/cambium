# Cambium CLI

Source and scenario tests are authoritative.

## Coding sessions

```sh
cambium run PROMPT [--repo PATH] [--provider NAME[:MODEL]]
                   [--model [NAME/]MODEL] [--auto]
                   [--max-wall-s N] [--max-tokens N] [--max-turns N]
                   [--session-dir DIR] [--json]

cambium repl [the same routing and budget options]
cambium tui  [the same routing and budget options] [--quiet]
```

`run` executes one prompt. `repl` and `tui` accept multiple prompts, each as a
separate durable one-shot session leaf. On a TTY, `tui` displays the live
operator dashboard while the current run is active.

## Monitoring

```sh
cambium monitor [SESSION] [--repo PATH] [--interval SECONDS] [--once] [--json]
cambium-monitor [the same arguments]
```

Without `SESSION`, discovery checks `CAMBIUM_SESSION_ID` and then the newest
repository-local session. `--once` prints one frame. `--json` emits the exact
reducer snapshot. A monitor is read-only and never cancels a session.

## Session inspection and recovery

```sh
cambium session list   [--session-dir ROOT]
cambium session latest [--session-dir ROOT]
cambium session show   [--session-dir ROOT] SESSION
cambium session status [--session-dir ROOT] SESSION
cambium session usage  [--session-dir ROOT] SESSION
cambium session resume SESSION
```

`status` and `usage` read the durable event database. `resume` requires the
persisted `plan.json`.

## DSPy

```sh
cambium optimize MODULE [--optimizer zero|bootstrap]
                        [--budget-usd N] [--seed N] [--tier TIER]
                        [--dry-run]
                        [--include-transcript-candidates |
                         --transcript-candidates PATH]
```

Transcript candidates must pass the explicit approval/redaction gate described
in `optimization.md`.

## Authentication

```sh
cambium auth set PROVIDER [--stdin]
cambium auth remove PROVIDER
cambium auth list
cambium auth oauth login PROVIDER [--client-id ID]
cambium auth oauth status PROVIDER
cambium auth oauth logout PROVIDER
cambium auth oauth import-codex-cli
```

The Codex public OAuth client identifier is pinned. `--client-id` and
`CAMBIUM_CODEX_CLIENT_ID` override it; they are not required for the normal
profile. Refresh tokens remain in the supervisor-owned OAuth store and are
never injected into workers.

## Supervisor and diagnostics

```sh
cambium supervisor --session-dir DIR (--plan PATH | --task-spec PATH | --demo)
                   [--warm-pool-size N] [--conversations]
cambium doctor [--session-dir DIR] [--oauth-live]
cambium bench {report,gate,re-anchor,quality} [...]
cambium module-test NAME
cambium architectus [...]
cambium version
```

Provider config comes from the trusted user path
`~/.config/cambium/providers.json` or explicit `CAMBIUM_PROVIDERS`; target
repositories cannot supply it implicitly.
