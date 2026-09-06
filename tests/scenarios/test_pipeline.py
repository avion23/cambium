"""Process-boundary coverage for tasktree CLI rejection."""

from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
SRC_DIR = str(ROOT / "src")


def _pythonpath_env() -> dict[str, str]:
    env = dict(os.environ)
    env["PYTHONPATH"] = os.pathsep.join(filter(None, [SRC_DIR, env.get("PYTHONPATH")]))
    return env


@pytest.mark.slow
def test_tasktree_cli_rejects_cycle_before_supervisor(tmp_path: Path) -> None:
    cyclic_plan = {
        "tasks": [
            {"task_id": "root", "kind": "FEATURE", "depends_on": []},
            {"task_id": "cycle-a", "kind": "TEST", "depends_on": ["cycle-c"]},
            {"task_id": "cycle-b", "kind": "TEST", "depends_on": ["cycle-a"]},
            {"task_id": "cycle-c", "kind": "TEST", "depends_on": ["cycle-b"]},
        ]
    }
    result = subprocess.run(
        [sys.executable, "-m", "cambium.tasktree"],
        cwd=str(ROOT),
        env=_pythonpath_env(),
        input=json.dumps(cyclic_plan),
        capture_output=True,
        text=True,
        timeout=30,
    )

    assert result.returncode == 1
    assert result.stdout == ""
    assert "cycle" in result.stderr
    assert all(task_id in result.stderr for task_id in ("cycle-a", "cycle-b", "cycle-c"))
    assert not (tmp_path / "session").exists()
