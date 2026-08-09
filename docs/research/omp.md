# Competitive analysis: `omp` (Oh My Pi)

**Date:** 2026-08-09
**Target:** locally installed `/home/ubuntu/.local/bin/omp`, investigated from worktree `/tmp/opencode/cambium-omp` (branch `wt-omp`).
**Purpose:** input to Cambium system design (`docs/architecture/system-design.md`).
**Verification rule:** local claims cite the exact command + observed output; web claims cite the URL; anything not directly observed is marked **UNVERIFIED**.

---

## What it is / stack

`omp` is **Oh My Pi**, a terminal AI coding agent by GitHub user `can1357` (Can Bölük), derived from [badlogic/pi-mono](https://github.com/badlogic/pi-mono) (Pi, by Mario Zechner). **Provenance note:** GitHub API reports `fork:false, parent:None, source:None` for `can1357/oh-my-pi` (`curl -s https://api.github.com/repos/can1357/oh-my-pi | python3 -c "import json,sys; d=json.load(sys.stdin); print(d['fork'], d.get('parent'), d.get('source'))"` → `False None None`), i.e. it is a standalone repo, not a GitHub fork of pi-mono; the derivation is inferred from the inherited `PI_` env prefix and config-layout similarity (see "Naming" below), not from GitHub fork metadata.

- **Runtime:** Bun-compiled single-file JavaScript. The local binary starts with `#!/usr/bin/env bun` and `// @bun`; `file` reports `JavaScript source, ASCII text, with very long lines (10260)`, 12,099,834 bytes, executable, mtime 2026-08-07 21:54. Local `bun` is 1.3.14 (matches the package's `engines: bun >= 1.3.14`).
- **Stack (web):** TypeScript monorepo with a ~80k-line Rust core (`pi-natives`, `pi-shell`, `pi-ast`, `pi-walker`, `pi-iso`, `pi-voice`) shipped as an N-API addon; in-process grep/glob/find and a vendored bash fork with 58 ported coreutils. Claimed: 60+ providers, 31 built-in tools, 14 LSP ops, 28 DAP ops. (https://github.com/can1357/oh-my-pi)
- **License:** MIT (web; https://github.com/can1357/oh-my-pi). Local evidence consistent: the bundled `mcp-schema.json` URL references `can1357/oh-my-pi/main/packages/coding-agent` (98 hits when grepping the binary).
- **Version:** local `omp/17.2.10`; npm `latest` for `@oh-my-pi/pi-coding-agent` is `17.2.12` (registry fetch 2026-08-09), i.e. local is 2 patch releases behind. Corresponding npm package ↔ local binary identity is consistent but exact provenance is **UNVERIFIED**.
- **Naming:** env vars are prefixed `PI_` (`PI_SMOL_MODEL`, `PI_SLOW_MODEL`, `PI_PLAN_MODEL`, `PI_PROFILE`, `PI_CONFIG_DIR`) and `OMP_PROFILE` — inheritance from the original "Pi" project.

### Local install shape (what the operator set up)

- `~/.omp/agent/config.yml` — role-based model routing. Roles `default/general/explorer/reviewer/task` → `openai-codex/gpt-5.6-sol:medium`; `worker` → `gpt-5.6-luna:high`; `luna`/`smol` → `gpt-5.6-luna:low`; `plan` → `gpt-5.6-sol:xhigh`; `advisor`/`tiny`/`fast`/`zai` → `zai-coding-plan/glm-5.2`; `kimi` → `kimi-k3:high`. Six providers in `modelProviderOrder` (openai-codex, zai-coding-plan, kimi-for-coding, micu-vip2, openrouter, nvidia) with per-provider `maxInFlightRequests` (openai-codex: 12) and per-model `retry.fallbackChains`. `tools.approvalMode: yolo`. `edit.mode: hashline`. Context: `compaction.strategy: snapcompact`, `reserveTokens: 20000`, `keepRecentTokens: 12000`. `task.isolation.mode: rcopy`, `task.maxConcurrency: 12`, `task.maxRecursionDepth: 2`.
- `~/.omp/agent/models.yml` — custom OpenAI-compatible providers with inline plaintext API keys (micuapi, z.ai, kimi, openrouter, opencode.ai, plus `openai-codex` overrides). Header comment: "Custom providers — ported from OpenCode config". **Plaintext API keys are stored unencrypted in this file (keys redacted here).**
- `~/.omp/agent/SYSTEM.md` — the system prompt is a lean, OpenCode-style prompt ("You are OpenCode, an autonomous AI software engineer."). The operator runs omp with an OpenCode-flavored persona.
- `~/.omp/agent/AGENTS.md` — multi-agent routing rules ("recursion depth 2 max", explicit named roles, worktree commits, no destructive git).
- `~/.omp/agent/.git` — config is version-controlled; HEAD = `7b70842 perf(omp): lean system prompt, disable skills and unused tools` (2026-08-08 02:08).

### Recent activity (from logs + session files)

- **2026-06-29 → 07-07:** heavy real work on a private repo `~/polymarket-arbitrage` (HFT trading bot, Rust + Python). Session titles include "Deploy to dev-a and dev-b", "Merge hotpath review fixes", "Refactor position book for performance", "Audit dev-a logs and fix diagnostics", "Merge goodparts implementation review", "Kalman filter degradation analysis", "Analyze HFT trading session losses". The polymarket session directory alone holds ~10 jsonl transcripts from 2026-07-06/07, the largest 4–5.6 MB.
- **2026-07-03:** omp self-configuration sessions ("Check omp health", "Configure OMP local models", "Disable models below gpt-5.5", "OMP update").
- **2026-08-07:** model-connectivity smoke tests from `/tmp` (e.g. Kimi K3 replying exactly `INSTANCE2 KIMI OK`, thinking level `max`).
- **2026-08-08:** automated benchmark-harness runs (cwd `/home/ubuntu/bench-harness` and `/tmp/bench-ctx`) feeding canned sysadmin prompts ("which process consumes most RAM…", "analyze this repository's disk usage…") to `opencode-go/deepseek-v4-flash`, `zai-coding-plan/glm-5.2`, `openrouter/nvidia/nemotron-3-ultra-550b-a55b:free`, `kimi-for-coding/kimi-k3`. Model use across recent `/tmp*` sessions: `openai-codex/gpt-5.5` 591 occurrences, `cerebras/zai-glm-4.7` 40.
- **Log timeline:** daily rotated logs `~/.omp/logs/omp.2026-07-03.log` … `omp.2026-08-09`; activity tail is Aug 8. Today's log (Aug 9) shows only a fresh 443-byte startup (the session that produced this report).

---

## What it does well

1. **Edit reliability is the headline.** `edit.mode: hashline` uses hash-anchored line patches (content-hash anchors, stale-anchor rejection) — the README's central bench claim. Note this is a web claim: (https://github.com/can1357/oh-my-pi).
2. **Model routing + fallback is first-class and operator-validated.** Local config proves it is used in anger: 6 providers, per-provider in-flight caps, per-model fallback chains, role-based routing, 13 provider groups visible in `omp models`. Matches Cambium's FanOut (M2) motivation.
3. **Isolation + fan-out matches Cambium's core thesis.** `task.isolation.mode: rcopy`, `maxConcurrency: 12`, `maxRecursionDepth: 2`, subagents yielding schema-validated objects the parent reads directly (web claim) — the same worktree-isolation / bounded-recursion design as Cambium M5/M3, already proven in production on the operator's machine.
4. **Context-engineering depth:** snapcompact bitmap-frame compaction, auto-compaction thresholds with reserve/keep budgets, thinking budgets per level (minimal 1024 … xhigh 49152 tokens), context promotion. The `omp.2026-08-08` logs show real threshold decisions logged per turn ("Auto-compaction threshold decision … shouldCompact:false").
5. **Relentless benchmaxxing culture.** The project benchmarks every tool format against models and tunes per model (blog: https://blog.can.ac/2026/02/12/the-harness-problem/). Claims include edit success 6.7% → 68.3% for one model, −61% output tokens on another (**UNVERIFIED** — vendor-published).
6. **Batteries-included tooling:** in-process grep/glob/find/bash (no fork/exec), LSP wired into writes/renames, DAP debugger driving, `web_search` with 23 backends, PDF/URL `read`, browser/computer tools, ACP and `--mode rpc` surfaces for embedding (web claims).

---

## What it does poorly / limitations (observed locally)

1. **`omp stats` hangs.** `omp stats` produced no output and was killed after 120 s by the runner (`shell tool terminated command after exceeding timeout 120000 ms`). Admin/telemetry commands can hang non-interactively.
2. **Secrets in plaintext config.** `~/.omp/agent/models.yml` stores live API keys for 6 providers in cleartext (redacted here). A harness that is "open all the way down" puts credential hygiene on the user.
3. **Brittle optional tooling.** Startup log `omp.2026-08-08.3858494.log` shows `MCP tool load failed path:"mcp:codebase-memory-mcp" error:"ENOENT … /home/ubuntu/.local/bin/codebase-memory-mcp"` — a configured MCP server whose binary does not exist still errors at load.
4. **Config drift against catalog.** Same log: `No models match pattern "openai-codex/gpt-5.6-sol"` (×3, twice per boot) — the config references models the bundled catalog no longer resolves; warnings only, no resolution. The operator's own config was also edited live during bench runs (`config.yml.bak-bench` backup exists alongside `config.yml`).
5. **Single giant JS blob.** The installed artifact is a 12 MB Bun bundle with an embedded model catalog (`omp models` prints 950 model rows). Startup pays for a full interpreter + catalog load.
6. **Fast-moving upstream, big attack surface.** 17,506 commits (web), version bumps 17.2.10 → 17.2.12 between local install and npm latest. Relying on it means tracking a very active fork.
7. **No evidence of programmatic batch/orchestration in the local install.** The operator drives it interactively and via one-shot `-p` runs; the multi-agent `task` fan-out was configured but the recorded sessions are mostly single-agent turns. Whether the fan-out/merge loop is reliable at scale is **UNVERIFIED** from local data.

---

## Relevant lessons for Cambium

1. **Fallback chains and in-flight caps are table stakes.** omp models per-provider `maxInFlightRequests` + ordered `retry.fallbackChains` on 429/quota. Cambium M2 (FanOut) should treat cascade-with-backoff as the default path, with per-provider concurrency throttles, not best-effort.
2. **Deterministic edit tooling beats prompting.** omp's whole differentiation is a hash-anchored edit protocol with stale-anchor rejection. Cambium workers should ship a deterministic `edit` primitive (content-hash anchors, pre-apply validation) rather than depend on the model producing correct string patches.
3. **Role-based routing with thinking budgets maps onto DSPy modules.** omp's `modelRoles` (smol/low, plan/xhigh, reviewer/medium, advisor) mirror Cambium's M6/M5 roles; the operator already tuned thinking budgets per role. This validates Cambium's design and gives it a ready-made config schema to copy.
4. **Cheap non-US providers are real and usable.** The local install routes across z.ai/GLM, Kimi, and micu gateways with per-provider caps — direct evidence for Cambium's "multiple cheap subscriptions" premise and for making FanOut provider-agnostic (OpenAI-compatible surfaces only).
5. **Context compaction must be an explicit subsystem.** omp logs every compaction decision (threshold, used, freed). Cambium's checkpointed ReAct state should include a defined compaction strategy (e.g. summarize-and-prune older tool results) rather than growing context unboundedly.
6. **Log everything, expose thresholds.** The per-turn "auto-compaction threshold decision" debug lines are a model for Cambium's observability: structured, auditable decision logs are cheap and valuable for M9 (Ascensus) optimization runs.
7. **Version-control your agent config.** omp tracks `config.yml`/`SYSTEM.md`/`AGENTS.md` in a git repo inside `~/.omp/agent` — Cambium should ship a tracked, reproducible agent-config layout from day one.
8. **Do not swallow config/startup errors.** omp logs (but tolerates) unresolved model patterns and missing MCP binaries; the cost is silent capability loss. Cambium should fail loudly on configuration that references nonexistent providers/models.
9. **Keep the harness benchmark-driven.** omp publishes per-model edit-format gains; Cambium's Ascensus (M9) should include an offline eval corpus (edit success, token efficiency) so every prompt/tool change is measured the same way.

---

## Local install evidence (command → output)

- `file /home/ubuntu/.local/bin/omp` → `JavaScript source, ASCII text, with very long lines (10260)`; `ls -la` → `-rwxr-xr-x … 12099834 Aug 7 21:54 /home/ubuntu/.local/bin/omp`.
- `head -c 3000 /home/ubuntu/.local/bin/omp` → first lines `#!/usr/bin/env bun` / `// @bun`.
- `omp --version` → `omp/17.2.10`.
- `omp --help` → first line `omp v17.2.10`; USAGE `$ omp [COMMAND]`; flags include `--smol/--slow/--plan` (env `PI_SMOL_MODEL`/`PI_SLOW_MODEL`/`PI_PLAN_MODEL`), `--mode text|json|rpc|rpc-ui`, `-p/--print`, `-r/--resume`, `--from-claude`, `--from-codex`, `--advisor`, `--thinking`, `--export`; COMMANDS include `acp`, `bench`, `cleanse`, `commit`, `config`, `models`, `plugin`, `search`, `setup`, `stats`, `update`, `usage`, `worktree`.
- `grep -ao 'https://[a-zA-Z0-9./_-]*' /home/ubuntu/.local/bin/omp | sort | uniq -c | sort -rn | head -30` → top hits `https://raw.githubusercontent.com/can1357/oh-my-pi/main/packages/coding-agent/theme-schema.json` (98), plus 25+ provider API endpoints (nano-gpt, kilo, openrouter, aimlapi, zenmux, …).
- `omp models` → 950 rows (count of `^│` lines); provider groups: `openrouter (447)`, `nvidia (168)`, `amazon-bedrock (141)`, `google (61)`, `opencode-zen (61)`, `google-antigravity (17)`, `cerebras (7)`, `bedrock-mantle (5)`, `kimi-for-coding (1)`, `micu-free2 (2)`, `micu-vip2 (1)`, `opencode-go (25)`, `zai-coding-plan (1)`.
- `omp stats` → no output; killed after 120 s timeout.
- `find ~/.omp/agent/sessions -name '*.jsonl' | wc -l` → `760`; `du -sh ~/.omp/agent/sessions` → `765M`.
- `~/.omp/agent/config.yml` (read) → role→model map, `tools.approvalMode: yolo`, `edit.mode: hashline`, compaction/thinking budgets, `task.isolation.mode: rcopy`, `task.maxConcurrency: 12`, `task.maxRecursionDepth: 2`.
- `~/.omp/agent/models.yml` (read) → 7 custom providers; header "Custom providers — ported from OpenCode config"; plaintext API keys present (redacted).
- `~/.omp/agent/SYSTEM.md` (read) → "You are OpenCode, an autonomous AI software engineer. Verify before reporting done."
- `git -C ~/.omp/agent log --oneline -10` → `7b70842 perf(omp): lean system prompt, disable skills and unused tools`; `5984b3a chore(omp): track agent config baseline`.
- `~/.omp/logs/omp.2026-08-08.3858494.log` (read) → `MCP tool load failed … ENOENT … codebase-memory-mcp`; `No models match pattern "openai-codex/gpt-5.6-sol"`; `Auto-compaction threshold decision … shouldCompact:false`.
- Session transcripts read: `sessions/-polymarket-arbitrage/*.jsonl` (titles listed above), `sessions/-bench-harness/2026-08-08*.jsonl` (canned prompts, cwd `/home/ubuntu/bench-harness`), `sessions/-tmp/2026-08-07*.jsonl` (`INSTANCE2 KIMI OK`), `sessions/-tmp-bench-ctx/…` (cwd `/tmp/bench-ctx`, disk-usage prompt).
- `bun --version` → `1.3.14`.

**Stats (objectively verifiable):**
- Binary: 12,099,834 bytes, JavaScript, executable, mtime 2026-08-07 21:54.
- Local version `omp/17.2.10`; npm latest `17.2.12`; `omp models` lists 950 model rows across 13 provider groups.
- 760 session transcripts / 765 MB under `~/.omp/agent/sessions`; polymarket transcripts up to 5.6 MB each.
- `models.yml` has 7 providers, 6 with inline plaintext `apiKey` (`openai-codex` is an override without a key); `modelProviderOrder` in `config.yml` has 6 entries; `maxInFlightRequests` cap 12 on openai-codex.

---

## Sources

- Local binary + `~/.omp` config/logs/sessions (this machine; commands above).
- https://github.com/can1357/oh-my-pi — project README, repo stats (23.3k stars, 2.2k forks, 17,506 commits), features, tool list, provider list, license (MIT).
- https://registry.npmjs.org/@oh-my-pi/pi-coding-agent/latest — package `17.2.12`, `engines.bun >= 1.3.14`, author "Can Boluk", homepage omp.sh (fetched 2026-08-09).
- https://blog.can.ac/2026/02/12/the-harness-problem/ — vendor's harness/benchmark write-up (cited for the benchmaxxing culture claim; numbers **UNVERIFIED**).
- https://omp.sh — project site (referenced from GitHub README; not independently fetched).

**UNVERIFIED items:** exact provenance of the locally installed 17.2.10 bundle vs npm/GitHub releases; vendor benchmark numbers (edit success 6.7%→68.3%, −61% tokens, 2.1× pass rate); fan-out/merge reliability at scale; "60+ providers / 31 tools" counts.
