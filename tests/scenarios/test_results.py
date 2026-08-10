"""Accepted result-boundary contract scenarios."""

from __future__ import annotations

import json
import os
from dataclasses import fields, replace
from pathlib import Path

import pytest

from cambium.results import (
    CHILD_RESULT_KEYS,
    EXIT_CODES,
    ROOT_RESULT_KEYS,
    Result,
    result_to_dict,
    root_result_from_wire,
    status_from_wire,
    wire_to_child_result,
    write_result,
)
from cambium.supervisor import TaskResult


def _wire(**overrides: object) -> dict[str, object]:
    wire: dict[str, object] = {
        "type": "result_envelope",
        "request_id": "request-1",
        "generation": 4,
        "status": "succeeded",
        "exit_code": 99,
        "commits": ["abc123"],
        "files_changed": ["src/example.py"],
        "diff": "diff --git a/src/example.py b/src/example.py",
        "diff_truncated": False,
        "summary": "updated the example",
        "metrics": {"metric_score": 0.84, "metric_breakdown": {"tests": 1.0}},
        "failure_reason": "wire-only failure detail",
        "started_at": 10.0,
        "ended_at": 11.0,
        "stdout": "wire stdout",
        "stderr": "wire stderr",
        "scratchpad": "CANARY scratchpad",
        "reasoning": "CANARY reasoning",
        "trajectory": [{"tool": "CANARY"}],
    }
    wire.update(overrides)
    return wire


def _root(tmp_path: Path, **overrides: object) -> Result:
    return root_result_from_wire(
        _wire(**overrides),
        tmp_path,
        session_id="session-1",
        started_at=10.0,
        ended_at=11.0,
    )


def test_child_keys_are_exact_and_ordered() -> None:
    assert CHILD_RESULT_KEYS == (
        "parent_task_id",
        "unified_diff",
        "diff_truncated",
        "summary",
        "metric_score",
        "metric_breakdown",
        "commits",
        "files_changed",
        "status",
    )
    child = wire_to_child_result(_wire(parent_task_id="parent-1"))
    assert tuple(child) == CHILD_RESULT_KEYS
    assert tuple(child.keys()) == CHILD_RESULT_KEYS


def test_child_mapper_allowlist_and_scratchpad_canary() -> None:
    child = wire_to_child_result(_wire(parent_task_id="parent-1"))
    assert set(child) == set(CHILD_RESULT_KEYS)
    assert child["parent_task_id"] == "parent-1"
    assert child["unified_diff"].startswith("diff --git")
    for forbidden in (
        "type",
        "request_id",
        "generation",
        "exit_code",
        "started_at",
        "ended_at",
        "failure_reason",
        "stdout",
        "stderr",
        "scratchpad",
        "reasoning",
        "trajectory",
    ):
        assert forbidden not in child
    assert "CANARY" not in json.dumps(child)


def test_child_mapper_always_emits_empty_diff_when_omitted() -> None:
    child = wire_to_child_result(_wire(include_diff=False, diff="must not cross"))
    assert tuple(child) == CHILD_RESULT_KEYS
    assert child["unified_diff"] == ""

    no_diff = wire_to_child_result({"status": "succeeded"})
    assert tuple(no_diff) == CHILD_RESULT_KEYS
    child_without_diff = wire_to_child_result(_wire(diff=None))
    assert tuple(child_without_diff) == CHILD_RESULT_KEYS
    assert child_without_diff["unified_diff"] == ""


@pytest.mark.parametrize(
    ("wire", "expected"),
    [
        ({"status": "succeeded", "gate_exit_code": 0, "merge_status": "ok"}, "done"),
        ({"status": "failed"}, "failed"),
        ({"status": "succeeded", "gate_exit_code": 1}, "failed"),
        ({"status": "succeeded", "merge_status": "failed"}, "failed"),
        ({"status": "timeout"}, "timeout"),
        ({"status": "cancellation"}, "cancelled"),
        ({"status": "evaluator_reject"}, "rejected"),
        ({"status": "crashed"}, "failed"),
        ({"status": "restart_exhausted"}, "failed"),
        ({"status": "protocol_error"}, "failed"),
    ],
)
def test_status_conversion_table(wire: dict[str, object], expected: str) -> None:
    assert status_from_wire(wire) == expected
    assert wire_to_child_result(wire)["status"] == expected


@pytest.mark.parametrize(
    "wire",
    [
        {"status": "failed", "reason": "watchdog_timeout"},
        {"kind": "worker_killed", "payload": {"reason": "watchdog_timeout"}},
        {"status": "failed", "reason": "ready_timeout"},
        {"kind": "worker_killed", "payload": {"reason": "ping_no_pong"}},
    ],
)
def test_timeout_reasons_map_to_timeout_exit_code(wire: dict[str, object]) -> None:
    status = status_from_wire(wire)

    assert status == "timeout"
    assert EXIT_CODES[status] == 3
    assert wire_to_child_result(wire)["status"] == "timeout"


@pytest.mark.parametrize(
    "wire",
    [
        {"status": "succeeded", "gate_exit_code": 1},
        {"status": "succeeded", "merge_status": "failed"},
    ],
)
def test_gate_and_merge_failures_keep_failed_exit_code(wire: dict[str, object]) -> None:
    status = status_from_wire(wire)

    assert status == "failed"
    assert EXIT_CODES[status] == 1


def test_successful_evaluator_verdict_maps_to_done() -> None:
    status = status_from_wire({"status": "succeeded", "evaluator": {"ok": True}})

    assert status == "done"
    assert EXIT_CODES[status] == 0
    assert status_from_wire({"status": "succeeded", "evaluator": {"ok": False}}) == (
        "rejected"
    )


def test_success_reason_is_advisory_not_cancellation(tmp_path: Path) -> None:
    result = root_result_from_wire(
        {
            "status": "succeeded",
            "gate_exit_code": 0,
            "merge_status": "ok",
            "reason": "success",
        },
        tmp_path,
        session_id="session-1",
        started_at=10.0,
        ended_at=11.0,
    )

    assert result.status == "done"
    assert result.exit_code == 0
    assert result.failure_reason is None


def test_result_has_exact_fifteen_root_fields_and_finalized_values(tmp_path: Path) -> None:
    result = _root(tmp_path, status="succeeded", metric_score=7)
    assert tuple(field.name for field in fields(Result)) == ROOT_RESULT_KEYS
    assert tuple(result_to_dict(result)) == ROOT_RESULT_KEYS
    assert len(fields(Result)) == 15
    assert result.parent_task_id is None
    assert isinstance(result.metric_score, float)
    assert result.metric_score == 7.0
    assert result.exit_code == 0
    assert result.event_log_ref == f"sqlite:{tmp_path}/.cambium/events.db"


def test_result_exit_codes_are_canonical(tmp_path: Path) -> None:
    for status, exit_code in (
        ("succeeded", 0),
        ("failed", 1),
        ("evaluator_reject", 2),
        ("timeout", 3),
        ("cancelled", 4),
    ):
        result = _root(tmp_path, status=status)
        assert result.exit_code == exit_code


def test_result_writer_uses_atomic_replace_and_leaves_no_temp(tmp_path: Path, monkeypatch) -> None:
    result = _root(tmp_path)
    calls: list[tuple[Path, Path]] = []
    original_replace = os.replace

    def tracked_replace(source: str | os.PathLike[str], target: str | os.PathLike[str]) -> None:
        calls.append((Path(source), Path(target)))
        original_replace(source, target)

    monkeypatch.setattr("cambium.results.os.replace", tracked_replace)
    path = write_result(result, tmp_path, session_id="session-1")

    assert path == tmp_path / ".cambium" / "result.json"
    assert calls
    source, target = calls[0]
    assert source.parent == target.parent == tmp_path / ".cambium"
    assert source.name.endswith(".tmp")
    assert target == path
    assert path.exists()
    assert list(path.parent.glob(".result.json.*.tmp")) == []
    assert json.loads(path.read_text()) == result_to_dict(result)


def test_writer_rejects_taskresult_wire_and_event_wrappers(tmp_path: Path) -> None:
    task_result = TaskResult(task_id="task-1", status="succeeded", exit_code=0)
    wire = _wire()
    event = {"type": "result", "timestamp": 0.0}
    for value in (task_result, wire, event):
        with pytest.raises(TypeError):
            result_to_dict(value)  # type: ignore[arg-type]
        with pytest.raises(TypeError):
            write_result(value, tmp_path, session_id="session-1")  # type: ignore[arg-type]


def test_session_id_must_be_explicit_and_match(tmp_path: Path) -> None:
    result = _root(tmp_path)
    with pytest.raises(TypeError):
        write_result(result, tmp_path)  # type: ignore[call-arg]
    with pytest.raises(ValueError):
        write_result(result, tmp_path, session_id="other-session")


def test_writer_requires_session_scoped_event_log_ref(tmp_path: Path) -> None:
    result = _root(tmp_path)
    foreign_ref = replace(result, event_log_ref="sqlite:/other/session/events.db")

    with pytest.raises(ValueError, match="event_log_ref"):
        write_result(foreign_ref, tmp_path, session_id="session-1")

    path = write_result(result, tmp_path, session_id="session-1")
    assert path == tmp_path / ".cambium" / "result.json"
