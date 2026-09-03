# Event-kind glossary

Reference for the durable event kinds emitted through `store.EventStore`
(`.cambium/events.db`). Emit sites are file-level; line numbers rot, so none
are given — grep the kind string to find the emitter. `merge_*` covers every
merge-prefixed kind found in the emit sites. `render.py` uses the same
vocabulary.

| Kind | Emit site | Meaning |
| --- | --- | --- |
| `task_assigned` | `supervisor.py` | Records validated task admission, branch/base, provider assignment, and requirements. |
| `child_admitted` | `supervisor.py` | Records a validated child revision before its task is created/spawned. |
| `child_rejected` | `supervisor.py` | Records a rejected child proposal; validation, persistence, or child creation failed. |
| `child_result` | `supervisor.py` | Records the bounded child resume envelope the parent consumes at join. |
| `child_failed` | `supervisor.py` | Records a child whose terminal result status was not succeeded, with its bounded reason. |
| `spawned` | `supervisor.py` | Records launch of a fresh worker process and the credential-name allowlist. |
| `init` | `supervisor.py` | Records the supervisor-to-worker initialization handshake for a generation. |
| `ready` | `supervisor.py` | Records the worker's correlated init acknowledgment and readiness to run. |
| `reuse_ready` | `supervisor.py` (worker message in `worker.py`) | Records a worker reporting itself reusable and kept alive for pooling. |
| `run_task` | `supervisor.py` | Records dispatch of the correlated task payload to the ready worker. |
| `worker_reused` | `supervisor.py` | Records dispatch onto a pooled worker process instead of a fresh spawn. |
| `ping` | `supervisor.py` | Records a supervisor liveness ping with its correlated request id. |
| `pong` | `supervisor.py` | Records the worker's correlated pong before its deadline. |
| `protocol` | `supervisor.py` | Records a supervisor-to-worker wire write failure and the worker kill that follows. |
| `parse_error` | `supervisor.py` | Records an undecodable worker stdout line with a truncated message. |
| `log` | `supervisor.py` | Records a bounded worker log/diagnostic line (stderr tail, worker log/error messages, EOF lifecycle notes). |
| `heartbeat` | `worker.py`; forwarded by `supervisor.py` | Periodic worker liveness/progress; current fields are `turn`, `tool`, `status`. |
| `tool_event` | `worker.py`; forwarded by `supervisor.py` | Redacted bounded tool invocation/result with turn, outcome, command, and duration. |
| `tool_output_delta` | `worker.py`; forwarded by `supervisor.py` | Records a redacted streamed chunk of tool output with tool, turn, and stream name. |
| `usage_event` | `worker.py`; forwarded by `supervisor.py` | Redacted provider call usage, cost, latency, rate, quota, and context accounting. |
| `provider_boundary_degraded` | `supervisor.py` (worker message in `worker.py`) | Records a provider call that failed at the worker boundary with its typed `error_type`. |
| `checkpoint` | `worker.py`; forwarded by `supervisor.py` | Records an ordinary turn checkpoint reference and commits-so-far. |
| `context_checkpoint` | `worker.py`; forwarded by `supervisor.py` | Records an immutable epoch checkpoint and its cache descriptor. |
| `context_epoch_advanced` | `worker.py`; forwarded by `supervisor.py` | Records a successful fold from one context epoch to its successor. |
| `context_fork` | `supervisor.py` | Records the parent's epoch and whether a child gets exact or semantic context reuse. |
| `context_fork_skipped` | `worker.py`; forwarded by `supervisor.py` | Records that an unavailable, incompatible, or invalid epoch was not reused. |
| `context_resume` | `supervisor.py` | Records a suspended parent resuming after bounded child results and join checks. |
| `context_resume_failed` | `supervisor.py` | Records a parent resume after child join failing its budget/deadline checks. |
| `compaction_failed` | `worker.py`; forwarded by `supervisor.py` | Records a context-compaction failure with its epoch and bounded reason. |
| `compaction_deferred` | `worker.py` | Worker-wire notice for a malformed/invalid summary fold deferred before the bounded retry limit. |
| `worktree_salvaged` | `supervisor.py` | Records a bounded dirty-worktree evidence artifact captured before recovery or cleanup. |
| `worktree_pruned` | `supervisor.py` | Records successful removal of a task worktree and branch. |
| `worktree_cleanup_deferred` | `supervisor.py` | Records cleanup being retained/deferred because a safety or removal step failed. |
| `provider_infeasible` | `supervisor.py` | Records a provider rejected at admission because its required credential is unavailable. |
| `resource_denied` | `supervisor.py` | Records a heavy-resource gate denial with bounded reasons before provider feasibility. |
| `merge_started` | `supervisor.py` | Records start of private child integration or ref-only publication. |
| `parent_snapshot` | `supervisor.py` | Records the parent base/head snapshot taken before child integration. |
| `child_integration_prepared` | `supervisor.py` | Records prepared child staging (old/new) awaiting the serialized integration advance. |
| `child_integrated` | `supervisor.py` | Records the accepted expected-old child integration ref advance. |
| `join_invariant_failed` | `supervisor.py` | Records that the parent worktree HEAD does not match the accepted integration head at join. |
| `merge_committed` | `supervisor.py`; recovery in `merge.py` | Records an accepted expected-old ref advance, including recovered publication. |
| `merge_failed` | `supervisor.py` | Records merge/publication failure; conflicts carry a structured `merge_conflict` status. |
| `merge_reconciled` | `supervisor.py`; sequencer in `merge.py` | Records startup discovery that the ref advanced before its commit event. |
| `merge_staging_prune_started` | `merge.py`; flushed by `supervisor.py` | Records the start of bounded stale staging/quarantine pruning. |
| `merge_staging_pruned` | `merge.py`; flushed by `supervisor.py` | Records removal of one stale staging/quarantine artifact. |
| `merge_staging_quarantined` | `merge.py`; flushed by `supervisor.py` | Records dirty staging moved into the bounded quarantine. |
| `merge_staging_cleanup_failed` | `merge.py`; fallback in `supervisor.py` | Records staging cleanup failure that prevents silent artifact loss. |
| `resolver_staging_prepared` | `supervisor.py` | Records prepared conflict-resolver staging with diff evidence and conflicted files. |
| `resolver_child_admitted` | `supervisor.py` | Records admission of a bounded conflict-resolver child attempt. |
| `resolver_succeeded` | `supervisor.py` | Records a resolver attempt that produced the accepted integration head. |
| `resolver_failed` | `supervisor.py` | Records a failed or exhausted conflict-resolver attempt with its status and bounded reason. |
| `resolver_cleanup_failed` | `supervisor.py` | Records failure to prune the resolver worktree after an attempt. |
| `result` | `supervisor.py` (worker envelope in `worker.py`) | Records the correlated terminal worker verdict, bounded `terminal_action` (`type`, `objective_met`, and summary presence), and redacted provider metadata. |
| `exit` | `supervisor.py` (worker `exit_message` in `worker.py`) | Records the worker generation's terminal exit reason. |
| `worker_failed` | `supervisor.py` | Records task/generation failure after protocol, integrity, recovery, or restart exhaustion. |
| `restart_scheduled` | `supervisor.py` | Records a scheduled worker restart with its count, max, and bounded backoff delay. |
| `worker_terminated` | `supervisor.py` | Records an orphaned worker found at startup with its pid, reason, and audited event sequence range. |
| `timeout` | `supervisor.py` | Records a wall, ready, heartbeat, pong, or stdin deadline timeout and its phase. |
| `recover` | `supervisor.py` | Records fenced worktree reset/clean recovery and the next generation. |
| `session_ended` | `supervisor.py` | Records final session status and per-task statuses after shutdown cleanup. |

`requires_commit` is an envelope field, not an event kind: the supervisor
passes it from the task spec and the worker echoes it in `result_envelope`.
