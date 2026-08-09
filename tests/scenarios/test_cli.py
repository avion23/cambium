"""Subprocess scenarios for the unified ``cambium`` CLI."""

from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]
CLI = [sys.executable, "-m", "cambium.cli"]


def _run(*args: str, input_text: str | None = None) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [*CLI, *args],
        cwd=REPO_ROOT,
        input=input_text,
        capture_output=True,
        text=True,
        timeout=300,
    )


def test_version_prints_package_version() -> None:
    result = _run("version")

    assert result.returncode == 0, result.stdout + result.stderr
    assert result.stdout == "0.1.0\n"
    assert result.stderr == ""


def test_doctor_exits_zero_on_healthy_repo() -> None:
    result = _run("doctor")

    assert result.returncode == 0, result.stdout + result.stderr
    assert "Summary:" in result.stdout
    assert "0 fail" in result.stdout


def test_unknown_subcommand_exits_two() -> None:
    result = _run("not-a-command")

    assert result.returncode == 2
    assert "usage:" in result.stderr


def test_tasktree_cyclic_plan_exits_one() -> None:
    plan = {
        "tasks": [
            {"task_id": "root", "kind": "FEATURE", "depends_on": []},
            {"task_id": "a", "kind": "BUGFIX", "depends_on": ["b"]},
            {"task_id": "b", "kind": "REFACTOR", "depends_on": ["c"]},
            {"task_id": "c", "kind": "TEST", "depends_on": ["a"]},
        ]
    }
    result = _run("tasktree", input_text=json.dumps(plan))

    assert result.returncode == 1
    assert "cycle" in result.stderr
    assert result.stdout == ""
