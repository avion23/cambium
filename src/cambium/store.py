"""SQLite WAL event store with a single dedicated writer thread.

Implements the architecture's event-log durability contract (docs/architecture.md
§6.1-§6.5, validated in docs/research/sqlite-wal-durability.md): WAL mode with
``synchronous=NORMAL``, one writer thread that owns the write connection and the
DB/WAL fds, a ``wal_checkpoint(TRUNCATE)`` + fsync cadence every
``fsync_interval_s``, and critical kinds that block the producer until the row is
fsync'd.

Deviations from the architecture text (noted here because another agent owns
docs/architecture.md):

- **No ``recovery_gap`` gaps.** ``seq`` is reserved at enqueue and the sole
  writer commits in reservation order, so gaps cannot occur by construction; the
  architecture.md §6.5 ``recovery_gap`` mechanism is superseded. Lost events are
  detected via the crash window (the fsync cadence), not seq gaps.
- **Phantom read.** A non-critical append returns a reserved ``seq`` whose row
  may not be durable yet: ``events_after(seq)`` may not observe it, and a crash
  inside ``fsync_interval_s`` can lose it. Callers must tolerate both.
- **Unbounded queue.** Events are the source of truth; dropping one would lose
  state, so the queue is unbounded by design. Bounded-with-backpressure is a
  v2.1 option.
- **Writer death is fatal.** Any error in the writer thread (sqlite/fsync/disk)
  marks the store dead: pending appends raise ``StoreError``, pending events are
  lost, and the supervisor must treat store death as fatal.

All write-connection use (including ``busy_timeout`` and ``_fsync_now``) happens
on the writer thread; readers use their own short-lived connections.
"""

from __future__ import annotations

import json
import os
import queue
import sqlite3
import threading
import time
from pathlib import Path
from typing import Any

CRITICAL_KINDS = frozenset({
    "result", "checkpoint", "worker_exit", "task_failed",
    "merge_progress", "task_assigned", "merge_committed",
    "merge_staging_quarantined", "merge_staging_cleanup_failed",
    "merge_staging_prune_started", "merge_staging_pruned",
})

_SCHEMA = """CREATE TABLE IF NOT EXISTS events (
    seq          INTEGER PRIMARY KEY AUTOINCREMENT,
    kind         TEXT    NOT NULL,
    payload      TEXT    NOT NULL,
    ts           TEXT,
    monotonic_ms INTEGER,
    task_id      TEXT,
    worker_id    TEXT,
    generation   INTEGER,
    request_id   TEXT
)"""

_INSERT = (
    "INSERT INTO events(seq, kind, payload, ts, monotonic_ms, task_id, "
    "worker_id, generation, request_id) VALUES(?, ?, ?, ?, ?, ?, ?, ?, ?)"
)

_SELECT_AFTER = (
    "SELECT seq, kind, payload, ts, monotonic_ms, task_id, worker_id, "
    "generation, request_id FROM events WHERE seq > ? ORDER BY seq"
)

_WRITER_BUSY_TIMEOUT_MS = 5000

_SENTINEL = object()
_TIMER = object()


class StoreError(Exception):
    """The event store is dead; pending events were not committed."""


class StoreInitError(StoreError):
    """The event store failed to start."""


class _Pending:
    """One in-flight append: the writer signals completion and any failure."""

    __slots__ = ("event", "exc")

    def __init__(self) -> None:
        self.event = threading.Event()
        self.exc: BaseException | None = None


class EventStore:
    """Append-only SQLite WAL event log; the writer thread is the sole write connection."""

    def __init__(
        self,
        path: Path,
        *,
        fsync_interval_s: float = 1.0,
        startup_timeout_s: float = 10.0,
    ) -> None:
        self._path = Path(path)
        self._path.parent.mkdir(parents=True, exist_ok=True)
        self._fsync_interval_s = fsync_interval_s
        self._queue: queue.Queue[Any] = queue.Queue()
        self._lock = threading.Lock()
        self._closed = False
        self._dead: BaseException | None = None
        self._next_seq = 0
        self._started = threading.Event()
        self._thread = threading.Thread(
            target=self._writer_loop, name="cambium-event-store", daemon=True
        )
        self._thread.start()
        if not self._started.wait(startup_timeout_s):
            raise StoreInitError(
                f"event store did not start within {startup_timeout_s}s: {self._path}"
            )
        if self._dead is not None:
            raise StoreInitError("event store failed to initialize") from self._dead

    def append(self, event: dict[str, Any]) -> int:
        kind = event.get("kind")
        if not isinstance(kind, str) or not kind:
            raise ValueError("event requires a non-empty string 'kind'")
        row = (
            json.dumps(event.get("payload", {})),
            str(event["ts"]) if event.get("ts") is not None else None,
            event.get("monotonic_ms"),
            event.get("task_id"),
            event.get("worker_id"),
            event.get("generation"),
            event.get("request_id"),
        )
        pending = _Pending()
        with self._lock:
            if self._dead is not None:
                raise StoreError("event store is dead") from self._dead
            if self._closed:
                raise RuntimeError("EventStore is closed")
            seq = self._next_seq
            self._next_seq += 1
            self._queue.put_nowait((seq, kind, row, pending))
        if kind in CRITICAL_KINDS:
            pending.event.wait()
            if pending.exc is not None:
                raise StoreError("event store died while appending") from pending.exc
        return seq

    def events_after(self, seq: int) -> list[dict[str, Any]]:
        conn = sqlite3.connect(self._path)
        try:
            conn.execute("PRAGMA busy_timeout=5000")
            rows = conn.execute(_SELECT_AFTER, (seq,)).fetchall()
        finally:
            conn.close()
        return [self._row_to_event(row) for row in rows]

    def close(self) -> None:
        with self._lock:
            if self._closed:
                return
            self._closed = True
            dead = self._dead
        if dead is not None:
            self._thread.join()
            return
        self._queue.put(_SENTINEL)
        self._thread.join()

    def _writer_loop(self) -> None:
        conn = None
        db_fd = None
        wal_fd = None
        try:
            conn = sqlite3.connect(self._path, isolation_level=None)
            conn.execute(f"PRAGMA busy_timeout={_WRITER_BUSY_TIMEOUT_MS}").fetchall()
            conn.execute("PRAGMA journal_mode=WAL").fetchall()
            conn.execute("PRAGMA synchronous=NORMAL").fetchall()
            conn.execute("PRAGMA wal_autocheckpoint=0").fetchall()
            conn.execute(_SCHEMA)
            db_fd = os.open(self._path, os.O_RDWR)
            wal_fd = os.open(f"{self._path}-wal", os.O_RDWR)
            self._conn = conn
            self._db_fd = db_fd
            self._wal_fd = wal_fd
            with self._lock:
                self._next_seq = conn.execute(
                    "SELECT COALESCE(MAX(seq), 0) + 1 FROM events"
                ).fetchone()[0]
                self._started.set()
        except Exception as exc:
            with self._lock:
                self._dead = exc
                self._started.set()
            if wal_fd is not None:
                os.close(wal_fd)
            if db_fd is not None:
                os.close(db_fd)
            if conn is not None:
                conn.close()
            return

        dirty = False
        next_fsync = time.monotonic() + self._fsync_interval_s
        cur_pending = None
        dead_exc = None
        try:
            while True:
                remaining = next_fsync - time.monotonic()
                try:
                    item = self._queue.get(timeout=max(remaining, 0.0))
                except queue.Empty:
                    item = _TIMER
                if item is _SENTINEL:
                    break
                if item is _TIMER:
                    if dirty:
                        self._fsync_now()
                        dirty = False
                    next_fsync = time.monotonic() + self._fsync_interval_s
                    continue
                seq, kind, row, pending = item
                cur_pending = pending
                conn.execute(_INSERT, (seq, kind, *row))
                dirty = True
                if kind in CRITICAL_KINDS:
                    self._fsync_now()
                    dirty = False
                    next_fsync = time.monotonic() + self._fsync_interval_s
                pending.event.set()
                cur_pending = None
        except Exception as exc:
            dead_exc = exc
            with self._lock:
                self._dead = exc
            self._fail_pending(exc)
            if cur_pending is not None:
                cur_pending.exc = exc
                cur_pending.event.set()
        finally:
            if dead_exc is None:
                try:
                    self._fsync_now()
                except Exception:
                    pass
            if wal_fd is not None:
                os.close(wal_fd)
            if db_fd is not None:
                os.close(db_fd)
            if conn is not None:
                conn.close()

    def _fail_pending(self, exc: BaseException) -> None:
        while True:
            try:
                item = self._queue.get_nowait()
            except queue.Empty:
                break
            if item is _SENTINEL or item is _TIMER:
                continue
            seq, kind, row, pending = item
            pending.exc = exc
            pending.event.set()

    def _fsync_now(self) -> None:
        cur = self._conn.execute("PRAGMA wal_checkpoint(TRUNCATE)")
        cur.fetchone()
        cur.close()
        os.fsync(self._wal_fd)
        os.fsync(self._db_fd)

    @staticmethod
    def _row_to_event(row: tuple) -> dict[str, Any]:
        (
            seq, kind, payload, ts, monotonic_ms,
            task_id, worker_id, generation, request_id,
        ) = row
        return {
            "seq": seq,
            "kind": kind,
            "payload": json.loads(payload),
            "ts": ts,
            "monotonic_ms": monotonic_ms,
            "task_id": task_id,
            "worker_id": worker_id,
            "generation": generation,
            "request_id": request_id,
        }
