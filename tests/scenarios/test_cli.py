"""Scenarios for the unified ``cambium`` CLI.

Fast scenarios drive :func:`cambium.cli.main` in-process with ``capsys`` (and
a non-TTY fake stdin for stdin-reading commands). Only scenarios that
inherently need a real subprocess are marked slow.
"""

from __future__ import annotations

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
    assert "example: passed=54 failed=0 skipped=0" in output, output


def test_module_test_unknown_module_exits_two(capsys) -> None:
    assert cli.main(["module-test", "does_not_exist"]) == 2
    assert "unknown module" in capsys.readouterr().err


def test_module_test_rejects_arbitrary_pytest_arguments(capsys) -> None:
    with pytest.raises(SystemExit) as raised:
        cli.main(["module-test", "example", "--maxfail=1"])
    assert raised.value.code == 2
    assert "usage:" in capsys.readouterr().err


@pytest.mark.parametrize("option", ["--plan", "--task-spec"])
def test_unified_supervisor_forwards_module_input_options(
    option, monkeypatch, tmp_path: Path
) -> None:
    from cambium import supervisor

    calls: list[list[str]] = []

    def fake_main(argv=None):
        calls.append(list(argv or []))
        return 17

    monkeypatch.setattr(supervisor, "main", fake_main)

    assert cli.main(["supervisor", "--session-dir", str(tmp_path), option, "input.json"]) == 17
    assert calls == [["--session-dir", str(tmp_path), option, "input.json"]]


def test_unified_supervisor_forwards_demo_and_warm_pool(monkeypatch, tmp_path: Path) -> None:
    from cambium import supervisor

    calls: list[list[str]] = []
    monkeypatch.setattr(
        supervisor,
        "main",
        lambda argv=None: calls.append(list(argv or [])) or 0,
    )

    assert (
        cli.main(
            [
                "supervisor",
                "--session-dir",
                str(tmp_path),
                "--demo",
                "--warm-pool-size",
                "2",
                "--conversations",
            ]
        )
        == 0
    )
    assert calls == [
        [
            "--session-dir",
            str(tmp_path),
            "--demo",
            "--warm-pool-size",
            "2",
            "--conversations",
        ]
    ]


@pytest.mark.parametrize("optimizer", ["bootstrap", "gepa"])
def test_unified_optimize_forwards_dataset_and_optimizer_options(
    optimizer, monkeypatch, tmp_path: Path
) -> None:
    from cambium import optimize

    calls: list[list[str]] = []
    monkeypatch.setattr(
        optimize,
        "main",
        lambda argv=None: calls.append(list(argv or [])) or 23,
    )
    dataset = tmp_path / "train_queue.jsonl"

    assert (
        cli.main(
            [
                "optimize",
                "should_decompose",
                "--optimizer",
                optimizer,
                "--budget-usd",
                "5.00",
                "--seed",
                "7",
                "--tier",
                "fast",
                "--dry-run",
                "--dataset",
                str(dataset),
            ]
        )
        == 23
    )
    assert calls == [
        [
            "should_decompose",
            "--optimizer",
            optimizer,
            "--budget-usd",
            "5.0",
            "--seed",
            "7",
            "--tier",
            "fast",
            "--dry-run",
            "--dataset",
            str(dataset),
        ]
    ]


def test_removed_context_reuse_option_is_not_in_run_help(capsys) -> None:
    with pytest.raises(SystemExit) as raised:
        cli.main(["run", "--help"])
    assert raised.value.code == 0
    assert "--context-reuse" not in capsys.readouterr().out


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
