"""One black-box smoke check for the installed-style CLI boundary."""

from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

import pytest

from cambium.process_env import build_subprocess_env

REPO_ROOT = Path(__file__).resolve().parents[2]


@pytest.mark.slow
def test_module_entrypoint_rejects_unknown_command() -> None:
    """Keep one real interpreter boundary; parser cases are tested in-process."""
    result = subprocess.run(
        [sys.executable, "-m", "cambium.cli", "not-a-cambium-command"],
        cwd=REPO_ROOT,
        capture_output=True,
        text=True,
        env=build_subprocess_env(os.environ, worktree=REPO_ROOT),
        timeout=10,
    )

    assert result.returncode != 0, result.stdout + result.stderr


def test_missing_dependency_hints_environment_not_traceback(monkeypatch, capsys) -> None:
    """A third-party dep missing inside a staged module hints setup, not a raw traceback."""
    from cambium import cli

    def raise_rich_missing(name: str):
        raise ModuleNotFoundError("No module named 'rich'", name="rich")

    monkeypatch.setattr(cli.importlib, "import_module", raise_rich_missing)
    assert cli._import_or_fail("cambium.tui", "tui") is None
    err = capsys.readouterr().err
    assert "missing dependency 'rich'" in err
    assert "uv sync" in err


def test_missing_staged_module_stays_quiet(monkeypatch, capsys) -> None:
    """The not-installed path still reports the module itself, without a dep hint."""
    from cambium import cli

    def raise_tui_missing(name: str):
        raise ModuleNotFoundError("No module named 'cambium.tui'", name="cambium.tui")

    monkeypatch.setattr(cli.importlib, "import_module", raise_tui_missing)
    assert cli._import_or_fail("cambium.tui", "tui") is None
    assert "cambium tui: cambium.tui is not installed" in capsys.readouterr().err
