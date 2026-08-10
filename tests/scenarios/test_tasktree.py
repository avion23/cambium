"""TaskTree DAG-builder scenarios (architecture §3.4/§3.7, invariants I2.1-I2.7).

Pure-logic scenarios for the deterministic task-tree module: build validation
(unique ids, dependency references, single root, multi-parent rejection,
depth/width bounds), Kahn cycle detection with the cycle named, topological
order validity, the supervisor's ready/leaves scheduler inputs, subtree
info-hiding (I2.4), and the upward result envelope restricted to exactly the
current arch §3.4/§3.7 I2.7 key set. The CLI scenarios drive
``python -m cambium.tasktree`` as a real subprocess (D8a pipe contract).
"""

from __future__ import annotations

import json
import os
import pty
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
    with pytest.raises(CycleError) as exc:
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


# -- 6. upward_result: the arch §3.4/§3.7 I2.7 envelope ----------------------


_ENVELOPE_KEYS_EXACT = {
    "parent_task_id",
    "unified_diff",
    "diff_truncated",
    "summary",
    "metric_score",
    "metric_breakdown",
    "commits",
    "files_changed",
    "status",
}


def test_upward_result_exact_envelope_keys() -> None:
    node = TaskNode(
        task_id="c",
        kind=TaskKind.TEST,
        parent_task_id="a",
        spec={
            "unified_diff": "--- a/x\n+++ b/x\n",
            "diff_truncated": False,
            "summary": "Added the test gate.",
            "metric_score": 0.84,
            "metric_breakdown": {"tests": 1.0, "spec_adherence": 0.9},
            "commits": ["c9f8e7d"],
            "files_changed": ["src/x.rs"],
        },
        depth=2,
        width_idx=0,
        status=NodeStatus.DONE,
    )
    result = upward_result(node)
    assert set(result) == _ENVELOPE_KEYS_EXACT
    assert result["parent_task_id"] == "a"
    assert result["unified_diff"].startswith("--- a/x")
    assert result["diff_truncated"] is False
    assert result["summary"] == "Added the test gate."
    assert result["metric_score"] == 0.84
    assert result["metric_breakdown"] == {"tests": 1.0, "spec_adherence": 0.9}
    assert result["commits"] == ["c9f8e7d"]
    assert result["files_changed"] == ["src/x.rs"]
    assert result["status"] == NodeStatus.DONE


def test_upward_result_carries_diff_truncated_flag() -> None:
    node = TaskNode(
        task_id="c",
        kind=TaskKind.TEST,
        parent_task_id="a",
        spec={"unified_diff": "64 KiB overflow", "diff_truncated": True},
        depth=2,
        width_idx=0,
        status=NodeStatus.DONE,
    )
    result = upward_result(node)
    assert set(result) == _ENVELOPE_KEYS_EXACT
    assert result["unified_diff"] == "64 KiB overflow"
    assert result["diff_truncated"] is True


def test_upward_result_never_carries_scratchpad() -> None:
    node = TaskNode(
        task_id="c",
        kind=TaskKind.TEST,
        parent_task_id="a",
        spec={
            "scratchpad": "secret chain-of-thought",
            "reasoning": "hidden trace",
            "trajectory": [{"tool": "run_shell"}],
            "unified_diff": "d",
            "summary": "s",
        },
        depth=2,
        width_idx=0,
        status=NodeStatus.DONE,
    )
    result = upward_result(node)
    # I2.7 "exactly": no scratchpad/reasoning/trajectory fields exist to send
    assert set(result) == _ENVELOPE_KEYS_EXACT
    assert "scratchpad" not in result
    assert "reasoning" not in result
    assert "trajectory" not in result


def test_upward_result_root_has_null_parent_and_defaults() -> None:
    node = TaskNode("r", TaskKind.FEATURE, None, {}, 0, 0, NodeStatus.PENDING)
    result = upward_result(node)
    assert set(result) == _ENVELOPE_KEYS_EXACT
    assert result == {
        "parent_task_id": None,
        "unified_diff": "",
        "diff_truncated": False,
        "summary": "",
        "metric_score": None,
        "metric_breakdown": {},
        "commits": [],
        "files_changed": [],
        "status": NodeStatus.PENDING,
    }


# -- 7. CLI (D8a): pipe a plan in, get the topological order as JSON lines ----


def _run_cli(payload: str = "", *args: str) -> subprocess.CompletedProcess[str]:
    env = dict(os.environ)
    env["PYTHONPATH"] = os.pathsep.join(filter(None, [SRC_DIR, env.get("PYTHONPATH")]))
    return subprocess.run(
        [sys.executable, "-m", "cambium.tasktree", *args],
        input=payload,
        capture_output=True,
        text=True,
        env=env,
        cwd=str(REPO_ROOT),
        timeout=120,
    )


def _run_unified_cli(payload: str = "", *args: str) -> subprocess.CompletedProcess[str]:
    env = dict(os.environ)
    env["PYTHONPATH"] = os.pathsep.join(filter(None, [SRC_DIR, env.get("PYTHONPATH")]))
    return subprocess.run(
        [sys.executable, "-m", "cambium.cli", "tasktree", *args],
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


def test_cli_reads_plan_from_json_file(tmp_path: Path) -> None:
    plan = _plan([
        ("r", "FEATURE", []),
        ("a", "BUGFIX", ["r"]),
    ])
    plan_path = tmp_path / "plan.json"
    plan_path.write_text(json.dumps(plan), encoding="utf-8")

    result = _run_cli("", str(plan_path))

    assert result.returncode == 0, result.stderr
    assert [json.loads(line) for line in result.stdout.splitlines()] == ["r", "a"]
    assert result.stderr == ""


def test_cli_explicit_dash_reads_plan_from_stdin() -> None:
    plan = _plan([
        ("r", "FEATURE", []),
        ("a", "BUGFIX", ["r"]),
    ])

    result = _run_cli(json.dumps(plan), "-")

    assert result.returncode == 0, result.stderr
    assert [json.loads(line) for line in result.stdout.splitlines()] == ["r", "a"]
    assert result.stderr == ""


def test_cli_explicit_dash_rejects_invalid_json_from_stdin() -> None:
    result = _run_cli("{", "-")

    assert result.returncode == 1
    assert result.stdout == ""
    assert "tasktree: invalid JSON in stdin" in result.stderr


def test_cli_no_args_prints_help_for_empty_stdin() -> None:
    result = _run_cli()

    assert result.returncode == 0
    assert result.stdout.startswith("usage: python -m cambium.tasktree")
    assert "PLAN" in result.stdout
    assert result.stderr == ""


def test_cli_no_args_prints_help_without_waiting_on_tty() -> None:
    master_fd, slave_fd = pty.openpty()
    try:
        process = subprocess.Popen(
            [sys.executable, "-m", "cambium.tasktree"],
            stdin=slave_fd,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            env={
                **os.environ,
                "PYTHONPATH": os.pathsep.join(
                    filter(None, [SRC_DIR, os.environ.get("PYTHONPATH")])
                ),
            },
            cwd=str(REPO_ROOT),
        )
    finally:
        os.close(slave_fd)

    try:
        try:
            stdout, stderr = process.communicate(timeout=5)
        except subprocess.TimeoutExpired:
            process.kill()
            process.communicate()
            pytest.fail("no-argument tasktree CLI blocked on TTY stdin")
    finally:
        os.close(master_fd)

    assert process.returncode == 0
    assert stdout.startswith("usage: python -m cambium.tasktree")
    assert "PLAN" in stdout
    assert stderr == ""


def test_cli_entry_points_share_help_and_extra_argument_errors() -> None:
    module_help = _run_cli("", "--help")
    unified_help = _run_unified_cli("", "--help")

    assert unified_help.returncode == module_help.returncode == 0
    assert unified_help.stdout == module_help.stdout
    assert unified_help.stderr == module_help.stderr == ""

    module_extra = _run_cli("", "plan.json", "extra")
    unified_extra = _run_unified_cli("", "plan.json", "extra")

    assert unified_extra.returncode == module_extra.returncode == 2
    assert unified_extra.stdout == module_extra.stdout == ""
    assert unified_extra.stderr == module_extra.stderr
    assert "unrecognized arguments: extra" in unified_extra.stderr


def test_cli_rejects_invalid_json_from_stdin() -> None:
    result = _run_cli("{")

    assert result.returncode == 1
    assert result.stdout == ""
    assert "tasktree: invalid JSON in stdin" in result.stderr


def test_cli_bad_plan_argument_exits_two_with_stderr(tmp_path: Path) -> None:
    missing = tmp_path / "missing-plan.json"

    result = _run_cli("", str(missing))

    assert result.returncode == 2
    assert result.stdout == ""
    assert "usage:" in result.stderr
    assert "cannot read plan file" in result.stderr
    assert str(missing) in result.stderr


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
