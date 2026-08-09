"""TaskTree DAG-builder scenarios (design-deltas D2 I2.1-I2.7, feedback-2 D8b).

Pure-logic scenarios for the deterministic task-tree module: build validation
(unique ids, dependency references, single root, multi-parent rejection,
depth/width bounds), Kahn cycle detection with the cycle named, topological
order validity, the supervisor's ready/leaves scheduler inputs, subtree
info-hiding (I2.4), and the upward result envelope restricted to exactly
I2.7's field set. The CLI scenarios drive ``python -m cambium.tasktree`` as a
real subprocess (D8a pipe contract).
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

from cambium.tasktree import (
    MAX_DEPTH,
    MAX_WIDTH,
    CycleError,
    DepthBoundError,
    DuplicateTaskError,
    MissingDependencyError,
    MultiParentError,
    NodeStatus,
    NoRootError,
    TaskKind,
    TaskNode,
    TaskPlanError,
    TaskTree,
    TaskTreeError,
    WidthBoundError,
    build_tree,
    leaves,
    ready_tasks,
    subtree_of,
    topological_order,
    upward_result,
)

SRC_DIR = str(Path(__file__).resolve().parents[2] / "src")
REPO_ROOT = Path(__file__).resolve().parents[2]


def _plan(tasks: list[tuple[str, str, list[str]]]) -> dict:
    """Build a planner payload from ``(task_id, kind, depends_on)`` triples."""
    return {
        "tasks": [
            {"task_id": task_id, "kind": kind, "depends_on": depends_on}
            for task_id, kind, depends_on in tasks
        ]
    }


def _node(tree: TaskTree, task_id: str) -> TaskNode:
    by_id = {node.task_id: node for node in tree.nodes}
    return by_id[task_id]


# -- 1. build_tree happy path + depth/width bounds ---------------------------


def test_build_tree_chain_and_fanout() -> None:
    plan = _plan([
        ("r", "FEATURE", []),
        ("a", "BUGFIX", ["r"]),
        ("b", "REFACTOR", ["r"]),
        ("c", "TEST", ["a"]),
        ("d", "DOCS", ["a"]),
    ])
    tree = build_tree(plan)

    assert len(tree.nodes) == 5
    assert set(tree.edges) == {("r", "a"), ("r", "b"), ("a", "c"), ("a", "d")}

    root = _node(tree, "r")
    assert root.parent_task_id is None
    assert root.depth == 0
    assert root.width_idx == 0
    assert _node(tree, "a").parent_task_id == "r"
    assert _node(tree, "a").depth == 1
    assert _node(tree, "a").width_idx == 0
    assert _node(tree, "b").width_idx == 1
    assert _node(tree, "c").depth == 2
    assert _node(tree, "c").width_idx == 0
    assert _node(tree, "d").width_idx == 1
    assert _node(tree, "a").kind is TaskKind.BUGFIX
    assert _node(tree, "b").kind is TaskKind.REFACTOR
    assert all(node.status is NodeStatus.PENDING for node in tree.nodes)


def test_task_kind_is_the_enum_norm() -> None:
    assert {kind.name for kind in TaskKind} == {
        "FEATURE", "BUGFIX", "REFACTOR", "TEST", "DOCS", "INVESTIGATION",
    }


def test_build_tree_max_depth_chain_is_allowed() -> None:
    plan = _plan([
        ("r", "FEATURE", []),
        ("a", "BUGFIX", ["r"]),
        ("b", "REFACTOR", ["a"]),
        ("c", "TEST", ["b"]),
    ])
    tree = build_tree(plan)
    assert max(node.depth for node in tree.nodes) == MAX_DEPTH


def test_build_tree_enforces_depth_bound() -> None:
    plan = _plan([
        ("r", "FEATURE", []),
        ("a", "BUGFIX", ["r"]),
        ("b", "REFACTOR", ["a"]),
        ("c", "TEST", ["b"]),
        ("d", "DOCS", ["c"]),  # depth 4 > MAX_DEPTH 3
    ])
    with pytest.raises(DepthBoundError) as exc:
        build_tree(plan)
    assert "max_depth" in str(exc.value)
    assert "d" in str(exc.value)


def test_build_tree_max_width_fanout_is_allowed() -> None:
    tasks = [("r", "FEATURE", [])] + [
        (f"leaf-{i}", "TEST", ["r"]) for i in range(MAX_WIDTH)
    ]
    tree = build_tree(_plan(tasks))
    assert len(tree.nodes) == MAX_WIDTH + 1
    assert len(leaves(tree)) == MAX_WIDTH


def test_build_tree_enforces_width_bound() -> None:
    tasks = [("r", "FEATURE", [])] + [
        (f"leaf-{i}", "TEST", ["r"]) for i in range(MAX_WIDTH + 1)
    ]
    with pytest.raises(WidthBoundError) as exc:
        build_tree(_plan(tasks))
    assert "max_width" in str(exc.value)
    assert "r" in str(exc.value)


def test_build_tree_custom_bounds() -> None:
    deep = _plan([
        ("r", "FEATURE", []),
        ("a", "BUGFIX", ["r"]),
        ("b", "REFACTOR", ["a"]),
    ])
    with pytest.raises(DepthBoundError):
        build_tree(deep, max_depth=1)

    wide = _plan([("r", "FEATURE", [])] + [(f"l-{i}", "TEST", ["r"]) for i in range(3)])
    with pytest.raises(WidthBoundError):
        build_tree(wide, max_width=2)

    narrow = _plan([
        ("r", "FEATURE", []),
        ("a", "BUGFIX", ["r"]),
        ("b", "REFACTOR", ["a"]),
    ])
    tree = build_tree(narrow, max_depth=2, max_width=1)
    assert max(node.depth for node in tree.nodes) == 2


# -- 2. cycle detection ------------------------------------------------------


def test_cycle_detection_names_the_cycle() -> None:
    plan = _plan([
        ("r", "FEATURE", []),
        ("a", "BUGFIX", ["b"]),
        ("b", "REFACTOR", ["c"]),
        ("c", "TEST", ["a"]),
    ])
    with pytest.raises(CycleError) as exc:
        build_tree(plan)
    message = str(exc.value)
    assert "cycle" in message
    assert " -> " in message
    for tid in ("a", "b", "c"):
        assert tid in message


def test_topological_order_raises_on_cyclic_tree() -> None:
    nodes = (
        TaskNode("r", TaskKind.FEATURE, None, {}, 0, 0, NodeStatus.PENDING),
        TaskNode("a", TaskKind.BUGFIX, "b", {}, 1, 0, NodeStatus.PENDING),
        TaskNode("b", TaskKind.REFACTOR, "c", {}, 2, 0, NodeStatus.PENDING),
        TaskNode("c", TaskKind.TEST, "a", {}, 3, 0, NodeStatus.PENDING),
    )
    tree = TaskTree(nodes=nodes, edges=(("b", "a"), ("c", "b"), ("a", "c")))
    with pytest.raises(TaskTreeError) as exc:
        topological_order(tree)
    assert " -> " in str(exc.value)
    for tid in ("a", "b", "c"):
        assert tid in str(exc.value)


def test_self_loop_is_a_cycle() -> None:
    plan = _plan([("r", "FEATURE", []), ("a", "BUGFIX", ["a"])])
    with pytest.raises(CycleError) as exc:
        build_tree(plan)
    assert "a" in str(exc.value)


# -- 3. topological order correctness ---------------------------------------


def test_topological_order_satisfies_all_edges() -> None:
    plan = _plan([
        ("r", "FEATURE", []),
        ("a", "BUGFIX", ["r"]),
        ("b", "REFACTOR", ["r"]),
        ("c", "TEST", ["a"]),
        ("d", "DOCS", ["b"]),
        ("e", "INVESTIGATION", ["c"]),
    ])
    tree = build_tree(plan)
    order = topological_order(tree)

    assert sorted(order) == sorted(node.task_id for node in tree.nodes)
    position = {tid: index for index, tid in enumerate(order)}
    for parent, child in tree.edges:
        assert position[parent] < position[child]
    assert order[0] == "r"
    assert topological_order(tree) == order  # deterministic


# -- 4. ready_tasks / leaves: the supervisor's spawn scheduler input ---------


def test_ready_tasks_advance_in_waves() -> None:
    plan = _plan([
        ("r", "FEATURE", []),
        ("a", "BUGFIX", ["r"]),
        ("b", "REFACTOR", ["r"]),
        ("c", "TEST", ["a"]),
        ("d", "DOCS", ["b"]),
    ])
    tree = build_tree(plan)

    assert [node.task_id for node in ready_tasks(tree, set())] == ["r"]
    assert [node.task_id for node in ready_tasks(tree, {"r"})] == ["a", "b"]
    assert [node.task_id for node in ready_tasks(tree, {"r", "a", "b"})] == ["c", "d"]
    assert ready_tasks(tree, {"r", "a", "b", "c", "d"}) == []


def test_ready_tasks_never_returns_finished_or_blocked() -> None:
    plan = _plan([
        ("r", "FEATURE", []),
        ("a", "BUGFIX", ["r"]),
        ("b", "REFACTOR", ["a"]),
    ])
    tree = build_tree(plan)

    # r finished: only a is ready; b waits on a, a itself is not re-offered
    ready = ready_tasks(tree, {"r"})
    assert [node.task_id for node in ready] == ["a"]
    assert all(node.status is NodeStatus.PENDING for node in ready)


def test_leaves_returns_terminal_nodes() -> None:
    plan = _plan([
        ("r", "FEATURE", []),
        ("a", "BUGFIX", ["r"]),
        ("b", "REFACTOR", ["r"]),
        ("c", "TEST", ["a"]),
    ])
    tree = build_tree(plan)
    assert [node.task_id for node in leaves(tree)] == ["b", "c"]


# -- 5. subtree_of: info hiding (I2.4) ---------------------------------------


def test_subtree_of_isolates_child_context() -> None:
    plan = _plan([
        ("r", "FEATURE", []),
        ("a", "BUGFIX", ["r"]),
        ("b", "REFACTOR", ["r"]),
        ("c", "TEST", ["a"]),
        ("d", "DOCS", ["a"]),
    ])
    tree = build_tree(plan)

    sub = subtree_of(tree, "a")
    assert [node.task_id for node in sub.nodes] == ["a", "c", "d"]
    assert set(sub.edges) == {("a", "c"), ("a", "d")}
    # the sibling and its subtree are absent (I2.4: no sibling context)
    assert "b" not in {node.task_id for node in sub.nodes}

    assert _node(sub, "a").parent_task_id is None
    assert _node(sub, "a").depth == 0
    assert _node(sub, "c").depth == 1
    assert _node(sub, "c").width_idx == 0
    assert _node(sub, "d").width_idx == 1

    full = subtree_of(tree, "r")
    assert len(full.nodes) == len(tree.nodes)
    assert len(full.edges) == len(tree.edges)


def test_subtree_of_unknown_task_raises() -> None:
    tree = build_tree(_plan([("r", "FEATURE", [])]))
    with pytest.raises(TaskTreeError):
        subtree_of(tree, "nope")


# -- 6. upward_result: the I2.7 envelope -------------------------------------


def test_upward_result_exact_envelope_keys() -> None:
    node = TaskNode(
        task_id="c",
        kind=TaskKind.TEST,
        parent_task_id="a",
        spec={
            "unified_diff": "--- a/x\n+++ b/x\n",
            "summary": "Added the test gate.",
            "metrics": {"tests": 1.0},
        },
        depth=2,
        width_idx=0,
        status=NodeStatus.DONE,
    )
    result = upward_result(node)
    assert set(result) == {"parent_task_id", "unified_diff", "summary", "metrics"}
    assert result["parent_task_id"] == "a"
    assert result["unified_diff"].startswith("--- a/x")
    assert result["summary"] == "Added the test gate."
    assert result["metrics"] == {"tests": 1.0}


def test_upward_result_never_carries_scratchpad() -> None:
    node = TaskNode(
        task_id="c",
        kind=TaskKind.TEST,
        parent_task_id="a",
        spec={
            "scratchpad": "secret chain-of-thought",
            "reasoning": "hidden trace",
            "unified_diff": "d",
            "summary": "s",
            "metrics": {},
        },
        depth=2,
        width_idx=0,
        status=NodeStatus.DONE,
    )
    result = upward_result(node)
    # I2.7 "exactly": no scratchpad/reasoning/trajectory fields exist to send
    assert set(result) == {"parent_task_id", "unified_diff", "summary", "metrics"}
    assert "scratchpad" not in result
    assert "reasoning" not in result


def test_upward_result_root_has_null_parent_and_defaults() -> None:
    node = TaskNode("r", TaskKind.FEATURE, None, {}, 0, 0, NodeStatus.PENDING)
    assert upward_result(node) == {
        "parent_task_id": None,
        "unified_diff": "",
        "summary": "",
        "metrics": {},
    }


# -- 7. CLI (D8a): pipe a plan in, get the topological order as JSON lines ----


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


def test_cli_prints_topological_order_json_lines() -> None:
    plan = _plan([
        ("r", "FEATURE", []),
        ("a", "BUGFIX", ["r"]),
        ("b", "REFACTOR", ["r"]),
    ])
    result = _run_cli(json.dumps(plan))
    assert result.returncode == 0, result.stderr
    assert [json.loads(line) for line in result.stdout.splitlines()] == ["r", "a", "b"]
    assert result.stderr == ""


def test_cli_cyclic_plan_exits_one_with_stderr() -> None:
    plan = _plan([
        ("r", "FEATURE", []),
        ("a", "BUGFIX", ["b"]),
        ("b", "REFACTOR", ["c"]),
        ("c", "TEST", ["a"]),
    ])
    result = _run_cli(json.dumps(plan))
    assert result.returncode == 1
    assert "cycle" in result.stderr
    assert result.stdout == ""


# -- 8. plan validation errors ------------------------------------------------


def test_unknown_dependency_raises() -> None:
    plan = _plan([("r", "FEATURE", []), ("a", "BUGFIX", ["missing"])])
    with pytest.raises(MissingDependencyError) as exc:
        build_tree(plan)
    assert "missing" in str(exc.value)


def test_duplicate_task_ids_raise() -> None:
    plan = _plan([("r", "FEATURE", []), ("r", "BUGFIX", ["r"])])
    with pytest.raises(DuplicateTaskError) as exc:
        build_tree(plan)
    assert "r" in str(exc.value)


def test_no_root_raises() -> None:
    plan = _plan([
        ("a", "BUGFIX", ["b"]),
        ("b", "REFACTOR", ["c"]),
        ("c", "TEST", ["a"]),
    ])
    with pytest.raises(NoRootError):
        build_tree(plan)


def test_two_roots_raise() -> None:
    plan = _plan([
        ("r1", "FEATURE", []),
        ("r2", "FEATURE", []),
    ])
    with pytest.raises(NoRootError):
        build_tree(plan)


def test_multi_parent_dependency_raises() -> None:
    plan = _plan([
        ("r", "FEATURE", []),
        ("a", "BUGFIX", ["r"]),
        ("b", "REFACTOR", ["r"]),
        ("c", "TEST", ["a", "b"]),
    ])
    with pytest.raises(MultiParentError) as exc:
        build_tree(plan)
    assert "multi-parent" in str(exc.value)


def test_invalid_kind_raises() -> None:
    plan = {"tasks": [{"task_id": "r", "kind": "SOMETHING", "depends_on": []}]}
    with pytest.raises(TaskPlanError):
        build_tree(plan)


def test_malformed_plan_raises() -> None:
    with pytest.raises(TaskPlanError):
        build_tree({"tasks": "not-a-list"})
    with pytest.raises(TaskPlanError):
        build_tree({"tasks": [{"task_id": 7, "kind": "FEATURE"}]})
    with pytest.raises(TaskPlanError):
        build_tree([1, 2, 3])
