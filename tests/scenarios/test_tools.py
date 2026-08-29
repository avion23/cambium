"""Scenario tests for the executable worker tool dispatch."""

from __future__ import annotations

import asyncio
import os
import subprocess
import sys
import threading
import time
from pathlib import Path
from typing import cast

import pytest

from cambium import tools
from cambium.lint_diag import LintDiag
from cambium.tools import (
    MAX_OUTPUT_BYTES,
    MAX_READ_BYTES,
    MAX_READ_LINES,
    SHELL_OUTPUT_HEAD_BYTES,
    SHELL_OUTPUT_TAIL_BYTES,
    ToolContext,
    run_read_batch,
    run_tool,
)


def _run(name: str, args: dict, ctx: ToolContext):
    return asyncio.run(run_tool(name, args, ctx))


def test_read_batch_happy_path_and_cap(tmp_path: Path) -> None:
    (tmp_path / "hello.txt").write_text("hello\n", encoding="utf-8")
    result = _run("read_batch", {"paths": ["hello.txt"]}, ToolContext(tmp_path))

    assert result.ok
    assert result.output == "--- hello.txt ---\nhello\n"
    assert result.error is None
    assert isinstance(result.duration_ms, int)

    (tmp_path / "large.txt").write_bytes(b"x" * (MAX_READ_BYTES + 1))
    capped = _run("read_batch", {"paths": ["large.txt"]}, ToolContext(tmp_path))

    assert capped.ok
    assert "--- large.txt ---" in capped.output
    assert "[output truncated]" in capped.output
    assert len(capped.output.encode()) <= MAX_OUTPUT_BYTES


def test_read_batch_reads_line_window_with_total_header(tmp_path: Path) -> None:
    (tmp_path / "lines.txt").write_text(
        "".join(f"line {number}\n" for number in range(1, 6)), encoding="utf-8"
    )

    result = _run(
        "read_batch",
        {"paths": ["lines.txt"], "offset": 2, "limit": 2},
        ToolContext(tmp_path),
    )

    assert result.ok
    assert result.output == (
        "--- lines.txt ---\n"
        "showing lines 2-3 of 5\n"
        "line 2\n"
        "line 3\n"
    )


def test_read_batch_line_window_past_eof_is_empty(tmp_path: Path) -> None:
    (tmp_path / "lines.txt").write_text("one\ntwo\n", encoding="utf-8")

    result = _run(
        "read_batch",
        {"paths": ["lines.txt"], "offset": 10, "limit": 2},
        ToolContext(tmp_path),
    )

    assert result.ok
    assert result.output == "--- lines.txt ---\nshowing lines 10-2 of 2\n"


def test_read_batch_offset_only_uses_default_line_cap(tmp_path: Path) -> None:
    path = tmp_path / "lines.txt"
    path.write_text(
        "".join(f"line {number}\n" for number in range(1, MAX_READ_LINES + 2)),
        encoding="utf-8",
    )

    result = _run(
        "read_batch",
        {"paths": ["lines.txt"], "offset": 1},
        ToolContext(tmp_path),
    )

    assert result.ok
    assert f"showing lines 1-{MAX_READ_LINES} of {MAX_READ_LINES + 1}" in result.output
    assert f"line {MAX_READ_LINES}\n" in result.output
    assert f"line {MAX_READ_LINES + 1}\n" not in result.output


def test_read_batch_window_rejects_non_utf8_file_cleanly(tmp_path: Path) -> None:
    (tmp_path / "binary.bin").write_bytes(b"valid\n\xff\n")

    result = _run(
        "read_batch",
        {"paths": ["binary.bin"], "offset": 1},
        ToolContext(tmp_path),
    )

    assert not result.ok
    assert result.error is None
    assert result.output == "--- binary.bin ---\nfile is not valid UTF-8: binary.bin"
    assert "codec can't decode" not in result.output


@pytest.mark.parametrize(
    ("field", "value"),
    [("offset", 0), ("limit", 0), ("limit", MAX_READ_BYTES + 1)],
)
def test_read_batch_rejects_invalid_line_window(
    tmp_path: Path, field: str, value: int
) -> None:
    (tmp_path / "lines.txt").write_text("line\n", encoding="utf-8")

    result = _run(
        "read_batch",
        {"paths": ["lines.txt"], field: value},
        ToolContext(tmp_path),
    )

    assert not result.ok
    assert result.output == ""
    assert result.error is not None
    assert f"'{field}'" in result.error


def test_read_batch_allows_absolute_path(tmp_path: Path) -> None:
    outside = tmp_path.parent / "outside.txt"
    outside.write_text("outside\n", encoding="utf-8")

    result = _run("read_batch", {"paths": [str(outside)]}, ToolContext(tmp_path))

    assert result.ok
    assert result.output == f"--- {outside} ---\noutside\n"


def _batch_context(tmp_path: Path, events: list[dict] | None = None) -> ToolContext:
    return ToolContext(
        tmp_path,
        init={"tools": ["read_batch"]},
        emit=events.append if events is not None else None,
    )


@pytest.mark.slow  # 3x100ms scripted reads; concurrency proven by overlap, not wall clock
def test_read_batch_runs_three_100ms_reads_concurrently(tmp_path: Path, monkeypatch) -> None:
    for name in ("one.txt", "two.txt", "three.txt"):
        (tmp_path / name).write_text(name, encoding="utf-8")

    original = tools._read_file_sync
    state = {"active": 0, "max_active": 0}

    def delayed_read(path, display_path):
        state["active"] += 1
        state["max_active"] = max(state["max_active"], state["active"])
        try:
            time.sleep(0.1)
            return original(path, display_path)
        finally:
            state["active"] -= 1

    monkeypatch.setattr(tools, "_read_file_sync", delayed_read)
    results = asyncio.run(
        run_read_batch(
            [{"path": "one.txt"}, {"path": "two.txt"}, {"path": "three.txt"}],
            _batch_context(tmp_path),
        )
    )

    # Sequential execution would peak at 1 active read; overlap proves the
    # batch ran the three 100ms reads concurrently. Wall-clock bounds are
    # load-sensitive under parallel test runs (pytest-xdist), so the
    # overlap metric is the load-immune proof.
    assert state["max_active"] == 3
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
    assert all(
        set(event) == {"type", "tool", "batch_index", "batch_size", "ok", "duration_ms"}
        for event in events
    )


def test_read_batch_rejects_mixed_array_atomically(tmp_path: Path, monkeypatch) -> None:
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

    def counting_read(path, display_path):
        with lock:
            state["active"] += 1
            state["peak"] = max(state["peak"], state["active"])
        time.sleep(0.05)
        try:
            return original(path, display_path)
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


def test_read_batch_requires_read_batch_in_init_tools(tmp_path: Path) -> None:
    results = asyncio.run(run_read_batch([{"path": "anything.txt"}], ToolContext(tmp_path)))

    assert len(results) == 1
    assert not results[0].ok
    assert "read_batch is not offered in init.tools" in (results[0].error or "")


class _LintFeedback:
    def lint_file(self, path: Path) -> list[dict]:
        return [{"path": str(path), "line": 1, "col": 1, "code": "E999", "message": "bad"}]

    def format_diags(self, diagnostics: list[dict]) -> str:
        return "\n".join(
            f"{item['path']}:{item['line']}:{item['col']}: {item['code']} {item['message']}"
            for item in diagnostics
        )


def test_write_file_is_atomic_and_returns_lint_feedback(tmp_path: Path) -> None:
    result = _run(
        "write_file",
        {"path": "nested/new.py", "content": "broken(:\n"},
        ToolContext(tmp_path, lint=cast(LintDiag, _LintFeedback())),
    )

    assert result.ok
    assert (tmp_path / "nested/new.py").read_text(encoding="utf-8") == "broken(:\n"
    assert "Lint diagnostics:" in result.output
    assert "E999 bad" in result.output
    assert not list((tmp_path / "nested").glob("*.tmp"))


def test_write_file_allows_absolute_path(tmp_path: Path) -> None:
    outside = tmp_path.parent / "outside.txt"
    result = _run(
        "write_file",
        {"path": str(outside), "content": "outside"},
        ToolContext(tmp_path),
    )

    assert result.ok
    assert outside.read_text(encoding="utf-8") == "outside"


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

    outside = tmp_path.parent / "edit-outside.txt"
    outside.write_text("one\n", encoding="utf-8")
    absolute = _run(
        "edit_file",
        {"path": str(outside), "old_string": "one", "new_string": "two"},
        context,
    )
    assert absolute.ok
    assert outside.read_text(encoding="utf-8") == "two\n"


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


@pytest.mark.slow  # real long-running subprocess output is observed before exit
def test_run_shell_reports_bounded_output_progress(tmp_path: Path) -> None:
    progress: list[tuple[str, str]] = []
    command = [
        sys.executable,
        "-u",
        "-c",
        "import sys,time; print('first', flush=True); time.sleep(.15); "
        "print('second', flush=True); time.sleep(.15); print('third', flush=True)",
    ]

    result = _run(
        "run_shell",
        {"cmd": command},
        ToolContext(tmp_path, progress=lambda stream, delta: progress.append((stream, delta))),
    )

    assert result.ok
    assert result.output == "first\nsecond\nthird\n"
    assert len(progress) >= 2
    joined = "".join(delta for _stream, delta in progress)
    assert all(word in joined for word in ("first", "second", "third"))
    assert all(len(delta.encode()) <= tools._PROCESS_PROGRESS_MAX_BYTES for _, delta in progress)


@pytest.mark.slow  # python interpreter spawn; asserts subprocess execution
def test_run_shell_output_is_capped(tmp_path: Path) -> None:
    command = [sys.executable, "-c", "print('x' * 100000)"]
    result = _run("run_shell", {"cmd": command}, ToolContext(tmp_path))

    assert result.ok
    assert "bytes truncated, full output:" in result.output
    assert len(result.output.encode()) <= MAX_OUTPUT_BYTES


@pytest.mark.slow  # python interpreter spawn; asserts complete spill persistence
def test_run_shell_spills_oversized_output_to_session_dir(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    session_dir = tmp_path / "session"
    worktree = session_dir / "worktree"
    worktree.mkdir(parents=True)
    monkeypatch.setenv("CAMBIUM_SESSION_ID", str(session_dir))
    expected = "HEAD\n" + ("middle\n" * 5000) + "TAIL\n"
    command = [sys.executable, "-c", "import sys; sys.stdout.write(sys.argv[1])", expected]

    result = _run("run_shell", {"cmd": command}, ToolContext(worktree))

    assert result.ok
    assert result.output.startswith(expected[:SHELL_OUTPUT_HEAD_BYTES])
    assert result.output.endswith(expected[-SHELL_OUTPUT_TAIL_BYTES:])
    marker = next(line for line in result.output.splitlines() if line.startswith("[... "))
    spill_files = sorted((session_dir / ".cambium" / "spill").glob("run-*.txt"))
    assert len(spill_files) == 1
    spill_path = spill_files[0]
    assert f"{len(expected.encode())} bytes truncated" in marker
    assert f"full output: {spill_path} ..." in marker
    assert spill_path.read_text(encoding="utf-8") == expected


@pytest.mark.slow  # real subprocess tree and timeout
def test_run_shell_timeout_kills_background_grandchild(tmp_path: Path) -> None:
    pid_file = tmp_path / "grandchild.pid"
    script = (
        "import os, sys, time\n"
        "pid = os.fork()\n"
        "if pid == 0:\n"
        "    pid = os.fork()\n"
        "    if pid == 0:\n"
        "        open(sys.argv[1], 'w').write(str(os.getpid()))\n"
        "        time.sleep(60)\n"
        "    os._exit(0)\n"
        "os.waitpid(pid, 0)\n"
        "time.sleep(60)\n"
    )
    result = _run(
        "run_shell",
        {"cmd": [sys.executable, "-c", script, str(pid_file)], "timeout_s": 1},
        ToolContext(tmp_path),
    )

    assert not result.ok
    assert "timed out" in (result.error or "")
    grandchild_pid = int(pid_file.read_text())
    deadline = time.monotonic() + 5.0
    while time.monotonic() < deadline:
        try:
            os.kill(grandchild_pid, 0)
        except ProcessLookupError:
            break
        time.sleep(0.05)
    else:
        pytest.fail(f"grandchild {grandchild_pid} survived the process-group kill")


@pytest.mark.slow  # python linter subprocess and bounded timeout
def test_write_file_reports_lint_timeout(tmp_path: Path) -> None:
    from cambium.lint_diag import LintDiag

    lint = LintDiag(
        lint_cmd=[sys.executable, "-c", "import time; time.sleep(60)"],
        timeout_s=0.1,
    )
    result = _run(
        "write_file",
        {"path": "new.py", "content": "ok\n"},
        ToolContext(tmp_path, lint=lint),
    )

    assert result.ok
    assert "lint-timeout lint timed out after 0.1s" in result.output


def test_write_file_keeps_success_when_lint_reports_syntax_error(tmp_path: Path) -> None:
    lint = LintDiag(
        lint_cmd=[
            sys.executable,
            "-c",
            (
                'print(\'[{"code":"invalid-syntax","message":"bad syntax",'
                '"filename":"broken.py","location":{"row":1,"column":1}}]\')'
            ),
        ]
    )

    result = _run(
        "write_file",
        {"path": "broken.py", "content": "def broken(:\n"},
        ToolContext(tmp_path, lint=lint),
    )

    assert result.ok
    assert (tmp_path / "broken.py").read_text(encoding="utf-8") == "def broken(:\n"
    assert "lint: 1 error, 0 warnings" in result.output
    assert "E999 bad syntax" in result.output


def test_write_file_reports_clean_lint_suffix(tmp_path: Path) -> None:
    lint = LintDiag(lint_cmd=[sys.executable, "-c", "print('[]')"])

    result = _run(
        "write_file",
        {"path": "clean.py", "content": "value = 1\n"},
        ToolContext(tmp_path, lint=lint),
    )

    assert result.ok
    assert result.output.endswith("lint: clean")


@pytest.mark.slow  # python interpreter spawn; asserts subprocess execution
def test_tool_subprocesses_do_not_inherit_provider_credentials(tmp_path: Path, monkeypatch) -> None:
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
    invalid = _run("read_batch", {}, ToolContext(tmp_path))
    assert not invalid.ok
    assert invalid.error == "validation failed: missing 'paths' (array)"

    unknown = _run("does_not_exist", {}, ToolContext(tmp_path))
    assert not unknown.ok
    assert unknown.error == "unknown tool: 'does_not_exist'"

    removed = _run("read_file", {"path": "anything.txt"}, ToolContext(tmp_path))
    assert not removed.ok
    assert removed.error == "unknown tool: 'read_file'"


def test_delegate_rejects_unknown_task_kind_at_call_time(tmp_path: Path) -> None:
    invalid = _run(
        "delegate",
        {
            "child_task_id": "child",
            "kind": "message",
            "spec": {"task": "child task"},
        },
        ToolContext(tmp_path),
    )
    assert not invalid.ok
    assert invalid.error == (
        "validation failed: unknown task kind message "
        "(allowed: feature, bugfix, refactor, test, docs, investigation)"
    )

    valid = _run(
        "delegate",
        {
            "child_task_id": "child",
            "kind": "test",
            "spec": {"task": "child task"},
        },
        ToolContext(tmp_path),
    )
    assert valid.ok
