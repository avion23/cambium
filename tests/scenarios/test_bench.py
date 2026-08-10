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


def test_gate_fails_closed_without_pre_existing_anchor(tmp_path) -> None:
    bench_root = tmp_path / "baselines"
    gate = run_bench(bench_root, "gate")

    assert gate.returncode == 1, gate.stdout + gate.stderr
    assert "missing pre-existing anchor" in gate.stdout
    assert not (bench_root / "should_decompose" / "baseline.json").exists()


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


# ---------------------------------------------------------------------------
# dataset_version re-anchor (design §3) and drift-threshold behavior
# ---------------------------------------------------------------------------


def _fake_report(
    mean: float = 1.0,
    dataset_version: str = "1.1.0",
    failed: int = 0,
    wall_p90: float = 0.02,
) -> dict:
    """A minimal but schema-shaped report/anchor body for unit comparisons."""
    return {
        "schema_version": 1,
        "module": "should_decompose",
        "dataset_version": dataset_version,
        "metric": {
            "train": {"mean": mean, "std": 0.0, "count": 200},
            "eval": {"mean": mean, "std": 0.0, "count": 50},
            "canaries": {"mean": mean, "std": 0.0, "count": 10},
        },
        "canaries": {
            "total": 10,
            "kinds_present": [],
            "taxonomy_coverage": 1.0,
            "failed": failed,
        },
        "dataset": {
            "records": 260,
            "duplicate_ids": 0,
            "cross_split_leaks": 0,
            "decompose_true": 128,
            "decompose_false": 132,
            "canaries": 10,
        },
        "tests": {
            "count": 2,
            "wall_seconds": {"p50": 0.01, "p90": wall_p90, "max": 0.02},
            "by_nodeid": {},
        },
    }


def test_compare_stale_dataset_version_returns_reanchor_sentinel() -> None:
    from cambium.bench import compare_against_anchor

    report = _fake_report(dataset_version="1.1.0")
    stale = _fake_report(dataset_version="1.0.0")
    assert compare_against_anchor(report, stale) is None
    # same version, no drift -> no regressions
    assert compare_against_anchor(report, report) == []


def test_compare_metric_delta_default_and_run_override() -> None:
    from cambium.bench import compare_against_anchor

    anchor = _fake_report(mean=1.0)
    assert compare_against_anchor(_fake_report(mean=0.9), anchor) != []  # drop 0.1
    assert compare_against_anchor(_fake_report(mean=0.995), anchor) == []  # drop 0.005

    run_thresholds = {"metric_mean_delta": 0.01}
    assert compare_against_anchor(_fake_report(mean=0.98), anchor, run_thresholds) != []
    assert compare_against_anchor(_fake_report(mean=0.995), anchor, run_thresholds) == []


def test_compare_canary_failed_delta_is_wired() -> None:
    from cambium.bench import compare_against_anchor

    anchor = _fake_report(failed=0)
    report_with_one_failure = _fake_report(failed=1)
    assert compare_against_anchor(report_with_one_failure, anchor) != []  # delta 0
    assert compare_against_anchor(report_with_one_failure, anchor, {"canary_failed_delta": 2}) == []
    assert compare_against_anchor(_fake_report(failed=2), _fake_report(failed=1)) != []


def test_gate_reanchors_on_dataset_version_change(tmp_path) -> None:
    """A baseline for an older dataset_version must not fail the gate:
    it is replaced by a new anchor for the current version (design §3)."""
    bench_root = tmp_path / "baselines"
    report = run_bench(bench_root, "report")
    assert report.returncode == 0, report.stdout + report.stderr
    anchor_path = bench_root / "should_decompose" / "baseline.json"
    anchor = json.loads(anchor_path.read_text())
    assert anchor["dataset_version"] == "1.1.0"
    anchor["dataset_version"] = "1.0.0"  # simulate a pre-bump anchor
    anchor_path.write_text(json.dumps(anchor))

    gate = run_bench(bench_root, "gate")
    assert gate.returncode == 0, gate.stdout + gate.stderr
    assert "RE-ANCHOR should_decompose: 1.0.0 -> 1.1.0" in gate.stdout
    fresh = json.loads(anchor_path.read_text())
    assert fresh["dataset_version"] == "1.1.0"


def test_gate_honors_cli_metric_delta_override(tmp_path) -> None:
    """--bench-metric-delta must override the anchor thresholds on a gate run."""
    bench_root = tmp_path / "baselines"
    report = run_bench(bench_root, "report")
    assert report.returncode == 0, report.stdout + report.stderr

    (tmp_path / "drift_2pct.py").write_text(
        "import cambium.bench as _bench\n"
        "\n"
        "def _fake(module, scored):\n"
        "    return {'mean': 0.98, 'std': 0.0, 'count': len(scored)}\n"
        "\n"
        "_bench.score_examples = _fake\n"
    )
    fail = run_bench(
        bench_root,
        "gate",
        "-p",
        "drift_2pct",
        "--bench-metric-delta",
        "0.01",
        env={"PYTHONPATH": str(tmp_path)},
    )
    assert fail.returncode == 1, fail.stdout + fail.stderr
    assert "DRIFT metric.train.mean" in fail.stdout  # drop 0.02 > 0.01

    (tmp_path / "drift_half_pct.py").write_text(
        "import cambium.bench as _bench\n"
        "\n"
        "def _fake(module, scored):\n"
        "    return {'mean': 0.995, 'std': 0.0, 'count': len(scored)}\n"
        "\n"
        "_bench.score_examples = _fake\n"
    )
    ok = run_bench(
        bench_root,
        "gate",
        "-p",
        "drift_half_pct",
        "--bench-metric-delta",
        "0.01",
        env={"PYTHONPATH": str(tmp_path)},
    )
    assert ok.returncode == 0, ok.stdout + ok.stderr
    assert "DRIFT" not in ok.stdout  # drop 0.005 <= 0.01


def test_cli_report_protects_committed_baseline(tmp_path) -> None:
    """CLI report without --bench-root writes to .cambium/, never the
    committed baseline, and leaves the tree clean."""
    committed = (
        REPO_ROOT
        / "src"
        / "cambium"
        / "modules"
        / "example"
        / "tests"
        / "baselines"
        / "baseline.json"
    )
    before = committed.read_bytes()
    status_before = subprocess.run(
        ["git", "status", "--porcelain"],
        cwd=REPO_ROOT,
        capture_output=True,
        text=True,
        timeout=60,
    ).stdout

    result = subprocess.run(
        [sys.executable, "-m", "cambium.bench", "report"],
        cwd=REPO_ROOT,
        capture_output=True,
        text=True,
        timeout=180,
    )
    assert result.returncode == 0, result.stdout + result.stderr

    assert committed.read_bytes() == before  # committed baseline untouched
    runtime = (
        REPO_ROOT / ".cambium" / "baselines" / "should_decompose" / "baseline.json"
    )
    assert runtime.exists()
    assert json.loads(runtime.read_text())["module"] == "should_decompose"

    status_after = subprocess.run(
        ["git", "status", "--porcelain"],
        cwd=REPO_ROOT,
        capture_output=True,
        text=True,
        timeout=60,
    ).stdout
    assert status_after == status_before, status_after  # only gitignored writes
