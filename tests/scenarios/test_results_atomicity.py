from __future__ import annotations

import json
import os
import threading
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import pytest

from cambium import results as results_module
from cambium.results import (
    EXIT_CODES,
    ROOT_RESULT_KEYS,
    Result,
    result_to_dict,
    root_result_from_child,
    root_result_from_wire,
    write_result,
)


def _result(session_dir: Path, status: str = "done", marker: str = "result") -> Result:
    return Result(
        status=status,
        exit_code=EXIT_CODES[status],
        commits=tuple(f"{marker}-commit-{index}" for index in range(512)),
        files_changed=tuple(f"{marker}/file-{index}.py" for index in range(512)),
        unified_diff=marker * 16384,
        diff_truncated=False,
        summary=marker,
        metric_score=0.5,
        metric_breakdown={"tests": 0.5},
        parent_task_id=None,
        event_log_ref=f"sqlite:{session_dir / '.cambium' / 'events.db'}",
        session_id="session-1",
        started_at=10.0,
        ended_at=11.0,
        failure_reason=None if status == "done" else marker,
    )


def test_write_result_preserves_utf8_json_and_private_permissions(tmp_path: Path) -> None:
    result = _result(tmp_path, marker="marker-\u03a9")
    path = write_result(result, tmp_path, session_id="session-1")
    expected = (
        json.dumps(
            result_to_dict(result),
            ensure_ascii=False,
            allow_nan=False,
            separators=(",", ":"),
        )
        + "\n"
    ).encode("utf-8")

    assert path.read_bytes() == expected
    assert os.stat(path).st_mode & 0o777 == 0o600


def test_replace_failure_keeps_previous_result_and_removes_temp_file(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    previous = _result(tmp_path, marker="previous")
    replacement = _result(tmp_path, marker="replacement")
    path = write_result(previous, tmp_path, session_id="session-1")

    def fail_replace(source: object, target: object) -> None:
        raise OSError("replace failed")

    monkeypatch.setattr(results_module.os, "replace", fail_replace)

    with pytest.raises(OSError, match="replace failed"):
        write_result(replacement, tmp_path, session_id="session-1")

    assert json.loads(path.read_text(encoding="utf-8")) == result_to_dict(previous)
    assert list(path.parent.glob(".result.json.*.tmp")) == []


def test_concurrent_writers_publish_complete_last_writer_result(tmp_path: Path) -> None:
    initial = _result(tmp_path, marker="initial")
    path = write_result(initial, tmp_path, session_id="session-1")
    candidates = tuple(
        _result(tmp_path, status=status, marker=f"writer-{index}")
        for index, status in enumerate(
            ("done", "failed", "rejected", "timeout", "cancelled", "done", "failed", "timeout")
        )
    )
    allowed_records = [result_to_dict(initial)] + [result_to_dict(result) for result in candidates]
    barrier = threading.Barrier(len(candidates))
    stop = threading.Event()
    invalid_reads: list[bool] = []

    def observe() -> None:
        while not stop.is_set():
            try:
                record = json.loads(path.read_bytes())
            except (FileNotFoundError, json.JSONDecodeError, UnicodeDecodeError):
                invalid_reads.append(True)
                continue
            if set(record) != set(ROOT_RESULT_KEYS) or record not in allowed_records:
                invalid_reads.append(True)

    observer = threading.Thread(target=observe)
    observer.start()

    def publish(result: Result) -> None:
        barrier.wait()
        write_result(result, tmp_path, session_id="session-1")

    with ThreadPoolExecutor(max_workers=len(candidates)) as pool:
        list(pool.map(publish, candidates))
    stop.set()
    observer.join()

    final = json.loads(path.read_text(encoding="utf-8"))
    assert invalid_reads == []
    assert final in [result_to_dict(result) for result in candidates]
    assert list(path.parent.glob(".result.json.*.tmp")) == []


def test_explicit_timestamps_do_not_read_the_clock(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    def fail_clock() -> float:
        raise AssertionError("clock should not be read")

    monkeypatch.setattr(results_module.time, "time", fail_clock)
    wire_result = root_result_from_wire(
        {"status": "succeeded", "started_at": 10.0, "ended_at": 11.0},
        tmp_path,
        session_id="session-1",
    )
    child_result = root_result_from_child(
        {"status": "done"},
        tmp_path,
        session_id="session-1",
        started_at=20.0,
        ended_at=21.0,
    )

    assert (wire_result.started_at, wire_result.ended_at) == (10.0, 11.0)
    assert (child_result.started_at, child_result.ended_at) == (20.0, 21.0)


def test_missing_timestamps_sample_the_clock_for_start_and_end(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    values = iter((30.0, 31.0))

    def clock() -> float:
        return next(values)

    monkeypatch.setattr(results_module.time, "time", clock)
    result = root_result_from_wire({"status": "succeeded"}, tmp_path, session_id="session-1")

    assert (result.started_at, result.ended_at) == (30.0, 31.0)


@pytest.mark.parametrize(
    ("wire", "expected_status"),
    [
        ({"status": "failed"}, "failed"),
        ({"status": "error"}, "failed"),
        ({"status": "crashed"}, "failed"),
        ({"status": "protocol_error"}, "failed"),
        ({"status": "restart_exhausted"}, "failed"),
        ({"status": "succeeded", "merge_status": "failed"}, "failed"),
        ({"status": "evaluator_reject"}, "rejected"),
        ({"status": "succeeded", "evaluator": {"ok": False}}, "rejected"),
        ({"status": "timeout"}, "timeout"),
        ({"status": "failed", "reason": "watchdog_timeout"}, "timeout"),
        ({"status": "cancelled"}, "cancelled"),
        ({"kind": "worker_killed", "payload": {"reason": "shutdown"}}, "cancelled"),
    ],
)
def test_error_paths_use_documented_exit_codes(
    tmp_path: Path, wire: dict[str, object], expected_status: str
) -> None:
    result = root_result_from_wire(
        wire,
        tmp_path,
        session_id="session-1",
        started_at=10.0,
        ended_at=11.0,
    )

    assert result.status == expected_status
    assert result.status in EXIT_CODES
    assert result.exit_code == EXIT_CODES[result.status]


def test_exit_codes_cover_the_canonical_result_statuses() -> None:
    assert EXIT_CODES == {
        "done": 0,
        "failed": 1,
        "rejected": 2,
        "timeout": 3,
        "cancelled": 4,
    }
