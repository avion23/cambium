from __future__ import annotations

import asyncio
import json
from typing import Any

import pytest

from cambium.architectus import CORE_DIRECTIVE_MAX, ActionKind, ArchitectusCore, ScriptedLLM
from cambium.supervisor import ArchitectusAdmissionPort
from cambium.tasktree import TaskTree, build_tree


def _tree(
    goal: str = "deliver the goal",
    children: list[dict[str, Any]] | None = None,
) -> TaskTree:
    tasks: list[dict[str, Any]] = [
        {
            "task_id": "root",
            "kind": "FEATURE",
            "depends_on": [],
            "spec": {"goal": goal},
        }
    ]
    for child in children or []:
        tasks.append(
            {
                "task_id": child["task_id"],
                "kind": child.get("kind", "FEATURE"),
                "depends_on": ["root"],
                "spec": child.get("spec", {}),
            }
        )
    return build_tree({"tasks": tasks})


def _envelope(parent_task_id: str | None = None, summary: str = "finished") -> dict[str, Any]:
    return {
        "parent_task_id": parent_task_id,
        "unified_diff": "",
        "diff_truncated": False,
        "summary": summary,
        "metric_score": None,
        "metric_breakdown": {},
        "commits": [],
        "files_changed": [],
        "status": "succeeded",
    }


@pytest.mark.parametrize("kind", tuple(ActionKind))
def test_action_kind_json_round_trip(kind: ActionKind) -> None:
    wire_action = json.loads(json.dumps({"action": kind, "task_id": "root"}))
    assert wire_action["action"] == kind.value

    core = ArchitectusCore(
        ScriptedLLM([wire_action]),
        tree=_tree(),
    )
    assert asyncio.run(core.step([])) == [{"action": kind.value, "task_id": "root"}]


def test_empty_step_and_minimal_context_are_stable() -> None:
    llm = ScriptedLLM([])
    core = ArchitectusCore(llm, tree=_tree())

    assert asyncio.run(core.step([])) == []
    assert llm.calls[0][1] == []
    assert llm.calls[0][0]["ready"] == ["root"]
    assert core.compose_context("root") == {
        "task_id": "root",
        "static_prefix": ["deliver the goal"],
        "dynamic_tail": [],
        "prompt": "deliver the goal",
        "truncated": False,
    }
    assert core.finished == {}
    assert core.in_flight == frozenset()
    assert core.action_history == []
    assert core.reset_retry_tasks == frozenset()
    assert core.durable_state == {"reset_retry_consumed": []}


def test_oversized_core_directive_is_bounded_without_mutating_tree() -> None:
    goal = "word " * (CORE_DIRECTIVE_MAX + 25)
    tree = _tree(goal=goal)
    core = ArchitectusCore(ScriptedLLM([]), tree=tree)

    context = core.compose_context("root")
    directive = context["static_prefix"][0]
    assert len(directive.split()) == CORE_DIRECTIVE_MAX
    assert directive.endswith("... [truncated]")
    assert tree.nodes[0].spec["goal"] == goal


def test_aggregate_accepts_minimal_envelope_and_keeps_defensive_state() -> None:
    core = ArchitectusCore(ScriptedLLM([]), tree=_tree())
    envelope = _envelope()

    core.aggregate("root", envelope)
    envelope["summary"] = "changed"
    finished = core.finished
    finished["root"]["summary"] = "changed again"

    assert core.finished["root"]["summary"] == "finished"
    assert core.in_flight == frozenset()


@pytest.mark.parametrize(
    "envelope",
    [{}, {**_envelope(), "unexpected": "value"}],
)
def test_aggregate_rejects_empty_or_oversized_envelopes(envelope: dict[str, Any]) -> None:
    core = ArchitectusCore(ScriptedLLM([]), tree=_tree())

    with pytest.raises(ValueError, match="invalid upward envelope"):
        core.aggregate("root", envelope)


def test_oversized_spawn_wave_obeys_width_bound() -> None:
    tree = _tree(
        children=[
            {"task_id": "child-a"},
            {"task_id": "child-b"},
            {"task_id": "child-c"},
        ]
    )
    core = ArchitectusCore(
        ScriptedLLM(
            [
                {"action": "spawn", "task_id": "child-a"},
                {"action": "spawn", "task_id": "child-b"},
                {"action": "spawn", "task_id": "child-c"},
            ]
        ),
        tree=tree,
        max_width=2,
    )
    core.aggregate("root", _envelope())

    actions = asyncio.run(core.step([{"kind": "parent_completed", "task_id": "root"}]))

    assert actions == [
        {"action": "spawn", "task_id": "child-a"},
        {"action": "spawn", "task_id": "child-b"},
    ]
    assert core.in_flight == frozenset({"child-a", "child-b"})


@pytest.mark.parametrize(
    "proposed",
    [
        [
            {"action": "reset_retry", "task_id": "child-a"},
            {"action": "spawn", "task_id": "child-b"},
        ],
        [
            {"action": "spawn", "task_id": "child-b"},
            {"action": "reset_retry", "task_id": "child-a"},
        ],
    ],
)
def test_reset_retry_recomputes_width_capacity_for_later_spawns(
    proposed: list[dict[str, str]],
) -> None:
    tree = _tree(
        children=[
            {"task_id": "child-a"},
            {"task_id": "child-b"},
        ]
    )
    core = ArchitectusCore(
        ScriptedLLM([proposed]),
        tree=tree,
        max_width=1,
    )
    core.aggregate("root", _envelope())
    core.aggregate("child-a", _envelope("root"))

    actions = asyncio.run(core.step([{"kind": "decision_tick"}]))

    assert actions == [{"action": "reset_retry", "task_id": "child-a"}]
    assert core.in_flight == frozenset({"child-a"})


def test_blocked_status_overrides_finished_envelope() -> None:
    llm = ScriptedLLM(
        [
            [{"action": "abort_subtree", "task_id": "root"}],
            [],
        ]
    )
    core = ArchitectusCore(llm, tree=_tree())
    core.aggregate("root", _envelope())

    assert asyncio.run(core.step([{"kind": "decision_tick"}])) == [
        {"action": "abort_subtree", "task_id": "root"}
    ]
    asyncio.run(core.step([]))

    assert llm.calls[1][0]["nodes"][0]["status"] == "failed"


@pytest.mark.parametrize("raw_kind", ["unknown", "SPAWN", "", None, 1])
def test_invalid_action_kinds_raise_value_error(raw_kind: Any) -> None:
    core = ArchitectusCore(
        ScriptedLLM([{"action": raw_kind, "task_id": "root"}]),
        tree=_tree(),
    )

    with pytest.raises(ValueError, match="Architectus action"):
        asyncio.run(core.step([]))


def test_same_input_produces_same_output_twice() -> None:
    tree = _tree()
    events = [{"kind": "decision_tick", "payload": {"sequence": 1}}]
    first_llm = ScriptedLLM([{"action": "spawn", "task_id": "root"}])
    second_llm = ScriptedLLM([{"action": "spawn", "task_id": "root"}])
    first = ArchitectusCore(first_llm, tree=tree)
    second = ArchitectusCore(second_llm, tree=tree)

    first_actions = asyncio.run(first.step(events))
    second_actions = asyncio.run(second.step(events))

    assert first_actions == second_actions == [{"action": "spawn", "task_id": "root"}]
    assert first_llm.calls == second_llm.calls
    assert first.action_history == second.action_history == first_actions
    assert first.in_flight == second.in_flight == frozenset({"root"})


def test_supervisor_port_consumes_tree_aggregate_and_spawn_contract() -> None:
    child_spec = {"task": "implement child"}
    core = ArchitectusCore(
        ScriptedLLM([{"action": "spawn", "task_id": "child"}]),
        tree=_tree(children=[{"task_id": "child", "spec": child_spec}]),
    )
    port = ArchitectusAdmissionPort(core)
    port.aggregate("root", _envelope())

    proposals = asyncio.run(port.step([{"kind": "parent_completed", "task_id": "root"}]))

    assert len(proposals) == 1
    proposal = proposals[0]
    assert set(proposal) == {
        "request_id",
        "parent_task_id",
        "child_task_id",
        "kind",
        "spec",
    }
    assert isinstance(proposal["request_id"], str)
    assert proposal["parent_task_id"] == "root"
    assert proposal["child_task_id"] == "child"
    assert proposal["kind"] == "feature"
    assert proposal["spec"] == child_spec


def test_failure_decision_method_is_deterministic_and_rejects_empty_event() -> None:
    event = {"kind": "crash", "task_id": "root"}

    assert ArchitectusCore.decide_failure(event) == "restart"
    assert ArchitectusCore.decide_failure(event) == ArchitectusCore.decide_failure(event)
    with pytest.raises(ValueError, match="unclassified failure event"):
        ArchitectusCore.decide_failure({})
