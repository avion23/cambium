"""Subprocess scenarios for the unified ``cambium`` CLI."""

from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]
SRC_DIR = str(REPO_ROOT / "src")
CLI = [sys.executable, "-m", "cambium.cli"]


def _run(*args: str, input_text: str | None = None) -> subprocess.CompletedProcess[str]:
    env = dict(os.environ)
    env["PYTHONPATH"] = os.pathsep.join(filter(None, [SRC_DIR, env.get("PYTHONPATH")]))
    return subprocess.run(
        [*CLI, *args],
        cwd=REPO_ROOT,
        input=input_text,
        capture_output=True,
        text=True,
        env=env,
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


def test_tasktree_reads_plan_from_file(tmp_path) -> None:
    plan_path = tmp_path / "plan.json"
    plan_path.write_text(
        json.dumps(
            {
                "tasks": [
                    {"task_id": "root", "kind": "FEATURE", "depends_on": []},
                    {"task_id": "leaf", "kind": "TEST", "depends_on": ["root"]},
                ]
            }
        ),
        encoding="utf-8",
    )

    result = _run("tasktree", str(plan_path))

    assert result.returncode == 0, result.stdout + result.stderr
    assert result.stdout == '"root"\n"leaf"\n'
    assert result.stderr == ""


def test_tasktree_reads_plan_from_stdin() -> None:
    plan = {
        "tasks": [
            {"task_id": "root", "kind": "FEATURE", "depends_on": []},
            {"task_id": "leaf", "kind": "TEST", "depends_on": ["root"]},
        ]
    }

    result = _run("tasktree", input_text=json.dumps(plan))

    assert result.returncode == 0, result.stdout + result.stderr
    assert result.stdout == '"root"\n"leaf"\n'
    assert result.stderr == ""


def test_tasktree_reads_explicit_stdin_plan() -> None:
    plan = {
        "tasks": [{"task_id": "root", "kind": "FEATURE", "depends_on": []}]
    }

    result = _run("tasktree", "-", input_text=json.dumps(plan))

    assert result.returncode == 0, result.stdout + result.stderr
    assert result.stdout == '"root"\n'
    assert result.stderr == ""


def test_tasktree_no_args_prints_help() -> None:
    result = _run("tasktree", input_text="")

    assert result.returncode == 0
    assert result.stdout.startswith("usage: python -m cambium.tasktree")
    assert "PLAN" in result.stdout
    assert result.stderr == ""


def test_tasktree_bad_arguments_exit_two() -> None:
    result = _run("tasktree", "plan.json", "extra")

    assert result.returncode == 2
    assert result.stdout == ""
    assert "usage: python -m cambium.tasktree" in result.stderr
    assert "unrecognized arguments: extra" in result.stderr


def test_tasktree_missing_file_exits_two(tmp_path) -> None:
    missing = tmp_path / "missing-plan.json"

    result = _run("tasktree", str(missing))

    assert result.returncode == 2
    assert result.stdout == ""
    assert "usage: python -m cambium.tasktree" in result.stderr
    assert "cannot read plan file" in result.stderr
    assert str(missing) in result.stderr


def test_tasktree_invalid_json_exits_one() -> None:
    result = _run("tasktree", input_text="{")

    assert result.returncode == 1
    assert result.stdout == ""
    assert "tasktree: invalid JSON in stdin" in result.stderr
