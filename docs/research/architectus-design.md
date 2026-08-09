# Architectus — RLM Task-Tree Orchestrator (v2.1 M5)

**Date:** 2026-08-09
**Status:** **DRAFT** — design spec for review; nothing here is merged code.
**Owner:** orchestrator owner (`Architectus`, arch §4 M6)
**Branch:** `wt-doc-architectus`
**Milestone:** v2.1 M5 (L) — "Architectus RLM/task-tree execution and conversations"
(`docs/research/v2-1-review.md` §3, M5, lines 383-399).

## 0. Scope, vocabulary, and provenance

This document designs **Architectus**: the RLM (recursive LLM) task-tree orchestrator of
the v2.1 roadmap. It executes the task DAG, composes per-node contexts, steers workers,
aggregates results, and makes replan decisions. It is the module that makes the six
decisions of `docs/research/v2-1-review.md` §2 executable (thin Custos, Architectus owns
the DAG workflow).

Terms used here are the codebase's terms:

- **NodeSession** — a task-tree node's sub-session, `session_id == task_id`, checkpointed
  and reloadable (`docs/architecture/architecture.md` §3.7, line 248; `design-deltas.md`
  D3 Q3.5, line 157).
- **Upward envelope** — the child→parent result message carrying exactly the I2.7 key set
  (arch §3.7 I2.7, line 246; `src/cambium/tasktree.py:50-60,453-478`).
- **Steer** — parent→child direction to a live NodeSession (arch §5.2, lines 314-316, 383).
- **Wave** — one scheduling tick in the ready-tasks loop (§2).
- **Gate** — the deterministic verification command that defines "work is done" (D4;
  arch §7.1 GATING, lines 564-572).

### 0.1 Verification convention

Every citation below was read in the current worktree (`/home/ubuntu/cambium` @
`6109a6a`). Anything that could **not** be verified against the merged `main` tree is
flagged **UNVERIFIED** with the branch it lives on. This matches the convention of
`docs/research/design-deltas.md` §0.1 (lines 14-20) and `v2-1-review.md` §0 (lines
26-33).

### 0.2 Current code surface (verified)

| Surface | State | Source |
|---|---|---|
| `src/cambium/tasktree.py` | Merged, pure, tested (29 scenarios) | `v2-1-review.md` lines 66-71 |
| `src/cambium/supervisor.py` | The vertical-slice supervisor, **not Custos** (docstring, lines 1-25) | `supervisor.py:1-25` |
| `src/cambium/orchestrator.py` | Skeleton: submit/drain, no orchestration logic (lines 1-59) | `orchestrator.py:1-59`; `v2-1-review.md` lines 192-196 |
| `src/cambium/worker.py` | Opifex seed; `steer` handled as a v2.1 hook (lines 460-469) | `worker.py:460-469` |
| `src/cambium/modules/base.py` | `Module` ABC (`decide()` + `metric()`), lines 46-57 | `modules/base.py:46-57` |
| `src/cambium/modules/example/decide.py` | Reference rule-engine module + DSPy seam | `modules/example/decide.py:146-161` |
| Canonical Custos (`run_plan`) | **UNVERIFIED** on `main`; inspected at `wt-impl-super@9746b96` | `v2-1-review.md` lines 80-87 |
| `src/cambium/conversations.py` | **Does not exist on `main`** (verified by listing `src/cambium/`) | §6.6 + gap 11 below |
| `src/cambium/diffundo.py` | **UNVERIFIED** on `main`; lives on `wt-impl-diffundo@f5ae0d3` | `v2-1-review.md` lines 88-94 |
| `docs/research/compaction-design.md` | **Does not exist** (verified by listing `docs/research/`) | §3.4 below |

---

## 1. Role split: Custos = thin watcher; Architectus = DAG owner

Verdict A of the v2.1 review is the normative baseline and is adopted verbatim:

> **Decision:** Custos owns process lifecycle, IPC transport, hard budgets, generation
> fencing, resource permits, and durable emission. It does **not** own workflow policy.
> **Architectus owns DAG execution:** call decision modules, validate the Task Tree, select
> ready nodes, route work, aggregate child envelopes, request gates/merges, and decide
> retries that change task content.
> (`docs/research/v2-1-review.md` lines 215-222)

This corrects the branch-local `_Runtime`, which mixes watching, gate policy, gate caching,
worktree recovery, merge orchestration, event writing, and result aggregation
(`v2-1-review.md` lines 230-233). Risk 3 of the review — "Custos becomes the workflow
engine" (`v2-1-review.md` lines 480-481) — is the failure this split exists to prevent.

### 1.1 Custos — deterministic primitives (the thin contract)

Custos is the Deterministic Layer's process watcher (arch §2, lines 71-78). Its complete
responsibility is: lifecycle, watchdog, restart policy, worktree recovery, durable event
log, gate/budget enforcement (arch §4 M4, line 261). The layering invariant is
non-negotiable: the Deterministic Layer **never calls an LLM and never imports a DSPy
module** (arch §2, line 91).

Custos exposes **primitives**, not policy. Architectus is the only caller:

| Primitive | Shape | Normative source |
|---|---|---|
| `spawn(node_session)` → admission (synchronous control-plane ack, before RUNNING) | in-process Python API | D3 admission (design-deltas.md lines 140-141; arch §5.2, line 381) |
| `cancel` / process-group SIGTERM→SIGKILL ladder | `graceful_s` 10 s / `term_grace_s` 5 s | ipc-protocol-draft.md §6 (lines 497, 505-506); custos-asyncio-design.md §4 |
| `restart` (bounded, jittered, generation bump) | burst cap 5/60 s, absolute cap 10 | arch §7.4; ipc-protocol-draft.md §4.4 (lines 445-449) |
| `gate(worktree, command)` → content-addressed verdict | via GateRunner (D4) | arch §7.9; v2-1-review.md lines 223-225 |
| `merge` request → `merge_succeeded` / `merge_failed` | via Unio, serialized `asyncio.Lock` | arch §7.8; event-schema-draft.md §3.11-3.13 |
| `emit(kind, payload)` → durable event (critical tier fsync-before-yield) | single-writer thread | arch §6.2/§6.5; custos-asyncio-design.md §2.4 |
| hard budgets: `max_wall_s`, `max_turns`, `max_tokens`, `timeout_ms`, `gate_max_retries` | **supervisor-owned, never self-reported** | D4 (design-deltas.md lines 172-178); arch §5.2 `budget` (lines 309-311), §7.4 (lines 645-649) |

The exact event-loop architecture (loop-affine `WorkerHandle`, single writer thread,
shutdown choreography) is specified in `docs/research/custos-asyncio-design.md` §1-§4 and
is not redesigned here. The canonical Custos implementation lives on
`wt-impl-super@9746b96` (**UNVERIFIED** on `main`; `v2-1-review.md` lines 80-87); M1 makes
one canonical runtime on `main` its acceptance gate.

### 1.2 Architectus — the DAG executor

Architectus is the Orchestration Layer (arch §2, lines 61-67). It **owns**, end to end:

1. **Plan** — ingest the session spec; run `should_decompose` (reference rule engine:
   `src/cambium/modules/example/decide.py:68-143`; DSPy seam per arch §17 and v2-1-review
   decision F, lines 296-310) to decide whether to decompose (the no-decompose fast path
   resolves arch review LLM-C6).
2. **Build** — `TaskDecomposer` produces the plan payload; `tasktree.build_tree` validates
   it (I2.1-I2.3) before any dispatch (arch §3.7, line 234; `tasktree.py:233-347`).
3. **Schedule** — the `ready_tasks` wave loop with bounded concurrency (`max_width`,
   session-wide cap enforced at dispatch per I2.3, `tasktree.py:13-17`).
4. **Compose** — per-node context from the conversation store, per I2.4 (§3).
5. **Steer** — parent direction to live NodeSessions (arch §5.2, lines 314-316, 383).
6. **Aggregate** — child upward envelopes via `upward_result` (I2.7, `tasktree.py:453-478`).
7. **Evaluate** — `ResultEvaluator` verdicts (arch §10, line 884).
8. **Replan** — the orchestrator feedback loop: on typed outcomes (especially
   `merge_failed`), decide resolver/replan/abort (§6).

Architectus never spawns or kills a process, never writes a worktree, never runs a gate
command, and never emits durable events directly. It issues typed **actions** to Custos
and reads typed **events** back. That one-way dependency is the layering invariant
"the Orchestrator depends on the Deterministic Layer; the reverse is false" (arch §2, line
92).

The current `src/cambium/orchestrator.py` (59 lines) is a submit/drain placeholder with
"no orchestration logic" (`orchestrator.py:1-59`); per v2-1-review §5 item 4 it is to be
**replaced** by Architectus, not extended (lines 506-508).

---

## 2. The scheduling loop as a state machine

The scheduler is a **wave-based, dependency-gated, bounded-concurrency loop** over the
frozen `TaskTree`. The tree itself is stateless and immutable after `build_tree`
(`tasktree.py:25-29`); the scheduler tracks progress externally via the `finished` set it
feeds to `ready_tasks` (`tasktree.py:388-403`) — exactly the contract the module
docstring defines.

### 2.1 Node-level lifecycle (per node)

Extends the arch §7.1 per-task machine (lines 544-572) with the tree-level completion
rule I2.5 (line 244) and the D3 admission semantics:

```
PENDING ──► READY ──► SPAWNING ──► RUNNING ──► GATING ──► DONE
   │          │          │            │          │          │
   │          │          │            │          └─► GATE_FAILED ─► RUNNING (retry) ─► DONE
   │          │          │            │          └─► FAILED (retries exhausted)
   │          │          │            └─► FAILED (recoverable:false / budget exceeded)
   │          │          └─► CRASHED ─► SPAWNING (bounded restart, generation bump)
   │          └─► REJECTED (dispatch-time validation, I2.2/I2.3)
   └─► (never dispatched; subtree of a failed node)
```

Transitions are owned as follows:

- **PENDING → READY**: `ready_tasks(tree, finished ∪ in-flight)` — deterministic order
  (depth, sibling `width_idx`, then id; `tasktree.py:400-403`).
- **READY → SPAWNING**: Architectus admits a node **only while `in-flight < max_width`**.
  `max_width` here is the session-wide parallel-worker cap (I2.3; the dispatch-time
  check, not the build-time fan-out bound — `tasktree.py:13-17, 41-47`).
- **SPAWNING → RUNNING**: Custos `spawn` returns admission synchronously (D3); the worker
  reaches `ready` under `ready_timeout` 60 s (ipc-protocol-draft.md §6, line 490). The
  wire handshake stays `init → ready`; admission is a control-plane ack, never a
  pre-`ready` wire message (arch §5.2, line 381).
- **RUNNING → GATING**: deterministic result-envelope receipt (arch §7.1, line 587) —
  "done" is never self-reported.
- **GATING → DONE / GATE_FAILED**: gate verdict. Gate fail → steering turn with gate
  evidence, bounded `gate_max_retries` (D4; arch §7.1, lines 587-591).
- **CRASHED → SPAWNING**: bounded restart by Custos (burst cap, absolute cap, generation
  bump, jitter — arch §7.4; ipc-protocol-draft.md §4.4). Architectus is not consulted for
  a crash; Custos restarts within its hard budgets. **This is the one automatic retry.**

### 2.2 The loop

Pseudo-code (contract; pure core, no I/O):

```
loop(tree, custos, conversation_store):
    finished = {}          # task_id -> upward envelope (I2.7)
    subtree_failed = {}    # task_id -> (reason, root)
    in_flight = {}         # task_id -> NodeSession handle
    for wave:
        ready = [n for n in ready_tasks(tree, set(finished) | set(subtree_failed))
                 if n.task_id not in in_flight]
        if not ready and not in_flight:
            break
        # bounded admission
        for node in ready[: max_width - len(in_flight)]:
            handle = custos.spawn(node)               # D3 admission
            in_flight[node.task_id] = handle
            emit(task_decomposed, node)               # event-schema-draft §3.10
        # await one or more envelopes; each result arrives as an event
        for envelope, events in await envelopes(in_flight):
            node_id = envelope.task_id
            record into conversation_store.context_for(node_id)
            if envelope.status == succeeded:
                finished[node_id] = upward_result(node, envelope)   # I2.7 key set
            else:
                dead_end = apply_failure_policy(node, envelope, §6)
                if dead_end:
                    mark_subtree_failed(node, subtree_failed)       # §2.3
            in_flight.pop(node_id)
        # the orchestrating LLM seam (optional; §4) sees (tree_state, events) here
        # and may return replan/resolve actions (§6) before the next wave.
    return aggregate root (or fail)
```

### 2.3 Wave properties

- **Cycle-free by construction.** `build_tree` rejects cycles, multi-parent, unknown
  dependencies, multiple roots, over-depth, and over-fan-out *before any dispatch*
  (`tasktree.py:233-347`; I2.1-I2.3). `topological_order` is the dispatch-time re-check
  (`tasktree.py:350-377`) per DS-M6 (arch §18.1, line 1169; design-deltas.md D2 item 5,
  line 107). Cyclic graphs can otherwise leave tasks `pending` forever (design-deltas.md
  D2 WHY, line 115).
- **Bounded concurrency.** Admission is capped by `max_width − in_flight` each wave.
- **Dead-end detection.** A node that fails with no retries left, or that exhausts a
  supervisor-owned budget, is a dead end: Architectus marks its **subtree** failed
  (`subtree_of`, `tasktree.py:406-450`) and informs the parent via the `subtree_failed`
  event (§5). Descendants never dispatch. Siblings are untouched — a failed subtree
  does not fail its parent's other children (Erlang one-for-one spirit;
  custos-asyncio-design.md §5 delta 3).
- **Tree-level completion (I2.5).** The root reaches DONE only when every descendant has
  returned an envelope (arch §3.7 line 244). This is M5 acceptance criterion 1
  (`v2-1-review.md` lines 392-393).
- **Determinism.** Wave order is deterministic by construction
  (`ready_tasks` sort, `tasktree.py:400-403`); replay uses the event log
  (`task_decomposed`/`submitted` payload-first linkage, design-deltas.md D2 item 4).

---

## 3. Context composition algorithm

I2.4 is the normative rule: *a node's context = its own session log (bounded) + parent
summary + subtree result envelopes; a node never reads a sibling's raw session*
(arch §3.7, line 243). D8c adds the prompt-layout convention: *static, byte-stable
content at the TOP; dynamic content at the BOTTOM* (feedback-2-deltas.md D8c, lines
136-158). Both are enforced structurally (§3.5).

### 3.1 The static prefix (byte-stable, at the top)

Compiled once per (module, session) and reused unchanged across turns — this is what the
provider's exact-prefix KV cache can hit (D8c item 1-2; feedback-2-deltas.md lines
144-146). Contents, in order:

1. **System prompt** — role and capability framing for the node's worker kind.
2. **AGENTS.md-derived guidelines** — repo-constitution rules (citation: the `AGENTS.md`
   convention referenced by D8c item 1, feedback-2-deltas.md line 144; the guideline file
   itself lives in the repo root — **UNVERIFIED**: not re-read for this design).
3. **Tool definitions** — the `tools` allowlist semantics sent in `init` (arch §5.2, line
   304).
4. **Module instructions** — the node's decision-module instructions
   (`should_decompose` / `TaskRouter` / worker ReAct module instructions), which are the
   module's harness state (D5; feedback-2-deltas.md D8c line 144).

**Forbidden in the static prefix:** timestamps, `request_id`s, monotonic values,
per-call nonces, and any per-node volatile content — they churn the prefix and destroy
provider cache hits (D8c item 2, lines 145-146). Tested by a prompt-lint (D8c item 4,
line 147).

### 3.2 The dynamic tail (per-node, at the bottom)

Built per node per turn, in this exact order, each segment deterministically truncated to
the budget (§3.3):

1. **The node's own conversation.** `context_for(node_id)` from the conversation store
   (§6.6; the queryable substrate for I2.4): the bounded `last_turns(node_id, n)` — the
   node's own `init`/`steer`/`tool_event`/`checkpoint`/`result` transcript
   (arch §6.6, line 533). Own turns are truncated from the oldest end: a long-running node
   keeps its most recent steering context.
2. **Parent summary.** The parent's own `summary` (≤2k chars, I2.7) from the parent's
   upward envelope — the "parent summary" of I2.4.
3. **Subtree result envelopes.** Each child's `upward_result` envelope — **exactly** the
   I2.7 key set (`parent_task_id`, `unified_diff`, `diff_truncated`, `summary`,
   `metric_score`, `metric_breakdown`, `commits`, `files_changed`, `status`;
   `tasktree.py:50-60,453-478`). No scratchpad/CoT/trajectory can be present because the
   envelope key set is the only structure allowed through (structural enforcement, I2.7).
4. **Relevant files list.** `spec`-referenced files plus `files_changed` from child
   envelopes, capped to the file-list allowance. The *contents* are the worker's job; the
   list only names paths.

### 3.3 Token budget per node

The budget is **supervisor-owned and carried in `init.budget.max_tokens`** (D4; arch §5.2
lines 309-311, §7.4 lines 645-649). Architectus's composition rule is deterministic:

1. The static prefix is compiled once and its token count is fixed (it never churns).
2. The dynamic tail is filled in order: own turns → parent summary → child envelopes →
   file list.
3. Overflow evicts from the **least-recent** end first: oldest own turns, then older child
   envelopes, then file-list entries. Truncation is deterministic and logged with a
   `context_truncated` marker.
4. No segment may starve the others: `max_tokens` is allocated by fixed ratios
   (e.g. own 40% / children 30% / parent 10% / files 10% / reserve 10%) — the ratios are
   **proposed defaults** for M5, configurable, and **UNVERIFIED against any measurement**
   (see §3.4).

### 3.4 Compaction / snapshot parameters — open extension

`docs/research/compaction-design.md` **does not exist** (verified against
`docs/research/`). The task brief names it as the source for per-node token budgets; the
design therefore anchors on the material that *does* exist and flags the rest:

- arch §6.6 mandates *per-node snapshot/compaction and bounded retention* (line 535) and
  the conversation store is separate from the append-only event log (line 532).
- design-deltas.md D2 Q2.2 leaves "own session log (bounded)" open: *full tool-event
  stream vs bounded summary* — "token budget implications" (line 122). This design
  proposes bounded summary + parent summary + envelopes, which is Q2.2's proposed answer.
- The Prime-Agent compaction-summary precedent (goal/constraints/progress/blocked/
  decisions) is cited in design-deltas.md D2 WHY (line 116) as the summary shape to
  adopt for the bounded segment.

**UNVERIFIED:** the concrete `max_tokens` values, retention counts, and summary ratios
have no measured evidence on `main`; they are M5 test-time calibration inputs, not
pre-verified constants. A dedicated `compaction-design.md` is a follow-on research item
(§8, chunk S2).

### 3.5 Info-hiding enforcement (structural, not prompt-convention)

- `context_for(node_id)` reads only the node's own store and its own subtree
  (`subtree_of`, `tasktree.py:406-450`); sibling stores are never queried (I2.4).
- Upward envelopes are validated against `_ENVELOPE_KEYS`; a child message with an
  unknown top-level field (e.g. `scratchpad`) is rejected at the envelope boundary — the
  "Nuntius/Custos validate and reject unknown top-level fields" rule (arch §3.4, line
  188; D8b item 4, feedback-2-deltas.md line 121).
- The parent's context therefore *cannot* contain a child's scratchpad even if a buggy
  worker tried to send one. M5 acceptance criterion 3 tests exactly this with a canary
  string (`v2-1-review.md` lines 396-397).

---

## 4. The LLM interaction: Architectus as a module with a fake-LLM port

### 4.1 Module shape (D8a decide-style pure core + CLI)

Architectus follows the per-module contract: a **pure `decide` core** plus a **thin CLI**,
both per D8a (feedback-2-deltas.md D8a, lines 82-107), and the module-template pattern
(`docs/architecture/module-template/example-spec.md` §3; `src/cambium/modules/base.py:46-57`).

```
OrchestrationInput   = { "tree_state": serialized TaskTree + per-node status/envelope
                          pointers, "events": [ the events since the last tick ] }
OrchestrationOutput  = { "next_actions": [ Action, ... ] }

Action = Spawn(node_id) | Steer(node_id, turn) | Resolve(subtree_root) |
         Abort(node_id) | Replan(plan_revision) | Finalize(root)
```

- `decide(input: OrchestrationInput) -> OrchestrationOutput` is the **pure function**. It
  receives the serialized tree state and the new event batch and returns the typed next
  actions. State and I/O live at the edges: the loop (§2.2) calls `decide` after each wave
  with the accumulated events; the loop executes the returned actions.
- The CLI is the D8a adapter: `python -m cambium.modules.architectus` reads one JSON
  object from stdin (the input), writes one JSON object to stdout (the output), exit `0`;
  stderr is human diagnostics. This mirrors the `tasktree` CLI
  (`tasktree.py:481-493`) and makes the whole orchestrator pipe-testable without Custos
  (D8a item 1, feedback-2-deltas.md line 90).

### 4.2 The seam where the orchestrating LLM lives

The orchestrating LLM sits **behind the module's `decide`**, exactly where DSPy lives for
the other decision modules (v2-1-review decision F: DSPy imports and programs live
strictly behind each decision module's `decide.py` seam; the Deterministic Layer never
imports DSPy — lines 296-301).

Two implementations of the same `decide` seam:

1. **Rule-engine default (v2.1 production).** The deterministic decision table of §6 is
   implemented as pure rules (like `ShouldDecomposeModule`, `modules/example/decide.py:146-161`).
   Every failure transition has a deterministic default. This is what M5 ships and tests.
2. **LLM-backed interpretation (the RLM seam, behind the same interface).** For the
   judgment-heavy decisions (replan vs abort, resolver-task shape on `merge_failed`), the
   rule engine may delegate to an **`LLMProvider` port** — the D8d port
   (`call(prompt, tier, temperature) -> response`; arch §4, line 273) — injected at the
   composition root (v2-1-review decision F, line 301; arch §2 line 95). The provider is
   Diffundo's adapter (Diffundo itself is **UNVERIFIED** on `main`, §0.2). "Orchestrating
   LLM" never means a hardcoded import; it means *an LLMProvider implementation of the
   decide seam's interpretation step*.

### 4.3 The fake-LLM port pattern

`decide` never touches a real provider in tests. The `LLMProvider` port (D8d) is
implemented by a **`ScriptedLLM`** test adapter that returns deterministic, scripted
responses — keyed by prompt digest and/or a sequence index — so the entire loop
(spawn → envelope → aggregate → replan) is reproducible offline:

- The port is the *same* interface Diffundo's adapter implements, so the seam is
  independently testable (D8d; arch §4 line 273).
- A scripted response can force any branch of the §6 table: `replan(merge_failed)`,
  `abort(subtree)`, `resolve(conflict)`, `finalize(root)`. The loop's behavior is then
  pinned by scenario tests (§7) with zero network and zero provider cost.
- This is the same discipline as the fake worker for Custos
  (custos-asyncio-design.md §6, lines 201-204; `scripts/fake_worker.py`) applied at the
  orchestration layer.

---

## 5. Protocol and catalogue additions

### 5.1 What already exists (verified)

| Item | Status | Evidence |
|---|---|---|
| `steer` wire message | Defined in arch §5.2, lines 314-316; handled as a **v2.1 hook** in the worker (`worker.py:460-469`) | arch §5.2; `worker.py:460-469` |
| child→parent `result_envelope` flowing up the tree | Exists (D3 item 2; ipc-protocol-draft.md §2.4 line 293, §3) | design-deltas.md line 141 |
| `init.parent_task_id` tree linkage | Exists (D2; arch §5.2 line 299) | design-deltas.md line 97 |
| `init.budget` (max_turns, max_tokens, timeout_ms, gate_max_retries) | Exists, supervisor-owned (D4) | arch §5.2 lines 309-311; §7.4 |
| `resume_from_checkpoint` on restart | Exists (D3) | ipc-protocol-draft.md §2.2 line 186 |

### 5.2 Two verified reconciliation gaps (flagged, not solved here)

1. **`steer` is absent from the merged IPC draft catalogue.** `grep steer
   docs/research/ipc-protocol-draft.md` returns no match. D3 claims it amended the draft
   ("the merged IPC draft", design-deltas.md line 132, 139), but the merged copy of
   `ipc-protocol-draft.md` §2.2 lists only `init`, `context`, `run_task`, `check_health`,
   `cancel`, `shutdown` (lines 163-256). The design-deltas D3 amendment did **not** land
   in the draft. Architectus's M5 work must add it (§5.3, addition A).
2. **The wire shape of `steer` diverges between arch and worker.** arch §5.2 carries a
   `context` field (line 315); the merged worker parses `payload` with `{"action":
   "cancel"}` and **ignores everything else** (`worker.py:461-469`). Reconciliation is
   required before steer can carry real direction content (§5.3, addition A); the current
   worker can only abort, not receive context.

### 5.3 Additions required for M5

**A. `steer` enters the IPC catalogue (ipc-protocol-draft.md §2.2) as the 7th
supervisor→worker request.** Proposed shape (reconciling arch §5.2 and worker.py):

```jsonc
{"type":"steer","request_id":"01J…","session_id":"wt-abc-001",
 "payload":{"turn":4,"kind":"direction"|"gate_retry","context":"<parent turn, ≤2k chars>"}}
```

- Valid **only after `ready`** (arch §5.2 line 316; D3 line 139).
- Additive → no `proto` bump (ipc-protocol-draft.md §5, line 471: "new optional field, new
  event type" are backward-compatible within a `proto`).
- Architectus → Custos is a Python call; Custos → worker is the wire `steer`. Routing by
  `session_id`, parent→child only, sibling→sibling via the parent (arch §5.2 line 383;
  D3 item 4).
- **UNVERIFIED on `main`:** worker-side consumption of direction/gate-retry content. The
  current `worker.py:468` hook ("steer (v2.1 hook; continuing)") logs and continues; the
  real consumption is the Opifex M5 counterpart (gate-evidence steering turns, D4).

**B. Aggregate/feedback event kinds enter the event catalog (event-schema-draft.md §3).**
These are Architectus-issued **event-log kinds**, not worker wire messages. The catalog's
kind set is open-ended and unknown kinds pass through uninterpreted
(event-schema-draft.md §7, line 501), so all three are additive:

| New kind | Tier | Payload (proposed) | Meaning |
|---|---|---|---|
| `child_result` | NC | the I2.7 upward envelope fields (tasktree.py:50-60) | A child's envelope accepted into the parent's context; the aggregation bookkeeping record. Reconstructible from `worker_finished` + tree topology, hence non-critical. |
| `subtree_failed` | **C** | `{parent_task_id, failed_subtree_root, reason, failed_children[]}` | A dead end: subtree marked failed, parent informed, descendant dispatch stopped (§2.3). Critical: replay must not re-dispatch a failed subtree. |
| `replan` | NC | `{trigger ("merge_failed"\|"cap_exhausted"\|"resolver_requested"), plan_revision, added_tasks[]}` | The orchestrator feedback loop's decision record (the critique's "merge_failed → resolver task" loop). Reconstructible; audit value for Ascensus. |

**C. Entry points into the catalogue.** The IPC catalogue (`ipc-protocol-draft.md` §2)
covers **worker↔supervisor wire**; the new kinds are **Architectus↔Custos
control-plane/event-log** surfaces. Their normative home is therefore:

- Wire: `steer` → `ipc-protocol-draft.md` §2.2 (addition A) and the §7 reconciliation
  table.
- Event log: `child_result`, `subtree_failed`, `replan` → `event-schema-draft.md` §3
  catalog (addition B), with tier assignments following the draft's D7/D8 tier rule
  (event-schema-draft.md lines 560-561).
- Architectus↔Custos control plane (spawn/kill/restart/gate/merge) is **not wire**: it is
  the Custos Python API specified in custos-asyncio-design.md §5 deltas 7-11 (lines
  190-195), plus the event kinds above.

---

## 6. Failure semantics: the decision table

Two distinct classes, kept deliberately separate:

1. **Process crashes** are Custos's domain: restart within hard budgets, no LLM involved
   (arch §7.4; ipc-protocol-draft.md §4.4 lines 438-449).
2. **Task/result failures** are Architectus's domain: decide replan / resolve / abort —
   the orchestrator feedback loop.

| # | Event (source) | Custos does | Architectus decides | Architectus action |
|---|---|---|---|---|
| 1 | Node crash (no `exit_message`, or `reason:"crash"`) | Bounded restart (burst 5/60 s, absolute 10, generation bump, jitter) | — (automatic) | none; observe `restart_scheduled` |
| 2 | Node `failed` result envelope, retries left | gate/run again | **resolve in place** | `steer` turn with failure evidence (D4), bounded `gate_max_retries` |
| 3 | Gate `GATE_FAILED`, evidence as steering turn | run gate again (content-addressed verdict skip, D4) | **resolve in place → fail** | steer with gate output; then `FAILED` after bound |
| 4 | Node failed, retries exhausted | — | **abort subtree** | `subtree_failed` (critical event); stop descendant dispatch; inform parent |
| 5 | Budget exceeded (`max_turns`/`max_tokens`/`timeout_ms`) | kills process group, marks failed (supervisor-owned, D4) | **abort subtree** | `subtree_failed`; M5 AC: subtree abort on cap-exhausted |
| 6 | `merge_failed` (reason `conflict`) — Unio | — | **replan: resolver task** | new resolver subtask under the parent; requeue the affected subtree (critique's orchestrator feedback loop) |
| 7 | `merge_failed` (reason `test_failure`) | — | **resolve once → abort** | steering turn with test evidence, bounded; then `subtree_failed` |
| 8 | `merge_failed` (reason `non_fast_forward`) | — | **re-merge** | re-verify & merge on the moved base (arch §7.8; event-schema-draft.md §3.13) |
| 9 | Provider exhaustion (all providers down) | queue-pause + recovery monitor (D8f) | **pause dispatch** | no respawn/retry-loop; await recovery (arch §7.4 line 649) |
| 10 | Spec/config error (`recoverable:false`) | fail immediately, no restart | **fail node** | no retry (arch §7.4; ipc-protocol-draft.md §4.2 lines 405-406) |
| 11 | Cyclic / multi-parent / over-depth / over-width plan | none dispatched | **reject & re-prompt decomposer** | typed rejection evidence (M5 AC2), bounded re-prompt (I2.2; design-deltas.md D2 line 107) |
| 12 | Worker generation-mismatch fencing violation | `exit_message reason:"fatal"` (arch §7.3) | **fail node** | no retry (non-recoverable) |

The table is the **deterministic default policy** — the rule engine behind the §4.2 seam.
The LLM seam may propose deviations; the table is the offline-testable fallback and the
acceptance oracle. Every row maps 1:1 to a scenario test (§7).

The causal-chain rule from the coding constitution applies: a "resolve" decision must be
prompted by *evidence* (gate output, test output, conflict list from `merge_failed`), and
a retry counts as distinct only if the content changes (D4 item 1 — a byte-identical
retry is a no-op; gate verdicts are content-addressed, arch §7.9).

---

## 7. Test strategy: scenario tests with the fake LLM

Method: **real fixtures, no mocks, deterministic** — the established scenario discipline
(`tests/scenarios/test_tasktree.py`; custos-asyncio-design.md §6; v2-1-review line 473,
"Integration illusion" mitigation). The orchestrator loop is driven by a `ScriptedLLM`
(§4.3) and the fake worker (`scripts/fake_worker.py`) so every run is reproducible.

Each scenario asserts the event trail (events DB) and the terminal tree state, not just
the final status.

| # | Scenario | Decision-table row / invariant exercised | Key assertions |
|---|---|---|---|
| 1 | **Three-level fixture, dependency-gated dispatch** | I2.1-I2.5 | Only dependency-ready nodes dispatch; `max_width` honored per wave; root DONE only after all descendants' envelopes + gates (M5 AC1) |
| 2 | **Cyclic / multi-parent / over-depth / over-width rejection** | I2.2, I2.3, row 11 | Zero workers dispatched; typed rejection evidence emitted (M5 AC2) |
| 3 | **Replan on `merge_failed` (conflict)** | row 6 | A resolver subtask spawns under the parent; `replan` event with trigger `merge_failed`; subtree requeued |
| 4 | **Subtree abort on cap-exhausted** | row 5 | `subtree_failed` (critical) emitted; descendant dispatch stops; siblings unaffected |
| 5 | **Gate fail → steering retry → exhausted → fail** | row 3, D4 | Worker receives gate evidence as a `steer` turn; retries bounded; then FAILED with evidence |
| 6 | **Crash → bounded restart with generation bump** | row 1, arch §7.3 | Fake worker exits without `exit_message`; Custos restarts, generation increments, fencing accepted |
| 7 | **Context composition correctness** | I2.4, D8c | Parent context = static prefix + own bounded turns + parent summary + child envelopes; order and truncation per §3; prefix contains no volatile tokens (prompt-lint) |
| 8 | **Info-hiding enforcement** | I2.7, D8b | A canary scratchpad string written by a child never appears in parent context; an envelope with an unknown top-level field is rejected (M5 AC3) |
| 9 | **Dead-end propagation** | §2.3, row 4 | Child fails with no retries → `subtree_failed`; parent informed; sibling subtree completes normally |
| 10 | **Conversations.db queries + reconstruction** | §6.6, D8g | `last_turns`/`cost_by_node`/`context_for` use indexed query plans; deleting the projection and replaying durable protocol events reconstructs it (M5 AC4) |
| 11 | **Deterministic wave order** | `tasktree.py:400-403` | Two runs with identical scripted LLM input produce byte-identical event trails (modulo timestamps) |
| 12 | **Steer routing + sibling isolation** | D3 item 4 | Parent→child steer routes by `session_id`; sibling→sibling steer is rejected (parent-mediated only) |
| 13 | **Scripted-LLM replan decision** | §4.3, row 6/7 | A scripted response forces `replan(merge_failed)`; the loop's action sequence matches the script; no network |

Scenarios 7-10 are the M5 acceptance criteria 3-4 in executable form (`v2-1-review.md`
lines 396-399). Scenario count 13 = the 10-12 requested plus the deterministic-replay
pair; any can be dropped without weakening the set.

---

## 8. Milestone slicing: M5 in parallel-implementable chunks

M5 is an **L** milestone (v2-1-review.md line 383) with hard predecessors M1, M3, M4
(line 383). Sizes are implementation estimates (person-days, nominal), and the chunks are
parallelizable where noted.

| Chunk | Size | Scope | Depends on | Parallel with |
|---|---|---|---|---|
| **S1 — Architectus module skeleton** | S | `OrchestrationInput/Output` dataclasses, `Action` enum, `decide` pure core, D8a CLI, rule-engine default policy (§6 table) | M5 base | S2, S4 |
| **S2 — Conversation store + context composition** | S | `ConversationStore` (conversations.db, §6.6/D8g: `last_turns`/`cost_by_node`/`context_for`), §3 composition algorithm + token budget + prompt-lint | S1; conversation store component (v2-1-review gap 11, lines 189-191) | S4, S5 |
| **S3 — Wave scheduler** | M | §2 loop over `tasktree.ready_tasks` with bounded admission, envelope aggregation via `upward_result`, `subtree_failed` handling; fake-Custos harness | S1, S2 | — |
| **S4 — Steer emission + routing** | S | `steer` wire addition (ipc-protocol-draft §2.2), WorkerHandle routing by `session_id`, Opifex consumption of direction/gate-retry content | M2 (FD-3 protocol + pipe hardening); S3 | S5 |
| **S5 — Feedback-loop events** | S | `child_result`/`subtree_failed`/`replan` event kinds (event-schema-draft §3), the §6 decision table applied to live events | S3; M4 (gate verdicts) | S4 |
| **S6 — ResultEvaluator + aggregation** | M | `ResultEvaluator` module (arch §10, §4 M6), envelope aggregation into parent contexts, root finalization | S2, S3 | — |
| **S7 — Full-loop integration + scenario suite** | L | §7 scenarios 1-13 end-to-end: real worker + ScriptedLLM + real gate + Unio publish | S3-S6, M1 (canonical Custos), M3 (fencing/redaction), M4 (gate/budgets) | — |

Critical path: **S1 → S2 → S3 → S5 → S7** (context → schedule → feedback). S4 (protocol)
and S6 (evaluation) run off-path and can be owned by different implementers
concurrently with the critical path. The conversation-store component (v2-1-review gap 11)
is a sibling in-flight module; S2 is the first integration point.

Hard predecessor milestones (acceptance gates before S7 can be called accepted):
- **M1** — one canonical Custos on `main`, no fallback paths (v2-1-review.md lines 316-331).
- **M3** — generation fencing, redaction, approval (lines 349-364).
- **M4** — GateRunner extraction, deep budgets, `max_turns` real enforcement (lines 366-381).

Explicitly **out of M5**: the persistent cross-task worker pool (M7, lines 418-434 — the
pre-spawned pool; M5 steering works within one NodeSession's lifetime), DSPy
`should_decompose` refinement (M8), and tree-sitter compression (M9). M5's steering is
**mid-task** parent direction on a live NodeSession; the "multiple init messages per
process" model that would host multiple tasks per process remains deferred to M7
(ipc-protocol-draft.md §7 reconciliation row 1, line 525; arch §14, line 999).

---

## 9. Verification appendix

| Citation | Verified against | Status |
|---|---|---|
| v2-1-review.md M5 scope + AC1-AC4 | lines 383-399 | VERIFIED |
| v2-1-review.md decision A (thin Custos) | lines 215-233 | VERIFIED |
| v2-1-review.md decision C (conversations.db) | lines 257-268 | VERIFIED |
| v2-1-review.md decision D (pool trigger `max_width>=4`) | lines 270-282 | VERIFIED |
| v2-1-review.md decision F (DSPy behind decide.py) | lines 296-310 | VERIFIED |
| arch §3.4 upward envelope | lines 163-188 | VERIFIED |
| arch §3.7 I2.1-I2.7, NodeSession | lines 232-248 | VERIFIED |
| arch §4 M4/M6, module CLI (D8a), ports (D8d) | lines 252-274 | VERIFIED |
| arch §5.2 `steer`, `init.budget`, admission | lines 294-383 | VERIFIED |
| arch §6.6 conversation store | lines 528-536 | VERIFIED |
| arch §7.1 state machine | lines 544-591 | VERIFIED |
| arch §7.4 budgets supervisor-owned | lines 645-649 | VERIFIED |
| arch §16.2 `.cambium/sessions/conversations.db` | lines 1024-1048 | VERIFIED |
| `tasktree.py` build/topo/ready/subtree/upward | lines 233-478 | VERIFIED |
| `supervisor.py` is the slice, not Custos | lines 1-25 | VERIFIED |
| `orchestrator.py` skeleton, no logic | lines 1-59 | VERIFIED |
| `worker.py` steer v2.1 hook | lines 460-469 | VERIFIED |
| ipc-protocol-draft.md §2.2 request list (no `steer`) | lines 163-256 | VERIFIED (gap flagged) |
| ipc-protocol-draft.md §3 result envelope | lines 323-376 | VERIFIED |
| ipc-protocol-draft.md §5 additive versioning | lines 464-480 | VERIFIED |
| ipc-protocol-draft.md §7 reconciliation | lines 517-543 | VERIFIED |
| event-schema-draft.md §3 catalog + tiers | lines 72-98 | VERIFIED |
| event-schema-draft.md §3.10/§3.13 submitted/merge_failed | lines 227-273 | VERIFIED |
| design-deltas.md D2 (tree + invariants), D3 (sessions/steer) | lines 93-157 | VERIFIED |
| feedback-2-deltas.md D8a/D8b/D8c/D8d/D8g | lines 82-260 | VERIFIED |
| custos-asyncio-design.md WorkerHandle + deltas | §3.1, §5 | VERIFIED |
| Canonical Custos `run_plan` on `main` | — | **UNVERIFIED** (branch `wt-impl-super@9746b96`) |
| `src/cambium/conversations.py` exists | — | **UNVERIFIED** — does not exist on `main` |
| `src/cambium/diffundo.py` (LLMProvider adapter) | — | **UNVERIFIED** (branch `wt-impl-diffundo@f5ae0d3`) |
| `docs/research/compaction-design.md` exists | — | **UNVERIFIED** — does not exist on `main` |
| Token-budget ratios / compaction constants | — | **UNVERIFIED** — M5 calibration inputs |
| Worker-side steer content consumption | — | **UNVERIFIED** — `worker.py:468` is a placeholder hook |
| arch §5.2 `steer.context` vs worker `steer.payload` | lines 314-316 vs 460-469 | **DIVERGENT** — reconciliation required (§5.2) |
| `AGENTS.md` guideline file re-read | — | **UNVERIFIED** — cited by name only (D8c) |
