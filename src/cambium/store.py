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
- **Dropped incoming events do not receive a ``seq``.** Sequence reservation
  happens only after queue admission, so a dropped tail cannot be reused after
  restart. Critical admission can still evict an older non-critical queue item;
  that item already has a sequence and the eviction is counted.
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
- **Phantom read.** An accepted non-critical append returns a reserved ``seq``
  whose row may not be durable yet: ``events_after(seq)`` may not observe it,
  and a crash inside ``fsync_interval_s`` can lose it. Callers must tolerate
  both. A dropped non-critical append returns ``None``.
- **Writer death is fatal.** Any error in the writer thread (sqlite/fsync/disk)
  marks the store dead: pending appends raise ``StoreError``, pending events are
  lost, and the supervisor must treat store death as fatal.

Event rows and WAL checkpoints use the writer thread's connection; eviction
reservations use a short-lived, full-synchronous metadata connection before a
replacement can enter the queue. Readers use their own short-lived connections.

Redaction is optional for backward compatibility: when an event store is
constructed with a ``cambium.redact.Redactor``, the event envelope uses its
explicit protocol redaction API before it enters the bounded queue and again
immediately before the INSERT in the writer; nested payloads use generic
recursive redaction. Without one, the event is persisted unchanged.
``build_session_redactor`` is the single place a session constructs the shared
redactor.
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
from collections.abc import Callable
from pathlib import Path
from typing import Any

from .redact import EVENT_RECORD_STRUCTURAL_FIELDS, Redactor

logger = logging.getLogger(__name__)

CRITICAL_KINDS = frozenset({
    "result", "checkpoint", "worker_exit", "task_failed",
    "merge_progress", "task_assigned", "merge_committed",
    "merge_staging_quarantined", "merge_staging_cleanup_failed",
    "merge_staging_prune_started", "merge_staging_pruned",
    "context_checkpoint", "context_fork", "context_fork_skipped",
    "context_resume", "context_resume_failed", "child_admitted",
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

_SEQUENCE_SCHEMA = """CREATE TABLE IF NOT EXISTS event_store_state (
    id       INTEGER PRIMARY KEY CHECK (id = 1),
    next_seq INTEGER NOT NULL
)"""

_INSERT = (
    "INSERT INTO events(seq, kind, payload, ts, monotonic_ms, task_id, "
    "worker_id, generation, request_id) VALUES(?, ?, ?, ?, ?, ?, ?, ?, ?)"
)

_SELECT_AFTER = (
    "SELECT seq, kind, payload, ts, monotonic_ms, task_id, worker_id, "
    "generation, request_id FROM events WHERE seq > ? ORDER BY seq"
)
_SELECT_NEXT_SEQ = "SELECT next_seq FROM event_store_state WHERE id = 1"
_INSERT_NEXT_SEQ = "INSERT INTO event_store_state(id, next_seq) VALUES(1, ?)"
_UPDATE_NEXT_SEQ = (
    "UPDATE event_store_state SET next_seq = MAX(next_seq, ?) WHERE id = 1"
)

_WRITER_BUSY_TIMEOUT_MS = 5000
# Checkpoints poll busy readers; a short per-call busy wait keeps the retry
# loop (and the critical-append deadline) in control of total wait time.
_CHECKPOINT_BUSY_TIMEOUT_MS = 20
_CHECKPOINT_RETRY_SLEEP_S = 0.02
_SEQUENCE_PERSIST_RETRY_SLEEP_S = 0.01
# The close join must cover the writer's own busy-checkpoint retry budget
# (``checkpoint_busy_retry_s``): a WAL busy at close (a reader overlapping a
# checkpoint) legitimately holds the writer for up to that budget, and a
# shorter close deadline would fail a healthy session spuriously.
_CLOSE_JOIN_TIMEOUT_S = 12.0
_CLOSE_STOP_JOIN_TIMEOUT_S = 0.1

_SENTINEL = object()
_TIMER = object()


def read_events_file(db_path: Path | str, after_seq: int = 0) -> list[dict[str, Any]]:
    """Read durable events without creating or modifying store state."""
    path = Path(db_path)
    if not path.is_file():
        return []
    conn = sqlite3.connect(f"{path.resolve().as_uri()}?mode=ro", uri=True)
    try:
        conn.execute("PRAGMA busy_timeout=5000")
        rows = conn.execute(_SELECT_AFTER, (after_seq,)).fetchall()
    finally:
        conn.close()
    return [EventStore._row_to_event(row) for row in rows]


def _make_private_dir(path: Path) -> None:
    """Ensure a session-owned directory is not readable by other local users."""
    try:
        os.chmod(path, 0o700)
    except OSError:
        # Best effort: a pre-existing directory with a restrictive parent may
        # refuse chmod; the DB/DLQ files below are still forced private.
        pass


def _make_private_db_file(path: Path) -> None:
    """Pre-create the SQLite DB as mode 0600 so WAL/SHM sidecars inherit it.

    SQLite derives ``-wal``/``-shm`` files with the same mode as the database
    when it already exists, so creating the DB privately before ``connect``
    keeps all three files private under a normal 0022 umask.
    """
    fd = os.open(path, os.O_RDWR | os.O_CREAT | os.O_CLOEXEC, 0o600)
    try:
        os.fchmod(fd, 0o600)
    finally:
        os.close(fd)
    for suffix in ("-wal", "-shm"):
        sidecar = Path(f"{path}{suffix}")
        if sidecar.exists():
            try:
                os.chmod(sidecar, 0o600)
            except OSError:
                pass


class _AdmissionCancelled(Exception):
    """A close started while an append was waiting for queue admission."""


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

    ``on_evict`` runs after an accepted non-critical item is removed and before
    the replacement is admitted, without holding the queue condition. The
    removed item keeps a reserved queue slot until the callback succeeds; a
    callback failure restores it. ``on_admit`` runs only after there is space.
    This lets the caller persist an evicted sequence before reserving the next
    one without blocking queue consumers or shutdown.
    """

    __slots__ = ("_items", "_maxsize", "_cond", "_pending_evictions")

    def __init__(self, maxsize: int) -> None:
        self._items: deque[Any] = collections.deque()
        self._maxsize = maxsize
        self._cond = threading.Condition()
        self._pending_evictions: deque[Any] = deque()

    def put(
        self,
        item: Any,
        *,
        critical: bool,
        timeout: float,
        deadline: float | None = None,
        evict_noncritical: bool = True,
        on_admit: Callable[[], Any] | None = None,
        on_evict: Callable[[Any, float], None] | None = None,
        cancel: threading.Event | None = None,
        check: Callable[[], None] | None = None,
        dropped_holder: list[int] | None = None,
    ) -> int:
        deadline = time.monotonic() + timeout if deadline is None else deadline

        def check_state() -> None:
            if cancel is not None and cancel.is_set():
                raise _AdmissionCancelled
            if check is not None:
                check()

        def full() -> bool:
            return len(self._items) + len(self._pending_evictions) >= self._maxsize

        with self._cond:
            check_state()
            if not critical:
                if full():
                    check_state()
                    return 1
                check_state()
                self._items.append(on_admit() if on_admit is not None else item)
                self._cond.notify()
                return 0

        dropped = 0
        while True:
            with self._cond:
                check_state()
                if not full():
                    check_state()
                    self._items.append(on_admit() if on_admit is not None else item)
                    self._cond.notify()
                    return dropped

                evicted = (
                    self._evict_oldest_noncritical()
                    if evict_noncritical
                    else None
                )
                if evicted is None:
                    remaining = deadline - time.monotonic()
                    if remaining <= 0:
                        raise queue.Full
                    self._cond.wait(remaining)
                    continue
                self._pending_evictions.append(evicted)

            try:
                if on_evict is not None:
                    on_evict(evicted, deadline)
            except BaseException:
                with self._cond:
                    if self._remove_pending_eviction(evicted):
                        self._restore_evicted(evicted)
                    self._cond.notify_all()
                raise

            with self._cond:
                if not self._remove_pending_eviction(evicted):
                    self._cond.notify_all()
                    check_state()
                    raise StoreError("event store eviction was drained")
                dropped += 1
                if dropped_holder is not None:
                    dropped_holder[0] = dropped
                try:
                    check_state()
                    self._items.append(on_admit() if on_admit is not None else item)
                except BaseException:
                    self._cond.notify_all()
                    raise
                self._cond.notify()
                return dropped

    def get(self, timeout: float, *, stop_event: threading.Event | None = None) -> Any:
        deadline = time.monotonic() + timeout
        with self._cond:
            while not self._items:
                if stop_event is not None and stop_event.is_set():
                    raise queue.Empty
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

    def wake(self) -> None:
        with self._cond:
            self._cond.notify_all()

    def drain(self) -> list[Any]:
        with self._cond:
            items = list(self._items)
            items.extend(self._pending_evictions)
            self._items.clear()
            self._pending_evictions.clear()
            self._cond.notify_all()
            return items

    def _evict_oldest_noncritical(self) -> Any | None:
        for i, item in enumerate(self._items):
            if isinstance(item, tuple) and item[1] not in CRITICAL_KINDS:
                del self._items[i]
                return item
        return None

    def _restore_evicted(self, item: Any) -> None:
        evicted_seq = item[0]
        for i, queued in enumerate(self._items):
            if queued is _SENTINEL or (
                isinstance(queued, tuple) and queued[0] > evicted_seq
            ):
                self._items.insert(i, item)
                return
        self._items.append(item)

    def _remove_pending_eviction(self, item: Any) -> bool:
        for i, pending in enumerate(self._pending_evictions):
            if pending is item:
                del self._pending_evictions[i]
                return True
        return False


class EventStore:
    """Append-only SQLite WAL event log with a sole event-row writer thread."""

    def __init__(
        self,
        path: Path,
        *,
        fsync_interval_s: float = 1.0,
        startup_timeout_s: float = 10.0,
        max_queue_size: int = 10_000,
        critical_timeout_s: float = 10.0,
        checkpoint_busy_retry_s: float = 10.0,
        redactor: Redactor | None = None,
    ) -> None:
        if max_queue_size < 1:
            raise ValueError("max_queue_size must be >= 1")
        if critical_timeout_s <= 0:
            raise ValueError("critical_timeout_s must be > 0")
        if checkpoint_busy_retry_s <= 0:
            raise ValueError("checkpoint_busy_retry_s must be > 0")
        if redactor is not None and not isinstance(redactor, Redactor):
            raise TypeError("redactor must be a cambium.redact.Redactor")
        self._path = Path(path)
        self._path.parent.mkdir(parents=True, exist_ok=True)
        _make_private_dir(self._path.parent)
        _make_private_db_file(self._path)
        self._fsync_interval_s = fsync_interval_s
        self._critical_timeout_s = critical_timeout_s
        self._checkpoint_busy_retry_s = checkpoint_busy_retry_s
        self._redactor = redactor
        self._queue: _BoundedEventQueue = _BoundedEventQueue(max_queue_size)
        self._lock = threading.Lock()
        self._close_lock = threading.Lock()
        self._admission_cond = threading.Condition(self._lock)
        self._active_admissions = 0
        self._closed = False
        self._close_requested = threading.Event()
        self._stop_requested = threading.Event()
        self._dead: BaseException | None = None
        self._close_error: BaseException | None = None
        self._dropped = 0
        self._pending_sequence_high_water: int | None = None
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
        """Number of non-critical events lost to overflow or forced shutdown."""
        with self._lock:
            return self._dropped

    def append(self, event: dict[str, Any]) -> int | None:
        kind = event.get("kind")
        if not isinstance(kind, str) or not kind:
            raise ValueError("event requires a non-empty string 'kind'")
        if self._redactor is not None:
            event = self._redactor.redact_protocol_record(
                event, structural_fields=EVENT_RECORD_STRUCTURAL_FIELDS
            )
            kind = event.get("kind")
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
        seq_holder: list[int] = []
        dropped_holder = [0]

        with self._lock:
            if self._dead is not None:
                raise StoreError("event store is dead") from self._dead
            if self._closed:
                raise RuntimeError("EventStore is closed")
            self._active_admissions += 1

        def admit() -> tuple[int, str, tuple, _Pending]:
            with self._lock:
                if self._dead is not None:
                    raise StoreError("event store is dead") from self._dead
                if self._closed:
                    raise _AdmissionCancelled
                seq = self._next_seq
                self._next_seq += 1
            seq_holder.append(seq)
            return (seq, kind, row, pending)

        try:
            remaining = max(deadline - time.monotonic(), 0.0)
            dropped = self._queue.put(
                None,
                critical=critical,
                timeout=remaining,
                deadline=deadline,
                on_admit=admit,
                on_evict=self._reserve_evicted_sequence,
                cancel=self._close_requested,
                check=self._check_writer_alive,
                dropped_holder=dropped_holder,
            )
        except BaseException as exc:
            if dropped_holder[0]:
                self._record_dropped(dropped_holder[0])
                logger.warning(
                    "event store overflow: dropped %d non-critical event(s) "
                    "before failed admission kind=%r",
                    dropped_holder[0],
                    kind,
                )
            if isinstance(exc, _AdmissionCancelled):
                raise RuntimeError("EventStore is closed") from None
            if isinstance(exc, queue.Full):
                raise StoreTimeout(
                    f"critical event {kind!r} not enqueued within "
                    f"{self._critical_timeout_s}s (writer stalled)"
                ) from None
            raise
        finally:
            with self._lock:
                self._active_admissions -= 1
                if self._active_admissions == 0:
                    self._admission_cond.notify_all()

        if not seq_holder:
            self._record_dropped(1)
            logger.warning(
                "event store overflow: dropped incoming non-critical event kind=%r",
                kind,
            )
            return None

        seq = seq_holder[0]
        if dropped:
            self._record_dropped(dropped)
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

    def _redact_row(self, row: tuple) -> tuple:
        if self._redactor is None:
            return row
        try:
            payload = json.loads(row[0])
        except (TypeError, ValueError):
            # Undecodable payloads are left untouched; substring redaction of
            # the encoded text would corrupt them further.
            payload = row[0]
        else:
            payload = json.dumps(self._redactor.redact_mapping(payload))
        metadata = self._redactor.redact_protocol_record(
            {
                "ts": row[1],
                "monotonic_ms": row[2],
                "task_id": row[3],
                "worker_id": row[4],
                "generation": row[5],
                "request_id": row[6],
            },
            structural_fields=EVENT_RECORD_STRUCTURAL_FIELDS,
        )
        return (
            payload,
            metadata["ts"],
            metadata["monotonic_ms"],
            metadata["task_id"],
            metadata["worker_id"],
            metadata["generation"],
            metadata["request_id"],
        )

    def _record_dropped(self, count: int) -> None:
        if count <= 0:
            return
        with self._lock:
            self._dropped += count

    def _remember_sequence_high_water(self, next_seq: int) -> None:
        with self._lock:
            if (
                self._pending_sequence_high_water is None
                or next_seq > self._pending_sequence_high_water
            ):
                self._pending_sequence_high_water = next_seq

    def _persist_pending_sequence_high_water(self, deadline: float | None = None) -> bool:
        if deadline is None:
            deadline = time.monotonic() + _CLOSE_JOIN_TIMEOUT_S

        while True:
            with self._lock:
                next_seq = self._pending_sequence_high_water
            if next_seq is None:
                return True

            conn = None
            try:
                conn = sqlite3.connect(self._path, isolation_level=None, timeout=0.0)
                conn.execute("PRAGMA busy_timeout=0").fetchall()
                conn.execute("PRAGMA synchronous=FULL").fetchall()
                cursor = conn.execute(_UPDATE_NEXT_SEQ, (next_seq,))
                if cursor.rowcount != 1:
                    raise StoreError("event store sequence counter is missing")
                cursor.close()
            except sqlite3.OperationalError as exc:
                message = str(exc).lower()
                if "locked" not in message and "busy" not in message:
                    raise
                if time.monotonic() >= deadline:
                    return False
                time.sleep(
                    min(
                        _SEQUENCE_PERSIST_RETRY_SLEEP_S,
                        max(deadline - time.monotonic(), 0.0),
                    )
                )
                continue
            finally:
                if conn is not None:
                    conn.close()

            with self._lock:
                if (
                    self._pending_sequence_high_water is not None
                    and self._pending_sequence_high_water <= next_seq
                ):
                    self._pending_sequence_high_water = None

    def _reserve_evicted_sequence(self, item: Any, deadline: float) -> None:
        evicted_seq = item[0]
        with self._lock:
            next_seq = max(self._next_seq, evicted_seq + 1)
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            raise StoreTimeout(
                "event store could not persist an evicted sequence within the "
                "critical append deadline"
            )
        conn = sqlite3.connect(self._path, isolation_level=None, timeout=0.0)
        try:
            timeout_ms = max(int((deadline - time.monotonic()) * 1000), 0)
            conn.execute(f"PRAGMA busy_timeout={timeout_ms}").fetchall()
            conn.execute("PRAGMA synchronous=FULL").fetchall()
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise StoreTimeout(
                    "event store could not persist an evicted sequence within the "
                    "critical append deadline"
                )
            timeout_ms = int(remaining * 1000)
            conn.execute(f"PRAGMA busy_timeout={timeout_ms}").fetchall()
            cursor = conn.execute(_UPDATE_NEXT_SEQ, (next_seq,))
            if cursor.rowcount != 1:
                raise StoreError("event store sequence counter is missing")
            cursor.close()
            if time.monotonic() > deadline:
                raise StoreTimeout(
                    "event store could not persist an evicted sequence within the "
                    "critical append deadline"
                )
        except sqlite3.OperationalError as exc:
            message = str(exc).lower()
            if "locked" not in message and "busy" not in message:
                raise
            raise StoreTimeout(
                "event store could not persist an evicted sequence within the "
                "critical append deadline"
            ) from exc
        finally:
            conn.close()

    def _check_writer_alive(self) -> None:
        with self._lock:
            dead = self._dead
        if dead is not None:
            raise StoreError("event store is dead") from dead

    def close(self) -> None:
        with self._close_lock:
            self._close_impl()

    def _close_impl(self) -> None:
        close_deadline = time.monotonic() + _CLOSE_JOIN_TIMEOUT_S
        with self._lock:
            if self._closed:
                failure = self._close_error or self._dead
                already_closed = True
            else:
                self._closed = True
                self._close_requested.set()
                failure = self._close_error or self._dead
                already_closed = False
        self._queue.wake()

        if already_closed:
            if failure is not None:
                self._request_stop(failure, deadline=close_deadline)
                self._thread.join(_CLOSE_JOIN_TIMEOUT_S)
                self._raise_close_failure(self._close_failure())
            return

        if failure is not None:
            self._request_stop(failure, deadline=close_deadline)
            self._thread.join(_CLOSE_JOIN_TIMEOUT_S)
            self._raise_close_failure(self._close_failure())

        with self._lock:
            while self._active_admissions:
                remaining = close_deadline - time.monotonic()
                if remaining <= 0:
                    break
                self._admission_cond.wait(remaining)
            admissions_active = self._active_admissions != 0

        if admissions_active:
            failure = StoreTimeout(
                "event store close: append admission did not stop within the close deadline"
            )
        else:
            try:
                # A close sentinel is not an event. It must wait for space and
                # never evict an accepted event from the bounded queue.
                self._queue.put(
                    _SENTINEL,
                    critical=True,
                    timeout=max(
                        min(self._critical_timeout_s, close_deadline - time.monotonic()),
                        0.0,
                    ),
                    evict_noncritical=False,
                )
            except queue.Full:
                failure = StoreTimeout(
                    "event store close: writer stalled; sentinel was not admitted"
                )

        if failure is not None:
            failure = self._set_close_failure(failure)
            self._request_stop(failure, deadline=close_deadline)
            self._thread.join(_CLOSE_JOIN_TIMEOUT_S)
            if self._thread.is_alive():
                self._request_stop(failure, deadline=close_deadline)
                self._thread.join(_CLOSE_STOP_JOIN_TIMEOUT_S)
                if self._thread.is_alive():
                    failure = self._set_writer_stop_failure(failure)
                    self._request_stop(failure, deadline=close_deadline)
            self._raise_close_failure(self._close_failure())

        self._thread.join(_CLOSE_JOIN_TIMEOUT_S)
        if self._thread.is_alive():
            failure = self._set_close_failure(
                StoreTimeout("event store close: writer did not stop within the close deadline")
            )
            self._request_stop(failure, deadline=close_deadline)
            self._thread.join(_CLOSE_STOP_JOIN_TIMEOUT_S)
            if self._thread.is_alive():
                self._set_writer_stop_failure(failure)

        failure = self._close_failure()
        if failure is not None:
            self._request_stop(failure, deadline=close_deadline)
            self._raise_close_failure(self._close_failure())

    def _close_failure(self) -> BaseException | None:
        with self._lock:
            return self._close_error or self._dead

    def _set_close_failure(self, exc: BaseException) -> BaseException:
        with self._lock:
            if self._close_error is None and self._dead is None:
                self._close_error = exc
                self._dead = exc
            return self._close_error or self._dead or exc

    def _set_writer_stop_failure(self, cause: BaseException) -> BaseException:
        failure = StoreTimeout("event store close: writer could not be stopped")
        failure.__cause__ = cause
        failure.__suppress_context__ = True
        with self._lock:
            self._close_error = failure
            if self._dead is None:
                self._dead = failure
        return failure

    def _request_stop(self, exc: BaseException, *, deadline: float | None = None) -> None:
        self._stop_requested.set()
        self._queue.wake()
        self._fail_pending(exc)
        if not self._persist_pending_sequence_high_water(deadline):
            failure = StoreTimeout(
                "event store close: sequence reservation was not persisted before "
                "the close deadline"
            )
            failure.__cause__ = exc
            with self._lock:
                if self._close_error is None:
                    self._close_error = failure
                if self._dead is None:
                    self._dead = failure

    def _raise_close_failure(self, exc: BaseException | None) -> None:
        if exc is None:
            return
        with self._lock:
            final_close_error = self._close_error is exc
        if isinstance(exc, StoreError) or final_close_error:
            raise exc
        raise StoreError("event store writer died") from exc

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
            conn.execute(_SEQUENCE_SCHEMA)
            db_fd = os.open(self._path, os.O_RDWR)
            wal_fd = os.open(f"{self._path}-wal", os.O_RDWR)
            self._conn = conn
            self._db_fd = db_fd
            self._wal_fd = wal_fd
            with self._lock:
                derived_next_seq = conn.execute(
                    "SELECT COALESCE(MAX(seq), 0) + 1 FROM events"
                ).fetchone()[0]
                row = conn.execute(_SELECT_NEXT_SEQ).fetchone()
                if row is None:
                    self._next_seq = derived_next_seq
                    conn.execute(_INSERT_NEXT_SEQ, (self._next_seq,))
                else:
                    self._next_seq = max(int(row[0]), derived_next_seq)
                    if self._next_seq != row[0]:
                        conn.execute(_UPDATE_NEXT_SEQ, (self._next_seq,))
                self._started.set()
        except BaseException as exc:
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
        cur_item = None
        cur_pending = None
        cur_inserted = False
        dead_exc = None
        try:
            while not self._stop_requested.is_set():
                remaining = next_fsync - time.monotonic()
                try:
                    item = self._queue.get(
                        timeout=max(remaining, 0.0), stop_event=self._stop_requested
                    )
                except queue.Empty:
                    if self._stop_requested.is_set():
                        break
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
                cur_item = item
                cur_pending = pending
                cur_inserted = False
                if self._stop_requested.is_set():
                    self._account_failed_item(item, inserted=cur_inserted)
                    cur_pending.exc = self._termination_error()
                    cur_pending.event.set()
                    cur_item = None
                    cur_pending = None
                    break
                row = self._redact_row(row)
                conn.execute(_INSERT, (seq, kind, *row))
                cur_inserted = True
                dirty = True
                if kind in CRITICAL_KINDS:
                    if self._stop_requested.is_set():
                        self._account_failed_item(item, inserted=cur_inserted)
                        cur_pending.exc = self._termination_error()
                        cur_pending.event.set()
                        cur_item = None
                        cur_pending = None
                        break
                    self._fsync_now()
                    dirty = False
                    next_fsync = time.monotonic() + self._fsync_interval_s
                if self._stop_requested.is_set():
                    self._account_failed_item(item, inserted=cur_inserted)
                    cur_pending.exc = self._termination_error()
                    cur_pending.event.set()
                    cur_item = None
                    cur_pending = None
                    break
                pending.event.set()
                cur_item = None
                cur_pending = None
                cur_inserted = False
        except BaseException as exc:
            dead_exc = exc
            if cur_item is not None:
                self._account_failed_item(cur_item, inserted=cur_inserted)
            self._record_writer_failure(exc)
            self._fail_pending(exc)
            if cur_pending is not None:
                cur_pending.exc = exc
                cur_pending.event.set()
        finally:
            if dead_exc is None and not self._stop_requested.is_set():
                try:
                    self._fsync_now()
                except BaseException as exc:
                    self._record_writer_failure(exc, close_error=True)
                    self._fail_pending(exc)
                    if cur_pending is not None:
                        if cur_item is not None:
                            self._account_failed_item(cur_item, inserted=cur_inserted)
                        cur_pending.exc = exc
                        cur_pending.event.set()
            if wal_fd is not None:
                os.close(wal_fd)
            if db_fd is not None:
                os.close(db_fd)
            if conn is not None:
                conn.close()

    def _record_writer_failure(self, exc: BaseException, *, close_error: bool = False) -> None:
        with self._lock:
            if self._dead is None:
                self._dead = exc
            if close_error and self._close_error is None:
                self._close_error = exc
        self._queue.wake()

    def _termination_error(self) -> BaseException:
        with self._lock:
            return self._close_error or self._dead or StoreError("event store writer stopped")

    def _account_failed_item(self, item: tuple, *, inserted: bool) -> None:
        if inserted:
            return
        seq, kind, _, _ = item
        self._remember_sequence_high_water(seq + 1)
        if kind in CRITICAL_KINDS:
            return
        self._record_dropped(1)
        logger.warning(
            "event store shutdown: dropped non-critical event seq %d kind=%r",
            seq,
            kind,
        )

    def _fail_pending(self, exc: BaseException) -> None:
        dropped = 0
        next_seq = 0
        for item in self._queue.drain():
            if item is _SENTINEL or item is _TIMER:
                continue
            seq, kind, row, pending = item
            next_seq = max(next_seq, seq + 1)
            if kind not in CRITICAL_KINDS:
                dropped += 1
            pending.exc = exc
            pending.event.set()
        if next_seq:
            self._remember_sequence_high_water(next_seq)
        if dropped:
            self._record_dropped(dropped)
            logger.warning(
                "event store shutdown: dropped %d queued non-critical event(s)",
                dropped,
            )

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
