"""Scenario tests for the durable bounded dead-letter queue."""

from __future__ import annotations

import threading
from pathlib import Path

import pytest

from cambium.dlq import DeadLetterQueue


def _record(task_id: str, *, status: str = "failed", reason: str = "protocol_error") -> dict:
    return {
        "task_id": task_id,
        "generation": 3,
        "status": status,
        "reason": reason,
        "failure_kind": "worker",
        "stderr_tail": "worker stopped",
        "gate_exit": 1,
        "merge_info": {"published": False},
        "payload": {"message": "protocol message rejected"},
    }


def test_put_get_roundtrip_and_entries_include_filename(tmp_path) -> None:
    queue = DeadLetterQueue(tmp_path)
    record = _record("task-roundtrip")

    path = queue.put(record)

    assert path == tmp_path / ".cambium" / "dlq" / path.name
    assert queue.get(path.name) == record
    entries = queue.entries()
    assert len(entries) == 1
    assert entries[0]["file"] == path.name
    assert {key: value for key, value in entries[0].items() if key != "file"} == record


def test_redaction_is_applied_when_redactor_is_available(tmp_path) -> None:
    pytest.importorskip("cambium.redact")
    queue = DeadLetterQueue(tmp_path)
    secret = "sk-proj-12345678901234567890"
    record = _record("task-redaction")
    record["payload"] = {"prompt": secret, "TEST_API_KEY": "test-api-value"}

    path = queue.put(record)
    content = path.read_bytes()

    assert secret.encode() not in content
    assert b"test-api-value" not in content
    assert b"***" in content


def test_bounded_queue_keeps_three_newest_records(tmp_path) -> None:
    queue = DeadLetterQueue(tmp_path, max_entries=3)
    paths = [queue.put(_record(f"task-{index}")) for index in range(5)]

    assert [entry["file"] for entry in queue.entries()] == [path.name for path in paths[-3:]]


def test_prune_skips_file_removed_before_stat(tmp_path, monkeypatch) -> None:
    queue = DeadLetterQueue(tmp_path, max_entries=1)
    first = queue.put(_record("task-first"))
    original_is_file = Path.is_file
    original_stat = Path.stat
    removed = False

    def is_file_without_race(path: Path) -> bool:
        if path == first:
            return True
        return original_is_file(path)

    def stat_with_race(path: Path, *args, **kwargs):
        nonlocal removed
        if path == first and not removed:
            first.unlink()
            removed = True
        return original_stat(path, *args, **kwargs)

    monkeypatch.setattr(Path, "is_file", is_file_without_race)
    monkeypatch.setattr(Path, "stat", stat_with_race)

    second = queue.put(_record("task-second"))

    assert removed
    assert second.exists()
    assert [entry["file"] for entry in queue.entries()] == [second.name]


def test_summarize_counts_status_and_reason(tmp_path) -> None:
    queue = DeadLetterQueue(tmp_path)
    queue.put(_record("task-1", status="failed", reason="timeout"))
    queue.put(_record("task-2", status="failed", reason="timeout"))
    queue.put(_record("task-3", status="cancelled", reason="user"))

    assert queue.summarize() == {
        "total": 3,
        "by_status": {"failed": 2, "cancelled": 1},
        "by_reason": {"timeout": 2, "user": 1},
    }


def test_remove_deletes_record(tmp_path) -> None:
    queue = DeadLetterQueue(tmp_path)
    path = queue.put(_record("task-remove"))

    assert queue.remove(path.name) is None
    assert not path.exists()
    assert queue.entries() == []


def test_concurrent_puts_are_safe(tmp_path) -> None:
    queue = DeadLetterQueue(tmp_path, max_entries=100)
    errors: list[BaseException] = []

    def put(index: int) -> None:
        try:
            queue.put(_record(f"task-{index}"))
        except BaseException as exc:  # pragma: no cover - assertion reports failures
            errors.append(exc)

    threads = [threading.Thread(target=put, args=(index,)) for index in range(20)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()

    assert errors == []
    entries = queue.entries()
    assert len(entries) == 20
    assert {entry["task_id"] for entry in entries} == {f"task-{index}" for index in range(20)}
