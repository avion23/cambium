# Cambium — Test Strategy for the Harness Itself

**Historical snapshot — 2026-08-10.** This design answers **IMPL-M8** and review
findings for Custos, Opifex, Nuntius, Surculus, Unio, Diffundo, and the event log. It is
not a test-count or current-status claim. Current authority is
[`docs/architecture/architecture.md`](../architecture/architecture.md)
and source/tests.

**Current note (not retroactive):** active `supervisor.run_plan` is flat;
`task_decomposed` remains unsupported; provider cascade is source-defined and honors
`Retry-After`; worker stdout/event admission is bounded; no per-worker OS sandbox or
approval; DLQ and eval cache are absent. The implemented module evaluation surface is
`python -m cambium optimize eval`.

Tree canaries treat static DAG validation/admission as a harness boundary: no dynamic
child dispatch before validation, each child gets a fresh bounded context, and only the
strict upward envelope is visible to its parent. Tests reject implicit single-context
recursion. Prefix-cache/prompt-prefix behavior is tested by measurement, not assumed
cheap branching or a fixed discount.

Constraints: scenario tests use real processes, git, SQLite, and a local
fake provider; no LLM network, pytest plugins, `pytest-asyncio`, timeout plugins,
`responses`, or `requests-mock`. Sync tests call `asyncio.run()`. Module datasets,
metrics, and canaries remain the L2 gate.

## 1. Pyramid and fake-worker liveness

The useful order is public-API scenarios, process-boundary mechanisms, pure
replay/contracts, and frozen datasets. A behavior change adds a scenario or a
meaningful pure contract, not a mock copy of internals. Scenario workers live in
`scripts/fake_worker.py` and `tests/fixtures/` and speak NDJSON.

Supervisor events, not worker self-report, are asserted: `worker_spawned`, ready,
heartbeat, checkpoint/result, exit/failure, parse errors, and `supervisor_stall`.

## 2. Deterministic contracts

### 2.1 Event log and replay (DS-C1, DS-M3, DS-C2)

1. Writer thread keeps the loop responsive while queue/disk is slow.
2. Critical result/checkpoint rows survive a simulated writer crash before timer fsync;
   non-critical heartbeat tail may be lost only within the documented window and emits
   `recovery_gap`.
3. Replay after a snapshot equals post-snapshot events in gap-free `seq` order.
4. Checkpoint-first result survives a torn stdout result line.
5. A stalled reader emits `supervisor_stall` and suspends heartbeat enforcement until
   drain resumes; it never blames the worker.

### 2.2 Surculus worktrees (IMPL-M3, DS-C5, DS-N7)

Against a real `git init` repository (`gc.auto=0`): create writes generation; recovery
clears `index.lock`, admin locks, rebase/merge state, resets/cleans and rewrites
generation; failed recovery quarantines and creates fresh tree; startup/shutdown prune
stale administrative entries.

### 2.3 Unio merge sequencing (IMPL-C1, DS-M1, LLM-M1)

Concurrent distinct-file merges serialize under `asyncio.Lock`, verify in a throwaway
worktree, atomically `update-ref`, and leave `git fsck` clean. Same-file race produces
one `NonFastForward`, then remerge. A nonzero gate exit prevents publication. A crash
between ref update and event emits `merge_reconciled`.

## 3. LLM-adjacent tests (offline)

Offline provider scenarios use stdlib `ThreadingHTTPServer` fixtures on
`http://127.0.0.1:<port>`; each scenario pins its request/response schema.
Diffundo cases cover tier fallback (LLM-C2), capability filters/transparency (LLM-C3),
context-hash cache (LLM-C1), provider outage (`AllProvidersFailed`, DS-M7/IMPL-M5),
and race disabled/quality-safe (LLM-M6). Decision modules run frozen train/eval splits,
with sibling pinning for LLM-C4 and canaries for LLM-C5/LLM-M1/LLM-M3.

## 4. Named scenario catalog (S01–S15)

| ID | Scenario and invariant |
| --- | --- |
| **S01** | One real worker/merge smoke; readiness, gate, canonical result, no credentials. |
| **S02** | Crash recovery: checkpoint, locks, quarantine, fresh worktree. |
| **S03** | Hang/heartbeat watchdog, per-tool heartbeat, bounded kill/restart (DS-C3, IMPL-M10). |
| **S04** | EOF/grandchild/garbage four-layer liveness (DS-C2). |
| **S05** | Torn result line recovered from checkpoint. |
| **S06** | Event-log fsync/replay, logging flush and no loop disk I/O. |
| **S07** | Merge race, raw gate exit, remerge and clean `git fsck`. |
| **S08** | Worktree lock/rebase cleanup and prune (DS-C5/DS-N7). |
| **S09** | Restart burst cap/jitter (DS-C4). |
| **S10** | Absolute restart cap and failure reason. |
| **S11** | Generation fencing after supervisor crash (DS-C6). |
| **S12** | Cascade/cooldown/cache and fake-provider routing (LLM-C1/C2/C3, DS-M4). |
| **S13** | Provider outage parks dispatch; workers survive (IMPL-M5/DS-M7). |
| **S14** | Dataset metric and canary gate; do-not-decompose path (LLM-C5/C6, M1). |
| **S15** | Graceful shutdown: cancel/group kill, reaping, durable end, prune (IMPL-C11/M7). |

## 5. Canaries and anti-gaming

Every held-out task carries 3–5 canaries: do not delete failing tests, add `assert True`,
write `.cambium/`, or pad output. `canaries` zero the score on failure; promotion must
run `python -m cambium optimize eval MODULE --dataset PATH` and reject any metric gain
with canary regression. Dataset loaders reject secret patterns, duplicate IDs, split
leaks, and schema errors. These controls test the metric, not all backdoors.

## 6. Layout and command record

Current layout keeps scenarios under `tests/scenarios/`, opt-in live-provider checks
under `tests/acceptance/`, and worker scripts under `scripts/` and `tests/fixtures/`.
Registered markers in `pyproject.toml` are `slow`, `acceptance`, and `xdist_group`.

Current commands (repo root) are:

```text
PYTHONPATH=src python -m pytest -q
PYTHONPATH=src python -m pytest -m slow -q
PYTHONPATH=src python -m pytest tests/acceptance/ -q
PYTHONPATH=src python -m ruff check src tests
PYTHONPATH=src python -m cambium doctor
```

The default pytest invocation excludes `slow`; acceptance checks are opt-in and
require explicit provider credentials and configuration.

## 7. Finding-to-test map (retained IDs)

`IMPL-M8` → this strategy; `IMPL-C1/C2..C9/C10/C11`, `IMPL-N1..N14` → S01/S15;
`IMPL-M2/M3/M4/M5/M6/M7/M9/M10` → S01/S02/S03/S08/S12/S13/S15;
`DS-C1/C2/C3/C4/C5/C6`, `DS-M1/M2/M3/M4/M6/M7`, `DS-N5/N7` → §§2–4;
`LLM-C1/C2/C3/C4/C5/C6`, `LLM-M1/M3/M4/M6` → §§3–5. This index preserves the
historical mapping without asserting that every target test exists today.

## Appendix A — detailed test contracts

### A.1 Fake workers and liveness evidence

The fake worker reads `init`, echoes `request_id`, emits `ready`, and then follows
`FAKE_MODE`. `healthy` verifies a checkpoint before result and a merge after exit.
`hang` sleeps after one heartbeat so the watchdog—not a test timeout—kills its process
group and increments generation. `crash` self-SIGKILLs while writing a result line;
the reader must deliver a partial tail, log `parse_error`, recover the checkpoint, and
restart. `garbage` interleaves valid NDJSON with raw bytes, truncated JSON, and a stray
print; every invalid line is advisory and the task still completes. `grandchild` exits
while a child inherits stdout; EOF starts grace/ping escalation and `killpg` removes the
grandchild. Assertions inspect the supervisor event stream and DB, never a fake worker
self-report.

### A.2 Event-log durability checks

The writer-thread canary enqueues thousands of non-critical records while a probe
`asyncio.sleep` runs; a late probe proves the loop was not blocked by SQLite. Critical
fsync is tested by enqueueing `result`, stopping the writer before its timer tick, and
reopening the DB. A heartbeat-only tail may disappear within `fsync_interval_s`, but
replay must record a gap. Snapshot replay compares every post-snapshot `seq`, and result
recovery verifies checkpoint-first persistence after a torn protocol line. A drain
deadline test deliberately stalls a stdout reader and checks `supervisor_stall` plus
suspended heartbeat enforcement.

### A.3 Worktree and merge checks

Worktree tests plant `.git/index.lock`, repository admin `locked`, `rebase-merge`,
`REBASE_HEAD`, dirty files, and untracked artifacts. `recover()` must remove all locks,
abort merge state, reset to base, clean, and rewrite generation. A corrupted tree must
be quarantined, not reused. Merge tests launch N distinct-file merges with
`asyncio.gather`, assert serialized `merge_progress`/`merge_committed`, clean `git fsck`,
and ensure main was never checked out. Same-file conflict yields one expected
`NonFastForward`, then remerge. A gate command that exits nonzero must prevent
publication; a shell pipeline that masks exit status is a failing test, preserving
LLM-M1's raw-exit requirement.

### A.4 Provider and module checks

The fake provider binds `127.0.0.1:0`, records each request, and serves deterministic
responses. Tests assert tier fallback, capability filtering, context-hash cache keys,
cooldown/circuit state, `Retry-After`, cost caps, and provider outage parking. No API key
values appear in requests, events, or logs. Decision-module tests freeze train/eval and
canary splits; sibling-pinning prevents an optimizer from changing a helper metric to
hide a regression. Canaries specifically catch deleted tests, `assert True`, `.cambium/`
writes, no-op patches, and output padding.

## Appendix B — review-finding matrix

`IMPL-C1`/`DS-M1` map to S07 merge serialization; `IMPL-C2..C9`, `IMPL-C10`, `IMPL-C11`,
and `IMPL-N1..N14` map to S01/S15 runtime smoke and protocol assertions. `IMPL-M2` is
ready timeout; `IMPL-M3` S02/S08 worktrees; `IMPL-M4` Septum abstraction (historical,
now no sandbox); `IMPL-M5` S13 outage; `IMPL-M6` redaction; `IMPL-M7` S06/S15 logging;
`IMPL-M9` S02 fresh restart; `IMPL-M10` S01/S03 timing. DS-C1/C2/C3/C4/C5/C6 and
DS-M1/M2/M3/M4/M6/M7 plus DS-N5/N7 map to §§2–4. LLM-C1/C2/C3/C4/C5/C6 and
LLM-M1/M3/M4/M6 map to §§3–5. The IDs are preserved for audit traceability, not as a
claim that each scenario currently exists.

## Appendix C — public-boundary and context tests

Tree tests build a plan with a root, two siblings, and a dependency chain. They assert
that validation completes before any worker process starts, that `max_width` limits
admission, and that a dynamic child proposal is not admitted until the next validated
wave. A malformed child (duplicate ID, wrong parent, cycle, over-depth, or over-width)
produces a typed rejection and zero partial dispatch. This catches an implementation
that recursively calls a child worker while bypassing the harness tree.

Context tests use a child session containing a sentinel scratchpad string and a parent
summary. `context_for(child)` must contain the child's own bounded turns, parent summary,
and strict result envelopes; it must not contain sibling raw turns or the sentinel. A
child envelope with an unknown top-level key is rejected. A 199/200/201-token root
directive test checks the exact `CORE_DIRECTIVE_MAX` truncation marker. The prompt-lint
asserts no timestamp, request ID, generation, nonce, or volatile path enters the static
prefix.

Prefix-cache tests compare measured prompt bytes and provider usage metadata under a
pinned fake provider. They may report a stable prefix, but they must not assert a cost
discount or latency win without provider evidence. A test that changes provider/model,
dataset, or context layout must record a new baseline instead of reusing a prior claim.

## Appendix D — failure/restart sequencing

The restart suite separates worker crash, provider outage, EOF advisory, and supervisor
stall. A worker that exits without `exit_message` consumes restart budget; a provider
that returns `AllProvidersFailed` stays alive through patience; a grandchild-held pipe
requires ping/group kill; a stalled supervisor suspends heartbeat enforcement. Each test
asserts generation increments and no stale worktree write reaches main. A gate failure
uses raw process exit status, not a shell pipeline's final `tail` or a worker's success
token. A merge conflict records paths and expected SHAs before a resolver/remerge.

Canary promotion tests run a variant that deletes a failing test, inserts `assert True`,
writes `.cambium/`, or pads an output. Every variant must fail a canary even if its
training metric increases. Dataset hygiene tests reject secret patterns, duplicate IDs,
cross-split leakage, and malformed fields as hard failures; they never silently skip a
bad record. This is the historical reason the test strategy keeps canaries in a separate
gate from ordinary eval rows.

## Appendix E — harness contract assertions

The original strategy treated the harness as the system under test. Pure contract checks
covered tree validation, envelope filtering, retry-budget arithmetic, and event sequence
allocation. Scenario tests then exercised the real process boundary with a worker script;
they did not replace the boundary with an in-process mock. Every scenario recorded the
worker generation, task ID, worktree path, and event sequence so a failure could be
replayed from durable data.

Tree admission tests were deliberately front-loaded. A plan with duplicate IDs, unknown
dependencies, two roots, a cycle, excessive depth, or fan-out failed before `spawn` was
called. A valid static DAG admitted only dependency-ready nodes, sorted by depth and
`width_idx`, and never exceeded `max_width`. Dynamic child proposals were queued for a
new validated revision; a child could not mutate the active tree from inside a worker
turn. This records the explicit-tree direction without claiming that current flat
`run_plan` implements it.

Context tests enforced the information boundary. A child context contained its own
bounded turns, the parent summary, and strict upward envelopes. It excluded sibling
raw turns, hidden scratchpad text, prompts, provider credentials, timestamps, nonce
values, and volatile paths from the static prefix. An unknown envelope key, an overlong
directive, or an attempt to return a trajectory was a hard failure. Prefix-cache tests
measured serialized bytes and provider usage metadata under a pinned fake provider;
they did not turn a measurement into a cost or latency guarantee.

## Appendix F — failure-injection catalogue

The fake worker modes were intentionally orthogonal: clean result, nonzero exit, delayed
ready, heartbeat silence, stdout flood, malformed JSON, partial final line, grandchild
holding a pipe, provider outage, gate failure, merge conflict, and stale-generation write.
The supervisor tests asserted the distinction between a provider patience timeout and a
worker crash, between EOF and process death, and between an advisory output drop and a
critical result drop. A test that passed only because a shell pipeline returned the
status of `tail` was rejected; the raw process status was the oracle.

Restart tests checked bounded burst and absolute budgets, generation fencing, no stale
worktree publication, and idempotent merge reconciliation. Logging tests checked that
redacted values did not appear in SQLite, JSONL, stderr mirrors, or rotated files. The
security suite exercised list-form `grep_code` and `git_op`, path traversal, symlinks,
environment allowlists, prompt-injected repository text, and canary-gaming variants.

## Appendix G — historical verification boundaries

The snapshot's command record used system Python and pinned source revisions. Typical
checks were the full pytest suite, targeted scenario selectors, and small scripts that
inspected raw event rows. A green targeted test was evidence for that
scenario only; it was not a claim that the full suite, power-loss path, macOS signals,
free-threaded Python, or live provider network had passed. These limits explain why
several IDs remain **UNVERIFIED** even when neighboring canaries were green.

## Appendix H — evidence labels

`VERIFIED` meant the named command produced the expected observation on the recorded
source revision. `UNVERIFIED` meant the document had a design assertion, a missing
fixture, or a platform/network path that was not run. `PROPOSED` marked a future tree,
compaction, or provider behavior. The strategy kept these labels next to scenario IDs so
a later reader could not mistake a test template, source grep, or review statement for a
passing end-to-end run.

Scenario names were kept stable so later audits could compare failure evidence. The
strategy preferred a small fake worker with deterministic modes over a live provider or
network dependency. When a scenario needed a command result, the command, cwd, source
revision, and raw exit status were recorded. A missing fixture or skipped platform path
was reported as **UNVERIFIED**, not hidden by a broad marker.

Canary IDs remain useful only when their fixture and oracle remain unchanged.

Changing a fake-worker mode, provider stub, or event schema required a new baseline and
preserved the old result as historical evidence.

The strategy did not claim universal platform coverage.

The canary gate remained independent from ordinary metric improvement, so reward hacking
could not turn a green score into a green safety result.

Test IDs are historical anchors.

Current status belongs to tests and v2-1-status.

Skipped checks remain unverified.

Canaries are not production status.

Keep command evidence.

Record raw exit status.

Historical only.

Historical identifiers retained: `DS-M2`, `DS-M6`, `IMPL-N10`, `IMPL-N14`, `LLM-C6`,
and `LLM-M4`.
