"""Bounded structured code navigation without a daemon or global index.

The scanner is intentionally small and deterministic. It uses iterative
``os.scandir`` traversal, caps files/bytes/results, skips generated/cache trees,
and returns compact source locations. An optional LSP tool handles richer
language-specific queries; these primitives remain the fast portable fallback.
"""

from __future__ import annotations

import ast
import io
import json
import os
import re
import tokenize
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any

_SOURCE_SUFFIXES = frozenset(
    {
        ".py",
        ".pyi",
        ".rs",
        ".go",
        ".c",
        ".h",
        ".cc",
        ".cpp",
        ".hpp",
        ".java",
        ".kt",
        ".kts",
        ".js",
        ".jsx",
        ".ts",
        ".tsx",
        ".rb",
        ".php",
        ".swift",
        ".scala",
        ".sh",
        ".bash",
        ".zsh",
        ".toml",
        ".yaml",
        ".yml",
        ".json",
        ".md",
    }
)
_SKIP_DIRS = frozenset(
    {
        ".git",
        ".cambium",
        ".venv",
        "venv",
        "node_modules",
        "target",
        "dist",
        "build",
        "__pycache__",
        ".mypy_cache",
        ".pytest_cache",
        ".ruff_cache",
    }
)
_GENERIC_SYMBOL = re.compile(
    r"^\s*(?:(?:pub|public|private|protected|static|async|export|default|final|sealed)\s+)*"
    r"(?P<kind>class|struct|enum|interface|trait|type|fn|function|def|module|namespace)\s+"
    r"(?P<name>[A-Za-z_][A-Za-z0-9_]*)",
    re.MULTILINE,
)
_IDENTIFIER = re.compile(r"[A-Za-z_][A-Za-z0-9_]*\Z")


@dataclass(frozen=True, slots=True)
class SourceLocation:
    path: str
    line: int
    column: int
    kind: str
    name: str
    preview: str


def _inside(root: Path, candidate: Path) -> Path:
    resolved_root = root.resolve()
    resolved = candidate.resolve()
    if not resolved.is_relative_to(resolved_root):
        raise ValueError("path escapes the worktree")
    return resolved


def _walk_source_files(
    root: Path,
    *,
    max_files: int = 10_000,
    max_total_bytes: int = 64 * 1024 * 1024,
):
    resolved = root.resolve()
    stack = [resolved]
    files = 0
    total = 0
    while stack:
        directory = stack.pop()
        try:
            entries = list(os.scandir(directory))
        except OSError:
            continue
        entries.sort(key=lambda entry: entry.name, reverse=True)
        for entry in entries:
            try:
                if entry.is_dir(follow_symlinks=False):
                    if entry.name not in _SKIP_DIRS:
                        stack.append(Path(entry.path))
                    continue
                if not entry.is_file(follow_symlinks=False):
                    continue
                path = Path(entry.path)
                if path.suffix.lower() not in _SOURCE_SUFFIXES:
                    continue
                size = entry.stat(follow_symlinks=False).st_size
            except OSError:
                continue
            if size > 2 * 1024 * 1024:
                continue
            files += 1
            total += size
            if files > max_files or total > max_total_bytes:
                return
            yield path


def _read(path: Path) -> str | None:
    try:
        return path.read_text(encoding="utf-8")
    except (OSError, UnicodeError):
        return None


def _preview(line: str, *, limit: int = 240) -> str:
    value = " ".join(line.strip().split())
    return value if len(value) <= limit else value[: limit - 1] + "…"


def _python_symbols(path: Path, text: str, root: Path) -> list[SourceLocation]:
    try:
        tree = ast.parse(text)
    except (SyntaxError, ValueError):
        return []
    lines = text.splitlines()
    result: list[SourceLocation] = []
    for node in ast.walk(tree):
        kind = None
        name = None
        if isinstance(node, ast.FunctionDef | ast.AsyncFunctionDef):
            kind = "function"
            name = node.name
        elif isinstance(node, ast.ClassDef):
            kind = "class"
            name = node.name
        elif isinstance(node, ast.Assign | ast.AnnAssign):
            targets = node.targets if isinstance(node, ast.Assign) else [node.target]
            if len(targets) == 1 and isinstance(targets[0], ast.Name):
                kind = "variable"
                name = targets[0].id
        if kind is None or name is None or not hasattr(node, "lineno"):
            continue
        line_no = int(node.lineno)
        source_line = lines[line_no - 1] if 0 < line_no <= len(lines) else ""
        result.append(
            SourceLocation(
                str(path.relative_to(root)),
                line_no,
                int(getattr(node, "col_offset", 0)) + 1,
                kind,
                name,
                _preview(source_line),
            )
        )
    return result


def _generic_symbols(path: Path, text: str, root: Path) -> list[SourceLocation]:
    lines = text.splitlines()
    result: list[SourceLocation] = []
    for match in _GENERIC_SYMBOL.finditer(text):
        line_no = text.count("\n", 0, match.start()) + 1
        line = lines[line_no - 1] if line_no <= len(lines) else ""
        result.append(
            SourceLocation(
                str(path.relative_to(root)),
                line_no,
                match.start("name") - text.rfind("\n", 0, match.start("name")),
                match.group("kind"),
                match.group("name"),
                _preview(line),
            )
        )
    return result


def search_symbols(
    root: str | Path,
    query: str,
    *,
    exact: bool = False,
    max_results: int = 50,
) -> list[SourceLocation]:
    """Search declarations with bounded portable syntax extraction."""

    if not isinstance(query, str) or not query.strip():
        raise ValueError("symbol query must be non-empty")
    if max_results <= 0 or max_results > 500:
        raise ValueError("max_results must be in [1, 500]")
    needle = query.strip()
    folded = needle.casefold()
    resolved = Path(root).resolve()
    matches: list[SourceLocation] = []
    for path in _walk_source_files(resolved):
        text = _read(path)
        if text is None:
            continue
        symbols = (
            _python_symbols(path, text, resolved)
            if path.suffix.lower() in {".py", ".pyi"}
            else _generic_symbols(path, text, resolved)
        )
        for location in symbols:
            accepted = location.name == needle if exact else folded in location.name.casefold()
            if accepted:
                matches.append(location)
    if not exact:
        matches.sort(
            key=lambda location: (
                0
                if location.name.casefold() == folded
                else 1
                if location.name.casefold().startswith(folded)
                else 2
            )
        )
    return matches[:max_results]


def _python_reference_positions(text: str, symbol: str) -> list[tuple[int, int]]:
    positions: list[tuple[int, int]] = []
    try:
        tokens = tokenize.generate_tokens(io.StringIO(text).readline)
        for token in tokens:
            if token.type == tokenize.NAME and token.string == symbol:
                positions.append((token.start[0], token.start[1] + 1))
    except (IndentationError, SyntaxError, tokenize.TokenError):
        # Tokenize yields useful tokens before reporting an incomplete source
        # construct; retaining those is preferable to treating comments and
        # strings as references in the fallback below.
        pass
    return positions


_GENERIC_STRING = re.compile(r"""(?:"(?:\\.|[^"\\])*"|'(?:\\.|[^'\\])*'|`(?:\\.|[^`\\])*`)""")
_GENERIC_COMMENT = re.compile(r"(?:#|//|/\\*|--).*$")


def _generic_reference_positions(text: str, symbol: str) -> list[tuple[int, int]]:
    """Find identifiers after masking common quoted and comment regions."""

    pattern = re.compile(rf"\b{re.escape(symbol)}\b")
    positions: list[tuple[int, int]] = []
    for line_no, line in enumerate(text.splitlines(), start=1):
        code = _GENERIC_STRING.sub(lambda match: " " * len(match.group(0)), line)
        code = _GENERIC_COMMENT.sub(lambda match: " " * len(match.group(0)), code)
        positions.extend((line_no, match.start() + 1) for match in pattern.finditer(code))
    return positions


def _reference_positions(path: Path, text: str, symbol: str) -> list[tuple[int, int]]:
    if path.suffix.lower() in {".py", ".pyi"}:
        return _python_reference_positions(text, symbol)
    return _generic_reference_positions(text, symbol)


def find_references(
    root: str | Path,
    symbol: str,
    *,
    max_results: int = 100,
) -> list[SourceLocation]:
    """Find bounded exact-identifier references across source files."""

    if not isinstance(symbol, str) or _IDENTIFIER.fullmatch(symbol) is None:
        raise ValueError("symbol must be one identifier")
    if max_results <= 0 or max_results > 1000:
        raise ValueError("max_results must be in [1, 1000]")
    resolved = Path(root).resolve()
    matches: list[SourceLocation] = []
    for path in _walk_source_files(resolved):
        text = _read(path)
        if text is None:
            continue
        lines = text.splitlines()
        for line_no, column in _reference_positions(path, text, symbol):
            line = lines[line_no - 1] if line_no <= len(lines) else ""
            matches.append(
                SourceLocation(
                    str(path.relative_to(resolved)),
                    line_no,
                    column,
                    "reference",
                    symbol,
                    _preview(line),
                )
            )
            if len(matches) >= max_results:
                return matches
    return matches


def read_symbol(
    root: str | Path,
    path: str,
    line: int,
    *,
    context_lines: int = 40,
) -> dict[str, Any]:
    """Return one bounded source window around a declaration/reference."""

    if line <= 0:
        raise ValueError("line must be positive")
    if context_lines <= 0 or context_lines > 200:
        raise ValueError("context_lines must be in [1, 200]")
    resolved_root = Path(root).resolve()
    target = _inside(resolved_root, resolved_root / path)
    text = _read(target)
    if text is None:
        raise ValueError("source file is not readable UTF-8")
    lines = text.splitlines()
    start = max(1, line - context_lines // 2)
    end = min(len(lines), start + context_lines - 1)
    return {
        "path": str(target.relative_to(resolved_root)),
        "start_line": start,
        "end_line": end,
        "content": "\n".join(f"{index:>6}  {lines[index - 1]}" for index in range(start, end + 1)),
    }


def locations_json(locations: list[SourceLocation]) -> str:
    return json.dumps([asdict(location) for location in locations], ensure_ascii=False)


__all__ = [
    "SourceLocation",
    "find_references",
    "locations_json",
    "read_symbol",
    "search_symbols",
]
