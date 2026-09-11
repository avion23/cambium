"""Model and operator inspection share one turn-local, read-only projection."""

from __future__ import annotations

import asyncio
from types import SimpleNamespace

from cambium.situation import DEFAULT_FRAME_BYTES, render_situation_frame
from cambium.state_view import load_state, state_text
from cambium.store import EventStore
from cambium.tools import ToolContext, run_tool
from cambium.tui import _command_output
from cambium.worker import _strip_situation_frame


def test_inspection_does_not_interleave_turns_and_can_focus_a_child(tmp_path, monkeypatch):
    root = tmp_path / "session"
    for turn, objective in ((1, "obsolete task"), (2, "current task")):
        path = root / f"turn-{turn:04d}" / ".cambium" / "events.db"
        path.parent.mkdir(parents=True)
        store = EventStore(path)
        try:
            store.append(
                {
                    "kind": "task_assigned",
                    "task_id": "root",
                    "payload": {
                        "task": objective,
                    },
                }
            )
            store.append(
                {
                    "kind": "child_admitted",
                    "task_id": "root",
                    "payload": {
                        "parent_task_id": "root",
                        "child_task_id": "review",
                    },
                }
            )
            store.append(
                {
                    "kind": "task_assigned",
                    "task_id": "review",
                    "payload": {
                        "parent_task_id": "root",
                        "task": "review one commit",
                    },
                }
            )
            active = state_text(root, "review")
            assert "objective: review one commit" in active
            assert "result: unknown" in active
            store.append(
                {
                    "kind": "result",
                    "task_id": "review",
                    "payload": {
                        "status": "succeeded",
                        "summary": f"reviewed {objective}",
                    },
                }
            )
        finally:
            store.close()
    state = load_state(root)
    assert state.mission.objective == "current task"
    assert state.source_watermark == 4
    child = load_state(root, "review")
    assert child.mission.objective == "review one commit"
    assert child.result.summary == "reviewed current task"
    assert child.identity.session_id.endswith("turn-0002")
    monkeypatch.setenv("CAMBIUM_SESSION_ID", str(root / "turn-0002"))
    monkeypatch.setenv("CAMBIUM_TASK_ID", "review")

    async def inspect():
        with ToolContext(tmp_path) as context:
            return await run_tool("inspect_state", {}, context)

    tool = asyncio.run(inspect())
    assert tool.ok, tool.error
    operator = _command_output(
        "/inspect review",
        session=SimpleNamespace(root=root),
        cumulative=None,
        snapshot=None,
        timeline=None,
    )
    assert tool.output == operator == render_situation_frame(child)
    assert "summary: reviewed current task" in operator
    observation = {"role": "user", "content": f"tool inspect_state ok=True\n{tool.output}"}
    assert _strip_situation_frame([observation]) == [observation]


def test_inspection_remains_bounded_and_keeps_active_children_visible(tmp_path):
    path = tmp_path / ".cambium" / "events.db"
    path.parent.mkdir()
    store = EventStore(path)
    try:
        store.append(
            {
                "kind": "task_assigned",
                "task_id": "root",
                "payload": {
                    "task": "Review independent commits",
                },
            }
        )
        for index in range(20):
            task = f"review-{index:02d}"
            store.append(
                {
                    "kind": "child_admitted",
                    "task_id": "root",
                    "payload": {
                        "parent_task_id": "root",
                        "child_task_id": task,
                    },
                }
            )
            if index < 19:
                store.append(
                    {
                        "kind": "result",
                        "task_id": task,
                        "payload": {
                            "status": "succeeded",
                            "summary": "Reviewed without changes",
                        },
                    }
                )
        text = state_text(tmp_path)
        assert len(text.encode("utf-8")) <= DEFAULT_FRAME_BYTES
        assert "branch_id=review-19" in text
        assert "branch_history(action=branches)" in text
        assert len(load_state(tmp_path).children) == 20
    finally:
        store.close()
