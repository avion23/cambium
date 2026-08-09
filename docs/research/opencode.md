# OpenCode (anomalyco/opencode) — Competitive Analysis for Cambium

**Date:** 2026-08-09
**Author:** research subagent (opencode's own `general` agent, running inside the tool under study)
**Verification policy:** every local-install claim cites the exact command run; every web claim cites the URL; anything unverified is marked **UNVERIFIED**.

---

## 1. What it is / stack

OpenCode is an open-source AI coding agent with a terminal UI, desktop app, web UI, and headless server. It is the tool this very document was produced with. Project home moved from `sst/opencode` to `anomalyco/opencode` (GitHub page headers and docs footer now say "Anomaly"; `sst` URL redirects).

| Concern | Finding | Source |
|---|---|---|
| Repo health | 195k stars, 25.1k forks, 15,366 commits on `dev`, 3.8k open issues, 1.2k PRs (fetched 2026-08-09) | https://github.com/anomalyco/opencode |
| Language / package manager | TypeScript monorepo, Bun (`"packageManager": "bun@1.3.14"`), turbo, workspaces under `packages/*` | https://raw.githubusercontent.com/anomalyco/opencode/dev/package.json |
| Runtime model | Bun-compiled single-file binary; server/client split. Local binary is a 140 MB ELF aarch64 executable (see §5) | local + https://github.com/anomalyco/opencode |
| TUI framework | **OpenTUI** (`@opentui/core`, `@opentui/solid`, `@opentui/keymap` 0.4.5) + **Solid.js** 1.9.10. NOT Ink/React on the current dev branch. | https://raw.githubusercontent.com/anomalyco/opencode/dev/packages/tui/package.json |
| Core runtime | **Effect** 4.0.0-beta.83 (effectful session runtime); storage via **Drizzle ORM + SQLite** (`drizzle-orm` 1.0.0-rc.2, `effect-drizzle-sqlite`, `effect-sqlite-node`) | https://raw.githubusercontent.com/anomalyco/opencode/dev/package.json |
| HTTP layer | **Hono** 4.10.7 server; `@opencode-ai/client` + `sdk` generated from a single authoritative `HttpApi` via `httpapi-codegen` (Promise and Effect emitters, "SDK Contract IR") | https://raw.githubusercontent.com/anomalyco/opencode/dev/CONTEXT.md |
| Provider abstraction | **AI SDK** (`ai` 6.0.168) with 17+ `@ai-sdk/*` adapters (openai, anthropic, google, google-vertex, groq, mistral, amazon-bedrock, azure, cerebras, cohere, deepinfra, xai, togetherai, perplexity, etc.), plus OpenRouter, OpenCode Zen, and arbitrary OpenAI-compatible providers | https://raw.githubusercontent.com/anomalyco/opencode/dev/packages/opencode/package.json |
| Protocol integrations | MCP client (`@modelcontextprotocol/sdk` 1.29.0), ACP server (`@agentclientprotocol/sdk`), LSP via `vscode-jsonrpc` + tree-sitter (bash, powershell) | https://raw.githubusercontent.com/anomalyco/opencode/dev/packages/opencode/package.json |

**How the agent loop works (from `CONTEXT.md`, a spec doc in-repo):**
- A session is durable history (SQLite). Each model request is a **Provider Turn**; the model-visible context is **Session History** plus an immutable **Baseline System Context** that stays fixed for a **Context Epoch** — deliberately structured so the provider's prompt cache has a stable prefix. Context changes arrive as **Mid-Conversation System Messages** admitted only at a **Safe Provider-Turn Boundary**.
- System context is assembled from ordered, keyed **Context Sources** (e.g., `AGENTS.md` instruction files, current date, selected-agent available-skill guidance) via a **System Context Registry**.
- Tools are registered in a **Tool Registry**; each tool result is bounded to a configurable max lines/bytes, and oversized output spills to **Managed Tool Output Files** under a shared directory, keeping the durable transcript small.
- Agents come in two kinds: **primary** (build/plan, user-selectable via Tab) and **subagents** (invoked by the model through a `task` tool or by `@mention`). Subagents run as child sessions. Nesting is capped by `subagent_depth` (default 1).

---

## 2. What it does well

1. **Provider/model breadth.** 17+ AI-SDK adapters plus arbitrary OpenAI-compatible proxies and OpenCode Zen make it trivial to run any model. The local install defines 11 providers and 28 explicitly-named custom models in JSON (counting rule: `provider.<id>.models` entries in `opencode.json` only; see §5). Cached model catalog at `~/.cache/opencode/models.json` (3.6 MB).
2. **Granular, glob-based permission system.** Every tool can be `allow`/`ask`/`deny`, per agent and per command pattern, with last-matching-rule-wins semantics (e.g. `bash: { "*": "ask", "git diff": "allow" }`). Permissions also gate external directories, the `task` tool, and skill loading. — https://opencode.ai/docs/permissions/, https://opencode.ai/docs/agents/
3. **Extensibility without code.** Custom agents (JSON or Markdown frontmatter), skills (`SKILL.md`, discovered from `.opencode/skills`, `.claude/skills`, `.agents/skills`, Claude-compatible), MCP servers, custom tools, commands, and npm plugins — all config-driven. — https://opencode.ai/docs/agents/, https://opencode.ai/docs/skills/, https://opencode.ai/docs/plugins/
4. **Durable session UX.** Everything persists to SQLite: sessions, messages, parts, todos, permissions, share URLs, and per-session token/cost counters. Undo/redo is backed by internal git snapshots; sessions can be resumed (`-c`/`--session`), forked, exported, and shared as URLs. — local DB schema (§5), https://opencode.ai/docs/
5. **Deliberate context-epoch caching design.** The immutable Baseline System Context per epoch is an explicit scheme to keep the provider prompt-cache prefix stable across turns and restarts — a more sophisticated answer to "caching" than an app-level prompt hash. — https://raw.githubusercontent.com/anomalyco/opencode/dev/CONTEXT.md
6. **One API, many frontends.** A single `HttpApi` generates both the network client and the embedded in-process host (`sdk-next`); TUI, CLI (`opencode run`), web, desktop, and the GitHub/GitLab integrations all speak the same server. — https://raw.githubusercontent.com/anomalyco/opencode/dev/CONTEXT.md

---

## 3. What it does poorly / limitations

1. **Subagents run sequentially.** Confirmed by the maintainers themselves: issue #29638 "Subagents dispatched sequentially instead of in parallel" (closed **as not planned**), reporting that the session loop `tasks.pop()` → `handleSubtask(...)` blocks until each subagent finishes; the reporter suggested `Effect.forEach(..., { concurrency: "unbounded" })`. Parallel fan-out is not a supported pattern. — https://github.com/anomalyco/opencode/issues/29638
2. **Heavy resource footprint.** The binary is 140 MB; the global SQLite DB was 299 MB at measurement time (104 sessions / 5,362 messages / 22,754 parts, as of 2026-08-09T21:04:47Z — see §5); individual managed tool-output files reach ~1 MB each; the package/cache dir is 91 MB (see §5). This is a JS/Bun + Effect + SQLite stack — far heavier than Cambium's zero-runtime-dependency Python plan.
3. **Snapshot/undo indexing cost.** The docs themselves warn that snapshots "can cause slow indexing and significant disk usage as it tracks all changes using an internal git repository," and undo/redo only works inside a git repo. — https://opencode.ai/docs/config/ (Snapshot)
4. **Provider-package churn.** OpenCode "dynamically installs provider packages as needed and caches them locally"; the troubleshooting page's standard fix for API errors is `rm -rf ~/.cache/opencode`. Config carries a long legacy/deprecation tail (`maxSteps` → `steps`, `tools` → `permission`, `theme`/`keybinds` moved from `opencode.json` to `tui.json`). — https://opencode.ai/docs/troubleshooting/, https://opencode.ai/docs/agents/
5. **Compaction is a lossy LLM pass.** When context is full, a hidden `compaction` agent summarizes the session (auto-compaction, optional pruning). It burns an extra model call and can lose detail; the docs expose only coarse knobs (`reserved`, `prune`). There is no replay-based durable checkpoint as Cambium plans. — https://opencode.ai/docs/config/ (Compaction), https://opencode.ai/docs/agents/ (Built-in)
6. **App-level caching is essentially absent.** Cost/context management is delegated to provider-side prompt caching (a `setCacheKey` per-provider option), not an application cache. Cambium's own review process flagged that an app-level `(model, temp, prompt)` hash cache is a correctness hazard for a coding harness; OpenCode sidesteps it by not having one. — https://opencode.ai/docs/config/ (Models), cf. `docs/reviews/review-llm-design.md` in this worktree

**Secondhand limitations (UNVERIFIED against primary sources):** Cambium's `docs/system-design.md` cites two more OpenCode failures — "subagent without timeout hangs 20-30 min silently (OpenCode #11865)" and "bidirectional agent-to-agent messaging degenerates into ACK loops (OpenCode community)". Issue numbers were not re-verified; #29638 above IS verified.

---

## 4. Relevant lessons for Cambium

1. **Orchestrator/worker model.** OpenCode proves a single-process, event-loop orchestrator with cheap child "subagent sessions" is workable and gives excellent UX (parent/child session navigation, `@mention`, resume/fork). But its subagents are **sequential by design** (#29638, closed not-planned). Cambium's process-isolated workers with parallel fan-out are a genuine differentiator — keep them, but copy the cheap `@mention`/child-session ergonomics and the model-driven decomposition via a `task`-like tool.
2. **One authoritative API, many interfaces.** OpenCode's `HttpApi` → codegen IR → Promise/Effect clients → TUI/web/desktop/headless is the pattern Cambium's Janus (M10) should follow: define the wire protocol once (Cambium's Nuntius JSON-lines IPC is the analog) and build every interface against it, including an embedded in-process client for tests.
3. **TUI.** OpenCode moved to a real reactive component framework (OpenTUI + Solid.js) rather than hand-rolled rendering, with a separate `tui.json`, themes, keybinds, mouse, diff view, and attention/notifications. Cambium should pick an equivalent mature Python TUI stack (the current design leaves Janus unspecified) rather than building one.
4. **Permission model.** The allow/ask/deny + glob-pattern + last-match-wins scheme, applied per agent and even per bash command, is proven and should be copied for per-worker sandbox policies in Septum (M8). Note the do-loop guard (`doom_loop` permission) and external-directory gating — both relevant to Cambium's supervisor.
5. **Caching.** Do NOT ship a naive app-level prompt cache. OpenCode's correct instinct is: keep the model-visible context prefix stable across an epoch so the *provider's* cache hits, and bound per-turn tool output. Cambium's FanOut cache (flagged as a correctness hole in its own reviews) should either be keyed by worktree+HEAD, backed by a shared store, or dropped in favor of provider-side caching.
6. **Tool output management.** OpenCode's bounded model-visible preview + spill-to-file (Managed Tool Output Files) exactly solves the problem Cambium's event log will face with chatty workers. Adopt it: cap tool output in the durable log, keep full output in a managed directory, and log the path.
7. **Skills.** The `SKILL.md` format (frontmatter `name`+`description`, loaded on-demand via a `skill` tool, Claude/agent-compatible discovery) is a proven standard. Cambium workers should consume the same format so existing skills are portable into the harness.
8. **Compaction/checkpointing.** OpenCode's compaction is a lossy extra LLM call. Cambium's checkpoint-per-tool-call ReAct recovery is strictly better for crash recovery; use compaction only as a last-resort context reducer, not as the durability mechanism.

---

## 5. Local install evidence

Binary and version:

```
$ file /home/ubuntu/.local/bin/opencode
ELF 64-bit LSB executable, ARM aarch64, version 1 (SYSV), dynamically linked, interpreter /lib/ld-linux-aarch64.so.1, ... not stripped
$ /home/ubuntu/.local/bin/opencode --version
0.0.0-dev-202608071959
$ ls -la /home/ubuntu/.local/bin/opencode
-rwxr-xr-x 1 ubuntu ubuntu 146909328 Aug  7 20:00 opencode   # 140 MB
```

Config locations (both present, JSONC-capable per docs):

```
~/.opencode/           # 15 skill dirs, node_modules (plugin deps), bun.lock, .env (API keys for openai/openrouter/google/groq/z.ai/kimi/moltbook/websearch)
~/.config/opencode/    # opencode.json (809 lines, `wc -l`), tui.json, opencode.json.bak-*, skills/i-have-adhd, openai-compact/checkpoints.db (1.8MB WAL), .git (12 commits, `git log --oneline | wc -l`), patch-models-cache.py
```

Effective agents (`opencode agent list`): build, compaction, explore, general, plan, summary, title (built-ins) + deepseek, glm, kimi, luna, reviewer, sol (custom subagents). Config file (`~/.config/opencode/opencode.json`) defines 10 agents, 11 providers, and 28 explicitly-named custom models (counting rule: entries under `provider.<id>.models` in `opencode.json` only — catalog entries from `~/.cache/opencode/models.json` are not counted; `opencode models` would list far more). Style: pure-JSON, per-agent `model: "provider/model-id"` + `variant` + `mode` (`all`/`primary`/`subagent`) + `steps` + `permission`, with provider blocks declaring custom model names/limits/reasoning variants (e.g. `openai/gpt-5.6-sol`, `zai-coding-plan/glm-5.2`, `opencode-go/deepseek-v4-flash`). Default agent is `build` on `opencode-go/deepseek-v4-flash`.

Data store (single global SQLite DB; live and growing — all counts are a point-in-time snapshot as of the measurement moment, not stable truth):

```
$ sqlite3 ~/.local/share/opencode/opencode-dev.db "SELECT (SELECT count(*) FROM session), (SELECT count(*) FROM message), (SELECT count(*) FROM part);"
104|5362|22754        # as of 2026-08-09T21:04:47Z
$ stat -c %s ~/.local/share/opencode/opencode-dev.db   # 313,778,176 bytes (299 MB) as of 2026-08-09T21:04:47Z
$ du -sh ~/.cache/opencode                           # 91 M (incl. models.json 3.6 MB)
$ ls -la ~/.local/share/opencode/log/opencode.log    # 8,066,508 bytes, first entry 2026-08-08T14:21Z
```

The DB grew from the first draft of this doc (87/5,004/21,054; 292,880,384 B) to 104/5,362/22,754; 313,778,176 B within ~30 minutes because the research sessions themselves are writing to it.

Recent activity (all timestamps UTC, `sqlite3 ... SELECT title, datetime(time_updated/1000,'unixepoch'), agent FROM session ORDER BY time_updated DESC`): the immediately-preceding work is a **Cambium design/research sprint on 2026-08-09 20:38–20:52** in `/home/ubuntu/cambium`: "Designing Python coding agent with subagents" (build), "Design Cambium architecture (@sol subagent)" (GPT-5.6 Sol), and nine parallel `@general` research subagent sessions (Python 3.14, TUI best practices, Cloud Code, py.dev assistant, Prime Agent, OMP, Pi, Codex, and "Research OpenCode agent" — the session running this task), each costing $0.001–0.005. Prior to that: `polymarket-arbitrage` build work throughout 2026-08-09 (age-knob, tip-gate, telemetry, reviews via `@glm`), and `bench-harness` sessions on 2026-08-08. Log confirms `version=0.0.0-dev-202608071959` on every created session.

Git history of the config repo (`git -C ~/.config/opencode log --oneline -5`): `6a34b2c fix(config): use sol for planning` (2026-08-09 15:45), `9cb6ca3 fix(config): unify sol subagents`, `c3ec47f fix(config): switch deepseek agent to opencode-go, drop blocked zen free` (2026-08-07), `112d8ab feat(config): switch primary agents to opencode-go deepseek, disable openai+llama`, `13ee470 chore: set build/plan/explore/general to all mode` — shows active iteration on agent/model routing over the last week.

---

## 6. Sources

Web:
- https://github.com/anomalyco/opencode (repo, stars/forks/commits, README)
- https://opencode.ai/docs/ (intro, install, usage)
- https://opencode.ai/docs/agents/ (agent types, config, permissions)
- https://opencode.ai/docs/tools/ (built-in tool list, permissions)
- https://opencode.ai/docs/skills/ (SKILL.md format, discovery)
- https://opencode.ai/docs/config/ (schema: providers, compaction, snapshot, plugins, precedence)
- https://opencode.ai/docs/tui/ (TUI commands, tui.json)
- https://opencode.ai/docs/troubleshooting/ (logs, storage, provider-package cache)
- https://raw.githubusercontent.com/anomalyco/opencode/dev/package.json (root: bun, turbo, catalog versions)
- https://raw.githubusercontent.com/anomalyco/opencode/dev/packages/opencode/package.json (AI-SDK adapters, deps)
- https://raw.githubusercontent.com/anomalyco/opencode/dev/packages/tui/package.json (OpenTUI + Solid)
- https://raw.githubusercontent.com/anomalyco/opencode/dev/CONTEXT.md (session runtime spec: Context Epoch, System Context, Provider Turn, tool-output bounding)
- https://github.com/anomalyco/opencode/issues/29638 (sequential subagent dispatch, verified)

Local:
- `file` / `--version` / `ls -la` on `/home/ubuntu/.local/bin/opencode`
- `sqlite3` queries on `~/.local/share/opencode/opencode-dev.db`
- `ls -laR` of `~/.opencode`, `~/.config/opencode`, `~/.cache/opencode`, `~/.local/share/opencode`
- `opencode agent list`, `opencode --help`
- `git -C ~/.config/opencode log --oneline -10`
- `tail` of `~/.local/share/opencode/log/opencode.log`

**Version pin:** this analysis reflects OpenCode `0.0.0-dev-202608071959` (locally installed) and upstream `dev` branch at package version 1.18.15, both fetched 2026-08-09.
