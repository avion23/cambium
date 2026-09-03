"""Small Python AST search tools for coding-agent code navigation.

The optional tree-sitter backend is preferred when both ``tree-sitter`` and
``tree-sitter-python`` are installed.  The stdlib :mod:`ast` walker is always
available as a fallback.  :func:`backend` reports the backend selected when
this module was imported.

Positions use one-based lines and zero-based, character-based columns.  The
tree-sitter API reports byte offsets for UTF-8 input, so this module parses
encoded source and converts byte ranges back to Python string positions before
decoding or reporting them.  This keeps signatures and locations correct when
non-ASCII text occurs before a symbol.

Reference search is intentionally per-file.  It finds identifier occurrences
in one source string; it does not resolve imports or build a cross-file index.
That index belongs to the workspace-aware v2.1 tooling layer.
"""

from __future__ import annotations

import ast as _stdlib_ast
import unicodedata
from bisect import bisect_right
from collections.abc import Iterator
from dataclasses import dataclass
from typing import Any, cast

try:
    import tree_sitter_python as _tree_sitter_python  # type: ignore[import-not-found]  # pyright: ignore[reportMissingImports]
    from tree_sitter import Language as _TreeSitterLanguage
    from tree_sitter import Parser as _TreeSitterParser
except ImportError:  # Optional dependency: the stdlib backend remains usable.
    _tree_sitter_python = None
    _TreeSitterLanguage = None  # type: ignore[misc,assignment]
    _TreeSitterParser = None  # type: ignore[misc,assignment]


try:
    if _TreeSitterLanguage is None or _tree_sitter_python is None:
        _TREE_SITTER_LANGUAGE = None
    else:
        _TREE_SITTER_LANGUAGE = cast(Any, _TreeSitterLanguage)(_tree_sitter_python.language())
except (AttributeError, TypeError):
    # An incomplete or incompatible optional installation is equivalent to an
    # absent installation.  The stdlib backend must still be importable.
    _TREE_SITTER_LANGUAGE = None


_TREE_DEFINITION_TYPES = frozenset({"function_definition", "class_definition"})

__all__ = [
    "backend",
    "extract_signature",
    "find_definitions",
    "find_references",
]


@dataclass(frozen=True, slots=True)
class _SourceIndex:
    """Map UTF-8 byte offsets to source line and character-column positions."""

    source: str
    encoded: bytes
    line_starts: tuple[int, ...]

    @classmethod
    def from_source(cls, source: str) -> _SourceIndex:
        encoded = source.encode("utf-8")
        line_starts = [0]
        line_starts.extend(offset + 1 for offset, byte in enumerate(encoded) if byte == ord("\n"))
        return cls(source, encoded, tuple(line_starts))

    def position(self, byte_offset: int) -> tuple[int, int]:
        """Return a one-based line and zero-based character column."""
        bounded_offset = max(0, min(byte_offset, len(self.encoded)))
        line_index = bisect_right(self.line_starts, bounded_offset) - 1
        line_start = self.line_starts[line_index]
        prefix = self.encoded[line_start:bounded_offset].decode("utf-8")
        return line_index + 1, len(prefix)

    def byte_offset(self, line: int, column: int) -> int:
        """Convert an AST line plus UTF-8 byte column to an absolute offset."""
        line_index = max(0, min(line - 1, len(self.line_starts) - 1))
        return min(self.line_starts[line_index] + max(column, 0), len(self.encoded))

    def decode(self, start: int, end: int) -> str:
        """Decode a tree-sitter byte range without slicing ``str`` by bytes."""
        return self.encoded[start:end].decode("utf-8")


@dataclass(frozen=True, slots=True)
class _Definition:
    name: str
    kind: str
    start_byte: int
    end_byte: int
    signature: str
    body_lines: int


def backend() -> str:
    """Return ``"tree-sitter"`` or ``"stdlib"`` for the active backend."""
    return "tree-sitter" if _TREE_SITTER_LANGUAGE is not None else "stdlib"


def _first_line(text: str) -> str:
    lines = text.splitlines()
    if lines:
        return lines[0].strip()
    return text.strip()


def _line_text(index: _SourceIndex, line: int) -> str:
    lines = index.source.splitlines()
    if 1 <= line <= len(lines):
        return lines[line - 1]
    return ""


def _definition_result(
    index: _SourceIndex,
    definition: _Definition,
    *,
    first_line: bool,
    path: str = "",
) -> dict[str, Any]:
    line, column = index.position(definition.start_byte)
    result: dict[str, Any] = {
        "name": definition.name,
        "kind": definition.kind,
        "line": line,
        "col": column,
        "signature": _first_line(definition.signature) if first_line else definition.signature,
    }
    if path:
        result["path"] = path
    return result


def _signature_result(index: _SourceIndex, definition: _Definition) -> dict[str, Any]:
    line, column = index.position(definition.start_byte)
    return {
        "name": definition.name,
        "kind": definition.kind,
        "line": line,
        "col": column,
        "signature": definition.signature,
        "body_lines": definition.body_lines,
    }


def _tree_parser() -> Any:
    if _TREE_SITTER_LANGUAGE is None or _TreeSitterParser is None:
        raise RuntimeError("tree-sitter backend is not available")
    return cast(Any, _TreeSitterParser)(_TREE_SITTER_LANGUAGE)


def _tree_root(index: _SourceIndex) -> Any:
    root = _tree_parser().parse(index.encoded).root_node
    if root.has_error:
        try:
            _stdlib_ast.parse(index.source)
        except SyntaxError:
            raise
        raise SyntaxError("tree-sitter reported a parse error")
    return root


def _normalize_name(name: str) -> str:
    return unicodedata.normalize("NFKC", name)


def _tree_walk(node: Any) -> Iterator[Any]:
    stack = list(reversed(node.named_children))
    while stack:
        current = stack.pop()
        yield current
        stack.extend(reversed(current.named_children))


def _tree_definition_node(node: Any) -> Any | None:
    if node.type in _TREE_DEFINITION_TYPES:
        return node
    if node.type == "decorated_definition":
        for child in node.named_children:
            if child.type in _TREE_DEFINITION_TYPES:
                return child
    return None


def _tree_definition(index: _SourceIndex, node: Any) -> _Definition:
    name_node = node.child_by_field_name("name")
    if name_node is None:
        raise ValueError(f"definition has no name: {node.type}")
    name = _normalize_name(index.decode(name_node.start_byte, name_node.end_byte))
    kind = "class" if node.type == "class_definition" else "function"
    body_node = node.child_by_field_name("body")
    if body_node is None:
        signature = _line_text(index, node.start_point.row + 1).strip()
        body_lines = 0
    else:
        # ``start_byte`` and ``body_node.start_byte`` are byte offsets.  Decode
        # this range first; never use it as a slice into ``index.source``.
        signature = index.decode(node.start_byte, body_node.start_byte).strip()
        body_line = index.position(body_node.start_byte)[0]
        end_line = index.position(node.end_byte)[0]
        body_lines = max(0, end_line - body_line + 1)
    return _Definition(name, kind, node.start_byte, node.end_byte, signature, body_lines)


def _tree_definitions(root: Any, index: _SourceIndex) -> list[_Definition]:
    definitions: list[_Definition] = []
    for node in _tree_walk(root):
        if node.type in _TREE_DEFINITION_TYPES:
            definitions.append(_tree_definition(index, node))
    return definitions


def _find_definitions_tree(source: str, path: str = "") -> list[dict[str, Any]]:
    index = _SourceIndex.from_source(source)
    root = _tree_root(index)
    definitions: list[dict[str, Any]] = []
    for node in root.named_children:
        definition_node = _tree_definition_node(node)
        if definition_node is None:
            continue
        definition = _tree_definition(index, definition_node)
        definitions.append(_definition_result(index, definition, first_line=True, path=path))
    return definitions


def _find_references_tree(source: str, name: str) -> list[dict[str, Any]]:
    index = _SourceIndex.from_source(source)
    root = _tree_root(index)
    normalized_name = _normalize_name(name)
    references: list[dict[str, Any]] = []
    for node in _tree_walk(root):
        if node.type != "identifier":
            continue
        if _normalize_name(index.decode(node.start_byte, node.end_byte)) != normalized_name:
            continue
        line, column = index.position(node.start_byte)
        references.append({"name": normalized_name, "line": line, "col": column})
    references.sort(key=lambda reference: (reference["line"], reference["col"]))
    return references


def _extract_signature_tree(source: str, name: str) -> dict[str, Any] | None:
    index = _SourceIndex.from_source(source)
    root = _tree_root(index)
    normalized_name = _normalize_name(name)
    definitions = _tree_definitions(root, index)
    definitions.sort(key=lambda definition: definition.start_byte)
    for definition in definitions:
        if definition.name == normalized_name:
            return _signature_result(index, definition)
    return None


def _ast_kind(node: _stdlib_ast.AST) -> str | None:
    if isinstance(node, _stdlib_ast.ClassDef):
        return "class"
    if isinstance(node, _stdlib_ast.FunctionDef | _stdlib_ast.AsyncFunctionDef):
        return "function"
    return None


def _ast_start_byte(index: _SourceIndex, node: _stdlib_ast.AST) -> int:
    return index.byte_offset(node.lineno, node.col_offset)  # type: ignore[attr-defined]


def _ast_definition(index: _SourceIndex, node: _stdlib_ast.AST) -> _Definition:
    kind = _ast_kind(node)
    if kind is None:
        raise ValueError(f"not a definition: {type(node).__name__}")
    start_byte = _ast_start_byte(index, node)
    body = node.body  # type: ignore[attr-defined]
    if body:
        body_start_byte = _ast_start_byte(index, body[0])
        signature = index.decode(start_byte, body_start_byte).strip()
        body_lines = max(0, node.end_lineno - body[0].lineno + 1)  # type: ignore[attr-defined]
    else:
        signature = _line_text(index, node.lineno).strip()  # type: ignore[attr-defined]
        body_lines = 0
    return _Definition(
        _normalize_name(node.name),  # type: ignore[attr-defined]
        kind,
        start_byte,
        index.byte_offset(node.end_lineno, node.end_col_offset),  # type: ignore[attr-defined]
        signature,
        body_lines,
    )


def _ast_all_definitions(tree: _stdlib_ast.AST, index: _SourceIndex) -> list[_Definition]:
    definitions = [
        _ast_definition(index, node)
        for node in _stdlib_ast.walk(tree)
        if _ast_kind(node) is not None
    ]
    definitions.sort(key=lambda definition: definition.start_byte)
    return definitions


def _find_definitions_stdlib(source: str, path: str = "") -> list[dict[str, Any]]:
    index = _SourceIndex.from_source(source)
    tree = _stdlib_ast.parse(source)
    definitions: list[dict[str, Any]] = []
    for node in tree.body:
        if _ast_kind(node) is None:
            continue
        definition = _ast_definition(index, node)
        definitions.append(_definition_result(index, definition, first_line=True, path=path))
    return definitions


def _attribute_position(index: _SourceIndex, node: _stdlib_ast.Attribute) -> tuple[int, int]:
    """Locate the attribute tail, not the beginning of the whole expression."""
    end_line = cast(int, node.end_lineno)
    end_column = cast(int, node.end_col_offset)
    end_byte = index.byte_offset(end_line, end_column)
    name_bytes = node.attr.encode("utf-8")
    start_byte = end_byte - len(name_bytes)
    if start_byte >= 0 and index.encoded[start_byte:end_byte] == name_bytes:
        return index.position(start_byte)
    return index.position(_ast_start_byte(index, node))


def _find_references_stdlib(source: str, name: str) -> list[dict[str, Any]]:
    index = _SourceIndex.from_source(source)
    tree = _stdlib_ast.parse(source)
    normalized_name = _normalize_name(name)
    references: list[dict[str, Any]] = []
    for node in _stdlib_ast.walk(tree):
        if isinstance(node, _stdlib_ast.Name) and _normalize_name(node.id) == normalized_name:
            line, column = index.position(_ast_start_byte(index, node))
        elif (
            isinstance(node, _stdlib_ast.Attribute)
            and _normalize_name(node.attr) == normalized_name
        ):
            line, column = _attribute_position(index, node)
        else:
            continue
        references.append({"name": normalized_name, "line": line, "col": column})
    references.sort(key=lambda reference: (reference["line"], reference["col"]))
    return references


def _extract_signature_stdlib(source: str, name: str) -> dict[str, Any] | None:
    index = _SourceIndex.from_source(source)
    tree = _stdlib_ast.parse(source)
    normalized_name = _normalize_name(name)
    for definition in _ast_all_definitions(tree, index):
        if definition.name == normalized_name:
            return _signature_result(index, definition)
    return None


def find_definitions(source: str, path: str = "") -> list[dict[str, Any]]:
    """Return top-level function and class definitions in source.

    Each result contains ``name``, ``kind`` (``"function"`` or ``"class"``),
    one-based ``line``, zero-based ``col``, and the first line of the
    definition header as ``signature``.  A non-empty ``path`` is copied into
    each result for callers searching more than one file.
    """
    if _TREE_SITTER_LANGUAGE is not None:
        return _find_definitions_tree(source, path)
    return _find_definitions_stdlib(source, path)


def find_references(source: str, name: str) -> list[dict[str, Any]]:
    """Return matching identifier occurrences from this file only.

    Results contain ``name``, one-based ``line``, and zero-based ``col``.
    Tree-sitter reports every matching ``identifier`` node.  The stdlib
    fallback reports matching :class:`ast.Name` nodes and attribute tails.
    Import resolution and cross-file references are deliberately out of scope.
    """
    if _TREE_SITTER_LANGUAGE is not None:
        return _find_references_tree(source, name)
    return _find_references_stdlib(source, name)


def extract_signature(source: str, name: str) -> dict[str, Any] | None:
    """Return the first named function/class header and its physical body size.

    The result contains ``name``, ``kind``, ``line``, ``col``, the complete
    definition header in ``signature`` (including continuation lines), and
    ``body_lines``.  Nested definitions are considered.  ``None`` means that
    no function or class with that name exists in this source file.
    """
    if _TREE_SITTER_LANGUAGE is not None:
        return _extract_signature_tree(source, name)
    return _extract_signature_stdlib(source, name)
