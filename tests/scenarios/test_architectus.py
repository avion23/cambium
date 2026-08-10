"""Scenario tests for the pure Architectus scheduling core."""

from __future__ import annotations

import asyncio

import pytest

from cambium.architectus import ActionKind, ArchitectusCore, ScriptedLLM, decide_failure
from cambium.conversations import ConversationStore
from cambium.tasktree import build_tree, topological_order


def _plan(tasks: list[tuple[str, str, list[str], dict | None]]) -> dict:
    return {
        "tasks": [
            {
                "task_id": task_id,
                "kind": kind,
                "depends_on": depends_on,
                "spec": spec or {},
            }
            for task_id, kind, depends_on, spec in tasks
        ]
    }


def _envelope(parent_task_id: str | None, *, summary: str = "", status: str = "done") -> dict:
    return {
        "parent_task_id": parent_task_id,
        "unified_diff": "",
        "diff_truncated": False,
        "summary": summary,
        "metric_score": None,
        "metric_breakdown": {},
        "commits": [],
        "files_changed": [],
        "status": status,
    }


def _spawn_ready(state: dict, events: list[dict]) -> list[dict]:
    del events
    return [{"action": ActionKind.SPAWN, "task_id": task_id} for task_id in state["ready"]]


def test_scheduling_order_matches_topological_order() -> None:
    tree = build_tree(
        _plan(
            [
                ("root", "FEATURE", [], None),
                ("a", "TEST", ["root"], None),
                ("b", "TEST", ["root"], None),
                ("c", "DOCS", ["a"], None),
            ]
        )
    )
    core = ArchitectusCore(ScriptedLLM(_spawn_ready), tree=tree)
    dispatched: list[str] = []

    while len(dispatched) < len(tree.nodes):
        actions = asyncio.run(core.step([]))
        for action in actions:
            if action["action"] == ActionKind.SPAWN.value:
                task_id = action["task_id"]
                dispatched.append(task_id)
                core.aggregate(task_id, _envelope(next(
                    node.parent_task_id
                    for node in tree.nodes
                    if node.task_id == task_id
                )))

    assert dispatched == topological_order(tree)


def test_max_width_caps_spawn_actions_against_in_flight() -> None:
    tree = build_tree(
        _plan(
            [
                ("root", "FEATURE", [], None),
                ("a", "TEST", ["root"], None),
                ("b", "TEST", ["root"], None),
                ("c", "TEST", ["root"], None),
            ]
        )
    )
    core = ArchitectusCore(ScriptedLLM(_spawn_ready), tree=tree, max_width=2)

    assert asyncio.run(core.step([])) == [{"action": "spawn", "task_id": "root"}]
    core.aggregate("root", _envelope(None))
    assert asyncio.run(core.step([])) == [
        {"action": "spawn", "task_id": "a"},
        {"action": "spawn", "task_id": "b"},
    ]
    assert core.in_flight == {"a", "b"}

    core.aggregate("a", _envelope("root"))
    assert asyncio.run(core.step([])) == [{"action": "spawn", "task_id": "c"}]


def test_envelope_validation_rejects_scratchpad() -> None:
    tree = build_tree(_plan([("root", "FEATURE", [], None)]))
    core = ArchitectusCore(ScriptedLLM([]), tree=tree)
    invalid = _envelope(None)
    invalid["scratchpad"] = "private chain of thought"

    with pytest.raises(ValueError, match="scratchpad"):
        core.aggregate("root", invalid)


def test_context_is_static_first_dynamic_last_and_evicts_old_tail(tmp_path) -> None:
    tree = build_tree(
        _plan(
            [
                ("root", "FEATURE", [], {"summary": "PARENT"}),
                (
                    "child",
                    "TEST",
                    ["root"],
                    {"system": "SYSTEM", "module_instructions": "MODULE"},
                ),
                ("leaf", "TEST", ["child"], None),
            ]
        )
    )
    core = ArchitectusCore(ScriptedLLM([]), tree=tree)
    core.aggregate("root", _envelope(None, summary="PARENT"))
    core.aggregate("leaf", _envelope("child", summary="CHILD"))

    context = core.compose_context("child")
    assert context["static_prefix"] == ["SYSTEM", "MODULE"]
    assert context["prompt"].splitlines()[:2] == ["SYSTEM", "MODULE"]
    assert [segment["kind"] for segment in context["dynamic_tail"]] == [
        "parent_summary",
        "child_envelope",
    ]
    assert context["truncated"] is False

    store = ConversationStore(tmp_path / "budget.db", fsync_interval_s=60.0)
    try:
        store.append("child", "user", "OLD")
        store.append("child", "assistant", "NEW")
        budget_tree = build_tree(
            _plan(
                [
                    ("root", "FEATURE", [], {"summary": "PARENT"}),
                    (
                        "child",
                        "TEST",
                        ["root"],
                        {
                            "system": "SYSTEM",
                            "module_instructions": "MODULE",
                            "max_tokens": 4,
                        },
                    ),
                ]
            )
        )
        budget_core = ArchitectusCore(ScriptedLLM([]), tree=budget_tree, store=store)
        budget_core.aggregate("root", _envelope(None, summary="PARENT"))
        budget_context = budget_core.compose_context("child")
    finally:
        store.close()

    assert budget_context["static_prefix"] == ["SYSTEM", "MODULE"]
    assert budget_context["truncated"] is True
    assert "OLD" not in repr(budget_context)
    assert "NEW" in repr(budget_context)


def test_scripted_llm_drives_a_three_task_wave() -> None:
    tree = build_tree(
        _plan(
            [
                ("root", "FEATURE", [], None),
                ("a", "TEST", ["root"], None),
                ("b", "DOCS", ["root"], None),
            ]
        )
    )
    llm = ScriptedLLM(
        [
            [{"action": "spawn", "task_id": "root"}],
            [
                {"action": "spawn", "task_id": "a"},
                {"action": "spawn", "task_id": "b"},
            ],
        ]
    )
    core = ArchitectusCore(llm, tree=tree, max_width=2)

    assert asyncio.run(core.step([])) == [{"action": "spawn", "task_id": "root"}]
    core.aggregate("root", _envelope(None))
    assert asyncio.run(core.step([{"kind": "root_finished"}])) == [
        {"action": "spawn", "task_id": "a"},
        {"action": "spawn", "task_id": "b"},
    ]
    assert llm.calls[1][1] == [{"kind": "root_finished"}]


@pytest.mark.parametrize(
    ("event", "expected"),
    [
        ({"kind": "node_crash", "reason": "crash"}, "restart"),
        ({"kind": "node_failed", "status": "failed", "retries_left": 1}, "resolve"),
        ({"kind": "gate_failed", "retries_left": 1}, "resolve"),
        ({"kind": "node_failed", "status": "failed", "retries_left": 0}, "abort-subtree"),
        ({"kind": "budget_exceeded"}, "abort-subtree"),
        ({"kind": "merge_failed", "reason": "conflict"}, "replan"),
        ({"kind": "merge_failed", "reason": "test_failure"}, "merge-resolve"),
        ({"kind": "merge_failed", "reason": "non_fast_forward"}, "merge-resolve"),
        ({"kind": "provider_exhaustion"}, "resolve"),
        ({"kind": "spec_error", "recoverable": False}, "abort-subtree"),
        ({"kind": "invalid_plan"}, "replan"),
        ({"kind": "generation_mismatch"}, "abort-subtree"),
    ],
)
def test_decide_failure_table(event: dict, expected: str) -> None:
    assert decide_failure(event) == expected
    assert ArchitectusCore.decide_failure(event) == expected


def test_aggregate_marks_finished_and_releases_next_wave() -> None:
    tree = build_tree(
        _plan(
            [
                ("root", "FEATURE", [], None),
                ("child", "TEST", ["root"], None),
            ]
        )
    )
    core = ArchitectusCore(ScriptedLLM(_spawn_ready), tree=tree)
    assert asyncio.run(core.step([])) == [{"action": "spawn", "task_id": "root"}]
    assert core.in_flight == {"root"}

    core.aggregate("root", _envelope(None, summary="root done"))
    assert core.finished["root"]["summary"] == "root done"
    assert core.in_flight == set()
    assert asyncio.run(core.step([])) == [{"action": "spawn", "task_id": "child"}]


def test_info_hiding_keeps_scratchpad_out_of_parent_context() -> None:
    tree = build_tree(
        _plan(
            [
                ("root", "FEATURE", [], None),
                ("child", "TEST", ["root"], None),
            ]
        )
    )
    core = ArchitectusCore(ScriptedLLM([]), tree=tree)
    invalid = _envelope(None)
    invalid["scratchpad"] = "CANARY-SECRET"
    with pytest.raises(ValueError):
        core.aggregate("root", invalid)

    core.aggregate("child", _envelope("root", summary="safe"))
    context = core.compose_context("root")
    assert "CANARY-SECRET" not in repr(context)
    assert all(set(segment["envelope"]) == {
        "parent_task_id",
        "unified_diff",
        "diff_truncated",
        "summary",
        "metric_score",
        "metric_breakdown",
        "commits",
        "files_changed",
        "status",
    } for segment in context["dynamic_tail"] if segment["kind"] == "child_envelope")


def test_store_backed_context_uses_conversation_roundtrip(tmp_path) -> None:
    store = ConversationStore(tmp_path / "conversations.db", fsync_interval_s=60.0)
    try:
        tree = build_tree(
            _plan(
                [
                    ("root", "FEATURE", [], None),
                    ("child", "TEST", ["root"], {"system": "SYSTEM"}),
                ]
            )
        )
        first = store.append("child", "user", "first turn")
        second = store.append("child", "assistant", "latest turn")
        core = ArchitectusCore(ScriptedLLM([]), tree=tree, store=store)
        context = core.compose_context("child")

        records = [segment["record"] for segment in context["dynamic_tail"]]
        assert [record["id"] for record in records] == [first, second]
        assert [record["content"] for record in records] == ["first turn", "latest turn"]
    finally:
        store.close()


def test_callable_scripted_llm_receives_ready_state() -> None:
    tree = build_tree(_plan([("root", "FEATURE", [], None)]))
    seen: list[tuple[list[str], list[dict]]] = []

    def decide(state: dict, events: list[dict]) -> list[dict]:
        seen.append((state["ready"], events))
        return [{"action": "spawn", "task_id": state["ready"][0]}]

    core = ArchitectusCore(ScriptedLLM(decide), tree=tree)
    assert asyncio.run(core.step([{"kind": "tick"}])) == [
        {"action": "spawn", "task_id": "root"}
    ]
    assert seen == [(["root"], [{"kind": "tick"}])]
