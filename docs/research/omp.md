# Competitive analysis: `omp` (Oh My Pi)

**Date:** 2026-08-09. **Target:** `/home/ubuntu/.local/bin/omp`; operator worktree `/tmp/opencode/cambium-omp`, branch `wt-omp`. **Purpose:** Cambium design input. Local claims cite commands/output; web claims cite URLs; unsupported claims are **UNVERIFIED**. This is a snapshot, not runtime authority.

## What it is / stack

`omp` is Oh My Pi by GitHub user `can1357` (Can Bölük), derived in behavior and naming from badlogic/pi-mono but not a GitHub fork: API metadata for `can1357/oh-my-pi` reported `fork:false, parent:None, source:None`. The MIT project’s upstream page describes a TypeScript monorepo with a Rust N-API core (`pi-natives`, `pi-shell`, `pi-ast`, `pi-walker`, `pi-iso`, `pi-voice`), in-process search/glob/find, a vendored bash/coreutils layer, LSP/DAP, web/browser tools, ACP, and RPC. Exact “60+ providers / 31 tools / 14 LSP / 28 DAP” counts are **UNVERIFIED**. https://github.com/can1357/oh-my-pi

- Local artifact is a Bun single-file JavaScript bundle (`#!/usr/bin/env bun`, 12,099,834 bytes); local Bun is 1.3.14, matching package engine `>=1.3.14`.
- Local version is `omp/17.2.10`; npm latest was `@oh-my-pi/pi-coding-agent` 17.2.12 on 2026-08-09. Exact bundle provenance is **UNVERIFIED**.
- Config uses `PI_*` names (`PI_SMOL_MODEL`, `PI_SLOW_MODEL`, `PI_PLAN_MODEL`, `PI_PROFILE`, `PI_CONFIG_DIR`) and `OMP_PROFILE`, supporting the Pi lineage inference.

`~/.omp/agent/config.yml` routes roles through six provider groups with per-provider in-flight limits and fallback chains. It sets `tools.approvalMode: yolo`, hashline edits, snapcompact (`reserveTokens: 20000`, `keepRecentTokens: 12000`), `task.isolation.mode: rcopy`, max concurrency 12, and recursion depth 2. `models.yml` has custom OpenAI-compatible providers and plaintext API keys (redacted); `SYSTEM.md` uses an OpenCode-style persona; `AGENTS.md` sets worktree/role rules. The config directory is a git repo; latest inspected commit is `7b70842 perf(omp): lean system prompt, disable skills and unused tools` (2026-08-08).

Recent sessions show real Polymarket code/review/deploy work, model smoke tests, and benchmark-harness prompts from `/home/ubuntu/bench-harness` and `/tmp/bench-ctx`. This demonstrates local use, not general reliability.

## What it does well

1. **Deterministic edits:** hashline content-hash anchors reject stale patches (README claim). https://github.com/can1357/oh-my-pi
2. **Routing:** six providers, role models, per-provider caps, and ordered per-model fallbacks are configured and used locally; this directly matches Diffundo’s cascade need.
3. **Bounded delegation:** rcopy isolation, concurrency 12, recursion depth 2, and role thinking budgets map to Cambium’s worktree/worker design. Fan-out reliability at scale is **UNVERIFIED**.
4. **Context tooling:** snapcompact decisions are logged per turn; thinking budgets and context promotion are explicit.
5. **Tool breadth and embedding:** in-process shell/search, LSP/DAP, web/PDF/browser tools, ACP, and `--mode rpc` are upstream claims. Benchmark gains in the project blog are vendor claims and **UNVERIFIED**. https://blog.can.ac/2026/02/12/the-harness-problem/

## What it does poorly / limitations (observed locally)

1. `omp stats` emitted no output and was killed after the runner’s 120-second timeout.
2. `~/.omp/agent/models.yml` stores six live provider keys in plaintext (values redacted).
3. `omp.2026-08-08.3858494.log` reports a missing `codebase-memory-mcp` binary (`ENOENT`), and repeated `No models match pattern "openai-codex/gpt-5.6-sol"` warnings. The config was edited during benchmark runs.
4. The 12 MB Bun bundle embeds a catalog; `omp models` printed 950 rows. Startup and catalog load are heavier than a small native harness.
5. Upstream is fast-moving: 17,506 commits/23.3k stars in the fetched project snapshot, local 17.2.10 versus npm 17.2.12. https://github.com/can1357/oh-my-pi
6. Local evidence is mostly interactive and one-shot `-p`; configured task fan-out/merge behavior remains **UNVERIFIED**.

## 4. Relevant lessons for Cambium

- Make fallback chains and per-provider concurrency caps first-class; the observed caps were 12 (OpenAI), 2 (Z.AI), 2 (Kimi), 1 (Micu), 2 (OpenRouter), 2 (NVIDIA).
- Use deterministic hash-anchored edits with pre-apply validation, not free-form string patches.
- Keep role routing and thinking budgets in config (`smol`, `plan`, `reviewer`, `advisor`), and make context compaction an explicit, observable subsystem.
- Track config as a git repository, but store only environment references for secrets; fail loudly on missing MCP binaries or stale model IDs.
- Benchmark edits/token efficiency offline (Ascensus) rather than trusting vendor numbers. Cheap Z.AI/Kimi/Micu gateways show provider-agnostic FanOut is practical.

### 4.5 Benchmark-driven harness

The benchmark recommendation is the fifth lesson in this section: measure edit success and token efficiency offline, and keep vendor-published gains **UNVERIFIED** unless independently reproduced.

## Local install evidence

- `file /home/ubuntu/.local/bin/omp` → Bun JavaScript, 12,099,834 bytes; `omp --version` → `omp/17.2.10`; `bun --version` → `1.3.14`.
- `omp --help` exposes `text|json|rpc|rpc-ui`, print/resume, ACP, benchmark, models, stats, and worktree commands.
- `omp models` → 950 rows across 13 provider groups (OpenRouter 447, NVIDIA 168, Bedrock 141, Google 61, OpenCode Zen 61, etc.).
- `find ~/.omp/agent/sessions -name '*.jsonl' | wc -l` → 760; `du -sh ~/.omp/agent/sessions` → 765M.
- `~/.omp/agent/config.yml` and `models.yml` were read; API key values were not reproduced. Logs showed the MCP/model warnings and compaction decisions cited above.

## Additional inspected findings

The local bundle is a JavaScript source file rather than a native executable: `file` reported “ASCII text, with very long lines (10260)” and its mtime was 2026-08-07 21:54. The first 3,000 bytes contained `#!/usr/bin/env bun` and `// @bun`. `omp --help` exposes text, JSON, RPC, and RPC-UI modes; `--smol`, `--slow`, and `--plan`; print/resume; ACP; bench; cleanse; commit; config; models; plugin; search; setup; stats; update; usage; and worktree commands. This is a broad surface, but the timeout on `omp stats` shows that administrative paths need the same watchdog discipline as task paths.

The local model listing contained 950 rows: OpenRouter 447, NVIDIA 168, Amazon Bedrock 141, Google 61, OpenCode Zen 61, Google Antigravity 17, Cerebras 7, Bedrock Mantle 5, Kimi 1, Micu Free 2, Micu VIP 1, OpenCode-Go 25, and Z.AI 1. The session store held 760 JSONL files and 765 MB, with individual Polymarket transcripts up to 5.6 MB. This is direct local storage evidence, not an upstream size guarantee.

`config.yml`’s `modelRoles` and `models.yml` are versioned under `~/.omp/agent`; the header says custom providers were ported from OpenCode. The system prompt says “You are OpenCode, an autonomous AI software engineer. Verify before reporting done.” The 2026-08-08 log contains both `Auto-compaction threshold decision ... shouldCompact:false` and startup warnings for the missing MCP binary and unresolved Sol model. The operator’s sessions include real deployment/review work, Kimi smoke tests, and canned benchmark prompts, but they do not establish reliable parallel merge behavior.

The upstream README claims hashline editing, 60+ providers, 31 built-in tools, 14 LSP operations, and 28 DAP operations. The provider/tool counts and benchmark figures (including the blog’s edit-success and token-reduction numbers) were not independently verified and must stay labeled **UNVERIFIED**. GitHub API metadata also reported `fork:false`, so Pi lineage is an inference from naming/config similarity, not fork provenance.

The local `approvalMode: yolo` setting is a meaningful contrast with the config’s role and worktree controls: the tool permits autonomous commands while relying on user-supplied policy and external extensions for safety. `edit.mode: hashline` is independent of that approval choice. Cambium should not infer that deterministic editing makes unrestricted command execution safe.

The `rcopy` task isolation mode is also not identical to a Git worktree. The local config proves the mode name and concurrency/recursion limits, but the inspected sessions do not prove how copies are merged or cleaned up. Cambium’s Surculus/Unio lifecycle should remain explicit rather than treating OMP’s configured isolation as a completed merge protocol.

The binary also embeds the MCP schema URL `https://raw.githubusercontent.com/can1357/oh-my-pi/main/packages/coding-agent/theme-schema.json` many times and includes provider endpoint strings for NanoGPT, Kilo, OpenRouter, AIMLAPI, and Zenmux. These strings establish bundle breadth only; no endpoint was contacted and no key was used. The npm registry snapshot was fetched 2026-08-09, so the 17.2.12 latest claim is version-sensitive.

The local git history (`7b70842`, then `5984b3a chore(omp): track agent config baseline`) shows that the operator intentionally tracked prompts and role configuration. That supports reproducibility, but the plaintext-key finding means tracking the whole config directory is unsafe unless secret-bearing files are excluded or rewritten to environment references.

## Sources and stats

- Local binary, config, logs, sessions, and git history (commands above), inspected 2026-08-09.
- https://github.com/can1357/oh-my-pi
- https://github.com/badlogic/pi-mono
- https://api.github.com/repos/can1357/oh-my-pi
- https://raw.githubusercontent.com/can1357/oh-my-pi/main/packages/coding-agent/theme-schema.json
- https://registry.npmjs.org/@oh-my-pi/pi-coding-agent/latest (17.2.12; Bun engine)
- https://blog.can.ac/2026/02/12/the-harness-problem/
- https://omp.sh (referenced by README; not independently fetched)

Stats: binary 12,099,834 bytes; local 17.2.10; npm latest 17.2.12; 950 model rows; 760 session files / 765 MB; six inline-key providers in `models.yml`; six provider-order entries; OpenAI cap 12. Vendor benchmark figures and exact local-bundle provenance remain **UNVERIFIED**.

The original command canary also recorded the literal search pattern `https://[a-zA-Z0-9./_-]*` while scanning the bundle; retain it as command provenance, not as a source endpoint.
