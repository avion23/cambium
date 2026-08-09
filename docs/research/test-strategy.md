# Cambium — Test Strategy for the Harness Itself

Research date: 2026-08-10. Purpose: answer **IMPL-M8** ("No test strategy for the
harness itself", `docs/reviews/review-implementation.md`) and the test-relevant
findings of the distributed-systems and LLM-design reviews. The strategy
applies to the harness as designed in `docs/architecture.md` (v2, pending merge
from the architecture worktree): Custos (supervisor), Opifex (workers), Nuntius
(IPC), Surculus (worktrees), Unio (merge sequencer), Diffundo (provider
cascade), and the event log.

Constraints honored from `agents.md` §5 and `docs/architecture.md` §19:

- **No TDD / unit-test ceremony.** Scenario and integration tests are the norm.
  A module is done when its dataset/metric eval runs green, its scenario test
  passes, and the end-to-end smoke test passes against it (`agents.md` §9) —
  not when it has a per-function unit suite.
- **No LLM in the loop during harness tests.** Deterministic-layer tests use
  real processes, real git, and a fake provider server on localhost. The only
  LLM-adjacent modules tested are Diffundo (against a fake provider) and the
  decision modules (against their frozen datasets).
- **Stdlib + DSPy + git.** No pytest plugins, no `pytest-asyncio`, no
  `pytest-timeout` (the harness's own watchdogs do the timing), no `responses`
  / `requests-mock`. Tests use `asyncio.run()` inside sync test functions, as
  the existing scenario test does.

Pytest-feature claims in this document are annotated **VERIFIED** (tested
against pytest 8.x on CPython 3.14.7, commands shown in §10.1) or **UNVERIFIED**
(could not be checked here; treat as needing a check during implementation).

---

## 1. Test pyramid for a process-supervision harness

A subprocess-supervision harness inverts the classic pyramid: the most valuable
tests sit at the *top* (whole-supervisor scenarios with real worker processes),
and the base is a thin layer of pure-contract tests for functions that are
independently meaningful. There is deliberately no "unit-test everything"
layer.

| Layer | What lives here | Real I/O | Speed | Gate value |
|---|---|---|---|---|
| L5 Scenarios | The 15 named scenarios of §7, through the public API / orchestrator | workers, git, sqlite, local HTTP | seconds–minutes | The behavioral contract (this is the smoke test) |
| L4 Integration | Fake-worker liveness (§2), event-log replay (§3), worktree lifecycle (§4), merge concurrency (§5), Diffundo vs fake provider (§6.1) | workers, git, sqlite, localhost HTTP | seconds | Each harness mechanism under its real failure modes |
| L3 Deterministic replay | Nuntius frame-in/frame-out round trips, event-tier/fsync contract, restart-policy decisions as a pure function, DAG validation, Septum command wrapping, redaction | none (pure + temp files) | milliseconds | The contracts the upper layers rely on |
| L2 Module datasets | Every DSPy module's dataset loader + metric over the frozen split, canaries (§8) | none | milliseconds | The per-module eval gate |
| L1 Harness smoke | `python -m cambium.tests.smoke`: fake LLM + 1 worker + 1 merge (§7 S01) | all | ~1 min | The review's "single dry run" gate |

Ordering rule: a change that adds a harness behavior must add (or extend) an
L5/L4 scenario *or* an L3 contract test — never a mock-heavy unit test that
reimplements the module's internals. The v0.1 implementation review found
"~12 syntax errors and undefined-name bugs" that a single dry run would have
caught (`docs/reviews/review-implementation.md` verdict); the L1 smoke gate is
what closes that class.

---

## 2. Fake workers — the four liveness modes

The distributed-systems review (DS-C2) showed that "stdout EOF = worker dead"
is unsound. The liveness model in `docs/architecture.md` §5.3 is four-layer
(process exit → `exit` message → heartbeat watchdog → EOF advisory). To test it
we drive real worker *scripts* through the real spawn path
(`asyncio.create_subprocess_exec`, `docs/architecture.md` §7.2: `start_new_session=True`,
`PYTHONUNBUFFERED=1`) and give the supervisor exactly the four worker behaviors
the liveness model must distinguish.

One script, `tests/fakes/worker_liveness.py`, mode selected by env
`FAKE_MODE`; each mode speaks the Nuntius protocol (`docs/architecture.md` §5.2): read `init` from
stdin, echo the `request_id`, emit `ready`, then behave per mode.

| Mode | Script behavior | What it exercises | Expected supervisor outcome |
|---|---|---|---|
| `healthy` | `ready` → heartbeats + `tool_event` + `checkpoint` → git commit → `result` → `exit` | The happy path, `request_id` echoing, readiness gate | Task DONE; one merge; exit 0 |
| `hang` | `ready`, one heartbeat, then `time.sleep(1e6)`; no further beats | Heartbeat watchdog (DS-C3), kill of a **process group**, restart under budget | Watchdog fires at `timeout_s`; process group SIGKILLed; generation bumped; respawn |
| `crash` | `ready`, write a file, then SIGKILL itself **mid-write of the `result` line** | Partial write / torn line (DS-C2 mode c), EOF advisory (not auto-death), worktree recovery, checkpoint pickup | Parse-error line logged+skipped; CRASHED; worktree recovered; restart; task completes from checkpoint |
| `garbage` | Interleave valid protocol lines with random bytes, truncated JSON, and a stray `print()` | Parse-error tolerance (one JSON object per line), stdout reserved for protocol (IMPL-N14) | Every line independently parsed; invalid lines tagged `parse_error`; task still DONE |
| `grandchild` (variant of the four) | `ready`, spawn a child that inherits stdout and sleeps, then exit cleanly — pipe stays open | DS-C2 mode (a): EOF with a live process | EOF is not death: grace timer → `ping` → no `pong` in 10 s → process-group kill |

Assertions are the supervisor's own typed events, not the fake worker's
self-report: `worker_spawned` (generation N), `worker_ready`, `heartbeat`
stream, `worker_exit`/`worker_crashed`, `result`, `parse_error` tags,
`supervisor_stall` (if any). This tests the *supervisor*; the worker script is
the environment.

The `hang` and `crash` modes are also the vehicle for the restart-policy
scenarios (§7 S03, S09, S10): the supervisor's own timers do the pacing, so
tests do **not** need a pytest timeout plugin — they wait on the event stream
with `asyncio.wait_for` around the expected terminal event, and fail the test
if the supervisor itself misbehaves (which is the bug we are trying to catch).

---

## 3. Deterministic event-log replay tests

The event log is SQLite-WAL with a dedicated writer thread, critical-event
fsync, gap-free `seq`, and snapshot compaction (`docs/architecture.md` §6).
These are the tests that make the durability contract (`docs/architecture.md`
§6.5) a tested claim rather than a design aspiration:

1. **Writer thread, not event loop.** Enqueue a burst while the writer thread
   sleeps; assert the event loop keeps servicing other tasks (a canary
   `asyncio.sleep` completes on time). Regression test for DS-C1.
2. **Critical events are fsync'd before ack.** Enqueue a `result` (critical),
   then kill the writer (simulate crash) before the next timer tick; reopen the
   DB; assert the `result` row is present. Non-critical heartbeats may be lost
   — that is the documented contract, and the test asserts the loss window is
   bounded by `fsync_interval_s` (drop a heartbeats-only tail, reopen, assert
   `recovery_gap` event + gap in `seq`).
3. **Replay = events since last snapshot.** Take a snapshot, append events,
   "crash", reopen; assert the replayed stream equals the post-snapshot events
   in `seq` order and that `seq` is gap-free.
4. **Result recovery.** A `result` written to the checkpoint store before emit
   (`docs/architecture.md` §5.4 mode c) must be recoverable even when the `result` line itself was
   torn — scenario S05 covers this end-to-end.
5. **Drain-deadline watchdog (DS-C2 mode d).** Stall the supervisor's stdout
   reader for past the drain deadline and assert a `supervisor_stall` event is
   emitted and heartbeat enforcement is suspended for that worker until
   draining resumes — a supervisor-side stall must never be blamed on the
   worker.

These run on a temp DB in `tmp_path_factory.mktemp("log")` — **VERIFIED** that
`tmp_path_factory` is a real pytest fixture (see §10.1).

---

## 4. Worktree lifecycle tests (Surculus)

Surculus owns `worktree add / recover / prune` (`docs/architecture.md` §7.5).
All tests run against a real scratch repo (`git init` in a temp dir, `gc.auto=0`
set as the design requires — IMPL-M3):

1. **Create.** `add` a detached worktree at `base_commit`; assert the worktree
   exists and `.cambium/generation` is written with the spawn generation.
   Retry-on-lock-contention: pre-create a `.git/worktrees/<id>/locked` file and
   assert the create retries (or fails loudly with a typed error) rather than
   hanging.
2. **Recover clears every stale lock.** Plant `worktree/.git/index.lock`,
   `repo/.git/worktrees/<id>/locked`, `.git/rebase-merge/`, `.git/REBASE_HEAD`,
   plus a dirty working tree and untracked files; run
   `Surculus.recover(worktree, base_commit)`; assert all `*.lock` gone,
   rebase/merge aborted, `reset --hard base` + `clean -fd` applied, generation
   file rewritten. Regression test for DS-C5.
3. **Quarantine on recovery failure.** Make the reset fail (e.g., a worktree
   with a corrupted `.git`); assert the tree is moved to
   `${session_dir}/cambium/quarantine/<task_id>-<generation>/` and a fresh
   worktree is created from `base_commit` (`docs/architecture.md` §7.5).
4. **Prune.** Create stale `git worktree` administrative entries; assert
   `prune()` on startup and shutdown removes them (DS-N7).

---

## 5. Merge-sequencer concurrency tests (Unio)

Unio is serialized by an `asyncio.Lock`, verifies in a throwaway worktree, and
publishes via an atomic `git update-ref` (`docs/architecture.md` §7.8). These
tests are where the IMPL-C1 / DS-M1 "parallel workers, serial merge, no
corruption" claim is proven:

1. **N concurrent merges, all fast-forward.** Build N branches, each with a
   commit on distinct files; launch N `merge_worker` calls with `asyncio.gather`
   (the orchestrator's real submission shape); assert all N commits land on
   `main`, the throwaway worktree was used (the main working tree was never
   checked out), and `git fsck` reports no corruption. The lock is proven by
   asserting the merges never interleave their `update-ref` calls (observable
   in the `merge_progress`/`merge_committed` event order).
2. **Merge race → one winner, no corruption.** Two branches modify the *same*
   file. The first merge wins; the second detects a non-fast-forward
   (`old_sha` mismatch) and raises `NonFastForward`; the orchestrator re-merges
   against the new `main`. Assert the final tree contains both changes, the
   loser's commits are present after re-merge, and `git fsck` is clean.
   (Scenario S07.)
3. **Test gate uses the raw exit code.** Run the gate with a fake `test_cmd`
   that exits non-zero; assert the merge is rejected and the branch is not
   published. The v0.1 `cargo test | tail -5` no-op gate (LLM-M1) is a dead
   feature — a test that fails to fail is a bug.
4. **`reconcile()` closes the crash gap.** Simulate a crash between
   `update-ref` and the `merge_committed` event emit: advance `refs/heads/main`
   behind the log's back, run `Unio.reconcile()`, assert a `merge_reconciled`
   event is emitted and the ref/log gap is closed (`docs/architecture.md`
   §7.8).

---

## 6. LLM-dependent modules — no network

### 6.1 Diffundo against a fake provider server

`tests/fakes/fake_provider_server.py` is a stdlib
`http.server.ThreadingHTTPServer` bound to `127.0.0.1:0` (ephemeral port). It
implements the provider endpoint Diffundo's client code actually calls, with
per-provider behavior selected by path or header: canned `200` completion,
`429` (rate limit), `500`, slow-then-timeout, malformed body. No external
network is touched; every test resolves providers to `http://127.0.0.1:<port>`.

Tests call `Diffundo.call(...)` directly (not through a full DSPy ReAct), so
they cover the contract of `docs/architecture.md` §9 ("Diffundo — Provider
Cascade"):

1. **Tier cascade actually cascades.** Two providers in tier `"fast"`; the
   first returns 429; assert the result comes from the second and the first is
   in cooldown. Regression test for LLM-C2 / IMPL-C10 ("the cascade only ever
   tried the first provider").
2. **Priority order.** Within a tier, the lower `priority` is tried first; a
   success short-circuits.
3. **Capability filters.** `require_tools=True` skips `supports_tools=False`;
   `min_context_window` skips a small-context provider (LLM-C3).
4. **`AllProvidersFailed` is typed and carries evidence.** All providers down
   → the exception carries `providers_tried` and `last_error`; the orchestrator
   catches it and parks dispatch instead of crashing (IMPL-M5,
   `docs/architecture.md` §9.2).
5. **Cache is opt-in, keyed, TTL'd.** `cache=True` without `context_hash` is
   rejected; with `context_hash`, a repeat call is served with
   `"cache_hit": true`; a different `context_hash` is a miss; TTL expiry and
   namespace isolation are asserted (LLM-C1).
6. **Per-provider LM reuse.** The provider client is constructed once, not per
   call (IMPL-N10).
7. **No race mode.** The same-priority cascade is the only "first of N"
   behavior; there is no `_race` to test (LLM-M6).

> **UNVERIFIED:** the exact wire format Diffundo's client must speak to a
> provider (OpenAI-style chat-completions shape LiteLLM accepts) is **not**
> pinned by this document. The fake server's contract is defined by the
> Diffundo client code as written; during implementation the stub's response
> schema must be checked against the real provider spec (and one recorded
> golden response kept as a fixture) before these tests are trusted.

### 6.2 Decision modules against dataset + metric

`ShouldDecompose` is the reference (scenario S14, existing
`tests/scenarios/test_example_module.py`). The pattern every future module
repeats, per `docs/module-template/architecture.md` §9:

1. Load the real dataset; every record schema-valid; a malformed record raises
   `DatasetError` (the loader is a hard gate, never caught).
2. Run `decide()` over every pair, attach predictions, score with `metric()`;
   assert the aggregate score meets the module's threshold.
3. Assert the canary records are present, were processed (a prediction was
   attached), and pass (§8).
4. Determinism: same input → same output (the rule engine is a pure function;
   a DSPy replacement must hold under `temperature=0`).

The `TaskDecomposer`/`TaskRouter`/`ResultEvaluator` datasets, when they land,
add the sibling-pinning rule: they are evaluated against **stub** siblings
(frozen references, `docs/architecture.md` §17.2), never against live
co-adapted modules.

---

## 7. Scenario test catalog

Fifteen named scenarios. Each is a `@pytest.mark.scenario` test that drives
the real public API (or the real orchestrator+supervisor), never a mock. Setup
uses `tmp_path_factory` scratch repos and the fake workers of §2.

| ID | Scenario | Setup | Concrete assertions |
|---|---|---|---|
| **S01** | Happy-path smoke | Atomic task (`ShouldDecompose=false`), one `healthy` fake worker, real scratch repo, fake LLM | `result` with `status="done"`; `merge_committed`; `refs/heads/main` advanced; exit code 0; no key material in any event (redaction) |
| **S02** | Worker killed mid-edit → restart with fresh worktree | `crash` fake worker that wrote a partial file then SIGKILLed itself; `recoverable=true` | `worker_crashed`; generation bumped on respawn; worktree reset to base (no partial file); task completes on generation N+1 |
| **S03** | Hang → watchdog kill + restart | `hang` fake worker (no heartbeats past `timeout_s`) | Watchdog kills the **process group**; `worker_exit` reason `crash`; restart under burst budget; no `ProcessLookupError` raised in the watchdog (DS-M2) |
| **S04** | Garbage stdout tolerated | `garbage` fake worker (random bytes + truncated JSON + `print()`) | Invalid lines logged with `parse_error` and skipped; valid protocol lines all processed; task DONE |
| **S05** | Crash mid-result → checkpoint recovery | Worker writes `checkpoint` then SIGKILLs itself while writing `result` | The `result` is recovered from the checkpoint store; task marked DONE; no duplicate execution of completed work |
| **S06** | Event-log crash → replay restores state | Run a task, SIGKILL the supervisor process, reopen the session dir | Replay from last snapshot reconstructs the task state; `result.json` is correct; `seq` gap-free or a documented `recovery_gap` |
| **S07** | Merge race → one winner, no corruption | Two `healthy` workers edit the same file; both merges submitted concurrently | First merge wins; second raises `NonFastForward` and is re-merged; final tree has both changes; `git fsck` clean |
| **S08** | Stale locks don't block restart | Plant `index.lock`, `worktrees/<id>/locked`, `REBASE_HEAD` in a task's worktree; restart the worker | `Surculus.recover()` clears all three before respawn; the restarted worker commits successfully |
| **S09** | Restart budget: burst cap + absolute cap | Always-crash fake worker | Escalation after `burst_max` crashes in `burst_window_s`; task FAILED at the `absolute_max` ceiling; typed `task_failed` event |
| **S10** | No thundering herd | Four workers killed simultaneously (kill their process group) | Restart delays are jittered (two identical runs produce different delay orderings); no lock contention on `worktree add` |
| **S11** | Orphan fencing after supervisor crash | Spawn a worker, SIGKILL the supervisor, leave the orphan running; start a new supervisor in the same session dir | New supervisor bumps generation; the orphan's next git op reads `.cambium/generation`, detects the mismatch, emits `exit reason=fatal`, and dies — no split-brain writes |
| **S12** | Diffundo cascade failover | Fake providers: A=429, B=200 (same tier) | Result from B; A in cooldown; no "only first provider ever tried" behavior |
| **S13** | Provider outage parks dispatch | All fake providers down | `AllProvidersFailed` caught by the orchestrator; dispatch parked; an already-running worker survives (no provider-outage mass kill) |
| **S14** | ShouldDecompose dataset + metric + canaries | Existing `tests/scenarios/test_example_module.py` | Metric 1.0 over the full dataset; both canaries present and passing; `DatasetError` on malformed records |
| **S15** | Shutdown hygiene | Cancel a mid-task session (`cancel` → SIGTERM → SIGKILL per `docs/architecture.md` §7.7) | Graceful cancel path completes; straggler process groups SIGKILLed (no `.kill()` on asyncio Tasks — IMPL-C11); worktrees pruned; event-log writer flushed and DB closed |

S01 is the `cambium.tests.smoke` entry point referenced by `agents.md` §5 and
the smoke-test gate of `docs/architecture.md` §19 item 15. Every new harness
module must first pass S01 (or extend it), because that is the test the v0.1
reviews proved was missing.

---

## 8. Canary policy — catching reward hacking in tests

Canaries are the brakes on the optimization flywheel
(`docs/architecture.md` §10, §17.4; `docs/module-template/dataset-format.md`
§6). They are enforced at **three** points, all testable:

1. **Dataset-integrity canaries (module tests).** Each dataset ships trap
   records whose gold labels are deliberately misaligned with the surface
   heuristics the metric would otherwise reward. The scenario test asserts they
   are present, processed, and pass (S14). Dropping canaries to inflate the
   metric is caught by the "canaries were loaded and scored" assertion.
   Concrete: the `should_decompose` canary with four `HIGH_SIGNAL` keywords and
   gold `decompose=false` catches a keyword-greedy replacement; the canary with
   zero surface keywords and gold `decompose=true` catches one that dropped
   verb-clause analysis.
2. **Optimization canaries (Ascensus eval gate).** `python -m
   cambium.modules.<name>.eval --suite canaries` exits non-zero on the first
   canary failure (`docs/module-template/architecture.md` §9.3). This is the
   promotion gate: a prompt variant that improves the training metric while
   regressing the canary rate is **rejected** even if its score went up
   (`docs/architecture.md` §17.4 step 8). In v2 the single-file dataset with
   inline `canary: true` markers achieves the same effect via the
   1.0-aggregate assertion.
3. **Dataset hygiene gates.** The loader refuses records containing secret
   patterns; cross-split leaks, duplicate IDs, and schema mismatches raise
   `DatasetError` and fail the run (`dataset-format.md` §7, §9). A dataset that
   cannot load is a hard test failure, never a skip.

Rule of thumb: **any metric signal that can be gamed by deleting code, editing
tests, or padding output must have a canary that specifically traps that
gaming behavior** — e.g., "the worker did not delete the failing test", "the
worker did not add `assert True`", "no `.cambium/` writes from the worker"
(`docs/architecture.md` §10 table). Each held-out task ships 3–5 such
canaries.

---

## 9. What to run where

### 9.1 Test layout and markers

```
tests/
├── conftest.py                  # shared fixtures: scratch repo, fake-worker spawn, fake provider
├── unit/                        # L3 pure contracts — deliberately thin
│   ├── test_nuntius_framing.py  #   frame in/out round trip, torn-line handling
│   ├── test_restart_policy.py   #   should_restart / delay as a pure function
│   ├── test_redaction.py        #   secrets never reach the log
│   ├── test_dag_validation.py   #   cycle detection in Architectus (DS-M6)
│   └── test_septum.py           #   namespace / sandbox-exec / noop command wrapping
├── scenarios/                   # L5 named scenarios (§7); @pytest.mark.scenario
│   ├── test_example_module.py   #   S14 (exists today)
│   ├── test_smoke_happy_path.py #   S01
│   ├── test_crash_recovery.py   #   S02, S05, S06
│   ├── test_liveness.py         #   S03, S04
│   ├── test_merge_race.py       #   S07
│   ├── test_worktree_locks.py   #   S08
│   ├── test_restart_budget.py   #   S09, S10
│   ├── test_fencing.py          #   S11
│   ├── test_provider_outage.py  #   S12, S13
│   └── test_shutdown.py         #   S15
├── integration/                 # L4 harness mechanisms; @pytest.mark.integration
│   ├── test_event_log_replay.py #   §3
│   ├── test_worktree_lifecycle.py # §4
│   ├── test_merge_sequencer.py  #   §5
│   └── test_diffundo_cascade.py #   §6.1
└── fakes/                       # not collected (no test_ prefix)
    ├── worker_liveness.py       #   §2 fake worker (FAKE_MODE=healthy|hang|crash|garbage|grandchild)
    ├── fake_provider_server.py  #   §6.1 localhost HTTP provider
    └── gold_llm_responses/      #   golden provider responses for Diffundo fixtures
```

Markers are registered in `pyproject.toml` to keep the suite warning-free
(**VERIFIED**: an unregistered marker produces a `PytestUnknownMarkWarning` —
see §10.1; registering via `[tool.pytest.ini_options].markers` silences it):

```toml
[tool.pytest.ini_options]
testpaths = ["tests"]
markers = [
    "scenario: named end-to-end scenario through the real public API",
    "integration: needs real subprocesses, git repos, or a local fake HTTP server",
    "slow: wall-clock minutes; excluded from default local runs",
]
```

### 9.2 Command list (CI-less local verification)

Run from repo root on Python 3.14.7. All `pytest` options below are **VERIFIED**
against the local install (§10.1).

| Purpose | Command |
|---|---|
| Whole suite (default) | `uv run --python 3.14.7 --extra test pytest -q` |
| Fast contract layer only | `uv run --python 3.14.7 --extra test pytest -q -m "not integration"` |
| Scenario suite only | `uv run --python 3.14.7 --extra test pytest -q -m scenario` |
| Integration suite only | `uv run --python 3.14.7 --extra test pytest -q -m integration` |
| Exclude known-slow | `uv run --python 3.14.7 --extra test pytest -q -m "not slow"` |
| One named scenario | `uv run --python 3.14.7 --extra test pytest -q tests/scenarios/test_liveness.py -v` |
| Deselect one flaky test | `... pytest -q --deselect tests/scenarios/test_liveness.py::test_hang_watchdog` |
| Find the slowest tests | `... pytest -q --durations=5` |
| No `.pytest_cache` writes | `... pytest -q -p no:cacheprovider` |
| Show registered markers | `... pytest --markers` |
| Harness smoke gate (`agents.md` §5) | `python -m cambium.tests.smoke` |
| Module eval + canary gate | `python -m cambium.modules.<name>.eval` and `... --suite canaries` |
| Type / syntax gate | `python -m compileall src/cambium && python -c "import cambium"` |

Local "gate" definition (matches `agents.md` §9 "done" criteria): full suite
green **and** the smoke gate passes **and** the touched module's eval + canary
suites pass. There is no CI server; the gate is the command list above, run
before every module is marked complete.

### 9.3 Which findings require which scenario (mapping)

Every review finding that demands a test maps to a concrete item. IMPL-M8
("no test strategy") is answered by this entire document; the rest:

| Review finding | Demands a test for | Covered by |
|---|---|---|
| IMPL-M8 — no test strategy | the harness itself | this document; L1 smoke gate; §3–§7 |
| IMPL-C1 / DS-M1 — merge no concurrency guard / bottleneck | serialized merge, no corruption under concurrency | S07; §5 items 1–2 |
| IMPL-C2..C9, IMPL-N1..N14 — runtime bugs (`self.root`, `os.getpid`, `write_content`, missing returns, broken syntax, `shutdown` `.kill()`) | any module runs at all | S01 (the "single dry run" gate); S15 for C11 |
| IMPL-M2 — unbounded cold start | readiness gate | S01 (ready handshake; a worker that never readies trips `ready_timeout` → killed, folded into S03) |
| IMPL-M3 — git worktree concurrency / `gc.auto` | no cross-worker git contention | S08; §4.1; S07 |
| IMPL-M4 — sandbox backend Linux-only | Septum platform abstraction | §9.1 `unit/test_septum.py` (namespace/sandbox-exec/noop command wrapping) |
| IMPL-M5 / DS-M7 — all-provider-down unhandled; kills workers | typed `AllProvidersFailed`, dispatch parked, workers survive | S13; §6.1 item 4 |
| IMPL-M6 — no secrets management | keys never in logs/protocol | `unit/test_redaction.py`; S01 assertion (no key material in events) |
| IMPL-M7 — no real logging | non-blocking writer, rotation, flush | S15 (flush on shutdown); §3 items 1–3; S06 |
| IMPL-M9 — restart reuses corrupted worktree | fresh worktree per restart | S02; §4.2–4.3 |
| IMPL-M10 — heartbeat timing / readiness gap | configurable watchdog, ready gating | S01, S03 |
| DS-C1 — sync I/O in event loop | event loop never blocks on disk | §3 item 1 |
| DS-C2 — "EOF = dead" unsound (4 modes) | the four-layer liveness model | S03, S04, S11; §2 (`hang`, `crash`, `garbage`, `grandchild`); §3 item 5 (drain-deadline) |
| DS-C3 — heartbeat granularity | per-tool heartbeats, long-tool safety | S03 |
| DS-C4 — no jitter / rate-window gaming | jittered restarts; burst + absolute caps | S09, S10 |
| DS-C5 — stale worktree locks | lock cleanup before respawn | S02, S08 |
| DS-C6 — supervisor crash orphans / split-brain | generation fencing, no orphan writes | S02, S06, S11 |
| DS-M2 — `WorkerHandle` logical races | no `ProcessLookupError` in watchdog | S03 (assert the watchdog survives killing a dead process) |
| DS-M3 — no fsync / partial lines | durability contract per tier | §3 items 2–4; S06 |
| DS-M4 — FanOut shared-state races | cooldown/cache correctness under contention | §6.1 items 1–2, 5 (S12) |
| DS-M6 — no cycle detection | DAG validation rejects cycles | `unit/test_dag_validation.py` |
| DS-N5 — unbounded event log | snapshot compaction | §3 item 3 (replay since last snapshot) |
| DS-N7 — shutdown doesn't clean worktrees | prune on shutdown | S15; §4.4 |
| LLM-C1 — cache ignores repo state | opt-in cache keyed on `context_hash` | §6.1 item 5 |
| LLM-C2 — cascade doesn't cascade | tier-based cascade across providers | S12; §6.1 items 1–2 |
| LLM-C3 — model transparency assumed | capability filters | §6.1 item 3 |
| LLM-C4 — "independently hill-climbable" false | pinned-sibling eval | §6.2 sibling-pinning rule (with `TaskDecomposer`/`TaskRouter` datasets) |
| LLM-C5 / LLM-M1 — metric gameable / broken test gate | multi-signal metric, raw test exit code | S14; §5 item 3; §8 |
| LLM-C6 — no do-not-decompose path | atomic fast path | S14; S01 (atomic task) |
| LLM-M3 — flywheel no brakes | canary rejection at promotion | §8 items 1–2 |
| LLM-M4 — checkpoint callback doesn't exist | checkpoint written and resumed | S05 |
| LLM-M6 — race mode unsafe | no race mode; same-priority cascade | §6.1 item 7; S12 |

---

## 10. Pytest-claim verification record

### 10.1 VERIFIED claims

Tested on CPython 3.14.7 with pytest 8.x (installed via
`uv run --python 3.14.7 --extra test`), using a throwaway project at
`/tmp/opencode/pytest-verify`:

- Custom markers registered under `[tool.pytest.ini_options].markers` appear in
  `pytest --markers` (`@pytest.mark.scenario: ...`) and select with
  `-m "scenario"`, `-m "integration or slow"`, `-m "not slow"`.
- An **unregistered** marker on a test emits `PytestUnknownMarkWarning` at
  collection; registration silences it.
- `-m "not slow"` / `-m "integration or slow"` run the expected subset
  ("N passed, M deselected").
- `--deselect <nodeid>` skips exactly that test.
- `-k "slow and not hang"` filters by keyword expression.
- `--durations=2` prints the slowest calls.
- `-p no:cacheprovider` runs with no `.pytest_cache` writes.
- `--collect-only -q -m scenario` lists only matching tests.
- `tmp_path` and `tmp_path_factory` fixtures; `pytest.raises`; `pytest.skip`;
  `@pytest.mark.parametrize`.

### 10.2 UNVERIFIED claims

- **Diffundo fake-provider wire contract** (§6.1): the exact request/response
  schema the provider stub must serve to be a faithful LiteLLM-compatible
  endpoint. Must be pinned against the real provider spec during
  implementation.
- **`cambium.tests.smoke` / `cambium.modules.<name>.eval` interfaces**: the
  entry points are specced (`agents.md` §5, module-template §9) but do not
  exist in the scaffold yet; their CLI surface (`--suite canaries`, exit codes)
  follows the module-template spec and must be verified when implemented.
- **Writer-thread timing assertions** (§3 items 1–2): the specific jitter of
  the fsync timer means loss-window bounds are statistical; assertions use
  generous margins and are re-checked against the real implementation.

---

## 11. Scope note

This document is a **design** for the harness tests. Today only S14 exists
(`tests/scenarios/test_example_module.py`, the reference module). Every other
test lands alongside its module (Custos, Nuntius, Surculus, Unio, Diffundo,
Septum), in the same PR that implements the module, gated by the L1 smoke test
(S01). A module that ships without its scenario test is not complete
(`agents.md` §9).
