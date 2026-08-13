# high-performance agent orchestrator readiness

## Executive summary

Cambium already has a substantial single-session multi-worker runtime: validated task trees, dependency-aware dispatch, bounded concurrent worker subprocesses, worker budgets, restart generations, durable redacted session events, provider usage accounting, and diff review/merge controls. The highest-impact readiness gaps are above that foundation: there is no evidence of a durable cross-session task queue and fleet-wide worker/resource scheduler; decomposition is a bounded supplied/LLM-directed DAG rather than a complete autonomous planning service; supervisor-crash resume and replay-safe recovery are not established; and telemetry is an event/usage audit trail rather than fleet-level latency, cost, tracing, and reliability metrics. Security is repository/process isolation rather than a hostile-code sandbox, and verification, provider capability parity, and clean installation need explicit end-to-end contracts. Claims below are tied to source evidence; items marked TODO require targeted confirmation before being treated as a final deficiency.

## Scored gap table

| Dimension | Current capability | Missing capability / deficiency | Severity |
|---|---|---|---|
| 1. Parallelism and throughput | One supervisor can dispatch multiple plan tasks under an `asyncio.TaskGroup`, with a session semaphore and optional warm worker pool. Evidence: `src/cambium/supervisor.py:4-5,1117-1139,3770-3785`. | No demonstrated durable cross-session queue, fleet-wide worker pool, fair scheduling, or global CPU/memory/provider backpressure; ten worktrees are limited by configured session width and host/provider capacity. | **High** |
| 2. Task decomposition | `tasktree.py` validates a rooted bounded DAG; `architectus.py` admits dependency-ready actions up to `max_width`. Evidence: `src/cambium/tasktree.py:1-28,268-275,392-410`; `src/cambium/architectus.py:226-301,390-433`. | No evidence of an autonomous decomposition service that turns one goal into an optimized durable DAG, supports joins/multi-parent dependencies, or rebalances a live queue. | **High** |
| 3. Cost and latency control per turn | Worker and supervisor expose turn/token/wall budgets, watchdogs, bounded waits, restart counts, fresh restart generations, and jittered backoff. Evidence: `src/cambium/worker.py:133-153,1980-2030,2140-2182`; `src/cambium/supervisor.py:163-175,1989-2191`. | Restart is replay by fresh process and budget/retry accounting is not shown to be one charge-aware per-turn contract across generations, provider retries, tool time, queue time, and dollar cost. | **High** |
| 4. Context management | Architectus separates static and dynamic context and evicts dynamic records against a context budget; upward results use an exact restricted envelope. Evidence: `src/cambium/architectus.py:301-342,548-582`; `src/cambium/tasktree.py:22-28,418-448`. | `max_turns` bounds loop iterations, not necessarily transcript/tool-output tokens; no single provider-independent transcript compaction and token-window policy is established for all worker requests/restarts. | **High** |
| 5. Observability | Session events are durable in SQLite; usage events feed session stats and routing debt. Evidence: `src/cambium/supervisor.py:12-14,895-902,3799-3800`; `src/cambium/stats.py:1-10,80-173`; `src/cambium/routing.py:189-204`. | No identified distributed tracing, metrics export, cross-session time series, latency percentiles, or provider reliability/SLO aggregation. | **High** |
| 6. Reliability and failure recovery | Plan/events/result/conversation artifacts, admission locking, fencing, heartbeats, process-group cleanup, and bounded worker restarts exist. Evidence: `src/cambium/supervisor.py:1002-1020,1267-1304,1932-2191,3181-3212,3770-3800`; `src/cambium/store.py:4-53`. | No cited supported supervisor-crash reconciliation/resume command or complete replayable checkpoint of every external operation; one-shot sessions reject reuse. Evidence: `src/cambium/oneshot.py:435-470`. | **High** |
| 7. Provider integration surface | Provider configuration, Codex OAuth, an OpenAI-shaped Responses path, and compatible usage/error parsing are present. Evidence: `src/cambium/provider_config.py:1-75`; `src/cambium/diffundo.py:61-77,629-656`. | Need a verified capability matrix: configuration labels do not prove equally live protocols/models; generic OpenAI-compatible endpoint, streaming/tool/cancellation parity, and normalized failures are not established by the surveyed evidence. | **Med** |
| 8. Security | Session redaction, credential stores, process groups, argument-vector subprocesses, and worktree-under-session checks exist. Evidence: `src/cambium/redact.py:1457-1489`; `src/cambium/tools.py:632-650`; `src/cambium/worker.py:1395-1400`; `src/cambium/supervisor.py:3233-3255`. | `run_shell` is not an OS/container sandbox: host commands run with the worker account's permissions; no evidence of network, syscall, filesystem, or resource isolation for hostile code. | **High** |
| 9. Verification loop | Worker tools can run commands and supervisor has diff review/undo/merge machinery. Evidence: `src/cambium/tools.py:625-650`; `src/cambium/supervisor.py:2990-3088`. | No evidence that every task must execute a declared test/lint policy and attach machine-checkable verification evidence before terminal success; review is not equivalent to full validation. | **High** |
| 10. Packaging and installation | `pyproject.toml` declares project metadata and `cambium = cambium.cli:main`; tests add `src` to `pythonpath`. Evidence: `pyproject.toml:1-10,22-27`. | No declared build backend is visible in the surveyed metadata and no installation smoke-test evidence is present; clean wheel/editable installation and package discovery should be verified before release. | **Med** |

## 1. Parallelism and throughput

### Current capability

The supervisor module describes multiple worker subprocesses under one `asyncio.TaskGroup` (`src/cambium/supervisor.py:4-5`). `run_session` is explicitly a one-task compatibility adapter (`src/cambium/supervisor.py:25-29,414-428`), while `run_plan` is the multi-task entry point and documents that it runs every task concurrently (`src/cambium/supervisor.py:3756-3785`). The runtime constructs an optional semaphore from `max_concurrent_tasks` (`src/cambium/supervisor.py:1111-1121`) and starts one task per plan entry in the task group (`src/cambium/supervisor.py:3890-3905`). It also has a session-scoped warm pool whose size is controlled by `_warm_pool_size` (`src/cambium/supervisor.py:1130-1139,2198-2244`).

### Gap

This is intra-session fan-out, not a durable orchestrator service. The surveyed supervisor owns one session runtime and one session semaphore; the pool is explicitly session-scoped (`src/cambium/supervisor.py:1050-1053,1136-1139`). There is no cited queue daemon, cross-session lease table, global worker pool, fair scheduler, work stealing, or host-wide resource admission. Thus ten worktrees can be launched only if the plan is admitted and `max_concurrent_tasks`, provider lanes, process/file-descriptor limits, CPU/memory, and provider quotas permit it; the code does not provide a fleet-level controller that automatically coordinates those constraints. The next implementation should add durable queue leases, global/per-provider/per-repository limits, cancellation, fairness, and critical-path-aware admission.

## 2. Task decomposition

### Current capability

`tasktree.py` is a pure JSON-in/JSON-out validator and scheduler input. It requires one root, exactly one parent for non-root nodes, rejects cycles and multi-parent edges, and applies depth and per-parent width bounds (`src/cambium/tasktree.py:1-28,92-129,268-275,310-385`). `ready_tasks` exposes nodes whose parent is finished (`src/cambium/tasktree.py:392-410`). `ArchitectusCore` is a dependency-gated bounded-width state machine (`src/cambium/architectus.py:226-301`); it asks an injected `LLM.decide` for action mappings (`src/cambium/architectus.py:50-74,301-342`) and admits at most `max_width - len(in_flight)` spawn actions (`src/cambium/architectus.py:390-433`).

### Gap

These modules execute a plan or accept LLM action intents; they do not constitute a general decomposition planner. The required one-root/one-parent shape is explicit (`src/cambium/tasktree.py:119-129,310-337`), so joins/multi-parent dependencies are rejected rather than represented. `ArchitectusCore` validates/adopts proposed actions but its LLM is an injected port (`src/cambium/architectus.py:50-74,301-342`); no source evidence shows critical-path estimation, subtask quality scoring, automatic granularity selection, durable dynamic DAG persistence, or queue rebalancing. The one-shot API still builds a plan containing one task (`src/cambium/supervisor.py:414-428`). A high-performance target needs a planner that emits/repairs a durable DAG, supports explicit join/aggregate nodes, and separates planning tokens from worker execution capacity.

## 3. Cost and latency control per turn

### Current capability

The worker's public execution contract includes turn, token, and wall limits (`src/cambium/worker.py:133-153`), and the worker checks budget state while running the loop (`src/cambium/worker.py:1980-2030`). The supervisor bounds stdin draining and durable terminal emission (`src/cambium/supervisor.py:163-175`), uses a watchdog/heartbeat protocol (`src/cambium/supervisor.py:479-500`), and handles `max_restarts` with generation counters and fresh processes (`src/cambium/supervisor.py:1989-2191`). Restart delays use capped exponential full jitter (`src/cambium/supervisor.py:2178-2188`). A restarted task is deliberately spawned fresh rather than returned to the warm pool (`src/cambium/supervisor.py:2318-2323`).

### Gap

A fresh generation bounds wall time for that process, but can replay prompt/tool work. The evidence shows a restart counter and worker-local budgets, not a durable per-turn cost ledger carried across generations (`src/cambium/supervisor.py:2079-2094,2169-2182`). Queue wait, provider request time, tool subprocess time, retry backoff, restart replay, and monetary/token cost need one admission/deadline policy. Provider exhaustion is handled as a routing/failure condition (`src/cambium/architectus.py:186-190`), but a fleet needs explicit quota-aware backoff and retry budgets to avoid synchronized retry storms. Restarts are implemented, not stubs; the deficiency is accounting and global policy, not absence of restart code.

## 4. Context management

### Current capability

Architectus constructs a static prefix plus dynamic tail and evicts dynamic records when a context budget is configured (`src/cambium/architectus.py:301-342`). The core's context budget is read from task configuration and its token estimator/eviction logic is implemented in the same module (`src/cambium/architectus.py:548-582`). The task-tree upward envelope has an exact key set and excludes scratchpad/reasoning/trajectory (`src/cambium/tasktree.py:22-28,418-448`); Architectus also rejects envelopes with extra or missing keys (`src/cambium/architectus.py:344-389`).

### Gap

The bounded Architectus context is not proof that every provider request is bounded. The worker loop's `max_turns` is a turn-count control (`src/cambium/worker.py:1980-2030`), while provider context windows also depend on accumulated messages, tool output, parent data, and restart history. The surveyed evidence does not show one provider-independent transcript compactor, per-message token ledger, maximum tool-output size, or a restart protocol that resumes from a compact checkpoint rather than replaying an unbounded transcript. Add provider-window-aware accounting, deterministic summarization/truncation, and explicit preservation/loss rules for parent envelopes and tool results.

## 5. Observability

### Current capability

The supervisor states that every event is persisted to `.cambium/events.db` (`src/cambium/supervisor.py:12-14`) and exposes `read_events` over the durable store (`src/cambium/supervisor.py:895-902`). `run_plan` creates the event and conversation stores for the session (`src/cambium/supervisor.py:3799-3800,3845-3867`). `stats.py` aggregates provider-usage events from that event log (`src/cambium/stats.py:1-10,80-173`), and the routing ledger maintains durable debt plus an in-memory session accumulator (`src/cambium/routing.py:189-204,304-321`).

### Gap

The event log is an audit stream and the stats module is per-session usage aggregation; neither is a fleet metrics/tracing backend. The surveyed files do not expose a Prometheus/OpenTelemetry exporter or cross-session time-series schema. A high-performance operator cannot derive reliable queue delay, task latency percentiles, tool latency, provider request latency, token burn per turn, retry/restart rate, failure-class rate, or provider SLO/reliability trends without querying many session databases and reconstructing timestamps. Add normalized start/queue/dispatch/request/tool/finish timestamps, trace/span IDs, cumulative token/cost fields, and an export/aggregation service.

## 6. Reliability and failure recovery

### Current capability

The runtime has a session admission lock (`src/cambium/supervisor.py:1002-1020`), process-group shutdown and terminal session events (`src/cambium/supervisor.py:1267-1304`), generation fencing and restart handling (`src/cambium/supervisor.py:1932-2191`), and atomic plan persistence (`src/cambium/supervisor.py:3181-3212`). The store design documents WAL durability and a fatal writer-thread error policy (`src/cambium/store.py:4-53`). Session artifacts include events, conversations, and a result written before `run_plan` returns (`src/cambium/supervisor.py:3770-3800,3924-3928`).

### Gap

Worker restart and supervisor recovery are different. The source documents a one-shot used-session rejection (`src/cambium/oneshot.py:435-470`) and read-only completed-session discovery (`src/cambium/session.py:1-11,34-62`), but the surveyed CLI/runtime paths do not provide a supported `resume` operation that reconciles an interrupted supervisor, orphan workers, event sequence, leases, worktree branches, and provider charges. Durable plan/event/result files are necessary evidence, not a complete replayable checkpoint for every external side effect. The audit found no production `NotImplementedError` claim to make here; any future stub finding should cite its exact production call path rather than benchmark fixtures. Implement supervisor leases/heartbeats, recovery scanning, idempotency keys for spawn/merge/provider operations, and explicit resume/reconcile states.

## 7. Provider integration surface

### Current capability

The repository has a provider configuration module with Codex/ChatGPT OAuth profile material (`src/cambium/provider_config.py:1-75`), OAuth preflight and token injection in the supervisor (`src/cambium/supervisor.py:647-806`), and an OpenAI-compatible Responses-shaped HTTP implementation in `diffundo.py` (`src/cambium/diffundo.py:61-77`). That module also parses cached-token usage and allowlisted quota-owner errors across OpenAI/Anthropic-compatible payload shapes (`src/cambium/diffundo.py:629-656`).

### Gap / evidence boundary

The requested question—whether `codex_responses` is the only fully live path and whether there is a generic OpenAI-compatible path—cannot be answered from provider configuration names alone. `provider_config.py` proves profiles/configuration, while `diffundo.py` proves an OpenAI-shaped HTTP path for its review/undo use; it does not by itself prove that every worker provider mode supports identical streaming, tool calls, cancellation, retries, and usage. **TODO: inspect the worker provider adapter dispatch and provider-specific request functions before asserting a model count or calling another path stubbed.** The readiness gap is a capability matrix and uniform adapter contract: model discovery, endpoint/auth selection, tool/stream semantics, cancellation, rate limits, normalized errors, and per-provider concurrency/cost accounting.

## 8. Security

### Current capability

Session redaction is built from a secret snapshot and supports registering rotated secrets (`src/cambium/redact.py:1457-1469`); OAuth documents are sanitized by the same module (`src/cambium/redact.py:1483-1489`). Tool subprocesses are launched with an argument vector and a new session/process group (`src/cambium/tools.py:625-650`). Worker paths are checked relative to the session scratch root (`src/cambium/worker.py:1395-1400`), and plan worktrees are required to stay under the session directory (`src/cambium/supervisor.py:3233-3255`).

### Gap

These controls reduce accidental leakage and repository collisions but do not sandbox arbitrary code. `run_shell`/tool subprocess creation proves process-group and argv isolation, not a restricted UID, filesystem mount namespace, syscall policy, network deny/allow list, cgroup quota, or secret broker (`src/cambium/tools.py:625-650`). A compromised worker can still use whatever the host account can read or execute and can exfiltrate data through available network access; redaction only changes persisted/rendered data (`src/cambium/redact.py:1457-1489`). For unattended multi-agent operation, add container/VM or OS sandboxing, least-privilege credentials, network/filesystem policy, resource quotas, and secret egress controls.

## 9. Verification loop

### Current capability

The tools layer exposes command execution to the worker (`src/cambium/tools.py:625-650`). The supervisor has a diff-review/undo/merge path: review/undo helpers are invoked around merge preparation and merge failure handling (`src/cambium/supervisor.py:2990-3088`), and the diffundo module documents review/undo behavior and OpenAI-compatible review calls (`src/cambium/diffundo.py:61-77`).

### Gap

The cited execution interface means a worker can run tests or linters; it does not show a universal requirement that it did so. The final result/merge gate must carry declared verification commands, exit status, logs/artifacts, and repository state, and must fail closed for task kinds that require tests. A diff review/undo gate protects the patch and can reject/restore changes, but it is not a substitute for the project's full test, lint, type, security, and integration matrix. Add policy-driven verification after worker completion and before merge, with bounded commands and machine-readable evidence.

## 10. Packaging and installation

### Current capability

`pyproject.toml` declares project metadata, Python `>=3.14`, runtime dependencies, and the console script `cambium = "cambium.cli:main"` (`pyproject.toml:1-10`). It also configures pytest's source-tree `pythonpath` (`pyproject.toml:22-27`).

### Gap / verification boundary

The metadata shown does not declare a `[build-system]` backend or explicit package-discovery configuration (`pyproject.toml:1-27`). Therefore the advertised command is present, but a clean PEP 517 wheel/sdist install and package inclusion are not proven by this file. **TODO: run a clean build/install smoke test outside the checkout (`python -m build`, install the wheel into a fresh environment, then `cambium --help`) before final release.** If the environment supplies a backend implicitly or another file configures it, cite that file and downgrade this gap; otherwise add explicit build metadata and a packaging CI smoke test. The pytest `pythonpath` setting is test configuration, not a production installation mechanism (`pyproject.toml:22-27`).

## Prioritized actions

1. Add a durable queue/scheduler service with global and per-session/provider/repository quotas, leases, fairness, cancellation, and critical-path-aware dispatch.
2. Implement supervisor crash recovery and resume: reconcile orphan processes/worktrees/events, checkpoint turn/tool side effects, and make retries/merges/provider calls idempotent.
3. Unify per-turn context, deadline, retry, and token/cost accounting across providers and restart generations; compact transcripts and cap tool output.
4. Export fleet-level metrics/traces for queue/run/tool/provider latency, token/cost burn, retries, restarts, failures, and provider reliability.
5. Add an explicit autonomous decomposition planner and richer DAG/join semantics, while retaining Architectus validation and information hiding.
6. Make verification policy fail closed and deploy workers in a real OS/container sandbox with least-privilege credentials and network/filesystem/resource controls.
7. Complete the provider capability matrix and clean wheel/install smoke test; document any deliberately partial protocol/model paths.
