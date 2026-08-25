# Cambium CLI

Source and scenario tests are authoritative.

## Coding sessions

```sh
cambium run PROMPT [--repo PATH] [--provider NAME[:MODEL]]
                   [--model [NAME/]MODEL] [--auto]
                   [--max-wall-s N] [--max-tokens N] [--max-turns N]
                   [--session-dir DIR] [--json]

cambium repl [the same routing and budget options]
cambium tui  [the same routing and budget options]
             [-c [SESSION]] [--quiet]
```

`run` executes one prompt. With no explicit `--session-dir`, `repl` starts
each submitted prompt in a fresh durable one-shot leaf. `tui` accepts multiple
prompts on one persistent interactive branch, with each turn in its own
durable leaf; it allocates a fresh interactive root by default. On a TTY, the
TUI displays the live operator cockpit while the current turn is active.
`-c [SESSION]` / `--continue [SESSION]` explicitly continues the newest
reconnectable session when `SESSION` is omitted, or the named session when it
is supplied. A missing or non-reconnectable target is an error, and the flag
cannot be combined with `--session-dir`. Inside a running TUI, `/new` remains
unchanged: it starts a fresh semantic branch while retaining old artifacts.

The TTY cockpit puts the conversation pane above a live status pane showing
provider/model, turn, tokens, cost, agents, tool-error counters, and the
checkpoint, followed by an input row. The status activity uses `WAITING`,
`STREAMING`, and `IDLE` (with terminal `DONE`/`ERROR` results). Conversation
Markdown renders headings, tables, bold text, and fenced code blocks; routine
tool failures collapse into per-turn counters. Short terminals use a
width-bounded, unframed conversation/status fallback instead of the fixed
two-pane frame.

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
cambium optimize MODULE [--optimizer zero|bootstrap|gepa]
                        [--budget-usd N] [--seed N] [--tier TIER]
                        [--dry-run]
                        [--include-transcript-candidates |
                         --transcript-candidates PATH]

cambium optimize eval MODULE --dataset PATH
                        [--program-dir PATH] [--budget-usd N]
                        [--tier TIER] [--json]
```

Transcript candidates must pass the explicit approval/redaction gate described
in `optimization.md`. The `eval` form evaluates every dataset split; without
`--program-dir`, it checks `optimized/<MODULE>/program.json` and otherwise
uses a fresh program. Its JSON report shape is documented in
[`optimization.md`](optimization.md).

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
