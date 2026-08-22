"""Black-box smoke checks for the documented operator CLI contract."""

from __future__ import annotations

import os
import re
import subprocess
import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]
SRC_DIR = str(REPO_ROOT / "src")
CLI = [sys.executable, "-m", "cambium.cli"]
UNIFIED_COMMANDS = (
    "auth",
    "supervisor",
    "doctor",
    "bench",
    "module-test",
    "version",
    "run",
    "repl",
    "tui",
    "monitor",
    "optimize",
    "session",
    "architectus",
)


def _run(*args: str) -> subprocess.CompletedProcess[str]:
    """Run the CLI as a subprocess with the repository source on its path."""
    env = dict(os.environ)
    env["PYTHONPATH"] = os.pathsep.join(
        filter(None, [SRC_DIR, env.get("PYTHONPATH")])
    )
    return subprocess.run(
        [*CLI, *args],
        cwd=REPO_ROOT,
        capture_output=True,
        text=True,
        env=env,
        timeout=300,
    )


def test_help_lists_exact_unified_commands() -> None:
    result = _run("--help")

    assert result.returncode == 0, result.stdout + result.stderr
    expected = ",".join(UNIFIED_COMMANDS)
    command_lines = [
        line.strip()[1:-1]
        for line in result.stdout.splitlines()
        if line.strip().startswith("{") and line.strip().endswith("}")
    ]
    assert command_lines == [expected], result.stdout


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


HELP_SURFACES = (
    ("--help",),
    ("auth", "--help"),
    ("auth", "set", "--help"),
    ("auth", "remove", "--help"),
    ("auth", "list", "--help"),
    ("auth", "oauth", "--help"),
    ("auth", "oauth", "login", "--help"),
    ("auth", "oauth", "status", "--help"),
    ("auth", "oauth", "logout", "--help"),
    ("auth", "oauth", "import-codex-cli", "--help"),
    ("auth", "run", "--help"),
    ("auth", "run", "supervisor", "--help"),
    ("supervisor", "--help"),
    ("doctor", "--help"),
    ("bench", "--help"),
    ("bench", "report", "--help"),
    ("bench", "gate", "--help"),
    ("bench", "re-anchor", "--help"),
    ("bench", "quality", "--help"),
    ("module-test", "--help"),
    ("version", "--help"),
    ("run", "--help"),
    ("repl", "--help"),
    ("tui", "--help"),
    ("monitor", "--help"),
    ("optimize", "--help"),
    ("session", "--help"),
    ("session", "list", "--help"),
    ("session", "latest", "--help"),
    ("session", "show", "--help"),
    ("session", "status", "--help"),
    ("session", "resume", "--help"),
    ("session", "usage", "--help"),
    ("architectus", "--help"),
)


@pytest.mark.parametrize("args", HELP_SURFACES)
def test_no_context_reuse_option_appears_in_help(args: tuple[str, ...]) -> None:
    result = _run(*args)

    assert result.returncode == 0, result.stdout + result.stderr
    assert "--context-reuse" not in result.stdout + result.stderr


def test_version_prints_a_version_string() -> None:
    result = _run("version")

    assert result.returncode == 0, result.stdout + result.stderr
    assert re.fullmatch(
        r"\d+\.\d+\.\d+(?:[.-][0-9A-Za-z.-]+)?", result.stdout.strip()
    )
