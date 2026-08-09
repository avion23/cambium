# Competitive Analysis: Prime Agent (prime-agent)

**Researched:** 2026-08-09 · **Local version:** 0.7.1 · **Author:** research task wt-prime-agent
**Scope:** local install at `/home/ubuntu/.local/bin/prime-agent` + `~/.prime` + public web sources. Every local claim cites the exact command + output. Web claims cite URLs. Unverified items are marked **UNVERIFIED**.

---

## 1. What it is / stack

**Prime Agent** is an open-source "self-improving RLM agent for coding workflows and long-running autonomous tasks" published by **PrimeIntellect-ai** (MIT license).

- **Public project:** https://github.com/PrimeIntellect-ai/prime-agent — 10.8k stars, 1.1k forks, 4,480 commits, 176 open issues, 271 PRs (fetched from GitHub repo page, 2026-08-09).
- **Lineage:** a hard fork of [`pi-mono`](https://github.com/badlogic/pi-mono) by Mario Zechner; retains inherited `@earendil-works/pi-*` package identifiers (`README.md` in installed package, line 16; "Upstream" section).
- **Stack (verified locally):** Node.js/TypeScript esbuild bundle. The launcher is a 54-line ESM script (`/home/ubuntu/.local/npm-global/bin/prime-agent`) that enforces **Node ≥ 22.8.0** before importing `dist/cli-main-77S767BW.js`.
- **Install method (verified):** npm global install from a local versioned tarball. `~/.npm/_logs/2026-08-09T18_51_34_372Z-debug-0.log` shows `verbose argv "install" "--global" "/tmp/prime-agent-0.7.1.tgz"`. Core deps are private R2-hosted tarballs, e.g. `https://pub-728493de92a943e2a9b2d17b4719f318.r2.dev/releases/v0.7.1/prime-agent-core-0.7.1.tgz`. The npm registry name `prime-agent` is **not** the distribution path (`npm view prime-agent` → E404); the README instructs `curl -fsSL https://app.primeintellect.ai/prime-agent/install.sh | sh`.
- **Two core abstractions** (README + repo readme):
  1. **RLM — Recursive Language Model**: context as variables, the model drives a persistent IPython kernel (the *only* built-in tool, "Available built-in tools: `ipython`" in CLI reference), and recursive subagents are programmatic function calls via `rlm(...)`.
  2. **Continual Harness**: prompts, memories, skills, and subagent specs stored as durable state the agent can refine via `/refine` (evidence-backed, snapshot/rollback, never rewrites the base system prompt).
- **Runtime architecture** (installed `docs/architecture.md`, "System at a Glance"): interactive TUI / headless clients → `AgentConnection` → **Daemon supervisor** (Unix socket; routing, attachments, recovery) → **session workers** (one root session tree each: `AgentSessionRuntime`, root `AgentSession`, scheduler, root IPython kernel, RLM child runtimes) → model providers + JSONL session storage. "Workers and kernels are separate processes for lifecycle and failure containment, **not** security sandboxes."
- **Sessions:** append-only JSONL under `~/.prime/agent/sessions/` with a tree (`id`/`parentId`); in-place branching, `/fork`, `/clone`, compaction.
- **Config dir `~/.prime/agent`:** `settings.json`, `models.json`, `auth.json`, `AGENTS.md`, `SYSTEM.md`, `APPEND_SYSTEM.md`, `harness/harness_state.json`, `kernel-venv/` (bundled IPython venv), `sessions/`, `session-leases/`, `logs/`.
- **Model configuration (local, `models.json`):** 3 custom OpenAI-compatible providers — `opencode-go` (DeepSeek V4 Flash/Pro, 1M context), `nvidia` (MiniMax M3), `tokenrouter` (`auto:balance/fast/cost/quality`, Kimi K3 Free). `auth.json` holds credentials for `google`, `openrouter`, `zai`, `kimi-coding`, `openai-codex`. `settings.json`: defaultProvider `opencode-go`, defaultModel `deepseek-v4-flash`, thinking `high`, retry enabled (2 retries, 3s base delay), compaction on (`reserveTokens 24576`, `keepRecentTokens 24000`).

## 2. What it does well

1. **Everything programmatic.** A single persistent IPython kernel is the model's only tool; imports, variables, and file state survive across turns (README; local `SYSTEM.md`: "State, imports, variables, and file handles persist across turns").
2. **Native subagents.** `rlm(...)` spawns real child agents (parallel/background), children are named and listed (`prime-agent list` shows `build-dryrun-*`, `review-dryrun-*`, etc.), results/usage are attributed back (`child_usage_attributed` events), and agents message each other without user routing (`agent_message`).
3. **Daemon-backed continuity.** Sessions, IPython state, schedules, and subagents survive terminal detach and can be reattached (`prime-agent attach`); goals, heartbeats, schedules, and bounded autonomous mode with user-defined gates.
4. **Continual self-improvement harness.** `/refine` persists evidence-backed lessons as prompts/memories/skill descriptions/subagent specs, with recorded refinement history and rollback. Local evidence: `harness/harness_state.json` holds a `refine_workflow` prompt and a `mac_test_reviewer` subagent spec (created 2026-08-06).
5. **Robust session format.** Append-only JSONL with branching; `compaction` summaries carry goals/progress/decisions forward (local sessions show 4 compactions; the largest compaction summary is ~2,000 words with a full progress ledger).
6. **Large orchestration workload.** Local evidence: a single session (`019fdf20-8158`, polymarket-arbitrage) reached **1,988 messages** and drove parallel worktree-based subagents against a Rust codebase, including adversarial reviewers (glm-5.2) and mac-remote test gates.
7. **Rich provider/model management.** `prime-agent model list` returns **399 lines (~398 models)** across google, openai-codex, kimi-coding, nvidia, opencode, opencode-go, tokenrouter, etc., with context/max-output/thinking/image columns; multiple subscriptions and API-key providers via `/login`.

## 3. What it does poorly / limitations

1. **Memory scaling is the flagship weakness (verified by a real incident).** The daemon's session worker holds the parent session **plus every RLM subagent runtime in one Node process**. Local `agent.jsonl` contains **6** `FATAL ERROR: Reached heap limit Allocation failed - JavaScript heap out of memory` stack traces. The 2026-08-09 QA session (`019fe693-f125`) documents the reproduction: bug config (32GB cap) → `VmRSS 20,145 MiB` → **earlyoom SIGTERM** at 15% memory (machine starved to ~3.6GB free); fix config `NODE_OPTIONS=12288` → V8 self-abort at 12,291MB heap, RSS ~12.5GB, machine stayed healthy. Root-cause notes in-session: the 1M-token context of `deepseek-v4-flash` is "the memory driver: per-runtime LLM context, not fixed overhead"; recommendation `idleEvictionMinutes=30` to evict a 12-child swarm. **UNVERIFIED** (in-session analysis, not shipped docs): exact tuning values beyond the evidence quoted.
2. **Socket + lock-file supervision has real failure modes.** The daemon uses Unix sockets and lock directories (`/tmp/prime-agent-1001/daemon.sock`, `daemon.sock.lock`, `session-leases/*.lock`). Local `agent.jsonl`: **36** `Timed out connecting to daemon session worker` lines, **57** `Supervisor command attach failed: Error: Unknown active session`, **8** `Could not adopt worker ... Timed out waiting for daemon worker hello`. This validates Cambium's design doc anti-pattern list ("Unix socket + lock-file supervision", "Prime Agent").
3. **Children die mid-work.** Local compaction summary (session `019fdf20`): "Children die frequently mid-work ('completed without sending a reply') — checkpoint early, commit after every milestone, salvage transcripts"; a glm-5.2 reviewer "completed without sending a reply at 204 msgs". Session JSONL contains `custom_message` type `prime-agent.worker_recovery`: "The isolated session worker stopped during in-flight work... uncertain model, tool, bash, or child-agent work was not replayed."
4. **Not a security sandbox** (README warning): "Its worker and kernel processes improve lifecycle isolation and recovery; they are **not** a security sandbox." Model-generated Python runs with the user's permissions.
5. **Plaintext credentials.** API keys live in plaintext in `~/.prime/agent/models.json` and `~/.prime/agent/auth.json` (verified: `models.json` contains raw `apiKey` values; `auth.json` holds OAuth access/refresh tokens). No secrets management layer.
6. **Heavy daemon/log sprawl.** 59 worker socket logs under `~/.prime/agent/logs/`, plus session-leases lock files for every session; frequent daemon restarts logged (110 `shutting down (exit 0)` lines).
7. **Distribution is fragmented.** Not on the npm registry; released as R2-hosted tarballs / GitHub releases / `install.sh`; the npm package name is squat-free but 404s, and versioned tarball deps make npm-installed builds awkward to audit.

## 4. Relevant lessons for Cambium

Cambium's design doc (`docs/system-design.md`) already cites Prime Agent as the source for: append-only JSONL sessions, RLM context-as-variables, the continual-harness self-improvement loop, and (as anti-pattern) `.pid` files + Unix-socket supervision. This research corroborates and sharpens those choices:

1. **Process-per-worker isolation is the differentiator that matters.** Prime Agent's single-Node-process-per-session (parent + all children in one heap) produces OOM/earlyoom incidents under subagent swarms. Cambium's supervisor spawns each worker as an independent process (PID-based identity, pipe EOF = death, one crash restarts only that worker) is the direct, incident-validated contrast. Keep it.
2. **Supervise memory, not just process liveness.** The 2026-08-09 incident shows wall-clock/process health checks miss heap exhaustion. Cambium's heartbeat watchdog should watch per-worker RSS/heap and per-tool duration (design doc fix F6: per-tool heartbeat) to avoid the "killed mid-swarm" cascade.
3. **Cap context per worker.** Prime Agent's memory driver was the 1M-token context retained per subagent runtime. Cambium should set explicit per-worker token budgets and evict idle workers (mirroring `idleEvictionMinutes`), not only bound turns.
4. **Keep stdin/stdout IPC; avoid the socket+lockfile layer.** Cambium's pipe-based Kahn network avoids the attach/hello timeout failures seen here (36 "Timed out" + 57 "Unknown active session" in logs).
5. **Named, inspectable children are valuable.** `prime-agent list` exposed named child sessions (`build-dryrun-exec-authority`, etc.) with per-child message counts — cheap observability that Cambium's supervisor event log should emulate (task_id + name + turn + last-activity).
6. **JSONL session tree with compaction summaries is a proven durable-state model.** The compaction carry-forward (goal/constraints/progress/blocked/decisions) kept a 1,988-message orchestration session coherent. Cambium's checkpoint-per-turn should include a comparable lossy-but-valuable summary, not just raw trajectory.
7. **Credentials: don't repeat plaintext keys.** Prime Agent stores API keys in plaintext JSON; Cambium's design (M4, secrets via env references) is the right call and this is the empirical justification.
8. **Self-hosting dogfooding works.** Prime Agent's own QA session investigated its daemon memory bug with canaries and objective numbers — the same evidence discipline Cambium's design ("canaries + objectively verifiable stats") prescribes.

## 5. Local install evidence

| Fact | Command | Output (trimmed) |
|---|---|---|
| Binary is a symlink to npm global | `file /home/ubuntu/.local/bin/prime-agent` | `symbolic link to /home/ubuntu/.local/npm-global/bin/prime-agent` |
| Version | `prime-agent --version` | `0.7.1` |
| Help banner | `prime-agent --help` | `prime-agent - AI coding assistant with an IPython tool` (options: `--provider/--model/--thinking`, `-c/--resume/--fork`, `-t/--no-tools`, `--autonomous` w/ gates+limits, commands `agents/list/attach/stop/send/schedule/status/doctor/shutdown/package/update/model/session/config`) |
| Node requirement | source of `/home/ubuntu/.local/npm-global/bin/prime-agent` | `MIN_NODE_VERSION_PARTS = [22, 8, 0]`; "prime-agent requires Node 22.8.0 or newer" |
| Package identity | `/home/ubuntu/.local/npm-global/lib/node_modules/prime-agent/package.json` | `"name": "prime-agent", "version": "0.7.1", "description": "Coding agent CLI with IPython-backed tools and session management"`, deps `@earendil-works/pi-agent-core/-ai/-tui` (R2 tarballs), `@agentclientprotocol/sdk`, `zeromq`, `undici` |
| npm registry name 404s | `npm view prime-agent version` | `npm error 404 Not Found - GET https://registry.npmjs.org/prime-agent` |
| Install command | `grep argv ~/.npm/_logs/2026-08-09T18_51_34_372Z-debug-0.log` | `verbose argv "install" "--global" "/tmp/prime-agent-0.7.1.tgz"` |
| Config dir layout | `ls ~/.prime/agent/` | `AGENTS.md, APPEND_SYSTEM.md, SYSTEM.md, auth.json, models.json, settings.json, telemetry.json, harness/, kernel-venv/, sessions/, session-leases/, logs/, archive-2026-08-06/` |
| Default model config | `cat ~/.prime/agent/settings.json` | `defaultProvider: opencode-go`, `defaultModel: deepseek-v4-flash`, `defaultThinkingLevel: high`, retry `{maxRetries:2, baseDelayMs:3000}`, compaction `{reserveTokens:24576, keepRecentTokens:24000}` |
| Custom providers | `cat ~/.prime/agent/models.json` | providers `nvidia`, `opencode-go`, `tokenrouter` (raw `apiKey` values present, redacted here) |
| Auth providers | keys of `cat ~/.prime/agent/auth.json` | `google, openrouter, zai, kimi-coding, openai-codex` |
| Available models | `prime-agent model list` | 399 lines: google (gemini-2.x/3.x), openai-codex (gpt-5.1…gpt-5.6-sol/terra/luna), kimi-coding, nvidia minimax-m3, opencode, opencode-go, tokenrouter |
| Daemon status | `prime-agent status` | `socket /tmp/prime-agent-1001/daemon.sock  pid 1038512  version 0.7.1  status current  sessions 8  uptime 1h` |
| Active named agents | `prime-agent list` | `exec-test-inventory, review-dryrun-config-parity, review-dryrun-exec-authority, b2-spec, docs-dryrun-inventory, build-dryrun-config-parity, build-dryrun-exec-authority` (all `opencode-go/deepseek-v4-flash`) |
| Session corpus stats | python over `~/.prime/agent/sessions/*.jsonl` | 19 files, 7,397 lines, 2,945 `message` events, 1,714 `child_usage_attributed` events, 4 `compaction` events |
| Log levels | python over `~/.prime/agent/logs/agent.jsonl` | `{'warn': 886, 'error': 6}` |
| OOM evidence | `grep -c "JavaScript heap out of memory" ~/.prime/agent/logs/agent.jsonl` | `6` (V8 `FATAL ERROR: Reached heap limit` from worker `4604e3b511db`) |
| Daemon timeout/attach failures | `grep -c "Timed out" …` / `grep -c "Unknown active session" …` | `36` / `57` |
| Worktree usage | `cat ~/.prime/worktrees/core-audit-7f40/.git` | `gitdir: /home/ubuntu/polymarket-arbitrage/.git/worktrees/core-audit-7f40` (a `git worktree` of the target repo, detached HEAD at `8a6472b5e`) |
| Recent work (topics) | first-user-message extraction over sessions | 2026-08-08: polymarket-arbitrage log/markout analysis ("TradeEvil"→`trade_eval`), bench-harness eval prompts (`df -h /`, process/RAM queries), session resume tests, "PRIME OK" smoke test. 2026-08-09: "internal QA engineer on prime-agent's own daemon/worker infrastructure" (memory-failure bug), and a 662-message orchestration session ("Always use subagents in worktrees... keep at least 7 subagents running in parallel... streamline individual modules"). Worktrees `core-audit-7f40`, `streamline-audit-5de25641` match the "core audit" and "streamline audit" tasks. |
| Kernel runtime | `ls ~/.prime/agent/kernel-venv/bin/` | IPython venv (`activate`, `ipykernel`-managed; system python has no `ipykernel`) |

## 6. Sources

- GitHub repo (stars/forks/commits/README): https://github.com/PrimeIntellect-ai/prime-agent
- Installed README: `/home/ubuntu/.local/npm-global/lib/node_modules/prime-agent/README.md`
- Installed CHANGELOG (0.7.1 released 2026-08-07, PR/issue links to PrimeIntellect-ai/prime-agent): `/home/ubuntu/.local/npm-global/lib/node_modules/prime-agent/CHANGELOG.md`
- Installed architecture doc: `/home/ubuntu/.local/npm-global/lib/node_modules/prime-agent/docs/architecture.md`
- Local config/sessions/logs: `~/.prime/agent/*`, `~/.prime/worktrees/*`, `~/.npm/_logs/2026-08-09T18_51_34_372Z-debug-0.log`
- **UNVERIFIED (claimed by project, not independently checked):** arXiv continual-harness paper `arxiv.org/abs/2605.09998`; the RLM design blog post `https://www.primeintellect.ai/blog/rlm`; accuracy of the 10.8k-star figure beyond the fetched page snapshot.

## Stats (objectively verifiable)

1. GitHub repo metrics at fetch time: **10.8k stars, 1.1k forks, 4,480 commits** (https://github.com/PrimeIntellect-ai/prime-agent).
2. Local session corpus: **19 session files, 7,397 JSONL lines, 2,945 messages, 1,714 child-usage events, 4 compactions** (computed from `~/.prime/agent/sessions/*.jsonl`).
3. Local `agent.jsonl`: **886 warn + 6 error** lines; **6** V8 heap-OOM traces; **36** "Timed out" + **57** "Unknown active session" daemon-supervisor failures.
4. `prime-agent model list` exposes **399 lines / ~398 models** across 8 providers.
5. Version **0.7.1**, minimum Node **22.8.0**, installed via `npm install --global /tmp/prime-agent-0.7.1.tgz`.
