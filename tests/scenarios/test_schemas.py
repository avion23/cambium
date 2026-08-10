"""Scenario tests for stdlib dataclass conversion and worker tool schemas."""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
from pathlib import Path

from cambium.schemas import dataclass_to_json_schema


class Mode(Enum):
    FAST = "fast"
    SAFE = "safe"


@dataclass
class Nested:
    count: int = field(doc="Number of nested items.")


@dataclass
class Sample:
    name: str = field(doc="Display name.")
    mode: Mode = field(doc="Execution mode.")
    nested: Nested = field(doc="Nested configuration.")
    maybe: str | None = field(doc="An optional label.")
    values: list[float] = field(doc="Measured values.")
    labels: dict[str, int] = field(doc="Counts by label.")
    location: Path = field(doc="Filesystem location.")
    note: str | None = field(
        default=None,
        metadata={"description": "An optional note."},
    )


def test_dataclass_converter_handles_nested_types_and_descriptions() -> None:
    schema = dataclass_to_json_schema(Sample)

    assert schema["type"] == "object"
    assert schema["additionalProperties"] is False
    assert schema["required"] == [
        "name",
        "mode",
        "nested",
        "maybe",
        "values",
        "labels",
        "location",
    ]
    assert "note" not in schema["required"]
    assert schema["properties"]["name"] == {
        "type": "string",
        "description": "Display name.",
    }
    assert schema["properties"]["mode"]["type"] == "string"
    assert schema["properties"]["mode"]["enum"] == ["fast", "safe"]
    assert schema["properties"]["nested"]["properties"]["count"]["type"] == "integer"
    assert schema["properties"]["maybe"]["anyOf"] == [
        {"type": "string"},
        {"type": "null"},
    ]
    assert schema["properties"]["values"] == {
        "type": "array",
        "items": {"type": "number"},
        "description": "Measured values.",
    }
    assert schema["properties"]["labels"]["additionalProperties"] == {"type": "integer"}
    assert schema["properties"]["location"]["type"] == "string"
    assert schema["properties"]["note"]["description"] == "An optional note."
