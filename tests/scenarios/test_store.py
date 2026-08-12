"""Scenario tests for the Cambium SQLite WAL event store (src/cambium/store.py).

No mocking libraries: every scenario drives the real EventStore against a temp
directory, including a crash-durability subprocess that mirrors the methodology
of docs/research/sqlite-wal-durability.md (os._exit mid-write, reopen, integrity
check).

M4 probes: bounded-queue overflow under a stalled writer (drop oldest
non-critical, preserve critical), the critical-append hard deadline, the
checkpoint-busy rule (never ack while a reader holds the WAL), and close()
propagating a final fsync failure. Store-hardening probes cover close
admission, sequence reuse, bounded forced shutdown, writer death, and lock
ordering under concurrent overflow.
"""

from __future__ import annotations

import os
import sqlite3
import subprocess
import sys
import threading
import time

import pytest

import cambium.store as store_module
from cambium.redact import Redactor
from cambium.store import (
    CRITICAL_KINDS,
    EventStore,
    StoreError,
    StoreInitError,
    StoreTimeout,
    read_events_file,
)


def _open(path):
    return EventStore(path, fsync_interval_s=5.0)


def test_append_read_back_fields_and_monotonic_seq(tmp_path) -> None:
    store = _open(tmp_path / "events.db")
    try:
        seq1 = store.append({
            "kind": "worker_started", "payload": {"pid": 20471, "phase": "ready"},
            "ts": "1754212801.204", "monotonic_ms": 481234568400,
            "task_id": "wt-001", "worker_id": "wt-001#1", "generation": 1,
            "request_id": "r1",
        })
        seq2 = store.append({"kind": "log", "payload": {"line": "hello"}})
        seq3 = store.append({
            "kind": "result", "payload": {"status": "done"},
            "monotonic_ms": 481234580700, "task_id": "wt-001",
        })
        assert seq1 == 1
        assert seq2 == 2
        assert seq3 == 3

        events = store.events_after(0)
        assert [e["seq"] for e in events] == [1, 2, 3]

        first = events[0]
        assert first["kind"] == "worker_started"
        assert first["payload"] == {"pid": 20471, "phase": "ready"}
        assert first["ts"] == "1754212801.204"
        assert first["monotonic_ms"] == 481234568400
        assert first["task_id"] == "wt-001"
        assert first["worker_id"] == "wt-001#1"
        assert first["generation"] == 1
        assert first["request_id"] == "r1"

        assert events[1]["kind"] == "log"
        assert events[1]["payload"] == {"line": "hello"}
        assert events[1]["task_id"] is None
        assert events[2]["kind"] == "result"
        assert events[2]["payload"] == {"status": "done"}
        assert events[2]["monotonic_ms"] == 481234580700
    finally:
        store.close()


@pytest.mark.slow
def test_crash_durability_critical_events_survive(tmp_path) -> None:
    path = tmp_path / "crash" / "events.db"
    n = 50
    script = (
        "import os, sys\n"
        "from cambium.store import EventStore\n"
        "store = EventStore(sys.argv[1])\n"
        f"for i in range({n}):\n"
        "    kind = 'result' if i % 2 == 0 else 'checkpoint'\n"
        "    store.append({'kind': kind, 'payload': {'i': i}, 'task_id': 't'})\n"
        "os._exit(9)\n"
    )
    proc = subprocess.run(
        [sys.executable, "-c", script, str(path)], capture_output=True, text=True, timeout=120
    )
    assert proc.returncode == 9, proc.stderr

    conn = sqlite3.connect(path)
    try:
        assert conn.execute("PRAGMA integrity_check").fetchone()[0] == "ok"
        count, max_seq = conn.execute(
            "SELECT count(*), COALESCE(max(seq), 0) FROM events"
        ).fetchone()
        seqs = [r[0] for r in conn.execute("SELECT seq FROM events ORDER BY seq")]
    finally:
        conn.close()
    assert count == n
    assert max_seq == n
    assert seqs == list(range(1, n + 1))

    store = _open(path)
    try:
        events = store.events_after(0)
        assert [e["seq"] for e in events] == list(range(1, n + 1))
        assert all(e["kind"] in CRITICAL_KINDS for e in events)
        assert all(e["task_id"] == "t" for e in events)
    finally:
        store.close()


def test_non_critical_append_does_not_block_on_fsync(tmp_path, monkeypatch) -> None:
    store = _open(tmp_path / "events.db")
    calls = {"n": 0}

    def slow_fsync(self) -> None:
        calls["n"] += 1
        time.sleep(0.1)

    monkeypatch.setattr(EventStore, "_fsync_now", slow_fsync)
    try:
        start = time.monotonic()
        seq = store.append({"kind": "log", "payload": {"line": "x"}})
        elapsed = time.monotonic() - start
        assert seq == 1
        assert elapsed < 0.05  # returned without waiting on the writer's fsync

        start = time.monotonic()
        store.append({"kind": "result", "payload": {"ok": True}})
        blocked = time.monotonic() - start
        assert blocked >= 0.08  # the slow fsync is in effect; critical append waits
        assert calls["n"] >= 1
    finally:
        store.close()


def test_events_after_returns_only_newer_ordered(tmp_path) -> None:
    store = _open(tmp_path / "events.db")
    try:
        for i in range(5):
            store.append({"kind": "log", "payload": {"i": i}})
        store.append({"kind": "result", "payload": {"done": True}})  # barrier

        events = store.events_after(3)
        assert [e["seq"] for e in events] == [4, 5, 6]
        logs = [e for e in events if e["kind"] == "log"]
        assert [e["payload"]["i"] for e in logs] == [3, 4]
        assert events[-1]["kind"] == "result"
        assert store.events_after(6) == []
    finally:
        store.close()


def test_read_events_file_is_noncreating_read_only_observer(
    tmp_path, monkeypatch
) -> None:
    path = tmp_path / "existing" / "events.db"
    store = _open(path)
    try:
        store.append({"kind": "result", "payload": {"done": True}})
    finally:
        store.close()

    def fail_side_effect(*args, **kwargs):
        raise AssertionError("read-only event replay caused a writer-side effect")

    monkeypatch.setattr(store_module.os, "chmod", fail_side_effect)
    monkeypatch.setattr(store_module.Path, "mkdir", fail_side_effect)
    monkeypatch.setattr(store_module.threading.Thread, "start", fail_side_effect)

    assert read_events_file(path, 0) == [{
        "seq": 1,
        "kind": "result",
        "payload": {"done": True},
        "ts": None,
        "monotonic_ms": None,
        "task_id": None,
        "worker_id": None,
        "generation": None,
        "request_id": None,
    }]

    missing = tmp_path / "missing" / "events.db"
    assert read_events_file(missing, 0) == []
    assert not missing.parent.exists()


def test_close_drains_pending_appends(tmp_path) -> None:
    path = tmp_path / "events.db"
    store = _open(path)
    seqs = [store.append({"kind": "log", "payload": {"i": i}}) for i in range(100)]
    assert seqs == list(range(1, 101))
    store.close()

    conn = sqlite3.connect(path)
    try:
        count, max_seq = conn.execute(
            "SELECT count(*), COALESCE(max(seq), 0) FROM events"
        ).fetchone()
    finally:
        conn.close()
    assert count == 100
    assert max_seq == 100

    reopened = _open(path)
    try:
        events = reopened.events_after(0)
        assert len(events) == 100
        assert [e["payload"]["i"] for e in events] == list(range(100))
    finally:
        reopened.close()


def test_corrupt_db_init_raises_not_hangs(tmp_path) -> None:
    path = tmp_path / "events.db"
    path.write_bytes(b"this is not a sqlite database at all" * 16)
    start = time.monotonic()
    with pytest.raises(StoreInitError):
        EventStore(path, startup_timeout_s=10.0)
    assert time.monotonic() - start < 5.0


@pytest.mark.slow
def test_writer_dead_on_locked_db_critical_append_raises(
    tmp_path, monkeypatch
) -> None:
    path = tmp_path / "events.db"
    # The writer's SQLite busy timeout is what bounds the wait on the locked
    # DB; the value is an implementation detail, not the signal. The signal
    # is: the append waits the busy timeout, then raises, and the store is
    # dead afterwards.
    monkeypatch.setattr(store_module, "_WRITER_BUSY_TIMEOUT_MS", 200)
    store = _open(path)
    blocker = sqlite3.connect(path, isolation_level=None)
    try:
        blocker.execute("BEGIN EXCLUSIVE")
        start = time.monotonic()
        with pytest.raises(StoreError):
            store.append({"kind": "result", "payload": {"ok": True}})
        elapsed = time.monotonic() - start
        # Lower bound proves the append waited the busy timeout rather than
        # failing instantly; the upper bound allows the hard append deadline
        # (10s) to fire under load without flaking the bound.
        assert 0.15 <= elapsed < 30.0  # bounded by busy_timeout, not a hang
    finally:
        blocker.close()
    # store is dead: subsequent appends fail immediately, and close() surfaces
    # the writer failure instead of reporting success.
    with pytest.raises(StoreError):
        store.append({"kind": "log", "payload": {}})
    with pytest.raises(StoreError):
        store.append({"kind": "result", "payload": {}})
    with pytest.raises(StoreError):
        store.close()


@pytest.mark.slow
def test_writer_execute_failure_counts_removed_noncritical_and_burns_sequence(
    tmp_path, monkeypatch
) -> None:
    path = tmp_path / "events.db"
    # Shorten the writer busy timeout (see the dead-writer test above) so the
    # locked-DB writer failure is observed quickly; the drop accounting and
    # sequence burn do not depend on the timeout value.
    monkeypatch.setattr(store_module, "_WRITER_BUSY_TIMEOUT_MS", 200)
    store = EventStore(path, fsync_interval_s=60.0)
    blocker = sqlite3.connect(path, isolation_level=None)
    try:
        blocker.execute("BEGIN EXCLUSIVE")
        assert store.append({"kind": "log", "payload": {}}) == 1
        store._thread.join(7.0)
        assert not store._thread.is_alive()
        assert store.dropped == 1

        blocker.rollback()
        blocker.close()
        blocker = None
        assert store.events_after(0) == []
        with pytest.raises(StoreError):
            store.close()
    finally:
        if blocker is not None:
            blocker.rollback()
            blocker.close()
        if store._thread.is_alive():
            store._stop_requested.set()
            store._queue.wake()
            store._thread.join(2.0)

    reopened = EventStore(path, fsync_interval_s=60.0)
    try:
        assert reopened.append({"kind": "log", "payload": {}}) == 2
    finally:
        reopened.close()

    assert [event["seq"] for event in reopened.events_after(0)] == [2]


def test_stalled_writer_flood_drops_non_critical_preserves_critical(
    tmp_path, monkeypatch
) -> None:
    store = EventStore(
        tmp_path / "events.db",
        fsync_interval_s=60.0,
        max_queue_size=8,
        critical_timeout_s=10.0,
    )
    release = threading.Event()
    stalled = threading.Event()
    real_fsync = EventStore._fsync_now

    def stalled_fsync(self) -> None:
        stalled.set()
        release.wait(30.0)
        real_fsync(self)

    monkeypatch.setattr(EventStore, "_fsync_now", stalled_fsync)
    try:
        starter = threading.Thread(
            target=lambda: store.append({"kind": "result", "payload": {"c": 0}})
        )
        starter.start()
        assert stalled.wait(0.6)  # writer is now blocked in C0's fsync

        start = time.monotonic()
        for i in range(40):
            store.append({"kind": "log", "payload": {"i": i}})
        assert time.monotonic() - start < 5.0  # full queue never blocks non-critical

        blocker = threading.Thread(
            target=lambda: store.append({"kind": "result", "payload": {"c": 1}})
        )
        blocker.start()
        while store.dropped < 33:  # 32 incoming drops + 1 eviction for the critical
            time.sleep(0.01)
        release.set()
        blocker.join(10.0)
        starter.join(10.0)
        assert not blocker.is_alive()
        assert not starter.is_alive()
    finally:
        release.set()
        store.close()

    assert store.dropped == 33
    events = store.events_after(0)
    # C0 (1) survives; seq 2 was evicted to admit C1 (10); incoming drops do
    # not reserve sequence numbers; the remaining 7 flood events (3..9) are
    # written before the critical.
    assert [e["seq"] for e in events] == [1, 3, 4, 5, 6, 7, 8, 9, 10]
    assert [e["kind"] for e in events] == ["result"] + ["log"] * 7 + ["result"]


@pytest.mark.slow
def test_critical_append_hard_deadline_raises_store_timeout(tmp_path, monkeypatch) -> None:
    store = EventStore(
        tmp_path / "events.db", fsync_interval_s=60.0, critical_timeout_s=0.4
    )
    release = threading.Event()
    real_fsync = EventStore._fsync_now

    def stuck_fsync(self) -> None:
        release.wait(30.0)
        real_fsync(self)

    monkeypatch.setattr(EventStore, "_fsync_now", stuck_fsync)
    try:
        start = time.monotonic()
        with pytest.raises(StoreTimeout):
            store.append({"kind": "result", "payload": {"ok": True}})
        elapsed = time.monotonic() - start
        assert 0.3 <= elapsed < 5.0  # bounded by the hard deadline, no hang
        # store stays alive: a non-critical append is unaffected while the
        # writer is stalled, and a later critical append succeeds on recovery.
        assert store.append({"kind": "log", "payload": {}}) > 0
        release.set()
        seq = store.append({"kind": "result", "payload": {"ok": True}})
        assert seq > 0
    finally:
        release.set()
        store.close()

    events = store.events_after(0)
    # the timed-out event was still written once the writer recovered — it was
    # never acknowledged before fsync, but the pending row is not dropped.
    assert [e["seq"] for e in events] == [1, 2, 3]
    assert len([e for e in events if e["kind"] == "result"]) == 2


@pytest.mark.slow
def test_checkpoint_busy_never_acks_while_reader_holds(tmp_path) -> None:
    path = tmp_path / "events.db"
    # 2.0s critical timeout: the first append must fail to ack while the
    # reader holds the WAL (no ack, no hang — asserted below), and the second
    # append needs the writer thread to recover once the reader releases;
    # under parallel load (pytest-xdist) 0.3s leaves no headroom for the
    # writer's busy-retry + fsync.
    store = EventStore(path, fsync_interval_s=60.0, critical_timeout_s=2.0)
    reader = sqlite3.connect(path)
    try:
        reader.execute("BEGIN")
        reader.execute("SELECT seq FROM events").fetchall()
        start = time.monotonic()
        with pytest.raises(StoreError):  # StoreTimeout is a StoreError
            store.append({"kind": "result", "payload": {"ok": True}})
        elapsed = time.monotonic() - start
        assert 0.25 <= elapsed < 10.0  # no ack, no hang
    finally:
        reader.rollback()
        reader.close()

    # once the reader releases, the writer's busy retry succeeds; the store
    # stays usable and the timed-out event is still written durably.
    store.append({"kind": "result", "payload": {"ok": True}})
    store.close()

    events = store.events_after(0)
    assert [e["seq"] for e in events] == [1, 2]
    assert all(e["kind"] == "result" for e in events)


def test_close_propagates_final_fsync_error(tmp_path, monkeypatch) -> None:
    store = EventStore(tmp_path / "events.db", fsync_interval_s=60.0)
    store.append({"kind": "log", "payload": {"i": 0}})

    def fail_fsync(self) -> None:
        raise OSError(28, "No space left on device")

    monkeypatch.setattr(EventStore, "_fsync_now", fail_fsync)
    with pytest.raises(OSError, match="No space left on device"):
        store.close()


def test_concurrent_close_waits_for_final_fsync_result(tmp_path, monkeypatch) -> None:
    store = EventStore(tmp_path / "events.db", fsync_interval_s=60.0)
    store.append({"kind": "log", "payload": {"i": 0}})
    fsync_started = threading.Event()
    release = threading.Event()
    close_errors: list[tuple[str, BaseException]] = []
    second_done = threading.Event()

    def fail_final_fsync(self) -> None:
        fsync_started.set()
        release.wait(1.0)
        raise OSError(28, "No space left on device")

    def close_store(name: str, done: threading.Event | None = None) -> None:
        try:
            store.close()
        except BaseException as exc:
            close_errors.append((name, exc))
        finally:
            if done is not None:
                done.set()

    monkeypatch.setattr(EventStore, "_fsync_now", fail_final_fsync)
    first = threading.Thread(target=close_store, args=("first",))
    second = threading.Thread(target=close_store, args=("second", second_done))
    try:
        first.start()
        assert fsync_started.wait(1.0)
        second.start()
        assert not second_done.wait(0.05)

        release.set()
        first.join(2.0)
        second.join(2.0)
        assert not first.is_alive()
        assert not second.is_alive()
        assert close_errors
        assert any(isinstance(error, OSError) for _, error in close_errors)
        assert isinstance(store._close_error, OSError)
    finally:
        release.set()
        first.join(2.0)
        second.join(2.0)
        if store._thread.is_alive():
            store._stop_requested.set()
            store._queue.wake()
            store._thread.join(2.0)


@pytest.mark.slow
def test_close_reports_writer_not_stopped_after_sentinel_failure(tmp_path, monkeypatch) -> None:
    path = tmp_path / "events.db"
    # Shrink the close join budget so the force-stop verdict is pinned
    # quickly instead of paying the full production 12.0s budget.
    monkeypatch.setattr(store_module, "_CLOSE_JOIN_TIMEOUT_S", 1.0)
    # max_queue_size=2: the close sentinel is admitted immediately (one slot
    # free alongside the stalled non-critical item), so close() spends its
    # deadline on the writer join rather than on the sentinel admission wait;
    # the force-stop verdict is what this test pins.
    store = EventStore(
        path, fsync_interval_s=60.0, max_queue_size=2, critical_timeout_s=60.0
    )
    fsync_started = threading.Event()
    release = threading.Event()
    append_errors: list[BaseException] = []
    real_fsync = EventStore._fsync_now

    def stalled_fsync(self) -> None:
        fsync_started.set()
        # Stall longer than the close join budget (1.0s here) so the
        # force-stop verdict is pinned even with headroom.
        release.wait(3.0)
        real_fsync(self)

    def append_critical() -> None:
        try:
            store.append({"kind": "result", "payload": {"i": 0}})
        except BaseException as exc:
            append_errors.append(exc)

    monkeypatch.setattr(EventStore, "_fsync_now", stalled_fsync)
    starter = threading.Thread(target=append_critical)
    try:
        starter.start()
        assert fsync_started.wait(1.0)
        assert store.append({"kind": "log", "payload": {"i": 1}}) == 2

        with pytest.raises(StoreTimeout, match="writer could not be stopped"):
            store.close()
        assert store._thread.is_alive()

        release.set()
        starter.join(5.0)
        store._thread.join(5.0)
        assert not starter.is_alive()
        assert not store._thread.is_alive()
        assert len(append_errors) == 1
        assert isinstance(append_errors[0], StoreError)
        for fd in (store._db_fd, store._wal_fd):
            with pytest.raises(OSError):
                os.fstat(fd)
    finally:
        release.set()
        starter.join(5.0)
        if store._thread.is_alive():
            store._stop_requested.set()
            store._queue.wake()
            store._thread.join(5.0)


def test_restart_after_evicted_event_writer_death_does_not_reuse_sequence(
    tmp_path, monkeypatch
) -> None:
    path = tmp_path / "events.db"
    store = EventStore(path, fsync_interval_s=60.0, max_queue_size=1, critical_timeout_s=2.0)
    fsync_started = threading.Event()
    release = threading.Event()
    eviction_seen = threading.Event()
    append_errors: list[tuple[str, BaseException]] = []

    def fail_fsync(self) -> None:
        fsync_started.set()
        release.wait(5.0)
        raise OSError("writer failed after eviction")

    def append_event(name: str, kind: str, value: int) -> None:
        try:
            store.append({"kind": kind, "payload": {"i": value}})
        except BaseException as exc:
            append_errors.append((name, exc))

    monkeypatch.setattr(EventStore, "_fsync_now", fail_fsync)
    first = threading.Thread(target=append_event, args=("first", "result", 0))
    first.start()
    try:
        assert fsync_started.wait(1.0)
        assert store.append({"kind": "log", "payload": {"i": 1}}) == 2

        real_check = store._check_writer_alive
        death_triggered = False

        def check_after_eviction() -> None:
            nonlocal death_triggered
            if not death_triggered and not store._queue._items:
                death_triggered = True
                eviction_seen.set()
                release.set()
                deadline = time.monotonic() + 1.0
                while True:
                    with store._lock:
                        dead = store._dead
                    if dead is not None:
                        break
                    if time.monotonic() >= deadline:
                        raise AssertionError("writer did not die after eviction")
                    time.sleep(0.001)
            real_check()

        monkeypatch.setattr(store, "_check_writer_alive", check_after_eviction)
        critical = threading.Thread(target=append_event, args=("critical", "result", 1))
        critical.start()
        assert eviction_seen.wait(1.0)
        critical.join(2.0)
        first.join(2.0)
        store._thread.join(2.0)

        assert not critical.is_alive()
        assert not first.is_alive()
        assert not store._thread.is_alive()
        assert len(append_errors) == 2
        assert all(isinstance(error, StoreError) for _, error in append_errors)
        assert store.dropped == 1
        assert [event["seq"] for event in store.events_after(0)] == [1]
        with pytest.raises(StoreError):
            store.close()
    finally:
        release.set()
        first.join(2.0)
        if "critical" in locals():
            critical.join(2.0)
        if store._thread.is_alive():
            store._stop_requested.set()
            store._queue.wake()
            store._thread.join(2.0)

    monkeypatch.undo()
    reopened = EventStore(path, fsync_interval_s=60.0)
    try:
        new_seq = reopened.append({"kind": "log", "payload": {"i": 2}})
        assert new_seq == 3
    finally:
        reopened.close()

    assert [event["seq"] for event in reopened.events_after(0)] == [1, 3]


def test_close_sentinel_preserves_accepted_queue_items_and_counts_drops(
    tmp_path, monkeypatch
) -> None:
    path = tmp_path / "events.db"
    store = EventStore(path, fsync_interval_s=60.0, max_queue_size=2, critical_timeout_s=0.5)
    release = threading.Event()
    stalled = threading.Event()
    real_fsync = EventStore._fsync_now

    def stalled_fsync(self) -> None:
        stalled.set()
        release.wait(1.0)
        real_fsync(self)

    monkeypatch.setattr(EventStore, "_fsync_now", stalled_fsync)
    starter = threading.Thread(
        target=lambda: store.append({"kind": "result", "payload": {"i": 0}})
    )
    closer_done = threading.Event()
    close_errors: list[BaseException] = []
    closer: threading.Thread | None = None

    def close_store() -> None:
        try:
            store.close()
        except BaseException as exc:
            close_errors.append(exc)
        finally:
            closer_done.set()

    try:
        starter.start()
        assert stalled.wait(0.6)
        accepted = [
            store.append({"kind": "log", "payload": {"i": 1}}),
            store.append({"kind": "log", "payload": {"i": 2}}),
        ]
        assert accepted == [2, 3]
        assert store.append({"kind": "log", "payload": {"i": 3}}) is None
        assert store.dropped == 1

        closer = threading.Thread(target=close_store)
        closer.start()
        assert not closer_done.wait(0.05)  # sentinel is waiting, not evicting
        release.set()
        assert closer_done.wait(1.0)
        closer.join(1.0)
        starter.join(1.0)
        assert not starter.is_alive()
        assert close_errors == []
    finally:
        release.set()
        if closer is not None:
            closer.join(5.0)
        starter.join(5.0)

    events = store.events_after(0)
    assert [event["seq"] for event in events] == [1, 2, 3]
    assert [event["payload"]["i"] for event in events] == [0, 1, 2]
    assert store.dropped == 1


def test_restart_after_tail_drop_does_not_reuse_a_sequence(tmp_path, monkeypatch) -> None:
    path = tmp_path / "events.db"
    store = EventStore(path, fsync_interval_s=60.0, max_queue_size=1, critical_timeout_s=0.5)
    release = threading.Event()
    stalled = threading.Event()
    real_fsync = EventStore._fsync_now
    first_result: list[int] = []

    def stalled_fsync(self) -> None:
        stalled.set()
        release.wait(1.0)
        real_fsync(self)

    monkeypatch.setattr(EventStore, "_fsync_now", stalled_fsync)
    starter = threading.Thread(
        target=lambda: first_result.append(
            store.append({"kind": "result", "payload": {"i": 0}})
        )
    )
    try:
        starter.start()
        assert stalled.wait(0.6)
        accepted = store.append({"kind": "log", "payload": {"i": 1}})
        dropped = store.append({"kind": "log", "payload": {"i": 2}})
        assert accepted == 2
        assert dropped is None
        release.set()
        starter.join(5.0)
        assert first_result == [1]
        store.close()
    finally:
        release.set()
        starter.join(5.0)

    reopened = EventStore(path, fsync_interval_s=60.0)
    try:
        new_seq = reopened.append({"kind": "log", "payload": {"i": 3}})
        assert new_seq == 3
    finally:
        reopened.close()

    assert [event["seq"] for event in reopened.events_after(2)] == [3]


@pytest.mark.slow
def test_failed_eviction_reservation_restores_event_and_sequence(tmp_path, monkeypatch) -> None:
    path = tmp_path / "events.db"
    store = EventStore(
        path, fsync_interval_s=60.0, max_queue_size=1, critical_timeout_s=0.2
    )
    release = threading.Event()
    stalled = threading.Event()
    starter_errors: list[BaseException] = []
    real_fsync = EventStore._fsync_now

    def stalled_fsync(self) -> None:
        stalled.set()
        release.wait(5.0)
        real_fsync(self)

    def append_starter() -> None:
        try:
            store.append({"kind": "result", "payload": {"i": 0}})
        except BaseException as exc:
            starter_errors.append(exc)

    monkeypatch.setattr(EventStore, "_fsync_now", stalled_fsync)
    starter = threading.Thread(target=append_starter)
    blocker = sqlite3.connect(path, isolation_level=None)
    try:
        starter.start()
        assert stalled.wait(1.0)
        assert store.append({"kind": "log", "payload": {"i": 1}}) == 2
        blocker.execute("BEGIN IMMEDIATE")

        with pytest.raises(StoreTimeout):
            store.append({"kind": "result", "payload": {"i": 2}})

        assert store.dropped == 0
        with store._queue._cond:
            assert [item[0] for item in store._queue._items] == [2]
    finally:
        blocker.rollback()
        blocker.close()
        release.set()
        starter.join(2.0)
        store.close()

    reopened = EventStore(path, fsync_interval_s=60.0)
    try:
        assert reopened.append({"kind": "log", "payload": {"i": 3}}) == 3
    finally:
        reopened.close()

    assert [event["seq"] for event in reopened.events_after(0)] == [1, 2, 3]


@pytest.mark.slow
def test_writer_death_during_blocked_eviction_persistence_burns_sequence(
    tmp_path, monkeypatch
) -> None:
    path = tmp_path / "events.db"
    store = EventStore(path, fsync_interval_s=60.0, max_queue_size=1, critical_timeout_s=0.2)
    fsync_started = threading.Event()
    release = threading.Event()
    reservation_started = threading.Event()
    append_errors: list[BaseException] = []
    real_reserve = store._reserve_evicted_sequence

    def fail_fsync(self) -> None:
        fsync_started.set()
        release.wait(5.0)
        raise OSError("writer failed during eviction persistence")

    def reserve_eviction(item, deadline) -> None:
        reservation_started.set()
        real_reserve(item, deadline)

    def append_event(kind: str, value: int) -> None:
        try:
            store.append({"kind": kind, "payload": {"i": value}})
        except BaseException as exc:
            append_errors.append(exc)

    monkeypatch.setattr(EventStore, "_fsync_now", fail_fsync)
    monkeypatch.setattr(store, "_reserve_evicted_sequence", reserve_eviction)
    first = threading.Thread(target=append_event, args=("result", 0))
    critical = threading.Thread(target=append_event, args=("result", 1))
    blocker = sqlite3.connect(path, isolation_level=None)
    try:
        first.start()
        assert fsync_started.wait(1.0)
        assert store.append({"kind": "log", "payload": {"i": 1}}) == 2
        blocker.execute("BEGIN IMMEDIATE")

        critical.start()
        assert reservation_started.wait(1.0)
        release.set()
        store._thread.join(2.0)
        critical.join(2.0)
        first.join(2.0)
        assert not store._thread.is_alive()
        assert not critical.is_alive()
        assert not first.is_alive()
        assert len(append_errors) == 2
        assert all(isinstance(error, StoreError) for error in append_errors)
        assert store.dropped == 1
        with store._queue._cond:
            assert not store._queue._items
    finally:
        blocker.rollback()
        blocker.close()
        release.set()
        first.join(2.0)
        critical.join(2.0)
        if store._thread.is_alive():
            store._stop_requested.set()
            store._queue.wake()
            store._thread.join(2.0)
        if not store._closed:
            try:
                store.close()
            except StoreError:
                pass

    monkeypatch.undo()
    reopened = EventStore(path, fsync_interval_s=60.0)
    try:
        assert reopened.append({"kind": "log", "payload": {"i": 2}}) == 3
    finally:
        reopened.close()

    assert [event["seq"] for event in reopened.events_after(0)] == [1, 3]


@pytest.mark.slow
def test_close_retries_pending_sequence_persistence_after_reservation_lock(
    tmp_path, monkeypatch
) -> None:
    path = tmp_path / "events.db"
    store = EventStore(path, fsync_interval_s=60.0, max_queue_size=1, critical_timeout_s=0.2)
    fsync_started = threading.Event()
    fsync_release = threading.Event()
    reservation_started = threading.Event()
    persistence_started = threading.Event()
    append_errors: list[BaseException] = []
    close_errors: list[BaseException] = []
    real_reserve = store._reserve_evicted_sequence
    real_persist = store._persist_pending_sequence_high_water

    def fail_fsync(self) -> None:
        fsync_started.set()
        fsync_release.wait(5.0)
        raise OSError("writer failed during close persistence")

    def reserve_eviction(item, deadline) -> None:
        reservation_started.set()
        real_reserve(item, deadline)

    def persist_sequence(*args, **kwargs):
        persistence_started.set()
        return real_persist(*args, **kwargs)

    def append_event(kind: str, value: int) -> None:
        try:
            store.append({"kind": kind, "payload": {"i": value}})
        except BaseException as exc:
            append_errors.append(exc)

    def close_store() -> None:
        try:
            store.close()
        except BaseException as exc:
            close_errors.append(exc)

    monkeypatch.setattr(EventStore, "_fsync_now", fail_fsync)
    monkeypatch.setattr(store, "_reserve_evicted_sequence", reserve_eviction)
    monkeypatch.setattr(store, "_persist_pending_sequence_high_water", persist_sequence)
    first = threading.Thread(target=append_event, args=("result", 0))
    critical = threading.Thread(target=append_event, args=("result", 1))
    closer = threading.Thread(target=close_store)
    blocker = sqlite3.connect(path, isolation_level=None)
    try:
        first.start()
        assert fsync_started.wait(1.0)
        assert store.append({"kind": "log", "payload": {"i": 1}}) == 2
        blocker.execute("BEGIN IMMEDIATE")

        critical.start()
        assert reservation_started.wait(1.0)
        fsync_release.set()
        store._thread.join(2.0)
        assert not store._thread.is_alive()

        closer.start()
        assert persistence_started.wait(1.0)
        time.sleep(0.3)  # let the blocked eviction reservation reach its deadline
        blocker.rollback()
        blocker.close()
        blocker = None

        closer.join(2.0)
        critical.join(2.0)
        first.join(2.0)
        assert not closer.is_alive()
        assert not critical.is_alive()
        assert not first.is_alive()
        assert len(close_errors) == 1
        assert isinstance(close_errors[0], StoreError)
        assert store.dropped == 1
        assert store._pending_sequence_high_water is None
    finally:
        fsync_release.set()
        if blocker is not None:
            blocker.rollback()
            blocker.close()
        closer.join(2.0)
        critical.join(2.0)
        first.join(2.0)
        if store._thread.is_alive():
            store._stop_requested.set()
            store._queue.wake()
            store._thread.join(2.0)

    monkeypatch.undo()
    reopened = EventStore(path, fsync_interval_s=60.0)
    try:
        assert reopened.append({"kind": "log", "payload": {"i": 2}}) == 3
    finally:
        reopened.close()

    assert [event["seq"] for event in reopened.events_after(0)] == [1, 3]


def test_uncaught_base_exception_marks_writer_dead_and_wakes_critical_append(
    tmp_path, monkeypatch
) -> None:
    store = EventStore(tmp_path / "events.db", fsync_interval_s=60.0, critical_timeout_s=10.0)
    fsync_started = threading.Event()
    release = threading.Event()
    append_errors: list[BaseException] = []

    def fail_with_system_exit(self) -> None:
        fsync_started.set()
        release.wait(5.0)
        raise SystemExit("injected writer termination")

    def append_result() -> None:
        try:
            store.append({"kind": "result", "payload": {}})
        except BaseException as exc:
            append_errors.append(exc)

    monkeypatch.setattr(EventStore, "_fsync_now", fail_with_system_exit)
    caller = threading.Thread(target=append_result)
    try:
        caller.start()
        assert fsync_started.wait(1.0)
        release_at = time.monotonic()
        release.set()
        caller.join(1.0)
        elapsed = time.monotonic() - release_at
        assert not caller.is_alive()
        assert elapsed < 0.5
        assert store._dead is not None
        assert isinstance(store._dead, SystemExit)
        assert len(append_errors) == 1
        assert isinstance(append_errors[0], StoreError)
        assert isinstance(append_errors[0].__cause__, SystemExit)
    finally:
        release.set()
        caller.join(1.0)
        if store._thread.is_alive():
            store._stop_requested.set()
            store._queue.wake()
            store._thread.join(1.0)


def test_writer_death_rejects_waiting_admission_and_wakes_all_callers(
    tmp_path, monkeypatch
) -> None:
    store = EventStore(
        tmp_path / "events.db", fsync_interval_s=60.0, max_queue_size=1, critical_timeout_s=10.0
    )
    fsync_started = threading.Event()
    release = threading.Event()
    append_errors: list[tuple[int, BaseException]] = []

    def fail_fsync(self) -> None:
        fsync_started.set()
        release.wait(5.0)
        raise OSError("fsync failed")

    def append_result(value: int) -> None:
        try:
            store.append({"kind": "result", "payload": {"i": value}})
        except BaseException as exc:
            append_errors.append((value, exc))

    monkeypatch.setattr(EventStore, "_fsync_now", fail_fsync)
    first = threading.Thread(target=append_result, args=(0,))
    queued = threading.Thread(target=append_result, args=(1,))
    waiting = threading.Thread(target=append_result, args=(2,))
    admission_waiting = threading.Event()
    real_wait = store._queue._cond.wait

    def track_wait(timeout=None):
        admission_waiting.set()
        return real_wait(timeout)

    try:
        first.start()
        assert fsync_started.wait(1.0)
        queued.start()
        deadline = time.monotonic() + 1.0
        while time.monotonic() < deadline:
            with store._queue._cond:
                if len(store._queue._items) == 1:
                    break
            time.sleep(0.001)
        else:
            pytest.fail("queued event was not admitted")
        monkeypatch.setattr(store._queue._cond, "wait", track_wait)
        waiting.start()
        assert admission_waiting.wait(1.0)
        release_at = time.monotonic()
        release.set()
        for caller in (first, queued, waiting):
            caller.join(1.0)
        elapsed = time.monotonic() - release_at

        assert elapsed < 0.5
        assert all(not caller.is_alive() for caller in (first, queued, waiting))
        assert len(append_errors) == 3
        assert all(isinstance(error, StoreError) for _, error in append_errors)
        with store._queue._cond:
            assert not store._queue._items
        assert store._dead is not None
    finally:
        release.set()
        for caller in (first, queued, waiting):
            caller.join(1.0)
        if store._thread.is_alive():
            store._stop_requested.set()
            store._queue.wake()
            store._thread.join(1.0)


def test_noncritical_drop_is_not_blocked_by_critical_queue_waiter(
    tmp_path, monkeypatch
) -> None:
    store = EventStore(
        tmp_path / "events.db", fsync_interval_s=60.0, max_queue_size=1, critical_timeout_s=2.0
    )
    release = threading.Event()
    stalled = threading.Event()
    real_fsync = EventStore._fsync_now
    append_errors: list[BaseException] = []
    second_started = threading.Event()
    third_started = threading.Event()

    def stalled_fsync(self) -> None:
        stalled.set()
        release.wait(1.0)
        real_fsync(self)

    def append_result(value: int, started: threading.Event) -> None:
        started.set()
        try:
            store.append({"kind": "result", "payload": {"i": value}})
        except BaseException as exc:
            append_errors.append(exc)

    monkeypatch.setattr(EventStore, "_fsync_now", stalled_fsync)
    first = threading.Thread(target=append_result, args=(0, threading.Event()))
    second = threading.Thread(target=append_result, args=(1, second_started))
    third = threading.Thread(target=append_result, args=(2, third_started))
    try:
        first.start()
        assert stalled.wait(5.0)
        second.start()
        assert second_started.wait(1.0)
        deadline = time.monotonic() + 1.0
        while time.monotonic() < deadline:
            with store._queue._cond:
                full = len(store._queue._items) == 1
            if full:
                break
            time.sleep(0.001)
        assert full
        third.start()
        assert third_started.wait(1.0)
        time.sleep(0.01)

        start = time.monotonic()
        assert store.append({"kind": "log", "payload": {"i": 3}}) is None
        elapsed = time.monotonic() - start
        assert elapsed < 0.2
        assert store.dropped == 1
    finally:
        release.set()
        first.join(5.0)
        second.join(5.0)
        third.join(5.0)
        store.close()

    assert not first.is_alive()
    assert not second.is_alive()
    assert not third.is_alive()
    assert append_errors == []


def test_redactor_scrubs_payload_but_preserves_structural_event_ids(tmp_path) -> None:
    opaque_key = "opaque-worker-controlled-secret-1234567890"
    event = {
        "kind": "result",
        "payload": {"message": f"provider returned {opaque_key}", "status": "ok"},
        "task_id": opaque_key,
        "request_id": opaque_key,
    }

    redacting = EventStore(
        tmp_path / "redacted.db",
        fsync_interval_s=5.0,
        redactor=Redactor(secret_values={opaque_key}),
    )
    plain = EventStore(tmp_path / "plain.db", fsync_interval_s=5.0)
    try:
        redacting.append(event)
        plain.append(event)
    finally:
        redacting.close()
        plain.close()

    def durable_rows(path):
        conn = sqlite3.connect(path)
        try:
            return conn.execute(
                "SELECT payload, task_id, request_id FROM events ORDER BY seq"
            ).fetchall()
        finally:
            conn.close()

    (payload, task_id, request_id) = durable_rows(tmp_path / "redacted.db")[0]
    assert opaque_key not in payload
    assert task_id == opaque_key
    assert request_id == opaque_key
    assert "***" in payload

    (payload, task_id, request_id) = durable_rows(tmp_path / "plain.db")[0]
    assert opaque_key in payload
    assert task_id == opaque_key
    assert request_id == opaque_key


def test_redactor_runs_before_storage_on_long_strings_with_shaped_key(tmp_path) -> None:
    opaque_key = "opaque-worker-key-" + "B" * 24
    shaped_key = "sk-proj-" + "A" * 40
    store = EventStore(
        tmp_path / "events.db",
        fsync_interval_s=5.0,
        redactor=Redactor(secret_values={opaque_key}),
    )
    try:
        store.append({
            "kind": "result",
            "payload": {"message": "x" * 500 + " " + shaped_key, "stderr": opaque_key},
            "request_id": "x" * 500 + " " + shaped_key,
        })
    finally:
        store.close()

    conn = sqlite3.connect(tmp_path / "events.db")
    try:
        payload, request_id = conn.execute(
            "SELECT payload, request_id FROM events WHERE seq = 1"
        ).fetchone()
    finally:
        conn.close()

    assert shaped_key not in payload
    assert shaped_key[:12] not in payload
    assert opaque_key not in payload
    assert shaped_key not in request_id
    assert shaped_key[:12] not in request_id
    assert opaque_key not in request_id


@pytest.mark.slow
def test_session_artifacts_are_private_under_permissive_umask(tmp_path) -> None:
    """umask 0022 must not widen .cambium (0700) or events.db/-wal/-shm (0600)."""
    session = tmp_path / "session"
    script = (
        "import os, stat, sys\n"
        "from pathlib import Path\n"
        "os.umask(0o022)\n"
        "from cambium.store import EventStore\n"
        "session = Path(sys.argv[1])\n"
        "store = EventStore(session / '.cambium' / 'events.db', fsync_interval_s=60.0)\n"
        "store.append({'kind': 'result', 'payload': {'ok': True}})\n"
        "base = session / '.cambium'\n"
        "assert stat.S_IMODE(base.stat().st_mode) == 0o700, base\n"
        "for rel in ('events.db', 'events.db-wal', 'events.db-shm'):\n"
        "    path = base / rel\n"
        "    assert path.exists(), rel\n"
        "    assert stat.S_IMODE(path.stat().st_mode) == 0o600, (\n"
        "        rel, oct(stat.S_IMODE(path.stat().st_mode)))\n"
        "store.close()\n"
    )
    proc = subprocess.run(
        [sys.executable, "-c", script, str(session)], capture_output=True, text=True, timeout=120
    )
    assert proc.returncode == 0, proc.stderr


@pytest.mark.slow
def test_reopen_repairs_preseeded_permissive_artifacts(tmp_path) -> None:
    """A crash leaves -wal/-shm behind; a permissive preseed must be repaired on reopen."""
    session = tmp_path / "session"
    crash = (
        "import os, sys\n"
        "from pathlib import Path\n"
        "from cambium.store import EventStore\n"
        "store = EventStore(Path(sys.argv[1]) / '.cambium' / 'events.db')\n"
        "for i in range(5):\n"
        "    store.append({'kind': 'result', 'payload': {'i': i}})\n"
        "os._exit(9)\n"
    )
    proc = subprocess.run(
        [sys.executable, "-c", crash, str(session)], capture_output=True, text=True, timeout=120
    )
    assert proc.returncode == 9, proc.stderr
    base = session / ".cambium"
    for rel in ("events.db", "events.db-wal", "events.db-shm"):
        assert (base / rel).exists(), rel
    os.chmod(base, 0o755)
    os.chmod(base / "events.db", 0o644)
    os.chmod(base / "events.db-wal", 0o644)
    os.chmod(base / "events.db-shm", 0o644)

    reopen = (
        "import os, stat, sys\n"
        "from pathlib import Path\n"
        "os.umask(0o022)\n"
        "from cambium.store import EventStore\n"
        "session = Path(sys.argv[1])\n"
        "base = session / '.cambium'\n"
        "store = EventStore(base / 'events.db', fsync_interval_s=60.0)\n"
        "store.append({'kind': 'result', 'payload': {'ok': True}})\n"
        "assert stat.S_IMODE(base.stat().st_mode) == 0o700, base\n"
        "for rel in ('events.db', 'events.db-wal', 'events.db-shm'):\n"
        "    path = base / rel\n"
        "    assert path.exists(), rel\n"
        "    assert stat.S_IMODE(path.stat().st_mode) == 0o600, (\n"
        "        rel, oct(stat.S_IMODE(path.stat().st_mode)))\n"
        "store.close()\n"
    )
    proc = subprocess.run(
        [sys.executable, "-c", reopen, str(session)], capture_output=True, text=True, timeout=120
    )
    assert proc.returncode == 0, proc.stderr
