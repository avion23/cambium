"""Delegation policy and operator state use the same real lifecycle."""
import json

from cambium.observability import ObservabilityState
from cambium.render import render_tokens_per_s
from cambium.tui_screen import Transcript
from cambium.worker import _parse_agent_action


def test_delegate_defaults_depend_on_independent_batch() -> None:
    def call(name):
        return {"name": "delegate", "arguments": {
            "child_task_id": name, "spec": {"task": f"Implement {name}.py and verify"},
        }}

    single = _parse_agent_action(json.dumps({"type": "tool_call", "calls": [call("a")]}))
    args = single["calls"][0]["arguments"]
    assert args["kind"] == "feature"
    assert args["spec"]["context_mode"] == "trunk"
    assert args["spec"]["placement"] == "inherit"
    batch = _parse_agent_action(json.dumps({
        "type": "tool_call", "calls": [call("a"), call("b")],
    }))
    assert all(c["arguments"]["spec"]["context_mode"] == "semantic" for c in batch["calls"])
    assert all(c["arguments"]["spec"]["placement"] == "spread" for c in batch["calls"])


def test_suspended_parent_stays_live_and_resumes_after_child() -> None:
    state, transcript = ObservabilityState(), Transcript()
    events = [
        ("root", "spawned", {}),
        ("root", "result", {"status": "suspended"}),
        ("root", "exit", {"reason": "suspended"}),
        ("root", "context_fork", {
            "child_task_id": "child", "parent_task_id": "root",
            "resolved_context_mode": "fresh", "resolved_placement": "spread",
        }),
    ]
    for seq, (task, kind, payload) in enumerate(events, 1):
        event = {"seq": seq, "task_id": task, "kind": kind, "payload": payload}
        state.apply(event)
        transcript.observe_event(event)
    snapshot = state.snapshot()
    assert snapshot.agents[0].state == "suspended"
    assert snapshot.agents[1].lineage == "fresh"
    assert not transcript.live_final
    state.apply({"seq": 5, "task_id": "root", "kind": "context_resume", "payload": {}})
    assert state.snapshot().agents[0].state == "active"


def test_render_none_matches_other_empty_event_inputs() -> None:
    assert render_tokens_per_s(None) == render_tokens_per_s([]) == ""
