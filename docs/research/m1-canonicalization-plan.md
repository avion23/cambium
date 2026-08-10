# M1 Canonicalization Plan — one runtime, one store, one sequencer

**Date:** 2026-08-09
**Branch:** `wt-doc-m1` off `main@6109a6a`
**Scope:** design document (docs only). Implements milestone M1 of `docs/research/v2-1-review.md` §3: integrate ONE Custos runtime, remove slice/fallback paths, rerun the three audits against one SHA.

## 0. Purpose

`v2-1-review.md` diagnoses the release blocker precisely: modules are built faster than they are integrated. On `main`, `EventStore` and `MergeSequencer` are unit-tested but have **no production caller**; `supervisor.py` is still the slice (`supervisor.py:1-25` scope guard: "this is the slice, not Custos"), writing its own JSONL `EventLog` and publishing with plain `git merge --ff-only`. The Custos multi-worker runtime (`run_plan`, `_Runtime`) exists only on `wt-impl-super@9746b96/0a83016`, embedded with silent `_FallbackEventStore`/`_FallbackSequencer` drop-ins that "undermine the claim that integration failures fail loudly" (`v2-1-review.md:86`).

This plan makes `run_plan` the single execution path, makes `cambium.store.EventStore`/`cambium.merge.MergeSequencer`/`cambium.worker` hard runtime dependencies, deletes every slice/fallback duplicate, and re-audits the result.

## 1. Baseline verified for this plan

| Claim | Evidence | Status |
|---|---|---|
| `main` scenario suite is green | `.venv/bin/python -m pytest tests/scenarios -q` → **108 passed** (tasktree 29, ipc 22, dataset_splits 19, merge 14, vertical_slice 8, store 7, example_module 6, tooling 3), Python 3.14.7 | VERIFIED |
| Custos fan-out suite is green on its branch | `PYTHONPATH=src pytest tests/scenarios/test_supervisor_fanout.py` at `wt-impl-super@0a83016` → **7 passed** (T1–T6) | VERIFIED |
| `cambium.store`/`cambium.merge`/`cambium.worker`/`cambium.supervisor` have no production caller on `main` | grep of `src/`: only `cambium.worker` imports `cambium.ipc` (`worker.py:64`); `orchestrator.py:15` imports `cambium.events` | VERIFIED |
| `wt-impl-super` ships no `store.py`/`merge.py`/`worker.py`/`ipc.py`/`doctor.py` | `src/cambium/` on that branch = `{events.py, __init__.py, modules, orchestrator.py, supervisor.py}` | VERIFIED — this is why the import guards and `_Fallback*` exist |
| `doctor` already reads the canonical store DB | `doctor.py:33,139-161` checks `.cambium/events.db` read-only | VERIFIED |

The three audit reports exist on their own worktrees, not on `main`:
- Conformance: `wt-audit-conformance@30832d1` → `docs/research/conformance-report.md`
- Security: `wt-audit-security@6a137fb` → `docs/research/security-audit.md`
- Constitution: `wt-audit-constitution@cb3dde2` → `docs/research/constitution-compliance.md`

All three audited `main@3d27ba3`, before IPC/worker/task-tree/datasets merged. **No audit has seen the integrated runtime.**

---

## 2. Inventory — every slice/fallback/duplicate path

Line citations are repo-relative. The Custos file is cited as `wt-impl-super` for its current home; after Step 1 the same symbols live in `src/cambium/supervisor.py` on `main`. Classification key: **KEEP** (canonical), **MIGRATE** (slice → custos), **DELETE** (dead/superseded), **DEFER** (v2.1 scope, out of M1).

### 2.1 Execution paths

| # | Item | Location | Class | Reason |
|---|---|---|---|---|
| 1 | `run_session` — slice single-worker supervisor (spawn → init/ready/run_task → gate → `git merge --ff-only`) | `supervisor.py:181-372` (`main`); `wt-impl-super:185-376` | MIGRATE | Becomes a thin adapter over `run_plan` with a 1-task plan (§4.2 mapping contract). The slice body is the executable form of security F-01/F-20 and conformance M2 (`v2-1-review.md:129-133`). |
| 2 | Slice CLI `main` (`--session-dir` + `--task-spec`) | `supervisor.py:423-447` (`main`); `wt-impl-super:1652-1690` | MIGRATE | Single-task CLI mode becomes the plan CLI with a 1-task plan (task requirement #2). |
| 3 | `run_plan` + `_Runtime` — Custos multi-worker runtime | `wt-impl-super:763-1484` (runtime), `:1523-1557` (`run_plan`) | KEEP | The canonical runtime. To be merged onto `main` in Step 1. |
| 4 | `Orchestrator.run` skeleton submit/drain (emits `WorkerStarted`/`WorkerFinished` placeholders, no work) | `orchestrator.py:20-59` (`main`); `wt-impl-super:71-75` | DELETE | A no-op path that emits start/finish without work is more dangerous than a missing API (`v2-1-review.md:506-508`). Replace with Architectus in M5, not another adapter. |
| 5 | `Orchestrator.run(session_dir, plan)` forwarding to `run_plan` | `wt-impl-super:60-70` | KEEP | The library-level entry point that exercises the canonical runtime. |
| 6 | Slice `_default_spec` (default worker = `scripts/fake_worker.py`) | `supervisor.py:375-388` (`main`); `wt-impl-super:1576-1589` | DELETE | Adapter default worker becomes `cambium.worker`. |
| 7 | `_bootstrap_scratch` / `_load_task_spec` / `_sh` (slice CLI repo bootstrap) | `supervisor.py:391-421` (`main`); `wt-impl-super:1592-1621` | MIGRATE | `_ensure_repo_initialized` (`wt-impl-super:1560-1573`) is the canonical repo bootstrap; the slice bootstrap is folded into the adapter or deleted. |
| 8 | `make_request_id(seq)` (ns-hex+seq) | `supervisor.py:48-50` (`main`); `wt-impl-super:52-54` | KEEP | Used by `_Runtime._next_rid` (`wt-impl-super:791-793`). |

### 2.2 Event stores (the duality that motivated the plan)

| # | Item | Location | Class | Reason |
|---|---|---|---|---|
| 9 | `EventLog` — JSON-Lines `{kind,timestamp,payload}`, written on the event loop, no fsync | `supervisor.py:67-87` (`main`); `wt-impl-super:71-92` | DELETE | Conformance M2 (`conformance-report.md:178`), constitution (c) "every disk write off the event loop" (`constitution-compliance.md:112-117`), F-02/F-17. The slice's `EventLog` is the "slice EventLog" the M1 acceptance criteria name explicitly (`v2-1-review.md:324-325`). |
| 10 | `EventStore` — SQLite WAL, writer thread, fsync-before-ack, critical kinds | `store.py:93-290` (append `:123-149`, writer loop `:172-253`, `_fsync_now` `:267-272`) | KEEP | The canonical store. Conformance-verified mechanism (`conformance-report.md` §1.2-§1.3). |
| 11 | `_FallbackEventStore` — inline SQLite store in supervisor, no writer thread, discards checkpoint return, no `StoreError`/dead-writer semantics | `wt-impl-super:455-538` | DELETE | Silently selects weaker durability when `cambium.store` is absent (`v2-1-review.md:499-501`: missing components must fail import, not degrade). Its `_SCHEMA` (`:464-474`) duplicates `store.py:47-57`. |
| 12 | `_resolve_event_store` / `_open_store` import guards + fallback branch | `wt-impl-super:685-690`, `:693-698` | DELETE | Replaced by `from cambium.store import EventStore` at module top. Import failure then fails at load (fail-loud). |
| 13 | `events.py` seed dataclasses — `Event`/`WorkerStarted`/`WorkerFinished`/`LogEvent`, envelope `{type,timestamp}` | `events.py:1-47` (only importer `orchestrator.py:15`) | DELETE | Conformance M4 (`conformance-report.md:148-159`): orphaned, and its `type`/`timestamp` contradict the store's `kind`/`ts` envelope. Constitution §2(l) (`constitution-compliance.md:253-274`). Event-schema-draft canonical envelope is `kind/seq/ts/monotonic_ms/task_id/worker_id/request_id/generation/payload` (`event-schema-draft.md:28-45`). |
| 14 | `doctor` event-store integrity check | `doctor.py:139-161` (`EVENTS_DB_REL = ".cambium/events.db"`, `:33`) | KEEP | Canonical consumer of the store DB; stays. |
| 15 | `events.jsonl` artifact under the session dir | written at `supervisor.py:195` (`main`); `wt-impl-super:199` | DELETE | Superseded by `events.db`; `read_events` (below) is the replay surface. |

### 2.3 Merge sequencers

| # | Item | Location | Class | Reason |
|---|---|---|---|---|
| 16 | `MergeSequencer` — atomic expected-old `update-ref` publish, `create_main`, quarantine refusal, staging-ref capture | `merge.py:158-533` (`prepare_staging` `:281-344`, `create_main` `:372-407`, `publish_merge` `:409-482`, `reconcile` `:484-494`, `cleanup_staging` `:496-533`) | KEEP | The canonical Unio core; all worktree-concurrency findings verified (`conformance-report.md` §3). |
| 17 | `_FallbackSequencer` — stripped stand-in: no `create_main`, no ancestry check, no quarantine refusal, `--force`-removes worktrees | `wt-impl-super:541-674` | DELETE | Weaker merge semantics selected silently when `cambium.merge` is absent (`v2-1-review.md:499-501`). Its porcelain parsing lacks `-z` (`:657-674` vs `merge.py:252-277`). |
| 18 | `_resolve_merge_sequencer` guard + `_make_sequencer` fallback branch | `wt-impl-super:677-682`, `:1430-1433` | DELETE | Hard import of `cambium.merge.MergeSequencer`. |
| 19 | `_merge_branch` — slice `git merge --ff-only` (working-tree merge, no expected-old, no first-publish guard) | `supervisor.py:161-178` (`main`); `wt-impl-super:165-182` | DELETE | Security F-20's runtime bypass (`security-audit.md:53`): the only *tested* protections live in `MergeSequencer`, which the slice never calls. |

### 2.4 Workers

| # | Item | Location | Class | Reason |
|---|---|---|---|---|
| 20 | `scripts/fake_worker.py` — protocol fixture + demo worker | `scripts/fake_worker.py:1-130` (`do_work` `:52-92`, `main` `:95-126`) | KEEP (fixture only) | Review verdict: "Keep `scripts/fake_worker.py`, but only as a test fixture. It must not be a production worker choice" (`v2-1-review.md:524-525`). After Step 5, grep-verified to be referenced only from tests. |
| 21 | `src/cambium/worker.py` — Opifex seed, `python -m cambium.worker` | `worker.py:1-553` (`do_work` `:111-216`, wire loop `:356-494`) | KEEP | The canonical production worker; becomes `run_plan`'s default via `_worker_command` (`wt-impl-super:954-964`). Its task spec is explicitly fake_worker-compatible (`worker.py:31-43`). |
| 22 | `tests/fixtures/crash_worker.py` — in-place crash fixture (T3 worktree-recovery proof) | `tests/fixtures/crash_worker.py:1-81` | KEEP (fixture) | Test-only; moved to `main` with the fan-out tests in Step 1. |
| 23 | `_worker_command` defaulting | `wt-impl-super:954-964` | KEEP | Already prefers `cambium.worker`; raises `ValueError` only if the module is missing. |

### 2.5 IPC / wire framing

| # | Item | Location | Class | Reason |
|---|---|---|---|---|
| 24a | `_next_message` — slice-only message pump | `supervisor.py:131-138` (`main`); `wt-impl-super:135-142` | DELETE | The Custos drive loop awaits `messages.get()` inline with its own deadline handling (`wt-impl-super:1237-1240`); the helper has no runtime caller. |
| 24b | Runtime inline reader/writer — `_read_stdout`/`_read_stderr`/`_write_json` | `wt-impl-super:118-124`, `:1143-1178` | KEEP | Part of the canonical runtime for M1; framing unification onto `ipc.py` + FD-3 is M2 (`v2-1-review.md` §B). The slice copies of `_read_stdout`/`_read_stderr` (`supervisor.py:219-241` on `main`) die with the slice. |
| 25 | `ipc.py` — Nuntius NDJSON framing (1 MiB cap, resync, skip non-objects, `read_message`/`write_message`) | `ipc.py:1-129` (`MAX_LINE_BYTES` `:28`, `read_message` `:100-129`) | KEEP | Canonical framing; consumed by `worker.py:64`. Runtime transport migration → DEFER (M2). |
| 26 | `ipc.make_request_id` (uuid) vs supervisor's (ns+seq) | `ipc.py:43-45`; `supervisor.py:48-50` | DEFER | `ipc`'s has no production caller (only `test_ipc.py:240-244`; constitution §2(l) `:269-271`). Keep as a public framing helper or drop in v2.1 — not M1-blocking. |
| 27 | Message vocabulary `result_envelope`/`exit_message` vs arch `result`/`exit` | runtime accepts both (`wt-impl-super:1278`, `:1290`) | DEFER | Conformance L6 vocabulary seam (`conformance-report.md:141`). Accepted for M1; canonicalize in the audit fold. |

### 2.6 Event model / schema

| # | Item | Location | Class | Reason |
|---|---|---|---|---|
| 28 | Runtime event envelope — `worker_id = f"{task_id}:{generation}"`, `ts` float, `monotonic_ms` | `wt-impl-super:795-813` | KEEP | The canonical producer envelope matching `store.py` columns. Drifts flagged: draft D3 writes `"{task_id}#{generation}"` (`event-schema-draft.md:38`) → LOW drift to reconcile in the re-audit. |
| 29 | event-schema-draft canonical envelope | `docs/research/event-schema-draft.md:26-45` | KEEP (normative reference) | `event_id` is draft-only (`:32`); `ts` float vs store `TEXT` is conformance L1 (`conformance-report.md:183`). Both LOW. |
| 30 | Local `CRITICAL_KINDS` duplicate | `wt-impl-super:412-415` (== `store.py:42-45`) | DELETE | Import `CRITICAL_KINDS` from `cambium.store` in Step 3 — single source. |

### 2.7 Helpers / duplicates inside `supervisor.py`

| # | Item | Location | Class | Reason |
|---|---|---|---|---|
| 31 | `_cfg_float` (spec-or-env float) | `wt-impl-super:94-98` | KEEP | Used by the runtime (`:1013-1026`). |
| 32 | `_validate_paths` (slice path guard) | `supervisor.py:97-111` (`main`); `wt-impl-super:101-115` | DELETE | Slice-only; the runtime uses `_validate_plan_task` (`wt-impl-super:1498-1520`). |
| 33 | `_kill_worker` (killpg) | `supervisor.py:123-128` (`main`); `wt-impl-super:127-132` | KEEP | Runtime process-group kill (`:1211`, `:1233`, `:1253`). |
| 34 | Module-level `_run_gate` (slice gate runner) | `supervisor.py:141-158` (`main`); `wt-impl-super:145-162` | DELETE | The runtime owns gate execution in `_Runtime._run_gate` (`wt-impl-super:1386-1426`), which adds the tree-hash skip (`:1395-1400`). |
| 35 | `SliceResult` frozen dataclass | `supervisor.py:53-64` (`main`); `wt-impl-super:57-68` | KEEP | Public return type of the `run_session` adapter; field mapping in §4.2. |
| 36 | `_Runtime._writer_loop` swallowing store errors (`print(..., file=sys.stderr)`) | `wt-impl-super:815-823` | DEFER | A dead store should be fatal per `store.py:23-25`; bounded-backpressure handling is M4 (`v2-1-review.md` P0 #10). Flag for the audit. |
| 37 | `_strip_sensitive_env` (name-based env scrub) | `wt-impl-super:422-424` | KEEP | Partial F-01 mitigation; M3 builds the strict allowlist (`security-audit.md:34`). |
| 38 | `_validate_plan_task` path checks | `wt-impl-super:1498-1520` | KEEP | Confines `worktree_path`; `repo` unconfined = accepted-as-trust F-03 (`security-audit.md:36`). |
| 39 | `read_events` (replay over `events_after`) | `wt-impl-super:701-707` | KEEP | Public replay API; the consumer that makes conformance's §6 replay item checkable. |

### 2.8 Acceptance-gap items

| # | Item | Location | Class | Reason |
|---|---|---|---|---|
| 40 | `result.json` per task — M1 acceptance criterion 2 ("writes result.json") | **no implementation** | DEFER/ADD | `run_plan` persists results via events + `PlanResult`; no `result.json` artifact exists. Either add a per-task `result.json` write in Step 6 or formally accept events.db + PlanResult as satisfying the criterion. Explicit decision point; see Step 6. |

### Inventory counts

| Class | Count |
|---|---|
| KEEP | 20 |
| MIGRATE | 3 |
| DELETE | 14 |
| DEFER | 4 |
| **Total rows** | 41 |

---

## 3. Target state

After M1, `main` looks like:

- **One runtime.** `run_plan` (`supervisor.py`) is the only execution path. The single-task CLI (`--session-dir` + `--task-spec`) and `run_session` are thin adapters that build a 1-task plan and call `run_plan`. `_Runtime` is the only supervisor. `Orchestrator.run(session_dir, plan)` forwards to `run_plan`.
- **One event store.** `from cambium.store import EventStore, CRITICAL_KINDS` is a module-top import; `_open_store`/`_FallbackEventStore`/`EventLog`/`events.jsonl` are gone. Events land in `<session_dir>/.cambium/events.db`; `read_events` replays; `doctor` verifies.
- **One merge sequencer.** `from cambium.merge import MergeSequencer` at module top; `_FallbackSequencer`/`_make_sequencer` fallback/`_merge_branch`/`git merge --ff-only` are gone. Publication is `publish_merge` (expected-old + ancestry) under the runtime's `_merge_lock` (`wt-impl-super:1451-1460`), emitting fsynced `merge_committed` (`:1480-1483`).
- **One worker.** Default worker is `python -m cambium.worker`. `scripts/fake_worker.py` and `tests/fixtures/crash_worker.py` exist only as test fixtures (grep-verified no production reference).
- **One event model.** The store envelope (`kind/ts/seq/...`). `events.py` deleted; `orchestrator.py` no longer imports it.
- **Audits are rerunnable.** No conformance N-A item caused by unmerged modules; security slice-era findings are verdicts about the runtime that actually runs.

### Test decision: migrate `test_vertical_slice.py` (with explicit updates)

**Decision: MIGRATE the slice tests, do not keep them byte-identical.**

Justification:

1. The 8 scenarios encode real behavioral guarantees (happy path, gate-failure-no-merge, nonzero worker exit, missing exit_message, missing result_envelope, misrouted request_id, rid echo, ready timeout) that must survive — but pointed at canonical Custos, matching the review's M1 instruction "Keep the behavioral scenario, pointed at canonical Custos" (`v2-1-review.md:498`).
2. Keeping the tests unchanged would freeze the slice's semantics into the adapter: no restart policy, worker exit-code propagation (exit 5 → supervisor exit 5), timeout taxonomy exit-code 3, and an `events.jsonl` on disk. Each of those is exactly what M1 deletes. A byte-identical test suite and a canonical runtime are contradictory.
3. The canonical runtime's observable differences are real and testable: crash → restart-to-cap (default 3) instead of immediate failure; `PlanResult.exit_code ∈ {0,1}`; events durable in `events.db` read via `read_events`; extra lifecycle kinds (`task_assigned`, `spawned`, `merge_started`, `merge_committed`, `session_ended`) that the slice never emitted. The migrated tests assert the canonical outcomes.
4. The slice edge cases (`badrid`, `noresult`, `noexit`, `exit5`, gate-fail) are *not* covered by `test_supervisor_fanout.py` (T1–T6), so the migrated file adds unique run_plan-level coverage. `FAKE_MODE` variants keep working unchanged because `fake_worker.py` remains the fixture.

The 8 test functions are renamed or re-asserted per the mapping contract in §4.2 Step 2.

---

## 4. Migration steps in dependency order

Each step is one commit on `main`. Steps 1–3 mutate `supervisor.py` serially (a single agent owns that file's sequence). Steps 4–5 touch disjoint files and can run in parallel with Step 3. Step 7 is three independent audit agents after 1–6 land. Every step's gate is the full scenario suite on Python 3.14.

### Step 1 — Merge Custos runtime onto `main` (coexistence commit)

**Files:** `src/cambium/supervisor.py`, `tests/scenarios/test_supervisor_fanout.py` (new), `tests/fixtures/crash_worker.py` (new).

- Replace `main`'s slice-only `supervisor.py` (451 lines) with the merged slice+Custos file from `wt-impl-super@0a83016` (1694 lines). Both `run_session` (slice, intact) and `run_plan` (Custos) coexist; import guards and `_Fallback*` remain for this step only.
- Because `main` already has `store.py`/`merge.py`/`worker.py`/`ipc.py`, the guards resolve to the **real** classes: `_open_store` → `EventStore`, `_resolve_merge_sequencer` → `MergeSequencer`, `_worker_command` default → `cambium.worker`. The fallbacks are dead-but-present until Step 3.
- Add `tests/scenarios/test_supervisor_fanout.py` and `tests/fixtures/crash_worker.py`.
- Keep `events.py`/`orchestrator.py` untouched.

**Test gate:** `pytest tests/scenarios -q` → 115 passed (108 existing + 7 fan-out), Python 3.14. `git grep _FallbackEventStore` still hits (expected at this step).

**Rollback:** `git revert` the single commit.

### Step 2 — `run_session` → thin adapter over `run_plan`; delete slice machinery + `EventLog`; migrate `test_vertical_slice.py`

**Files:** `src/cambium/supervisor.py`, `tests/scenarios/test_vertical_slice.py`.

- Rewrite `run_session(session_dir, task_spec, on_event)` as an adapter:
  1. Build a 1-task plan: `{"tasks": [ _adapt(task_spec) ]}` where `_adapt` maps slice spec → plan task: `repo=scratch_repo`, `task=spec`, `worktree_path` (validated inside the session dir), `branch`, `target_file`, `marker`, `write_marker`, `gate`, `base_commit=None` (runtime resolves `refs/heads/main`), `ready_timeout_s`, `gate_timeout_s`, `max_wall_s=wall_budget_s`, and **`max_restarts=0` by default** (slice parity: the slice never restarted; override via `task_spec["max_restarts"]`).
  2. `plan_result = await run_plan(session_dir, plan, on_event=on_event)`; take `task = plan_result.results[0]`.
  3. Map to `SliceResult` (keeps the public return shape):
     - `status = task.status`
     - `exit_code = plan_result.exit_code`
     - `worker_exit_code = task.exit_code` (0/1)
     - `worker_status = task.status`
     - `gate_exit_code = task.gate_exit_code`
     - `merge_sha = task.merge_sha`
     - `timed_out`/`timeout_phase` via substring on `task.reason`: the runtime always fails timeout crashes through the restart-cap branch, so the reason is prefixed (`"max_restarts (0): ready"`). Map `timed_out = any(p in (task.reason or "") for p in ("ready", "wall", "heartbeat"))`; `timeout_phase` = the matching phase, else `None`. A **gate** timeout surfaces as reason `"gate_failed"` in canonical semantics (the runtime emits `gate ... timed_out=True` but records `gate_failed`), so gate is not a timeout phase in the adapter.
     - Preserved reason vocabulary from `_GenOutcome` (`wt-impl-super:1366-1377`): `worker_exit_N`, `missing_exit_message`, `missing_result_envelope`, `result_request_id_mismatch`, plus `gate_failed`/`merge_failed`/`max_restarts (N): …`.
- **Provider fan-out porting note:** the legacy slice currently has a temporary
  `run_session` bridge for `fanout_config`. Until this step lands, that bridge
  must carry the non-empty `task`, `fanout_config`, and `provider_env_keys` into
  the one-task plan/run payload. The canonical `_Runtime` path already carries
  `task` in `_run_payload` and the provider configuration in `init`; retain the
  provider response-model validation at the worker/provider boundary when the
  slice body is removed. Do not copy the slice's `EventLog` or `git merge
  --ff-only` behavior into the M1 `run_plan` path.
- Delete from `supervisor.py`: `EventLog` (row 9), `_merge_branch` (19), module-level `_run_gate` (34), `_next_message` (24a), `_validate_paths` (32), slice run-loop bodies, slice `_default_spec` (6), `_bootstrap_scratch` (7 → fold into `_ensure_repo_initialized`). Keep `SliceResult` (35), `_cfg_float` (31), `_write_json`/`_kill_worker` (24b/33), `_strip_sensitive_env` (37).
- CLI (`main`, `wt-impl-super:1652-1690`): the `--session-dir`/`--task-spec` single-task mode now builds a 1-task plan (default worker `"cambium.worker"`) and calls the plan path; the `--plan` mode is unchanged.
- Migrate `test_vertical_slice.py` (8 scenarios, same names/guarantees, canonical assertions):
  - Read events via `cambium.supervisor.read_events(session_dir)` (events.db), never `events.jsonl`.
  - Happy path → succeeded, merged, protocol sequence `["init","ready","run_task","result","exit"]` filtered from db events.
  - Gate-failure → failed, no merge, `main` unchanged.
  - `FAKE_MODE=exit5` → set `max_restarts=0`; assert failed, reason contains `worker_exit_5`, no merge, `main` unchanged (exit_code is 1, not 5 — canonical semantics).
  - `noexit` / `noresult` / `badrid` → failed with the preserved reason + a `protocol` event for `badrid`.
  - rid echo → `init`/`run_task` request_ids correlate via db events; `exit` carries no `request_id`.
  - `noready` + `CAMBIUM_READY_TIMEOUT_S=2` (adapter's `max_restarts=0`) → failed, `reason` contains `"ready"`, `timeout_phase == "ready"`, no merge, a `timeout` kind present in db events.

**Test gate:** `pytest tests/scenarios -q` → 115 passed (108 migrated-in-place + 7 fan-out). Additional assertions in the migrated file: `events.jsonl` does not exist; `events.db` does.

**Rollback:** revert the commit. (Both Steps 1 and 2 can be reverted independently; they are separate commits.)

### Step 3 — Hard imports; remove `_Fallback*`

**Files:** `src/cambium/supervisor.py`.

- Add `from cambium.store import CRITICAL_KINDS, EventStore` and `from cambium.merge import MergeSequencer` at module top.
- Delete `_FallbackEventStore` (11), `_FallbackSequencer` (17), `_resolve_event_store`/`_resolve_merge_sequencer` (12/18), `_open_store`'s fallback branch (12), `_make_sequencer`'s fallback branch (18), the local `CRITICAL_KINDS` copy (30; import from `store.py`).
- `_open_store` becomes `EventStore(session_dir / ".cambium" / "events.db")`; `_make_sequencer` becomes `MergeSequencer(task_id=task_id)`.
- Any missing architecture component now fails at import time — the fail-loud property the review demands (`v2-1-review.md:499-501`).

**Test gate:** full suite green; `git grep -c "_Fallback\|EventLog" src/` → 0. This satisfies M1 acceptance criterion 1 (`v2-1-review.md:324-325`).

**Rollback:** revert the commit.

### Step 4 — Delete `events.py` + orchestrator skeleton *(parallel with Step 3; disjoint files)*

**Files:** `src/cambium/orchestrator.py`, `src/cambium/events.py` (+ any grep-discovered importers).

- `Orchestrator`: keep `run(session_dir, plan)` forwarding to `run_plan` (`wt-impl-super:60-70`); the `on_event` forwarder stops constructing `Event(...)` and emits plain `{kind, ts, payload}` records. Delete `submit()`/`_queue`/skeleton drain (`wt-impl-super:28-46,71-75`).
- Delete `src/cambium/events.py`. Update `orchestrator.py:15-16` import.
- Constitution §2(l) dead-code items (`events.py`, no-op orchestrator) both resolve here.

**Test gate:** full suite green; `git grep -c "cambium\.events" src/ tests/` → 0; `uv run ruff check src/` clean.

**Rollback:** revert the commit.

### Step 5 — Canonical worker; `fake_worker` fixture-only *(parallel with Steps 3–4)*

**Files:** `src/cambium/supervisor.py` (default spec only — serialize against Steps 1–3), `scripts/fake_worker.py` (docstring), `README.md` if it names fake_worker as a production choice.

- Grep-verify no production reference to `scripts/fake_worker.py` in `src/` (the only references are `tests/scenarios/test_vertical_slice.py` and `tests/scenarios/test_supervisor_fanout.py`).
- Add one run_plan scenario (or extend T1) driving the **default** worker (`python -m cambium.worker`) through the T1 happy path — this is the "one worker edits one file, publishes, emits fsynced merge_committed" proof of M1 criterion 2 with the canonical worker.
- Update `fake_worker.py`'s module docstring to state fixture-only status.

**Test gate:** full suite green; `git grep "scripts/fake_worker" src/` → 0 hits.

### Step 6 — M1 acceptance gap: `result.json` *(small, optional; decision point)*

**Files:** `src/cambium/supervisor.py` (or none).

- M1 criterion 2 says the single worker "writes `result.json`" (`v2-1-review.md:327`). No such artifact exists in the runtime (inventory row 40).
- **Option A (recommended):** in `_supervise`'s terminal branches, write `<worktree or session>/.cambium/results/<task_id>.json` with `TaskResult` fields after gate+merge decision — small, isolated, keeps the criterion true.
- **Option B:** amend the criterion: events.db `result` kind + `TaskResult` return is the durable result; `result.json` was slice-era output. Requires an architecture-doc owner sign-off.
- Record the choice in the commit message; leave the criterion satisfied or formally amended.

**Test gate:** full suite green (+ assertion on the artifact if Option A).

### Step 7 — Re-run the three audits against one SHA

**Files (new, not edits to the old audit docs):** `docs/research/m1-conformance-report.md`, `docs/research/m1-security-audit.md`, `docs/research/m1-constitution-compliance.md`. The old audit files stay immutable with their baseline SHA (`v2-1-review.md:521-523`).

- Three parallel audit agents, one per audit, on the post-Step-6 SHA. Each records the scenario count and SHA.
- Scope per §5 checklist below.

**Test gate:** audit deliverables contain zero N-A items caused by unmerged modules (M1 criterion 4); scenario count + SHA recorded.

---

## 5. Audit re-run checklist

### 5.1 Conformance (`conformance-report.md`)

| Old item | Old status | Becomes | Expected verdict change |
|---|---|---|---|
| Check 4 §4 — `ipc.py`/`worker.py` not on main (`:130-134`) | N-A | Checkable: both merged (`git log --oneline` shows `38e1d43`); §5 framing exercised by `test_ipc.py` (22) and `cambium.worker` as the runtime default | N-A → CONFORM (framing) / CONFORM-with-gaps (worker contract: `do_work` catch-all, §8.3) |
| M2 §7 — no production wiring; §7.8 `merge_committed`/`merge_reconciled` + single-writer lock have no caller (`:178`) | MEDIUM | Checkable: `run_plan` writes through `EventStore`; `_merge_task` holds `_merge_lock` across verify+publish and emits `merge_committed` (`wt-impl-super:1451-1483`) | MEDIUM → CLOSED, residual: `merge_reconciled` still never emitted (`reconcile` hook `merge.py:484-494` unused) → LOW/DEFER |
| M3 §7 — IPC/worker unmerged (`:179`) | MEDIUM | Merged (same as Check 4) | MEDIUM → CLOSED |
| M4 §7 — `events.py` orphaned (`:180`) | MEDIUM | Deleted (Step 4) | MEDIUM → CLOSED |
| M5 §7 — `worker_id` derivation not implemented (`:181`) | LOW/MED | Checkable: runtime emits `worker_id=f"{task_id}:{generation}"` (`wt-impl-super:802`); draft D3 says `#` (`event-schema-draft.md:38`) | LOW/MED → CONFORM-with-drift (separator `:` vs `#`; decide in audit) |
| §6 end-to-end store↔worker seq/worker_id/generation — "no producer on main" (`:167`) | N-A | Checkable: `run_plan` is the producer; seq from `EventStore`, generation from `WorkerHandle` | N-A → CONFORM-with-gaps |
| §6.2 subscriber/ring-buffer/replay — "no caller" (`:191`) | N-A | Replay gains a caller (`read_events`/`events_after`); the subscriber/ring-buffer itself is still architect-future (Architectus, M5) | Partially checkable; remaining part flagged as spec-future, NOT unmerged-module N-A |
| L1–L8 store DDL drifts (`:182-189`) | LOW | Now on the live path (`ts` str-coercion `store.py:129`, no indexes, unbounded queue, checkpoint `busy` discarded `store.py:268-269`) | Verdicts unchanged (LOW); re-verified against the runtime that actually runs |
| L6 status vocabulary (`succeeded` vs `done`) (`:187`) | LOW | Runtime accepts `result`/`result_envelope` and `succeeded`/`failed`/`cancelled` | Unchanged; canonicalization is a v2.1 fold decision |

### 5.2 Security (`security-audit.md`) — slice-era findings against the canonical runtime

| Finding | Old verdict | Becomes | Expected verdict change |
|---|---|---|---|
| F-01 full host env inheritance (`:34`) | HIGH, open | Checkable against `_worker_env` + `_strip_sensitive_env` (`wt-impl-super:422-424,966-971`): name-based scrub exists; strict `provider_env_keys` allowlist (D7) still absent | HIGH → MEDIUM (scrubbing exists; allowlist still open for M3) |
| F-02 redaction absent (`:35`) | MEDIUM | Checkable: runtime still persists raw stderr (`:1168-1175`), gate output (`:1420-1425`), worker errors (`:1315-1320`) verbatim | MEDIUM, unchanged (M3 scope) |
| F-03 `scratch_repo` unconfined (`:36`) | MEDIUM, accepted | Checkable in `_validate_plan_task` (`wt-impl-super:1516`) | Unchanged (accepted-as-trust) |
| F-04 sequencer worktree clobber (`:37`) | MEDIUM | Runtime uses a dedicated `merge-wt/<task_id>` dir (`wt-impl-super:1448`); library-level ownership marker still absent (`merge.py:305-323`) | Downgrade: not reachable from `run_plan`; library guard DEFER → M3 |
| F-05 unvalidated refs/refspecs (`:38`) | MEDIUM (latent) | Checkable: branch interpolated in `_ensure_worktree_locked` (`wt-impl-super:920-922`) and `MergeSequencer._ensure_worker_tip` fetch (`merge.py:232`); still host-authored | Unchanged (latent; M3) |
| F-06 no stdin/merge deadline (`:39`) | MEDIUM | Checkable: `_write_json` (`wt-impl-super:118-124`) and `_merge_task` still unbounded | Unchanged (M2) |
| F-07 unbounded stdout queue (`:40`) | MEDIUM | Checkable: `messages = asyncio.Queue()` (`wt-impl-super:1140`) | Unchanged (M2) |
| F-08 read-cap mislabel / swallowed reader error (`:41`) | LOW | Runtime reader now always puts the EOF sentinel (`finally: messages.put(None)`, `wt-impl-super:1165-1166`); fail-fast on oversized line still absent | LOW, partially improved |
| F-16 unbounded store queue + no-timeout critical append (`:49`) | INFO/LOW | Unchanged (`store.py:20-22,106,145-148`) | Unchanged (M4) |
| **F-20** runtime bypass of hardened store/merge (`:53`) | **MEDIUM, open** | **Checkable and closed**: `run_plan` uses `EventStore` + `MergeSequencer`; slice `git merge --ff-only` deleted | **MEDIUM → MITIGATED** |
| UNVERIFIED §6 — worker/ipc absent (`:90`) | UNVERIFIED | `cambium.worker`/`ipc.py` on `main` and exercised | UNVERIFIED → VERIFIED (worker tool-confinement checks now land in code) |
| New surface | — | `_strip_sensitive_env` regex coverage; restart-loop bounded by `max_restarts`; `sh -c` gate (`:1401-1403`, F-09) | New verdicts recorded by the audit |

### 5.3 Constitution (`constitution-compliance.md`)

| Norm | Old verdict | Expected verdict change |
|---|---|---|
| (l) delete-over-add (`:253-274`) | PARTIAL | **Improves toward COMPLIANT**: `events.py`, orchestrator skeleton, slice runtime, `EventLog` all deleted; one event model remains. Residual: `ipc.make_request_id` (DEFER), `LogEvent` gone. |
| (c)/(e) store queue + lock (`:106-110`, `:136-145`) | PARTIAL (LOW) | Unchanged — the runtime is now the producer, same documented deviations |
| (i) enums over strings (`:178-208`) | PARTIAL (MEDIUM) | Unchanged: `TaskResult.status`, `WorkerHandle.state`, `EXIT_CODES` are still str allowlists. v2.1 enum migration (M8 fold) |
| §7 module CLI (`:275-296`) | PARTIAL (MEDIUM) | Unchanged (DEFER to M8 rename) |
| §8.3 let-it-crash (`:307-319`) | PARTIAL (LOW) | Unchanged: `worker.py:214` catch-all still masks task crashes; verify at the runtime boundary |
| (b) flat records / (h) flat control flow | COMPLIANT | Re-verify against the merged 1,694-line supervisor (the audit flagged `supervisor.py:332-363` 6-level nesting — that block is deleted with the slice) |

---

## 6. Risks and rollback

Every step is exactly one commit → `git revert <sha>` rolls a step back without touching the others. Steps 1–3 serialize on `supervisor.py`; Steps 4–5 are disjoint files and parallel-safe; Step 7 is three independent audit agents. Because no step rewrites history and none share a file, parallel-agent execution is safe with disjoint file scope.

| Risk | Likelihood / impact | Mitigation |
|---|---|---|
| **Behavior drift in migrated slice tests** (restart semantics, exit-code taxonomy, event source) silently weakens coverage | High / Medium | Step 2's explicit `SliceResult` mapping contract (§4.2) plus the renamed assertions; `FAKE_MODE` variants unchanged; `test_supervisor_fanout.py` T1–T6 remain the deep run_plan coverage |
| **Integration illusion**: guards resolve to real store/merge at Step 1, so fallback bugs hide until Step 3 | Medium / Medium | Step 3's gate is `git grep _Fallback == 0` + full suite; the fan-out tests exercise real `EventStore` fsync and `MergeSequencer` publish from Step 1 onward |
| **1694-line supervisor merge conflicts** with concurrent work on `main` | Low / Low | Wholesale replacement from `wt-impl-super@0a83016`; Steps 2–3 are small diffs on top; single-file ownership rule for `supervisor.py` |
| **`_writer_loop` swallows store death** (`wt-impl-super:822`) — a dead store yields silent data loss | Medium / Medium | Not M1-blocking; flag in the re-audit and track as M4 item (bounded backpressure + fatal store). Do not leave it unrecorded |
| **L1–L8 store drifts become live** once the runtime is the producer (`ts` TEXT, no indexes, busy return discarded) | Certain but LOW | Recorded in §5.1; none block M1 (they are LOW conformance drifts with owned fixes) |
| **`run_plan` oversubscription** (all tasks in one `TaskGroup`, P0 #9) | Medium / Medium | Out of M1 scope (M4 resource semaphore); note in audit deliverable |
| **`result.json` criterion unsatisfied** | Certain / Low | Step 6 explicit decision: add the artifact or amend the criterion with an architecture-doc owner |
| **Fake worker's destructive worktree re-create masks runtime bugs** (`fake_worker.py:73-76` removes the runtime-created worktree) | Low / Low | Keep `crash_worker.py` T3 as the genuine worktree-recovery proof; Step 5 adds the default-`cambium.worker` happy path |

---

## 7. Verification performed for this plan

- `pytest tests/scenarios -q` at `main@6109a6a`: **108 passed** (Python 3.14.7).
- `PYTHONPATH=src pytest tests/scenarios/test_supervisor_fanout.py -q` at `wt-impl-super@0a83016`: **7 passed**.
- Provenance confirmed: `wt-impl-super` has no `store.py`/`merge.py`/`worker.py`/`ipc.py`/`doctor.py` (guards + fallbacks exist for that reason); `main` has them and no production caller.
- Audit docs located on `wt-audit-{conformance,security,constitution}` worktrees; none post-date `main@3d27ba3`.

**UNVERIFIED (explicit):** Steps 1–7 themselves are not executed — this is a design document. The 41-row inventory and every `file:line` citation were read directly in the current `main` and `wt-impl-super` worktrees; the target-state assertions (e.g. Step 3 `git grep` cleanliness) are predictions that the executing agents must verify per step gate. The `result.json` gap (row 40) and the `_writer_loop` error-swallow (row 36) are the two items most likely to surface as real defects during execution.

---

## 8. Acceptance-criteria mapping (from `v2-1-review.md` §3 M1)

| M1 criterion | Where it lands |
|---|---|
| 1. `git grep` finds one event store, one sequencer, one supervisor entry; no `_FallbackEventStore`/`_FallbackSequencer`/`EventLog` | Step 3 gate (event store/sequencer/supervisor) + Step 2 (EventLog) |
| 2. One worker edits one file, passes gate, publishes via `update-ref`, emits fsynced `merge_committed`, writes `result.json`, leaves no process/worktree | Step 5 (canonical worker happy path) + Step 6 (`result.json` decision) |
| 3. Full suite passes on Python 3.14; count + SHA recorded | Every step gate; final count + SHA recorded in Step 7 deliverables |
| 4. Fresh audits contain no N-A caused by unmerged modules | Step 7 (§5 checklist) |
