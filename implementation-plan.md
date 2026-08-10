# Implementation Plan (TRANSIENT — delete when implementation is done)

Status: 2026-08-10 (post-main-merge baseline refresh). Orchestrator-owned. This file is the DECISION LOG + CONTEXT MAP.
Orientation: docs/research/README.md (tiered index) → docs/architecture/architecture.md (final v2) → agents.md.

## Context map (where everything lives)
- Final design: docs/architecture/architecture.md (v2, deltas D1-D8 folded, §21 adoption record; Septum removed, no local cache, task tree, gates, refinement loop, include_diff note)
- v0.1 origin + 3 adversarial reviews: docs/architecture/system-design.md, docs/architecture/reviews/
- Module templates: docs/architecture/module-template/{architecture,dataset-format,example-spec}.md
- Evidence/research: docs/research/ (44 research docs; TIER 1 = python-3.14, sqlite-wal-durability, worktree-concurrency, ipc-protocol-draft, event-schema-draft, custos-asyncio-design, design-deltas, coding-constitution)
- Roadmap: docs/research/v2-1-review.md (M1-M9), v2-1-status.md (living tracker), m1-canonicalization-plan.md, architectus-design.md, compaction-design.md
- Naming map: docs/research/glossary.md
- Code: src/cambium/ — store, merge, ipc, worker, supervisor, orchestrator, tasktree, diffundo, bench, doctor, cli, conversations, dlq, resources, approval, fencing, system_health, lint_diag, ast_tools, schemas, eval_cache, provider_config, architectus, events (to be deleted per M1), modules/example (Decision enum v2.1)
- Redaction (`39005fa`), Diffundo + worker-provider routing (`77f3d52`), and the M6 fake-provider staging path are merged; redaction is still not wired into the supervisor's enqueue/INSERT paths.
- Orientation norms: agents.md (sections 1-11; constitution subsection; module inventory §3; lookup table §9)
- Tests: harness in tests/scenarios/; MODULE tests colocated: src/cambium/modules/<name>/tests/ (+ baselines) — removability rule

## Decision log (all user directives, in order, with disposition)
1. Python >=3.14,<3.15 REGULAR build (free-threaded rejected: verified GIL present by default; dspy 3.3.0 works on 3.14, blocked on freethreaded by orjson).
2. Headless-first interface: JSON-Lines stdio is the contract; TUI optional view (rich/Textual, v2.1); proto-AGI upper layer spawns instances.
3. NO LOCAL LLM CACHE in production (D1): provider-side prefix caching only; static prompt prefix top, dynamic tail bottom (D8c, diffundo.validate_prompt_structure). EVAL-HARNESS-ONLY cache exists (eval_cache.py, opt-in).
4. Task tree / conversation tree (D2): flat task list with requires:[] → Python builds DAG (tasktree.py, Kahn, cycle detection); info hiding: upward = result envelope only (9 keys, arch §3.4).
5. NO SANDBOXING in the harness (decision 10, D7, user directive): containment = worktree isolation + allowlists + approval gates; bwrap REMOVED from all docs (nobwrap commit); sandbox-options.md is a stub; containers = deployment vehicle outside harness (D8e).
6. Logging: YES, non-blocking (stdlib logging + QueueHandler + writer thread; logging-design.md).
7. Tests bundled WITH modules (colocated, deletable by removing the module dir); scenario/integration tests only (no TDD ceremony).
8. Coding constitution (Rust/HFT) translated for Python (coding-constitution.md; agents.md §7 subsection): enums over bools, no globals, flat flow, stdlib-over-custom, delete-over-add, measure-before-optimizing. Decision enum migration done (v2.1, wire format unchanged).
9. Prime Agent patterns adopted: persistent named workers + steer (D3, v2.1), gate+budgets (D4, in supervisor), /refine-style evidence-backed refinement loop (D5, M8).
10. Subagents: worktree-per-agent, canaries + verifiable stats against LLM lies, adversarial review before merge; 20+ concurrent OK; backends: sol/luna working after auth fix (used for architecture/grunt), glm depleted, kimi misconfigured (never use), general/build (DeepSeek) always works.
11. Compaction prep: this file + v2-1-status.md + glossary + tiered research index are the self-sufficient context map.
12. Critiques 1-4 dispositions: docs/research/feedback-4-assessment.md (21 claims: 6 reject/3 adopt/6 lite/6 already).
13. Feedback-5/6 dispositions: accepted actions are registered below (F6-01..F6-18). Provenance note: the F6 register was recovered from a stale status-refresh branch; no feedback-6 source assessment document exists in the repository, so rows are plan entries rechecked against current main, NOT "VERIFIED" claims.
14. Feedback-7 dispositions: external "brutal teardown" feedback (TOCTOU races, database crimes, static constraints); 17 adversarial audits + reviews; accepted actions registered below (F7-01..F7-09 + constitution review). Provenance note: like F6, rows are plan entries rechecked against current main, NOT "VERIFIED" claims.

## Feedback-6 accepted-action register
Plan entries, not completion claims. Owner branch may be merged (evidence on main) or in flight; state reflects current main as of the last tracker refresh.

| Item | Disposition | Milestone / owner | Current state |
|---|---|---|---|
| F6-01 dynamic tree growth | ADOPT: versioned atomic plan revisions, event-driven executor, durable checkpoint suspend/resume, single-parent revision boundary (M5). REJECTED: Promise/Future vocabulary. | M5 — `wt-luna-directive` | OPEN. M5 executor seam. |
| F6-02 supervisor decomposition | ADOPT: keep Nuntius/Custos/Surculus/GateRunner/Unio. REJECTED: direct Opifex\|Unio piping. | M1/M2 — `wt-super-hardening-v2` | PARTIAL. Hardening merged; M1 deletion set remains (M1 executor queued). |
| F6-03 identity/store/lock/merge-actor | REJECTED: actor+blackboard, central `shared.db`, lock removal, merge actor. GAP RECORDED: gate runs pre-rebase; rebased tree not re-gated inside lock. | M3/M4 merge boundary — `wt-supervisor-worktree-cleanup` | OPEN GAP. Re-gate proof or fix inside merge lock. |
| F6-04 provider pacing | ADOPT: session-global GCRA-style pacer, injected clock, `max_in_flight`, 60/rpm, per-retry permits; honor Retry-After only when verified. REJECTED: knapsack/PID. Supersedes F5-10. | M6 — `wt-worker-provider` | OPEN. Worker→Diffundo is now the gated seam; pacer not yet implemented. |
| F6-05 doom-loop detection | ADOPT: typed durable `doom_loop_detected` event, Architectus replans/aborts; AST fingerprinting rules; structured traceback data; epoch on env revision; critical kinds `doom_loop_detected`/`reset_retry_consumed`. REJECTED: raw diff/text hashing. | M5 — `wt-luna-directive` | OPEN. M5 executor seam. |
| F6-06 documentation consistency | ADOPT-LITE: `cascade-design.md` non-normative (local-cache mandate contradicts D1); refresh README/status/glossary; research index 44 docs; fix `worker.py` arch reference; keep `system-design.md` immutable. | Docs — `wt-status-refresh` | DONE (tracker refresh + agents.md + M1 plan merged). Index count recheck pending. |
| F6-07 store deadlock hardening | ADOPT: bound waits; supervisor fail-closed on `StoreError` (M1); admission waiters re-check writer death. | M1/M4 — `wt-store-hardening` | PARTIAL. store-hardening merged; writer-death admission evidence and M1 fail-closed remain. |
| F6-08 worktree quarantine | ADOPT: bounded quarantine, `git worktree move`, refs retained, `.cambium/quarantine/merge/`, cap 15/1 GiB/7 days, fail closed, events `merge_staging_quarantined`/`cleanup_failed`/`prune_started`/`pruned`, restart reconciliation. | M3/M4 — `wt-staging-quarantine` | PARTIAL. quarantine merged; startup reconciliation + bounded-prune critical-event proof remain. |
| F6-09 resource controls | ADOPT-LITE: cgroups note (MemoryHigh/MemoryMax/CPU), wire `can_run_heavy` into admission, clean cancel/retire. REJECTED: SIGSTOP at 85%, psutil. | M3/M4 — `wt-super-hardening-v2` | PARTIAL. hardening merged; admission wiring + fail-closed verification remain. |
| F6-10 schema library choice | REJECTED: msgspec/pydantic. ADOPT-LITE: conformance tests for each used schema feature. | M2/M8 — `wt-module-conformance` | PARTIAL. gate merged; per-feature conformance expansion remains. |
| F6-11 root prompt directive | ADOPT: minimal immutable root directive ≤200 tokens (F5-11); wire via luna-directive. DEFERRED: AST context to M9. REJECTED: universal goal/AST/test-only prompt. | M5/M9 — `wt-luna-directive` | PARTIAL. core directive in architectus; wiring verification remains. |
| F6-12 AST edits | ADOPT-LITE: Python-first AST editing opt-in; expose find_definitions/find_references/extract_signature; anchored primitive; parse-before-replace. | M5/M9 — `wt-get-signature` | PARTIAL. get_signature merged; edit seam remains. |
| F6-13 worker pool width | REJECTED: fixed five workers. ADOPT: configurable reusable pool; `worker_pool.py` seed is evidence, not acceptance; benchmark widths 1/3/5/8. | M7 — `wt-worker-pool-seed` | OPEN. seed merged; subprocess lifecycle + width benchmark remain. |
| F6-14 LSP | REJECTED: LSP. ADOPT-LITE: AST tools + Ruff after edits, cross-file evidence. | M5/M9 — `wt-get-signature` | DEFERRED. |
| F6-15 evaluation cache identity | REJECTED: AST as primary key. Define response identity (exact request+provider+model rev+params+key schema) and score identity (dataset bytes+metric/harness+relative names+runtime); SHA-256 keys; static import boundary. | M8 — `wt-eval-cache-fix` | OPEN. import-boundary scanner in flight. |
| F6-16 IPC bounds | ADOPT: FD3, 256-msg/8 MiB caps, `protocol_overflow` kill, write deadlines, bounded DLQ routing. | M2 — `wt-ipc-fuzz-timing` | PARTIAL. fuzz-timing merged; bounded transport + FD3 remain. |
| F6-17 DSPy metrics | ADOPT-SCOPED: M8 binary-classification metric sufficient; non-inferiority criterion; SIMBA adapter accepts `(Example, prediction)`; canaries inert until wired. | M8 — `wt-luna-baseline` / `wt-dspy-cambiumlm` | PARTIAL. baseline merged; DSPy branch in flight; M8 criterion correction remains. |
| F6-18 module isolation | ADOPT: sibling-import ban, JSON CLI probe, layout/removability, freeze/version integrity (meta.json + split digests + content anchor), offline guards, per-module proof, `cambium module-test NAME`; decouple bench reverse-imports and scripts/tooling. | M8 — `wt-module-conformance` / `wt-module-cli` / `wt-bench-decouple` | DONE. `cambium module-test example` passes 57/0/0 on main; committed baseline is module-scoped (57 node IDs, 0 foreign); bench-decouple merged. |

## Feedback-7 accepted-action register
Plan entries, not completion claims. Provenance note: like F6, rows are plan entries rechecked against current main, NOT "VERIFIED" claims. External "brutal teardown" feedback (TOCTOU races, database crimes, static constraints) reviewed by root via 17 adversarial audits + reviews.

| Item | Disposition | Milestone / owner | Current state |
|---|---|---|---|
| F7-01 dynamic tree growth blocked by static DAG validation | ALREADY PLANNED: `build_tree`/`topological_order` are re-invocable pure functions; validation does not block expansion. Real blockers: immutable ArchitectusCore tree (`architectus.py:308`) with no revision path, no runtime wiring, `run_plan` bypasses DAG (flat fan-out). Home = F6-01/M5. Use existing vocabulary `replan(plan_revision, added_tasks[])` + `task_decomposed`. REJECTED: "Saga/continuation checkpoint" jargon. | M5 (F6-01 home) | OPEN. M5 executor seam. |
| F7-02 conversations.py N+1 query problem | TRUE (proven: D+1 round-trips). ADOPT: guarded `WITH RECURSIVE` rewrite with `_MAX_CHAIN_DEPTH` + cycle/missing-link post-checks; single-statement atomic snapshot. REJECTED: naive unbounded CTE (hangs on cycles). | `wt-conversations-cte` | IN FLIGHT. |
| F7-03 DLQ filesystem abuse | TRUE bounded cost (`summarize` O(n)); durability parity already holds. ADOPT: SQLite-backed DLQ at `.cambium/dlq.db` (separate DB per architecture.md:456), single writer, keep-newest prune, SQL summarize, two-layer redaction; remove optional-redactor compat path. | `wt-dlq-sqlite` | IN FLIGHT. |
| F7-04 system_health TOCTOU; delete system_health.py | PARTLY TRUE / REJECT deletion. `can_run_heavy` has no production callers today; TOCTOU is a hazard in the unwired admission path. ADOPT-LITE: wire existing `CompileGate` semaphore into both gate runners + `can_run_heavy` fail-closed pre-flight. `doctor.py` depends on `health`/`format_health` — do not delete. | `wt-resource-admission` | IN FLIGHT. |
| F7-05 Diffundo waterfall; use RR/LRU | REJECT algorithm change. Priority-ascending is the normative adopted contract (arch §9.2, cascade-design); RR cursor adds first cross-provider shared mutable state (violates D1 + no-hidden-global + `test_no_local_cache_instance_has_no_mutable_mapping_attribute`). ADOPT-LITE: add missing priority-order + equal-priority determinism regression tests. | M6 — Diffundo | OPEN. Regression tests to add. |
| F7-06 pluggable execution wrapper argv | ADOPT-LITE. Add list-form argv override for worker launch (task-spec `worker` field or future `Config.worker`), NO sandboxing claim (D7), list-form only (no shell), env allowlist unchanged. | DEFERRED — `supervisor.py` contested | DEFERRED. |
| F7-07 strip threading.Lock; pure actor/mailboxes | REJECT stripping locks (`asyncio.Queue` not thread-safe across threads; locks protect real invariants). ADOPT three PROVEN defects: (a) observer deadlock under `_merge_lock`/`_worktree_lock`, (b) mutable event dict crossing observer/writer boundary, (c) two `run_plan` calls owning one session. Plus `ConversationStore.close()` final-fsync propagation + bounded waits (folded into `wt-conversations-cte`). | `wt-supervisor-races` | IN FLIGHT. |
| F7-08 AST-assert evaluation; DSPy offline | PARTLY TRUE / ADOPT-SCOPED (M8). `extract_signature`/`get_signature` tool exists; AST-assert gate, locked-file diff, no-op rejection, static prompt artifact loader all MISSING (DRAFT). DSPy stays an optimization-time dependency; production loads static exports. | M8 | OPEN. DRAFT scope. |
| F7-09 split modules into workspaces | REJECT split (JSON subprocess boundary + structural gates are correct). ADOPT three P1 gate gaps: module-test must validate `module.json`; import scanner must cover `__import__`/dynamic importlib/CLI-subprocess siblings; removability must be enforced. | `wt-module-isolation` | IN FLIGHT. |

Constitution review (agents.md replacement): REJECT wholesale replacement (it is Cambium-specific, not Rust/HFT); ADOPT stale facts + three concurrency invariants. Owner: `wt-agents-facts`.

## Merged state (main, clean)
- Test suite: verified full run = **695 passed, 1 skipped** (`uv run --python 3.14.7 --extra test pytest -q`, 2026-08-10, after module-baseline + offline-test + tasktree merges).
- Module isolation: `cambium module-test example` PASSES (57 passed, 0 failed, 0 skipped); committed baseline is module-scoped (57 node IDs, 0 foreign) with `split_digests` from `meta.json` 1.1.0, and the standalone bench CLI populates real test wall timings so the gate compares live p90 against the anchor.
- Bench: pytest plugin with a committed module-local baseline, drift gate, dataset-version re-anchor, and fail-closed missing-anchor behavior; dataset 1.1 (`382f7f6`) and bench-decouple (`4f54e5d`) are merged.
- M6: Diffundo (`77f3d52`), provider configuration, fake-provider staging, M6-hygiene quota/publish-scope assertions merged; real-provider acceptance unverified (E2E script staged at `/tmp/opencode/cambium-real-e2e.sh`, blocked on tool-loop merge).
- Recent merges: redaction (`39005fa`), results (`a0403ae`), ready-correlation (`5e27be7`), worktree-cleanup (`19d2135`), doctor-decouple (`3bfbd0b`), tasktree-cli (`c558205`), auth store (`7f9823a`), store-hardening (`c31e781`), staging-quarantine (`629c5bf`), supervisor-hardening v2, worker-pool seed, agents.md condense (`d336da4`), M1 plan rewrite (`0605eaa`), session redactor into store/DLQ (`7e31724`), **supervisor consolidation** (`7f280b0`), module-scoped baseline + offline-test fix, tasktree credential-safe parser.

## Blockers (verified against current main)
- No public `Cambium`/`Session`/`Instance` API: `src/cambium/__init__.py` is version-only (design done, blocked on M1 result wiring).
- No `result.json` production wiring: `results.write_result_json` exists but nothing in the supervisor calls it (M1 phase d).
- Worker is single marker-append only (no tool loop): tool-loop implementation in flight.
- No FD-3 transport; unbounded `asyncio.Queue` (`supervisor.py:448`); DLQ unwired (M2).
- Architectus execution core exists but not wired into the supervisor; `orchestrator.py` still a submit/drain skeleton (M5).
- M1 deletion set still present: slice `EventLog`, `_FallbackEventStore`, `_FallbackSequencer`, `events.py`, slice `run_session` body — M1 executor queued behind the tool-loop merge.

## In flight (worktrees, from `git worktree list` 2026-08-10)
- Merged since last refresh: plan-refresh (`b94e277`), bench-reanchor (`b131904`), DSPy/CambiumLM (`c69ec92`), bench-creds (`7a32b86`), strict result contract (`1275011`), baseline-57 (`27691b2`).
- Active: wt-conversations-cte, wt-dlq-sqlite, wt-supervisor-races, wt-module-isolation, wt-resource-admission, wt-agents-facts, wt-plan-f7; wt-eval-cache-fix (import-boundary scanner, dirty), wt-worker-tool-loop, wt-batch-read-parked (deferred until tool-loop lands), plus two dirty detached scratch worktrees under audit.

## Next actions (dependency order)
1. Merge worker tool-loop (marker-append → real tool loop) — unlocks E2E and M1.
2. Merge packaging fixes (pytest core dep, wheel-safe default worker, bench fail-closed discovery) + DSPy + eval-cache.
3. Supervisor serial wave (after tool-loop): publish-integrity guards, redaction wiring (enqueue/INSERT via `build_session_redactor`), `result.json` wiring, M1 deletion (run_session adapter, EventLog/fallbacks/events.py, orchestrator trim).
4. Run real-provider E2E (`/tmp/opencode/cambium-real-e2e.sh` today variant after tool-loop; tool-loop variant after worker merge).
5. M5 integration (wire Architectus), M7 pool, M8 SIMBA + `example`→`should_decompose` rename, M9 adoption.
6. Final: re-baseline module-scoped, worktree cleanup, DELETE THIS FILE, final report.

## Verification norms (canaries)
Every agent: exact commands + outputs; UNVERIFIED markers; commit in own worktree (check git rev-parse --show-toplevel); empty report = failure; snapshots of live systems need as-of timestamps; adversarial review before merge; duplicate/conflicting branches resolved toward main + re-verify.
