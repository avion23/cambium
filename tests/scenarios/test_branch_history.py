from __future__ import annotations

import asyncio
import hashlib
import json
from pathlib import Path

import pytest

from cambium.branch_history import (
    MAX_HISTORY_OUTPUT_BYTES,
    BranchHistoryError,
    query_branch_history,
    tool_ref,
)
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


@pytest.mark.parametrize("action", ["tools", "transcript"])
def test_history_byte_bound_preserves_continuation_and_all_rows(
    tmp_path: Path, action: str
) -> None:
    session = _write_session(tmp_path)
    event_dir = session / ".cambium"
    checkpoint = event_dir / "checkpoints" / "child" / "turn-002.json"
    document = json.loads(checkpoint.read_text())
    contents = [f"row-{index}:" + "界" * 2000 for index in range(12)]
    document["transcript"] = [{"role": "user", "content": text} for text in contents]
    checkpoint.write_text(json.dumps(document))
    events_path = event_dir / "events.db"
    events = [json.loads(line) for line in events_path.read_text().splitlines()]
    tool_event = next(event for event in events if event["kind"] == "tool_event")
    events = [event for event in events if event["kind"] != "tool_event"]
    events.extend(
        {**tool_event, "seq": 6 + index, "payload": {**tool_event["payload"], "cmd": text}}
        for index, text in enumerate(contents)
    )
    events_path.write_text("".join(json.dumps(event) + "\n" for event in events))

    offset = 0
    pages = []
    while True:
        page = query_branch_history(
            session, {"action": action, "task_id": "child", "offset": offset, "limit": 64}
        )
        assert len(page.encode("utf-8")) <= MAX_HISTORY_OUTPUT_BYTES
        pages.append(page)
        last_line = page.splitlines()[-1]
        if not last_line.startswith("next_offset="):
            break
        next_offset = int(last_line.removeprefix("next_offset="))
        assert offset < next_offset < len(contents)
        offset = next_offset
    assert len(pages) > 1
    for text in contents:
        assert "\n".join(pages).count(text) == 1


@pytest.mark.parametrize("scope", ["session", "turn-0001"])
def test_tool_history_reopens_hash_verified_large_output_artifact(
    tmp_path: Path, scope: str
) -> None:
    session = _write_session(tmp_path, name=scope)
    exact = ("0123456789abcdef" * 4096).encode()
    spill = session / ".cambium" / "spill" / "exact.txt"
    spill.parent.mkdir(parents=True)
    spill.write_bytes(exact)
    events_path = session / ".cambium" / "events.db"
    events = [json.loads(line) for line in events_path.read_text(encoding="utf-8").splitlines()]
    events.append(
        {
            "seq": 6,
            "kind": "tool_output_artifact",
            "payload": {
                "tool": "read_batch",
                "turn": 2,
                "batch_index": 0,
                "output_ref": ".cambium/spill/exact.txt",
                "output_sha256": hashlib.sha256(exact).hexdigest(),
                "output_bytes": len(exact),
            },
            "task_id": "child",
            "generation": 1,
        }
    )
    events_path.write_text(
        "".join(json.dumps(event) + "\n" for event in events),
        encoding="utf-8",
    )

    page = query_branch_history(
        session,
        {
            "action": "tool",
            "ref": "tool:child:1:2:0",
            "output_offset": 100,
            "output_limit": 64,
        },
    )

    assert f"sha256={hashlib.sha256(exact).hexdigest()}" in page
    assert exact[100:164].decode() in page
    assert "next_output_offset=164" in page

    if scope.startswith("turn-"):
        # Both the live worker's current turn and the enclosing session can
        # reopen the same scoped artifact, including from a later turn.
        later = _write_session(tmp_path, name="turn-0002")
        for root in (tmp_path, later):
            historical = query_branch_history(
                root,
                {
                    "action": "tool",
                    "ref": f"tool:child:1:2:0@{scope}",
                    "output_offset": 100,
                    "output_limit": 64,
                },
            )
            assert exact[100:164].decode() in historical
            assert "next_output_offset=164" in historical

    spill.write_bytes(b"tampered")
    ref = tool_ref("child", 1, 2, session=scope if scope.startswith("turn-") else "")
    with pytest.raises(BranchHistoryError):
        query_branch_history(session, {"action": "tool", "ref": ref})


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
    with pytest.raises(BranchHistoryError):
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

    with pytest.raises(BranchHistoryError):
        query_branch_history(
            session,
            {"action": "tool", "ref": tool_ref("missing", 1, 1)},
        )


def test_legacy_tool_reference_without_batch_index_is_rejected(tmp_path: Path) -> None:
    session = _write_session(tmp_path)

    with pytest.raises(BranchHistoryError):
        query_branch_history(session, {"action": "tool", "ref": "tool:child:1:2"})


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
    "mutation",
    [
        "checkpoint",
        "checkpoint_generation",
        "checkpoint_turn",
        "action",
        "observation",
        "observation_tool",
        "observation_ok",
        "event_tool",
        "event_ok",
    ],
)
def test_tool_reopen_fails_without_matching_recorded_exchange(
    tmp_path: Path, mutation: str
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

    with pytest.raises(BranchHistoryError):
        query_branch_history(session, {"action": "tool", "ref": tool_ref("child", 1, 2)})
