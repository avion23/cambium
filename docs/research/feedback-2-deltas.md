# Cambium — Second External Critique: Assessment + Deltas D8a..D8g

**Version:** 1.0.0
**Date:** 2026-08-09
**Branch:** `wt-fb2` (`/tmp/opencode/cambium-fb2`)
**Status:** Assessment of the second external critique against the current state (architecture v2.0.0 + design-deltas D1–D7 + merged research). New residue is captured as **D8a–D8g** in the same format as `docs/research/design-deltas.md`. This document supplements `docs/architecture/architecture.md` and `design-deltas.md` and supersedes them wherever a delta is marked **adopt**.

**Current-main note (2026-08-09):** the branch and count details below are the
historical assessment snapshot. Current main is at `6109a6a`, with 80 tracked
files, 33 research documents, and 108 collected tests. The canonical paths are
`docs/architecture/architecture.md`, `docs/architecture/module-template/`,
and `docs/architecture/reviews/`.

**Sources incorporated:** (1) the second external critique of the v2 architecture — 14 claims reproduced in §1; (2) current state, read from the branches listed in §0.3.

---

## 0. Reading this document

### 0.1 Verification convention

- **Every citation is a real file/section or a branch tree.** Files are cited by stable repository-relative path so coherence is auditable when branches merge (the same convention as `architecture.md` §20 and `design-deltas.md` §0.1).
- **Branch provenance is stated.** At the time of writing, `wt-fb2` is based at `96da568`; research docs merged onto `main` after that commit (`custos-asyncio-design.md`, `example-datasets-v1.md`, `logging-design.md`, `replay-restart-design.md`, `sandbox-options.md`, `test-strategy.md`, `threat-model.md`, `worktree-concurrency.md`) and the branch-local drafts (`cascade-design.md`, `sqlite-wal-durability.md`, `repo-structure-plan.md`) were read read-only from their named branches / `main`. All are real, verified trees.
- Anything that could not be verified is marked **UNVERIFIED** (§4).

### 0.2 "As of" note

- `docs/architecture/architecture.md` v2.0.0 — **as of commit `17ef25f` on branch `wt-arch`**. The D1–D7 deltas (`design-deltas.md` v1.0.0, branch `wt-deltas2`) are authoritative amendments to that document and are treated as current state here.
- Decision record: `implementation-plan.md` Decisions 8 (no local LLM cache), 9 (task tree), 10 (no sandboxing in the harness) — directive-sourced, adopted.
- Merged research: `main` tip `3621fd9` at the time of writing.

### 0.3 Files read for this assessment (provenance)

| Path | Where it lives now | Read from |
|---|---|---|
| `docs/architecture/architecture.md`, `agents.md`, `docs/architecture/module-template/*` | `wt-arch` @ `17ef25f` | `wt-arch` |
| `docs/research/design-deltas.md` (D1–D7) | `wt-deltas2` @ `905fc1b` | `wt-deltas2` |
| `docs/research/example-datasets-v1.md`, `custos-asyncio-design.md`, `threat-model.md`, `sandbox-options.md`, `ipc-protocol-draft.md` | `main` | `main` (read-only) |
| `docs/research/cascade-design.md`, `sqlite-wal-durability.md`, `repo-structure-plan.md` | `wt-cascade` @ `73093e7`, `wt-sqlite` @ `7f6ac8d`, `wt-hygiene` @ `660f930` | branches (read-only) |
| `docs/architecture/reviews/review-distributed-systems.md`, `review-llm-design.md`, `review-implementation.md` | `main` | `main` |
| `src/cambium/**` (scaffold) | `wt-fb2` (= main @ `96da568`) | `wt-fb2`; tests re-run here |
| `implementation-plan.md` | `main` / `wt-fb2` | `wt-fb2` |

### 0.4 Summary

| Claim | Verdict | Action |
|---|---|---|
| 1. Directory mess / duplicates | **REJECT** (with evidence; one real hygiene residue) | Layout proposal in §3 |
| 2. Independent hill-climbing delusion | **STALE — already handled** | None (residue already in design) |
| 3. `should_decompose` regex = toy | **ACKNOWLEDGED — roadmap** | None |
| 4. Async I/O deadlock (`with open` in loop) | **STALE — already fixed + validated** | None |
| 5. Actor model / SoC / Kahn / "Custos dumb" | **CONFIRMED — aligned** | None |
| 6. Let it crash / event sourcing / saga | **CONFIRMED — aligned** | None |
| 7. Unix-philosophy pure JSON modules + CLI | **ADOPT** | **D8a** |
| 8. RLM tree information hiding | **ADOPT** | **D8b** |
| 9. Provider prefix caching (static top / dynamic bottom) | **ADOPT** | **D8c** |
| 10. Hexagonal architecture / ports / DI | **ADOPT** (formalize) | **D8d** |
| 11. Sandbox → container/microVM | **ADOPT residue** (decision 10 already drops sandbox) | **D8e** |
| 12. Parallelism (LLM plan → N workers → queue → Unio) | **CONFIRMED — matches design** | None |
| 13. Token bucket + circuit breaker + pause-on-exhaustion | **ADOPT residue** (breaker/tiers already drafted) | **D8f** |
| 14. SQLite WAL conversation store; JSONL for IPC | **ADOPT residue** (event log already WAL) | **D8g** |

---

## 1. Point-by-point verdict table

| # | Critique claim | Status | Evidence |
|---|---|---|---|
| 1 | "Directory mess: cambium/cambium, duplicate files everywhere" | **REJECT** (claim false against the tracked tree); one genuine hygiene residue: **docs proliferation + template/instance doc overlap**, addressed by the layout proposal (§3) | **Verified here and in the hygiene audit.** `git ls-files` on `wt-fb2` (== main @ `96da568`): **30 tracked files**, the only `cambium` directory is `src/cambium/` (`src/cambium/cambium/` count = **0**); duplicate basenames = `__init__.py` only (intentional Python package markers). `docs/research/repo-structure-plan.md` (wt-hygiene) §1 re-ran the same checks: "0 matches", "only duplicate basename among the 30 tracked files: `__init__.py`", "No tracked junk: zero tracked files under `.pytest_cache/`, `.venv/`, `__pycache__/`". The duplicates the critique saw exist **only inside gitignored `.venv/` / `__pycache__/`** (confirmed present on the live tree in `/home/ubuntu/cambium`). **Real residue:** 31 research/design docs post-merge (§3) and the intended-but-overlapping pair `docs/module-template/architecture.md` (normative template) vs `src/cambium/modules/example/architecture.md` (filled-in instance; `example-spec.md` §12 already marks the in-tree doc "a shorter reference"). Both are intentional per `repo-structure-plan.md` §2 (doc taxonomy) and §3 (rule c); the layout proposal makes the taxonomy normative. |
| 2 | "Independent hill-climbing delusion, nodes coupled" | **STALE — already handled; the golden-dataset residue is already the design** | `docs/architecture.md` §17 restates the claim: §17.1 "They are not [independent]"; §17.2 "Each module is optimized against **frozen references** of its siblings, not their live co-adapted versions" (stub-worker / stub-decomposer pins, frozen held-out eval per module); §17.5 "per-module optimizable against frozen references… **not** jointly optimized"; resolution **LLM-C4** (§18.2): "Claim restated: per-module optimizable against pinned siblings." **Golden dataset already exists as the design:** `docs/research/example-datasets-v1.md` §1 — `train.jsonl` **200**, `eval.jsonl` **50**, `canaries.jsonl` **10**; `docs/module-template/dataset-format.md` §4 targets train 200 / eval 50 / canary 15 (v1 deviates to 10 canaries — recorded as a deviation in `example-datasets-v1.md` §5 item 4). |
| 3 | "`should_decompose` regex = toy" | **TRUE and acknowledged — roadmap, no change** | `example-spec.md` §0.2: "The v2 implementation is a **rule engine** (~140 LOC) — a DSPy program is a *future seam*, not the v2 primary." The DSPy seam is documented as **v2.1**: `example-spec.md` §5.1 ("The DSPy seam (v2.1+)"), §10 "Optimization Plan (v2.1)" (`dspy.SIMBA`, train ≥ 200, eval ≥ 0.85, canary 100%, model-pinned). `architecture.md` §17.1: "v2 rule engine today, DSPy seam documented in `docs/module-template/example-spec.md` §5.1." The toy-ness is the deliberate v2 trade; the replacement path is specced, not built. |
| 4 | "Async I/O deadlock (`with open` in event loop)" | **STALE — already fixed in the arch and empirically validated** | The flaw is **DS-C1** in `docs/reviews/review-distributed-systems.md` §C1 (blocking `open()`/`write()` in `_log_event`). Resolved in `architecture.md` §6.2 — "dedicated writer thread … the supervisor never performs disk I/O on the event-loop thread"; §6.5 fsync cadence; §18.1 row DS-C1. **Validated empirically:** `docs/research/sqlite-wal-durability.md` (wt-sqlite) — WAL read-while-write (Q1), crash-loss = 0 committed events (Q2), fsync target (Q3), power-loss ≤ 1 s window (Q4), conclusion "The durability contract holds"; `docs/research/custos-asyncio-design.md` (main) — "The event loop never calls `open()`, `write()`, `fsync()`, `sqlite3`, or blocking `git` directly. **DS-C1 is structurally impossible**." |
| 5 | "Actor model, SoC, Kahn networks, Custos dumber than a bag of hammers" | **CONFIRMED — aligned with the design** | Actor/OTP supervision: `architecture.md` §0 "Primary patterns kept: Erlang/OTP one-for-one transient supervision". SoC: §2 layering (Deterministic / Orchestration / Worker / Upper system; "The Deterministic Layer never calls an LLM and never crashes"). "Custos dumber than a bag of hammers" is exactly the intent: `§2` invariant + `§4` M4 (Custos is pure deterministic supervision) — a deliberately dumb, crash-proof core. Kahn: `§8.2` "This is the Kahn-process-network property… a true pass-through channel" — retained only for `Nuntius` where structurally true (resolution **DS-N6** dropped the CSP name-dropping elsewhere). No new delta. |
| 6 | "Let it crash, event sourcing, saga" | **CONFIRMED — aligned** | Let-it-crash: `§5.3` four-layer liveness + `§7.1` state machine (CRASHED → restart, bounded) + `§7.4` restart policy. Event sourcing: `§6.1`–`§6.3` SQLite WAL append-only event log (gap-free `seq`, replay from snapshots) — the durable feedback channel. Saga-style compensation: `§7.5` worktree recovery (`Surculus.recover` reset/clean/cherry-pick, quarantine on failure) + `§7.3` generation fencing + `§7.8` atomic `update-ref` publish with crash reconciliation (`Unio.reconcile`). Confirmed; no new delta. |
| 7 | "Modules as pure JSON-in/JSON-out functions; CLI pipe; strict JSON schemas; module knows only TaskInput→DecomposeOutput" | **ADOPT** (normative rule; the scaffold's `Module` ABC is compatible — `decide()` is the pure function; add the CLI wrapper) | Partially present: `base.py` `Module.decide` + typed `TaskInput`/`DecomposeOutput` dataclasses (strict schemas already enforced by `ExampleDatasetLoader._validate`), and `module-template/architecture.md` §3 ("Untyped `dict` inputs are not permitted"; outputs "must be JSON-serializable for the event log"). **Missing:** any CLI/pipe surface — the only module entry points are the scenario test and the *v2.1* eval stub `python -m cambium.modules.<name>.eval` (`module-template/architecture.md` §9.2, `example-spec.md` §12). **→ NEW DELTA D8a.** |
| 8 | "RLM tree: parent NEVER reads child scratchpad/reasoning; child returns unified diff + 3-sentence summary" | **ADOPT** (task tree already designed in D2; add the info-hiding rule explicitly) | D2 (`design-deltas.md`) formalizes the Task Tree: nodes, DAG validation, upward result envelopes, `parent_task_id` in the event log, I2.4 "a node never reads a sibling's raw session", I2.5 tree-level completion. `§3.4`/`§5.2` already carry `summary` (≤2k chars) + `metric_breakdown` upward. **Missing:** (a) the explicit rule that the *parent* never receives the child's scratchpad/chain-of-thought; (b) `unified_diff` as a normative upward field (only the `wt-slice` vertical slice carries `diff` in its `result_envelope`; the arch `result` message does not). **→ NEW DELTA D8b.** |
| 9 | "Provider prefix caching: static prefix at TOP, dynamic at BOTTOM; never timestamps/request IDs at top" | **ADOPT** (works with decision 8 — upstream caching) | Decision 8 (no local LLM cache) makes provider-side caching the only cache (`implementation-plan.md` Decisions #8; D1). D1's WHY already notes the residue: "hit rate degrades if the prompt prefix churns … managed by prompt structuring (**stable prefix first**)". The critique turns that guidance into a normative, testable prompt-layout convention. **→ NEW DELTA D8c.** |
| 10 | "Hexagonal architecture, ports and adapters, typing.Protocol, DI at root" | **ADOPT** (mostly aligned; formalize in the module template) | `base.py` already declares `Output` and `Metric` as `typing.Protocol`; workers receive `DiffundoConfig` and construct their own `Diffundo` client (`§9.3` `CambiumLM(diffundo, ...)` — injection, not import); `§2` layering keeps the Deterministic Layer type-independent of providers. **Missing:** the template does not name the ports/adapters boundary or a composition root; module wiring is implicit. **→ NEW DELTA D8d.** |
| 11 | "Sandbox: rip out of harness; worker in disposable Docker/Firecracker" | **ADOPT residue** (the rip-out is already decision 10; the residue is naming the deployment isolation vehicle) | Decision 10 (2026-08-09): "NO SANDBOXING IN THE HARNESS" (`implementation-plan.md`); D7 "No sandboxing in the harness; Septum removed from v2 scope", containment = worktree isolation + allowlists + approval gates, least-privilege worker env (R4 fix); `docs/research/sandbox-options.md` retained as evidence (bwrap blocked by AppArmor `apparmor_restrict_unprivileged_userns=1`); threat-model R3 re-rated "accepted — out of scope". **Missing:** the deployment-side statement that containers/microVMs are the *host's* isolation vehicle and that the worker is a plain stdio process whether local or in a container. **→ NEW DELTA D8e.** |
| 12 | "LLM plans (JSON array of sub-tasks); deterministic supervisor spawns N workers in N worktrees; results to queue; Unio merges; orchestrator wakes" | **CONFIRMED — matches the design exactly** | `§2` layering: Architectus (`TaskDecomposer` → `TaskRouter`) produces the subtask plan; `Custos` (Deterministic) spawns N `Opifex` workers, each in its own worktree (`§7.2`, `§7.5`); results flow up as envelopes (D3 "child→parent result messages"; `§5.2` `result`); `Unio` serializes merges (`§7.8`, `§4` M7); the orchestrator wakes on envelopes / tree-level completion (D2 I2.5). The `wt-slice` vertical slice is the working proof (spawn → worktree → gate → `git merge --ff-only`). |
| 13 | "Token bucket + circuit breaker in Diffundo; capability tiers; all-providers-exhausted → async queue pauses, workers await" | **ADOPT residue** (circuit breaker + tiers already drafted; token bucket + pause-on-exhaustion are new) | Circuit breaker + capability tiers: `docs/research/cascade-design.md` §1.1 (ordered per-tier fallback), §2.3 (sliding-window circuit breaker: HEALTHY/COOLDOWN/OPEN/HALF_OPEN, `failure_threshold`); `architecture.md` §9.1 (`tier`, `supports_tools`, `context_window`) and §9.2 (tier primary key — resolution LLM-C2). `AllProvidersFailed` is a real class (`§9.2`, IMPL-M5). **Missing:** a token-bucket rate limiter (cooldown bounds failures, not throughput) and explicit **pause-on-exhaustion** at the dispatch queue (arch §7.4 today bounds provider outage inside the worker with `provider_patience_s` backoff; the critique wants the queue to pause and workers to await recovery). **→ NEW DELTA D8f.** |
| 14 | "SQLite WAL for conversation storage (queryable context extraction); JSONL only for low-level IPC" | **ADOPT residue** (event log already SQLite WAL; the per-node conversation store is new) | Event log = SQLite WAL: `§6.1`–`§6.3` + validated by `docs/research/sqlite-wal-durability.md`. IPC = JSON-Lines: `§5.1`. **Missing:** per-node conversation/session history as a *queryable* store. D2 item 2 currently places each node's session log under `${session_dir}/cambium/sessions/<node_id>/` append-only (file-based, not queryable). **→ NEW DELTA D8g.** |

---

## 2. NEW DELTAS D8a–D8g

### D8a — Unix-philosophy module contract: pure JSON-in/JSON-out CLI + strict schemas

**Source:** EXTERNAL CRITIQUE (feedback-2, claim 7).
**Status:** **adopt** (normative rule).
**Amends:** `docs/module-template/architecture.md` §3 (Interfaces — add CLI contract) and §9 (test strategy — add CLI test); `docs/architecture.md` §4 (module catalog — each module ships a CLI entry); `docs/module-template/example-spec.md` §12 (scaffold alignment — add the CLI wrapper to the "extensions" list).

#### WHAT changes

1. **Every module MUST ship a CLI entry `python -m cambium.modules.<name>`** — `<name>` is the **package directory** per `module-template/architecture.md` §9.2 (e.g. the scaffold's reference module lives at `cambium.modules.example`; its logical name is `should_decompose`, its package path is `example`). Contract: read **one JSON object from stdin** (module input), write **one JSON object to stdout** (module output), exit `0` on success; non-zero exit with a JSON `{"error": {…}}` object on failure. stderr is reserved for human diagnostics (mirrors `§5.1` stdout-reserved-for-protocol discipline). The module must be pipe-able: `echo '<json>' | python -m cambium.modules.example` is a supported, tested invocation.
2. **Strict JSON schemas are the module's typed dataclasses.** The input/output schemas (e.g., `TaskInput` / `DecomposeOutput`) are the CLI's schema; the wrapper validates the stdin object against them and rejects unknown/invalid fields (reuse the loader-validation pattern from `ExampleDatasetLoader._validate`).
3. **The scaffold's `Module` ABC is unchanged and compatible.** `decide()` **is** the pure function; the CLI is a thin adapter (~30 LOC: `json.loads(sys.stdin.buffer.read())` → construct input → `asyncio.run(decide(input))` → `json.dumps(output)`, `sort_keys=True`). This keeps the DSPy seam intact (a DSPy replacement implements the same `decide`, the CLI is untouched).
4. **Distinct from the eval entry point.** `python -m cambium.modules.<name>.eval` (v2.1, `module-template/architecture.md` §9.2) scores the dataset; the new CLI *is* the module. Both can coexist (`__main__.py` for the module CLI; `eval.py` for scoring).

#### WHY

- The critique's Unix-philosophy point is sound and cheap: a pure JSON-in/JSON-out module is independently testable, composable in shell pipelines, drivable from datasets ("pipe a dataset into decomposer.py from CLI"), and decoupled from the harness's event loop. It also gives the vertical-slice and smoke tests a stable seam (`agents.md` §9 item 4/5).
- It matches what the design already asserts — `module-template/architecture.md` §3.2 "outputs … must be JSON-serializable for the event log", `§3.1` "Untyped `dict` inputs are not permitted" — by adding the missing transport.
- It closes the only real gap in claim 7: today the module has no executable face other than the scenario test.

#### Open questions

- **Q8a.1** Batch mode: single-object-in/single-object-out (proposed, minimal) vs JSONL-stream-in/JSONL-stream-out for dataset piping? (Owner: module-template owner.)
- **Q8a.2** The CLI is async (`asyncio.run(decide(...))`) so a DSPy-backed `decide` works unchanged — confirmed? (Owner: `Architectus` author.)
- **Q8a.3** Error envelope shape: include a `schema_version` field for forward-compat? (Owner: `Nuntius` schema owner.)
- **Q8a.4** Where the wrapper lives: `__main__.py` in the module dir (proposed) vs `cli.py` per module? (Owner: build agent.)

---

### D8b — Task Tree information hiding: child→parent envelope = diff + summary + metrics ONLY

**Source:** EXTERNAL CRITIQUE (feedback-2, claim 8).
**Status:** **adopt**.
**Amends:** `docs/architecture.md` §3.4 (`Result` envelope — add `unified_diff`), §5.2 (`result` message — add `diff`), §6.3 (event log — `parent_task_id` per D2); `docs/research/design-deltas.md` D2 (add normative invariant **I2.7**).

#### WHAT changes

1. **Normative rule (new invariant I2.7): a child node NEVER sends its scratchpad, chain-of-thought, reasoning trace, or trajectory upward.** The child→parent result envelope carries **exactly**: `unified_diff` (per-file diff body), `summary` (≤2k chars, worker-authored), `metric_breakdown` (§10), `commits`, `files_changed`, terminal `status`. Nothing else. This is the critique's "parent never reads child scratchpad; child returns unified diff + 3-sentence summary" made normative.
2. **`unified_diff` becomes a first-class upward field.** The `wt-slice` vertical slice already ships `diff` in its `result_envelope` (`vertical-slice-report.md`, `scripts/fake_worker.py`); this delta promotes that shape into `§3.4`/`§5.2`. **Cap: 64 KiB**, adopted from the merged IPC draft — `docs/research/ipc-protocol-draft.md` §3 (`result_envelope` message: `"diff": "…", // draft, capped 64 KiB`; field table: "`git diff base_commit..worktree`, capped at 64 KiB"). This supersedes the 256 KiB figure in this document's first draft: the IPC draft is the merged, reconciled artifact for the `result_envelope` shape (it also caps `summary` at 2k chars per `arch §3.4`), and D8b deliberately shares its envelope with it. Overflow truncates with a `diff_truncated: true` flag.
3. **Scratchpad/reasoning stays in the node's own session store** (D2 item 2, I2.6 append-only) and is read only by: the node itself (resume), `Ascensus` (offline optimization reads the session store directly — never upward messages), and a host that explicitly queries the session store. It is never forwarded by `Custos`.
4. **Enforcement is deterministic.** `Nuntius`/`Custos` validates upward messages against the envelope schema (rejecting unknown top-level fields like `scratchpad`/`reasoning`), so the rule is structural, not a prompt convention.

#### WHY

- Information hiding is the point of the RLM tree (D2): the parent's context is already bounded (`I2.4` node context = own log + parent summary + subtree envelopes). Letting raw child reasoning up would (a) pollute the parent's context window, (b) let the parent over-fit on child transcripts instead of artifacts, and (c) leak private reasoning across trust levels (`threat-model.md` R1 — injected-content steering). The precedent is opencode's subagent model, which returns results, not transcripts (`docs/research/opencode.md` §1).
- D2 already forbids sibling→sibling raw-session reads; I2.7 closes the parent direction, making the tree a strict envelope-passing structure.

#### Open questions

- **Q8b.1** Diff cap confirmed at 64 KiB (IPC draft §3); truncation flag `diff_truncated: true` on overflow, or content-addressed dedup across children (a child whose diff is empty sends `diff: null`)? (Owner: `Nuntius` schema owner.)
- **Q8b.2** Does the 3-sentence summary stay worker-authored (proposed) or get a deterministic parent-side fallback when empty? (Owner: `Architectus` author.)
- **Q8b.3** Should `checkpoint`/tool-event streams remain upward-visible to the *parent* for audit, or is "envelope only" absolute? Proposed: absolute for LLM-facing upward messages; tool events stay in the node's store and the event log, not in upward messages. (Owner: orchestrator owner.)

---

### D8c — Provider prefix caching: static prefix at TOP, dynamic at BOTTOM

**Source:** EXTERNAL CRITIQUE (feedback-2, claim 9).
**Status:** **adopt** (works with decision 8 — upstream caching only).
**Amends:** `docs/architecture.md` §9.3 (`CambiumLM` — prompt-construction contract); `docs/research/design-deltas.md` D1 (add the prompt-structure convention to the D1 WHY/WHAT residue); `docs/module-template/architecture.md` §5 (prompt-structure convention).

#### WHAT changes

1. **Normative prompt-layout convention for every `CambiumLM`/`Diffundo.call` caller:** static, byte-stable content goes **at the TOP** — system prompt, AGENTS.md-derived guidelines, tool definitions, module instructions, task-independent few-shot context; dynamic content goes **at the BOTTOM** — task spec, repo context, observations, tool results.
2. **Never place timestamps, `request_id`s, monotonic values, or per-call nonces at the top of a prompt.** They churn the exact-prefix key and destroy provider-side cache hits (OpenAI / Anthropic / DeepSeek prefix KV caching — citations in D1).
3. **This is guidance that enables upstream caching, not a correctness mechanism** — consistent with D1 ("the worker and orchestrator code do not manage any cache; they may only place stable prefixes so the provider's cache hits (guidance, not a correctness mechanism)"). Decision 8 stands: no local cache.
4. **Testable:** a prompt-lint check in the module test suite asserts static-before-dynamic ordering and no volatile tokens in the static prefix (small pure helper, e.g., `build_prompt(static: tuple[str, ...], dynamic: str) -> str`).

#### WHY

- With the local cache deleted (decision 8/D1), the only caching in the system is the provider's, and provider caches are exact-prefix content-addressed (D1's three cited provider docs). Prefix churn at the top is the one way hit rate collapses; the convention is cheap and structural.
- The critique's "never timestamps/request IDs at top" is exactly D1's "stable prefix first" made testable.

#### Open questions

- **Q8c.1** Helper `build_prompt` in `cambium.diffundo` (proposed) vs convention-only? (Owner: `Diffundo` author.)
- **Q8c.2** DeepSeek cache-prefix-units are ~64-token aligned; does the static prefix need alignment padding for max hit rate? (UNVERIFIED — no provider instrumentation yet; D1 Q1.2's `cached_tokens` telemetry is the prerequisite.) (Owner: `Diffundo`.)
- **Q8c.3** Do worker ReAct prompts violate the rule inherently (each turn appends observations)? No — observations are dynamic and belong at the bottom; the static prefix is the invariant head. Confirm in the lint. (Owner: `Opifex` author.)

---

### D8d — Hexagonal modules: ports/adapters + DI at the composition root

**Source:** EXTERNAL CRITIQUE (feedback-2, claim 10).
**Status:** **adopt** (formalization; the scaffold already has the raw material).
**Amends:** `docs/module-template/architecture.md` §3 (Interfaces — add a "ports and adapters" subsection), §5.4 (LLM access — constructor injection of the port); `docs/architecture.md` §4 (module catalog — note the composition root), §2 (layering — module instantiation boundary).

#### WHAT changes

1. **A module's boundary is defined by typed ports (`typing.Protocol`), not concrete imports.** v1 port set: `LLMProvider` (`call(prompt, tier, temperature) -> response`), `EventSink` (emit the module's decision events), `DatasetStore` (load examples). Adapters implement the ports: `DiffundoAdapter(LLMProvider)` wrapping `Diffundo`. The ports pattern is the **target for the DSPy seam, not an existing fake**: the scaffold's `example-spec.md` §9.1 runs `decide()` with "no mocking, no network" simply because the v2 rule engine is a **pure function** — there is no LLM call and no fake-LLM harness in the scaffold today. When a DSPy-backed `decide` replaces the engine (v2.1), it will need an injected `LLMProvider`, and tests can then implement a fake adapter against the same port.
2. **Constructor injection at a composition root.** Module instances are built in one place (proposed: `cambium.orchestrator` wiring or a dedicated `cambium.container`) from `Config`, with ports injected; a module never constructs `Diffundo`/a provider itself (except the worker-side `CambiumLM` construction, which is already config-injected via `init.fanout_config` — `§9.3`).
3. **The scaffold's `Output`/`Metric` Protocols (`base.py`) and `Module` ABC stay; the delta adds the explicit port list to the template** so every future module's architecture.md names its ports and the adapters that implement them.

#### WHY

- The critique's point is mostly already true (`Output`/`Metric` Protocols; `CambiumLM(diffundo, ...)` injection; `§2` "Workers depend only on Nuntius and Diffundo"). The gap is that the module template never *names* the boundary, so a future module can silently import a concrete provider. Naming ports + a composition root makes testability (fake ports in scenario tests) and the DSPy seam mechanical.
- It preserves the layering invariant (`§2`: Deterministic Layer never imports an LLM type).

#### Open questions

- **Q8d.1** Composition root: `cambium/container.py` (new) vs wiring inside `cambium/orchestrator.py` (proposed elsewhere for `Architectus`)? (Owner: orchestrator owner.)
- **Q8d.2** Port granularity: is `LLMProvider` + `EventSink` + `DatasetStore` sufficient for v2, or add `Clock` (for deterministic timing in tests)? (Owner: module-template owner.)
- **Q8d.3** Does `CambiumLM` (`§9.3`) already satisfy `LLMProvider`, or does the adapter wrap it? (Owner: `Diffundo` author.)

---

### D8e — Deployment isolation: containers/microVMs live OUTSIDE the harness

**Source:** EXTERNAL CRITIQUE (feedback-2, claim 11).
**Status:** **adopt** (residue of decision 10 — the sandbox removal is already done).
**Amends:** `docs/architecture.md` §7.2 (spawn — worker is a stdio subprocess regardless of container), §4 (M8 row — "removed"; add deployment note), §2 (upper system owns isolation); `docs/research/design-deltas.md` D7 (add deployment note).

#### WHAT changes

1. **Decision 10 (no sandboxing in the harness) is unchanged.** This delta documents the *deployment* boundary the critique asks for: **containers (Docker) / microVMs (Firecracker) are the isolation vehicle, and they live OUTSIDE the harness** — owned by the upper system (`§2`), not by Cambium.
2. **The worker is a stdio process whether local or in a container.** The worker contract is `python -m cambium.opifex` speaking JSON-Lines on stdin/stdout (`§5.1`). A host that wants isolation wraps that process in a container/microVM and connects the pipes; Cambium code, IPC, and semantics are byte-identical. No harness change is required — the IPC contract is transport-agnostic.
3. **Cambium does not build, manage, or assume containers.** Document the boundary so operators know the removed Septum is replaced by a host-side vehicle, not by nothing. `docs/research/sandbox-options.md` remains as the evidence record of why in-harness sandboxing was dropped (AppArmor block).

#### WHY

- The critique's "rip the sandbox out of the harness" is already executed (decision 10 / D7). Its residue — "run the worker in a disposable Docker container / Firecracker" — is correct as a *deployment* story and currently unnamed in the docs. Naming it costs nothing (it is a documentation delta) and answers the operational question "what replaces the sandbox?" with: host-side containers/microVMs, plus the D7 containment stack (worktrees + allowlists + approval gates + least-privilege env).

#### Open questions

- **Q8e.1** Reference container image layout (Python 3.14, `uv`, no provider keys baked)? Proposed as a deployment example doc, not a Cambium artifact. (Owner: ops / host.)
- **Q8e.2** Does the least-privilege worker env (D7/R4 fix) compose with container env injection (env passthrough)? Proposed: yes — container env is a superset channel; the harness still constructs the scrubbed dict at spawn. (Owner: `Custos` author.)
- **Q8e.3** Firecracker vs Docker guidance, or leave the vehicle un-pinned? Proposed: un-pinned (host's choice). **UNVERIFIED:** no container was run in this worktree.

---

### D8f — Diffundo token-bucket rate limiting + all-providers-exhausted pause

**Source:** EXTERNAL CRITIQUE (feedback-2, claim 13).
**Status:** **adopt** (residue — circuit breaker + tiers already drafted).
**Amends:** `docs/architecture.md` §9.1 (`ProviderConfig` — add token-bucket params), §9.2 (cascade steps 4–5 — bucket check before attempt; `AllProvidersFailed` → queue pause), §7.4 (provider-outage handling — add queue-level pause); `docs/research/cascade-design.md` §2.3 (breaker already there — no change).

#### WHAT changes

1. **Token-bucket rate limiter per provider** (and optionally per tier): before each cascade attempt, `Diffundo.call` checks the provider's bucket; an empty bucket marks the provider `RATE_LIMITED` and the cascade skips it (same selection-filter path as cooldown — `§9.2` step 2). Bucket refills at `rpm` tokens/min (`ProviderConfig.rpm`, default per provider; tier budget is the cascade-design cost-budget concept, unchanged).
2. **Pause-on-exhaustion.** When the cascade exhausts every provider (`AllProvidersFailed`), the **dispatch queue pauses**: the orchestrator stops dispatching new tasks (already the IMPL-M5 "park dispatch" posture) and — new — the supervisor does not respawn/retry-loop workers awaiting LLM; a **recovery monitor** wakes dispatch when any provider's bucket/cooldown/breaker recovers. Workers in-flight await, they do not crash-loop (extends `§7.4`'s in-worker `provider_patience_s` backoff to the queue level).
3. **Circuit breaker and capability tiers are NOT new** — `cascade-design.md` §2.3 (sliding-window breaker, OPEN/HALF_OPEN) and `§1.1` (per-tier ordered fallback) already cover them; `§9.1` tier/capability metadata already exists. D8f layers the missing rate limiter + queue pause on top.

#### WHY

- Cooldown (`§9.1` `cooldown_s`) bounds *failures*; it does not bound *throughput* — a healthy provider can still be hammered at a rate the provider's API rejects, and each rejection resets cooldown. A token bucket is the standard fix and composes with the existing selection filter.
- The critique's "all-providers-exhausted → async queue pauses, workers await" is stronger than the current `provider_patience_s` (which keeps the worker alive but retrying inside its loop). An explicit queue pause + wake-on-recovery prevents both worker thrash and provider re-hammering after a total outage.

#### Open questions

- **Q8f.1** Bucket params: per-provider `rpm` defaults (UNVERIFIED — no provider-landscape rate data; `docs/research/provider-landscape.md` may inform them). (Owner: `Diffundo`.)
- **Q8f.2** Who owns the recovery monitor — `Custos` (Deterministic, no LLM) watching provider health events, or the orchestrator? Proposed: `Custos` timer + `provider_health_change` events (cascade-design §5.2). (Owner: `Custos` author.)
- **Q8f.3** Interaction with D4 gate retries: a gate run that needs LLM during a pause — does the gate wait (proposed) or fail fast? (Owner: orchestrator owner.)

---

### D8g — Per-node conversation/session history in SQLite WAL (queryable)

**Source:** EXTERNAL CRITIQUE (feedback-2, claim 14).
**Status:** **adopt** (residue — the event log is already SQLite WAL; the per-node store is new).
**Amends:** `docs/architecture.md` §6.1 (event store — add the conversation store); `docs/research/design-deltas.md` **D2 item 2** (node session log — storage engine). **Attribution note:** the `sessions/<node_id>/` subtree is introduced by **D2**, not by `architecture.md` §16.2 — §16.2's session layout lists only `events.db`, `events.jsonl`, `cambium.log`, `result.json`, `status.json`, `worktrees/`, `checkpoints/`, `quarantine/`, `optimized/`. D8g rewrites D2 item 2's "append-only files" into a SQLite-backed store; §16.2 is affected only insofar as the post-merge layout gains the D2 `sessions/` subtree.

#### WHAT changes

1. **The event log stays SQLite WAL (`§6.1`–`§6.3`) — no change.** New: **per-node conversation/session history is stored in SQLite WAL, queryable.** D2 item 2 currently places each node's full conversation under `${session_dir}/cambium/sessions/<node_id>/` as append-only files; D8g makes that store SQLite WAL (same writer-thread discipline as `§6.2`) so the orchestrator can run **bounded queries** for context-window composition (D2 I2.4: node context = own session log (bounded) + parent summary + subtree envelopes) — e.g., "last N turns", "cost by node", "turns since last checkpoint" — without reading raw files.
2. **Layout proposal:** a `conversations.db` (SQLite WAL) under `sessions/` owning per-node `node_sessions` tables, OR new tables in the existing `events.db`. Proposal: separate `conversations.db` (the event log is append-only history; conversations are mutable-queryable state) — open question Q8g.1.
3. **JSONL is retained exactly where the design already uses it:** the IPC transport is JSON-Lines (`§5.1`), and the optional event mirror is JSON-Lines (`§6.1`). The critique's "JSONL only for low-level IPC" is already true; the conversation store is not IPC.
4. **Growth bounds mirror the event log:** per-node snapshot/compaction (like `§6.1` snapshots), bounded retention; a node's store is pruned with its session dir (`§16.2`).

#### WHY

- The event log answers "what happened system-wide"; the conversation store answers "what did *this node* see and decide" — the queryable substrate the RLM tree (D2) and the D8b envelope rule need for context composition without forwarding scratchpads.
- SQLite WAL is already the validated durability engine in this project (`sqlite-wal-durability.md` Q1 read-while-write: "reader never blocks, never sees uncommitted, sees commits immediately"); extending it to conversations reuses proven machinery instead of inventing a second store.

#### Open questions

- **Q8g.1** Separate `conversations.db` (proposed) vs tables in `events.db`? Trade: one writer thread per DB vs cross-table consistency; events.db is append-only, conversations are read-query-heavy. (Owner: `Custos` + `Nuntius` schema owners.)
- **Q8g.2** Query API: a `ConversationStore` port (adheres to D8d ports) with `last_turns(node_id, n)`, `context_for(node_id)` returning the bounded D2 I2.4 context? (Owner: `Custos` author.)
- **Q8g.3** What exactly is in the conversation vs the event log — full message payloads (steering, tool events, checkpoints, results) in `conversations.db`, with `events.db` keeping the same facts for audit? Risk of double-write. Proposed: conversation = the node's protocol transcript (init/steer/tool/checkpoint/result), event log = the cross-cutting durable record with `parent_task_id` (D2). (Owner: event-schema owner.)

---

## 3. Repo-layout proposal (final tree after wave-2 merges)

**Verified baseline:** union of all 17 branch trees (`wt-*` + `main`) = **65 tracked files** (counted via `git ls-tree` across all refs). This commit adds `docs/research/feedback-2-deltas.md` → **66**. `docs/research/repo-structure-plan.md` (wt-hygiene) is the canonical audit; this section reconciles it with the two delta docs and this file. **No structural moves are required** — every file already lands in a rule-compliant location; the "reorg" is verification + transient removal + README polish (repo-structure-plan §5).

```
cambium/
├── .gitignore
├── README.md                        # pointers: architecture.md canonical; system-design.md superseded
├── agents.md                        # (wt-arch) agent orientation
├── implementation-plan.md           # TRANSIENT — removed at reorg end (repo-structure-plan §5 step 2)
├── pyproject.toml
├── uv.lock                          # intentional, tracked
├── docs/
│   ├── architecture.md              # canonical v2 (wt-arch @ 17ef25f); amended by design-deltas D1–D7 + this doc D8a–D8g
│   ├── system-design.md             # v0.1 draft, superseded, kept as origin record
│   ├── module-template/             # normative (wt-arch)
│   │   ├── architecture.md          # amended by D8a (CLI), D8d (ports/DI)
│   │   ├── dataset-format.md
│   │   └── example-spec.md          # amended by D8a (CLI wrapper)
│   ├── research/                    # 31 evidence + design-record docs — no pruning (rule b)
│   │   ├── bench-harness-design.md
│   │   ├── cascade-design.md        # circuit breaker + tiers (D8f reference)
│   │   ├── cloud-code.md
│   │   ├── codex.md
│   │   ├── custos-asyncio-design.md
│   │   ├── design-deltas.md         # D1–D7 (wt-deltas2)
│   │   ├── dspy-python-314.md
│   │   ├── event-schema-draft.md
│   │   ├── example-datasets-v1.md   # golden dataset 200/50/10
│   │   ├── feedback-2-deltas.md     # this document (wt-fb2)
│   │   ├── ipc-protocol-draft.md
│   │   ├── logging-design.md
│   │   ├── metric-design.md
│   │   ├── omp.md
│   │   ├── onboarding-checklist-draft.md
│   │   ├── opencode.md
│   │   ├── pi.md
│   │   ├── prime-agent.md
│   │   ├── provider-landscape.md
│   │   ├── pydev.md
│   │   ├── python-3.14.md
│   │   ├── replay-restart-design.md
│   │   ├── repo-structure-plan.md   # hygiene audit + reorg checklist (wt-hygiene)
│   │   ├── sandbox-options.md       # evidence record for decision 10 / D7
│   │   ├── sqlite-wal-durability.md # empirical validation (D8g basis)
│   │   ├── test-strategy.md
│   │   ├── threat-model.md
│   │   ├── tui-best-practices.md
│   │   ├── vertical-slice-report.md # worker→worktree→gate→merge proof
│   │   ├── worker-coldstart.md
│   │   └── worktree-concurrency.md
│   └── reviews/                     # 3 adversarial reviews, kept
│       ├── review-distributed-systems.md
│       ├── review-implementation.md
│       └── review-llm-design.md
├── scripts/                         # repo tooling
│   ├── check_dataset_v1.py
│   ├── fake_worker.py               # (wt-slice) JSON-Lines worker proof
│   └── generate_should_decompose_v1.py
├── src/cambium/
│   ├── __init__.py
│   ├── events.py
│   ├── orchestrator.py
│   ├── supervisor.py                # (wt-slice) minimal asyncio supervisor
│   └── modules/
│       ├── __init__.py
│       ├── base.py                  # Module ABC + Output/Metric Protocols (D8a, D8d)
│       └── example/
│           ├── __init__.py
│           ├── architecture.md      # per-module instance doc — "shorter reference" (example-spec §12)
│           ├── dataset.py
│           ├── datasets/
│           │   ├── canaries.jsonl   # 10 (dataset v1)
│           │   ├── eval.jsonl       # 50
│           │   ├── example_pairs.jsonl
│           │   ├── meta.json
│           │   └── train.jsonl      # 200
│           ├── decide.py
│           └── metric.py
└── tests/
    └── scenarios/
        ├── test_example_module.py
        └── test_vertical_slice.py
```

**Doc taxonomy (normative, from repo-structure-plan §2 + D8 deltas):**

| Category | Location | Role | Post-merge count | Action |
|---|---|---|---|---|
| Research (evidence) | `docs/research/` | Competitive analysis + design drafts + decision records (`*-design.md`, `*-draft.md`, `*-deltas.md`, competitor names) | 31 | No pruning — historical evidence |
| Reviews | `docs/reviews/` | Adversarial reviews (flaw→fix evidence) | 3 | No |
| Canonical architecture | `docs/architecture.md` | v2 authoritative spec (+ delta docs amend it) | 1 | No |
| Templates | `docs/module-template/` | Normative template + reference spec | 3 | No (D8a/D8d amend) |
| Design draft | `docs/system-design.md` | v0.1 origin record, superseded | 1 | No (mark superseded in README) |
| Per-module docs | `src/cambium/modules/<name>/architecture.md` | Filled instance of the template, co-located with code | grows with modules | No (the "overlap" is intended: template = normative, instance = concrete) |
| Agent orientation | `agents.md` (root) | Onboarding | 1 | No |
| Transient | `implementation-plan.md` (root) | Orchestrator tracker | 1 | **Remove at reorg end** |

**The critique's claim-1 residue is therefore resolved as:** (a) the "duplicates" it saw were in gitignored `.venv/`/`__pycache__/` — nothing tracked, nothing to delete; (b) the docs proliferation is real and *by design* (evidence-docs policy), now bounded by the taxonomy above; (c) the template-vs-instance doc overlap is intentional and now explicitly labelled. Execution is repo-structure-plan §5's reorg checklist (post-merge), unchanged except the two added delta docs.

---

## 4. UNVERIFIED flags

1. **Branch-state divergence.** `wt-fb2` is based at `96da568`; `main` has advanced to `3621fd9` (merged providers/ipc and other research). Files cited from `main`/other branches were read read-only from their branches — the final merged `main` was **not** the worktree base for this document. The citations are by stable path and should resolve post-merge; re-verify after the merge lands.
2. **D8a CLI is a spec, not code.** The `python -m cambium.modules.<name>` wrapper does not exist yet anywhere; the scaffold test run (`uv run --python 3.14.7 --extra test pytest src/cambium/modules/example/tests/test_example_module.py -q` → **6 passed**, worktree `/tmp/opencode/cambium-fb2`, exit 0) exercises `decide()`/`metric()` only, not a CLI.
3. **D8f rate-limit defaults.** Token-bucket `rpm` values are UNVERIFIED — no provider rate-limit data was measured in this worktree (`cascade-design.md` §2.3's breaker defaults are likewise un-calibrated). `docs/research/provider-landscape.md` may inform the defaults at implementation time.
4. **D8e container claims.** No Docker/Firecracker container was run in this worktree; the claim that "the worker runs unchanged in a container" rests on the stdio IPC contract (`§5.1`) and the `wt-slice` stdio proof, not on a container execution.
5. **D8g conversation-store layout.** Whether `conversations.db` is separate from `events.db` is an open question (Q8g.1); the SQLite WAL durability claims are validated by `docs/research/sqlite-wal-durability.md` (a branch-local document at the wt-fb2 baseline, read read-only).
6. **`should_decompose` "regex = toy".** The critique's characterization is accepted as the design's deliberate v2 state (rule engine, DSPy seam at v2.1) — it is **not** a defect claim that needs fixing, so no verification was run beyond the scaffold tests above.
7. **Claim-1 counts.** The "30 tracked files / 66 post-merge" numbers are computed from `git ls-tree` unions in this worktree at commit time; re-run repo-structure-plan §5 step 1's `git ls-files | wc -l` on the real merged `main` and trust that number.

---

## 5. Changelog

| Version | Date | Change |
|---|---|---|
| 1.0.0 | 2026-08-09 | Initial: point-by-point assessment of the second external critique (14 claims); deltas D8a–D8g; repo-layout proposal (66-file post-merge tree); UNVERIFIED flags. |
