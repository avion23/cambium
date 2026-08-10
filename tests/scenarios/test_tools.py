"""Scenario tests for the executable worker tool dispatch."""

from __future__ import annotations

import asyncio
import subprocess
import sys
from pathlib import Path

from cambium import tools
from cambium.approval import ApprovalGate, ApprovalPolicy
from cambium.tools import MAX_OUTPUT_BYTES, MAX_READ_BYTES, ToolContext, run_tool


def _run(name: str, args: dict, ctx: ToolContext):
    return asyncio.run(run_tool(name, args, ctx))


def test_read_file_happy_path_and_cap(tmp_path: Path) -> None:
    (tmp_path / "hello.txt").write_text("hello\n", encoding="utf-8")
    result = _run("read_file", {"path": "hello.txt"}, ToolContext(tmp_path))

    assert result.ok
    assert result.output == "hello\n"
    assert result.error is None
    assert isinstance(result.duration_ms, int)

    (tmp_path / "large.txt").write_bytes(b"x" * (MAX_READ_BYTES + 1))
    capped = _run("read_file", {"path": "large.txt"}, ToolContext(tmp_path))

    assert capped.ok
    assert "[file truncated]" in capped.output
    assert len(capped.output.encode()) <= MAX_READ_BYTES


def test_read_file_rejects_path_escape(tmp_path: Path) -> None:
    result = _run("read_file", {"path": "../outside.txt"}, ToolContext(tmp_path))

    assert not result.ok
    assert result.error is not None
    assert "escapes worktree" in result.error


class _LintFeedback:
    def lint_file(self, path: Path) -> list[dict]:
        return [{"path": str(path), "line": 1, "col": 1, "code": "E999", "message": "bad"}]

    def format_diags(self, diagnostics: list[dict]) -> str:
        return "\n".join(
            f"{item['path']}:{item['line']}:{item['col']}: "
            f"{item['code']} {item['message']}"
            for item in diagnostics
        )


def test_write_file_is_atomic_and_returns_lint_feedback(tmp_path: Path) -> None:
    result = _run(
        "write_file",
        {"path": "nested/new.py", "content": "broken(:\n"},
        ToolContext(tmp_path, lint=_LintFeedback()),
    )

    assert result.ok
    assert (tmp_path / "nested/new.py").read_text(encoding="utf-8") == "broken(:\n"
    assert "Lint diagnostics:" in result.output
    assert "E999 bad" in result.output
    assert not list((tmp_path / "nested").glob("*.tmp"))


def test_write_file_rejects_path_escape(tmp_path: Path) -> None:
    result = _run(
        "write_file",
        {"path": "../outside.txt", "content": "no"},
        ToolContext(tmp_path),
    )

    assert not result.ok
    assert "escapes worktree" in (result.error or "")


def test_edit_file_requires_exactly_one_occurrence(tmp_path: Path) -> None:
    path = tmp_path / "edit.txt"
    path.write_text("one\none\n", encoding="utf-8")
    context = ToolContext(tmp_path)

    repeated = _run(
        "edit_file",
        {"path": "edit.txt", "old_string": "one", "new_string": "two"},
        context,
    )
    assert not repeated.ok
    assert "exactly one occurrence" in (repeated.error or "")
    assert "Context:" in (repeated.error or "")
    assert path.read_text(encoding="utf-8") == "one\none\n"

    path.write_text("one\n", encoding="utf-8")
    edited = _run(
        "edit_file",
        {"path": "edit.txt", "old_string": "one", "new_string": "two"},
        context,
    )
    assert edited.ok
    assert path.read_text(encoding="utf-8") == "two\n"


def test_grep_code_uses_list_form_rg(tmp_path: Path, monkeypatch) -> None:
    (tmp_path / "sample.py").write_text("needle\n", encoding="utf-8")
    fake_rg = tmp_path / "rg"
    fake_rg.write_text(
        f"#!{sys.executable}\n"
        "import sys\n"
        "assert sys.argv[1:3] == ['-n', '--no-heading']\n"
        "print(repr(sys.argv[3:]))\n",
        encoding="utf-8",
    )
    fake_rg.chmod(0o755)
    monkeypatch.setattr(tools.shutil, "which", lambda _name: str(fake_rg))

    result = _run(
        "grep_code",
        {"pattern": "needle; not-a-command", "path": "sample.py"},
        ToolContext(tmp_path),
    )

    assert result.ok
    assert "needle; not-a-command" in result.output


def test_grep_code_falls_back_to_stdlib_regex(tmp_path: Path, monkeypatch) -> None:
    (tmp_path / "sample.py").write_text("nothing\nneedle here\n", encoding="utf-8")
    monkeypatch.setattr(tools.shutil, "which", lambda _name: None)

    result = _run(
        "grep_code",
        {"pattern": r"need\w+", "path": None},
        ToolContext(tmp_path),
    )

    assert result.ok
    assert "sample.py:2:needle here" in result.output


def test_grep_code_rejects_path_escape(tmp_path: Path) -> None:
    result = _run(
        "grep_code",
        {"pattern": "needle", "path": "../"},
        ToolContext(tmp_path),
    )

    assert not result.ok
    assert "escapes worktree" in (result.error or "")


def test_git_op_runs_allowlisted_status(tmp_path: Path) -> None:
    subprocess.run(["git", "init", "-q"], cwd=tmp_path, check=True)

    result = _run(
        "git_op",
        {"op": "status", "args": "--short"},
        ToolContext(tmp_path),
    )

    assert result.ok
    assert result.error is None


def test_run_shell_is_list_form_and_approval_gated(tmp_path: Path) -> None:
    command = [sys.executable, "-c", "print('shell-ok')"]
    result = _run("run_shell", {"cmd": command}, ToolContext(tmp_path))
    assert result.ok
    assert result.output == "shell-ok\n"

    gate = ApprovalGate(ApprovalPolicy({"allowlist": [[sys.executable, "-c"]]}))
    denied = _run(
        "run_shell",
        {"cmd": ["not-allowlisted", "argument"]},
        ToolContext(tmp_path, approval=gate),
    )
    assert not denied.ok
    assert denied.error == "DENIED: run_shell command is not approved"


def test_run_shell_output_is_capped(tmp_path: Path) -> None:
    command = [sys.executable, "-c", "print('x' * 100000)"]
    result = _run("run_shell", {"cmd": command}, ToolContext(tmp_path))

    assert result.ok
    assert "[output truncated]" in result.output
    assert len(result.output.encode()) <= MAX_OUTPUT_BYTES


def test_tool_subprocesses_do_not_inherit_provider_credentials(
    tmp_path: Path, monkeypatch
) -> None:
    monkeypatch.setenv("CAMBIUM_PROVIDER_OPENAI_API_KEY", "tool-secret")
    command = [
        sys.executable,
        "-c",
        "import os; print('CAMBIUM_PROVIDER_OPENAI_API_KEY' in os.environ)",
    ]

    result = _run("run_shell", {"cmd": command}, ToolContext(tmp_path))

    assert result.ok
    assert result.output == "False\n"


def test_tool_lint_result_does_not_contain_provider_key(tmp_path: Path, monkeypatch) -> None:
    """A linter that echoes its environment proves the tool result carries no
    provider credential (regression: the linter subprocess inherited os.environ)."""
    from cambium.lint_diag import LintDiag

    monkeypatch.setenv("CAMBIUM_PROVIDER_OPENAI_API_KEY", "lint-secret")
    script = (
        "import json, os, sys\n"
        "secret = os.environ.get('CAMBIUM_PROVIDER_OPENAI_API_KEY', 'NO-SECRET')\n"
        "print(json.dumps([{'code': 'E001', 'message': secret, "
        "'filename': sys.argv[1], 'location': {'row': 1, 'column': 1}}]))\n"
    )
    ctx = ToolContext(tmp_path, lint=LintDiag(lint_cmd=[sys.executable, "-c", script]))

    result = _run("write_file", {"path": "new.py", "content": "ok\n"}, ctx)

    assert result.ok
    assert "lint-secret" not in (result.output or "")
    assert "NO-SECRET" in (result.output or "")


def test_run_tool_validates_before_dispatch_and_rejects_unknown_tools(tmp_path: Path) -> None:
    invalid = _run("read_file", {}, ToolContext(tmp_path))
    assert not invalid.ok
    assert invalid.error == "validation failed: missing 'path' (string)"

    unknown = _run("does_not_exist", {}, ToolContext(tmp_path))
    assert not unknown.ok
    assert unknown.error == "unknown tool: 'does_not_exist'"
