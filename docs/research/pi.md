# Research: `pi` — @earendil-works/pi-coding-agent

Competitive analysis of the locally installed `pi` coding agent, prepared as input to the
Cambium system design (`docs/architecture/system-design.md`).

Date: 2026-08-09. All local facts cite the exact command + trimmed output. Web facts cite URLs.
Anything not directly observed is marked **UNVERIFIED**.

---

## What it is / stack

`pi` is not a wrapper script — it is a real, published npm package: `@earendil-works/pi-coding-agent` v0.84.1,
a Node.js (>=22.19.0) terminal coding agent CLI by Mario Zechner, MIT-licensed, from the monorepo
`github.com/earendil-works/pi` (packages: `pi-coding-agent`, `pi-agent-core`, `pi-ai`, `pi-tui`, `pi-protocol`, `pi-client`).

- Install shape: `~/.local/bin/pi` is a two-hop symlink to `dist/cli.js` in the npm-global install.
- Runtime: Node v22.23.2, npm 10.9.8 (local).
- Modes: interactive TUI, print (`-p`) / JSON, RPC (`--mode rpc`), and an SDK (`--mode sdk`/programmatic).
- Extension system: TypeScript extensions + Skills + Prompt Templates + Themes + installable "Pi Packages" (npm/git).
- Config dir `~/.pi/agent`: `settings.json`, `auth.json`, `models.json`, `subagents.json`, `agents/*.md`,
  `extensions/`, `sessions/` (JSONL), `npm/` (installed packages), plus a `models-store.json` catalog cache.
- The config dir is itself a git repository (single commit: `69da669 chore(pi): track agent config baseline`).
- Session log format: append-only JSONL with event types `session`, `model_change`, `thinking_level_change`, `message`.

Objectively verifiable stats:

| # | Stat | Evidence |
|---|------|----------|
| 1 | Installed version `0.84.1` | `pi --version` → `0.84.1` |
| 2 | `1,637,586` weekly npm downloads (2026-08-02..08-08) | `api.npmjs.org/downloads/point/last-week/@earendil-works/pi-coding-agent` |
| 3 | GitHub: **86.0k stars**, **10.7k forks**, **5,582 commits** | `https://github.com/earendil-works/pi` |
| 4 | **20** session JSONL files, **1,456,622** bytes total | `find ~/.pi/agent/sessions` (largest single session 1,390,461 bytes) |
| 5 | **7** providers in `auth.json`; **7** enabled models across **3** providers in `settings.json` | `cat ~/.pi/agent/auth.json`, `cat ~/.pi/agent/settings.json` |

### Local config (verified)

`settings.json` (trimmed):
```json
{
  "defaultProvider": "openai-codex",
  "defaultModel": "gpt-5.6-sol",
  "defaultThinkingLevel": "medium",
  "thinkingBudgets": { "minimal": 1024, "low": 4096, "medium": 10240, "high": 32768 },
  "compaction": { "enabled": true, "reserveTokens": 16384, "keepRecentTokens": 20000 },
  "retry": { "enabled": true, "maxRetries": 3, "baseDelayMs": 2000 },
  "enabledModels": [
    "openai-codex/gpt-5.6-sol", "openai-codex/gpt-5.6-terra", "openai-codex/gpt-5.6-luna",
    "zai/glm-5.2", "kimi/k3-256k", "kimi/k3", "kimi/kimi-for-coding-highspeed"
  ],
  "defaultProjectTrust": "always",
  "subagents": { "maxParallel": 12, "worktree": true }
}
```

- `auth.json` provider keys (structure only; **raw API keys stored in plaintext — not reproduced here**):
  `google`, `groq`, `nvidia`, `openrouter`, `zai`, `openai-codex` (OAuth refresh-token flow), `tokenrouter`.
  `models.json` additionally defines `micu-vip2` (OpenAI-compatible proxy), `kimi`, and `opencode-go`
  providers, each with inline `apiKey` strings. Model catalog entries carry `contextWindow` (e.g.
  openai-codex 272,000; kimi/k3 1,048,576) and `maxTokens` (e.g. 131,072).
- Installed packages (`pi list`): `npm:@narumitw/pi-retry`, `npm:@tintinweb/pi-subagents`,
  `npm:pi-web-access`, `npm:pi-lens`, `npm:@howaboua/pi-codex-conversion`.
  Plus local TS extensions in `~/.pi/agent/extensions/`: `opencode-transfer-safety.ts`
  (blocks write/edit to `.git/`, `node_modules/`, `.pi/agent/auth.json`, `.env*`), `permission-gate.ts`
  (blocks `sudo` unconditionally, allows `ssh`), and a `plan-mode/` package dir.
- Subagent definitions in `~/.pi/agent/agents/*.md` route by model/thinking:
  planner → `gpt-5.6-sol` medium, worker → `gpt-5.6-luna` high (worktree isolation, max_turns 40),
  luna → `gpt-5.6-luna` low (max_turns 10), reviewer → `gpt-5.6-sol` medium (max_turns 30),
  scout/scouts → `gpt-5.6-sol` low/medium (read-only, `disallowed_tools: Agent, Edit, Write, Bash` for scout).
  `subagents.json`: `maxConcurrent: 12`, `defaultJoinMode: group`, `scopeModels: true`, `disableDefaultAgents: true`.

### Recent local activity (sessions under ~/.pi/agent/sessions)

| Cwd | Dates | Topics (from user prompts) |
|-----|-------|---------------------------|
| `/home/ubuntu` | 2026-08-07 | Smoke tests: "Reply with exactly: PI OK" (openai-codex/gpt-5.6-sol), "PI KIMI OK" (kimi/k3-256k); `models` listing |
| `/home/ubuntu` | 2026-08-09 | Six sessions, all gpt-5.6-sol: 1×`status` + 5×`doctor`; assistant repeatedly replied "needs a target" without resolving — a repetitive-response loop |
| `/home/ubuntu/bench-harness` | 2026-08-08 | Benchmark-style single commands (`df -h /`, top-RAM/zombie inspection) run across gpt-5.6-sol, gemini-2.5-pro, openrouter/auto, zai/gpt-5.2, nvidia/nemotron-3-ultra-550b, opencode-go/deepseek-v4-flash — a multi-provider eval harness |
| `/home/ubuntu/polymarket-arbitrage` | 2026-08-01 | Config work: made pi "permissionless" (`defaultProjectTrust: "always"`), fixed `codebase-memory/SKILL.md:3` YAML frontmatter, then reverted over-scoped changes. 1,390,461-byte session |

(`models.json.bak-bench` / `auth.json.bak-bench` timestamps match the bench-harness runs.)

---

## What it does well

1. **Multi-provider by default.** One `pi` binary talks to OpenAI Codex (OAuth), Google, Groq, Nvidia,
   OpenRouter, Z.AI, Kimi, TokenRouter, and arbitrary OpenAI-compatible proxies, with per-model
   `contextWindow`/`maxTokens`/thinking maps. The local install demonstrates a real user running
   many providers in parallel (incl. a proxy cascade in `bench-harness`). `pi auth check` (0.84.1)
   adds provider/model credential preflight.
2. **Extensibility over built-ins.** README philosophy: "No sub-agents, No plan mode, No permission
   popups, No MCP, No to-dos, No background bash" — the local install confirms users install these
   as packages (`pi-subagents`, `plan-mode`, `pi-retry`) and local TS extensions instead of forking.
3. **Sessions as structured event logs.** Append-only JSONL (`session`/`model_change`/`message` events),
   with `--resume`, `--fork`, compaction, and HTML export. Aligns with Cambium's append-only JSONL design.
4. **Supply-chain hardening.** Pinned exact deps, shipped `npm-shrinkwrap.json`, `--ignore-scripts`
   installs, lockfile commit guard, scheduled `npm audit`.
5. **Config-as-git-repo.** `~/.pi/agent` is a git repo, so config/model/auth drift is diffable and revertable.
6. **Subagent isolation via worktree.** `isolation: worktree`, per-agent `max_turns`, `thinking` level,
   and `disallowed_tools` give structured delegation (the `agents/*.md` files are hand-written role prompts).

## What it does poorly / limitations

1. **No permission system.** GitHub README: "Pi does not include a built-in permission system… runs with
   the permissions of the user and process that launched it." Combined with `defaultProjectTrust: "always"`,
   the local install is permissionless by default; the user had to write `permission-gate.ts` (block `sudo`)
   and `opencode-transfer-safety.ts` (protect `.env`, `.git/`, auth.json) extensions to compensate.
2. **Orchestration is bolted on.** Sub-agents/plan mode are third-party packages, not core; reliability,
   timeouts, and scheduling depend on those packages' quality. Cambium's supervisor/worker process model
   with per-subagent timeouts and watchdog is the stronger, in-core design.
3. **Plaintext API keys at rest.** `auth.json` and `models.json` store provider keys/secrets in plaintext
   in the config dir. **UNVERIFIED** whether pi ever encrypts them; local evidence says no.
4. **Session bloat + heuristic compaction.** A single 1.39 MB session; compaction is a token heuristic
   (`thresholdRatio: 0.7`, `keepRecentTokens: 20000`, `reserveTokens: 16384`) — context can be silently
   dropped mid-task, exactly the risk Cambium's "single-attempt compaction guard" addresses.
5. **Repetitive-response loops.** The five identical `doctor` sessions (assistant replying "needs a
   target" each time) show pi will keep re-answering an unactionable prompt rather than bailing;
   there is no observed doom-loop detector. (Local observation; generality **UNVERIFIED**.)
6. **Provider naming quirks.** Sessions show `nvidia/nvidia/nemotron-3-ultra-550b-a55b` (doubled
   provider prefix) — catalog/normalization is user-maintained and inconsistent.
7. **Auth is key-in-file driven.** OAuth only for `openai-codex`; all other providers are static keys,
   expiring tokens, or proxy keys. No vault integration observed locally.

## Relevant lessons for Cambium

- **FanOut (M2) matches real usage.** This user runs 6+ providers in parallel and even built a
  cross-provider bench harness. Multi-provider cascade/race/cache is validated; pi's static
  key-in-json config is a gap Cambium's provider abstraction should close.
- **Permissionless is desired, sandbox is required.** The user's own session: "can we make pi
  permissionless?" — and then had to hand-write extensions to block `sudo` and protect `.env`/auth files.
  Cambium's Septum (M8) + tool allowlists (per-task `permissions` in the M1 init frame) is the
  correct in-core answer; do not ship permissionless-without-sandbox.
- **Subagent pattern to steal:** worktree isolation, per-agent `max_turns` + thinking budget +
  `disallowed_tools`, and role prompts (planner→worker→reviewer, read-only scouts). Cambium's
  Architectus/Opifex division already mirrors this; add explicit `max_turns`/timeout enforcement
  (pi leaves it to package quality).
- **Repetition guard matters.** The `doctor` loop is evidence models do not self-terminate on
  unactionable requests; Cambium's doom-loop detector (turn counting + repetition + convergence) is a
  differentiator.
- **Event-log sessions are validated.** pi's JSONL `session`/`model_change`/`message` format matches
  Cambium's append-only JSONL + replay design. Worth also logging tool calls as first-class events
  (local pi sessions did not expose `tool_call` events in the JSONL messages I inspected).
- **Config-as-git-repo is a cheap, useful pattern** for tracking harness/config drift over time.
- **Secrets handling:** do not repeat pi's plaintext key files; keep secrets out of the config dir
  or in a dedicated 0600 store (see `token-manager` skill).

## Local install evidence

```bash
$ file /home/ubuntu/.local/bin/pi
/home/ubuntu/.local/bin/pi: symbolic link to /home/ubuntu/.local/npm-global/bin/pi
$ file /home/ubuntu/.local/npm-global/bin/pi
.../npm-global/bin/pi: symbolic link to ../lib/node_modules/@earendil-works/pi-coding-agent/dist/cli.js
$ pi --version
0.84.1
$ pi --help   # first lines
pi - AI coding assistant with read, bash, edit, write tools
Usage: pi [options] [@files...] [messages...]
  pi install/remove/update/list/config/auth ...    --provider <name> --model <pattern> ...
  --mode <mode>: text (default), json, or rpc   -p non-interactive   -c/-r resume   --fork ...
  --no-session  --models <patterns>  --tools/-t allowlist  --thinking <off|minimal|low|medium|high|xhigh|max>
  --extension/-e  --skill  --no-skills  --no-context-files  --approve/-a  --offline ...
$ node --version; npm --version
v22.23.2
10.9.8
$ ls ~/.pi/agent   # excerpt, listing trimmed for brevity
AGENTS.md  agents/  auth.json  auth.json.bak-bench  cache/  extensions/  git/  models-store.json
models.json  models.json.bak-bench  npm/  REALTIME-SYSTEM-PROMPT.md  sessions/  settings.json  subagents.json
$ git -C ~/.pi/agent log --oneline
69da669 chore(pi): track agent config baseline
$ pi list
User packages: npm:@narumitw/pi-retry, npm:@tintinweb/pi-subagents, npm:pi-web-access,
npm:pi-lens, npm:@howaboua/pi-codex-conversion
```

Session scans (topics/dates) were produced by decoding the JSONL events with a small python script
(`session`, `model_change`, `message` event types); the `bench-harness` and `polymarket-arbitrage`
rows above are transcript summaries, not verbatim.

## Sources

- Local install: commands in "Local install evidence" (this document), run 2026-08-09.
- `https://github.com/earendil-works/pi` — README, stars/forks/commits, package list, permissions/containerization section.
- `https://www.npmjs.com/package/@earendil-works/pi-coding-agent` — **UNVERIFIED directly (HTTP 403 on fetch)**;
  equivalent data obtained from `https://registry.npmjs.org/@earendil-works/pi-coding-agent` (name, latest 0.84.1,
  MIT, repo URL, engines node >=22.19.0, bin `dist/cli.js`).
- `https://api.npmjs.org/downloads/point/last-week/@earendil-works/pi-coding-agent` — weekly download count.
- Installed package metadata: `/home/ubuntu/.local/npm-global/lib/node_modules/@earendil-works/pi-coding-agent/package.json`,
  `README.md`, `CHANGELOG.md` (0.84.1 dated 2026-08-07: `pi auth check`, fullscreen TUI, Qwen Token Plan, extension `terminate`).
- `pi --help` full output (not reproduced here beyond the excerpt above).
