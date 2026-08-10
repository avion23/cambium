"""SQLite WAL event store with a single dedicated writer thread.

Implements the architecture's event-log durability contract (docs/architecture.md
§6.1-§6.5, validated in docs/research/sqlite-wal-durability.md): WAL mode with
``synchronous=NORMAL``, one writer thread that owns the write connection and the
DB/WAL fds, a ``wal_checkpoint(TRUNCATE)`` + fsync cadence every
``fsync_interval_s``, and critical kinds that block the producer until the row is
fsync'd.

Deviations from the architecture text (noted here because another agent owns
docs/architecture.md):

- **Bounded queue with an explicit overflow policy (M4).** The enqueue queue is
  bounded by ``max_queue_size`` (default 10 000, architecture §6.2 inv. 2).
  Critical events are **never** dropped: to admit a critical event the oldest
  non-critical item is evicted, and if the queue is still full the producer
  waits (backpressure) up to ``critical_timeout_s``. A non-critical event
  against a full queue is dropped; ``dropped`` counts both evictions and
  incoming drops, and each drop is logged.
- **Dropped events create ``seq`` gaps.** ``seq`` is reserved at enqueue, so a
  dropped event's ``seq`` never reaches the table. This supersedes the old
  "gaps cannot occur by construction" claim *under overflow only*; replay and
  ``events_after`` already tolerate missing events (phantom-read caveat §6.5).
- **Hard deadline for critical appends (M4).** A critical append waits at most
  ``critical_timeout_s`` for its fsync ack; on expiry it raises
  ``StoreTimeout`` and the store stays alive (the event is still queued and is
  written if the writer recovers). The row is never acknowledged before the
  writer's fsync barrier completes.
- **Checkpoint ``busy`` is never acknowledged (M4).** ``_fsync_now`` inspects
  the ``wal_checkpoint(TRUNCATE)`` result row; while ``busy != 0`` it retries
  for up to ``checkpoint_busy_retry_s`` and then raises (writer death) rather
  than acking a non-flushed checkpoint (docs/research/sqlite-wal-durability.md
  §3 finding 4).
- **Final close/fsync errors propagate.** ``close()`` re-raises a failure in
  the writer's final flush instead of swallowing it.
- **Phantom read.** A non-critical append returns a reserved ``seq`` whose row
  may not be durable yet: ``events_after(seq)`` may not observe it, and a crash
  inside ``fsync_interval_s`` can lose it. Callers must tolerate both.
- **Writer death is fatal.** Any error in the writer thread (sqlite/fsync/disk)
  marks the store dead: pending appends raise ``StoreError``, pending events are
  lost, and the supervisor must treat store death as fatal.

All write-connection use (including ``busy_timeout`` and ``_fsync_now``) happens
on the writer thread; readers use their own short-lived connections.
"""

from __future__ import annotations

import collections
import json
import logging
import os
import queue
import sqlite3
import threading
import time
from collections import deque
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)

CRITICAL_KINDS = frozenset({
    "result", "checkpoint", "worker_exit", "task_failed",
    "merge_progress", "task_assigned", "merge_committed",
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
# Checkpoints poll busy readers; a short per-call busy wait keeps the retry
# loop (and the critical-append deadline) in control of total wait time.
_CHECKPOINT_BUSY_TIMEOUT_MS = 20
_CHECKPOINT_RETRY_SLEEP_S = 0.02

_SENTINEL = object()
_TIMER = object()


class StoreError(Exception):
    """The event store is dead; pending events were not committed."""


class StoreTimeout(StoreError):
    """A critical append was not fsync-acknowledged within its hard deadline."""


class StoreInitError(StoreError):
    """The event store failed to start."""


class _Pending:
    """One in-flight append: the writer signals completion and any failure."""

    __slots__ = ("event", "exc")

    def __init__(self) -> None:
        self.event = threading.Event()
        self.exc: BaseException | None = None


class _BoundedEventQueue:
    """Bounded FIFO of ``(seq, kind, row, pending)`` items.

    Overflow policy (architecture §6.2 inv. 2): critical items are never
    dropped — to admit one, the oldest non-critical item is evicted; if the
    queue is still full the enqueuer waits (backpressure) up to ``timeout`` and
    then ``queue.Full`` is raised. A non-critical item against a full queue is
    dropped (the incoming one). ``put`` returns the number of dropped items.
    """

    __slots__ = ("_items", "_maxsize", "_cond")

    def __init__(self, maxsize: int) -> None:
        self._items: deque[Any] = collections.deque()
        self._maxsize = maxsize
        self._cond = threading.Condition()

    def put(self, item: tuple, *, critical: bool, timeout: float) -> int:
        deadline = time.monotonic() + timeout
        with self._cond:
            if not critical:
                if len(self._items) >= self._maxsize:
                    return 1
                self._items.append(item)
                self._cond.notify()
                return 0
            dropped = 0
            while len(self._items) >= self._maxsize:
                if self._evict_oldest_noncritical():
                    dropped += 1
                    continue
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    raise queue.Full
                self._cond.wait(remaining)
            self._items.append(item)
            self._cond.notify()
            return dropped

    def get(self, timeout: float) -> Any:
        deadline = time.monotonic() + timeout
        with self._cond:
            while not self._items:
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    raise queue.Empty
                self._cond.wait(remaining)
            self._cond.notify_all()
            return self._items.popleft()

    def get_nowait(self) -> Any:
        with self._cond:
            if not self._items:
                raise queue.Empty
            self._cond.notify_all()
            return self._items.popleft()

    def _evict_oldest_noncritical(self) -> bool:
        for i, item in enumerate(self._items):
            if isinstance(item, tuple) and item[1] not in CRITICAL_KINDS:
                del self._items[i]
                return True
        return False


class EventStore:
    """Append-only SQLite WAL event log; the writer thread is the sole write connection."""

    def __init__(
        self,
        path: Path,
        *,
        fsync_interval_s: float = 1.0,
        startup_timeout_s: float = 10.0,
        max_queue_size: int = 10_000,
        critical_timeout_s: float = 10.0,
        checkpoint_busy_retry_s: float = 10.0,
    ) -> None:
        if max_queue_size < 1:
            raise ValueError("max_queue_size must be >= 1")
        if critical_timeout_s <= 0:
            raise ValueError("critical_timeout_s must be > 0")
        if checkpoint_busy_retry_s <= 0:
            raise ValueError("checkpoint_busy_retry_s must be > 0")
        self._path = Path(path)
        self._path.parent.mkdir(parents=True, exist_ok=True)
        self._fsync_interval_s = fsync_interval_s
        self._critical_timeout_s = critical_timeout_s
        self._checkpoint_busy_retry_s = checkpoint_busy_retry_s
        self._queue: _BoundedEventQueue = _BoundedEventQueue(max_queue_size)
        self._lock = threading.Lock()
        self._closed = False
        self._dead: BaseException | None = None
        self._close_error: BaseException | None = None
        self._dropped = 0
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

    @property
    def dropped(self) -> int:
        """Number of non-critical events dropped by the bounded-queue policy."""
        return self._dropped

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
        critical = kind in CRITICAL_KINDS
        deadline = time.monotonic() + self._critical_timeout_s
        with self._lock:
            if self._dead is not None:
                raise StoreError("event store is dead") from self._dead
            if self._closed:
                raise RuntimeError("EventStore is closed")
            seq = self._next_seq
            self._next_seq += 1
            try:
                remaining = max(deadline - time.monotonic(), 0.0)
                dropped = self._queue.put(
                    (seq, kind, row, pending), critical=critical, timeout=remaining
                )
            except queue.Full:
                raise StoreTimeout(
                    f"critical event {kind!r} not enqueued within "
                    f"{self._critical_timeout_s}s (writer stalled)"
                ) from None
            if dropped:
                self._dropped += dropped
                logger.warning(
                    "event store overflow: dropped %d non-critical event(s), "
                    "latest seq %d kind=%r",
                    dropped,
                    seq,
                    kind,
                )
        if critical:
            if not pending.event.wait(max(deadline - time.monotonic(), 0.0)):
                raise StoreTimeout(
                    f"critical event {kind!r} not fsync-acknowledged within "
                    f"{self._critical_timeout_s}s"
                )
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
        try:
            self._queue.put(_SENTINEL, critical=True, timeout=self._critical_timeout_s)
        except queue.Full:
            logger.error("event store close: writer stalled, sentinel not enqueued")
        self._thread.join()
        if self._close_error is not None:
            raise self._close_error

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
                except Exception as exc:
                    self._close_error = exc
                    with self._lock:
                        self._dead = exc
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
        """Checkpoint the WAL and fsync both fds; never ack a busy checkpoint.

        The ``wal_checkpoint(TRUNCATE)`` result row is ``(busy, log, ckpt)``.
        While ``busy != 0`` some frames were not flushed (a reader holds the
        WAL); retry up to ``checkpoint_busy_retry_s`` and then raise so no
        durability acknowledgement is given (docs/research/sqlite-wal-durability.md
        §3 finding 4).
        """
        deadline = time.monotonic() + self._checkpoint_busy_retry_s
        self._conn.execute(f"PRAGMA busy_timeout={_CHECKPOINT_BUSY_TIMEOUT_MS}").fetchall()
        try:
            while True:
                cur = self._conn.execute("PRAGMA wal_checkpoint(TRUNCATE)")
                row = cur.fetchone()
                cur.close()
                busy = row[0] if row is not None else 1
                if busy == 0:
                    break
                if time.monotonic() >= deadline:
                    raise StoreError(
                        f"wal_checkpoint(TRUNCATE) stayed busy ({busy} frames) for "
                        f"{self._checkpoint_busy_retry_s}s; durability not acked"
                    )
                time.sleep(_CHECKPOINT_RETRY_SLEEP_S)
        finally:
            self._conn.execute(f"PRAGMA busy_timeout={_WRITER_BUSY_TIMEOUT_MS}").fetchall()
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
