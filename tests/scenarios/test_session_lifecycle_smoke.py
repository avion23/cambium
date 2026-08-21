"""One subprocess smoke of the documented session operator surface."""

from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]
SRC_DIR = str(REPO_ROOT / "src")
CLI = [sys.executable, "-m", "cambium.cli"]


def _run(*args: str) -> subprocess.CompletedProcess[str]:
    env = dict(os.environ)
    env["PYTHONPATH"] = os.pathsep.join(filter(None, [SRC_DIR, env.get("PYTHONPATH")]))
    try:
        return subprocess.run(
            [*CLI, *args],
            cwd=REPO_ROOT,
            capture_output=True,
            text=True,
            env=env,
            timeout=300,
        )
    except OSError as exc:
        pytest.skip(f"cannot spawn Cambium subprocess: {exc}")


def _assert_command(
    result: subprocess.CompletedProcess[str], args: tuple[str, ...], substring: str
) -> None:
    output = result.stdout + result.stderr
    command = " ".join([*CLI, *args])
    assert result.returncode == 0, (
        f"{command} exited {result.returncode}\n"
        f"stdout:\n{result.stdout}\n"
        f"stderr:\n{result.stderr}"
    )
    assert substring in output, f"{command} output did not contain {substring!r}\n{output}"


def test_session_lifecycle_smoke(tmp_path: Path) -> None:
    session_root = tmp_path / "sessions"
    session_root.mkdir()
    session_dir = session_root / "smoke"
    session_id = session_dir.name

    args = ("--help",)
    _assert_command(_run(*args), args, "usage: cambium")

    args = ("supervisor", "--session-dir", str(session_dir), "--demo")
    _assert_command(_run(*args), args, "plan: exit_code=0")

    args = ("session", "list", "--session-dir", str(session_root))
    _assert_command(_run(*args), args, str(session_dir.resolve()))

    args = ("session", "show", "--session-dir", str(session_root), session_id)
    show = _run(*args)
    _assert_command(show, args, '"status":"done"')
    assert isinstance(json.loads(show.stdout), dict)

    args = ("session", "status", "--session-dir", str(session_root), session_id)
    _assert_command(_run(*args), args, "demo-001")

    args = ("session", "usage", "--session-dir", str(session_root), session_id)
    usage = _run(*args)
    assert usage.returncode == 1, (
        f"{' '.join([*CLI, *args])} exited {usage.returncode}\n"
        f"stdout:\n{usage.stdout}\nstderr:\n{usage.stderr}"
    )
    assert "cambium session:" in usage.stderr, usage.stderr

    args = ("session", "resume", str(session_dir))
    _assert_command(_run(*args), args, "plan: exit_code=0")
