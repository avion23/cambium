"""Black-box smoke checks for the documented operator CLI contract."""

from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

import pytest

from cambium.process_env import build_subprocess_env

REPO_ROOT = Path(__file__).resolve().parents[2]
CLI = [sys.executable, "-m", "cambium.cli"]


def _run(*args: str) -> subprocess.CompletedProcess[str]:
    """Run the CLI as a subprocess with the repository source on its path."""
    env = build_subprocess_env(os.environ, worktree=REPO_ROOT)
    return subprocess.run(
        [*CLI, *args],
        cwd=REPO_ROOT,
        capture_output=True,
        text=True,
        env=env,
        timeout=300,
    )


def test_bare_unknown_command_is_rejected() -> None:
    result = _run("not-a-cambium-command")

    assert result.returncode != 0, result.stdout + result.stderr


def test_run_requires_prompt() -> None:
    result = _run("run")

    assert result.returncode == 2, result.stdout + result.stderr
    assert "PROMPT" in result.stderr


@pytest.mark.parametrize(
    "args",
    [
        ("supervisor", "--session-dir", "session"),
        ("supervisor", "--plan", "plan.json"),
        ("supervisor", "--task-spec", "task.json"),
        ("supervisor", "--demo"),
        (
            "supervisor",
            "--session-dir",
            "session",
            "--plan",
            "plan.json",
            "--task-spec",
            "task.json",
        ),
        (
            "supervisor",
            "--session-dir",
            "session",
            "--plan",
            "plan.json",
            "--demo",
        ),
        (
            "supervisor",
            "--session-dir",
            "session",
            "--task-spec",
            "task.json",
            "--demo",
        ),
    ],
    ids=[
        "missing-mode",
        "plan-missing-session-dir",
        "task-spec-missing-session-dir",
        "demo-missing-session-dir",
        "plan-and-task-spec",
        "plan-and-demo",
        "task-spec-and-demo",
    ],
)
def test_supervisor_requires_session_dir_and_exactly_one_input(
    args: tuple[str, ...],
) -> None:
    result = _run(*args)

    assert result.returncode == 2, result.stdout + result.stderr
