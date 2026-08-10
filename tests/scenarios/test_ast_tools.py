"""Scenario tests for the optional tree-sitter and stdlib AST search tools."""

from __future__ import annotations

import pytest

from cambium import ast_tools

SOURCE = '''"""module docstring with § before the definitions."""


def build(value):
    result = helper(value)
    return result


class Box:
    """A small container."""

    def render(self):
        return helper(self).target


output = helper(build("value"))
'''


def test_definitions_are_top_level_and_include_first_signature_line() -> None:
    definitions = ast_tools.find_definitions(SOURCE, path="sample.py")

    assert definitions == [
        {
            "name": "build",
            "kind": "function",
            "line": 4,
            "col": 0,
            "signature": "def build(value):",
            "path": "sample.py",
        },
        {
            "name": "Box",
            "kind": "class",
            "line": 9,
            "col": 0,
            "signature": "class Box:",
            "path": "sample.py",
        },
    ]


def test_references_are_counted_and_sorted() -> None:
    references = ast_tools.find_references(SOURCE, "helper")

    assert references == [
        {"name": "helper", "line": 5, "col": 13},
        {"name": "helper", "line": 13, "col": 15},
        {"name": "helper", "line": 16, "col": 9},
    ]


def test_signature_extracts_multiline_header_and_body_line_count() -> None:
    source = '''def build(
    value,
):
    result = helper(value)
    return result
'''

    assert ast_tools.extract_signature(source, "build") == {
        "name": "build",
        "kind": "function",
        "line": 1,
        "col": 0,
        "signature": "def build(\n    value,\n):",
        "body_lines": 2,
    }
    assert ast_tools.extract_signature(source, "missing") is None


def test_non_ascii_before_definition_keeps_line_and_signature() -> None:
    source = '# § comment\n\nclass Café:\n    pass\n'

    definition = ast_tools.find_definitions(source)[0]

    assert definition["name"] == "Café"
    assert definition["line"] == 3
    assert definition["col"] == 0
    assert definition["signature"] == "class Café:"


def test_stdlib_backend_helpers_work_without_optional_dependencies() -> None:
    definitions = ast_tools._find_definitions_stdlib(SOURCE, "sample.py")
    references = ast_tools._find_references_stdlib(SOURCE, "helper")
    signature = ast_tools._extract_signature_stdlib(SOURCE, "Box")

    assert [definition["name"] for definition in definitions] == ["build", "Box"]
    assert len(references) == 3
    assert signature is not None
    assert signature["kind"] == "class"


def test_malformed_source_reports_a_syntax_error_with_stdlib_backend() -> None:
    with pytest.raises(SyntaxError):
        ast_tools._extract_signature_stdlib("def broken(:\n    pass\n", "broken")


def test_malformed_source_reports_a_syntax_error_with_tree_sitter_backend() -> None:
    pytest.importorskip("tree_sitter")
    pytest.importorskip("tree_sitter_python")
    if ast_tools.backend() != "tree-sitter":
        pytest.skip("tree-sitter backend is unavailable")

    with pytest.raises(SyntaxError):
        ast_tools._extract_signature_tree("def broken(:\n    pass\n", "broken")


@pytest.mark.parametrize(
    ("source", "query"),
    [
        ("def \N{KELVIN SIGN}elvin():\n    pass\n", "Kelvin"),
        ("def Kelvin():\n    pass\n", "\N{KELVIN SIGN}elvin"),
    ],
)
def test_stdlib_normalizes_unicode_identifiers(source: str, query: str) -> None:
    signature = ast_tools._extract_signature_stdlib(source, query)

    assert signature is not None
    assert signature["name"] == "Kelvin"


@pytest.mark.parametrize(
    ("source", "query"),
    [
        ("def \N{KELVIN SIGN}elvin():\n    pass\n", "Kelvin"),
        ("def Kelvin():\n    pass\n", "\N{KELVIN SIGN}elvin"),
    ],
)
def test_tree_sitter_normalizes_unicode_identifiers(source: str, query: str) -> None:
    pytest.importorskip("tree_sitter")
    pytest.importorskip("tree_sitter_python")
    if ast_tools.backend() != "tree-sitter":
        pytest.skip("tree-sitter backend is unavailable")

    signature = ast_tools._extract_signature_tree(source, query)

    assert signature is not None
    assert signature["name"] == "Kelvin"


def test_tree_sitter_backend_is_active_when_optional_extra_is_installed() -> None:
    pytest.importorskip("tree_sitter")
    pytest.importorskip("tree_sitter_python")
    assert ast_tools.backend() == "tree-sitter"
