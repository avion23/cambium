"""Module contract shared by every Cambium decision module.

The DSPy seam: each module ships a rule-engine (or pure) implementation
of ``decide`` today; a DSPy program implementing the same interface can
replace it later without touching callers, the dataset, or the metric.
"""

from __future__ import annotations

import json
from abc import ABC, abstractmethod
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Protocol


class Output(Protocol):
    """Typed prediction returned by a module's ``decide``."""


@dataclass(frozen=True, slots=True)
class Example:
    """One dataset record: input, expected, and (optional) prediction."""

    input: Any
    expected: dict[str, Any]
    prediction: Any | None = None
    canary: bool = False

    def with_prediction(self, prediction: Any) -> "Example":
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
