"""Dataset loader for the should_decompose module."""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from enum import Enum
from pathlib import Path
from typing import NoReturn

from cambium.modules.base import DatasetError, DatasetLoader, Example, load_jsonl

from .decide import Decision, TaskInput


class Split(Enum):
    """The three v1 dataset splits (dataset-format.md §4)."""

    TRAIN = "train"
    EVAL = "eval"
    CANARIES = "canaries"


def _reject_json_constant(value: str) -> NoReturn:
    """Reject non-standard JSON constants in metadata."""
    raise ValueError(f"invalid JSON constant {value}")


@dataclass(frozen=True, slots=True)
class DatasetBundle:
    """All three splits plus the dataset version from ``meta.json``."""

    train: tuple[Example, ...]
    eval: tuple[Example, ...]
    canaries: tuple[Example, ...]
    dataset_version: str


class ExampleDatasetLoader(DatasetLoader):
    """Loads input/expected pairs from a JSONL file.

    Each line: ``{"input": {"task", "context"}, "expected":
    {"decompose": bool, "reason"}}``. The loader maps the wire boolean
    ``expected.decompose`` to :class:`Decision` in ``Example.expected``.
    The optional boolean ``canary`` field flags dataset-integrity entries
    planted to catch reward hacking in future evals; they are loaded and
    scored like any other entry by :meth:`load`. Split-aware loads
    (:meth:`load_split`, :meth:`load_all`) read ``datasets/<split>.jsonl`` and
    exclude canaries from train/eval (they load only via
    :data:`Split.CANARIES`).

    The v1 split files are loaded under the dataset-format.md §9
    contract: every record must carry a non-empty string ``id``, ids are
    unique within a file, versioned records must match ``meta.json``'s
    ``schema_version`` and ``dataset_version``, and :meth:`load_all`
    rejects a record present in more than one split (canonical
    ``(task, context)`` hash).
    """

    supported_schema_version = 1

    def load(self) -> list[Example]:
        meta = self._check_schema_version()
        return self._load_path(self.path, meta=meta)

    def load_split(self, split: Split) -> list[Example]:
        """Load one split from ``datasets/<split>.jsonl``.

        The canaries split returns only canary records; the train/eval splits
        exclude them (dataset-format.md §6).
        """
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
            data = json.loads(text, parse_constant=_reject_json_constant)
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
            (require_envelope or path.stem in {split.value for split in Split})
            and bool(
                meta.get("schema_version") is not None
                or meta.get("dataset_version") is not None
            )
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
                task_input = TaskInput(**record["input"])
            except TypeError as exc:
                raise DatasetError(
                    f"{path}:{line_no}: invalid input fields: {exc}"
                ) from exc
            expected = dict(record["expected"])
            expected["decompose"] = (
                Decision.DECOMPOSE
                if expected["decompose"]
                else Decision.DO_NOT_DECOMPOSE
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

    def _check_no_cross_split_collisions(
        self, splits: list[tuple[str, list[Example]]]
    ) -> None:
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
            json.dumps(payload, sort_keys=True, ensure_ascii=False).encode("utf-8")
        ).hexdigest()

    def _validate(self, record: dict, line_no: int, path: Path) -> None:
        if not {"input", "expected"} <= record.keys():
            raise DatasetError(
                f"{path}:{line_no}: record must have 'input' and 'expected'"
            )
        for key in ("input", "expected"):
            if not isinstance(record[key], dict):
                raise DatasetError(f"{path}:{line_no}: {key} must be an object")
        if not isinstance(record["input"].get("task"), str):
            raise DatasetError(f"{path}:{line_no}: input.task must be a string")
        if not isinstance(record["input"].get("context"), str):
            raise DatasetError(f"{path}:{line_no}: input.context must be a string")
        if not isinstance(record["expected"].get("decompose"), bool):
            raise DatasetError(
                f"{path}:{line_no}: expected.decompose must be a boolean"
            )
        if not isinstance(record["expected"].get("reason"), str):
            raise DatasetError(
                f"{path}:{line_no}: expected.reason must be a string"
            )
        canary = record.get("canary", False)
        if not isinstance(canary, bool):
            raise DatasetError(f"{path}:{line_no}: canary must be a boolean")
