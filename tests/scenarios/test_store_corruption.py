from __future__ import annotations

import json
import sqlite3

import pytest

from cambium.store import EventStore, StoreError, read_events_file


def _event(seq: int, event_id: str = "event-1") -> dict:
    return {
        "event_id": event_id,
        "seq": seq,
        "kind": "result",
        "ts": 1.0,
        "monotonic_ms": 10,
        "task_id": "task-1",
        "worker_id": "task-1:1",
        "generation": 1,
        "request_id": "request-1",
        "payload": {"seq": seq},
    }


def test_read_events_file_skips_only_torn_trailing_json(tmp_path) -> None:
    path = tmp_path / "events.jsonl"
    first = _event(1)
    path.write_text(
        json.dumps(first) + "\n" + json.dumps(_event(2, "event-2"))[:-1],
        encoding="utf-8",
    )

    assert read_events_file(path) == [first]


def test_read_events_file_fails_on_interleaved_garbage(tmp_path) -> None:
    path = tmp_path / "events.jsonl"
    path.write_text(
        json.dumps(_event(1)) + "\ngarbage\n" + json.dumps(_event(2, "event-2")),
        encoding="utf-8",
    )

    with pytest.raises(StoreError):
        read_events_file(path)


@pytest.mark.parametrize("contents", [b"", b"\n\n\r\n"])
def test_read_events_file_accepts_empty_line_files(tmp_path, contents: bytes) -> None:
    path = tmp_path / "events.jsonl"
    path.write_bytes(contents)

    assert read_events_file(path) == []


def test_read_events_file_rejects_duplicate_event_ids(tmp_path) -> None:
    path = tmp_path / "events.jsonl"
    path.write_text(
        json.dumps(_event(1, "same")) + "\n" + json.dumps(_event(2, "same")),
        encoding="utf-8",
    )

    with pytest.raises(StoreError):
        read_events_file(path)


@pytest.mark.parametrize("field", ["seq", "kind", "payload"])
def test_read_events_file_rejects_missing_event_fields(tmp_path, field: str) -> None:
    path = tmp_path / "events.jsonl"
    event = _event(1)
    del event[field]
    path.write_text(json.dumps(event), encoding="utf-8")

    with pytest.raises(StoreError):
        read_events_file(path)


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("seq", "1"),
        ("kind", 1),
        ("payload", []),
        ("event_id", 1),
        ("generation", "1"),
    ],
)
def test_read_events_file_rejects_wrong_type_fields(tmp_path, field: str, value) -> None:
    path = tmp_path / "events.jsonl"
    event = _event(1)
    event[field] = value
    path.write_text(json.dumps(event), encoding="utf-8")

    with pytest.raises(StoreError):
        read_events_file(path)


def _make_sqlite_store(path) -> None:
    store = EventStore(path, fsync_interval_s=60.0)
    store.append({"kind": "log", "payload": {"seq": 1}})
    store.append({"kind": "result", "payload": {"seq": 2}})
    store.close()


def _damage_payload(path, seq: int) -> None:
    conn = sqlite3.connect(path)
    try:
        conn.execute("UPDATE events SET payload = ? WHERE seq = ?", ('{"broken":', seq))
        conn.commit()
    finally:
        conn.close()


def test_sqlite_torn_trailing_payload_is_skipped(tmp_path) -> None:
    path = tmp_path / "events.db"
    _make_sqlite_store(path)
    _damage_payload(path, 2)

    events = read_events_file(path)

    assert [event["seq"] for event in events] == [1]


def test_sqlite_mid_file_payload_corruption_fails_closed(tmp_path) -> None:
    path = tmp_path / "events.db"
    _make_sqlite_store(path)
    _damage_payload(path, 1)

    with pytest.raises(StoreError):
        read_events_file(path)


def test_event_store_reader_uses_the_same_torn_tail_semantic(tmp_path) -> None:
    path = tmp_path / "events.db"
    _make_sqlite_store(path)
    _damage_payload(path, 2)
    store = EventStore(path, fsync_interval_s=60.0)
    try:
        assert [event["seq"] for event in store.events_after(0)] == [1]
    finally:
        store.close()
