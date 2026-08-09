# Cambium v2.1 — Context-Compaction Protocol (evidence-backed, never silent)

**STATUS: DRAFT — docs only, not normative.** Design for the v2.1 context-compaction
protocol for Cambium workers (Opifex). This document proposes the wire message, the store
semantics, the evidence/verification rules, and the acceptance gates. It is **not**
authoritative for behavior; the authoritative specification remains
`docs/architecture/architecture.md`, which this draft must be folded into before
implementation.

**Date:** 2026-08-09
**Author:** research task `wt-doc-compaction`
**Worktree:** `/tmp/opencode/cambium-doc-compaction` (branch `wt-doc-compaction`,
base `main@6109a6a`)
**Design driver:** the Prime Agent `/refine` + `compact.run()` lesson and the Cambium
constitution require that **lossy compaction is explicit, evidence-backed, and never
silent**. OpenCode's hidden lossy LLM compaction is the anti-pattern this design exists
to avoid (`docs/research/opencode.md` §3.5, §4.8).

---

## 0. TL;DR

1. Compaction happens **in the worker's own context**, never the parent's. The summary is
   a **new node in the conversation store** (append-only, branchable); history is never
   deleted.
2. Three triggers: the worker's per-model **token threshold**, an explicit **steer
   request** from the supervisor, and the supervisor's **checkpoint cadence**.
3. One new wire message (`compact`, supervisor→worker, added to the IPC catalogue) plus an
   extended `checkpoint` event carrying a `compact_summary` envelope. `checkpoint` is
   already a critical (fsync-d) event, so the summary rides the durable path
   (`architecture.md` §6.5).
4. Summaries are **evidence-backed**: every claim references a message-id range in the
   store (machine-checkable), and a deterministic **canary** requires every open question
   and every TODO file path from the covered range to survive into the summary. A failing
   canary rejects the compaction and retries with more budget.
5. Anti-patterns: silent compaction, compaction that deletes history, compaction in the
   parent's context, compaction without a preceding checkpoint.
6. The prime-agent "spawned GC agent" is **not adopted** — compaction runs in the worker's
   own process between turns (verified simpler; see §6).
7. Falsifiable acceptance: token-reduction threshold met, canary pass rate, and no module
   metric regression.

---

## 1. Where compaction happens

**Decision:** compaction operates on the **worker's (Opifex) own context**, and its output
lives **per node in the conversation store** (`architecture.md` §6.6, D8g). It never
happens in the parent's context and never feeds the parent's LLM directly.

Evidence for the boundary:

- **I2.4 context composition** — "A node's context = its own session log (bounded) +
  parent summary + subtree result envelopes. A node never reads a sibling's raw session"
  (`architecture.md` §3.7 I2.4). The worker's context is the node's own bounded log;
  compaction is the mechanism that keeps that log bounded without truncating it.
- **I2.7 / D8b information hiding** — "a child node NEVER sends its scratchpad,
  chain-of-thought, reasoning trace, or trajectory upward"; the child→parent envelope
  carries exactly `parent_task_id`, `unified_diff`, `summary` (≤2k chars), `metric_*`,
  `commits`, `files_changed`, terminal `status` (`architecture.md` §3.7 I2.7;
  `docs/research/feedback-2-deltas.md` D8b). A compaction summary is richer than a result
  envelope (it covers the whole bounded span, not just the terminal diff), so it **cannot**
  ride the upward envelope. It is a node-local durable artifact.
- **The compaction target is the node's own store.** "the conversation store answers what
  did *this node* see and decide... per-node conversation/session history in SQLite WAL at
  `${session_dir}/.cambium/sessions/conversations.db`... Queryable, e.g. `last_turns(node_id,
  n)`, `cost_by_node`, `context_for(node_id)` returning the bounded D2 I2.4 context"
  (`architecture.md` §6.6). The summary node is written there under the node's `node_id`.
- **One shared store** — `conversations.db` with `node_id` on every row and indexes for
  `(node_id, turn_seq)` and `(node_id, kind, turn_seq)` (`docs/research/v2-1-review.md` §C,
  decision C). The summary node is one more row kind; it does not get a separate database.
- **Opifex already owns the node session state** — module M5 (Opifex) state: "Per-node:
  trajectory, turn counter, generation token, session log" (`architecture.md` §4 M5).
  Compaction is an Opifex-side concern over that state.

**Current-state gap (UNVERIFIED / not present).** Two dependencies do not exist in the
merged tree at this design's base:

- `src/cambium/conversations.py` does **not exist** in `src/cambium/` (verified by
  directory listing, 2026-08-09) — "There is no conversation store. The architecture
  decisively specifies one shared `conversations.db`... but no `ConversationStore` exists"
  (`v2-1-review.md` §1.3 gap 11; milestone M5 scope, `v2-1-review.md` §3 M5). The
  compaction protocol presupposes the D8g store (M5).
- The merged `src/cambium/worker.py` (Opifex seed) is a single-shot task runner: it handles
  `init`/`run_task`/`check_health`/`steer`/`cancel`/`shutdown`, emits `result_envelope` +
  `exit_message`, and then exits; it has **no `context` message handling, no checkpoint
  emission, and no conversation-store binding** (`src/cambium/worker.py:356-494`, module
  docstring "One worker executes one task and then exits"). Compaction targets the
  M6/M7-era DSPy ReAct worker with per-tool checkpoints (`v2-1-review.md` M5/M6/M7), not
  today's fixture worker.

---

## 2. Triggers

Three triggers, two initiators.

### 2.1 Token threshold (worker-initiated, per-model context window, config)

- The worker estimates its **model-visible context** from provider usage telemetry returned
  by `Diffundo`/`CambiumLM` call metadata (`architecture.md` §9.3 — every DSPy call flows
  through `CambiumLM(diffundo, tier=...)`; usage is recorded, not cached). The model's
  context window is config data the worker already receives via `init.fanout_config`
  (provider capabilities, incl. `min_context_window` in the cascade selection filter,
  `architecture.md` §9.2 step 2).
- **Threshold formula (borrowed from prime-agent, verified):** auto-compact when
  `contextTokens > contextWindow − reserveTokens`, where `reserveTokens` reserves room for
  the model's response (prime-agent 0.7.1: `docs/compaction.md` "When It Triggers";
  defaults `reserveTokens 16384`, `keepRecentTokens 20000` in `settings.json`). Cambium
  carries both as config under `worker.compaction` (`reserve_tokens`, `keep_recent_tokens`,
  `threshold` as a fraction of the window).
- **Supervisor owns the budget.** `max_tokens` is "carried in `init.budget` and enforced by
  `Custos` — never self-reported by the worker" (`architecture.md` §7.4, D4). The worker's
  threshold detection is therefore **advisory**: it requests compaction; `Custos` still
  hard-enforces `budget.max_tokens`. Compaction reduces context; it is not the budget
  enforcer.
- **Observability.** "omp logs every compaction decision (threshold, used, freed)" is a
  lesson to adopt (`docs/research/omp.md` §4 lesson 5). Every Cambium compaction decision —
  threshold observed, tokens used, tokens freed — is a `checkpoint` event with
  `compact_summary` (§3), i.e. durable and auditable, never a silent background action.

### 2.2 Explicit steer request (supervisor-initiated)

- The parent may direct a live NodeSession with repeatable `steer` turns routed by `Custos`
  (`architecture.md` §5.2 `steer`, D3). Compaction-on-steer is the same channel with a
  focus hint: the supervisor sends `compact` with `instructions` (e.g. "keep the failing
  test names and the migration checklist"), mirroring prime-agent's `/compact [instructions]`
  and `compact.run(instructions)` (prime-agent 0.7.1: `docs/compaction.md` "You can also
  trigger manually with /compact [instructions]"; `skills/compact/src/compact/__init__.py`
  `run(instructions=None)`). Instructions are persisted on the summary node and shown in
  the store.

### 2.3 Supervisor checkpoint cadence (supervisor-initiated, defensive)

- Workers emit `checkpoint` after every tool call that produces or modifies durable state
  (`architecture.md` §6.4); `checkpoint` is a **critical** event, fsync-d before ack
  (`architecture.md` §6.5). The supervisor may request compaction defensively:
  - every `compaction.checkpoint_interval` checkpoints since the last compaction, and/or
  - before re-injecting a long session into a fresh worker (session resume D3,
    `architecture.md` §6.4 "checkpoint semantics extend to session resume"; pool-reset M7,
    `v2-1-review.md` §3 M7), so the resumed worker starts from a compact summary rather
    than a full replay.
- Rationale: the checkpoint stream is the supervisor's ground truth for "how far has this
  node run"; it is also the durability point the compaction rides on (§3, §5), so tying the
  trigger to the cadence makes "compaction without a checkpoint first" structurally
  impossible.

---

## 3. Protocol

### 3.1 New wire message: `compact` (supervisor → worker)

**Addition to the IPC catalogue.** The draft catalogue defines request messages `init`,
`context`, `run_task`, `check_health`, `cancel`, `shutdown`
(`docs/research/ipc-protocol-draft.md` §2.2). This design **adds `compact`** to that
request class. Per the versioning rule, adding a new request is an additive change,
backward-compatible within a `proto` (`ipc-protocol-draft.md` §5: "Additive changes (new
optional field, new event type) are backward-compatible within a `proto`"); the catalogue
count in §2.2 ("6 orchestrator→worker request types") becomes 7 and must be updated on
adoption.

```jsonc
// Supervisor → Worker (request class, §2.1: carries request_id, expects a response)
{"type":"compact",
 "request_id":"01J…",                    // ULID; echoed in the ok response
 "reason":"token_threshold"|"steer"|"checkpoint_cadence"|"supervisor",
 "instructions":"optional focus hint",   // steer-style, like /compact <instructions>
 "max_summary_tokens":2000,              // budget for the summarization LLM call
 "reserve_tokens":16384,                 // response headroom (see §2.1)
 "keep_recent_tokens":20000}             // newest tokens NOT summarized
```

- Response: `ok` echoing `request_id` (response envelope, `ipc-protocol-draft.md` §2.1).
  The supervisor must not send a second `compact` while one is pending (`PROTO_OUT_OF_ORDER`
  guard, `ipc-protocol-draft.md` §4.1).
- Error path: `error` with `error_type:"compaction_canary_failed"`, `recoverable:true`
  (worker error taxonomy, `ipc-protocol-draft.md` §4.2) — the summary was rejected by the
  canary and retried; see §4.
- **No checkpoint request:** the worker does not need permission to compact. `compact` is a
  request, not a mandatory protocol step; the worker may also self-trigger on §2.1 and
  report via the same `checkpoint` envelope. The `request_id` correlates the `ok`; the
  resulting `checkpoint`/`compact_summary` is the durable record (matching the `run_task` →
  `result_envelope` event-not-response pattern, `ipc-protocol-draft.md` §2.1 "run_task
  completes with a result_envelope event, not a response").

### 3.2 When it runs: safe boundary between turns

- Compaction runs at a **safe provider-turn boundary** — never mid-tool, never mid-LLM
  call. Prime-agent enforces the same rule: "Compaction never runs mid-cell: it runs when
  the current turn ends" (`skills/compact/src/compact/__init__.py`); opencode admits
  context changes "only at a Safe Provider-Turn Boundary" (`docs/research/opencode.md` §1,
  quoting its `CONTEXT.md`). The worker pauses its ReAct loop, performs the summary, emits
  the `checkpoint` + `compact_summary`, and resumes with the compacted context.
- **Static prefix stays byte-stable (D8c).** Compaction summarizes the **dynamic bottom** of
  the prompt (task spec, observations, tool results); the static top (system prompt,
  AGENTS.md-derived guidelines, tool definitions, module instructions, few-shot context)
  is untouched, preserving the exact-prefix provider-cache scheme
  (`architecture.md` §9.3 D8c "static, byte-stable content at the TOP"). This is the
  compatible half of opencode's context-epoch idea (stable prefix, `opencode.md` §1, §4.5);
  what we reject is opencode's hidden/lossy summary (§5).

### 3.3 The result: `checkpoint` event with a `compact_summary` envelope

The compaction outcome is delivered on the **existing** `checkpoint` event (already in the
catalogue: "`checkpoint` | Durable resume point | `turn`, `state_ref` (atomic write),
`commits_so_far`", `ipc-protocol-draft.md` §2.4; `architecture.md` §5.2, §6.4). Adding a
payload field to an existing event is additive (`ipc-protocol-draft.md` §5). A
`checkpoint` **without** `compact_summary` is the ordinary (non-compaction) checkpoint.

```jsonc
{"type":"checkpoint",
 "task_id":"wt-abc-001",
 "generation":3,
 "turn":12,
 "state_ref":"…/checkpoints/wt-abc-001/turn-012.json",   // written BEFORE emit (§5)
 "commits_so_far":["a1b2c3d"],
 "compact_summary":{
   "summary":"## Goal …\n## Constraints …\n## Progress (Done/In Progress/Blocked)\n## Key Decisions …\n## Next Steps …",
   "covers":{"from_msg_id":"wt-abc-001#42","to_msg_id":"wt-abc-001#77"},
   "claims":[{"claim":"kalman_fusion ported from main","refs":["wt-abc-001#44-49"]},
             {"claim":"cascade fallback verified","refs":["wt-abc-001#51"]}],
   "open_questions":["does the fusion gate run under cargo test?"],
   "todo_paths":["src/fusion.rs","tests/fusion_gate.rs"],
   "tokens_before":41200,"tokens_after":1100,
   "canary":{"pass":true,"checked":{"questions":1,"paths":2},"missing":[]}}}
```

- **Structured, not raw conversation.** The envelope carries `summary` + explicit
  `open_questions` + `todo_paths` + claim references — decisions made, files touched, open
  questions — never the raw transcript, matching the prime-agent compaction carry-forward
  ("goal/constraints/progress/blocked/decisions", `docs/research/prime-agent.md` §2.5,
  §4.6) and the I2.7 envelope rule (`architecture.md` §3.7).
- **Summary format** follows the prime-agent verified template: `## Goal`, `## Constraints
  & Preferences`, `## Progress` (`Done`/`In Progress`/`Blocked`), `## Key Decisions`,
  `## Next Steps` (prime-agent 0.7.1: `docs/compaction.md` "Summary Format").
- **`tokens_before`/`tokens_after`** mirror prime-agent's `tokensBefore` recorded on its
  `CompactionEntry` (prime-agent 0.7.1: `docs/session-format.md` — `CompactionEntry` with
  `tokensBefore`). They are the store-derived inputs to the acceptance gate (§7).

### 3.4 Store semantics: compaction ADDS, never deletes

- **Append-only, branchable summary node.** The store stays append-only
  (`architecture.md` §6.6; D2 "Each node owns its conversation/session log... append-only",
  `docs/research/design-deltas.md` D2). Compaction writes a new row (kind `compact_summary`)
  whose `parent_id` points at the last covered message (`covers.to_msg_id`) and whose
  `covers.from_msg_id` points at the previous summary node (or session start). New turns
  attach **after** the summary node. The full covered history remains queryable under the
  same `node_id`.
- **Direct precedent (verified):** prime-agent stores compaction as an entry in the
  append-only session tree with `id`/`parentId`/`firstKeptEntryId`/`tokensBefore`
  (`docs/session-format.md` `CompactionEntry`); "/tree — Navigate the session tree in-place.
  Select any previous point, continue from there, and switch between branches. All history
  preserved in a single file" (prime-agent 0.7.1 `README.md` §/tree); "Compaction is lossy.
  The full history remains in the JSONL file; use `/tree` to revisit" (`README.md`
  §Compaction). Cambium's equivalent of `/tree` is store querying: `context_for(node_id)`,
  `last_turns(node_id, n)` over the full range
  (`architecture.md` §6.6; `v2-1-review.md` §C).
- **What the LLM sees vs what the store keeps.** After compaction the worker's *model
  context* is `summary + messages from covers.to_msg_id onwards` (prime-agent's
  reload semantics: summary + kept messages, `docs/compaction.md` "What the LLM sees"). The
  *store* keeps everything. The distinction between model-visible context and durable
  history is the whole point of §5's "never deletes" rule.
- **Downward/upward direction.** The `compact_summary` is a node-local record written by
  the worker and persisted by `Custos` into `conversations.db` and the event log. It is
  **never** forwarded to the parent LLM (§1, I2.7). It exists so the node can resume
  (D3 session resume, `architecture.md` §6.4) and so the store can answer "what did this
  node decide" without replaying the raw span.

---

## 4. Evidence-backed summaries and the compaction-quality canary

A model-produced summary is not trustworthy by default. Two layers make it verifiable.

### 4.1 Machine-checkable claim references

Every claim in `compact_summary.claims[]` carries `refs`: message-id ranges (`[from,to]`
store rows) that the claim is drawn from. The verifier (deterministic, in `Custos`/
`ConversationStore`, never an LLM) checks each `refs` range exists and lies inside
`covers`, via the store's indexed `(node_id, turn_seq)` queries (`v2-1-review.md` §C).
A claim whose refs do not resolve is a **malformed summary** → rejected. This makes
"each claim references a message id range in the store (machine-checkable)" concrete:
the store is the authority for both the covered range and the claim references.

### 4.2 Compaction-quality canary (extractable, deterministic)

The canary is a **coverage** assertion, computed entirely with regex over the covered
range's stored payloads:

- **Open questions:** extract candidate open-question sentences from the covered rows with
  a regex over sentence text, e.g.
  `(?i)\b(open question|blocked on|blocked by|unresolved|uncertain|to be decided|tbd|need(?:s|ed)? to (?:decide|verify|check|confirm)|unknown if|does not (?:work|compile|pass))\b`,
  plus any sentence ending in `?` within a tool/steer/result payload.
- **TODO file paths:** extract paths adjacent to TODO markers with
  `(?i)\b(todo|fixme|xxx|hack)\b[^\n]*?((?:[\w.-]+/)*[\w.-]+\.(?:py|rs|ts|js|go|toml|json|yaml|md|sh|sql)\b|/(?:[\w.-]+/)+[\w.-]+)`,
  normalized (lowercase, no trailing punctuation).
- **Assertion:** every extracted question (normalized) must appear in
  `compact_summary.open_questions` or in `summary`; every extracted path must appear in
  `compact_summary.todo_paths` or in `summary` (substring match on the normalized path).
  This is set equality, fully deterministic, runnable as a pure function over the store
  rows + the envelope — no model in the loop.
- **Failure:** `canary.pass == false` → the compaction is **rejected**: the summary node
  is not committed as the active boundary (the worker keeps its current context), and the
  compaction is **retried with more budget** (`max_summary_tokens` increased per retry,
  bounded by `compaction.max_retries` and `compaction.max_summary_tokens`). The rejection
  is itself a durable `error`/`checkpoint` record — a rejected compaction is never silent.

This mirrors the architecture's canary discipline at the module/metric level: the `canaries`
signal is a gate that zeroes the whole metric on failure (`architecture.md` §10) and the
D5 refinement loop rejects any refinement that fails canaries
(`docs/research/design-deltas.md` D5; `docs/research/test-strategy.md` §8). Compaction is a
model-produced artifact, so it gets the same brake.

---

## 5. Anti-patterns (what this design forbids)

| # | Anti-pattern | Why it is wrong | Cambium rule |
|---|---|---|---|
| 1 | **Silent compaction** | "Compaction is a lossy LLM pass. When context is full, a hidden `compaction` agent summarizes the session (auto-compaction, optional pruning)... the docs expose only coarse knobs... There is no replay-based durable checkpoint" (`docs/research/opencode.md` §3.5). A model cannot reason well about history it does not know was summarized. | Every compaction is a `checkpoint` event with `compact_summary` (durable, critical tier, §6.5), written to the store; the worker's own context is rebuilt from the summary it can read. Never a hidden pass. |
| 2 | **Compaction that deletes history** | Prime-agent keeps full history and exposes it via `/tree` ("All history preserved in a single file"; "The full history remains in the JSONL file", prime-agent 0.7.1 `README.md`). Deleting history makes loss unrecoverable and audit impossible. | Store is append-only; compaction adds a summary node with `parent_id` (§3.4). Nothing is ever deleted or pruned by compaction. |
| 3 | **Compaction in the parent's context** | I2.7/D8b: the parent never sees the child's scratchpad/CoT/trajectory; the upward envelope is the `Result` envelope only (`architecture.md` §3.7 I2.7; `feedback-2-deltas.md` D8b). A compacted child history injected into the parent would both pollute the parent's bounded context (I2.4) and leak the trajectory. | `compact_summary` is a node-local record. The parent receives only result envelopes; steering is the only downward channel (`architecture.md` §5.2 `steer`). |
| 4 | **Compaction without a checkpoint first** | The checkpoint is the durable resume point; session resume reloads `state_ref` + node session log (`architecture.md` §6.4; D3). Compaction without a durable checkpoint would let a crash after summarization lose the boundary between pre-summary history and post-summary state. | Structurally impossible: the compaction result **is** a `checkpoint` event, and `state_ref` is written before emit (§3.3). |
| 5 | **Compaction as the durability mechanism** | "Cambium's checkpoint-per-tool-call ReAct recovery is strictly better for crash recovery; use compaction only as a last-resort context reducer, not as the durability mechanism" (`docs/research/opencode.md` §4.8). | Compaction is a context reducer only. Durability stays in checkpoints + the event log; the store rebuilds from durable events if the projection is deleted (`v2-1-review.md` §C). |
| 6 | **Compaction at an unsafe boundary** | A summary taken mid-tool-call or mid-LLM-response tears the trajectory and the model's self-knowledge (prime-agent: "never mid-cell"; opencode: safe-provider-turn-boundary only — §3.2). | Compaction runs only at a safe boundary between turns (§3.2). |

---

## 6. The GC-agent pattern: evaluation and verdict

**The claimed pattern.** The task directive describes prime-agent as doing "asynchronous
compaction with a spawned GC agent" so the worker continues while compaction runs.

**What is actually verified (prime-agent 0.7.1, local install):**

- `compact.run()` is a kernel-side skill that schedules **host-side** compaction via
  `rlm.host_request("compact.run", payload)` (`skills/compact/src/compact/__init__.py`);
  the summary is generated by the session worker itself with an LLM call, **not** by a
  spawned child agent/process.
- Compaction is queued/asynchronous relative to the model turn — "Compaction never runs
  mid-cell: it runs when the current turn ends and before the next model response";
  `status()` returns `scheduled` for a pending compaction — but it executes in the session
  worker, and the session **reloads** after it (`docs/compaction.md` "How It Works" step 5
  "Reload: Session reloads"; `skills/compact/src/compact/__init__.py`).
- The "spawned GC agent" framing is **UNVERIFIED** against the local install; no doc or
  code path in the 0.7.1 tree performs compaction in a separate spawned agent.

**Evaluation for Cambium.** Cambium's worker is a subprocess over one stdin/stdout pipe
pair (`architecture.md` §2 worker layer, §5.1); a "GC agent" would be a second process
holding the worker's history. Rejected for three reasons:

1. **Info hiding (decisive).** A spawned compactor would need the worker's raw history to
   summarize it — which is exactly the scratchpad that must never leave the node (I2.7,
   D8b). In-process compaction needs no copy and no second context.
2. **Simplicity.** "compaction happens in the worker's own process between turns" is the
   directly verified prime-agent model (host-side, turn-end), with zero new wire messages
   (the result rides the existing `checkpoint`), zero new lifecycle states, and zero pool
   admission/reset surface (M7 worker-pool reset already requires "empty conversation
   binding", `v2-1-review.md` §C D).
3. **Prime-agent's own incident evidence.** Its OOM failures came from one process holding
   many LLM contexts ("per-runtime LLM context, not fixed overhead" is "the memory
   driver"; `docs/research/prime-agent.md` §3.1). Spawning an additional summarizer context
   per compaction worsens exactly that failure class.

**Tradeoff (stated honestly).** In-process compaction **blocks** the worker for the
duration of the summarization call (bounded by `max_summary_tokens` and the per-task wall
budget `budget.max_wall_s`, supervisor-owned, `architecture.md` §7.4). A spawned compactor
would keep the worker turning, but at the cost of duplicating the context, breaching
I2.7, and adding a process/lifecycle class Cambium does not need. The liveness win does
not repay the information-hiding loss. **Verdict: adopt the in-process, between-turn
pattern; adopt the "async" scheduling only as safe-boundary queuing (§3.2), not as process
spawning.**

---

## 7. Falsifiable acceptance

Adoption requires measurable evidence, in the style of the v2.1 review's falsifiable gates
(M5, M9) and the D5 canary gate.

1. **Token reduction per compaction ≥ threshold (measured from the store).** For each
   accepted compaction, `tokens_before`/`tokens_after` (recorded on the envelope, §3.3)
   are derived from the store's covered range and the summary node. Acceptance:
   mean reduction `(tokens_before − tokens_after)/tokens_before` over a frozen scenario
   corpus ≥ config threshold (default proposal: ≥ 60%) with every sample above the
   configured floor. A compaction that passes the canary but not the reduction threshold
   invalidates the threshold config, not the corpus.
   - Prerequisite flagged: rows must carry token estimates (stored usage metadata or a
     deterministic estimator). The current D8g content list ("init/steer/tool_event/
     checkpoint/result message payloads", `architecture.md` §6.6) does not specify a token
     column — **UNVERIFIED / open store question** (§8 Q8).
2. **Summary-canary pass rate.** The §4.2 canary is pure and deterministic; acceptance:
   **100%** pass over the frozen corpus after budget-retry exhaustion, and zero accepted
   compactions with `canary.pass == false`. A retry that exhausts `compaction.max_retries`
   fails the compaction (durable `error` `compaction_canary_failed`), never silently
   downgrades the summary.
3. **No regression on the module metric.** On the module's frozen held-out eval
   (`architecture.md` §17.2; e.g. the multi-signal §10 metric for Opifex, or
   `should_decompose_metric` in `src/cambium/modules/example/metric.py`), run paired trials
   with compaction active at threshold vs inactive: metric delta must stay within a
   pre-registered bound (default: no more than −1 point) and canary pass must remain at
   baseline. Mirrors M9's "adopt only if ... degradation exceeds the bound → falsified"
   posture (`v2-1-review.md` §3 M9).
4. **Evidence discipline.** Every acceptance run records scenario count, commit SHA, pinned
   model, and the threshold/retry config (`v2-1-review.md` M1 acceptance 3: "scenario count
   and commit SHA are recorded").

---

## 8. Open questions for the orchestrator

| # | Question | Options / default proposal | Owner |
|---|---|---|---|
| Q1 | Token accounting authority | Worker self-estimates for the trigger (§2.1) vs supervisor metering of `budget.max_tokens`. Arch §7.4 says budgets are supervisor-owned; the *trigger* is a context-window property, the *enforcement* is the budget. Default: worker triggers, supervisor enforces and can veto. | Custos + Opifex authors |
| Q2 | Where `compact_summary` is durably recorded | Event log payload (redacted) only, conversation store only, or both. Double-write risk was flagged in Q8g.3 (`feedback-2-deltas.md` D8g). Default: event log (cross-cutting audit, `architecture.md` §6.5 critical tier) + store row (node-local). | event-schema + D8g owners |
| Q3 | Threshold across a cascade | `init.fanout_config` may name several models of a tier with different context windows (`architecture.md` §9.2); which window does the threshold use? Default: the smallest eligible window, so compaction precedes any truncation. | Diffundo owner |
| Q4 | Canary retry exhaustion | Fail open (continue uncompacted, supervisor budget still bounds) vs fail closed (task FAILED). Compaction never deletes, so failing open is safe; it only loses token relief. Default: fail open with durable `compaction_canary_failed` error. | Custos owner |
| Q5 | Does the summarization call count as an LLM turn | `max_turns` is supervisor-owned (§7.4). A compact under steer/token/cadence consumes a summarization call. Default: it does **not** count as a ReAct turn, but it does consume `budget.max_tokens`/wall budget and is metered in `cost_by_node`. | Custos + bench owner |
| Q6 | Composition with M9 tree-sitter compression | M9 compresses *static* context (AST/symbol chunks); compaction summarizes *dynamic* history. Both are "context adapters, never a supervisor concern" (`v2-1-review.md` M9). Do they compose in one worker? Default: yes, disjoint regions (static vs dynamic, D8c). | M9 research owner |
| Q7 | Parent summary refresh | May a child `compact_summary` ever refresh the parent's I2.4 "parent summary"? Default: **no** — that is anti-pattern 3; the parent's summary comes only from the ≤2k-char result envelope. | Architectus owner |
| Q8 | Token column in the store | D8g does not define token accounting per row; §7 gate 1 needs it. Default: store usage metadata on `tool_event`/`checkpoint` rows (redacted), or a deterministic estimator at query time. | D8g / ConversationStore owner |

---

## 9. Verification table

Every claim above cites a source. This table is the audit trail; **UNVERIFIED** items are
flagged inline and repeated here.

| Claim | Source | Status |
|---|---|---|
| I2.4 node context = own bounded log + parent summary + subtree envelopes; no sibling raw reads | `architecture.md` §3.7 I2.4 | Verified (read §3.7) |
| I2.7/D8b child never sends scratchpad/CoT/trajectory upward; envelope is fixed | `architecture.md` §3.7 I2.7; `feedback-2-deltas.md` D8b | Verified |
| Conversation store content and `context_for(node_id)` | `architecture.md` §6.6; `feedback-2-deltas.md` D8g | Verified |
| One `conversations.db`, `node_id` rows, `(node_id, turn_seq)` indexes | `v2-1-review.md` §C | Verified |
| Opifex owns per-node trajectory/turn/generation/session log | `architecture.md` §4 M5 | Verified |
| No `ConversationStore` in merged tree | `v2-1-review.md` §1.3 gap 11, M5; `src/cambium/` directory listing 2026-08-09 | Verified (present = absence) |
| Worker.py is single-shot, no `context` handling, no checkpoint emission | `src/cambium/worker.py:356-494` + module docstring | Verified (read file) |
| `max_tokens`/budgets supervisor-owned, never worker self-report | `architecture.md` §7.4 (D4) | Verified |
| Provider context-window data available to the worker via `fanout_config` | `architecture.md` §9.2 (capability/context filter, `min_context_window`) | Verified |
| IPC catalogue: 6 request types, request/response/event classes, correlation rule | `ipc-protocol-draft.md` §2.1–2.2 | Verified |
| `checkpoint` event fields and critical durability | `ipc-protocol-draft.md` §2.4; `architecture.md` §6.4–6.5 | Verified |
| Proto versioning: additive changes backward-compatible | `ipc-protocol-draft.md` §5 | Verified |
| `error` taxonomy incl. recoverable, `PROTO_OUT_OF_ORDER` | `ipc-protocol-draft.md` §4.1–4.2 | Verified |
| OpenCode compaction = hidden lossy LLM pass; no durable replay | `docs/research/opencode.md` §3.5, §4.8 | Verified (research doc + upstream docs URL cited therein) |
| OpenCode context-epoch / safe-provider-turn-boundary design | `docs/research/opencode.md` §1 | Verified (research doc quotes `CONTEXT.md`) |
| Prime-agent compaction carry-forward fields | `docs/research/prime-agent.md` §2.5, §4.6 | Verified |
| Prime-agent OOM: context is the memory driver; one process holds children | `docs/research/prime-agent.md` §3.1 | Verified (local logs cited therein) |
| `compact.run()` schedules host-side compaction via `host_request`; never mid-cell | prime-agent 0.7.1 install: `skills/compact/src/compact/__init__.py` | Verified (read file this task) |
| Auto-compaction formula `contextTokens > contextWindow − reserveTokens`; `reserveTokens`/`keepRecentTokens` defaults; summary format; reload after compaction | prime-agent 0.7.1 install: `docs/compaction.md` | Verified (read file this task) |
| `CompactionEntry` {id, parentId, summary, firstKeptEntryId, tokensBefore} | prime-agent 0.7.1 install: `docs/session-format.md` | Verified (read file this task) |
| `/tree` in-place tree navigation; "All history preserved in a single file"; "Compaction is lossy. The full history remains in the JSONL file" | prime-agent 0.7.1 install: `README.md` §/tree, §Compaction | Verified (read file this task) |
| "Spawned GC agent" performs compaction in a separate process | **No source** in prime-agent 0.7.1 install or research corpus | **UNVERIFIED — asserted in the task directive only; contradicted by the verified host-side turn-end model** |
| Store token accounting per row | `architecture.md` §6.6 content list; D8g | **UNVERIFIED — D8g specifies no token column; open question Q8** |
| Canary discipline as metric gate | `architecture.md` §10 (`canaries` gate); `design-deltas.md` D5; `test-strategy.md` §8 | Verified |
| Module held-out eval / metric (Opifex §10; `should_decompose_metric`) | `architecture.md` §17.2; `src/cambium/modules/example/decide.py`, `metric.py` | Verified |

---

## 10. Files to change on adoption (not this task)

- `docs/research/ipc-protocol-draft.md` §2.2 — add `compact` to the request catalogue and
  bump the message count; §7 reconciliation row for the addition.
- `docs/architecture/architecture.md` §5.2 — `compact` message schema; §6.6 — store row
  kind `compact_summary`; §6.4 — note the compacted resume path.
- `src/cambium/ipc.py` / worker wire loop — `compact` handling; `src/cambium/
  conversations.py` (new, M5) — summary node + canary verifier; Custos — trigger cadence,
  metering, acceptance logging.
