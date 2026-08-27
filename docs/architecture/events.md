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
| `spawned` | `supervisor.py` | Records launch of a fresh worker process and the credential-name allowlist. |
| `init` | `supervisor.py` | Records the supervisor-to-worker initialization handshake for a generation. |
| `ready` | `supervisor.py` | Records the worker's correlated init acknowledgment and readiness to run. |
| `run_task` | `supervisor.py` | Records dispatch of the correlated task payload to the ready worker. |
| `heartbeat` | `worker.py`; forwarded by `supervisor.py` | Periodic worker liveness/progress; current fields are `turn`, `tool`, `status`. |
| `tool_event` | `worker.py`; forwarded by `supervisor.py` | Redacted bounded tool invocation/result with turn, outcome, command, and duration. |
| `usage_event` | `worker.py`; forwarded by `supervisor.py` | Redacted provider call usage, cost, latency, rate, quota, and context accounting. |
| `checkpoint` | `worker.py`; forwarded by `supervisor.py` | Records an ordinary turn checkpoint reference and commits-so-far. |
| `context_checkpoint` | `worker.py`; forwarded by `supervisor.py` | Records an immutable epoch checkpoint and its cache descriptor. |
| `context_epoch_advanced` | `worker.py`; forwarded by `supervisor.py` | Records a successful fold from one context epoch to its successor. |
| `context_fork` | `supervisor.py` | Records the parent's epoch and whether a child gets exact or semantic context reuse. |
| `context_fork_skipped` | `worker.py`; forwarded by `supervisor.py` | Records that an unavailable, incompatible, or invalid epoch was not reused. |
| `context_resume` | `supervisor.py` | Records a suspended parent resuming after bounded child results and join checks. |
| `compaction_failed` | `worker.py`; forwarded by `supervisor.py` | Records a context-compaction failure with its epoch and bounded reason. |
| `compaction_deferred` | `worker.py` | Worker-wire notice for a malformed/invalid summary fold deferred before the bounded retry limit. |
| `worktree_salvaged` | `supervisor.py` | Records a bounded dirty-worktree evidence artifact captured before recovery or cleanup. |
| `worktree_pruned` | `supervisor.py` | Records successful removal of a task worktree and branch. |
| `worktree_cleanup_deferred` | `supervisor.py` | Records cleanup being retained/deferred because a safety or removal step failed. |
| `provider_infeasible` | `supervisor.py` | Records a provider rejected at admission because its required credential is unavailable. |
| `merge_started` | `supervisor.py` | Records start of private child integration or ref-only publication. |
| `merge_committed` | `supervisor.py`; recovery in `merge.py` | Records an accepted expected-old ref advance, including recovered publication. |
| `merge_failed` | `supervisor.py` | Records merge/publication failure; conflicts carry a structured `merge_conflict` status. |
| `merge_reconciled` | `supervisor.py`; sequencer in `merge.py` | Records startup discovery that the ref advanced before its commit event. |
| `merge_staging_prune_started` | `merge.py`; flushed by `supervisor.py` | Records the start of bounded stale staging/quarantine pruning. |
| `merge_staging_pruned` | `merge.py`; flushed by `supervisor.py` | Records removal of one stale staging/quarantine artifact. |
| `merge_staging_quarantined` | `merge.py`; flushed by `supervisor.py` | Records dirty staging moved into the bounded quarantine. |
| `merge_staging_cleanup_failed` | `merge.py`; fallback in `supervisor.py` | Records staging cleanup failure that prevents silent artifact loss. |
| `result` | `supervisor.py` (worker envelope in `worker.py`) | Records the correlated terminal worker verdict and redacted provider metadata. |
| `exit` | `supervisor.py` (worker `exit_message` in `worker.py`) | Records the worker generation's terminal exit reason. |
| `worker_failed` | `supervisor.py` | Records task/generation failure after protocol, integrity, recovery, or restart exhaustion. |
| `timeout` | `supervisor.py` | Records a wall, ready, heartbeat, pong, or stdin deadline timeout and its phase. |
| `recover` | `supervisor.py` | Records fenced worktree reset/clean recovery and the next generation. |
| `session_ended` | `supervisor.py` | Records final session status and per-task statuses after shutdown cleanup. |

`requires_commit` is an envelope field, not an event kind: the supervisor
passes it from the task spec and the worker echoes it in `result_envelope`.
