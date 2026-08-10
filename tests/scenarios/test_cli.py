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


def test_bench_report_honors_bench_root(tmp_path) -> None:
    bench_root = tmp_path / "baselines"
    module_baseline = REPO_ROOT / "src/cambium/modules/example/tests/baselines/baseline.json"
    before = module_baseline.read_bytes()

    result = _run("bench", "report", "--bench-root", str(bench_root))

    assert result.returncode == 0, result.stdout + result.stderr
    assert (bench_root / "should_decompose" / "baseline.json").is_file()
    assert module_baseline.read_bytes() == before


def test_bench_gate_fails_closed_without_pre_existing_anchor(tmp_path) -> None:
    bench_root = tmp_path / "baselines"
    bench_root.mkdir()

    result = _run("bench", "gate", "--bench-root", str(bench_root))

    assert result.returncode == 1, result.stdout + result.stderr
    assert "missing pre-existing anchor" in result.stdout
    assert not (bench_root / "should_decompose" / "baseline.json").exists()


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


def test_module_test_runs_reference_module() -> None:
    result = _run("module-test", "example")

    assert result.returncode == 1, result.stdout + result.stderr
    output = result.stdout + result.stderr
    assert "static module-isolation findings" in output, output
    assert "src/cambium/bench.py:223:build_module_report" in output
    assert "scripts/check_dataset_v1.py:25:ExampleDatasetLoader" in output


def test_module_test_unknown_module_exits_two() -> None:
    result = _run("module-test", "does_not_exist")

    assert result.returncode == 2
    assert "unknown module" in result.stderr


def test_module_test_rejects_arbitrary_pytest_arguments() -> None:
    result = _run("module-test", "example", "--maxfail=1")

    assert result.returncode == 2
    assert "usage:" in result.stderr


def test_module_test_propagates_child_failure(monkeypatch) -> None:
    from cambium import cli

    child = subprocess.CompletedProcess(args=["pytest"], returncode=1)
    monkeypatch.setenv("EXAMPLE_API_TOKEN", "must-not-leak")

    def fail_child(command, **kwargs):
        assert "-I" not in command
        assert "EXAMPLE_API_TOKEN" not in kwargs["env"]
        return child

    monkeypatch.setattr(cli.subprocess, "run", fail_child)

    assert cli.main(["module-test", "example"]) == 1
