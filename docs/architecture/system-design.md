# Cambium — System Design Document

> **Historical snapshot — pre-implementation.** This is the v0.1.0 draft and
> its v0.2.0-pending review record, not runtime authority. For current behavior,
> see [`docs/architecture/architecture.md`](architecture.md) and
> [`docs/research/v2-1-status.md`](../research/v2-1-status.md).

**Version:** 0.1.0-draft
**Date:** 2026-08-09
**Status:** Pre-implementation, adversarial-review-ready

## 0. TL;DR (historical proposal)

Cambium was proposed as a Python-native multi-agent coding harness: a persistent
**Custos** supervisor manages N **Opifex** worker processes. Workers run DSPy
ReAct loops in isolated Git worktrees; newline-delimited JSON travels over
stdin/stdout; one worker can die without taking down its siblings. The proposal
combined Erlang/OTP one-for-one supervision, Temporal-style durable activities,
Kahn-process/CSP channel semantics, and hill-climbable DSPy modules.

The draft targeted Python 3.14 free-threaded (3.12+ without true parallelism).
The current core Python package has no mandatory third-party runtime library;
it uses the Git executable. The `bench` and `module-test` commands require the
`test` or `dev` extra because they use pytest. DSPy and tree-sitter adapters are
optional extras. LiteLLM and sandbox dependencies below are historical plans.

## 1. Naming

The tree metaphor names system boundaries. The names and responsibilities were:

| Code | Latin name | Role |
| --- | --- | --- |
| M1 | **Nuntius** | JSON-lines IPC |
| M2 | **Diffundo** | Provider cascade/race/cache |
| M3 | **Surculus** | Git worktree allocation and cleanup |
| M4 | **Custos** | Deterministic process supervisor |
| M5 | **Opifex** | Worker ReAct runtime |
| M6 | **Architectus** | Task decomposition and routing |
| M7 | **Unio** | Branch merge and test gate |
| M8 | **Septum** | Per-worker sandbox |
| M9 | **Ascensus** | DSPy optimization harness |
| M10 | **Janus** | CLI/TUI |

Custos was the root; Architectus and Diffundo planned and served models; Nuntius
carried control; each Opifex owned a Surculus worktree and Septum sandbox; Unio
joined branches; Ascensus optimized trajectories; Janus exposed the system.

## 2. CS foundations and rejected patterns

| Concern | Adopted pattern (source) | Intended invariant |
| --- | --- | --- |
| Lifecycle | one-for-one, transient restart, intensity/period (Erlang/OTP) | A worker failure does not restart siblings. |
| Identity/readiness | stable task ID and explicit `ready` (Unix/s6) | PIDs are volatile; alive is not ready. |
| Recovery/side effects | checkpoints and deterministic idempotency keys (Temporal) | Resume after a tool call; retries do not double-commit. |
| IPC | blocking reads, non-blocking writes, one reader/writer (Kahn) | Ordered delivery and explicit backpressure. |
| Multiplexing | asyncio event loop over pipes/timers/control (CSP) | Race-free supervision decisions. |
| Rollback | compensation registered before Git steps (Temporal Saga) | Multi-step merges can be undone. |

The draft rejected one-for-all restarts, `.pid` identity files, Unix socket and
lock-file supervision, shared worker state, blanket worker `try/except`, random
(`uuid4()`) idempotency keys, bidirectional agent chat, prompt-only concurrency
rules, sequential dispatch, and unbounded subagents. The source lessons were
Prime Agent, OpenCode (including #29638 and #11865), Claude Code, and Temporal.

## 3. Architecture and module contracts

The target topology was a deterministic Custos process plus Architectus and
Diffundo in one event loop, with one Opifex process per task. Every Opifex had a
separate worktree and protocol pipe; the supervisor never delegated liveness to
an LLM.

| Module | Boundary | Dependencies | Priority |
| --- | --- | --- | --- |
| M1 Nuntius | Message types, framing, size limits | stdlib | P0 |
| M2 Diffundo | Multi-provider cascade/race/cache, quota and cooldown | DSPy, LiteLLM | P0 |
| M3 Surculus | `git worktree add/remove`, paths, pruning | Git CLI | P0 |
| M4 Custos | Spawn, ready/heartbeat, restart, events | M1, M3 | P0 |
| M5 Opifex | ReAct, tools, checkpoint/result | M1, M2 | P0 |
| M6 Architectus | Decompose, route, evaluate | M2, DSPy | P1 |
| M7 Unio | Rebase, fast-forward, conflicts, tests | Git CLI, M3 | P1 |
| M8 Septum | Namespace/policy wrapper | platform sandbox | P2 |
| M9 Ascensus | Trajectory metrics and SIMBA/GEPA | DSPy, M5 | P2 |
| M10 Janus | Submit and observe | M4, M6 | P2 |

The rough implementation estimate was 4,150 Python lines: M1/M2/M3 in
parallel, M4/M5 after their prerequisites, M6/M7 next, then M8/M9/M10.

### M1 — Nuntius (IPC)

The proposed protocol was newline-delimited JSON with supervisor-to-worker
`init`, `context`, and `cancel`, and worker-to-supervisor `ready`, `heartbeat`,
`result`, and `error`. One compact historical example is:

```jsonl
{"type":"init","task_id":"wt-abc-001","worktree":"/path/to/wt","spec":"Refactor dry_run.rs","max_turns":20,"tools":["read_file","write_file","run_shell","git_op","grep"],"permissions":{"network":true,"shell":true}}
{"type":"result","task_id":"wt-abc-001","status":"done","commits":["a1b2c3d"],"files_changed":["src/dry_run.rs"]}
{"type":"error","task_id":"wt-abc-001","error_type":"build_failure","message":"cargo build failed","partial_commits":[]}
```

Rules: stdout carried protocol only; diagnostics went to stderr; each line was
parsed independently; malformed JSON was logged/skipped; oversized lines,
wrong framing, and fatal protocol errors stopped the worker; EOF was treated as
an exit signal. The design also called for unbuffered output, task correlation,
and a maximum line size.

### M2 — Diffundo (LLM access)

`Provider` records were `name`, `model`, `api_key`, `base_url`, `priority`,
`cooldown_until`, call/error counters, and rate-limit state. `FanOut.call()`
selected `cascade`, `race`, or a direct provider, applied a 30-second provider
timeout, cached `(model, temperature, prompt)` for 3,600 seconds, and returned
the first successful response. Cascade order was proposed as DeepCode Flash →
Gemini Flash → OpenAI Mini → Claude Haiku; race used the first completed call.

The rationale was provider resilience, quota tracking, and a shared DSPy-facing
boundary for both supervisor and workers. The later review found the default
model filter prevents cross-model fallback (F5/LLM-C2), worker.py bypasses this
boundary (F4/IMPL-C12), and repository state is absent from the cache key
(F2/LLM-C1). These remain historical findings, not silently changed design
decisions.

### M3 — Surculus (worktrees)

Each task received `cambium/{task_id}` from `base_branch` (default `main`), a
path under `.cambium/worktrees`, and a recorded `base_commit`. Creation used
`git worktree add -b`; removal removed the worktree and branch; status checked
uncommitted changes. The draft favored isolation and deterministic task IDs but
did not specify lock retries or crash recovery.

### M4 — Custos (supervisor)

Custos spawned workers with `asyncio.create_subprocess_exec`, sent `init`, and
ran one supervision task per worker plus a heartbeat monitor. The intended
policy was one-for-one restart, 1-second minimum delay with exponential
backoff, a maximum of five crashes in 60 seconds (then task failure and
Architectus escalation), and a 60-second heartbeat watchdog. Events were
appended to an in-memory list and `events.jsonl`; the supervisor owned merge
ordering and never called an LLM.

Key decisions were: stable task IDs rather than PIDs; an explicit ready signal;
workers report results, not merge; stdout EOF plus process status indicates
failure; and provider outages should stop new LLM dispatch without killing
existing work. The reviews show why these decisions were incomplete: blocking
event writes (DS-C1), coarse heartbeats (DS-C3), unsound EOF assumptions, no
jitter, stale worktrees, and unsupported crash durability (DS-C6).

### M5 — Opifex (worker)

The worker configured DSPy ReAct with `task, context -> action`, a bounded
`max_iters`, and tools `read_file`, `write_file`, `run_shell`, `git_op`, and
`grep_code`. It emitted ready and heartbeats, checkpointed after tool calls,
ran tools in its worktree, collected recent commits, and returned a result or
error. The intended tool contract kept debug output off stdout and let Septum
restrict network and shell permissions. Reviews recorded missing structured
edits (F8/LLM-M2), protocol corruption risks, and code defects
(IMPL-C3-C9, the implementation review's C3–C9 range).

### M6 — Architectus (orchestrator)

Architectus decomposed a task into `SubTask` records (`task_id`, `spec`,
`depends_on`, `priority`, `model`, `max_turns`), dispatched ready nodes in
parallel, waited for dependencies, and asked a reviewer to accept or reject the
merged result. The supervisor remained pure deterministic code; Architectus
handled LLM decomposition, routing, and evaluation. The proposal lacked cycle
detection, a reliable task-ID counter, and an atomic “do not decompose” path
(DS-M6, LLM-C6).

### M7 — Unio (merge)

For each worker branch, Unio checked out `main`, rebased the branch, fast-forward
merged it, ran a test command (default draft: `cargo test --lib 2>&1 | tail -5`),
and reverted on failure. It returned commits, conflicts, test output, and a
diff summary. The alternative considered was batch merge then bisect; a
speculative merge tree could reduce test runs to O(log N). The draft selected
sequential merge for simplicity, but omitted a mutex and used the shared main
checkout (IMPL-C1).

### M8 — Septum (sandbox)

The target wrapper applied Linux namespaces and a firejail-like policy, with
network and shell permissions supplied in `init`; a macOS/Windows fallback was
not designed. The implementation review marked the Linux-only backend M4; the
draft assigned the module P2.

### M9 — Ascensus (optimization)

Ascensus recorded per-node trajectories and optimized prompts with SIMBA/GEPA.
The proposed metrics were worker success plus tool efficiency, decomposer
completion ratio, and reviewer F1 against labeled bugs. The flywheel was
trajectory → metric → optimized prompt → better results. Reviewers rejected the
claim that nodes are independently hill-climbable (F9/LLM-C4), found the metrics
gameable or lacking ground truth (F10/LLM-C5), and noted that checkpoint hooks,
paths, and syntax were incomplete.

### Historical module mechanics retained from the draft

These details explain the interfaces later reviews cite. They remain historical
design evidence, not a description of the checked-in runtime.

**Nuntius sequencing.** The supervisor was the sole writer to worker stdin and
the worker the sole reader; the reverse held for stdout. A worker blocked on
`stdin.readline()` rather than polling. The supervisor enqueued writes and let
the pipe buffer provide backpressure. `ready` carried `task_id` and PID;
`heartbeat` carried turn and status; `tool_event` carried tool, command, exit
code, and duration; `checkpoint` carried turn and a state reference; `result`
carried status, commits, changed files, and a summary; and `error` carried a
typed failure with optional partial commits. Every line had to flush, parse as
JSON, and stay below a configured size. Parse failures were advisory, but a
truncated result line could not be recovered by this design. No binary framing,
shared memory, or agent-to-agent pipe was planned.

**Diffundo selection.** Providers were sorted by priority. In cascade mode,
`_try_provider` skipped a provider in its 60-second cooldown, incremented call
and error counters, constructed a DSPy `LM`, and set cooldown after an
exception. In race mode the first `race_redundancy` providers were submitted
through `asyncio.to_thread`; the first completion won and pending calls were
cancelled. Cache eviction was TTL-based and bounded by `cache_max_size` in the
configuration, although the sample only implemented the TTL check. The intended
benefit was cheap-first routing for normal work and latency-first routing for
planning/evaluation. The design did not define a capability tier,
context-window check, or a safe distinction between stateless and
repository-dependent prompts; C1/C2/C3 in the LLM review follow directly from
those omissions.

**Surculus lifecycle.** `create(task_id, base_branch)` allocated
`.cambium/worktrees/{task_id}`, created `cambium/{task_id}`, and recorded
`git rev-parse HEAD` as `base_commit`. `remove()` called `git worktree remove`
then `git branch -D`; `list_active()` parsed porcelain output;
`has_uncommitted_changes()` used `git status --porcelain`. The draft assumed
each task ID was unique and that a failed worker could safely reuse its path. It
did not state who pruned orphan paths, how a worktree-add lock was retried, or
whether branch deletion could race a rebase.

**Custos state machine.** Historical `WorkerState` values were SPAWNING, READY,
RUNNING, CHECKPOINTING, DONE, DEAD, and FAILED. `WorkerHandle` held task ID,
process, worktree path, base spec, result, last heartbeat, restart count, and
crash timestamps. `run_task()` created the worktree and logged
`task_assigned`; `_supervise_worker()` spawned, started a heartbeat task, read
until result/EOF, cancelled the monitor, and either returned a result or applied
the restart policy. `result` and `error` were returned from the reader; EOF
without a result appended `worker_exit` and entered restart handling.

The intended clean path was `init → ready → heartbeat/tool_event/checkpoint →
result`; the abnormal path was `init → ready → heartbeat silence or EOF → kill
→ backoff → spawn`. `shutdown()` sent SIGTERM, waited up to ten seconds, then
intended to SIGKILL stragglers. The implementation sample passed bare
`h.proc.wait()` coroutines to `asyncio.wait()` and attempted `.kill()` on the
returned tasks, which became implementation finding C11. Event records were
mutated with a timestamp, appended to an unbounded list, and synchronously
written to `events.jsonl`; distributed finding C1 and implementation finding M7
explain the resulting pressure and durability problems.

The supervision decisions were deliberately narrow: one-for-one means a worker
failure does not cascade to siblings; transient means a clean result does not
restart; intensity/period (five crashes per 60 seconds) escalates a persistent
failure; and a mandatory one-second delay prevents a busy loop. The draft also
said “no lock files” because the parent knows child PIDs, but that choice did
not solve Git's own index/ref locks. It said EOF was definitive because stdout
was supposed to be protocol-only, but C2 records why that convention needed
enforcement and separate health signals.

**Opifex execution.** The worker imported DSPy, read one `init` envelope,
configured the requested model, and created a ReAct module with `max_iters`.
Tool calls were intended to be bounded: `read_file` returned text, `write_file`
overwrote a path, `run_shell` used a timeout, `git_op` ran a bounded Git action,
and `grep_code` searched the worktree. After each action the worker emitted a
heartbeat and attempted a checkpoint containing ReAct trajectory state. On
success it ran `collect_commits()` and compared changed names with the base;
on exception it emitted an error. The sample used `HEAD~5..HEAD`, which later
became distributed-review N2, implementation-review N12, and LLM-review N5;
it called `agent.forward()` synchronously inside `async main()`, which became
implementation-review N13. No structured patch tool, stdout guard for third-party
libraries, or resume reader was shown.

**Architectus scheduling.** `TaskDecomposer` returned a list of `SubTask`
records. The executor partitioned them into `pending`, `ready`, and `done`,
submitted all ready tasks concurrently, and moved dependants into the next wave
when their IDs appeared in results. A reviewer then saw the merged diff and
returned accept/reject. The supervisor remained pure deterministic code and did
not let an LLM choose kill/restart/merge actions. The missing cycle check,
broken private counter, failed-dependency handling, and absent retry body meant
that this was a target algorithm, not a tested DAG scheduler.

**Unio alternatives.** The chosen sequence was rebase each worker branch onto
the draft's `main`, fast-forward merge, run tests with a 300-second timeout, and
reset on failure. The review considered (a) serialize all merges with a
mutex/queue, (b) batch all branches and bisect after one test run, or (c) merge
a binary tree and test each level. The draft selected sequential merge because
it was easiest to reason about, while acknowledging O(N) test cost and shared-
checkout risk. The exact historical Git commands were `git checkout main`,
`git rebase main <branch>`, `git merge --ff-only <branch>`, and on a failed gate
`git reset --hard HEAD~1`. These commands are cited by branch/concurrency
findings, not recommended as current behavior.

**Septum and Janus.** Septum was a P2 wrapper around Linux namespaces and a
firejail-like policy. `permissions.network` and `permissions.shell` came from
the init envelope; a development no-op and macOS Seatbelt path were suggested
only in review, not in the draft. Janus was a P2 CLI/TUI for task submission and
status, with no wire contract beyond Custos and Architectus.

**Ascensus data path.** The draft recorded worker trajectories, computed a
metric, and called SIMBA/GEPA to write an optimized prompt under
`.cambium/flywheel/data/optimized/{node_name}.json`. The pictured loop was
collect → score → optimize → deploy → collect again. It listed worker,
decomposer, and reviewer metrics and assumed “better results” would generate
better data. The reviews preserve the causal objections: worker and decomposer
scores are coupled; “done” is self-reported; reviewer F1 needs labels; a broken
test command makes the floor ineffective; and optimizing on one provider can
harm another. No held-out split, rollback, human approval, or drift alarm was
specified.

### Historical binding and operation sequence

The complete target operation was: Janus submits a spec; Architectus may
decompose it; Custos allocates one Surculus worktree per subtask; Opifex sends
`ready`, performs ReAct actions through Diffundo, and checkpoints; Custos
restarts only abnormal exits; Unio serially tests and merges; Ascensus records
the trajectory after acceptance. A provider outage was supposed to leave the
Custos process and already-running workers alive, while new Architectus work
waited for a provider. This promise was later narrowed by DS-M7/IMPL-M5 because
workers themselves call the provider.

The draft's boundary table stated: supervisor↔worker was JSONL plus heartbeat,
checkpoint, and result; supervisor↔orchestrator was an in-process call with
`SubTask` records; both layers↔Diffundo used DSPy `LM`; Unio was the only code
allowed to publish to `main`; and Janus was the only user-facing entry point.
The review record retains these as adopted boundaries even where implementation
findings rejected the sample code.

### Historical pattern rationale and rejected alternatives

The pattern inventory was intended to be a set of constraints, not a list of
brand names. Stable task IDs make retries addressable after PIDs change. The
explicit `ready` event separates process creation from import and model setup.
One-for-one supervision limits blast radius; transient restart avoids replaying a
successful result. The intensity/period window was borrowed from Erlang, while
the mandatory delay and backoff came from s6. Temporal supplied the idea that a
tool call is an activity with a checkpoint and deterministic compensation, not
that a JSONL file is automatically durable. Kahn-process language meant one
reader and one writer per pipe; it did not grant arbitrary asyncio code
determinism.

The rejected patterns carried concrete consequences. A `.pid` file can go stale
when a process is replaced, while the parent already owns the real PID. A Unix
socket plus lock file can leave stale paths and has portability concerns on
macOS. Shared mutable worker state would require a synchronization protocol the
draft did not have. Blanket `try/except` in Opifex would convert programmer bugs
into opaque task errors, so the draft preferred “let it crash” and let Custos
apply policy. `uuid4()` cannot identify a retried Git operation, so the proposed
key had to derive from task, operation, and base commit. Prompt instructions
cannot reliably enforce “parallel reads, serialized writes”; that policy belongs
to the deterministic boundary. Sequential subagent dispatch loses the
parallelism OpenCode issue #29638 exposed, while no timeout repeats the
long-hanging behavior cited in #11865.

The competitor table therefore mixed adopted mechanisms with items explicitly
deferred. Async generator lifecycles, tool removal, three doom-loop heuristics,
per-subagent timeout/watchdog, ephemeral teams, unidirectional messaging,
append-only sessions, compaction and truncation guards, BM25 retrieval,
namespace sandboxing, two-phase memory, RLM variables, and continual harness
self-improvement were “what we copy.” The draft only specified a subset of
those modules. “What we do differently” was process isolation with pipes rather
than in-process async or fragile socket IPC; provider cascade rather than one
vendor; DSPy SIMBA/GEPA rather than static prompts; Python 3.14 rather than
TypeScript/Go; and Git worktree plus event-log recovery rather than lock files or
snapshots. The implementation and LLM reviews specifically flagged copied ideas
that had no module contract (implementation-review N18/N19) so the provenance
is preserved without claiming they were implemented.

## 4. Binding and competitor decisions

Supervisor↔worker used stdin/stdout JSONL, heartbeats, checkpoints, and results;
Supervisor↔Architectus used in-process calls and task records; both layers were
intended to share Diffundo. Borrowed ideas were async generator lifecycles
(Claude Code), tool removal, doom-loop and compaction guards (Claude Code),
timeouts and ephemeral teams (OpenCode), append-only sessions (Prime Agent /
Codex), truncation detection, BM25 tool search and OS-native sandboxing (Codex),
two-phase memory (Codex), RLM context variables and continual self-improvement
(Prime Agent). Cambium's alternatives were process isolation plus pipes,
provider cascade, DSPy prompt optimization, Git worktrees, and PID/EOF/event-log
recovery instead of socket locks or snapshots.

### Historical implementation phases and acceptance gates

The revised plan inserted a Phase 0 before P0 coding. It required fixing
F1-F12 (also written F1–F12),
writing a one-worker mock-LLM smoke test, and proving spawn → IPC → result →
merge before parallel work. Phase 1 then built Nuntius, the corrected Diffundo,
and Surculus concurrently; Custos followed Nuntius/Surculus and Opifex followed
Nuntius/Diffundo. Phase 2 built Architectus with atomic-task mode and Unio with
serialized, worktree-isolated merging. Phase 3 added Septum, Ascensus, and
Janus. The revised estimate was four to five weeks rather than three.

The review acceptance gate was deliberately stronger than “the files import.”
The smoke test had to exercise a fake LLM, one child process, the ready and
result envelopes, one worker branch, a test gate, and a merge. M1–M8 also called
for jitter, unbuffered protocol output, secrets loaded by environment, a test
strategy, an atomic dispatch escape hatch, deferred BM25 retrieval, and a real
logging framework. The reviewers liked the process-isolation core, the DSPy
flywheel as a possible differentiator, the Erlang derivation, and the competitor
analysis, but their verdict stayed “fix first” until the gates passed.

## 5. Historical review record (v0.2.0-pending)

Three reviews concluded **“Sound bones, not build-ready yet.”** Findings were:

| ID | Historical finding and recorded fix |
| --- | --- |
| **F1** | Sync event-log I/O blocks asyncio and cascades into pipe stalls and false heartbeat kills (DS-C1). Use a writer thread/`aiofiles`, batching, and durable flush. |
| **F2** | FanOut cache ignores repository state (LLM-C1). Include commit/worktree state or disable worker caching. |
| **F3** | Merge has no mutex and mutates shared main (IMPL-C1). Serialize and use a throwaway worktree. |
| **F4** | Workers construct direct `dspy.LM`, bypassing Diffundo (IMPL-C12). Inject provider configuration. |
| **F5** | Model equality guard makes cascade a no-op across providers (LLM-C2). Separate preference from capability/tier. |
| **F6** | 60-second heartbeat is shorter than 120-second shell timeout and four-provider cascade. Emit tool-progress heartbeats or raise/partition timeouts (DS-C3). |
| **F7** | About twelve sample syntax/name errors (IMPL-C3-C9). Smoke-test every sample. |
| **F8** | No structured edit tool (LLM-M2). Add `edit_file` or a patch grammar. |
| **F9** | Independent hill-climbing is overstated (LLM-C4). Treat it as a hypothesis; start with worker-only evaluation. |
| **F10** | No robust coding metric (LLM-C5). Combine tests-as-floor, behavioral checks, quality review, and held-out data. |
| **F11** | Event append is not crash-safe (DS-M3). Use fsync or SQLite WAL. |
| **F12** | Sandbox is Linux-only (IMPL-M4). Add platform backends or document limits. |

Moderate records M1–M8 retained the proposed fixes: add jitter; combine EOF
with process wait and unbuffered output; make free-threading optional; load
secrets from environment; add a one-worker mock smoke test; add atomic-task
mode; defer BM25 retrieval to Phase 3; and use structured logging. Reviewers
liked the sound process-isolation core, DSPy flywheel as a differentiator,
Erlang derivation, and competitor analysis. The revised estimate was 4–5 weeks:
Phase 0 fixes and smoke test, P0 core, P1 intelligence, then P2 hardening.

## 6. Status and provenance

The draft status block said “ready for adversarial review,” then “v0.2.0-pending”
and “hand to coding agent” only after F1–F12 and a smoke test. Its source file
was `/home/ubuntu/cambium/SYSTEM_DESIGN.md`; companion historical sources were
`../supervisor-worker-patterns.md` (373 lines) and
`../multi-agent-architecture-research.md` (200 lines), plus the three review
files. Those paths and dates are provenance, not claims about the current tree.
