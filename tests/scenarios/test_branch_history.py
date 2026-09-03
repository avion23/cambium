from __future__ import annotations

import json
from pathlib import Path

import pytest

from cambium.branch_history import BranchHistoryError, query_branch_history, tool_ref


def _write_session(tmp_path: Path) -> Path:
    session = tmp_path / "session"
    event_dir = session / ".cambium"
    event_dir.mkdir(parents=True)
    checkpoint = event_dir / "checkpoints" / "child" / "turn-002.json"
    checkpoint.parent.mkdir(parents=True)
    checkpoint.write_text(
        json.dumps(
            {
                "transcript": [
                    {"role": "user", "content": "inspect parser"},
                    {
                        "role": "assistant",
                        "content": json.dumps(
                            {
                                "type": "tool_call",
                                "name": "read_batch",
                                "arguments": {"paths": ["src/parser.py"]},
                            }
                        ),
                    },
                    {
                        "role": "user",
                        "content": "tool read_batch ok=True\nparser evidence",
                    },
                ]
            }
        ),
        encoding="utf-8",
    )
    events = [
        {
            "seq": 1,
            "kind": "child_admitted",
            "payload": {
                "task_id": "root",
                "parent_task_id": "root",
                "child_task_id": "child",
                "context_mode": "semantic",
                "placement": "spread",
            },
            "task_id": "root",
        },
        {
            "seq": 2,
            "kind": "tool_event",
            "payload": {
                "task_id": "child",
                "generation": 1,
                "turn": 2,
                "tool": "read_batch",
                "cmd": 'read_batch {"paths": ["src/parser.py"]}',
                "ok": True,
                "duration_ms": 7,
            },
            "task_id": "child",
            "generation": 1,
        },
        {
            "seq": 3,
            "kind": "checkpoint",
            "payload": {
                "task_id": "child",
                "generation": 1,
                "turn": 2,
                "state_ref": str(checkpoint),
            },
            "task_id": "child",
            "generation": 1,
        },
        {
            "seq": 4,
            "kind": "usage_event",
            "payload": {"task_id": "child", "provider": "provider-b", "turn": 2},
            "task_id": "child",
        },
        {
            "seq": 5,
            "kind": "result",
            "payload": {"task_id": "child", "status": "succeeded"},
            "task_id": "child",
        },
    ]
    (event_dir / "events.db").write_text(
        "".join(json.dumps(event) + "\n" for event in events),
        encoding="utf-8",
    )
    return session


def test_branch_listing_distinguishes_task_and_context_policy(tmp_path: Path) -> None:
    session = _write_session(tmp_path)

    output = query_branch_history(session, {"action": "branches"})

    assert "branch:root" in output
    assert "branch:child" in output
    assert "parent=root" in output
    assert "context=semantic" in output
    assert "placement=spread" in output
    assert "provider=provider-b" in output


def test_tool_call_is_branch_local_and_independently_retrievable(tmp_path: Path) -> None:
    session = _write_session(tmp_path)
    ref = tool_ref("child", 1, 2)

    listing = query_branch_history(
        session,
        {"action": "tools", "task_id": "child"},
    )
    detail = query_branch_history(session, {"action": "tool", "ref": ref})

    assert ref in listing
    assert "branch=branch:child" in listing
    assert "tool=read_batch" in listing
    assert "assistant_action:" in detail
    assert '"name": "read_batch"' in detail
    assert "tool_observation:" in detail
    assert "parser evidence" in detail


def test_batched_tool_calls_have_distinct_retrievable_references(tmp_path: Path) -> None:
    session = _write_session(tmp_path)
    checkpoint = session / ".cambium" / "checkpoints" / "child" / "turn-002.json"
    checkpoint.write_text(
        json.dumps(
            {
                "transcript": [
                    {"role": "user", "content": "inspect parser"},
                    {
                        "role": "assistant",
                        "content": json.dumps(
                            {
                                "type": "tool_call",
                                "calls": [
                                    {
                                        "name": "read_batch",
                                        "arguments": {"paths": ["alpha.py"]},
                                    },
                                    {
                                        "name": "read_batch",
                                        "arguments": {"paths": ["beta.py"]},
                                    },
                                ],
                            }
                        ),
                    },
                    {
                        "role": "user",
                        "content": "only the first action was executed; trailing JSON was ignored",
                    },
                    {"role": "user", "content": "tool read_batch ok=True\nalpha evidence"},
                    {"role": "user", "content": "tool read_batch ok=True\nbeta evidence"},
                ]
            }
        ),
        encoding="utf-8",
    )
    events_path = session / ".cambium" / "events.db"
    events = [json.loads(line) for line in events_path.read_text(encoding="utf-8").splitlines()]
    first_tool = events[1]
    first_tool["payload"]["batch_index"] = 0
    second_tool = {
        **first_tool,
        "seq": 3,
        "payload": {**first_tool["payload"], "batch_index": 1},
    }
    events[2]["seq"] = 4
    events[3]["seq"] = 5
    events[4]["seq"] = 6
    events.insert(2, second_tool)
    events_path.write_text("".join(json.dumps(event) + "\n" for event in events), encoding="utf-8")

    first_ref = tool_ref("child", 1, 2, 0)
    second_ref = tool_ref("child", 1, 2, 1)
    listing = query_branch_history(session, {"action": "tools", "task_id": "child"})
    first_detail = query_branch_history(session, {"action": "tool", "ref": first_ref})
    second_detail = query_branch_history(session, {"action": "tool", "ref": second_ref})

    assert first_ref in listing
    assert second_ref in listing
    assert "alpha evidence" in first_detail
    assert "beta evidence" not in first_detail
    assert "beta evidence" in second_detail
    assert "alpha evidence" not in second_detail


def test_branch_transcript_can_be_recalled_without_a_new_database(tmp_path: Path) -> None:
    session = _write_session(tmp_path)

    output = query_branch_history(
        session,
        {"action": "transcript", "task_id": "child", "limit": 8},
    )

    assert "branch=branch:child messages=3" in output
    assert "inspect parser" in output
    assert "parser evidence" in output


def test_unknown_tool_reference_fails_cleanly(tmp_path: Path) -> None:
    session = _write_session(tmp_path)

    with pytest.raises(BranchHistoryError, match="tool call not found"):
        query_branch_history(
            session,
            {"action": "tool", "ref": tool_ref("missing", 1, 1)},
        )
