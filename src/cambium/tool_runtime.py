"""Immutable tool registry and provider-native schema normalization.

Cambium keeps a small structured core for high-frequency, invariant-sensitive
operations. Short Python snippets remain an explicit scratch capability rather
than replacing read/write/navigation tools. Registries are constructed per
worker/task and frozen; there is no process-global mutable plugin table.
"""

from __future__ import annotations

import json
from collections.abc import Iterable, Mapping, Sequence
from copy import deepcopy
from dataclasses import dataclass
from enum import StrEnum
from types import MappingProxyType
from typing import Any, Protocol


class ToolCapability(StrEnum):
    READ = "read"
    WRITE = "write"
    SHELL = "shell"
    PYTHON = "python"
    NAVIGATION = "navigation"
    LSP = "lsp"


class ToolExecutor(Protocol):
    def __call__(self, arguments: Mapping[str, Any], context: Any) -> Any: ...


@dataclass(frozen=True, slots=True)
class ToolDefinition:
    name: str
    description: str
    parameters: Mapping[str, Any]
    capability: ToolCapability
    executor: ToolExecutor | None = None

    def __post_init__(self) -> None:
        parameters = deepcopy(dict(self.parameters))
        if not self.name or not self.description:
            raise ValueError("tool definition name/description must be non-empty")
        if parameters.get("type") != "object":
            raise ValueError("tool parameters must be an object schema")
        object.__setattr__(self, "parameters", parameters)

    def openai_schema(self) -> dict[str, Any]:
        return {
            "type": "function",
            "function": {
                "name": self.name,
                "description": self.description,
                "parameters": deepcopy(self.parameters),
            },
        }


class ToolRegistry:
    """Frozen name-to-definition map with deterministic schema order."""

    def __init__(self, definitions: Iterable[ToolDefinition]) -> None:
        ordered: list[ToolDefinition] = []
        by_name: dict[str, ToolDefinition] = {}
        for definition in definitions:
            owned = ToolDefinition(
                definition.name,
                definition.description,
                definition.parameters,
                definition.capability,
                definition.executor,
            )
            if owned.name in by_name:
                raise ValueError(f"duplicate tool definition {owned.name!r}")
            by_name[owned.name] = owned
            ordered.append(owned)
        self._ordered = tuple(ordered)
        self._by_name = MappingProxyType(by_name)

    @staticmethod
    def _copy_definition(definition: ToolDefinition) -> ToolDefinition:
        return ToolDefinition(
            definition.name,
            definition.description,
            definition.parameters,
            definition.capability,
            definition.executor,
        )

    @property
    def definitions(self) -> tuple[ToolDefinition, ...]:
        return tuple(self._copy_definition(item) for item in self._ordered)

    @property
    def schemas(self) -> tuple[dict[str, Any], ...]:
        return tuple(definition.openai_schema() for definition in self._ordered)

    def get(self, name: str) -> ToolDefinition | None:
        definition = self._by_name.get(name)
        return None if definition is None else self._copy_definition(definition)

    def extend(self, definitions: Iterable[ToolDefinition]) -> ToolRegistry:
        return ToolRegistry((*self._ordered, *definitions))

    def execute(self, name: str, arguments: Mapping[str, Any], context: Any) -> Any:
        definition = self._by_name.get(name)
        if definition is None or definition.executor is None:
            raise KeyError(name)
        return definition.executor(arguments, context)


def _legacy_definition(schema: Mapping[str, Any]) -> ToolDefinition:
    function = schema.get("function")
    source = function if isinstance(function, Mapping) else schema
    name = source.get("name")
    description = source.get("description", f"Cambium tool {name}")
    parameters = source.get("parameters")
    if not isinstance(name, str) or not name:
        raise ValueError("legacy tool schema has no name")
    if not isinstance(description, str) or not description:
        raise ValueError(f"legacy tool {name!r} has no description")
    if not isinstance(parameters, Mapping):
        raise ValueError(f"legacy tool {name!r} has no parameter schema")
    capability = ToolCapability.READ
    if name in {"write_file", "apply_patch", "edit", "delete_file"}:
        capability = ToolCapability.WRITE
    elif name == "run_shell":
        capability = ToolCapability.SHELL
    elif name == "run_python":
        capability = ToolCapability.PYTHON
    elif name in {"symbol_search", "find_references", "read_symbol"}:
        capability = ToolCapability.NAVIGATION
    elif name == "lsp_query":
        capability = ToolCapability.LSP
    return ToolDefinition(name, description, dict(parameters), capability)


def registry_from_schemas(schemas: Sequence[Mapping[str, Any]]) -> ToolRegistry:
    """Validate legacy/static schemas and freeze one task-local registry."""

    return ToolRegistry(_legacy_definition(schema) for schema in schemas)


def _function_payload(schema: Mapping[str, Any]) -> dict[str, Any]:
    function = schema.get("function")
    source = function if isinstance(function, Mapping) else schema
    name = source.get("name")
    description = source.get("description", "")
    parameters = source.get("parameters", {"type": "object", "properties": {}})
    if not isinstance(name, str) or not name:
        raise ValueError("native tool schema has no function name")
    if not isinstance(description, str):
        raise ValueError(f"native tool {name!r} description must be a string")
    if not isinstance(parameters, Mapping):
        raise ValueError(f"native tool {name!r} parameters must be an object")
    return {
        "name": name,
        "description": description,
        "parameters": dict(parameters),
    }


def normalize_native_tool_schemas(
    schemas: Sequence[Mapping[str, Any]], *, responses_api: bool
) -> list[dict[str, Any]]:
    """Normalize one registry for Chat Completions or Responses API."""

    normalized: list[dict[str, Any]] = []
    names: set[str] = set()
    for schema in schemas:
        payload = _function_payload(schema)
        name = payload["name"]
        if name in names:
            raise ValueError(f"duplicate native tool schema {name!r}")
        names.add(name)
        if responses_api:
            normalized.append({"type": "function", **payload})
        else:
            normalized.append({"type": "function", "function": payload})
    return normalized


def canonical_tool_schema_digest(schemas: Sequence[Mapping[str, Any]]) -> str:
    """Canonical schema bytes used by context/cache identity code."""

    return json.dumps(
        normalize_native_tool_schemas(schemas, responses_api=False),
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
        allow_nan=False,
    )


__all__ = [
    "ToolCapability",
    "ToolDefinition",
    "ToolExecutor",
    "ToolRegistry",
    "canonical_tool_schema_digest",
    "normalize_native_tool_schemas",
    "registry_from_schemas",
]
