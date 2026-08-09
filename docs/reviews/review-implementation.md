# Cambium — Implementation Readiness Review

**Reviewer:** Senior Software Engineer (implementation-risks perspective)
**Date:** 2026-08-10
**Document reviewed:** `/home/ubuntu/cambium/SYSTEM_DESIGN.md` (v0.1.0-draft)
**Verdict in one line:** **Not build-ready.** The architecture is sound, but the code samples contain ~20 syntax errors / undefined-name bugs that would crash on first run, and several core modules have concurrency and portability holes that make the current design unsafe for production. See below.

---

## CRITICAL FLAWS

These will prevent the system from running at all, or cause data loss / corruption under normal operation. Every one of these must be resolved before a single line is committed.

### C1. Merge Sequencer has no concurrency guard — concurrent merges corrupt the shared repo

`MergeSequencer.merge_worker()` (§M7, lines 941–993) runs this sequence on `self.repo_root`:

```python
git checkout main          # mutates the SHARED working tree
git rebase main <branch>   # mutates <branch> in the shared object DB
git checkout main          # again, on the shared tree
git merge --ff-only <branch>
git reset --hard HEAD~1    # on test failure
```

The orchestrator (§M6, line 875) calls `self.merge(results)` after a **parallel** batch of workers completes. If two task batches finish near-simultaneously — or if the orchestrator merges subtask groups incrementally — two `merge_worker()` calls race on the same `repo_root`. Outcomes include: one merge clobbering the other's checkout, `git rebase` operating on a HEAD that just changed, and `reset --hard` reverting the wrong commit. There is **no mutex, no queue, no lock file, and no serialization** anywhere in the design.

**Fix:** Merge operations must be serialized. Either (a) an `asyncio.Lock` around the entire `merge_worker` body, or (b) a dedicated single-consumer merge queue/actor that the orchestrator submits to. Additionally, merges should happen in a **throwaway worktree** (`git worktree add` a temp dir), never on the main repo's working tree — that way the main checkout is never disturbed.

### C2. Merge Sequencer references `self.root` which doesn't exist

Line 980:
```python
diff = subprocess.check_output(
    ["git", "show", "--stat"], cwd=self.root   # ← self.root is undefined
).decode()
```

The constructor (line 937) sets `self.repo_root`, not `self.root`. This is a guaranteed `AttributeError` on every successful merge path. The fix is trivial (`self.repo_root`) but the fact that this line was never executed means **the merge path has never been tested, not even a dry run.**

### C3. Worker calls `os.getpid()` but never imports `os`

Line 730:
```python
emit({"type": "ready", "task_id": task_id, "pid": os.getpid()})
```

`os` is not in the import block (lines 635–643). The worker crashes on the very first action after reading init — before it ever signals ready. The supervisor will see an immediate stdout EOF, classify it as a crash, and restart-loop until `max_restarts` is exceeded. **The system cannot complete a single task as written.**

### C4. `write_file` tool calls a non-existent pathlib method

Line 667:
```python
Path(path).write_content(content)   # ← no such method; should be write_text()
```

`pathlib.Path` has `write_text()` and `write_bytes()`, not `write_content()`. Every `write_file` tool call raises `AttributeError`. Since the worker's primary job is editing files, this is fatal.

### C5. `grep_code` tool has no `return` statement

Lines 686–692:
```python
def grep_code(pattern: str, path: str = ".") -> str:
    ...
    result = subprocess.run(...)
    result.stdout + result.stderr   # ← expression statement, not returned
```

The function always returns `None`. The DSPy agent will receive `None` instead of search results, silently breaking code navigation.

### C6. Orchestrator has a hard syntax error: `def __task_id_counter`

Line 843:
```python
class Orchestrator:
    def __task_id_counter       # ← incomplete, no body, no parens

    def __init__(self, fanout, supervisor):
```

This is a parse error. The entire `Orchestrator` class — and therefore the entire M6 module — will not import. This appears to be a leftover stub or editor corruption.

### C7. Sandbox module has a space in a method name and references undefined `sys`

Line 1010:
```python
def __ sandbox_command(self, ...):   # ← space in identifier; SyntaxError
```

Python identifiers cannot contain spaces. The `Sandbox` class will not parse. Even if fixed to `_sandbox_command`, line 1024 references `sys.executable` without `import sys`, and line 1029 calls `self._sandbox_command` (single underscore) while the definition uses double underscore (name-mangling applies). Three bugs in a 25-line module.

### C8. Orchestrator calls `await` on synchronous methods, and references undefined `merge`/`evaluate`/`asyncio`

Lines 853, 875, 878:
```python
subtasks = await self.decompose(spec, repo_context)   # decompose is sync
merged = await self.merge(results)                    # self.merge undefined
evaluation = await self.evaluate(spec, ...)           # self.evaluate undefined
```

`TaskDecomposer.forward()` (line 816) is a regular synchronous method; `await`-ing its return value raises `TypeError: object list can't be used in 'await' expression`. `self.merge` and `self.evaluate` are never defined on `Orchestrator`. And `asyncio` (line 863) is used but never imported in this module. Three distinct failure modes in the orchestrator's `execute()`.

### C9. `decomposer_metric` and `reviewer_metric` have syntax errors (corrupted tokens)

Line 1077:
```python
completed = sum(1 for r in actual_results.values() if r.get("status") "done")
#                                                                    ^ missing ==
```

Line 1091:
```python
recall = tp / len(true_bugs) if true_bugs polymorphism
#                                       ^^^^^^^^^^^^ not a keyword
```

Both are `SyntaxError`. The M9 optimization harness will not import.

### C10. FanOut cascade defeats its own purpose when `model` is resolved

In `FanOut.call()` (line 248):
```python
resolved_model = model or self.providers[0].model
```

Then `_cascade` (line 263) skips providers whose `model != resolved_model`:
```python
if model and provider.model != model:
    continue
```

When no explicit model is requested, `resolved_model` becomes the **first** provider's model. The cascade then skips every provider whose model differs — i.e. it only ever tries the first provider. The entire multi-provider cascade (the headline feature of M2) is dead code under default invocation. The cache key also bakes in `resolved_model`, so a cached call to provider 0 will never be re-served for a logically identical prompt routed to provider 1.

**Fix:** Separate "model preference" (a hint) from "model filter" (a hard constraint). Cascade should try all providers unless an explicit model was requested by the caller.

### C11. `shutdown()` calls `.kill()` on asyncio Tasks, not on Process objects

Lines 606–611:
```python
_, pending = await asyncio.wait(
    [h.proc.wait() for h in self.workers.values() if h.proc],
    timeout=10
)
for proc in pending:
    proc.kill()   # ← `proc` here is a Task wrapping wait(), not a Process
```

`asyncio.wait` returns a set of Task objects (the coroutines from `proc.wait()` get wrapped). Calling `.kill()` on a Task raises `AttributeError`. Graceful shutdown is broken; stragglers are never SIGKILLed.

Additionally, `h.proc.wait()` returns a coroutine — in modern asyncio, passing bare coroutines to `asyncio.wait()` is deprecated and may be removed. Each should be wrapped with `asyncio.create_task()`.

### C12. Worker bypasses FanOut entirely, contradicting the architecture

The worker (line 733) does:
```python
dspy.configure(lm=dspy.LM(model=model))   # direct LM, no FanOut
```

The design states "Every LLM call in the harness goes through FanOut" (line 205) and the binding table (line 1176) says FanOut is "shared by both layers." But the worker constructs its own `dspy.LM` and never touches FanOut. There is no integration point shown — FanOut is a standalone class with no DSPy wiring. The headline provider-failover benefit does not reach the workers.

---

## MODERATE ISSUES

These won't crash on first run, but will cause failures under load, on specific platforms, or over time.

### M1. Python 3.14 free-threaded build — experimental, no fallback documented

The document targets "Python 3.14 (free-threaded, no-GIL)" and says "3.12+ works without true parallelism" (line 15). Problems:

- **Free-threaded CPython (PEP 703) is not the default build.** Users must compile or install `python3.14t` specifically. It was experimental in 3.13; even if "stable" in 3.14 (not yet released as of the design date), it is not what `apt install python3` gives you.
- **Python 3.12 has no free-threading at all.** The "3.12+ works" claim is true only for the asyncio supervisor; `dspy.SIMBA(num_threads=4)` (line 1111) and any thread-pool parallelism will be serialized by the GIL.
- **C-extension safety is unaddressed.** DSPy, LiteLLM, and their transitive deps (tokenizers, torch, numpy) ship C extensions. Many are not yet free-threaded-safe. Running them on `3.14t` can produce segfaults or silent data corruption. No audit is mentioned.
- **No fallback strategy.** If the free-threaded build misbehaves, there is no documented degradation path (e.g., "fall back to multiprocessing.Pool").

**Recommendation:** Make the free-threaded build optional and additive. Default to CPython 3.12/3.13 with `asyncio` + `ProcessPoolExecutor`. Use free-threading only for the SIMBA optimization fan-out, and gate it behind a capability check. Remove the claim of "zero external runtime dependencies" — DSPy + LiteLLM pull in a large dependency tree.

### M2. Subprocess-per-worker has unbounded cold-start cost; no analysis

The supervisor spawns each worker via `asyncio.create_subprocess_exec(sys.executable, "worker.py", ...)` (line 511). Each worker pays:

- **Python interpreter startup:** ~30–50 ms.
- **`import dspy` and transitive imports:** potentially 1–3 seconds (DSPy pulls in torch or equivalent heavy deps depending on configuration).
- **DSPy configuration / LM warm-up:** first call often includes connection setup.

For a decomposition that spawns 8 workers, that's a multi-second wall of startup before any LLM call fires. The `ready` signal is emitted *after* imports complete (line 730), so the supervisor's readiness wait includes all of this. The document estimates "~600 lines" for M5 but gives **no latency budget, no worker-pool design, no pre-warming strategy.**

**Recommendation:** Either (a) accept the cost and document it, or (b) implement a persistent worker pool that stays alive between tasks (one long-lived process per slot, fed multiple init messages). Option (b) aligns better with the "Erlang supervisor" framing — OTP workers are long-lived, not fork-per-message.

### M3. Git worktree concurrency: `.git/index.lock` and shared object DB

Multiple workers operate on different worktrees of the same repo. The document claims isolation, but:

- **`git worktree add`** acquires locks in the shared `.git/` directory (e.g., `.git/worktrees/<name>/locked`). Concurrent `add` calls are generally safe (git serializes internally), but `WorktreeManager.create()` (line 327) does **not** retry on lock contention.
- **`git gc` / auto-gc:** Git may run auto-gc on the shared object database. A worker's `git commit` during another's `git gc` can fail with "fatal: gc is already running" or object-db lock errors. No `git config gc.auto 0` is set.
- **`git branch -D` in `remove()`** (line 344) touches the shared refs. If a merge sequencer is rebasing that branch concurrently, the delete can fail or the rebase can operate on a just-deleted ref.
- **Index locks:** Each worktree has its own index (`worktrees/<name>/index`), so per-worker `git add`/`commit` won't collide on `.git/index.lock`. But `git stash`, `git checkout` on the main repo, and `git worktree prune` do touch the main index. The merge sequencer's `git checkout main` (C1) is the worst offender.

**Recommendation:** Set `gc.auto=0` in the cambium-managed repo. Add retry-with-backoff on `worktree add` for lock errors. Ensure `remove()` and merge never run concurrently for the same branch. Consider a separate "merge repo" or bare clone for the sequencer.

### M4. Sandbox backend is Linux-only; the user's macOS build machine is unsupported

§M8 (line 998) hardcodes a Linux-only sandbox tool. The user's environment includes a MacBook Pro used for Rust compilation (per the task context). On macOS:

- No equivalent tool on macOS. Apple's sandboxing is via `sandbox-exec` (deprecated, undocumented) or Seatbelt profiles.
- The `Sandbox` class has no platform abstraction, no macOS branch, no `firejail` fallback (firejail is also Linux-only despite being mentioned in the module table).

**Recommendation:** Define a `Sandbox` protocol with platform backends: a Linux namespace backend, `SandboxExecSandbox` (macOS, best-effort), and `NoopSandbox` (development). Gate sandboxing behind a config flag. Document that macOS sandboxing is weaker and should not be trusted for untrusted-code scenarios.

### M5. No error handling when ALL FanOut providers are down

`_cascade` raises `AllProvidersFailed` (line 274), but:

- `AllProvidersFailed` is **never defined** — it's a bare `NameError` at raise time, not a clean exception.
- No caller in the orchestrator or supervisor catches it. The exception propagates up through `execute()` and crashes the orchestrator.
- The design *claims* (line 912) "if all providers are down, the supervisor keeps existing workers alive" — but the code doesn't implement this. The supervisor calls the orchestrator in-process; an unhandled exception in the orchestrator's task will cancel pending work.
- There's no circuit-breaker beyond a flat 60 s cooldown per provider.

**Recommendation:** Define the exception. Wrap orchestrator calls in a `try/except AllProvidersFailed` that logs, parks new dispatch, and lets in-flight workers finish. Add an aggregate circuit breaker that suspends dispatch for a configurable period when all providers are in cooldown.

### M6. No secrets management for provider API keys

`Provider.api_key` is a plaintext `str` in memory (line 189). Keys flow into:

- The FanOut config (how is it loaded? undocumented).
- The worker init message if the worker needs its own key — but the worker currently bypasses FanOut (C12), so it would need the key in the `dspy.LM(api_key=...)` call.
- Potentially the event log: `_log_event` serializes arbitrary dicts to JSONL; if a config or error dict contains a key, it's written to disk in plaintext.

There is no mention of: environment variables, `.env` loading, OS keychain, a vault, or redaction. The sandbox (M8) doesn't `--setenv` keys, so a sandboxed worker can't authenticate at all.

**Recommendation:** Load keys from environment or a secrets file with `0600` perms. Never log keys — add a redaction filter in `_log_event`. Pass keys to workers via an inherited env or a one-shot FD, not in the JSON init message. Document the threat model (keys at rest, in transit to workers, in logs).

### M7. No real logging framework; synchronous file I/O on the hot path

- `_log_event` (line 443) does `open(self.log_path, "a")` + `f.write(...)` on **every event**, synchronously, inside the asyncio event loop. Under load (heartbeats every few seconds per worker, tool events, checkpoints), this blocks the loop on disk I/O.
- `self.event_log` is an unbounded in-memory `list[dict]` (line 431) — memory leak for long-running supervisors.
- Worker "debug goes to stderr (unstructured, advisory only)" (line 170) — no structured logging, no log levels, no correlation IDs beyond `task_id`.
- No log rotation; the JSONL file grows forever.

**Recommendation:** Use Python `logging` with a structured/JSON formatter (e.g., `structlog` or stdlib `logging` with a `JsonFormatter`). Decouple file writes from the event loop via a queue + background flush task. Bound the in-memory `event_log` to a ring buffer. Add rotation.

### M8. No test strategy for the harness itself

The module table (§3.2) has no test module. Section 7 (implementation priority) has no testing phase. There is no mention of:

- Unit tests for `RestartPolicy.should_restart`, `FanOut._cascade`, `WorktreeManager.create`.
- Integration tests for the supervisor↔worker handshake, crash recovery, heartbeat timeout.
- Property-based tests (which the Erlang/OTP framing practically demands) for restart-intensity logic.
- Chaos/soak tests for concurrent merges, provider flapping, worktree exhaustion.
- A test harness for the merge sequencer with synthetic conflicts.

Given that the design explicitly borrows from Temporal (durable execution) and Erlang (supervision trees) — both of which are test-framework-heavy traditions — the absence of a test plan is a significant gap.

**Recommendation:** Add an M0/M11 "Test & Eval" module: unit tests per module contract, a fake-worker harness for supervisor integration tests, a fake-LLM (deterministic canned responses) for FanOut testing, and a property-based suite for restart/merge logic. Define a CI gate before any module is marked P0-complete.

### M9. Restart reuses the (possibly corrupted) worktree without re-creation or cleanup

In `_supervise_worker`, after a crash the loop calls `_spawn_worker(handle)` again (line 464) with the **same** `handle.worktree_path`. The worktree is not re-created or reset. If the worker crashed mid-`git rebase`, mid-`write_file`, or left the repo in a conflicted state, the restarted worker inherits that broken state and will likely crash again — burning through the 5-restart budget on the same underlying corruption.

The checkpoint mechanism (§M5) is supposed to enable resume-from-last-good-state, but `on_step_end_callback` is never wired into the DSPy ReAct loop (see C-adjacent issues), so checkpoints may never be written, and no resume logic reads them back.

**Recommendation:** On restart, either (a) `git reset --hard <base_commit>` + clean the worktree, or (b) tear down and re-create the worktree from `base_branch`, then load the latest checkpoint. Document the resume contract explicitly.

### M10. Heartbeat watchdog timing is coarse and the readiness gap is unguarded

`_heartbeat_monitor` (line 578) sleeps 10 s between checks with a 60 s timeout. Worst-case detection of a dead worker is ~70 s. For a system that claims to fix OpenCode's "stuck subagent hangs 30 min" problem, 70 s is fine — but the check interval should be configurable, not hardcoded.

More importantly, the supervisor sends the init message (line 522) immediately after `create_subprocess_exec`, before the worker has imported anything. The pipe buffers the message, so this is usually safe — but if the worker imports are slow and the supervisor sends a large `context` message afterward, a slow reader could cause `proc.stdin.drain()` to block. The `ready` handshake exists but the supervisor doesn't *wait* for ready before sending more messages (there's no gating in the code shown).

---

## MINOR NOTES

### Code-quality / correctness bugs

- **N1.** §3.2 module table lists **M7 twice** (lines 117–118), the second with a typo: "test test gate". Copy-paste error.
- **N2.** Line 761: error type string is `" M5: error"` (leading space, module prefix leaked into the value). Looks like editor corruption.
- **N3.** Line 1116: `save_path = f".c flywheel/data/optimized/{node_name}.json"` — `.c flywheel` is corrupted (should be `.cambium`), and the directory isn't created before `optimized.save()`.
- **N4.** Line 1267: Builder E priority cell reads `P Cambium draft, P2` — corrupted.
- **N5.** Line 1176: binding-summary table for "Both ↔ FanOut" has a broken Markdown row (`|---| LLM call | ...` missing a column separator).
- **N6.** Lines 1133, 1140: flywheel ASCII diagram has corrupted box-drawing characters (`┌` where `└` belongs).
- **N7.** `import uuid` in M3 (line 306) is unused.
- **N8.** `MergeResult.commits`/`conflicts` default to `None` but are typed `list[str]`; should use `field(default_factory=list)` for correctness.
- **N9.** `SubTask.depends_on: list[str] = None` (line 806) — typed as `list[str]` but defaults to `None`. Should be `Optional[list[str]] = None` or use a factory.
- **N10.** `FanOut._try_provider` creates a fresh `dspy.LM(...)` on every call (line 233) — wasteful; LMs should be cached per provider.
- **N11.** `RestartPolicy` uses class-level attributes (`max_restarts`, etc.) as if they were instance attributes. Works because they're read-only, but fragile if anyone tries to override per-instance.
- **N12.** `collect_commits()` (line 764) uses `HEAD~5..HEAD`, which grabs up to 5 arbitrary recent commits — not necessarily the worker's commits, and fails if the repo has <5 commits.
- **N13.** Worker `main()` is `async` but calls `agent.forward()` synchronously; the `async` wrapper adds nothing without an await point.
- **N14.** `emit()` writes to stdout, but nothing prevents DSPy/LiteLLM/torch from also writing to stdout (progress bars, warnings), which would corrupt the JSON-lines protocol. Stdout must be reserved; redirect library output to stderr.

### Design gaps worth documenting

- **N15.** No backpressure: the supervisor can enqueue unbounded messages to a worker's stdin; if the worker is slow, the pipe buffer fills and `drain()` blocks the event loop.
- **N16.** No worktree garbage collection / TTL — crashed workers leave orphan worktrees and branches.
- **N17.** No metrics/observability surface (Prometheus, OpenTelemetry). "Quota track" is listed in the diagram (line 78) but not implemented in FanOut.
- **N18.** The "doom loop detector" is listed as copied from Claude Code (line 1190) but never appears in any module spec.
- **N19.** "Two-phase memory" and "RLM context-as-variables" are listed as borrowed (lines 1199–1200) but not specified in any module.
- **N20.** The "idempotency keys for git operations" pattern (line 39) is never implemented; the anti-pattern table (line 55) warns against `uuid4()` for keys but no deterministic key scheme is shown.
- **N21.** No deployment story: how is the supervisor started, supervised (systemd/s6?), and kept alive? The document borrows from s6 but doesn't specify the host supervisor for the supervisor.
- **N22.** "Zero external runtime dependencies beyond stdlib + DSPy + git" (line 17) is misleading — DSPy pulls in LiteLLM, which pulls in httpx, pydantic, tokenizers, and often torch. The real dependency footprint is large and must be pinned.

---

## VERDICT

**Status: Not build-ready. Revise and re-review.**

The architectural vision — Erlang-style supervision, Kahn-process IPC, Temporal-style durability, DSPy optimization flywheel — is coherent and well-reasoned. The "what we avoid" table is genuinely thoughtful. But this is a design *sketch*, not a build-ready spec, in its current form:

1. **~12 syntax errors and undefined-name bugs (C3, C4, C5, C6, C7, C8, C9, C2, C11) mean that no module in the document can run as written.** M5 (Worker) crashes on `os.getpid()`. M6 (Orchestrator) won't parse. M8 (Sandbox) won't parse. M9 (Optimization) won't parse. M7 (Merge) hits `AttributeError: self.root`. M4 (Supervisor) `shutdown()` is broken. The fact that *none* of these have been caught suggests **zero of this code has been executed, even as a smoke test.**
2. **The headline concurrency story has a critical race (C1):** concurrent merges mutate the shared repo working tree with no serialization.
3. **The headline resilience story has a gap (C12, M5):** workers bypass FanOut, so provider failover doesn't actually protect the workers; and when all providers are down, the exception is unhandled and undefined.
4. **Platform portability is unaddressed (M4):** the sandbox backend is Linux-only, but the user's build machine is macOS.
5. **There is no test strategy (M8), no secrets management (M6), no real logging (M7), and no fallback for the experimental Python 3.14 free-threaded build (M1).**

**Recommended path to build-ready:**

1. **Fix all CRITICAL items.** Get every code sample to at least import and run a no-op.
2. **Write a minimal end-to-end smoke test** (spawn 1 worker, fake LLM, merge 1 branch) before any module is considered P0-complete. The number of bugs that a single dry run would have caught is telling.
3. **Serialize the merge sequencer** and operate it on a throwaway worktree, not the main checkout.
4. **Wire FanOut into the worker** (or document explicitly that workers use a single LM and only the orchestrator fans out — but then the resilience claim must be scaled back).
5. **Add a platform abstraction for sandboxing**, a secrets-loading convention, a structured logging plan, and a test module (M0/M11).
6. **De-risk the Python runtime:** default to 3.12/3.13, make free-threading optional, audit C-extension safety.
7. **Re-run adversarial review** after these changes, then hand to a coding agent.

The bones are good. The current draft is a strong architectural narrative wrapped around code that hasn't met an interpreter. Close that gap and this becomes a buildable system.
