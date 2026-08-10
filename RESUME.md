# RESUME INSTRUCTIONS — Cambium (post-compaction seed)

You are the orchestrator of **Cambium**: a Python 3.14 multi-agent coding-agent harness.
This file tells you exactly where things stand and what to do next. Read it FIRST, then
`implementation-plan.md` (decision log + context map), then `docs/research/v2-1-status.md`
(living milestone tracker), then orient via `docs/research/README.md` (tiered index).

## State (verified, main is clean)
- Repo: /home/ubuntu/cambium (git main). Test suite: **~209 passed, 5 skipped** on Python 3.14.7 via `uv run --python 3.14.7 --extra test pytest -q`. Ruff clean on src+tests.
- Full v2 stack merged: architecture (docs/architecture/architecture.md, deltas D1-D8 folded), 42 research docs, and modules: store, merge, ipc, worker, supervisor, orchestrator, tasktree, diffundo, bench, doctor, cli, conversations, dlq, resources, approval, fencing, system_health, lint_diag, ast_tools, schemas, eval_cache, provider_config, architectus, edits, tools, modules/example (Decision enum v2.1).
- The 5 skips are conformance/activation tests that flip to pass when the supervisor fix + redaction merge.
- Design decisions (all in implementation-plan.md decision log): Python >=3.14 regular build; headless-first JSON-Lines interface; NO local LLM cache in production (provider-side prefix caching; static-prefix-top prompts; eval-harness-only cache exists); task tree with info hiding (9-key upward envelope); NO sandboxing in the harness (containment = worktrees + allowlists + approval gates; bwrap removed); non-blocking logging; tests colocated with modules (deletable); coding constitution translated; Prime-Agent patterns (persistent workers, gates, refinement loop); FD-3 protocol channel is M2 (decision record in arch §5); separate single-writer DBs (events = source of truth).

## Critical path (in dependency order)
1. **Supervisor fix (wt-impl-super)** — THE blocker. A fresh fix task was relaunched (the earlier one silently died). Scope: env stripping on ALL spawn paths (slice + gates — the security HIGH), stdin write deadlines, fencing file, ping/pong on EOF, winner-agnostic race test, slice stdout cap. Verify 7 fanout tests + full suite, then MERGE.
2. **Redaction (wt-redact)** — has uncommitted progress; finish + merge; then wire Redactor into supervisor events + DLQ (the security MEDIUM).
3. **Hardening wiring** into supervisor: pipe-buffer caps + kill, DLQ.put on task failure, CompileGate on heavy gates, redaction filter. (Modules already exist: dlq.py, resources.py, approval.py, fencing.py, system_health.py.)
4. **M1 canonicalization** (docs/research/m1-canonicalization-plan.md): run_session → adapter over run_plan, delete slice machinery + events.py + orchestrator skeleton, fake_worker → fixture-only, re-run the 3 audits (conformance/security/constitution), refresh v2-1-status.md.
5. **M6 real-provider E2E**: diffundo + provider_config + worker against a REAL provider (this machine has working keys for google/zai-coding-plan/openrouter/nvidia — env-smoke verified; keys env-only, never log). The staging test (test_m6_staging.py) already passes against a local fake server.
6. **M5 Architectus executor** (run_plan integration), **M7 worker pool** (3 READY workers + background refill — cold-start 2.2s vs 5ms), **M8 DSPy SIMBA** (decision module rename example→should_decompose, DLQ-mined demos), **M9 tree-sitter adoption trial** (≥25% token cut, ≤2pt compile degradation — research done, runtime unverified).
7. Final: full integration verification, worktree cleanup, DELETE implementation-plan.md + this file, final report.

## In-flight worktrees (when you resume, check each for completion; relaunch if silent-dead)
- wt-impl-super (critical), wt-redact, wt-luna-baseline (README+baseline refresh), wt-doc-fb5 (feedback-5 assessment — 19 dispositions decided, see implementation-plan or rerun), wt-luna-directive (core directive + step-back rule), wt-doc-archnotes (FD-3 + single-DB decision records), wt-decifix (Decision test exception type).

## Workflow rules (non-negotiable)
- All implementation via subagents in isolated worktrees (/tmp/opencode/cambium-<name>, branch wt-<name>); never commit to main directly except orchestrator-owned artifacts (this file, implementation-plan.md).
- Every task: canaries — exact commands + outputs, UNVERIFIED markers, commit in own worktree (git rev-parse --show-toplevel check), empty report = failure, as-of timestamps for live snapshots.
- Adversarial review (general backend) before EVERY merge; fix via resumed sessions; re-review only what changed.
- Backends: sol + luna WORK (auth fixed; sol for architecture, luna for grunt); glm depleted; kimi misconfigured (never); general/build (DeepSeek) always works. Stuck agents: check worktree git log for commits; relaunch fresh with commit-early discipline.
- Merge conflicts: resolve toward main's newer content + re-verify (docs sweep pattern).
- Sol's v2.1 roadmap (docs/research/v2-1-review.md) is the authoritative plan: decisions A-F (thin Custos, FD-3 now, single conversations.db, pool at max_width≥4, round-robin FAST + REASONING for evaluation, SIMBA), milestones M1-M9.

## User directives not yet fully executed
- Hardening backlog (pipe caps, semaphore wiring, DLQ wiring) — after super fix.
- "We have to compact soon" — done: this file + decision log + status tracker are the resume path.
- Everything else (critiques 1-5 dispositions, constitution, colocated tests, no-cache, no-sandbox, task tree, logging) is IN the docs — read implementation-plan.md decision log for the full list.
