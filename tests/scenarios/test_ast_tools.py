"""Scenario tests for the optional tree-sitter and stdlib AST search tools."""

from __future__ import annotations

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
