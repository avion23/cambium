"""Dataset loader for the should_decompose module."""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from enum import Enum
from pathlib import Path

from cambium.modules.base import DatasetError, DatasetLoader, Example, load_jsonl

from .decide import Decision, TaskInput


class Split(Enum):
    """The three v1 dataset splits (dataset-format.md §4)."""

    TRAIN = "train"
    EVAL = "eval"
    CANARIES = "canaries"


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
    unique within a file, ``meta.json``'s ``schema_version`` must match
    the loader's, and :meth:`load_all` rejects a record present in more
    than one split (canonical ``(task, context)`` hash).
    """

    supported_schema_version = 1

    def load(self) -> list[Example]:
        return self._load_path(self.path)

    def load_split(self, split: Split) -> list[Example]:
        """Load one split from ``datasets/<split>.jsonl``.

        Falls back to ``example_pairs.jsonl`` (the v2 flat format, no
        envelope) when the split file is absent — backward compat. The
        canaries split returns only canary records; the train/eval
        splits exclude them (dataset-format.md §6).
        """
        self._check_schema_version()
        split_path = self.datasets_dir / f"{split.value}.jsonl"
        if split_path.is_file():
            examples = self._load_path(split_path, require_envelope=True)
        else:
            fallback = self.datasets_dir / "example_pairs.jsonl"
            if not fallback.is_file():
                raise DatasetError(
                    f"no dataset file for split {split.value} in {self.datasets_dir}"
                )
            examples = self._load_path(fallback)
        if split is Split.CANARIES:
            examples = [ex for ex in examples if ex.canary]
        else:
            examples = [ex for ex in examples if not ex.canary]
        return examples

    def load_all(self) -> DatasetBundle:
        """Load the full three-split dataset as a frozen bundle."""
        self._check_schema_version()
        train = tuple(self.load_split(Split.TRAIN))
        eval_ = tuple(self.load_split(Split.EVAL))
        canaries = tuple(self.load_split(Split.CANARIES))
        self._check_no_cross_split_collisions(
            [("train", train), ("eval", eval_), ("canaries", canaries)]
        )
        return DatasetBundle(
            train=train,
            eval=eval_,
            canaries=canaries,
            dataset_version=self.dataset_version,
        )

    @property
    def datasets_dir(self) -> Path:
        """The module's dataset directory (a directory path, or a file's parent)."""
        return self.path if self.path.is_dir() else self.path.parent

    @property
    def dataset_version(self) -> str:
        """Dataset version from ``meta.json``; ``"0.1.0"`` when absent."""
        data = self._read_meta()
        version = data.get("dataset_version")
        return version if isinstance(version, str) else "0.1.0"

    def _read_meta(self) -> dict:
        """Read ``meta.json``; ``{}`` when absent. DatasetError on bad content."""
        meta = self.datasets_dir / "meta.json"
        try:
            text = meta.read_text()
        except OSError:
            return {}
        try:
            data = json.loads(text)
        except json.JSONDecodeError as exc:
            raise DatasetError(f"{meta}: invalid JSON: {exc}") from exc
        if not isinstance(data, dict):
            raise DatasetError(
                f"{meta}: meta.json must be a JSON object, got {type(data).__name__}"
            )
        return data

    def _check_schema_version(self) -> None:
        data = self._read_meta()
        schema_version = data.get("schema_version")
        if schema_version is None:
            return
        if not isinstance(schema_version, int) or isinstance(schema_version, bool):
            raise DatasetError(
                f"{self.datasets_dir / 'meta.json'}: schema_version must be an integer"
            )
        if schema_version != self.supported_schema_version:
            raise DatasetError(
                f"{self.datasets_dir / 'meta.json'}: schema_version {schema_version} "
                f"unsupported; loader supports {self.supported_schema_version}"
            )

    def _load_path(self, path: Path, require_envelope: bool = False) -> list[Example]:
        records = load_jsonl(path)
        examples: list[Example] = []
        seen_ids: dict[str, int] = {}
        for line_no, record in enumerate(records, start=1):
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
