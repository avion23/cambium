# py.dev / JetBrains AI — Competitive Analysis

**Date:** 2026-08-09. **Scope:** web-only research for Cambium (`docs/architecture/system-design.md`). **Status:** all claims cite fetched URLs; unsupported claims are **UNVERIFIED**. This is a competitive snapshot, not runtime authority.

## 0. Verification caveat: `py.dev` was unreachable

`py.dev` is not installed locally. During this research, `https://py.dev/` failed in the webfetch tool; `curl` could not resolve `py.dev` (HTTP 000 for HTTPS/HTTP/www); local DNS returned SERVFAIL; Google 8.8.8.8 returned REFUSED/“No Reachable Authority”; Cloudflare and Quad9 returned no A/NS answers. Other `.dev` domains resolved. Jina reader also could not resolve it. Wayback CDX/availability returned no snapshots for `py.dev`, `www.py.dev`, `http://py.dev`, or `py.dev/*`; direct snapshot was 404. Marginalia returned 0 results; JetBrains blog and GitHub searches found no `py.dev` product. Therefore every claim about the site itself is **UNVERIFIED**. The remainder records verifiable JetBrains AI, Junie, Air, ACP, and Mellum material, none of whose fetched pages names `py.dev`.

## 1. What it is

### JetBrains AI ecosystem

JetBrains’ current family includes IDE AI features, Junie, integrated Claude/Codex/Gemini CLI agents, Air, JetBrains Central/IDE Services governance, and Mellum for completion/next-edit. JetBrains positions it as professional-development tooling with no vendor lock-in, BYOK, and cloud/on-prem/isolated deployment. https://www.jetbrains.com/ai/

### AI Assistant and Chat

AI Assistant is a **separate plugin**, not bundled or enabled by default in PyCharm. It requires the plugin, JetBrains AI Service licensing, and ToS/AUP consent. Agents plan, edit, run commands/tests, report progress, and allow keep/rollback; ACP and MCP are supported; BYOK/provider and local models are selectable. AI Chat has Chat mode (answers/snippets; never auto-applies changes) and Agents mode (multi-file changes with progress and rollback), with file/folder/image/symbol/commit attachments and a model selector. https://www.jetbrains.com/help/pycharm/ai-assistant-in-jetbrains-ides.html ; https://www.jetbrains.com/help/ai-assistant/about-ai-assistant.html ; https://www.jetbrains.com/help/ai-assistant/ai-chat.html

### Junie

Junie plans and executes multi-step edits, tests, terminal commands, tools, and (IntelliJ IDEA Ultimate Debug mode) breakpoints, runtime inspection, expression evaluation, and stepping. IDE context auto-includes active file/selection; broader context is not automatic. Brave Mode bypasses confirmation. The CLI says it works with any model, is free to start (5 credits), supports BYOK at provider rate/zero markup and local models, and offers Advanced Plan Mode with editable/committable `.junie/plans`, Live Prompting, dynamic allowlist, and shared `/commands`. Pricing presented: AI Pro $8.33/user/month (10 credits), Ultimate $25 (35 credits), plus top-ups. “Top performer on SWE-Rebench” is a marketing claim. https://www.jetbrains.com/help/ai-assistant/junie-agent.html ; https://junie.jetbrains.com/ ; https://github.com/JetBrains/junie

### Air, ACP, models

Air runs Codex, Claude Agent, Gemini CLI, and Junie in independent task loops; setup uses Docker or Git worktrees (cloud environments are “coming soon”), with a task list, review/commit flow, and planned cloud agents. https://air.dev/

ACP is an open JetBrains/Zed protocol for IDE↔agent communication, with local/remote/in-house agents including OpenCode, Codex, Gemini CLI, Kimi CLI, Cline, and others. Registry/manual config uses `~/.jetbrains/acp.json`; no JetBrains AI subscription is required, but ACP is unsupported in WSL. https://www.jetbrains.com/acp/ ; https://www.jetbrains.com/help/ai-assistant/acp.html

BYOK supports Anthropic, Google API/Vertex, OpenAI, OpenAI-compatible endpoints (llama.cpp/LiteLLM), Ollama, and LM Studio. Mellum handles completion/next-edit. https://www.jetbrains.com/help/ai-assistant/use-custom-models.html

## 2. What it does well

1. Deep IDE context and language-aware syntax/semantic checks; Junie’s debugger integration is beyond a terminal agent. https://www.jetbrains.com/help/ai-assistant/ai-chat.html ; https://www.jetbrains.com/junie/
2. Agents run commands/tests and support keep/rollback; Air provides parallel isolated tasks via Docker/worktrees. https://www.jetbrains.com/help/pycharm/ai-assistant-in-jetbrains-ides.html ; https://air.dev/
3. Agent choice and BYOK/local models reduce vendor lock-in; ACP extends the same surface to external agents without a JetBrains subscription. https://www.jetbrains.com/ai/ ; https://www.jetbrains.com/help/ai-assistant/acp.html
4. Human-in-the-loop controls range from Brave/Bypass to confirmation; Junie adds dynamic allowlists and mid-task steering. https://www.jetbrains.com/help/ai-assistant/agents.html ; https://junie.jetbrains.com/
5. Enterprise controls claim cloud, on-prem, or isolated deployment. https://www.jetbrains.com/ai/

## 3. What it does poorly / limitations

1. **IDE dependency:** AI Assistant needs a JetBrains IDE/plugin/license; only Junie has a CLI. Air is a desktop app and headless/cloud work is “coming soon.” https://www.jetbrains.com/help/pycharm/ai-assistant-in-jetbrains-ides.html ; https://air.dev/
2. High reasoning levels may take longer. Any network-latency comparison is **UNVERIFIED** in the fetched material. https://www.jetbrains.com/help/ai-assistant/junie-agent.html
3. AI-credit metering (5/10/35 credits) makes heavy use opaque and costs $8.33–$25/month before BYOK. https://junie.jetbrains.com/
4. JetBrains says opt-in shared data may train Mellum; this is a self-reported privacy caveat. https://www.jetbrains.com/ai/
5. ACP is unsupported in WSL; trial eligibility varies by license/region. https://www.jetbrains.com/help/ai-assistant/acp.html ; https://www.jetbrains.com/help/ai-assistant/jetbrains-ai-subscription.html
6. “Top performer” and “Powered by IDEs” are vendor marketing claims without benchmark evidence on fetched pages. https://junie.jetbrains.com/
7. `py.dev` itself remains unreachable/unverifiable (see §0).

## 4. Relevant lessons for Cambium

1. Provider-agnostic FanOut, BYOK, and local models are expected; Junie’s powerful-plan/fast-implement split supports role-based cascades. Product positioning is inference from the cited features.
2. Air overlaps parallel worktree execution. Cambium must differentiate with deterministic Custos supervision, crash-safe event logs/checkpoints, Python workers, and DSPy optimization; fetched JetBrains material does not claim these.
3. Keep plans as editable artifacts (`.junie/plans`) and expose per-task approval/allowlist controls.
4. ACP is a low-cost distribution adapter for JetBrains/Cursor-like clients; keep deterministic JSON-lines IPC as the harness core.
5. Do not chase IDE semantic/debugger depth; use Python-native pytest/ruff/mypy and test-gated merge. Python-native CLI + durable supervision is the wedge for data/ML workloads.
6. Credit-metered cloud leaves room for a no-seat, cheap-provider cascade. Pricing/credit facts are verified; advantage is inference.

## 5. Sources

All fetched 2026-08-09:

1. `https://py.dev/` — **UNVERIFIED**; transport/DNS/Jina failures, no Wayback snapshots, no search results.
2. https://www.jetbrains.com/ai/ ; https://www.jetbrains.com/help/pycharm/ai-assistant-in-jetbrains-ides.html ; https://www.jetbrains.com/help/ai-assistant/about-ai-assistant.html ; https://www.jetbrains.com/help/ai-assistant/agents.html ; https://www.jetbrains.com/help/ai-assistant/ai-chat.html ; https://www.jetbrains.com/help/ai-assistant/junie-agent.html
3. https://junie.jetbrains.com/ ; https://github.com/JetBrains/junie/ ; https://air.dev/
4. https://www.jetbrains.com/acp/ ; https://www.jetbrains.com/help/ai-assistant/acp.html ; https://www.jetbrains.com/help/ai-assistant/use-custom-models.html ; https://www.jetbrains.com/help/ai-assistant/jetbrains-ai-subscription.html ; https://www.jetbrains.com/junie/ ; https://www.jetbrains.com/ai-ides/

Marketing pages were read via `r.jina.ai` where needed; direct HTML was used for JetBrains help pages. User testimonials and unverified benchmark claims were not used as evidence.

### Evidence boundaries

The unavailable `py.dev` result is stronger than a normal failed page fetch: three resolver families returned no usable record, Wayback had no snapshot, and unrelated `.dev` names resolved from the same environment. It is therefore not valid to transfer JetBrains product facts to `py.dev` as if the site identity were confirmed. The report treats JetBrains AI as the claimed adjacent family and preserves that uncertainty.

JetBrains’ context advantage is IDE-owned state: active file/selection and recent changes are collected automatically; users can attach files, folders, images, symbols, and commits. Junie’s semantic/syntax checks and Ultimate debugger are IDE engine capabilities. Cambium should not promise equivalence; its defensible boundary is Python-native test/lint/type gates around isolated worker changes.

The human-control surface has several distinct modes. AI Chat’s Chat mode never applies changes; Agents mode performs multi-step edits and offers keep/rollback. Junie exposes Brave Mode and a dynamic allowlist; JetBrains’ agent page lists Codex read-only/agent/full-access and Claude plan/accept-edits/bypass modes. ACP agents use registry or manual `~/.jetbrains/acp.json` configuration and do not require a JetBrains AI subscription, but WSL is unsupported. These facts support a Cambium per-task permission/approval schema, not a claim of equivalent UI.

Pricing is also layered: Junie’s page presents Free-to-Start with five credits, AI Pro at $8.33/month with ten credits, and AI Ultimate at $25/month with 35 credits; BYOK is billed at provider rate with zero markup. The help page describes a 30-day AI Pro trial for eligible IDE licenses, payment-card requirements, and automatic move to AI Free after expiry. These values are dated 2026-08-09 and should be treated as version-sensitive.

Air’s isolation claim is specifically Docker or Git worktrees, with cloud environments and automations marked “coming soon.” The fetched pages do not claim deterministic restart, event-log replay, per-worker watchdogs, or DSPy optimization. Those missing claims define Cambium’s differentiation rather than proving Air lacks internal implementations.

Mellum and the data statement are likewise bounded: JetBrains says Mellum powers completion/next-edit and that opt-in shared data may improve JetBrains tools and train its models. This is a self-reported product statement, not an independent privacy audit.

The product family also exposes a distinction between an agent and an editor client. Junie/Claude/Codex/Gemini can execute work; AI Assistant supplies IDE context and controls; ACP lets an external client host or invoke an agent; Air coordinates isolated task loops. Cambium should implement its own worker protocol first and treat ACP/IDE integration as a later adapter, so a missing IDE never blocks headless execution.

JetBrains’ “no vendor lock-in” claim is supported by the listed third-party agents and BYOK/local endpoints, but the subscription and credit layer remains an entitlement boundary. A future comparison should record model/provider, IDE version, plugin version, license tier, and credit date separately. The current snapshot deliberately does not infer a py.dev URL, package, or version from those adjacent products.

JetBrains help pages also list compatible IDEs (CLion, DataGrip, DataSpell, GoLand, IntelliJ IDEA, PhpStorm, PyCharm, Rider, RubyMine, RustRover, WebStorm, Android Studio, and ReSharper). This breadth explains why the assistant’s context model is IDE-centered rather than Python-harness-centered. The plugin requires IDE/version eligibility rules (PyCharm docs updated 23 July 2026); preserve those dates when rechecking trials or ACP support.
The fetched JetBrains pages also separate local models, provider API keys, and JetBrains AI Service models in the model selector. That is another reason to make provider identity, credential source, and entitlement state separate fields in Cambium rather than one opaque model string.

The fetched-page provenance also included `https://r.jina.ai/…` reader URLs and the failed `http://`/`https://` resolver attempts; these are failure evidence, not product sources. Direct page references retain their trailing punctuation only in prose.
Future snapshots should record IDE/plugin versions, license tier, credit date, ACP platform support, and the unresolved py.dev identity independently.
