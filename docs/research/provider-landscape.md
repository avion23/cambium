# Local provider landscape — Diffundo provider matrix input

**Date:** 2026-08-09. **Target:** provider configuration files on this machine. **Purpose:** input to Cambium M2 Diffundo (multi-provider access/cascade/race/cache; `docs/architecture/system-design.md`, review `review-llm-design.md` C2/C3). **Scope:** config inspection only: no API calls, keys used, or provider network calls. This research snapshot is not runtime authority.

**Redaction:** no credential value appears here. Credential files were read for key names/types/lengths only; values are `***[redacted]`. Non-secret URLs, model IDs, provider names, numbers, and booleans are retained.

## 1. Methodology and provenance

Research worktree: `/tmp/opencode/cambium-providers`, branch `wt-providers`, a worktree of `/home/ubuntu/cambium`. Five tools were enumerated:

- OpenCode: real config is `~/.config/opencode/opencode.json`; the brief’s `~/.opencode/opencode.json` does not exist. `~/.opencode/` holds `.env`, skills, and `antigravity-accounts.json`.
- Codex: `~/.codex/config.toml`, structure-only `auth.json`, `model_catalog.json`, and `agents/*.toml`.
- Pi: `~/.pi/agent/models.json`, `settings.json`, structure-only `auth.json`, `subagents.json`, cached catalog, and conversion/compaction files.
- OMP: `~/.omp/agent/config.yml`, `models.yml`.
- Prime: `~/.prime/agent/models.json`, `settings.json`, structure-only `auth.json`, `telemetry.json`.

Every listed file was read on disk. JSON was parsed with `python3`; TOML/YAML were read through redacting filters. No provider endpoint was contacted. Claims below are direct observations unless marked **UNVERIFIED**.

## 2. Per-tool landscape

### 2.1 OpenCode (`~/.config/opencode/opencode.json`)

- Default `opencode-go/deepseek-v4-flash`, agent `build`; autoupdate off; compaction `auto`/`prune`, reserved 28,000, preserve recent 12,000; `llama-cpp` disabled.
- Providers/models: `google` (Gemini 2.5/3/3.1 and Antigravity Claude variants; OAuth/Gemini CLI); `openai` (gpt-5.6-sol/terra/luna, OAuth); `micu-free2` (Claude); `micu-vip2` (gpt-5.5); `zai-coding-plan` (glm-5.2, timeout false); `nvidia` (MiniMax M3 plus 16-model whitelist); `llama-cpp` (local qwen3 coder, disabled); `zenmux` (none); `tokenrouter` (auto balance/fast/cost/quality and Kimi free); `openrouter` (blacklist only); `opencode` (free models). Inline key values are redacted.
- Agent map: build/explore/general/deepseek → DeepSeek; plan/sol/reviewer → OpenAI Sol; luna → OpenAI Luna; glm → Z.AI; kimi → Kimi K3. No retry/fallback or max-concurrency setting is present; these absences are **UNVERIFIED** behavior. Prompt compaction is the observed cache mechanism; `patch-models-cache.py` exists but was not interpreted.
- Auth: `~/.local/share/opencode/auth.json` contains an `opencode-go` API key. `kimi-for-coding` is referenced but not a provider block; auth source is **UNVERIFIED**. Env key names in `~/.opencode/.env`: `OPENAI_API_KEY`, `OPENROUTER_API_KEY`, `GOOGLE_API_KEY`, `GROQ_API_KEY`, `ZAI_API_KEY`, `GEMINI_API_KEY`, `GOOGLE_GENERATIVE_AI_API_KEY`, `WEB_SEARCH_API_KEY`, `KIMI_API_KEY`, `MOLTBOOK_API_KEY`.

### 2.2 Codex (`~/.codex/config.toml`)

- Default `gpt-5.6-sol`, medium reasoning, plan high, low verbosity. ChatGPT OAuth structure is in `auth.json`; `OPENAI_API_KEY` is null there.
- Catalog fetched 2026-07-31 contains Sol/Terra/Luna, gpt-5.5/5.4/5.4-mini, 5.3-codex-spark, auto-review, per-model reasoning/context, and migration notices to 5.6.
- Multi-agent v2: 12 concurrent threads, max depth 2, role TOMLs (default Sol, explorer/reviewer read-only, Luna, worker Luna/xhigh), tool-output cap 5,000 tokens.
- Config has no retry/fallback/cache settings; internal behavior is **UNVERIFIED**.

### 2.3 Pi (`~/.pi/agent/models.json`, `settings.json`, `auth.json`, `subagents.json`)

- Providers/models: `openai-codex` Sol/Terra/Luna via ChatGPT Responses/OAuth; `micu-vip2` gpt-5.5/5.4/5.3 family; `tokenrouter` MiniMax; `zai` glm-5.2; `kimi` K3 variants; `opencode-go` DeepSeek V4 Flash. Models carry context/max-token fields.
- Default Sol/medium; enabled models = 7; thinking budgets 1,024/4,096/10,240/32,768. Retry enabled, max 3, 2-second base delay; provider retry 0/60-second cap. Compaction reserve 16,384/keep 20,000; Responses/server compaction files enabled. Subagents max parallel 12 with worktrees.
- Auth keys: Google, Groq, NVIDIA, OpenRouter, Z.AI, TokenRouter (API keys) and OpenAI-Codex (OAuth). Cached catalog `models-store.json` records connected provider lists.

### 2.4 OMP (`~/.omp/agent/config.yml`, `models.yml`)

- Role routing: default/general/explorer/reviewer/task → OpenAI Sol medium; worker → Luna high; luna/smol → Luna low; plan → Sol xhigh; advisor/tiny/fast/zai → GLM variants; kimi → K3; micu → gpt-5.5; freeReview → OpenRouter/NVIDIA free.
- Provider order/caps: OpenAI 12, Z.AI 2, Kimi 2, Micu 1, OpenRouter 2, NVIDIA 2. Retry max 10, model fallback enabled, explicit cross-provider `fallbackChains` (Sol→GLM; Terra→GLM→Nemotron free; Luna→Terra; Kimi→GLM→Nemotron; gpt-5.5→GLM). Enabled model list has 7 entries.
- `models.yml` defines Micu, Z.AI, Kimi, OpenRouter, OpenCode-Go and OpenAI overrides with inline plaintext keys (redacted). `disabledProviders` includes gpt-5.4, Ollama, llama.cpp, LM Studio, Z.AI; `disabledModels` includes old OpenAI families.

### 2.5 Prime (`~/.prime/agent/models.json`, `settings.json`, `auth.json`)

- Models: NVIDIA MiniMax M3, OpenCode-Go DeepSeek V4 Flash/Pro, TokenRouter auto routes/Kimi free. Auth names: Google, OpenRouter, Z.AI, Kimi-Coding, OpenAI-Codex.
- Default DeepSeek V4 Flash/high; retry max 2, 3-second base, provider max 1/60-second cap; compaction reserve 24,576/keep 24,000. No concurrency setting (**UNVERIFIED**).

## 3. Comparison table

| provider | tools / notable routing | auth evidence (values redacted) |
|---|---|---|
| **openai/openai-codex** | all five tools; Sol/Terra/Luna; OMP chains Sol→GLM, Luna→Terra, Terra→GLM→free | OAuth in Codex/Pi/Prime; OpenCode OAuth; env `OPENAI_API_KEY` |
| **opencode-go** | all five; DeepSeek V4 Flash default in OpenCode/Pi/OMP/Prime; Prime also Pro | OpenCode auth key; inline keys in Pi/OMP/Prime |
| **google** | OpenCode Gemini/Antigravity; Pi cached catalog; Prime auth | Antigravity OAuth; Pi/Prime API-key entries; env names |
| **micu-free2 / micu-vip2** | Claude or OpenAI-compatible gpt-5.5; Micu cap 1 in OMP | inline `apiKey` fields, redacted |
| **zai / zai-coding-plan** | GLM across tools; OMP fallback target and role variants | inline/auth keys, env `ZAI_API_KEY` |
| **kimi / kimi-for-coding** | K3 variants; OMP chain K3→GLM→free; OpenCode 300-second Kimi role | inline/auth keys, env `KIMI_API_KEY` |
| **nvidia** | MiniMax/Nemotron catalogs; OpenRouter free-review path | Pi auth and Prime inline key; OpenCode source absent (**UNVERIFIED**) |
| **openrouter** | OMP free fallback; Pi catalog; OpenCode blacklist | inline/auth/env `OPENROUTER_API_KEY` |
| **tokenrouter** | auto balance/fast/cost/quality; Kimi free; no assigned default agent (**UNVERIFIED**) | inline keys, redacted |
| **groq** | Pi auth/catalog only | Pi auth and env `GROQ_API_KEY` |
| **llama-cpp / zenmux** | local llama-cpp disabled; zenmux has no models/key | no key / usability **UNVERIFIED** |

## 4. Synthesis — Diffundo matrix

### Roles and local candidates

| role | candidates observed |
|---|---|
| fast | DeepSeek V4 Flash; OpenAI Luna; GLM low; Kimi highspeed; OpenRouter/Nemotron free |
| balanced | OpenAI Codex Sol medium; Terra; GLM; Kimi K3-256k |
| strong | OpenAI Sol high/xhigh; GLM high; Kimi K3 |
| reasoning | Sol high/xhigh/max; Antigravity Claude Opus thinking; Gemini 3.1 Pro; GLM xhigh; Kimi 256k |

The local configs show that Diffundo’s provider record needs more than name/model/priority/timeout: `role`, model list, base URL, **environment variable name only** for auth, max concurrency, ordered fallback chain, cooldown, context window, and capabilities (reasoning/tool format).

Confirmed key material by name: OpenAI/Codex, OpenCode-Go, Google, Z.AI, Kimi, Micu Free/VIP, OpenRouter, NVIDIA, TokenRouter, and Groq. Plus local llama-cpp needs no key but is disabled. Count observed: 13 provider identities; 11 with verifiable key material; roughly 40 distinct configured models. Exact key validity is deliberately not checked.

Generalized OMP chains: fast DeepSeek → Kimi highspeed → free; balanced Terra → GLM → free Nemotron; strong Sol → GLM → Kimi; reasoning Sol xhigh → GLM high → Gemini 3.1 Pro. These are cross-provider chains, not a flat provider loop.

## 5. Config lessons for Cambium

1. Per-role thinking levels are established practice (OMP up to 49,152; Pi up to 32,768).
2. Fallback chains must be ordered per model/provider and cross-provider; OMP is concrete evidence.
3. Race mode must enforce provider caps (12/2/2/1/2/2 observed).
4. Never inline credentials: OMP `models.yml` is git-tracked with live keys; Diffundo should store `api_key_env` only.
5. Most gateways are OpenAI-compatible (Micu, Z.AI, Kimi, OpenCode-Go, TokenRouter, llama-cpp, Zenmux), fitting a LiteLLM-style adapter.
6. Aggregators (`auto:*`, OpenRouter free, OpenCode Zen free) are practical terminal fallback targets.
7. Keep explicit enabled/disabled allowlists; stale models are actively pruned in OMP/OpenCode.
8. Compaction budgets are tool-specific (OpenCode 28k/12k, Pi 16k/20k, OMP 20k/12k, Prime 24.6k/24k), orthogonal to provider selection.

### Field-by-field evidence

**OpenAI/Codex.** Codex’s catalog is the only one with explicit migration notices and per-model reasoning levels in the inspected files. Its OAuth structure has `id_token`, `access_token`, `refresh_token`, and `account_id`; values were never copied. Pi and Prime use OAuth-shaped `access`/`refresh`/expiry/account fields. OpenCode’s OpenAI provider uses OAuth in config while an `OPENAI_API_KEY` name is present in `.env`; this is why identity and credential source must be separate Diffundo fields.

**DeepSeek/OpenCode-Go.** OpenCode, Pi, OMP, and Prime all route to `deepseek-v4-flash`; Prime additionally lists `deepseek-v4-pro`. OpenCode stores an `opencode-go` key in its auth store; Pi/OMP/Prime models files contain redacted inline keys. The same logical provider therefore appears under multiple credential stores and base URLs, requiring normalized provider IDs.

**Z.AI and Kimi.** Z.AI’s GLM-5.2 is a low/fast, advisor, high-review, and fallback target depending on tool. Kimi has K3-256k, K3, and highspeed variants; OMP’s Kimi role uses high thinking and its chain falls through GLM and a free Nemotron endpoint. Pi records both model context metadata and a provider-level retry override; OpenCode assigns a 300-second Kimi subagent timeout. These are model/capability facts, not guarantees of common API semantics.

**Google/Antigravity.** OpenCode defines Gemini and Antigravity Claude models with OAuth/account files; Pi’s cached store contains a larger Google catalog; Prime has auth names but no provider model block. No OpenCode agent is assigned to Google in the inspected config, so configured does not mean selected. Antigravity account values were structure-only.

**Micu, NVIDIA, aggregators, local.** Micu Free/VIP expose Claude or OpenAI-shaped models with inline keys; OMP caps Micu to one request. NVIDIA provides MiniMax/Nemotron catalogs in OpenCode/Pi/Prime; OpenCode has no key in its provider block, so environment sourcing is **UNVERIFIED**. TokenRouter exposes `auto:balance/fast/cost/quality`, while OpenRouter and OpenCode Zen expose free/blacklist paths. llama-cpp is local and keyless but explicitly disabled; Zenmux has no models/key.

### Operational interpretation

The methodology intentionally read `~/.config/opencode/openai-compact.jsonc`, `tui.json`, Pi conversion/compaction files, and Prime telemetry even though they do not all define providers. Those files establish where compaction, telemetry, and client-specific compatibility live. They should not be merged into the provider matrix as if a compaction setting were a provider capability.

The matrix also records disabled entries: OpenCode disables llama-cpp; OMP disables old OpenAI families, Ollama, llama.cpp, LM Studio, and Z.AI. Disabled does not mean unavailable—only that the local tool chose not to route there. Diffundo should retain both an inventory and an enabled allowlist so a canary can explain why a configured model was skipped.

The five tools do not share a single health signal. A provider may have a config block, a key name, a cached catalog, an assigned role, and a fallback edge at different times. Diffundo should distinguish `configured`, `credential_present`, `catalog_resolved`, `enabled`, `assigned`, and `healthy`; only the first five can be derived offline from these files, and `healthy` requires a separate authorized check that this task did not perform. The same distinction prevents a stale catalog row from being treated as a usable fallback.

## Additional concrete comparisons

OpenCode’s model assignments are uneven by design: build/explore/general/deepseek use DeepSeek V4 Flash, plan/sol/reviewer use OpenAI Sol, luna uses OpenAI Luna, GLM is a high-thinking subagent, and Kimi has a 300-second timeout. The file has no explicit retry/fallback or concurrency fields, so “no fallback” means “not expressed in this file,” not “the binary cannot retry.” Its `disabled_providers` setting removes llama-cpp even though a local qwen3-coder endpoint is configured.

Codex is the narrowest provider surface: its local config describes OpenAI/ChatGPT OAuth, model migrations, role TOMLs, 12 concurrent threads, and a 5,000-token tool-output limit, but no provider cascade. Pi has the broadest explicit retry/compaction controls: three retries, 2-second base delay, 16,384/20,000 compaction budgets, 12 worktree subagents, and model context metadata. Prime has two retries and a 24,576/24,000 compaction budget but no concurrency field. OMP is the only inspected config with explicit cross-provider ordered fallback chains and per-provider in-flight caps.

The most important security asymmetry is credential placement. OpenCode stores some `options.apiKey` values in the provider file and also has environment names; Pi and Prime have structure-only auth plus inline model keys; OMP’s `models.yml` is itself tracked by `git ls-files` while containing live keys. Codex’s auth file is OAuth-shaped and its API-key field is null. This is why Diffundo’s schema should carry `api_key_env` only, regardless of which existing tool is used as a routing model.

Base URLs observed in provider files include `https://api-slb.micuapi.ai/v1`, `https://api.z.ai/api/coding/paas/v4`, `https://api.kimi.com/coding/v1`, `https://api.tokenrouter.com/v1`, `https://integrate.api.nvidia.com/v1`, `https://opencode.ai/zen/go/v1`, `https://opencode.ai/zen/v1`, `https://zenmux.ai/api/v1`, `https://chatgpt.com/backend-api`, and `http://localhost:8080/v1`. These URLs are configuration evidence only; the task made no network calls.

The role mapping has a useful capability distinction. “Fast” candidates are chosen through low thinking/cheap or free routes; “balanced” uses medium Sol/Terra; “strong” uses high Sol/GLM/Kimi; “reasoning” selects explicit xhigh/max variants or Gemini/Claude thinking models. Context windows vary materially (the catalog records Kimi 1,048,576 and OpenAI 272,000), so role selection cannot be reduced to price or provider name.

The literal OMP fallback chains cross both provider and model: Terra → GLM → OpenRouter/Nemotron free; Sol → GLM; Luna → Terra; Kimi → GLM → free; gpt-5.5 → GLM. The generalized Diffundo chains in §4 are therefore an inference from observed config, not a live test of provider availability. Aggregators such as TokenRouter `auto:balance`, `auto:fast`, `auto:cost`, and `auto:quality`, OpenRouter free models, and OpenCode Zen free models are configured endpoints, not guaranteed healthy fallbacks.

## 6. Sources and commands

Files read (never modified): `~/.config/opencode/opencode.json`, `openai-compact.jsonc`, `tui.json`, structure-only `antigravity-accounts.json`, `~/.local/share/opencode/auth.json`, key-name-only `~/.opencode/.env`; `~/.codex/config.toml`, structure-only auth, catalog, role TOMLs; Pi models/settings/auth/subagents/catalog/conversion/compaction files; OMP config/models; Prime models/settings/auth/telemetry.

Commands: `python3 /tmp/opencode/structure.py`, `names_only.py`, `redact_toml.py`, `redact_yaml.py`; `git -C ~/.omp/agent ls-files`, `git -C ~/.pi/agent ls-files`, `git -C ~/.config/opencode log --oneline -5`. No LLM/API endpoint was called.

## 7. UNVERIFIED items

- Whether OpenCode NVIDIA/OpenCode/Kimi providers obtain credentials from environment or another store.
- Whether Zenmux works; whether Prime has a concurrency setting.
- Codex internal retry/fallback and server-side cache behavior.
- Exact value/validity of every credential (intentionally not inspected).
Provider identity normalization is required because the same logical endpoint appears as `openai` or `openai-codex`, `zai` or `zai-coding-plan`, and `kimi` or `kimi-for-coding` across tools. Keep display names and adapter IDs separate, and retain the original config path in diagnostics so operators can trace a route back to source.
No key validity, quota, or endpoint health was tested; the matrix is deliberately an offline inventory.
No provider endpoint was contacted.

Do not treat this offline inventory as health proof.
