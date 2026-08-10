# Cambium — Fifth External Critique: Assessment

**Version:** 1.0.0
**Date:** 2026-08-10
**Branch:** `wt-doc-fb5` (`/tmp/opencode/cambium-doc-fb5`)
**Snapshot source:** `/home/ubuntu/cambium` `main@e6d8bb1`.
**Status:** Historical disposition of 19 claims against architecture v2.0.0, D1–D8,
feedback-4, research, and source modules. Current readers use `docs/architecture/architecture.md`,
`src/cambium/`, and `docs/research/v2-1-status.md`.

**Historical snapshot / current pointer:** provider loop, Diffundo, EventStore, and root
`Result` exist; DLQ, eval cache, ResourceBudget, `worker_pool`, and `events` are absent; no
per-worker sandbox or production shell approval exists, and dynamic hierarchy is absent. The
old branch module/commit claims below are evidence, not current-main status.

## 0. Evidence convention

The critique text was not committed; claim numbers reproduce the orchestrator disposition. All
citations are repository-relative or explicitly marked **UNVERIFIED**. Sources included
architecture §§5–7/16.2, `v2-1-review.md` §4 (M2/M7), `architectus-design.md`,
`m1-canonicalization-plan.md`, feedback-4, worker-coldstart, bench/replay research, the three
v0.1 reviews, and `implementation-plan.md`.

## 1. Verdict table

| # | Claim | Disposition | Retained reason/evidence |
|---|---|---|---|
| 1 | Dedicated FD 3 IPC | **ADOPT** | v2.1 review decision B moves protocol to inherited FD 3; stdout/stderr become captured logs; one transport bump, no runtime negotiation (`v2-1-review` §3B/§4 M2; `ipc.py`; supervisor spawn). |
| 2 | One SQLite DB for events/conversations/blackboard | **REJECT** | Separate single-writer `events.db`, `conversations.db`, proposed `shared.db`; event is durable fact and projections rebuild (`architecture` §§6.1, 6.6; `v2-1-review` §3C; replay-restart §2.1; architectus-design §3.6). |
| 3 | Delete `events.py` + `orchestrator.py` | **ALREADY-PLANNED** | M1 canonicalization rows 4/13 and Step 4 delete both with `git grep` gate (`m1-canonicalization-plan.md` §§2.1–2.2, 3, 4; v2-1-review §5). |
| 4 | Delete `system-design.md` | **REJECT** | Immutable v0.1 origin record, cross-referenced by architecture §20/resolution matrix and all three reviews; v2-1-review §5 says keep immutable. |
| 5 | Delete fixtures/use `-c` injection | **PARTIAL** | `fake_worker.py` remains fixture-only; `crash_worker.py` is needed for T3 crash/recovery proof and real reset between generations (`m1-canonicalization-plan.md` §2.4/§4 Step 5; `tests/fixtures/crash_worker.py`; `test_supervisor_fanout.py`). |
| 6 | Trim generation/monotonic/ts from worker envelope | **PARTIAL** | Supervisor stamps authoritative `ts`/`monotonic_ms`, store reserves `seq`; generation remains fencing data and request ID remains correlation (`supervisor.py` emit; `store.py` append; `worker.py`; architecture §§5.1, 7.3). |
| 7 | Use `graphlib.TopologicalSorter` | **REJECT** | Hand Kahn provides deterministic order/message control and is covered by 29 tests; correction retained: Python 3.14.7 `CycleError.args[1]` does expose a path (`tasktree.py`; `test_tasktree.py`; DS-M6/I2.2). |
| 8 | Sandbox is theater; containers at deployment | **ALREADY-IMPLEMENTED (historical disposition; current boundary: PARTIAL)** | D7 removed Septum. An optional `ApprovalGate` primitive exists, but no production approval callback or per-worker OS sandbox exists; worktrees/allowlists are harness controls and containers/microVMs are host-owned D8e (`architecture` §§0,2,4,7.2,18.4; implementation-plan decision 5; feedback-2 D8e). |
| 9 | cgroups + `wait_for_resources` | **ADOPT-LITE** | `resources.py:CompileGate` and `system_health.py:can_run_heavy` are agent-side gates; `systemd-run` is a deployment note (`v2-1-review` §4 M4; architecture §7.2). |
| 10 | Global PID pacing | **ADOPT-LITE** | D8f token buckets/pause already exist in the snapshot Diffundo; PID reset-window pacing is a v2.1 enhancement (`diffundo.py`; architecture §§7.4, 9.2; D8f). Addendum later supersedes PID with GCRA F6-04. |
| 11 | ≤200-token root Core Directive | **ADOPT** | Static prefix segment in Architectus context, before dynamic content (architectus-design §3.1/§3.2; D8c); wiring was a separate task. |
| 12 | Three gate failures → reset/retry; `evaluate_goal` | **ADOPT-LITE** | Existing reset/clean before respawn and bounded `gate_max_retries`; codify reset once then abort and treat `evaluate_goal` as the existing gate (supervisor `_recover_worktree_locked`; architecture §§7.1, 7.5, 7.9; architectus-design §6). |
| 13 | Serialized JSON TaskInput fixtures | **ALREADY-IMPLEMENTED** | Frozen train/eval/canary JSONL and timestamps exist without supervisor; mock-git/AST wrapper is DRAFT M8 (`datasets/*`, `meta.json`, `dataset.py`, bench §8). |
| 14 | DSPy prompts/few-shots + DLQ mining | **ADOPT** | M8 SIMBA refinement may mine corrected DLQ trajectories. The old snapshot said DLQ was merged; current note says DLQ is absent, so this remains plan-level (`dlq.py` then; `v2-1-review` §4 M8; architectus-design §4.2). |
| 15 | Only lock is Unio merge lock | **PARTIAL** | The narrow Unio publication lock spans verify/publish, but store `threading.Lock` and other synchronization also exist; “only lock” is false. Preserve the merge-publication lock as the intended boundary (`architecture §7.8`; event-schema §3.11; worktree-concurrency; feedback-4 claim 11). |
| 16 | Batch tool executions | **ALREADY-IN-DESIGN** | Architectus §4.4 proposes speculative `read_file` batching with ≥30% M6 bar; sequential heartbeat loop remains v2. |
| 17 | Semantic code search `get_signature` | **ALREADY-IMPLEMENTED (snapshot partial wiring)** | `ast_tools.py` has `extract_signature`/references and schemas/dispatch merged, but schema exposure was the remaining task at the original assessment (`ast_tools.py`; `tools.py` 74ff5aa; `schemas.py`; architecture §11). |
| 18 | Drop upward `unified_diff` | **REJECT** | Keep 64 KiB diff + `diff_truncated`; `include_diff:false` is optional for higher tiers and default-on for evaluator (`feedback-4` claims 7/21; architecture §3.4; architectus-design §3.7; tasktree envelope keys). |
| 19 | Pre-warmed pool, 3 READY/refill | **ADOPT** | Cold-start measurement ~2.22 s/worker and ~7.0 s/10 versus warm 5.6 ms/39 ms; M7 makes reusable pool the production fan-out gate (`worker-coldstart`; `v2-1-review` §§3D, 4 M7; architecture §14). |

**Counts:** REJECT 4 (2,4,7,18) · ADOPT 4 (1,11,14,19) · ADOPT-LITE 3 (9,10,12) ·
ALREADY-IMPLEMENTED 3 (8,13,17) · ALREADY-PLANNED 1 (3) · ALREADY-IN-DESIGN 1 (16) ·
PARTIAL 3 (5,6,15). The original record also listed claim 17 as remaining wiring; the addendum below
records completion.

These are snapshot dispositions. Claim 8 is not a current implementation claim: Septum was
removed, an optional `ApprovalGate` primitive exists, but no production approval callback or
per-worker OS sandbox exists. Source and tests, not the status tracker, control current findings.
Claims 13 and 17 retain implementation labels only for their cited fixture and tool surfaces;
claim 15 is partial because the merge-publication lock is not the only synchronization lock.

## 2. Plan consequences

Retained follow-ups: FD-3 M2 transport; Core Directive/static-prefix lint; reset/retry row;
M8 corrected DLQ demos; M3 deployment cgroups; M7 pool; architecture §6 separate-DB tradeoff;
graphlib alternative comment (`9b071e0` recorded done); and AST tool schema/dispatch wiring
(`74ff5aa`, then `6ff9c42`/`b941c81`). No disposition reverts adopted deltas, separate stores,
historical `system-design.md`, cycle-path Kahn, or default-on diff.

## 3. UNVERIFIED flags and snapshot checks

Unverified: critique text and its independent FD-3 agreement; PID pacing documentation; initial
`include_diff` note; mock-git eval; batch latency headline; pool shape; tree-sitter version; and
the original `extract_signature` wiring. Snapshot checks included `git rev-parse` → `e6d8bb1`,
29 task-tree tests, `extract_signature` present but absent from `tools.py`, no `include_diff`/GCRA
hits at that baseline, frozen dataset timestamps, fixture-only worker references, and merge-base
checks showing `ast_tools.py`/`schemas.py` present while `system_health.py` `d4db2ff`,
`eval_cache.py` `d8f9408`, `lint_diag.py` `2d26e5f` were not.

## 4. Addendum — current-main re-check (2026-08-10)

The addendum was checked at `main@17dfcd3` in `/home/ubuntu/wt-f5-addendum`; it preserves the
historical §§1–3 text and changes only these items:

- **F5-10:** PID pacing is superseded/rejected by feedback-6 F6-04: adopt session-global
  GCRA-style pacing (`max_in_flight`, 60/rpm, per-retry permits; verified `Retry-After` only),
  reject knapsack/PID. `implementation-plan.md:41` recorded it OPEN and not implemented.
- **F5-17:** `get_signature` is wired: `schemas.py` `TOOL_SCHEMAS` lines 120/166–184,
  `tools.py` `TOOL_DISPATCH` line 680/685, validation in `run_tool`; commits `6ff9c42` and
  `b941c81` are ancestors. F6-12 remains PARTIAL for other AST functions.
- **F5-18:** `include_diff` note landed in architecture §3.4 via `16e61cf`/`66e5a16`, and
  `glossary.md:43` records it.
- **Follow-ups:** Core Directive and reset/retry rows are codified in `architectus-design.md`
  and `architectus.py` via `8dd1aee`; supervisor wiring remains OPEN.

Addendum checks: `git merge-base --is-ancestor` succeeded for `16e61cf`, `6ff9c42`, `b941c81`,
`8dd1aee`; `rg` found `get_signature` in schemas/tools and `include_diff` in architecture;
GCRA appeared only in `implementation-plan.md`; `git diff --check` was clean.

## 5. Detailed rationale retained

### Claims 1–8: transport, stores, fixtures, and graph validation

FD 3 is a transport decision, not a new wire schema: `ipc.read_message`/`write_message` bytes
stay JSON-Lines, but an inherited descriptor prevents C-extension or provider progress text from
contaminating stdout. The review explicitly says not to negotiate stdout versus FD 3 at runtime;
all in-repo workers, fixtures, wrappers, and tests must move once. The snapshot supervisor still
showed `pass_fds=()` and is therefore historical evidence of work remaining, not proof of FD 3.

The one-DB proposal was rejected for causal reasons. Events are append-only durable facts used by
replay (`replay-restart-design.md` §2.1); conversations and blackboard are rebuildable,
query-heavy projections. Separate single-writer databases avoid contention and do not promise
cross-store atomicity. This preserves architecture §6.1/§6.6 and `v2-1-review` §3C. The
canonicalization plan's deletion of `events.py`/`orchestrator.py` is already planned, but the
historical audit retains both until the M1 cleanliness gate.

`fake_worker.py` is useful as a fixture because it proves JSONL protocol behavior; `crash_worker.py`
is not replaceable by an inline `-c` string because T3 needs generation 1 to commit and crash,
then a real `git reset --hard base_commit` before generation 2. Envelope trimming is likewise
selective: supervisor owns authoritative timestamps and store owns seq; generation is required
for fencing and request ID for correlation; monotonic milliseconds are cheap liveness evidence.

The graphlib correction matters. `CycleError.args[1]` does expose a cycle list on Python 3.14.7,
so Kahn is not retained because graphlib cannot name a cycle. It remains because the hand-rolled
validator provides deterministic order, stable error text, and the exact re-prompt contract for
I2.2/DS-M6. The 29 task-tree tests cover cycle path, self-loop, depth/width, order, and envelope.

### Claims 9–12: resources, pacing, directive, and reset

Compile-resource control has two layers: `resources.CompileGate` bounds compile-heavy commands,
and `system_health.can_run_heavy` checks memory/load/disk before work. `systemd-run` cgroups are
not a new harness dependency; D8e leaves OOM isolation to a host deployment. D8f's token bucket
and queue pause are real design/branch behavior, but addendum F6-04 rejects PID reset-window
pacing in favor of a session-global GCRA-style pacer (`max_in_flight`, 60/rpm, per-retry permits,
verified `Retry-After`). The GCRA pacer was still OPEN and not implemented at addendum time.

The Core Directive is the first static-prefix segment and is capped at 200 tokens; it must not
churn when dynamic task context changes (D8c). The reset/retry row is a bounded recovery action,
not a new evaluate tool: three gate failures reset to base, retry once, then abort the subtree;
`evaluate_goal` is the existing GATING contract. Addendum `8dd1aee` codified the Architectus
state machine, but supervisor wiring remained open.

### Claims 13–19: eval, DLQ, AST, diff, and worker pool

Frozen JSONL fixtures are safe for an eval-only cache because `eval_frozen_at` and
`canary_frozen_at` make prompts/data immutable; D1's production no-cache remains intact. The
mock-git environment and AST-assert scoring in `bench-harness-design.md` §8 are DRAFT, so only
the fixture half is “implemented.” The snapshot's DLQ claim came from a branch that described a
bounded redacted queue; the current pointer explicitly says DLQ is absent, so D8/M8 DLQ mining
is plan-level rather than a current security control.

`ast_tools.py` has tree-sitter and stdlib AST fallback, but the first assessment did not expose
`extract_signature` through `TOOL_SCHEMAS`. The addendum verifies `get_signature` in schemas and
`TOOL_DISPATCH` (`6ff9c42`, hardened `b941c81`), while `find_definitions`/`find_references` remain
library functions. `include_diff` now has a normative architecture note via `16e61cf`/`66e5a16`:
higher tiers may omit it, but merge-failure resolution can request it and evaluator keeps it on.

The worker pool is justified by measured cold-start, not a slogan: 2.22 s per DSPy worker and
7.03 s for ten versus 5.6 ms/38.9 ms warm-fork figures. Warm fork is rejected for a threaded
supervisor; M7 requires reusable subprocesses with reset/retire proof. Exact “three READY” and
refill-on-consume are implementation details, not acceptance criteria.

### Addendum provenance

`implementation-plan.md` F6-04 and F6-12 are plan entries recovered from a stale status-refresh
branch; no feedback-6 source assessment exists. Therefore GCRA, Core Directive wiring, and
reset/retry are disposition evidence, not a claim that the current source implements them. The
addendum re-check at `17dfcd3` verified ancestor relationships and `rg` outputs only; it did not
run a provider or pool integration test.

## 6. Later hierarchy feedback — skeptical classification

The later feedback is accepted only where it restates D2/D8b boundaries: an explicit
harness-owned TaskTree, fresh child context, strict child→parent envelopes, and static DAG
validation before dynamic admission are target invariants. They are not claims that the current
runtime has an agent tree. “Implicit recursion is dead,” “explicit trees buy a 90% cache discount,”
“Prime 2026 proves it,” and “five cheap branches” are **UNVERIFIED as broad claims**: the primary
audit supports Prime explicit `AgentSession`/runtime contexts and bounded depth, with descendants
sharing one root worker, but not process-per-child isolation or a 90% total-request/latency metric.
Provider caches are org/workspace scoped and tasks may share an exact prefix. AlphaCodium is a
staged run/fix flow; LATS is candidate-solution MCTS with test/environment feedback. Neither is
universal orchestration. M5 requires per-node gate evidence; MCTS needs a falsifiable comparison.
D8c remains measured exact-prefix guidance, never a savings guarantee. Recursion evidence is
task-dependent; no implicit-recursion dead-end consensus is adopted.

The later hierarchy note does not alter the 19-row disposition table. It accepts an explicit
harness-owned tree, fresh child context, strict result envelopes, and static plan validation
before admission as target structure. It rejects no existing implementation because the current
pointer has no dynamic hierarchy. Any claim that implicit recursion is dead, a tree earns a 90%
cache discount, Prime 2026 proves the pattern, or five cheap branches are always optimal remains
UNVERIFIED without a primary source and fixed metrics. AlphaCodium/LATS/MCTS-at-every-node is
also not a universal requirement; per-node deterministic gates are the M5 contract, and MCTS
needs a falsifiable comparison before adoption.

The addendum's “done” labels are scoped to documentation/source wiring checks. `get_signature`
being present in `TOOL_SCHEMAS` and `TOOL_DISPATCH` does not mean every AST operation is exposed;
the F6-12 note keeps definitions/references as library functions. `include_diff` landing in
architecture does not make the field mandatory for every tier; the evaluator/default and
merge-failure on-demand paths remain distinct. GCRA appears in `implementation-plan.md` only,
so pacing remains a plan seam. Core Directive normalization and reset/retry are source symbols
in `architectus.py`, but supervisor wiring is still OPEN. These are the same source-versus-role
distinctions used by the conformance and security audits.

Prime's primary evidence is useful for the *shape* of D2—explicit child runtimes, independent
contexts, bounded depth, and a shared root worker—but not for M7's process-pool isolation or a
universal recursion verdict. Provider org/workspace cache scope means two tasks may share a cache
when their prefix matches; a cached-token price near 0.1× input rates does not prove 0.1× total
request cost or latency. These metrics must be measured on the same task set.

The shared root worker also explains why M7's reset/retire proof is separate from D2's context
boundary. Independent AgentSession context is a structural target; process isolation, provider
cache scope, and total-cost savings require separate evidence.

The admission check is therefore a cost boundary as well as a graph rule: reject an invalid DAG
before provider calls, worker spawn, or approval requests. Steering can repair a node's content
within its existing identity, but cannot add siblings or mutate the root. This is a testable M5
invariant and does not depend on MCTS or a cache-discount assumption.

This terminology keeps M7 and M6 measurable: AgentSession context is not process isolation,
cached-token price is not total request cost, and LATS/AlphaCodium method descriptions are not
universal orchestration requirements.

These are separate evidence axes.

The source and tests remain authoritative; `v2-1-status` is a rechecked tracker, not the authority.
