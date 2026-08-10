# Cambium capability and gap tracker

This is the sole detailed live capability/gap table. Source and tests remain
the authority; this page contains no branch, SHA, or test-count bookkeeping.

| Area | Current capability | Gap / next proof |
| --- | --- | --- |
| CLI and diagnostics | `pyproject.toml` registers `cambium.cli:main` with `auth`, `supervisor`, `doctor`, `bench`, `tasktree`, `module-test`, and `version`. `doctor` checks runtime, worktrees, provider/auth state, optional stores, datasets, and host health. | Diagnostics are not production admission or approval. |
| Plan supervisor | `supervisor.run_plan` validates a flat list, fans out under one `asyncio.TaskGroup`, persists events, writes a root result, and publishes a clean worker whose envelope reports `succeeded` by ref-only update. There is no pre-merge gate: the worker verdict alone decides merge eligibility (removed by product decision). No worker-count semaphore: an 11-task canary observed 11 concurrent supervisions. | No bounded worker admission, DAG scheduling, hierarchy admission, or dynamic decomposition. `resource_thresholds` remains the only host-health pre-flight. Static ready-node waves are the first hierarchy target; dynamic admission follows. |
| Worker | `worker.do_work` has deterministic marker and bounded provider/tool modes with strict actions, checkpoints, bounded usage, and one fenced commit. | No per-worker OS sandbox. Worktree/process-group isolation is the only worker boundary. |
| Providers | `Diffundo` performs tiered priority ordering with cooldown, circuit-breaker, configured RPM request-rate buckets, and bounded retry behavior. A depleted bucket reports `RATE_LIMITED`; HTTP 429 `Retry-After` is honored. | Provider token, cost, account-quota ownership, privacy, prompt-prefix stability, and provider cache-hit metrics must precede weighted routing. External-provider acceptance is not evidenced. |
| Event path | Worker stdout is NDJSON; each worker has a bounded decoded-stdout queue. Runtime records route through the bounded `store.EventStore` writer queue at `.cambium/events.db`. | Non-critical overflow is policy-dropped; end-to-end usage/accounting and operational dashboards are open. There is no current `events.py` or DLQ module. |
| Publication | Fencing, merge sequencing, expected-old ref checks, cleanup, and `.cambium/result.json` are active in `run_plan`. | Publication is still ref-only by design; consumer checkouts need explicit materialization. |
| Task tree | `build_tree`, `topological_order`, and `ready_tasks` validate roots, dependencies, cycles, and bounds. `build_tree` deep-copies input specs into node snapshots. | `run_plan` bypasses these helpers. Integrate static ready-node waves with fresh bounded child contexts and strict upward envelopes; admit dynamic children only through validated revisions. |
| Architectus and conversations | `ArchitectusCore` is tested with injected LLMs. The conversation database and doctor check exist as separate modules. | No Architectus caller, dynamic decomposition admission, or conversation-store wiring exists in `run_plan`; `orchestrator.py` remains a skeleton. |
| Controls | `tools.py` consumes schemas and validates arguments; the git allowlist stays. Provider environments are allowlisted and redaction is available. `approval.py` defines `ApprovalGate` and `resources.py` defines `CompileGate` as standalone reusable primitives. | Approval and compile-gate checks were removed from `run_shell`/`git_op` by product decision (local development tool, no production gating). `run_plan` no longer runs gate commands, so `CompileGate` bounds nothing there. `ResourceBudget`, `worker_pool.py`, `dlq.py`, `events.py`, and `eval_cache.py` are not tracked modules. |
| Module evaluation | `modules/example` exposes deterministic `decide` and `evaluate` JSON operations, split evaluators, train/eval/canary data, and metrics. `module_conformance` provides an isolated `module-test` gate. | Example evaluation is offline module evidence, not planner or whole-system optimization proof. |
| Provider smoke | No committed external-provider smoke command or artifact exists. Deployment credentials/configuration are external and ephemeral; doctor currently reports no runnable configured provider. | Gates, the escaped-secret canary, OS containment, and production approval were removed by product decision. An opt-in disposable smoke remains possible once usable credentials/configuration exist; local fixtures are not acceptance evidence. |

## Ordered gaps

1. Bounded worker admission (no worker-count semaphore in `run_plan`).
2. Smallest static ready-node slice with harness-owned tree, fresh bounded child contexts, and strict envelopes; then validated dynamic admission.
3. Provider usage observability and quota contract; weighted routing follows.
4. Opt-in external-provider smoke once credentials exist.

See [`../../implementation-plan.md`](../../implementation-plan.md) for ordered
work and [`../architecture/architecture.md`](../architecture/architecture.md)
for the current-versus-target contract.

## Evidence pointers

- Runtime: `src/cambium/supervisor.py`, `worker.py`, `ipc.py`, `store.py`, and
  `merge.py`.
- Providers and adapters: `src/cambium/diffundo.py`, `provider_config.py`, and
  `lm.py`; scenarios are under `tests/scenarios/`.
- Trees, controls, diagnostics, and evaluation:
  `src/cambium/tasktree.py`, `tools.py`, `approval.py`, `resources.py`,
  `doctor.py`, `module_conformance.py`, and `modules/example/`.
