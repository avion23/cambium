"""Scenario tests for the cambium bench harness itself.

The harness is exercised end-to-end in-process: the bench pytest plugin is
loaded through ``pytest.main(["-p", "cambium.bench", "--bench=..."])`` and the
standalone CLI through ``cambium.bench.main([...])`` instead of launching a
nested interpreter per invocation (interpreter startup plus real module
scoring dominated the old subprocess-based suite). Only the genuinely
process-level behaviors still spawn subprocesses: the standalone timing
subprocesses that must stay live (``@pytest.mark.slow`` tier-2), the
dataset-check script, and the git-status guarantee.

The bench plugin writes baselines to ``--bench-root``; the tests redirect it
to a per-test temporary directory so the committed repo baselines are never
touched. Wall-time drift is disabled (``--bench-wall-ratio=100``) so the
assertions isolate the metric/exit-code behavior. Deterministic fixture
modules replace the reference module wherever the assertions do not depend on
its exact values; tests that do depend on the real module score it in-process
(the module's own ``__main__`` runs unchanged, just without per-split
interpreter startup).
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
import textwrap
from contextlib import redirect_stderr, redirect_stdout
from io import StringIO
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]

EXAMPLE_MODULE_DIR = REPO_ROOT / "src" / "cambium" / "modules" / "example"
REQUIRES_EXAMPLE = pytest.mark.skipif(
    not EXAMPLE_MODULE_DIR.is_dir(),
    reason="reference module cambium.modules.example is absent",
)

# Tier-2: requires real wall timings or real process behavior; excluded from
# the default fast run (tier-1 = -m "not slow") but run explicitly with -m slow.
SLOW = pytest.mark.slow

SCHEMA_KEYS = {
    "schema_version",
    "module",
    "dataset_version",
    "split_digests",
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

MODULE_TESTS = [
    "src/cambium/modules/example/tests/test_dataset_splits.py::test_all_260_records_score_perfectly",
    "src/cambium/modules/example/tests/test_dataset_splits.py::test_split_loads_return_expected_counts",
    "src/cambium/modules/example/tests/test_example_module.py::test_dataset_is_loadable_and_schema_valid",
]

UNRELATED_TESTS = [
    "tests/scenarios/test_redact.py::test_provider_values_jwt_private_key_and_email_are_scrubbed",
]

FAST_TESTS = MODULE_TESTS + UNRELATED_TESTS

WALL_RATIO = "--bench-wall-ratio=100"

# Deterministic module-test timings for CLI-path tests whose assertions do not
# involve wall time (drift reports, re-anchor, dataset_version fail-closed).
# The standalone CLI normally re-measures the module's full test suite (57
# tests) in a nested pytest subprocess per invocation (~3.5s); substituting a
# fixed timing set keeps those tests exercising the report/gate/drift-report
# code paths without repeating identical subprocess work.
FAKE_TIMINGS = {
    "src/cambium/modules/example/tests/"
    "test_example_module.py::test_dataset_is_loadable_and_schema_valid": 0.01,
}


class _BenchResult:
    """Stand-in for ``subprocess.CompletedProcess`` on in-process runs."""

    def __init__(self, returncode: int, stdout: str, stderr: str) -> None:
        self.returncode = returncode
        self.stdout = stdout
        self.stderr = stderr


def run_plugin_bench(
    bench_root: Path, mode: str, *extra: str, tests: list[str] | None = None
) -> _BenchResult:
    """Run the bench pytest plugin in-process (no nested interpreter).

    The module under test is selected by monkeypatching
    ``cambium.bench.MODULES_DIR`` in the calling test before this runs; the
    plugin executes in the same process, so the patch drives its session-finish
    work. Output is captured to the returned object.
    """
    if not EXAMPLE_MODULE_DIR.is_dir():
        pytest.skip("reference module cambium.modules.example is absent")
    stdout, stderr = StringIO(), StringIO()
    with redirect_stdout(stdout), redirect_stderr(stderr):
        returncode = pytest.main(
            [
                "-q",
                "-p",
                "cambium.bench",
                f"--bench={mode}",
                f"--bench-root={bench_root}",
                WALL_RATIO,
                "-p",
                "no:xdist",
                "-o",
                "addopts=",
                "-p",
                "no:cacheprovider",
                "-p",
                "no:capture",
                "--no-header",
                "-p",
                "no:warnings",
                *extra,
                *(tests if tests is not None else FAST_TESTS),
            ]
        )
    return _BenchResult(returncode, stdout.getvalue(), stderr.getvalue())


def _inprocess_module_cli(cli_module, payload, **kwargs):
    """Run a module's real JSON CLI in-process instead of in a subprocess.

    Used only by tests whose assertions target the report/gate content, not
    the subprocess transport (which the fixture and credential tests exercise
    directly). The module's actual ``__main__`` runs unchanged in a worker
    thread — its ``asyncio.run`` cannot nest inside the harness's own event
    loop — so only the per-split interpreter startup is avoided.
    """
    import importlib
    import io
    import json as _json
    import threading

    from cambium.modules.base import ModuleCLIError

    module = importlib.import_module(f"{cli_module}.__main__")
    stdout, stderr = io.StringIO(), io.StringIO()
    holder: dict[str, int] = {}

    class _Stdin:
        buffer = io.BytesIO((_json.dumps(payload, ensure_ascii=False) + "\n").encode())

    def _run() -> None:
        with redirect_stdout(stdout), redirect_stderr(stderr):
            holder["rc"] = module.main()

    stdin_old = sys.stdin
    sys.stdin = _Stdin()
    try:
        thread = threading.Thread(target=_run)
        thread.start()
        thread.join()
    finally:
        sys.stdin = stdin_old
    if holder["rc"] != 0:
        raise ModuleCLIError(
            f"module {cli_module!r}: CLI exited {holder['rc']}: {stderr.getvalue().strip()[:300]}"
        )
    return _json.loads(stdout.getvalue())


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
    if manifest is None:
        manifest = {
            "contract_version": 1,
            "module_name": "fixture_module",
            "cli_module": "cambium.modules.fixture",
            "protocol": "json-v1",
            "dataset_schema_version": 1,
        }
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


def _write_env_probe_module(tmp_path: Path) -> Path:
    """Create one importable module whose CLI dumps its inherited env to stdout."""
    source_root = tmp_path / "src"
    modules_dir = source_root / "cambium" / "modules"
    package_dir = modules_dir / "envprobe"
    package_dir.mkdir(parents=True)
    (source_root / "cambium" / "__init__.py").write_text("")
    (modules_dir / "__init__.py").write_text("")
    (package_dir / "__init__.py").write_text("")
    (package_dir / "__main__.py").write_text(
        textwrap.dedent(
            """
            import json
            import os
            import sys

            sys.stdin.buffer.read()
            print(json.dumps(dict(os.environ), sort_keys=True))
            """
        )
    )
    return modules_dir


@REQUIRES_EXAMPLE
@pytest.mark.slow
def test_report_writes_valid_baseline(tmp_path, monkeypatch) -> None:
    import cambium.bench as bench

    monkeypatch.setattr(bench, "run_module_cli", _inprocess_module_cli)
    bench_root = tmp_path / "baselines"
    result = run_plugin_bench(bench_root, "report")
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
    meta = json.loads((REPO_ROOT / "src/cambium/modules/example/datasets/meta.json").read_text())
    assert baseline["split_digests"] == meta["split_digests"]
    assert baseline["tests"]["count"] == len(MODULE_TESTS)
    assert set(baseline["tests"]["wall_seconds"]) == {"p50", "p90", "max"}
    # Only the module's own test nodeids enter the baseline, never unrelated
    # scenario tests collected in the same run.
    assert set(baseline["tests"]["by_nodeid"]) == set(MODULE_TESTS)


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
    baseline = json.loads((bench_root / "fixture_module" / "baseline.json").read_text())
    assert baseline["dataset_version"] == "fixture-1"
    assert baseline["dataset"]["records"] == 3
    assert baseline["metric"]["train"] == {"mean": 1.0, "std": 0.0, "count": 1}
    assert bench.main(["gate", "--bench-root", str(bench_root)]) == 0


@REQUIRES_EXAMPLE
def test_real_combined_dataset_reports_only_flagged_canaries(monkeypatch) -> None:
    import cambium.bench as bench

    monkeypatch.setattr(bench, "run_module_cli", _inprocess_module_cli)
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


def test_split_version_drift_fallback_fails_gate(tmp_path, monkeypatch, capsys) -> None:
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


def test_no_combined_fallback_re_raises_split_failure(tmp_path, monkeypatch) -> None:
    """A module with no combined file re-raises the real split failure."""
    import cambium.bench as bench

    modules_dir = _write_fixture_module(tmp_path)
    monkeypatch.setattr(bench, "MODULES_DIR", modules_dir)
    datasets_dir = modules_dir / "fixture" / "datasets"
    (datasets_dir / "train.jsonl").unlink()  # no combined file exists either

    try:
        bench.build_module_report("fixture")
    except bench.ModuleBoundaryError as exc:
        assert "no combined fallback file exists" in str(exc)
    else:  # pragma: no cover - fail-loud guard
        raise AssertionError("expected ModuleBoundaryError")


def test_dataset_stats_honors_label_field() -> None:
    import cambium.bench as bench

    records = [
        {
            "id": "a",
            "input": {"task": "t", "context": "c"},
            "expected": {"review": True, "reason": "r"},
        },
        {
            "id": "b",
            "input": {"task": "t2", "context": "c2"},
            "expected": {"review": False, "reason": "r"},
        },
    ]
    stats = bench.dataset_stats(records, "review")
    assert stats["label_true"] == 1
    assert stats["label_false"] == 1


def test_dataset_stats_distinguishes_same_split_duplicates_from_leaks() -> None:
    import cambium.bench as bench

    first = {
        "id": "a",
        "input": {"task": "same", "context": "pair"},
        "expected": {"decompose": False},
    }
    second = {**first, "id": "b"}

    assert bench.dataset_stats([first, second])["cross_split_leaks"] == 0
    assert (
        bench.dataset_stats([first, second], split_records={"train": [first], "eval": [second]})[
            "cross_split_leaks"
        ]
        == 1
    )


def test_compare_against_anchor_checks_split_digests() -> None:
    import cambium.bench as bench

    anchor = {
        "dataset_version": "fixture-1",
        "split_digests": {split: f"{split}-digest" for split in bench.SPLITS},
        "metric": {split: {"mean": 1.0} for split in bench.SPLITS},
        "tests": {"wall_seconds": {"p90": 1.0}},
        "dataset": {},
        "canaries": {"total": 1, "failed": 0},
    }
    report = json.loads(json.dumps(anchor))
    report["split_digests"]["eval"] = "changed-digest"

    regressions = bench.compare_against_anchor(report, anchor)

    assert any(field == "split_digests.eval" for field, _detail in regressions)


@pytest.mark.parametrize("value", ["nan", "inf", "-inf", "-0.1"])
def test_cli_threshold_rejects_non_finite_or_negative(value: str) -> None:
    import cambium.bench as bench

    with pytest.raises(SystemExit):
        bench._cli_parser().parse_args(["gate", "--bench-metric-delta", value])


def test_stored_threshold_rejects_non_finite_value() -> None:
    import cambium.bench as bench

    report = {
        "dataset_version": "fixture-1",
        "split_digests": {split: f"{split}-digest" for split in bench.SPLITS},
        "metric": {split: {"mean": 1.0} for split in bench.SPLITS},
        "tests": {"wall_seconds": {"p90": 1.0}},
        "dataset": {},
        "canaries": {"total": 1, "failed": 0},
    }
    anchor = json.loads(json.dumps(report))
    anchor["drift_thresholds"] = {"metric_mean_delta": float("nan")}

    regressions = bench.compare_against_anchor(report, anchor)

    assert any(field == "drift_thresholds" for field, _detail in regressions)


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


def test_module_subprocess_env_does_not_inherit_provider_credentials(tmp_path, monkeypatch) -> None:
    import cambium.bench as bench

    secret = "opaque-bench-env-probe-value-42"
    monkeypatch.setenv("CAMBIUM_PROVIDER_TEST_API_KEY", secret)

    modules_dir = _write_env_probe_module(tmp_path)

    output = bench.run_module_cli(
        "cambium.modules.envprobe",
        {"operation": "env"},
        cwd=tmp_path,
        source_root=modules_dir.parents[1],
    )

    assert "CAMBIUM_PROVIDER_TEST_API_KEY" not in output
    assert output.get("PYTHONUNBUFFERED") == "1"
    assert "PATH" in output
    assert "HOME" in output


def test_bench_failure_stderr_never_contains_provider_credential(
    tmp_path, monkeypatch, capsys
) -> None:
    import cambium.bench as bench

    secret = "opaque-bench-credential-value-9f8e"
    monkeypatch.setenv("CAMBIUM_PROVIDER_TEST_API_KEY", secret)

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
    (modules_dir / "fixture" / "__main__.py").write_text(
        textwrap.dedent(
            """
            import os
            import sys

            print("echo:", os.environ.get("CAMBIUM_PROVIDER_TEST_API_KEY"), file=sys.stderr)
            print("hardcoded:", "opaque-bench-credential-value-9f8e", file=sys.stderr)
            raise SystemExit(1)
            """
        )
    )
    monkeypatch.setattr(bench, "MODULES_DIR", modules_dir)
    bench_root = tmp_path / "baselines"

    assert bench.main(["report", "--bench-root", str(bench_root)]) == 1
    captured = capsys.readouterr()
    assert "ERROR ModuleCLIError" in captured.err
    assert secret not in captured.err


def test_bench_stderr_never_leaks_multiline_provider_credential_raw_or_escaped(
    tmp_path, monkeypatch, capsys
) -> None:
    """A credential containing an internal newline must not leak from a module.

    A module reports the credential inside a JSON error object on stdout;
    JSON serialization (and the harness's ``str()`` of the parsed error) rewrites
    the newline into the two-character ``\\n`` escape, so the raw value no longer
    matches the registered secret.  Both the exit-0 error-object path and the
    exit-1 diagnostic path must redact the escaped form as well.
    """
    import cambium.bench as bench

    secret = "opaque-provider-line-one\nline-two"
    monkeypatch.setenv("CAMBIUM_PROVIDER_TEST_API_KEY", secret)

    for exit_code in (0, 1):
        modules_dir = _write_fixture_module(
            tmp_path / f"mod-{exit_code}",
            manifest={
                "contract_version": 1,
                "module_name": "fixture_module",
                "cli_module": "cambium.modules.fixture",
                "protocol": "json-v1",
                "dataset_schema_version": 1,
            },
        )
        (modules_dir / "fixture" / "__main__.py").write_text(
            textwrap.dedent(
                """
                import json
                import sys

                print(json.dumps({"error": {"message": __SECRET__}}))
                raise SystemExit(__EXIT_CODE__)
                """
            )
            .replace("__SECRET__", repr(secret))
            .replace("__EXIT_CODE__", repr(exit_code))
        )
        monkeypatch.setattr(bench, "MODULES_DIR", modules_dir)
        bench_root = tmp_path / f"baselines-{exit_code}"

        assert bench.main(["report", "--bench-root", str(bench_root)]) == 1
        captured = capsys.readouterr().err
        assert "ERROR ModuleCLIError" in captured
        assert secret not in captured
        assert json.dumps(secret)[1:-1] not in captured
        assert repr(secret)[1:-1] not in captured


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


@SLOW
@REQUIRES_EXAMPLE
def test_scripts_use_the_neutral_module_boundary() -> None:
    import concurrent.futures

    def run_check() -> subprocess.CompletedProcess[str]:
        return subprocess.run(
            [sys.executable, "scripts/check_dataset_v1.py"],
            cwd=REPO_ROOT,
            capture_output=True,
            text=True,
            timeout=180,
        )

    def run_generate() -> subprocess.CompletedProcess[str]:
        return subprocess.run(
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

    with concurrent.futures.ThreadPoolExecutor(max_workers=2) as pool:
        check_future = pool.submit(run_check)
        generate_future = pool.submit(run_generate)
        check = check_future.result()
        generated = generate_future.result()

    assert check.returncode == 0, check.stdout + check.stderr
    assert "through the neutral CLI" in check.stdout

    assert generated.returncode == 0, generated.stdout + generated.stderr
    assert "'decompose': False" in generated.stdout


def test_standalone_cli_gate_fails_closed_without_pre_existing_anchor(
    tmp_path, monkeypatch, capsys
) -> None:
    import cambium.bench as bench

    modules_dir = _write_fixture_module(tmp_path)
    monkeypatch.setattr(bench, "MODULES_DIR", modules_dir)
    bench_root = tmp_path / "baselines"
    bench_root.mkdir()

    assert bench.main(["gate", "--bench-root", str(bench_root)]) == 1
    assert "missing pre-existing anchor" in capsys.readouterr().out
    assert not (bench_root / "fixture_module" / "baseline.json").exists()

    assert bench.main(["re-anchor", "--bench-root", str(bench_root)]) == 1
    assert "missing pre-existing anchor" in capsys.readouterr().out
    assert not (bench_root / "fixture_module" / "baseline.json").exists()


@SLOW
@REQUIRES_EXAMPLE
def test_standalone_cli_report_records_module_test_timings(tmp_path, monkeypatch) -> None:
    """The standalone CLI report must populate real wall timings, not empty ones."""
    import cambium.bench as bench

    # Only the module under assertion is measured: its timing subprocess already
    # re-scores every discovered module, so restricting discovery to ``example``
    # halves the standalone report's work without touching the wall-p90 plumbing.
    # The metric sections are scored in-process; the assertion targets the wall
    # timings, which still come from the real timing subprocess.
    monkeypatch.setattr(bench, "discover_modules", lambda: ["example"])
    monkeypatch.setattr(bench, "run_module_cli", _inprocess_module_cli)
    bench_root = tmp_path / "baselines"
    assert bench.main(["report", "--bench-root", str(bench_root)]) == 0

    baseline = json.loads((bench_root / "should_decompose" / "baseline.json").read_text())
    assert baseline["tests"]["count"] == 53
    assert set(baseline["tests"]["wall_seconds"]) == {"p50", "p90", "max"}
    assert baseline["tests"]["wall_seconds"]["p90"] > 0
    assert all(
        nodeid.startswith("src/cambium/modules/example/tests/")
        for nodeid in baseline["tests"]["by_nodeid"]
    )


def test_standalone_cli_gate_detects_wall_time_regression(tmp_path, monkeypatch, capsys) -> None:
    """The standalone gate must compare live wall p90 against the anchor.

    Empty live timings (the previous CLI behavior) would silently disable the
    wall-time comparison; this asserts an injected wall regression fails.
    """
    import cambium.bench as bench

    modules_dir = _write_fixture_module(tmp_path)
    monkeypatch.setattr(bench, "MODULES_DIR", modules_dir)
    bench_root = tmp_path / "baselines"
    nodeid = "src/cambium/modules/fixture/tests/test_fixture.py::test_always_passes"

    monkeypatch.setattr(bench, "_measure_module_timings", lambda _pkg: {nodeid: 0.01})
    assert bench.main(["report", "--bench-root", str(bench_root)]) == 0
    anchor = json.loads((bench_root / "fixture_module" / "baseline.json").read_text())
    assert anchor["tests"]["count"] == 1
    assert anchor["tests"]["wall_seconds"]["p90"] > 0

    monkeypatch.setattr(bench, "_measure_module_timings", lambda _pkg: {nodeid: 100.0})
    assert bench.main(["gate", "--bench-root", str(bench_root)]) == 1
    assert "DRIFT fixture_module: tests.wall_seconds.p90" in capsys.readouterr().out


@SLOW
@REQUIRES_EXAMPLE
def test_standalone_cli_immediate_gate_does_not_false_fail_under_load(
    tmp_path, monkeypatch
) -> None:
    """An immediate report->gate on unchanged code must not fail under load.

    The standalone gate re-measures the module tests live and compares the new
    p90 against the report's recorded p90; a ~1.6x load swing between the two
    runs (0.72 > 0.46 * 1.5) used to fail the gate. The tolerant standalone
    defaults (3x ratio plus 0.5s absolute slack) keep unchanged code passing
    while a real 100s regression still fails (covered by
    ``test_standalone_cli_gate_detects_wall_time_regression``). Both runs are
    live: the report and the gate each spawn the module-timing subprocess.
    """
    import cambium.bench as bench

    # Restrict discovery to the module under assertion: the timing subprocess
    # re-scores every discovered module, so this halves the report->gate work
    # while keeping both timing runs live for the example module. The metric
    # sections are scored in-process; the load-tolerance assertion compares the
    # live wall p90 measurements, which still come from the real subprocesses.
    monkeypatch.setattr(bench, "discover_modules", lambda: ["example"])
    monkeypatch.setattr(bench, "run_module_cli", _inprocess_module_cli)
    bench_root = tmp_path / "baselines"
    assert bench.main(["report", "--bench-root", str(bench_root)]) == 0
    assert bench.main(["gate", "--bench-root", str(bench_root)]) == 0


@SLOW
def test_standalone_timing_rejects_symlinked_tests_directory(tmp_path, monkeypatch) -> None:
    import cambium.bench as bench

    modules_dir = _write_fixture_module(tmp_path)
    monkeypatch.setattr(bench, "MODULES_DIR", modules_dir)
    target = tmp_path / "real-tests"
    target.mkdir()
    (target / "test_fixture.py").write_text("def test_ok():\n    pass\n")
    (modules_dir / "fixture" / "tests").symlink_to(target, target_is_directory=True)

    with pytest.raises(bench.ModuleBoundaryError, match="symlink"):
        bench._measure_module_timings("fixture")


def test_standalone_cli_fails_closed_when_timing_subprocess_unavailable(
    tmp_path, monkeypatch, capsys
) -> None:
    """A failed timing subprocess must fail the standalone report and gate.

    A module with a ``tests/`` directory has mandatory wall timings: when its
    timing pytest run exits nonzero, the standalone CLI must fail with a
    diagnostic instead of writing a ``tests.count == 0`` baseline and letting
    the gate skip the wall comparison. Only a genuinely empty module (no
    ``tests/`` directory) is tolerated.
    """
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
    (modules_dir / "fixture" / "tests").mkdir()
    (modules_dir / "fixture" / "tests" / "test_failing.py").write_text(
        "def test_always_fails():\n    assert False\n"
    )

    # This test targets the standalone CLI's fail-closed handling of a timing
    # subprocess that exits nonzero.  Return that failed result directly
    # instead of launching the nested pytest process: the child would load the
    # repository's full bench plugin and run unrelated module scoring before
    # reporting this fixture failure.  Other subprocesses (git and the module
    # evaluation CLI) still run normally, so report/gate setup remains covered.
    real_run = subprocess.run

    def unavailable_timing_run(args, *run_args, **run_kwargs):
        if (
            isinstance(args, list | tuple)
            and len(args) >= 3
            and tuple(args[1:3]) == ("-m", "pytest")
        ):
            return subprocess.CompletedProcess(
                args,
                1,
                stdout=b"",
                stderr=b"simulated timing subprocess unavailable",
            )
        return real_run(args, *run_args, **run_kwargs)

    monkeypatch.setattr(bench.subprocess, "run", unavailable_timing_run)
    bench_root = tmp_path / "baselines"

    assert bench.main(["report", "--bench-root", str(bench_root)]) == 1
    assert "timing run exited" in capsys.readouterr().err
    assert not (bench_root / "fixture_module" / "baseline.json").exists()

    assert bench.main(["gate", "--bench-root", str(bench_root)]) == 1
    assert "timing run exited" in capsys.readouterr().err


@pytest.mark.slow
def test_gate_fails_on_metric_drift(tmp_path, monkeypatch) -> None:
    import cambium.bench as bench

    modules_dir = _write_fixture_module(tmp_path)
    monkeypatch.setattr(bench, "MODULES_DIR", modules_dir)
    bench_root = tmp_path / "baselines"
    report = run_plugin_bench(bench_root, "report", tests=UNRELATED_TESTS)
    assert report.returncode == 0, report.stdout + report.stderr

    monkeypatch.setattr(
        bench,
        "score_examples",
        lambda scored: {"mean": 0.9, "std": 0.0, "count": len(scored)},
    )
    gate = run_plugin_bench(bench_root, "gate", tests=UNRELATED_TESTS)
    assert gate.returncode == 1, gate.stdout + gate.stderr
    assert "DRIFT metric.train.mean" in gate.stdout
    assert "1.0 -> 0.9" in gate.stdout


# ---------------------------------------------------------------------------
# dataset_version re-anchor (design §3) and drift-threshold behavior
# ---------------------------------------------------------------------------


@pytest.mark.slow
def test_gate_honors_cli_metric_delta_override(tmp_path, monkeypatch) -> None:
    """--bench-metric-delta must override the anchor thresholds on a gate run."""
    import cambium.bench as bench

    modules_dir = _write_fixture_module(tmp_path)
    monkeypatch.setattr(bench, "MODULES_DIR", modules_dir)
    bench_root = tmp_path / "baselines"
    report = run_plugin_bench(bench_root, "report", tests=UNRELATED_TESTS)
    assert report.returncode == 0, report.stdout + report.stderr

    monkeypatch.setattr(
        bench,
        "score_examples",
        lambda scored: {"mean": 0.98, "std": 0.0, "count": len(scored)},
    )
    fail = run_plugin_bench(bench_root, "gate", "--bench-metric-delta=0.01", tests=UNRELATED_TESTS)
    assert fail.returncode == 1, fail.stdout + fail.stderr
    assert "DRIFT metric.train.mean" in fail.stdout  # drop 0.02 > 0.01

    monkeypatch.setattr(
        bench,
        "score_examples",
        lambda scored: {"mean": 0.995, "std": 0.0, "count": len(scored)},
    )
    ok = run_plugin_bench(bench_root, "gate", "--bench-metric-delta=0.01", tests=UNRELATED_TESTS)
    assert ok.returncode == 0, ok.stdout + ok.stderr
    assert "DRIFT" not in ok.stdout  # drop 0.005 <= 0.01


def test_standalone_cli_missing_modules_dir_fails_closed(tmp_path, monkeypatch, capsys) -> None:
    import cambium.bench as bench

    missing = tmp_path / "no-such-modules"
    monkeypatch.setattr(bench, "MODULES_DIR", missing)
    bench_root = tmp_path / "baselines"

    assert bench.main(["report", "--bench-root", str(bench_root)]) == 1
    assert "no modules discovered" in capsys.readouterr().err
    assert not bench_root.exists()
    assert bench.main(["gate", "--bench-root", str(bench_root)]) == 1


@REQUIRES_EXAMPLE
def test_cli_report_protects_committed_baseline(tmp_path, monkeypatch) -> None:
    """CLI report without --bench-root writes to .cambium/, never the
    committed baseline, and leaves the tree clean."""
    import cambium.bench as bench

    monkeypatch.setattr(bench, "run_module_cli", _inprocess_module_cli)
    monkeypatch.setattr(bench, "_measure_module_timings", lambda _pkg: dict(FAKE_TIMINGS))
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

    assert bench.main(["report"]) == 0

    assert committed.read_bytes() == before  # committed baseline untouched
    runtime = REPO_ROOT / ".cambium" / "baselines" / "should_decompose" / "baseline.json"
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


def test_cli_full_drift_report_writes_artifact(tmp_path, monkeypatch) -> None:
    """``report --full --drift-report`` writes a drift artifact to the root."""
    import cambium.bench as bench

    modules_dir = _write_fixture_module(tmp_path)
    monkeypatch.setattr(bench, "MODULES_DIR", modules_dir)
    bench_root = tmp_path / "baselines"
    assert bench.main(["report", "--full", "--drift-report", "--bench-root", str(bench_root)]) == 0
    assert (bench_root / "fixture_module" / "baseline.json").is_file()
    artifact = json.loads((bench_root / "drift-report.json").read_text())
    assert artifact["full"] is True
    assert artifact["mode"] == "report"
    assert "fixture_module" in artifact["modules"]
    assert artifact["modules"]["fixture_module"]["dataset_version"] == "fixture-1"


def test_cli_gate_drift_report_records_regressions(tmp_path, monkeypatch) -> None:
    """``gate --drift-report`` still fails on drift and records it."""
    import cambium.bench as bench

    modules_dir = _write_fixture_module(tmp_path)
    monkeypatch.setattr(bench, "MODULES_DIR", modules_dir)
    bench_root = tmp_path / "baselines"
    assert bench.main(["report", "--bench-root", str(bench_root)]) == 0
    monkeypatch.setattr(
        bench,
        "score_examples",
        lambda scored: {"mean": 0.9, "std": 0.0, "count": len(scored)},
    )
    assert bench.main(["gate", "--drift-report", "--bench-root", str(bench_root)]) == 1
    artifact = json.loads((bench_root / "drift-report.json").read_text())
    assert os.stat(bench_root / "drift-report.json").st_mode & 0o077 == 0
    regressions = artifact["modules"]["fixture_module"]["regressions"]
    assert any(field == "metric.train.mean" for field, _detail in regressions)


def test_cli_gate_drift_report_refuses_symlinked_artifact_and_preserves_anchor(
    tmp_path, monkeypatch, capsys
) -> None:
    """A symlinked drift-report.json must not redirect the gate's drift write
    onto a baseline anchor: the gate never writes the baseline, and a rejected
    artifact write fails the run instead of clobbering the anchor."""
    import cambium.bench as bench

    modules_dir = _write_fixture_module(tmp_path)
    monkeypatch.setattr(bench, "MODULES_DIR", modules_dir)
    bench_root = tmp_path / "baselines"
    assert bench.main(["report", "--bench-root", str(bench_root)]) == 0
    anchor_path = bench_root / "fixture_module" / "baseline.json"
    anchor_before = anchor_path.read_bytes()
    artifact = bench_root / "drift-report.json"
    artifact.symlink_to(anchor_path)

    assert bench.main(["gate", "--drift-report", "--bench-root", str(bench_root)]) == 1

    captured = capsys.readouterr()
    assert "symlink" in captured.err
    assert anchor_path.read_bytes() == anchor_before  # anchor bytes unchanged
    assert artifact.is_symlink()  # artifact never materialized over the link


def test_cli_gate_drift_report_refuses_hardlinked_artifact_and_preserves_anchor(
    tmp_path, monkeypatch, capsys
) -> None:
    """A drift-report.json hard-linked to a baseline anchor must not be
    overwritten by the gate: the write fails closed on the pre-existing file,
    preserving the anchor, and the run fails."""
    import cambium.bench as bench

    modules_dir = _write_fixture_module(tmp_path)
    monkeypatch.setattr(bench, "MODULES_DIR", modules_dir)
    bench_root = tmp_path / "baselines"
    assert bench.main(["report", "--bench-root", str(bench_root)]) == 0
    anchor_path = bench_root / "fixture_module" / "baseline.json"
    anchor_before = anchor_path.read_bytes()
    anchor_ino = anchor_path.stat().st_ino
    artifact = bench_root / "drift-report.json"
    os.link(anchor_path, artifact)
    assert anchor_path.stat().st_nlink == 2

    assert bench.main(["gate", "--drift-report", "--bench-root", str(bench_root)]) == 1

    captured = capsys.readouterr()
    assert "already exists" in captured.err
    assert anchor_path.stat().st_ino == anchor_ino  # same inode, never replaced
    assert anchor_path.read_bytes() == anchor_before  # anchor bytes unchanged
    assert artifact.stat().st_ino == anchor_ino  # artifact still aliases the anchor
    assert anchor_path.stat().st_nlink == 2  # hard link never unlinked


def test_cli_gate_fails_and_preserves_anchor_on_dataset_version_change(
    tmp_path, monkeypatch, capsys
) -> None:
    """The standalone CLI shares the fail-closed re-anchor rule: a dataset_version
    change fails the gate and preserves the anchor; ``re-anchor`` records it."""
    import cambium.bench as bench

    modules_dir = _write_fixture_module(tmp_path)
    monkeypatch.setattr(bench, "MODULES_DIR", modules_dir)
    bench_root = tmp_path / "baselines"
    assert bench.main(["report", "--bench-root", str(bench_root)]) == 0
    anchor_path = bench_root / "fixture_module" / "baseline.json"
    anchor = json.loads(anchor_path.read_text())
    assert anchor["dataset_version"] == "fixture-1"
    anchor["dataset_version"] = "0.0.0"  # simulate a pre-bump anchor
    anchor_path.write_text(json.dumps(anchor))

    assert bench.main(["gate", "--bench-root", str(bench_root)]) == 1
    out = capsys.readouterr().out
    assert "DRIFT fixture_module: dataset_version" in out
    fresh = json.loads(anchor_path.read_text())
    assert fresh["dataset_version"] == "0.0.0"  # old anchor preserved

    assert bench.main(["re-anchor", "--bench-root", str(bench_root)]) == 0
    assert "re-anchored fixture_module: 0.0.0 -> fixture-1" in capsys.readouterr().out
    fresh = json.loads(anchor_path.read_text())
    assert fresh["dataset_version"] == "fixture-1"


# ---------------------------------------------------------------------------
# bench quality: fixture repo builder and report formatter (pure helpers only;
# the live provider run is never invoked in tests)
# ---------------------------------------------------------------------------


def test_quality_repo_builder_creates_main_branch_and_fixture(tmp_path) -> None:
    import cambium.bench as bench

    repo = bench._build_quality_repo(tmp_path / "quality-repo")
    assert (repo / ".git").is_dir()
    assert (repo / "calculator.py").is_file()
    assert (repo / "tests" / "test_calculator.py").is_file()

    branch = subprocess.run(
        ["git", "-C", str(repo), "symbolic-ref", "--short", "HEAD"],
        capture_output=True,
        text=True,
    )
    assert branch.returncode == 0, branch.stderr
    assert branch.stdout.strip() == "main"

    calculator = (repo / "calculator.py").read_text()
    assert "TODO" in calculator
    assert "def square" in calculator
    test = (repo / "tests" / "test_calculator.py").read_text()
    assert "assert square(3) == 9" in test


def test_quality_aggregate_requires_both_exit_code_zero_and_succeeded() -> None:
    import cambium.bench as bench

    records = [
        {"exit_code": 0, "status": "failed", "wall_s": 1.0},
        {"exit_code": 1, "status": "succeeded", "wall_s": 2.0},
        {"exit_code": 1, "status": "failed", "wall_s": 3.0},
    ]
    assert bench.quality_aggregate(records) == {
        "success_rate": "0/3",
        "pct": 0.0,
        "avg_wall_s": 2.0,
    }
