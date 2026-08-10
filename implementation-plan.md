# Implementation Plan (TRANSIENT — delete when implementation is done)

Status: 2026-08-10 (refresh at `main@aeaedba`; packaging, F5 addendum, doctor empty-key, HTTP transport guard, and file modes merged). Orchestrator-owned. This file is the DECISION LOG + CONTEXT MAP.
Orientation: docs/research/README.md (tiered index) → docs/architecture/architecture.md (final v2) → agents.md.

## Context map (where everything lives)
- Final design: docs/architecture/architecture.md (v2, deltas D1-D8 folded, §21 adoption record; Septum removed, no local cache, task tree, gates, refinement loop, include_diff note)
- v0.1 origin + 3 adversarial reviews: docs/architecture/system-design.md, docs/architecture/reviews/
- Module templates: docs/architecture/module-template/{architecture,dataset-format,example-spec}.md
- Evidence/research: docs/research/ (44 research docs; TIER 1 = python-3.14, sqlite-wal-durability, worktree-concurrency, ipc-protocol-draft, event-schema-draft, custos-asyncio-design, design-deltas, coding-constitution)
- Roadmap: docs/research/v2-1-review.md (M1-M9), v2-1-status.md (living tracker), m1-canonicalization-plan.md, architectus-design.md, compaction-design.md
- Naming map: docs/research/glossary.md
- Code: src/cambium/ — store, merge, ipc, worker, supervisor, orchestrator, tasktree, diffundo, bench, doctor, cli, conversations, dlq, resources, approval, fencing, system_health, lint_diag, ast_tools, schemas, eval_cache, provider_config, architectus, events (to be deleted per M1), modules/example (Decision enum v2.1)
- Redaction (`39005fa`) merged; the session redactor seam into durable store/DLQ (`7e31724`) is merged, but the supervisor still constructs `_FallbackEventStore` and does not pass a session redactor into its event path (M1 executor in flight, phase (a)).
- Diffundo + worker-provider routing (`77f3d52`) and the M6 fake-provider staging path are merged.
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
13. Feedback-5/6 dispositions: accepted actions are registered below (F6-01..F6-18). Provenance note: the F6 register was recovered from a stale status-refresh branch; no feedback-6 source assessment document exists in the repository, so rows are plan entries rechecked against current main, NOT "VERIFIED" claims. The feedback-5 current-main addendum (`2e7aae3`, merged as `a2b670f`) records the F5-10/F5-17/F5-18 corrections below.
14. F5 stale corrections (merged addendum `2e7aae3`): F5-10 PID pacing is superseded/rejected by F6-04's session-global GCRA pacer; F5-17 `get_signature` is wired into `TOOL_SCHEMAS` + `TOOL_DISPATCH`; F5-18 the `include_diff` arch note landed (`16e61cf` is an ancestor of main).

## Feedback-6 accepted-action register
Plan entries, not completion claims. Owner branch may be merged (evidence on main) or in flight; state reflects current main as of the last tracker refresh.

| Item | Disposition | Milestone / owner | Current state |
|---|---|---|---|
| F6-01 dynamic tree growth | ADOPT: versioned atomic plan revisions, event-driven executor, durable checkpoint suspend/resume, single-parent revision boundary (M5). REJECTED: Promise/Future vocabulary. | M5 — `wt-luna-directive` | OPEN. M5 executor seam. |
| F6-02 supervisor decomposition | ADOPT: keep Nuntius/Custos/Surculus/GateRunner/Unio. REJECTED: direct Opifex\|Unio piping. | M1/M2 — `wt-m1-executor` | PARTIAL. Hardening merged; M1 deletion set handled by `wt-m1-executor` in flight (events.py + orchestrator placeholder deleted on branch; supervisor canonicalized onto one store/sequencer). |
| F6-03 identity/store/lock/merge-actor | REJECTED: actor+blackboard, central `shared.db`, lock removal, merge actor. GAP RECORDED: gate runs pre-rebase; rebased tree not re-gated inside lock. | M3/M4 merge boundary — `wt-supervisor-worktree-cleanup` | OPEN GAP. Re-gate proof or fix inside merge lock. |
| F6-04 provider pacing | ADOPT: session-global GCRA-style pacer, injected clock, `max_in_flight`, 60/rpm, per-retry permits; honor Retry-After only when verified. REJECTED: knapsack/PID. Supersedes F5-10 (F5-10 PID pacing is rejected, not implemented). | M6 — `wt-worker-provider` | OPEN. Worker→Diffundo is now the gated seam; pacer not yet implemented. |
| F6-05 doom-loop detection | ADOPT: typed durable `doom_loop_detected` event, Architectus replans/aborts; AST fingerprinting rules; structured traceback data; epoch on env revision; critical kinds `doom_loop_detected`/`reset_retry_consumed`. REJECTED: raw diff/text hashing. | M5 — `wt-luna-directive` | OPEN. M5 executor seam. |
| F6-06 documentation consistency | ADOPT-LITE: `cascade-design.md` non-normative (local-cache mandate contradicts D1); refresh README/status/glossary; research index 44 docs; fix `worker.py` arch reference; keep `system-design.md` immutable. | Docs — merged | DONE (tracker refresh + agents.md + M1 plan + F5 addendum merged). Index count recheck pending. |
| F6-07 store deadlock hardening | ADOPT: bound waits; supervisor fail-closed on `StoreError` (M1); admission waiters re-check writer death. | M1/M4 — `wt-store-hardening` | PARTIAL. store-hardening merged; writer-death admission evidence and M1 fail-closed remain. |
| F6-08 worktree quarantine | ADOPT: bounded quarantine, `git worktree move`, refs retained, `.cambium/quarantine/merge/`, cap 15/1 GiB/7 days, fail closed, events `merge_staging_quarantined`/`cleanup_failed`/`prune_started`/`pruned`, restart reconciliation. | M3/M4 — `wt-staging-quarantine` | PARTIAL. quarantine merged; startup reconciliation + bounded-prune critical-event proof remain. |
| F6-09 resource controls | ADOPT-LITE: cgroups note (MemoryHigh/MemoryMax/CPU), wire `can_run_heavy` into admission, clean cancel/retire. REJECTED: SIGSTOP at 85%, psutil. | M3/M4 — `wt-super-hardening-v2` | PARTIAL. hardening merged; admission wiring + fail-closed verification remain. |
| F6-10 schema library choice | REJECTED: msgspec/pydantic. ADOPT-LITE: conformance tests for each used schema feature. | M2/M8 — `wt-module-conformance` | PARTIAL. gate merged; per-feature conformance expansion remains. |
| F6-11 root prompt directive | ADOPT: minimal immutable root directive ≤200 tokens (F5-11); wire via luna-directive. DEFERRED: AST context to M9. REJECTED: universal goal/AST/test-only prompt. | M5/M9 — `wt-luna-directive` | PARTIAL. core directive in architectus; wiring verification remains. |
| F6-12 AST edits | ADOPT-LITE: Python-first AST editing opt-in; expose find_definitions/find_references/extract_signature; anchored primitive; parse-before-replace. | M5/M9 — `wt-get-signature` | PARTIAL. `get_signature` wired into `TOOL_SCHEMAS` (`schemas.py`) and `TOOL_DISPATCH` (`tools.py`, F5-17); edit seam remains. |
| F6-13 worker pool width | REJECTED: fixed five workers. ADOPT: configurable reusable pool; `worker_pool.py` seed is evidence, not acceptance; benchmark widths 1/3/5/8. | M7 — `wt-worker-pool-seed` | OPEN. seed merged; subprocess lifecycle + width benchmark remain. |
| F6-14 LSP | REJECTED: LSP. ADOPT-LITE: AST tools + Ruff after edits, cross-file evidence. | M5/M9 — `wt-get-signature` | DEFERRED. |
| F6-15 evaluation cache identity | REJECTED: AST as primary key. Define response identity (exact request+provider+model rev+params+key schema) and score identity (dataset bytes+metric/harness+relative names+runtime); SHA-256 keys; static import boundary. | M8 — `wt-eval-cache-fix` | OPEN. Branch separates request/score identities and enforces identity boundaries; static import-boundary scanner in flight (round-seventeen findings; dirty). |
| F6-16 IPC bounds | ADOPT: FD3, 256-msg/8 MiB caps, `protocol_overflow` kill, write deadlines, bounded DLQ routing. | M2 — `wt-ipc-fuzz-timing` | PARTIAL. fuzz-timing merged; bounded transport + FD3 remain. |
| F6-17 DSPy metrics | ADOPT-SCOPED: M8 binary-classification metric sufficient; non-inferiority criterion; SIMBA adapter accepts `(Example, prediction)`; canaries inert until wired. | M8 — `wt-luna-baseline` / `wt-dspy-cambiumlm` | PARTIAL. baseline merged; DSPy branch in flight (merge-blocker fixes committed, branch dirty); M8 criterion correction remains. |
| F6-18 module isolation | ADOPT: sibling-import ban, JSON CLI probe, layout/removability, freeze/version integrity (meta.json + split digests + content anchor), offline guards, per-module proof, `cambium module-test NAME`; decouple bench reverse-imports and scripts/tooling. | M8 — `wt-module-conformance` / `wt-module-cli` / `wt-bench-decouple` | DONE. `cambium module-test example` passes **57/0/0** live on main; bench-decouple merged. Committed baseline on main is still module-scoped **40 node IDs** (`b709375`); the baseline-57 refresh (57 node IDs, 0 foreign) is in flight on `wt-baseline-57` and NOT merged. |

## Merged state (main, clean at `aeaedba`)
- Test suite: verified full run = **711 passed, 1 skipped** (`uv run --python 3.14.7 --extra test pytest -q`, 2026-08-10, `main@aeaedba`; also 704/1 at `5d82a91`, 693/1 at `17dfcd3`).
- Module isolation: `cambium module-test example` PASSES (57 passed, 0 failed, 0 skipped) live on main. Committed baseline on main remains module-scoped (40 node IDs, 0 foreign) with `split_digests` from `meta.json` 1.1.0; the 57-node baseline refresh is in flight (`wt-baseline-57`).
- Packaging fixes merged (`17dfcd3`): pytest core dependency, wheel-safe default worker, bench fail-closed discovery, `--full`/`--drift-report` CLI wiring; the standalone bench wall-timing gate is in flight on `wt-baseline-57`.
- Bench: pytest plugin with a committed module-local baseline, drift gate, dataset-version re-anchor, and fail-closed missing-anchor behavior; dataset 1.1 (`382f7f6`) and bench-decouple (`4f54e5d`) are merged. A fail-closed dataset-version re-anchor gate (no silent re-anchor) is in flight (`wt-bench-reanchor`).
- M6: Diffundo (`77f3d52`), provider configuration, fake-provider staging, M6-hygiene quota/publish-scope assertions merged; real-provider acceptance unverified (E2E script staged at `/tmp/opencode/cambium-real-e2e.sh`, blocked on tool-loop merge).
- Recent merges on main: redaction (`39005fa`), results (`a0403ae`), ready-correlation (`5e27be7`), worktree-cleanup (`19d2135`), doctor-decouple (`3bfbd0b`), tasktree-cli (`c558205`), auth store (`7f9823a`), store-hardening (`c31e781`), staging-quarantine (`629c5bf`), supervisor-hardening v2, worker-pool seed, agents.md condense (`d336da4`), M1 plan rewrite (`0605eaa`), session redactor into store/DLQ (`7e31724`), **supervisor consolidation** (`7f280b0`), module-scoped baseline + offline-test fix, tasktree credential-safe parser (`73e16c8`), F6 register (`4f580d2`), module CLI wire-contract tests (`5078eb0`), **packaging fixes** (`17dfcd3`), **feedback-5 addendum** (`a2b670f`), **doctor empty-key fail-closed** (`c93d07b`), **HTTP transport guard** (loopback-only, redirects/proxies rejected; `5d82a91`), plan-mode ref-publication docs (`e203849`), **file modes** (0700 dirs / 0600 artifacts; `aeaedba`).

## Blockers (verified against current main)
- No public `Cambium`/`Session`/`Instance` API: `src/cambium/__init__.py` is version-only (design done, blocked on M1 result wiring; public API blocked until M1).
- No `result.json` production wiring on main: `results.write_result_json` exists but nothing in the supervisor calls it (M1 phase d; `wt-m1-executor` writes a canonical session result on its branch, not merged).
- Worker is single marker-append only (no tool loop): provider-backed tool loop in flight (`wt-worker-tool-loop`).
- No FD-3 transport; unbounded `asyncio.Queue` in `supervisor.py`; DLQ bounded-routing unwired (M2).
- Architectus execution core exists but not wired into the supervisor; `orchestrator.py` still a submit/drain skeleton (M5).
- M1 deletion set present on main: slice `EventLog`, `_FallbackEventStore`, `_FallbackSequencer`, `events.py`, slice `run_session` body — `wt-m1-executor` deletes `events.py` + orchestrator placeholder and canonicalizes onto one store/sequencer (in flight, not merged).

## In flight (worktrees, from `git worktree list` 2026-08-10; tips/branches move as children commit)
- `wt-worker-tool-loop` — provider-backed agent tool loop with fenced commit (tip `3f84e47`, includes batch-read commits `bd3ca40`/`7ff30f3`; backup branch `wt-worker-tool-loop-backup-709a1d9`).
- `wt-m1-executor` — canonicalize supervisor onto one store/sequencer, delete `events.py` + orchestrator placeholder, write canonical session result, harden publish integrity (tip `f58d45a`, dirty `supervisor.py`).
- `wt-batch-read` — batch confined file reads + tool_event type/duration_ms validation (tip `585e655`; supersedes parked `wt-batch-read-parked`).
- `wt-result-contract` — strict Result contract (metric_score range, unified_diff str) (tip `8579933`, dirty `results.py`).
- `wt-bench-creds` — scrub provider credentials from module subprocess environments + redact JSON/repr-escaped credentials at the module error boundary (tip `aa0517a`, dirty).
- `wt-bench-reanchor` — bench gate fails closed on dataset_version change, no silent re-anchor (tip `a9ae2f9`, clean).
- `wt-baseline-57` — 57-node module baseline refresh + standalone CLI wall-timing gate + timing-subprocess env scrub (tip `cbbe35d`, dirty; NOT merged).
- `wt-dspy-cambiumlm` — CambiumLM real-provider adapter, DSPy fixes; merge-blocker commits committed (tip `bf7f7da`).
- `wt-eval-cache-fix` — eval-cache request/score identity separation + static import-boundary scanner (tip `2017be7`, dirty `eval_cache.py`; previous WIP diagnosis: separated identities and import-boundary enforcement, scanner rounds through seventeen).
- Parked/backup branches: `wt-batch-read-parked` (superseded), `wt-worker-tool-loop-backup-709a1d9`. DLQ-routing and empty scratch worktrees were removed during cleanup; no live `wt-dlq-routing` worktree remains.

## Next actions (dependency order)
1. Merge worker tool-loop (`wt-worker-tool-loop`; includes batch-read commits) — unlocks E2E and M1.
2. Merge `wt-batch-read` (+ `wt-result-contract`), `wt-bench-creds`, `wt-bench-reanchor`, and `wt-baseline-57` — re-anchor module baseline to 57 and stand up the wall-timing gate.
3. Merge `wt-m1-executor` after tool-loop (supervisor canonicalization, redaction wiring into the supervisor event path, `result.json` wiring, M1 deletion of EventLog/fallbacks/`events.py`, orchestrator trim).
4. Merge DSPy (`wt-dspy-cambiumlm`) and eval-cache (`wt-eval-cache-fix`) after M1; run real-provider E2E (`/tmp/opencode/cambium-real-e2e.sh`).
5. M2 FD3/bounded transport (F6-16), M5 integration (wire Architectus), M7 pool, M8 SIMBA + `example`→`should_decompose` rename, M9 adoption.
6. Final: re-baseline module-scoped at 57, worktree cleanup, DELETE THIS FILE, final report.

## Verification norms (canaries)
Every agent: exact commands + outputs; UNVERIFIED markers; commit in own worktree (check git rev-parse --show-toplevel); empty report = failure; snapshots of live systems need as-of timestamps; adversarial review before merge; duplicate/conflicting branches resolved toward main + re-verify.
