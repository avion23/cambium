"""Scenario tests for the pure Architectus scheduling core."""

from __future__ import annotations

import asyncio

import pytest

from cambium.architectus import (
    CORE_DIRECTIVE_MAX,
    ActionKind,
    ArchitectusCore,
    ScriptedLLM,
    decide_failure,
)
from cambium.conversations import ConversationStore
from cambium.tasktree import build_tree, topological_order


def _plan(tasks: list[tuple[str, str, list[str], dict | None]]) -> dict:
    planned: list[dict] = []
    for task_id, kind, depends_on, spec in tasks:
        task_spec = dict(spec or {})
        if not depends_on and "goal" not in task_spec:
            task_spec["goal"] = "ROOT GOAL"
        planned.append(
            {
                "task_id": task_id,
                "kind": kind,
                "depends_on": depends_on,
                "spec": task_spec,
            }
        )
    return {"tasks": planned}


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
    assert context["static_prefix"] == ["ROOT GOAL", "SYSTEM", "MODULE"]
    assert context["prompt"].splitlines()[:3] == ["ROOT GOAL", "SYSTEM", "MODULE"]
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
                            "max_tokens": 6,
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

    assert budget_context["static_prefix"] == ["ROOT GOAL", "SYSTEM", "MODULE"]
    assert budget_context["truncated"] is True
    assert "OLD" not in repr(budget_context)
    assert "NEW" in repr(budget_context)


def test_core_directive_is_first_static_prefix_line() -> None:
    tree = build_tree(
        _plan(
            [
                ("root", "FEATURE", [], {"goal": "ROOT GOAL"}),
                (
                    "child",
                    "TEST",
                    ["root"],
                    {"system": "SYSTEM", "module_instructions": "MODULE"},
                ),
            ]
        )
    )
    core = ArchitectusCore(ScriptedLLM([]), tree=tree)

    context = core.compose_context("child")

    assert context["static_prefix"] == ["ROOT GOAL", "SYSTEM", "MODULE"]
    assert context["prompt"].splitlines()[:3] == ["ROOT GOAL", "SYSTEM", "MODULE"]


def test_compose_context_core_directive_is_truncated_to_hard_cap() -> None:
    tree = build_tree(
        _plan(
            [
                ("root", "FEATURE", [], None),
                ("child", "TEST", ["root"], {"system": "SYSTEM"}),
            ]
        )
    )
    directive = " ".join(["D"] * (CORE_DIRECTIVE_MAX + 20))
    tree = build_tree(
        _plan(
            [
                ("root", "FEATURE", [], {"goal": directive}),
                ("child", "TEST", ["root"], {"system": "SYSTEM"}),
            ]
        )
    )
    core = ArchitectusCore(ScriptedLLM([]), tree=tree)

    context = core.compose_context("child")

    prefix = context["static_prefix"][0]
    assert ArchitectusCore._estimate_tokens(prefix) == CORE_DIRECTIVE_MAX
    assert prefix.startswith("D " * (CORE_DIRECTIVE_MAX - 2))
    assert prefix.endswith("... [truncated]")


def test_root_directive_is_mandatory() -> None:
    tree = build_tree(
        {
            "tasks": [
                {"task_id": "root", "kind": "FEATURE", "depends_on": [], "spec": {}},
                {
                    "task_id": "child",
                    "kind": "TEST",
                    "depends_on": ["root"],
                    "spec": {"system": "SYSTEM"},
                },
            ]
        }
    )

    with pytest.raises(ValueError, match="root directive is required"):
        ArchitectusCore(ScriptedLLM([]), tree=tree)


def test_root_directive_is_immutable_after_construction() -> None:
    tree = build_tree(_plan([("root", "FEATURE", [], None), ("child", "TEST", ["root"], None)]))
    core = ArchitectusCore(ScriptedLLM([]), tree=tree)

    first = core.compose_context("child")

    with pytest.raises(TypeError):
        core.compose_context("child", core_directive="MUTATED ROOT GOAL")  # type: ignore[call-arg]
    assert core.compose_context("child") == first


@pytest.mark.parametrize("token_count", [199, 200, 201])
def test_root_directive_budget_uses_token_estimator(token_count: int) -> None:
    directive = " ".join(["token"] * token_count)
    tree = build_tree(
        _plan(
            [
                ("root", "FEATURE", [], {"goal": directive}),
                ("child", "TEST", ["root"], None),
            ]
        )
    )
    core = ArchitectusCore(ScriptedLLM([]), tree=tree)
    prefix = core.compose_context("child")["static_prefix"][0]

    assert ArchitectusCore._estimate_tokens(prefix) == min(token_count, CORE_DIRECTIVE_MAX)
    assert prefix.endswith("... [truncated]") is (token_count == 201)


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
        (
            {
                "kind": "gate_failed",
                "task_id": "task",
                "retries_remaining": 0,
            },
            "reset_retry",
        ),
        (
            {
                "kind": "gate_failed",
                "task_id": "task",
                "retries_remaining": 0,
                "reset_retry_attempted": True,
            },
            "abort-subtree",
        ),
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


@pytest.mark.parametrize("field", ["retries_left", "retries_remaining", "attempts_remaining"])
def test_gate_retry_aliases_share_one_failure_policy(field: str) -> None:
    assert decide_failure({"kind": "gate_failed", field: 1}) == "resolve"
    assert decide_failure({"kind": "gate_failed", field: 0}) == "reset_retry"


def test_step_back_reset_rerun_and_replayed_second_failure_abort() -> None:
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

    first_failure = {"kind": "gate_failed", "task_id": "root", "retries_left": 0}
    assert asyncio.run(core.step([first_failure])) == [
        {"action": "reset_retry", "task_id": "root"}
    ]
    assert core.reset_retry_tasks == frozenset({"root"})
    assert core.in_flight == {"root"}

    replayed_failure = {
        "kind": "gate_failed",
        "task_id": "root",
        "attempts_remaining": 0,
    }
    assert asyncio.run(core.step([replayed_failure])) == [
        {"action": "abort_subtree", "task_id": "root"}
    ]
    assert core.in_flight == set()
    assert asyncio.run(core.step([])) == []


def test_attempted_exhaustion_abort_consumes_one_shot_and_stays_aborted() -> None:
    tree = build_tree(_plan([("root", "FEATURE", [], None)]))
    core = ArchitectusCore(
        ScriptedLLM([[{"action": "spawn", "task_id": "root"}]]),
        tree=tree,
    )
    assert asyncio.run(core.step([])) == [{"action": "spawn", "task_id": "root"}]

    attempted_failure = {
        "kind": "gate_failed",
        "task_id": "root",
        "retries_remaining": 0,
        "reset_retry_attempted": True,
    }
    assert asyncio.run(core.step([attempted_failure])) == [
        {"action": "abort_subtree", "task_id": "root"}
    ]
    assert core.reset_retry_tasks == frozenset({"root"})
    assert core.in_flight == set()

    unmarked_failure = {
        "kind": "gate_failed",
        "task_id": "root",
        "retries_remaining": 0,
    }
    assert asyncio.run(core.step([unmarked_failure])) == [
        {"action": "abort_subtree", "task_id": "root"}
    ]
    assert core.reset_retry_tasks == frozenset({"root"})
    assert core.in_flight == set()


def test_reset_retry_consumption_survives_reconstruction() -> None:
    tree = build_tree(_plan([("root", "FEATURE", [], None)]))
    event = {"kind": "gate_failed", "task_id": "root", "retries_remaining": 0}
    core = ArchitectusCore(ScriptedLLM([]), tree=tree)

    assert asyncio.run(core.step([event])) == [
        {"action": "reset_retry", "task_id": "root"}
    ]
    durable_state = core.durable_state

    reconstructed = ArchitectusCore(
        ScriptedLLM([]),
        tree=tree,
        durable_state=durable_state,
    )

    assert reconstructed.reset_retry_tasks == frozenset({"root"})
    assert asyncio.run(reconstructed.step([event])) == [
        {"action": "abort_subtree", "task_id": "root"}
    ]


def test_failure_batch_processes_every_exhausted_gate() -> None:
    tree = build_tree(
        _plan(
            [
                ("root", "FEATURE", [], None),
                ("a", "TEST", ["root"], None),
                ("b", "TEST", ["root"], None),
            ]
        )
    )
    core = ArchitectusCore(
        ScriptedLLM(
            [
                [{"action": "spawn", "task_id": "root"}],
                [
                    {"action": "spawn", "task_id": "a"},
                    {"action": "spawn", "task_id": "b"},
                ],
            ]
        ),
        tree=tree,
        max_width=2,
    )
    assert asyncio.run(core.step([])) == [{"action": "spawn", "task_id": "root"}]
    core.aggregate("root", _envelope(None))
    assert asyncio.run(core.step([])) == [
        {"action": "spawn", "task_id": "a"},
        {"action": "spawn", "task_id": "b"},
    ]

    events = [
        {
            "kind": "gate_failed",
            "task_id": "a",
            "retries_remaining": 0,
            "reset_retry_attempted": True,
        },
        {
            "kind": "gate_failed",
            "task_id": "b",
            "retries_remaining": 0,
            "reset_retry_attempted": True,
        },
    ]
    assert asyncio.run(core.step(events)) == [
        {"action": "abort_subtree", "task_id": "a"},
        {"action": "abort_subtree", "task_id": "b"},
    ]
    assert core.action_history[-2:] == [
        {"action": "abort_subtree", "task_id": "a"},
        {"action": "abort_subtree", "task_id": "b"},
    ]
    assert core.in_flight == set()


def test_repeated_reset_retry_aborts_running_task_and_releases_slot() -> None:
    tree = build_tree(
        _plan(
            [
                ("root", "FEATURE", [], None),
                ("child", "TEST", ["root"], None),
            ]
        )
    )
    core = ArchitectusCore(
        ScriptedLLM(
            [
                [{"action": "spawn", "task_id": "root"}],
                [
                    {"action": "reset_retry", "task_id": "root"},
                    {"action": "reset_retry", "task_id": "root"},
                ],
                [],
            ]
        ),
        tree=tree,
    )
    assert asyncio.run(core.step([])) == [{"action": "spawn", "task_id": "root"}]
    assert asyncio.run(core.step([])) == [
        {"action": "reset_retry", "task_id": "root"},
        {"action": "abort_subtree", "task_id": "root"},
    ]
    assert core.action_history[-2:] == [
        {"action": "reset_retry", "task_id": "root"},
        {"action": "abort_subtree", "task_id": "root"},
    ]
    assert core.in_flight == set()

    assert asyncio.run(core.step([])) == []
    assert core.in_flight == set()


def test_mixed_spawn_and_repeated_reset_does_not_leave_aborted_task_in_flight() -> None:
    tree = build_tree(
        _plan(
            [
                ("root", "FEATURE", [], None),
                ("child", "TEST", ["root"], None),
            ]
        )
    )
    core = ArchitectusCore(
        ScriptedLLM(
            [
                [
                    {"action": "spawn", "task_id": "root"},
                    {"action": "reset_retry", "task_id": "root"},
                    {"action": "reset_retry", "task_id": "root"},
                ],
                [],
            ]
        ),
        tree=tree,
    )

    assert asyncio.run(core.step([])) == [
        {"action": "spawn", "task_id": "root"},
        {"action": "reset_retry", "task_id": "root"},
        {"action": "abort_subtree", "task_id": "root"},
    ]
    assert core.in_flight == set()
    assert asyncio.run(core.step([])) == []


def test_spawn_after_abort_is_not_emitted_and_in_flight_matches_actions() -> None:
    tree = build_tree(
        _plan(
            [
                ("root", "FEATURE", [], None),
                ("a", "TEST", ["root"], None),
                ("b", "TEST", ["root"], None),
            ]
        )
    )
    proposal = [
        {"action": "spawn", "task_id": "b"},
        {"action": "reset_retry", "task_id": "b"},
        {"action": "reset_retry", "task_id": "b"},
        {"action": "spawn", "task_id": "a"},
    ]
    core = ArchitectusCore(ScriptedLLM([proposal]), tree=tree)
    core.aggregate("root", _envelope(None))

    actions = asyncio.run(core.step([]))

    assert actions == [
        {"action": "spawn", "task_id": "b"},
        {"action": "reset_retry", "task_id": "b"},
        {"action": "abort_subtree", "task_id": "b"},
        {"action": "spawn", "task_id": "a"},
    ]
    abort_index = actions.index({"action": "abort_subtree", "task_id": "b"})
    assert not any(
        action == {"action": "spawn", "task_id": "b"}
        for action in actions[abort_index + 1 :]
    )
    emitted_in_flight: set[str] = set()
    for action in actions:
        if action["action"] == "spawn":
            emitted_in_flight.add(action["task_id"])
        elif action["action"] == "abort_subtree":
            emitted_in_flight.discard(action["task_id"])
    assert core.in_flight == emitted_in_flight == {"a"}


def test_malformed_later_failure_event_does_not_consume_prior_event() -> None:
    tree = build_tree(_plan([("root", "FEATURE", [], None)]))
    core = ArchitectusCore(
        ScriptedLLM([[{"action": "spawn", "task_id": "root"}]]),
        tree=tree,
    )
    assert asyncio.run(core.step([])) == [{"action": "spawn", "task_id": "root"}]

    first_failure = {
        "kind": "gate_failed",
        "task_id": "root",
        "retries_remaining": 0,
    }
    malformed_later_failure = {
        "kind": "gate_failed",
        "retries_remaining": 0,
    }

    with pytest.raises(ValueError, match="requires a non-empty task_id"):
        asyncio.run(core.step([first_failure, malformed_later_failure]))

    assert core.reset_retry_tasks == frozenset()
    assert core.in_flight == {"root"}
    assert asyncio.run(core.step([first_failure])) == [
        {"action": "reset_retry", "task_id": "root"}
    ]
