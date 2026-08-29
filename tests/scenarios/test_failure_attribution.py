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


def _write_store(
    path: Path,
    events: list[tuple[str, dict, str | None, int | None]],
) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with sqlite3.connect(path) as connection:
        connection.execute(_EVENTS_SCHEMA)
        for kind, payload, task_id, generation in events:
            connection.execute(
                "INSERT INTO events(kind, payload, task_id, generation) VALUES (?, ?, ?, ?)",
                (kind, json.dumps(payload), task_id, generation),
            )


def _event(
    kind: str,
    payload: dict,
    *,
    task_id: str | None = "task",
    generation: int | None = 1,
) -> tuple[str, dict, str | None, int | None]:
    return kind, payload, task_id, generation


def _session(
    tmp_path: Path,
    events: list[tuple[str, dict, str | None, int | None]] = (),
    *,
    turns: dict[int, list[tuple[str, dict, str | None, int | None]]] | None = None,
    direct_root: bool = False,
) -> Path:
    session = tmp_path / "session"
    root_db = session / ("events.db" if direct_root else ".cambium/events.db")
    _write_store(root_db, list(events))
    for turn, turn_events in (turns or {}).items():
        _write_store(session / f"turn-{turn:04d}" / ".cambium" / "events.db", turn_events)
    return session


def _ordinary_checkpoint(
    session: Path,
    relative: str,
    *,
    task_id: str = "task",
    generation: int = 1,
    turn: int = 1,
    code_changed: bool = False,
) -> Path:
    path = session / ".cambium" / "checkpoints" / relative
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(
            {
                "schema": 1,
                "task": "fixture task",
                "generation": generation,
                "turn": turn,
                "transcript": [],
                "usage": {},
                "commits_so_far": [],
                "workspace_hash": "0" * 64,
                "compaction_deferred": False,
                "code_changed": code_changed,
            }
        ),
        encoding="utf-8",
    )
    return path


def _epoch_checkpoint(
    session: Path,
    relative: str,
    *,
    task_id: str = "task",
    generation: int = 1,
    turn: int = 1,
    code_changed: bool = False,
    verified: bool = False,
) -> Path:
    path = session / ".cambium" / "checkpoints" / relative
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(
            {
                "schema": 5,
                "content": {"provider_messages": [], "continuation_suffix": []},
                "meta": {
                    "task_id": task_id,
                    "generation": generation,
                    "epoch": 1,
                    "turn": turn,
                    "code_changed": code_changed,
                    "verified_after_change": verified,
                    "verification_failed": not verified,
                },
            }
        ),
        encoding="utf-8",
    )
    return path


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


def _read_cmd(path: str, *, offset: int | None = None, limit: int | None = None) -> str:
    args: dict[str, object] = {"paths": [path]}
    if offset is not None:
        args["offset"] = offset
    if limit is not None:
        args["limit"] = limit
    return "read_batch " + json.dumps(args, sort_keys=True)


def _write_cmd(path: str) -> str:
    return "write_file " + json.dumps({"content": "changed", "path": path}, sort_keys=True)


def test_retry_loop_uses_production_tool_event_command(tmp_path: Path) -> None:
    report = _report(
        _session(
            tmp_path,
            [
                _event(
                    "tool_event",
                    {"tool": "read_batch", "cmd": _read_cmd("same.py"), "ok": True, "turn": turn},
                )
                for turn in range(1, 4)
            ]
            + [
                _event(
                    "result",
                    {
                        "status": "succeeded",
                        "terminal_action": {"type": "finish", "objective_met": True},
                    },
                )
            ],
        )
    )

    assert _detectors(report) == ["retry-loop"]
    assert report["detectors_fired"][0]["evidence"] == [
        "root:event-1",
        "root:event-2",
        "root:event-3",
    ]


def test_finish_without_verification_reads_durable_checkpoint_state(tmp_path: Path) -> None:
    session = _session(
        tmp_path,
        [
            _event(
                "tool_event",
                {"tool": "write_file", "cmd": _write_cmd("a.py"), "ok": True, "turn": 1},
            ),
            _event("checkpoint", {}),
            _event(
                "result",
                {
                    "status": "succeeded",
                    "terminal_action": {"type": "finish", "objective_met": True},
                },
            ),
        ],
    )
    checkpoint = _ordinary_checkpoint(session, "task/turn-001.json", code_changed=True)
    connection = sqlite3.connect(session / ".cambium" / "events.db")
    connection.execute(
        "UPDATE events SET payload = ? WHERE kind = 'checkpoint'",
        (json.dumps({"turn": 1, "state_ref": str(checkpoint)}),),
    )
    connection.commit()
    connection.close()

    report = _report(session)

    assert _detectors(report) == ["finish-without-verification"]
    assert report["detectors_fired"][0]["evidence"] == ["root:event-3"]


def test_latest_epoch_checkpoint_can_prove_verification(tmp_path: Path) -> None:
    session = _session(
        tmp_path,
        [
            _event(
                "tool_event",
                {"tool": "write_file", "cmd": _write_cmd("a.py"), "ok": True, "turn": 1},
            ),
            _event(
                "tool_event",
                {
                    "tool": "run_shell",
                    "cmd": "run_shell pytest -q",
                    "ok": True,
                    "turn": 2,
                },
            ),
            _event("context_checkpoint", {"checkpoint_ref": "task/epoch-001-ref.json"}),
            _event(
                "result",
                {
                    "status": "succeeded",
                    "terminal_action": {"type": "finish", "objective_met": True},
                },
            ),
        ],
    )
    _epoch_checkpoint(
        session,
        "task/epoch-001-ref.json",
        turn=2,
        code_changed=True,
        verified=True,
    )

    report = _report(session)

    assert _detectors(report) == []
    assert report["verdict"] == "clean"


def test_objective_met_override_uses_durable_terminal_action(tmp_path: Path) -> None:
    report = _report(
        _session(
            tmp_path,
            [
                _event(
                    "result",
                    {
                        "status": "succeeded",
                        "terminal_action": {"type": "finish", "objective_met": False},
                    },
                )
            ],
        )
    )

    assert _detectors(report) == ["objective_met-override"]
    assert report["detectors_fired"][0]["evidence"] == ["root:event-1"]


def test_read_churn_fires_for_failed_zero_change_task(tmp_path: Path) -> None:
    reads = [
        _event(
            "tool_event",
            {"tool": "read_batch", "cmd": _read_cmd(f"read-{index}.py"), "ok": True, "turn": index},
        )
        for index in range(1, 5)
    ]
    report = _report(_session(tmp_path, reads + [_event("result", {"status": "failed"})]))

    assert _detectors(report) == ["read-churn"]
    assert len(report["detectors_fired"][0]["evidence"]) == 4
    assert report["detectors_fired"][0]["evidence"] == [
        "root:event-1",
        "root:event-2",
        "root:event-3",
        "root:event-4",
    ]


def test_successful_read_only_task_is_clean_with_warning(tmp_path: Path) -> None:
    report = _report(
        _session(
            tmp_path,
            [
                _event(
                    "tool_event",
                    {
                        "tool": "read_batch",
                        "cmd": _read_cmd(f"read-{index}.py"),
                        "ok": True,
                        "turn": index,
                    },
                )
                for index in range(1, 5)
            ]
            + [
                _event(
                    "result",
                    {
                        "status": "succeeded",
                        "terminal_action": {"type": "finish", "objective_met": True},
                    },
                )
            ],
        )
    )

    assert report["verdict"] == "clean"
    assert _detectors(report) == []
    assert report["warnings"][0]["detector"] == "read-churn"


def test_one_read_does_not_trigger_churn(tmp_path: Path) -> None:
    report = _report(
        _session(
            tmp_path,
            [
                _event(
                    "tool_event",
                    {"tool": "read_batch", "cmd": _read_cmd("one.py"), "ok": True, "turn": 1},
                ),
                _event(
                    "result",
                    {
                        "status": "succeeded",
                        "terminal_action": {"type": "finish", "objective_met": True},
                    },
                ),
            ],
        )
    )

    assert report["verdict"] == "clean"
    assert "warnings" not in report


def test_interactive_requests_do_not_share_retry_runs(tmp_path: Path) -> None:
    turns = {
        turn: [
            _event(
                "tool_event",
                {"tool": "git_op", "cmd": "git_op status", "ok": True, "turn": 1},
            ),
            _event(
                "tool_event",
                {"tool": "git_op", "cmd": "git_op status", "ok": True, "turn": 2},
            ),
            _event(
                "result",
                {
                    "status": "succeeded",
                    "terminal_action": {"type": "finish", "objective_met": True},
                },
            ),
        ]
        for turn in (1, 2)
    }

    report = _report(_session(tmp_path, turns=turns))

    assert "retry-loop" not in _detectors(report)
    assert report["verdict"] == "clean"


def test_interactive_evidence_has_source_order(tmp_path: Path) -> None:
    report = _report(
        _session(
            tmp_path,
            turns={
                1: [
                    _event(
                        "tool_event",
                        {"tool": "git_op", "cmd": "git_op status", "ok": True, "turn": 1},
                    ),
                    _event(
                        "tool_event",
                        {"tool": "git_op", "cmd": "git_op status", "ok": True, "turn": 2},
                    ),
                    _event(
                        "tool_event",
                        {"tool": "git_op", "cmd": "git_op status", "ok": True, "turn": 3},
                    ),
                    _event(
                        "result",
                        {
                            "status": "succeeded",
                            "terminal_action": {"type": "finish", "objective_met": True},
                        },
                    ),
                ],
                2: [
                    _event("compaction_failed", {"epoch": 1, "reason": "provider error"}),
                    _event("result", {"status": "failed"}),
                ],
            },
        )
    )

    assert _detectors(report) == ["retry-loop", "compaction-stall"]
    assert report["detectors_fired"][0]["evidence"] == [
        "turn-0001:event-1",
        "turn-0001:event-2",
        "turn-0001:event-3",
    ]
    assert report["detectors_fired"][1]["evidence"] == ["turn-0002:event-1"]


def test_compaction_deferred_is_not_a_failure(tmp_path: Path) -> None:
    report = _report(
        _session(
            tmp_path,
            [
                _event("compaction_deferred", {"epoch": 1, "reason": "retry later"}),
                _event(
                    "result",
                    {
                        "status": "succeeded",
                        "terminal_action": {"type": "finish", "objective_met": True},
                    },
                ),
            ],
        )
    )

    assert report["verdict"] == "clean"
    assert "compaction-stall" not in _detectors(report)


def test_durable_compaction_failed_fires(tmp_path: Path) -> None:
    report = _report(
        _session(
            tmp_path,
            [
                _event("compaction_failed", {"epoch": 1, "reason": "provider error"}),
                _event("result", {"status": "failed"}),
            ],
        )
    )

    assert _detectors(report) == ["compaction-stall"]
    assert report["detectors_fired"][0]["evidence"] == ["root:event-1"]


def test_finish_verification_does_not_cross_task_boundaries(tmp_path: Path) -> None:
    report = _report(
        _session(
            tmp_path,
            [
                _event(
                    "tool_event",
                    {"tool": "write_file", "cmd": _write_cmd("a.py"), "ok": True, "turn": 1},
                    task_id="task-a",
                ),
                _event("result", {"status": "failed"}, task_id="task-a"),
                _event(
                    "result",
                    {
                        "status": "succeeded",
                        "terminal_action": {"type": "finish", "objective_met": True},
                    },
                    task_id="task-b",
                ),
            ],
        )
    )

    assert report["verdict"] == "failed"
    assert "finish-without-verification" not in _detectors(report)


def test_missing_session_path_is_rejected(tmp_path: Path) -> None:
    missing = tmp_path / "does-not-exist"
    result = subprocess.run(
        [sys.executable, str(SCRIPT), "--json", str(missing)],
        cwd=ROOT,
        env={**os.environ, "PYTHONPATH": str(ROOT / "src")},
        capture_output=True,
        text=True,
    )

    assert result.returncode != 0
    assert "session path does not exist" in result.stderr


def test_state_ref_escape_is_incomplete_without_reading_outside(tmp_path: Path) -> None:
    outside = tmp_path / "outside.json"
    outside.write_text(
        json.dumps({"turn": 99, "code_changed": True, "verified_after_change": False}),
        encoding="utf-8",
    )
    session = _session(
        tmp_path,
        [
            _event("checkpoint", {"turn": 1, "state_ref": str(outside)}),
            _event(
                "result",
                {
                    "status": "succeeded",
                    "terminal_action": {"type": "finish", "objective_met": True},
                },
            ),
        ],
    )

    report = _report(session)

    assert report["verdict"] == "incomplete"
    assert report["confidence"] < 1
    assert "finish-without-verification" not in _detectors(report)
    assert str(outside) not in json.dumps(report)


def test_direct_root_store_layout_is_supported(tmp_path: Path) -> None:
    report = _report(
        _session(
            tmp_path,
            [
                _event(
                    "result",
                    {
                        "status": "succeeded",
                        "terminal_action": {"type": "finish", "objective_met": True},
                    },
                )
            ],
            direct_root=True,
        )
    )

    assert report["verdict"] == "clean"


def test_empty_event_store_is_incomplete(tmp_path: Path) -> None:
    session = tmp_path / "session"
    database = session / ".cambium" / "events.db"
    database.parent.mkdir(parents=True)
    with sqlite3.connect(database) as connection:
        connection.execute(_EVENTS_SCHEMA)

    report = _report(session)

    assert report["verdict"] == "incomplete"
    assert report["confidence"] < 1


def test_clean_session_has_no_detectors(tmp_path: Path) -> None:
    report = _report(
        _session(
            tmp_path,
            [
                _event(
                    "result",
                    {
                        "status": "succeeded",
                        "terminal_action": {"type": "finish", "objective_met": True},
                    },
                )
            ],
        )
    )

    assert report["verdict"] == "clean"
    assert report["detectors_fired"] == []
    assert report["confidence"] == 1.0
