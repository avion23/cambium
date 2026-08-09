# py.dev / JetBrains AI — Competitive Analysis

**Date:** 2026-08-09
**Scope:** Web-only research for Cambium (Python-native multi-agent coding harness; see `docs/system-design.md`).
**Status:** All claims sourced from fetched URLs. Anything not verifiable is marked UNVERIFIED.

---

## 0. Verification caveat: `py.dev` the domain was unreachable

py.dev is **not installed locally** (nothing to run or inspect), and the live website **could not be fetched or resolved from any path tried** during this research. Evidence gathered:

- `https://py.dev/` → transport error via the webfetch tool.
- `curl` → `Could not resolve host: py.dev`; HTTP code 000 for `https://`, `http://`, and `www.py.dev`.
- DNS: SERVFAIL from the local resolver; REFUSED / "No Reachable Authority (At delegation py.dev)" from Google (8.8.8.8), no answers from Cloudflare (1.1.1.1) or Quad9 (9.9.9.9); no A or NS records returned. Other `.dev` domains (e.g. `google.dev`, `air.dev`) resolve fine, so this is specific to `py.dev`.
- Jina AI's external reader infrastructure: `Could not resolve hostname` for `https://py.dev/`.
- Wayback Machine: **no snapshots** for `py.dev`, `www.py.dev`, `http://py.dev`, or `py.dev/*` (CDX API and availability API both empty; direct snapshot fetch → 404).
- Marginalia search for `py.dev`: 0 results. JetBrains blog search (`?s=py.dev`): no `py.dev` string present in any result. GitHub repo search: no product repository under that name.

**Consequence:** every claim about the "py.dev website" itself (its features, wording, pricing page) is **UNVERIFIED** in this report. The research below documents the verifiable substance of the same product family that py.dev is claimed to front — **JetBrains AI** — as it is currently presented on JetBrains' own sites, plus the adjacent products **Junie**, **Air**, **ACP**, and **Mellum**. Note: none of the JetBrains pages fetched during this research reference a "py.dev" site.

---

## 1. What it is

### 1.1 JetBrains AI ecosystem (the product family)

The current JetBrains AI offering is an ecosystem, per https://www.jetbrains.com/ai/ (fetched 2026-08-09):

- **AI features in JetBrains IDEs** — in-IDE assistance, agentic workflows, code/version-control/database AI features.
- **Junie** — JetBrains' own coding agent (`https://www.jetbrains.com/junie/`).
- **Integrated third-party agents** — "Claude, Codex, and Gemini" via JetBrains AI; the page lists "Junie by JetBrains, OpenAI Codex, Claude Agent, and **Gemini CLI**" as integrated options, accessible with JetBrains AI **or your own provider API keys**.
- **JetBrains Air** — "the agentic development environment JetBrains Air" at `https://air.dev/`.
- **Enterprise governance** — "JetBrains Central" and JetBrains IDE Services.
- **Mellum** — JetBrains' proprietary model used for code completion / next-edit suggestions.

Positioning on the same page: "Built for professional software development – not to replace it"; "no vendor lock-in" is a repeated claim; "Your code, prompts, and company data remain entirely yours"; enterprise deployment "cloud, on-premises, or isolated environments."

### 1.2 AI Assistant (the in-IDE plugin)

The in-IDE product is the **AI Assistant plugin**. Per the PyCharm docs (https://www.jetbrains.com/help/pycharm/ai-assistant-in-jetbrains-ides.html, updated 23 July 2026):

- **Not bundled and not enabled in PyCharm by default.** Requires installing the plugin, acquiring a JetBrains AI Service license, and explicit consent to the JetBrains AI Terms of Service / Acceptable Use Policy.
- Key capabilities: **coding agents** (Junie, Claude Agent, Codex, GitHub Copilot — "an agent plans the work, edits files, runs commands and tests, and reports progress, while you review, keep, or roll back the changes"); **external agents via ACP**; **MCP tools** (extend agents, or expose the IDE itself as an MCP server); **flexible setup** (JetBrains AI subscription, BYOK, provider accounts, or local models); **context-aware AI Chat**; **in-editor code assistance** (autocomplete + "next edit suggestions").
- Compatible with PyCharm and "almost all other JetBrains IDEs."

From https://www.jetbrains.com/help/ai-assistant/about-ai-assistant.html: AI Assistant is "a collection of AI-powered features and coding agents integrated into JetBrains IDEs." Compatible IDEs listed: CLion, DataGrip, DataSpell, GoLand, IntelliJ IDEA, PhpStorm, PyCharm, Rider, RubyMine, RustRover, WebStorm, plus Android Studio and ReSharper. Workflow: user triggers a feature → AI Assistant collects IDE context (open file, selected code, recent changes) → request + context sent to a cloud AI model → response returned to the IDE.

### 1.3 AI Chat: the interaction surface

From https://www.jetbrains.com/help/ai-assistant/ai-chat.html: AI Chat is a tool window (right toolbar) with two modes — **Chat** (answers/questions, generates snippets, **never applies changes automatically**) and **Agents** (multi-step, modifies multiple files, reports progress, changes can be kept or rolled back). There is a model selector (JetBrains AI service models, third-party providers, or locally hosted models). Chat opens with a JetBrains-"Recommended agent" chosen by "JetBrains benchmarks." Context is attached as files, folders, images, symbols, or commits.

### 1.4 Junie: JetBrains' own agent (IDE plugin + CLI)

From https://www.jetbrains.com/help/ai-assistant/junie-agent.html and https://www.jetbrains.com/junie/:

- Junie "autonomously plan[s] and execute[s] complex, multi-step actions"; can make large edits, run tests/terminal commands, use external tools.
- **"Junie is also available in an interactive terminal interface"** — i.e. a CLI exists (official CLI site: https://junie.jetbrains.com/, which redirects from `jetbrains.com/junie-github`).
- In the IDE it requires a JetBrains AI subscription; it can auto-collect IDE context (open file, selected text) — broader project context is NOT auto-added. Model + "Reasoning level" selectable; higher levels "may take longer."
- **Brave Mode** executes commands/file edits without confirmation; also an "Auto" option.
- **Debug mode** (IntelliJ IDEA Ultimate only): Junie drives the IDE debugger via a bundled "Debugger MCP Toolset" — sets breakpoints, inspects runtime state, evaluates expressions in the paused frame, steps execution.

From the Junie CLI site (https://junie.jetbrains.com/):

- "The coding agent that works with any model you choose." "Free to start. No card or subscription required." (5 AI credits included; otherwise **BYOK with provider-rate pricing, zero markup**; supports locally running models.)
- "Cost-efficient by design": "Plan on a powerful model, implement on a fast one. Same quality, fraction of the cost."
- **Advanced Plan Mode**: writes structured plan requirements/design/delivery stages before touching code; plans live in `.junie/plans` — "editable, committable"; user approves or redirects.
- **Live Prompting** (mid-task steering) and **Human in the Loop** with a "dynamic allowlist" and user-confirmed execution.
- Custom guidelines/skills: team coding standards, naming conventions, review rules; `/commands` shared between CLI and IDE via ACP.
- GitHub: https://github.com/JetBrains/junie. Claim: "Top performer on SWE-Rebench."

### 1.5 Air: the agentic development environment

From https://air.dev/ ("Air: Multitask with agents, stay in control"):

- "JetBrains Air is the Agentic Development Environment where Codex, Claude Agent, Gemini CLI, and Junie execute independent task loops without interfering with each other."
- "Run tasks in parallel – without conflicts": launch multiple agents at once, choose where each runs; **Air handles setup with Docker, Git worktrees, or cloud environments (coming soon)** — "keeping every task isolated."
- Task list gives an overview of parallel agent progress; you can switch into a task to add input.
- "Review and commit changes"; "language-aware navigation"; reference files, commits, symbols, or images as context.
- Coming: cloud agents and automations ("Run agents in the cloud without local setup… Start and review tasks from your browser").

### 1.6 ACP: the agent/editor interoperability protocol

From https://www.jetbrains.com/acp/:

- The **Agent Client Protocol** is an open protocol "developed openly by JetBrains and Zed" defining how IDEs and AI coding agents communicate; "a standard protocol, developers and teams can implement ACP in their own agents."
- No vendor lock-in; supports "local, remote, and in-house agents."
- ACP agents listed on the page: Gemini CLI, GitHub Copilot, Codex, Cursor, Mistral Vibe, **OpenCode**, Kimi CLI, Qwen Code, Factory Droid, Cline, Kiro CLI.
- Per https://www.jetbrains.com/help/ai-assistant/acp.html: ACP agents can be installed from a curated registry or configured manually in `~/.jetbrains/acp.json`; **no JetBrains AI subscription required** for ACP agents; **not supported in WSL**; the IDE can expose its own IntelliJ MCP server to agents.

### 1.7 Models and BYOK

From https://www.jetbrains.com/help/ai-assistant/use-custom-models.html and https://www.jetbrains.com/ai/:

- Default: a curated set of **cloud models** served through the JetBrains AI service (providers: OpenAI, Google, Anthropic, xAI).
- BYOK supported for: Anthropic (Claude), Google (Gemini API or Vertex AI), OpenAI (GPT/o-series), OpenAI-compatible endpoints (e.g. llama.cpp, LiteLLM), **Ollama**, **LM Studio** (local models). Local models are selectable in AI Chat and assignable to features.
- JetBrains' own **Mellum** model powers code completion / next-edit suggestions.
- Admin-controlled environments (JetBrains IDE Services / JetBrains Central) can restrict third-party BYOK.

### 1.8 Pricing

From https://junie.jetbrains.com/ (current JetBrains AI pricing, as presented there):

- **Free to Start**: 5 AI credits included; BYOK at provider rate, zero markup.
- **AI Pro**: $8.33/user/month — 10 AI credits per 30 days, anytime top-ups.
- **AI Ultimate**: $25.00/user/month — 35 AI credits per 30 days; "Recommended for Junie."
- SOC 2 certification claimed.
- From https://www.jetbrains.com/help/ai-assistant/jetbrains-ai-subscription.html: a limited **30-day AI Pro trial** is available to holders of paid/complimentary JetBrains IDE licenses; requires linking a payment card; after trial expiry you move to a paid license or are "automatically moved to the AI Free tier." Requires IDE 2023.3+ (2024.1.1+ for Community editions, 2025.1+ for PyCharm Unified).

---

## 2. What it does well

1. **Deep IDE integration and project context.** The core value is context derived from the IDE: "the currently open file, selected code, or recent changes" are gathered automatically; you can attach "files, folders, images, symbols, commits" (https://www.jetbrains.com/help/ai-assistant/ai-chat.html; https://www.jetbrains.com/help/ai-assistant/about-ai-assistant.html). Junie auto-receives the active file/selection (https://www.jetbrains.com/help/ai-assistant/junie-agent.html).
2. **First-party code-analysis integration.** "When Junie updates your code, it uses the power of your IDE to make sure every change meets your standards. With built-in syntax and semantic checks" (https://www.jetbrains.com/junie/). The debugger-aware **Debug mode** (breakpoints, runtime state inspection, expression evaluation) is an IDE-embedded capability a terminal agent cannot offer (https://www.jetbrains.com/help/ai-assistant/junie-agent.html).
3. **Test/run integration.** Agents "run code and tests when needed… verifies that everything runs smoothly" (https://www.jetbrains.com/junie/); agent workflow includes "runs commands and tests, and reports progress" (https://www.jetbrains.com/help/pycharm/ai-assistant-in-jetbrains-ides.html).
4. **Agent choice without lock-in.** Native support for Junie + Claude Agent + Codex + Copilot, external agents via ACP, and "no vendor lock-in" as explicit positioning (https://www.jetbrains.com/ai/). ACP agents work without a JetBrains AI subscription (https://www.jetbrains.com/help/ai-assistant/acp.html).
5. **Parallel isolated multi-agent execution (Air).** "Codex, Claude Agent, Gemini CLI, and Junie execute independent task loops without interfering with each other," isolated via Docker or Git worktrees (https://air.dev/).
6. **Cost flexibility.** BYOK at provider rate with zero markup; local models; "plan on a powerful model, implement on a fast one" (https://junie.jetbrains.com/).
7. **Human-in-the-loop controls.** Agent operation modes range from fully autonomous (Brave Mode, Bypass) to require-approval; changes can be reviewed, kept, or rolled back; Junie CLI has a dynamic allowlist and execution confirmation (https://www.jetbrains.com/help/ai-assistant/agents.html; https://junie.jetbrains.com/).
8. **Enterprise governance.** Centralized control of models, agents, data, and deployment; "cloud, on-premises, or isolated environments" (https://www.jetbrains.com/ai/).

---

## 3. What it does poorly / limitations

1. **IDE lock-in and heavyweight install.** AI Assistant is a plugin that is **not bundled and not enabled by default**; it needs a JetBrains IDE, the plugin, a JetBrains AI Service license, and explicit ToS/AUP consent (https://www.jetbrains.com/help/pycharm/ai-assistant-in-jetbrains-ides.html). There is no standalone in-IDE assistant; only Junie decouples into a CLI (https://junie.jetbrains.com/).
2. **Not a terminal-native agent harness.** The primary surface is the IDE tool window (AI Chat). JetBrains' own agentic-environment product, Air, is a desktop app whose headless/cloud mode is explicitly "coming soon" (https://air.dev/). The CLI story is limited to Junie CLI.
3. **Speed/latency and reasoning cost.** High "Reasoning level" settings "may take longer" (https://www.jetbrains.com/help/ai-assistant/junie-agent.html). Cloud routing of every request adds latency; no claims of fast iteration are made.
4. **Transparency / metered cost model.** Usage is rationed by **AI credits** (5/10/35 per 30 days across tiers) with "anytime top-ups"; model routing for chat features is opaque to the user (https://junie.jetbrains.com/). Cost per user is $8.33–$25/mo before BYOK.
5. **Data-privacy position is a self-reported caveat.** "If you choose to share data with us, this data is used exclusively to improve JetBrains tools and train our own models, like Mellum" — i.e. JetBrains trains its own model on opt-in shared usage data (https://www.jetbrains.com/ai/).
6. **Platform gaps.** ACP agents are not supported in WSL (https://www.jetbrains.com/help/ai-assistant/acp.html); trial eligibility depends on IDE-license type and is not offered to some users (e.g. Mainland China without purchase history) (https://www.jetbrains.com/help/ai-assistant/jetbrains-ai-subscription.html).
7. **Maturity/trust claims are marketing claims.** "Top performer on SWE-Rebench," "Powered by IDEs / IntelliJ IDEA Engine" are JetBrains' own assertions without external benchmark data on the fetched pages (https://junie.jetbrains.com/).
8. **py.dev website itself is unreachable/unverifiable** (see §0).

---

## 4. Relevant lessons for Cambium

1. **Provider-agnosticism is table stakes.** JetBrains markets "no vendor lock-in," BYOK, and local models (https://www.jetbrains.com/ai/). Cambium's FanOut cascade/race across providers (system-design §M2) matches this expectation; its default of cheap-model-first + cooldowns mirrors Junie's "plan on powerful, implement on fast" (https://junie.jetbrains.com/).
2. **Direct architectural overlap with Air.** Air already runs multiple agents in parallel in isolated **Git worktrees** with a task dashboard (https://air.dev/). Cambium's differentiators must be its **deterministic supervisor** (crash recovery, event log, restart policy), Python-native workers, and **DSPy hill-climbing** — capabilities none of the fetched JetBrains material claims.
3. **Plan-as-artifact is a validated pattern.** Junie writes plans to `.junie/plans` — "editable, committable" (https://junie.jetbrains.com/). Cambium's checkpoint + event-log design (system-design §M4) is the durable-execution analogue and should be kept front and center.
4. **Human-in-the-loop expectations.** JetBrains surfaces approval modes, a dynamic allowlist, and keep/roll-back of agent changes (https://www.jetbrains.com/help/ai-assistant/agents.html; https://junie.jetbrains.com/). Cambium's per-task permission spec (init message) should expose the same granularity.
5. **JetBrains' moat is IDE deep analysis — do not chase it.** Syntax/semantic checks, refactoring, and debugger integration are IDE strengths (https://www.jetbrains.com/junie/; https://www.jetbrains.com/help/ai-assistant/junie-agent.html). Cambium should instead lean on Python-native tooling in the loop (pytest, mypy, ruff) and its test-gated merge sequencer (system-design §M7).
6. **ACP is the emerging integration standard.** JetBrains positions ACP as the industry protocol; **OpenCode already implements ACP** (https://www.jetbrains.com/acp/). A Cambium-facing ACP implementation would make it usable from JetBrains IDEs, Cursor, etc. — a cheap distribution channel. The `acp.json` + command-line agent-server shape (https://www.jetbrains.com/help/ai-assistant/acp.html) is simple enough to adopt.
7. **Python-centricity is the wedge.** JetBrains' Python surface is PyCharm/DataSpell via a general IDE; there is no Python-native harness equivalent to Cambium. For Python data-science/ML workloads where a heavyweight IDE-bound agent is overkill, a Python-native CLI harness with crash-safe supervision is a defensible niche.
8. **Credit-metered cloud is a weakness to exploit.** JetBrains AI limits heavy agent use via credits (5/35 per 30 days) (https://junie.jetbrains.com/). Cambium's BYO-cheap-provider cascade has no per-agent credit ceiling — worth stating as a cost advantage in any comparison.

---

## 5. Sources

All fetched 2026-08-09 (UTC). Direct HTML for JetBrains docs pages; `https://r.jina.ai/…` reader-render for the JS-heavy marketing pages (JetBrains marketing pages are client-rendered SPAs whose raw HTML contains only meta tags).

1. https://py.dev/ — **UNVERIFIED**: transport error (webfetch), DNS SERVFAIL/REFUSED from multiple resolvers, no Wayback snapshots, 0 Marginalia results, `Could not resolve hostname` via Jina reader. Site content unknown.
2. https://www.jetbrains.com/ai/ — JetBrains AI ecosystem; integrated agents (Junie, Codex, Claude Agent, Gemini CLI), Air, ACP, Mellum, BYOK, enterprise governance, data/Mellum training statement. (via r.jina.ai)
3. https://www.jetbrains.com/help/pycharm/ai-assistant-in-jetbrains-ides.html — plugin not bundled/not enabled by default; license + ToS/AUP consent; coding agents; ACP; MCP; BYOK; AI Chat; completion. (direct HTML)
4. https://www.jetbrains.com/help/ai-assistant/about-ai-assistant.html — what AI Assistant is; workflow (context → cloud model); IDE compatibility list. (direct HTML)
5. https://www.jetbrains.com/help/ai-assistant/agents.html — agent list and operation modes (Junie Brave/Debug; Claude Agent Auto/Default/Accept Edits/Plan/Don't Ask/Bypass; Codex Read-only/Agent/Agent full access; Copilot Agent/Plan/Autopilot); AGENTS.md/CLAUDE.md handling; skills; MCP. (direct HTML)
6. https://www.jetbrains.com/help/ai-assistant/ai-chat.html — Chat vs Agents modes; Recommended agent; model selector; context attachments (files/folders/images/symbols/commits). (direct HTML)
7. https://www.jetbrains.com/help/ai-assistant/junie-agent.html — Junie behavior; IDE context auto-collection; model/reasoning levels; Brave Mode; Debug mode (IntelliJ IDEA Ultimate); Junie CLI availability. (direct HTML)
8. https://junie.jetbrains.com/ — Junie CLI: free to start, BYOK zero markup, local models, plan-on-powerful/implement-on-fast, Advanced Plan Mode (.junie/plans), Live Prompting, dynamic allowlist, SWE-Rebench claim, pricing tiers ($8.33/$25), 5/10/35 AI credits, SOC 2. (via r.jina.ai)
9. https://air.dev/ — Air: parallel agents (Codex, Claude Agent, Gemini CLI, Junie) in Docker/Git worktrees/cloud (coming), task dashboard, review & commit, cloud coming soon. (via r.jina.ai)
10. https://www.jetbrains.com/acp/ — ACP: open protocol by JetBrains + Zed; no vendor lock-in; agent list including OpenCode. (via r.jina.ai)
11. https://www.jetbrains.com/help/ai-assistant/acp.html — ACP registry vs custom `~/.jetbrains/acp.json`; no JetBrains AI subscription required; WSL unsupported; IntelliJ MCP server exposure. (direct HTML)
12. https://www.jetbrains.com/help/ai-assistant/use-custom-models.html — third-party/local models: Anthropic, Google (Gemini API/Vertex), OpenAI, OpenAI-compatible endpoints (llama.cpp/LiteLLM), Ollama, LM Studio. (direct HTML)
13. https://www.jetbrains.com/help/ai-assistant/jetbrains-ai-subscription.html — JetBrains AI subscription; 30-day AI Pro trial; payment card; AI Free tier fallback; version/eligibility rules. (direct HTML)
14. https://www.jetbrains.com/junie/ — Junie: "right in your IDE," IDE syntax/semantic checks, tests, code/ask modes, available IDEs + Android Studio. (via r.jina.ai)
15. https://www.jetbrains.com/ai-ides/ — AI in JetBrains IDEs product meta: "no vendor lock-in" tagline. (meta tags + via r.jina.ai)

**Not cited as evidence:** user testimonials on the Junie page (X/YouTube posts) and JetBrains' self-reported benchmark claims; treat the latter as marketing.
