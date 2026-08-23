"""Cambium benchmark harness: baseline report + drift gate.

A pytest plugin usable as ``-p cambium.bench`` or through the ``pytest11``
entry point (``cambium_bench``). It is inert unless ``--bench`` is passed.

Modes::

    pytest -p cambium.bench --bench=report      # measure + write baseline JSON
    pytest -p cambium.bench --bench=gate        # fail (exit 1) on drift
    pytest -p cambium.bench --bench=re-anchor   # explicitly record a new baseline

The standalone CLI additionally supports ``quality``
(``python -m cambium.bench quality``), which measures task SUCCESS RATE by
running a fixed set of known-good coding prompts against a scratch git repo
(``<cwd>/.cambium/quality-repo``) and reports per-prompt outcomes plus an
aggregate success-rate line.  When no provider credentials are configured it
skips cleanly and exits 0.

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

Wall-time gate design: the pytest plugin compares the current session's wall
p90 against a fixed committed anchor with the strict 1.5x ratio. The
standalone CLI (``python -m cambium.bench``) has no session report objects, so
it re-measures the module's tests in a throwaway pytest subprocess and compares
two live runs. To keep that live-vs-live comparison robust to legitimate load
variation between the report and gate invocations (a 1.6x swing was observed),
the standalone path defaults to a 3x ratio plus a 0.5s absolute slack instead
of 1.5x. The slack is additive: the gate fails only when the live p90 exceeds
``anchor_p90 * ratio + slack``, which still flags a real regression (e.g. a
test that sleeps 100s) while not false-failing unchanged code under load. The
plugin path behavior is unchanged, and ``--bench-wall-ratio`` overrides the
standalone default.

Credential hygiene: every child process spawned by the harness — the module
evaluation CLI, the timing subprocess, and, transitively, the module's own CLI
tests — gets a scrubbed environment via :func:`cambium.auth.scrub_environment`;
``os.environ`` is never copied wholesale into a subprocess.
"""

from __future__ import annotations

import argparse
import asyncio
import datetime as _dt
import hashlib
import inspect
import json
import math
import os
import platform
import shutil
import statistics
import subprocess
import sys
import tempfile
import textwrap
import time
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Any, cast

import pytest

from cambium.auth import AuthError, scrub_environment
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
    "wall_p90_abs_slack": 0.0,
    "canary_failed_delta": 0,
    "dataset": {"duplicate_ids": 0, "cross_split_leaks": 0},
}
_THRESHOLD_FIELDS = (
    "metric_mean_delta",
    "wall_p90_ratio",
    "wall_p90_abs_slack",
    "canary_failed_delta",
)
_DATASET_THRESHOLD_FIELDS = ("duplicate_ids", "cross_split_leaks")


def _finite_nonnegative_float(value: str) -> float:
    """Parse a finite, non-negative command-line threshold."""
    try:
        parsed = float(value)
    except (TypeError, ValueError) as exc:
        raise argparse.ArgumentTypeError("must be a number") from exc
    if not math.isfinite(parsed) or parsed < 0:
        raise argparse.ArgumentTypeError("must be finite and non-negative")
    return parsed


def _validate_thresholds(value: object, label: str = "drift_thresholds") -> dict[str, Any]:
    """Validate the numeric values used by a drift threshold mapping."""
    if not isinstance(value, Mapping):
        raise ValueError(f"{label} must be an object")
    validated = dict(value)
    for field in _THRESHOLD_FIELDS:
        if field not in value:
            continue
        threshold = value[field]
        if isinstance(threshold, bool) or not isinstance(threshold, int | float):
            raise ValueError(f"{label}.{field} must be numeric")
        try:
            finite = math.isfinite(float(threshold))
        except (OverflowError, TypeError, ValueError):
            finite = False
        if not finite or threshold < 0:
            raise ValueError(f"{label}.{field} must be finite and non-negative")
    if "dataset" in value:
        dataset = value["dataset"]
        if not isinstance(dataset, Mapping):
            raise ValueError(f"{label}.dataset must be an object")
        validated["dataset"] = dict(dataset)
        for field in _DATASET_THRESHOLD_FIELDS:
            if field not in dataset:
                continue
            threshold = dataset[field]
            if isinstance(threshold, bool) or not isinstance(threshold, int | float):
                raise ValueError(f"{label}.dataset.{field} must be numeric")
            try:
                finite = math.isfinite(float(threshold))
            except (OverflowError, TypeError, ValueError):
                finite = False
            if not finite or threshold < 0:
                raise ValueError(f"{label}.dataset.{field} must be finite and non-negative")
    return validated


def _merged_thresholds(*overrides: object | None) -> dict[str, Any]:
    """Return defaults plus validated threshold overrides."""
    merged = dict(DEFAULT_THRESHOLDS)
    merged["dataset"] = dict(DEFAULT_THRESHOLDS["dataset"])
    for index, override in enumerate(overrides):
        if override is None:
            continue
        validated = _validate_thresholds(override, f"drift_thresholds[{index}]")
        for field in _THRESHOLD_FIELDS:
            if field in validated:
                merged[field] = validated[field]
        dataset = validated.get("dataset")
        if isinstance(dataset, Mapping):
            merged["dataset"].update(dataset)
    return merged


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
# The quality benchmark owns its scratch repository under the same runtime
# root; the repo is rebuilt before every prompt so each prompt measures the
# identical pristine fixture.
QUALITY_REPO_DIR = Path.cwd() / ".cambium" / "quality-repo"

SPLITS = ("train", "eval", "canaries")


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


def _git_sha() -> str | None:
    """Full SHA of the current HEAD, or ``None`` when git provenance is unavailable.

    ``None`` (not an empty string) marks unavailable provenance so a missing
    SHA is distinguishable from a real one; a ``null`` baseline ``git_sha``
    records that the run could not capture tree identity. Provenance-dependent
    callers must handle ``None``.
    """
    try:
        out = subprocess.run(
            ["git", "rev-parse", "HEAD"],
            cwd=REPO_ROOT,
            capture_output=True,
            text=True,
            timeout=10,
        )
    except (OSError, subprocess.SubprocessError):
        return None
    return out.stdout.strip() if out.returncode == 0 else None


def _utc_now_iso() -> str:
    return _dt.datetime.now(_dt.UTC).strftime("%Y-%m-%dT%H:%M:%SZ")


def percentiles(times: list[float]) -> dict[str, float]:
    """Return p50/p90/max over wall times, including a singleton sample."""
    ordered = sorted(times)
    if not ordered:
        return {"p50": 0.0, "p90": 0.0, "max": 0.0}
    if len(ordered) == 1:
        value = round(ordered[0], 6)
        return {"p50": value, "p90": value, "max": value}
    qs = statistics.quantiles(ordered, n=100)
    return {
        "p50": round(qs[49], 6),
        "p90": round(qs[89], 6),
        "max": round(ordered[-1], 6),
    }


# --------------------------------------------------------------------------
# Quality benchmark: task success rate over a fixed fixture repo
# --------------------------------------------------------------------------

# One tiny self-contained fixture: a calculator module whose ``square()`` is a
# TODO, and a test suite that fails until it is implemented. The fixture is a
# minimal real task the provider agent must read, fix, and verify.
QUALITY_CALCULATOR = textwrap.dedent(
    """\
    \"\"\"A tiny calculator used by the cambium bench quality fixture.\"\"\"


    def square(x: int) -> int:
        \"\"\"Return x * x.

        TODO: implement square so tests/test_calculator.py passes.
        \"\"\"
        raise NotImplementedError("square() is not implemented")
    """
)

QUALITY_TEST = textwrap.dedent(
    """\
    from calculator import square


    def test_square_positive() -> None:
        assert square(3) == 9


    def test_square_negative() -> None:
        assert square(-4) == 16


    def test_square_zero() -> None:
        assert square(0) == 0
    """
)

# A small FIXED set of known-good prompts, each asking for the same one-line
# fix so every prompt exercises the full read-fix-verify loop independently
# (the scratch repo is rebuilt pristine before each prompt).
QUALITY_PROMPTS: tuple[tuple[str, str], ...] = (
    (
        "quality-square-1",
        "tests/test_calculator.py is failing. Read the failing test, implement "
        "square() in calculator.py, and verify by running the test suite.",
    ),
    (
        "quality-square-2",
        "Fix the failing test suite in this repository: implement "
        "calculator.square() so that all tests in tests/test_calculator.py pass, "
        "then run pytest to confirm.",
    ),
    (
        "quality-square-3",
        "Complete the TODO in calculator.py: implement square(x) to return x*x. "
        "Make sure tests/test_calculator.py passes by running the tests.",
    ),
)


def _build_quality_repo(root: Path) -> Path:
    """Create a pristine git repo at ``root`` holding the quality fixture.

    The repo is initialized with a ``main`` branch and seeded user identity,
    and contains one commit with ``calculator.py`` plus the failing
    ``tests/test_calculator.py``. A pre-existing directory at ``root`` is
    removed first so every prompt in a quality run measures the identical
    pristine fixture regardless of what an earlier prompt committed.
    """
    if root.exists():
        shutil.rmtree(root)
    root.mkdir(parents=True)
    (root / "tests").mkdir()
    (root / "calculator.py").write_text(QUALITY_CALCULATOR)
    (root / "tests" / "test_calculator.py").write_text(QUALITY_TEST)
    for command in (
        ("init", "-b", "main"),
        ("config", "user.name", "Cambium Quality Bench"),
        ("config", "user.email", "cambium-bench@localhost"),
        ("add", "."),
        ("commit", "-m", "quality fixture: failing square()"),
    ):
        result = subprocess.run(
            ["git", "-C", str(root), *command],
            capture_output=True,
            text=True,
            env=scrub_environment(),
            timeout=60,
        )
        if result.returncode != 0:
            detail = (result.stderr or result.stdout or "no diagnostic output").strip()
            raise ModuleBoundaryError(
                f"quality fixture: `git {' '.join(command)}` exited "
                f"{result.returncode}: {detail[:300]}"
            )
    return root


def _is_provider_selection_error(exc: Exception) -> bool:
    """True when a oneshot resolution error means no provider is usable.

    ``run_oneshot`` surfaces a missing or disabled provider as a ``ValueError``
    whose message names the selection or credential failure. ``AuthError`` is
    credential-domain by construction.
    """
    if isinstance(exc, AuthError):
        return True
    return isinstance(exc, ValueError) and any(
        marker in str(exc)
        for marker in (
            "provider selection",
            "provider credential",
            "auto mode requires at least one enabled provider",
        )
    )


def _run_one_quality_prompt(repo: Path, task_id: str, prompt: str) -> dict[str, Any] | None:
    """Run one prompt against ``repo``; None when no provider is configured."""
    from cambium.oneshot import OneShotConfig, run_oneshot

    config = OneShotConfig(prompt=prompt, repo=str(repo), task_id=task_id)
    started = time.monotonic()
    try:
        value = run_oneshot(config)
        result = asyncio.run(value) if inspect.isawaitable(value) else value
    except (AuthError, OSError, ValueError) as exc:
        if _is_provider_selection_error(exc):
            return None
        raise
    wall_s = time.monotonic() - started
    first = result.results[0] if getattr(result, "results", None) else None
    if first is not None:
        exit_code = first.exit_code
        status = first.status
        merge_sha = first.merge_sha
    else:
        exit_code = getattr(result, "exit_code", 1)
        status = "failed"
        merge_sha = None
    return {
        "task_id": task_id,
        "prompt": prompt,
        "exit_code": exit_code,
        "status": status,
        "merge_sha": merge_sha,
        "wall_s": wall_s,
    }


def _run_quality_prompts(root: Path) -> list[dict[str, Any]] | None:
    """Run every fixed prompt on a fresh fixture repo; None when provider missing."""
    records: list[dict[str, Any]] = []
    for task_id, prompt in QUALITY_PROMPTS:
        repo = _build_quality_repo(root)
        record = _run_one_quality_prompt(repo, task_id, prompt)
        if record is None:
            return None
        records.append(record)
    return records


def quality_aggregate(records: list[dict[str, Any]]) -> dict[str, Any]:
    """Success/total, success percentage, and mean wall seconds over a run.

    A prompt counts as a success only when its exit code is 0 AND its status
    is ``"succeeded"``, mirroring the supervisor's task verdict.
    """
    total = len(records)
    successes = sum(
        1
        for record in records
        if record.get("exit_code") == 0 and record.get("status") == "succeeded"
    )
    pct = round(successes / total * 100, 1) if total else 0.0
    walls = [float(record.get("wall_s", 0.0)) for record in records]
    avg_wall_s = round(sum(walls) / len(walls), 1) if walls else 0.0
    return {
        "success_rate": f"{successes}/{total}",
        "pct": pct,
        "avg_wall_s": avg_wall_s,
    }


def format_quality_report(records: list[dict[str, Any]]) -> str:
    """Deterministic text report: one line per prompt plus the aggregate line."""
    lines = []
    for record in records:
        merge_sha = record.get("merge_sha") or "-"
        lines.append(
            f"cambium bench quality: task {record['task_id']}: "
            f"exit_code={record['exit_code']} status={record['status']} "
            f"merge_sha={merge_sha} wall_s={record['wall_s']:.1f}"
        )
    aggregate = quality_aggregate(records)
    lines.append(
        f"cambium bench quality: success_rate={aggregate['success_rate']} "
        f"pct={aggregate['pct']} avg_wall_s={aggregate['avg_wall_s']}"
    )
    return "\n".join(lines)


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
        schema_matches = schema_version is None or (
            isinstance(record_schema, int)
            and not isinstance(record_schema, bool)
            and record_schema == schema_version
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
        if isinstance(score, bool) or not isinstance(score, int | float):
            raise ModuleCLIError(
                f"module {manifest.package_name!r}: evaluate result {index} has no numeric score"
            )
        if not math.isfinite(score) or not 0.0 <= score <= 1.0:
            raise ModuleCLIError(
                f"module {manifest.package_name!r}: evaluate result {index} score is outside [0, 1]"
            )
        scored.append(ScoredRecord(record=record, score=float(score)))
    return scored


def score_examples(scored: list[ScoredRecord]) -> dict[str, float]:
    """Metric mean/std/count over records scored by the neutral CLI."""
    scores = [example.score for example in scored]
    if not scores:
        return {"mean": 0.0, "std": 0.0, "count": 0}
    mean = statistics.fmean(scores)
    std = statistics.pstdev(scores) if len(scores) > 1 else 0.0
    return {"mean": round(mean, 6), "std": round(std, 6), "count": len(scores)}


def dataset_stats(
    records: list[dict],
    label_field: str = "decompose",
    split_records: Mapping[str, list[dict]] | None = None,
) -> dict[str, int]:
    """Duplicate ids, cross-split leaks, class balance over raw records.

    Class balance counts the module's declared ``label_field`` (the v1
    default is ``decompose``); the baseline schema keeps the generic
    ``label_true``/``label_false`` key names.  ``split_records`` preserves
    split identity so repeated examples within one split are not reported as
    cross-split leaks.
    """
    ids = [r["id"] for r in records if isinstance(r.get("id"), str)]
    rows_by_split = split_records if split_records is not None else {"combined": records}
    seen: dict[tuple[str, str], str] = {}
    leaks = 0
    for split, rows in rows_by_split.items():
        for r in rows:
            key = (r["input"]["task"], r["input"]["context"])
            previous_split = seen.get(key)
            if previous_split is not None and previous_split != split:
                leaks += 1
            elif previous_split is None:
                seen[key] = split
    return {
        "records": len(records),
        "duplicate_ids": len(ids) - len(set(ids)),
        "cross_split_leaks": leaks,
        "label_true": sum(1 for r in records if r["expected"].get(label_field) is True),
        "label_false": sum(1 for r in records if r["expected"].get(label_field) is False),
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
        if isinstance(r.get("canary_info"), dict) and isinstance(r["canary_info"].get("kind"), str)
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


def _compute_split_digests(datasets_dir: Path, meta: dict[str, Any] | None) -> dict[str, str]:
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
            metric[split] = score_examples(scored[split])
            if split == "canaries":
                canary_scores = [
                    example.score for example in scored[split] if _is_canary(example.record)
                ]
    except (DatasetError, ModuleSplitError) as exc:
        pairs = datasets_dir / f"{pkg_name}_pairs.jsonl"
        if not pairs.exists():
            pairs = datasets_dir / "example_pairs.jsonl"
        if not pairs.exists():
            raise ModuleBoundaryError(
                f"module {pkg_name!r}: three-split dataset unavailable ({exc}) and "
                "no combined fallback file exists"
            ) from exc
        combined = True
        note = (
            f"three-split dataset unavailable ({exc}); fell back to the combined "
            "file and split metrics are null"
        )
        raw = {"combined": load_jsonl(pairs)}
        scored = {}
        metric = {split: None for split in SPLITS}
        canary_scores = []
    if combined:
        scored["combined"] = asyncio.run(_predict(manifest, raw["combined"]))
        metric["combined"] = score_examples(scored["combined"])
        canary_scores = [
            example.score for example in scored["combined"] if _is_canary(example.record)
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
        "dataset": dataset_stats(
            records,
            manifest.label_field,
            {"combined": raw.get("combined", [])}
            if combined
            else {split: raw.get(split, []) for split in SPLITS},
        ),
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
        "drift_thresholds": _merged_thresholds(thresholds),
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
    ``drift_thresholds``, which override the defaults. Split digests are
    compared before metric drift, and a missing or changed digest fails closed.

    Metric means only fail when they fall by more than ``metric_mean_delta``;
    wall p90 fails when it exceeds ``anchor * wall_p90_ratio +
    wall_p90_abs_slack``, and a missing or zero live wall p90 against a
    positive anchor is itself a regression (the comparison is never silently
    skipped); duplicate ids
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

    try:
        anchor_thresholds = anchor["drift_thresholds"] if "drift_thresholds" in anchor else {}
        merged = _merged_thresholds(anchor_thresholds, thresholds)
    except ValueError as exc:
        return missing_split_regressions + [("drift_thresholds", str(exc))]
    regressions: list[tuple[str, str]] = missing_split_regressions

    report_digests = report.get("split_digests")
    anchor_digests = anchor.get("split_digests")
    for split in SPLITS:
        report_digest = report_digests.get(split) if isinstance(report_digests, Mapping) else None
        anchor_digest = anchor_digests.get(split) if isinstance(anchor_digests, Mapping) else None
        if not isinstance(report_digest, str) or not isinstance(anchor_digest, str):
            regressions.append(
                (
                    f"split_digests.{split}",
                    "split digest is missing from the report or anchor",
                )
            )
        elif report_digest != anchor_digest:
            regressions.append(
                (
                    f"split_digests.{split}",
                    f"anchor {anchor_digest} != report {report_digest}",
                )
            )

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
                    f"{a_metric['mean']} -> {r_metric['mean']} (drop {drop:.4f} > {metric_delta})",
                )
            )

    r_wall = (report.get("tests") or {}).get("wall_seconds") or {}
    a_wall = (anchor.get("tests") or {}).get("wall_seconds") or {}
    if a_wall.get("p90") and not r_wall.get("p90"):
        regressions.append(
            (
                "tests.wall_seconds.p90",
                "live wall timing unavailable; wall comparison cannot run",
            )
        )
    elif a_wall.get("p90") and r_wall.get("p90"):
        wall_ratio = merged["wall_p90_ratio"]
        wall_slack = merged["wall_p90_abs_slack"]
        if r_wall["p90"] > a_wall["p90"] * wall_ratio + wall_slack:
            regressions.append(
                (
                    "tests.wall_seconds.p90",
                    f"{r_wall['p90']} > {a_wall['p90']} * {wall_ratio} + {wall_slack}",
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
                f"{a_canaries.get('failed', 0)} + {canary_delta} -> {r_canaries.get('failed', 0)}",
            )
        )

    return regressions


# --------------------------------------------------------------------------
# pytest plugin
# --------------------------------------------------------------------------


class BenchPlugin:
    """Registered when ``--bench`` is set; collects timings, writes/checks."""

    def __init__(self, config: pytest.Config, thresholds: dict[str, Any] | None = None) -> None:
        self.mode = cast(str, config.getoption("bench"))
        bench_root = config.getoption("bench_root")
        self.root = Path(bench_root) if bench_root else None
        self.thresholds = _merged_thresholds(thresholds)
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

    def pytest_terminal_summary(self, terminalreporter: Any, exitstatus: Any, config: Any) -> None:
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
    group = parser.getgroup("cambium-bench")
    if any(getattr(option, "dest", None) == "bench" for option in group.options):
        return  # the entry point and -p may both register this module
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
        type=_finite_nonnegative_float,
        default=None,
        help="override the metric mean drop drift threshold",
    )
    group.addoption(
        "--bench-wall-ratio",
        type=_finite_nonnegative_float,
        default=None,
        help="override the wall p90 ratio drift threshold",
    )


def pytest_configure(config: Any) -> None:
    if config.getoption("bench") is None:
        return
    if config.pluginmanager.hasplugin("cambium-bench"):
        return  # the entry point and -p may both register this module
    thresholds = _merged_thresholds(
        {
            "metric_mean_delta": config.getoption("bench_metric_delta")
            if config.getoption("bench_metric_delta") is not None
            else DEFAULT_THRESHOLDS["metric_mean_delta"],
            "wall_p90_ratio": config.getoption("bench_wall_ratio")
            if config.getoption("bench_wall_ratio") is not None
            else DEFAULT_THRESHOLDS["wall_p90_ratio"],
        }
    )
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


def _measure_module_timings(pkg_name: str) -> dict[str, float]:
    """Run one module's colocated tests once and return per-nodeid wall times.

    The standalone CLI has no pytest report objects to time, so it re-runs the
    module's ``tests/`` with the bench plugin into a throwaway ``--bench-root``
    and reads the recorded ``tests.by_nodeid`` back. The wall-time gate is
    therefore populated in the CLI report/gate path instead of silently
    comparing ``0.0`` against the anchor.

    Only a genuinely empty module — one with no ``tests/`` directory at all —
    is tolerated: it has no wall timings, so both the report and the anchor
    carry ``tests.count == 0`` and the wall comparison is skipped for it by
    design. Every other failure (the timing subprocess cannot run or exceeds
    the 600s timeout, its tests fail, it writes no baseline, or the baseline
    carries no usable timings) raises :class:`ModuleBoundaryError`, which
    aborts the standalone report/gate with a diagnostic instead of silently
    disabling the wall-time check.
    """
    tests_dir = MODULES_DIR / pkg_name / "tests"
    if tests_dir.is_symlink():
        raise ModuleBoundaryError(
            f"module {pkg_name!r}: tests directory is a symlink; refusing to "
            "silently disable wall-time measurements"
        )
    if not tests_dir.is_dir():
        return {}
    manifest = _module_manifest(pkg_name)
    with tempfile.TemporaryDirectory(prefix="cambium-bench-timings-") as root:
        # Credential scrubbing is mandatory: the timing subprocess re-runs the
        # module's own tests, whose CLI tests spawn further subprocesses from
        # ``os.environ``. Never copy ``os.environ`` wholesale into the timing
        # run; ``scrub_environment`` removes CAMBIUM_PROVIDER_* and other
        # credential-like variables (the same fail-closed scrub the supervisor
        # applies to every child environment).
        env = scrub_environment()
        env["PYTEST_DISABLE_PLUGIN_AUTOLOAD"] = "1"
        env.pop("PYTEST_ADDOPTS", None)
        env.pop("PYTEST_PLUGINS", None)
        try:
            result = subprocess.run(
                [
                    sys.executable,
                    "-m",
                    "pytest",
                    "-q",
                    "-p",
                    "cambium.bench",
                    "--bench=report",
                    f"--bench-root={root}",
                    str(tests_dir),
                ],
                cwd=REPO_ROOT,
                env=env,
                capture_output=True,
                check=False,
                timeout=600,
            )
        except subprocess.TimeoutExpired as exc:
            raise ModuleBoundaryError(
                f"module {pkg_name!r}: timing run timed out after 600 seconds"
            ) from exc
        except (OSError, subprocess.SubprocessError) as exc:
            raise ModuleBoundaryError(
                f"module {pkg_name!r}: timing run could not start: {exc}"
            ) from exc
        baseline_path = Path(root) / manifest.module_name / "baseline.json"
        if result.returncode != 0:
            detail = (
                (result.stderr or result.stdout or b"no diagnostic output")
                .decode(errors="replace")
                .strip()
            )
            raise ModuleBoundaryError(
                f"module {pkg_name!r}: timing run exited {result.returncode}: {detail[:500]}"
            )
        if not baseline_path.is_file():
            raise ModuleBoundaryError(
                f"module {pkg_name!r}: timing run wrote no baseline at {baseline_path}"
            )
        try:
            baseline = json.loads(baseline_path.read_text())
        except (OSError, json.JSONDecodeError) as exc:
            raise ModuleBoundaryError(
                f"module {pkg_name!r}: timing baseline is unreadable: {exc}"
            ) from exc
        timings = (baseline.get("tests") or {}).get("by_nodeid")
        if not isinstance(timings, dict):
            raise ModuleBoundaryError(
                f"module {pkg_name!r}: timing baseline has no tests.by_nodeid timings"
            )
        measured = {
            str(nodeid): float(duration)
            for nodeid, duration in timings.items()
            if isinstance(duration, int | float) and not isinstance(duration, bool)
        }
        if not measured:
            raise ModuleBoundaryError(
                f"module {pkg_name!r}: timing run produced no usable wall timings"
            )
        return measured


def _run_quality(args: argparse.Namespace) -> int:
    """Run the fixed quality fixture prompts and report task success rate.

    Returns 0 on a completed run and on a clean skip; 1 only when the fixture
    itself cannot be prepared or a prompt run fails for a reason other than
    missing provider credentials.
    """
    root = args.bench_root if args.bench_root is not None else QUALITY_REPO_DIR
    try:
        records = _run_quality_prompts(root)
    except (AuthError, ModuleBoundaryError, OSError, ValueError) as exc:
        print(
            f"cambium bench quality: ERROR {type(exc).__name__}: {exc}",
            file=sys.stderr,
        )
        return 1
    if records is None:
        print(
            "cambium bench quality: no configured provider credentials for this "
            "repository; skipping the quality run"
        )
        return 0
    print(format_quality_report(records))
    return 0


def _cli_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="python -m cambium.bench",
        description=(
            "Run the Cambium benchmark report, drift gate, explicit re-anchor, "
            "or the task-success-rate quality run."
        ),
    )
    parser.add_argument(
        "mode",
        nargs="?",
        default="report",
        metavar="{report,gate,re-anchor,quality}",
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
        help="runtime root override (default: .cambium/baselines/; quality uses "
        "it as the scratch-repo root, default .cambium/quality-repo/; gitignored)",
    )
    parser.add_argument(
        "--bench-metric-delta",
        type=_finite_nonnegative_float,
        default=None,
        help="override the metric mean drop drift threshold",
    )
    parser.add_argument(
        "--bench-wall-ratio",
        type=_finite_nonnegative_float,
        default=None,
        help="override the wall p90 ratio drift threshold",
    )
    return parser


def main(argv: list[str] | None = None) -> int:
    """CLI form: ``python -m cambium.bench report|gate|re-anchor|quality``."""
    args = _cli_parser().parse_args(sys.argv[1:] if argv is None else argv)
    mode = args.mode
    if mode == "quality":
        return _run_quality(args)
    if mode not in ("report", "gate", "re-anchor"):
        print(
            "usage: python -m cambium.bench report|gate|re-anchor|quality",
            file=sys.stderr,
        )
        return 2
    # The standalone CLI compares two live measurements (the recorded report
    # p90 and the gate's re-measured p90), so its default wall tolerance must
    # absorb legitimate load variation between the two runs without disabling
    # real regression detection: a 3x ratio plus 0.5s absolute slack passes
    # the observed 1.6x load swing while a 100s regression still fails. The
    # pytest plugin path keeps the strict 1.5x ratio (its anchor is a fixed
    # committed baseline), and explicit CLI flags override these defaults.
    thresholds = _merged_thresholds(
        {
            "wall_p90_ratio": args.bench_wall_ratio if args.bench_wall_ratio is not None else 3.0,
            "wall_p90_abs_slack": 0.5,
            "metric_mean_delta": args.bench_metric_delta
            if args.bench_metric_delta is not None
            else DEFAULT_THRESHOLDS["metric_mean_delta"],
        }
    )
    failures = 0
    root = args.bench_root or RUNTIME_BASELINE_DIR
    try:
        pending = []
        for pkg_name in discover_modules():
            pending.append(
                (
                    pkg_name,
                    _assemble_baseline(
                        build_module_report(pkg_name),
                        _measure_module_timings(pkg_name),
                        thresholds,
                    ),
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
