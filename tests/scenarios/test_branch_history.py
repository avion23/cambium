from __future__ import annotations

import asyncio
import json
from pathlib import Path

import pytest

from cambium.branch_history import BranchHistoryError, query_branch_history, tool_ref
from cambium.tools import ToolContext, run_tool


def _write_session(
    tmp_path: Path, *, name: str = "session", evidence: str = "parser evidence"
) -> Path:
    session = tmp_path / name
    event_dir = session / ".cambium"
    event_dir.mkdir(parents=True)
    checkpoint = event_dir / "checkpoints" / "child" / "turn-002.json"
    checkpoint.parent.mkdir(parents=True)
    checkpoint.write_text(
        json.dumps(
            {
                "generation": 1,
                "turn": 2,
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
                        "content": f"tool read_batch ok=True\n{evidence}",
                    },
                ],
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


@pytest.mark.parametrize("scopes", [("turn-0001", "turn-0002"), ("turn-9999", "turn-10000")])
def test_interactive_history_has_chronological_and_unambiguous_tool_identity(
    tmp_path: Path, scopes: tuple[str, str]
) -> None:
    root = tmp_path / "interactive"
    _write_session(root, name=scopes[0], evidence="first-turn evidence")
    current = _write_session(root, name=scopes[1], evidence="second-turn evidence")
    latest = query_branch_history(current, {"action": "transcript", "task_id": "child"})
    assert "second-turn evidence" in latest
    listing = query_branch_history(current, {"action": "tools"})
    for scope, evidence in zip(scopes, ("first-turn", "second-turn"), strict=True):
        ref = f"tool:child:1:2:0@{scope}"
        assert ref in listing
        result = query_branch_history(current, {"action": "tool", "ref": ref})
        assert f"{evidence} evidence" in result
    with pytest.raises(BranchHistoryError, match="ambiguous"):
        query_branch_history(current, {"action": "tool", "ref": "tool:child:1:2:0"})


def test_worker_can_reopen_exact_evidence_without_mutating_history(
    tmp_path: Path, monkeypatch
) -> None:
    session = _write_session(tmp_path)
    monkeypatch.setenv("CAMBIUM_SESSION_ID", str(session))
    before = {path: path.read_bytes() for path in session.rglob("*") if path.is_file()}
    ctx = ToolContext(tmp_path)
    listing = asyncio.run(run_tool("branch_history", {"action": "tools"}, ctx))
    assert listing.ok and "tool:child:1:2:0" in listing.output
    detail = asyncio.run(
        run_tool("branch_history", {"action": "tool", "ref": "tool:child:1:2:0"}, ctx)
    )
    assert detail.ok and "parser evidence" in detail.output
    assert before == {path: path.read_bytes() for path in session.rglob("*") if path.is_file()}


def test_branch_listing_distinguishes_task_and_context_policy(tmp_path: Path) -> None:
    session = _write_session(tmp_path)

    output = query_branch_history(session, {"action": "branches"})

    assert "branch:root" in output
    assert "branch:child" in output
    assert "parent=root" in output
    assert "context=semantic" in output
    assert "placement=spread" in output
    assert "provider=provider-b" in output


def test_branch_listing_prefers_resolved_context_policy(tmp_path: Path) -> None:
    session = _write_session(tmp_path)
    events_path = session / ".cambium" / "events.db"
    events = [
        {
            "seq": 1,
            "kind": "context_fork",
            "task_id": "root",
            "payload": {
                "parent_task_id": "root",
                "child_task_id": "child",
                "context_mode": "semantic",
                "placement": "spread",
                "resolved_context_mode": "fresh",
                "resolved_placement": "spread",
            },
        },
        {
            "seq": 2,
            "kind": "child_admitted",
            "task_id": "root",
            "payload": {
                "parent_task_id": "root",
                "child_task_id": "child",
                "context_mode": "semantic",
                "placement": "spread",
            },
        },
    ]
    events_path.write_text(
        "".join(f"{json.dumps(event)}\n" for event in events),
        encoding="utf-8",
    )

    output = query_branch_history(session, {"action": "branches"})

    assert "branch:child" in output
    assert "context=fresh" in output
    assert "context=semantic" not in output
    assert "placement=spread" in output


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
                "generation": 1,
                "turn": 2,
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
                ],
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


def test_branch_listing_projects_all_terminal_event_forms(tmp_path: Path) -> None:
    session = tmp_path / "terminal-events"
    event_dir = session / ".cambium"
    event_dir.mkdir(parents=True)
    events = [
        {
            "seq": 1,
            "kind": "child_admitted",
            "task_id": "parent",
            "payload": {
                "parent_task_id": "parent",
                "child_task_id": "child-rejected",
            },
        },
        {
            "seq": 2,
            "kind": "worker_failed",
            "task_id": "worker-failed",
            "payload": {"reason": "provider unavailable"},
        },
        {
            "seq": 3,
            "kind": "child_failed",
            "task_id": "child-failed",
            "payload": {
                "parent_task_id": "parent",
                "reason": "timeout",
            },
        },
        {
            "seq": 4,
            "kind": "child_rejected",
            "task_id": "parent",
            "payload": {
                "parent_task_id": "parent",
                "child_task_id": "child-rejected",
                "reason": "ChildPolicyError",
            },
        },
        {
            "seq": 5,
            "kind": "exit",
            "task_id": "exit-succeeded",
            "payload": {"reason": "done"},
        },
        {
            "seq": 6,
            "kind": "exit",
            "task_id": "exit-cancelled",
            "payload": {"reason": "cancelled"},
        },
        {
            "seq": 7,
            "kind": "exit",
            "task_id": "exit-suspended",
            "payload": {"reason": "suspended"},
        },
        {
            "seq": 8,
            "kind": "exit",
            "task_id": "exit-failed",
            "payload": {"reason": "crash"},
        },
        {
            "seq": 9,
            "kind": "worker_exit",
            "task_id": "worker-exit",
            "payload": {"exit_code": 0},
        },
        {
            "seq": 10,
            "kind": "worker_exit",
            "task_id": "worker-exit-unknown",
            "payload": {},
        },
        {
            "seq": 11,
            "kind": "worker_terminated",
            "task_id": "worker-terminated",
            "payload": {"status": "terminated"},
        },
        {
            "seq": 12,
            "kind": "child_rejected",
            "task_id": "parent",
            "payload": {"reason": "missing child identity"},
        },
    ]
    (event_dir / "events.db").write_text(
        "".join(json.dumps(event) + "\n" for event in events), encoding="utf-8"
    )

    output = query_branch_history(session, {"action": "branches"})

    assert "branch:worker-failed parent=- status=failed" in output
    assert "branch:child-failed parent=parent status=failed" in output
    assert "branch:child-rejected parent=parent status=rejected" in output
    assert "branch:exit-succeeded parent=- status=succeeded" in output
    assert "branch:exit-cancelled parent=- status=cancelled" in output
    assert "branch:exit-suspended parent=- status=suspended" in output
    assert "branch:exit-failed parent=- status=failed" in output
    assert "branch:worker-exit parent=- status=succeeded" in output
    assert "branch:worker-exit-unknown parent=- status=unknown" in output
    assert "branch:worker-terminated parent=- status=failed" in output
    assert "branch:parent parent=- status=unknown" in output


@pytest.mark.parametrize(
    ("mutation", "message"),
    [
        ("checkpoint", "checkpoint evidence"),
        ("checkpoint_generation", "checkpoint generation"),
        ("checkpoint_turn", "checkpoint turn"),
        ("action", "assistant action"),
        ("observation", "observation"),
        ("observation_tool", "does not match"),
        ("observation_ok", "disagrees"),
        ("event_tool", "valid tool name"),
        ("event_ok", "boolean result"),
    ],
)
def test_tool_reopen_fails_without_matching_recorded_exchange(
    tmp_path: Path, mutation: str, message: str
) -> None:
    session = _write_session(tmp_path)
    events_path = session / ".cambium" / "events.db"
    events = [json.loads(line) for line in events_path.read_text(encoding="utf-8").splitlines()]
    checkpoint = session / ".cambium" / "checkpoints" / "child" / "turn-002.json"
    document = json.loads(checkpoint.read_text(encoding="utf-8"))

    if mutation == "checkpoint":
        events = [event for event in events if event["kind"] != "checkpoint"]
    elif mutation == "checkpoint_generation":
        document.pop("generation")
    elif mutation == "checkpoint_turn":
        document.pop("turn")
    elif mutation == "action":
        document["transcript"][1]["content"] = json.dumps(
            {"type": "tool_call", "name": "write_file", "arguments": {}}
        )
    elif mutation == "observation":
        document["transcript"] = document["transcript"][:-1]
    elif mutation == "observation_tool":
        document["transcript"][2]["content"] = "tool write_file ok=True\nparser evidence"
    elif mutation == "observation_ok":
        document["transcript"][2]["content"] = "tool read_batch ok=False\nparser evidence"
    elif mutation == "event_tool":
        next(event for event in events if event["kind"] == "tool_event")["payload"]["tool"] = ""
    elif mutation == "event_ok":
        next(event for event in events if event["kind"] == "tool_event")["payload"]["ok"] = 1
    else:  # pragma: no cover - guarded by the parameter table
        raise AssertionError(mutation)

    checkpoint.write_text(json.dumps(document), encoding="utf-8")
    events_path.write_text("".join(json.dumps(event) + "\n" for event in events), encoding="utf-8")

    with pytest.raises(BranchHistoryError, match=message):
        query_branch_history(session, {"action": "tool", "ref": tool_ref("child", 1, 2)})
