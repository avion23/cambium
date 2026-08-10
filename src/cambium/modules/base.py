"""Neutral contracts shared by Cambium decision modules and tooling.

The DSPy seam: each module ships a rule-engine (or pure) implementation
of ``decide`` today; a DSPy program implementing the same interface can
replace it later without touching callers, the dataset, or the metric.

Every decision package that has a ``datasets`` directory also ships a
``module.json`` manifest.  The harness reads that JSON file as data; it never
imports the decision package to discover its name or implementation.  The
manifest fields are ``contract_version``, ``module_name``, ``cli_module``,
``protocol`` (``"json-v1"``), and ``dataset_schema_version``.  The package's
``python -m`` entry implements that protocol: one JSON object in and one JSON
object out.  The optional ``decide`` and ``evaluate`` operations are used by
tooling; the default operation is one module decision.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
from abc import ABC, abstractmethod
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Protocol

MODULE_CONTRACT_VERSION = 1
MODULE_MANIFEST_FILENAME = "module.json"
MODULE_PROTOCOL = "json-v1"
_REQUIRED_MANIFEST_FIELDS = (
    "contract_version",
    "module_name",
    "cli_module",
    "protocol",
    "dataset_schema_version",
)


class ModuleBoundaryError(ValueError):
    """Raised when a neutral module boundary cannot be used safely."""


class ModuleContractError(ModuleBoundaryError):
    """Raised when a module manifest is missing or invalid."""


class ModuleCLIError(ModuleBoundaryError):
    """Raised when a module JSON CLI fails or violates its wire contract."""


@dataclass(frozen=True, slots=True)
class ModuleManifest:
    """Validated, import-free metadata for one decision package."""

    package_dir: Path
    package_name: str
    module_name: str
    cli_module: str
    contract_version: int
    protocol: str
    dataset_schema_version: int

    @property
    def source_root(self) -> Path:
        """Return the directory that contains the ``cambium`` package."""
        return self.package_dir.parents[2]


def _manifest_int(data: dict[str, Any], key: str, path: Path) -> int:
    value = data.get(key)
    if isinstance(value, bool) or not isinstance(value, int):
        raise ModuleContractError(
            f"module {path.parent.name!r}: manifest field {key!r} must be an integer"
        )
    return value


def load_module_manifest(
    package_dir: str | Path, package_name: str | None = None
) -> ModuleManifest:
    """Read and validate a module manifest without importing its package.

    ``package_name`` is normally the directory name discovered by the bench.
    It is explicit so callers can produce diagnostics that identify the
    directory being checked even when the manifest is malformed.
    """
    directory = Path(package_dir)
    name = package_name or directory.name
    path = directory / MODULE_MANIFEST_FILENAME
    if not path.is_file():
        raise ModuleContractError(
            f"module {name!r}: missing required manifest field 'module.json'"
        )
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except OSError as exc:
        raise ModuleContractError(f"module {name!r}: cannot read {path}: {exc}") from exc
    except json.JSONDecodeError as exc:
        raise ModuleContractError(f"module {name!r}: invalid {path}: {exc}") from exc
    if not isinstance(data, dict):
        raise ModuleContractError(f"module {name!r}: {path} must contain a JSON object")

    for key in _REQUIRED_MANIFEST_FIELDS:
        if key not in data:
            raise ModuleContractError(
                f"module {name!r}: missing required manifest field {key!r}"
            )

    contract_version = _manifest_int(data, "contract_version", path)
    if contract_version != MODULE_CONTRACT_VERSION:
        raise ModuleContractError(
            f"module {name!r}: unsupported contract_version {contract_version}; "
            f"expected {MODULE_CONTRACT_VERSION}"
        )

    module_name = data["module_name"]
    if not isinstance(module_name, str) or not module_name:
        raise ModuleContractError(
            f"module {name!r}: manifest field 'module_name' must be a non-empty string"
        )

    cli_module = data["cli_module"]
    expected_cli = f"cambium.modules.{name}"
    if not isinstance(cli_module, str) or not cli_module:
        raise ModuleContractError(
            f"module {name!r}: manifest field 'cli_module' must be a non-empty string"
        )
    if cli_module != expected_cli:
        raise ModuleContractError(
            f"module {name!r}: manifest field 'cli_module' must be {expected_cli!r}"
        )

    protocol = data["protocol"]
    if protocol != MODULE_PROTOCOL:
        raise ModuleContractError(
            f"module {name!r}: manifest field 'protocol' must be {MODULE_PROTOCOL!r}"
        )

    return ModuleManifest(
        package_dir=directory,
        package_name=name,
        module_name=module_name,
        cli_module=cli_module,
        contract_version=contract_version,
        protocol=protocol,
        dataset_schema_version=_manifest_int(data, "dataset_schema_version", path),
    )


def _cli_detail(stdout: str, stderr: str) -> str:
    detail = stderr.strip() or stdout.strip() or "no diagnostic output"
    return detail if len(detail) <= 500 else f"{detail[:497]}..."


def run_module_cli(
    cli_module: str,
    payload: dict[str, Any],
    *,
    cwd: str | Path | None = None,
    source_root: str | Path | None = None,
    timeout: float = 60.0,
) -> dict[str, Any]:
    """Call a module's neutral JSON CLI and return one JSON object.

    ``source_root`` is used by repository tooling and fixture tests to make
    the package importable without relying on an installed editable package.
    The caller still crosses the module boundary through a subprocess; this
    function never imports ``cli_module``.
    """
    try:
        encoded = json.dumps(payload, ensure_ascii=False) + "\n"
    except (TypeError, ValueError) as exc:
        raise ModuleCLIError(f"module {cli_module!r}: request is not JSON: {exc}") from exc

    env = os.environ.copy()
    if source_root is not None:
        source = str(Path(source_root).resolve())
        existing = env.get("PYTHONPATH")
        env["PYTHONPATH"] = os.pathsep.join(
            part for part in (source, existing) if part
        )

    try:
        result = subprocess.run(
            [sys.executable, "-m", cli_module],
            cwd=cwd,
            env=env,
            input=encoded,
            capture_output=True,
            text=True,
            timeout=timeout,
            check=False,
        )
    except (OSError, subprocess.SubprocessError) as exc:
        raise ModuleCLIError(f"module {cli_module!r}: CLI could not run: {exc}") from exc

    if result.returncode != 0:
        raise ModuleCLIError(
            f"module {cli_module!r}: CLI exited {result.returncode}: "
            f"{_cli_detail(result.stdout, result.stderr)}"
        )

    try:
        output = json.loads(result.stdout)
    except json.JSONDecodeError as exc:
        raise ModuleCLIError(
            f"module {cli_module!r}: CLI output is not one JSON object: {exc}"
        ) from exc
    if not isinstance(output, dict):
        raise ModuleCLIError(
            f"module {cli_module!r}: CLI output must be one JSON object"
        )
    if "error" in output:
        raise ModuleCLIError(
            f"module {cli_module!r}: CLI returned an error object: {output['error']}"
        )
    return output


class Output(Protocol):
    """Typed prediction returned by a module's ``decide``."""


@dataclass(frozen=True, slots=True)
class Example:
    """One dataset record: input, expected, and (optional) prediction."""

    input: Any
    expected: dict[str, Any]
    prediction: Any | None = None
    canary: bool = False

    def with_prediction(self, prediction: Any) -> Example:
        """Return a copy of this example with a prediction attached."""
        return Example(
            input=self.input,
            expected=self.expected,
            prediction=prediction,
            canary=self.canary,
        )


class Metric(Protocol):
    """Scores one example (with prediction attached) as a float in [0, 1]."""

    def __call__(self, example: Example) -> float: ...


class Module(ABC):
    """A Cambium decision module. Seed of the per-module pattern."""

    name: str

    @abstractmethod
    async def decide(self, input: Any) -> Output:
        """Run the module over one input; return a typed prediction."""

    @abstractmethod
    def metric(self, example: Example) -> float:
        """Score one example (prediction vs expected) in [0, 1]."""


class DatasetError(ValueError):
    """Raised when a dataset file is unreadable or schema-invalid."""


class DatasetLoader(ABC):
    """Loads a module's dataset as validated :class:`Example` records."""

    def __init__(self, path: str | Path) -> None:
        self.path = Path(path)

    @abstractmethod
    def load(self) -> list[Example]:
        """Load and validate all examples in the dataset."""


def load_jsonl(path: str | Path) -> list[dict[str, Any]]:
    """Read a JSONL file into a list of records, ignoring blank lines.

    Shared by dataset loaders. Raises :class:`DatasetError` on unreadable
    files or invalid JSON.
    """
    dataset_path = Path(path)
    try:
        lines = dataset_path.read_text().splitlines()
    except OSError as exc:
        raise DatasetError(f"cannot read dataset {dataset_path}: {exc}") from exc

    records: list[dict[str, Any]] = []
    for line_no, line in enumerate(lines, start=1):
        if not line.strip():
            continue
        try:
            record = json.loads(line)
        except json.JSONDecodeError as exc:
            raise DatasetError(f"{dataset_path}:{line_no}: invalid JSON: {exc}") from exc
        if not isinstance(record, dict):
            raise DatasetError(f"{dataset_path}:{line_no}: record must be a JSON object")
        records.append(record)
    return records
