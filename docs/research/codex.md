# Competitive Analysis: OpenAI Codex CLI

**Research date:** 2026-08-09. **Purpose:** Cambium harness design input (`docs/architecture/system-design.md`). This snapshot records the installed Codex 0.146.1 and the upstream sources inspected; it is not runtime authority.

## What it is / stack

OpenAI describes Codex CLI as a coding agent that runs locally. The open-source monorepo is a Rust/Bazel workspace (`codex-rs`, roughly 100 crates: CLI/TUI, sandboxing, providers, state, MCP, skills, hooks, telemetry). GitHub’s languages API reported Rust 46,874,883 bytes versus roughly 2 MB Python/TypeScript. Distribution is a native platform binary via curl, GitHub Releases, Homebrew, or npm `@openai/codex`; the npm `bin/codex.js` launches the platform binary.

- **Surfaces:** interactive TUI; `codex exec`, `review`, `resume`, `fork`, `archive`; MCP client/server; sandbox; plugins/skills; experimental app-server/TypeScript SDK; desktop and IDE integrations. Sources: `codex --help`, https://github.com/openai/codex, https://developers.openai.com/codex/cli
- **Backend:** Responses API (`wire_api = "responses"`) with websocket streaming where supported; ChatGPT device-code OAuth is first-class and API keys are secondary. `model_providers` supports base URL, environment-key, headers, retry/timeouts, but only the Responses wire protocol; built-ins include OpenAI, Ollama, and LM Studio. Sources: local `codex doctor`; https://developers.openai.com/codex/config-file/config-reference
- **Loop/state:** shell, file-edit, web, subagent, and MCP tools; spawned commands run inside a sandbox; approvals pause execution. Append-only `history.jsonl` plus SQLite state (`state_N.sqlite`, `_sqlx_migrations`) persist sessions.
- **Safety:** Linux/WSL2 user namespaces, macOS Seatbelt, and Windows sandbox; sandbox wraps spawned commands. `approval_policy` supports `untrusted`, `on-request`, `never` (and granular rules); `approvals_reviewer = auto_review` can delegate review. Source: https://developers.openai.com/codex/sandboxing ; https://developers.openai.com/codex/config-file/config-reference
- **Git and agents:** CLI guidance is checkpoints before/after tasks. Desktop adds detached per-chat worktrees under `$CODEX_HOME/worktrees`, `.worktreeinclude`, snapshots, and roughly 15-worktree GC. `[features.multi_agent]` provides spawn/resume/wait/close; `[agents.<name>]` points to role TOMLs with model, reasoning, and sandbox settings. Source: https://developers.openai.com/codex/environments/git-worktrees and local config.

## Local install evidence (commands run 2026-08-09; exit 0 unless noted)

```text
$ file /home/ubuntu/.local/bin/codex
symbolic link to ../npm-global/bin/codex
$ readlink -f /home/ubuntu/.local/bin/codex
/home/ubuntu/.local/npm-global/lib/node_modules/@openai/codex/bin/codex.js
$ /home/ubuntu/.local/bin/codex --version
codex-cli 0.146.1
$ file /home/ubuntu/.local/npm-global/lib/node_modules/@openai/codex/node_modules/@openai/codex-linux-arm64/vendor/aarch64-unknown-linux-musl/bin/codex
ELF 64-bit LSB executable, ARM aarch64, statically linked, stripped
```

Install path is `/home/ubuntu/.local/npm-global/lib/node_modules/@openai/codex`; package version is `0.146.1`, Apache-2.0, with an arm64 optional dependency on this `aarch64` host. `codex doctor` reported `16 ok · 1 idle · 2 notes · 1 warn · 0 fail`, current 0.146.1 with 0.147.0 available, Ubuntu 24.04, ChatGPT auth, restricted filesystem/network and on-request approval, sandbox helper present, websocket handshake 101, and the warning “state DB rows point at missing or unusable rollout files.”

`~/.codex/config.toml` (2,536 bytes) sets `gpt-5.6-sol`, medium reasoning, multi-agent v2, 12 concurrent threads, and role TOMLs under `~/.codex/agents/` (worker = gpt-5.6-luna/xhigh; explorer/reviewer read-only). `~/.codex/AGENTS.md` (8.0 KB) contains the operator’s orchestrator/worker playbook. `~/.codex` is a git repo; latest inspected commits are `0c32870`, `d83e658`, `b8c92f9`.

State measurements: `state_5.sqlite` 42,598,400 bytes with 44 migrations; `plugins/` 28 MB; cache 9.7 MB; `history.jsonl` 1,183,605 bytes, 1,387 lines, 174 sessions, max 97 messages, median 4; `model_catalog.json` 314,817 bytes fetched 2026-07-31. `sessions/` and `archived_sessions/` were empty despite history, matching the doctor warning. Auth contents were inspected only for structure and are not reproduced.

History spans 2026-02-06–08-06 UTC. Keyword counts included worktree 287, review 250, adversarial 141, polymarket 138, trade 118, deploy 105, and risk 72. Recent sessions were adversarial reviews, deployment/readiness work, and install-health questions in the operator’s Polymarket repository; this demonstrates orchestration use, not a general performance claim.

## What it does well

1. Static musl binary with fast, dependency-light deployment (verified by `file` above).
2. OS-native sandbox plus approval policies, command-prefix rules, and optional auto-review: a strong autonomy/safety dial when platform setup works.
3. First-party OpenAI integration: OAuth, Responses/websocket streaming, server prompt caching, model reasoning levels, service tiers, and migration notices.
4. Real role-configured subagents, concurrency cap 12, AGENTS.md hierarchy, skills/MCP/plugins, and durable history/resume/fork/archive.
5. Automation surfaces (`exec`, `review`, MCP server, app-server/SDK, lifecycle hooks, `--config`) and desktop worktree snapshots provide useful harness patterns.

## What it does poorly / limitations

1. **OpenAI-first.** Custom providers must speak Responses; there is no first-party Anthropic/Google adapter or in-session multi-provider failover. Cambium’s Diffundo cascade remains a differentiator.
2. **Observed state drift.** Doctor’s missing-rollout warning, empty session directories, 42 MB database, and rapid 0.146.1→0.147.0 churn show operational risk.
3. **Linux sandbox friction.** User namespaces and often AppArmor/sysctl setup are required on Ubuntu 24.04; portability is not uniform. Source: https://developers.openai.com/codex/sandboxing
4. **No process supervisor in the CLI.** Subagents share the session process/context; there is no orchestrator heartbeat, restart, or per-worker pipe protocol. Desktop worktrees are not CLI behavior; CLI guidance only recommends checkpoints.
5. **Large experimental config surface.** Hundreds of feature/permission/MCP/hook keys and fast model migration make reproducibility harder.
6. **No harness-level optimization store.** Prompt caching is provider-side; no client prompt-hash cache or per-node optimization trajectory was observed.

## Relevant lessons for Cambium

- Copy the mechanism, not the platform assumptions: sandbox every spawned command, expose approval/prefix rules, and make the sandbox backend pluggable (user namespaces/AppArmor on Linux, Seatbelt on macOS, documented Windows limits).
- Keep per-role model/reasoning/sandbox configuration, but retain Cambium’s process-isolated Supervisor/Worker, pipe protocol, worktrees, heartbeats, and restart policy as the stronger boundary.
- Adopt the provider adapter fields shown by `model_providers` (base URL, env-key, headers, retry/stream timeouts, wire protocol), while keeping Diffundo multi-provider failover and correctness-aware caching.
- Use desktop worktree rules: detached per-task worktree, `.worktreeinclude` for ignored files, snapshot before deletion, and bounded GC. Make checkpoints/rollback implicit on failure rather than advice.
- Combine append-only event history with migrated SQLite and a `doctor` consistency check; the local broken rollout rows are concrete drift evidence. Version config and model migrations.
- Treat `exec`/`review`/`mcp-server` as the worker contract: prompt in, transcript/diff/exit status out. Keep the config core small instead of reproducing Codex’s experimental toggle sprawl.

## Additional inspected findings

The local `codex doctor` environment was Ubuntu 24.04 (noble), npm install, ChatGPT auth, restricted filesystem and network, on-request approvals, and the Linux helper under `~/.codex/tmp/arg0/`. The websocket endpoint was `wss://chatgpt.com/backend-api/`; handshake HTTP 101 succeeded and an unauthenticated base URL check returned expected HTTP 403. A user-namespace sandbox utility version 0.9.0 was on PATH. These checks establish local setup, not cross-platform support.

The role files under `~/.codex/agents/` are concrete examples of per-role policy: `worker.toml` selected `gpt-5.6-luna` with xhigh reasoning, while `explorer.toml` selected read-only sandboxing. The config enabled multi-agent v2 with 12 concurrent threads. Model migration notices in the local catalog map gpt-5.2-codex/gpt-5.3/gpt-5.4/gpt-5.5 to gpt-5.6-sol and 5.3-codex-spark/5.4-mini to Luna. This churn is why a versioned config schema matters.

The history scan found 174 sessions, with message keyword counts of worktree 287, review 250, adversarial 141, polymarket 138, trade 118, deploy 105, fit 93, risk 72, arbitrage 62, quoter 56, memory 48, markout 31, feedback 29, claude 20, and futurecast 18. The newest session asked whether the install was broken; other recent sessions performed code-review, architecture, deployment, and data-analysis work. This supports the “used as an orchestrator” finding while remaining one operator’s corpus.

The local state layout is itself a caution: `history.jsonl` remains populated while `sessions/` and `archived_sessions/` are empty, and SQLite rows point to missing rollout files. The doctor warning and the user’s health-check session are consistent observations; neither proves data loss for every installation. Any Cambium doctor command should validate event-log/state/rollout references explicitly.

The npm wrapper forwards signals to the platform binary, so the install has a small JavaScript distribution layer but a static runtime payload. The local `file` result confirms the aarch64 musl artifact rather than a dynamically linked Node process. Codex’s model catalog was fetched 2026-07-31 and the local version was already behind the available 0.147.0 release on 2026-08-09; version-sensitive claims should retain both dates.

The safety model has two separate axes: sandbox mode (`read-only`, `workspace-write`, `danger-full-access`) and approval policy (`untrusted`, `on-request`, `never`, with granular prefix rules). `approvals_reviewer = auto_review` delegates a user decision to another agent, but it does not replace the sandbox. Cambium should keep these as independent fields and fail closed when either is unspecified.

The provider catalog also carries context windows, reasoning levels, service tiers, and migration notices, while `config.toml` carries project trust entries for `/home/ubuntu` and the Polymarket repository. This makes trust, model selection, and approval orthogonal in the inspected layout. The local config’s `sqlite_home`, history persistence, MCP per-server options, and experimental feature flags demonstrate power but also explain why a smaller versioned Cambium schema is easier to reproduce.

The local package’s optional platform dependencies explain why `file` on the launcher and `file` on the payload differ. Distribution convenience is not runtime architecture: npm, Homebrew, and curl all lead to the same native binary. That distinction matters when comparing Codex to Bun-compiled competitors whose executable remains a large language-runtime bundle.

Codex’s desktop worktree behavior is more extensive than the CLI: detached HEADs under `$CODEX_HOME/worktrees`, `.worktreeinclude` for ignored files such as `.env`, snapshot-before-delete, and GC around 15. The CLI only recommends Git checkpoints. This distinction must remain explicit when adopting the lifecycle pattern.

## 6. Sources

- Local paths/commands inspected 2026-08-09: `/home/ubuntu/.local/bin/codex`, npm package `@openai/codex`, `codex --version`, `doctor`, `--help`, `file`, `readlink`, `~/.codex/config.toml`, `~/.codex/agents/*.toml`, `~/.codex/history.jsonl`, `~/.codex/state_5.sqlite`, `~/.codex/model_catalog.json`, `~/.codex/version.json`, `~/.codex/log/codex-login.log` (auth values not reproduced).
- https://github.com/openai/codex
- https://api.github.com/repos/openai/codex/languages
- https://api.github.com/repos/openai/codex/contents/codex-rs
- https://developers.openai.com/codex/cli
- https://developers.openai.com/codex/sandboxing
- https://developers.openai.com/codex/config-file/config-reference
- https://developers.openai.com/codex/environments/git-worktrees

## 7. Objectively verifiable stats

1. `codex-cli 0.146.1`; doctor says 0.147.0 available.
2. `history.jsonl`: 1,387 lines / 174 sessions, 2026-02-06 → 2026-08-06 UTC.
3. `state_5.sqlite`: 42,598,400 bytes / 44 migrations.
4. Platform binary: aarch64, statically linked, stripped.
5. GitHub snapshot: 104,943 stars, 15,886 forks, 9,052 commits (2026-08-09).
6. Doctor summary: `16 ok · 1 idle · 2 notes · 1 warn · 0 fail`.
Codex’s local history and SQLite state are separate persistence layers, and the doctor warning links them explicitly through rollout references. The useful design lesson is not “use SQLite” alone: it is to version the relation between append-only events, durable rows, and transcript files, then make the diagnostic command check all three.
The local install’s 42 MB state database is a measured size, not a recommended allocation; its value here is the observed drift warning and migration count.
Future snapshots should keep CLI behavior separate from desktop-app worktree behavior, because the inspected docs assign automatic snapshots and GC only to the desktop surface.
