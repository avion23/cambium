"""Exercise navigation through the same schema/dispatch path as a worker."""

from __future__ import annotations

import asyncio
import json
from pathlib import Path

import pytest

from cambium.tools import ToolContext, run_tool


def query(root: Path, **arguments):
    return asyncio.run(run_tool("repo_query", arguments, ToolContext(root)))


def test_locate_then_read_without_dumping_the_repository(tmp_path: Path) -> None:
    source = tmp_path / "calc.py"
    source.write_text("def add(a, b):\n    return a + b\n\nvalue = add(2, 3)\n")
    found = query(tmp_path, action="symbols", query="add", exact=True)
    assert found.ok
    location = json.loads(found.output)[0]
    assert (location["path"], location["line"]) == ("calc.py", 1)
    window = query(tmp_path, action="window", path=location["path"], line=location["line"], limit=2)
    assert window.ok
    assert "return a + b" in json.loads(window.output)["content"]
    assert "value =" not in window.output
    references = query(tmp_path, action="references", query="add")
    assert references.ok
    assert [row["line"] for row in json.loads(references.output)] == [1, 4]


def test_tree_and_literal_search_skip_generated_trees(tmp_path: Path) -> None:
    (tmp_path / "src").mkdir()
    (tmp_path / "src" / "a.py").write_text("value = 1\nvalue = 2\n")
    (tmp_path / ".git").mkdir()
    (tmp_path / ".git" / "private.py").write_text("value = 3\n")
    tree = query(tmp_path, action="tree")
    assert tree.ok and tree.output == "src/a.py"
    search = query(tmp_path, action="search", query="value", path="src", limit=1)
    assert search.ok
    assert "src/a.py:1: value = 1" in search.output
    assert "narrow the path or query" in search.output
    assert "value = 2" not in search.output


def test_generic_symbol_line_skips_leading_blank_lines(tmp_path: Path) -> None:
    (tmp_path / "math.rs").write_text("\n\npub fn divide() {}\n")
    result = query(tmp_path, action="symbols", query="divide")
    assert result.ok
    location = json.loads(result.output)[0]
    assert location["line"] == 3
    assert location["preview"] == "pub fn divide() {}"


def test_division_is_not_a_comment(tmp_path: Path) -> None:
    (tmp_path / "math.c").write_text(
        "int divide(int value, int divisor) {\n"
        "    return value / divisor; // divisor is nonzero\n"
        "}\n"
        "/* divisor is not a reference here */\n"
    )
    result = query(tmp_path, action="references", query="divisor")
    assert result.ok
    assert [row["line"] for row in json.loads(result.output)] == [1, 2]


@pytest.mark.parametrize("action", ["symbols", "references"])
def test_symbol_queries_honor_the_requested_path(tmp_path: Path, action: str) -> None:
    (tmp_path / "a.py").write_text("def add(): pass\n")
    (tmp_path / "b.py").write_text("def add(): pass\n")
    result = query(tmp_path, action=action, query="add", path="a.py", limit=1)
    assert result.ok
    assert [row["path"] for row in json.loads(result.output)] == ["a.py"]


@pytest.mark.parametrize("action", ["tree", "search", "window", "symbols", "references"])
def test_queries_cannot_leave_the_worktree(tmp_path: Path, action: str) -> None:
    result = query(tmp_path, action=action, path="../outside.py", query="text", line=1)
    assert not result.ok and "escapes the worktree" in result.error


def test_window_past_eof_is_not_a_successful_empty_read(tmp_path: Path) -> None:
    (tmp_path / "a.py").write_text("value = 1\n")
    result = query(tmp_path, action="window", path="a.py", line=100)
    assert not result.ok and "past the end" in result.error


def test_unconfigured_lsp_does_not_pretend_to_find_a_definition(
    tmp_path: Path, monkeypatch
) -> None:
    monkeypatch.delenv("CAMBIUM_LSP_COMMAND", raising=False)
    (tmp_path / "a.py").write_text("value = 1\n")
    result = query(tmp_path, action="lsp", method="definition", path="a.py", line=1)
    assert not result.ok and "CAMBIUM_LSP_COMMAND is not configured" in result.error


def test_navigation_output_is_bounded(tmp_path: Path) -> None:
    (tmp_path / "a.py").write_text(("needle " + "x" * 1000 + "\n") * 100)
    result = query(tmp_path, action="search", query="needle", limit=100)
    assert result.ok
    assert len(result.output.encode()) <= 16 * 1024
