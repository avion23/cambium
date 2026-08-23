"""JSON Schema definitions and validation for worker tool calls.

The module deliberately uses only the standard library.  The schemas are
plain dictionaries so they can be passed directly to an LLM client, while
``validate_tool_call`` supplies deterministic feedback when a call is invalid.
"""

from __future__ import annotations

import re
import types
from dataclasses import MISSING, fields, is_dataclass
from enum import Enum
from pathlib import Path
from typing import Annotated, Any, Literal, Union, get_args, get_origin, get_type_hints


def _enum_schema(enum_type: type[Enum]) -> dict[str, Any]:
    values = [member.value for member in enum_type]
    schema: dict[str, Any] = {"enum": values}
    if values and all(type(value) is type(values[0]) for value in values):
        schema.update(_type_schema(type(values[0])))
    return schema


def _type_schema(annotation: Any) -> dict[str, Any]:
    origin = get_origin(annotation)

    if origin is Annotated:
        base, *metadata = get_args(annotation)
        schema = _type_schema(base)
        descriptions = [item for item in metadata if isinstance(item, str) and item]
        if descriptions:
            schema.setdefault("description", descriptions[0])
        return schema

    if origin in (Union, types.UnionType):
        return {"anyOf": [_type_schema(option) for option in get_args(annotation)]}

    if origin is Literal:
        values = list(get_args(annotation))
        literal_schema: dict[str, Any] = {"enum": values}
        if values and all(type(value) is type(values[0]) for value in values):
            literal_schema.update(_type_schema(type(values[0])))
        return literal_schema

    if annotation in (Any, object):
        return {}
    if annotation in (None, type(None)):
        return {"type": "null"}
    if isinstance(annotation, type) and issubclass(annotation, Enum):
        return _enum_schema(annotation)
    if isinstance(annotation, type) and issubclass(annotation, Path):
        return {"type": "string"}
    if annotation is bool:
        return {"type": "boolean"}
    if annotation is int:
        return {"type": "integer"}
    if annotation is float:
        return {"type": "number"}
    if annotation is str:
        return {"type": "string"}
    if annotation is list or origin is list:
        args = get_args(annotation)
        return {"type": "array", "items": _type_schema(args[0]) if args else {}}
    if annotation is dict or origin is dict:
        args = get_args(annotation)
        return {
            "type": "object",
            "additionalProperties": _type_schema(args[1]) if len(args) > 1 else {},
        }
    if isinstance(annotation, type) and is_dataclass(annotation):
        return dataclass_to_json_schema(annotation)
    return {}


def dataclass_to_json_schema(cls: type[Any]) -> dict[str, Any]:
    """Convert a dataclass into a strict JSON Schema object."""
    if not is_dataclass(cls):
        raise TypeError("dataclass_to_json_schema() requires a dataclass")

    target = cls if isinstance(cls, type) else type(cls)
    try:
        annotations = get_type_hints(target, include_extras=True)
    except (NameError, TypeError):
        annotations = {}

    properties: dict[str, Any] = {}
    required: list[str] = []
    for field in fields(cls):
        schema = _type_schema(annotations.get(field.name, field.type))
        description = getattr(field, "doc", None)
        if not description:
            for key in ("description", "doc", "comment", "type_comment"):
                description = field.metadata.get(key)
                if description:
                    break
        if description:
            schema["description"] = str(description).strip()
        properties[field.name] = schema
        if field.default is MISSING and field.default_factory is MISSING:
            required.append(field.name)

    return {
        "type": "object",
        "properties": properties,
        "required": required,
        "additionalProperties": False,
    }


def _parameters(properties: dict[str, dict[str, Any]], required: list[str]) -> dict[str, Any]:
    return {
        "type": "object",
        "properties": properties,
        "required": required,
        "additionalProperties": False,
    }


TOOL_SCHEMAS: list[dict[str, Any]] = [
    {
        "name": "write_file",
        "description": "Write UTF-8 text content to a file inside the worker worktree.",
        "parameters": _parameters(
            {
                "path": {"type": "string", "description": "Path to the file."},
                "content": {"type": "string", "description": "Complete file contents."},
            },
            ["path", "content"],
        ),
    },
    {
        "name": "edit_file",
        "description": "Replace exactly one occurrence of old_string in a worktree file.",
        "parameters": _parameters(
            {
                "path": {"type": "string", "description": "Path to the file."},
                "old_string": {"type": "string", "description": "Text to replace."},
                "new_string": {"type": "string", "description": "Replacement text."},
            },
            ["path", "old_string", "new_string"],
        ),
    },
    {
        "name": "grep_code",
        "description": "Search worktree files for a pattern.",
        "parameters": _parameters(
            {
                "pattern": {"type": "string", "description": "Pattern to search for."},
                "path": {
                    "anyOf": [{"type": "string"}, {"type": "null"}],
                    "description": "File or directory to search; null searches the worktree.",
                },
            },
            ["pattern"],
        ),
    },
    {
        "name": "get_signature",
        "description": "Extract a Python function or class signature from a worktree file.",
        "parameters": _parameters(
            {
                "path": {
                    "type": "string",
                    "minLength": 1,
                    "description": "Path to the Python source file.",
                },
                "symbol": {
                    "type": "string",
                    "minLength": 1,
                    "description": "Function or class name to inspect.",
                },
            },
            ["path", "symbol"],
        ),
    },
    {
        "name": "git_op",
        "description": "Run an allowlisted git operation in the worker worktree.",
        "parameters": _parameters(
            {
                "op": {
                    "type": "string",
                    "enum": ["add", "commit", "status", "diff", "log", "stash"],
                    "description": "Git operation to run.",
                },
                "args": {"type": "string", "description": "Arguments for the operation."},
            },
            ["op", "args"],
        ),
    },
    {
        "name": "run_shell",
        "description": "Run an argv command when shell permission is enabled.",
        "parameters": _parameters(
            {
                "cmd": {
                    "type": "array",
                    "items": {"type": "string"},
                    "description": "Command argv to execute without a shell.",
                },
                "timeout_s": {
                    "type": "integer",
                    "default": 120,
                    "description": "Timeout in seconds.",
                },
            },
            ["cmd"],
        ),
    },
    {
        "name": "read_batch",
        "description": (
            "Read multiple UTF-8 text files inside the worker worktree in one call. "
            "Reading files individually is not available; batch reads are the only "
            "way to read files."
        ),
        "parameters": _parameters(
            {
                "paths": {
                    "type": "array",
                    "items": {"type": "string"},
                    "description": "File paths to read, in order.",
                },
            },
            ["paths"],
        ),
    },
    {
        "name": "delegate",
        "description": (
            "Propose a child task for supervisor validation; the child is admitted "
            "only after this task completes with a result envelope."
        ),
        "parameters": _parameters(
            {
                "child_task_id": {
                    "type": "string",
                    "minLength": 1,
                    "description": "Stable id for the proposed child task.",
                },
                "kind": {
                    "type": "string",
                    "minLength": 1,
                    "description": "Tree kind of the proposed child task.",
                },
                "spec": {
                    "type": "object",
                    "description": (
                        "The child's task spec (task, repo, worktree_path, branch, "
                        "target_file, marker); the supervisor re-validates it."
                    ),
                },
            },
            ["child_task_id", "kind", "spec"],
        ),
    },
]


def _type_matches(value: Any, schema: dict[str, Any]) -> bool:
    if "anyOf" in schema:
        return any(_type_matches(value, option) for option in schema["anyOf"])
    expected = schema.get("type")
    if expected is None:
        return True
    if isinstance(expected, list):
        return any(_type_matches(value, {"type": item}) for item in expected)
    return {
        "null": value is None,
        "boolean": type(value) is bool,
        "integer": type(value) is int,
        "number": type(value) in (int, float),
        "string": type(value) is str,
        "array": type(value) is list,
        "object": type(value) is dict,
    }.get(expected, False)


def _expected_type(schema: dict[str, Any]) -> str:
    expected = schema.get("type")
    if isinstance(expected, list):
        return " or ".join(expected)
    if isinstance(expected, str):
        return expected
    options = schema.get("anyOf")
    if options:
        return " or ".join(_expected_type(option) for option in options)
    return "value"


def _validate_value(schema: dict[str, Any], value: Any, label: str) -> list[str]:
    """Validate one value, including the constraints on nested schemas."""

    options = schema.get("anyOf")
    if isinstance(options, list):
        option_errors = [
            _validate_value(option, value, label) for option in options if isinstance(option, dict)
        ]
        if any(not option_error for option_error in option_errors):
            return []
        return [f"validation failed: '{label}' must be {_expected_type(schema)}"]

    if not _type_matches(value, schema):
        return [f"validation failed: '{label}' must be {_expected_type(schema)}"]

    errors: list[str] = []
    allowed = schema.get("enum")
    if allowed is not None and not any(
        type(value) is type(item) and value == item for item in allowed
    ):
        errors.append(f"validation failed: '{label}' must be one of {allowed!r}")
        return errors

    if type(value) is str:
        min_length = schema.get("minLength")
        if (
            isinstance(min_length, int)
            and not isinstance(min_length, bool)
            and len(value) < min_length
        ):
            unit = "character" if min_length == 1 else "characters"
            errors.append(f"validation failed: '{label}' must have at least {min_length} {unit}")
        max_length = schema.get("maxLength")
        if (
            isinstance(max_length, int)
            and not isinstance(max_length, bool)
            and len(value) > max_length
        ):
            unit = "character" if max_length == 1 else "characters"
            errors.append(f"validation failed: '{label}' must have at most {max_length} {unit}")
        pattern = schema.get("pattern")
        if isinstance(pattern, str):
            try:
                matched = re.search(pattern, value) is not None
            except re.error:
                matched = False
            if not matched:
                errors.append(f"validation failed: '{label}' must match pattern {pattern!r}")

    if type(value) in (int, float):
        minimum = schema.get("minimum")
        if isinstance(minimum, int | float) and not isinstance(minimum, bool):
            if value < minimum:
                errors.append(f"validation failed: '{label}' must be >= {minimum}")
        exclusive_minimum = schema.get("exclusiveMinimum")
        if isinstance(exclusive_minimum, bool):
            if exclusive_minimum and isinstance(minimum, int | float) and value <= minimum:
                errors.append(f"validation failed: '{label}' must be > {minimum}")
        elif isinstance(exclusive_minimum, int | float) and not isinstance(
            exclusive_minimum, bool
        ):
            if value <= exclusive_minimum:
                errors.append(f"validation failed: '{label}' must be > {exclusive_minimum}")
        maximum = schema.get("maximum")
        if isinstance(maximum, int | float) and not isinstance(maximum, bool):
            if value > maximum:
                errors.append(f"validation failed: '{label}' must be <= {maximum}")
        exclusive_maximum = schema.get("exclusiveMaximum")
        if isinstance(exclusive_maximum, bool):
            if exclusive_maximum and isinstance(maximum, int | float) and value >= maximum:
                errors.append(f"validation failed: '{label}' must be < {maximum}")
        elif isinstance(exclusive_maximum, int | float) and not isinstance(
            exclusive_maximum, bool
        ):
            if value >= exclusive_maximum:
                errors.append(f"validation failed: '{label}' must be < {exclusive_maximum}")

    if isinstance(value, list):
        min_items = schema.get("minItems")
        if (
            isinstance(min_items, int)
            and not isinstance(min_items, bool)
            and len(value) < min_items
        ):
            errors.append(f"validation failed: '{label}' must have at least {min_items} items")
        max_items = schema.get("maxItems")
        if (
            isinstance(max_items, int)
            and not isinstance(max_items, bool)
            and len(value) > max_items
        ):
            errors.append(f"validation failed: '{label}' must have at most {max_items} items")
        item_schema = schema.get("items", {})
        if isinstance(item_schema, dict):
            for index, item in enumerate(value):
                errors.extend(_validate_value(item_schema, item, f"{label}[{index}]"))

    if isinstance(value, dict):
        min_properties = schema.get("minProperties")
        if (
            isinstance(min_properties, int)
            and not isinstance(min_properties, bool)
            and len(value) < min_properties
        ):
            errors.append(
                f"validation failed: '{label}' must have at least {min_properties} properties"
            )
        max_properties = schema.get("maxProperties")
        if (
            isinstance(max_properties, int)
            and not isinstance(max_properties, bool)
            and len(value) > max_properties
        ):
            errors.append(
                f"validation failed: '{label}' must have at most {max_properties} properties"
            )
        errors.extend(_validate_object(schema, value, label))
    return errors


def _validate_object(
    schema: dict[str, Any], arguments: dict[str, Any], prefix: str = ""
) -> list[str]:
    errors: list[str] = []
    properties = schema.get("properties", {})
    if not isinstance(properties, dict):
        properties = {}
    for name in schema.get("required", []):
        if name not in arguments:
            label = f"{prefix}.{name}" if prefix else name
            errors.append(
                f"validation failed: missing '{label}' ({_expected_type(properties.get(name, {}))})"
            )

    for name, value in arguments.items():
        label = f"{prefix}.{name}" if prefix else name
        property_schema = properties.get(name)
        if property_schema is None:
            additional = schema.get("additionalProperties")
            if additional is False:
                errors.append(f"validation failed: unknown argument '{label}'")
            elif isinstance(additional, dict):
                errors.extend(_validate_value(additional, value, label))
            continue
        if isinstance(property_schema, dict):
            errors.extend(_validate_value(property_schema, value, label))
    return errors


def validate_tool_call(schema: dict[str, Any], call: dict[str, Any]) -> list[str]:
    """Return LLM-facing structural validation errors for one tool call.

    ``call`` may be the argument object itself or an LLM envelope containing
    ``name`` and ``arguments``.  No values are coerced.
    """
    parameters = schema.get("parameters", schema)
    if not isinstance(parameters, dict) or parameters.get("type") != "object":
        return ["validation failed: schema parameters must be an object"]
    if not isinstance(call, dict):
        return ["validation failed: tool call must be an object"]

    arguments: Any = call
    errors: list[str] = []
    expected_name = schema.get("name")
    if "arguments" in call:
        arguments = call["arguments"]
        if expected_name is not None and "name" in call and call["name"] != expected_name:
            errors.append(f"validation failed: tool name must be '{expected_name}'")
    elif "name" in call and expected_name is not None:
        if call["name"] != expected_name:
            errors.append(f"validation failed: tool name must be '{expected_name}'")
        arguments = {key: value for key, value in call.items() if key != "name"}

    if not isinstance(arguments, dict):
        errors.append("validation failed: 'arguments' must be an object")
        return errors
    errors.extend(_validate_object(parameters, arguments))
    return errors


_RUN_PYTHON_SCHEMA_DIRECT = {
    "name": "run_python",
    "description": (
        "Run a short trusted Python 3 snippet in the worktree for structured "
        "data transformation, inspection, or calculations. Prefer read/search/edit "
        "tools for ordinary repository operations. The process is isolated from "
        "site packages and credential environment, but Cambium is not an OS sandbox."
    ),
    "parameters": {
        "type": "object",
        "properties": {"code": {"type": "string", "maxLength": 32768}},
        "required": ["code"],
        "additionalProperties": False,
    },
}
_RUN_PYTHON_SCHEMA = (
    {"type": "function", "function": _RUN_PYTHON_SCHEMA_DIRECT}
    if TOOL_SCHEMAS and isinstance(TOOL_SCHEMAS[0], dict) and "function" in TOOL_SCHEMAS[0]
    else _RUN_PYTHON_SCHEMA_DIRECT
)
if not any(
    isinstance(item, dict)
    and (
        item.get("name") == "run_python"
        or (isinstance(item.get("function"), dict) and item["function"].get("name") == "run_python")
    )
    for item in TOOL_SCHEMAS
):
    TOOL_SCHEMAS = type(TOOL_SCHEMAS)([*TOOL_SCHEMAS, _RUN_PYTHON_SCHEMA])
