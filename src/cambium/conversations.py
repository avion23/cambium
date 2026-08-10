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

Schema version 2 stores an optional token estimate in ``tokens`` and a node
kind in ``kind``. Summary envelope fields use one JSON ``meta`` column rather
than separate cover columns; :meth:`history` and :meth:`path` return decoded
metadata. Those methods include summary and system rows in the same chain as
turn rows, so consumers that need only turns must filter on ``record["kind"]``.
"""

from __future__ import annotations

import datetime
import json
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
_WRITE_TIMEOUT_S = 30.0
_MAX_CHAIN_DEPTH = 1_000_000
_SCHEMA_VERSION = 2
_SENTINEL = object()
_BRANCH_ROLE = "branch"
_SUMMARY_ROLE = "summary"
_KIND_ORDER = ("turn", "summary", "system")
_VALID_KINDS = frozenset(_KIND_ORDER)

_CREATE_TABLE = """CREATE TABLE IF NOT EXISTS conversations (
    id       INTEGER PRIMARY KEY,
    node_id  TEXT NOT NULL,
    parent_id INTEGER NULL REFERENCES conversations(id),
    turn     INTEGER NOT NULL,
    role     TEXT NOT NULL,
    content  TEXT NOT NULL,
    ts       TEXT NOT NULL,
    seq      INTEGER NOT NULL,
    tokens   INTEGER NULL,
    kind     TEXT NOT NULL DEFAULT 'turn',
    meta     TEXT NULL
)"""
_CREATE_NODE_INDEX = (
    "CREATE INDEX IF NOT EXISTS conversations_node_id_idx "
    "ON conversations(node_id)"
)

_SELECT_HEAD = (
    "SELECT id FROM conversations WHERE node_id = ? "
    "ORDER BY seq DESC, id DESC LIMIT 1"
)
_SELECT_CHAIN = """WITH RECURSIVE chain(id, parent_id, depth) AS (
    SELECT id, parent_id, 0 FROM conversations WHERE id = :head_id
    UNION ALL
    SELECT c.id, c.parent_id, chain.depth + 1
    FROM chain JOIN conversations c ON c.id = chain.parent_id
    WHERE chain.depth < :max_depth
)
SELECT c.id, c.node_id, c.parent_id, c.turn, c.role, c.content,
       c.ts, c.seq, c.tokens, c.kind, c.meta
FROM chain JOIN conversations c ON c.id = chain.id
ORDER BY chain.depth DESC"""
_INSERT_ROW = (
    "INSERT INTO conversations"
    "(node_id, parent_id, turn, role, content, ts, seq, tokens, kind, meta) "
    "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)"
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
        tokens: int | None = None,
        kind: str = "turn",
        meta: dict[str, Any] | None = None,
    ) -> int:
        """Append one message and return its row id.

        When ``parent_id`` is omitted, a non-empty node continues its current
        head.  The first message for a node is a root.  Supplying ``parent_id``
        explicitly permits a cross-node parent and is how callers can attach a
        message to a branch point.

        ``tokens`` is an optional non-negative token estimate.  ``kind`` must
        be ``"turn"``, ``"summary"``, or ``"system"``.  ``meta`` is encoded
        as JSON in the schema and decoded back to a dictionary by readers.

        The call waits for the SQLite commit, not for the periodic fsync.  This
        keeps readers coherent while retaining the store's advisory durability
        class.
        """
        self._validate_node_id(node_id)
        self._validate_role(role)
        self._validate_content(content)
        self._validate_parent_id(parent_id)
        self._validate_tokens(tokens)
        self._validate_kind(kind)
        self._validate_meta(meta)
        return self._submit(
            node_id,
            role,
            content,
            parent_id,
            tokens,
            kind,
            self._encode_meta(meta),
        )

    def add_summary(
        self,
        node_id: str,
        content: str,
        *,
        covers_from: int,
        covers_to: int,
        tokens_before: int,
        tokens_after: int,
    ) -> int:
        """Append a summary node covering the inclusive ``[from, to]`` range.

        The summary's parent is ``covers_to`` and its stored token estimate is
        ``tokens_after``. The full covered chain remains in the append-only
        store; later turns continue after the new summary node.
        """
        self._validate_node_id(node_id)
        self._validate_content(content)
        self._validate_row_id(covers_from, "covers_from")
        self._validate_row_id(covers_to, "covers_to")
        if covers_from > covers_to:
            raise ValueError("covers_from must not be greater than covers_to")
        self._validate_tokens(tokens_before, name="tokens_before", allow_none=False)
        self._validate_tokens(tokens_after, name="tokens_after", allow_none=False)
        return self.append(
            node_id,
            _SUMMARY_ROLE,
            content,
            parent_id=covers_to,
            tokens=tokens_after,
            kind="summary",
            meta={
                "covers_from": covers_from,
                "covers_to": covers_to,
                "tokens_before": tokens_before,
                "tokens_after": tokens_after,
            },
        )

    def branch(self, node_id: str, from_id: int) -> int:
        """Create a branch marker for ``node_id`` below conversation row ``from_id``.

        Only the new marker row is inserted.  The shared prefix is not copied;
        :meth:`history` and :meth:`path` follow the marker's ``parent_id`` to
        reconstruct it.  The returned id is the branch marker and can be used
        as the explicit ``parent_id`` for the first branch message.
        """
        self._validate_node_id(node_id)
        self._validate_parent_id(from_id)
        return self._submit(node_id, _BRANCH_ROLE, "", from_id, None, "turn", None)

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

    def token_accounting(self, node_id: str) -> dict[str, Any]:
        """Return token totals and the latest summary's reduction envelope.

        Totals cover the active head chain, with NULL token values omitted.
        The returned dictionary has ``tokens_by_kind`` for the three supported
        kinds, ``reduction`` for the latest summary, and ``covered_range`` with
        ``from``/``to`` ids. Nodes without a summary return ``None`` for the
        latter two values.
        """
        self._validate_node_id(node_id)
        with closing(self._reader()) as conn:
            head = conn.execute(_SELECT_HEAD, (node_id,)).fetchone()
            records = [] if head is None else self._chain(conn, int(head[0]))

        tokens_by_kind = dict.fromkeys(_KIND_ORDER, 0)
        for record in records:
            tokens = record["tokens"]
            if tokens is not None:
                tokens_by_kind[record["kind"]] = tokens_by_kind.get(record["kind"], 0) + int(
                    tokens
                )

        latest_summary = next(
            (record for record in reversed(records) if record["kind"] == "summary"),
            None,
        )
        reduction: int | None = None
        covered_range: dict[str, int] | None = None
        if latest_summary is not None and isinstance(latest_summary["meta"], dict):
            meta = latest_summary["meta"]
            values = (
                meta.get("covers_from"),
                meta.get("covers_to"),
                meta.get("tokens_before"),
                meta.get("tokens_after"),
            )
            if all(isinstance(value, int) and not isinstance(value, bool) for value in values):
                covered_range = {"from": values[0], "to": values[1]}
                reduction = values[2] - values[3]

        return {
            "tokens_by_kind": tokens_by_kind,
            "reduction": reduction,
            "covered_range": covered_range,
        }

    def close(self) -> None:
        """Drain queued writes, fsync the WAL/database, and stop the writer."""
        with self._lock:
            if not self._closed:
                self._closed = True
                self._queue.put_nowait(_SENTINEL)
        self._thread.join()
        if self._dead is not None:
            raise ConversationStoreError("conversation store failed while closing") from self._dead

    def _submit(
        self,
        node_id: str,
        role: str,
        content: str,
        parent_id: int | None,
        tokens: int | None,
        kind: str,
        meta: str | None,
    ) -> int:
        pending = _Pending()
        with self._lock:
            if self._dead is not None:
                raise ConversationStoreError("conversation store is dead") from self._dead
            if self._closed:
                raise RuntimeError("ConversationStore is closed")
            self._queue.put_nowait((node_id, role, content, parent_id, tokens, kind, meta, pending))

        if not pending.event.wait(_WRITE_TIMEOUT_S):
            raise ConversationStoreError(
                f"conversation write did not complete within {_WRITE_TIMEOUT_S}s"
            )
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
            self._migrate_schema(conn)
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

                node_id, role, content, parent_id, tokens, kind, meta, pending = item
                try:
                    pending.result = self._insert_row(
                        conn, node_id, role, content, parent_id, tokens, kind, meta
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
        tokens: int | None,
        kind: str,
        meta: str | None,
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
            (
                node_id,
                effective_parent_id,
                turn,
                role,
                content,
                ts,
                self._next_seq,
                tokens,
                kind,
                meta,
            ),
        )
        row_id = cursor.lastrowid
        if row_id is None:
            raise RuntimeError("SQLite did not return the conversation row id")
        self._next_seq += 1
        return int(row_id)

    @staticmethod
    def _migrate_schema(conn: sqlite3.Connection) -> None:
        version = int(conn.execute("PRAGMA user_version").fetchone()[0])
        if version > _SCHEMA_VERSION:
            raise RuntimeError(
                f"unsupported conversation schema version {version}; "
                f"maximum supported version is {_SCHEMA_VERSION}"
            )

        columns = {row[1] for row in conn.execute("PRAGMA table_info(conversations)")}
        required = {
            "tokens": "tokens INTEGER",
            "kind": "kind TEXT NOT NULL DEFAULT 'turn'",
            "meta": "meta TEXT",
        }
        missing = [
            (name, definition)
            for name, definition in required.items()
            if name not in columns
        ]
        if version >= _SCHEMA_VERSION and missing:
            names = ", ".join(name for name, _ in missing)
            raise RuntimeError(f"conversation schema v2 is missing columns: {names}")

        conn.execute("BEGIN")
        try:
            for _, definition in missing:
                conn.execute(f"ALTER TABLE conversations ADD COLUMN {definition}")
            conn.execute(f"PRAGMA user_version = {_SCHEMA_VERSION}")
            conn.execute("COMMIT")
        except Exception:
            conn.execute("ROLLBACK")
            raise

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
        rows = conn.execute(
            _SELECT_CHAIN,
            {"head_id": head_id, "max_depth": _MAX_CHAIN_DEPTH},
        ).fetchall()
        deepest = rows[0]
        if deepest[2] is not None:
            if deepest[2] in {row[0] for row in rows}:
                raise ConversationStoreError(
                    f"cycle in conversation parent chain at id {deepest[2]}"
                )
            raise ConversationStoreError(
                f"conversation parent id does not exist: {deepest[2]}"
            )

        if stop_id is not None:
            stop_index = next(
                (index for index, row in enumerate(rows) if int(row[0]) == stop_id),
                None,
            )
            if stop_index is None:
                raise ValueError(
                    f"conversation id is not on node {head_id}'s path: {stop_id}"
                )
            rows = rows[: stop_index + 1]

        return [cls._row_to_dict(row) for row in rows]

    @staticmethod
    def _row_to_dict(row: tuple[Any, ...]) -> dict[str, Any]:
        row_id, node_id, parent_id, turn, role, content, ts, seq, tokens, kind, meta = row
        return {
            "id": int(row_id),
            "node_id": node_id,
            "parent_id": None if parent_id is None else int(parent_id),
            "turn": int(turn),
            "role": role,
            "content": content,
            "ts": ts,
            "seq": int(seq),
            "tokens": None if tokens is None else int(tokens),
            "kind": kind,
            "meta": ConversationStore._decode_meta(meta),
        }

    @staticmethod
    def _decode_meta(meta: Any) -> dict[str, Any] | None:
        if meta is None:
            return None
        try:
            decoded = json.loads(meta)
        except (TypeError, ValueError) as exc:
            raise ConversationStoreError("conversation row has invalid JSON metadata") from exc
        if not isinstance(decoded, dict):
            raise ConversationStoreError("conversation row metadata must be a JSON object")
        return decoded

    @staticmethod
    def _encode_meta(meta: dict[str, Any] | None) -> str | None:
        if meta is None:
            return None
        return json.dumps(meta, separators=(",", ":"), sort_keys=True)

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
        if parent_id is not None:
            ConversationStore._validate_row_id(parent_id, "parent_id")

    @staticmethod
    def _validate_row_id(value: int, name: str) -> None:
        if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
            raise ValueError(f"{name} must be a positive integer")

    @staticmethod
    def _validate_tokens(
        tokens: int | None,
        *,
        name: str = "tokens",
        allow_none: bool = True,
    ) -> None:
        if tokens is None:
            if allow_none:
                return
            raise ValueError(f"{name} must be a non-negative integer")
        if isinstance(tokens, bool) or not isinstance(tokens, int) or tokens < 0:
            raise ValueError(f"{name} must be a non-negative integer or None")

    @staticmethod
    def _validate_kind(kind: str) -> None:
        if not isinstance(kind, str) or kind not in _VALID_KINDS:
            raise ValueError(f"kind must be one of {sorted(_VALID_KINDS)!r}")

    @staticmethod
    def _validate_meta(meta: dict[str, Any] | None) -> None:
        if meta is not None and not isinstance(meta, dict):
            raise TypeError("meta must be a dictionary or None")

    @staticmethod
    def _validate_tail(tail: int | None) -> None:
        if tail is not None and (
            isinstance(tail, bool) or not isinstance(tail, int) or tail < 0
        ):
            raise ValueError("tail must be a non-negative integer or None")
