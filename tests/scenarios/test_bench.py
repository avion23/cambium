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

import ast
import json
import os
import subprocess
import sys
import textwrap
from pathlib import Path

import pytest

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


def _write_fixture_module(
    tmp_path: Path,
    *,
    manifest: dict | None = None,
    invalid_output: bool = False,
) -> Path:
    """Create one importable module that is driven only through its JSON CLI."""
    source_root = tmp_path / "src"
    modules_dir = source_root / "cambium" / "modules"
    package_dir = modules_dir / "fixture"
    datasets_dir = package_dir / "datasets"
    datasets_dir.mkdir(parents=True)
    (source_root / "cambium" / "__init__.py").write_text("")
    (modules_dir / "__init__.py").write_text("")
    (package_dir / "__init__.py").write_text("")
    if manifest is not None:
        (package_dir / "module.json").write_text(json.dumps(manifest))
    (package_dir / "__main__.py").write_text(
        textwrap.dedent(
            """
            import json
            import sys

            class SchemaInvalidError(ValueError):
                pass

            def main():
                payload = json.load(sys.stdin)
                if __INVALID_OUTPUT__:
                    sys.stdout.buffer.write(b"\\xff")
                    return
                if payload["operation"] != "evaluate":
                    raise ValueError("fixture only supports evaluate")
                results = []
                for record in payload["records"]:
                    expected_record = record["expected"]
                    if not isinstance(expected_record.get("reason"), str):
                        raise SchemaInvalidError("expected.reason must be a string")
                    expected = expected_record["decompose"]
                    results.append({
                        "prediction": {"decompose": expected},
                        "score": 1.0,
                    })
                print(json.dumps({"results": results}))

            if __name__ == "__main__":
                try:
                    main()
                except SchemaInvalidError as exc:
                    print(json.dumps({
                        "error": {
                            "code": "SCHEMA_INVALID",
                            "message": str(exc),
                        }
                    }))
                    raise SystemExit(1)
            """
        ).replace("__INVALID_OUTPUT__", repr(invalid_output))
    )
    records = {
        "train": {
            "id": "fixture-train-1",
            "schema_version": 1,
            "dataset_version": "fixture-1",
            "input": {"task": "Fixture train", "context": ""},
            "expected": {"decompose": False, "reason": "atomic"},
        },
        "eval": {
            "id": "fixture-eval-1",
            "schema_version": 1,
            "dataset_version": "fixture-1",
            "input": {"task": "Fixture eval", "context": ""},
            "expected": {"decompose": True, "reason": "parallel"},
        },
        "canaries": {
            "id": "fixture-canary-1",
            "schema_version": 1,
            "dataset_version": "fixture-1",
            "input": {"task": "Fixture canary", "context": ""},
            "expected": {"decompose": False, "reason": "atomic"},
            "canary": True,
            "canary_info": {"kind": "trivially_atomic"},
        },
    }
    for split, record in records.items():
        (datasets_dir / f"{split}.jsonl").write_text(json.dumps(record) + "\n")
    (datasets_dir / "meta.json").write_text(
        json.dumps({"schema_version": 1, "dataset_version": "fixture-1"})
    )
    return modules_dir


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


def test_fixture_module_report_and_gate_use_neutral_contract(tmp_path, monkeypatch) -> None:
    import cambium.bench as bench

    modules_dir = _write_fixture_module(
        tmp_path,
        manifest={
            "contract_version": 1,
            "module_name": "fixture_module",
            "cli_module": "cambium.modules.fixture",
            "protocol": "json-v1",
            "dataset_schema_version": 1,
        },
    )
    monkeypatch.setattr(bench, "MODULES_DIR", modules_dir)
    bench_root = tmp_path / "baselines"

    assert bench.main(["report", "--bench-root", str(bench_root)]) == 0
    baseline = json.loads(
        (bench_root / "fixture_module" / "baseline.json").read_text()
    )
    assert baseline["dataset_version"] == "fixture-1"
    assert baseline["dataset"]["records"] == 3
    assert baseline["metric"]["train"] == {"mean": 1.0, "std": 0.0, "count": 1}
    assert bench.main(["gate", "--bench-root", str(bench_root)]) == 0


def test_real_combined_dataset_reports_only_flagged_canaries(monkeypatch) -> None:
    import cambium.bench as bench

    load_jsonl = bench.load_jsonl

    def force_combined(path: Path) -> list[dict]:
        if path.name in {"train.jsonl", "eval.jsonl", "canaries.jsonl"}:
            raise bench.DatasetError("force combined fallback")
        return load_jsonl(path)

    monkeypatch.setattr(bench, "load_jsonl", force_combined)

    report = bench.build_module_report("example")

    assert report["dataset"]["records"] == 9
    assert report["dataset"]["canaries"] == 2
    assert report["canaries"]["total"] == 2


def test_invalid_module_name_fails_closed_before_baseline_write(
    tmp_path, monkeypatch, capsys
) -> None:
    import cambium.bench as bench

    modules_dir = _write_fixture_module(
        tmp_path,
        manifest={
            "contract_version": 1,
            "module_name": "../../target",
            "cli_module": "cambium.modules.fixture",
            "protocol": "json-v1",
            "dataset_schema_version": 1,
        },
    )
    monkeypatch.setattr(bench, "MODULES_DIR", modules_dir)
    bench_root = tmp_path / "bench" / "baselines"

    assert bench.main(["report", "--bench-root", str(bench_root)]) == 1
    assert "module_name" in capsys.readouterr().err
    assert not bench_root.exists()
    assert not (tmp_path / "target" / "baseline.json").exists()


def test_split_version_drift_fallback_fails_gate(
    tmp_path, monkeypatch, capsys
) -> None:
    import cambium.bench as bench

    modules_dir = _write_fixture_module(
        tmp_path,
        manifest={
            "contract_version": 1,
            "module_name": "fixture_module",
            "cli_module": "cambium.modules.fixture",
            "protocol": "json-v1",
            "dataset_schema_version": 1,
        },
    )
    monkeypatch.setattr(bench, "MODULES_DIR", modules_dir)
    datasets_dir = modules_dir / "fixture" / "datasets"
    invalid_split = {
        "id": "invalid-train-1",
        "schema_version": 999,
        "dataset_version": "0.0.0",
        "input": {"task": "Invalid split", "context": ""},
        "expected": {"decompose": False, "reason": "atomic"},
    }
    (datasets_dir / "train.jsonl").write_text(json.dumps(invalid_split) + "\n")
    combined = [
        {
            "id": "combined-1",
            "input": {"task": "Combined atomic", "context": ""},
            "expected": {"decompose": False, "reason": "atomic"},
        },
        {
            "id": "combined-canary-1",
            "input": {"task": "Combined canary", "context": ""},
            "expected": {"decompose": False, "reason": "atomic"},
            "canary": True,
            "canary_info": {"kind": "trivially_atomic"},
        },
    ]
    (datasets_dir / "example_pairs.jsonl").write_text(
        "".join(json.dumps(record) + "\n" for record in combined)
    )
    bench_root = tmp_path / "baselines"

    assert bench.main(["report", "--bench-root", str(bench_root)]) == 0
    baseline = json.loads((bench_root / "fixture_module" / "baseline.json").read_text())
    assert baseline["metric"]["train"] is None
    assert baseline["metric"]["eval"] is None
    assert baseline["metric"]["canaries"] is None
    assert baseline["metric"]["combined"] == {"mean": 1.0, "std": 0.0, "count": 2}
    assert baseline["dataset"]["records"] == 2
    assert baseline["canaries"]["total"] == 1
    assert "fell back to the combined file" in baseline["note"]
    assert bench.main(["gate", "--bench-root", str(bench_root)]) == 1
    output = capsys.readouterr().out
    for split in bench.SPLITS:
        assert f"DRIFT fixture_module: metric.{split}" in output
    assert "legacy combined fallback was scored" in output


def test_cli_timeout_fails_without_combined_fallback(tmp_path, monkeypatch, capsys) -> None:
    import cambium.bench as bench

    modules_dir = _write_fixture_module(
        tmp_path,
        manifest={
            "contract_version": 1,
            "module_name": "fixture_module",
            "cli_module": "cambium.modules.fixture",
            "protocol": "json-v1",
            "dataset_schema_version": 1,
        },
    )
    monkeypatch.setattr(bench, "MODULES_DIR", modules_dir)
    calls: list[list[dict]] = []

    def timeout(_cli_module, payload, **_kwargs):
        calls.append(payload["records"])
        raise bench.ModuleCLIError("simulated train-split timeout")

    monkeypatch.setattr(bench, "run_module_cli", timeout)
    bench_root = tmp_path / "baselines"

    assert bench.main(["report", "--bench-root", str(bench_root)]) == 1
    assert len(calls) == 1
    assert len(calls[0]) == 1
    assert not (bench_root / "fixture_module" / "baseline.json").exists()
    assert "ERROR ModuleCLIError: simulated train-split timeout" in capsys.readouterr().err


def test_invalid_utf8_cli_output_raises_module_cli_error(tmp_path) -> None:
    import cambium.bench as bench

    modules_dir = _write_fixture_module(tmp_path, invalid_output=True)

    with pytest.raises(bench.ModuleCLIError, match="could not be decoded"):
        bench.run_module_cli(
            "cambium.modules.fixture",
            {"operation": "evaluate", "records": []},
            cwd=REPO_ROOT,
            source_root=modules_dir.parents[1],
        )


def test_invalid_utf8_cli_output_fails_without_combined_fallback_or_baseline(
    tmp_path, monkeypatch, capsys
) -> None:
    import cambium.bench as bench

    modules_dir = _write_fixture_module(
        tmp_path,
        manifest={
            "contract_version": 1,
            "module_name": "fixture_module",
            "cli_module": "cambium.modules.fixture",
            "protocol": "json-v1",
            "dataset_schema_version": 1,
        },
        invalid_output=True,
    )
    monkeypatch.setattr(bench, "MODULES_DIR", modules_dir)
    datasets_dir = modules_dir / "fixture" / "datasets"
    (datasets_dir / "example_pairs.jsonl").write_text(
        json.dumps(
            {
                "id": "combined-1",
                "input": {"task": "Combined", "context": ""},
                "expected": {"decompose": False, "reason": "atomic"},
            }
        )
        + "\n"
    )
    calls: list[list[dict]] = []
    real_run_module_cli = bench.run_module_cli

    def record_call(_cli_module, payload, **kwargs):
        calls.append(payload["records"])
        return real_run_module_cli(_cli_module, payload, **kwargs)

    monkeypatch.setattr(bench, "run_module_cli", record_call)
    bench_root = tmp_path / "baselines"

    assert bench.main(["report", "--bench-root", str(bench_root)]) == 1
    assert len(calls) == 1
    assert len(calls[0]) == 1
    captured = capsys.readouterr()
    assert "ERROR ModuleCLIError" in captured.err
    assert "fell back to the combined file" not in captured.err
    assert not (bench_root / "fixture_module" / "baseline.json").exists()


def test_zero_canary_combined_dataset_fails_gate(tmp_path, monkeypatch) -> None:
    import cambium.bench as bench

    modules_dir = _write_fixture_module(
        tmp_path,
        manifest={
            "contract_version": 1,
            "module_name": "fixture_module",
            "cli_module": "cambium.modules.fixture",
            "protocol": "json-v1",
            "dataset_schema_version": 1,
        },
    )
    monkeypatch.setattr(bench, "MODULES_DIR", modules_dir)
    datasets_dir = modules_dir / "fixture" / "datasets"
    invalid_split = {
        "id": "invalid-train-1",
        "input": {"task": "Invalid split", "context": ""},
        "expected": {"decompose": False},
    }
    (datasets_dir / "train.jsonl").write_text(json.dumps(invalid_split) + "\n")
    normal = {
        "id": "combined-normal-1",
        "input": {"task": "Combined normal", "context": ""},
        "expected": {"decompose": False, "reason": "atomic"},
    }
    (datasets_dir / "example_pairs.jsonl").write_text(json.dumps(normal) + "\n")
    bench_root = tmp_path / "baselines"

    assert bench.main(["report", "--bench-root", str(bench_root)]) == 0
    baseline = json.loads((bench_root / "fixture_module" / "baseline.json").read_text())
    assert baseline["canaries"]["total"] == 0

    assert bench.main(["gate", "--bench-root", str(bench_root)]) != 0


def test_module_contract_violation_fails_closed_with_module_diagnostic(
    tmp_path, monkeypatch, capsys
) -> None:
    import cambium.bench as bench

    modules_dir = _write_fixture_module(
        tmp_path,
        manifest={
            "contract_version": 1,
            "cli_module": "cambium.modules.fixture",
            "protocol": "json-v1",
            "dataset_schema_version": 1,
        },
    )
    monkeypatch.setattr(bench, "MODULES_DIR", modules_dir)

    with pytest.raises(ValueError, match="fixture.*module_name"):
        bench.discover_modules()

    bench_root = tmp_path / "baselines"
    assert bench.main(["report", "--bench-root", str(bench_root)]) == 1
    assert "fixture" in capsys.readouterr().err
    assert not bench_root.exists()


def test_scripts_use_the_neutral_module_boundary() -> None:
    check = subprocess.run(
        [sys.executable, "scripts/check_dataset_v1.py"],
        cwd=REPO_ROOT,
        capture_output=True,
        text=True,
        timeout=180,
    )
    assert check.returncode == 0, check.stdout + check.stderr
    assert "through the neutral CLI" in check.stdout

    generated = subprocess.run(
        [
            sys.executable,
            "-c",
            (
                "import runpy; "
                "m = runpy.run_path('scripts/generate_should_decompose_v1.py'); "
                "print(m['neutral_decide']('Fix one typo.', ''))"
            ),
        ],
        cwd=REPO_ROOT,
        capture_output=True,
        text=True,
        timeout=180,
    )
    assert generated.returncode == 0, generated.stdout + generated.stderr
    assert "'decompose': False" in generated.stdout


def test_bench_and_scripts_have_no_concrete_module_imports() -> None:
    paths = [
        REPO_ROOT / "src" / "cambium" / "bench.py",
        REPO_ROOT / "scripts" / "check_dataset_v1.py",
        REPO_ROOT / "scripts" / "generate_should_decompose_v1.py",
    ]
    for path in paths:
        source = path.read_text(encoding="utf-8")
        tree = ast.parse(source)
        assert "importlib" not in source
        assert not any(
            isinstance(node, ast.ImportFrom)
            and node.module
            and node.module.startswith("cambium.modules.example")
            for node in ast.walk(tree)
        )


def test_gate_fails_closed_without_pre_existing_anchor(tmp_path) -> None:
    bench_root = tmp_path / "baselines"
    gate = run_bench(bench_root, "gate")

    assert gate.returncode == 1, gate.stdout + gate.stderr
    assert "missing pre-existing anchor" in gate.stdout
    assert not (bench_root / "should_decompose" / "baseline.json").exists()


def test_standalone_cli_gate_fails_closed_without_pre_existing_anchor(tmp_path) -> None:
    bench_root = tmp_path / "baselines"
    bench_root.mkdir()

    gate = subprocess.run(
        [
            sys.executable,
            "-m",
            "cambium.bench",
            "gate",
            "--bench-root",
            str(bench_root),
        ],
        cwd=REPO_ROOT,
        capture_output=True,
        text=True,
        timeout=180,
    )

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


def test_compare_combined_metric_delta_is_gated_for_fallback_reports() -> None:
    from cambium.bench import compare_against_anchor

    anchor = _fake_report()
    report = _fake_report()
    anchor["metric"] = {
        "train": None,
        "eval": None,
        "canaries": None,
        "combined": {"mean": 1.0, "std": 0.0, "count": 9},
    }
    report["metric"] = {
        "train": None,
        "eval": None,
        "canaries": None,
        "combined": {"mean": 0.0, "std": 0.0, "count": 9},
    }

    assert compare_against_anchor(report, anchor) == [
        ("metric.train", "split metric unavailable; legacy combined fallback was scored"),
        ("metric.eval", "split metric unavailable; legacy combined fallback was scored"),
        ("metric.canaries", "split metric unavailable; legacy combined fallback was scored"),
        ("metric.combined.mean", "1.0 -> 0.0 (drop 1.0000 > 0.05)"),
    ]


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
