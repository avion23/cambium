"""Branchable per-node conversation history backed by SQLite WAL.

``ConversationStore`` owns one SQLite write connection in one dedicated writer
thread.  Calls to :meth:`append` and :meth:`branch` wait until their row is
committed to SQLite, so a following read sees the row, but they do not wait for
an fsync.  Conversation records are advisory: the writer checkpoints and
fsyncs the database on the configured cadence and always does so during
:meth:`close`.

``branch`` inserts a branch marker row for ``node_id`` whose ``parent_id`` is
``from_id``.  It does not copy the prefix.  Readers reconstruct the shared
prefix by following ``parent_id`` links, so a branch from turn N starts with
the rows through that turn and then its own marker/future messages.  The
marker uses ``role="branch"`` and an empty content string because the schema
requires message-shaped, non-null role and content columns.
"""

from __future__ import annotations

import datetime
import os
import queue
import sqlite3
import threading
import time
from contextlib import closing
from pathlib import Path
from typing import Any

_WRITER_BUSY_TIMEOUT_MS = 5000
_STARTUP_TIMEOUT_S = 10.0
_SENTINEL = object()
_BRANCH_ROLE = "branch"

_CREATE_TABLE = """CREATE TABLE IF NOT EXISTS conversations (
    id       INTEGER PRIMARY KEY,
    node_id  TEXT NOT NULL,
    parent_id INTEGER NULL REFERENCES conversations(id),
    turn     INTEGER NOT NULL,
    role     TEXT NOT NULL,
    content  TEXT NOT NULL,
    ts       TEXT NOT NULL,
    seq      INTEGER NOT NULL
)"""
_CREATE_NODE_INDEX = (
    "CREATE INDEX IF NOT EXISTS conversations_node_id_idx "
    "ON conversations(node_id)"
)

_SELECT_HEAD = (
    "SELECT id FROM conversations WHERE node_id = ? "
    "ORDER BY seq DESC, id DESC LIMIT 1"
)
_SELECT_ROW = (
    "SELECT id, node_id, parent_id, turn, role, content, ts, seq "
    "FROM conversations WHERE id = ?"
)
_INSERT_ROW = (
    "INSERT INTO conversations(node_id, parent_id, turn, role, content, ts, seq) "
    "VALUES (?, ?, ?, ?, ?, ?, ?)"
)


class ConversationStoreError(Exception):
    """The conversation store cannot accept or complete an operation."""


class ConversationStoreInitError(ConversationStoreError):
    """The conversation store failed to initialize."""


class _Pending:
    """Completion state for one queued write."""

    __slots__ = ("event", "result", "exc")

    def __init__(self) -> None:
        self.event = threading.Event()
        self.result: int | None = None
        self.exc: BaseException | None = None


class ConversationStore:
    """A queryable conversation tree with one SQLite writer thread."""

    def __init__(self, db_path: Path, *, fsync_interval_s: float = 1.0) -> None:
        if fsync_interval_s < 0:
            raise ValueError("fsync_interval_s must be non-negative")

        self._path = Path(db_path)
        self._path.parent.mkdir(parents=True, exist_ok=True)
        self._fsync_interval_s = fsync_interval_s
        self._queue: queue.Queue[Any] = queue.Queue()
        self._lock = threading.Lock()
        self._closed = False
        self._dead: BaseException | None = None
        self._next_seq = 1
        self._started = threading.Event()
        self._thread = threading.Thread(
            target=self._writer_loop,
            name="cambium-conversation-store",
            daemon=True,
        )
        self._thread.start()
        if not self._started.wait(_STARTUP_TIMEOUT_S):
            raise ConversationStoreInitError(
                f"conversation store did not start within {_STARTUP_TIMEOUT_S}s: {self._path}"
            )
        if self._dead is not None:
            raise ConversationStoreInitError(
                f"conversation store failed to initialize: {self._path}"
            ) from self._dead

    def append(
        self,
        node_id: str,
        role: str,
        content: str,
        *,
        parent_id: int | None = None,
    ) -> int:
        """Append one message and return its row id.

        When ``parent_id`` is omitted, a non-empty node continues its current
        head.  The first message for a node is a root.  Supplying ``parent_id``
        explicitly permits a cross-node parent and is how callers can attach a
        message to a branch point.

        The call waits for the SQLite commit, not for the periodic fsync.  This
        keeps readers coherent while retaining the store's advisory durability
        class.
        """
        self._validate_node_id(node_id)
        self._validate_role(role)
        self._validate_content(content)
        self._validate_parent_id(parent_id)
        return self._submit(node_id, role, content, parent_id)

    def branch(self, node_id: str, from_id: int) -> int:
        """Create a branch marker for ``node_id`` below conversation row ``from_id``.

        Only the new marker row is inserted.  The shared prefix is not copied;
        :meth:`history` and :meth:`path` follow the marker's ``parent_id`` to
        reconstruct it.  The returned id is the branch marker and can be used
        as the explicit ``parent_id`` for the first branch message.
        """
        self._validate_node_id(node_id)
        self._validate_parent_id(from_id)
        return self._submit(node_id, _BRANCH_ROLE, "", from_id)

    def history(self, node_id: str, *, tail: int | None = None) -> list[dict[str, Any]]:
        """Return the current node head's chain from root to head.

        Parent links can cross node ids, which is what makes a branch cheap.
        The result is ordered from root to head by chain depth and turn.  When
        ``tail`` is set, only the last ``tail`` rows are returned; ``tail=0``
        returns an empty list.
        """
        self._validate_node_id(node_id)
        self._validate_tail(tail)
        with closing(self._reader()) as conn:
            head = conn.execute(_SELECT_HEAD, (node_id,)).fetchone()
            if head is None:
                return []
            records = self._chain(conn, int(head[0]))
        if tail is not None:
            return records[-tail:] if tail else []
        return records

    def path(self, node_id: str, to_id: int) -> list[dict[str, Any]]:
        """Return the active ``node_id`` chain from its root through ``to_id``.

        ``to_id`` may be an ancestor owned by another node id.  It must be on
        the active head's parent chain; otherwise the requested point is not a
        path in this node's current branch.
        """
        self._validate_node_id(node_id)
        self._validate_parent_id(to_id)
        with closing(self._reader()) as conn:
            head = conn.execute(_SELECT_HEAD, (node_id,)).fetchone()
            if head is None:
                raise ValueError(f"node_id has no conversation rows: {node_id!r}")
            return self._chain(conn, int(head[0]), stop_id=to_id)

    def close(self) -> None:
        """Drain queued writes, fsync the WAL/database, and stop the writer."""
        with self._lock:
            if not self._closed:
                self._closed = True
                self._queue.put_nowait(_SENTINEL)
        self._thread.join()

    def _submit(
        self,
        node_id: str,
        role: str,
        content: str,
        parent_id: int | None,
    ) -> int:
        pending = _Pending()
        with self._lock:
            if self._dead is not None:
                raise ConversationStoreError("conversation store is dead") from self._dead
            if self._closed:
                raise RuntimeError("ConversationStore is closed")
            self._queue.put_nowait((node_id, role, content, parent_id, pending))

        pending.event.wait()
        if pending.exc is not None:
            if isinstance(pending.exc, ValueError):
                raise pending.exc
            raise ConversationStoreError("conversation store failed while appending") from (
                pending.exc
            )
        if pending.result is None:
            raise ConversationStoreError("conversation writer completed without a row id")
        return pending.result

    def _writer_loop(self) -> None:
        conn: sqlite3.Connection | None = None
        db_fd: int | None = None
        wal_fd: int | None = None
        try:
            conn = sqlite3.connect(self._path, isolation_level=None)
            conn.execute(f"PRAGMA busy_timeout={_WRITER_BUSY_TIMEOUT_MS}").fetchall()
            conn.execute("PRAGMA foreign_keys=ON")
            journal_mode = conn.execute("PRAGMA journal_mode=WAL").fetchone()[0]
            if str(journal_mode).lower() != "wal":
                raise RuntimeError(f"SQLite did not enable WAL mode: {journal_mode!r}")
            conn.execute("PRAGMA synchronous=NORMAL")
            conn.execute("PRAGMA wal_autocheckpoint=0")
            conn.execute(_CREATE_TABLE)
            conn.execute(_CREATE_NODE_INDEX)

            db_fd = os.open(self._path, os.O_RDWR)
            wal_fd = os.open(
                f"{self._path}-wal",
                os.O_RDWR | os.O_CREAT,
                0o600,
            )
            self._conn = conn
            self._db_fd = db_fd
            self._wal_fd = wal_fd
            self._next_seq = conn.execute(
                "SELECT COALESCE(MAX(seq), 0) + 1 FROM conversations"
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
        try:
            while True:
                remaining = next_fsync - time.monotonic()
                try:
                    item = self._queue.get(timeout=max(remaining, 0.0))
                except queue.Empty:
                    if dirty:
                        self._fsync_now()
                        dirty = False
                    next_fsync = time.monotonic() + self._fsync_interval_s
                    continue

                if item is _SENTINEL:
                    break

                node_id, role, content, parent_id, pending = item
                try:
                    pending.result = self._insert_row(
                        conn, node_id, role, content, parent_id
                    )
                except ValueError as exc:
                    pending.exc = exc
                    pending.event.set()
                    continue
                except Exception as exc:
                    pending.exc = exc
                    pending.event.set()
                    raise

                pending.event.set()
                dirty = True
                if self._fsync_interval_s == 0 or time.monotonic() >= next_fsync:
                    self._fsync_now()
                    dirty = False
                    next_fsync = time.monotonic() + self._fsync_interval_s
        except Exception as exc:
            with self._lock:
                self._dead = exc
            self._fail_pending(exc)
        finally:
            if self._dead is None:
                try:
                    self._fsync_now()
                except Exception as exc:
                    with self._lock:
                        self._dead = exc
                    self._fail_pending(exc)
            if wal_fd is not None:
                os.close(wal_fd)
            if db_fd is not None:
                os.close(db_fd)
            if conn is not None:
                conn.close()

    def _insert_row(
        self,
        conn: sqlite3.Connection,
        node_id: str,
        role: str,
        content: str,
        parent_id: int | None,
    ) -> int:
        if parent_id is None:
            parent = conn.execute(
                "SELECT id, turn FROM conversations WHERE node_id = ? "
                "ORDER BY seq DESC, id DESC LIMIT 1",
                (node_id,),
            ).fetchone()
            effective_parent_id = None if parent is None else int(parent[0])
        else:
            parent = conn.execute(
                "SELECT id, turn FROM conversations WHERE id = ?",
                (parent_id,),
            ).fetchone()
            if parent is None:
                raise ValueError(f"parent conversation id does not exist: {parent_id}")
            effective_parent_id = parent_id

        turn = 1 if parent is None else int(parent[1]) + 1
        ts = datetime.datetime.now(datetime.UTC).isoformat(timespec="microseconds")
        cursor = conn.execute(
            _INSERT_ROW,
            (node_id, effective_parent_id, turn, role, content, ts, self._next_seq),
        )
        row_id = cursor.lastrowid
        if row_id is None:
            raise RuntimeError("SQLite did not return the conversation row id")
        self._next_seq += 1
        return int(row_id)

    def _fail_pending(self, exc: BaseException) -> None:
        while True:
            try:
                item = self._queue.get_nowait()
            except queue.Empty:
                break
            if item is _SENTINEL:
                continue
            pending = item[-1]
            pending.exc = exc
            pending.event.set()

    def _fsync_now(self) -> None:
        cursor = self._conn.execute("PRAGMA wal_checkpoint(TRUNCATE)")
        cursor.fetchone()
        cursor.close()
        os.fsync(self._wal_fd)
        os.fsync(self._db_fd)

    def _reader(self) -> sqlite3.Connection:
        conn = sqlite3.connect(self._path, timeout=_WRITER_BUSY_TIMEOUT_MS / 1000)
        conn.execute(f"PRAGMA busy_timeout={_WRITER_BUSY_TIMEOUT_MS}").fetchall()
        conn.execute("PRAGMA foreign_keys=ON")
        return conn

    @classmethod
    def _chain(
        cls,
        conn: sqlite3.Connection,
        head_id: int,
        *,
        stop_id: int | None = None,
    ) -> list[dict[str, Any]]:
        rows: list[tuple[Any, ...]] = []
        seen: set[int] = set()
        current_id: int | None = head_id
        stop_index: int | None = None
        while current_id is not None:
            if current_id in seen:
                raise ConversationStoreError(
                    f"cycle in conversation parent chain at id {current_id}"
                )
            seen.add(current_id)
            row = conn.execute(_SELECT_ROW, (current_id,)).fetchone()
            if row is None:
                raise ConversationStoreError(
                    f"conversation parent id does not exist: {current_id}"
                )
            rows.append(row)
            if stop_id is not None and int(row[0]) == stop_id:
                stop_index = len(rows) - 1
            current_id = row[2]

        if stop_id is not None and stop_index is None:
            raise ValueError(f"conversation id is not on node {head_id}'s path: {stop_id}")
        if stop_index is not None:
            rows = rows[stop_index:]

        rows.reverse()
        return [cls._row_to_dict(row) for row in rows]

    @staticmethod
    def _row_to_dict(row: tuple[Any, ...]) -> dict[str, Any]:
        row_id, node_id, parent_id, turn, role, content, ts, seq = row
        return {
            "id": int(row_id),
            "node_id": node_id,
            "parent_id": None if parent_id is None else int(parent_id),
            "turn": int(turn),
            "role": role,
            "content": content,
            "ts": ts,
            "seq": int(seq),
        }

    @staticmethod
    def _validate_node_id(node_id: str) -> None:
        if not isinstance(node_id, str) or not node_id:
            raise ValueError("node_id must be a non-empty string")

    @staticmethod
    def _validate_role(role: str) -> None:
        if not isinstance(role, str) or not role:
            raise ValueError("role must be a non-empty string")

    @staticmethod
    def _validate_content(content: str) -> None:
        if not isinstance(content, str):
            raise TypeError("content must be a string")

    @staticmethod
    def _validate_parent_id(parent_id: int | None) -> None:
        if parent_id is not None and (
            isinstance(parent_id, bool) or not isinstance(parent_id, int) or parent_id <= 0
        ):
            raise ValueError("parent_id must be a positive integer or None")

    @staticmethod
    def _validate_tail(tail: int | None) -> None:
        if tail is not None and (
            isinstance(tail, bool) or not isinstance(tail, int) or tail < 0
        ):
            raise ValueError("tail must be a non-negative integer or None")
