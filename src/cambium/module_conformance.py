"""Pytest gate for the isolated Cambium module contract.

The gate is deliberately repository-aware.  A module is not conformant just
because its tests pass: its required files must be tracked, its data must be
readable, its imports must stay inside the module boundary, and its JSON CLI
must work from the source tree on ``PYTHONPATH``.
"""

from __future__ import annotations

import ast
import datetime as _dt
import hashlib
import json
import math
import os
import re
import stat
import subprocess
import sys
import tempfile
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import Any, cast

import pytest

from cambium.modules.base import (
    ModuleBoundaryError,
    ModuleManifest,
    load_module_manifest,
)


def _find_repo_root() -> Path:
    source = Path(__file__).resolve().parent
    for candidate in (source, *source.parents):
        if (candidate / ".git").exists() and source == candidate / "src" / "cambium":
            return candidate
    # A wheel has no repository root.  Keep all resource-relative operations
    # inside the installed package instead of guessing from the caller's cwd.
    return source


PACKAGE_ROOT = Path(__file__).resolve().parent
REPO_ROOT = _find_repo_root()
# ``cambium`` is under ``src`` in a checkout and under site-packages in a
# wheel.  The package resource is the stable boundary in both layouts.
MODULES_DIR = PACKAGE_ROOT / "modules"

PROVIDER_IMPORTS = (
    "cambium.diffundo",
    "cambium.provider_config",
    "anthropic",
    "cohere",
    "google.genai",
    "google.generativeai",
    "litellm",
    "mistralai",
    "openai",
)

DECISION_SPLITS = {
    "train": "train.jsonl",
    "eval": "eval.jsonl",
    "canaries": "canaries.jsonl",
}
SUPPORTED_DATASET_SCHEMA_VERSION = 1
SEMVER_RE = re.compile(
    r"^(0|[1-9]\d*)\.(0|[1-9]\d*)\.(0|[1-9]\d*)"
    r"(?:-[0-9A-Za-z-]+(?:\.[0-9A-Za-z-]+)*)?"
    r"(?:\+[0-9A-Za-z-]+(?:\.[0-9A-Za-z-]+)*)?$"
)
SHA256_RE = re.compile(r"^[0-9a-f]{64}$")
GIT_SHA_RE = re.compile(r"^(?:[0-9a-f]{40}|[0-9a-f]{64})$")
VERSION_RE = re.compile(r"^\d+\.\d+(?:\.\d+)?(?:[-+][0-9A-Za-z.-]+)?$")
ISO_DATE_RE = re.compile(r"^\d{4}-\d{2}-\d{2}$")
_SENSITIVE_ENV_RE = re.compile(
    r"(?:api|key|token|secret|password|passwd|credential|authorization)", re.IGNORECASE
)
_OPTIONS_ADDED = False
_AUDIT_HOOK_INSTALLED = False


class ModuleConformanceError(ValueError):
    """Raised when a module violates the conformance contract."""


class _GitHistoryLookupError(RuntimeError):
    """Raised when git history exists but cannot be read reliably."""


@dataclass(frozen=True, slots=True)
class AuditFinding:
    """A static finding with a stable file, line, and symbol."""

    rule: str
    path: Path
    line: int
    symbol: str
    detail: str

    def format(self) -> str:
        return f"{self.rule}: {self.path}:{self.line}:{self.symbol}: {self.detail}"


@dataclass(frozen=True, slots=True)
class ModuleSpec:
    """Tracked files and paths for one discovered module."""

    name: str
    path: Path
    tracked_files: tuple[Path, ...]
    python_files: tuple[Path, ...]
    test_files: tuple[Path, ...]
    baseline_files: tuple[Path, ...]
    dataset_files: tuple[Path, ...]
    manifest: ModuleManifest | None = None

    @property
    def tests_dir(self) -> Path:
        """Return the colocated test directory."""
        return self.path / "tests"

    @property
    def package_name(self) -> str:
        """Return the import name for this module."""
        return f"cambium.modules.{self.name}"


def _is_provider_import(name: str) -> bool:
    return any(name == root or name.startswith(f"{root}.") for root in PROVIDER_IMPORTS)


def _is_regular_file(path: Path) -> bool:
    if path.is_symlink():
        return False
    try:
        return stat.S_ISREG(path.stat().st_mode)
    except OSError:
        return False


def _repository_available() -> bool:
    """Return whether this installation has a git checkout beside it."""
    return (REPO_ROOT / ".git").exists()


def _module_prefix(name: str) -> Path:
    """Return the tracked/resource prefix for one installed package module."""
    try:
        return MODULES_DIR.relative_to(REPO_ROOT) / name
    except ValueError:
        return Path("modules") / name


def _package_files(name: str) -> tuple[Path, ...]:
    """List regular files from an installed module when git is unavailable."""
    module_path = MODULES_DIR / name
    if not module_path.is_dir() or module_path.is_symlink():
        return ()
    return tuple(
        sorted(
            path.relative_to(REPO_ROOT) for path in module_path.rglob("*") if _is_regular_file(path)
        )
    )


def _git_ls_files(pathspec: str) -> tuple[Path, ...]:
    if not _repository_available():
        return ()
    try:
        result = subprocess.run(
            ["git", "ls-files", "-z", "--", pathspec],
            cwd=REPO_ROOT,
            capture_output=True,
            check=False,
            timeout=10,
        )
    except (OSError, subprocess.SubprocessError) as exc:
        raise ModuleConformanceError(f"git ls-files failed for {pathspec}: {exc}") from exc
    if result.returncode != 0:
        detail = result.stderr.decode(errors="replace").strip()
        raise ModuleConformanceError(f"git ls-files failed for {pathspec}: {detail}")
    return tuple(Path(os.fsdecode(part)) for part in result.stdout.split(b"\0") if part)


def _module_files(name: str, prefix: Path) -> tuple[Path, ...]:
    if _repository_available():
        return _git_ls_files(prefix.as_posix())
    return _package_files(name)


def module_names() -> list[str]:
    """Return sorted immediate package children with a physical ``__init__.py``."""
    if not MODULES_DIR.is_dir():
        return []
    return sorted(
        child.name
        for child in MODULES_DIR.iterdir()
        if child.is_dir() and not child.is_symlink() and _is_regular_file(child / "__init__.py")
    )


def discover_modules() -> list[str]:
    """Return discovered module names without validating their contents."""
    return module_names()


def _load_json(path: Path) -> Any:
    def reject_constant(value: str) -> None:
        raise ValueError(f"non-standard JSON constant {value!r}")

    def reject_duplicate(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
        result: dict[str, Any] = {}
        for key, value in pairs:
            if key in result:
                raise ValueError(f"duplicate JSON field {key!r}")
            result[key] = value
        return result

    return json.loads(
        path.read_text(encoding="utf-8"),
        object_pairs_hook=reject_duplicate,
        parse_constant=reject_constant,
    )


def _resource_path(path: Path) -> Path:
    """Resolve a tracked relative path in a checkout or installed wheel."""
    return path if path.is_absolute() else REPO_ROOT / path


def _validate_json_files(
    module_name: str,
    baseline_files: tuple[Path, ...],
    dataset_files: tuple[Path, ...],
) -> None:
    errors: list[str] = []
    for path in baseline_files:
        if path.suffix.lower() != ".json":
            continue
        resource = _resource_path(path)
        try:
            value = _load_json(resource)
        except (OSError, ValueError, json.JSONDecodeError) as exc:
            errors.append(f"{path}: invalid baseline JSON: {exc}")
            continue
        if not isinstance(value, dict):
            errors.append(f"{path}: baseline JSON must be an object")

    for path in dataset_files:
        if path.suffix.lower() not in {".json", ".jsonl"}:
            continue
        current_line = 0
        resource = _resource_path(path)
        try:
            if path.suffix.lower() == ".json":
                _load_json(resource)
                continue
            for line_number, line in enumerate(
                resource.read_text(encoding="utf-8").splitlines(), start=1
            ):
                current_line = line_number
                if not line.strip():
                    continue
                record = json.loads(line)
                if not isinstance(record, dict):
                    raise ValueError("JSONL record must be an object")
        except (OSError, ValueError, json.JSONDecodeError) as exc:
            line_detail = f" at line {current_line}" if current_line else ""
            errors.append(f"{path}{line_detail}: invalid dataset JSON: {exc}")
    if errors:
        raise ModuleConformanceError(f"{module_name}:\n" + "\n".join(errors))


def _valid_semver(value: object) -> bool:
    return isinstance(value, str) and SEMVER_RE.fullmatch(value) is not None


def _valid_sha256(value: object) -> bool:
    return isinstance(value, str) and SHA256_RE.fullmatch(value) is not None


def _valid_freeze_date(value: object) -> bool:
    if not isinstance(value, str) or ISO_DATE_RE.fullmatch(value) is None:
        return False
    try:
        _dt.date.fromisoformat(value)
    except ValueError:
        return False
    return True


def _load_jsonl_records(path: Path) -> list[tuple[int, dict[str, Any]]]:
    records: list[tuple[int, dict[str, Any]]] = []

    def reject_duplicate(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
        result: dict[str, Any] = {}
        for key, value in pairs:
            if key in result:
                raise ValueError(f"duplicate JSON field {key!r}")
            result[key] = value
        return result

    for line_number, line in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
        if not line.strip():
            continue
        try:
            value = json.loads(line, object_pairs_hook=reject_duplicate)
        except (TypeError, ValueError, json.JSONDecodeError) as exc:
            raise ModuleConformanceError(f"{path}:{line_number}: invalid JSONL: {exc}") from exc
        if not isinstance(value, dict):
            raise ModuleConformanceError(f"{path}:{line_number}: record must be a JSON object")
        records.append((line_number, value))
    return records


def _canonical_record_hash(record: dict[str, Any]) -> str:
    input_data = record.get("input")
    if not isinstance(input_data, dict):
        raise TypeError("input must be an object")
    task = input_data.get("task")
    context = input_data.get("context")
    if not isinstance(task, str) or not isinstance(context, str):
        raise TypeError("input.task and input.context must be strings")
    # Keep this explicit: malformed or otherwise unhashable input must never
    # silently disappear from the cross-split collision set.
    hash((task, context))
    payload = json.dumps((task, context), ensure_ascii=False, separators=(",", ":"))
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def _git_file(revision: str, relative: Path) -> bytes | None:
    """Read one historical file; ``None`` means it did not exist then.

    A failed git invocation is different from an absent historical path.  The
    former must fail the freeze gate rather than disabling it accidentally.
    """
    if not _repository_available():
        return None
    try:
        result = subprocess.run(
            ["git", "show", f"{revision}:{relative.as_posix()}"],
            cwd=REPO_ROOT,
            capture_output=True,
            check=False,
            timeout=10,
        )
    except (OSError, subprocess.SubprocessError) as exc:
        raise _GitHistoryLookupError(f"git show failed for {revision}:{relative}: {exc}") from exc
    if result.returncode == 0:
        return result.stdout
    detail = result.stderr.decode(errors="replace").strip()
    if "does not exist in" in detail or "exists on disk, but not in" in detail:
        return None
    raise _GitHistoryLookupError(
        f"git show failed for {revision}:{relative}: {detail or 'unknown git error'}"
    )


def _git_history_revisions(spec: ModuleSpec) -> tuple[str, ...]:
    """Return all commits touching the dataset history, newest first."""
    if not _repository_available():
        return ()
    datasets = spec.path / "datasets"
    paths = [
        (datasets / "meta.json").relative_to(REPO_ROOT),
        (datasets / "eval.jsonl").relative_to(REPO_ROOT),
        (datasets / "canaries.jsonl").relative_to(REPO_ROOT),
    ]
    try:
        result = subprocess.run(
            ["git", "log", "--format=%H", "--", *(path.as_posix() for path in paths)],
            cwd=REPO_ROOT,
            capture_output=True,
            text=True,
            check=False,
            timeout=10,
        )
    except (OSError, subprocess.SubprocessError) as exc:
        raise _GitHistoryLookupError(f"git log failed for {spec.name}: {exc}") from exc
    if result.returncode != 0:
        detail = result.stderr.strip()
        raise _GitHistoryLookupError(
            f"git log failed for {spec.name}: {detail or 'unknown git error'}"
        )
    return tuple(dict.fromkeys(line.strip() for line in result.stdout.splitlines() if line.strip()))


def _freeze_content_changed(previous: bytes, current: bytes) -> bool:
    """Compare frozen records while tolerating historical version-only rewrites."""
    if previous == current:
        return False
    try:

        def normalize(data: bytes) -> list[dict[str, Any]]:
            records: list[dict[str, Any]] = []
            for line in data.decode("utf-8").splitlines():
                if not line.strip():
                    continue
                record = json.loads(line)
                if not isinstance(record, dict):
                    raise ValueError("record is not an object")
                record = dict(record)
                record.pop("dataset_version", None)
                records.append(record)
            return records

        return normalize(previous) != normalize(current)
    except (UnicodeDecodeError, TypeError, ValueError, json.JSONDecodeError):
        return True


def _frozen_content_findings(spec: ModuleSpec, meta: dict[str, Any]) -> list[AuditFinding]:
    """Reject eval/canary edits without a dataset-version bump."""
    if not _repository_available():
        return []
    version = meta.get("dataset_version")
    if not _valid_semver(version):
        return []
    try:
        revisions = _git_history_revisions(spec)
    except _GitHistoryLookupError as exc:
        path = spec.path / "datasets" / "meta.json"
        return [
            AuditFinding(
                "freeze-version",
                path,
                0,
                "git-history",
                f"cannot verify frozen content history: {exc}",
            )
        ]
    findings: list[AuditFinding] = []
    meta_relative = (spec.path / "datasets" / "meta.json").relative_to(REPO_ROOT)
    for split, filename in (("eval", "eval.jsonl"), ("canary", "canaries.jsonl")):
        path = spec.path / "datasets" / filename
        try:
            current = path.read_bytes()
        except OSError:
            continue
        relative = path.relative_to(REPO_ROOT)
        saw_current_version = False
        for revision in revisions:
            try:
                previous_meta = _git_file(revision, meta_relative)
                if previous_meta is None:
                    continue
                old_meta = json.loads(previous_meta)
            except _GitHistoryLookupError as exc:
                findings.append(
                    AuditFinding(
                        "freeze-version",
                        path,
                        0,
                        "git-history",
                        f"cannot verify frozen content history: {exc}",
                    )
                )
                break
            except (TypeError, ValueError, json.JSONDecodeError):
                continue
            if not isinstance(old_meta, dict):
                continue
            if old_meta.get("dataset_version") != version:
                if saw_current_version:
                    break
                continue
            saw_current_version = True
            try:
                previous = _git_file(revision, relative)
            except _GitHistoryLookupError as exc:
                findings.append(
                    AuditFinding(
                        "freeze-version",
                        path,
                        0,
                        "git-history",
                        f"cannot verify frozen content history: {exc}",
                    )
                )
                break
            if previous is not None and _freeze_content_changed(previous, current):
                findings.append(
                    AuditFinding(
                        "freeze-version",
                        path,
                        0,
                        split,
                        f"{split} content changed from {revision} without dataset_version "
                        f"bump ({version})",
                    )
                )
                break
    return findings


def _valid_baseline_date(value: object) -> bool:
    if not isinstance(value, str) or not value.strip():
        return False
    try:
        parsed = _dt.datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return False
    return parsed.tzinfo is not None


def _current_git_sha() -> str:
    """Return the checkout's current commit for a regenerated baseline."""
    if not _repository_available():
        raise ModuleConformanceError("baseline regeneration requires a git checkout")
    try:
        result = subprocess.run(
            ["git", "rev-parse", "HEAD"],
            cwd=REPO_ROOT,
            capture_output=True,
            text=True,
            check=False,
            timeout=10,
        )
    except (OSError, subprocess.SubprocessError) as exc:
        raise ModuleConformanceError(f"could not read current git commit: {exc}") from exc
    sha = result.stdout.strip()
    if result.returncode != 0 or GIT_SHA_RE.fullmatch(sha) is None:
        detail = result.stderr.strip() or "invalid commit output"
        raise ModuleConformanceError(f"could not read current git commit: {detail}")
    return sha


def _regenerate_baseline(spec: ModuleSpec, timings: dict[str, float]) -> Path:
    """Rewrite only the live test timing fields and regen provenance."""
    baseline_file = next(
        (REPO_ROOT / path for path in spec.baseline_files if path.suffix.lower() == ".json"),
        None,
    )
    if baseline_file is None:
        raise ModuleConformanceError(f"{spec.name}: baseline is required for regeneration")
    try:
        baseline = _load_json(baseline_file)
    except (OSError, ValueError, json.JSONDecodeError) as exc:
        raise ModuleConformanceError(
            f"{spec.name}: cannot read baseline for regeneration: {exc}"
        ) from exc
    if not isinstance(baseline, dict):
        raise ModuleConformanceError(f"{spec.name}: baseline must be an object for regeneration")
    tests = baseline.get("tests")
    if not isinstance(tests, dict):
        raise ModuleConformanceError(
            f"{spec.name}: baseline tests must be an object for regeneration"
        )

    refreshed: dict[str, float] = {}
    for nodeid in sorted(timings):
        duration = timings[nodeid]
        if (
            not isinstance(nodeid, str)
            or not nodeid
            or isinstance(duration, bool)
            or not isinstance(duration, int | float)
            or not math.isfinite(duration)
            or duration < 0
        ):
            raise ModuleConformanceError(
                f"{spec.name}: invalid collected test timing for {nodeid!r}"
            )
        refreshed[nodeid] = round(float(duration), 6)

    tests["count"] = len(refreshed)
    tests["by_nodeid"] = refreshed
    baseline["git_sha"] = _current_git_sha()
    baseline["date"] = (
        _dt.datetime.now(_dt.UTC).isoformat(timespec="seconds").replace("+00:00", "Z")
    )
    try:
        baseline_file.write_text(
            json.dumps(baseline, indent=2, allow_nan=False) + "\n",
            encoding="utf-8",
        )
    except (OSError, TypeError, ValueError) as exc:
        raise ModuleConformanceError(
            f"{spec.name}: could not write regenerated baseline: {exc}"
        ) from exc
    return baseline_file


def _finite_non_negative(value: object) -> bool:
    return (
        not isinstance(value, bool)
        and isinstance(value, int | float)
        and math.isfinite(value)
        and value >= 0
    )


def _baseline_fact_findings(
    baseline: dict[str, Any], baseline_file: Path, spec: ModuleSpec
) -> list[AuditFinding]:
    findings: list[AuditFinding] = []
    provenance = {
        "git_sha": lambda value: isinstance(value, str) and GIT_SHA_RE.fullmatch(value) is not None,
        "date": _valid_baseline_date,
        "python": lambda value: isinstance(value, str) and VERSION_RE.fullmatch(value) is not None,
        "pytest": lambda value: isinstance(value, str) and VERSION_RE.fullmatch(value) is not None,
    }
    for field, valid in provenance.items():
        if not valid(baseline.get(field)):
            findings.append(
                AuditFinding(
                    "baseline-integrity",
                    baseline_file,
                    0,
                    field,
                    "must contain plausible non-null provenance",
                )
            )

    tests = baseline.get("tests")
    if isinstance(tests, dict):
        nodeids = tests.get("by_nodeid")
        module_test_paths = {
            relative
            for path in spec.test_files
            if (relative := _module_relative_path(path, spec)) is not None
        }
        if isinstance(nodeids, dict):
            for nodeid in nodeids:
                test_path = nodeid.split("::", 1)[0] if isinstance(nodeid, str) else ""
                relative = _module_relative_path(Path(test_path), spec) if test_path else None
                if test_path and relative not in module_test_paths:
                    findings.append(
                        AuditFinding(
                            "baseline-integrity",
                            baseline_file,
                            0,
                            nodeid,
                            "test nodeid does not belong to this module's tests",
                        )
                    )
        wall_seconds = tests.get("wall_seconds")
        if isinstance(wall_seconds, dict) and set(wall_seconds) == {"p50", "p90", "max"}:
            for field, value in wall_seconds.items():
                if not _finite_non_negative(value):
                    findings.append(
                        AuditFinding(
                            "baseline-integrity",
                            baseline_file,
                            0,
                            f"tests.wall_seconds.{field}",
                            "must be a finite non-negative number",
                        )
                    )

    thresholds = baseline.get("drift_thresholds")
    required = {"metric_mean_delta", "wall_p90_ratio", "canary_failed_delta", "dataset"}
    if isinstance(thresholds, dict):
        missing = required - set(thresholds)
        if missing:
            findings.append(
                AuditFinding(
                    "baseline-integrity",
                    baseline_file,
                    0,
                    "drift_thresholds",
                    "missing required thresholds: " + ", ".join(sorted(missing)),
                )
            )
        for field in ("metric_mean_delta", "wall_p90_ratio"):
            value = thresholds.get(field)
            if not _finite_non_negative(value) or (field == "wall_p90_ratio" and value == 0):
                qualifier = "positive" if field == "wall_p90_ratio" else "non-negative"
                findings.append(
                    AuditFinding(
                        "baseline-integrity",
                        baseline_file,
                        0,
                        f"drift_thresholds.{field}",
                        f"must be a finite {qualifier} number",
                    )
                )
        canary_delta = thresholds.get("canary_failed_delta")
        if isinstance(canary_delta, bool) or not isinstance(canary_delta, int) or canary_delta < 0:
            findings.append(
                AuditFinding(
                    "baseline-integrity",
                    baseline_file,
                    0,
                    "drift_thresholds.canary_failed_delta",
                    "must be a non-negative integer",
                )
            )
        dataset = thresholds.get("dataset")
        dataset_fields = {"duplicate_ids", "cross_split_leaks"}
        if not isinstance(dataset, dict) or set(dataset) != dataset_fields:
            findings.append(
                AuditFinding(
                    "baseline-integrity",
                    baseline_file,
                    0,
                    "drift_thresholds.dataset",
                    "must contain duplicate_ids and cross_split_leaks",
                )
            )
        else:
            for field, value in dataset.items():
                if isinstance(value, bool) or not isinstance(value, int) or value < 0:
                    findings.append(
                        AuditFinding(
                            "baseline-integrity",
                            baseline_file,
                            0,
                            f"drift_thresholds.dataset.{field}",
                            "must be a non-negative integer",
                        )
                    )
    return findings


def _module_relative_path(path: Path, spec: ModuleSpec) -> str | None:
    """Normalize source and wheel paths relative to one module package."""
    prefixes = (_module_prefix(spec.name), Path("src/cambium/modules") / spec.name)
    for prefix in prefixes:
        try:
            return path.relative_to(prefix).as_posix()
        except ValueError:
            continue
    try:
        return _resource_path(path).relative_to(spec.path).as_posix()
    except ValueError:
        return None


def _validate_dataset_integrity(spec: ModuleSpec, manifest: ModuleManifest | None = None) -> None:
    """Validate the frozen split contract without importing the decision package."""
    findings: list[AuditFinding] = []
    datasets = spec.path / "datasets"
    meta_path = datasets / "meta.json"
    tracked = set(spec.tracked_files)
    if meta_path.relative_to(REPO_ROOT) not in tracked or not _is_regular_file(meta_path):
        findings.append(
            AuditFinding(
                "dataset-integrity",
                meta_path,
                0,
                "meta.json",
                "valid meta.json is required; missing metadata is never defaulted",
            )
        )
        message = "{}:\n{}".format(spec.name, "\n".join(f.format() for f in findings))
        raise ModuleConformanceError(message)

    try:
        meta = _load_json(meta_path)
    except (OSError, ValueError, json.JSONDecodeError) as exc:
        findings.append(AuditFinding("dataset-integrity", meta_path, 0, "meta.json", str(exc)))
        message = f"{spec.name}:\n" + "\n".join(f.format() for f in findings)
        raise ModuleConformanceError(message) from exc
    if not isinstance(meta, dict):
        findings.append(
            AuditFinding("dataset-integrity", meta_path, 0, "meta.json", "must be a JSON object")
        )
        raise ModuleConformanceError(f"{spec.name}:\n" + "\n".join(f.format() for f in findings))

    schema_version = meta.get("schema_version")
    if isinstance(schema_version, bool) or schema_version != SUPPORTED_DATASET_SCHEMA_VERSION:
        findings.append(
            AuditFinding(
                "dataset-integrity",
                meta_path,
                0,
                "schema_version",
                f"must be integer {SUPPORTED_DATASET_SCHEMA_VERSION}",
            )
        )
    module_label_field = manifest.label_field if manifest is not None else "decompose"
    if not isinstance(module_label_field, str) or not module_label_field:
        findings.append(
            AuditFinding(
                "dataset-integrity",
                spec.path / "module.json",
                0,
                "label_field",
                "must be a non-empty string when present",
            )
        )
        module_label_field = "decompose"
    if manifest is not None and schema_version != manifest.dataset_schema_version:
        findings.append(
            AuditFinding(
                "dataset-integrity",
                meta_path,
                0,
                "schema_version",
                "meta.json schema_version must match module.json dataset_schema_version "
                f"({schema_version!r} != {manifest.dataset_schema_version!r})",
            )
        )
    if not _valid_semver(meta.get("dataset_version")):
        findings.append(
            AuditFinding(
                "dataset-integrity",
                meta_path,
                0,
                "dataset_version",
                "must be a valid semantic version",
            )
        )
    for field in ("eval_frozen_at", "canary_frozen_at"):
        if not _valid_freeze_date(meta.get(field)):
            findings.append(
                AuditFinding(
                    "dataset-integrity",
                    meta_path,
                    0,
                    field,
                    "must be an ISO-8601 date in YYYY-MM-DD form",
                )
            )

    raw_digests = meta.get("split_digests")
    if not isinstance(raw_digests, dict):
        findings.append(
            AuditFinding(
                "dataset-integrity",
                meta_path,
                0,
                "split_digests",
                "must contain one SHA-256 digest for train, eval, and canaries",
            )
        )
        raw_digests = {}
    actual_digests: dict[str, str] = {}
    for split, filename in DECISION_SPLITS.items():
        path = datasets / filename
        relative = path.relative_to(REPO_ROOT)
        if relative not in tracked or not _is_regular_file(path):
            findings.append(
                AuditFinding("dataset-integrity", path, 0, split, "required split is not tracked")
            )
            continue
        digest = hashlib.sha256(path.read_bytes()).hexdigest()
        actual_digests[split] = digest
        recorded = raw_digests.get(split)
        if not _valid_sha256(recorded):
            findings.append(
                AuditFinding(
                    "dataset-integrity",
                    meta_path,
                    0,
                    f"split_digests.{split}",
                    "must be a lowercase SHA-256 hex digest",
                )
            )
        elif recorded != digest:
            findings.append(
                AuditFinding(
                    "dataset-integrity",
                    path,
                    0,
                    f"split_digests.{split}",
                    f"metadata digest does not match content ({digest})",
                )
            )

    seen_ids: dict[str, tuple[str, int]] = {}
    seen_hashes: dict[str, tuple[str, int]] = {}
    records_by_split: dict[str, list[tuple[int, dict[str, Any]]]] = {}
    for split, filename in DECISION_SPLITS.items():
        path = datasets / filename
        if not path.is_file():
            continue
        try:
            records = _load_jsonl_records(path)
        except ModuleConformanceError as exc:
            findings.append(AuditFinding("dataset-integrity", path, 0, split, str(exc)))
            continue
        records_by_split[split] = records
        previous_id: str | None = None
        for line_number, record in records:
            record_id = record.get("id")
            if not isinstance(record_id, str) or not record_id.strip():
                findings.append(
                    AuditFinding(
                        "dataset-integrity", path, line_number, "id", "must be non-empty string"
                    )
                )
            elif record_id in seen_ids:
                first_split, first_line = seen_ids[record_id]
                findings.append(
                    AuditFinding(
                        "dataset-integrity",
                        path,
                        line_number,
                        record_id,
                        f"duplicate id; first seen at {first_split}:{first_line}",
                    )
                )
            else:
                seen_ids[record_id] = (split, line_number)
            if previous_id is not None and isinstance(record_id, str) and record_id < previous_id:
                findings.append(
                    AuditFinding("dataset-integrity", path, line_number, "id", "ids must be sorted")
                )
            if isinstance(record_id, str):
                previous_id = record_id
            if not isinstance(record.get("input"), dict):
                findings.append(
                    AuditFinding(
                        "dataset-integrity",
                        path,
                        line_number,
                        "input",
                        "top-level input object is required (the current wire schema is not data)",
                    )
                )
            if not isinstance(record.get("expected"), dict):
                findings.append(
                    AuditFinding(
                        "dataset-integrity",
                        path,
                        line_number,
                        "expected",
                        "top-level expected object is required (current wire schema is not data)",
                    )
                )
            input_obj = record.get("input")
            if isinstance(input_obj, dict):
                task = input_obj.get("task")
                if not isinstance(task, str) or not task.strip():
                    findings.append(
                        AuditFinding(
                            "dataset-integrity",
                            path,
                            line_number,
                            "input.task",
                            "must be a non-empty string",
                        )
                    )
                if not isinstance(input_obj.get("context"), str):
                    findings.append(
                        AuditFinding(
                            "dataset-integrity",
                            path,
                            line_number,
                            "input.context",
                            "must be a string",
                        )
                    )
            expected_obj = record.get("expected")
            if isinstance(expected_obj, dict):
                label = expected_obj.get(module_label_field)
                if not isinstance(label, bool):
                    findings.append(
                        AuditFinding(
                            "dataset-integrity",
                            path,
                            line_number,
                            f"expected.{module_label_field}",
                            "must be a boolean label",
                        )
                    )
                if not isinstance(expected_obj.get("reason"), str):
                    findings.append(
                        AuditFinding(
                            "dataset-integrity",
                            path,
                            line_number,
                            "expected.reason",
                            "must be a string",
                        )
                    )
            if (
                isinstance(expected_obj, dict)
                and module_label_field != "decompose"
                and "decompose" in expected_obj
                and expected_obj.get("decompose") is not expected_obj.get(module_label_field)
            ):
                findings.append(
                    AuditFinding(
                        "dataset-integrity",
                        path,
                        line_number,
                        "decompose",
                        f"expected.decompose must mirror expected.{module_label_field} "
                        f"(label_field {module_label_field!r})",
                    )
                )
            if record.get("schema_version") != schema_version:
                findings.append(
                    AuditFinding(
                        "dataset-integrity",
                        path,
                        line_number,
                        "schema_version",
                        "record schema_version must match meta.json",
                    )
                )
            record_version = record.get("dataset_version")
            if not _valid_semver(record_version):
                findings.append(
                    AuditFinding(
                        "dataset-integrity",
                        path,
                        line_number,
                        "dataset_version",
                        "record dataset_version must be semantic version",
                    )
                )
            elif record_version != meta.get("dataset_version"):
                findings.append(
                    AuditFinding(
                        "dataset-integrity",
                        path,
                        line_number,
                        "dataset_version",
                        "record dataset_version must match meta.json "
                        f"({record_version!r} != {meta.get('dataset_version')!r})",
                    )
                )
            expected_split = "canary" if split == "canaries" else split
            if record.get("split") != expected_split:
                findings.append(
                    AuditFinding(
                        "dataset-integrity",
                        path,
                        line_number,
                        "split",
                        f"must be {expected_split!r}",
                    )
                )
            if split == "canaries" and record.get("canary") is not True:
                findings.append(
                    AuditFinding(
                        "dataset-integrity",
                        path,
                        line_number,
                        "canary",
                        "canaries.jsonl records must set canary=true",
                    )
                )
            if split != "canaries" and record.get("canary", False) is not False:
                findings.append(
                    AuditFinding(
                        "dataset-integrity",
                        path,
                        line_number,
                        "canary",
                        "train/eval records must not set canary=true",
                    )
                )
            try:
                canonical_hash = _canonical_record_hash(record)
            except (TypeError, ValueError, KeyError) as exc:
                findings.append(
                    AuditFinding(
                        "dataset-integrity",
                        path,
                        line_number,
                        "cross_split_hash",
                        f"canonical input cannot be hashed: {exc}",
                    )
                )
            else:
                if canonical_hash in seen_hashes:
                    first_split, first_line = seen_hashes[canonical_hash]
                    findings.append(
                        AuditFinding(
                            "dataset-integrity",
                            path,
                            line_number,
                            "cross_split_hash",
                            f"canonical input collides with {first_split}:{first_line}",
                        )
                    )
                else:
                    seen_hashes[canonical_hash] = (split, line_number)

    baseline_required = {
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
    split_counts = {split: len(records) for split, records in records_by_split.items()}
    total_records = sum(split_counts.values())
    canary_records = records_by_split.get("canaries", [])
    label_field = module_label_field
    labels = {
        True: sum(
            record.get("expected", {}).get(label_field) is True
            for records in records_by_split.values()
            for _, record in records
            if isinstance(record.get("expected"), dict)
        ),
        False: sum(
            record.get("expected", {}).get(label_field) is False
            for records in records_by_split.values()
            for _, record in records
            if isinstance(record.get("expected"), dict)
        ),
    }
    canary_kinds: list[str] = sorted(
        {
            cast(str, info.get("kind"))
            for _, record in canary_records
            if isinstance(info := record.get("canary_info"), dict)
            and isinstance(info.get("kind"), str)
        }
    )

    for baseline_path in spec.baseline_files:
        if baseline_path.suffix.lower() != ".json":
            continue
        baseline_file = REPO_ROOT / baseline_path
        try:
            baseline = _load_json(baseline_file)
        except (OSError, ValueError, json.JSONDecodeError) as exc:
            findings.append(
                AuditFinding("baseline-integrity", baseline_file, 0, "baseline", str(exc))
            )
            continue
        if not isinstance(baseline, dict):
            findings.append(
                AuditFinding(
                    "baseline-integrity",
                    baseline_file,
                    0,
                    "baseline",
                    "must be a JSON object",
                )
            )
            continue

        findings.extend(_baseline_fact_findings(baseline, baseline_file, spec))

        missing = sorted(baseline_required - set(baseline))
        if missing:
            findings.append(
                AuditFinding(
                    "baseline-integrity",
                    baseline_file,
                    0,
                    "schema",
                    "missing required fields: " + ", ".join(missing),
                )
            )
        if baseline.get("schema_version") != SUPPORTED_DATASET_SCHEMA_VERSION:
            findings.append(
                AuditFinding(
                    "baseline-integrity",
                    baseline_file,
                    0,
                    "schema_version",
                    f"must be integer {SUPPORTED_DATASET_SCHEMA_VERSION}",
                )
            )
        if not isinstance(baseline.get("module"), str) or not baseline["module"].strip():
            findings.append(
                AuditFinding(
                    "baseline-integrity",
                    baseline_file,
                    0,
                    "module",
                    "must be a non-empty logical module name",
                )
            )
        elif manifest is not None and baseline["module"] != manifest.module_name:
            findings.append(
                AuditFinding(
                    "baseline-integrity",
                    baseline_file,
                    0,
                    "module",
                    f"must match module.json module_name ({manifest.module_name!r})",
                )
            )
        if baseline.get("dataset_version") != meta.get("dataset_version"):
            findings.append(
                AuditFinding(
                    "baseline-integrity",
                    baseline_file,
                    0,
                    "dataset_version",
                    "baseline dataset_version must match metadata",
                )
            )
        if baseline.get("split_digests") != meta.get("split_digests"):
            findings.append(
                AuditFinding(
                    "baseline-integrity",
                    baseline_file,
                    0,
                    "split_digests",
                    "baseline split_digests must match metadata",
                )
            )
        if baseline.get("split_digests") != actual_digests:
            findings.append(
                AuditFinding(
                    "baseline-integrity",
                    baseline_file,
                    0,
                    "split_digests",
                    "baseline split_digests must match current split content",
                )
            )

        metric = baseline.get("metric")
        if not isinstance(metric, dict):
            findings.append(
                AuditFinding("baseline-integrity", baseline_file, 0, "metric", "must be an object")
            )
        else:
            for split in DECISION_SPLITS:
                fact = metric.get(split)
                if not isinstance(fact, dict):
                    findings.append(
                        AuditFinding(
                            "baseline-integrity",
                            baseline_file,
                            0,
                            f"metric.{split}",
                            "must be an object",
                        )
                    )
                    continue
                if fact.get("count") != split_counts.get(split, 0):
                    findings.append(
                        AuditFinding(
                            "baseline-integrity",
                            baseline_file,
                            0,
                            f"metric.{split}.count",
                            "must match the dataset record count",
                        )
                    )
                for field in ("mean", "std"):
                    value = fact.get(field)
                    if (
                        isinstance(value, bool)
                        or not isinstance(value, int | float)
                        or not math.isfinite(value)
                        or value < 0
                        or (field == "mean" and value > 1)
                    ):
                        findings.append(
                            AuditFinding(
                                "baseline-integrity",
                                baseline_file,
                                0,
                                f"metric.{split}.{field}",
                                "must be a finite non-negative metric value",
                            )
                        )

        dataset = baseline.get("dataset")
        if not isinstance(dataset, dict):
            findings.append(
                AuditFinding("baseline-integrity", baseline_file, 0, "dataset", "must be an object")
            )
        else:
            expected_dataset = {
                "records": total_records,
                "duplicate_ids": 0,
                "cross_split_leaks": 0,
                "label_true": labels[True],
                "label_false": labels[False],
                "canaries": len(canary_records),
            }
            for field, expected in expected_dataset.items():
                if dataset.get(field) != expected:
                    findings.append(
                        AuditFinding(
                            "baseline-integrity",
                            baseline_file,
                            0,
                            f"dataset.{field}",
                            f"must match current datasets ({expected!r})",
                        )
                    )

        canaries = baseline.get("canaries")
        if not isinstance(canaries, dict):
            findings.append(
                AuditFinding(
                    "baseline-integrity", baseline_file, 0, "canaries", "must be an object"
                )
            )
        else:
            baseline_failed = canaries.get("failed")
            if (
                isinstance(baseline_failed, bool)
                or not isinstance(baseline_failed, int)
                or not 0 <= baseline_failed <= len(canary_records)
            ):
                findings.append(
                    AuditFinding(
                        "baseline-integrity",
                        baseline_file,
                        0,
                        "canaries.failed",
                        "must be a non-negative count no greater than the canary total",
                    )
                )
                baseline_failed = -1
            checks: tuple[tuple[str, object], ...] = (
                ("total", len(canary_records)),
                ("kinds_present", canary_kinds),
                ("failed", baseline_failed),
            )
            for check_field, check_expected in checks:
                if canaries.get(check_field) != check_expected:
                    findings.append(
                        AuditFinding(
                            "baseline-integrity",
                            baseline_file,
                            0,
                            f"canaries.{check_field}",
                            f"must match current datasets ({check_expected!r})",
                        )
                    )
            coverage = canaries.get("taxonomy_coverage")
            if (
                isinstance(coverage, bool)
                or not isinstance(coverage, int | float)
                or not math.isfinite(coverage)
                or not 0 <= coverage <= 1
            ):
                findings.append(
                    AuditFinding(
                        "baseline-integrity",
                        baseline_file,
                        0,
                        "canaries.taxonomy_coverage",
                        "must be a finite value in [0, 1]",
                    )
                )

        tests = baseline.get("tests")
        if not isinstance(tests, dict):
            findings.append(
                AuditFinding("baseline-integrity", baseline_file, 0, "tests", "must be an object")
            )
        else:
            nodeids = tests.get("by_nodeid")
            if not isinstance(nodeids, dict) or not nodeids:
                findings.append(
                    AuditFinding(
                        "baseline-integrity",
                        baseline_file,
                        0,
                        "tests.by_nodeid",
                        "must contain module-scoped test timings",
                    )
                )
            else:
                for nodeid, duration in nodeids.items():
                    if not isinstance(nodeid, str) or not nodeid:
                        findings.append(
                            AuditFinding(
                                "baseline-integrity",
                                baseline_file,
                                0,
                                str(nodeid),
                                "baseline test nodeid must be a non-empty string",
                            )
                        )
                    if (
                        isinstance(duration, bool)
                        or not isinstance(duration, int | float)
                        or not math.isfinite(duration)
                        or duration < 0
                    ):
                        findings.append(
                            AuditFinding(
                                "baseline-integrity",
                                baseline_file,
                                0,
                                str(nodeid),
                                "test timing must be a finite non-negative number",
                            )
                        )
                if tests.get("count") != len(nodeids):
                    findings.append(
                        AuditFinding(
                            "baseline-integrity",
                            baseline_file,
                            0,
                            "tests.count",
                            "must equal tests.by_nodeid count",
                        )
                    )
            wall_seconds = tests.get("wall_seconds")
            if not isinstance(wall_seconds, dict) or set(wall_seconds) != {"p50", "p90", "max"}:
                findings.append(
                    AuditFinding(
                        "baseline-integrity",
                        baseline_file,
                        0,
                        "tests.wall_seconds",
                        "must contain p50, p90, and max",
                    )
                )

        drift_thresholds = baseline.get("drift_thresholds")
        if not isinstance(drift_thresholds, dict):
            findings.append(
                AuditFinding(
                    "baseline-integrity",
                    baseline_file,
                    0,
                    "drift_thresholds",
                    "must be an object",
                )
            )

    findings.extend(_frozen_content_findings(spec, meta))
    if findings:
        raise ModuleConformanceError(f"{spec.name}:\n" + "\n".join(f.format() for f in findings))


def validate_module(name: str) -> ModuleSpec:
    """Discover and validate one module's tracked shape and JSON files."""
    if name not in module_names():
        raise ModuleConformanceError(f"unknown module {name!r}")

    module_path = MODULES_DIR / name
    prefix = _module_prefix(name)
    tracked = _module_files(name, prefix)
    tracked_set = set(tracked)
    errors: list[str] = []

    required_files = ("__init__.py", "__main__.py", "architecture.md", "module.json")
    for filename in required_files:
        relative = prefix / filename
        if relative not in tracked_set or not _is_regular_file(REPO_ROOT / relative):
            errors.append(f"missing tracked regular file {relative}")

    if not _is_regular_file(module_path / "__init__.py"):
        errors.append(f"missing regular package marker {module_path / '__init__.py'}")
    tests_dir = module_path / "tests"
    if not tests_dir.is_dir() or tests_dir.is_symlink():
        errors.append(f"missing regular tests directory {tests_dir}")

    def files_in(*parts: str) -> tuple[Path, ...]:
        selected: list[Path] = []
        for path in tracked:
            try:
                relative = path.relative_to(prefix)
            except ValueError:
                continue
            if relative.parts[: len(parts)] == parts:
                selected.append(path)
        return tuple(sorted(selected))

    test_files = tuple(
        path
        for path in files_in("tests")
        if len(path.relative_to(prefix).parts) == 2
        and path.relative_to(prefix).parts[1].startswith("test_")
        and path.suffix == ".py"
        and _is_regular_file(REPO_ROOT / path)
    )
    if not test_files:
        errors.append(f"no tracked tests/test_*.py in {tests_dir}")

    baseline_files = tuple(
        path for path in files_in("tests", "baselines") if _is_regular_file(REPO_ROOT / path)
    )
    if not baseline_files or not any(path.suffix.lower() == ".json" for path in baseline_files):
        errors.append(f"no tracked regular baseline JSON in {tests_dir / 'baselines'}")

    dataset_files = tuple(
        path for path in files_in("datasets") if _is_regular_file(REPO_ROOT / path)
    )
    if not dataset_files or not any(
        path.suffix.lower() in {".json", ".jsonl"} for path in dataset_files
    ):
        errors.append(f"no tracked regular dataset JSON/JSONL in {module_path / 'datasets'}")

    python_files = tuple(path for path in tracked if path.suffix == ".py")
    for path in python_files:
        if not _is_regular_file(REPO_ROOT / path):
            errors.append(f"tracked Python file is not regular: {path}")

    if errors:
        raise ModuleConformanceError(f"{name}:\n" + "\n".join(errors))
    try:
        manifest = load_module_manifest(module_path, name)
    except ModuleBoundaryError as exc:
        raise ModuleConformanceError(f"{name}: invalid module.json: {exc}") from exc
    _validate_json_files(name, baseline_files, dataset_files)
    spec = ModuleSpec(
        name=name,
        path=module_path,
        tracked_files=tuple(sorted(tracked)),
        python_files=python_files,
        test_files=test_files,
        baseline_files=baseline_files,
        dataset_files=dataset_files,
        manifest=manifest,
    )
    _validate_dataset_integrity(spec, manifest)
    return spec


def _dataset_input(spec: ModuleSpec) -> dict[str, Any]:
    dataset_files = set(spec.dataset_files)
    dataset_dir = spec.path / "datasets"
    preferred = [
        dataset_dir / "eval.jsonl",
        dataset_dir / "train.jsonl",
    ]
    candidates = [path for path in preferred if path.relative_to(REPO_ROOT) in dataset_files]
    candidates.extend(
        REPO_ROOT / path
        for path in sorted(dataset_files)
        if path.suffix.lower() == ".jsonl" and REPO_ROOT / path not in candidates
    )
    for path in candidates:
        for line in path.read_text(encoding="utf-8").splitlines():
            if not line.strip():
                continue
            record = json.loads(line)
            if not isinstance(record, dict):
                raise ModuleConformanceError(f"{path}: first dataset object is not an object")
            value = record.get("input")
            if not isinstance(value, dict):
                raise ModuleConformanceError(f"{path}: first object input must be a JSON object")
            return value
    raise ModuleConformanceError(f"{spec.name}: no non-empty dataset JSONL available for CLI probe")


_SAFE_MODULE_ENV_KEYS = frozenset(
    {
        "LANG",
        "LC_ALL",
        "LC_CTYPE",
        "PATH",
        "TEMP",
        "TMP",
        "TMPDIR",
        "SystemRoot",
        "WINDIR",
    }
)


def _module_test_env() -> dict[str, str]:
    """Return the minimal inherited environment for module test processes."""
    return {
        key: value
        for key, value in os.environ.items()
        if key in _SAFE_MODULE_ENV_KEYS
        or (key.startswith("LC_") and not _SENSITIVE_ENV_RE.search(key))
    }


@contextmanager
def module_offline_environment() -> Iterator[dict[str, str]]:
    """Yield a credential-free environment with common network forms denied.

    The parent pytest process uses an audit hook, but audit hooks do not cross
    ``fork``/``exec``.  A temporary ``sitecustomize`` blocks Python socket
    clients and provider imports in normal child interpreters.  Command shims
    and ``subprocess.Popen`` checks reject common literal network clients.

    This offline guard is a BEST-EFFORT, deterministic lint-style check for
    common forms of accidental network use; it is not a security boundary. It
    CANNOT prevent a hostile same-UID module from bypassing the check with
    ``os.system``, ``posix_spawn``, raw sockets, subprocess monkey-patching, or
    by killing a same-UID tracer. The harness does not start such a tracer or
    provide an in-harness sandbox. Real containment is the deployment-layer
    boundary.
    """
    with tempfile.TemporaryDirectory(prefix="cambium-module-offline-") as root:
        offline_root = Path(root)
        provider_imports = repr(PROVIDER_IMPORTS)
        (offline_root / "sitecustomize.py").write_text(
            "import importlib.abc\n"
            "import os\n"
            "import shlex\n"
            "import shutil\n"
            "import socket\n"
            "import subprocess\n"
            "import sys\n"
            "\n"
            f"_PROVIDERS = {provider_imports}\n"
            "_REQUIRED_ENV = {key: os.environ[key] for key in (\n"
            "    'CAMBIUM_MODULE_OFFLINE', 'PATH', 'PYTHONPATH'\n"
            ") if key in os.environ}\n"
            "_NETWORK_CLIENTS = frozenset((\n"
            "    'curl', 'wget', 'http', 'https', 'nc', 'netcat', 'ncat', 'ssh'\n"
            "))\n"
            "_PROBE_LOG = os.environ.get('CAMBIUM_MODULE_PROBE_IMPORT_LOG')\n"
            "\n"
            "class _ProviderBlocker(importlib.abc.MetaPathFinder):\n"
            "    def find_spec(self, fullname, path=None, target=None):\n"
            "        if any(fullname == root or fullname.startswith(root + '.') "
            "for root in _PROVIDERS):\n"
            "            raise ModuleNotFoundError(\n"
            "                'provider import blocked by module conformance: ' + fullname\n"
            "            )\n"
            "        return None\n"
            "\n"
            "class _ProbeImporter(importlib.abc.MetaPathFinder):\n"
            "    def find_spec(self, fullname, path=None, target=None):\n"
            "        if _PROBE_LOG and fullname.startswith('cambium.modules.'):\n"
            "            try:\n"
            "                with open(_PROBE_LOG, 'a', encoding='utf-8') as handle:\n"
            "                    handle.write(fullname + '\\n')\n"
            "            except OSError:\n"
            "                pass\n"
            "        return None\n"
            "\n"
            "def _deny_network(*args, **kwargs):\n"
            "    raise PermissionError('network access is forbidden during module conformance')\n"
            "\n"
            "def _resolved_command(token):\n"
            "    located = token if os.path.dirname(token) else shutil.which(token)\n"
            "    return os.path.realpath(located) if located else os.path.realpath(token)\n"
            "\n"
            "def _command_tokens(args, executable=None, shell=False):\n"
            "    if shell:\n"
            "        command = args[0] if isinstance(args, (list, tuple)) and args else args\n"
            "        command = os.fsdecode(command)\n"
            "        try:\n"
            "            tokens = shlex.split(command)\n"
            "        except ValueError:\n"
            "            tokens = command.split()\n"
            "        return [token.strip('\\\"\\';&|()') for token in tokens]\n"
            "    if isinstance(args, (list, tuple)):\n"
            "        values = list(args)\n"
            "        if executable is not None:\n"
            "            if values:\n"
            "                values[0] = executable\n"
            "            else:\n"
            "                values.append(executable)\n"
            "        tokens = [os.fsdecode(value) for value in values "
            "if isinstance(value, (str, bytes, os.PathLike))]\n"
            "        if tokens:\n"
            "            tokens[0] = _resolved_command(tokens[0])\n"
            "        return tokens\n"
            "    command = os.fsdecode(args)\n"
            "    try:\n"
            "        tokens = shlex.split(command)\n"
            "    except ValueError:\n"
            "        tokens = command.split()\n"
            "    if executable is not None and tokens:\n"
            "        tokens[0] = _resolved_command(os.fsdecode(executable))\n"
            "    return [token.strip('\\\"\\';&|()') for token in tokens]\n"
            "\n"
            "def _network_executable(tokens, executable=None):\n"
            "    if executable is not None:\n"
            "        resolved = _resolved_command(os.fsdecode(executable))\n"
            "        if (os.path.basename(os.fsdecode(executable)) in _NETWORK_CLIENTS or\n"
            "                os.path.basename(resolved) in _NETWORK_CLIENTS):\n"
            "            return resolved\n"
            "    for token in tokens:\n"
            "        resolved = _resolved_command(token)\n"
            "        if (os.path.basename(token) in _NETWORK_CLIENTS or\n"
            "                os.path.basename(resolved) in _NETWORK_CLIENTS):\n"
            "            return resolved\n"
            "    for index, token in enumerate(tokens):\n"
            "        if os.path.basename(token).startswith('python') and any(\n"
            "            'urllib' in argument for argument in tokens[index + 1:]\n"
            "        ):\n"
            "            return _resolved_command(token) + ' (urllib)'\n"
            "    return None\n"
            "\n"
            "def _unsafe_python_flag(tokens):\n"
            "    for index, token in enumerate(tokens):\n"
            "        if not os.path.basename(token).startswith('python'):\n"
            "            continue\n"
            "        arguments = tokens[index + 1:]\n"
            "        argument_index = 0\n"
            "        while argument_index < len(arguments):\n"
            "            argument = arguments[argument_index]\n"
            "            if argument == '--' or not argument.startswith('-'):\n"
            "                break\n"
            "            if argument in ('-W', '-X', '-c', '-m'):\n"
            "                argument_index += 2\n"
            "                continue\n"
            "            if argument.startswith(('-W', '-X', '-c', '-m')):\n"
            "                argument_index += 1\n"
            "                continue\n"
            "            if not argument.startswith('--') and any(\n"
            "                flag in argument[1:] for flag in 'ESI'\n"
            "            ):\n"
            "                return argument\n"
            "            argument_index += 1\n"
            "    return None\n"
            "\n"
            "_popen_init = subprocess.Popen.__init__\n"
            "def _offline_popen(self, args, *pargs, **kwargs):\n"
            "    command_executable = kwargs.get('executable')\n"
            "    tokens = _command_tokens(args, command_executable, kwargs.get('shell', False))\n"
            "    network_executable = _network_executable(tokens, command_executable)\n"
            "    if network_executable:\n"
            "        raise PermissionError(\n"
            "            'network client denied during module conformance: ' + network_executable\n"
            "        )\n"
            "    unsafe_flag = _unsafe_python_flag(tokens)\n"
            "    if unsafe_flag:\n"
            "        raise PermissionError(\n"
            "            'isolated Python flag denied during module conformance: ' + unsafe_flag\n"
            "        )\n"
            "    child_env = dict(kwargs.get('env') or os.environ)\n"
            "    child_env.update(_REQUIRED_ENV)\n"
            "    kwargs['env'] = child_env\n"
            "    return _popen_init(self, args, *pargs, **kwargs)\n"
            "\n"
            "sys.meta_path.insert(0, _ProviderBlocker())\n"
            "sys.meta_path.insert(1, _ProbeImporter())\n"
            "subprocess.Popen.__init__ = _offline_popen\n"
            "socket.socket.connect = _deny_network\n"
            "socket.socket.connect_ex = _deny_network\n"
            "socket.socket.sendto = _deny_network\n"
            "socket.create_connection = _deny_network\n",
            encoding="utf-8",
        )
        command_dir = offline_root / "bin"
        command_dir.mkdir()
        for command in ("curl", "wget", "http", "https", "nc", "netcat", "ncat", "ssh"):
            wrapper = command_dir / command
            wrapper.write_text(
                "#!/bin/sh\n"
                "printf '%s\\n' 'network client denied during module conformance' >&2\n"
                "exit 126\n",
                encoding="utf-8",
            )
            wrapper.chmod(0o755)
        env = _module_test_env()
        env["CAMBIUM_MODULE_OFFLINE"] = "1"
        env["PATH"] = f"{command_dir}{os.pathsep}{env.get('PATH', os.defpath)}"
        env["PYTHONPATH"] = os.pathsep.join((str(offline_root), str(PACKAGE_ROOT.parent)))
        yield env


def probe_module_cli(spec: ModuleSpec) -> None:
    """Run the module CLI from an empty cwd.

    The CLI subprocess receives the source tree through ``PYTHONPATH``: the
    offline environment injects the checkout's ``src`` directory.  The CLI
    subprocess also runs the sibling/import runtime check: every
    ``cambium.modules.*`` import it performs is recorded, and a sibling
    decision package loaded inside the probe fails the gate.
    """
    cli_module = spec.manifest.cli_module if spec.manifest is not None else spec.package_name
    payload = json.dumps(_dataset_input(spec), separators=(",", ":")) + "\n"
    command = [sys.executable, "-m", cli_module]
    loaded_imports: set[str] = set()
    try:
        with (
            module_offline_environment() as env,
            tempfile.TemporaryDirectory(prefix="cambium-module-") as cwd,
        ):
            import_log = Path(cwd) / "probe-imports.log"
            env["CAMBIUM_MODULE_PROBE_IMPORT_LOG"] = str(import_log)
            result = subprocess.run(
                command,
                cwd=cwd,
                env=env,
                input=payload,
                capture_output=True,
                text=True,
                check=False,
                timeout=10,
            )
            loaded_imports = _probe_loaded_imports(import_log)
    except subprocess.TimeoutExpired as exc:
        raise ModuleConformanceError(f"{spec.name}: JSON CLI timed out after 10 seconds") from exc
    except OSError as exc:
        raise ModuleConformanceError(f"{spec.name}: JSON CLI could not start: {exc}") from exc

    if result.returncode != 0:
        detail = result.stderr.strip() or "no stderr diagnostics"
        raise ModuleConformanceError(f"{spec.name}: JSON CLI exited {result.returncode}: {detail}")
    if not result.stdout.endswith("\n") or result.stdout[:-1].endswith("\n"):
        raise ModuleConformanceError(
            f"{spec.name}: JSON CLI stdout must contain one object and one trailing newline"
        )
    try:
        value, end = json.JSONDecoder().raw_decode(result.stdout[:-1])
    except json.JSONDecodeError as exc:
        raise ModuleConformanceError(
            f"{spec.name}: JSON CLI stdout is not one JSON object: {exc}"
        ) from exc
    if end != len(result.stdout) - 1:
        raise ModuleConformanceError(
            f"{spec.name}: JSON CLI stdout contains extra output after its JSON object"
        )
    if not isinstance(value, dict):
        raise ModuleConformanceError(f"{spec.name}: JSON CLI stdout must be a JSON object")
    if "error" in value:
        raise ModuleConformanceError(
            f"{spec.name}: JSON CLI returned an error-shaped object with exit 0"
        )
    label_field = spec.manifest.label_field if spec.manifest is not None else "decompose"
    expected_fields = {"confidence", "reason", label_field}
    if set(value) != expected_fields:
        raise ModuleConformanceError(
            f"{spec.name}: JSON CLI decision fields must be exactly "
            f"{sorted(expected_fields)!r}, got {sorted(value)!r}"
        )
    label = value.get(label_field)
    if not isinstance(label, bool):
        raise ModuleConformanceError(
            f"{spec.name}: JSON CLI decision field {label_field!r} must be boolean"
        )
    reason = value.get("reason")
    if not isinstance(reason, str):
        raise ModuleConformanceError(f"{spec.name}: JSON CLI reason must be a string")
    confidence = value.get("confidence")
    if (
        isinstance(confidence, bool)
        or not isinstance(confidence, int | float)
        or not math.isfinite(confidence)
        or not 0 <= confidence <= 1
    ):
        raise ModuleConformanceError(
            f"{spec.name}: JSON CLI confidence must be a finite number in [0, 1]"
        )
    _check_probe_siblings(spec, loaded_imports)


def _probe_loaded_imports(import_log: Path) -> set[str]:
    """Return the unique ``cambium.modules.*`` imports recorded by a probe."""
    try:
        if not import_log.is_file():
            return set()
        return {
            line.strip()
            for line in import_log.read_text(encoding="utf-8").splitlines()
            if line.strip()
        }
    except OSError:
        return set()


def _check_probe_siblings(spec: ModuleSpec, loaded_imports: set[str]) -> None:
    """Fail when the CLI probe subprocess loaded a sibling decision package."""
    siblings = sorted(
        name
        for name in loaded_imports
        if (child := name.removeprefix("cambium.modules.").split(".")[0]) in module_names()
        and child != spec.name
    )
    if siblings:
        raise ModuleConformanceError(
            f"{spec.name}: sibling modules loaded inside the JSON CLI probe: " + ", ".join(siblings)
        )


def _evaluate_module_predictions(spec: ModuleSpec) -> None:
    """Execute each split through the module CLI and enforce live quality gates."""
    if spec.manifest is None:
        raise ModuleConformanceError(f"{spec.name}: module manifest is required for evaluation")
    cli_module = spec.manifest.cli_module
    split_results: dict[str, list[float]] = {}
    loaded_imports: set[str] = set()
    try:
        with (
            module_offline_environment() as env,
            tempfile.TemporaryDirectory(prefix="cambium-module-evaluate-") as cwd,
        ):
            import_log = Path(cwd) / "evaluate-imports.log"
            env["CAMBIUM_MODULE_PROBE_IMPORT_LOG"] = str(import_log)
            for split, filename in DECISION_SPLITS.items():
                path = spec.path / "datasets" / filename
                try:
                    records = _load_jsonl_records(path)
                except OSError as exc:
                    raise ModuleConformanceError(
                        f"{spec.name}: cannot read {split} for live evaluation: {exc}"
                    ) from exc
                if split == "canaries" and not records:
                    raise ModuleConformanceError(
                        f"{spec.name}: live canary evaluation has no records"
                    )
                payload = (
                    json.dumps(
                        {"operation": "evaluate", "records": [record for _, record in records]},
                        separators=(",", ":"),
                    )
                    + "\n"
                )
                try:
                    result = subprocess.run(
                        [sys.executable, "-m", cli_module],
                        cwd=cwd,
                        env=env,
                        input=payload,
                        capture_output=True,
                        text=True,
                        check=False,
                        timeout=30,
                    )
                except subprocess.TimeoutExpired as exc:
                    raise ModuleConformanceError(
                        f"{spec.name}: live {split} evaluation timed out after 30 seconds"
                    ) from exc
                except OSError as exc:
                    raise ModuleConformanceError(
                        f"{spec.name}: live {split} evaluation could not start: {exc}"
                    ) from exc
                if result.returncode != 0:
                    detail = result.stderr.strip() or "no stderr diagnostics"
                    raise ModuleConformanceError(
                        f"{spec.name}: live {split} evaluation exited {result.returncode}: {detail}"
                    )
                if not result.stdout.endswith("\n") or result.stdout[:-1].endswith("\n"):
                    raise ModuleConformanceError(
                        f"{spec.name}: live {split} evaluation must emit one trailing newline"
                    )
                try:
                    value, end = json.JSONDecoder().raw_decode(result.stdout[:-1])
                except json.JSONDecodeError as exc:
                    raise ModuleConformanceError(
                        f"{spec.name}: live {split} evaluation is not one JSON object: {exc}"
                    ) from exc
                if end != len(result.stdout) - 1:
                    raise ModuleConformanceError(
                        f"{spec.name}: live {split} evaluation contains extra output"
                    )
                if not isinstance(value, dict) or "error" in value:
                    raise ModuleConformanceError(
                        f"{spec.name}: live {split} evaluation returned an error-shaped object"
                    )
                if set(value) != {"results"} or not isinstance(value["results"], list):
                    raise ModuleConformanceError(
                        f"{spec.name}: live {split} evaluation must return a results array"
                    )
                results = value["results"]
                if len(results) != len(records):
                    raise ModuleConformanceError(
                        f"{spec.name}: live {split} evaluation returned {len(results)} "
                        f"results for {len(records)} records"
                    )
                scores: list[float] = []
                label_field = spec.manifest.label_field
                for index, item in enumerate(results):
                    if not isinstance(item, dict) or set(item) != {"prediction", "score"}:
                        raise ModuleConformanceError(
                            f"{spec.name}: live {split} result {index} has wrong schema"
                        )
                    prediction = item["prediction"]
                    if (
                        not isinstance(prediction, dict)
                        or "error" in prediction
                        or set(prediction) != {"confidence", "reason", label_field}
                        or not isinstance(prediction.get(label_field), bool)
                        or not isinstance(prediction.get("reason"), str)
                    ):
                        raise ModuleConformanceError(
                            f"{spec.name}: live {split} prediction {index} has wrong schema"
                        )
                    score = item["score"]
                    if (
                        isinstance(score, bool)
                        or not isinstance(score, int | float)
                        or not math.isfinite(score)
                        or not 0 <= score <= 1
                    ):
                        raise ModuleConformanceError(
                            f"{spec.name}: live {split} score {index} is not in [0, 1]"
                        )
                    scores.append(float(score))
                split_results[split] = scores
            loaded_imports = _probe_loaded_imports(import_log)
    except ModuleConformanceError:
        raise
    _check_probe_siblings(spec, loaded_imports)

    baseline_file = next(
        (REPO_ROOT / path for path in spec.baseline_files if path.suffix.lower() == ".json"),
        None,
    )
    if baseline_file is None:
        raise ModuleConformanceError(f"{spec.name}: baseline is required for live evaluation")
    try:
        baseline = _load_json(baseline_file)
    except (OSError, ValueError, json.JSONDecodeError) as exc:
        raise ModuleConformanceError(
            f"{spec.name}: cannot read baseline for live evaluation: {exc}"
        ) from exc
    if not isinstance(baseline, dict):
        raise ModuleConformanceError(f"{spec.name}: baseline must be an object for live evaluation")
    thresholds = baseline.get("drift_thresholds")
    baseline_metric = baseline.get("metric")
    baseline_canaries = baseline.get("canaries")
    if not isinstance(thresholds, dict) or not isinstance(baseline_metric, dict):
        raise ModuleConformanceError(f"{spec.name}: baseline quality gates are unavailable")
    metric_delta = thresholds.get("metric_mean_delta")
    canary_delta = thresholds.get("canary_failed_delta")
    if (
        isinstance(metric_delta, bool)
        or not isinstance(metric_delta, int | float)
        or not math.isfinite(metric_delta)
        or metric_delta < 0
        or isinstance(canary_delta, bool)
        or not isinstance(canary_delta, int)
        or canary_delta < 0
        or not isinstance(baseline_canaries, dict)
    ):
        raise ModuleConformanceError(f"{spec.name}: baseline quality gates are invalid")
    findings: list[str] = []
    for split, scores in split_results.items():
        fact = baseline_metric.get(split)
        if not isinstance(fact, dict):
            findings.append(f"metric.{split}: baseline metric is unavailable")
            continue
        baseline_mean = fact.get("mean")
        if (
            isinstance(baseline_mean, bool)
            or not isinstance(baseline_mean, int | float)
            or not math.isfinite(baseline_mean)
        ):
            findings.append(f"metric.{split}: baseline mean is invalid")
            continue
        mean = sum(scores) / len(scores) if scores else float("nan")
        if not math.isfinite(mean) or mean < baseline_mean - metric_delta:
            findings.append(
                f"metric.{split}: live mean {mean!r} below baseline {baseline_mean!r} "
                f"minus allowed delta {metric_delta!r}"
            )
        if split == "canaries":
            failed = sum(score < 1.0 for score in scores)
            baseline_failed = baseline_canaries.get("failed")
            if (
                isinstance(baseline_failed, bool)
                or not isinstance(baseline_failed, int)
                or baseline_failed < 0
                or failed > baseline_failed + canary_delta
            ):
                findings.append(
                    f"canaries.failed: live {failed} exceeds baseline "
                    f"{baseline_failed!r} plus allowed delta {canary_delta!r}"
                )
    if findings:
        raise ModuleConformanceError(
            f"{spec.name}: live quality gates failed:\n" + "\n".join(findings)
        )


def _relative_package(path: Path, spec: ModuleSpec) -> list[str]:
    relative = path.relative_to(spec.path)
    package = ["cambium", "modules", spec.name]
    current = spec.path
    for directory in relative.parts[:-1]:
        current /= directory
        if _is_regular_file(current / "__init__.py"):
            package.append(directory)
        else:
            break
    return package


def _relative_import_target(
    path: Path, spec: ModuleSpec, level: int, module: str | None
) -> list[str]:
    package = _relative_package(path, spec)
    base_length = len(package) - level + 1
    if base_length < 0:
        return []
    target = package[:base_length]
    if module:
        target.extend(module.split("."))
    return target


def _target_name(parts: list[str]) -> str:
    return ".".join(parts)


def _is_sibling_target(target: str, spec: ModuleSpec) -> bool:
    prefix = "cambium.modules."
    if not target.startswith(prefix):
        return False
    child = target[len(prefix) :].split(".")[0]
    return child in module_names() and child != spec.name


def _is_harness_target(target: str, spec: ModuleSpec) -> bool:
    """Reject cambium harness imports that are not the shared base or own package.

    A decision module may import the shared module base, its own package, and
    the enclosing package markers; importing any other ``cambium.*`` module
    (``cambium.supervisor``, ``cambium.routing``, ...) crosses the module
    boundary and is a static gate failure.
    """
    if not target.startswith("cambium."):
        return False
    if target == "cambium.modules":
        return False
    if target.startswith("cambium.modules."):
        child = target[len("cambium.modules.") :].split(".")[0]
        return child not in {spec.name, "base"}
    return True


def _literal_import_target(node: ast.Call) -> str | None:
    """Return a dynamic-import target only when it is a literal string.

    ``None`` means the call cannot be statically resolved (variable, keyword
    override, or missing argument); callers fail closed on it.
    """
    if node.args and isinstance(node.args[0], ast.Constant) and isinstance(node.args[0].value, str):
        return node.args[0].value
    for keyword in node.keywords:
        if keyword.arg == "name":
            if isinstance(keyword.value, ast.Constant) and isinstance(keyword.value.value, str):
                return keyword.value.value
            return None
    return None


def _is_module_test_file(path: Path, spec: ModuleSpec) -> bool:
    """Return whether *path* lives under the module's colocated tests dir."""
    try:
        path.resolve().relative_to(spec.tests_dir.resolve())
        return True
    except ValueError:
        return False


def _check_import_target(
    target: str,
    path: Path,
    node: ast.Import | ast.ImportFrom | ast.Call,
    spec: ModuleSpec,
) -> str | None:
    if _is_provider_import(target):
        return f"{path}:{node.lineno}: provider import is forbidden: {target}"
    if _is_sibling_target(target, spec):
        return f"{path}:{node.lineno}: sibling import is forbidden: {target}"
    if _is_harness_target(target, spec) and not _is_module_test_file(path, spec):
        return f"{path}:{node.lineno}: cambium harness import is forbidden: {target}"
    return None


def _scan_python_file(path: Path, spec: ModuleSpec) -> list[str]:
    try:
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    except (OSError, SyntaxError) as exc:
        return [f"{path}: cannot parse tracked Python file: {exc}"]

    importlib_names = {"importlib"}
    builtin_module_names = {"builtins"}
    import_module_names = {"import_module"}
    builtin_import_names = {"__import__"}
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                if alias.name == "importlib":
                    importlib_names.add(alias.asname or "importlib")
                if alias.name == "builtins":
                    builtin_module_names.add(alias.asname or "builtins")
        elif isinstance(node, ast.ImportFrom):
            if node.module == "importlib":
                for alias in node.names:
                    if alias.name == "import_module":
                        import_module_names.add(alias.asname or alias.name)
            if node.module == "builtins":
                for alias in node.names:
                    if alias.name == "__import__":
                        builtin_import_names.add(alias.asname or alias.name)

    issues: list[str] = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                issue = _check_import_target(alias.name, path, node, spec)
                if issue:
                    issues.append(issue)
        elif isinstance(node, ast.ImportFrom):
            if node.level:
                target_parts = _relative_import_target(path, spec, node.level, node.module)
                if node.module:
                    issue = _check_import_target(_target_name(target_parts), path, node, spec)
                    if issue:
                        issues.append(issue)
                else:
                    for alias in node.names:
                        target = _target_name([*target_parts, alias.name])
                        issue = _check_import_target(target, path, node, spec)
                        if issue:
                            issues.append(issue)
            elif node.module:
                issue = _check_import_target(node.module, path, node, spec)
                if issue:
                    issues.append(issue)
                for alias in node.names:
                    issue = _check_import_target(f"{node.module}.{alias.name}", path, node, spec)
                    if issue:
                        issues.append(issue)
        elif isinstance(node, ast.Call):
            function = node.func
            is_import_call = isinstance(function, ast.Name) and (
                function.id in import_module_names or function.id in builtin_import_names
            )
            if isinstance(function, ast.Attribute) and function.attr == "import_module":
                is_import_call = (
                    isinstance(function.value, ast.Name) and function.value.id in importlib_names
                )
            if isinstance(function, ast.Attribute) and function.attr == "__import__":
                is_import_call = isinstance(function.value, ast.Name) and function.value.id in {
                    *builtin_module_names,
                }
            if not is_import_call:
                continue
            literal_target = _literal_import_target(node)
            if literal_target is None:
                issues.append(
                    f"{path}:{node.lineno}: dynamic import with a non-literal target "
                    "is forbidden (fail closed)"
                )
                continue
            issue = _check_import_target(literal_target, path, node, spec)
            if issue:
                issues.append(issue)
    return issues


def scan_module_imports(spec: ModuleSpec) -> None:
    """Reject sibling/provider imports and syntax errors in tracked Python files."""
    issues = [
        issue for path in spec.python_files for issue in _scan_python_file(REPO_ROOT / path, spec)
    ]
    if issues:
        raise ModuleConformanceError(f"{spec.name}:\n" + "\n".join(issues))


def _reverse_scan_paths() -> tuple[Path, ...]:
    """Return the production-harness and repository-tooling Python scope."""
    paths = [
        path
        for path in sorted(PACKAGE_ROOT.glob("*.py"))
        if path.name
        not in {
            "module_conformance.py",
            "cli.py",
            # optimize.py is the sanctioned in-process DSPy consumer, like cli.py
            # for module-test; it must import the module's dspy_program at runtime.
            "optimize.py",
            # extract_pi.py is optional training-data tooling and intentionally
            # uses the reference module to infer candidate labels.
            "extract_pi.py",
        }
    ]
    for directory_name in ("scripts", "tools") if _repository_available() else ():
        directory = REPO_ROOT / directory_name
        if directory.is_dir():
            paths.extend(
                sorted(
                    path
                    for path in directory.rglob("*.py")
                    if _is_regular_file(path) and path != REPO_ROOT / "scripts" / "extract_pi.py"
                )
            )
    return tuple(dict.fromkeys(paths))


def _reverse_enclosing_symbol(node: ast.AST, parents: dict[ast.AST, ast.AST]) -> str:
    current = parents.get(node)
    while current is not None:
        if isinstance(current, ast.FunctionDef | ast.AsyncFunctionDef | ast.ClassDef):
            return current.name
        current = parents.get(current)
    return "<module>"


def _reverse_target(parts: list[str], names: set[str]) -> tuple[str, str] | None:
    if len(parts) < 3 or parts[:2] != ["cambium", "modules"]:
        return None
    package = parts[2]
    if package not in names:
        return None
    return package, ".".join(parts[:3])


def _reverse_relative_target(node: ast.ImportFrom, path: Path) -> list[str] | None:
    """Resolve relative imports from top-level ``cambium`` modules."""
    try:
        relative = path.relative_to(REPO_ROOT / "src" / "cambium")
    except ValueError:
        return None
    if relative.parent != Path("."):
        return None
    package = ["cambium"]
    remove = node.level - 1
    if remove > len(package):
        return None
    target = package[: len(package) - remove]
    if node.module:
        target.extend(node.module.split("."))
    return target


def _reverse_importlib_aliases(tree: ast.AST) -> tuple[set[str], set[str], set[str]]:
    importlib_names = {"importlib"}
    import_module_names = {"import_module"}
    builtin_import_names = {"__import__"}
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                if alias.name == "importlib":
                    importlib_names.add(alias.asname or "importlib")
                if alias.name == "builtins":
                    builtin_import_names.add(alias.asname or "builtins")
        elif isinstance(node, ast.ImportFrom) and node.level == 0:
            if node.module == "importlib":
                for alias in node.names:
                    if alias.name == "import_module":
                        import_module_names.add(alias.asname or "import_module")
            if node.module == "builtins":
                for alias in node.names:
                    if alias.name == "__import__":
                        builtin_import_names.add(alias.asname or "__import__")
    return importlib_names, import_module_names, builtin_import_names


def _reverse_importlib_targets(node: ast.expr, names: set[str]) -> tuple[tuple[str, str], ...]:
    if isinstance(node, ast.Constant) and isinstance(node.value, str):
        target = _reverse_target(node.value.split("."), names)
        return (target,) if target else ()
    if isinstance(node, ast.JoinedStr):
        prefix = ""
        for value in node.values:
            if isinstance(value, ast.Constant) and isinstance(value.value, str):
                prefix += value.value
            else:
                break
        if prefix == "cambium.modules.":
            return tuple((name, f"cambium.modules.{name}") for name in sorted(names))
    return ()


def scan_reverse_imports() -> tuple[AuditFinding, ...]:
    """Enumerate every concrete reverse import in harness and tooling scope."""
    names = set(module_names())
    findings: list[AuditFinding] = []
    for path in _reverse_scan_paths():
        try:
            tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        except (OSError, SyntaxError) as exc:
            findings.append(
                AuditFinding("reverse-import", path, 0, "ast.parse", f"cannot parse file: {exc}")
            )
            continue
        parents: dict[ast.AST, ast.AST] = {}
        for parent in ast.walk(tree):
            for child in ast.iter_child_nodes(parent):
                parents[child] = parent
        importlib_names, import_module_names, builtin_import_names = _reverse_importlib_aliases(
            tree
        )
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                for alias in node.names:
                    target = _reverse_target(alias.name.split("."), names)
                    if target is not None:
                        findings.append(
                            AuditFinding(
                                "reverse-import",
                                path.relative_to(REPO_ROOT),
                                node.lineno,
                                alias.asname or alias.name,
                                f"imports decision package {target[1]}",
                            )
                        )
                continue
            if isinstance(node, ast.ImportFrom):
                targets: list[tuple[str, str]] = []
                if node.level == 0 and node.module:
                    direct = _reverse_target(node.module.split("."), names)
                    if direct is not None:
                        targets.append(direct)
                    elif node.module == "cambium.modules":
                        targets.extend(
                            target
                            for alias in node.names
                            if (
                                target := _reverse_target(["cambium", "modules", alias.name], names)
                            )
                            is not None
                        )
                elif node.level:
                    relative = _reverse_relative_target(node, path)
                    if relative is not None:
                        target = _reverse_target(relative, names)
                        if target is not None:
                            targets.append(target)
                for target in targets:
                    for alias in node.names:
                        findings.append(
                            AuditFinding(
                                "reverse-import",
                                path.relative_to(REPO_ROOT),
                                node.lineno,
                                alias.asname or alias.name,
                                f"imports decision package {target[1]}",
                            )
                        )
                continue
            if not isinstance(node, ast.Call):
                continue
            function = node.func
            is_dynamic_import = isinstance(function, ast.Name) and (
                function.id in import_module_names or function.id in builtin_import_names
            )
            if isinstance(function, ast.Attribute) and function.attr == "import_module":
                is_dynamic_import = (
                    isinstance(function.value, ast.Name) and function.value.id in importlib_names
                )
            if isinstance(function, ast.Attribute) and function.attr == "__import__":
                is_dynamic_import = (
                    isinstance(function.value, ast.Name)
                    and function.value.id in builtin_import_names
                )
            if not is_dynamic_import:
                continue
            symbol = _reverse_enclosing_symbol(node, parents)
            relative_path = path.relative_to(REPO_ROOT)
            if not node.args:
                findings.append(
                    AuditFinding(
                        "reverse-import",
                        relative_path,
                        node.lineno,
                        symbol,
                        "dynamic import with no target is forbidden (fail closed)",
                    )
                )
                continue
            resolved = _reverse_importlib_targets(node.args[0], names)
            if resolved:
                for _, resolved_target in resolved:
                    findings.append(
                        AuditFinding(
                            "reverse-import",
                            relative_path,
                            node.lineno,
                            symbol,
                            f"dynamic import loads decision package {resolved_target}",
                        )
                    )
            elif _literal_import_target(node) is None:
                findings.append(
                    AuditFinding(
                        "reverse-import",
                        relative_path,
                        node.lineno,
                        symbol,
                        "dynamic import with a non-literal target may load a "
                        "decision package (fail closed)",
                    )
                )
    return tuple(
        sorted(findings, key=lambda finding: (str(finding.path), finding.line, finding.symbol))
    )


def scan_external_module_files() -> tuple[AuditFinding, ...]:
    """Find module-specific test/data/generator files outside their package."""
    findings: list[AuditFinding] = []
    paths = {relative: REPO_ROOT / relative for relative in _git_ls_files(".")}
    for directory_name in ("scripts", "tools", "tests"):
        directory = REPO_ROOT / directory_name
        if not directory.is_dir():
            continue
        for path in directory.rglob("*"):
            if _is_regular_file(path):
                paths[path.relative_to(REPO_ROOT)] = path
    for relative, path in sorted(paths.items(), key=lambda item: item[0].as_posix()):
        lower = relative.as_posix().lower()
        if lower in {
            "src/cambium/module_conformance.py",
            "src/cambium/cli.py",
            "tests/scenarios/test_module_conformance.py",
            # These scenarios intentionally import the example DSPy program and
            # optimizer; DSPy pulls openai into sys.modules, so they cannot run inside
            # the isolated module gate.
            "tests/scenarios/test_dspy_program.py",
            "tests/scenarios/test_optimize.py",
            # The optional transcript extractor intentionally uses the
            # reference module to infer candidate labels.
            "scripts/extract_pi.py",
        }:
            continue
        if not lower.startswith(("scripts/", "tools/", "tests/")):
            continue
        try:
            text = path.read_text(encoding="utf-8")
        except (OSError, UnicodeDecodeError):
            continue
        for module_name in module_names():
            if path.suffix == ".py":
                reference = re.compile(
                    rf"^[ \t]*(?:from|import)[ \t]+cambium\.modules\.{re.escape(module_name)}\b",
                    re.MULTILINE,
                )
            else:
                reference = re.compile(rf"(?<![\w.])cambium\.modules\.{re.escape(module_name)}\b")
            match = reference.search(text)
            if match is None:
                continue
            line = text.count("\n", 0, match.start()) + 1
            package_dir = REPO_ROOT / "src" / "cambium" / "modules" / module_name
            if package_dir in path.parents:
                continue
            findings.append(
                AuditFinding(
                    "layout",
                    relative,
                    line,
                    module_name,
                    f"module-specific file references cambium.modules.{module_name} outside "
                    f"{package_dir.relative_to(REPO_ROOT).as_posix()}/",
                )
            )
    return tuple(
        sorted(findings, key=lambda finding: (str(finding.path), finding.line, finding.symbol))
    )


class ProviderImportBlocker:
    """Meta-path finder that fails closed on provider imports."""

    def find_spec(self, fullname: str, path: Any = None, target: Any = None) -> Any:
        if _is_provider_import(fullname):
            raise ModuleNotFoundError(f"provider import blocked by module conformance: {fullname}")
        return None


def _install_provider_blocker() -> None:
    if not any(isinstance(finder, ProviderImportBlocker) for finder in sys.meta_path):
        sys.meta_path.insert(0, ProviderImportBlocker())


def _install_socket_audit_hook() -> None:
    global _AUDIT_HOOK_INSTALLED
    if _AUDIT_HOOK_INSTALLED:
        return

    def audit(event: str, args: tuple[Any, ...]) -> None:
        if event == "socket.connect":
            raise PermissionError("socket.connect is forbidden during module conformance")

    sys.addaudithook(audit)
    _AUDIT_HOOK_INSTALLED = True


def _loaded_siblings(module_name: str) -> list[str]:
    names = []
    for name in sys.modules:
        if not name.startswith("cambium.modules."):
            continue
        child = name.removeprefix("cambium.modules.").split(".")[0]
        if child in module_names() and child != module_name:
            names.append(name)
    return sorted(names)


def _loaded_providers() -> list[str]:
    return sorted(name for name in sys.modules if _is_provider_import(name))


class ModuleConformancePlugin:
    """Enforce one module's complete gate inside one pytest process."""

    def __init__(self, config: pytest.Config) -> None:
        self.config = config
        self.name = config.getoption("cambium_isolated_module")
        self.regen_baseline = config.getoption("cambium_regen_baseline")
        self.spec: ModuleSpec | None = None
        self.reports: dict[str, str] = {}
        self.timings: dict[str, float] = {}
        self.failures: list[str] = []
        self.siblings_before: list[str] = []
        self.regenerated_baseline: Path | None = None

    @pytest.hookimpl(tryfirst=True)
    def pytest_sessionstart(self, session: pytest.Session) -> None:
        if not self.name:
            return
        _install_provider_blocker()
        self.siblings_before = _loaded_siblings(self.name)
        if self.siblings_before:
            pytest.exit(
                f"module {self.name!r} started with sibling modules loaded: "
                + ", ".join(self.siblings_before),
                returncode=1,
            )
        loaded_providers = _loaded_providers()
        if loaded_providers:
            pytest.exit(
                "provider modules were loaded before isolated tests: "
                + ", ".join(loaded_providers),
                returncode=1,
            )
        _install_socket_audit_hook()
        reverse_imports = scan_reverse_imports()
        external_module_files = scan_external_module_files()
        try:
            self.spec = validate_module(self.name)
            scan_module_imports(self.spec)
            if reverse_imports or external_module_files:
                findings = [*reverse_imports, *external_module_files]
                raise ModuleConformanceError(
                    "static module-isolation findings:\n"
                    + "\n".join(finding.format() for finding in findings)
                )
            probe_module_cli(self.spec)
            _evaluate_module_predictions(self.spec)
        except ModuleConformanceError as exc:
            message = str(exc)
            if (reverse_imports or external_module_files) and not message.startswith(
                "static module-isolation findings:"
            ):
                findings = [*reverse_imports, *external_module_files]
                message += "\nstatic module-isolation findings:\n" + "\n".join(
                    finding.format() for finding in findings
                )
            pytest.exit(message, returncode=1)

    def pytest_collection_modifyitems(
        self, session: pytest.Session, config: pytest.Config, items: list[pytest.Item]
    ) -> None:
        if self.spec is None:
            return
        tests_dir = self.spec.tests_dir.resolve()
        outside = []
        for item in items:
            item_path = Path(str(getattr(item, "path", item.fspath))).resolve()
            try:
                item_path.relative_to(tests_dir)
            except ValueError:
                outside.append(item.nodeid)
        if outside:
            pytest.exit(
                f"out-of-module collection for {self.name!r}: " + ", ".join(outside),
                returncode=1,
            )

    def pytest_runtest_logreport(self, report: pytest.TestReport) -> None:
        if report.when == "call":
            self.reports[report.nodeid] = report.outcome
            if self.regen_baseline:
                self.timings[report.nodeid] = report.duration

    def pytest_sessionfinish(
        self, session: pytest.Session, exitstatus: pytest.ExitCode | int
    ) -> None:
        if self.spec is None:
            return
        passed = sum(outcome == "passed" for outcome in self.reports.values())
        skipped = sum(outcome == "skipped" for outcome in self.reports.values())
        failed = sum(outcome == "failed" for outcome in self.reports.values())
        if self.regen_baseline:
            if (
                exitstatus != pytest.ExitCode.OK
                or not self.reports
                or len(self.reports) != len(self.timings)
                or passed != len(self.reports)
                or skipped
                or failed
            ):
                self.failures.append(
                    "baseline regeneration requires a complete passing test session: "
                    f"passed={passed} skipped={skipped} failed={failed} "
                    f"reported={len(self.reports)} timed={len(self.timings)}"
                )
        else:
            expected_count: int | None = None
            for baseline_path in self.spec.baseline_files:
                if baseline_path.suffix.lower() != ".json":
                    continue
                try:
                    baseline = _load_json(REPO_ROOT / baseline_path)
                except (OSError, ValueError, json.JSONDecodeError):
                    continue
                tests = baseline.get("tests") if isinstance(baseline, dict) else None
                count = tests.get("count") if isinstance(tests, dict) else None
                if isinstance(count, int) and not isinstance(count, bool) and count >= 1:
                    expected_count = count
                    break
            if expected_count is None:
                self.failures.append("baseline test count is unavailable; refusing partial gate")
            elif len(self.reports) != expected_count or passed != expected_count:
                self.failures.append(
                    f"module test session is incomplete: passed={passed} skipped={skipped} "
                    f"failed={failed} reported={len(self.reports)} expected={expected_count}"
                )
            elif skipped or failed:
                self.failures.append(
                    f"module test session has non-passing tests: passed={passed} "
                    f"skipped={skipped} failed={failed}"
                )
        siblings_after = _loaded_siblings(cast(str, self.name))
        if siblings_after:
            self.failures.append(
                "sibling modules loaded during isolated tests: " + ", ".join(siblings_after)
            )
        if self.regen_baseline and not self.failures:
            try:
                self.regenerated_baseline = _regenerate_baseline(self.spec, self.timings)
            except ModuleConformanceError as exc:
                self.failures.append(str(exc))
        if self.failures:
            session.exitstatus = 1

    def pytest_terminal_summary(
        self, terminalreporter: Any, exitstatus: pytest.ExitCode | int, config: pytest.Config
    ) -> None:
        if self.spec is None:
            return
        counts = {
            outcome: sum(value == outcome for value in self.reports.values())
            for outcome in (
                "passed",
                "failed",
                "skipped",
            )
        }
        terminalreporter.section("cambium module conformance")
        terminalreporter.write_line(
            f"{self.name}: passed={counts['passed']} failed={counts['failed']} "
            f"skipped={counts['skipped']}"
        )
        if self.regenerated_baseline is not None:
            terminalreporter.write_line(f"baseline regenerated: {self.regenerated_baseline}")
        for failure in self.failures:
            terminalreporter.write_line(f"FAIL: {failure}", red=True)


def pytest_addoption(parser: Any) -> None:
    global _OPTIONS_ADDED
    if _OPTIONS_ADDED:
        return
    _OPTIONS_ADDED = True
    group = parser.getgroup("cambium-module-conformance")
    group.addoption(
        "--cambium-isolated-module",
        default=None,
        metavar="NAME",
        help="run the complete conformance gate for one module",
    )
    group.addoption(
        "--cambium-regen-baseline",
        action="store_true",
        help="regenerate the isolated module's baseline after a passing run",
    )


def pytest_configure(config: pytest.Config) -> None:
    if config.getoption("cambium_isolated_module") is None:
        return
    if not config.pluginmanager.hasplugin("cambium-module-conformance"):
        config.pluginmanager.register(ModuleConformancePlugin(config), "cambium-module-conformance")
