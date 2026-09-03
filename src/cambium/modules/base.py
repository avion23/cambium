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

import hashlib
import json
import math
import os
import re
import statistics
import subprocess
import sys
from abc import ABC, abstractmethod
from asyncio import get_running_loop, run
from collections.abc import Sequence
from dataclasses import dataclass
from enum import Enum
from pathlib import Path
from typing import Any, ClassVar, Literal, NoReturn, Protocol, cast

from cambium.auth import is_provider_env_name
from cambium.redact import Redactor, is_secret_name

MODULE_CONTRACT_VERSION = 1
MODULE_MANIFEST_FILENAME = "module.json"
MODULE_PROTOCOL = "json-v1"
PARSE_FAILURE_REASON = "DSPy output unparseable"
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


def is_parse_failure_prediction(prediction: object) -> bool:
    """Return whether a prediction is the conservative DSPy parse fallback."""
    return getattr(prediction, "reason", None) == PARSE_FAILURE_REASON


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


# The two shipped decision modules deliberately keep their rules in their own
# packages.  Everything below is the boring contract plumbing those packages
# share: split loading, exact-match evaluation, the neutral JSON adapter, and
# the lazy DSPy seam.


class Split(Enum):
    """The three v1 dataset splits (dataset-format.md §4)."""

    TRAIN = "train"
    EVAL = "eval"
    CANARIES = "canaries"


def _reject_dataset_json_constant(value: str) -> NoReturn:
    """Reject non-standard JSON constants in module metadata."""
    raise ValueError(f"invalid JSON constant {value}")


@dataclass(frozen=True, slots=True)
class DatasetBundle:
    """All three splits plus the dataset version from ``meta.json``."""

    train: tuple[Example, ...]
    eval: tuple[Example, ...]
    canaries: tuple[Example, ...]
    dataset_version: str


class SharedDatasetLoader(DatasetLoader):
    """Load the common v1 split contract for a module-specific decision type."""

    supported_schema_version = 1
    input_type: ClassVar[type]
    label_field: ClassVar[str]
    positive_decision: ClassVar[Enum]
    negative_decision: ClassVar[Enum]
    require_decompose_mirror: ClassVar[bool] = False

    def load(self) -> list[Example]:
        meta = self._check_schema_version()
        return self._load_path(self.path, meta=meta)

    def load_split(self, split: Split) -> list[Example]:
        """Load one split from ``datasets/<split>.jsonl``."""
        meta = self._check_schema_version()
        return self._load_split(split, meta)

    def _load_split(self, split: Split, meta: dict) -> list[Example]:
        """Load one split using metadata captured by the public entry point."""
        split_path = self.datasets_dir / f"{split.value}.jsonl"
        if not split_path.is_file():
            raise DatasetError(f"dataset split file is missing: {split_path}")
        examples = self._load_path(split_path, meta=meta, require_envelope=True)
        if split is Split.CANARIES:
            examples = [ex for ex in examples if ex.canary]
        else:
            examples = [ex for ex in examples if not ex.canary]
        return examples

    def load_all(self) -> DatasetBundle:
        """Load the full three-split dataset as a frozen bundle."""
        meta = self._check_schema_version()
        train = tuple(self._load_split(Split.TRAIN, meta))
        eval_ = tuple(self._load_split(Split.EVAL, meta))
        canaries = tuple(self._load_split(Split.CANARIES, meta))
        self._check_no_cross_split_collisions(
            [("train", train), ("eval", eval_), ("canaries", canaries)]
        )
        return DatasetBundle(
            train=train,
            eval=eval_,
            canaries=canaries,
            dataset_version=self._dataset_version_from_meta(meta),
        )

    @property
    def datasets_dir(self) -> Path:
        """The module's dataset directory (a directory path, or a file's parent)."""
        return self.path if self.path.is_dir() else self.path.parent

    @property
    def dataset_version(self) -> str:
        """Dataset version from ``meta.json``; ``"0.1.0"`` when absent."""
        return self._dataset_version_from_meta(self._read_meta())

    @staticmethod
    def _dataset_version_from_meta(data: dict) -> str:
        """Return the dataset version represented by already-read metadata."""
        version = data.get("dataset_version")
        return version if isinstance(version, str) else "0.1.0"

    def _read_meta(self) -> dict:
        """Read ``meta.json``; ``{}`` when absent. DatasetError on bad content."""
        meta = self.datasets_dir / "meta.json"
        try:
            text = meta.read_text()
        except FileNotFoundError as exc:
            if not meta.is_symlink():
                return {}
            raise DatasetError(f"{meta}: cannot read metadata: {exc}") from exc
        except OSError as exc:
            raise DatasetError(f"{meta}: cannot read metadata: {exc}") from exc
        except UnicodeError as exc:
            raise DatasetError(f"{meta}: invalid text: {exc}") from exc
        try:
            data = json.loads(text, parse_constant=_reject_dataset_json_constant)
        except ValueError as exc:
            raise DatasetError(f"{meta}: invalid JSON: {exc}") from exc
        if not isinstance(data, dict):
            raise DatasetError(
                f"{meta}: meta.json must be a JSON object, got {type(data).__name__}"
            )
        return data

    def _check_schema_version(self) -> dict:
        data = self._read_meta()
        schema_version = data.get("schema_version")
        if schema_version is None:
            return data
        if not isinstance(schema_version, int) or isinstance(schema_version, bool):
            raise DatasetError(
                f"{self.datasets_dir / 'meta.json'}: schema_version must be an integer"
            )
        if schema_version != self.supported_schema_version:
            raise DatasetError(
                f"{self.datasets_dir / 'meta.json'}: schema_version {schema_version} "
                f"unsupported; loader supports {self.supported_schema_version}"
            )
        return data

    def _load_path(
        self, path: Path, *, meta: dict, require_envelope: bool = False
    ) -> list[Example]:
        records = load_jsonl(path)
        require_versions = (
            require_envelope or path.stem in {split.value for split in Split}
        ) and bool(
            meta.get("schema_version") is not None or meta.get("dataset_version") is not None
        )
        examples: list[Example] = []
        seen_ids: dict[str, int] = {}
        for line_no, record in enumerate(records, start=1):
            self._validate_record_versions(record, line_no, path, meta, require_versions)
            self._validate(record, line_no, path)
            record_id = record.get("id")
            if require_envelope:
                if not isinstance(record_id, str) or not record_id:
                    raise DatasetError(
                        f"{path}:{line_no}: record must have a non-empty string 'id'"
                    )
            if isinstance(record_id, str):
                if record_id in seen_ids:
                    raise DatasetError(
                        f"{path}:{line_no}: duplicate id {record_id!r} "
                        f"(first at line {seen_ids[record_id]})"
                    )
                seen_ids[record_id] = line_no
            try:
                task_input = self.input_type(**record["input"])
            except TypeError as exc:
                raise DatasetError(f"{path}:{line_no}: invalid input fields: {exc}") from exc
            expected = dict(record["expected"])
            expected[self.label_field] = (
                self.positive_decision if expected[self.label_field] else self.negative_decision
            )
            examples.append(
                Example(
                    input=task_input,
                    expected=expected,
                    canary=bool(record.get("canary", False)),
                )
            )
        return examples

    def _validate_record_versions(
        self,
        record: dict,
        line_no: int,
        path: Path,
        meta: dict,
        require_versions: bool,
    ) -> None:
        expected_schema_version = meta.get("schema_version")
        expected_dataset_version = meta.get("dataset_version")
        if expected_schema_version is None and expected_dataset_version is None:
            return

        has_versions = "schema_version" in record or "dataset_version" in record
        if not has_versions:
            if require_versions:
                raise DatasetError(
                    f"{path}:{line_no}: record must have schema_version and "
                    "dataset_version matching meta.json"
                )
            return

        mismatches: list[str] = []
        record_schema_version = record.get("schema_version")
        if expected_schema_version is not None:
            if (
                not isinstance(record_schema_version, int)
                or isinstance(record_schema_version, bool)
                or record_schema_version != expected_schema_version
            ):
                mismatches.append(
                    f"schema_version {record_schema_version!r} != "
                    f"meta.json {expected_schema_version!r}"
                )

        record_dataset_version = record.get("dataset_version")
        if expected_dataset_version is not None:
            if (
                not isinstance(record_dataset_version, str)
                or record_dataset_version != expected_dataset_version
            ):
                mismatches.append(
                    f"dataset_version {record_dataset_version!r} != "
                    f"meta.json {expected_dataset_version!r}"
                )

        if mismatches:
            raise DatasetError(f"{path}:{line_no}: version drift: {', '.join(mismatches)}")

    def _check_no_cross_split_collisions(self, splits: list[tuple[str, Sequence[Example]]]) -> None:
        seen: dict[str, tuple[str, str]] = {}
        for split_name, examples in splits:
            for example in examples:
                digest = self._canonical_digest(example)
                if digest in seen:
                    first_split, first_task = seen[digest]
                    raise DatasetError(
                        f"cross-split collision: (task, context) "
                        f"from {first_split} ({first_task!r}) "
                        f"also in {split_name} ({example.input.task!r})"
                    )
                seen[digest] = (split_name, example.input.task)

    @staticmethod
    def _canonical_digest(example: Example) -> str:
        payload = (example.input.task, example.input.context)
        return hashlib.sha256(
            json.dumps(payload, sort_keys=True, ensure_ascii=False, separators=(",", ":")).encode(
                "utf-8"
            )
        ).hexdigest()

    def _validate(self, record: dict, line_no: int, path: Path) -> None:
        if not {"input", "expected"} <= record.keys():
            raise DatasetError(f"{path}:{line_no}: record must have 'input' and 'expected'")
        for key in ("input", "expected"):
            if not isinstance(record[key], dict):
                raise DatasetError(f"{path}:{line_no}: {key} must be an object")
        if not isinstance(record["input"].get("task"), str):
            raise DatasetError(f"{path}:{line_no}: input.task must be a string")
        if not isinstance(record["input"].get("context"), str):
            raise DatasetError(f"{path}:{line_no}: input.context must be a string")
        expected = record["expected"]
        if not isinstance(expected.get(self.label_field), bool):
            raise DatasetError(f"{path}:{line_no}: expected.{self.label_field} must be a boolean")
        if self.require_decompose_mirror:
            decompose = expected.get("decompose")
            if not isinstance(decompose, bool) or decompose != expected[self.label_field]:
                raise DatasetError(
                    f"{path}:{line_no}: expected.decompose must mirror "
                    f"expected.{self.label_field} (generic v1 class-balance field)"
                )
        if not isinstance(expected.get("reason"), str):
            raise DatasetError(f"{path}:{line_no}: expected.reason must be a string")
        canary = record.get("canary", False)
        if not isinstance(canary, bool):
            raise DatasetError(f"{path}:{line_no}: canary must be a boolean")


def score_decision(example: Example, *, label_field: str, decision_type: type[Enum]) -> float:
    """Score one example in [0, 1] by exact match on its decision enum.

    The production DSPy fallback deliberately scores as an ordinary
    per-example zero here. Optimizer aggregates identify that fallback with
    :func:`is_parse_failure_prediction` and exclude it from their means while
    reporting its count separately.
    """
    prediction = example.prediction
    if prediction is None or is_parse_failure_prediction(prediction):
        return 0.0
    expected = example.expected.get(label_field)
    if not isinstance(expected, decision_type) or not isinstance(
        prediction.decision, decision_type
    ):
        return 0.0
    return 1.0 if prediction.decision == expected else 0.0


async def evaluate_split_async(module: Module, loader, split) -> dict:
    """Score one dataset split and report parse fallbacks separately."""
    scores: list[float] = []
    parse_failures = 0
    count = 0
    for example in loader.load_split(split):
        count += 1
        prediction = await module.decide(example.input)
        if is_parse_failure_prediction(prediction):
            parse_failures += 1
            continue
        scores.append(module.metric(example.with_prediction(prediction)))
    if not scores:
        empty = count == 0
        return {
            "mean": float("nan") if empty else 0.0,
            "std": float("nan") if empty else 0.0,
            "count": count,
            "scored_count": 0,
            "parse_failures": parse_failures,
        }
    return {
        "mean": statistics.fmean(scores),
        "std": statistics.pstdev(scores),
        "count": count,
        "scored_count": len(scores),
        "parse_failures": parse_failures,
    }


def evaluate_split(module: Module, loader, split) -> dict:
    """Score one dataset split outside a running event loop."""
    try:
        get_running_loop()
    except RuntimeError:
        return run(evaluate_split_async(module, loader, split))
    raise RuntimeError(
        "evaluate_split must not be called from a running event loop; "
        "use evaluate_split_async in async contexts"
    )


class InputValidationError(ValueError):
    """Raised when a JSON request does not match a module wire schema."""


class SchemaInvalidError(ValueError):
    """Raised when a dataset record does not match a module dataset schema."""


_MODULE_INPUT_FIELDS = frozenset({"task", "context"})


def _parse_module_input(payload: Any, input_type: type) -> Any:
    """Validate one decoded JSON value and build a typed module input."""
    if not isinstance(payload, dict):
        raise InputValidationError("input must be a JSON object")

    unknown_fields = sorted(set(payload) - _MODULE_INPUT_FIELDS)
    if unknown_fields:
        names = ", ".join(repr(field) for field in unknown_fields)
        raise InputValidationError(f"unknown input field(s): {names}")
    if "task" not in payload:
        raise InputValidationError("input.task is required")
    task = payload["task"]
    if not isinstance(task, str):
        raise InputValidationError("input.task must be a string")
    if not task.strip():
        raise InputValidationError("input.task must not be empty")

    context = payload.get("context", "")
    if not isinstance(context, str):
        raise InputValidationError("input.context must be a string")

    return input_type(task=task, context=context)


def _reject_duplicate_module_fields(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    """Reject duplicate JSON fields with the CLI's historical error type."""
    fields: dict[str, Any] = {}
    for name, value in pairs:
        if name in fields:
            raise json.JSONDecodeError(f"duplicate JSON object field: {name!r}", "", 0)
        fields[name] = value
    return fields


def _serialize_module_output(
    output: Any, output_type: type, decision_type: type[Enum], label_field: str
) -> dict[str, bool | float | str]:
    """Convert a typed domain output to its stable JSON wire shape."""
    if not isinstance(output, output_type):
        raise TypeError("module returned an invalid output type")
    if not isinstance(output.decision, decision_type):
        raise TypeError("module returned an invalid decision")
    if not isinstance(output.reason, str):
        raise TypeError("module returned an invalid reason")
    if isinstance(output.confidence, bool) or not isinstance(output.confidence, int | float):
        raise TypeError("module returned an invalid confidence")
    if not math.isfinite(output.confidence) or not 0.0 <= output.confidence <= 1.0:
        raise ValueError("module returned confidence outside [0.0, 1.0]")
    return {
        "confidence": output.confidence,
        label_field: getattr(output, label_field),
        "reason": output.reason,
    }


async def _module_decide(
    module: Module,
    inputs: list[Any],
    output_type: type,
    decision_type: type[Enum],
    label_field: str,
) -> list[dict]:
    return [
        _serialize_module_output(
            await module.decide(task_input), output_type, decision_type, label_field
        )
        for task_input in inputs
    ]


async def _module_evaluate(
    module: Module,
    records: list[dict[str, Any]],
    input_type: type,
    output_type: type,
    decision_type: type[Enum],
    label_field: str,
) -> list[dict[str, Any]]:
    results: list[dict[str, Any]] = []
    for index, record in enumerate(records):
        if not isinstance(record, dict):
            raise SchemaInvalidError(f"record {index} must be a JSON object")
        try:
            task_input = _parse_module_input(record.get("input"), input_type)
        except InputValidationError as exc:
            raise SchemaInvalidError(f"record {index}.input: {exc}") from exc
        expected = record.get("expected")
        if not isinstance(expected, dict):
            raise SchemaInvalidError(f"record {index}.expected must be a JSON object")
        expected_label = expected.get(label_field)
        if not isinstance(expected_label, bool):
            raise SchemaInvalidError(f"record {index}.expected.{label_field} must be a boolean")
        if not isinstance(expected.get("reason"), str):
            raise SchemaInvalidError(f"record {index}.expected.reason must be a string")
        canary = record.get("canary", False)
        if not isinstance(canary, bool):
            raise SchemaInvalidError(f"record {index}.canary must be a boolean")
        prediction = await module.decide(task_input)
        prediction_wire = _serialize_module_output(
            prediction, output_type, decision_type, label_field
        )
        expected_typed = dict(expected)
        expected_typed[label_field] = (
            next(member for member in decision_type if member.value == label_field)
            if expected_label
            else next(member for member in decision_type if member.value == f"do_not_{label_field}")
        )
        score = module.metric(
            Example(
                input=task_input,
                expected=expected_typed,
                prediction=prediction,
                canary=canary,
            )
        )
        if isinstance(score, bool) or not isinstance(score, int | float):
            raise TypeError(f"record {index}: module metric is not numeric")
        if not math.isfinite(score) or not 0.0 <= score <= 1.0:
            raise ValueError(f"record {index}: module metric is outside [0.0, 1.0]")
        results.append({"prediction": prediction_wire, "score": score})
    return results


def run_module_entrypoint(
    module_type: type[Module],
    input_type: type,
    output_type: type,
    decision_type: type[Enum],
    label_field: str,
    module_name: str,
) -> int:
    """Run one module's neutral JSON stdin/stdout adapter."""

    def write_json(payload: object) -> None:
        sys.stdout.write(json.dumps(payload, sort_keys=True) + "\n")

    def write_error(exc: Exception) -> int:
        error: dict[str, Any] = {
            "message": str(exc) or "module CLI failed",
            "type": type(exc).__name__,
        }
        if isinstance(exc, SchemaInvalidError):
            error["code"] = "SCHEMA_INVALID"
        write_json({"error": error})
        print(f"{module_name}: {type(exc).__name__}: {exc}", file=sys.stderr)
        return 1

    try:
        payload = json.loads(
            sys.stdin.buffer.read(), object_pairs_hook=_reject_duplicate_module_fields
        )
        module = module_type()
        if isinstance(payload, dict) and "operation" in payload:
            operation = payload.get("operation")
            if operation == "decide":
                inputs = payload.get("inputs")
                if not isinstance(inputs, list):
                    raise InputValidationError("decide.inputs must be a JSON array")
                decisions = run(
                    _module_decide(
                        module,
                        [_parse_module_input(item, input_type) for item in inputs],
                        output_type,
                        decision_type,
                        label_field,
                    )
                )
                result = {"results": decisions}
            elif operation == "evaluate":
                records = payload.get("records")
                if not isinstance(records, list):
                    raise InputValidationError("evaluate.records must be a JSON array")
                result = {
                    "results": run(
                        _module_evaluate(
                            module,
                            records,
                            input_type,
                            output_type,
                            decision_type,
                            label_field,
                        )
                    )
                }
            else:
                raise InputValidationError(f"unknown operation: {operation!r}")
        else:
            result = run(
                _module_decide(
                    module,
                    [_parse_module_input(payload, input_type)],
                    output_type,
                    decision_type,
                    label_field,
                )
            )[0]
        write_json(result)
    except Exception as exc:
        return write_error(exc)
    return 0


class DSPyModuleBase:
    """Lazy DSPy classifier base shared by the shipped module programs."""

    name: ClassVar[str]
    label_field: ClassVar[str]
    fallback_decision: ClassVar[Enum]
    output_type: ClassVar[type]
    decision_type: ClassVar[type[Enum]]
    signature_name: ClassVar[str]
    signature_docstring: ClassVar[str]

    def __init__(self, lm) -> None:
        import dspy  # type: ignore[import-untyped]

        cls = type(self)
        if DSPyModuleBase in cls.__bases__:
            cls.__init__ = DSPyModuleBase.__init__
            cls.decide = DSPyModuleBase.decide
            cls.metric = DSPyModuleBase.metric
            cls.__bases__ = (dspy.Module,)
        dspy.Module.__init__(cast(Any, self))

        decision_values = tuple(member.value for member in self.decision_type)
        signature = type(
            self.signature_name,
            (dspy.Signature,),
            {
                "__module__": cls.__module__,
                "__doc__": self.signature_docstring,
                "__annotations__": {
                    "task": str,
                    "context": str,
                    "decision": Literal[decision_values],
                    "reason": str,
                },
                "task": dspy.InputField(),
                "context": dspy.InputField(),
                "decision": dspy.OutputField(desc="exactly one of the allowed values"),
                "reason": dspy.OutputField(desc="one short sentence naming the evidence"),
            },
        )
        self._predict = dspy.Predict(signature)
        self._lm = lm

    async def decide(self, input: Any) -> Any:
        """Run the DSPy predictor and map its output to the domain enum."""
        import dspy  # type: ignore[import-untyped]

        try:
            with dspy.context(lm=self._lm):
                pred = await self._predict.acall(task=input.task, context=input.context)
            decision = self.decision_type(str(pred.decision))
        except (ValueError, dspy.AdapterParseError):
            return self.output_type(
                decision=self.fallback_decision,
                reason=PARSE_FAILURE_REASON,
                confidence=0.0,
            )
        return self.output_type(decision=decision, reason=str(pred.reason), confidence=0.5)

    def metric(self, example: Example) -> float:
        """Score a prediction with the module's exact decision metric."""
        return score_decision(
            example, label_field=self.label_field, decision_type=self.decision_type
        )
