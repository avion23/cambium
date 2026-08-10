"""Scenario tests for the SQLite-backed dead-letter queue."""

from __future__ import annotations

import json
import os
import sqlite3
import stat
import subprocess
import sys
import threading

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
        "payload": {"message": "protocol message rejected"},
    }


def test_put_get_roundtrip_and_entries_include_id(tmp_path) -> None:
    queue = DeadLetterQueue(tmp_path)
    record = _record("task-roundtrip")

    row_id = queue.put(record)

    assert isinstance(row_id, int)
    assert queue.get(row_id) == record
    assert queue.entries() == [record | {"id": row_id}]
    queue.close()


def test_default_redactor_scrubs_secrets(tmp_path) -> None:
    queue = DeadLetterQueue(tmp_path)
    secret = "sk-proj-12345678901234567890"
    record = _record("task-redaction")
    record["payload"] = {"prompt": secret, "TEST_API_KEY": "test-api-value"}

    row_id = queue.put(record)

    persisted = json.dumps(queue.get(row_id))
    assert secret not in persisted
    assert "test-api-value" not in persisted
    assert "***" in persisted
    queue.close()


def test_prune_sql_keeps_newest_records(tmp_path) -> None:
    queue = DeadLetterQueue(tmp_path, max_entries=3)
    row_ids = [queue.put(_record(f"task-{index}")) for index in range(5)]

    assert [entry["id"] for entry in queue.entries()] == row_ids[-3:]
    with sqlite3.connect(tmp_path / ".cambium" / "dlq.db") as connection:
        assert connection.execute("SELECT id FROM dlq_records ORDER BY id").fetchall() == [
            (row_id,) for row_id in row_ids[-3:]
        ]
    queue.close()


def test_summarize_uses_sql_fallbacks(tmp_path) -> None:
    queue = DeadLetterQueue(tmp_path)
    queue.put(_record("task-1", status="failed", reason="timeout"))
    queue.put(_record("task-2", status="", reason=""))
    third = _record("task-3")
    third.pop("status")
    third.pop("reason")
    third.pop("failure_kind")
    queue.put(third)

    assert queue.summarize() == {
        "total": 3,
        "by_status": {"failed": 1, "unknown": 2},
        "by_reason": {"timeout": 1, "worker": 1, "unknown": 1},
    }
    queue.close()


def test_remove_missing_is_harmless(tmp_path) -> None:
    queue = DeadLetterQueue(tmp_path)
    row_id = queue.put(_record("task-remove"))

    queue.remove(row_id)
    queue.remove(row_id)

    assert queue.entries() == []
    with pytest.raises(FileNotFoundError):
        queue.get(row_id)
    queue.close()


def test_get_and_remove_require_positive_exact_integer_id(tmp_path) -> None:
    queue = DeadLetterQueue(tmp_path)

    for row_id in (True, False, 0, -1, 1.0, "1"):
        with pytest.raises(ValueError, match="row_id must be a positive integer"):
            queue.get(row_id)
        with pytest.raises(ValueError, match="row_id must be a positive integer"):
            queue.remove(row_id)

    queue.close()


def test_writer_redacts_again_after_enqueue(tmp_path, monkeypatch) -> None:
    redactor = Redactor(secret_values={"first-secret", "second-secret"})
    queue = DeadLetterQueue(tmp_path, redactor=redactor)
    original = redactor.redact_mapping
    calls = 0

    def redact_in_stages(record):
        nonlocal calls
        calls += 1
        result = original(record)
        if calls == 1:
            result["payload"] = {"message": "second-secret"}
        return result

    monkeypatch.setattr(redactor, "redact_mapping", redact_in_stages)
    row_id = queue.put({"payload": {"message": "first-secret"}})

    assert calls == 2
    assert queue.get(row_id)["payload"]["message"] == "***"
    queue.close()


def test_crash_durability_preserves_acknowledged_records(tmp_path) -> None:
    session = tmp_path / "crash"
    script = (
        "import os, sys\n"
        "from pathlib import Path\n"
        "from cambium.dlq import DeadLetterQueue\n"
        "queue = DeadLetterQueue(Path(sys.argv[1]))\n"
        "for index in range(30):\n"
        "    queue.put({'task_id': f'task-{index}', 'status': 'failed'})\n"
        "os._exit(9)\n"
    )
    result = subprocess.run([sys.executable, "-c", script, str(session)], timeout=120)
    assert result.returncode == 9

    reopened = DeadLetterQueue(session)
    assert [entry["task_id"] for entry in reopened.entries()] == [
        f"task-{index}" for index in range(30)
    ]
    with sqlite3.connect(session / ".cambium" / "dlq.db") as connection:
        assert connection.execute("PRAGMA integrity_check").fetchone() == ("ok",)
    reopened.close()


def test_refuses_newer_schema_version(tmp_path) -> None:
    database = tmp_path / ".cambium" / "dlq.db"
    database.parent.mkdir(parents=True)
    with sqlite3.connect(database) as connection:
        connection.execute("PRAGMA user_version = 2")

    with pytest.raises(RuntimeError, match="unsupported DLQ schema version 2"):
        DeadLetterQueue(tmp_path)


def test_single_writer_thread_handles_concurrent_puts(tmp_path) -> None:
    queue = DeadLetterQueue(tmp_path, max_entries=100)
    writer_ident = queue._thread.ident
    errors: list[BaseException] = []

    def put(index: int) -> None:
        try:
            queue.put(_record(f"task-{index}"))
        except BaseException as exc:  # pragma: no cover - assertion reports failures
            errors.append(exc)

    threads = [threading.Thread(target=put, args=(index,)) for index in range(40)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()

    assert errors == []
    assert len(queue.entries()) == 40
    assert queue._thread.ident == writer_ident
    queue.close()


def test_database_and_session_directory_are_private(tmp_path) -> None:
    session = tmp_path / "session"
    old_umask = os.umask(0o022)
    try:
        queue = DeadLetterQueue(session)
        queue.put(_record("task-mode"))
    finally:
        os.umask(old_umask)

    assert stat.S_IMODE((session / ".cambium").stat().st_mode) == 0o700
    assert stat.S_IMODE((session / ".cambium" / "dlq.db").stat().st_mode) == 0o600
    queue.close()


def test_reopen_repairs_database_and_session_directory_modes(tmp_path) -> None:
    session = tmp_path / "session"
    queue = DeadLetterQueue(session)
    queue.put(_record("task-mode"))
    queue.close()
    os.chmod(session / ".cambium", 0o755)
    os.chmod(session / ".cambium" / "dlq.db", 0o644)

    reopened = DeadLetterQueue(session)

    assert stat.S_IMODE((session / ".cambium").stat().st_mode) == 0o700
    assert stat.S_IMODE((session / ".cambium" / "dlq.db").stat().st_mode) == 0o600
    reopened.close()
