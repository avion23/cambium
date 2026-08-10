# Conformance Report — Merged Implementation vs Normative Specs

**Date:** 2026-08-09
**Auditor:** wt-audit-conformance (read-only audit)
**Baseline audited:** `/home/ubuntu/cambium` main `3d27ba3` ("merge: feat(store) — sqlite wal event store")
**Scope:** `src/cambium/{store,merge,ipc,worker,tasktree,events,supervisor,orchestrator,doctor}.py`, `tests/scenarios/*.py` vs `docs/architecture/architecture.md` §5/§6/§7.8, `docs/research/{event-schema-draft,ipc-protocol-draft,sqlite-wal-durability,worktree-concurrency}.md`.

**Verification harness at audit time:** `uv run pytest tests/ -q` → `38 passed`.

**Current-main status (2026-08-09):** main is now `6109a6a`; the current
verification is `uv run --python 3.14.7 --extra test pytest --collect-only -q`
→ 108 collected and `pytest -q` → 108 passed. M3 is **FALSE as a current N-A**:
`ipc.py` and `worker.py` are re-auditable after merge `38e1d43`. M1 is
**RESOLVED** by the recovery-gap specification fix `dcc1bbb`. M2 remains
**IN-FLIGHT** on `wt-impl-super`, which is wiring `EventStore` and
`MergeSequencer` into the canonical runtime.

---

## 0. Executive finding

The two components that were merged on main — `store.py` (SQLite WAL event store) and `merge.py` (Unio sequencer) — **conform at the module level** to their normative mechanics: the fsync-before-ack critical path, single-writer invariant, `update-ref` expected-old publish, quarantine refusal, and staging-ref capture all match the architecture text, and every F-number/experiment cited in `merge.py` exists and matches in `worktree-concurrency.md`.

The **dominant** conformance problem is *integration*, not *component drift*:

1. **No production caller.** Nothing on main imports `cambium.store` or `cambium.merge` (grep of `src/`, `tests/`, `scripts/` confirms). `supervisor.py` is still the historical slice module: it writes its own JSON-Lines `EventLog` (`supervisor.py:67-87`) and merges via `git merge --ff-only` (`supervisor.py:161-178`). The §6.2 subscriber/ring-buffer/replay path and the §7.8 `merge_committed`/`merge_reconciled` event flow therefore have **no caller on main**; this is M2 in-flight on `wt-impl-super`.
2. **The IPC/worker N-A finding is closed.** `ipc.py`/`worker.py` are in main
   via `38e1d43`, so Check 4 is re-auditable. The remaining dominant issue is
   production integration of store and merge through `wt-impl-super`.

---

## 1. `store.py` vs architecture §6.5 (durability contract)

### 1.1 Critical-kind set — CONFORM (with one naming drift, LOW)

`store.py:42-45`:
```python
CRITICAL_KINDS = frozenset({
    "result", "checkpoint", "worker_exit", "task_failed",
    "merge_progress", "task_assigned", "merge_committed",
})
```
Architecture §6.5 (`architecture.md:500`): critical = `submitted` (v2.1 name: `task_assigned`), `result`, `checkpoint`, `worker_exit`, `task_failed`, `merge_progress`, `merge_committed`. The event-schema-draft catalog uses `submitted` (kind #1, `event-schema-draft.md:78`).

The set matches the task's expected set exactly. Drift: the code uses the **v2.1 alias** `task_assigned` where the arch's *current* table name is `submitted`. The arch itself annotates the alias (`architecture.md:500`), so this is an early adoption of the v2.1 name, not a semantic break. **(L8)**.

### 1.2 fsync-before-ack — CONFORM

The producer blocks on a completion event for critical kinds: `store.py:145-148` (`if kind in CRITICAL_KINDS: pending.event.wait()`). The writer calls `_fsync_now()` **before** signalling: `store.py:228-231` (checkpoint+fsync) precedes `pending.event.set()` at `store.py:232`. `_fsync_now` (`store.py:267-272`) is `PRAGMA wal_checkpoint(TRUNCATE)` + `os.fsync(wal_fd)` + `os.fsync(db_fd)`, byte-for-byte the arch §6.5 mechanism (`architecture.md:505-511`) and the empirically validated incantation (`sqlite-wal-durability.md:175-184`).

Non-critical events are NOT fsync'd before ack (`store.py:232` fires immediately after `INSERT`), matching §6.5's "at most `fsync_interval_s`" window.

### 1.3 Single writer — CONFORM

The write connection is created **inside the writer thread** (`store.py:177-187`), exactly what `sqlite-wal-durability.md:265-273` proves is required (per-thread connections). Readers use short-lived connections (`events_after`, `store.py:151-158`). `_next_seq` is seeded from `MAX(seq)+1` on startup (`store.py:189-191`). The queue is drained FIFO by a single consumer (`store.py:210-233`), preserving insert order = reservation order.

### 1.4 Recovery semantics / `recovery_gap` — RESOLVED (M1 specification fix)

**Status update:** the original audit recorded a gap against the earlier
architecture text. `dcc1bbb` folded the no-gap-by-construction and phantom-read
semantics into the authoritative specification; the historical code analysis
below remains evidence, not a current conformance failure.

Architecture §6.5 (`architecture.md:520`): "Non-critical events missing from the tail are detected by a gap in `seq` … the writer emits a `recovery_gap` event documenting the lost range." The event-schema-draft carries `recovery_gap` as a **critical** kind (§3.16, `event-schema-draft.md:336-342`; replay step 3 at §4.4).

`store.py:13-16` declares the mechanism **superseded**:
```
- **No ``recovery_gap`` gaps.** ``seq`` is reserved at enqueue and the sole
  writer commits in reservation order, so gaps cannot occur by construction;
  the architecture.md §6.5 ``recovery_gap`` mechanism is superseded.
```
The store never emits `recovery_gap` and performs no gap detection. The code's justification is defensible only for *committed* rows: seq is reserved at enqueue (`store.py:142-144`) and non-critical events return immediately; a crash between enqueue and INSERT leaves a reserved seq that is never materialized, so `MAX(seq)+1` reseeding (`store.py:189-191`) reuses the hole — no visible gap, but the caller who received that seq has a **phantom read**, which `store.py:17-19` documents. This is a real divergence from the §6.5 replay contract and from the draft's `recovery_gap` kind. Owner: **fix spec** (architecture fold — §6.5 wording must be reconciled with the no-gap-by-construction + phantom-read design) or **fix code** (implement tail-gap detection). **(M1)**

### 1.5 Other §6.5 / §6.3 DDL drifts — GAP (LOW)

- **Bounded queue:** §6.2 inv. 2 (`architecture.md:449`) mandates a bounded queue (10 000) that drops oldest non-critical + `drop` marker. `store.py:20-22` documents an **unbounded** queue ("Bounded-with-backpressure is a v2.1 option"). Documented deviation. **(M-queue, folded into M1 set of spec deviations)**.
- **Missing `snapshots` table** — §6.1/§6.3 (`architecture.md:422`, `471-475`) defines a `snapshots` table for compaction; the store schema (`store.py:47-57`) has only `events`. **(L4)**
- **Missing indexes** — §6.3 DDL defines `events_task_idx`, `events_kind_idx` (`architecture.md:468-469`); the store schema has no indexes. **(L3)**
- **`ts` typed TEXT, nullable** — §6.3 `wall_ts REAL NOT NULL` (`architecture.md:461`), draft `ts` float (`event-schema-draft.md:35`); the store declares `ts TEXT` (`store.py:51`) and coerces to `str()` on append (`store.py:129`). **(L1)**
- **`monotonic_ms` nullable** — §6.3 `INTEGER NOT NULL` (`architecture.md:460`); schema declares nullable (`store.py:53`). **(L2)**
- **Checkpoint `busy` return ignored** — `sqlite-wal-durability.md:182-184` warns: "if a checkpoint returns `busy` the call returns without flushing and the writer would ack a critical event as durable. Recommend checking the return value." `store.py:268-269` calls `fetchone()` and **discards** the row. **(L5)**

**Verdict 1: CONFORM on the durability mechanism (fsync-before-ack, single writer, fsync target); GAP on recovery semantics (recovery_gap superseded) and §6.3 DDL fidelity.**

---

## 2. `store.py` vs event-schema-draft envelope

### 2.1 Envelope field names — CONFORM for the audited set

Draft canonical envelope (`event-schema-draft.md:30-41`): `event_id, kind, seq, ts, monotonic_ms, task_id, worker_id, request_id, generation, payload`.

Store schema columns (`store.py:47-57`): `seq, kind, payload, ts, monotonic_ms, task_id, worker_id, generation, request_id` — i.e. **every field the audit task lists** (`kind/payload/ts/monotonic_ms/task_id/worker_id/generation/request_id`) is present with the exact draft name, plus `seq`. Field names do not drift. `_row_to_event` returns the same names (`store.py:275-290`), and `test_store.py:46-61` round-trips them.

### 2.2 Drifts — GAP (LOW)

- **`event_id` absent.** Draft D2 adds `event_id` (ULID) as a correlation key (`event-schema-draft.md:32`, `555`). The store has no such column. This is a *draft-only* addition (arch has `seq` only, `architecture.md:459`), so the store follows the arch, not the draft. The draft was written before the code; the code chose the arch's column set. Flag as low drift. **(L7)**
- **`ts` type drift.** Draft `ts` is `float (epoch seconds)` (`event-schema-draft.md:35`); the store stores TEXT (see §1.5 L1). **(L1, already counted).**
- **`worker_id` derivation not implemented.** Draft D3 derives `worker_id = "{task_id}#{generation}"` (`event-schema-draft.md:38`, `556`). The store treats `worker_id` as an opaque passthrough — nothing on main constructs the `task#generation` form (`test_store.py:31` supplies it manually). **(M5, counted in §6.)**

**Verdict 2: envelope field names CONFORM; drifts are `event_id` (draft-only) and the `ts` type.**

---

## 3. `merge.py` vs §7.8 + worktree-concurrency F-numbers

### 3.1 Publish mechanics — CONFORM

- **expected-old publish:** `publish_merge` passes the old SHA to `git update-ref refs/heads/main <new> <old>` (`merge.py:457-459`), matching §7.8 step 2 (`architecture.md:737`).
- **fast-forward enforced:** `_is_ancestor` (`merge.py:360-368`) is checked **before** the atomic ref update (`merge.py:450-455`), matching §7.8 step 1 (`architecture.md:729-731`). A rewind or sideways publish is refused even when the old-value check alone would pass (`test_merge.py:327-353`).
- **empty/zero `expected_old` backdoor closed:** `merge.py:434-443` rejects `""`/`ZERO_SHA`, which git would otherwise read as "ref must not exist" and silently *create* `main` (`test_merge.py:307-324`). Documented extension beyond §7.8 (which assumes `main` exists) and consistent with the arch's first-publish gap.
- **`create_main`** (`merge.py:370-405`) is the race-safe first-publish path via the empty-old "must not exist" primitive — a documented extension, not a §7.8 violation.
- **quarantine refusal:** `_check_quarantine` (`merge.py:344-354`) + `_git_env` strips `GIT_QUARANTINE_PATH` (`merge.py:174-177`) + result-string defense (`merge.py:464-467`). Finding F5.
- **staging ref captured before worktree removal:** `update-ref refs/cambium/staging/<id>` runs at `merge.py:339-341`, before any `worktree remove`. Finding F11/Exp 4.
- **staging branch in the throwaway worktree:** `prepare_staging` copies the worker tip to a local staging branch and rebases there (`merge.py:296-321`), never rebasing the worker branch in place. Finding F18/Exp 6a.
- **cleanup:** `cleanup_staging` (`merge.py:494-531`) removes the sequencer-owned worktree, staging branch, and staging ref; never `--force`-removes an unknown path.
- **reconcile hook:** `reconcile` (`merge.py:482-492`) returns the current `refs/heads/main` SHA (or None) so the caller can close the ref-advance/event crash gap (§7.8, `architecture.md:752`).

### 3.2 Cited F-numbers and experiments — all exist and match

| merge.py citation | claim | research doc | match |
|---|---|---|---|
| `merge.py:176` `_git_env` "(finding F5)" | quarantine-free env | `worktree-concurrency.md:321` F5 "update-ref under GIT_QUARANTINE fails … Exp 2e" | ✓ |
| `merge.py:347` "(finding F5)" | quarantine refusal | F5 (Exp 2e, `worktree-concurrency.md:146-154`) | ✓ |
| `merge.py:12` "Experiment 4: … dangling" | staging SHA before removal | Exp 4, `worktree-concurrency.md:211-224` | ✓ |
| `merge.py:340` "(dangling-commit finding F11)" | capture-before-remove | `worktree-concurrency.md:327` F11 | ✓ |
| `merge.py:18-20` "Experiment 6a: … already used by worktree" | no in-place rebase | Exp 6a, `worktree-concurrency.md:274-285` | ✓ |
| `merge.py:317` "(finding F18: it cannot be rebased in place)" | staging branch | `worktree-concurrency.md:334` F18 | ✓ |
| `merge.py:22` "Experiment 1b poison" | ref-only publish vs staged poison | Exp 1b, `worktree-concurrency.md:40-76`; test `test_merge.py:167-191` | ✓ |
| `merge.py:16` "Experiment 2e: update-ref fails under quarantine" | quarantine | `worktree-concurrency.md:146-154` | ✓ |

All eight citations resolve to real findings with matching claims. **CONFORM on citations.**

### 3.3 The §7.8 event flow is not wired — GAP (MEDIUM)

§7.8 step 3 requires `publish_merge` to emit the critical `merge_committed` event **before returning** (`architecture.md:739-744`), and §7.8's crash story requires a `merge_reconciled` event on recovery (`architecture.md:752`). `merge.py` emits **no events** — it is a pure-git library. Its own `reconcile` docstring says "the caller compares the returned SHA to its last durable `merge_committed` event and appends a `merge_reconciled` event" (`merge.py:485-487`). The arch's `Unio` lock (held across verify + publish, `architecture.md:712`, `724`, `755`) is likewise absent — the class holds only per-instance staging bookkeeping (`merge.py:158-164`).

On main there is **no caller**: the supervisor still publishes via `git merge --ff-only` (`supervisor.py:163`). So `merge_committed`, `merge_reconciled`, and the single-writer lock are specified-but-unimplemented in the merged tree. Owner: **fix code** (implementation wave: wire `MergeSequencer` into the supervisor; emit `merge_committed`/`merge_reconciled` into the store; hold the lock across verify+publish). **(M2 partial)**

**Verdict 3: component mechanics and all F-number citations CONFORM; §7.8's event-emission + single-writer-lock integration is absent on main.**

---

## 4. `ipc.py` + `worker.py` vs §5 + ipc-protocol-draft — RE-AUDITABLE (M3 resolved)

`src/cambium/ipc.py` and `src/cambium/worker.py` are in main via `38e1d43`
(`merge: feat(ipc) — nuntius framing + worker runtime`). The §5 framing,
message catalogue, request correlation, worker deadlines, and result envelope
are now re-auditable in the merged tree. The original branch-only N-A verdict
is retained as historical evidence, not a current verdict.

The remaining conformance question is integration into canonical Custos, tracked
as M2 on `wt-impl-super`; M3 is no longer an absence-of-file finding.

*Historical annex:* the original `ad372ae` branch implemented
`MAX_LINE_BYTES = 1_048_576`, newline resync, a 64 KiB diff cap, a 2,000-character
summary cap, and `status ∈ {succeeded, failed, cancelled}`. Those observations
remain valid and are now confirmed against the merged files.

**Supervisor slice (the only IPC code on main) — documented drifts (LOW):**
- Framing: `WORKER_STDIN_LIMIT = 1_048_576` (`supervisor.py:43`) matches the 1 MiB cap, but the historical slice reads via `async for raw in proc.stdout` with no resync/`line_too_long` handling; the canonical `ipc.py` path now supplies that behavior.
- Message names: the slice uses `result_envelope`/`exit_message` (draft names) where arch §5.2 uses `result`/`exit` (`architecture.md:350`, `372`) — documented drift (`vertical-slice-report.md` #2).
- **Status vocabulary drift:** slice `status ∈ {succeeded, failed}` vs arch `Result.status ∈ {done, failed, rejected, timeout, cancelled}` (`architecture.md:167-172`; draft §3 wire vocabulary is `succeeded/failed/timeout/cancelled`). The draft's wire set matches the slice; the arch's `done` does not. Flagged as vocabulary drift (`event-schema-draft.md:204-211` seam 2). **(L6)**
- **`exit_message` carries no `request_id` — CONFORM.** Supervisor reads `exit_message.reason` (`supervisor.py:310-312`) and `test_vertical_slice.py:204` asserts `"request_id" not in by_kind["exit"]`, matching arch §5.2's `exit` (no request_id, `architecture.md:372-376`) and the IPC draft's reconciliation #7 (echo is a draft extension).

**Verdict 4: M3 is re-auditable and conforms at the framing/worker-module level; canonical supervisor integration remains M2 in-flight. The historical slice still has documented message-name and status-vocabulary drift.**

---

## 5. `events.py` seed dataclasses — GAP (MEDIUM): orphaned

`events.py` defines `Event`, `WorkerStarted`, `WorkerFinished`, `LogEvent` (`events.py:14-47`). Its **only importer** is `orchestrator.py:15` (`from .events import Event, WorkerFinished, WorkerStarted`), used at `orchestrator.py:58-59`.

`orchestrator.py` is itself imported by **nothing** in `src/`, `tests/`, or `scripts/` (grep confirms; no test references it). The real event producers do not use the seed at all:
- `supervisor.py` writes plain dicts via its own `EventLog` (`supervisor.py:67-87`);
- `store.py` writes plain dicts (`store.py:123-135`);
- `merge.py`, `doctor.py` never import `events.py`.

The seed's field names (`type`, `timestamp`) also contradict the store envelope (`kind`, `ts`) that the event-schema-draft §2 treats as canonical, and the draft's own Appendix A mapping (`event-schema-draft.md:580-597`) is already stale relative to the store's schema. **`events.py` is dead code in the merged tree.** Owner: **fix code** (delete or wire into the store envelope) — the draft's "normative for field naming" claim must be updated either way. **(M4)**

**Verdict 5: orphaned; nothing on the runtime path consumes the seed dataclasses.**

---

## 6. Cross-module: seq / worker_id / generation — GAP (LOW/MEDIUM)

- `store.py` semantics are self-consistent and test-covered: seq reserved at enqueue (`store.py:142-144`), single-writer commit order, `MAX(seq)+1` reseed on restart (`store.py:189-191`), `events_after(seq)` for replay (`store.py:151-158`).
- **`worker_id` derivation (draft D3: `"{task_id}#{generation}"`, `event-schema-draft.md:38`) is not implemented anywhere on main.** The store is an opaque passthrough; no producer constructs the compound identity. **(M5)**
- **No end-to-end producer exists.** On main, the supervisor's slice `EventLog` records `{kind, timestamp, payload}` (`supervisor.py:82`) with **no** `seq`, `worker_id`, `generation`, or `request_id` at the envelope level (`vertical-slice-report.md` #8 documents this). Nothing writes into `EventStore` from production code — only tests and `doctor.py`'s read-only integrity check (`doctor.py:139-165`). So cross-module seq/worker_id/generation consistency between `store.py` and any worker payload **cannot be exercised on main** and is N-A / integration-pending. **(M2 partial)**

**Verdict 6: store-internal semantics consistent and tested; the cross-module contract is unverified because no production producer wires into the store on main.**

---

## 7. Gap table

| # | Severity | Location | Gap | Recommended fix | Owner |
|---|---|---|---|---|---|
| M1 | MEDIUM | `store.py:13-16` vs `architecture.md:520`, `event-schema-draft.md:336-342` | `recovery_gap` mechanism superseded; no tail-gap detection/event; phantom-read seq on crash | Reconcile §6.5 wording with the no-gap-by-construction design **or** implement tail-gap detection | fix spec (architecture fold) |
| M2 | MEDIUM | `supervisor.py:67-87,161-178`; `merge.py:482-492` | No production wiring: supervisor never uses `EventStore` or `MergeSequencer`; §6.2 subscriber/ring-buffer/replay and §7.8 `merge_committed`/`merge_reconciled` + single-writer lock have no caller | Wire store + sequencer into the supervisor; emit merge events; hold lock across verify+publish | fix code (implementation wave) |
| M3 | RESOLVED | `src/cambium/{ipc,worker}.py` via `38e1d43` | Nuntius framing and worker runtime are present and re-auditable | Keep the module scenarios green; address canonical Custos integration under M2 | merged code / M2 integration |
| M4 | MEDIUM | `events.py`; `orchestrator.py:15,58-59` | Seed dataclasses orphaned (only importer is the unused `orchestrator.py`); field names contradict store envelope | Delete or wire into the store envelope; update draft Appendix A | fix code (implementation wave) |
| M5 | LOW/MED | `store.py:53-55`; draft D3 | `worker_id` derived identity `task#generation` not implemented; opaque passthrough only | Implement derivation in the producer | fix code (implementation wave) |
| L1 | LOW | `store.py:51,129` vs `architecture.md:461` | `ts` typed TEXT / str-coerced, nullable vs `wall_ts REAL NOT NULL` / draft float | Align column type | fix code (implementation wave) |
| L2 | LOW | `store.py:53` vs `architecture.md:460` | `monotonic_ms` nullable vs `NOT NULL` | Align DDL | fix code (implementation wave) |
| L3 | LOW | `store.py:47-57` vs `architecture.md:468-469` | Missing `events_task_idx` / `events_kind_idx` | Add indexes | fix code (implementation wave) |
| L4 | LOW | `store.py:47-57` vs `architecture.md:471-475` | Missing `snapshots` table (§6.1/§6.3) | Add snapshots or scope it out in the spec | fix code / fix spec |
| L5 | LOW | `store.py:268-269` vs `sqlite-wal-durability.md:182-184` | Checkpoint `busy` return row discarded → could ack critical event as durable on a busy checkpoint | Check `(busy, log, ckpt)` return; retry on busy | fix code (implementation wave) |
| L6 | LOW | `supervisor.py:354-361` vs `architecture.md:167-172` | Status vocabulary `succeeded` vs arch `done` (draft uses `succeeded`; seam 2) | Adopt draft wire vocabulary in §3.4/§5.2, or rename slice statuses | fix spec (architecture fold) |
| L7 | LOW | `store.py:47-57` vs `event-schema-draft.md:32` (D2) | `event_id` (ULID) absent (draft-only; arch has `seq` only) | Adopt draft D2 or drop from draft | fix spec (architecture fold) |
| L8 | LOW | `store.py:42-45` vs `architecture.md:500` | `CRITICAL_KINDS` uses v2.1 name `task_assigned`; arch's current name is `submitted` | Canonicalize the kind name in §6.5 (adopt v2.1) | fix spec (architecture fold) |

**Remaining N-A items:** §6.2 subscriber/ring-buffer integration; end-to-end
store↔worker seq/worker_id/generation until `wt-impl-super` wires the canonical
runtime. Check 4 is no longer N-A.

---

## 8. Summary

- **Verdicts:** Check 1 CONFORM-with-gaps (fsync/single-writer conform; DDL drift) · Check 2 CONFORM-with-gaps (envelope names conform; `event_id`/`ts` drift) · Check 3 CONFORM-with-gaps (mechanics + all F-citations conform; event flow unwired) · Check 4 **RE-AUDITABLE** (merged) · Check 5 GAP (orphaned) · Check 6 GAP/N-A (unwired cross-module contract).
- **Gap counts:** 5 MEDIUM (M1–M5) + 8 LOW (L1–L8) = **13**; 0 HIGH. 5 "fix code (implementation wave)", 5 "fix spec (architecture fold)", 1 either (L4), M1 split.
- **Top 3 gaps:**
  1. **M2** — No production wiring: `supervisor.py` (slice) bypasses both `EventStore` and `MergeSequencer`; §6.2 subscriber/replay and §7.8 `merge_committed`/`merge_reconciled` event flow are unimplemented on main.
  2. **M2** — canonical supervisor wiring: `wt-impl-super` must connect `EventStore` and `MergeSequencer`; this is in-flight.
  3. **M1** — resolved by `dcc1bbb`, which canonicalized the no-gap-by-construction + phantom-read specification.
- **Not yet checkable:** canonical supervisor↔store/merge integration and
  end-to-end worker payload identity remain M2 integration work; §5 IPC is
  present and independently re-auditable.

## 9. Evidence index

- Specs: `architecture.md:449` (bounded queue), `:458-476` (§6.3 DDL), `:500` (critical set), `:505-511` (fsync mechanism), `:520` (recovery_gap), `:708-757` (§7.8), `:167-172` (Result.status), `:372-376` (exit no-rid).
- Research: `sqlite-wal-durability.md:26-32,175-184` (fsync correctness + busy warning), `worktree-concurrency.md:40-76,146-154,211-224,274-285,321,327,334` (F-numbers/experiments), `event-schema-draft.md:30-41,78,336-342,555-556,580-597`.
- Code: `store.py:13-25,42-57,123-158,177-191,210-233,267-272`; `merge.py:158-164,174-177,296-321,339-341,344-354,370-405,450-459,482-531`; `supervisor.py:43,67-87,161-178,310-312,354-361`; `events.py:14-47`; `orchestrator.py:15,58-59`; `doctor.py:139-165`.
- Tests: current main collects 108 scenario tests and reports 108 passed; the
  original 38-test audit run is retained as the historical baseline.
