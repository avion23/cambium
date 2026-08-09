"""Dataset loader for the should_decompose module."""

from __future__ import annotations

from cambium.modules.base import DatasetError, DatasetLoader, Example, load_jsonl

from .decide import TaskInput


class ExampleDatasetLoader(DatasetLoader):
    """Loads input/expected pairs from a JSONL file.

    Each line: ``{"input": {"task", "context"}, "expected":
    {"decompose", "reason"}}``. The optional boolean ``canary`` field
    flags dataset-integrity entries planted to catch reward hacking in
    future evals; they are loaded and scored like any other entry.
    """

    def load(self) -> list[Example]:
        records = load_jsonl(self.path)
        examples: list[Example] = []
        for line_no, record in enumerate(records, start=1):
            self._validate(record, line_no)
            try:
                task_input = TaskInput(**record["input"])
            except TypeError as exc:
                raise DatasetError(
                    f"{self.path}:{line_no}: invalid input fields: {exc}"
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
        canary = record.get("canary", False)
        if not isinstance(canary, bool):
            raise DatasetError(f"{self.path}:{line_no}: canary must be a boolean")
