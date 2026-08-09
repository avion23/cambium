# Cambium — Design Deltas (D1..D7)

**Version:** 1.0.0
**Date:** 2026-08-09
**Branch:** `wt-deltas2` (`/tmp/opencode/cambium-deltas2`)
**Status:** Authoritative design-delta record. Supplements `docs/architecture.md` (v2.0.0) and supersedes it wherever a delta is marked **adopt**. Each delta states the arch section it amends.

**Sources incorporated:** (1) the external re-review of the v2 architecture (in flight at the time of writing — see §0.2); (2) the Prime Agent good parts, researched in `docs/research/prime-agent.md`; (3) explicit orchestrator/user directives dated 2026-08-09.

---

## 0. Reading this document

### 0.1 Verification convention

- **Every citation is a real file/section or a URL.** Files are cited by stable repository-relative path so coherence is auditable when branches merge (`architecture.md` §20 uses the same convention).
- Files **not merged into `main`** at the time of writing are cited with their branch in parentheses. Their content was read from the named branch (real, verified).
- **Every provider-caching claim is backed by a URL** fetched 2026-08-09.
- Anything that could not be verified is marked **UNVERIFIED**.
- Claims that come from the orchestrator directive rather than from a file in the corpus are labeled "directive" — they are requirements, not research findings.

### 0.2 "As of" note

This document is written **as of** the current state of:

- `docs/architecture.md` v2.0.0 — **as of commit `17ef25f` on branch `wt-arch`** (`docs(architecture): fix review findings — durability contract, spec-scaffold alignment, eval sizes, DSPy idioms, merge terminal step`). A **re-review of the v2 architecture is in flight**; this delta document records the dispositions and adopted residue for that review (D6) and must be reconciled against the re-review text when it lands.
- `agents.md` and `docs/module-template/*` — **as of the same `wt-arch` commit**.
- `docs/system-design.md` (v0.1, superseded), `docs/reviews/*` (v0.1 adversarial reviews), and the research subset present in this worktree — **as merged in `main` at the branch point**. `main` has since advanced (research merges for IPC, events, threat-model, sandbox-options, benchmark, etc.); files cited here that are not in this worktree were read from `main` — see §0.3.

### 0.3 Files cited that are not in `main` (branch provenance)

| Path | Branch | Content |
|---|---|---|
| `docs/architecture.md`, `agents.md`, `docs/module-template/*` | `wt-arch` @ `17ef25f` | v2 architecture, orientation, module template |
| `docs/research/threat-model.md` | `main` (merged after this worktree branched; first drafted on `wt-threat` @ `b863084`) | Threat model (R1..R10) — cited by D7 |
| `docs/research/sandbox-options.md` | `main` (merged after this worktree branched; first drafted on `wt-sandbox` @ `242a509`) | Septum sandbox options on this host — retained as evidence by D7 |
| `docs/research/event-schema-draft.md` | `main` | Event-log schema draft — cited by D2 (payload-first `parent_task_id`) |
| `docs/research/ipc-protocol-draft.md` | `main` | Nuntius IPC draft — cited by D3 (`steer`, `ready` gating, `PROTO_OUT_OF_ORDER`, `ready_timeout`) |
| `docs/research/worker-coldstart.md` | `wt-coldstart` @ `108c83d` (still **not** in `main`) | fork-per-task vs persistent-pool benchmark — cited by D3 |

These are the same stable-path references the v2 architecture itself uses for research and drafts that may not be present on every branch (`architecture.md` §20: "references here are by stable path so coherence is auditable when the merge lands").

### 0.4 Summary

| Delta | Source | Status | Amends | One line |
|---|---|---|---|---|
| D1 | USER DIRECTIVE | adopt | §2, §4 (M2), §8, §8.1, §9.2, §9.3, §18.2 (LLM-C1), §18.4 (F2) | Delete the Diffundo local cache; Diffundo becomes a stateless router; provider-side caching is content-addressed and never stale. |
| D2 | USER DIRECTIVE | adopt | §3.4, §6.3, §7.1, §4 (Architectus) | Formalize the Task Tree: nodes = sub-LLM sessions, DAG with cycle detection, upward result envelopes, `parent_task_id` in the event log. |
| D3 | PRIME AGENT | adopt | §5.1, §5.2, §6.4, §7.2, §14 | Persistent named worker sessions: `spawn`→`admission` (supervisor-internal ack), `steer` by `session_id`, child→parent result messages; sessions checkpointed and reloadable. |
| D4 | PRIME AGENT | adopt | §7.1, §7.4, §5.2 (init budget), §10, §7.8 | Task completes only when a gate passes; gate dedup by workspace-hash; max_turns/max_tokens/timeout_ms owned by the supervisor; bounded gate retries. |
| D5 | PRIME AGENT | adopt | §17.3, §17.4, §10, module-template §10, dataset-format §6 | Each module's optimization becomes an evidence-backed refinement loop over harness state with plan/apply, rollback-by-refinement-ID, and canary-gated promotion. |
| D6 | CRITIQUE | adopt (residue); reject-with-reason (EOF praise) | §0, §5.3, §6.2, §7.8, §9.2, §14, §18, §19.15 | Honest status: free-threading pin / event-log writer / merge sequencer / tier cascade predate the critique; "stdout EOF = death" praise rejected; smoke-test milestone adopted as the FIRST implementation milestone. |
| D7 | USER DIRECTIVE | adopt | §2, §4 (M8), §7.2, §8, §11, §12.1, §19, §18.3 (IMPL-M4/M6/C7) | No sandboxing in the harness: Septum removed from v2 scope; containment = worktree isolation + permission allowlists + approval gates; least-privilege worker env (R4). |

---

## D1 — Remove the Diffundo local LLM cache entirely

**Source:** USER DIRECTIVE (2026-08-09). Also resolves review LLM-C1 by deletion.
**Status:** **adopt**
**Amends:** `docs/architecture.md` §2 (layering diagram, Diffundo line), §4 (module catalog, M2 row), §8 (transparency table, Diffundo row), §8.1 (deleted/replaced), §9.2 (cascade `call` step 1), §9.3 (worker-side `CambiumLM` cache flags), §18.2 (LLM-C1 and LLM-M5 rows — the LLM Design table), §18.4 (F2 row reference); `docs/system-design.md` §M2 (superseded, for history).

### WHAT changes

1. **Delete the local LLM response cache from `Diffundo`.** Removed: the LRU store, TTL, per-instance cache state, and the `cache` / `cache_namespace` / `context_hash` parameters on `Diffundo.call` (`architecture.md` §9.2, step 1 "Cache check"), and the `"cache_hit": true` tagging in result envelopes (§8.1). Also removed: §8.1's `context_hash` contract ("calls that omit `context_hash` are rejected when `cache=True`"), the cache-key construction, and the "cache lives here, upstream of workers" claim.
2. **`Diffundo` becomes a stateless router.** Its only state is per-provider cooldown timers (§9.1 `cooldown_s`, §9.2). Module catalog row M2 "State owned" changes from "Cache (bounded, opt-in); per-provider cooldown timers" to "None; per-provider cooldown timers" (`architecture.md` §4). The §8 transparency table's Diffundo row changes from "Owns (cache)" to stateless/pass-through.
3. **Provider-side `cache_control` knobs become per-provider config.** Instead of a local cache, `ProviderConfig` (or the provider adapter in `Diffundo`) carries the provider's own caching affordance — e.g. Anthropic `cache_control` (`{"type":"ephemeral","ttl":"5m"|"1h"}`), OpenAI `prompt_cache_key` / `prompt_cache_breakpoint` / `prompt_cache_options.mode`, DeepSeek (automatic, no client knob). The worker and orchestrator code do not manage any cache; they may only place stable prefixes so the provider's cache hits (guidance, not a correctness mechanism).
4. **`CambiumLM` (worker-side Diffundo integration, §9.3) stops passing cache flags**; it forwards `tier`/`model`/`temperature` only.

### WHY

- **Upstream providers reward caching heavily, and their caches auto-invalidate on content change because they are content-addressed.** Three provider docs, verified 2026-08-09:
  - **OpenAI — Prompt caching:** https://platform.openai.com/docs/guides/prompt-caching — "Cache hits are only possible for exact prefix matches within a prompt" and "Prompt Caching does not change how the model generates output. The model computes a new response from the cached prompt prefix." The cache stores KV activations of the prompt **prefix**, never a response; a changed input is a different prefix, so the cache misses and the answer is freshly computed.
  - **Anthropic — Prompt caching:** https://docs.anthropic.com/en/docs/build-with-claude/prompt-caching — "Cache hits require 100% identical prompt segments" and "Because the hash is cumulative, covering everything up to and including the breakpoint, changing any block at or before the breakpoint produces a different hash on the next request." "What invalidates the cache" documents that modifying tools/system/messages invalidates that level and all levels after it. Caches are isolated per organization/workspace.
  - **DeepSeek — Context Caching:** https://api-docs.deepseek.com/guides/kv_cache — enabled by default for all users; "A subsequent request can only hit the cache if it **fully matches** a **cache prefix unit**"; best-effort, auto-cleared when unused.
- **Resolves LLM-C1 by deletion.** The review's core defect was a *response* cache keyed on `(model, temperature, prompt)` with no repo state (`docs/reviews/review-llm-design.md` C1; consensus F2 in `docs/system-design.md` §9). Without a local cache there is no cache key, no TTL, no coherence window, and no stored response that could be served stale. The deletion also resolves LLM-M5 ("cache per-instance nearly useless") and threat-model R8 ("FanOut cache poisoning / stale cache", `docs/research/threat-model.md` §5 R8) — both are moot by removal.
- **The critique's "`git rev-parse HEAD` in the cache key" concern is moot without a local cache.** Provider-side caching lacks the stale-repo problem for three structural reasons:
  1. **Content-addressed.** The cache key is the exact token prefix. If repo content changed, the tokens reflecting that state changed, the prefix hash differs, and the cache misses. There is no key under which an old prefix can be served against new content (OpenAI/Anthropic/DeepSeek, cited above).
  2. **Per-conversation / per-organization, not a global prompt→answer map.** A cache entry belongs to the request stream that wrote it and its organization (`anthropic prompt caching — cache storage and sharing`); two tasks with different contexts never share a prefix by construction.
  3. **It never caches the answer.** The provider cache stores the prefix computation; the response is regenerated on every call against the current full prompt (OpenAI: "computes a new response from the cached prompt prefix"). The v0.1 failure class — serving a stale completion — does not exist.
  The only residue is a **cost/latency** concern: hit rate degrades if the prompt prefix churns (e.g., repo content embedded mid-prompt). That is managed by prompt structuring (stable prefix first), the same technique opencode uses ("Context Epoch" / stable baseline context — `docs/research/opencode.md` §1 and §4.5), and it is guidance, not a correctness mechanism.
- **Precedent in the competitive corpus:** opencode ships no app-level cache — "App-level caching is essentially absent. Cost/context management is delegated to provider-side prompt caching... OpenCode sidesteps it by not having one" (`docs/research/opencode.md` §3.6; §4.5: "Do NOT ship a naive app-level prompt cache"). Codex likewise: "Prompt caching is server-side OpenAI magic; there is no client-side prompt-hash cache" (`docs/research/codex.md` §7).
- **Simplicity:** deleting the cache removes the only piece of `Diffundo` whose correctness depended on callers getting a `context_hash` right. "Repo-state awareness wherever dedup exists" (D6 residue (a)) is satisfied trivially: the only dedup left in the harness is the D4 gate dedup, which is content-addressed by design.

### Open questions

- **Q1.1** Provider `cache_control` config shape: `ProviderConfig.cache_control: dict | None` with per-provider knobs? Which subset for v2 (Anthropic `ttl`; OpenAI `prompt_cache_key`+`breakpoint`; DeepSeek none)? (Owner: `Diffundo` module author; module spec time.)
- **Q1.2** Should usage telemetry surface `cached_tokens` / `cache_read_input_tokens` in `tool_event` payloads so the harness can *measure* provider-side hit rate? Nice-to-have; does not reintroduce a cache. (Owner: `Diffundo` + `Nuntius` schema.)
- **Q1.3** If a future host system (outside Cambium, per §8.1's "shared cross-worker caching is the host's job") wants a cross-session cache, it must be content-addressed and repo-state-aware (D6 (a)). Out of scope here; documented so the boundary is explicit.

---

## D2 — Formalize the task structure as an explicit Task Tree

**Source:** USER DIRECTIVE (2026-08-09).
**Status:** **adopt**
**Amends:** `docs/architecture.md` §3.4 (`Result` envelope), §3.6/§6.3 (Event schema — `parent_task_id` in payload, not a column), §4 (Architectus — decomposition produces the tree), §7.1 (per-task state machine gains a tree-level completion rule), §16 (host navigation of the session tree); `docs/module-template/example-spec.md` §3 (decomposition output feeds the tree); `docs/research/event-schema-draft.md` §3.1/§3.10 (payload-first `parent_task_id`, adopted).

### WHAT changes

1. **First-class `TaskTree`.** Decomposition (`TaskDecomposer`) produces a tree, not a flat subtask list. Nodes = sub-LLM sessions (workers); edges = parent/child delegation. The tree is a **DAG** with a single root per session. It is validated by a deterministic helper (proposed: `cambium.orchestrator.tasktree` — pure functions for validation, cycle detection, topological ordering) before any dispatch.
2. **Each node owns its conversation/session log.** The node's full conversation (spec, steering turns, tool events, checkpoints) lives under its session store (`${session_dir}/.cambium/sessions/<node_id>/`, aligning with §16.2 layout — the canonical dotted `.cambium/` state dir, per the arch §16.2 naming note), append-only. This is the durable memory of the sub-LLM session (Prime Agent's session-tree precedent: `docs/research/prime-agent.md` §1 — "Sessions: append-only JSONL under `~/.prime/agent/sessions/` with a tree (`id`/`parentId`); in-place branching, `/fork`, `/clone`, compaction").
3. **Results flow upward as result envelopes.** A child completes by emitting a `Result` envelope (§3.4 shape, plus parent linkage); the parent aggregates its own output with child envelopes; the root's envelope is the session result. The orchestrating LLM's feedback is: **upward result envelopes** (child verdicts + metric breakdowns) and **downward steering** (parent follow-up/steering turns, D3).
4. **Event log records `parent_task_id` — payload-first, per the merged events draft.** The events-schema draft (`docs/research/event-schema-draft.md`, now in `main`) models tree linkage in the event **payload**, not as a SQLite column: `submitted` carries `payload.parent_task_id?` + `depends_on?` (§3.1) and `task_decomposed` carries `payload.parent_task_id` + `subtasks[]` + `cycle_detected` (§3.10). This delta **adopts payload-first**: `parent_task_id` is a payload key on `submitted` (critical) and `task_decomposed` (non-critical), and **no new column** is added to the `events` table — a new required column would be a breaking envelope change under the draft's migration policy (§7.2: "Change the envelope: new required column → Yes — breaking"). Tree reconstruction joins `task_decomposed`/`submitted` records on `payload.parent_task_id`; an index over the payload field is a v2.1 optimization if query volume demands it. This lets the tree be reconstructed from the log and lets the host navigate the session tree (§16).
5. **Invariants (normative):**
   - I2.1 **Single root.** One root node per session; every non-root node has exactly one parent (`parent_task_id`).
   - I2.2 **No cycles.** The decomposition graph is a DAG: no cycles, no self-loops, no multi-parent in v2. Cycle detection = topological sort (Kahn) on the decomposition graph before dispatch; a cyclic decomposition is **rejected and the decomposer re-prompted** (bounded retries), per DS-M6. This is the existing v2 intent (`architecture.md` §18.1 DS-M6: "DAG validation in Architectus: topological sort with cycle detection before dispatch; cyclic graphs rejected and re-prompted") made normative and explicit.
   - I2.3 **Depth/width bounds.** `max_depth` default 3 and `max_width` (per-session parallel worker cap, config) enforced by the supervisor at dispatch. (Precedent: opencode caps subagent nesting at `subagent_depth` default 1 — `docs/research/opencode.md` §1; Prime Agent drives deeper trees with process-per-worker isolation — `docs/research/prime-agent.md` §4.1. The 3/1 gap is an open question, Q2.3.)
   - I2.4 **Context composition.** Node context = its own session log (bounded) + parent summary + subtree result envelopes. A node never reads a sibling's raw session; siblings communicate only through the parent (nuclear-family A2A, D3). (The node's "session" is the `NodeSession` of D3 — distinct from the public `Session` of arch §3.3; see the D3 terminology note and Q3.5.)
   - I2.5 **Tree-level completion.** A node reaches terminal state only when its own work is done **and** every child has returned an envelope (recursively). The §7.1 state machine is per-task; D4 adds the gate that defines "work is done."
   - I2.6 **Append-only session logs.** Nodes' logs are immutable history; steering writes new turns, never edits old ones (Prime Agent append-only JSONL sessions — `docs/research/prime-agent.md` §1; opencode durable session history — `docs/research/opencode.md` §1).

### WHY

- The v0.1/v2 design treats subtasks as a flat `list[SubTask]` with a loose `depends_on` field whose lifecycle is under-specified. The reviews flagged exactly this: **DS-M6** "Orchestrator has no cycle detection and a broken task-ID counter" (a cyclic graph leaves tasks `pending` forever) and **LLM review N2** "`SubTask.depends_on` default and DAG handling — no cycle detection; a subtask whose dependency failed sits in `pending` permanently." (Note: the directive cites "DS-M6/LLM-M6", but in `docs/reviews/review-llm-design.md` the DAG/cycle item is **N2**; M6 is the race-mode item. The cycle-detection citation in the LLM review is N2.) The Task Tree makes cycle detection and failed-dependency handling first-class instead of an emergent property of the dispatch loop.
- The tree is also the **durable conversation structure** the persistent-session model (D3) needs: a node is the unit of session identity, checkpointing, steering, and result aggregation. Prime Agent's session tree (`id`/`parentId`, §1), compaction summaries carrying "goal/constraints/progress/blocked/decisions" forward (§4.6 — §2.5 names only the shorter "goals/progress/decisions"), and the named-child list (§4.5) are the proven precedents this delta adopts.
- Without `parent_task_id` the event log cannot answer "who asked for this, and what did it produce" — the core feedback question of the orchestrating LLM. Modeling it payload-first (events draft §3.1/§3.10) keeps the log schema stable (no breaking envelope change, events draft §7.2) while making the causal chain first-class.

### Open questions

- **Q2.1** Where does `TaskTree` live — deterministic helper in the Orchestration layer (proposed) or inside `Custos`? The Deterministic Layer must never import DSPy (`architecture.md` §2), so the tree validator must be pure Python either way.
- **Q2.2** Does "node context" include the full tool-event stream (expensive) or a bounded summary (proposed: bounded summary + parent summary + envelopes)? Token budget implications.
- **Q2.3** `max_depth`: default 3 (proposed) vs opencode's `subagent_depth=1` (`docs/research/opencode.md` §1). Deep trees trade context coherence for parallelism; pick with the D3 steering model in mind. (Owner: orchestrator owner.)
- **Q2.4** Multi-parent (a child feeding two parents) is common in real dependency graphs. Rejected for v2 (I2.2); is it a v2.1 research item with a DAG (not tree) generalization? (Owner: future.)

---

## D3 — Persistent sub-agents: admission, steering, checkpointed sessions

**Source:** PRIME AGENT good part (persistent sub-agents + steering). Direct precedent: `docs/research/prime-agent.md` §2.2 ("Native subagents... results/usage are attributed back... agents message each other without user routing (`agent_message`)"), §2.3 ("Daemon-backed continuity. Sessions, IPython state, schedules, and subagents survive terminal detach and can be reattached (`prime-agent attach`); goals, heartbeats, schedules, and bounded autonomous mode with user-defined gates").
**Status:** **adopt**
**Amends:** `docs/architecture.md` §5.1 (channel invariants), §5.2 (message schema — add `steer`; child→parent result semantics), §6.4 (checkpoint — session reload), §7.2 (spawn returns admission — supervisor-internal ack), §7.1 (lifecycle), §14 (cold-start paragraph — pool deferral re-scoped), §16.3 (proto-AGI lifecycle); `docs/research/ipc-protocol-draft.md` §2.2/§2.3/§3 (`steer`, `ready` gating, `result_envelope`) and §4.1 (`PROTO_OUT_OF_ORDER` — admission collision, resolved).

### WHAT changes

1. **Workers become persistent named sessions.** A worker is identified by a `session_id` (proposal: equal to its node `task_id`, D2) and stays addressable across multiple turns of the parent's direction. `spawn` no longer implies "run to completion"; it means "a session exists and will produce results later."
   **Terminology collision (explicit).** The public `Session` (`architecture.md` §3.3) is **one task execution** of the headless API; reusing "session" for a worker/node collides with that contract. Resolution proposed: introduce **`NodeSession`** — a sub-session owned by a node in the Task Tree (D2), identified by `session_id == task_id`, checkpointed and reloadable — while the public `Session` keeps its §3.3 meaning. The wire messages (`init`/`ready`/`steer`/`result_envelope`) address `task_id`; `session_id` is the same value until the split is finalized (see Q3.5). This collision is also acknowledged in D2 invariant I2.4 ("node session").
2. **`spawn` returns admission immediately (async handle).** Protocol additions to the authoritative schema (`architecture.md` §5.2) and the merged IPC draft (`docs/research/ipc-protocol-draft.md`):
   - Supervisor → Worker: `{"type":"steer", "request_id":..., "session_id":..., "context":"<parent's follow-up / steering turn>"}` — parent direction to an existing session (repeatable). **Valid only after `ready`** (worker RUNNING), consistent with the IPC draft's gating ("The orchestrator must not send further requests until `ready`", §2.3, per `arch §7.2`).
   - **`admission` is a supervisor-internal ack, NOT a wire message before `ready`.** The first draft of this delta proposed a worker→supervisor `admission` message sent immediately on accepting `init`, before `ready`. That **collides** with (a) `arch §7.2`'s `ready_timeout` (default 60 s), which bounds exactly the `init → ready` handshake (IPC draft §6) — a pre-`ready` wire message has no timer slot; and (b) the IPC draft's `PROTO_OUT_OF_ORDER` state-machine rule ("Message violates the state machine (result before `ready`; `run_task` before `init`; ...)", §4.1), which logs-and-ignores out-of-order messages and counts repeated violations as a crash. **Resolution: `admission` is a control-plane ack from `Custos` to the orchestrator/host, issued synchronously when the spawn is accepted and before the worker is RUNNING**; the wire handshake stays `init → ready` with `ready_timeout` unchanged, and results arrive later as messages.
   - Worker → Supervisor: child→parent result messages — the existing `result_envelope` event (IPC draft §3) flows up the tree (D2) as a **message**, not merely a terminal report.
3. **Sessions are checkpointed and reloadable.** `§6.4` checkpoint semantics extend from "task resume" to "session resume": the checkpoint `state_ref` plus the node's own session log are the reload state; on crash/restart the supervisor reloads the session (own log + DSPy trajectory + steering history) rather than starting a fresh task. This directly answers Prime Agent's observed failure mode "children die mid-work — checkpoint early... 'The isolated session worker stopped during in-flight work... uncertain model, tool, bash, or child-agent work was not replayed'" (`docs/research/prime-agent.md` §3.3).
4. **A2A within the nuclear family (parent/child/sibling).** Messages are routable by `session_id`: parent→child (steer), child→parent (result envelopes), sibling→sibling **via the parent** (v2 scope — see Q3.3). Routing is performed by the deterministic supervisor (`Custos`), never directly process-to-process; this keeps the ACK-loop pathology outside Cambium's trusted core. (The pathology is documented for opencode as "bidirectional agent-to-agent messaging degenerates into ACK loops" — but that claim is **UNVERIFIED against primary sources**: `docs/research/opencode.md` §3 lists it under "Secondhand limitations (UNVERIFIED against primary sources)". It informs the design as a precaution, not as a verified failure.)
5. **`architecture.md` §14 cold-start paragraph is re-scoped.** The current text defers a persistent pool to v2.1 ("it requires a different IPC model (multiple init messages per process)"). D3 adopts exactly that IPC model (multiple `steer` messages per process), so the deferral text is superseded for sessions **within a task**. The cross-task pool question is still open and now has a measured benchmark.

### WHY

- Prime Agent's session model (named, reattachable, steerable children) is its single most productive pattern (`docs/research/prime-agent.md` §2.2–2.3), and Cambium's fork-per-task worker already pays the documented cold-start cost of `import dspy` (~2.1 s) on every spawn. The cold-start benchmark (`docs/research/worker-coldstart.md`, **branch `wt-coldstart`, not in main**) measures: fork-per-task with the realistic worker payload (cambium + dspy) = **~2.09 s per worker, ~6.7 s wall for a 10-worker fan-out**; a pre-warmed persistent session forks in **5.3 ms** (89 MB parent) and brings up 10 in **~24 ms** (~280× on the fan-out). Persistent sessions amortize the 2.1 s dspy import once per session lifetime. (The benchmark's caveat stands: `os.fork` from the asyncio supervisor is unsafe; the win is a **pre-spawned pool of subprocesses**, which is exactly what persistent sessions are.)
- Steering turns make D2's downward direction real: without `steer`, a parent cannot correct a mid-task child, and the orchestrating LLM's only feedback is terminal results. The admission/steer/result-message triple gives the tree its async, message-passing semantics (D2 invariants I2.4/I2.5).

### Open questions

- **Q3.1** Is `session_id` equal to the node `task_id` (proposed) or a distinct namespace? Distinct ids would allow one process to host multiple sessions later (v2.1 pool); identical ids keep v2 simple. (Owner: `Nuntius` + `Custos` authors.)
- **Q3.2** What exactly is reloaded on session resume — full tool-event replay, DSPy trajectory only, or trajectory + steering history + parent summary? Affects the checkpoint payload size and the reload test. (Owner: `Opifex` author.)
- **Q3.3** Sibling→sibling messaging: v2 scope is parent-mediated only. Direct sibling messaging (Prime Agent allows it) is deferred — it needs a routing/rate-limit rule to avoid the opencode ACK-loop pathology. (Owner: orchestrator owner.)
- **Q3.4** Does "persistent" imply the session process stays alive between *tasks* (cross-task pool, v2.1 per §14) or only across *steering turns within a task* (v2, this delta)? The benchmark supports the former eventually; v2 scope is the latter. (Owner: v2.1.)
- **Q3.5** Terminology: adopt a distinct **`NodeSession`** concept (sub-session of a task-tree node, `session_id == task_id`) vs a `Session` subtype with its own `session_id`? The public `Session` (arch §3.3, one task execution) is not renamed; the collision must be resolved before the `src/cambium` API surface grows. (Owner: orchestrator owner.)

---

## D4 — Autonomous gate + budgets

**Source:** PRIME AGENT good part (autonomous mode with user-defined gates and limits). Direct precedent: `docs/research/prime-agent.md` §2.3 ("bounded autonomous mode with user-defined gates"), §5 `--autonomous` ("w/ gates+limits").
**Status:** **adopt**
**Amends:** `docs/architecture.md` §7.1 (state machine — add `GATING`/`GATE_FAILED`), §7.4 (budget fields — add `max_turns`/`max_tokens`/`timeout_ms` ownership), §5.2 (`init` message `budget` block), §10 (tests-as-floor becomes the gate), §7.8 (Unio test gate is the final gate), §4 (Architectus/Unio).

### WHAT changes

1. **Task completes only when a gate command passes.** The gate is a command that verifies the task's outcome (e.g., the task's scenario test suite; `Unio`'s test gate at merge time, §7.8; the `tests` signal in §10). A task whose work is done but whose gate fails does **not** reach `DONE`; it enters `GATE_FAILED`.
2. **Failed gate → bounded output for another attempt.** On gate failure the worker receives the gate's failure evidence (command, output tail, failing assertion) as a steering turn (D3) and is allowed a bounded number of retries (`gate_max_retries`, default 2, owned by the supervisor). After the bound, the task **fails with evidence**: `status="failed"`, `failure_reason` includes the gate command, exit code, and captured output (`Result` §3.4).
3. **Skip re-running the gate when the workspace is unchanged.** Gate verdicts are **content-addressed**: key = `sha256(tree-hash of worktree state || gate command || base_commit || gate input spec)`, stored per-session (in the events DB or a small `gate_verdicts` table). If a retry (or a crash-restart) re-derives an identical key, the prior verdict is reused instead of re-running the gate. This is the same content-addressing argument as D1: the key is derived from the exact bytes that determine the outcome, so it cannot serve a verdict for different state (D6 (a)).
4. **Bounds owned by the supervisor.** `max_turns`, `max_tokens`, and `timeout_ms` are supervisor-owned (`Custos`), carried in `init.budget` (§5.2, which today has `max_wall_s`/`max_restarts`) and enforced by the supervisor — never self-reported by the worker. Exceeding any bound → task `FAILED` (or `timeout`, §3.4). This is the v2 "bounded everything" goal (§1.5, §19.14) applied at per-session granularity.

### WHY

- Prime Agent's autonomous mode is gate-bounded by design; its local failures (children dying mid-work, daemon supervision timeouts — `docs/research/prime-agent.md` §3.2–3.3) are exactly what an explicit gate + supervisor-owned budgets prevent. The v2 design already has the raw material: the `tests` signal is a **floor** in the metric (§10), and `Unio` runs a test gate at merge (§7.8). D4 makes the gate a lifecycle state instead of a scoring input, so "done" cannot be self-reported by the worker.
- The "unchanged workspace → skip" rule is required for crash-restart sanity: after a `Surculus.recover()` (reset --hard, §7.5) the worktree may be byte-identical to a previously-gated state, and re-running a long suite is pure waste. Content-addressing makes the skip provably safe.
- Budget ownership belongs in the Deterministic Layer by the layering invariant (§2): the Deterministic Layer never calls an LLM and never crashes; budgets are deterministic enforcement.

### Open questions

- **Q4.1** Where is the gate executed — in the worker's worktree for iteration (proposed) with `Unio` re-running it as the final gate at merge (§7.8)? Two gate runs per task doubles suite cost; the content-addressed verdict cache (per-session) covers the second run when the tree is unchanged.
- **Q4.2** Gate verdict cache scope: per-session only (proposed) vs shared across sessions. Cross-session sharing reintroduces exactly the coherence question D1 removed. Per-session is the safe default.
- **Q4.3** Is `gate_max_retries` part of the absolute restart budget (§7.4 `absolute_max`) or separate? Proposal: separate counter, but wall-time budget shared. (Owner: `Custos` author.)

---

## D5 — Continual-harness refinement loop → DSPy seam

**Source:** PRIME AGENT good part (continual-harness self-improvement). Direct precedent: `docs/research/prime-agent.md` §1 (continual harness — "prompts, memories, skills, and subagent specs stored as durable state the agent can refine via `/refine` (evidence-backed, snapshot/rollback, never rewrites the base system prompt)") and §2.4 (local `harness/harness_state.json` with a `refine_workflow` prompt and refinement history).
**Status:** **adopt**
**Amends:** `docs/architecture.md` §17.3 (per-module artifacts — add harness state), §17.4 (optimization loop → refinement loop), §10 (canaries gate), §4 (Ascensus M9); `docs/module-template/architecture.md` §10 (optimization plan — plan/apply, rollback by refinement ID); `docs/module-template/dataset-format.md` §6 (canary taxonomy is the gate).

### WHAT changes

1. **Each module's optimization (arch §17) becomes an evidence-backed refinement loop over its own harness state.** Harness state per module = the module's prompt/decide program **plus** skills/memories **plus** dataset **plus** metric (extending §17.3's artifact list). The loop:
   - **Plan/apply split.** A refinement is first a **proposal** (a `refinement_id` + the planned edit to harness state + the evidence behind it). Only after the proposal passes its gates is it **applied** (promoted). This matches the v2 `§17.4` step split (optimize → score → promote) made explicit.
   - **Rollback by refinement ID.** Every applied refinement records a `refinement_id`; promotion is a versioned pointer swap (already the `optimized/<name>/v<N>/` symlink-swap design, `module-template/architecture.md` §10) and any refinement can be rolled back atomically by restoring the previous pointer. Prime Agent's "snapshot/rollback" (/refine) is the precedent.
   - **Refinement gated by eval on train/eval/canaries.** The gate is the existing three-split evaluation: mean metric on frozen `eval.jsonl` ≥ threshold **and** canary pass rate 100% (`module-template/architecture.md` §9.3, `dataset-format.md` §6, `architecture.md` §10 `canaries` signal and §17.4 steps 8–9). **A degraded canary score → the refinement is rejected** — this is already §17.4 step 8; D5 makes "rejected" mean "rollback to the previous refinement_id".
2. **Reward-hacking guard (Prime Agent Factorio case).** The known incident in which a self-improving agent refined its own skills into *cheating skills* (referenced in the orchestrator directive; not in the research corpus — **directive-provided** context) is the exact failure the canary gate targets. Two guards, both in v2:
   - **Canary gate:** canaries are authored at dataset time, frozen (`dataset-format.md` §4, §6), and invisible to the refiner — a refinement that improves the training metric by teaching the model to game the metric (delete failing tests, `assert True`, `# noqa`, no-op patches; `architecture.md` §10 gameability column) fails the frozen canaries and is rolled back.
   - **Human approval for harness-state edits beyond module scope.** Edits to the module's **own** prompt/decide program are gateable by eval alone. Edits that reach **beyond module scope** — the dataset (labels, splits, canaries), the metric, or sibling pins (`meta.json.sibling_pins`, `dataset-format.md` §5) — require **human approval** before apply. This is the §17.4 step-9 human gate, made explicit for the state types it covers.
3. **Connected to metric-design and the canary taxonomy.** The refinement gate consumes `architecture.md` §10's multi-signal metric (tests floor, spec adherence, diff quality, behavioral checks, canaries) and the `dataset-format.md` §6 canary taxonomy (`trivially_atomic`, `must_decompose`, `ambiguous_calibration`, `format_only_hack`, `keyword_hack`, and module-specific kinds). Each refinement's evidence must reference which signals/canaries it is expected to move.

### WHY

- The v2 flywheel (`§17.4`) already has the brakes (held-out eval, canaries, human gate, rollback — §17.4 steps 8–9; the "brakes the v0.1 flywheel lacked" line). D5 raises the abstraction to match what Prime Agent proved workable: a **persistent harness state** that the module itself refines, with an audit trail and rollback. The v0.1 reviews' requirement (LLM-M3: "Optimization flywheel coupled, no stability") is fully met by the loop's plan/apply + canary gate + rollback-by-id.
- The Factorio class of failure is a real, observed failure mode of self-improving harnesses (directive-provided; the generic mechanism is documented in the threat model as "canaries protect the metric" — `docs/research/threat-model.md` §4 M5). The canary gate + human-approval-for-out-of-scope-edits is the cheapest structural defense, and it reuses the dataset machinery Cambium already mandates.

### Open questions

- **Q5.1** Harness-state store layout: proposed `src/cambium/modules/<name>/harness/` (prompts.yaml, skills/, memories/, `meta.json` with refinement history) with promoted artifacts under `optimized/<name>/` (§16.2). Confirm vs the v2.1 split target.
- **Q5.2** Does bumping harness dataset/metric via refinement also bump `dataset_version` and force sibling-pin re-validation (`dataset-format.md` §5 `sibling_pins`)? Proposed: yes for any dataset/metric edit. (Owner: `Ascensus`.)
- **Q5.3** In an unattended loop, who is the "human" for out-of-scope edits? Proposal: an approval gate callback in the host (same mechanism as D7's approval gates) — or an explicit "halt and queue for review" state. (Owner: orchestrator owner.)
- **Q5.4** Refinement evidence format: must a refinement carry a before/after eval delta table (train/eval/canary per signal) to be eligible for apply? Proposed: yes, mandatory. (Owner: `Ascensus`.)

---

## D6 — Honest status vs the external critique + hardening residue

**Source:** EXTERNAL CRITIQUE (the v2 re-review, in flight at the time of writing — see §0.2).
**Status:** **adopt** for the residue ((a) repo-state awareness, (b) smoke-test milestone); **reject-with-reason** for the critique's EOF claim.
**Amends:** `docs/architecture.md` §0 (what changed), §5.3 (four-layer liveness — reaffirmed), §18 (resolution matrix — status notes), §19.15 (smoke test → first milestone); the smoke-test gate referenced in `agents.md` §5/§9 and `docs/module-template/example-spec.md` §9.4.

### WHAT — already resolved before the critique arrived

These items in the critique were **already resolved in `docs/architecture.md` v2.0.0 before the critique was written**; the delta is only a status note so the resolution matrix is honest about provenance:

| Critique item | Resolved by | Citation |
|---|---|---|
| Free-threading pin (risk of running on `python3.14t`) | Standard CPython 3.14, GIL build, pinned `>=3.14,<3.15`; free-threading is an opt-in extra; documented 5–10% overhead and C-extension FT-safety UNVERIFIED | `architecture.md` §14; `docs/research/python-3.14.md`; resolution DS-M5, IMPL-M1 (§18) |
| Dedicated event-log writer (sync I/O in the asyncio loop) | Single-consumer writer thread, bounded queue, fsync cadence, critical-vs-non-critical tiers | `architecture.md` §6.2, §6.5; resolution DS-C1, IMPL-M7 (§18) |
| Serialized merge sequencer (concurrent merges race the shared repo) | `asyncio.Lock`, throwaway worktree, single writer to `refs/heads/main`, atomic `update-ref` | `architecture.md` §7.8, §4 (Unio); resolution IMPL-C1, DS-M1 (§18) |
| Tier-based cascade (exact-model guard made cascade a no-op) | `tier` as primary key; capability filters; no exact-model filter except explicit pin | `architecture.md` §9.2; resolution LLM-C2, IMPL-C10 (§18) |

### WHAT — the critique item that is rejected

- **The critique praised "stdout EOF = death" as a liveness signal. That is rejected.** `architecture.md` §5.3 states the opposite, normatively: v0.1 conflated EOF with death; the v2 **four-layer liveness model** treats EOF as **advisory only** (rank 4 of 4), with process exit (`proc.wait()`) and the `exit` message as definitive, heartbeat watchdog as strong, and a drain-deadline watchdog so supervisor-induced stalls are not blamed on the worker (§5.3, §5.4). The critique's own predecessor review (DS-C2, `docs/reviews/review-distributed-systems.md`) documented four EOF failure modes — grandchild pipe inheritance, Python block-buffering, torn partial writes, supervisor stalls — all of which §5.3/§5.4 explicitly handle. The four-layer model **stands**; re-deriving death from EOF would reopen DS-C2.

### WHAT — adopted residue

**(a) Repo-state awareness wherever dedup exists.** Every remaining dedup/verdict-reuse mechanism in the harness must be **content-addressed** (key derived from the exact state that determines the outcome), never keyed on prompt/identity alone. Concretely in this document: D1's provider-side caching is content-addressed by construction (exact-prefix KV); D4's gate-verdict cache is content-addressed on the worktree tree-hash. If any host-side cache is ever added outside Cambium, the same rule applies (D1 Q1.3). This keeps the LLM-C1 failure class structurally impossible rather than merely mitigated.

**(b) Smoke-test milestone as the FIRST implementation milestone.** Adopted verbatim from the critique, with acceptance criteria:

> **Milestone 0 — "one worker, one file, one merge."** One worker spawns, edits one file in a throwaway worktree, the task's scenario test passes, the branch merges, the process exits cleanly.

Acceptance criteria (each must be VERIFIED with a cited command, per `agents.md` §5):
1. **Environment.** `uv run --python 3.14.7 --extra test` works on the scaffold (`pyproject.toml`), no provider keys required (fake LLM harness).
2. **Spawn.** `Custos` spawns exactly one `Opifex` subprocess into a throwaway worktree created by `Surculus`; the worker emits `ready` echoing the `init` `request_id` (§5.2).
3. **Edit.** The worker performs exactly one file edit via `edit_file` (search-and-replace with uniqueness, §11) and writes a checkpoint (§6.4).
4. **Gate.** The task's scenario test passes inside the worktree (D4 gate; `§7.8` test-gate semantics).
5. **Merge.** `Unio` verifies in a throwaway worktree and publishes via atomic `update-ref` under the lock; `merge_committed` is a durable critical event (§7.8, §6.5).
6. **Result.** `result.json` is written atomically with `status="done"` (§3.4, §16.4) and `Session.run()` returns the `Result`.
7. **Clean exit.** No orphan processes, no leftover worktree entries (`Surculus.prune()`), exit code 0 (§7.7).

This milestone is currently referenced but not yet built: `agents.md` §5 ("`python -m cambium.tests.smoke`"), §9 (item 5), and `docs/module-template/example-spec.md` §9.4 ("pending `Architectus.execute` wiring"). D6 makes it the **entry condition for Phase 1** — nothing else is P0-complete until Milestone 0 passes, which is exactly the gate the v0.1 reviews demanded ("Get the smoke test to pass" — `docs/system-design.md` §9 "Revised Build Priority", Phase 0).

### Open questions

- **Q6.1** The re-review is in flight; when it lands, reconcile this delta's "already resolved" table against its actual text and update citations in §0.2. (Owner: orchestrator.)
- **Q6.2** Milestone 0 depends on `Architectus.execute` minimal wiring — is the milestone gate the headless path (`Session.run()` on an atomic task) or does it include `should_decompose`'s atomic fast path? Proposed: headless path with the atomic fast path. (Owner: `Architectus` author.)
- **Q6.3** Does Milestone 0 use the fake-LLM harness (proposed) or a real provider on the `fast` tier? Fake LLM keeps it deterministic and gated on no external keys. (Owner: test-strategy owner.)

---

## D7 — No sandboxing in the harness; Septum removed from v2 scope

**Source:** USER DIRECTIVE (2026-08-09).
**Status:** **adopt**
**Amends:** `docs/architecture.md` §2 (layering diagram — remove the Septum box), §4 (module catalog — M8 Septum removed from v2 scope; code retained for history, not renumbered), §7.2 (spawn — direct `create_subprocess_exec`, no `sandbox.wrap`), §8 (transparency table — Septum row removed), §11 (run_shell justification), §12.1 (env allowlist enforced at spawn), §19 (items 11, 12, 14 and the "honest gaps" line), §18.3 (rows IMPL-M4, IMPL-M6, IMPL-C7 — status notes). Threat-model `R3` re-rated; `R4` becomes a required fix.

### WHAT changes

1. **Septum (M8) is removed from v2 scope.** There is no `SandboxExecSandbox` / `NoopSandbox` in v2. The layering diagram (§2), module catalog (§4, M8 row), transparency table (§8), and spawn path (§7.2 `sandbox.wrap([...])`) are updated accordingly. Module codes are stable vocabulary (`agents.md` §6) — M8 is marked "Removed — out of scope (2026-08-09)" rather than renumbered.
2. **Containment = git worktree isolation + permission allowlists + approval gates.**
   - **Worktree isolation** is already the deepest containment layer and stays: per-task worktrees, `Surculus` recovery, generation fencing, quarantine (§7.3, §7.5).
   - **Permission allowlists** become the primary policy surface: per-task `permissions` in `init` (§5.2 — `network`, `shell`), the `git_op` op allowlist and list-form `grep_code` (§11), no `fetch_url`/`curl` tool (§11), and the worker env allowlist (R4 fix below).
   - **Approval gates** (new): the host-facing mechanism for risky operations. Proposed v2 shape: a host `approve(session_id, op)` callback for operations outside the pre-declared allowlist (first-time external-path writes, non-allowlisted network egress), wired through the supervisor. Exact shape is an open question (Q7.2).
3. **`docs/research/sandbox-options.md` stays as evidence** (now merged in `main`; truncated to a stub 2026-08-09 — retained as evidence, not normative; the full research is preserved in git history at commit `242a509`). Its core finding is cited: unprivileged namespace sandboxing is **blocked on this host by AppArmor** — `cat /proc/sys/kernel/apparmor_restrict_unprivileged_userns` → `1`, and `unshare -Ur true` → `Operation not permitted` (`sandbox-options.md` stub). The root-mode workaround is also broken for this uid (uid-1001 files invisible/nobody). The document's recommendation table is retained in git history as the record of why sandboxing was evaluated and dropped from v2 scope.
4. **Threat-model R3 is re-rated "accepted — out of scope".** `docs/research/threat-model.md` §5 R3 ("Sandbox gap on macOS and dev/CI (`NoopSandbox`)", formerly `needs v2`) becomes **accepted — out of scope**: with the sandbox removed from v2 entirely, the cross-platform parity gap (Linux-only effective namespace sandbox, best-effort macOS, noop dev/CI) is no longer a platform inconsistency — the harness simply does not sandbox, and the accepted residual risk is the absence of a kernel-namespace boundary for `run_shell`. The remaining controls are worktree isolation, allowlists, and approval gates.
5. **Threat-model R4 is fixed, not re-rated** — least-privilege worker env. `architecture.md` §7.2 currently spawns workers with `env={**os.environ, ...}` (the entire host environment), contradicting §12.1's stated per-worker allowlist ("the sandbox injects only the env keys the worker is authorized to receive via `--setenv`"). With no sandbox to scrub at the `--setenv` boundary, the spawn path must enforce the allowlist itself:
   - Worker env = a constructed dict: `PATH` (minimal), `PYTHONUNBUFFERED=1`, `CAMBIUM_TASK_ID`, `CAMBIUM_GENERATION`, `CAMBIUM_SESSION_ID`, `HOME` (worktree-scoped, optional), **plus only the keys named in `init.provider_env_keys`** (which are names only; `architecture.md` §5.2, §12.2). Everything else is dropped.
   - This resolves `docs/research/threat-model.md` §3.7/R4: a compromised worker can no longer `print(os.environ)` for unrelated secrets, and the egress chain (key → tool output → model context → committed file → merged to `main`) is cut at the source. The **norm** behind the removed sandbox's `--setenv` per-worker key allowlist (`architecture.md` §12.1; resolution IMPL-M6 "sandbox `--setenv` per-worker key allowlist", §18.3) is **retained** — the per-worker key allowlist survives; only its enforcement mechanism moves, from the sandbox boundary to spawn-time env construction.
6. **run_shell policy.** With the sandbox gone, `run_shell`'s `shell=True` (justified in §11 "because the worker runs in a sandbox") loses that justification. The rule adopted: **no shell where a list form exists — already realized for `grep_code` (ripgrep list-form, §11/DS-N4) and `git_op` (list-form + op allowlist, §11)** — and `run_shell` itself remains a deliberate, **explicitly permission-gated** capability: it is only offered when `init.permissions.shell == true`, it is logged verbatim in the event log (§5.2 `tool_event.cmd`), and it runs under the per-task wall/timeout and heartbeat budget (§7.6). It is the documented residual high-privilege tool, gated by the allowlist rather than by the (removed) sandbox.
7. **Resolution-matrix and §19 statuses.** `architecture.md` §18.3 rows touched by the removal get status notes: **IMPL-M4** ("sandbox backend Linux-only — `Septum` has a kernel-namespace backend… `NoopSandbox`") and **IMPL-C7** ("sandbox space in identifier + undefined `sys`") are **moot by removal** (the module no longer exists); **IMPL-M6**'s resolution mechanism ("sandbox `--setenv` per-worker key allowlist", §18.3) is **repointed** to the spawn-time least-privilege env policy of item 5 while the per-worker key-allowlist norm is retained. §19.12 ("Secrets handled once… sandboxed per-worker via `--setenv`") is restated as "per-worker env allowlist enforced at spawn"; §19.11 ("cross-platform from day 1 — Linux/macOS/noop backends") and the §19 "honest gaps" line ("macOS sandbox is weaker than Linux (documented as best-effort)") are **replaced** by the single posture: no sandbox in v2, containment = worktree isolation + permission allowlists + approval gates.

### WHY

- The user directive (2026-08-09) removes sandboxing from the harness. The evidence on this host supports the decision as a *deferral, not a regression*: every namespace-based sandbox option is blocked unprivileged by AppArmor (`sandbox-options.md` stub), the root-mode workaround is broken for uid-1001 state, and the alternatives (Landlock via ctypes, gVisor, nsjail, firejail) are rejected or absent (full record preserved in git history at `242a509`). Retaining `NoopSandbox` in v2 would silently ship "no sandbox" anyway; removing the module makes the posture honest and the containment story unitary.
- The v2 design already carried the claim that sandbox ≠ primary isolation: `architecture.md` §19 lists worktree recovery, fencing, and process-group kill as the liveness/split-brain controls, and the threat model's own summary ranks `run_shell` containment "on Linux (namespace sandbox + network off)" as the only sandbox-dependent piece (`docs/research/threat-model.md` §3.3, §5 R3). D7 re-bases that piece on allowlists + gates.
- R4 must be fixed *because* the sandbox is gone: `--setenv` scrubbing was the stated mechanism for per-worker key allowlisting (§12.1); with Septum removed, the spawn-time env construction is the mechanism.

### Open questions

- **Q7.1** Module-catalog hygiene: keep M8 in the table marked "removed" (proposed, keeps codes stable per `agents.md` §6) vs drop the row and let the next module take the code? Proposed: keep marked.
- **Q7.2** Approval-gate shape: per-task static allowlist only (proposed minimum) vs an interactive/per-operation host `approve()` callback. The current design has no per-tool approval gate ("workers are autonomous", `docs/research/threat-model.md` §4 M4); D7 lists approval gates as a containment layer, so the mechanism must be specified. (Owner: orchestrator + `Custos`.)
- **Q7.3** Least-privilege env and `run_shell`: does `run_shell` need to further restrict env at the subprocess level (e.g., drop `CAMBIUM_*`)? Proposed: inherit the worker's already-scrubbed env as-is; no additional scrub.
- **Q7.4** macOS/dev: with no sandbox anywhere, does `SandboxExecSandbox` (best-effort) survive as an optional extra? Proposed: out of scope with R3; can be re-added as an opt-in backend later without contract change.

---

## 1. Cross-delta consequences

- **Event schema (§6.3):** `parent_task_id` in the `submitted`/`task_decomposed` **payloads** (D2; payload-first per the events draft, no schema column); +gate-verdict records (D4, per-session). Both are additive; the existing gap-free `seq` invariant is untouched.
- **IPC (§5.2):** +`steer` (supervisor→worker, valid only after `ready`); `admission` is a supervisor-internal ack, not a wire message; child→parent `result_envelope` semantics (D3). Additive; `request_id` correlation is preserved.
- **Budgets (§5.2 `init.budget`, §7.4):** +`max_turns`, +`max_tokens`, +`timeout_ms`, +`gate_max_retries` (D4). Supervisor-owned.
- **Env policy (§7.2):** worker env becomes a constructed least-privilege dict, not `{**os.environ}` (D7). This is a breaking behavioral change vs the current spec and must land with the D7 ripple.
- **Diffundo (§8, §9):** cache-free (D1); `CambiumLM` and `Diffundo.call` signatures change. The `provider_cache_control` config replaces the cache config.

## 2. UNVERIFIED items

- `docs/research/worker-coldstart.md` is cited from branch `wt-coldstart`; it is **not in `main`** as of this writing (verified against the current `main` listing). Its numbers were not re-measured here.
- `docs/research/sandbox-options.md` and `docs/research/threat-model.md` were on unmerged branches when this document was first drafted; both are now **merged in `main`** (verified). Their claims were verified on this host in their own research; not re-run here.
- `docs/research/event-schema-draft.md` and `docs/research/ipc-protocol-draft.md` are cited from `main` (read there; not present in this worktree).
- The external critique's full text is not yet committed to any branch (re-review in flight); D6's disposition table is written against the critique items as described in the orchestrator directive.
- Provider-caching URLs (D1) were fetched 2026-08-09; pricing and model-specific details (OpenAI GPT-5.6 family, Anthropic per-model minimums) may drift after that date. The structural claims (content-addressed, exact-prefix, never-caches-responses) are stable across the cited docs.
- The Factorio reward-hacking incident (D5) is directive-provided context, not a research-corpus file.

## 3. Changelog

| Version | Date | Change |
|---|---|---|
| 1.0.0 | 2026-08-09 | Initial design-delta record: D1..D7 (user directives, Prime Agent good parts, critique disposition + residue). |
| 1.0.1 | 2026-08-09 | Review pass (CONDITIONAL-PASS must-fixes): correct §18.2 vs §18.1 citations (D1); correct prime-agent section numbers and compaction-summary tuple (D2, D5); `parent_task_id` payload-first per the events draft (D2); `.cambium/` canonical path (D2); admission = supervisor-internal ack, not a pre-`ready` wire message (D3); carry UNVERIFIED qualifier for the opencode ACK-loop claim (D3); acknowledge the `Session`/node-session terminology collision (D2/D3, Q3.5); complete the D7 ripple list (§8, §18.3 IMPL-M6/IMPL-C7, §19 items 12 + honest gaps, `--setenv` norm retention); update provenance for research docs now merged in `main`. |
