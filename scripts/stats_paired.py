"""Compare two same-task pass/fail result files."""

from __future__ import annotations

import argparse
import json
import sys
from collections.abc import Mapping
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from cambium.stats import paired_significance  # noqa: E402


class ResultsError(ValueError):
    """A results file cannot be reduced to task pass/fail values."""


def _passed(record: Any) -> bool | None:
    if isinstance(record, bool):
        return record
    if not isinstance(record, Mapping):
        return None
    for key in ("passed", "pass", "success", "succeeded", "ok"):
        if type(record.get(key)) is bool:
            return record[key]
    value = record.get("result")
    if type(value) is bool:
        return value
    status = record.get("status", record.get("verdict"))
    if isinstance(status, str):
        if status.casefold() in {"pass", "passed", "success", "succeeded", "ok", "done"}:
            return True
        if status.casefold() in {"fail", "failed", "failure", "error"}:
            return False
    return None


def _result_records(value: Any) -> list[tuple[str, Any]]:
    if isinstance(value, list):
        return [(str(index), item) for index, item in enumerate(value)]
    if isinstance(value, Mapping):
        if _passed(value) is not None:
            task_id = value.get("task_id", value.get("id", value.get("task", "0")))
            return [(str(task_id), value)]
        for key in ("results", "outcomes", "tasks", "runs"):
            if key not in value:
                continue
            nested = value[key]
            if isinstance(nested, list):
                return [(str(index), item) for index, item in enumerate(nested)]
            if isinstance(nested, Mapping):
                return [(str(task_id), item) for task_id, item in nested.items()]
        return [(str(task_id), item) for task_id, item in value.items()]
    raise ResultsError("results JSON must be an array or object")


def load_results(path: str | Path) -> dict[str, bool]:
    """Load task ids and pass/fail values from common JSON result shapes."""
    try:
        value = json.loads(Path(path).read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise ResultsError(f"cannot read results file {path}: {exc}") from exc
    results: dict[str, bool] = {}
    for fallback_id, record in _result_records(value):
        passed = _passed(record)
        if passed is None and isinstance(record, Mapping):
            passed = _passed(record.get("outcome"))
        if passed is None:
            raise ResultsError(f"result {fallback_id!r} has no boolean pass/fail value")
        task_id = fallback_id
        if isinstance(record, Mapping):
            declared_id = record.get("task_id", record.get("id", record.get("task")))
            if isinstance(declared_id, str | int) and not isinstance(declared_id, bool):
                task_id = str(declared_id)
        if task_id in results:
            raise ResultsError(f"duplicate task id {task_id!r} in {path}")
        results[task_id] = passed
    return results


def compare_files(
    path_a: str | Path,
    path_b: str | Path,
    *,
    iterations: int = 10_000,
    confidence: float = 0.95,
    seed: int = 0,
) -> dict[str, Any]:
    """Load and compare two result files, refusing unpaired task ids."""
    results_a = load_results(path_a)
    results_b = load_results(path_b)
    if not results_a:
        raise ResultsError("results files must contain at least one task")
    if set(results_a) != set(results_b):
        missing_a = sorted(set(results_b) - set(results_a))
        missing_b = sorted(set(results_a) - set(results_b))
        raise ResultsError(f"task ids differ: only-a={missing_b!r}, only-b={missing_a!r}")
    task_ids = sorted(results_a)
    try:
        report = paired_significance(
            [results_a[task_id] for task_id in task_ids],
            [results_b[task_id] for task_id in task_ids],
            iterations=iterations,
            confidence=confidence,
            seed=seed,
        )
    except (TypeError, ValueError) as exc:
        raise ResultsError(str(exc)) from exc
    report["tasks"] = task_ids
    return report


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("results_a", type=Path)
    parser.add_argument("results_b", type=Path)
    parser.add_argument("--iterations", type=int, default=10_000)
    parser.add_argument("--confidence", type=float, default=0.95)
    parser.add_argument("--seed", type=int, default=0)
    parser.add_argument("--json", action="store_true", help="emit one JSON report")
    args = parser.parse_args(argv)
    try:
        report = compare_files(
            args.results_a,
            args.results_b,
            iterations=args.iterations,
            confidence=args.confidence,
            seed=args.seed,
        )
    except ResultsError as exc:
        parser.error(str(exc))
    if args.json:
        print(json.dumps(report, sort_keys=True))
    else:
        ci = report["ci"]
        print(f"p-value: {report['p_value']:.6g}")
        print(
            f"CI ({ci['confidence']:.0%}) for delta B-A: "
            f"[{ci['low']:.6g}, {ci['high']:.6g}]"
        )
        print(f"McNemar p-value: {report['mcnemar_p_value']:.6g}")
        print(f"verdict: {report['verdict']}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
