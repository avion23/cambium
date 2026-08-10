# Research: `pi` — @earendil-works/pi-coding-agent

**Date:** 2026-08-09. **Purpose:** Cambium system-design input. Local facts cite commands; web facts cite URLs; anything not directly observed is **UNVERIFIED**. Product snapshot: `@earendil-works/pi-coding-agent` 0.84.1.

## What it is / stack

`pi` is a published MIT npm package by Mario Zechner: Node.js (>=22.19.0) terminal coding agent from `github.com/earendil-works/pi`, with `pi-coding-agent`, `pi-agent-core`, `pi-ai`, `pi-tui`, `pi-protocol`, and `pi-client`. Local runtime is Node 22.23.2/npm 10.9.8; `~/.local/bin/pi` links to `dist/cli.js` in the npm-global install.

Surfaces are interactive TUI, print/JSON (`-p`), RPC (`--mode rpc`), and SDK (`--mode sdk`). Extensions are TypeScript; Skills, prompt templates, themes, and installable npm/git Pi Packages provide the rest. `~/.pi/agent` contains settings, structure-only auth, models, subagents, extensions, sessions, packages, and a catalog cache; it is a git repo with commit `69da669 chore(pi): track agent config baseline`.

### Objectively verifiable stats

| fact | evidence |
|---|---|
| version | `pi --version` → `0.84.1` |
| weekly downloads | 1,637,586 for 2026-08-02..08-08, npm API |
| upstream snapshot | 86.0k stars, 10.7k forks, 5,582 commits, GitHub page |
| local sessions | 20 JSONL files, 1,456,622 bytes; largest 1,390,461 |
| providers/models | 7 auth providers; 7 enabled models across 3 providers |

### Local config (verified)

`settings.json` sets `openai-codex/gpt-5.6-sol`, medium thinking, budgets minimal 1024/low 4096/medium 10240/high 32768, compaction reserve 16,384/keep 20,000, retry 3 with 2-second base delay, project trust `always`, and subagents max parallel 12 with worktrees. Auth/model files contain plaintext key material (values not reproduced); models add Micu, Kimi, and OpenCode-Go providers with context windows up to 1,048,576. Historical local config observed on 2026-08-09 also included `~/.pi/agent/pi-codex-conversion.json` with `responsesCompaction: true`, and `~/.pi/agent/openai-server-compaction.json` with `enabled: true`, `thresholdRatio: 0.7`, and `usePreviousResponseId: true`; these paths and values document inspected configuration, not a claim about current runtime behavior. Installed packages include `@narumitw/pi-retry`, `@tintinweb/pi-subagents`, `pi-web-access`, `pi-lens`, and `@howaboua/pi-codex-conversion`. Local extensions protect `.git`, `node_modules`, auth, and `.env*`, and block `sudo`.

Role files route planner → Sol/medium, worker → Luna/high with worktree and 40 turns, luna → Luna/low, reviewer → Sol/medium read-only, scouts → read-only with agent/edit/write/bash disallowed. `subagents.json` sets max concurrent 12, group join, scoped models, and disables defaults.

Recent sessions: 2026-08-07 smoke/model checks; six repetitive `status`/`doctor` sessions on 2026-08-09; benchmark prompts in `/home/ubuntu/bench-harness`; and Polymarket configuration work. This is local usage evidence, not a general reliability result.

## What it does well

1. Multi-provider and OpenAI-compatible routing is built in; local configs demonstrate OpenAI OAuth, Google, Groq, NVIDIA, OpenRouter, Z.AI, Kimi, TokenRouter, and proxies.
2. Extensions/packages supply subagents, plan mode, retries, permissions, and web access without forking core.
3. Append-only JSONL sessions support resume/fork, compaction, and HTML export; the format aligns with Cambium’s event-log direction.
4. Supply-chain controls include exact dependencies, shrinkwrap, `--ignore-scripts`, lockfile checks, and scheduled audit.
5. Git-tracked config and worktree-isolated role files are reproducible delegation patterns.

## What it does poorly / limitations

1. **No built-in permission system.** The upstream README says Pi runs with the invoking user’s permissions. The local trust setting is `always`; safety came from custom extensions blocking `sudo`, `.git`, auth, and env files.
2. **Orchestration is add-on code.** Subagents/plan mode are packages, so scheduling, timeout, and recovery quality depend on them rather than an in-core supervisor.
3. **Plaintext credentials:** `auth.json` and `models.json` hold provider keys/tokens; encryption was not observed locally.
4. **Session growth/heuristic compaction:** a 1.39 MB session and ratio-based compaction can silently discard task context.
5. **Repetition and naming evidence:** five doctor sessions returned “needs a target” repeatedly; a session showed `nvidia/nvidia/nemotron...` provider duplication. Generality is **UNVERIFIED**.
6. No vault integration was observed; non-Codex providers use static keys/proxy credentials.

## Relevant lessons for Cambium

- Diffundo’s provider cascade is validated by real multi-provider use, but secrets must be environment references or a dedicated 0600 store, never config values.
- Permissionless UX requires a sandbox: keep per-task allowlists and Septum in-core rather than relying on extensions.
- Borrow worktree isolation, role prompts, `max_turns`, thinking budgets, and disallowed tools; enforce timeout/watchdog centrally.
- Add a repetition/doom-loop guard for unactionable prompts. Keep event logs, but make tool calls first-class events; inspected Pi JSONL exposed session/model/message events only.
- Config-as-git is useful for drift review. Use explicit model normalization and credential preflight (`pi auth check`) before execution.

## Local install evidence

```text
$ file /home/ubuntu/.local/bin/pi
symbolic link to /home/ubuntu/.local/npm-global/bin/pi
$ pi --version
0.84.1
$ node --version; npm --version
v22.23.2; 10.9.8
$ pi --help
text/json/rpc modes; -p; resume/fork; tools/thinking/extension/skill/auth options
$ git -C ~/.pi/agent log --oneline
69da669 chore(pi): track agent config baseline
$ pi list
five user packages (listed above)
```

Session scans decoded `session`, `model_change`, `thinking_level_change`, and `message` JSONL events. Auth/model values were read only for structure and are not reproduced. The largest local session (1,390,461 bytes) belongs to Polymarket configuration work; the six 2026-08-09 doctor sessions all used gpt-5.6-sol and repeatedly returned “needs a target.” That is direct local evidence for the repetition concern, not a population-level failure rate.

The local model catalog included OpenAI-Codex context 272,000 and Kimi K3 context 1,048,576 with max output 131,072. `models.json.bak-bench` and `auth.json.bak-bench` timestamps matched the benchmark runs, showing that provider/model files were changed during evaluation. `subagents.json` has `defaultJoinMode: group`, `scopeModels: true`, and `disableDefaultAgents: true`; role Markdown files add max-turns and read-only restrictions. These controls are useful patterns, but package-level scheduling and timeout behavior were not independently audited.

The upstream README’s deliberately minimal core (“No sub-agents, No plan mode, No permission popups, No MCP, No to-dos, No background bash”) explains why the local operator installed packages and extensions for each capability. This is a strong extensibility finding, not evidence that those packages share one reliability boundary.

The local extension set illustrates both the value and cost of that boundary: `opencode-transfer-safety.ts` blocks writes to `.git/`, `node_modules/`, `.pi/agent/auth.json`, and `.env*`; `permission-gate.ts` blocks `sudo` but allows `ssh`; `plan-mode/` and `pi-subagents` add orchestration. These policies are user code and can drift from the agent core. Cambium should keep analogous controls in the worker init frame and supervisor rather than requiring every operator to install them.

Pi’s `--mode json`/`rpc` and SDK are useful machine surfaces, but local session JSONL did not expose tool-call events in the inspected messages. Cambium should retain the append-only shape while recording tool invocation, approval, heartbeat, checkpoint, gate, and merge events as first-class records so replay can distinguish model text from side effects.

The installed package changelog was dated 2026-08-07 and mentioned `pi auth check`, fullscreen TUI work, Qwen Token Plan, and extension `terminate`. These are inspected version facts, not stable API guarantees. The npm page itself returned HTTP 403, so registry metadata—not the page—supports the package name, version, MIT license, repository, and Node engine claims.

The package’s two-hop symlink path (`~/.local/bin/pi` → npm-global bin → `dist/cli.js`) and Node 22 floor are part of the local install provenance. The config repo’s single baseline commit is not upstream history; it records only this operator’s starting state. Keep those provenance layers separate in future comparisons.

## Sources

- Local install/config/session commands above, inspected 2026-08-09; package metadata under `/home/ubuntu/.local/npm-global/lib/node_modules/@earendil-works/pi-coding-agent/`.
- https://github.com/earendil-works/pi
- https://www.npmjs.com/package/@earendil-works/pi-coding-agent — direct fetch returned **HTTP 403**; equivalent metadata came from https://registry.npmjs.org/@earendil-works/pi-coding-agent (0.84.1, MIT, Node >=22.19.0, repo/bin).
- https://api.npmjs.org/downloads/point/last-week/@earendil-works/pi-coding-agent
- Installed `README.md` and `CHANGELOG.md` (0.84.1 dated 2026-08-07; `pi auth check`, fullscreen TUI, extension `terminate`).
Pi’s config-as-git approach is especially cheap: a single baseline commit makes model/role drift reviewable without a database migration. The security lesson is equally concrete because the same directory contains secret-bearing model files; reproducibility and credential separation must be designed together.
Future snapshots should distinguish package/core capabilities from installed third-party extensions, which supplied permissions, plan mode, retries, and subagents locally.
