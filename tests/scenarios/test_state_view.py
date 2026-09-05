"""Model and operator inspection share one turn-local, read-only projection."""
from __future__ import annotations

import asyncio
import json
from types import SimpleNamespace

from cambium.state_view import load_state, state_text
from cambium.store import EventStore
from cambium.tools import ToolContext, run_tool
from cambium.tui import _command_output


def test_inspection_does_not_interleave_turns_and_can_focus_a_child(tmp_path, monkeypatch):
    root = tmp_path / "session"
    for turn, objective in ((1, "obsolete task"), (2, "current task")):
        path = root / f"turn-{turn:04d}" / ".cambium" / "events.db"
        path.parent.mkdir(parents=True)
        store = EventStore(path)
        try:
            store.append({"kind": "task_assigned", "task_id": "root", "payload": {
                "task": objective,
            }})
            store.append({"kind": "child_admitted", "task_id": "root", "payload": {
                "parent_task_id": "root", "child_task_id": "review",
            }})
            store.append({"kind": "task_assigned", "task_id": "review", "payload": {
                "parent_task_id": "root", "task": "review one commit",
            }})
            active = json.loads(state_text(root, "review"))
            assert active["objective"] == "review one commit"
            assert active["result"]["status"] is None
            store.append({"kind": "result", "task_id": "review", "payload": {
                "status": "succeeded", "summary": f"reviewed {objective}",
            }})
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
        "/inspect review", session=SimpleNamespace(root=root), cumulative=None,
        snapshot=None, cockpit=None,
    )
    assert json.loads(tool.output) == json.loads(operator) == json.loads(state_text(root, "review"))
