"""Scenarios for the unified ``cambium`` CLI.

Fast scenarios drive :func:`cambium.cli.main` in-process with ``capsys`` (and
a non-TTY fake stdin for stdin-reading commands). The bench scenarios
substitute deterministic timings for the standalone bench CLI's nested
module-test re-measurement (the FAKE_TIMINGS pattern from test_bench.py). Only
scenarios that inherently need a real subprocess are marked slow.
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

# Deterministic module-test timings for bench scenarios whose assertions do not
# involve wall time. The standalone bench CLI re-measures the example module's
# full 57-test suite in a nested pytest subprocess per invocation; substituting
# a fixed timing set keeps those tests exercising the report/gate code paths
# without repeating identical subprocess work.
FAKE_TIMINGS = {
    "src/cambium/modules/example/tests/"
    "test_example_module.py::test_dataset_is_loadable_and_schema_valid": 0.01,
}


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


class _PipedStdin:
    """A non-TTY stdin pipe, matching the subprocess ``input_text`` contract."""

    def __init__(self, payload: str) -> None:
        self._payload = payload.encode()
        self.buffer = self

    def isatty(self) -> bool:
        return False

    def read(self) -> bytes:
        return self._payload


def _feed_stdin(monkeypatch: pytest.MonkeyPatch, payload: str) -> None:
    monkeypatch.setattr(sys, "stdin", _PipedStdin(payload))


def _bench_main(monkeypatch: pytest.MonkeyPatch, *args: str) -> int:
    import cambium.bench as bench

    monkeypatch.setattr(bench, "_measure_module_timings", lambda _pkg: dict(FAKE_TIMINGS))
    return bench.main([*args])


def test_version_prints_package_version(capsys) -> None:
    assert cli.main(["version"]) == 0

    captured = capsys.readouterr()
    assert captured.out == "0.1.0\n"
    assert captured.err == ""


def test_doctor_exits_zero_on_healthy_repo(tmp_path, monkeypatch, capsys) -> None:
    provider_config = tmp_path / "providers.json"
    provider_config.write_text('{"providers": []}\n', encoding="utf-8")

    monkeypatch.chdir(REPO_ROOT)
    monkeypatch.setenv("CAMBIUM_PROVIDERS", str(provider_config))
    assert cli.main(["doctor"]) == 0

    captured = capsys.readouterr()
    assert "Summary:" in captured.out
    assert "0 fail" in captured.out


def test_bench_report_honors_bench_root(tmp_path, monkeypatch) -> None:
    bench_root = tmp_path / "baselines"
    module_baseline = REPO_ROOT / "src/cambium/modules/example/tests/baselines/baseline.json"
    before = module_baseline.read_bytes()

    assert _bench_main(monkeypatch, "report", "--bench-root", str(bench_root)) == 0

    assert (bench_root / "should_decompose" / "baseline.json").is_file()
    assert module_baseline.read_bytes() == before


def test_bench_gate_fails_closed_without_pre_existing_anchor(
    tmp_path, monkeypatch, capsys
) -> None:
    bench_root = tmp_path / "baselines"
    bench_root.mkdir()

    assert _bench_main(monkeypatch, "gate", "--bench-root", str(bench_root)) == 1

    assert "missing pre-existing anchor" in capsys.readouterr().out
    assert not (bench_root / "should_decompose" / "baseline.json").exists()


def test_unknown_subcommand_exits_two(capsys) -> None:
    with pytest.raises(SystemExit) as raised:
        cli.main(["not-a-command"])
    assert raised.value.code == 2
    assert "usage:" in capsys.readouterr().err


def test_tasktree_cyclic_plan_exits_one(monkeypatch, capsys) -> None:
    plan = {
        "tasks": [
            {"task_id": "root", "kind": "FEATURE", "depends_on": []},
            {"task_id": "a", "kind": "BUGFIX", "depends_on": ["b"]},
            {"task_id": "b", "kind": "REFACTOR", "depends_on": ["c"]},
            {"task_id": "c", "kind": "TEST", "depends_on": ["a"]},
        ]
    }
    _feed_stdin(monkeypatch, json.dumps(plan))

    assert cli.main(["tasktree"]) == 1

    captured = capsys.readouterr()
    assert "cycle" in captured.err
    assert captured.out == ""


def test_tasktree_reads_plan_from_file(tmp_path, capsys) -> None:
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

    assert cli.main(["tasktree", str(plan_path)]) == 0

    captured = capsys.readouterr()
    assert captured.out == '"root"\n"leaf"\n'
    assert captured.err == ""


def test_tasktree_reads_plan_from_stdin(monkeypatch, capsys) -> None:
    plan = {
        "tasks": [
            {"task_id": "root", "kind": "FEATURE", "depends_on": []},
            {"task_id": "leaf", "kind": "TEST", "depends_on": ["root"]},
        ]
    }
    _feed_stdin(monkeypatch, json.dumps(plan))

    assert cli.main(["tasktree"]) == 0

    captured = capsys.readouterr()
    assert captured.out == '"root"\n"leaf"\n'
    assert captured.err == ""


def test_tasktree_reads_explicit_stdin_plan(monkeypatch, capsys) -> None:
    plan = {
        "tasks": [{"task_id": "root", "kind": "FEATURE", "depends_on": []}]
    }
    _feed_stdin(monkeypatch, json.dumps(plan))

    assert cli.main(["tasktree", "-"]) == 0

    captured = capsys.readouterr()
    assert captured.out == '"root"\n'
    assert captured.err == ""


def test_tasktree_no_args_prints_help(monkeypatch, capsys) -> None:
    _feed_stdin(monkeypatch, "")

    assert cli.main(["tasktree"]) == 0

    captured = capsys.readouterr()
    assert captured.out.startswith("usage: python -m cambium.tasktree")
    assert "PLAN" in captured.out
    assert captured.err == ""


def test_tasktree_bad_arguments_exit_two(capsys) -> None:
    with pytest.raises(SystemExit) as raised:
        cli.main(["tasktree", "plan.json", "TOP_SECRET_123"])
    assert raised.value.code == 2
    captured = capsys.readouterr()
    assert captured.out == ""
    assert "usage: python -m cambium.tasktree" in captured.err
    assert "TOP_SECRET_123" not in captured.err


def test_tasktree_missing_file_exits_two(tmp_path, capsys) -> None:
    missing = tmp_path / "missing-plan.json"

    with pytest.raises(SystemExit) as raised:
        cli.main(["tasktree", str(missing)])
    assert raised.value.code == 2
    captured = capsys.readouterr()
    assert captured.out == ""
    assert "usage: python -m cambium.tasktree" in captured.err
    assert "cannot read plan file" in captured.err
    assert str(missing) in captured.err


def test_tasktree_invalid_json_exits_one(monkeypatch, capsys) -> None:
    _feed_stdin(monkeypatch, "{")

    assert cli.main(["tasktree"]) == 1

    captured = capsys.readouterr()
    assert captured.out == ""
    assert "tasktree: invalid JSON in stdin" in captured.err


@pytest.mark.slow
def test_module_test_runs_reference_module() -> None:
    # ``cambium module-test`` inherently runs the example module's real 57-test
    # pytest suite in a subprocess; this scenario stays on the slow tier.
    result = _run("module-test", "example")

    assert result.returncode == 0, result.stdout + result.stderr
    output = result.stdout + result.stderr
    assert "example: passed=57 failed=0 skipped=0" in output, output


def test_module_test_unknown_module_exits_two(capsys) -> None:
    assert cli.main(["module-test", "does_not_exist"]) == 2
    assert "unknown module" in capsys.readouterr().err


def test_module_test_rejects_arbitrary_pytest_arguments(capsys) -> None:
    with pytest.raises(SystemExit) as raised:
        cli.main(["module-test", "example", "--maxfail=1"])
    assert raised.value.code == 2
    assert "usage:" in capsys.readouterr().err
