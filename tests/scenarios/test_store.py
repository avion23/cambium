"""Scenario tests for the Cambium SQLite WAL event store (src/cambium/store.py).

No mocking libraries: every scenario drives the real EventStore against a temp
directory, including a crash-durability subprocess that mirrors the methodology
of docs/research/sqlite-wal-durability.md (os._exit mid-write, reopen, integrity
check).
"""

from __future__ import annotations

import sqlite3
import subprocess
import sys
import time

import pytest

from cambium.store import CRITICAL_KINDS, EventStore, StoreError, StoreInitError


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
        time.sleep(0.5)

    monkeypatch.setattr(EventStore, "_fsync_now", slow_fsync)
    try:
        start = time.monotonic()
        seq = store.append({"kind": "log", "payload": {"line": "x"}})
        elapsed = time.monotonic() - start
        assert seq == 1
        assert elapsed < 0.2  # returned without waiting on the writer's fsync

        start = time.monotonic()
        store.append({"kind": "result", "payload": {"ok": True}})
        blocked = time.monotonic() - start
        assert blocked >= 0.4  # the slow fsync is in effect; critical append waits
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


def test_writer_dead_on_locked_db_critical_append_raises(tmp_path) -> None:
    path = tmp_path / "events.db"
    store = _open(path)
    blocker = sqlite3.connect(path, isolation_level=None)
    try:
        blocker.execute("BEGIN EXCLUSIVE")
        start = time.monotonic()
        with pytest.raises(StoreError):
            store.append({"kind": "result", "payload": {"ok": True}})
        elapsed = time.monotonic() - start
        assert 4.0 <= elapsed < 30.0  # bounded by busy_timeout, not a hang
    finally:
        blocker.close()
    # store is dead: subsequent appends fail immediately, close() does not hang
    with pytest.raises(StoreError):
        store.append({"kind": "log", "payload": {}})
    with pytest.raises(StoreError):
        store.append({"kind": "result", "payload": {}})
    store.close()
