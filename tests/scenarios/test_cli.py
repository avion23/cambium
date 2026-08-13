"""Scenarios for the unified ``cambium`` CLI.

Fast scenarios drive :func:`cambium.cli.main` in-process with ``capsys`` (and
a non-TTY fake stdin for stdin-reading commands). Only scenarios that
inherently need a real subprocess are marked slow.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

from cambium import cli

REPO_ROOT = Path(__file__).resolve().parents[2]
SRC_DIR = str(REPO_ROOT / "src")
CLI = [sys.executable, "-m", "cambium.cli"]


def _run(
    *args: str,
    input_text: str | None = None,
    extra_env: dict[str, str] | None = None,
) -> subprocess.CompletedProcess[str]:
    """Run the unified CLI as a real subprocess (slow-tier scenarios)."""
    env = dict(os.environ)
    env["PYTHONPATH"] = os.pathsep.join(filter(None, [SRC_DIR, env.get("PYTHONPATH")]))
    if extra_env is not None:
        env.update(extra_env)
    return subprocess.run(
        [*CLI, *args],
        cwd=REPO_ROOT,
        input=input_text,
        capture_output=True,
        text=True,
        env=env,
        timeout=300,
    )


@pytest.mark.slow
def test_module_test_runs_reference_module() -> None:
    # ``cambium module-test`` inherently runs the example module's real 57-test
    # pytest suite in a subprocess; this scenario stays on the slow tier.
    result = _run("module-test", "example")

    assert result.returncode == 0, result.stdout + result.stderr
    output = result.stdout + result.stderr
    assert "example: passed=51 failed=0 skipped=0" in output, output


def test_module_test_unknown_module_exits_two(capsys) -> None:
    assert cli.main(["module-test", "does_not_exist"]) == 2
    assert "unknown module" in capsys.readouterr().err


def test_module_test_rejects_arbitrary_pytest_arguments(capsys) -> None:
    with pytest.raises(SystemExit) as raised:
        cli.main(["module-test", "example", "--maxfail=1"])
    assert raised.value.code == 2
    assert "usage:" in capsys.readouterr().err


def test_architectus_scripted_dry_run_prints_actions_and_exits_zero(capsys) -> None:
    """The scripted architectus path needs no credentials or live LLM."""
    assert cli.main(["architectus", "--dry-run"]) == 0

    captured = capsys.readouterr()
    lines = captured.out.splitlines()
    assert "provider: scripted" in lines[0]
    assert json.loads(lines[1].split(": ", 1)[1]) == [{"action": "spawn", "task_id": "root"}]
    assert captured.err == ""


def test_architectus_scripted_alias_and_task_text(capsys) -> None:
    assert cli.main(["architectus", "--scripted", "--task", "inspect the module"]) == 0

    captured = capsys.readouterr()
    assert "provider: scripted" in captured.out
    assert "wave 1:" in captured.out
    assert "root" in captured.out
    assert captured.err == ""


def test_architectus_live_without_provider_config_exits_two(
    capsys, monkeypatch, tmp_path: Path
) -> None:
    """A live run with an unreadable provider config fails before any LLM call."""
    missing = tmp_path / "missing" / "providers.json"
    monkeypatch.setattr(cli, "_architectus_provider_config_path", lambda: missing)

    assert cli.main(["architectus"]) == 2

    captured = capsys.readouterr()
    assert "cambium architectus:" in captured.err
    assert "provider selection failed" in captured.err
    assert "Traceback" not in captured.err
