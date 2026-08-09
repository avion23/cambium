"""SQLite WAL event store with a single dedicated writer thread.

Implements the architecture's event-log durability contract (docs/architecture.md
§6.1-§6.5, validated in docs/research/sqlite-wal-durability.md): WAL mode with
``synchronous=NORMAL``, one writer thread that owns the write connection and the
DB/WAL fds, a ``wal_checkpoint(TRUNCATE)`` + fsync cadence every
``fsync_interval_s``, and critical kinds that block the producer until the row is
fsync'd.
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

_SENTINEL = object()
_TIMER = object()


class EventStore:
    """Append-only SQLite WAL event log; the writer thread is the sole write connection."""

    def __init__(self, path: Path, *, fsync_interval_s: float = 1.0) -> None:
        self._path = Path(path)
        self._path.parent.mkdir(parents=True, exist_ok=True)
        self._fsync_interval_s = fsync_interval_s
        self._queue: queue.Queue[Any] = queue.Queue()
        self._lock = threading.Lock()
        self._closed = False
        self._next_seq = 0
        self._started = threading.Event()
        self._thread = threading.Thread(
            target=self._writer_loop, name="cambium-event-store", daemon=True
        )
        self._thread.start()
        self._started.wait()

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
        done = threading.Event()
        with self._lock:
            if self._closed:
                raise RuntimeError("EventStore is closed")
            seq = self._next_seq
            self._next_seq += 1
            self._queue.put_nowait((seq, kind, row, done))
        if kind in CRITICAL_KINDS:
            done.wait()
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
        self._queue.put(_SENTINEL)
        self._thread.join()

    def _writer_loop(self) -> None:
        conn = sqlite3.connect(self._path, isolation_level=None)
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
        dirty = False
        next_fsync = time.monotonic() + self._fsync_interval_s
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
                seq, kind, row, done = item
                conn.execute(_INSERT, (seq, kind, *row))
                dirty = True
                if kind in CRITICAL_KINDS:
                    self._fsync_now()
                    dirty = False
                    next_fsync = time.monotonic() + self._fsync_interval_s
                done.set()
        finally:
            try:
                self._fsync_now()
            finally:
                os.close(wal_fd)
                os.close(db_fd)
                conn.close()

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
