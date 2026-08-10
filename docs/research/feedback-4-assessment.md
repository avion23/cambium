# Cambium — Fourth External Critique: Assessment

**Version:** 1.0.0
**Date:** 2026-08-09
**Branch:** `wt-doc-fb4` (`/tmp/opencode/cambium-doc-fb4`)
**Status:** Historical disposition of 21 critique claims against architecture v2.0.0, D1–D7,
D8a–D8g, research, and the snapshot source. Current readers use
`docs/architecture/architecture.md`, `src/cambium/`, and `docs/research/v2-1-status.md`.

**Historical snapshot / current pointer:** sources were read from `/home/ubuntu/cambium`; branch
and test counts below are historical. Current notes: provider loop, Diffundo, EventStore, and
root `Result` exist; DLQ, eval cache, ResourceBudget, `worker_pool`, and `events` are absent;
there is no per-worker sandbox or production shell approval, and dynamic hierarchy is absent.

## 0. Evidence convention

The critique text was not in the corpus; claim numbers reproduce the orchestrator disposition.
Paths are repository-relative. Checks are listed in §3. The source tree at the snapshot included
`system_health.py`/`eval_cache.py`/`lint_diag.py` only on their named branches where noted.

## 1. Verdict table

| # | Claim | Disposition | Compact rationale and evidence |
|---|---|---|---|
| 1 | Delete Latin module names | **REJECT** | Established vocabulary and 35-file references; unmeasured token saving cannot justify rename (`agents.md` §6–7; `architecture.md` §4; `rg -l` count 35). |
| 2 | Delete `doctor.py` | **REJECT** | Tested diagnostics: worktree/store/secrets checks; snapshot run 5 pass/1 warn/1 skip/0 fail. `doctor.py`; `test_tooling.py`; architecture §§13, 19.4. |
| 3 | Replace SQLite with JSONL | **REJECT** | WAL durability and queryable conversations are adopted D8g; JSONL mirror remains optional (`sqlite-wal-durability.md` §2; architecture §§6.1, 6.6; feedback-2 D8g). |
| 4 | Use `ProcessPoolExecutor` | **REJECT** | Pipes provide streamed JSONL RPC, process groups, cwd/env, restart and worktree binding (`architecture.md` §§5.1, 7.2; `vertical-slice-report.md`). |
| 5 | Replace IPC with Redis/ZeroMQ | **REJECT** | Single-host/no-new-framework scope; `ipc.py` framing and four-layer liveness already address stated failures (`architecture.md` §§1, 5, 5.3, 6.2; `agents.md` §7). |
| 6 | Remove DAG cycle detection | **REJECT** | 29 task-tree tests cover cycles, order, bounds and envelopes; Kahn names the cycle and blocks pending tasks (`tasktree.py`; `test_tasktree.py`; architecture §3.7/DS-M6). |
| 7 | Eliminate upward diff | **ADOPT-LITE** | Keep capped 64 KiB `unified_diff`/`diff_truncated`; add `include_diff` for higher tiers, default on for evaluator (architecture §§3.4, 5.2; D8b; `_ENVELOPE_KEYS`). |
| 8 | Blackboard for cross-cutting changes | **ADOPT-LITE** | Bounded parent-mediated query substrate in conversation store; not free-for-all scratchpad (architecture §6.6/I2.4; `architectus-design.md`). |
| 9 | Flat `requires:[…]` task list | **ALREADY-IMPLEMENTED** | `tasktree.build_tree` validates flat `depends_on` plans, one root, no multi-parent/cycle, then Kahn-orders them (`tasktree.py`; D2). |
| 10 | Data → function → data; supervisor side effects | **ALREADY-IMPLEMENTED** | Pure module contract, writer thread, worker subprocesses, deterministic LLM-free layer (base `Module.decide`; architecture §§2, 4; `agents.md` §7). |
| 11 | Only lock is final merge | **ALREADY-IMPLEMENTED** | Unio lock spans verify/publish; store lock protects writer scalars, not merge (`architecture §7.8`; event draft §3.11; `worktree-concurrency.md`; D6). |
| 12 | Mailboxes/queues (CSP) | **ALREADY-IMPLEMENTED** | Single-writer store/logging queues and worker pipes. Store queue is intentionally unbounded v2.1 (`store.py:20–22,106`; review §1.3 P0 gap 10; architecture §§5.1, 6.2). |
| 13 | `check_system_health` before heavy ops | **ADOPT** | Stdlib `/proc`/`os.sysconf` health gate for resource gap; `system_health.py` existed on `wt-luna-health@d4db2ff`, unmerged in snapshot (`v2-1-review` §1.3 P0 gap 9). |
| 14 | Token bucket limiter | **ALREADY-IMPLEMENTED** | D8f per-provider buckets, pause/recovery, and cascade are folded into architecture and branch Diffundo (`architecture` §§7.4, 9.1–9.2; D8f; `v2-1-review` §1.1 item 7). |
| 15 | Local cache for DSPy evals | **ADOPT (scoped)** | Eval-only full-prompt/model cache is safe with frozen datasets; never production. `eval_cache.py` `wt-luna-evalcache@d8f9408` was unmerged in snapshot (`design-deltas` D1; architecture §8.1/M9; `meta.json`). Current note: eval cache is absent. |
| 16 | Mock git env + AST asserts | **ALREADY-IMPLEMENTED IN PART** | Frozen splits/metrics exist; mock-git/AST scoring is DRAFT v2.1 (`bench-harness-design.md` §8; `example-datasets-v1.md`; `test_dataset_splits.py`). |
| 17 | AST code search | **ADOPT** | `ast_tools.py` `1ed155b` had tree-sitter + stdlib fallback; version verification and tool wiring were unverified in snapshot. |
| 18 | LSP/lint diagnostics | **ADOPT-LITE** | Reuse ruff; no pyright. `lint_diag.py` `wt-luna-lint@2d26e5f` was unmerged in snapshot. |
| 19 | Strict tool schemas | **ADOPT-LITE** | Stdlib dataclass→JSON-Schema and validation, no pydantic dependency; `schemas.py` `4e2c2ea` merged in snapshot. |
| 20 | Speculative batched tool calls | **ADOPT-LITE** | Record as v2.1 worker-loop note; v2 remains sequential heartbeat loop (architecture §7.6; architectus-design; v2-1-review §1.1 item 3). |
| 21 | `include_diff` flag | **ADOPT-LITE** | Higher tiers may omit diff; evaluator stays default-on and schema enforcement remains (architecture §3.4; D8b). |

**Counts:** 21 rows — REJECT 6 (1–6) · ADOPT 3 (13,15,17) · ADOPT-LITE 6 (7,8,18–21) ·
ALREADY-IMPLEMENTED 6 (9,10,11,12,14,16). Claim 7's correction is retained: Python 3.14.7
`graphlib` exposes `CycleError.args[1]`;
Kahn remains for deterministic ordering/message control.

## 2. Plan consequences

1. Land FD-3 IPC as M2 once, atomically across worker/fixtures/pool (v2-1-review §§2B, 3 M2).
2. Add immutable ≤200-token Core Directive to the static prompt prefix (claim 11/D8c).
3. Record “three gate failures → reset to `base_commit`, retry once, abort subtree” and keep
   `evaluate_goal` as the existing gate (claim 12).
4. Mine corrected DLQ trajectories for M8 few-shots (claim 14).
5. Record host `systemd-run` cgroups as deployment note; harness health gate remains stdlib.
6. Specify M7 pre-warmed pool (worker-coldstart/v2-1-review numbers).
7. Document separate single-writer `events.db`/`conversations.db`/`shared.db` tradeoff.
8. Keep the `graphlib` alternative comment (follow-up `9b071e0` was recorded as done).
9. Expose AST signature/reference tools after `tools.py` merge (`74ff5aa` in snapshot).

No disposition reverted adopted deltas, SQLite/pipes, cycle detection, or default-on diff.

## 3. UNVERIFIED flags and checks

Unverified: tree-sitter 0.26.0 on CPython 3.14; token-burn claim; unmerged `system_health.py`,
`eval_cache.py`, `lint_diag.py`; `doctor` behavior on other hosts. The snapshot checks were:
`PYTHONPATH=src python3.14 -m cambium.doctor` → 5 pass/1 warn/1 skip/0 fail (models.yml WARN);
`rg -c '^def test_' tests/scenarios/test_tasktree.py` → 29; `rg -l` vocabulary → 35; no pydantic
in `uv.lock`; frozen timestamps in dataset `meta.json`; merge-base checks showed AST/schemas in
main and the three luna modules out; `git diff --check` clean before the historical commit.

The document is an immutable assessment. Current module state belongs in source and
`v2-1-status`, not in this snapshot.

## 4. Disposition rationale retained

### Stable boundaries

The Latin-name rejection was not an aesthetic preference. The corpus maps Architectus, Custos,
Opifex, Diffundo, and Unio to concrete layers, and the vocabulary rule in `agents.md` forbids
invented synonyms. A rename would touch 35 files (Unio 20; Architectus and Diffundo 18 each;
Custos 16; Opifex 15) without a measured token saving. `doctor.py` is likewise not a dead
bootstrap: `test_tooling.py` exercises worktree hygiene, SQLite integrity, and the secrets WARN;
the snapshot run found `/home/ubuntu/.omp/agent/models.yml` git-tracked but did not print its
contents. `system-design.md` remains the v0.1 origin referenced by architecture §20 and all
three reviews, so deletion would destroy provenance.

The SQLite/pipe/cycle dispositions preserve explicit boundaries. SQLite WAL has eight crash
trials and 44,992 committed events with zero loss in the cited durability experiment; JSONL is
already available as a mirror. A `ProcessPoolExecutor` cannot provide streamed request-ID RPC,
`start_new_session`/`killpg`, worktree cwd/env, or the one-for-one transient supervision model.
Redis/ZeroMQ would add a broker to a single-host library whose non-goals forbid distributed
infrastructure. Kahn cycle detection prevents pending tasks and is only ~20 lines; the correction
that `graphlib.CycleError` exposes `args[1]` does not remove the deterministic ordering/message
reason for retaining Kahn.

### Adopted-lite controls

The upward diff is not a transcript: `unified_diff` is capped 64 KiB, can be omitted with
`include_diff:false` for higher tiers, and remains on for the evaluator and on-demand merge
conflict resolution. The blackboard proposal is a bounded conversation-store query, still
parent-mediated and subject to I2.4; siblings do not gain a free scratchpad. `check_system_health`
uses `/proc/meminfo` and `os.sysconf` rather than psutil, matching the stdlib/no-new-framework
norm. The snapshot branch `wt-luna-health@d4db2ff` held the helper but it was not in main then;
likewise eval cache `wt-luna-evalcache@d8f9408` and lint `wt-luna-lint@2d26e5f` were unmerged.

The AST tool (`ast_tools.py@1ed155b`) and schema converter (`schemas.py@4e2c2ea`) were merged,
but tree-sitter 0.26.0 on CPython 3.14 was not recorded and signature dispatch was initially
absent. The frozen dataset splits and metrics are implemented; mock-git/AST-assert evaluation
remained the DRAFT §8 of `bench-harness-design.md`. These distinctions avoid treating a module
branch or an adopted design as delivered runtime code.

### Plan and current boundary

The plan items preserve sequencing: FD-3 is an atomic M2 transport change; the ≤200-token Core
Directive sits in the D8c static prefix; reset/retry is a bounded worktree action, not a new
`evaluate_goal` tool; DLQ few-shots belong to M8; cgroups belong to host deployment; pool
warmup gates `max_width >= 4`; and the separate `events.db`/`conversations.db`/`shared.db`
tradeoff is documented before implementation. The graphlib comment landed in `9b071e0`; AST
tool wiring was pending `74ff5aa` at the snapshot.

The current security facts remain explicit: D7 has no in-harness sandbox, and production approval
for external-path writes/network is not implemented. This is why claims 8 and 9 are deployment/
planning dispositions, not a claim that a kernel boundary or `wait_for_resources` tool already
protects a live worker.

The module-state anchor for the original plan was `main@6d80a05`; the related historical merges
were `97ef7d6` (bench-harness design), `790f470` (luna tooling), and `a9d59c9` (conversion/token
work). They are retained as branch provenance, not as current component claims.

The adopted-lite label means “record the boundary and a bounded follow-up,” not “the proposed
module is present.” Claim 13's helper was branch-local, claim 15's eval cache was production-
disabled (and is absent in the current pointer), claim 17's AST backend needed a version check,
and claim 18's lint wrapper reused an existing ruff dependency. This distinction is material for
security: a plan-level health gate or eval cache cannot be cited as a worker resource limit or a
production cache.

The same classification applies to later hierarchy feedback: explicit TaskTree ownership,
fresh child context, strict envelope filtering, and static-DAG-before-admission are acceptable
targets. Primary audit evidence supports Prime explicit AgentSession contexts and bounded depth,
with descendants sharing one root worker; process-per-child isolation, a 90% cache discount,
Prime-as-proof, five cheap branches, and universal MCTS remain unverified. AlphaCodium is a
staged run/fix flow and LATS candidate-solution MCTS with test/environment feedback, not universal
task orchestration. Claim 7's `include_diff` choice is a bounded payload option, not evidence of
cache economics or parent reasoning visibility.

Prime's shared root-session worker is an important correction to the deployment reading: explicit
child contexts do not imply process-per-child isolation. A host container can supply that boundary
outside Cambium, while D8b still enforces fresh context and strict envelopes inside. Provider
cache-read pricing and total request latency must be measured independently.

The same staged order applies here: validate the explicit DAG and envelopes with a fake provider;
measure prefix hits/cached-token pricing separately; then test shared-worker reset. No broad
consensus claim is a substitute for these checks.

These checks remain future work.

They require source evidence.

Prime's context evidence does not waive M7's reset proof or establish a universal recursion rule.
