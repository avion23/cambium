"""One black-box smoke check for the installed-style CLI boundary."""

from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

import pytest

from cambium.process_env import build_subprocess_env

REPO_ROOT = Path(__file__).resolve().parents[2]

pytestmark = pytest.mark.slow


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
