# Cambium — Implementation Readiness Review

> **Historical snapshot — pre-implementation review.** This review records
> risks in `/home/ubuntu/cambium/SYSTEM_DESIGN.md` v0.1.0-draft, not current
> runtime behavior. For current behavior, see
> [`docs/architecture/architecture.md`](../architecture.md) and
> [`docs/research/v2-1-status.md`](../../research/v2-1-status.md).

**Reviewer:** Senior Software Engineer (implementation-risks perspective)
**Date:** 2026-08-10
**Verdict in one line:** **Not build-ready.**

## CRITICAL FLAWS

### C1. Merge Sequencer has no concurrency guard

`merge_worker()` checked out `main`, rebased, fast-forwarded, and reset the
shared `repo_root`. Parallel batches can interleave these mutations and corrupt
HEAD, the index, or the wrong commit. Serialize with an `asyncio.Lock` or
single-consumer queue and merge in a throwaway worktree.

The historical command sequence was explicitly dangerous: `git checkout main`
mutated the shared checkout; `git rebase main <branch>` mutated the branch in
the shared object database; a second checkout and `git merge --ff-only <branch>`
published the result; and a failed test ran `git reset --hard HEAD~1`. Two
parallel calls can make the reset undo the other call's commit. A lock around the
whole sequence or a single-consumer merge actor is required; a lock only around
the final fast-forward is too late.

### C2. Merge Sequencer references `self.root`

The constructor sets `self.repo_root`, but the success path runs `git show --stat`
with `cwd=self.root`; every successful merge therefore raises `AttributeError`.

This is a deterministic, reachable success-path defect rather than a platform
edge case: the branch has already rebased and passed its gate before
`git show --stat` is called. The result is reported as a failure after repository
mutation, so retrying can compound the merge race in C1.

### C3. Worker calls `os.getpid()` without importing `os`

The first ready message raises `NameError`; the supervisor sees EOF and enters a
restart loop before a task can complete.

The ready envelope is the first post-init action, so this failure also makes the
supervisor's “alive versus ready” distinction untestable. A minimal smoke run
would catch it before any DSPy call or worktree merge.

### C4. `write_file` calls a nonexistent pathlib method

`Path(path).write_content(content)` always raises; the historical fix was
`write_text()`.

Because this is the primary editing tool, the defect affects every successful
ReAct trajectory that reaches a write. The agent may have a valid plan and
checkpoint, but no file can be changed through the advertised interface.

### C5. `grep_code` has no `return`

The expression `result.stdout + result.stderr` is discarded, so the agent gets
`None` instead of search output.

This is silent data loss at the tool boundary: the ReAct model receives no
search results and may write a speculative full-file replacement. It also masks
the separate shell-injection issue recorded by the LLM and distributed reviews.

### C6. Orchestrator has a hard syntax error

The shown `def __task_id_counter` has no body. M6 cannot import.

Since the module is imported before task dispatch, this parse error prevents the
supervisor from reaching any fallback path. It is not recoverable by a runtime
retry or a provider change.

### C7. Sandbox has an invalid method name and undefined `sys`

`def __ sandbox_command` cannot parse; the intended `_sandbox_command` also
references unimported `sys` and mismatches the call's name-mangling.

The module therefore fails at parse time first, then would fail at name lookup
and method lookup after a partial repair. The review kept these as one finding
because they share the same unexecuted sandbox sample.

### C8. Orchestrator awaits synchronous/undefined methods

`decompose()` is synchronous but is awaited; `self.merge` and `self.evaluate`
are absent; `asyncio` is not imported. `execute()` has three independent failure
paths.

The `await` error occurs before merge or evaluation; if that is removed, missing
methods and missing import still fail. The intended deterministic boundary was
to keep merge/test actions in Unio and have Architectus await only true async
supervisor calls.

### C9. `decomposer_metric` and `reviewer_metric` contain corrupted tokens

The samples show `r.get("status") "done"` (missing `==`) and
`true_bugs polymorphism` (not Python). M9 cannot import.

The two corrupted expressions were visible in the metric definitions, not in a
generated artifact. They block import of the optimization harness even when no
optimization run is requested.

### C10. FanOut cascade defeats itself when model is resolved

`call()` fills an omitted model from provider 0, then `_cascade` skips every
provider whose model differs. Default invocation never reaches Gemini, OpenAI,
or Claude. Treat a caller model as a preference/filter only when explicitly set,
or define capability tiers.

The bug is triggered by the normal call with `model=None`, not only by a caller
asking for an unknown model. It converts a list of providers into an effective
single-provider route and makes cooldown/failover counters misleading.

### C11. `shutdown()` kills asyncio Tasks, not Process objects

The sample passes bare `h.proc.wait()` coroutines to `asyncio.wait`. Python 3.14
rejects those bare coroutines with `TypeError` before `asyncio.wait` returns
anything. The correction is to retain each `Process` in its handle,
create explicit wait tasks with `asyncio.create_task(h.proc.wait())`, await the
tasks, and call `handle.proc.kill()` for pending processes. Do not call `kill()`
on wait tasks. Shutdown must also close pipes and remove worktrees, which the
distributed review's N7 and local M9 cover.

### C12. Worker bypasses FanOut

The worker calls `dspy.configure(lm=dspy.LM(model=model))`, despite the binding
claim that every LLM call uses Diffundo. Provider failover therefore does not
protect the majority of calls. Inject provider configuration or narrow the claim.

The direct `dspy.LM` call also means worker prompts bypass the cache, cooldown,
quota counters, and provider policy that the architecture presents as a shared
boundary. If that is intentional, the design must state that only Architectus
uses Diffundo and remove the worker-resilience claims.

## MODERATE ISSUES

### M1. Python 3.14 free-threaded build is experimental

PEP 703 is not the default Python build; 3.12 has no free-threading, and DSPy,
LiteLLM, tokenizers, torch, and numpy compatibility is unverified. Make it
optional, default to CPython 3.12/3.13, and gate any thread fan-out by capability.

The only cited true parallel workload was SIMBA `num_threads=4`, an offline
optimization job that can use processes. Production workers were already
separate processes and provider calls were I/O-bound. The review found no
benchmark or extension compatibility matrix to justify requiring a no-GIL build.

### M2. Subprocess-per-worker cold start is unbounded

Each worker pays interpreter, DSPy imports, and LM setup (the draft estimated
roughly 30–50 ms plus 1–3 seconds of imports). Eight workers can add seconds
before `ready`; document a latency budget or use a persistent pool.

The ready signal occurs after imports, so an eight-worker decomposition pays the
full cold-start wall before the first tool call. A persistent pool changes task
isolation and checkpoint semantics; accepting process-per-task instead requires
an explicit startup budget and backpressure on spawn.

### M3. Git worktree and object-database concurrency is underspecified

Concurrent `worktree add`, auto-gc, branch deletion, and a merge sequencer can
contend on shared `.git` locks even though per-worktree indexes differ. Disable
auto-gc or retry lock errors, serialize branch cleanup, and consider a dedicated
merge repo.

Per-worker indexes do not protect shared refs, object storage, auto-gc, or
`git worktree prune`. The historical recommendation was `gc.auto=0`, retry with
backoff on add/remove lock errors, and no branch deletion while a merge or rebase
is active.

### M4. Sandbox backend is Linux-only

The user's historical build context included macOS, but Septum had no Seatbelt
(`sandbox-exec`) or no-op backend and firejail is Linux-only. Define a platform
protocol, document weaker macOS isolation, and gate the feature.

The review did not treat `sandbox-exec` as equivalent security: it is deprecated
and undocumented. A backend protocol plus a clearly weaker macOS development
mode is safer than silently claiming cross-platform isolation.

### M5. All FanOut providers down is unhandled

`AllProvidersFailed` is undefined and uncaught. The orchestrator can crash even
though the prose promised that existing workers survive. Define the exception,
park new dispatch, and add an aggregate cooldown/circuit breaker.

Provider outage handling must not be a blanket `except Exception`: it needs a
typed outcome so task failures still reach the gate while new work is parked.
Otherwise C12's worker bypass and the outage combine into a restart storm.

### M6. No secrets management

Plaintext `Provider.api_key` can enter init messages or JSONL events. Load names
from environment or a 0600 secrets file, pass them through inherited environment
or a one-shot FD, and redact logs.

Passing `api_key` in an `init` JSON record makes it visible to protocol captures,
debug output, and event-log serializers. Environment names—not secret values—
were the safer historical boundary; the review also required a threat model for
crash dumps and inherited child environments.

### M7. No structured logging; synchronous hot-path writes

Per-event open/write/close blocks asyncio; the in-memory event list and JSONL file
are unbounded and unstructured. Use stdlib logging/JSON, a queue and writer,
ring-buffer state, and rotation.

The draft's “quota track” and correlation IDs appeared only in diagrams. A
structured logger should make task, worker generation, provider, and event class
fields explicit, while the durable queue should bound memory and define which
advisory records may be dropped.

### M8. No harness test strategy

No unit, supervisor/worker handshake, fake-LLM, merge-conflict, property, chaos,
or soak plan was specified. Add a test-and-evaluation workstream (called
“Test & Eval” in the historical review, not an M-numbered module): deterministic
fake workers and providers, restart-intensity properties, and a CI gate before
P0 completion.

The minimum requested smoke test was one fake worker, one fake LLM, one worktree,
one IPC handshake, and one merge. Property tests should cover restart windows and
cycle rejection; chaos/soak tests should cover concurrent merges, provider
flapping, and orphan cleanup.

### M9. Restart reuses possibly corrupted worktree

After a crash, `_spawn_worker(handle)` reuses the same path; a half-rebase,
conflict, or missing checkpoint is inherited. Reset to `base_commit` or recreate
the worktree, then define how a checkpoint resumes.

Resetting in place is only safe after fencing the crashed process. Recreating a
worktree costs disk but avoids stale `index.lock`, untracked build outputs, and
half-completed rebases; either route needs a documented checkpoint version.

### M10. Heartbeat watchdog and readiness gap are coarse

The monitor slept 10 seconds with a 60-second threshold (up to ~70 seconds to
detect death) and sent init before waiting for `ready`. Make intervals
configurable and gate large follow-up messages on readiness.

Sending a large context record before readiness can block `stdin.drain()` while
the worker is still importing DSPy. That blocks the supervisor's event loop and
recreates the same pipe-stall mechanism as distributed-review C1.

## MINOR NOTES

The original review numbered these **N1–N22**; all are retained here as compact
canaries:

| ID | Recorded defect or design gap |
| --- | --- |
| N1 | M7 appears twice in the module table; second row says “test test gate”. |
| N2 | Error type is `" M5: error"` with leaked prefix/space. |
| N3 | `.c flywheel/...` path is corrupted and its directory is not created. |
| N4 | Builder E priority cell is corrupted (`P Cambium draft, P2`). |
| N5 | Both↔FanOut binding row has malformed Markdown separators. |
| N6 | Flywheel box drawing has wrong `┌`/`└` characters. |
| N7 | `import uuid` in M3 is unused. |
| N8 | `MergeResult.commits/conflicts` are typed lists but default to `None`. |
| N9 | `SubTask.depends_on: list[str] = None` is not Optional/factory-backed. |
| N10 | `_try_provider` constructs a fresh `dspy.LM` on every call. |
| N11 | RestartPolicy uses class attributes where per-instance configuration is expected. |
| N12 | `HEAD~5..HEAD` can fail on a short history and captures unrelated commits. |
| N13 | Async worker `main()` calls synchronous `agent.forward()` with no await point. |
| N14 | Library stdout can corrupt the JSON-lines protocol; redirect it to stderr. |
| N15 | Unbounded stdin messages can fill a pipe and block the event loop. |
| N16 | No worktree/branch garbage collection or TTL. |
| N17 | No metrics/observability surface despite “quota track” in the diagram. |
| N18 | Claude Code doom-loop detector is listed but not specified. |
| N19 | Codex two-phase memory and Prime Agent RLM context variables are listed but not specified. |
| N20 | Temporal idempotency keys are named but no deterministic scheme is shown. |
| N21 | No host deployment/supervisor story for Custos itself. |
| N22 | “Zero external runtime dependencies” ignores LiteLLM and its transitive footprint. |

## VERDICT

**Status: Not build-ready. Revise and re-review.** The architectural vision—OTP
supervision, Kahn-process IPC, Temporal durability, and DSPy optimization—was
coherent, but the samples did not meet an interpreter. The recommended sequence
was: make every sample import; run a one-worker fake-LLM → IPC → merge smoke test;
serialize merge in a throwaway worktree; wire or narrow FanOut; add sandbox,
secrets, logging, and test-and-evaluation work; default to CPython 3.12/3.13; then repeat
adversarial review. The date, source path, branch references in Git examples,
and v0.1.0-draft label are historical provenance.
