"""Scenario tests for the executable worker tool dispatch."""

from __future__ import annotations

import asyncio
import json
import os
import subprocess
import sys
import threading
import time
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


def test_get_signature_returns_structured_signature(tmp_path: Path) -> None:
    (tmp_path / "sample.py").write_text(
        "def build(\n    value,\n):\n    return value\n",
        encoding="utf-8",
    )

    result = _run(
        "get_signature",
        {"path": "sample.py", "symbol": "build"},
        ToolContext(tmp_path),
    )

    assert result.ok
    assert result.error is None
    assert json.loads(result.output) == {
        "body_lines": 1,
        "col": 0,
        "kind": "function",
        "line": 1,
        "name": "build",
        "path": "sample.py",
        "signature": "def build(\n    value,\n):",
    }


def test_get_signature_rejects_escape_and_invalid_symbol(tmp_path: Path) -> None:
    outside = tmp_path.parent / "outside.py"
    outside.write_text("def build():\n    pass\n", encoding="utf-8")
    escaped = _run(
        "get_signature",
        {"path": "../outside.py", "symbol": "build"},
        ToolContext(tmp_path),
    )
    assert not escaped.ok
    assert "escapes worktree" in (escaped.error or "")

    (tmp_path / "sample.py").write_text("def build():\n    pass\n", encoding="utf-8")
    invalid = _run(
        "get_signature",
        {"path": "sample.py", "symbol": "build.nested"},
        ToolContext(tmp_path),
    )
    assert not invalid.ok
    assert invalid.error == "get_signature symbol must be a Python identifier"


def test_get_signature_reports_missing_symbol(tmp_path: Path) -> None:
    (tmp_path / "sample.py").write_text("def build():\n    pass\n", encoding="utf-8")
    missing = _run(
        "get_signature",
        {"path": "sample.py", "symbol": "missing"},
        ToolContext(tmp_path),
    )
    assert not missing.ok
    assert "symbol not found" in (missing.error or "")


def test_get_signature_does_not_follow_replaced_validated_file(
    tmp_path: Path, monkeypatch
) -> None:
    sample = tmp_path / "sample.py"
    outside = tmp_path.parent / "outside.py"
    sample.write_text("def build():\n    return 'inside'\n", encoding="utf-8")
    outside.write_text("def build():\n    return 'outside'\n", encoding="utf-8")
    original_confined_path = tools._confined_path

    def replace_after_validation(ctx: ToolContext, raw_path: str) -> Path:
        path = original_confined_path(ctx, raw_path)
        path.unlink()
        path.symlink_to(outside)
        return path

    monkeypatch.setattr(tools, "_confined_path", replace_after_validation)
    result = _run(
        "get_signature",
        {"path": "sample.py", "symbol": "build"},
        ToolContext(tmp_path),
    )

    assert not result.ok
    assert "could not read sample.py" in (result.error or "")


def test_get_signature_rejects_replaced_worktree_root(tmp_path: Path) -> None:
    root = tmp_path / "worktree"
    outside = tmp_path / "outside"
    root.mkdir()
    outside.mkdir()
    (outside / "sample.py").write_text(
        "def build():\n    return 'outside'\n", encoding="utf-8"
    )
    context = ToolContext(root)

    root.rename(tmp_path / "original-worktree")
    root.symlink_to(outside, target_is_directory=True)
    result = _run(
        "get_signature",
        {"path": "sample.py", "symbol": "build"},
        context,
    )

    assert not result.ok
    assert result.output == ""
    assert result.error == "path escapes worktree: 'sample.py'"


def test_get_signature_rejects_oversized_source(tmp_path: Path) -> None:
    (tmp_path / "large.py").write_bytes(b"x" * (MAX_READ_BYTES + 1))

    result = _run(
        "get_signature",
        {"path": "large.py", "symbol": "build"},
        ToolContext(tmp_path),
    )

    assert not result.ok
    assert "MAX_READ_BYTES" in (result.error or "")


def test_get_signature_read_and_parse_do_not_block_dispatcher(
    tmp_path: Path, monkeypatch
) -> None:
    (tmp_path / "sample.py").write_text("def build():\n    pass\n", encoding="utf-8")
    original_extract_signature = tools.ast_tools.extract_signature

    def slow_extract_signature(source: str, symbol: str):
        time.sleep(0.1)
        return original_extract_signature(source, symbol)

    monkeypatch.setattr(tools.ast_tools, "extract_signature", slow_extract_signature)

    async def invoke():
        task = asyncio.create_task(
            run_tool(
                "get_signature",
                {"path": "sample.py", "symbol": "build"},
                ToolContext(tmp_path),
            )
        )
        await asyncio.sleep(0.01)
        yielded_before_completion = not task.done()
        result = await task
        return result, yielded_before_completion

    result, yielded_before_completion = asyncio.run(invoke())

    assert yielded_before_completion
    assert result.ok


def test_get_signature_timeout_does_not_wait_for_blocked_read(
    tmp_path: Path, monkeypatch
) -> None:
    (tmp_path / "sample.py").write_text("def build():\n    pass\n", encoding="utf-8")
    original_read = tools._read_and_extract_signature

    def slow_read(*args):
        time.sleep(2)
        return original_read(*args)

    monkeypatch.setattr(tools, "_read_and_extract_signature", slow_read)
    started = time.monotonic()
    result = _run(
        "get_signature",
        {"path": "sample.py", "symbol": "build"},
        ToolContext(tmp_path),
    )
    elapsed = time.monotonic() - started

    assert elapsed < 1.5
    assert not result.ok
    assert result.error == (
        "get_signature read timed out after 1.0s: sample.py"
    )


def test_get_signature_repeated_blocked_reads_do_not_use_shared_executor(
    tmp_path: Path, monkeypatch
) -> None:
    (tmp_path / "sample.py").write_text("def build():\n    pass\n", encoding="utf-8")
    original_read = tools._read_and_extract_signature

    def slow_read(*args):
        time.sleep(2)
        return original_read(*args)

    monkeypatch.setattr(tools, "_read_and_extract_signature", slow_read)
    started = time.monotonic()

    async def invoke_repeatedly():
        return await asyncio.gather(
            *(
                run_tool(
                    "get_signature",
                    {"path": "sample.py", "symbol": "build"},
                    ToolContext(tmp_path),
                )
                for _ in range(3)
            )
        )
    results = asyncio.run(invoke_repeatedly())
    elapsed = time.monotonic() - started

    assert elapsed < 1.5
    assert all(not result.ok for result in results)
    assert all("timed out" in (result.error or "") for result in results)
    signature_threads = [
        thread
        for thread in threading.enumerate()
        if thread.name == "cambium-get-signature-read"
    ]
    assert signature_threads
    assert all(thread.daemon for thread in signature_threads)

    deadline = time.monotonic() + 2.5
    while time.monotonic() < deadline and any(
        thread.is_alive() for thread in signature_threads
    ):
        time.sleep(0.01)
    assert not any(thread.is_alive() for thread in signature_threads)


def test_get_signature_caps_serialized_output(tmp_path: Path) -> None:
    arguments = ", ".join(f"value_{index}" for index in range(7_000))
    source = f"def build({arguments}):\n    pass\n"
    assert len(source.encode("utf-8")) <= MAX_READ_BYTES
    (tmp_path / "large_signature.py").write_text(source, encoding="utf-8")

    result = _run(
        "get_signature",
        {"path": "large_signature.py", "symbol": "build"},
        ToolContext(tmp_path),
    )

    assert result.ok
    parsed = json.loads(result.output)
    assert isinstance(parsed, dict)
    assert parsed["truncated"] is True
    assert "[output truncated]" in parsed["signature"]
    assert len(result.output.encode("utf-8")) <= MAX_OUTPUT_BYTES


def test_get_signature_caps_exact_oversized_identifier(tmp_path: Path) -> None:
    identifier = "a" * 65_386
    source = f"def {identifier}():\n    pass\n"
    assert identifier.isidentifier()
    assert len(source.encode("utf-8")) <= tools.MAX_READ_BYTES
    (tmp_path / "large_signature.py").write_text(source, encoding="utf-8")

    result = _run(
        "get_signature",
        {"path": "large_signature.py", "symbol": identifier},
        ToolContext(tmp_path),
    )

    assert result.ok
    parsed = json.loads(result.output)
    assert parsed["truncated"] is True
    assert "[output truncated]" in parsed["signature"]
    assert len(result.output.encode("utf-8")) <= MAX_OUTPUT_BYTES


def test_get_signature_caps_when_non_signature_fields_are_oversized() -> None:
    result = tools._serialize_signature_result(
        {
            "path": "p" * (MAX_OUTPUT_BYTES + 1),
            "name": "build",
            "kind": "function",
            "line": 1,
            "col": 0,
            "body_lines": 1,
            "signature": "def build():",
        }
    )

    parsed = json.loads(result)
    assert parsed["truncated"] is True
    assert "[output truncated]" in parsed["signature"]
    assert len(result.encode("utf-8")) <= MAX_OUTPUT_BYTES


def test_get_signature_rejects_fifo_quickly(tmp_path: Path) -> None:
    os.mkfifo(tmp_path / "sample.py")

    started = time.monotonic()
    result = _run(
        "get_signature",
        {"path": "sample.py", "symbol": "build"},
        ToolContext(tmp_path),
    )
    elapsed = time.monotonic() - started

    assert elapsed < 1.0
    assert not result.ok
    assert "not a regular file" in (result.error or "")


def test_get_signature_rejects_fifo_replaced_after_validation(
    tmp_path: Path, monkeypatch
) -> None:
    sample = tmp_path / "sample.py"
    sample.write_text("def build():\n    pass\n", encoding="utf-8")
    original_confined_path = tools._confined_path

    def replace_after_validation(ctx: ToolContext, raw_path: str) -> Path:
        path = original_confined_path(ctx, raw_path)
        path.unlink()
        os.mkfifo(path)
        return path

    monkeypatch.setattr(tools, "_confined_path", replace_after_validation)
    started = time.monotonic()
    result = _run(
        "get_signature",
        {"path": "sample.py", "symbol": "build"},
        ToolContext(tmp_path),
    )
    elapsed = time.monotonic() - started

    assert elapsed < 1.0
    assert not result.ok
    assert "not a regular file" in (result.error or "")


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


def test_run_tool_validates_before_dispatch_and_rejects_unknown_tools(tmp_path: Path) -> None:
    invalid = _run("read_file", {}, ToolContext(tmp_path))
    assert not invalid.ok
    assert invalid.error == "validation failed: missing 'path' (string)"

    unknown = _run("does_not_exist", {}, ToolContext(tmp_path))
    assert not unknown.ok
    assert unknown.error == "unknown tool: 'does_not_exist'"
