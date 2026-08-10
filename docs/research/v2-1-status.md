# Cambium capability and gap tracker

This is the sole detailed live capability/gap table. Source and tests remain
the authority; this page contains no branch, SHA, or test-count bookkeeping.

| Area | Current capability | Gap / next proof |
| --- | --- | --- |
| CLI and diagnostics | `pyproject.toml` registers `cambium.cli:main` with `auth`, `supervisor`, `doctor`, `bench`, `tasktree`, `module-test`, and `version`. `doctor` checks runtime, worktrees, provider/auth state, optional stores, datasets, and host health. | Diagnostics are not production admission or approval. |
| Plan supervisor | `supervisor.run_plan` validates a flat supplied task list, fans out under one `asyncio.TaskGroup`, runs gates, persists `EventStore` events, writes a root result, and publishes successful commits by ref-only update. | No dependency DAG scheduling, hierarchy admission, or dynamic decomposition. The one-task `run_session` adapter remains. |
| Worker | `worker.do_work` has deterministic marker mode and a custom bounded provider/tool loop. The loop parses strict actions, dispatches validated tools, checkpoints, tracks bounded usage/transcript data, and makes one fenced commit. | No production per-worker OS sandbox or approval service in the run-plan worker context. |
| Providers | `Diffundo` performs tiered priority ordering with cooldown, circuit-breaker, configured RPM request-rate buckets, and bounded retry behavior. A depleted bucket reports `RATE_LIMITED`; HTTP 429 `Retry-After` is honored. | Provider token, cost, account-quota ownership, and privacy contract must precede weighted routing. External-provider acceptance is not evidenced. |
| Event path | Worker stdout is NDJSON; each worker has a bounded decoded-stdout queue. Runtime records route through the bounded `store.EventStore` writer queue at `.cambium/events.db`. | Non-critical overflow is policy-dropped; end-to-end usage/accounting and operational dashboards are open. There is no current `events.py` or DLQ module. |
| Publication | Gates, fencing, merge sequencing, expected-old ref checks, cleanup, and `.cambium/result.json` are active in `run_plan`. | Publication is still ref-only by design; consumer checkouts need explicit materialization. |
| Task tree | `build_tree`, `topological_order`, and `ready_tasks` validate roots, dependencies, cycles, and bounds. `build_tree` deep-copies input specs into node snapshots. | `run_plan` bypasses these helpers. Integrate ready-node dispatch, result envelopes, and failure propagation. |
| Architectus and conversations | `ArchitectusCore` is tested with injected LLMs. The conversation database and doctor check exist as separate modules. | No Architectus caller, dynamic decomposition admission, or conversation-store wiring exists in `run_plan`; `orchestrator.py` remains a skeleton. |
| Controls | `tools.py` consumes schemas, command policy, and injected gates; `approval.py` defines `ApprovalGate`, and `resources.py` defines `CompileGate`. Provider environments are allowlisted and redaction is available. | No production approval callback or per-worker OS containment. `ResourceBudget`, `worker_pool.py`, `dlq.py`, `events.py`, and `eval_cache.py` are not tracked modules. |
| Module evaluation | `modules/example` exposes deterministic `decide` and `evaluate` JSON operations, split evaluators, train/eval/canary data, and metrics. `module_conformance` provides an isolated `module-test` gate. | Example evaluation is offline module evidence, not planner or whole-system optimization proof. |
| Provider smoke | No committed external-provider smoke command or artifact exists, and credentials/configuration are absent. | Run an opt-in disposable smoke through the worker loop, gate, and ref-only merge once credentials exist; do not treat local fixtures as acceptance evidence. |

## Ordered gaps

1. Production hierarchy and dynamic admission with validated TaskTree revisions.
2. Per-worker OS containment and a fail-closed production approval boundary.
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
