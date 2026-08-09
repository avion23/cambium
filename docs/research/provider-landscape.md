# Local provider landscape — Diffundo provider matrix input

**Date:** 2026-08-09
**Target:** the locally configured LLM provider landscape on this machine (evidence: config files only).
**Purpose:** input to Cambium's M2 **Diffundo** (FanOut — multi-provider LLM access with cascade/race/cache; `docs/system-design.md` M2, review `docs/reviews/review-llm-design.md` C2/C3). Task scope: docs only — **no API calls, no keys used, no network calls to LLM providers.**

**Redaction rule (stated explicitly):** no API key, token, refresh token, session token, or other secret **value** appears anywhere in this document or in the report. Where a value could be a secret, only the **key name** and its **type** (e.g. `api_key`, `oauth`) and **length** are listed, or the value is replaced by `***[redacted]`. Files whose only content is credentials (`~/.codex/auth.json`, `~/.pi/agent/auth.json`, `~/.prime/agent/auth.json`, `~/.local/share/opencode/auth.json`, `~/.config/opencode/antigravity-accounts.json`) were inspected for **structure only** (key names, types, lengths); their values were never copied and are not reproduced here. The only exception to "values redacted": non-secret scalar settings (numbers, booleans, URLs, model IDs, provider names) are reported as-is because they carry the routing/caching facts Diffundo needs.

---

## 1. Methodology

1. Set up worktree at `/tmp/opencode/cambium-providers`, branch `wt-providers` (git worktree of `/home/ubuntu/cambium`).
2. Enumerated provider/agent config locations for five tools:
   - OpenCode: `~/.config/opencode/opencode.json` (the task brief says `~/.opencode/opencode.json`, but **that path does not exist** — the real config is `~/.config/opencode/opencode.json`; `~/.opencode/` holds only `.env`, skill dirs, and `antigravity-accounts.json`). Also read `~/.config/opencode/openai-compact.jsonc`, `~/.config/opencode/tui.json`, `~/.local/share/opencode/auth.json`, `~/.opencode/.env` (key names only).
   - Codex: `~/.codex/config.toml`, `~/.codex/auth.json` (structure only), `~/.codex/model_catalog.json`, `~/.codex/agents/*.toml`.
   - Pi: `~/.pi/agent/models.json`, `~/.pi/agent/settings.json`, `~/.pi/agent/auth.json` (structure only), `~/.pi/agent/subagents.json`, `~/.pi/agent/models-store.json` (cached catalog), `~/.pi/agent/pi-codex-conversion.json`, `~/.pi/agent/openai-server-compaction.json`.
   - OMP: `~/.omp/agent/config.yml`, `~/.omp/agent/models.yml`.
   - Prime: `~/.prime/agent/models.json`, `~/.prime/agent/settings.json`, `~/.prime/agent/auth.json` (structure only), `~/.prime/agent/telemetry.json`.
3. Every file was read on-disk; JSON parsed with `python3`, TOML/YAML read as text with a redacting filter. Commands used are listed in §6. No provider endpoint was contacted.
4. Credential stores were dumped via a **structure-only** walker (key names, value type, value length, never value content).

**Verification rule:** every claim cites the file it came from. Anything not directly observed in a config file is marked **UNVERIFIED**.

---

## 2. Per-tool landscape

### 2.1 OpenCode (`~/.config/opencode/opencode.json`)

- **Default model:** `opencode-go/deepseek-v4-flash`; `default_agent: build`; `disabled_providers: [llama-cpp]`; autoupdate off; compaction on (`auto`, `prune`, `reserved: 28000`, `preserve_recent_tokens: 12000`).
- **Providers defined (with models):**
  | provider | models | baseURL / auth |
  |---|---|---|
  | `google` | antigravity-gemini-3-pro, antigravity-gemini-3.1-pro, antigravity-gemini-3-flash, antigravity-claude-sonnet-4-6, antigravity-claude-opus-4-6-thinking, gemini-2.5-pro, gemini-3-flash-preview, gemini-3-pro-preview, gemini-3.1-pro-preview, gemini-3.1-pro-preview-customtools | Antigravity OAuth (refresh token in `~/.config/opencode/antigravity-accounts.json`) + Gemini CLI |
  | `openai` | gpt-5.6-sol, gpt-5.6-terra, gpt-5.6-luna | OAuth (no key in config); env `OPENAI_API_KEY` also present in `~/.opencode/.env` |
  | `micu-free2` | claude-3-5-sonnet, claude-sonnet-4-6, claude-opus-4-6 | `https://api-slb.micuapi.ai/v1`; `options.apiKey` **redacted** |
  | `micu-vip2` | gpt-5.5, gpt-5.5-openai-compact | `https://api-slb.micuapi.ai/v1`; `options.apiKey` **redacted** |
  | `zai-coding-plan` | glm-5.2 | `https://api.z.ai/api/coding/paas/v4`; `options.apiKey` **redacted**; `timeout: false` |
  | `nvidia` | minimaxai/minimax-m3 (thinking_mode variants none/low/high) + 16-model `whitelist` (nemotron family, meta/llama, moonshotai/kimi-k2.6, openai/gpt-oss, stepfun-ai/step-3.7-flash) | no key in config (**UNVERIFIED** whether env `NVIDIA_API_KEY`/`nvapi-` is used — see §5) |
  | `llama-cpp` | qwen3-coder-30b-a3b-instruct-abliterated | `http://localhost:8080/v1`, `apiKey: none` — **disabled** via `disabled_providers` |
  | `zenmux` | (no models) | `https://zenmux.ai/api/v1`, no key |
  | `tokenrouter` | auto:balance, auto:fast, auto:cost, auto:quality, moonshotai/kimi-k3-free | `https://api.tokenrouter.com/v1`; `options.apiKey` **redacted** |
  | `openrouter` | (no models; `blacklist: [mancer/weaver]`) | env `OPENROUTER_API_KEY` |
  | `opencode` | deepseek-v4-flash-free, mimo-v2.5-free | `https://opencode.ai/zen/v1`; no key in config (**UNVERIFIED** auth) |
- **Agent → model map** (`agent.*`): build/explore/general/deepseek → `opencode-go/deepseek-v4-flash` (variant high); plan → `openai/gpt-5.6-sol` (high); sol → `openai/gpt-5.6-sol` (medium, 60 steps); reviewer → `openai/gpt-5.6-sol` (high, read-only, 40 steps); luna → `openai/gpt-5.6-luna` (max); glm → `zai-coding-plan/glm-5.2` (high); kimi → `kimi-for-coding/k3-256k` (high, 300s timeout).
- **Auth store:** `~/.local/share/opencode/auth.json` — `opencode-go` → `{type: api, key: ***}`. `kimi-for-coding` is referenced but **not defined** as a provider block (built-in; auth **UNVERIFIED**, likely env `KIMI_API_KEY`).
- **Env key names** present in `~/.opencode/.env` (values redacted, names only): `OPENAI_API_KEY`, `OPENROUTER_API_KEY`, `GOOGLE_API_KEY`, `GROQ_API_KEY`, `ZAI_API_KEY`, `GEMINI_API_KEY`, `GOOGLE_GENERATIVE_AI_API_KEY`, `WEB_SEARCH_API_KEY`, `KIMI_API_KEY`, `MOLTBOOK_API_KEY`.
- **Retry/fallback:** none configured. **Max concurrency:** none configured (**UNVERIFIED** — opencode has no such setting in this file; `experimental.primary_tools: [task]`). **Caching:** prompt-compaction only (see above); models cache patched by a local script `patch-models-cache.py` (present, not read for content).

### 2.2 Codex (`~/.codex/config.toml`)

- **Default model:** `gpt-5.6-sol`; `model_reasoning_effort: medium`; `plan_mode_reasoning_effort: high`; `model_verbosity: low`; `personality: none`.
- **Provider:** OpenAI only (ChatGPT/Codex plan). Auth is OAuth: `~/.codex/auth.json` holds `auth_mode = "chatgpt"` plus OAuth tokens (key names `id_token`, `access_token`, `refresh_token`, `account_id` — all values redacted); an `OPENAI_API_KEY` field is present but **null** in that file. (`config.toml` itself contains no `auth_mode` — it holds model/agent/routing config only.)
- **Model catalog** (`~/.codex/model_catalog.json`, fetched 2026-07-31): gpt-5.6-sol, gpt-5.6-terra, gpt-5.6-luna, codex-auto-review, gpt-5.5, gpt-5.4, gpt-5.4-mini, gpt-5.3-codex-spark. Catalog carries per-model reasoning levels (low…ultra), context windows (272k for the 5.6 family), and migration notices (`model_migrations`: gpt-5.2-codex/5.3/5.4/5.5 → gpt-5.6-sol; 5.3-codex-spark/5.4-mini → gpt-5.6-luna).
- **Multi-agent:** `features.multi_agent_v2.enabled = true`, `max_concurrent_threads_per_session = 12`, `agents.max_depth = 2`; per-role agent TOMLs under `~/.codex/agents/`: default → sol/medium; explorer → sol/medium (read-only); luna → luna/low; reviewer → sol/medium (read-only); worker → luna/xhigh. `tool_output_token_limit = 5000`.
- **Retry/fallback:** none in config (codex-internal, **UNVERIFIED**). **Caching:** none configured in `config.toml` (server-side prompt caching is a codex platform property, **UNVERIFIED** locally). `tool_output` token cap = 5000.

### 2.3 Pi (`~/.pi/agent/models.json` + `settings.json` + `auth.json` + `subagents.json`)

- **Providers defined in `models.json`** (with per-model `contextWindow`/`maxTokens`):
  | provider | models | baseURL / auth |
  |---|---|---|
  | `openai-codex` | gpt-5.6-sol, gpt-5.6-terra, gpt-5.6-luna | `https://chatgpt.com/backend-api`, api `openai-codex-responses`, OAuth (auth.json: access/refresh/expires/accountId — values redacted) |
  | `micu-vip2` | gpt-5.5, gpt-5.5-openai-compact, gpt-5.4, gpt-5.4-mini, gpt-5.4-openai-compact, gpt-5.3-codex, gpt-5.3-codex-spark, codex-auto-review | `https://api-slb.micuapi.ai/v1`; `apiKey` **redacted**; `compat.maxTokensField: max_tokens` |
  | `tokenrouter` | MiniMax-M3 | `https://api.tokenrouter.com/v1`; `apiKey` **redacted** |
  | `zai` | glm-5.2 | `https://api.z.ai/api/coding/paas/v4`; `apiKey` **redacted**; thinkingFormat `zai` |
  | `kimi` | k3-256k, k3, kimi-for-coding-highspeed | `https://api.kimi.com/coding/v1`; `apiKey` **redacted** |
  | `opencode-go` | deepseek-v4-flash | `https://opencode.ai/zen/go/v1`; `apiKey` **redacted** |
- **`auth.json` key names (values redacted):** `google` (api_key), `groq` (api_key), `nvidia` (api_key), `openrouter` (api_key), `zai` (api_key), `openai-codex` (oauth), `tokenrouter` (api-key).
- **`settings.json`:** `defaultProvider: openai-codex`, `defaultModel: gpt-5.6-sol`, `defaultThinkingLevel: medium`; `enabledModels`: openai-codex/gpt-5.6-sol, openai-codex/gpt-5.6-terra, openai-codex/gpt-5.6-luna, zai/glm-5.2, kimi/k3-256k, kimi/k3, kimi/kimi-for-coding-highspeed (7 enabled). Thinking budgets: minimal 1024 / low 4096 / medium 10240 / high 32768.
- **Retry:** `retry.enabled: true`, `maxRetries: 3`, `baseDelayMs: 2000`, provider-level `maxRetries: 0`, `maxRetryDelayMs: 60000`. **Caching:** compaction enabled, `reserveTokens: 16384`, `keepRecentTokens: 20000`; `pi-codex-conversion.json` `responsesCompaction: true`; `openai-server-compaction.json` `enabled: true`, `thresholdRatio: 0.7`, `usePreviousResponseId: true`. **Max concurrency:** `subagents.maxParallel: 12`, `subagents.worktree: true`.
- **Cached catalog:** `models-store.json` (gitignored) holds cached model lists for google, groq, nvidia, openrouter, zai, openai-codex — evidence of which providers are connected (see §5).

### 2.4 OMP (`~/.omp/agent/config.yml` + `models.yml`)

- **Role → model map (`modelRoles`)** — the richest role-routing example found:
  - `default/general/explorer/reviewer/task` → `openai-codex/gpt-5.6-sol:medium`
  - `worker` → `openai-codex/gpt-5.6-luna:high`
  - `luna`/`smol` → `openai-codex/gpt-5.6-luna:low`
  - `plan` → `openai-codex/gpt-5.6-sol:xhigh`
  - `advisor`/`tiny` → `zai-coding-plan/glm-5.2:off`
  - `fast` → `zai-coding-plan/glm-5.2:low`
  - `zai` → `zai-coding-plan/glm-5.2:high`
  - `kimi` → `kimi-for-coding/kimi-k3:high`
  - `micu` → `micu-vip2/gpt-5.5:medium`
  - `freeReview` → `openrouter/nvidia/nemotron-3-ultra-550b-a55b:free:high`
- **`enabledModels`:** openai-codex/gpt-5.6-sol, openai-codex/gpt-5.6-terra, openai-codex/gpt-5.6-luna, micu-vip2/gpt-5.5, zai-coding-plan/glm-5.2, kimi-for-coding/kimi-k3, openrouter/nvidia/nemotron-3-ultra-550b-a55b:free (7).
- **Provider order + per-provider concurrency (`modelProviderOrder` / `providers.maxInFlightRequests`):** openai-codex **12**, zai-coding-plan **2**, kimi-for-coding **2**, micu-vip2 **1**, openrouter **2**, nvidia **2**.
- **Retry/fallback — the key finding.** `retry.enabled: true`, `maxRetries: 10`, `modelFallback: true`, and explicit per-model **`fallbackChains`**:
  - `openai-codex/gpt-5.6-terra` → `zai-coding-plan/glm-5.2` → `openrouter/nvidia/nemotron-3-ultra-550b-a55b:free`
  - `openai-codex/gpt-5.6-sol` → `zai-coding-plan/glm-5.2`
  - `openai-codex/gpt-5.6-luna` → `openai-codex/gpt-5.6-terra`
  - `zai-coding-plan/glm-5.2` → `openrouter/nvidia/nemotron-3-ultra-550b-a55b:free`
  - `micu-vip2/gpt-5.5` → `zai-coding-plan/glm-5.2`
  - `kimi-for-coding/kimi-k3` → `zai-coding-plan/glm-5.2` → `openrouter/nvidia/nemotron-3-ultra-550b-a55b:free`
  - `openrouter/nvidia/nemotron-3-ultra-550b-a55b:free` → `zai-coding-plan/glm-5.2`
- **Caching/compaction:** enabled, strategy `snapcompact`, mid-turn enabled, `reserveTokens: 20000`, `keepRecentTokens: 12000`. Thinking budgets: minimal 1024 / low 2048 / medium 12288 / high 24576 / xhigh 49152.
- **Task/concurrency:** `task.isolation.mode: rcopy`, `task.maxConcurrency: 12`, `task.maxRecursionDepth: 2`.
- **Disabled:** `disabledProviders: [gpt-5.4, ollama, llama.cpp, lm-studio, zai]`; `disabledModels: [openai-codex/gpt-5.4, openai-codex/gpt-5.5, openai-codex/gpt-5.3-codex-spark]`.
- **`models.yml`:** custom OpenAI-compatible providers with **inline plaintext apiKeys** (**redacted here**): micu-free2, micu-vip2, zai-coding-plan, kimi-for-coding, openrouter, opencode-go; plus `openai-codex` overrides (blockedModels gpt-5.4; contextWindow/maxTokens overrides for 5.6 family) and an `equivalence` map (micu-vip2/gpt-5.5 ≡ gpt-5.5 etc.).
- **Security note:** `~/.omp/agent/models.yml` is tracked in the OMP config git repo (`git ls-files` shows `models.yml`) and **contains plaintext API keys** — a credential-hygiene lesson for Diffundo (see §6).

### 2.5 Prime (`~/.prime/agent/models.json` + `settings.json` + `auth.json`)

- **Providers defined in `models.json`:**
  | provider | models | baseURL / auth |
  |---|---|---|
  | `nvidia` | minimaxai/minimax-m3 | `https://integrate.api.nvidia.com/v1`; `apiKey` **redacted** |
  | `opencode-go` | deepseek-v4-flash, deepseek-v4-pro | `https://opencode.ai/zen/go/v1`; `apiKey` **redacted** |
  | `tokenrouter` | auto:balance, auto:fast, auto:cost, auto:quality, moonshotai/kimi-k3-free | `https://api.tokenrouter.com/v1`; `apiKey` **redacted** |
- **`auth.json` key names (values redacted):** `google` (api_key), `openrouter` (api_key), `zai` (api_key), `kimi-coding` (api_key), `openai-codex` (oauth).
- **`settings.json`:** `defaultProvider: opencode-go`, `defaultModel: deepseek-v4-flash`, `defaultThinkingLevel: high`. **Retry:** enabled, `maxRetries: 2`, `baseDelayMs: 3000`, provider-level `maxRetries: 1`, `maxRetryDelayMs: 60000`. **Compaction:** enabled, `reserveTokens: 24576`, `keepRecentTokens: 24000`. `recentModels: [opencode-go/deepseek-v4-flash, openai-codex/gpt-5.6-sol]`. No concurrency setting (**UNVERIFIED**).

---

## 3. Comparison table

| provider | tools using it | models (enabled/configured) | routing/fallback | cache/compaction | auth mechanism (key names only) |
|---|---|---|---|---|---|
| **openai / openai-codex** (ChatGPT OAuth) | opencode, codex, pi, omp, prime | opencode: gpt-5.6-sol/terra/luna; codex catalog: sol/terra/luna + gpt-5.5/5.4/5.4-mini/5.3-codex-spark/codex-auto-review; pi: sol/terra/luna; omp: sol/terra/luna; prime: (recent) sol | opencode: plan/sol/reviewer/luna agents; codex: per-role sol/medium, luna/xhigh, plan high; pi: default sol/medium; omp: role map + fallbackChains sol→glm, luna→terra, terra→glm→nemotron-free; prime: none | opencode: compaction on (28k/12k); pi: compaction 16k/20k; prime: compaction 24.6k/24k; codex: none | codex auth.json: `id_token`/`access_token`/`refresh_token`/`account_id` (+ null `OPENAI_API_KEY` field); pi/prime auth.json: `access`/`refresh`/`expires`/`accountId`, type oauth; env `OPENAI_API_KEY` |
| **opencode-go** (DeepSeek V4) | opencode, pi, omp, prime | opencode: deepseek-v4-flash (default); pi: deepseek-v4-flash; omp: deepseek-v4-flash; prime: deepseek-v4-flash, deepseek-v4-pro | opencode default agent build/explore/general/deepseek (variant high); pi/omp/prime: default provider (prime: default) | opencode: compaction; pi/omp/prime: compaction per §2 | `~/.local/share/opencode/auth.json`: `opencode-go` → `{type: api, key}`; pi/omp/prime models.json: `apiKey` **redacted** |
| **google** (Gemini / Antigravity) | opencode, pi (auth+store), prime (auth) | opencode: 10 models (gemini-3/3.1 pro+flash, gemini-2.5-pro, antigravity claude-sonnet-4-6, opus-4-6-thinking); pi store: gemini-2.5/3/3.1/3.5/3.6, gemma-4 | opencode: no agent currently assigned to google (UNVERIFIED if any) | n/a | opencode: Antigravity OAuth refresh token in `~/.config/opencode/antigravity-accounts.json` (key names: `refreshToken`, `sessionToken`, `cachedQuota`, `rateLimitResetTimes`); pi/prime auth.json: `google` → `key`, type api_key; env `GOOGLE_API_KEY`/`GEMINI_API_KEY`/`GOOGLE_GENERATIVE_AI_API_KEY` |
| **micu-free2** (Claude proxy) | opencode, omp | opencode: claude-3-5-sonnet, claude-sonnet-4-6, claude-opus-4-6; omp: claude-sonnet-4-6, claude-opus-4-6 | none | n/a | `options.apiKey` / `models.yml apiKey` **redacted** |
| **micu-vip2** (OpenAI proxy) | opencode, pi, omp | opencode: gpt-5.5, gpt-5.5-openai-compact; pi: 8 models (gpt-5.5, 5.4, 5.3 family, codex-auto-review); omp: gpt-5.5 | omp: micu role → gpt-5.5:medium; fallback gpt-5.5→glm-5.2 | n/a | `apiKey` **redacted** (opencode options, pi models.json, omp models.yml) |
| **zai-coding-plan / zai** (GLM-5.2) | opencode, pi, omp, prime | glm-5.2 everywhere (prime: auth only) | opencode: glm agent (subagent, high); omp: advisor/tiny/fast/zai roles, fallback target for sol/terra/kimi/gpt-5.5; pi: enabled model | n/a | `apiKey` **redacted** (opencode options, pi models.json + auth.json, omp models.yml, prime auth.json); env `ZAI_API_KEY` |
| **kimi-for-coding / kimi** (Moonshot) | opencode, pi, omp, prime | opencode: k3-256k; pi: k3-256k, k3, kimi-for-coding-highspeed; omp: kimi-k3; prime: auth only | opencode: kimi agent (subagent, high, 300s); omp: kimi role → k3:high; fallback kimi-k3→glm→nemotron-free | n/a | pi models.json `apiKey`, omp models.yml `apiKey`, prime auth.json `kimi-coding` `key` — all **redacted**; env `KIMI_API_KEY` |
| **nvidia** (NIM) | opencode, pi (auth+store), prime | opencode: minimax-m3 + 16-model whitelist; pi store: 30 models (nemotron-3, gpt-oss, minimax-m3, kimi-k2.6…); prime: minimaxai/minimax-m3 | opencode: nvidia provider no agent assigned (UNVERIFIED); omp: `freeReview` role uses openrouter/nvidia/nemotron-3-ultra:free | n/a | pi auth.json: `nvidia` → `key` (type api_key, `nvapi-…` prefix observed — value redacted); prime models.json `apiKey` **redacted**; opencode: no key in config (**UNVERIFIED** env) |
| **openrouter** | opencode (blacklist only), pi (auth+store), omp, prime (auth) | omp: nvidia/nemotron-3-ultra-550b-a55b:free; pi store: full catalog (~300 models) | omp: freeReview role + fallback chains ending at nemotron-free | n/a | omp models.yml `apiKey`, pi auth.json `openrouter` `key`, prime auth.json `openrouter` `key` — **redacted**; env `OPENROUTER_API_KEY` |
| **tokenrouter** (aggregator) | opencode, pi, prime | opencode/prime: auto:balance/fast/cost/quality, kimi-k3-free; pi: MiniMax-M3 | opencode: no agent assigned (UNVERIFIED); prime: no default | n/a | `options.apiKey`/`apiKey` **redacted** (opencode, pi models.json, prime models.json) |
| **groq** | pi (auth+store) | pi store: llama-3.1/3.3, gpt-oss, qwen3-32b | none | n/a | pi auth.json `groq` `key` (api_key) **redacted**; env `GROQ_API_KEY` |
| **llama-cpp** (local) | opencode (disabled) | qwen3-coder-30b-a3b (Q4_K_M) | none | n/a | `apiKey: none` |
| **zenmux** | opencode | none | none | n/a | no key configured |

---

## 4. Synthesis — Diffundo provider matrix

### 4.1 Roles (from Cambium architecture: fast / balanced / strong / reasoning)

The existing research (`docs/research/omp.md`, review `review-llm-design.md` C2) already converged on role-based cascade: a "tier"/"role" field so a request for "a fast coding model" can match interchangeable providers. The local configs give us the operator's own proven assignments. Mapping local evidence onto the four roles:

| role | candidates (local, key present) | rationale |
|---|---|---|
| **fast** | `opencode-go/deepseek-v4-flash` (opencode default; prime default), `openai/gpt-5.6-luna` (luna/smol roles, worker@xhigh), `zai-coding-plan/glm-5.2:low` (omp `fast` role), `kimi/kimi-for-coding-highspeed` (pi), `openrouter/nvidia/nemotron-3-ultra:free` (omp freeReview, free tier) | cheap, high-concurrency; luna/low and glm/low explicitly chosen for "fast" and "tiny" roles |
| **balanced** | `openai-codex/gpt-5.6-sol:medium` (default/general/explorer/reviewer/task roles), `openai-codex/gpt-5.6-terra` (catalog "balanced agentic coding"), `zai/glm-5.2` (pi enabled), `kimi/k3-256k` (pi enabled, kimi role high) | the everyday workhorse tier |
| **strong** | `openai/gpt-5.6-sol` (plan/sol/reviewer agents, variant high), `openai-codex/gpt-5.6-sol:xhigh` (omp plan role), `zai/glm-5.2:high` (omp zai role; used for architecture reviews per `implementation-plan.md`), `kimi/k3` | complex tasks, audits, architecture |
| **reasoning** | `openai/gpt-5.6-sol` (variants high/xhigh/max; reasoning=true), `google/antigravity-claude-opus-4-6-thinking` (thinkingBudget 8k–32k), `google/antigravity-gemini-3.1-pro`, `zai/glm-5.2` (omp plan@xhigh), `kimi/k3-256k` (thinkingLevelMap xhigh→max) | explicit thinking/variant support is the differentiator |

### 4.2 Recommended Diffundo provider matrix (shape)

Diffundo's `Provider` dataclass (system-design M2: `name`, `model`, `priority`, `timeout`, `cooldown_until`, `rate_limit_remaining`) is missing exactly what the local configs prove necessary (review C2/C3): **role/tier**, **max concurrency**, **fallback chain**, **auth ref**, **context window**, and **capability flags**. Recommended schema:

```text
Provider:
  name            # provider id (e.g. "openai-codex")
  role            # fast | balanced | strong | reasoning   (from C2 tier field)
  models          # list of model ids per role  (mirrors opencode provider.model lists)
  base_url
  api_key_env     # env var name ONLY — never the value (lesson §6.1)
  max_concurrency # per-provider in-flight cap (omp: 12/2/2/1/2/2)
  fallback_chain  # ordered model/provider chain per role (omp fallbackChains)
  cooldown        # seconds after failure (system-design 60s)
  context_window  # per model (gemini 1M vs haiku 200K — review C3)
  capabilities    # reasoning variants, thinking budgets, tool-calling format
```

### 4.3 Providers actually available with keys on this machine (by name)

Confirmed to have key material present locally (auth.json entry, inline apiKey, or documented env var):

1. **openai / openai-codex** (ChatGPT OAuth — codex, pi, prime auth files; opencode OAuth)
2. **opencode-go** (auth.json key + inline in pi/omp/prime)
3. **google** (Antigravity OAuth in opencode; api_key in pi/prime; env vars)
4. **zai-coding-plan / zai** (opencode inline, pi auth+inline, omp inline, prime auth; env `ZAI_API_KEY`)
5. **kimi-for-coding / kimi** (pi inline, omp inline, prime auth; env `KIMI_API_KEY`)
6. **micu-free2** and **micu-vip2** (opencode inline, omp inline, pi inline)
7. **openrouter** (omp inline, pi auth, prime auth; env `OPENROUTER_API_KEY`)
8. **nvidia** (pi auth `nvapi-…`, prime inline)
9. **tokenrouter** (opencode inline, pi inline, prime inline)
10. **groq** (pi auth; env `GROQ_API_KEY`)

Plus local **llama-cpp** (no key needed, but currently `disabled` in opencode). **zenmux** is configured with no key — usable status **UNVERIFIED**.

Count: **13 configured provider identities**; **11 with verifiable key material** on this machine (opencode-go, openai, google, zai, kimi, micu×2, openrouter, nvidia, tokenrouter, groq). Distinct *models* configured across all tools: ~40 (OpenAI 5.6/5.5/5.4 family, DeepSeek V4 flash/pro, GLM-5.2, Kimi K3 family, MiniMax M3, Gemini 3/3.1/2.5 family, Claude Sonnet/Opus via Micu+Antigravity, nemotron/gpt-oss via NIM/OpenRouter).

### 4.4 Fallback-chain synthesis (what the operator actually built)

The most reusable artifact for Diffundo is OMP's `fallbackChains`. Generalized across the four roles:

- **fast:** opencode-go/deepseek-v4-flash → kimi/highspeed → openrouter/*:free
- **balanced:** openai-codex/gpt-5.6-terra → zai/glm-5.2 → openrouter/nemotron-ultra:free *(literal omp chain)*
- **strong:** openai/gpt-5.6-sol → zai/glm-5.2 → kimi/k3-256k
- **reasoning:** openai/gpt-5.6-sol:xhigh → zai/glm-5.2:high → google/gemini-3.1-pro

Note the operator's chains **cross providers, not just models** — exactly what review C2 requires ("cascade should be across providers… and explicitly across models of comparable tier"). Diffundo should adopt per-role ordered chains as first-class config, not `if provider.model != model: continue`.

---

## 5. Config lessons for Cambium

1. **Per-role routing + per-role thinking budgets is the house style.** OMP's `modelRoles` (role→`provider/model:variant`) with `thinkingBudgets` (1024…49152) and Pi's `thinkingBudgets` (1024…32768) both map onto Diffundo's role tier. Cambium's workers should carry a `variant`/`thinkingLevel` per role, not just a model id.
2. **Fallback chains must be ordered, per-model/provider, and cross-tier.** OMP is the reference implementation (`fallbackChains`), and review C2/C3 names the requirement. A flat "try N providers" loop is proven insufficient by the design review itself.
3. **Per-provider concurrency caps exist in practice.** OMP caps: openai-codex 12, zai 2, kimi 2, micu 1, openrouter 2, nvidia 2; pi `subagents.maxParallel: 12`; codex `max_concurrent_threads_per_session: 12`. Diffundo's race mode must respect per-provider in-flight caps (an uncapped race would hammer the 1-cap micu tier).
4. **Plaintext keys in git-tracked config are a real risk here.** `~/.omp/agent/models.yml` is git-tracked **and contains live API keys**. OMP/Pi `.gitignore` `auth.json` but not `models.yml`. Diffundo must never inline keys; store only an **env-var name reference** (`api_key_env`), with values sourced from the environment (this machine already has `~/.opencode/.env` with 10 key *names*).
5. **Multiple providers use OpenAI-compatible HTTP** (micu, zai, kimi, opencode-go, tokenrouter, llama-cpp, zenmux all `openai-completions`/`openai-compatible`). Diffundo's LiteLLM-backed design (system-design M2) fits; the base URLs are all OpenAI-shaped and cheap to register.
6. **Aggregators/auto-routing are already in use:** TokenRouter (`auto:balance/fast/cost/quality`), OpenRouter free tier, OpenCode Zen free models. A Diffundo fallback chain should be allowed to end at an aggregator's `auto:*` or `:free` endpoint to absorb long-tail failures.
7. **Enabled-model lists are explicit allowlists.** OMP `enabledModels` (7), pi `enabledModels` (7), opencode `agent.*.model`. Diffundo should carry an explicit allowlist per role, plus `disabledProviders`/`disabledModels` (opencode disables llama-cpp; omp disables gpt-5.4/ollama/llama.cpp/lm-studio) — the operator actively prunes stale/broken models.
8. **Compaction budgets are tuned per tool, not per provider** (opencode 28k/12k, pi 16k/20k, omp 20k/12k, prime 24.6k/24k). Diffundo's caching (keyed on task+context+model+TTL per implementation-plan.md) should treat compaction as orthogonal to provider choice.

---

## 6. Sources (files) and commands used

**Files (all read from the live machine, never modified):**
- `~/.config/opencode/opencode.json`, `~/.config/opencode/openai-compact.jsonc`, `~/.config/opencode/tui.json`, `~/.config/opencode/antigravity-accounts.json` (structure only), `~/.local/share/opencode/auth.json` (structure only), `~/.opencode/.env` (key names only)
- `~/.codex/config.toml`, `~/.codex/auth.json` (structure only), `~/.codex/model_catalog.json`, `~/.codex/agents/{default,explorer,luna,reviewer,worker}.toml`
- `~/.pi/agent/models.json`, `~/.pi/agent/settings.json`, `~/.pi/agent/auth.json` (structure only), `~/.pi/agent/subagents.json`, `~/.pi/agent/models-store.json`, `~/.pi/agent/pi-codex-conversion.json`, `~/.pi/agent/openai-server-compaction.json`
- `~/.omp/agent/config.yml`, `~/.omp/agent/models.yml`
- `~/.prime/agent/models.json`, `~/.prime/agent/settings.json`, `~/.prime/agent/auth.json` (structure only), `~/.prime/agent/telemetry.json`

**Commands used (analysis only):**
- `python3 /tmp/opencode/structure.py <json>` — JSON structure walker; prints `key: type/value`, replaces secret-typed key values and token-shaped strings with `***[redacted]`.
- `python3 /tmp/opencode/names_only.py <auth.json>` — prints key names, value types, lengths only; never values.
- `python3 /tmp/opencode/redact_toml.py <config.toml> <agents/*.toml>` — line-based TOML reader, redacts `*key*`/`*token*`/token-shaped values, keeps numeric/URL scalars.
- `python3 /tmp/opencode/redact_yaml.py <config.yml> <models.yml>` — line-based YAML reader with same redaction policy.
- `git -C ~/.omp/agent ls-files`, `git -C ~/.pi/agent ls-files`, `git -C ~/.config/opencode log --oneline -5` — confirmed which config files are version-controlled (found: omp `models.yml` tracked with keys; pi `auth.json` and `models-store.json` gitignored).

**Not contacted:** no LLM/API endpoint was called; no key value was used. Task was config-analysis only.

---

## 7. UNVERIFIED items (explicit)

- Whether opencode `provider.nvidia` and `provider.opencode`/`kimi-for-coding` obtain keys from env vs. another store — no key present in `opencode.json`.
- Whether `zenmux` is usable (no key anywhere).
- Whether Codex has internal retry/fallback and server-side caching (not expressed in `config.toml`).
- Whether prime enforces any max concurrency (no setting present).
- The exact value/validity of any key (deliberately not inspected).
