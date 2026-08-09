# Competitive Analysis: OpenAI Codex CLI

Research date: 2026-08-09. Purpose: inform the Cambium harness design (see `docs/system-design.md`).

---

## What it is / stack

OpenAI Codex is OpenAI's coding agent. "Codex CLI is a coding agent from OpenAI that runs locally on your computer" (source: local npm package README at `~/.local/npm-global/lib/node_modules/@openai/codex/README.md`; also github.com/openai/codex).

Stack (from the open-source monorepo, github.com/openai/codex):

- **Rust workspace `codex-rs`** is the core. GitHub languages API reports Rust as the dominant language (46,874,883 bytes vs ~2 MB Python/TypeScript/etc). The repo is built with Bazel (root `MODULE.bazel`, `.bazelrc`, `defs.bzl`) and ships per-platform statically linked musl binaries (`codex-aarch64-unknown-linux-musl`, `codex-x86_64-unknown-linux-musl`, etc). The workspace contains ~100 crates: `cli`, `tui`, `linux-sandbox`, `windows-sandbox-rs`, `network-proxy`, `sandboxing`, `process-hardening`, `model-provider`, `models-manager`, `ollama`, `lmstudio`, `chatgpt`, `codex-api`, `codex-client`, `backend-client`, `state`, `thread-store`, `thread-manager-sample`, `git-utils`, `hooks`, `mcp-server`, `skills`, `memories`, `rollout-trace`, `otel`, `analytics` (source: GitHub contents API on `codex-rs/`).
- **Distribution**: the primary artifact is a single native binary. It is distributed via a curl installer, GitHub Releases, Homebrew (`brew install --cask codex`), and an npm meta-package `@openai/codex` whose `bin/codex.js` is a thin Node launcher that spawns the real platform binary (`@openai/codex-linux-<arch>` etc) (source: github.com/openai/codex README; local `codex.js` inspected).
- **Client surfaces**: interactive TUI, non-interactive `codex exec`, `codex review`, `codex resume`/`fork`/`archive`, `codex mcp` (MCP client *and* server), `codex sandbox`, plugins/skills, plus a ChatGPT desktop app, IDE extension, and an experimental app-server + TypeScript SDK (source: `codex --help` output; developers.openai.com/codex/cli).
- **Backend**: talks to OpenAI via the **Responses API** (`wire_api: "responses"` is the only supported wire protocol for providers), with websocket streaming when the provider supports it. ChatGPT-plan auth (device-code OAuth to auth.openai.com) is the first-class auth path; API-key auth is secondary. Source: developers.openai.com/codex/config-file/config-reference (`model_providers.<id>.wire_api`, `auth`); local `codex doctor` ("wire API responses", "auth mode chatgpt", "endpoint wss://chatgpt.com/backend-api/").
- **Agent loop**: a turn-based loop in the `cli`/`tui` crates. The model gets tools (shell, file edit via `apply-patch`, web search, subagent spawn, MCP tools). Spawned commands run inside the sandbox. Approvals pause the loop when the agent hits a boundary. Sessions persist to an append-only `history.jsonl` plus SQLite state DBs (`state_N.sqlite` with `_sqlx_migrations`). Source: local `~/.codex/` layout; config-reference (`history.persistence`, `sqlite_home`, `sandbox_mode`, `approval_policy`).
- **Sandboxing**: OS-native. Linux/WSL2 use a user-namespace sandbox; macOS uses Seatbelt; native Windows uses a Windows sandbox. Sandbox applies to spawned commands, not just built-in file ops. Source: developers.openai.com/codex/sandboxing.
- **Approval modes**: `approval_policy = untrusted | on-request | never` (plus a `granular` mode) and `approvals_reviewer = user | auto_review` (a reviewer subagent can field approval prompts). Source: developers.openai.com/codex/config-file/config-reference.
- **Git-based rollback**: official best practice is "Create Git checkpoints before and after a task so you can revert changes" (developers.openai.com/codex/cli). The desktop app adds per-chat **git worktrees** (`$CODEX_HOME/worktrees`, detached HEAD, snapshot-before-delete, ~15 worktree GC) and a "Handoff" flow between local checkout and worktree (developers.openai.com/codex/environments/git-worktrees). The CLI itself does not auto-create worktrees.
- **Multi-agent**: `[features.multi_agent]` (default on) with `spawn_agent`/`send_input`/`resume_agent`/`wait_agent`/`close_agent` tools; roles declared as `[agents.<name>]` pointing at per-role TOML config files with their own model/reasoning/sandbox (source: local `config.toml`; config-reference `agents` section).
- **Provider flexibility**: `model_providers.<id>` lets you define custom providers (base_url, env_key for the API key, headers, retries/timeouts) — but only the `responses` wire protocol; built-in providers `openai`, `ollama`, `lmstudio`; `--oss` flag for local models. No first-party Anthropic/Google adapters. Source: config-reference; local `codex --help`.

### Local install evidence (all commands run 2026-08-09, exit status 0 unless noted)

```
$ file /home/ubuntu/.local/bin/codex
/home/ubuntu/.local/bin/codex: symbolic link to ../npm-global/bin/codex

$ readlink -f /home/ubuntu/.local/bin/codex
/home/ubuntu/.local/npm-global/lib/node_modules/@openai/codex/bin/codex.js

$ /home/ubuntu/.local/bin/codex --version
codex-cli 0.146.1

$ file /home/ubuntu/.local/npm-global/lib/node_modules/@openai/codex/node_modules/@openai/codex-linux-arm64/vendor/aarch64-unknown-linux-musl/bin/codex
ELF 64-bit LSB executable, ARM aarch64, version 1 (SYSV), statically linked, stripped
```

- Installed via npm into `/home/ubuntu/.local/npm-global/lib/node_modules/@openai/codex`; `package.json` says `"version": "0.146.1"`, `"license": "Apache-2.0"`, platform binaries via optional deps `@openai/codex-linux-x64/arm64` etc.
- Host is `aarch64` (`uname -m`), so the launcher picked the arm64 musl binary.
- `codex doctor` (v0.146.1 · linux-aarch64): `16 ok · 1 idle · 2 notes · 1 warn · 0 fail`. Notes: `0.147.0 available (current 0.146.1)`. Warn: **"state DB rows point at missing or unusable rollout files"** (threads). Environment: Ubuntu 24.4.0 (noble), install method npm, `auth mode chatgpt`, `sandbox: restricted fs + restricted network · approval OnRequest`, linux helper `~/.codex/tmp/arg0/…/codex-linux-sandbox`, websocket to `wss://chatgpt.com/backend-api/` handshake HTTP 101 OK, ChatGPT base URL reachable (HTTP 403 = auth required, expected).
- A user-namespace sandbox utility (version 0.9.0) is installed and on PATH (verified via `which`).

#### Config: `~/.codex/config.toml` (2,536 bytes)

- `model = "gpt-5.6-sol"`, `personality = "none"`, `model_reasoning_effort = "medium"`, `model_catalog_json = "/home/ubuntu/.codex/model_catalog.json"`.
- `[features.multi_agent] = true`; `[features.multi_agent_v2]` enabled with `max_concurrent_threads_per_session = 12`.
- Per-role agents: `[agents.default|explorer|luna|reviewer|worker]` each with a `config_file` under `~/.codex/agents/*.toml`. The agent TOMLs set per-role models/effort/sandbox, e.g. `worker.toml` → `model = "gpt-5.6-luna"`, `model_reasoning_effort = "xhigh"`; `explorer.toml` → `sandbox_mode = "read-only"`.
- Model migration notices map `gpt-5.2-codex`/`gpt-5.3`/… → `gpt-5.6-sol` (schema/version churn handled via config notices).
- `[projects."/home/ubuntu"]` and `[projects."/home/ubuntu/polymarket-arbitrage"]` are `trusted`.
- `~/.codex/AGENTS.md` (8.0 KB) holds the user's own agent-role playbook (orchestrator/worker routing, worktree rules, verification policy) — evidence that Codex is *used as* a harness substrate with custom agent config.

#### Data/session state (`~/.codex`, `du -sh`)

| path | size |
|---|---|
| `state_5.sqlite` | 41 MB (42,598,400 B; 44 `_sqlx_migrations`) |
| `plugins/` | 28 MB |
| `cache/` | 9.7 MB (`codex_apps_server_info`, `codex_apps_tools`, `remote_plugin_catalog`) |
| `history.jsonl` | 1.2 MB (1,183,605 B; **1,387 lines, 174 sessions, 97 msgs max, median 4 msgs/session**) |
| `model_catalog.json` | 314,817 B (fetched 2026-07-31, client_version 0.146.0) |
| `log/` | `codex-login.log` only (device-code OAuth to auth.openai.com, 2026-07-09, succeeded) |

- `~/.codex/sessions/` and `~/.codex/archived_sessions/` are **empty**, even though history.jsonl spans 174 sessions — transcript storage has moved to SQLite/rollout files, and some of those rows are now broken (matches the doctor warning).
- `auth.json`: `auth_mode: chatgpt` (ChatGPT Pro account, tokens present; contents redacted here — do not reproduce tokens).
- `~/.codex` is itself a git repo. `git log --oneline -3`: `0c32870 Enable i-have-adhd plugin` / `d83e658 shorten root agent usage hint` / `b8c92f9 tune Codex agent configuration` (last commit dated 2026-07-20).

#### What it was recently working on (from `history.jsonl`, 174 sessions 2026-02-06 → 2026-08-06 UTC)

- Session message keyword counts: worktree 287, review 250, adversarial 141, polymarket 138, trade 118, deploy 105, fit 93, risk 72, arbitrage 62, quoter 56, memory 48, markout 31, feedback 29, claude 20, futurecast 18.
- Most recent 8 sessions (first user message, date UTC):
  - 2026-08-06 "check this thread, is this install broken as well?" (2 msgs)
  - 2026-08-02 "check this feedback, implement the correct parts: ## Severe Bugs and Mathematical Errors * Speculative Transaction Leak …" (25 msgs)
  - 2026-08-02 "check the software architecture vs the agents.md. the pricing models should have no knowledge about the risk manager or the quoter …" (3 msgs)
  - 2026-08-02 "check /home/ubuntu/.claude/projects/…/memory/ should we remove / consolidate?" (4 msgs)
  - 2026-08-02 "check these: venue_mid_markout_mean_curve.png …" (3 msgs)
  - 2026-08-01 "check claude code. we have tuned it with tweakcc and lobotimized claude code. update all …" (9 msgs)
  - 2026-08-01 "Not ready to trade. dev-a is healthy, but three defects between it and live money …" (10 msgs)
  - 2026-08-01 "You are the fit-operations engineer for the OU/futurecast production fit. Read plan.md …" (22 msgs)

Summary of recent work: the agent has been doing **adversarial code review and parallel worktree-based implementation on the user's Polymarket arbitrage trading system** (fit/markout analytics, quoter/risk-manager refactors, deploy readiness checks), used as a subagent-executor driven by a shared AGENTS.md playbook — i.e., exactly the orchestrator/worker pattern Cambium formalizes. One recent session questions install health, consistent with the doctor warning.

## What it does well

1. **Single static Rust binary, zero-runtime-dependency deployment.** Statically linked musl builds on Linux (verified locally: aarch64 ELF, statically linked, stripped). Start-up and execution are fast; the npm wrapper is only a distribution convenience (local `codex.js` spawns the binary and forwards signals).
2. **Best-in-class OS-native sandbox + approval model.** A kernel-namespace sandbox (Linux), Seatbelt (macOS), native Windows sandbox; the sandbox wraps *spawned commands* (git, package managers, test runners), not just file edits. `approval_policy` (untrusted/on-request/never) + `sandbox_mode` (read-only/workspace-write/danger-full-access) + per-command `rules` give a fine-grained autonomy dial, and `approvals_reviewer: auto_review` delegates approval to a reviewer subagent. This is the mechanism that makes low-approval-friction autonomy safe. Source: developers.openai.com/codex/sandboxing, config-reference.
3. **Tight, first-party model integration.** ChatGPT-plan auth (device-code OAuth), Responses API over websocket streaming, server-side prompt caching/predicted outputs, model catalog with per-model reasoning levels (`low`…`ultra`), service tiers, model-migration notices. This is the best possible experience *if you standardize on OpenAI models*.
4. **Real multi-agent / subagent system.** `[agents.<name>]` role configs with per-role model, reasoning effort, and sandbox mode (verified locally: worker= gpt-5.6-luna/xhigh, explorer= read-only); spawn/resume/wait/close tools; concurrency cap (12/session locally). Extensible via AGENTS.md hierarchy and custom agent TOMLs — the user already drives Codex as an orchestrator/worker harness.
5. **Robust session persistence and resume ergonomics.** Append-only `history.jsonl` + sqlx-migrated SQLite state, `codex resume`/`fork`/`archive`/`delete`, compaction with a configurable prompt, MCP servers, skills, plugins and a marketplace. Very forgiving to long-running and interrupted work.
6. **Extensible surface for automation.** `codex exec` (non-interactive), `codex review`, `codex mcp-server`, an experimental app-server and TypeScript SDK, lifecycle hooks (`PreToolUse`/`PostToolUse`/`SessionStart`/…), and `--config key=value` overrides make it scriptable and embeddable — the properties a harness like Cambium needs from its workers.
7. **Git-worktree isolation (desktop app).** Per-chat worktrees under `$CODEX_HOME/worktrees`, detached HEAD (no branch pollution), `.worktreeinclude` for ignored files (`.env` etc), snapshot-before-delete, GC at ~15 worktrees. Source: developers.openai.com/codex/environments/git-worktrees.

## What it does poorly / limitations

1. **OpenAI-first provider model.** Custom providers are constrained to the `responses` wire protocol; auth is ChatGPT-centric (`auth_mode: chatgpt`, OAuth tokens) with API-key as a secondary path; local models only via `ollama`/`lmstudio` + `--oss`. There is no multi-provider failover/load-balancing inside one session — if OpenAI is degraded, the agent stops. Cambium's FanOut cascade is a genuine differentiator here (system-design.md §6).
2. **Local install shows state drift / degradation.** `codex doctor` reports `1 warn: state DB rows point at missing or unusable rollout files`, `sessions/` and `archived_sessions/` are empty despite 174 history sessions, and the 42 MB state DB has 44 migrations. The user's own latest session ("is this install broken as well?") corroborates. Fast release churn (0.146.1 installed, 0.147.0 already available; model migration notices from gpt-5.2→gpt-5.6) adds operational noise.
3. **Sandbox has real-world setup friction on Linux.** Requires a user-namespace sandbox tool + working unprivileged user namespaces; on Ubuntu 24.04 an extra AppArmor profile or a sysctl override is often needed, and the bundled fallback helper only works where unprivileged userns are permitted (developers.openai.com/codex/sandboxing). This is exactly the portability trap Cambium's M8 Septum must design around (reviews/review-implementation.md M4 flags the sandbox backend being Linux-only).
4. **Isolation is sandbox-only; no process supervisor.** Subagents run inside the main process's session context; there is no orchestrator-level process isolation, heartbeat/kill/restart, or per-worker stdin/stdout pipe protocol. A stuck subagent or a corrupt state row affects the whole session (evidenced locally by the broken rollout rows). Cambium's Supervisor (M4) is the stronger design and should stay.
5. **Git-worktree isolation and auto-rollback are desktop-app features, not CLI.** The CLI's only rollback guidance is "create Git checkpoints before and after a task" (developers.openai.com/codex/cli). Multi-agent threads in the CLI share one working tree; there is no automatic commit/rollback cycle per task. Cambium's Surculus + Unio merge/rollback loop is more automated than anything the CLI ships.
6. **Config/feature surface is enormous and fast-moving.** The config-reference documents hundreds of keys (permissions profiles, network proxy, otel, memories, goals, hooks, MCP per-server allowlists, apps). Fine-grained power, but a lot of it is experimental/under-development (`features.*` flags, `app-server` marked `[experimental]` in `--help`), which makes reproducible harness behavior harder.
7. **No harness-level caching or optimization feedback loop.** Prompt caching is server-side OpenAI magic; there is no client-side prompt-hash cache and no per-node optimization trajectory store like Cambium's M9 Ascensus flywheel (system-design.md §6 "What We Do Differently": "Cache: None").

## Relevant lessons for Cambium

- **Sandbox is the autonomy enabler.** Codex's pattern — OS-native sandbox for *all spawned commands* + an approval-policy dial (untrusted/on-request/never) + command-prefix rules + an auto-reviewer agent — is what lets an agent run unmoderated. Cambium M8 (Septum) should copy the mechanism, but budget for the platform friction: a user-namespace sandbox + unprivileged userns + AppArmor on Ubuntu 24.04, Seatbelt on macOS, and no clean Windows story. Make the sandbox a pluggable backend with a documented fallback, not a hard Linux dependency.
- **Per-role worker config is right; isolation level is the differentiator.** Codex's `[agents.<name>]` → per-role TOML (model, reasoning effort, sandbox mode) matches Cambium's worker configs exactly and validates the design. But Codex's subagents share the parent's context tree and process; Cambium's process-isolated Supervisor/Worker with stdin/stdout pipes (M4/M5) and per-worker worktrees is the defensible upgrade.
- **Provider adapter surface: keep FanOut, steal the details.** Codex's `model_providers` shows the minimum viable adapter surface (base_url, env_key auth, headers, request/stream retry counts, stream idle timeout, wire protocol tag). Cambium M2 (FanOut) should adopt those fields per provider for its cascade, while keeping multi-provider failover and prompt-hash caching that Codex lacks.
- **Worktree lifecycle engineering.** Adopt Codex's desktop-app worktree rules wholesale: per-task worktree, detached HEAD, copy `.worktreeinclude`-listed ignored files (`.env`, secrets) into fresh worktrees, snapshot before deletion, GC cap (~15). These are exactly the edge cases Cambium M3 (Surculus) and the review notes (stale locks, shared object DB, `.git/index.lock`) must handle.
- **Make git checkpoint/rollback implicit.** Codex only *recommends* checkpoints before/after tasks. Cambium should bake checkpoint-before / rollback-on-failure into the Supervisor so workers can never leave the repo unrecoverable.
- **Persistence = append-only log + migrated SQLite, plus a diagnostics command.** Codex's `history.jsonl` + sqlx-migrated `state_N.sqlite` is a proven shape — but this local install proves the drift failure mode (migrations ahead of data, rows pointing at missing rollout files, `sessions/` emptied). Cambium's event log should version with real migrations and ship a `doctor`-style diagnostic that validates log↔state consistency. Adopt `codex doctor` as a template for a harness health command.
- **Version your config and model migrations.** Codex ships model-migration notices (`gpt-5.2-codex` → `gpt-5.6-sol`) in config because OpenAI's model names churn monthly. Cambium's config schema should carry a version + migration path from day one.
- **Headless exec is the harness surface.** `codex exec`/`review`/`mcp-server` are the right integration points; the TUI is irrelevant to a harness. Cambium's worker interface should mirror a non-interactive exec contract (prompt in, transcript + diff + exit status out), which matches M1 Nuntius's stdin/stdout pipe design.
- **Don't chase Codex's config sprawl.** Hundreds of `features.*` toggles (many experimental) hurt reproducibility. Cambium should keep a small, versioned config core and expose per-node tuning through its optimization harness (M9) instead of configuration flags.

## Sources

- Local install (all paths under `/home/ubuntu/.local/`, `/home/ubuntu/.codex/`): `codex --version`, `codex doctor`, `codex --help`, `file`/`readlink` on binary, `~/.codex/config.toml`, `~/.codex/agents/*.toml`, `~/.codex/history.jsonl` (Python parsing), `~/.codex/state_5.sqlite` (sqlite3), `~/.codex/version.json`, `~/.codex/log/codex-login.log`, `~/.codex/model_catalog.json`, npm package `package.json` and `bin/codex.js` — all inspected 2026-08-09.
- https://github.com/openai/codex (README: "Lightweight coding agent that runs in your terminal"; stars 104,943, forks 15,886, 9,052 commits, Apache-2.0, created 2025-04-13, last push 2026-08-09).
- https://api.github.com/repos/openai/codex/languages (Rust 46,874,883 bytes dominant) and https://api.github.com/repos/openai/codex/contents/codex-rs (crate listing).
- https://developers.openai.com/codex/cli (CLI features, checkpoint best practice)
- https://developers.openai.com/codex/sandboxing (OS-native sandbox, kernel-namespace/Seatbelt, approval policies)
- https://developers.openai.com/codex/config-file/config-reference (config keys: sandbox_mode, approval_policy, model_providers, agents, multi_agent, sqlite_home, history.persistence)
- https://developers.openai.com/codex/environments/git-worktrees (desktop-app worktree behavior: detached HEAD, snapshots, GC, .worktreeinclude)

### Objectively verifiable stats (re-check on demand)

1. `codex --version` → `codex-cli 0.146.1`; `codex doctor` shows latest available `0.147.0`.
2. `history.jsonl` = 1,387 lines / 174 sessions, span 2026-02-06 → 2026-08-06 UTC (parsed from file timestamps).
3. `state_5.sqlite` = 42,598,400 bytes with 44 `_sqlx_migrations` rows.
4. `file` on platform binary → "ELF 64-bit LSB executable, ARM aarch64 … statically linked, stripped".
5. GitHub: 104,943 stars, 15,886 forks, 9,052 commits (via GitHub API, 2026-08-09).
6. `codex doctor` summary line: `16 ok · 1 idle · 2 notes · 1 warn · 0 fail`.
