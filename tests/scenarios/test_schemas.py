"""Scenario tests for stdlib dataclass conversion and worker tool schemas."""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
from pathlib import Path
from typing import Any

from cambium.schemas import TOOL_SCHEMAS, dataclass_to_json_schema, validate_tool_call


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


def _assert_well_formed_schema(node: dict[str, Any]) -> None:
    valid_types = {"array", "boolean", "integer", "null", "number", "object", "string"}
    if "type" in node:
        schema_type = node["type"]
        if isinstance(schema_type, list):
            assert all(item in valid_types for item in schema_type)
        else:
            assert schema_type in valid_types
    if "enum" in node:
        assert isinstance(node["enum"], list)
    if "anyOf" in node:
        assert isinstance(node["anyOf"], list)
        for option in node["anyOf"]:
            _assert_well_formed_schema(option)
    if node.get("type") == "array":
        _assert_well_formed_schema(node.get("items", {}))
    if node.get("type") == "object":
        properties = node.get("properties", {})
        assert isinstance(properties, dict)
        required = node.get("required", [])
        assert all(name in properties for name in required)
        for property_schema in properties.values():
            _assert_well_formed_schema(property_schema)
        additional = node.get("additionalProperties", True)
        if isinstance(additional, dict):
            _assert_well_formed_schema(additional)


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


def test_tool_schemas_are_well_formed_and_strict() -> None:
    assert [schema["name"] for schema in TOOL_SCHEMAS] == [
        "read_file",
        "write_file",
        "edit_file",
        "grep_code",
        "git_op",
        "run_shell",
    ]
    for schema in TOOL_SCHEMAS:
        assert set(schema) == {"name", "description", "parameters"}
        assert isinstance(schema["description"], str) and schema["description"]
        assert schema["parameters"]["additionalProperties"] is False
        _assert_well_formed_schema(schema["parameters"])

    git_schema = next(schema for schema in TOOL_SCHEMAS if schema["name"] == "git_op")
    assert git_schema["parameters"]["properties"]["op"]["enum"] == [
        "add",
        "commit",
        "status",
        "diff",
        "log",
        "stash",
    ]


def _tool(name: str) -> dict[str, Any]:
    return next(schema for schema in TOOL_SCHEMAS if schema["name"] == name)


def test_validate_tool_call_reports_missing_required_argument() -> None:
    assert validate_tool_call(_tool("read_file"), {}) == [
        "validation failed: missing 'path' (string)"
    ]


def test_validate_tool_call_reports_wrong_type() -> None:
    errors = validate_tool_call(_tool("read_file"), {"path": 42})
    assert errors == ["validation failed: 'path' must be string"]


def test_validate_tool_call_reports_unknown_argument() -> None:
    errors = validate_tool_call(_tool("read_file"), {"path": "README.md", "extra": True})
    assert errors == ["validation failed: unknown argument 'extra'"]


def test_validate_tool_call_reports_bad_enum() -> None:
    errors = validate_tool_call(
        _tool("git_op"),
        {"op": "push", "args": "origin main"},
    )
    assert errors == [
        "validation failed: 'op' must be one of ['add', 'commit', 'status', 'diff', 'log', 'stash']"
    ]


def test_validate_tool_call_accepts_valid_arguments() -> None:
    assert validate_tool_call(
        _tool("edit_file"),
        {"path": "src/app.py", "old_string": "old", "new_string": "new"},
    ) == []
