"""Scenario tests for the optional Ruff diagnostics adapter."""

from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

from cambium import lint_diag
from cambium.lint_diag import LintDiag


def _available_linter() -> LintDiag:
    linter = LintDiag()
    if not linter.is_available():
        pytest.skip("ruff is not installed")
    return linter


def test_lint_file_reports_e501_and_f841(tmp_path: Path) -> None:
    path = tmp_path / "bad.py"
    path.write_text(
        "def bad() -> None:\n"
        "    unused_local = 1\n"
        "    this_line_is_deliberately_long_to_trigger_e501_because_it_is_over_"
        "one_hundred_characters_because_it_really_is_long = 1\n"
    )

    diagnostics = _available_linter().lint_file(path)

    assert {diagnostic["code"] for diagnostic in diagnostics} >= {"E501", "F841"}
    assert all(
        {"path", "line", "col", "message", "code"} <= diagnostic.keys()
        for diagnostic in diagnostics
    )


def test_lint_file_returns_no_diagnostics_for_clean_file(tmp_path: Path) -> None:
    path = tmp_path / "clean.py"
    path.write_text("def clean() -> int:\n    return 1\n")

    assert _available_linter().lint_file(path) == []


def test_lint_files_maps_paths_and_total_diagnostics(tmp_path: Path) -> None:
    bad = tmp_path / "bad.py"
    bad.write_text(
        "def bad() -> None:\n"
        "    unused_local = 1\n"
        "    this_line_is_deliberately_long_to_trigger_e501_because_it_is_over_"
        "one_hundred_characters_because_it_really_is_long = 1\n"
    )
    clean = tmp_path / "clean.py"
    clean.write_text("def clean() -> int:\n    return 1\n")

    diagnostics = _available_linter().lint_files([bad, clean])

    assert set(diagnostics) == {bad, clean}
    assert diagnostics[clean] == []
    assert sum(len(items) for items in diagnostics.values()) >= 2


def test_format_diags_is_bounded() -> None:
    diagnostics = [
        {
            "path": "example.py",
            "line": number,
            "col": 1,
            "code": "RUF001",
            "message": "ambiguous unicode character",
        }
        for number in range(1, 26)
    ]

    formatted = lint_diag.format_diags(diagnostics, max_lines=20)

    lines = formatted.splitlines()
    assert len(lines) == 20
    assert lines[0] == "example.py:1:1: RUF001 ambiguous unicode character"
    assert lines[-1].startswith("example.py:20:1:")
    assert "example.py:21:1:" not in formatted
    assert LintDiag.format_diags(diagnostics, max_lines=2).count("\n") == 1


def test_disabled_when_ruff_is_not_on_path(monkeypatch, tmp_path: Path) -> None:
    monkeypatch.setattr(lint_diag.shutil, "which", lambda _name: None)

    linter = LintDiag()

    assert linter.is_available() is False
    assert linter.lint_file(tmp_path / "missing.py") == []
    assert linter.lint_files([tmp_path / "missing.py"]) == {
        tmp_path / "missing.py": []
    }


def test_syntax_error_is_returned_as_e999(tmp_path: Path) -> None:
    path = tmp_path / "syntax_error.py"
    path.write_text("import os\n\ndef broken(:\n")

    diagnostics = _available_linter().lint_file(path)

    syntax_errors = [diagnostic for diagnostic in diagnostics if diagnostic["code"] == "E999"]
    assert syntax_errors
    assert all(diagnostic["path"] == str(path) for diagnostic in syntax_errors)
    assert all(isinstance(diagnostic["line"], int) for diagnostic in syntax_errors)
    assert all(isinstance(diagnostic["col"], int) for diagnostic in syntax_errors)
    assert all(diagnostic["message"] for diagnostic in syntax_errors)


def test_lint_subprocess_does_not_inherit_provider_credentials(
    tmp_path: Path, monkeypatch,
) -> None:
    """The linter subprocess gets a scrubbed env: a linter that echoes its
    environment must not observe any provider credential."""
    monkeypatch.setenv("CAMBIUM_PROVIDER_OPENAI_API_KEY", "lint-secret")
    script = (
        "import json, os, sys\n"
        "secret = os.environ.get('CAMBIUM_PROVIDER_OPENAI_API_KEY', 'NO-SECRET')\n"
        "print(json.dumps([{'code': 'E001', 'message': secret, "
        "'filename': sys.argv[1], 'location': {'row': 1, 'column': 1}}]))\n"
    )
    linter = LintDiag(lint_cmd=[sys.executable, "-c", script])

    diagnostics = linter.lint_file(tmp_path / "example.py")

    assert diagnostics
    assert diagnostics[0]["message"] == "NO-SECRET"
    assert "lint-secret" not in json.dumps(diagnostics)
