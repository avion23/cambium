# Cambium capability and gap tracker

This is a concise snapshot of the checked-in runtime. Source and tests remain
the authority. Recheck the table after integration work; it contains no branch
or commit bookkeeping.

## Capability summary

| Area | Current capability | Gap |
| --- | --- | --- |
| Plan supervisor | `supervisor.run_plan` validates a flat task list, supervises tasks concurrently, runs gates, and publishes successful commits by ref update. | It does not schedule a dependency DAG; the one-task `run_session` slice and supervisor fallback paths remain. |
| Worker | Deterministic marker mode and a bounded provider tool loop are implemented and scenario-tested. | A real external-provider release proof is not complete. |
| Providers and LM | Diffundo routing, provider configuration, `CambiumLM`, and `ArchitectusLM` are merged; loopback provider integration is tested. | No public package API exposes these capabilities, and external-provider acceptance is unverified. |
| Task tree | `build_tree`, `topological_order`, and `ready_tasks` validate and inspect dependency graphs. | `run_plan` bypasses them; fixed-tree scheduling is not wired. |
| Architectus | Pure `ArchitectusCore` behavior is covered with injected test LLMs. | No production caller; `orchestrator.py` is still a skeleton. |
| Worker pool | Pure state-machine seed and scenario tests exist. | No supervisor integration or persistent worker reuse. |
| Persistence and merge | SQLite WAL `EventStore`, `MergeSequencer`, fencing, worktree cleanup, and result-record modules exist and are tested. | Supervisor still keeps `EventLog`, fallback store/sequencer, and slice paths; canonical redaction/result wiring is incomplete. |
| IPC and controls | NDJSON framing, request IDs, line limits, gates, approval, resource helpers, schemas, provider environment filtering, redaction, and DLQ modules exist. | End-to-end bounded supervisor queues, durable overflow routing, and some control wiring remain open. |
| Example module | `modules/example` has deterministic decision logic, train/eval/canary data, a metric, CLI, and DSPy seam. | It is not the runtime planner or a proof of whole-system optimization. |

## Gaps in delivery order

1. Canonicalize the runtime and wire controls at the supervisor boundaries.
2. Prove one thin real-provider worker → gate → merge path with explicit
   credentials and no default network run.
3. Integrate fixed-tree validation and ready-node scheduling with the
   supervisor; keep dynamic replanning out of this first proof.
4. Measure worker reuse, provider routing, and context or DSPy experiments only
   after the preceding path is reproducible.

## Evidence pointers

- Runtime entry points: `src/cambium/supervisor.py`, `worker.py`,
  `tasktree.py`, `architectus.py`, and `worker_pool.py`.
- Provider and adapter tests: `tests/scenarios/test_worker_provider.py`,
  `test_lm.py`, and `test_diffundo*.py`.
- Plan, merge, IPC, controls, and store tests: the corresponding files under
  `tests/scenarios/`.
- Authority and target boundaries: [`docs/architecture/architecture.md`](../architecture/architecture.md).
