# M1 Canonicalization Plan (corrected) — one runtime, one store, one sequencer

**Date:** 2026-08-10
**Branch:** `wt-m1-plan` off `main@b709375`
**Scope:** design document (docs only). Implements milestone M1 of `docs/research/v2-1-review.md` §3: integrate ONE Custos runtime, remove slice/fallback paths, rerun the three audits against one SHA.

This revision supersedes the previous M1 plan. The old plan is stale for five reasons:

1. **Step 1 (wholesale supervisor replacement) is obsolete.** `main` already has `_Runtime` + `run_plan` and the post-hardening (post-0867572) work; there is nothing to "merge onto main" and no coexistence commit to make.
2. **The old plan omits mandatory redactor integration.** Redaction (security F-02) is not optional M3 scope; it is a precondition of every event being persisted. It runs first.
3. **Step 2's test surface was incomplete** — it missed the slice-reader semantics tests in `test_supervisor_hardening.py` and the provider-bridge tests in `test_worker_provider.py` that assert `events.jsonl` absence.
4. **Step 6 conflicted with the merged canonical results contract.** A `result.json` writing step that writes `TaskResult` fields per task contradicts `cambium.results` / `ROOT_RESULT_KEYS`, which are already on `main`. The result wiring is now phase (d), not an optional step.
5. **The old plan claimed Steps 4–5 could run in parallel, but both touch `supervisor.py`** (Step 5 serialized against Steps 1–3 on the same file). Phases (a), (b), and (d) all own `supervisor.py`: they are ONE serialized effort executed as ordered commits within a single worktree merge — not one-file commits.

---

## 1. Corrected minimal sequence (5 phases)

Phases (a), (b), (c), (d) are strictly ordered; phase (e) is audits and docs only. Phases (a), (b), (d) all own `supervisor.py` and are ONE serialized effort — ordered commits within a single worktree merge, not one-file commits; phase (b)'s §3 test migrations land in the same merge.

### (a) INTEGRATE REDACTION FIRST

**Prerequisite (ordering dependency):** the credential-isolation fix (declared-keys-only worker env) is a PREREQUISITE for the redaction registry to be complete, and its consolidation merge must land BEFORE phase (a). `_worker_environment` (`supervisor.py:824-826`) today admits every canonical `CAMBIUM_PROVIDER_*` variable present in the host env via `is_provider_env_name` (`auth.py:191-193`) even when it is not declared in `provider_env_keys` (codified by `tests/scenarios/test_supervisor_fanout.py:739-764`); a value admitted this way bypasses a registry built from the declared keys alone.

Build **one session `Redactor`** with:

- default patterns (secrets, tokens, API keys — the F-02 pattern set), plus
- the **exact values of every env var `_worker_environment` can pass a worker** — after the isolation merge that is exactly the session's declared `provider_env_keys` values. The registry is complete only when those two sets coincide.

Redact the **COMPLETE event record** in `_Runtime.emit` (`supervisor.py:1291-1319`) before:

- the critical `EventStore.append` path,
- the non-critical queue, and
- `on_event` observers.

Add belt-and-braces **structured redaction inside `EventStore.append`** before JSON serialization (`store.py:368-380`, `783-815`) so no future caller can write an unredacted row.

**Proof requirement:** a worker/gate secret never reaches `events.db` — verified by raw SQLite row inspection and by observer records.

Preserve `_redacted_provider_metadata` as a **field allowlist**, not the general boundary: the Redactor does not replace the allowlist; it operates on the full record, and `_redacted_provider_metadata` controls which provider fields may be retained.

### (b) CANONICALIZE `supervisor.py` (WITHIN THE SERIALIZED (a)/(b)/(d) EFFORT)

Phases (a), (b), (d) all own `supervisor.py` and are ONE serialized effort — ordered commits within a single worktree merge, not one-file commits. This phase also includes the §3 test migrations in the same merge. No coexistence window.

1. **Rewrite `run_session`** as a thin one-task adapter over `run_plan`:
   - copy spec,
   - `scratch_repo` → `repo`,
   - `spec` → `task`,
   - `wall_budget_s` → `max_wall_s`,
   - preserve `worker` / `provider` / `gate` / path / timeouts / fanout,
   - `max_restarts=0`.
2. **Map `TaskResult` back to `SliceResult`** (keeps the public return shape).
3. **Rewire CLI `--task-spec`** and the built-in demo to a one-task plan via `_amain_plan`.
4. **Built-in default worker = `python -m cambium.worker`** — never `scripts/fake_worker.py`.
5. **Fold new-repo init** into `_ensure_repo_initialized`.
6. **Hard imports:** `CRITICAL_KINDS`, `EventStore` from `cambium.store`; `MergeConflictError`, `MergeSequencer`, `NonFastForwardError` from `cambium.merge`. Import failure fails at load (fail-loud).

**REWRITE:** the supervisor module docstring (`supervisor.py:1-24`) still claims it writes `events.jsonl`, uses `git merge --ff-only`, and is "the vertical-slice milestone"; rewrite it to describe the canonical `run_plan` / `EventStore` / `MergeSequencer` runtime (inventory row 17).

**DELETE** (exact file:line inventory in §2):

- `_FallbackEventStore` (`:930-1013`)
- `_FallbackSequencer` (`:1016-1158`)
- local merge exception duplicates (`:902-927`)
- resolver functions (`:1161-1174`)
- `_open_store` fallback branch (`:1177-1182`)
- `_make_sequencer` fallback raise (`:2254-2256`)
- local `CRITICAL_KINDS` (`:770-775`)
- `EventLog` (`:105-125`)
- `_validate_paths` (`:135-150`)
- `_next_message` (`:284-291`)
- module-level `_run_gate` (`:294-324`) and `_merge_branch` (`:327-383`)
- `_default_spec` (`:2577-2590`)
- `_load_task_spec` (`:2593-2599`)
- `_bootstrap_scratch` (`:2614-2630`)
- slice CLI mode (`:2661-2704`)

**KEEP:** `EventSink`, `make_request_id`, `SliceResult`, `_cfg_float`, `_write_json`, `_kill_worker`, `_kill_process_group_and_reap`, `_GateOutputOverflow`, `_communicate_gate_bounded`, `_strip_sensitive_env`, `_redacted_provider_metadata`, `_provider_env_keys`, `_worker_environment`, `_sh`, `_ensure_repo_initialized`, `_amain_plan`, `read_events`, `_Runtime`, `run_plan`, `TaskResult`/`PlanResult`, `_merge_task`/`reconcile`/`_flush_sequencer_events`.

**Preserve all post-0867572 hardening:** env stripping, worktree cleanup, quarantine, stdin deadlines.

### (c) DELETE `events.py` AND ORCHESTRATOR PLACEHOLDER

- Delete `src/cambium/events.py`.
- Delete `Orchestrator.submit` / `_queue` / `_next_task_id` and the no-work start/finish drain (`orchestrator.py:28-46`, `71-75`).
- Keep thin `Orchestrator.run(session_dir, plan)` forwarding to `run_plan`.
- `EventHandler` receives canonical redacted event dicts.

### (d) WIRE CANONICAL RESULT

**Retention boundary (prerequisite):** the worker envelope does not exist at the result-writing point today. `run_plan` keeps it only transiently in `_GenOutcome.envelope` (`supervisor.py:1245-1255`, `:2183-2188`); `_supervise` retains only `TaskResult` (no commits/files/diff/summary; `:1194-1204`, `:1731-1745`); and the persisted result event keeps only status + provider metadata (`:2082-2089`). Phase (d) adds an explicit retention boundary: `_Runtime` must retain the terminal correlated envelope — or its sanitized commits/files/diff/summary — from `_GenOutcome` until AFTER `runtime.shutdown`, e.g. a `_Runtime.last_envelope` field, redacted before retention. Without this, the commits/files assertions cannot match.

After `runtime.shutdown`, construct a `cambium.results.Result`:

- one task → the **sanitized envelope fields COMBINED with the authoritative supervisor verdict** (the worker's own status token alone is never authoritative);
- flat multi-task → an **aggregate status record** (no invented root).

**Verdict override:** the worker envelope is emitted BEFORE gate/merge processing (`supervisor.py:2074-2089`), so a successful worker can later fail its gate or merge and `TaskResult` becomes `failed` (`:1724-1745`) while `status_from_wire` would still map worker `status == "succeeded"` to `"done"` (`results.py:359-399`). The root `Result` must therefore compute its status from the supervisor verdict FIRST: gate exit code, merge outcome (`merge_sha` vs `merge_failed`), and timeout/rejection/cancellation signals OVERRIDE the worker's own status. Worker `"succeeded"` + failing gate → `status == "failed"`, `exit_code == 1`. Graceful cancellation → `status == "cancelled"`, `exit_code 4`. The envelope contributes only sanitized commits/files/diff/summary.

Timing: `started_at` before startup; `ended_at` after shutdown. `session id = str(session_dir.resolve())`.

Call `write_result` via `asyncio.to_thread` **before `run_plan` returns**. On graceful cancellation, write `status="cancelled"`, `exit_code 4`, then re-raise `CancelledError`.

**Result-write failure propagates** — never return a successful `PlanResult` without `result.json`.

**Acceptance:**

- `supervisor.py` changes land within the serialized (a)/(b)/(d) effort, not one-file commits,
- gate passed,
- `refs/heads/main` advanced via canonical sequencer,
- fsynced `merge_committed` in `events.db`,
- `.cambium/result.json` has exactly `ROOT_RESULT_KEYS`,
- worker success + failing gate → `result.json` `status == "failed"`, `exit_code == 1`,
- worker success + passing gate → `status == "done"`, `exit_code == 0`,
- commits/files match the retained (redacted) terminal envelope,
- no temp file,
- worker PID gone,
- only primary worktree remains + task branch deleted.

### (e) FREEZE SHA + AUDIT

1. Freeze the post-commit SHA.
2. Run the full scenario suite + `ruff` + structural greps:
   - single `EventStore` / `MergeSequencer` class,
   - no `EventLog` / `_Fallback*`,
   - no `events.py`,
   - no `os.environ` assignment in `src/`,
   - `doctor`.
3. Run the 3 audits (conformance / security / constitution) against that SHA, adding:
   - `docs/research/m1-conformance-report.md`,
   - `docs/research/m1-security-audit.md`,
   - `docs/research/m1-constitution-compliance.md`.
4. Final docs commit marks M1 done with the audited SHA.

---

## 2. Deletion inventory table

All line numbers reference `main@b709375` and are advisory (see §5).

### `src/cambium/supervisor.py` — phase (b)

| # | Item | Location | Disposition |
|---|---|---|---|
| 1 | `EventLog` (JSON-Lines slice log) | `:105-125` | DELETE |
| 2 | `_validate_paths` (slice path guard) | `:135-150` | DELETE |
| 3 | `_next_message` (slice-only message pump) | `:284-291` | DELETE |
| 4 | module-level `_run_gate` (slice gate runner) | `:294-324` | DELETE |
| 5 | `_merge_branch` (slice `git merge --ff-only`) | `:327-383` | DELETE |
| 6 | local `CRITICAL_KINDS` duplicate | `:770-775` | DELETE (import from `cambium.store`) |
| 7 | local merge exception duplicates (`MergeConflictError`/`NonFastForwardError`) | `:902-927` | DELETE (import from `cambium.merge`) |
| 8 | `_FallbackEventStore` | `:930-1013` | DELETE |
| 9 | `_FallbackSequencer` | `:1016-1158` | DELETE |
| 10 | resolver functions (`_resolve_event_store`/`_resolve_merge_sequencer`) | `:1161-1174` | DELETE |
| 11 | `_open_store` fallback branch | `:1177-1182` | DELETE |
| 12 | `_make_sequencer` fallback raise | `:2254-2256` | DELETE |
| 13 | `_default_spec` (default worker = fake_worker) | `:2577-2590` | DELETE (default = `python -m cambium.worker`) |
| 14 | `_load_task_spec` (slice CLI spec loader) | `:2593-2599` | DELETE |
| 15 | `_bootstrap_scratch` (slice repo bootstrap) | `:2614-2630` | DELETE (folded into `_ensure_repo_initialized`) |
| 16 | slice CLI mode (`--session-dir`/`--task-spec` body) | `:2661-2704` | DELETE (one-task plan via `_amain_plan`) |
| 17 | stale module docstring (claims `events.jsonl`, `git merge --ff-only`, "vertical-slice milestone") | `:1-24` | REWRITE (describe the canonical `run_plan` / `EventStore` / `MergeSequencer` runtime) |

**KEEP:** `EventSink`, `make_request_id`, `SliceResult`, `_cfg_float`, `_write_json`, `_kill_worker`, `_kill_process_group_and_reap`, `_GateOutputOverflow`, `_communicate_gate_bounded`, `_strip_sensitive_env`, `_redacted_provider_metadata`, `_provider_env_keys`, `_worker_environment`, `_sh`, `_ensure_repo_initialized`, `_amain_plan`, `read_events`, `_Runtime`, `run_plan`, `TaskResult`/`PlanResult`, `_merge_task`/`reconcile`/`_flush_sequencer_events`. Preserve all post-0867572 hardening (env stripping, worktree cleanup, quarantine, stdin deadlines).

### `src/cambium/orchestrator.py` + `src/cambium/events.py` — phase (c)

| # | Item | Location | Disposition |
|---|---|---|---|
| 18 | `Orchestrator.submit` / `_queue` / `_next_task_id` | `orchestrator.py:28-46` | DELETE |
| 19 | no-work start/finish drain | `orchestrator.py:71-75` | DELETE |
| 20 | thin `Orchestrator.run(session_dir, plan)` forwarding to `run_plan` | `orchestrator.py` | KEEP |
| 21 | `events.py` seed dataclasses + envelope | `events.py` (whole file) | DELETE |

`EventHandler` receives canonical redacted event dicts.

---

## 3. Test migration table

Exactly FOUR test files migrate in phase (b), all landing in the same merge as the supervisor rewrite: `test_vertical_slice.py`, `test_supervisor_hardening.py`, `test_worker_provider.py`, `test_conformance.py`. `test_supervisor_fanout.py` and `test_results.py` remain no-change (see "Keep working" below).

| Test file / range | Change |
|---|---|
| `test_vertical_slice.py` (all tests) | Drive the `run_session` adapter; read events via `read_events` (events.db); assert no `events.jsonl`; assert canonical exit codes + cleanup. |
| `test_supervisor_hardening.py:818-833` (oversized stdout line) | Slice-reader semantics: migrate to the adapter or delete. |
| `test_supervisor_hardening.py:836-892` (wrong ready rid) | Slice-reader semantics: migrate to the adapter or delete. |
| `test_supervisor_hardening.py:895-924` (missing proto) | Slice-reader semantics: migrate to the adapter or delete. |
| `test_worker_provider.py:312-345` (provider bridge) | Migrate to the adapter. |
| `test_worker_provider.py:348-360` (asserts `events.jsonl` absent) | Flip to `events.db`. |
| `test_conformance.py:203-210` (`supervisor.CRITICAL_KINDS`) | Point at `cambium.store.CRITICAL_KINDS`. |

**Keep working (no change):** `test_store`, `test_supervisor_fanout`, `test_merge`, `test_m6_staging`, `test_pipeline`, `test_worker_provider` non-slice, `test_ipc:790`, `test_tooling` events.db, `test_results` `event_log_ref` sqlite, `test_bench`.

---

## 4. Defer items

| Item | Location | Why deferred |
|---|---|---|
| `_Runtime._writer_loop` error-swallow | `supervisor.py:1337-1338` | Bounded backpressure + fatal-store handling is M4; flag in the security audit, do not fix in M1. |
| Multi-task root aggregation policy detail | — | Before M5, write an aggregate status record only; no invented root. Full root policy lands with Architectus (M5). |

---

## 5. Provenance note

Line numbers reference `main@b709375` (2026-08-10) and are **advisory**; they will drift as the consolidation/packaging/tool-loop merges land.

The execution **ORDER** is normative:

1. redaction first (phase a),
2. then supervisor canonicalization (phase b, including its §3 test migrations),
3. then `events.py`/orchestrator (phase c),
4. then result wiring (phase d),
5. then audits (phase e).

Phases (a)–(e) are strictly ordered. Phases (a), (b), and (d) all own `supervisor.py` and are ONE serialized effort — ordered commits within a single worktree merge, not one-file commits; phase (c) is its own change; phase (e) is three independent audit agents plus one docs commit on the frozen SHA.
