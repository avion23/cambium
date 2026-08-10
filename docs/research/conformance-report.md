# Conformance Report — Merged Implementation vs Normative Specs

**Date:** 2026-08-09
**Auditor:** wt-audit-conformance (read-only)
**Baseline audited:** `/home/ubuntu/cambium` `main@3d27ba3`, “merge: feat(store) — sqlite wal event store”.
**Scope:** `src/cambium/{store,merge,ipc,worker,tasktree,events,supervisor,orchestrator,doctor}.py`
and `tests/scenarios/*.py` against `docs/architecture/architecture.md` §§5–7.8 and the
event/IPC/durability/worktree research drafts.

**Historical snapshot / current pointer:** the audit harness was `uv run pytest tests/ -q` →
38 passed. A later snapshot recorded `main@b709375`, 647 passed/4 skipped. Those counts and
SHAs are historical anchors, not current-main claims. Read current behavior from
`docs/architecture/architecture.md`, `src/cambium/`, and `docs/research/v2-1-status.md`.
Current notes: provider loop, Diffundo, EventStore, and root `Result` exist; DLQ, eval cache,
ResourceBudget, `worker_pool`, and `events` are absent; there is no per-worker sandbox or
production shell approval, and no dynamic hierarchy.

## 0. Executive finding

`store.py` and `merge.py` conform at module level on fsync-before-ack, single-writer behavior,
expected-old `update-ref`, quarantine refusal, staging-ref capture, and the cited F-numbers.
The dominant failure is integration: the historical supervisor writes JSONL and uses
`git merge --ff-only`, so it does not call `EventStore`/`MergeSequencer`, emit
`merge_committed`/`merge_reconciled`, or provide the §6.2 replay path. The original IPC/worker
N-A finding is closed by `38e1d43`; canonical supervisor wiring remains M2 evidence.

## 1. Checks and verdicts

| Check | Verdict | Evidence and retained finding |
|---|---|---|
| 1. `store.py` vs architecture §6.5/§6.3 | **CONFORM with gaps** | Critical kinds match except the documented `submitted`/`task_assigned` alias (L8). Producer waits for critical events; writer checkpoints and fsyncs WAL/DB before ack (`store.py:145–148,228–232,267–272`; `sqlite-wal-durability.md:175–184`). One writer owns the connection (`store.py:177–191,210–233`). `recovery_gap` is superseded by no-gap-by-construction plus phantom-read semantics (`dcc1bbb`; M1). Queue, DDL, type, index, snapshot, and checkpoint-busy drifts are L1–L5/M-queue. |
| 2. Store vs event-schema envelope | **CONFORM with gaps** | Audited field names round-trip (`event-schema-draft.md:30–41`; `store.py:47–57,275–290`). `event_id` is draft-only (L7), `ts` is TEXT (L1), and producer-side `worker_id = task_id#generation` is absent (M5). |
| 3. `merge.py` vs §7.8 | **CONFORM component; GAP integration** | Expected-old publish, FF check, empty-old guard/`create_main`, quarantine defense, staging capture, throwaway branch, cleanup, and reconcile are implemented (`merge.py:296–321,339–354,360–459,482–531`). F5/F11/F18 and Experiments 2e/4/6a match `worktree-concurrency.md`. Events and Unio's cross-verify/publish lock are not wired (M2). |
| 4. `ipc.py` + `worker.py` vs §5 | **RE-AUDITABLE** | `38e1d43` merged framing, 1 MiB cap/resync, request correlation, deadlines, result/exit vocabulary, and 64 KiB diff. The old `ad372ae` branch remains historical. Slice-only message/status drift is L6; Custos integration is M2. |
| 5. Historical `events.py` seed | **GAP (M4)** | Snapshot dataclasses were imported only by unused `orchestrator.py`; production writes used supervisor JSONL or store dicts. `type/timestamp` conflicted with canonical `kind/ts`; both seed files are absent now. |
| 6. seq/worker/generation cross-module | **GAP/N-A (M2/M5)** | Store-internal reservation/replay is consistent and tested, but no production producer wires envelope identity into it. |

## 2. Retained mechanics and drifts

### Store

The critical set is `result`, `checkpoint`, `worker_exit`, `task_failed`, `merge_progress`,
`task_assigned`, `merge_committed` (`store.py:42–45`), with the architecture's `submitted`
alias documented at `architecture.md:500` (L8). The writer creates its connection in the writer
thread, drains FIFO, and readers use short-lived connections. The historical recovery analysis
recorded that a reserved-but-not-inserted non-critical event can be a phantom read; `dcc1bbb`
folded the no-gap semantics into the specification (M1), so the old “recovery_gap” finding is
evidence, not a current code verdict.

The remaining DDL differences are: unbounded queue versus §6.2's 10,000/drop policy
(M-queue); `ts TEXT`/nullable versus `wall_ts REAL NOT NULL` (L1); nullable `monotonic_ms`
(L2); missing `events_task_idx`/`events_kind_idx` (L3); missing `snapshots` (L4); and discarded
SQLite checkpoint `busy` status (L5). Draft `event_id`/ULID is L7. These are specification or
implementation work, not evidence that fsync-before-ack is broken.

### Merge and runtime boundary

`publish_merge` passes the expected old SHA to `git update-ref`, checks ancestry first, refuses
empty/zero old values, strips/rejects quarantine, captures staging refs before worktree removal,
and exposes `reconcile`. `merge.py` emits no events and holds no global lock; the supervisor's
historical `git merge --ff-only` path is the real integration gap (M2). The same gap leaves
seq/worker/generation consistency unexercised (M5).

### IPC, worker, and event seed

The canonical files now provide framing and worker behavior; the slice still has
`result_envelope`/`exit_message` and `succeeded`/`failed` vocabulary where architecture uses
`result`/`exit` and includes `done` (L6). Historical `events.py` and `orchestrator.py` were orphaned (M4) and are absent now.

## 3. Gap register

| ID | Severity | Evidence | Finding and action |
|---|---|---|---|
| **M1** | MEDIUM | `store.py:13–19`; `architecture.md:520`; `event-schema-draft.md:336–342`; `dcc1bbb` | `recovery_gap` is superseded; reconcile the spec (done in the historical fold) or implement tail-gap detection. |
| **M2** | MEDIUM | `supervisor.py:67–87,161–178`; `merge.py:482–492` | No production `EventStore`/`MergeSequencer` caller; wire the canonical runtime, merge events, replay, and single-writer lock. |
| **M3** | RESOLVED | `src/cambium/{ipc,worker}.py` via `38e1d43`; old `ad372ae` | Nuntius framing/Opifex worker are present and re-auditable; retain the branch history and finish Custos integration under M2. |
| **M4** | MEDIUM | Historical `events.py:14–47`; `orchestrator.py:15,58–59` | Seed dataclasses were orphaned and disagreed with store envelope; delete or wire them, then update draft Appendix A. Both are absent now. |
| **M5** | LOW/MEDIUM | `store.py:53–55`; `event-schema-draft.md:38,556` | `worker_id` derivation is not implemented; construct `task_id#generation` in the producer. |
| **L1** | LOW | `store.py:51,129`; `architecture.md:461`; draft `ts` | Align `ts` type and non-null wall timestamp. |
| **L2** | LOW | `store.py:53`; `architecture.md:460` | Make `monotonic_ms` `NOT NULL`, or revise the spec. |
| **L3** | LOW | `store.py:47–57`; `architecture.md:468–469` | Add task/kind indexes. |
| **L4** | LOW | `store.py:47–57`; `architecture.md:471–475` | Add snapshots or scope them out. |
| **L5** | LOW | `store.py:268–269`; `sqlite-wal-durability.md:182–184` | Check checkpoint `busy` before acknowledging critical durability. |
| **L6** | LOW | `supervisor.py:354–361`; `architecture.md:167–172`; draft `204–211` | Canonicalize `succeeded`/`done` status vocabulary. |
| **L7** | LOW | `store.py:47–57`; draft D2 `event_id` | Adopt or remove the draft-only ULID field. |
| **L8** | LOW | `store.py:42–45`; `architecture.md:500` | Canonicalize `task_assigned` versus `submitted`. |

Remaining N-A items at the snapshot were §6.2 subscriber/ring-buffer wiring and the
store↔worker identity contract. Check 4 is no longer N-A.

## 4. Summary and evidence index

Verdicts: Check 1 conform-with-gaps; Check 2 conform-with-gaps; Check 3 conform-with-gaps;
Check 4 re-auditable; Check 5 GAP; Check 6 GAP/N-A. Counts were 5 MEDIUM (M1–M5) and 8 LOW
(L1–L8), with no HIGH. The original top gap was M2 production wiring; M1's specification
reconciliation is recorded as resolved.

Primary pointers retained: architecture §§5.1, 6.2–6.5, 7.8; `event-schema-draft.md:30–41,78,336–342,555–556,580–597`; `ipc-protocol-draft.md`; `sqlite-wal-durability.md:26–32,175–184,265–273`; `worktree-concurrency.md:40–76,146–154,211–224,274–285,321,327,334`;
`store.py:13–25,42–57,123–158,177–191,210–233,267–272`; `merge.py:158–177,296–321,339–354,370–405,450–459,482–531`; `supervisor.py:43,67–87,161–178,310–312,354–361`;
`doctor.py:139–165`; and `tests/scenarios/` store/merge/IPC coverage.

## 5. Historical evidence detail

### Store append and recovery

At the audited SHA, `append` reserved `seq` under a lock (`store.py:142–144`) and returned
immediately for non-critical events. Critical kinds waited on a completion event
(`store.py:145–148`). The writer opened SQLite in its own thread, initialized `_next_seq` from
`MAX(seq)+1` (`store.py:177–191`), drained the queue FIFO, inserted the row, and ran
`PRAGMA wal_checkpoint(TRUNCATE)` plus `os.fsync` on WAL and DB before setting the completion
event (`store.py:210–233,267–272`). Readers used short-lived connections in `events_after`
(`store.py:151–158`). The original report treated a crash between reservation and INSERT as a
real phantom read and compared that behavior with the draft's `recovery_gap`; `dcc1bbb` later
folded no-gap-by-construction into the authoritative architecture, so M1 is retained as a
decision history rather than a contradictory current verdict.

The DDL drift is broader than a type typo: architecture §6.3 requires `wall_ts REAL NOT NULL`,
`monotonic_ms INTEGER NOT NULL`, task/kind indexes, and a `snapshots` table. The store declares
`ts TEXT`, coerces with `str()`, leaves `monotonic_ms` nullable, and creates only `events`. The
checkpoint's `busy` result is fetched and discarded, the exact failure warned about by
`sqlite-wal-durability.md:182–184`; a busy checkpoint could acknowledge durability too early.
The queue's unbounded behavior is documented as “bounded-with-backpressure is a v2.1 option”
because dropping a source-of-truth event was not acceptable at the snapshot.

### Merge citations and integration

The conformance audit matched every cited worktree experiment: F5/Experiment 2e covers
quarantine-free `update-ref`; F11/Experiment 4 captures the staging SHA before worktree removal;
F18/Experiment 6a rebases a local staging branch rather than the worker branch; the other rows
cover worker-tip reachability, cleanup, and expected-old/fast-forward refusal. `create_main`
uses git's empty-old “must not exist” primitive but `publish_merge` refuses an empty/zero expected
old, closing the backdoor tested in `test_merge.py:307–324`. `reconcile` returns the current main
SHA so a future caller can close the ref/event crash gap.

The architecture's §7.8 event flow is still absent: `merge.py` emits no `merge_committed` or
`merge_reconciled`, and its per-instance staging bookkeeping is not Unio's cross-session lock.
The historical supervisor calls `git merge --ff-only` and writes JSONL, so component-level
conformance must not be reported as runtime conformance. This distinction is the causal check
for M2: import/wiring inspection, not another unit test, distinguishes the two hypotheses.

### IPC and event seed history

The old N-A item was based on `ad372ae` branch-only IPC. Merge `38e1d43` placed `ipc.py` and
`worker.py` in main; the canonical framer now caps a line at 1,048,576 bytes, resynchronizes
after an oversized line, skips malformed/non-object JSON, caps diff at 64 KiB, and enforces
ready/init/idle deadlines. The slice remains a separate path with `result_envelope`/
`exit_message` names and `succeeded`/`failed` statuses; the draft uses the former and the
architecture table uses `result`/`exit` and includes `done`. Historical `events.py`'s `type`/`timestamp`
dataclasses are imported only through the unused `orchestrator.py`; store and slice producers
write dict envelopes with `kind`/`ts`. This is why M4 is a model-selection problem, not a missing
field bug.

### F-number and experiment cross-check

The `merge.py` comments were compared one by one with `worktree-concurrency.md`: worker-tip
reachability and stale-worktree refusal (`F1`/Experiment 1), quarantine-free environment and
`update-ref` refusal (`F5`/Experiment 2e), staging SHA capture (`F11`/Experiment 4), and local
staging-branch rebasing (`F18`/Experiment 6a) all matched. The remaining rows cover the same
worktree-concurrency evidence for no lost updates, cleanup, and ref ownership; no citation
exceeded its experiment.

The event draft's D2 `event_id` (ULID) and D3 derived worker identity are deliberately separate.
Architecture has `seq` but no `event_id`; the store accepts opaque `worker_id` and tests provide
it manually. The slice envelope contains only `{kind,timestamp,payload}` and no top-level seq,
generation, request ID, or worker ID. M5 is therefore an absent producer, not an EventStore type
error.

The historical current-main recheck named later branch folds `39005fa`, `77f3d52`, and
`c31e781`, and expected canonical supervisor integration at `b709375`. These refs identify the
old status snapshot only; the current pointer at the top controls present claims.

The report's “CONFORM” wording is intentionally scoped: it means the cited function performs the
normative operation with the tested inputs. It does not mean the supervisor reaches that
function. Likewise, “RE-AUDITABLE” for IPC means the file and tests exist after `38e1d43`; it does
not mean FD 3, Custos admission, DLQ, or production provider wiring exists. This scope rule is
why the gap table keeps M2/M4/M5 even after component merges.

The report preserves evidence pointers rather than repeating an architecture overview: §6.2's
subscriber/ring-buffer/replay path, §7.8's merge events, draft D2/D3 envelope fields, and the
specific source lines above are the checks a future M1 audit must rerun. A changed baseline SHA
requires a fresh anchor; historical 38/108/647 test counts must not be silently re-used.

The later hierarchy evidence belongs to architecture-target conformance, not this runtime audit:
Prime's child contexts and bounded depth are compatible with D2 I2.1–I2.6, but descendants share
one root worker and no process-per-child isolation is implied. Conformance should test static DAG
validation before admission, fresh context construction, strict upward envelope fields, and
projection rebuild before it measures provider cache or branch economics.

That ordering keeps structural conformance separate from performance claims.
