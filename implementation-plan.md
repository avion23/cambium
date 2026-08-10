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
- Redaction is still absent from main; Diffundo and the M6 fake-provider staging path are merged.
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

## Merged state (main, clean)
- Test suite: 307 tests are collected; the verified full run reports 305 passed and 2 skipped; the committed baseline records all 307 node IDs.
- Bench: pytest plugin with a committed module-local baseline, drift gate, dataset-version re-anchor, and fail-closed missing-anchor behavior.
- M6: Diffundo, provider configuration, fake-provider staging, and M6-hygiene quota/publish-scope assertions are merged; real-provider acceptance remains unverified.
- Recent merges: status tracker, glossary, agents inventory, decision enum, research index, Diffundo, M6 staging, M6-hygiene (`4c4065f`), and the current pure worker-pool state seed.

## In flight (worktrees)
- wt-impl-super: supervisor review-fix RELAUNCHED fresh (env stripping everywhere, write deadlines, fencing, ping/pong, race test) — CRITICAL PATH
- wt-redact: redact.py (has uncommitted progress — alive)
- wt-impl-diffundo: review fixes (busy-spin pause, budget-bounded attempts, refusal scan)
- wt-impl-bench: review fixes (dataset_version re-anchor, canary_failed_delta wiring, CLI baseline protection, baseline regen)
- Luna wave: luna-baseline (README+baseline), luna-tools (tool dispatch), luna-envsmoke, luna-edits (anchored edits), luna-template, luna-docx (doctor ext), luna-fuzz (IPC fuzz), luna-convtok (conversations tokens/summary nodes)

## Next actions (dependency order)
1. Merge super fix → merge diffundo fix → merge redact → merge bench fix → activate m6/conformance skips
2. HARDENING WIRING into supervisor: pipe caps+kill, stdin deadlines (in super fix), redaction filter on events, DLQ put on task failure, CompileGate on heavy gates, fencing file (in super fix)
3. M1 canonicalization (m1-canonicalization-plan.md): run_session→adapter, delete slice machinery + events.py + orchestrator skeleton, re-run 3 audits
4. M6: real-provider E2E (provider_config + diffundo + worker; keys env-only; user's machine has 11 configured providers)
5. M5: architectus executor chunk (run_plan integration), M7 worker pool, M8 DSPy SIMBA + example→should_decompose rename, M9 adoption trial (≥25% token cut, ≤2pt compile degradation)
6. Final: integration verification, worktree cleanup, DELETE THIS FILE, final report

## Verification norms (canaries)
Every agent: exact commands + outputs; UNVERIFIED markers; commit in own worktree (check git rev-parse --show-toplevel); empty report = failure; snapshots of live systems need as-of timestamps; adversarial review before merge; duplicate/conflicting branches resolved toward main + re-verify.
