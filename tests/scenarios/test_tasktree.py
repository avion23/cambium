"""TaskTree CLI scenarios (architecture §3.4/§3.7, D8a pipe contract).

The CLI scenarios drive ``python -m cambium.tasktree`` (and the unified
``cambium.cli tasktree`` entry point) as a real subprocess: pipe a plan in,
get the topological order as JSON lines, with exit codes for cyclic plans,
depth-bound violations, malformed JSON, and bad arguments.
"""

from __future__ import annotations

import json
import os
import pty
import subprocess
import sys
from pathlib import Path

import pytest

from cambium.tasktree import TaskKind

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


def _chain_plan(length: int = 1200) -> dict:
    return _plan([
        (f"task-{index}", "TEST", [] if index == 0 else [f"task-{index - 1}"])
        for index in range(length)
    ])


def test_task_kind_is_the_enum_norm() -> None:
    assert {kind.name for kind in TaskKind} == {
        "FEATURE", "BUGFIX", "REFACTOR", "TEST", "DOCS", "INVESTIGATION",
    }


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


@pytest.mark.slow  # real python -m subprocess; process-boundary assertions
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


@pytest.mark.slow  # real python -m subprocess; process-boundary assertions
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


@pytest.mark.slow  # real python -m subprocess; process-boundary assertions
def test_cli_explicit_dash_reads_plan_from_stdin() -> None:
    plan = _plan([
        ("r", "FEATURE", []),
        ("a", "BUGFIX", ["r"]),
    ])

    result = _run_cli(json.dumps(plan), "-")

    assert result.returncode == 0, result.stderr
    assert [json.loads(line) for line in result.stdout.splitlines()] == ["r", "a"]
    assert result.stderr == ""


@pytest.mark.slow  # real python -m subprocess; process-boundary assertions
def test_cli_explicit_dash_rejects_invalid_json_from_stdin() -> None:
    result = _run_cli("{", "-")

    assert result.returncode == 1
    assert result.stdout == ""
    assert "tasktree: invalid JSON in stdin" in result.stderr


@pytest.mark.slow  # real python -m subprocess; process-boundary assertions
def test_cli_no_args_prints_help_for_empty_stdin() -> None:
    result = _run_cli()

    assert result.returncode == 0
    assert result.stdout.startswith("usage: python -m cambium.tasktree")
    assert "PLAN" in result.stdout
    assert result.stderr == ""


@pytest.mark.slow  # real python -m subprocess on a pty; process-boundary assertions
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


@pytest.mark.slow  # real python -m subprocess; process-boundary assertions
def test_cli_entry_points_share_help_and_extra_argument_errors() -> None:
    module_help = _run_cli("", "--help")
    unified_help = _run_unified_cli("", "--help")

    assert unified_help.returncode == module_help.returncode == 0
    assert unified_help.stdout == module_help.stdout
    assert unified_help.stderr == module_help.stderr == ""

    module_extra = _run_cli("", "plan.json", "TOP_SECRET_123")
    unified_extra = _run_unified_cli("", "plan.json", "TOP_SECRET_123")

    assert unified_extra.returncode == module_extra.returncode == 2
    assert unified_extra.stdout == module_extra.stdout == ""
    assert unified_extra.stderr == module_extra.stderr
    assert "TOP_SECRET_123" not in unified_extra.stderr


@pytest.mark.slow  # real python -m subprocess; process-boundary assertions
def test_cli_rejects_invalid_json_from_stdin() -> None:
    result = _run_cli("{")

    assert result.returncode == 1
    assert result.stdout == ""
    assert "tasktree: invalid JSON in stdin" in result.stderr


@pytest.mark.slow  # real python -m subprocess; process-boundary assertions
def test_cli_bad_plan_argument_exits_two_with_stderr(tmp_path: Path) -> None:
    missing = tmp_path / "missing-plan.json"

    result = _run_cli("", str(missing))

    assert result.returncode == 2
    assert result.stdout == ""
    assert "usage:" in result.stderr
    assert "cannot read plan file" in result.stderr
    assert str(missing) in result.stderr


@pytest.mark.slow  # real python -m subprocess; process-boundary assertions
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


@pytest.mark.slow  # real python -m subprocess; process-boundary assertions
@pytest.mark.parametrize(
    "runner",
    [_run_cli, _run_unified_cli],
    ids=["module-entry-point", "unified-entry-point"],
)
def test_cli_deep_chain_exits_one_with_clean_depth_error(runner) -> None:
    result = runner(json.dumps(_chain_plan()))

    assert result.returncode == 1
    assert result.stdout == ""
    assert result.stderr.startswith("tasktree: ")
    assert "max_depth" in result.stderr
    assert "DepthBoundError" not in result.stderr
    assert "RecursionError" not in result.stderr
    assert "Traceback" not in result.stderr
