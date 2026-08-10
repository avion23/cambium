"""Run Ruff and turn its findings into worker-context diagnostics.

The intended runtime wiring is explicit: after ``write_file``, the worker's
``lint_diags`` step calls :class:`LintDiag`, formats the returned diagnostics
with ``format_diags``, and appends that string to its context before
compilation.  This module only runs Ruff and returns data.  It does not import
or modify the worker.

Ruff's syntax-error rule is canonically ``E999``.  Recent Ruff JSON output
uses ``invalid-syntax`` as the code for that rule, so this module normalizes
that spelling to ``E999`` for callers.
"""

from __future__ import annotations

import json
import shutil
import subprocess
from pathlib import Path
from typing import Any

from .auth import scrub_environment


def _location_value(diagnostic: dict[str, Any], direct: str, nested: str) -> Any:
    location = diagnostic.get("location")
    if isinstance(location, dict) and nested in location:
        return location[nested]
    return diagnostic.get(direct)


def _normalize_diagnostic(path: Path, diagnostic: dict[str, Any]) -> dict[str, Any]:
    code = diagnostic.get("code")
    if not isinstance(code, str):
        code = diagnostic.get("rule", "")
    if not isinstance(code, str):
        code = str(code)
    if code == "invalid-syntax":
        code = "E999"

    filename = diagnostic.get("filename")
    if not isinstance(filename, str):
        filename = str(path)

    message = diagnostic.get("message", "")
    if not isinstance(message, str):
        message = str(message)

    return {
        "path": filename,
        "line": _location_value(diagnostic, "line", "row"),
        "col": _location_value(diagnostic, "col", "column"),
        "message": message,
        "code": code,
    }


def _parse_diagnostics(path: Path, output: str) -> list[dict[str, Any]]:
    payload = json.loads(output or "[]")
    if not isinstance(payload, list):
        return []
    return [
        _normalize_diagnostic(path, item)
        for item in payload[:50]
        if isinstance(item, dict)
    ]


def format_diags(diags: list[dict], *, max_lines: int = 20) -> str:
    """Format diagnostics for appending to the worker's context.

    The result has at most ``max_lines`` lines.  The caller decides whether
    the diagnostics are validation failures; formatting does not enforce or
    reject anything.
    """
    if max_lines <= 0:
        return ""

    lines: list[str] = []
    for diagnostic in diags[:max_lines]:
        location = diagnostic.get("location")
        line = diagnostic.get("line")
        if line is None and isinstance(location, dict):
            line = location.get("row")
        col = diagnostic.get("col")
        if col is None and isinstance(location, dict):
            col = location.get("column")
        path = diagnostic.get("path", diagnostic.get("filename", ""))
        code = diagnostic.get("code", diagnostic.get("rule", ""))
        message = diagnostic.get("message", "")
        lines.append(f"{path}:{line}:{col}: {code} {message}")
    return "\n".join(lines)


class LintDiag:
    """Small Ruff subprocess adapter used by the worker's edit loop."""

    def __init__(self, lint_cmd: list[str] | None = None) -> None:
        if lint_cmd is None:
            ruff = shutil.which("ruff")
            self.lint_cmd = (
                [ruff, "check", "--output-format", "json"] if ruff is not None else None
            )
        else:
            self.lint_cmd = list(lint_cmd)

    def is_available(self) -> bool:
        """Return whether a linter command is configured and runnable."""
        return self.lint_cmd is not None

    def lint_file(self, path: Path) -> list[dict]:
        """Run Ruff against exactly one file and return at most 50 findings."""
        if self.lint_cmd is None:
            return []
        result = subprocess.run(
            [*self.lint_cmd, str(path)],
            capture_output=True,
            text=True,
            check=False,
            env=scrub_environment(),
        )
        return _parse_diagnostics(path, result.stdout)

    def lint_files(self, paths: list[Path]) -> dict[Path, list[dict]]:
        """Return the diagnostics for each requested path."""
        return {path: self.lint_file(path) for path in paths}

    @staticmethod
    def format_diags(diags: list[dict], *, max_lines: int = 20) -> str:
        """Expose :func:`format_diags` on the adapter without adding state."""
        return format_diags(diags, max_lines=max_lines)
