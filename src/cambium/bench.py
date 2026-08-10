"""Cambium benchmark harness: baseline report + drift gate.

A pytest plugin usable as ``-p cambium.bench`` or through the ``pytest11``
entry point (``cambium_bench``). It is inert unless ``--bench`` is passed.

Modes::

    pytest -p cambium.bench --bench=report      # measure + write baseline JSON
    pytest -p cambium.bench --bench=gate        # fail (exit 1) on drift
    pytest -p cambium.bench --bench=re-anchor   # explicitly record a new baseline

A ``gate`` run never writes a baseline: a dataset_version change is a hard
regression that fails the gate and preserves the old anchor. Recording a new
baseline is an explicit, separate operation (``report`` or ``re-anchor``).

The report writes ``src/cambium/modules/<name>/tests/baselines/baseline.json``
per the schema in ``docs/research/bench-harness-design.md``: schema_version, module,
dataset_version, git_sha, date, python, pytest; metric mean/std/count per
train/eval/canaries split; canary total/kinds/taxonomy coverage/failed;
dataset records/duplicate ids/leaks/balance; test count + p50/p90/max wall
times; and the drift thresholds the gate enforces.

Only the Python standard library plus pytest is used.
"""

from __future__ import annotations

import argparse
import asyncio
import datetime as _dt
import hashlib
import json
import math
import os
import platform
import statistics
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import pytest

from cambium.modules.base import (
    DatasetError,
    ModuleBoundaryError,
    ModuleCLIError,
    ModuleManifest,
    ModuleSplitError,
    load_jsonl,
    load_module_manifest,
    run_module_cli,
)

CANARY_TAXONOMY: tuple[str, ...] = (
    "trivially_atomic",
    "must_decompose",
    "ambiguous_calibration",
    "format_only_hack",
    "keyword_hack",
)

DEFAULT_THRESHOLDS: dict[str, Any] = {
    "metric_mean_delta": 0.05,
    "wall_p90_ratio": 1.5,
    "canary_failed_delta": 0,
    "dataset": {"duplicate_ids": 0, "cross_split_leaks": 0},
}

PACKAGE_ROOT = Path(__file__).resolve().parent


def _find_repo_root() -> Path:
    """Return the git checkout root, or the package root in a wheel install.

    A real source checkout keeps ``cambium`` under ``<root>/src/cambium``.
    A wheel has no repository; keep all resource-relative operations inside
    the installed package instead of guessing from the caller's cwd.
    """
    source = Path(__file__).resolve().parent
    for candidate in (source, *source.parents):
        if (candidate / ".git").exists() and source == candidate / "src" / "cambium":
            return candidate
    return source


REPO_ROOT = _find_repo_root()
# Prefer the checkout modules dir when a real source checkout is detected;
# otherwise fall back to the installed package resources. The wheel does not
# carry ``src/``, so ``parents[2]/"src"`` must never be derived from here.
if REPO_ROOT != PACKAGE_ROOT:
    MODULES_DIR = REPO_ROOT / "src" / "cambium" / "modules"
else:
    MODULES_DIR = PACKAGE_ROOT / "modules"
# Standalone runtime baselines live under the invocation directory so an
# installed wheel run never writes into the checkout or the package.
RUNTIME_BASELINE_DIR = Path.cwd() / ".cambium" / "baselines"

SPLITS = ("train", "eval", "canaries")

_OPTIONS_ADDED = False


def _baseline_path(
    package_name: str,
    module_name: str,
    baseline_root: Path | None = None,
) -> Path:
    """Return a baseline path under an explicit root, or the committed one.

    ``baseline_root / <module>/baseline.json`` when a root is given; without
    one, the module-local committed baseline
    (``src/cambium/modules/<name>/tests/baselines/baseline.json``).
    """
    if baseline_root is not None:
        return baseline_root / module_name / "baseline.json"
    return MODULES_DIR / package_name / "tests" / "baselines" / "baseline.json"


# --------------------------------------------------------------------------
# Pure helpers: git sha, date, percentiles
# --------------------------------------------------------------------------


def _git_sha() -> str:
    """Full SHA of the tree the run was executed in, or "" when unavailable."""
    try:
        out = subprocess.run(
            ["git", "rev-parse", "HEAD"],
            cwd=REPO_ROOT,
            capture_output=True,
            text=True,
            timeout=10,
        )
    except Exception:
        return ""
    return out.stdout.strip() if out.returncode == 0 else ""


def _utc_now_iso() -> str:
    return _dt.datetime.now(_dt.UTC).strftime("%Y-%m-%dT%H:%M:%SZ")


def percentiles(times: list[float]) -> dict[str, float]:
    """p50/p90/max over wall times via ``statistics.quantiles`` (n=100)."""
    ordered = sorted(times)
    if not ordered:
        return {"p50": 0.0, "p90": 0.0, "max": 0.0}
    qs = statistics.quantiles(ordered, n=100)
    return {
        "p50": round(qs[49], 6),
        "p90": round(qs[89], 6),
        "max": round(ordered[-1], 6),
    }


# --------------------------------------------------------------------------
# Module discovery and dataset/metric computation
# --------------------------------------------------------------------------


def discover_modules() -> list[str]:
    """Names of dataset-bearing modules with valid neutral manifests.

    Discovery is deliberately a filesystem operation.  Every candidate is
    validated before any report work starts, so a malformed sibling cannot be
    silently skipped after another module has already passed.  Fails closed
    with :class:`ModuleBoundaryError` when no module resources exist at all
    or when none of them are dataset-bearing, so report/gate never succeed
    silently on an empty or wheel-broken installation.
    """
    if not MODULES_DIR.is_dir():
        raise ModuleBoundaryError(
            f"no modules discovered: modules directory does not exist at {MODULES_DIR}"
        )
    names: list[str] = []
    for child in sorted(MODULES_DIR.iterdir(), key=lambda path: path.name):
        if not child.is_dir() or not (child / "datasets").is_dir():
            continue
        load_module_manifest(child, child.name)
        names.append(child.name)
    if not names:
        raise ModuleBoundaryError(
            f"no modules discovered: no dataset-bearing modules under {MODULES_DIR}"
        )
    return names


def _module_manifest(pkg_name: str) -> ModuleManifest:
    package_dir = MODULES_DIR / pkg_name
    return load_module_manifest(package_dir, pkg_name)


def _read_meta(path: Path) -> dict[str, Any] | None:
    try:
        return json.loads(path.read_text())
    except (OSError, json.JSONDecodeError):
        return None


@dataclass(frozen=True, slots=True)
class ScoredRecord:
    """One raw dataset record and the metric returned by the module CLI."""

    record: dict[str, Any]
    score: float


def _is_canary(record: dict[str, Any]) -> bool:
    """Return whether a raw dataset record carries the canary marker."""
    return record.get("canary", False) is True


def _validate_split_versions(
    split: str,
    records: list[dict[str, Any]],
    *,
    schema_version: int | None,
    dataset_version: object,
) -> None:
    """Reject split records whose versions do not match dataset metadata."""
    if schema_version is None and dataset_version is None:
        return
    for index, record in enumerate(records, start=1):
        record_schema = record.get("schema_version")
        record_dataset = record.get("dataset_version")
        schema_matches = (
            schema_version is None
            or (
                isinstance(record_schema, int)
                and not isinstance(record_schema, bool)
                and record_schema == schema_version
            )
        )
        dataset_matches = dataset_version is None or (
            isinstance(record_dataset, str) and record_dataset == dataset_version
        )
        if schema_matches and dataset_matches:
            continue
        raise ModuleSplitError(
            f"{split}.jsonl record {index}: version drift from meta.json "
            f"(schema_version={record_schema!r}, dataset_version={record_dataset!r})"
        )


async def _predict(manifest: ModuleManifest, records: list[dict]) -> list[ScoredRecord]:
    """Evaluate raw records through the module's neutral subprocess boundary."""
    output = run_module_cli(
        manifest.cli_module,
        {"operation": "evaluate", "records": records},
        cwd=REPO_ROOT,
        source_root=manifest.source_root,
    )
    results = output.get("results")
    if not isinstance(results, list) or len(results) != len(records):
        raise ModuleCLIError(
            f"module {manifest.package_name!r}: evaluate returned "
            f"{len(results) if isinstance(results, list) else 'no'} "
            f"results for {len(records)} records"
        )

    scored: list[ScoredRecord] = []
    for index, (record, result) in enumerate(zip(records, results, strict=True)):
        if not isinstance(result, dict):
            raise ModuleCLIError(
                f"module {manifest.package_name!r}: evaluate result {index} is not an object"
            )
        score = result.get("score")
        if isinstance(score, bool) or not isinstance(score, (int, float)):
            raise ModuleCLIError(
                f"module {manifest.package_name!r}: evaluate result {index} has no numeric score"
            )
        if not math.isfinite(score) or not 0.0 <= score <= 1.0:
            raise ModuleCLIError(
                f"module {manifest.package_name!r}: evaluate result {index} score is outside [0, 1]"
            )
        scored.append(ScoredRecord(record=record, score=float(score)))
    return scored


def score_examples(module: Any, scored: list[ScoredRecord]) -> dict[str, float]:
    """Metric mean/std/count over records scored by the neutral CLI."""
    del module  # Kept in the signature for the existing drift-test seam.
    scores = [example.score for example in scored]
    if not scores:
        return {"mean": 0.0, "std": 0.0, "count": 0}
    mean = statistics.fmean(scores)
    std = statistics.pstdev(scores) if len(scores) > 1 else 0.0
    return {"mean": round(mean, 6), "std": round(std, 6), "count": len(scores)}


def dataset_stats(records: list[dict]) -> dict[str, int]:
    """Duplicate ids, cross-split leaks, class balance over raw records."""
    ids = [r["id"] for r in records if isinstance(r.get("id"), str)]
    seen: dict[tuple[str, str], str] = {}
    leaks = 0
    for r in records:
        key = (r["input"]["task"], r["input"]["context"])
        if key in seen:
            leaks += 1
        seen[key] = r.get("id", "")
    return {
        "records": len(records),
        "duplicate_ids": len(ids) - len(set(ids)),
        "cross_split_leaks": leaks,
        "decompose_true": sum(1 for r in records if r["expected"]["decompose"] is True),
        "decompose_false": sum(1 for r in records if r["expected"]["decompose"] is False),
        "canaries": sum(1 for r in records if _is_canary(r)),
    }


def canary_stats(raw_canaries: list[dict], canary_scores: list[float]) -> dict[str, Any]:
    """Canary counts, taxonomy kinds present/coverage, failures.

    A canary "fails" when the module's decision metric is not perfect on it
    (the anti-reward-hacking pass condition the current metric can check).
    """
    kinds = [
        r["canary_info"]["kind"]
        for r in raw_canaries
        if isinstance(r.get("canary_info"), dict)
        and isinstance(r["canary_info"].get("kind"), str)
    ]
    present = sorted(set(kinds))
    coverage = round(
        len([kind for kind in CANARY_TAXONOMY if kind in present]) / len(CANARY_TAXONOMY),
        4,
    )
    return {
        "total": len(raw_canaries),
        "kinds_present": present,
        "taxonomy_coverage": coverage,
        "failed": sum(1 for score in canary_scores if score < 1.0),
    }


def _compute_split_digests(
    datasets_dir: Path, meta: dict[str, Any] | None
) -> dict[str, str]:
    """SHA-256 map of the exact split JSONL bytes, meta.json values as fallback."""
    recorded = meta.get("split_digests") if isinstance(meta, dict) else None
    digests: dict[str, str] = {}
    for split in SPLITS:
        path = datasets_dir / f"{split}.jsonl"
        if path.is_file():
            digests[split] = hashlib.sha256(path.read_bytes()).hexdigest()
        elif (
            isinstance(recorded, dict)
            and isinstance(recorded.get(split), str)
            and len(recorded[split]) == 64
        ):
            digests[split] = recorded[split]
    return digests


def build_module_report(pkg_name: str) -> dict[str, Any]:
    """Metric/canaries/dataset sections of a module's baseline.

    Reads the three-split datasets (train/eval/canaries.jsonl) through the
    module's neutral JSON CLI; if a split is unavailable or explicitly
    schema-invalid, it falls back to the combined ``*_pairs.jsonl`` file and
    marks the split metric fields null with a ``note``. Other CLI failures
    propagate instead of silently shrinking the dataset. The concrete package
    is never imported by this harness.
    """
    manifest = _module_manifest(pkg_name)
    datasets_dir = MODULES_DIR / pkg_name / "datasets"
    meta = _read_meta(datasets_dir / "meta.json")
    dataset_version = meta.get("dataset_version") if meta else None
    meta_schema = meta.get("schema_version") if meta else None
    if meta_schema is not None and (
        isinstance(meta_schema, bool)
        or not isinstance(meta_schema, int)
        or meta_schema != manifest.dataset_schema_version
    ):
        raise ModuleBoundaryError(
            f"module {pkg_name!r}: dataset schema_version {meta_schema!r} does not "
            f"match manifest dataset_schema_version {manifest.dataset_schema_version}"
        )

    note: str | None = None
    combined = False
    raw: dict[str, list[dict]] = {}
    scored: dict[str, list[ScoredRecord]] = {}
    metric: dict[str, Any] = {}
    canary_scores: list[float] = []
    try:
        for split in SPLITS:
            path = datasets_dir / f"{split}.jsonl"
            raw[split] = load_jsonl(path)
            _validate_split_versions(
                split,
                raw[split],
                schema_version=meta_schema,
                dataset_version=dataset_version,
            )
        for split in SPLITS:
            scored[split] = asyncio.run(_predict(manifest, raw[split]))
            metric[split] = score_examples(manifest, scored[split])
            if split == "canaries":
                canary_scores = [
                    example.score
                    for example in scored[split]
                    if _is_canary(example.record)
                ]
    except (DatasetError, ModuleSplitError) as exc:
        combined = True
        note = (
            f"three-split dataset unavailable ({exc}); fell back to the combined "
            "file and split metrics are null"
        )
        pairs = datasets_dir / f"{pkg_name}_pairs.jsonl"
        if not pairs.exists():
            pairs = datasets_dir / "example_pairs.jsonl"
        raw = {"combined": load_jsonl(pairs)}
        scored = {}
        metric = {split: None for split in SPLITS}
        canary_scores = []
    if combined:
        scored["combined"] = asyncio.run(_predict(manifest, raw["combined"]))
        metric["combined"] = score_examples(manifest, scored["combined"])
        canary_scores = [
            example.score
            for example in scored["combined"]
            if _is_canary(example.record)
        ]

    records = [r for split in SPLITS for r in raw.get(split, [])] or raw.get("combined", [])
    canary_source = raw["combined"] if combined else raw.get("canaries", [])
    canary_records = [record for record in canary_source if _is_canary(record)]
    report: dict[str, Any] = {
        "module": manifest.module_name,
        "dataset_version": dataset_version,
        "split_digests": _compute_split_digests(datasets_dir, meta),
        "metric": metric,
        "canaries": canary_stats(canary_records, canary_scores),
        "dataset": dataset_stats(records),
    }
    if note:
        report["note"] = note
    return report


def _assemble_baseline(
    body: dict[str, Any],
    timings: dict[str, float],
    thresholds: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Wrap a module body into the full baseline schema."""
    baseline: dict[str, Any] = {
        "schema_version": 1,
        "module": body["module"],
        "dataset_version": body["dataset_version"],
        "split_digests": dict(body["split_digests"]),
        "git_sha": _git_sha(),
        "date": _utc_now_iso(),
        "python": platform.python_version(),
        "pytest": pytest.__version__,
        "metric": body["metric"],
        "canaries": body["canaries"],
        "dataset": body["dataset"],
        "tests": {
            "count": len(timings),
            "wall_seconds": percentiles(list(timings.values())),
            "by_nodeid": {k: round(v, 6) for k, v in sorted(timings.items())},
        },
        "drift_thresholds": dict(thresholds or DEFAULT_THRESHOLDS),
    }
    if body.get("note"):
        baseline["note"] = body["note"]
    return baseline


def compare_against_anchor(
    report: dict[str, Any],
    anchor: dict[str, Any],
    thresholds: dict[str, Any] | None = None,
) -> list[tuple[str, str]]:
    """Return ``[(field, detail)]`` regressions of ``report`` vs ``anchor``.

    Returns an empty list when there is no drift. It never returns a re-anchor
    sentinel: a stale anchor — one whose ``dataset_version`` differs from the
    report's — is itself a ``dataset_version`` regression, so callers fail
    closed and preserve the anchor. Recording a new baseline is an explicit,
    separate operation (``report`` or ``re-anchor`` mode), never a gate side
    effect. A report with unavailable split metrics also fails closed.

    Threshold precedence: run-level ``thresholds`` (e.g. from
    ``--bench-metric-delta``) override the anchor's stored
    ``drift_thresholds``, which override the defaults.

    Metric means only fail when they fall by more than ``metric_mean_delta``;
    wall p90 fails when it exceeds ``anchor * wall_p90_ratio``; duplicate ids
    or cross-split leaks of any size and missing canaries always fail; a
    canary failure is a regression when it exceeds the anchor's count by more
    than ``canary_failed_delta``; unavailable split metrics are regressions.
    """
    report_metrics = report.get("metric") or {}
    missing_split_regressions = [
        (
            f"metric.{split}",
            "split metric unavailable; legacy combined fallback was scored",
        )
        for split in SPLITS
        if not isinstance(report_metrics.get(split), dict)
    ]
    if report.get("dataset_version") != anchor.get("dataset_version"):
        version_regression = (
            "dataset_version",
            f"dataset_version changed: anchor {anchor.get('dataset_version')!r} != "
            f"report {report.get('dataset_version')!r}; run report/re-anchor mode "
            "to record a new baseline",
        )
        if missing_split_regressions:
            return missing_split_regressions + [version_regression]
        return [version_regression]

    merged = dict(DEFAULT_THRESHOLDS)
    merged.update(anchor.get("drift_thresholds") or {})
    if thresholds:
        merged.update(thresholds)
    regressions: list[tuple[str, str]] = missing_split_regressions

    metric_delta = merged["metric_mean_delta"]
    for split in SPLITS + ("combined",):
        r_metric = (report.get("metric") or {}).get(split)
        a_metric = (anchor.get("metric") or {}).get(split)
        if not isinstance(r_metric, dict) or not isinstance(a_metric, dict):
            continue
        drop = a_metric.get("mean", 0.0) - r_metric.get("mean", 0.0)
        if drop > metric_delta:
            regressions.append(
                (
                    f"metric.{split}.mean",
                    f"{a_metric['mean']} -> {r_metric['mean']} "
                    f"(drop {drop:.4f} > {metric_delta})",
                )
            )

    r_wall = (report.get("tests") or {}).get("wall_seconds") or {}
    a_wall = (anchor.get("tests") or {}).get("wall_seconds") or {}
    if a_wall.get("p90") and r_wall.get("p90"):
        wall_ratio = merged["wall_p90_ratio"]
        if r_wall["p90"] > a_wall["p90"] * wall_ratio:
            regressions.append(
                (
                    "tests.wall_seconds.p90",
                    f"{r_wall['p90']} > {a_wall['p90']} * {wall_ratio}",
                )
            )

    dataset = report.get("dataset") or {}
    dataset_thresholds = merged.get("dataset") or {}
    if dataset.get("duplicate_ids", 0) > dataset_thresholds.get("duplicate_ids", 0):
        regressions.append(("dataset.duplicate_ids", str(dataset["duplicate_ids"])))
    if dataset.get("cross_split_leaks", 0) > dataset_thresholds.get("cross_split_leaks", 0):
        regressions.append(("dataset.cross_split_leaks", str(dataset["cross_split_leaks"])))

    r_canaries = report.get("canaries") or {}
    a_canaries = anchor.get("canaries") or {}
    canary_delta = merged["canary_failed_delta"]
    if r_canaries.get("total", 0) == 0:
        regressions.append(("canaries.total", "dataset has no canaries"))
    if r_canaries.get("failed", 0) > a_canaries.get("failed", 0) + canary_delta:
        regressions.append(
            (
                "canaries.failed",
                f"{a_canaries.get('failed', 0)} + {canary_delta} -> "
                f"{r_canaries.get('failed', 0)}",
            )
        )

    return regressions


# --------------------------------------------------------------------------
# pytest plugin
# --------------------------------------------------------------------------


class BenchPlugin:
    """Registered when ``--bench`` is set; collects timings, writes/checks."""

    def __init__(self, config: pytest.Config, thresholds: dict[str, Any] | None = None) -> None:
        self.mode: str = config.getoption("bench")
        bench_root = config.getoption("bench_root")
        self.root = Path(bench_root) if bench_root else None
        self.thresholds = dict(DEFAULT_THRESHOLDS)
        if thresholds:
            self.thresholds.update(thresholds)
        self.times: dict[str, float] = {}
        self.item_paths: dict[str, Path] = {}
        self.module_reports: dict[str, dict[str, Any]] = {}
        self.regressions: dict[str, list[tuple[str, str]]] = {}
        self.reanchored: dict[str, str] = {}
        self.error: str | None = None

    @pytest.hookimpl(tryfirst=True)
    def pytest_runtest_makereport(self, item: Any, call: Any) -> None:
        if call.when == "call":
            self.times[item.nodeid] = call.duration
            raw_path = getattr(item, "path", None)
            if raw_path is None:
                raw_path = item.fspath
            if raw_path is not None:
                self.item_paths[item.nodeid] = Path(str(raw_path)).resolve()

    def _module_timings(self, pkg_name: str) -> dict[str, float]:
        """Timings whose collected test file is under one module's ``tests/``."""
        module_tests_dir = (MODULES_DIR / pkg_name / "tests").resolve()
        return {
            nodeid: duration
            for nodeid, duration in self.times.items()
            if (path := self.item_paths.get(nodeid)) is not None
            and path.is_relative_to(module_tests_dir)
        }

    def pytest_sessionfinish(self, session: Any, exitstatus: Any) -> None:
        if exitstatus != 0:
            return  # never anchor a baseline on a red run
        try:
            package_names = discover_modules()
            pending = []
            for pkg_name in package_names:
                pending.append(
                    (
                        pkg_name,
                        _assemble_baseline(
                            build_module_report(pkg_name),
                            self._module_timings(pkg_name),
                            self.thresholds,
                        ),
                    )
                )
        except (ModuleBoundaryError, DatasetError) as exc:
            self.error = f"{type(exc).__name__}: {exc}"
            session.exitstatus = 1
            return

        for pkg_name, report in pending:
            self.module_reports[report["module"]] = report
            anchor_path = _baseline_path(pkg_name, report["module"], self.root)
            if self.mode == "report":
                _write_baseline(report, anchor_path)
            elif self.mode == "re-anchor":
                if not anchor_path.exists():
                    self.regressions[report["module"]] = [
                        (
                            "anchor",
                            f"missing pre-existing anchor at {anchor_path}; "
                            f"run --bench=report to create one",
                        )
                    ]
                    session.exitstatus = 1
                else:
                    anchor = json.loads(anchor_path.read_text())
                    self.reanchored[report["module"]] = (
                        f"{anchor.get('dataset_version')} -> {report['dataset_version']}"
                    )
                    _write_baseline(report, anchor_path)
            elif not anchor_path.exists():
                self.regressions[report["module"]] = [
                    ("anchor", f"missing pre-existing anchor at {anchor_path}")
                ]
                session.exitstatus = 1
            else:
                anchor = json.loads(anchor_path.read_text())
                regressions = compare_against_anchor(report, anchor, self.thresholds)
                if regressions:
                    self.regressions[report["module"]] = regressions
                    session.exitstatus = 1

    def pytest_terminal_summary(
        self, terminalreporter: Any, exitstatus: Any, config: Any
    ) -> None:
        if self.error:
            terminalreporter.section("cambium bench", yellow=True)
            terminalreporter.write_line(f"ERROR {self.error}", red=True)
        if not self.module_reports:
            return
        terminalreporter.section("cambium bench", yellow=True)
        for module, report in sorted(self.module_reports.items()):
            metric = report["metric"]
            canaries = report["canaries"]
            terminalreporter.write_line(
                f"{module}: dataset records={report['dataset']['records']} "
                f"metric train={_fmt(metric.get('train'))} "
                f"eval={_fmt(metric.get('eval'))} "
                f"canaries={_fmt(metric.get('canaries'))} "
                f"canary total={canaries['total']} "
                f"taxonomy_coverage={canaries['taxonomy_coverage']} "
                f"failed={canaries['failed']}"
            )
            for field, detail in self.regressions.get(module, []):
                terminalreporter.write_line(f"  DRIFT {field}: {detail}", red=True)
        for module, detail in sorted(self.reanchored.items()):
            terminalreporter.write_line(f"  RE-ANCHOR {module}: {detail}")


def _write_baseline(report: dict[str, Any], path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(report, indent=2) + "\n")


def _fmt(stats: Any) -> str:
    if not isinstance(stats, dict):
        return "n/a"
    return f"{stats['mean']:.4f}"


def pytest_addoption(parser: Any) -> None:
    global _OPTIONS_ADDED
    if _OPTIONS_ADDED:
        return  # the entry point and -p may both register this module
    _OPTIONS_ADDED = True
    group = parser.getgroup("cambium-bench")
    group.addoption(
        "--bench",
        choices=("report", "gate", "re-anchor"),
        default=None,
        help="run cambium bench: report writes the baseline, gate fails on drift "
        "(and preserves the anchor on dataset_version change), re-anchor records "
        "a new baseline over an existing anchor",
    )
    group.addoption(
        "--bench-root",
        default=None,
        metavar="DIR",
        help="baseline root override (default: next to each module)",
    )
    group.addoption(
        "--bench-metric-delta",
        type=float,
        default=None,
        help="override the metric mean drop drift threshold",
    )
    group.addoption(
        "--bench-wall-ratio",
        type=float,
        default=None,
        help="override the wall p90 ratio drift threshold",
    )


def pytest_configure(config: Any) -> None:
    if config.getoption("bench") is None:
        return
    if config.pluginmanager.hasplugin("cambium-bench"):
        return  # the entry point and -p may both register this module
    thresholds = dict(DEFAULT_THRESHOLDS)
    if config.getoption("bench_metric_delta") is not None:
        thresholds["metric_mean_delta"] = config.getoption("bench_metric_delta")
    if config.getoption("bench_wall_ratio") is not None:
        thresholds["wall_p90_ratio"] = config.getoption("bench_wall_ratio")
    config.pluginmanager.register(BenchPlugin(config, thresholds), "cambium-bench")


def _write_drift_report(
    root: Path,
    module_reports: dict[str, dict[str, Any]],
    regressions: dict[str, list[tuple[str, str]]],
    *,
    mode: str,
    full: bool,
) -> Path:
    """Write a drift artifact summarizing each module against its anchor.

    The artifact must never alias a baseline anchor, so ``drift-report.json``
    is not allowed to pre-exist: a symlink could redirect the write onto an
    anchor, and a hard link would let a truncating write clobber the anchor in
    place, violating "gate never writes the baseline". The path is rejected if
    it exists and is then created with ``O_CREAT|O_EXCL|O_NOFOLLOW``, so a
    pre-existing file (regular or hard-linked) or symlink fails the run
    atomically instead of being opened or followed. The file is created mode
    ``0o600`` so the drift artifact stays private.
    """
    artifact: dict[str, Any] = {
        "schema_version": 1,
        "date": _utc_now_iso(),
        "python": platform.python_version(),
        "mode": mode,
        "full": full,
        "modules": {
            module: {
                "dataset_version": report["dataset_version"],
                "metric": report["metric"],
                "canaries": report["canaries"],
                "regressions": regressions.get(module, []),
            }
            for module, report in sorted(module_reports.items())
        },
    }
    path = root / "drift-report.json"
    if os.path.lexists(path):
        kind = "a symlink" if path.is_symlink() else "a file"
        raise OSError(
            f"refusing to write drift report: {path} already exists ({kind}); "
            "remove it and rerun to allow the artifact to be written"
        )
    path.parent.mkdir(parents=True, exist_ok=True)
    fd = os.open(
        path,
        os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW,
        0o600,
    )
    with os.fdopen(fd, "w", encoding="utf-8") as handle:
        handle.write(json.dumps(artifact, indent=2) + "\n")
    return path


def _cli_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="python -m cambium.bench",
        description="Run the Cambium benchmark report, drift gate, or explicit re-anchor.",
    )
    parser.add_argument(
        "mode",
        nargs="?",
        default="report",
        metavar="{report,gate,re-anchor}",
    )
    parser.add_argument(
        "--full",
        action="store_true",
        help="full (nightly/release) run; include the drift artifact sections",
    )
    parser.add_argument(
        "--drift-report",
        action="store_true",
        help="write a drift artifact to the baseline root (default: .cambium/baselines/)",
    )
    parser.add_argument(
        "--bench-root",
        type=Path,
        default=None,
        metavar="DIR",
        help="baseline root override (default: .cambium/baselines/, gitignored)",
    )
    parser.add_argument(
        "--bench-metric-delta",
        type=float,
        default=None,
        help="override the metric mean drop drift threshold",
    )
    parser.add_argument(
        "--bench-wall-ratio",
        type=float,
        default=None,
        help="override the wall p90 ratio drift threshold",
    )
    return parser


def main(argv: list[str] | None = None) -> int:
    """CLI form: ``python -m cambium.bench report|gate|re-anchor``."""
    args = _cli_parser().parse_args(sys.argv[1:] if argv is None else argv)
    mode = args.mode
    if mode not in ("report", "gate", "re-anchor"):
        print("usage: python -m cambium.bench report|gate|re-anchor", file=sys.stderr)
        return 2
    thresholds = dict(DEFAULT_THRESHOLDS)
    if args.bench_metric_delta is not None:
        thresholds["metric_mean_delta"] = args.bench_metric_delta
    if args.bench_wall_ratio is not None:
        thresholds["wall_p90_ratio"] = args.bench_wall_ratio
    failures = 0
    root = args.bench_root or RUNTIME_BASELINE_DIR
    try:
        pending = []
        for pkg_name in discover_modules():
            pending.append(
                (
                    pkg_name,
                    _assemble_baseline(build_module_report(pkg_name), {}, thresholds),
                )
            )
    except (ModuleBoundaryError, DatasetError) as exc:
        print(f"cambium bench: ERROR {type(exc).__name__}: {exc}", file=sys.stderr)
        return 1

    module_reports: dict[str, dict[str, Any]] = {}
    drift: dict[str, list[tuple[str, str]]] = {}
    for pkg_name, report in pending:
        module_reports[report["module"]] = report
        path = _baseline_path(pkg_name, report["module"], root)
        if mode == "report":
            _write_baseline(report, path)
            print(f"cambium bench: wrote {path}")
        elif mode == "re-anchor":
            if not path.exists():
                failures += 1
                print(
                    f"cambium bench: missing pre-existing anchor for {report['module']}: "
                    f"{path}; run `report` to create one"
                )
            else:
                anchor = json.loads(path.read_text())
                _write_baseline(report, path)
                print(
                    f"cambium bench: re-anchored {report['module']}: "
                    f"{anchor.get('dataset_version')} -> {report['dataset_version']}"
                )
        elif not path.exists():
            failures += 1
            print(f"cambium bench: missing pre-existing anchor for {report['module']}: {path}")
        else:
            anchor = json.loads(path.read_text())
            module_drift = compare_against_anchor(report, anchor, thresholds)
            if module_drift:
                failures += 1
                drift[report["module"]] = module_drift
                for field, detail in module_drift:
                    print(f"cambium bench: DRIFT {report['module']}: {field}: {detail}")
            else:
                print(f"cambium bench: gate passed: {report['module']}")
    if args.drift_report:
        try:
            written = _write_drift_report(
                root,
                module_reports,
                drift,
                mode=mode,
                full=args.full,
            )
        except OSError as exc:
            print(f"cambium bench: ERROR {type(exc).__name__}: {exc}", file=sys.stderr)
            failures += 1
        else:
            print(f"cambium bench: wrote drift report {written}")
    return 1 if failures else 0


if __name__ == "__main__":
    raise SystemExit(main())
