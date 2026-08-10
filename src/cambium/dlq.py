"""SQLite-backed durable, bounded dead-letter records."""

from __future__ import annotations

import json
import os
import queue
import sqlite3
import threading
from pathlib import Path
from typing import Any

from .redact import Redactor
from .store import _make_private_db_file, _make_private_dir

__all__ = ["DeadLetterQueue"]

_SCHEMA_VERSION = 1
_STARTUP_TIMEOUT_S = 10.0
_SENTINEL = object()
_MISSING = "unknown"

_CREATE_TABLE = """CREATE TABLE dlq_records (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    status TEXT,
    reason TEXT,
    failure_kind TEXT,
    record TEXT NOT NULL,
    ts TEXT
)"""

_INSERT = """INSERT INTO dlq_records(status, reason, failure_kind, record, ts)
VALUES (?, ?, ?, ?, ?)"""

_PRUNE = """DELETE FROM dlq_records
WHERE id NOT IN (
    SELECT id FROM dlq_records ORDER BY id DESC LIMIT :max_entries
)"""


class _Pending:
    __slots__ = ("event", "result", "exc")

    def __init__(self) -> None:
        self.event = threading.Event()
        self.result: int | None = None
        self.exc: BaseException | None = None


class DeadLetterQueue:
    """A thread-safe DLQ with one dedicated SQLite writer thread."""

    def __init__(
        self,
        dir: Path,
        *,
        max_entries: int = 1000,
        redactor: Redactor | None = None,
    ) -> None:
        if isinstance(max_entries, bool) or not isinstance(max_entries, int):
            raise TypeError("max_entries must be an integer")
        if max_entries < 1:
            raise ValueError("max_entries must be at least 1")
        if redactor is not None and not isinstance(redactor, Redactor):
            raise TypeError("redactor must be a Redactor")

        session_dir = Path(dir)
        self._base_directory = session_dir / ".cambium"
        self._path = self._base_directory / "dlq.db"
        self._max_entries = max_entries
        self._redactor = redactor if redactor is not None else Redactor()
        self._commands: queue.Queue[Any] = queue.Queue()
        self._state_lock = threading.Lock()
        self._closed = False
        self._dead: BaseException | None = None
        self._started = threading.Event()

        self._base_directory.mkdir(parents=True, exist_ok=True)
        _make_private_dir(self._base_directory)
        _make_private_db_file(self._path)

        self._thread = threading.Thread(
            target=self._writer_loop,
            name="cambium-dlq-writer",
            daemon=True,
        )
        self._thread.start()
        if not self._started.wait(_STARTUP_TIMEOUT_S):
            raise RuntimeError(
                f"DLQ writer did not start within {_STARTUP_TIMEOUT_S}s: {self._path}"
            )
        if self._dead is not None:
            raise RuntimeError(str(self._dead)) from self._dead

    def put(self, record: dict) -> int:
        """Redact and durably insert *record*, then return its SQLite row id."""

        if not isinstance(record, dict):
            raise TypeError("record must be a dict")
        return self._put_redacted(self._redactor.redact_mapping(record))

    def entries(self) -> list[dict]:
        """Return records oldest-to-newest with their SQLite row ids."""

        rows = self._read_rows("SELECT id, record FROM dlq_records ORDER BY id")
        result: list[dict] = []
        for row_id, encoded in rows:
            record = self._decode_record(encoded)
            record["id"] = row_id
            result.append(record)
        return result

    def get(self, row_id: int) -> dict:
        """Read one queue record by its positive SQLite row id."""

        row_id = self._validate_row_id(row_id)
        rows = self._read_rows("SELECT record FROM dlq_records WHERE id = ?", (row_id,))
        if not rows:
            raise FileNotFoundError(str(row_id))
        return self._decode_record(rows[0][0])

    def remove(self, row_id: int) -> None:
        """Remove one record by row id; an absent record is harmless."""

        self._submit("remove", self._validate_row_id(row_id))

    def summarize(self) -> dict[str, Any]:
        """Summarize status and failure reason using stored SQL columns."""

        statuses = self._read_rows(
            "SELECT COALESCE(NULLIF(status, ''), 'unknown'), COUNT(*) "
            "FROM dlq_records GROUP BY COALESCE(NULLIF(status, ''), 'unknown')"
        )
        reasons = self._read_rows(
            "SELECT COALESCE(NULLIF(reason, ''), NULLIF(failure_kind, ''), 'unknown'), "
            "COUNT(*) FROM dlq_records GROUP BY "
            "COALESCE(NULLIF(reason, ''), NULLIF(failure_kind, ''), 'unknown')"
        )
        total = self._read_rows("SELECT COUNT(*) FROM dlq_records")[0][0]
        return {
            "total": total,
            "by_status": dict(statuses),
            "by_reason": dict(reasons),
        }

    def close(self) -> None:
        """Stop the writer after all preceding commands complete."""

        with self._state_lock:
            if self._closed:
                return
            self._closed = True
            self._commands.put(_SENTINEL)
        self._thread.join()
        if self._dead is not None:
            raise RuntimeError("DLQ writer failed") from self._dead

    def _put_redacted(self, record: dict) -> int:
        result = self._submit("put", record)
        if result is None:
            raise RuntimeError("DLQ writer completed without a row id")
        return result

    def _submit(self, operation: str, value: Any) -> int | None:
        pending = _Pending()
        with self._state_lock:
            if self._closed:
                raise RuntimeError("DLQ is closed")
            if self._dead is not None:
                raise RuntimeError("DLQ writer is dead") from self._dead
            self._commands.put((operation, value, pending))
        pending.event.wait()
        if pending.exc is not None:
            raise RuntimeError(f"DLQ {operation} failed") from pending.exc
        return pending.result

    def _writer_loop(self) -> None:
        conn: sqlite3.Connection | None = None
        db_fd: int | None = None
        wal_fd: int | None = None
        try:
            conn = sqlite3.connect(self._path, isolation_level=None)
            version = int(conn.execute("PRAGMA user_version").fetchone()[0])
            if version > _SCHEMA_VERSION:
                raise RuntimeError(
                    f"unsupported DLQ schema version {version}; "
                    f"maximum supported version is {_SCHEMA_VERSION}"
                )
            journal_mode = conn.execute("PRAGMA journal_mode=WAL").fetchone()[0]
            if str(journal_mode).lower() != "wal":
                raise RuntimeError(f"SQLite did not enable WAL mode: {journal_mode!r}")
            conn.execute("PRAGMA synchronous=FULL")
            conn.execute("PRAGMA wal_autocheckpoint=0")
            self._migrate_schema(conn, version)
            db_fd = os.open(self._path, os.O_RDWR)
            wal_fd = os.open(f"{self._path}-wal", os.O_RDWR | os.O_CREAT, 0o600)
            self._started.set()
        except Exception as exc:
            with self._state_lock:
                self._dead = exc
                self._started.set()
            self._close_writer_resources(conn, db_fd, wal_fd)
            return

        try:
            while True:
                item = self._commands.get()
                if item is _SENTINEL:
                    break
                operation, value, pending = item
                try:
                    if operation == "put":
                        pending.result = self._insert(conn, value)
                    elif operation == "remove":
                        conn.execute("DELETE FROM dlq_records WHERE id = ?", (value,))
                    else:  # pragma: no cover - internal invariant
                        raise RuntimeError(f"unknown DLQ operation: {operation}")
                except Exception as exc:
                    pending.exc = exc
                    pending.event.set()
                    raise
                pending.event.set()
        except Exception as exc:
            with self._state_lock:
                self._dead = exc
            self._fail_pending(exc)
        finally:
            self._close_writer_resources(conn, db_fd, wal_fd)

    def _insert(self, conn: sqlite3.Connection, record: dict) -> int:
        persisted = self._redactor.redact_mapping(record)
        encoded = json.dumps(persisted, ensure_ascii=False, sort_keys=True)
        conn.execute("BEGIN IMMEDIATE")
        try:
            cursor = conn.execute(
                _INSERT,
                (
                    _label(persisted.get("status")),
                    _nullable_label(persisted.get("reason")),
                    _nullable_label(persisted.get("failure_kind")),
                    encoded,
                    _nullable_label(persisted.get("ts")),
                ),
            )
            row_id = cursor.lastrowid
            conn.execute(_PRUNE, {"max_entries": self._max_entries})
            conn.execute("COMMIT")
        except Exception:
            conn.execute("ROLLBACK")
            raise
        if row_id is None:
            raise RuntimeError("SQLite did not return the DLQ row id")
        return int(row_id)

    @staticmethod
    def _migrate_schema(conn: sqlite3.Connection, version: int) -> None:
        if version == _SCHEMA_VERSION:
            columns = {row[1] for row in conn.execute("PRAGMA table_info(dlq_records)")}
            expected = {"id", "status", "reason", "failure_kind", "record", "ts"}
            if columns != expected:
                raise RuntimeError("DLQ schema v1 has unexpected columns")
            return
        conn.execute("BEGIN")
        try:
            conn.execute(_CREATE_TABLE)
            conn.execute(f"PRAGMA user_version = {_SCHEMA_VERSION}")
            conn.execute("COMMIT")
        except Exception:
            conn.execute("ROLLBACK")
            raise

    def _read_rows(self, sql: str, parameters: tuple[Any, ...] = ()) -> list[tuple[Any, ...]]:
        with self._state_lock:
            if self._closed:
                raise RuntimeError("DLQ is closed")
            if self._dead is not None:
                raise RuntimeError("DLQ writer is dead") from self._dead
        conn = sqlite3.connect(f"file:{self._path}?mode=ro", uri=True)
        try:
            return conn.execute(sql, parameters).fetchall()
        finally:
            conn.close()

    def _fail_pending(self, exc: BaseException) -> None:
        while True:
            try:
                item = self._commands.get_nowait()
            except queue.Empty:
                return
            if item is _SENTINEL:
                continue
            pending = item[-1]
            pending.exc = exc
            pending.event.set()

    @staticmethod
    def _close_writer_resources(
        conn: sqlite3.Connection | None,
        db_fd: int | None,
        wal_fd: int | None,
    ) -> None:
        if wal_fd is not None:
            os.close(wal_fd)
        if db_fd is not None:
            os.close(db_fd)
        if conn is not None:
            conn.close()

    @staticmethod
    def _decode_record(encoded: str) -> dict:
        record = json.loads(encoded)
        if not isinstance(record, dict):  # pragma: no cover - schema writer invariant
            raise ValueError("dead-letter record is not an object")
        return record

    @staticmethod
    def _validate_row_id(row_id: object) -> int:
        if type(row_id) is not int or row_id <= 0:
            raise ValueError("row_id must be a positive integer")
        return row_id


def _label(value: object) -> str:
    if value is None or value == "":
        return _MISSING
    return value if isinstance(value, str) else str(value)


def _nullable_label(value: object) -> str | None:
    if value is None:
        return None
    return value if isinstance(value, str) else str(value)
