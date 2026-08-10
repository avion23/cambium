# OpenCode (anomalyco/opencode) — Competitive Analysis for Cambium

**Date:** 2026-08-09. **Author:** research subagent running inside OpenCode. **Verification:** local claims cite commands; web claims cite URLs; unsupported items are **UNVERIFIED**. Snapshot versions: local `0.0.0-dev-202608071959`; upstream `dev` package 1.18.15.

## 1. What it is / stack

OpenCode is an open-source coding agent with TUI, desktop, web, and headless server surfaces. The project moved from `sst/opencode` to `anomalyco/opencode`.

| Concern | Finding | Source |
|---|---|---|
| Repo snapshot | 195k stars, 25.1k forks, 15,366 commits, 3.8k open issues, 1.2k PRs | https://github.com/anomalyco/opencode |
| Build/runtime | TypeScript monorepo, Bun 1.3.14, Turbo/workspaces; Bun-compiled binary | https://raw.githubusercontent.com/anomalyco/opencode/dev/package.json |
| TUI | OpenTUI 0.4.5 + Solid.js 1.9.10; current branch is not Ink/React | https://raw.githubusercontent.com/anomalyco/opencode/dev/packages/tui/package.json |
| Core/storage | Effect 4.0.0-beta.83; Drizzle + SQLite | https://raw.githubusercontent.com/anomalyco/opencode/dev/package.json |
| API | Hono; generated Promise/Effect clients from one `HttpApi`/SDK Contract IR | https://raw.githubusercontent.com/anomalyco/opencode/dev/CONTEXT.md |
| Providers/protocols | AI SDK with 17+ adapters, OpenAI-compatible providers, MCP, ACP, LSP/tree-sitter | https://raw.githubusercontent.com/anomalyco/opencode/dev/packages/opencode/package.json |

`CONTEXT.md` describes durable SQLite session history, Provider Turns, immutable Baseline System Context per Context Epoch (stable provider-cache prefix), keyed Context Sources, bounded tool results with spill-to-file, and primary/subagent sessions (default depth 1). Subagents are invoked by `task` or `@mention`.

## 2. What it does well

1. Broad provider/model support. The local config defines 11 providers and 28 explicitly named custom models; catalog entries in `~/.cache/opencode/models.json` are separate.
2. Per-agent/per-command `allow`/`ask`/`deny` glob permissions with last-match-wins, external-directory gating, task and skill controls. https://opencode.ai/docs/permissions/ ; https://opencode.ai/docs/agents/
3. Config-driven agents, `SKILL.md` discovery across `.opencode`, `.claude`, and `.agents`, MCP, custom tools/commands, and plugins. https://opencode.ai/docs/skills/ ; https://opencode.ai/docs/plugins/
4. Durable sessions, resume/fork/export/share, todos, token/cost counters, and internal git snapshots for undo/redo. https://opencode.ai/docs/
5. Context Epoch caching and managed tool-output files bound the model-visible transcript. https://raw.githubusercontent.com/anomalyco/opencode/dev/CONTEXT.md
6. One `HttpApi` serves TUI, CLI, web, desktop, and integrations; this is a useful boundary pattern for Janus. https://raw.githubusercontent.com/anomalyco/opencode/dev/CONTEXT.md

## 3. What it does poorly / limitations

1. **Subagents are sequential.** Issue #29638 reports `tasks.pop()` → `handleSubtask(...)` blocking each child; maintainers closed it **not planned**. https://github.com/anomalyco/opencode/issues/29638
2. **Heavy footprint:** local binary 140 MB, SQLite DB 299 MB at measurement, cache/package 91 MB, and managed outputs near 1 MB. The JS/Bun/Effect stack is materially heavier than Cambium’s Python target.
3. **Snapshot cost:** docs warn internal-git snapshots can slow indexing and consume significant disk; undo requires a git repository. https://opencode.ai/docs/config/
4. **Provider/config churn:** providers install dynamically and cache locally; troubleshooting suggests clearing `~/.cache/opencode`; config has `maxSteps`→`steps`, `tools`→`permission`, and TUI-key migration. https://opencode.ai/docs/troubleshooting/ ; https://opencode.ai/docs/agents/
### 3.5 Compaction and context reduction

5. **Compaction is lossy:** hidden `compaction` agent makes another model call; `reserved`/`prune` are coarse and do not provide replay durability. https://opencode.ai/docs/config/ ; https://opencode.ai/docs/agents/

### 3.6 Application-level caching

6. App-level prompt caching was not observed; cost management uses provider-side cache keys. Cambium review `docs/architecture/reviews/review-llm-design.md` treats a naive prompt hash as a correctness hazard. **UNVERIFIED secondhand reports:** system-design cites issue #11865 timeout hangs and community ACK loops; those were not rechecked.

## 4. Relevant lessons for Cambium

### 4.1 Orchestrator and worker model

1. Keep process-isolated parallel workers: OpenCode’s cheap child-session UX is useful, but sequential dispatch is a limitation. Borrow `@mention`/resume/fork ergonomics and a task-like decomposition tool.

### 4.2 One authoritative API

2. Define one authoritative wire API and generate clients/frontends from it; Nuntius JSON-lines is Cambium’s analog.

### 4.3 Optional TUI adapter

3. Use mature TUI components rather than hand-rolled rendering; keep TUI optional and separate (`tui.json` is a useful precedent).

### 4.4 Permission model

4. Copy allow/ask/deny glob permissions, last-match-wins, external-directory gates, and `doom_loop` protection for Septum.

### 4.5 Context-epoch caching

5. Keep a stable context prefix per epoch and avoid a naive app-level cache; bind any cache to worktree/HEAD or use provider caching.

### 4.6 Managed tool-output files

Bounded model-visible preview plus spill-to-file (Managed Tool Output Files), with the full output path recorded, directly addresses chatty workers and keeps the durable log bounded.

### 4.8 Compaction and checkpointing

OpenCode’s compaction is a lossy extra LLM call. Cambium’s checkpoint-per-tool-call ReAct recovery is strictly better for crash recovery; use compaction only as a last-resort context reducer, not as the durability mechanism. Adopt portable `SKILL.md` discovery alongside that checkpoint boundary.

## 5. Local install evidence

```text
$ file /home/ubuntu/.local/bin/opencode
ELF 64-bit LSB executable, ARM aarch64, dynamically linked, not stripped
$ /home/ubuntu/.local/bin/opencode --version
0.0.0-dev-202608071959
$ ls -la /home/ubuntu/.local/bin/opencode
146909328 bytes (140 MB), Aug 7 20:00
```

`~/.opencode/` contains 15 skill dirs, dependencies, lockfile, and `.env`; effective config is `~/.config/opencode/opencode.json` (809 lines), with `tui.json`, 12 config commits, and `openai-compact/checkpoints.db` (1.8 MB WAL). `opencode agent list` showed built-ins build/compaction/explore/general/plan/summary/title plus deepseek/glm/kimi/luna/reviewer/sol; config defines 10 agents, 11 providers, and 28 named models. Default is build on `opencode-go/deepseek-v4-flash`.

Database command (point-in-time, live and growing):

```text
sqlite3 ~/.local/share/opencode/opencode-dev.db "SELECT (SELECT count(*) FROM session), (SELECT count(*) FROM message), (SELECT count(*) FROM part);"
104|5362|22754   # 2026-08-09T21:04:47Z
stat .../opencode-dev.db → 313,778,176 bytes (299 MB)
du -sh ~/.cache/opencode → 91 M
```

The DB had grown from 87/5,004/21,054 and 292,880,384 bytes within roughly 30 minutes because research sessions were writing to it. Logs began 2026-08-08T14:21Z. Recent sessions were the 2026-08-09 Cambium design/research sprint and prior Polymarket/bench-harness work; config git history shows active model-routing edits through 2026-08-09.

The local config’s 10 agents use per-agent `model`, `variant`, `mode`, `steps`, and `permission` fields; this is a concrete schema pattern, not an endorsement of all 11 configured providers. The 28 named custom models are counted only under `provider.<id>.models` in `opencode.json`; `opencode models` would include many more catalog entries. Managed tool-output files are separate from the durable SQLite parts, so their disk footprint and the database footprint should be measured independently.

OpenCode’s context design deliberately keeps the Baseline System Context immutable during a Context Epoch and admits changes only at a Safe Provider-Turn Boundary. That is different from lossy compaction: the former stabilizes provider cache prefixes; the latter summarizes history when full. Cambium should retain the distinction when designing checkpoint and context-budget behavior.

The sequential-subagent issue is concrete: the reporter identified `tasks.pop()` followed by blocking `handleSubtask(...)`, and maintainers closed #29638 as not planned. The report supports a parallel-worker differentiator, but does not establish that every OpenCode workload is slow. The unverified #11865 timeout and ACK-loop notes remain secondhand.

The storage measurements also show why “durable” does not mean “bounded.” The SQLite database holds sessions, messages, and parts, while managed tool-output files and provider/model caches live elsewhere. A Cambium event log should define retention and spill paths up front; otherwise a successful long session can become a disk-pressure failure. OpenCode’s troubleshooting advice to remove `~/.cache/opencode` is a recovery operation, not a consistency check.

The permission system is finer-grained than a single approval boolean: a rule can allow `git diff`, ask for other bash commands, deny a tool, gate external directories, and control `task`/skill loading. The docs’ last-match-wins behavior should be copied only with tests for rule ordering. The local OpenCode configuration’s active model edits show that permission and model routing drift can happen independently.

The TUI configuration is intentionally separate (`~/.config/opencode/tui.json`), while `opencode run --format json` and `serve` keep automation available. This separation supports Janus’ future adapter split: a renderer can evolve without changing the worker/session protocol. It also means a TUI-only feature claim should not be treated as evidence of headless behavior.

The local binary is dynamically linked (`/lib/ld-linux-aarch64.so.1`) and not stripped, unlike the Codex musl payload. The 140 MB size, 299 MB database, 91 MB cache, and 8 MB log are measured artifacts from one date; they should not be presented as fixed product limits. They do, however, make storage budgeting a concrete Cambium concern.

## 6. Sources

Web: https://github.com/anomalyco/opencode ; https://opencode.ai/docs/ ; https://opencode.ai/docs/agents/ ; https://opencode.ai/docs/tools/ ; https://opencode.ai/docs/skills/ ; https://opencode.ai/docs/config/ ; https://opencode.ai/docs/tui/ ; https://opencode.ai/docs/troubleshooting/ ; https://raw.githubusercontent.com/anomalyco/opencode/dev/package.json ; https://raw.githubusercontent.com/anomalyco/opencode/dev/packages/opencode/package.json ; https://raw.githubusercontent.com/anomalyco/opencode/dev/packages/tui/package.json ; https://raw.githubusercontent.com/anomalyco/opencode/dev/CONTEXT.md ; https://github.com/anomalyco/opencode/issues/29638

Local: `file`, `--version`, `ls -la` on `/home/ubuntu/.local/bin/opencode`; `sqlite3` on `~/.local/share/opencode/opencode-dev.db`; directory listings under `~/.opencode`, `~/.config/opencode`, `~/.cache/opencode`, `~/.local/share/opencode`; `opencode agent list`, `opencode --help`; `git -C ~/.config/opencode log --oneline -10`; log tail. All inspected 2026-08-09.

Direct configuration references: https://opencode.ai/docs/permissions/ ; https://opencode.ai/docs/agents/ ; https://opencode.ai/docs/skills/ ; https://opencode.ai/docs/plugins/ ; https://opencode.ai/docs/troubleshooting/ ; https://raw.githubusercontent.com/anomalyco/opencode/dev/CONTEXT.md ; https://raw.githubusercontent.com/anomalyco/opencode/dev/packages/tui/package.json
OpenCode’s `task` subagent mechanism and `@mention` UX are useful interaction patterns even though dispatch is sequential. The Cambium analogue can preserve named child sessions and resume/fork navigation while dispatching independent workers concurrently under Custos.
The local DB counts are point-in-time because the research process itself wrote sessions while measuring them; keep that caveat with every repeated count.
Future snapshots should record database size, session counts, cache size, and binary version together, because each measurement changed during the research run.
