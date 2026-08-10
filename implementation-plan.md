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

## Merged state (main, clean; HEAD `b709375`)
- Test suite: verified full run = **647 passed, 4 skipped** (`uv run --python 3.14.7 --extra test pytest -q`, 2026-08-10 at `b709375`). The committed module baseline still records 307 node IDs, of which 278 are scenario tests outside the module — a module-test gate blocker (see Blockers).
- Bench: pytest plugin with a committed module-local baseline, drift gate, dataset-version re-anchor, and fail-closed missing-anchor behavior; dataset 1.1 (`382f7f6`) and bench-decouple (`4f54e5d`) are merged.
- M6: Diffundo (`77f3d52`), provider configuration, fake-provider staging, and M6-hygiene quota/publish-scope assertions are merged; real-provider acceptance remains unverified.
- Merged since the last tracker refresh: redaction (`39005fa`), worker-provider/Diffundo (`77f3d52`), results (`a0403ae`), ready-correlation (`5e27be7`), worktree-cleanup (`19d2135`), doctor-decouple (`3bfbd0b`), luna-baseline (`38d46a7`), tasktree-cli (`c558205`), auth store (`7f9823a`), module-conformance gate (`9b8b32e`), store-hardening (`c31e781`), staging-quarantine (`629c5bf`), supervisor-hardening v2 (HEAD `b709375`), and the worker-pool state seed (`worker_pool.py`).

## Blockers (verified against `b709375`)
- No public `Cambium`/`Session`/`Instance` API: `src/cambium/__init__.py` is version-only.
- No `result.json` production wiring: `results.write_result_json` exists but nothing in the supervisor calls it.
- Worker is single marker-append only (no tool loop): `worker.py` loads the provider but returns exactly one append-marker decision.
- Credential leak: `_worker_environment` forwards every env var matching the canonical provider-key namespace (`CAMBIUM_PROVIDER_*_API_KEY`) regardless of `provider_env_keys` (`supervisor.py:824-826`).
- Module-test gate fails: the baseline records 278 foreign scenario node IDs, and the offline `test_subprocess_network_client_is_denied` raises `PermissionError`.
- No FD-3 transport; the supervisor uses an unbounded `asyncio.Queue` (`supervisor.py:448`), and the DLQ is unwired.
- Architectus execution core exists (`decide`/`step`/`compose_context`/`aggregate`) but is not wired into the supervisor; `orchestrator.py` is still a submit/drain skeleton.
- M1 deletion set still present: slice `EventLog` (`supervisor.py:105`), `_FallbackEventStore` (`:930`), `_FallbackSequencer` (`:1016`), and `events.py` (only importer is `orchestrator.py`).

## In flight (worktrees, from `git worktree list` 2026-08-10)
- wt-agents-condense, wt-auth-store, wt-dataset-version, wt-dspy-cambiumlm (CambiumLM still branch-local), wt-eval-cache-fix, wt-module-conformance, wt-staging-quarantine, wt-super-hardening-v2, wt-module-cli, wt-status-refresh
- Scratch/derivative worktrees at `b709375`: wt-fix-baseline, wt-module-offline-test, wt-packaging-bench, wt-redact-store, wt-supervisor-consolidation, wt-tasktree-leak, wt-worker-tool-loop, wt-tracker-refresh

## Next actions (dependency order)
1. Merge supervisor consolidation: credential isolation (fix the `_worker_environment` leak), provider default, plan validation, `cambium.worker` default.
2. Merge module baseline regeneration (re-scope to module-local node IDs) + offline-test fix.
3. Merge worker tool-loop (marker-append → real tool loop).
4. Supervisor serial wave: publish-integrity guards, redaction wiring (enqueue/INSERT), `result.json` wiring, M1 deletion (`EventLog`/fallbacks/`events.py`/slice path).
5. Real-provider E2E via `cambium auth run supervisor`.
6. M5 integration (wire Architectus), M7 pool, M8 SIMBA + `example`→`should_decompose` rename, M9 adoption.
7. Final: re-baseline module-scoped, worktree cleanup, DELETE THIS FILE, final report.

## Verification norms (canaries)
Every agent: exact commands + outputs; UNVERIFIED markers; commit in own worktree (check git rev-parse --show-toplevel); empty report = failure; snapshots of live systems need as-of timestamps; adversarial review before merge; duplicate/conflicting branches resolved toward main + re-verify.
