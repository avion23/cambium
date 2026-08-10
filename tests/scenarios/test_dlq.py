"""Scenario tests for the durable bounded dead-letter queue."""

from __future__ import annotations

import subprocess
import sys
import threading
from pathlib import Path

import pytest

from cambium.dlq import DeadLetterQueue
from cambium.redact import Redactor


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


def test_injected_redactor_scrubs_opaque_values_before_persistence(tmp_path) -> None:
    opaque_key = "opaque-dlq-secret-9876543210"
    queue = DeadLetterQueue(tmp_path, redactor=Redactor(secret_values={opaque_key}))
    record = _record("task-injected")
    record["stderr_tail"] = f"provider failed with {opaque_key}"
    record["payload"] = {"message": opaque_key}

    path = queue.put(record)
    content = path.read_bytes()

    assert opaque_key.encode() not in content
    assert b"***" in content


def test_default_queue_preserves_opaque_values_without_registry(tmp_path) -> None:
    opaque_key = "opaque-dlq-value-not-registered-42"
    queue = DeadLetterQueue(tmp_path)
    record = _record("task-plain")
    record["stderr_tail"] = opaque_key

    path = queue.put(record)
    content = path.read_bytes()

    assert opaque_key.encode() in content
    assert content.count(b"***") == 0


def test_queue_and_records_are_private_under_permissive_umask(tmp_path) -> None:
    """umask 0022 must not widen the DLQ dir (0700) or its records (0600)."""
    session = tmp_path / "session"
    script = (
        "import os, stat, sys\n"
        "from pathlib import Path\n"
        "os.umask(0o022)\n"
        "from cambium.dlq import DeadLetterQueue\n"
        "session = Path(sys.argv[1])\n"
        "queue = DeadLetterQueue(session)\n"
        "record = queue.put({'task_id': 't', 'generation': 1, 'status': 'failed'})\n"
        "dlq = session / '.cambium' / 'dlq'\n"
        "assert stat.S_IMODE(dlq.stat().st_mode) == 0o700, dlq\n"
        "assert stat.S_IMODE(record.stat().st_mode) == 0o600, record\n"
    )
    proc = subprocess.run(
        [sys.executable, "-c", script, str(session)], capture_output=True, text=True, timeout=120
    )
    assert proc.returncode == 0, proc.stderr


def test_reopen_repairs_preseeded_permissive_dlq_records(tmp_path) -> None:
    """Reopening a queue repairs a permissively preseeded dir and record files."""
    session = tmp_path / "session"
    script = (
        "import os, stat, sys\n"
        "from pathlib import Path\n"
        "os.umask(0o022)\n"
        "from cambium.dlq import DeadLetterQueue\n"
        "session = Path(sys.argv[1])\n"
        "queue = DeadLetterQueue(session)\n"
        "first = queue.put({'task_id': 't1', 'generation': 1, 'status': 'failed'})\n"
        "dlq = session / '.cambium' / 'dlq'\n"
        "os.chmod(dlq, 0o755)\n"
        "os.chmod(first, 0o644)\n"
        "reopened = DeadLetterQueue(session)\n"
        "assert stat.S_IMODE(dlq.stat().st_mode) == 0o700, dlq\n"
        "assert stat.S_IMODE(first.stat().st_mode) == 0o600, first\n"
        "second = reopened.put({'task_id': 't2', 'generation': 1, 'status': 'failed'})\n"
        "assert stat.S_IMODE(second.stat().st_mode) == 0o600, second\n"
        "assert len(reopened.entries()) == 2\n"
    )
    proc = subprocess.run(
        [sys.executable, "-c", script, str(session)], capture_output=True, text=True, timeout=120
    )
    assert proc.returncode == 0, proc.stderr
