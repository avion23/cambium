"""Neutral contracts shared by Cambium decision modules and tooling.

The DSPy seam: each module ships a rule-engine (or pure) implementation
of ``decide`` today; a DSPy program implementing the same interface can
replace it later without touching callers, the dataset, or the metric.

Every decision package that has a ``datasets`` directory also ships a
``module.json`` manifest.  The harness reads that JSON file as data; it never
imports the decision package to discover its name or implementation.  The
manifest fields are ``contract_version``, ``module_name``, ``cli_module``,
``protocol`` (``"json-v1"``), ``dataset_schema_version``, and the optional
``label_field`` (the dataset record boolean the class balance counts; the v1
default is ``decompose``).  The package's
``python -m`` entry implements that protocol: one JSON object in and one JSON
object out.  The optional ``decide`` and ``evaluate`` operations are used by
tooling; the default operation is one module decision.
"""

from __future__ import annotations

import json
import os
import re
import subprocess
import sys
from abc import ABC, abstractmethod
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Protocol

from cambium.auth import is_provider_env_name
from cambium.redact import Redactor, is_secret_name

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
_SAFE_MODULE_NAME = re.compile(r"[a-z][a-z0-9_]*\Z")

# The only environment names a module subprocess may inherit from the parent.
# Provider credentials are never on this list; ``LC_*``/``LANG*`` locale
# controls are inherited by prefix and then rejected individually when their
# name carries a credential-like token.
_MODULE_ENV_KEYS = frozenset(
    {
        "GIT_CONFIG_NOSYSTEM",
        "GIT_EDITOR",
        "GIT_SEQUENCE_EDITOR",
        "GIT_TERMINAL_PROMPT",
        "HOME",
        "LANG",
        "LC_ALL",
        "LC_CTYPE",
        "PATH",
        "PYTHONPATH",
        "PYTHONUNBUFFERED",
        "SYSTEMROOT",
        "SystemRoot",
        "TEMP",
        "TMP",
        "TMPDIR",
        "WINDIR",
    }
)
# Mirrors the credential-namespace pattern used by auth.scrub_environment and
# module_conformance: any name containing one of these tokens is treated as a
# credential and is never passed across the module boundary.
_MODULE_ENV_CREDENTIAL_RE = re.compile(
    r"(?:api|key|token|secret|password|passwd|credential|authorization)",
    re.IGNORECASE,
)


def _module_env(source_root: str | Path | None) -> dict[str, str]:
    """Build a credential-free environment for one module subprocess.

    Only a minimal allowlist of runtime controls is inherited from the parent
    environment; ``os.environ`` is never copied wholesale.  ``LC_*``/``LANG*``
    locale controls are inherited by prefix, but every candidate name is still
    dropped when it names a provider key or matches the credential namespace.
    """
    env: dict[str, str] = {}
    for name, value in os.environ.items():
        allowed = name in _MODULE_ENV_KEYS or name.startswith(("LC_", "LANG"))
        if not allowed or _MODULE_ENV_CREDENTIAL_RE.search(name):
            continue
        env[name] = value
    env["PYTHONUNBUFFERED"] = "1"
    if source_root is not None:
        source = str(Path(source_root).resolve())
        existing = env.get("PYTHONPATH")
        env["PYTHONPATH"] = os.pathsep.join(part for part in (source, existing) if part)
    return env


def _is_secret_shaped(value: str) -> bool:
    """Return whether *value* is a compact machine token.

    Prose-like values contain a space (natural-language phrases such as a
    benign status string); they are only redacted as a whole string so equal
    benign diagnostics are preserved.  Everything else is treated as a compact
    token and redacted wherever it appears, including control characters,
    quotes, and backslashes that JSON serialization rewrites.
    """
    if " " in value:
        return False
    if len(value) >= 16:
        return True
    return any(
        character.isdigit() or character.isupper() or character in "-_./+=:" for character in value
    )


def _module_redactor() -> Redactor:
    """Return a redactor registered with credential values in the parent env.

    Defense in depth for diagnostics: even if a module echoes a credential it
    obtained from outside the harness, its value is redacted before the output
    is embedded in an exception message.  Secret-shaped values are registered
    for substring redaction; prose-like values are registered as whole strings
    so equal benign text is not destroyed.  Callers must build this before
    spawning the module subprocess so a concurrent environment mutation cannot
    change the registered values mid-run.
    """
    secret_values: set[str] = set()
    whole_values: set[str] = set()
    for name, value in os.environ.items():
        if value and (is_provider_env_name(name) or is_secret_name(name)):
            if _is_secret_shaped(value):
                secret_values.add(value)
            else:
                whole_values.add(value)
    return Redactor(secret_values=secret_values, whole_values=whole_values)


class ModuleBoundaryError(ValueError):
    """Raised when a neutral module boundary cannot be used safely."""


class ModuleContractError(ModuleBoundaryError):
    """Raised when a module manifest is missing or invalid."""


class ModuleCLIError(ModuleBoundaryError):
    """Raised when a module JSON CLI fails or violates its wire contract."""


class ModuleSplitError(ModuleCLIError):
    """Raised when a module explicitly reports an unavailable or invalid split."""


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
    label_field: str = "decompose"
    dspy_program: str = ""

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
        raise ModuleContractError(f"module {name!r}: missing required manifest field 'module.json'")
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except OSError as exc:
        raise ModuleContractError(f"module {name!r}: cannot read {path}: {exc}") from exc
    except (UnicodeDecodeError, ValueError) as exc:
        raise ModuleContractError(f"module {name!r}: invalid {path}: {exc}") from exc
    if not isinstance(data, dict):
        raise ModuleContractError(f"module {name!r}: {path} must contain a JSON object")

    for key in _REQUIRED_MANIFEST_FIELDS:
        if key not in data:
            raise ModuleContractError(f"module {name!r}: missing required manifest field {key!r}")

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
    if _SAFE_MODULE_NAME.fullmatch(module_name) is None:
        raise ModuleContractError(
            f"module {name!r}: manifest field 'module_name' must be a safe identifier "
            "matching [a-z][a-z0-9_]*"
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

    label_field = data.get("label_field", "decompose")
    if not isinstance(label_field, str) or not label_field:
        raise ModuleContractError(
            f"module {name!r}: manifest field 'label_field' must be a non-empty string"
        )

    dspy_program = data.get("dspy_program", "")
    if "dspy_program" in data and (not isinstance(dspy_program, str) or not dspy_program):
        raise ModuleContractError(
            f"module {name!r}: manifest field 'dspy_program' must be a non-empty string"
        )

    return ModuleManifest(
        package_dir=directory,
        package_name=name,
        module_name=module_name,
        cli_module=cli_module,
        contract_version=contract_version,
        protocol=protocol,
        dataset_schema_version=_manifest_int(data, "dataset_schema_version", path),
        label_field=label_field,
        dspy_program=dspy_program,
    )


def _redact_structured(value: object, redactor: Redactor) -> str:
    """Re-serialize a decoded JSON value with credential values redacted.

    Redaction runs on the decoded values, so the re-serialized text cannot
    carry a credential in any JSON escape encoding: short escapes, ``\\uXXXX``
    with either hex case, or a per-character mix are all rewritten before
    matching.  ``json.dumps`` chooses the wire escapes, not the module.
    """
    redacted = redactor.redact_mapping(value)
    return json.dumps(redacted, ensure_ascii=False)


def _redact_wire_output(text: str, redactor: Redactor) -> str:
    """Redact credential values from raw module wire output.

    Valid JSON is decoded to the object level and re-serialized after
    structured redaction; free-form text is redacted with escape-aware
    matching.  In both cases the decoded value is matched, never the
    escape-encoded wire form.
    """
    try:
        decoded = json.loads(text)
    except json.JSONDecodeError:
        return redactor.redact_escaped(text)
    return _redact_structured(decoded, redactor)


def _cli_detail(stdout: str, stderr: str, redactor: Redactor) -> str:
    detail = stderr.strip() or stdout.strip() or "no diagnostic output"
    detail = _redact_wire_output(detail, redactor)
    return detail if len(detail) <= 500 else f"{detail[:497]}..."


def _split_error_status(output: object) -> str | None:
    """Return the fallback-eligible split status from a CLI error object."""
    if not isinstance(output, dict):
        return None
    error = output.get("error")
    if not isinstance(error, dict):
        return None

    for field in ("code", "status", "type"):
        value = error.get(field)
        if not isinstance(value, str):
            continue
        normalized = value.upper().replace("-", "_")
        if normalized in {"UNAVAILABLE", "SCHEMA_INVALID"}:
            return normalized

    return None


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

    env = _module_env(source_root)
    # Snapshot the parent credential values BEFORE spawning: the child must
    # not run while the registered values can still change under it.
    redactor = _module_redactor()

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
    except UnicodeError as exc:
        raise ModuleCLIError(
            f"module {cli_module!r}: CLI output could not be decoded: {exc}"
        ) from exc
    except (OSError, subprocess.SubprocessError) as exc:
        raise ModuleCLIError(f"module {cli_module!r}: CLI could not run: {exc}") from exc

    if result.returncode != 0:
        try:
            output = json.loads(result.stdout)
        except json.JSONDecodeError:
            output = None
        status = _split_error_status(output)
        if status is not None:
            raise ModuleSplitError(
                f"module {cli_module!r}: CLI reported split {status}: "
                f"{_cli_detail(result.stdout, result.stderr, redactor)}"
            )
        raise ModuleCLIError(
            f"module {cli_module!r}: CLI exited {result.returncode}: "
            f"{_cli_detail(result.stdout, result.stderr, redactor)}"
        )

    try:
        output = json.loads(result.stdout)
    except json.JSONDecodeError as exc:
        raise ModuleCLIError(
            f"module {cli_module!r}: CLI output is not one JSON object: {exc}"
        ) from exc
    if not isinstance(output, dict):
        raise ModuleCLIError(f"module {cli_module!r}: CLI output must be one JSON object")
    if "error" in output:
        status = _split_error_status(output)
        if status is not None:
            raise ModuleSplitError(
                f"module {cli_module!r}: CLI reported split {status}: "
                f"{_cli_detail(result.stdout, result.stderr, redactor)}"
            )
        raise ModuleCLIError(
            f"module {cli_module!r}: CLI returned an error object: "
            f"{_redact_structured(output, redactor)}"
        )
    return output


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
    async def decide(self, input: Any) -> Any:
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


def _reject_duplicate_json_keys(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    record: dict[str, Any] = {}
    for key, value in pairs:
        if key in record:
            raise ValueError(f"duplicate JSON object key {key!r}")
        record[key] = value
    return record


def _reject_json_constant(value: str) -> Any:
    raise ValueError(f"non-standard JSON constant {value!r}")


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
            record = json.loads(
                line,
                object_pairs_hook=_reject_duplicate_json_keys,
                parse_constant=_reject_json_constant,
            )
        except (json.JSONDecodeError, ValueError) as exc:
            raise DatasetError(f"{dataset_path}:{line_no}: invalid JSON: {exc}") from exc
        if not isinstance(record, dict):
            raise DatasetError(f"{dataset_path}:{line_no}: record must be a JSON object")
        records.append(record)
    return records
