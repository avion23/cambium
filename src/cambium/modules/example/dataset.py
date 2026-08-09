"""Dataset loader for the should_decompose module."""

from __future__ import annotations

import json
from dataclasses import dataclass
from enum import Enum
from pathlib import Path

from cambium.modules.base import DatasetError, DatasetLoader, Example, load_jsonl

from .decide import TaskInput


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
    {"decompose", "reason"}}``. The optional boolean ``canary`` field
    flags dataset-integrity entries planted to catch reward hacking in
    future evals; they are loaded and scored like any other entry by
    :meth:`load`. Split-aware loads (:meth:`load_split`, :meth:`load_all`)
    read ``datasets/<split>.jsonl`` and exclude canaries from train/eval
    (they load only via :data:`Split.CANARIES`).
    """

    def load(self) -> list[Example]:
        return self._load_path(self.path)

    def load_split(self, split: Split) -> list[Example]:
        """Load one split from ``datasets/<split>.jsonl``.

        Falls back to ``example_pairs.jsonl`` when the split file is
        absent (backward compat). The canaries split returns only canary
        records; the train/eval splits exclude them (dataset-format.md §6).
        """
        split_path = self.datasets_dir / f"{split.value}.jsonl"
        path = (
            split_path
            if split_path.is_file()
            else self.datasets_dir / "example_pairs.jsonl"
        )
        if not path.is_file():
            raise DatasetError(
                f"no dataset file for split {split.value} in {self.datasets_dir}"
            )
        examples = self._load_path(path)
        if split is Split.CANARIES:
            examples = [ex for ex in examples if ex.canary]
        else:
            examples = [ex for ex in examples if not ex.canary]
        return examples

    def load_all(self) -> DatasetBundle:
        """Load the full three-split dataset as a frozen bundle."""
        return DatasetBundle(
            train=tuple(self.load_split(Split.TRAIN)),
            eval=tuple(self.load_split(Split.EVAL)),
            canaries=tuple(self.load_split(Split.CANARIES)),
            dataset_version=self.dataset_version,
        )

    @property
    def datasets_dir(self) -> Path:
        """The module's dataset directory (a directory path, or a file's parent)."""
        return self.path if self.path.is_dir() else self.path.parent

    @property
    def dataset_version(self) -> str:
        """Dataset version from ``meta.json``; ``"0.1.0"`` when absent."""
        meta = self.datasets_dir / "meta.json"
        try:
            data = json.loads(meta.read_text())
        except (OSError, json.JSONDecodeError):
            return "0.1.0"
        version = data.get("dataset_version")
        return version if isinstance(version, str) else "0.1.0"

    def _load_path(self, path: Path) -> list[Example]:
        records = load_jsonl(path)
        examples: list[Example] = []
        for line_no, record in enumerate(records, start=1):
            self._validate(record, line_no)
            try:
                task_input = TaskInput(**record["input"])
            except TypeError as exc:
                raise DatasetError(
                    f"{path}:{line_no}: invalid input fields: {exc}"
                ) from exc
            examples.append(
                Example(
                    input=task_input,
                    expected=record["expected"],
                    canary=bool(record.get("canary", False)),
                )
            )
        return examples

    def _validate(self, record: dict, line_no: int) -> None:
        if not {"input", "expected"} <= record.keys():
            raise DatasetError(
                f"{self.path}:{line_no}: record must have 'input' and 'expected'"
            )
        for key in ("input", "expected"):
            if not isinstance(record[key], dict):
                raise DatasetError(f"{self.path}:{line_no}: {key} must be an object")
        if not isinstance(record["input"].get("task"), str):
            raise DatasetError(f"{self.path}:{line_no}: input.task must be a string")
        if not isinstance(record["expected"].get("decompose"), bool):
            raise DatasetError(
                f"{self.path}:{line_no}: expected.decompose must be a boolean"
            )
        if not isinstance(record["expected"].get("reason"), str):
            raise DatasetError(
                f"{self.path}:{line_no}: expected.reason must be a string"
            )
        canary = record.get("canary", False)
        if not isinstance(canary, bool):
            raise DatasetError(f"{self.path}:{line_no}: canary must be a boolean")
