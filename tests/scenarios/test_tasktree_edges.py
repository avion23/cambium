"""Tasktree edge contracts for graph validation, ordering, and input errors."""

from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path
from typing import Any

import pytest

from cambium.tasktree import (
    CycleError,
    MultiParentError,
    NodeStatus,
    TaskKind,
    TaskNode,
    TaskPlanError,
    TaskTreeError,
    build_tree,
    ready_tasks,
    topological_order,
    upward_result,
)

REPO_ROOT = Path(__file__).resolve().parents[2]
SRC_DIR = str(REPO_ROOT / "src")


def _run_cli(payload: str) -> subprocess.CompletedProcess[str]:
    env = dict(os.environ)
    env["PYTHONPATH"] = os.pathsep.join(filter(None, [SRC_DIR, env.get("PYTHONPATH")]))
    return subprocess.run(
        [sys.executable, "-m", "cambium.tasktree"],
        input=payload,
        capture_output=True,
        text=True,
        env=env,
        cwd=str(REPO_ROOT),
        timeout=120,
    )


def _plan(task_ids: list[str]) -> dict[str, Any]:
    parents = {
        "root": [],
        "z": ["root"],
        "a": ["root"],
        "z2": ["z"],
        "a2": ["a"],
    }
    return {
        "tasks": [
            {
                "task_id": task_id,
                "kind": "FEATURE" if task_id == "root" else "TEST",
                "depends_on": parents[task_id],
            }
            for task_id in task_ids
        ]
    }


@pytest.mark.parametrize(
    ("tasks", "error", "message"),
    [
        (
            [
                {"task_id": "root", "kind": "FEATURE", "depends_on": []},
                {"task_id": "self", "kind": "TEST", "depends_on": ["self"]},
            ],
            CycleError,
            "cycle in task DAG: self -> self",
        ),
        (
            [
                {"task_id": "root", "kind": "FEATURE", "depends_on": []},
                {"task_id": "left", "kind": "TEST", "depends_on": ["right"]},
                {"task_id": "right", "kind": "TEST", "depends_on": ["left"]},
            ],
            CycleError,
            "cycle in task DAG: left -> right -> left",
        ),
        (
            [
                {"task_id": "root", "kind": "FEATURE", "depends_on": []},
                {"task_id": "left", "kind": "TEST", "depends_on": ["root"]},
                {"task_id": "right", "kind": "TEST", "depends_on": ["root"]},
                {
                    "task_id": "leaf",
                    "kind": "TEST",
                    "depends_on": ["left", "right"],
                },
            ],
            MultiParentError,
            "multi-parent",
        ),
    ],
    ids=["self-loop", "two-node-cycle", "shared-leaf"],
)
def test_build_tree_graph_edge_errors(
    tasks: list[dict[str, Any]], error: type[Exception], message: str
) -> None:
    with pytest.raises(error) as raised:
        build_tree({"tasks": tasks})
    assert type(raised.value) is error
    assert message in str(raised.value)


def test_ready_and_topological_order_are_pinned_and_repeatable() -> None:
    task_ids = ["z2", "root", "a2", "z", "a"]
    expected_topological = ["root", "a", "a2", "z", "z2"]
    expected_ready = {
        frozenset(): ["root"],
        frozenset({"root"}): ["a", "z"],
        frozenset({"root", "a", "z"}): ["a2", "z2"],
    }

    observations: list[tuple[list[str], dict[frozenset[str], list[str]]]] = []
    for _ in range(20):
        tree = build_tree(_plan(task_ids))
        observations.append(
            (
                topological_order(tree),
                {
                    finished: [node.task_id for node in ready_tasks(tree, set(finished))]
                    for finished in expected_ready
                },
            )
        )

    assert all(order == expected_topological for order, _ in observations)
    assert all(ready == expected_ready for _, ready in observations)


def test_order_is_independent_of_task_definition_order() -> None:
    first = build_tree(_plan(["z2", "root", "a2", "z", "a"]))
    second = build_tree(_plan(["a", "z", "a2", "z2", "root"]))

    assert topological_order(first) == topological_order(second)
    for finished in (set(), {"root"}, {"root", "a", "z"}):
        first_ready = [node.task_id for node in ready_tasks(first, finished)]
        second_ready = [node.task_id for node in ready_tasks(second, finished)]
        assert first_ready == second_ready


@pytest.mark.parametrize(
    "plan",
    [
        "{",
        None,
        [],
        {"tasks": None},
        {"tasks": "tasks"},
        {"tasks": [None]},
        {"tasks": [{"task_id": "root", "kind": 1}]},
        {
            "tasks": [
                {"task_id": "root", "kind": "FEATURE", "depends_on": {}},
            ]
        },
        {
            "tasks": [
                {"task_id": "root", "kind": "FEATURE", "spec": []},
            ]
        },
    ],
    ids=[
        "invalid-json-text",
        "null",
        "array",
        "missing-tasks",
        "tasks-string",
        "task-null",
        "kind-number",
        "dependencies-object",
        "spec-array",
    ],
)
def test_malformed_plan_inputs_raise_task_plan_error(plan: Any) -> None:
    with pytest.raises(TaskPlanError):
        build_tree(plan)


@pytest.mark.parametrize(
    ("kwargs", "message"),
    [
        ({"max_depth": None}, "max_depth"),
        ({"max_depth": "3"}, "max_depth"),
        ({"max_width": None}, "max_width"),
        ({"max_width": "8"}, "max_width"),
    ],
)
def test_invalid_bound_types_raise_task_plan_error(
    kwargs: dict[str, Any], message: str
) -> None:
    with pytest.raises(TaskPlanError, match=message):
        build_tree(_plan(["root"]), **kwargs)


def test_deep_spec_copy_raises_task_plan_error() -> None:
    nested: dict[str, Any] = {}
    current = nested
    for _ in range(2000):
        child: dict[str, Any] = {}
        current["child"] = child
        current = child

    with pytest.raises(TaskPlanError, match="too deeply nested"):
        build_tree(
            {
                "tasks": [
                    {
                        "task_id": "root",
                        "kind": "FEATURE",
                        "depends_on": [],
                        "spec": nested,
                    }
                ]
            }
        )


def test_library_operations_do_not_write_output(capsys: pytest.CaptureFixture[str]) -> None:
    tree = build_tree(_plan(["root"]))

    topological_order(tree)
    ready_tasks(tree, set())

    captured = capsys.readouterr()
    assert captured.out == ""
    assert captured.err == ""


def test_module_cli_output_remains_byte_stable() -> None:
    payload = json.dumps(
        {
            "tasks": [
                {"task_id": "root", "kind": "FEATURE", "depends_on": []},
                {"task_id": "child", "kind": "TEST", "depends_on": ["root"]},
            ]
        }
    )

    result = _run_cli(payload)

    assert result.returncode == 0
    assert result.stdout == '"root"\n"child"\n'
    assert result.stderr == ""


def test_upward_result_rejects_unvalidated_text_and_lists() -> None:
    fields = [
        ({"summary": 1}, "summary.*string"),
        ({"unified_diff": 1}, "unified_diff.*string"),
        ({"commits": ["ok", 1]}, "commits.*list of strings"),
        ({"files_changed": ["ok", 1]}, "files_changed.*list of strings"),
    ]
    for spec, message in fields:
        node = TaskNode(
            "child",
            TaskKind.TEST,
            "root",
            spec,
            1,
            0,
            NodeStatus.DONE,
        )
        with pytest.raises(TaskTreeError, match=message):
            upward_result(node)
