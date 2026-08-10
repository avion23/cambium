"""Scenario tests for the cambium bench harness itself.

The harness is exercised end-to-end: each test launches a real pytest
subprocess (the same interpreter the suite runs under) with
``-p cambium.bench --bench=...`` and asserts on the produced baseline JSON
and the process exit code.

The bench plugin writes baselines to ``--bench-root``; the tests redirect it
to a per-test temporary directory so the committed repo baselines are never
touched. Wall-time drift is disabled (``--bench-wall-ratio=100``) so the
assertions isolate the metric/exit-code behavior.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]

SCHEMA_KEYS = {
    "schema_version",
    "module",
    "dataset_version",
    "git_sha",
    "date",
    "python",
    "pytest",
    "metric",
    "canaries",
    "dataset",
    "tests",
    "drift_thresholds",
}

FAST_TESTS = [
    "tests/scenarios/test_tooling.py::test_ruff_check_clean_on_src",
    "tests/scenarios/test_tasktree.py::test_task_kind_is_the_enum_norm",
]

WALL_RATIO = "--bench-wall-ratio=100"


def run_bench(
    bench_root: Path,
    mode: str,
    *extra: str,
    env: dict[str, str] | None = None,
) -> subprocess.CompletedProcess[str]:
    """Run ``pytest -p cambium.bench --bench=<mode>`` as a subprocess."""
    full_env = dict(os.environ)
    if env:
        full_env.update(env)
    return subprocess.run(
        [
            sys.executable,
            "-m",
            "pytest",
            "-q",
            "-p",
            "cambium.bench",
            f"--bench={mode}",
            f"--bench-root={bench_root}",
            WALL_RATIO,
            *extra,
            *FAST_TESTS,
        ],
        cwd=REPO_ROOT,
        env=full_env,
        capture_output=True,
        text=True,
        timeout=180,
    )


def test_report_writes_valid_baseline(tmp_path) -> None:
    bench_root = tmp_path / "baselines"
    result = run_bench(bench_root, "report")
    assert result.returncode == 0, result.stdout + result.stderr

    baseline = json.loads((bench_root / "should_decompose" / "baseline.json").read_text())
    assert SCHEMA_KEYS <= set(baseline)
    assert baseline["module"] == "should_decompose"
    assert baseline["dataset_version"] == "1.1.0"
    assert baseline["metric"]["train"] == {"mean": 1.0, "std": 0.0, "count": 200}
    assert baseline["metric"]["eval"]["count"] == 50
    assert baseline["metric"]["canaries"]["count"] == 10
    assert baseline["canaries"]["total"] == 10
    assert baseline["canaries"]["failed"] == 0
    assert baseline["canaries"]["taxonomy_coverage"] == 1.0
    assert baseline["dataset"]["records"] == 260
    assert baseline["dataset"]["duplicate_ids"] == 0
    assert baseline["dataset"]["cross_split_leaks"] == 0
    assert baseline["dataset"]["canaries"] == 10
    assert baseline["tests"]["count"] == len(FAST_TESTS)
    assert set(baseline["tests"]["wall_seconds"]) == {"p50", "p90", "max"}
    assert baseline["tests"]["by_nodeid"].keys() == set(FAST_TESTS)


def test_gate_passes_without_drift(tmp_path) -> None:
    bench_root = tmp_path / "baselines"
    report = run_bench(bench_root, "report")
    assert report.returncode == 0, report.stdout + report.stderr
    gate = run_bench(bench_root, "gate")
    assert gate.returncode == 0, gate.stdout + gate.stderr
    assert "DRIFT" not in gate.stdout


def test_gate_fails_on_metric_drift(tmp_path) -> None:
    bench_root = tmp_path / "baselines"
    (tmp_path / "drift_inject.py").write_text(
        "import cambium.bench as _bench\n"
        "\n"
        "def _fake(module, scored):\n"
        "    return {'mean': 0.9, 'std': 0.0, 'count': len(scored)}\n"
        "\n"
        "_bench.score_examples = _fake\n"
    )
    report = run_bench(bench_root, "report")
    assert report.returncode == 0, report.stdout + report.stderr

    gate = run_bench(bench_root, "gate", "-p", "drift_inject", env={"PYTHONPATH": str(tmp_path)})
    assert gate.returncode == 1, gate.stdout + gate.stderr
    assert "DRIFT metric.train.mean" in gate.stdout
    assert "1.0 -> 0.9" in gate.stdout
