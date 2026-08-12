# Cambium capability and gap tracker

This is the sole detailed live capability/gap table. Source and tests remain
the authority; this page contains no branch, SHA, or test-count bookkeeping.

| Area | Current capability | Gap / next proof |
| --- | --- | --- |
| CLI and diagnostics | The `cambium` CLI runs directly from source (`PYTHONPATH=src python3.14 -m cambium.cli`); no wheel or install is supported. Commands: `auth`, `supervisor`, `doctor`, `bench`, `tasktree`, `module-test`, `version`, `run`, `repl`, `tui`, `session`, and `architectus` (`session list/latest/show` reads completed session results). `doctor` checks runtime, worktrees, provider/auth state, optional stores, datasets, and host health; its provider check reads `CAMBIUM_PROVIDERS` or `<cwd>/.cambium/providers.json`, not the trusted user config that `run` uses. | Diagnostics are not production admission or approval. |
| Plan supervisor | `supervisor.run_plan` validates the plan, persists events, writes a root result, and publishes a clean worker whose envelope reports `succeeded` with a commit by ref-only update. A clean provider-backed worker that changed no files finishes successfully with zero commits: no empty commit or merge occurs and nothing is published. There is no task-command pre-merge gate: a succeeded envelope with a commit proceeds to merge only after the repository-integrity checks pass (worker success integrity, fencing, expected-old ref publication, session admission, worktree confinement, protocol/request correlation, and quarantine). A session-wide parallel-worker cap defaults to one worker per CPU on both dispatch paths (`max_concurrent_tasks=0` disables it). A flat plan (no `depends_on`) fans out under one `asyncio.TaskGroup`; the flat canary `test_flat_plan_ignores_max_width_and_preserves_canary` (four tasks) shows `max_width` is ignored on that path. A plan with `depends_on` is built into a validated `TaskTree` and dispatched in static ready-node waves bounded by `max_width`. | Dynamic child admission, dynamic decomposition, and weighted routing remain follow-on work. `resource_thresholds` remains the only host-health pre-flight. |
| Worker | `worker.do_work` has deterministic marker and bounded provider/tool modes with strict actions, checkpoints, and bounded usage. The marker mode always writes its marker and makes one fenced commit. The provider mode makes at most one fenced commit: one when the agent changed files, zero for a clean valid no-change/read-only finish (no empty commit or merge). | No per-worker OS sandbox. Worktree/process-group isolation is the only worker boundary. |
| Providers | `Diffundo` performs tiered priority ordering with cooldown, circuit-breaker, configured RPM request-rate buckets, and bounded retry behavior. A depleted bucket reports `RATE_LIMITED`; HTTP 429 `Retry-After` is honored. | Provider token, cost, account-quota ownership, privacy, prompt-prefix stability, and provider cache-hit metrics must precede weighted routing. External-provider acceptance is not evidenced. |
| Event path | Worker stdout is NDJSON; each worker has a bounded decoded-stdout queue. Runtime records route through the bounded `store.EventStore` writer queue at `.cambium/events.db`. | Non-critical overflow is policy-dropped; end-to-end usage/accounting and operational dashboards are open. There is no current `events.py` or DLQ module. |
| IPC framing | Worker stdout is NDJSON; malformed lines that fail JSON parsing are counted and skipped up to a bound. | Known open defect, not a new task: a valid JSON line that is not an object currently fails supervision (`agents.md` Boundary invariants; `architecture.md` §2). |
| Publication | Fencing, merge sequencing, expected-old ref checks, cleanup, and `.cambium/result.json` are active in `run_plan`. | Publication is still ref-only by design; consumer checkouts need explicit materialization. |
| Task tree | `build_tree`, `topological_order`, and `ready_tasks` validate roots, dependencies, cycles, and bounds. `build_tree` deep-copies input specs into node snapshots. `run_plan` integrates them on the hierarchy path: a plan with `depends_on` dispatches static ready-node waves, each child receiving a fresh bounded context derived from its own spec plus the strict parent envelope key set. | Dynamic child admission — a parent proposing a typed tree revision validated and durably admitted before dispatch — remains follow-on work. |
| Architectus and conversations | `ArchitectusCore` drives child admission through the injected decision port: `run_plan(architectus=...)` feeds each admitted parent's terminal envelope to `aggregate`/`step` and routes the resulting typed proposals through the existing `_admit_child` revision validation (never the live tree directly). `conversations=True` opens `ConversationStore` at `<session_dir>/.cambium/conversations.db` with the same session lifecycle and appends one `kind="system"` row per admitted/rejected revision (node_id = child task id, parent in `meta`). The public `Orchestrator.run` forwards both options; `cambium supervisor --conversations` exposes the flag on the CLI. `ArchitectusCore` failure waves are atomic; `ArchitectusLM` adapts `CambiumLM` to the async `decide` port. | Live decomposition is provider-backed via `cambium architectus` (`ArchitectusLM` over a configured `CambiumLM`, defaulting to the first enabled provider; `--dry-run`/`--scripted` uses `ScriptedLLM` with no credentials). Port tests still use `ScriptedLLM`; the live path is a smoke-level proof, not a decomposition benchmark. |
| Controls | `tools.py` consumes schemas and validates arguments; the git allowlist stays. Provider environments are allowlisted and redaction is available. Worker `run_shell` and worker-exposed `git_op` do not consult `ApprovalGate` or `CompileGate`; `approval.py` and `resources.py` are deleted. | `ResourceBudget`, `worker_pool.py`, `dlq.py`, `events.py`, and `eval_cache.py` are not tracked modules. |
| Module evaluation | `modules/example` and `modules/should_review` expose deterministic `decide` and `evaluate` JSON operations, split evaluators, train/eval/canary data, and metrics. `module_conformance` provides an isolated `module-test` gate. | Example evaluation is offline module evidence, not planner or whole-system optimization proof. |
| Provider smoke | `scripts/external-provider-smoke.sh` is the committed opt-in smoke: it refuses to run without `CAMBIUM_SMOKE_PROVIDER_CONFIG` (a real, non-loopback provider config whose credential env keys are set), runs one disposable provider-backed task through the custom worker loop (tool/checkpoint events, ref-only merge), and verifies the acceptance measures: exactly one ref update touching only the smoke fixture, durable `usage_event` rows with provider/model, and an unchanged `main` on the failure fixture. | The smoke is networked and credential-gated by design; it runs only when real credentials/configuration exist. Local fixtures are regression coverage, not acceptance evidence. |

## Ordered gaps

1. Static ready-node slice: landed. `run_plan` builds one harness-owned
   `TaskTree` for plans with `depends_on`, dispatches static ready-node waves
   bounded by `max_width`, gives each child a fresh context (own spec + strict
   parent envelope), and cascades failed nodes so dependants are never spawned.
   Flat plans keep the one-`TaskGroup` fan-out under the session-wide default
   CPU-count cap.
2. Validated dynamic child admission: landed. A parent proposes a typed tree
   revision (`propose_child`) that the harness validates with
   `tasktree.build_tree` over the accumulated session tasks and durably
   records (`child_admitted`/`child_rejected`) before dispatch; the injected
   Architectus decision port and the conversation store are wired only at
   this boundary, and the public `Orchestrator.run` plus
   `cambium supervisor --conversations` expose them.
3. Provider usage observability and quota contract: landed. Each router call
   persists one redacted `usage_event` (provider, model, turn, tokens,
   estimated cost, latency, Retry-After, request-rate status,
   account-quota owner, prompt-prefix bytes, provider-reported cache-hit,
   failure reason; un-reported fields omitted); the usage-debt ledger
   (`routing-state.json`, `DebtStore`) feeds admission-time balancing and
   `scripts/usage_evidence.py` aggregates cross-session routing evidence.
   Weighted routing follows only after that evidence is stable.
4. Opt-in external-provider smoke: `scripts/external-provider-smoke.sh` is
   committed and credential-gated; it runs once real credentials/configuration
   exist.

See [`../../implementation-plan.md`](../../implementation-plan.md) for ordered
work and [`../architecture/architecture.md`](../architecture/architecture.md)
for the current-versus-target contract.

## Evidence pointers

- Runtime: `src/cambium/supervisor.py`, `worker.py`, `ipc.py`, `store.py`, and
  `merge.py`.
- Providers and adapters: `src/cambium/diffundo.py`, `provider_config.py`, and
  `lm.py`; scenarios are under `tests/scenarios/`.
- Trees, controls, diagnostics, and evaluation:
  `src/cambium/tasktree.py`, `tools.py`,
  `doctor.py`, `module_conformance.py`, `modules/example/`, and
  `modules/should_review/`.
