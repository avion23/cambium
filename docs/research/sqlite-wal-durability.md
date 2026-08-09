# SQLite WAL event-log durability: empirical validation

Research date: 2026-08-09. Validates the Cambium event-log durability design
(architecture §6.1–6.5) against a real SQLite 3.53.1 WAL database driven by
Python 3.14.7 `sqlite3`. Every number below is a real run output from
`/tmp/opencode/exp-sqlite/` (the experiment directory, outside the worktree);
anything that could not be tested is marked **UNVERIFIED**.

## Claim under test (architecture.md)

> §6.2.4: "In batched mode it flushes the SQLite WAL to disk at most once per
> `fsync_interval_s` (default 1.0) via `PRAGMA wal_checkpoint(TRUNCATE)`
> followed by `os.fsync(wal_fd)` on the WAL file's fd (not the main DB fd — in
> WAL mode recent commits live in the `-wal` file, so fsyncing the main DB fd
> alone is a no-op for durability)."
>
> §6.5: "`PRAGMA synchronous=NORMAL` … WAL+NORMAL is crash-safe for the most
> recently committed transaction; FULL would add an fsync per commit and is
> unnecessary with our explicit WAL checkpoint."
>
> §6.5 table: critical events — "Loss window on supervisor crash: zero";
> non-critical — "Loss window on supervisor crash: at most `fsync_interval_s`
> of the most recent non-critical events."
>
> §6.5 mechanism:
> ```python
> def _fsync_now(self) -> None:
>     cur = self._conn.execute("PRAGMA wal_checkpoint(TRUNCATE)")
>     cur.close()
>     os.fsync(self._wal_fd)        # fsync the WAL file (recent commits live here)
>     os.fsync(self._db_fd)         # belt-and-braces; cheap after a TRUNCATE checkpoint
> ```

Reviewer flag addressed: "fsync target confusion (fsync db_fd vs WAL file)."

## Environment

| | |
|---|---|
| Interpreter | CPython 3.14.7 (`/home/ubuntu/.local/bin/python3.14`, uv build, Clang 22.1.3) |
| `sqlite3` module | 3.53.1 (bundled); `THREADSAFE=1`, `MUTEX_PTHREADS`, `DEFAULT_SYNCHRONOUS=2`, `WAL_AUTOCHECKPOINT=1000` |
| OS / kernel | Linux aarch64, 6.17.0-1009-oracle |
| Filesystem | btrfs, `noatime,compress=zstd:3,ssd` (fsync timing is fs-specific; see caveats) |
| strace | 6.8 |

```
$ python3.14 -c "import sqlite3; print(sqlite3.sqlite_version)"
3.53.1
$ python3.14 -c "import sqlite3; print(sqlite3.threadsafety)"
3
```

Event schema mirrors architecture §6.3: `events(seq INTEGER PRIMARY KEY
AUTOINCREMENT, monotonic_ms, wall_ts, kind, payload)`.

## 1. WAL read-while-write (Q1)

Design: one writer thread owns the sole write connection; one reader thread
owns its own connection; `synchronous=NORMAL`, `wal_autocheckpoint=0` (WAL
forced to stay uncheckpointed). Script: `01_read_while_write.py`.

(a) Writer holds `BEGIN IMMEDIATE` + `INSERT` uncommitted for 250 ms; reader
reads during the open transaction:

```
a_reader_while_write_txn_open: { count_seen: 1, read_latency_ms: 0.0844,
                                 saw_uncommitted: false }
a_after_commit_count_seen: 2
```

Reader saw only the committed baseline (1), never the uncommitted row, at
0.084 ms — it did **not** block on the open write transaction.

(b) Steady-state, writer at ~2 000 events/s, reader polling in a tight loop:

```
b_writer: { events: 4000, throughput_ev_s: 1997.7, wall_s: 2.002 }
b_reader: { reads: 28446, max_read_latency_ms: 240.3984, reads_over_1ms: 79,
            min_seen_seq: 6, max_seen_seq: 3994,
            never_saw_more_rows_than_final_committed: true }
b_final_count: 4002
c_wal: { size_bytes: 33256672 }   c_db_size_bytes: 4096
```

The single 240 ms read is an outlier (WAL file growth / scheduler); 79 of
28 446 reads (0.28%) exceeded 1 ms; median latency is well under 1 ms. The
reader saw rows grow live (seq 6 → 3994) and never observed more rows than
were ever committed. With the WAL uncheckpointed, all committed data lives in
the `-wal` file (33 MB) while `events.db` stays at one 4 KB page.

**Result:** WAL read-while-write holds exactly as documented — non-blocking,
snapshot-isolated, committed rows visible immediately, uncommitted rows
invisible.

## 2. Durability on process crash: the loss window (Q2)

Design: a writer process commits events (each `INSERT` + commit) and records
each committed count + commit time to a sidecar file; the writer then crashes
hard (`os._exit(9)` or parent `SIGKILL`) with the WAL uncheckpointed; the
parent reopens the DB (triggers WAL recovery), runs `PRAGMA integrity_check`,
and compares committed-before-crash vs survived. Script: `02_crash_loss.py`.

| Trial | synchronous | wal_autocheckpoint | crash | committed | survived | lost | integrity |
|---|---|---|---|---|---|---|---|
| default autockpt | NORMAL | 1000 | `os._exit` | 5 000 | 5 000 | **0** | ok |
| uncheckpointed | NORMAL | 0 | `os._exit` | 5 000 | 5 000 | **0** | ok |
| uncheckpointed + `fsync_now` | NORMAL | 0 | `os._exit` | 5 000 | 5 000 | **0** | ok |
| FULL | FULL | 0 | `os._exit` | 2 000 | 2 000 | **0** | ok |
| OFF | OFF | 0 | `os._exit` | 5 000 | 5 000 | **0** | ok |
| SIGKILL r0 | NORMAL | 0 | SIGKILL @1 s | 7 697 | 7 697 | **0** | ok |
| SIGKILL r1 | NORMAL | 0 | SIGKILL @1 s | 7 638 | 7 638 | **0** | ok |
| SIGKILL r2 | NORMAL | 0 | SIGKILL @1 s | 7 657 | 7 657 | **0** | ok |
| batch txn mid-write | NORMAL | 0 | SIGKILL mid-`BEGIN…INSERT` | 1 (pre-batch) | 1 | whole txn rolled back | ok |

Key finding: on a **process crash** (the "supervisor crash" the contract names),
`synchronous=NORMAL` with **no fsync at all** loses **zero** committed events
(8/8 trials, 44 992 committed events), and `integrity_check` is `ok` in every scenario
including a SIGKILL mid-transaction (the uncommitted transaction is rolled
back atomically, not corrupted). Committed events survive because the OS page
cache persists across process death; `conn.commit()` returns only after the
WAL frames reach the page cache via `write()`, and a supervisor crash does not
drop the page cache.

The measured non-critical loss window for a process crash is therefore
**0 s for committed events**, not "at most 1 s" — the architecture's contract
is satisfied and is, on this axis, conservative. The 1 s cadence is what
matters for the *other* failure mode (power/kernel loss, §4).

## 3. The correct fsync target (Q3)

Design: writer commits 3 000 events (`NORMAL`, `wal_autocheckpoint=0`), applies
exactly one incantation, records `stat()` before/after, then `os._exit(0)`;
parent reopens and verifies. Script: `03_fsync_target.py`. fd→path evidence
via `strace -e trace=fsync,fdatasync -y` (S4):

```
fsync_db          last syscall: fsync(7</…/q4inc.db>)            # main DB fd only
fsync_wal         last syscall: fsync(8</…/q4inc.db-wal>)        # WAL fd only
ckpt_truncate     fsync(5<…/q4inc.db-wal>) → fsync(4<…/q4inc.db>) # checkpoint syncs BOTH
fsync_now_full    fsync(5<wal>) → fsync(8<wal>) → fsync(7<db>)   # ckpt + explicit wal + explicit db
```

| Incantation | wal_size before | wal_size after | db_size after | fsync duration |
|---|---|---|---|---|
| none | 24 934 272 | 24 934 272 | 4 096 | 0.002 ms |
| `os.fsync(db_fd)` | 24 934 272 | 24 934 272 | 4 096 | 0.016 ms |
| `os.fsync(wal_fd)` | 24 934 272 | 24 934 272 | 4 096 | 65.1 ms |
| `wal_checkpoint(TRUNCATE)` | 24 934 272 | **0** | 114 688 | 70.3 ms |
| ckpt(TRUNCATE) + `fsync(wal_fd)` | 24 934 272 | **0** | 114 688 | 63.0 ms |
| ckpt(TRUNCATE) + `fsync(db_fd)` | 24 934 272 | **0** | 114 688 | 69.8 ms |
| **architecture `_fsync_now`** | 24 934 272 | **0** | 114 688 | 74.1 ms |

All incantations recovered all 3 000 events after the process crash
(integrity `ok`) — expected, because the page cache survives the process (§2);
the fsync target only decides what survives *power loss*.

Conclusions on the reviewer's flag:

1. **`os.fsync(db_fd)` alone is confirmed a no-op for uncheckpointed commits.**
   S4 shows it touches only `<q4inc.db>`; the WAL file (holding every frame of
   the 3 000 commits) is untouched and keeps its 24.9 MB. Architecture §6.2.4's
   parenthetical is empirically correct.
2. **The correct target depends on checkpoint state.** Uncheckpointed frames
   live in `-wal`, so **fsync the WAL fd** (`fsync_wal` is the one incantation
   that flushes them in place). After `wal_checkpoint(TRUNCATE)` the frames
   have moved into `events.db` and the WAL is truncated to **0 bytes**, so the
   effective barrier is now **fsync the DB fd**; fsyncing the WAL fd then is a
   no-op on an empty file.
3. **`PRAGMA wal_checkpoint(TRUNCATE)` alone is already a full barrier on this
   SQLite build.** S4 `ckpt_truncate` shows it issues its own `fsync(wal)` then
   `fsync(db)` under `synchronous=NORMAL` (confirmed in the standalone probe:
   `fsync(wal) → fsync(db) → fsync(wal)`; cf. `probe_ckpt.py`). So
   `wal_checkpoint(TRUNCATE)` + `os.fsync(db_fd)` is the minimal correct
   incantation.
4. **The architecture's `_fsync_now` is correct in net effect but its comment
   is wrong at the wrong moment.** `checkpoint(TRUNCATE) → fsync(wal_fd) →
   fsync(db_fd)` works: after the TRUNCATE the explicit `fsync(wal_fd)` is a
   harmless no-op (WAL is empty) and durability comes from the DB fsync (done
   both inside the checkpoint and explicitly). The comment "fsync the WAL file
   (recent commits live here)" (line 461) is only true *before* the checkpoint;
   post-checkpoint the recent commits live in `events.db`. One real defect: the
   snippet ignores the checkpoint return row `(busy, log, ckpt_frames)` — if a
   checkpoint returns `busy` the call returns without flushing and the writer
   would ack a critical event as durable. Recommend checking the return value.

## 4. The ≤1 s loss window for power/kernel loss (strace cadence)

`os._exit`/SIGKILL cannot simulate power loss (page cache persists), so the
power-loss window is measured as the **max gap between WAL fd `fsync`s**
observed with `strace -tt -e trace=fsync,fdatasync -y`
(`loop_writer.py` + `strace_gaps.py`). Direct power-loss injection
(page-cache drop / reboot / dm-log-writes replay) was not available on this
host — those numbers are strace-derived, not injection-measured
(**UNVERIFIED for direct injection**).

| Scenario | WAL fd fsyncs | max inter-fsync gap | DB fd fsyncs |
|---|---|---|---|
| S1 NORMAL, `wal_autocheckpoint=0`, 2 s full speed, no explicit fsync | 1 (startup only) | **no barrier during run** | 1 |
| S2 NORMAL, `wal_autocheckpoint=1000` (default), 2 s full speed | 33 | 0.145 s | 17 (max 0.158 s) |
| S3 NORMAL, `wal_autocheckpoint=0`, architecture `fsync_now` every 1 s, 5.5 s | 16 | **1.009 s** | 11 (max 1.084 s) |
| S5 NORMAL, `wal_autocheckpoint=1000`, **2 events/s** (heartbeat-like), 10.2 s | 1 (startup only) | **no barrier during run** | 1 |

S3 trace (one cadence cycle per second):

```
…26.409818 fsync(4<…/s3.db-wal>)   # checkpoint begin
…26.480529 fsync(3<…/s3.db>)       # checkpoint copies frames, syncs DB
…26.488439 fsync(7<…/s3.db-wal>)   # explicit os.fsync(wal_fd)
…26.489861 fsync(6<…/s3.db>)       # explicit os.fsync(db_fd)
…27.407556 fsync(4<…/s3.db-wal>)   # next cycle, ~1 s later
```

Findings:

- **`synchronous=NORMAL` provides no per-commit durability.** S1 shows zero WAL
  fsyncs during 2 s of continuous commits — `conn.commit()` returns while the
  frames are only in the page cache. Under FULL the same workload shows one
  `fsync(wal)` per commit (cf. `probe_ckpt.py` FULL trace).
- **SQLite's own autocheckpoint does not bound the window at low rates.** At
  full speed (S2) the 1 000-page autocheckpoint keeps the gap ≤ ~0.15 s. At a
  heartbeat-like 2 events/s (S5) the WAL never reaches 1 000 pages (it grows
  37 KB → 185 KB over 10.2 s) and **zero** fsyncs occur — the power-loss loss
  window is unbounded without an explicit barrier.
- **The architecture's 1 s timer does bound it.** S3 max gap = **1.009 s** on
  the WAL fd (1.084 s on the DB fd), i.e. `fsync_interval_s + ~10 ms` of timer
  jitter. The "at most `fsync_interval_s`" non-critical power-loss window holds
  provided the timer actually runs `_fsync_now` on the writer thread (§6).

So: the ≤1 s loss window is **not a property of NORMAL + WAL alone** — it is a
property of the architecture's explicit timer-driven `_fsync_now`, and the
design correctly relies on it. The architecture text attaches the "at most 1 s"
window to "supervisor crash"; the measured supervisor-crash window is actually
0 s for committed events (§2), and the 1 s bound is what protects against the
*kernel/page-cache-loss* case. The two failure modes are conflated in the
wording but both are met by the design.

## 5. Critical events: "immediate" barrier cost

`_fsync_now` on a small (fresh) WAL, measured end-to-end
(checkpoint + `fsync(wal_fd)` + `fsync(db_fd)`) on btrfs:

```
fsync_now with 1 event:   9.329 ms
fsync_now with 5 events:  7.050 ms
fsync_now with 100 events: 9.010 ms
```

~7–9 ms on this filesystem — acceptable for rare critical events; the event
is not acked/yielded until after the barrier, matching §6.5.

## 6. Python 3.14.7 `sqlite3` under threads (Q4)

```
module_threadsafety: 3                       # serialized, safe
a_cross_thread_use: "ProgrammingError: SQLite objects created in a thread can
                     only be used in that same thread."
b: single writer thread + 4 reader threads, 2000 events, reader_errors: []
   final_count 2000, max seq seen by readers 1999
```

Script: `04_threads.py`. The single-writer-thread pattern works with plain
`threading` (no aiohttp/asyncio needed): each thread owns its connection
(default `check_same_thread=True`), readers never see uncommitted rows, counts
stay consistent. Two practical footguns confirmed:
1. A connection created in the main thread cannot be used from the writer
   thread (`ProgrammingError`) — the writer connection must be created inside
   the writer thread (the architecture's "sole process holds the write
   connection" must mean *created and owned* by that thread).
2. A timer thread calling `PRAGMA wal_checkpoint(TRUNCATE)` on the writer's
   connection raises the same `ProgrammingError` (observed in the first S3
   run). The timer must only set a flag; `_fsync_now` must run on the writer
   thread — exactly what the architecture's "inside the writer's dequeue
   loop" prescribes, and a real bug if ever implemented on a timer thread.

## 7. Why FULL is rejected (and NORMAL is right)

20 000 autocommit inserts on this host:

```
NORMAL: 20000 commits in 0.465s = 43051 ev/s  (23 us/commit)
FULL:   20000 commits in 41.712s = 479 ev/s  (2086 us/commit)
```

FULL's per-commit WAL fsync costs ~90× throughput (numbers are btrfs-specific;
on ext4/xfs the ratio is smaller but still large). The architecture's choice
of NORMAL + explicit checkpoint is empirically validated.

## Measurements table (summary)

| # | Question | Method | Result |
|---|---|---|---|
| Q1 | read-while-write non-blocking, isolated | reader thread vs open/committing writer | reader never blocks (median ≪1 ms), never sees uncommitted, sees commits immediately; WAL grows 33 MB uncheckpointed |
| Q2 | process-crash loss window | 8 × `os._exit`/`SIGKILL` + reopen + `integrity_check` | 0 committed events lost in every trial; integrity `ok` everywhere; mid-txn SIGKILL rolls back atomically |
| Q2b | corruption after uncheckpointed-WAL crash | same | none — `PRAGMA integrity_check` = `ok` in all 9 scenarios |
| Q3 | correct fsync target | incantation × fd (strace) + WAL/db size | uncheckpointed → fsync WAL fd; after TRUNCATE → fsync DB fd; `wal_checkpoint(TRUNCATE)` alone already fsyncs both (3.53.1) |
| Q4 | power-loss ≤1 s window | strace max inter-fsync gap | NORMAL alone: unbounded at 2 ev/s (S5, 0 fsyncs); with 1 s `fsync_now`: 1.009 s (S3) — **UNVERIFIED** by direct power-loss injection |
| Q5 | Python 3.14.7 threads | single writer + 4 readers | threadsafety=3; per-thread connections required; no errors, no leaks |
| Q6 | FULL vs NORMAL cost | 20 k commits each | 479 vs 43 051 ev/s (~90×) |

## Verdict

**The durability contract holds, with one wording correction and one
recommendation.**

1. **Read-while-write, atomicity, crash-recovery-without-corruption: confirmed.**
   WAL mode behaves exactly as documented; `integrity_check` passes after every
   crash, including uncheckpointed-WAL SIGKILL and mid-transaction SIGKILL.
2. **Supervisor-crash loss window: actually 0 s for committed events, stronger
   than the claimed ≤1 s.** `synchronous=NORMAL` with no fsync loses no
   committed events on process crash (page cache survives); only the uncommitted
   in-flight transaction is lost (rolled back). §6.5's "at most 1 s" is a
   bound, not the achieved value.
3. **Correct fsync target: the reviewer is right that it's state-dependent —
   and the architecture's code is right anyway.** fsync the **WAL fd** while
   frames are uncheckpointed; fsync the **DB fd** after a checkpoint (or rely
   on `wal_checkpoint(TRUNCATE)`, which on SQLite 3.53.1 under NORMAL internally
   fsyncs both). `os.fsync(db_fd)` alone for WAL-resident commits is confirmed
   a no-op. `_fsync_now`'s order works because the DB fsync (checkpoint-internal
   or explicit) is the effective barrier; its "recent commits live in the WAL"
   comment is misleading post-truncate, and its ignored checkpoint return value
   is the one real risk.
4. **The ≤1 s non-critical loss window holds for power/kernel loss only
   because of the explicit 1 s timer — NORMAL + autocheckpoint alone does not
   provide it.** At heartbeat-like rates SQLite issues zero WAL fsyncs (S5);
   the measured timer-driven cadence bounds the window to 1.009 s (S3). The
   design's mechanism is necessary and sufficient; the wording should say the
   1 s window is a power-loss bound, with supervisor-crash loss actually 0.
5. **NORMAL over FULL: confirmed.** FULL's per-commit fsync costs ~90× here.

Caveats: filesystem is btrfs (`compress=zstd:3,noatime`); fsync latencies and
the FULL penalty are fs-specific, though the qualitative conclusions are POSIX
general. Direct power-loss injection (page-cache drop / reboot / dm-log-writes)
was not available; the power-loss numbers are **strace-derived (max inter-fsync
gap), not injection-measured**.

## Reproduce

```
cd /tmp/opencode/exp-sqlite   # all artifacts live here, outside the worktree
python3.14 01_read_while_write.py   # Q1
python3.14 02_crash_loss.py         # Q2
python3.14 03_fsync_target.py       # Q3
python3.14 04_threads.py            # Q4
bash run_strace.sh                  # S1–S5 strace cadence + fd evidence
```
