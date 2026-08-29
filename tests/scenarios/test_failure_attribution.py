from __future__ import annotations

import json
import os
import sqlite3
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
SCRIPT = ROOT / "scripts" / "failure_attribution.py"

_EVENTS_SCHEMA = """CREATE TABLE events (
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


def _session(
    tmp_path: Path,
    events: list[tuple[str, dict]],
    *,
    checkpoints: list[tuple[str, dict]] = (),
) -> Path:
    session = tmp_path / "session"
    database = session / ".cambium" / "events.db"
    database.parent.mkdir(parents=True)
    with sqlite3.connect(database) as connection:
        connection.execute(_EVENTS_SCHEMA)
        for kind, payload in events:
            connection.execute(
                "INSERT INTO events(kind, payload, task_id, generation) VALUES (?, ?, ?, ?)",
                (kind, json.dumps(payload), "task", 1),
            )
    for relative, payload in checkpoints:
        path = session / ".cambium" / "checkpoints" / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(payload), encoding="utf-8")
    return session


def _report(session: Path) -> dict:
    result = subprocess.run(
        [sys.executable, str(SCRIPT), "--json", str(session)],
        cwd=ROOT,
        env={**os.environ, "PYTHONPATH": str(ROOT / "src")},
        check=True,
        capture_output=True,
        text=True,
    )
    return json.loads(result.stdout)


def _detectors(report: dict) -> list[str]:
    return [item["detector"] for item in report["detectors_fired"]]


def test_retry_loop_uses_normalized_arguments(tmp_path: Path) -> None:
    session = _session(
        tmp_path,
        [
            ("tool_event", {"tool": "read_file", "arguments": {"b": 2, "a": 1}, "turn": 1}),
            ("tool_event", {"tool": "read_file", "arguments": {"a": 1, "b": 2}, "turn": 2}),
            ("tool_event", {"tool": "read_file", "arguments": {"a": 1, "b": 2}, "turn": 3}),
        ],
    )

    report = _report(session)

    assert _detectors(report) == ["retry-loop"]
    assert report["detectors_fired"][0]["evidence"] == [1, 2, 3]


def test_retry_loop_accepts_a_repeated_error_class(tmp_path: Path) -> None:
    session = _session(
        tmp_path,
        [
            ("tool_event", {"tool": f"tool-{turn}", "error_class": "ValueError", "turn": turn})
            for turn in range(1, 4)
        ],
    )

    report = _report(session)

    assert _detectors(report) == ["retry-loop"]


def test_finish_without_verification_reads_checkpoint_state(tmp_path: Path) -> None:
    session = _session(
        tmp_path,
        [("result", {"status": "succeeded", "turn": 3})],
        checkpoints=[
            (
                "task/turn-002.json",
                {
                    "turn": 2,
                    "generation": 1,
                    "code_changed": True,
                    "verified_after_change": False,
                },
            )
        ],
    )

    report = _report(session)

    assert _detectors(report) == ["finish-without-verification"]
    assert report["detectors_fired"][0]["first_turn"] == 3


def test_terminal_event_without_turn_uses_latest_checkpoint_turn(tmp_path: Path) -> None:
    session = _session(
        tmp_path,
        [("result", {"status": "succeeded", "code_changed": True})],
        checkpoints=[
            (
                "task/turn-007.json",
                {"turn": 7, "generation": 1, "code_changed": True},
            )
        ],
    )

    report = _report(session)

    assert report["detectors_fired"][0]["first_turn"] == 7


def test_objective_met_override_uses_terminal_action(tmp_path: Path) -> None:
    session = _session(
        tmp_path,
        [
            (
                "result",
                {
                    "status": "succeeded",
                    "turn": 4,
                    "terminal_action": {"type": "finish", "objective_met": False},
                },
            )
        ],
    )

    report = _report(session)

    assert _detectors(report) == ["objective_met-override"]
    assert report["detectors_fired"][0]["evidence"] == [4]


def test_read_churn_compares_reads_with_changed_files(tmp_path: Path) -> None:
    events = [
        (
            "tool_event",
            {
                "tool": "read_batch",
                "arguments": {"paths": [f"read-{turn}.py"]},
                "turn": turn,
            },
        )
        for turn in range(1, 5)
    ]
    report = _report(
        _session(
            tmp_path,
            events,
            checkpoints=[("task/turn-005.json", {"turn": 5, "files_changed": ["a.py"]})],
        )
    )

    assert _detectors(report) == ["read-churn"]
    assert report["detectors_fired"][0]["evidence"] == [1, 2, 3, 4]


def test_compaction_stall_accepts_one_deferral(tmp_path: Path) -> None:
    session = _session(
        tmp_path,
        [("log", {"turn": 6, "consecutive_compaction_deferrals": 1})],
    )

    report = _report(session)

    assert _detectors(report) == ["compaction-stall"]
    assert report["detectors_fired"][0]["first_turn"] == 6


def test_clean_session_has_no_detectors(tmp_path: Path) -> None:
    report = _report(
        _session(tmp_path, [("result", {"status": "succeeded", "turn": 1})])
    )

    assert report["verdict"] == "clean"
    assert report["detectors_fired"] == []
