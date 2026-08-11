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

import pytest

from cambium import tools
from cambium.tools import (
    MAX_OUTPUT_BYTES,
    MAX_READ_BYTES,
    ToolContext,
    run_read_batch,
    run_tool,
)


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


def _batch_context(tmp_path: Path, events: list[dict] | None = None) -> ToolContext:
    return ToolContext(
        tmp_path,
        init={"tools": ["read_file"]},
        emit=events.append if events is not None else None,
    )


@pytest.mark.slow  # 3x100ms scripted reads; asserts elapsed < 0.2 (load-sensitive)
def test_read_batch_runs_three_100ms_reads_concurrently(
    tmp_path: Path, monkeypatch
) -> None:
    for name in ("one.txt", "two.txt", "three.txt"):
        (tmp_path / name).write_text(name, encoding="utf-8")

    original = tools._read_file_sync

    def delayed_read(_ctx, path, display_path):
        time.sleep(0.1)
        return original(_ctx, path, display_path)

    monkeypatch.setattr(tools, "_read_file_sync", delayed_read)
    started = time.perf_counter()
    results = asyncio.run(
        run_read_batch(
            [{"path": "one.txt"}, {"path": "two.txt"}, {"path": "three.txt"}],
            _batch_context(tmp_path),
        )
    )

    assert time.perf_counter() - started < 0.2
    assert [result.output for result in results] == ["one.txt", "two.txt", "three.txt"]


def test_read_batch_orders_results_and_events_after_out_of_order_completion(
    tmp_path: Path, monkeypatch
) -> None:
    completion_order: list[int] = []
    events: list[dict] = []
    delays = {"zero.txt": 0.03, "one.txt": 0.05, "two.txt": 0.01}
    indexes = {"zero.txt": 0, "one.txt": 1, "two.txt": 2}

    async def delayed_read(args: dict, _ctx: ToolContext):
        path = args["path"]
        await asyncio.sleep(delays[path])
        completion_order.append(indexes[path])
        return tools._Outcome(ok=True, output=path)

    monkeypatch.setattr(tools, "_read_file", delayed_read)
    results = asyncio.run(
        run_read_batch(
            [{"path": "zero.txt"}, {"path": "one.txt"}, {"path": "two.txt"}],
            _batch_context(tmp_path, events),
        )
    )

    assert completion_order == [2, 0, 1]
    assert [result.output for result in results] == ["zero.txt", "one.txt", "two.txt"]
    assert [event["batch_index"] for event in events] == [0, 1, 2]
    assert all(set(event) == {
        "type", "tool", "batch_index", "batch_size", "ok", "duration_ms"
    } for event in events)


def test_read_batch_rejects_mixed_array_atomically(
    tmp_path: Path, monkeypatch
) -> None:
    events: list[dict] = []
    called = False

    async def unexpected_read(_args: dict, _ctx: ToolContext):
        nonlocal called
        called = True
        raise AssertionError("preflight must run before any read")

    monkeypatch.setattr(tools, "_read_file", unexpected_read)
    results = asyncio.run(
        run_read_batch(
            [{"path": "left.txt"}, {"path": 42}, {"path": "right.txt"}],
            _batch_context(tmp_path, events),
        )
    )

    assert len(results) == 3
    assert all(not result.ok for result in results)
    assert all("batch rejected atomically" in (result.error or "") for result in results)
    assert not called
    assert events == []


def test_read_batch_rejects_path_outside_confined_root(
    tmp_path: Path, monkeypatch
) -> None:
    (tmp_path / "inside.txt").write_text("inside", encoding="utf-8")
    called = False

    async def unexpected_read(_args: dict, _ctx: ToolContext):
        nonlocal called
        called = True
        raise AssertionError("preflight must reject escaped paths before any read")

    monkeypatch.setattr(tools, "_read_file", unexpected_read)
    results = asyncio.run(
        run_read_batch(
            [{"path": "inside.txt"}, {"path": "../outside.txt"}],
            _batch_context(tmp_path),
        )
    )

    assert len(results) == 2
    assert all(not result.ok for result in results)
    assert all("escapes worktree" in (result.error or "") for result in results)
    assert not called


def test_read_batch_missing_middle_file_is_per_call_failure(tmp_path: Path) -> None:
    (tmp_path / "first.txt").write_text("first", encoding="utf-8")
    (tmp_path / "last.txt").write_text("last", encoding="utf-8")
    events: list[dict] = []

    results = asyncio.run(
        run_read_batch(
            [
                {"path": "first.txt"},
                {"path": "missing.txt"},
                {"path": "last.txt"},
            ],
            _batch_context(tmp_path, events),
        )
    )

    assert [result.ok for result in results] == [True, False, True]
    assert [result.output for result in results] == ["first", "", "last"]
    assert [event["ok"] for event in events] == [True, False, True]


@pytest.mark.slow  # 8x50ms scripted reads; concurrency measured via timing
def test_read_batch_concurrency_is_bounded(tmp_path: Path, monkeypatch) -> None:
    for index in range(8):
        (tmp_path / f"f{index}.txt").write_text("x", encoding="utf-8")
    original = tools._read_file_sync
    lock = threading.Lock()
    state = {"active": 0, "peak": 0}

    def counting_read(_ctx, path, display_path):
        with lock:
            state["active"] += 1
            state["peak"] = max(state["peak"], state["active"])
        time.sleep(0.05)
        try:
            return original(_ctx, path, display_path)
        finally:
            with lock:
                state["active"] -= 1

    monkeypatch.setattr(tools, "_read_file_sync", counting_read)
    results = asyncio.run(
        run_read_batch(
            [{"path": f"f{index}.txt"} for index in range(8)],
            _batch_context(tmp_path),
        )
    )

    assert all(result.ok for result in results)
    assert state["peak"] > 1
    assert state["peak"] <= tools.BATCH_READ_MAX_CONCURRENCY


def test_read_batch_requires_read_file_in_init_tools(tmp_path: Path) -> None:
    results = asyncio.run(
        run_read_batch([{"path": "anything.txt"}], ToolContext(tmp_path))
    )

    assert len(results) == 1
    assert not results[0].ok
    assert "read_file is not offered in init.tools" in (results[0].error or "")


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


@pytest.mark.slow  # python interpreter spawn (fake rg); asserts subprocess argv
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


@pytest.mark.slow  # 100ms scripted parse; proves the dispatcher yields via timing
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


@pytest.mark.slow  # 0.6s blocked-read wait behind a 0.3s tool timeout; timing assertion
def test_get_signature_timeout_does_not_wait_for_blocked_read(
    tmp_path: Path, monkeypatch
) -> None:
    (tmp_path / "sample.py").write_text("def build():\n    pass\n", encoding="utf-8")
    original_read = tools._read_and_extract_signature

    def slow_read(*args):
        time.sleep(0.6)
        return original_read(*args)

    # the tool's read timeout is a module constant read at dispatch time; a
    # short timeout exercises the same mechanism in a fraction of the wall time
    monkeypatch.setattr(tools, "GET_SIGNATURE_READ_TIMEOUT_S", 0.3)
    monkeypatch.setattr(tools, "_read_and_extract_signature", slow_read)
    started = time.monotonic()
    result = _run(
        "get_signature",
        {"path": "sample.py", "symbol": "build"},
        ToolContext(tmp_path),
    )
    elapsed = time.monotonic() - started

    assert elapsed < 0.5
    assert not result.ok
    assert result.error == (
        "get_signature read timed out after 0.3s: sample.py"
    )


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


@pytest.mark.slow  # real git subprocess via the git_op tool
def test_git_op_runs_allowlisted_status(tmp_path: Path) -> None:
    subprocess.run(["git", "init", "-q"], cwd=tmp_path, check=True)

    result = _run(
        "git_op",
        {"op": "status", "args": "--short"},
        ToolContext(tmp_path),
    )

    assert result.ok
    assert result.error is None


@pytest.mark.slow  # python interpreter spawn; asserts subprocess execution
def test_run_shell_is_list_form(tmp_path: Path) -> None:
    command = [sys.executable, "-c", "print('shell-ok')"]
    result = _run("run_shell", {"cmd": command}, ToolContext(tmp_path))
    assert result.ok
    assert result.output == "shell-ok\n"


@pytest.mark.slow  # python interpreter spawn; asserts subprocess execution
def test_run_shell_output_is_capped(tmp_path: Path) -> None:
    command = [sys.executable, "-c", "print('x' * 100000)"]
    result = _run("run_shell", {"cmd": command}, ToolContext(tmp_path))

    assert result.ok
    assert "[output truncated]" in result.output
    assert len(result.output.encode()) <= MAX_OUTPUT_BYTES


@pytest.mark.slow  # python interpreter spawn; asserts subprocess execution
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


@pytest.mark.slow  # python linter subprocess; asserts subprocess env boundary
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
