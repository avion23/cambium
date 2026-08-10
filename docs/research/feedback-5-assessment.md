# Cambium — Fifth External Critique: Assessment

**Version:** 1.0.0
**Date:** 2026-08-10
**Branch:** `wt-doc-fb5` (`/tmp/opencode/cambium-doc-fb5`)
**Status:** Assessment of the fifth external critique against the current state
(architecture v2.0.0 + adopted deltas D1–D8 + feedback-4 dispositions + merged
research + current `src/cambium/` modules). The orchestrator has already decided
the disposition of each claim; this document records the disposition, the reason,
and the citation that substantiates it.

**Sources read (read-only from `/home/ubuntu/cambium` @ main `e6d8bb1`):**
`docs/architecture/architecture.md` (v2.0.0, §5 IPC, §6 event store, §7 lifecycle,
§16.2 layout), `docs/research/v2-1-review.md` (M2 FD-3, M7 pool), `docs/research/
architectus-design.md` (context composition §3, failure table §6), `docs/research/
m1-canonicalization-plan.md`, `docs/research/feedback-4-assessment.md`,
`docs/research/worker-coldstart.md`, `docs/research/bench-harness-design.md`,
`docs/research/replay-restart-design.md`, `docs/research/README.md` (tiered index),
`docs/architecture/reviews/` (three v0.1 reviews), `implementation-plan.md`
(decision log), and the current modules under `src/cambium/`.

### 0.1 Verification convention

- **Every citation is a real repository-relative file/section.** Anything that could
  not be verified from the corpus is marked **UNVERIFIED** (§3) and attributed as
  orchestrator-provided context, not corpus evidence.
- The critique text itself is not yet in the corpus; claims are reproduced here from
  the orchestrator's disposition record. Claim numbers below match that record.
- Independent checks run for this assessment are cited with the exact command and
  outcome in §3.1.

---

## 1. Verdict table

| # | Claim | Disposition | Reason | Citation |
|---|---|---|---|---|
| 1 | Dedicated pipe on FD 3 for IPC (C-extension writes corrupt stdout) | **ADOPT** | The v2.1 review already made FD 3 a hard decision: stdout/stderr become ordinary captured logs, the wire schema and JSON-Lines framing do not change, and the protocol transport version is bumped once with all in-repo workers updated atomically. The review's "Do not negotiate between stdout and FD 3 at runtime" wording makes this the M2 wire change: protocol on FD 3 via `pass_fds`, stdout/stderr reserved for logs, and the arch §5.1 stdout-reservation reshim deleted. The second critique's independent agreement is orchestrator-provided (**UNVERIFIED**); the adoption stands on the Sol review's decision B. | `docs/research/v2-1-review.md` §2 decision B (lines 236–256), §3 M2 scope (lines 333–348); `src/cambium/ipc.py` (`read_message`/`write_message` framing unchanged); `src/cambium/supervisor.py` spawn (`start_new_session=True`, `pass_fds=()`, lines 1133–1142) |
| 2 | Single SQLite DB — events + conversations + blackboard as tables | **REJECT** | The corpus is built on separate single-writer SQLite WAL stores: `events.db` (§6.1), `conversations.db` (§6.6), and the proposed `shared.db` (architectus-design §3.6, justified explicitly as avoiding two writers to one database). The event log is the source of truth for restart (replay-restart-design §2.1 reconstructs lifecycle state from the log), while conversations are a mutable-queryable projection that is rebuildable from protocol events — so cross-store atomicity is not promised or needed. A documented tradeoff note for architecture §6 is a separate task (§2). | `docs/architecture/architecture.md` §6.1 (primary store `events.db`, separate conversation store), §6.6 (conversation store, single-writer discipline); `docs/research/v2-1-review.md` §2 decision C (lines 258–269: "the event is the durable fact, and conversation projection is rebuildable from protocol events"); `docs/research/replay-restart-design.md` §2.1 (lines 60–73); `docs/research/architectus-design.md` §3.6 (lines 378–430) |
| 3 | Delete `events.py` + `orchestrator.py` | **ALREADY-PLANNED** | The M1 canonicalization plan already lists both as DELETE: the `events.py` seed dataclasses (row 13, conformance M4 / constitution §2(l)) and the `Orchestrator.run` submit/drain skeleton (row 4). M1 Step 4 executes the deletions with a `git grep "cambium.events"` cleanliness gate; the DELETE class counts 14 rows total. | `docs/research/m1-canonicalization-plan.md` §2.1 row 4, §2.2 row 13, inventory counts (lines 117–125), §4 Step 4 (lines 216–226), §3 target state (lines 129–138); `docs/research/v2-1-review.md` §5 items 3–4 (lines 503–509) |
| 4 | Delete `system-design.md` | **REJECT** | `system-design.md` is the immutable v0.1 origin record: architecture v2 supersedes but does not delete it (§0 status, §20 references "v0.1 draft (superseded)"), and the resolution matrix still cross-references it (§18.4 "Consensus items (`system-design.md` §9 table)"). All three adversarial reviews document it as their reviewed baseline (`SYSTEM_DESIGN.md` v0.1.0-draft). The v2.1 review is explicit: "Keep it immutable for history." Historical status is established by arch §20 and v2-1-review §5, not by the research tiered index (which governs `docs/research/` only and does not list it). | `docs/architecture/system-design.md`; `docs/architecture/architecture.md` §0 (lines 5–6), §18.4 (lines 1222–1225), §20 (lines 1271–1274); `docs/architecture/reviews/review-distributed-systems.md` line 5, `review-implementation.md` line 5, `review-llm-design.md` line 4; `docs/research/v2-1-review.md` §5 (lines 516–518) |
| 5 | Delete `fake_worker.py` + `crash_worker.py`; use `-c` injection | **PARTIAL** | The M1 plan migrates the production default to `python -m cambium.worker` while keeping `scripts/fake_worker.py` as a fixture only (row 20, v2-1-review §5: "Keep `scripts/fake_worker.py`, but only as a test fixture"); grep confirms it is referenced only from tests and docstrings. `crash_worker.py` **stays** as a test fixture (row 22): it is the T3 worktree-recovery proof — generation 1 commits an edit then crashes, and only a real `git reset --hard base_commit` recovery between respawns keeps generation 2's result clean. A `-c`-injected inline worker cannot carry that crash-then-succeed lifecycle. | `docs/research/m1-canonicalization-plan.md` §2.4 rows 20–22 (lines 74–77), §4 Step 5 (lines 228–236); `docs/research/v2-1-review.md` §5 (lines 524–526); `tests/fixtures/crash_worker.py` (docstring, lines 1–11); `tests/scenarios/test_supervisor_fanout.py` (T3); `rg -l "fake_worker" src/ tests/` → only `supervisor.py`/`worker.py` docstrings |
| 6 | Envelope trimming — worker stops echoing generation / monotonic_ms / ts | **PARTIAL** | The supervisor already stamps the authoritative `ts` (`time.time()`) and `monotonic_ms` on every event at the event loop (`emit`, lines 799–812) and `seq` is reserved by the store's single writer (store `append`), so worker-side `ts` echoing is neither present nor needed. The worker **keeps** echoing `generation` — the fencing requirement (arch §7.3: generation echoed in `ready`/`heartbeat`/`checkpoint`/`result`/`error`/`exit`; the worker compares the worktree fence file on every git op) — and `request_id` (the §5.1 correlation key). `monotonic_ms` stays as cheap liveness evidence (heartbeat/`ok`/`exit`); it is optional, not load-bearing. | `src/cambium/supervisor.py` `emit` (lines 799–812); `src/cambium/store.py` `append` seq reservation (lines 123–149); `src/cambium/worker.py` (generation echoes lines 227–235, 274–276, 289–317; `_monotonic_ms` line 77); `docs/architecture/architecture.md` §7.3 (lines 617–627), §5.1 framing (lines 281–282) |
| 7 | Use `graphlib.TopologicalSorter` instead of the hand-rolled Kahn | **REJECT** | Our Kahn implementation names the cycle path, which is the product requirement: `_find_cycle` returns the `[a, b, c, a]` trace and both `build_tree` and `topological_order` raise `CycleError("cycle in task DAG: a -> b -> a")`. `graphlib.TopologicalSorter` also raises a `CycleError` but does **not** expose the cycle path, so it cannot drive the "reject and re-prompt the decomposer with the cycle named" flow (arch I2.2, DS-M6). It is ~20 lines (`_find_cycle` + `topological_order`) and covered by 29 scenario tests. Note the alternative in a comment (follow-up, §2). | `src/cambium/tasktree.py` `_find_cycle` (lines 172–206), `CycleError` (lines 114–115, 318–320, 373–376), `topological_order` (lines 350–377); `tests/scenarios/test_tasktree.py` (29 tests, `rg -c`); `docs/architecture/architecture.md` §3.7 I2.2, §18.1 DS-M6 |
| 8 | Sandboxing is security theater; use containers at deployment | **ALREADY-IMPLEMENTED** | There is no sandbox in the harness: the Septum module (M8) was removed from v2 scope (decision 10/D7) and containment is worktree isolation + permission allowlists + approval gates (§7.2). Deployment isolation via containers/microVMs is explicitly the host's job (D8e: "Deployment isolation is the host's job … a host wraps the process and connects the pipes"). The implementation-plan decision log records the same posture verbatim. | `docs/architecture/architecture.md` §0 (lines 12, 20), §2 (lines 76–77), §4 M8 (line 265), §7.2 (lines 610–615), §18.4 F12; `implementation-plan.md` decision 5 (line 23); `docs/research/feedback-2-deltas.md` D8e |
| 9 | cgroups via `systemd-run` for OOM containment + a `wait_for_resources` tool | **ADOPT-LITE** | The agent-level resource gate already exists: `resources.py` `CompileGate` bounds concurrent compile-heavy gate commands (semaphore, 60 s acquire timeout) and `system_health.py` `can_run_heavy` checks memory/load/disk before heavy ops. `systemd-run` cgroups for OOM containment are a **deployment-layer** option — the host's responsibility per D8e — so record them as a deployment note (M3 scope in architecture) rather than harness code. | `src/cambium/resources.py` (`CompileGate`, lines 58–158); `src/cambium/system_health.py` (`can_run_heavy`, lines 148–189); `docs/architecture/architecture.md` §7.2 D8e (line 615); `docs/research/v2-1-review.md` §3 M3 (lines 349–364) |
| 10 | Global pacing scheduler / PID token pacing to hit provider reset windows | **ADOPT-LITE** | The per-provider token buckets (`rpm` refill, empty bucket → `RATE_LIMITED`, skipped via the same filter as cooldown) and queue-level pause-on-total-exhaustion with a recovery monitor already exist (D8f, arch §7.4/§9.2; implemented in `diffundo.py`). PID (process-ID) pacing as a global scheduler to align with provider reset windows is recorded as an open v2.1 enhancement. | `docs/architecture/architecture.md` §7.4 (lines 648–649), §9.2 (lines 835–860); `docs/research/feedback-2-deltas.md` D8f; `src/cambium/diffundo.py` (token buckets lines 9–18, 253; provider state lines 283–290) |
| 11 | Core Directive — a ≤200-token root goal injected into every sub-agent system prompt | **ADOPT** | Architectus context composition already specifies a byte-stable static prefix (§3.1: system prompt, AGENTS.md guidelines, tool definitions, module instructions) compiled once and reused across turns for provider exact-prefix cache hits. The root's unalterable goal is a natural additional static-prefix segment: fixed ≤200 tokens, at the top, never churned by dynamic content (D8c forbids volatile tokens in the prefix). A separate implementation task owns the wiring (§2). | `docs/research/architectus-design.md` §3.1 (lines 279–298), §3.2; `docs/research/feedback-2-deltas.md` D8c (static-before-dynamic layout) |
| 12 | Step-back — 3 gate failures → `git reset --hard`, restart from base; `evaluate_goal` tool | **ADOPT-LITE** | The mechanics already exist: worktree recovery hard-resets to `base_commit` before every respawn (`_recover_worktree_locked`: `reset --hard <base_commit>` + `clean -fd`, arch §7.5), and gate failures are bounded by `gate_max_retries` (default 2, D4/§7.9). The remaining codification is one failure-table row — "3 gate failures → reset worktree and retry once, then abort subtree" — in architectus-design §6, plus the clarification that `evaluate_goal` is the existing gate (arch §7.1 GATING), not a new tool. | `src/cambium/supervisor.py` `_recover_worktree_locked` (lines 939–956); `docs/architecture/architecture.md` §7.5 (lines 651–666), §7.9 (lines 763–770), §7.1 GATING; `docs/research/architectus-design.md` §6 rows 3–4 (lines 673–695) |
| 13 | Serialized JSON TaskInput fixtures for DSPy evals (no supervisor) | **ALREADY-IMPLEMENTED** | The frozen JSONL fixtures exist and are the eval input surface: `train.jsonl`/`eval.jsonl`/`canaries.jsonl` under `src/cambium/modules/example/datasets/` with `eval_frozen_at`/`canary_frozen_at` markers in `meta.json`, loaded by `ExampleDatasetLoader` without any supervisor. The mock-git eval environment that wraps them for M8 optimization is designed in bench-harness-design §8 (mock env §8.1, AST-assert §8.2) but is **DRAFT/not implemented** (§8 status note) — the fixture half is what is implemented today. | `src/cambium/modules/example/datasets/{train,eval,canaries}.jsonl` + `meta.json` (`eval_frozen_at`/`canary_frozen_at`, lines 4–5); `src/cambium/modules/example/dataset.py`; `docs/research/bench-harness-design.md` §8 (lines 392–564); `docs/research/v2-1-review.md` §3 M8 (lines 436–449) |
| 14 | DSPy for prompts/few-shots; DLQ mining for tool-call demos | **ADOPT** | M8 (DSPy `should_decompose` refinement with SIMBA) is the adopted refinement loop. The DLQ is real and merged (`dlq.py`: bounded, durable, redacted dead-letter records, keep-newest 1,000-entry cap) — the review's M2 "bounded redacted DLQ" surface. The refinement loop gains DLQ-sourced **corrected** trajectories (worker failures, gate evidence) as few-shot demonstrations for the optimizer, which is a natural fit for the decide-seam harness state. | `src/cambium/dlq.py` (`DeadLetterQueue`, lines 47–53, 77–94); `docs/research/v2-1-review.md` §3 M8 (lines 436–449), §1.3 P0 gap 8 (lines 171–176); `docs/research/architectus-design.md` §4.2 (decide seam, lines 501–520) |
| 15 | Only lock = Unio's merge lock | **ALREADY-IMPLEMENTED** | The merge path is serialized by Unio's `asyncio.Lock` held across verify-in-throwaway-worktree and atomic `update-ref` publish — the only merge lock (arch §7.8, event-schema-draft §3.11 `merge_started`). The concurrency semantics it protects were verified empirically in `worktree-concurrency.md` (exactly one winner, 0 lost updates, no corruption). The `threading.Lock` in `store.py` guards the event writer's queue, not a second merge. | `docs/architecture/architecture.md` §7.8 (lines 712–761); `docs/research/worktree-concurrency.md`; `docs/research/event-schema-draft.md` §3.11; `docs/research/feedback-4-assessment.md` claim 11 (line 42) |
| 16 | Batch tool executions — CRITICAL | **ALREADY-IN-DESIGN** | Speculative batched `read_file` calls (up to N candidate files in one model response, executed concurrently, results appended in call order) are already designed in architectus-design §4.4, with a falsifiable ≥30% latency bar at M6 and the 60% headline figure left UNVERIFIED. It is a worker-loop efficiency note (S4), not a protocol change; the sequential per-tool heartbeat loop (§7.6) remains the v2 behavior. | `docs/research/architectus-design.md` §4.4 (lines 538–566), §8 chunk S4; `docs/architecture/architecture.md` §7.6 (lines 670–695) |
| 17 | Semantic code search — `get_signature` | **ALREADY-IMPLEMENTED** | `ast_tools.py` ships `extract_signature` (tree-sitter backend with stdlib `ast` fallback) plus `find_definitions`/`find_references` — the AST/symbol search the v2 tool set deliberately deferred ("No AST/symbol search. Planned for v2.1", arch §11). Tool dispatch with schema validation is merged (`tools.py`, 74ff5aa, using `schemas.py` `TOOL_SCHEMAS`/`validate_tool_call`), but `extract_signature` is **not yet exposed** in `TOOL_SCHEMAS` — the wiring is the remaining task. | `src/cambium/ast_tools.py` `extract_signature` (lines 370–380), `find_references` (lines 357–367); `src/cambium/tools.py` (74ff5aa, merged); `src/cambium/schemas.py` (`TOOL_SCHEMAS`, `validate_tool_call`); `docs/architecture/architecture.md` §11 (lines 915–919) |
| 18 | Drop `unified_diff` from the upward payload | **REJECT** | The diff stays in the upward envelope by default: it is the merge-conflict context the evaluator tier and merge-conflict resolution need, it is capped (64 KiB + `diff_truncated`), and dropping it by default would delete the I2.7/D8b envelope's primary review surface. The critique's underlying concern is already handled by the adopted `include_diff` config flag (fb4 claim 21, ADOPT-LITE): `include_diff: false` omits the field for higher orchestrator tiers while the evaluator tier keeps it default-on and the diff stays available on demand for `merge_failed` resolution. | `docs/research/feedback-4-assessment.md` claims 7 and 21 (lines 38, 52); `docs/architecture/architecture.md` §3.4 (`unified_diff` ≤64 KiB, `diff_truncated`, lines 173, 188); `docs/research/architectus-design.md` §3.7 (lines 432–470); `src/cambium/tasktree.py` `_ENVELOPE_KEYS` (lines 49–60) |
| 19 | Pre-warmed worker pool — 3 READY workers, background refill | **ADOPT** | The measured cold-start gap is the spec's justification: ~2.22 s per DSPy worker and ~7.0 s for a 10-worker fan-out vs 5.6 ms per warm fork and ~39 ms fan-out (worker-coldstart.md). M7 already mandates a pre-spawned reusable subprocess pool (p50 <100 ms / p90 <250 ms task-ready after warmup; retire-on-any-doubt reset; never warm `os.fork` from the threaded supervisor). The critique's "refill-on-consume" and 3-READY concurrency are M7 implementation details; the pool is the release gate for `max_width >= 4`. | `docs/research/worker-coldstart.md` §Conclusion (lines 106–131, tables lines 30–96); `docs/research/v2-1-review.md` §2 decision D (lines 271–282), §3 M7 (lines 419–434); `docs/architecture/architecture.md` §14 (lines 999) |

**Counts:** 19 rows — **REJECT 4** (2, 4, 7, 18) · **ADOPT 4** (1, 11, 14, 19) ·
**ADOPT-LITE 3** (9, 10, 12) · **ALREADY-IMPLEMENTED 4** (8, 13, 15, 17) ·
**ALREADY-PLANNED 1** (3) · **ALREADY-IN-DESIGN 1** (16) · **PARTIAL 2** (5, 6).

---

## 2. What this means for the plan

Module-state is as of main `e6d8bb1`. The dispositions create these concrete follow-ups:

1. **FD-3 IPC wire change (claim 1, ADOPT/M2).** Protocol moves to an inherited FD 3
   via `pass_fds`; stdout/stderr become captured logs. One transport bump, all in-repo
   workers (including the fixtures `fake_worker.py`/`crash_worker.py` and the worker
   pool) updated atomically; the arch §5.1 stdout-reservation reshim is deleted. This
   is v2-1-review M2 scope (`v2-1-review.md` lines 333–348), sequenced after M1.
2. **Core Directive task (claim 11, ADOPT).** Add the root's unalterable ≤200-token
   goal as a static-prefix segment in architectus-design §3.1 — byte-stable, at the
   top, before the dynamic tail. Separate implementation task; the prompt-lint (D8c)
   must keep it volatile-token-free.
3. **Failure-table reset row (claim 12, ADOPT-LITE).** Codify "3 gate failures → reset
   worktree to `base_commit` and retry once, then abort subtree" as a row in the
   architectus-design §6 decision table (task in flight on `wt-doc-architectus`); the
   `evaluate_goal` tool is identified as the existing gate, not a new surface.
4. **M8 DLQ-sourced few-shot demos (claim 14, ADOPT).** The refinement loop
   (v2-1-review M8) gains corrected trajectories mined from `DeadLetterQueue` records
   as few-shot demonstrations behind the `decide` seam.
5. **M3 cgroups deployment note (claim 9, ADOPT-LITE).** Record `systemd-run` cgroup
   OOM containment as a deployment-layer option in the architecture's containment
   note (§7.2/D8e area); the harness-side `wait_for_resources` already exists
   (`resources.py` `CompileGate` + `system_health.py`).
6. **M7 pool spec (claim 19, ADOPT).** The persistent worker pool is specified by
   worker-coldstart.md numbers + v2-1-review M7; the critique's 3-READY / refill-on-
   consume concurrency is an M7 implementation detail to record in the pool spec.
7. **Architecture §6 tradeoff note (claim 2, REJECT).** Document the deliberate
   tradeoff: separate single-writer DBs (`events.db` / `conversations.db` /
   `shared.db`) over one consolidated SQLite file — contention avoidance and
   one-writer-per-DB at the cost of no cross-store atomicity (none required: the
   event is the durable fact, projections rebuild). Separate task.
8. **`graphlib` alternative comment (claim 7, REJECT).** Add a comment in
   `tasktree.py` noting `graphlib.TopologicalSorter` was considered and rejected
   because its `CycleError` does not name the cycle path that I2.2/DS-M6 re-prompting
   requires.
   **Follow-up status:** Done in commit `<COMMIT_SHA>`.
9. **AST tool wiring (claim 17).** Expose `ast_tools.extract_signature` /
   `find_references` in `tools.py` `TOOL_SCHEMAS` — the remaining wiring after the
   `tools.py` merge (74ff5aa).

No disposition requires reverting an existing adopted delta or a prior feedback-4
disposition. The four REJECTs preserve the one-writer-per-DB store split, the
historical `system-design.md` record, the cycle-path-naming Kahn, and the default-on
upward diff.

---

## 3. UNVERIFIED flags

- **The critique text itself.** As with feedback 4, the fifth critique's text is not in
  the corpus; claims are reproduced from the orchestrator's disposition record. Claim
  wording above that goes beyond the record (e.g., the "stdout corruption by
  C-extension writes" motivation for claim 1) is orchestrator-provided context.
- **"Second critique + Sol review agree" on FD-3 (claim 1).** The Sol review's
  decision B is verified; the second critique's independent FD-3 agreement is **not**
  in the corpus — `rg -n "FD-3|fd 3" docs/research/feedback-2-deltas.md` returns no
  hit. The adoption stands on `v2-1-review.md` decision B alone.
- **PID pacing as a documented v2.1 enhancement (claim 10).** `rg -rn "pacing"`
  across `docs/` and `src/` returns no hit. The existing half (token buckets +
  pause-on-exhaustion, D8f) is verified in `diffundo.py` and arch §7.4/§9.2; the "PID
  pacing documented as an open v2.1 enhancement" assertion is orchestrator-provided
  and must be added to the v2.1 research-questions list when that claim is recorded.
- **`include_diff` arch note is not in main (claim 18).** The normative §3.4 note
  quoted by architectus-design §3.7 lives on branch `wt-doc-difflag` (commit
  `16e61cf`, "docs(architecture): include_diff envelope flag"); `git merge-base
  --is-ancestor 16e61cf e6d8bb1` fails, so main's arch §3.4 (lines 163–188) carries
  the 64 KiB cap and `diff_truncated` but not the flag text. The disposition is
  plan-level until that branch merges.
- **Mock-git eval env is DRAFT (claim 13).** bench-harness-design §8 states "Status:
  DRAFT. Not implemented." The frozen TaskInput fixtures are implemented; the mock
  git environment and AST-assert scoring that wrap them for M8 are design until an M8
  run exists (§8.3: "UNVERIFIED until M8 runs").
- **Batch-tool latency figures (claim 16).** The critique's "CRITICAL" framing and
  the adopted ≥30% latency target are design-level; architectus-design §4.4 explicitly
  marks the 60% headline claim UNVERIFIED and falsifies the feature at M6.
- **Pool concurrency shape (claim 19).** The specific "3 READY workers" and
  "background refill" numbers are the critique's proposal; M7's acceptance criteria use
  p50 <100 ms / p90 <250 ms after warmup. worker-coldstart.md's 2.22 s / 5.6 ms / 7.0 s /
  39 ms figures are measured on this host under `loadavg` 2.6–8.7.
- **AST tool not yet wired (claim 17).** `extract_signature`/`find_references` are
  implemented and merged, and `tools.py` dispatch is merged (74ff5aa), but no
  `TOOL_SCHEMAS` entry exposes the signature tool yet — verified by `rg -n
  "extract_signature" src/cambium/tools.py` returning no hit.

### 3.1 Checks run for this assessment

- `git rev-parse HEAD` → `e6d8bb1`; `git worktree add -b wt-doc-fb5` clean. `git log
  --oneline -12` shows the luna/bench/doc merges (incl. `74ff5aa` tools, `790f470`
  luna-tools, `a9d59c9` luna-convtok).
- `rg -c '^((async )?def test_)' tests/scenarios/test_tasktree.py` → **29**. Supports
  claim 7.
- `rg -n "extract_signature" src/cambium/ast_tools.py` → line 370 (`def
  extract_signature`); `rg -n "extract_signature|ast_tools" src/cambium/tools.py` → **0
  hits** (tool not wired). Supports claim 17.
- `rg -n "include_diff" docs/architecture/architecture.md` → **no hit on main**;
  `git merge-base --is-ancestor 16e61cf e6d8bb1` fails (note on `wt-doc-difflag`).
  Supports claim 18 UNVERIFIED.
- `rg -rn "pacing" docs/ src/` → **no hit** (only a test-strategy typo). Supports
  claim 10 UNVERIFIED.
- `src/cambium/modules/example/datasets/meta.json` carries `eval_frozen_at:
  "2026-08-09"` and `canary_frozen_at: "2026-08-09"`. Supports claim 13.
- `rg -ln "fake_worker" src/ tests/ scripts/` → `crash_worker.py`, three test
  scenario files, and `supervisor.py`/`worker.py` (docstring/comment references only);
  `rg -ln "crash_worker" src/ tests/` → `test_supervisor_fanout.py` only. Supports
  claim 5 (fixture-only).
- `src/cambium/supervisor.py` `emit` (lines 799–812) stamps `ts`/`monotonic_ms` at the
  event loop; `store.py` `append` (lines 123–149) reserves `seq`; `_recover_worktree_
  locked` (lines 939–956) runs `reset --hard <base_commit>` + `clean -fd`. Supports
  claims 6, 12.
- `src/cambium/{dlq,resources,system_health,diffundo,tools,schemas}.py` all present on
  main. Supports claims 9, 14, 17.
- Reviews cite the v0.1 record: `review-distributed-systems.md:5`,
  `review-implementation.md:5`, `review-llm-design.md:4` (`SYSTEM_DESIGN.md`
  v0.1.0-draft). Supports claim 4.
