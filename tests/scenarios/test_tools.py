"""Data-driven scenarios for the public worker tool boundary."""

from __future__ import annotations

import asyncio
import hashlib
import os
import sys
import time
from pathlib import Path
from typing import cast

import pytest

from cambium.lint_diag import LintDiag
from cambium.tools import (
    MAX_EDIT_BYTES,
    MAX_OUTPUT_BYTES,
    MAX_READ_BYTES,
    MAX_READ_LINES,
    READ_BATCH_MAX_BYTES_PER_FILE,
    SHELL_OUTPUT_HEAD_BYTES,
    SHELL_OUTPUT_TAIL_BYTES,
    ToolContext,
    is_parallel_shell_call,
    is_parallel_shell_check,
    is_parallel_shell_command,
    run_shell_batch,
    run_tool,
)


def _run(name: str, args: dict, ctx: ToolContext):
    return asyncio.run(run_tool(name, args, ctx))


@pytest.mark.parametrize(
    ("command", "eligible"),
    [
        (["pytest", "-q", "tests/a.py"], True),
        ([sys.executable, "-m", "ruff", "check", "src"], True),
        (["ruff", "check", "src"], True),
        (["git", "diff", "--check"], True),
        (["python", "-m", "pytest", "tests/a.py", "--cov=src"], False),
        (["ruff", "check", "--fix", "src"], False),
        (["sh", "-c", "pytest -q"], False),
        (["npm", "test"], False),
        (["git", "status"], False),
    ],
)
def test_parallel_shell_command_allowlist(command: list[str], eligible: bool) -> None:
    assert is_parallel_shell_command(command) is eligible
    arguments = {"cmd": command, "parallel": True}
    assert is_parallel_shell_check(arguments) is eligible
    assert is_parallel_shell_call({"name": "run_shell", "arguments": arguments}) is eligible
    assert is_parallel_shell_call(arguments) is eligible


@pytest.mark.slow
def test_parallel_shell_batch_overlaps_eligible_checks_and_preserves_order(tmp_path: Path) -> None:
    first = tmp_path / "test_first.py"
    second = tmp_path / "test_second.py"
    first_started = tmp_path / "first.started"
    first_finished = tmp_path / "first.finished"
    second_started = tmp_path / "second.started"
    second_finished = tmp_path / "second.finished"
    first.write_text(
        f"import time\nimport unittest\n\n"
        "class TestFirst(unittest.TestCase):\n"
        "    def test_first(self):\n"
        f"        open({str(first_started)!r}, 'w').write(str(time.time_ns()))\n"
        "        time.sleep(.25)\n"
        f"        open({str(first_finished)!r}, 'w').write(str(time.time_ns()))\n",
        encoding="utf-8",
    )
    second.write_text(
        f"import time\nimport unittest\n\n"
        "class TestSecond(unittest.TestCase):\n"
        "    def test_second(self):\n"
        f"        open({str(second_started)!r}, 'w').write(str(time.time_ns()))\n"
        "        time.sleep(.25)\n"
        f"        open({str(second_finished)!r}, 'w').write(str(time.time_ns()))\n",
        encoding="utf-8",
    )
    results = asyncio.run(
        run_shell_batch(
            [
                {
                    "name": "run_shell",
                    "arguments": {
                        "cmd": [
                            sys.executable,
                            "-m",
                            "unittest",
                            str(first),
                        ],
                        "parallel": True,
                    },
                },
                {
                    "name": "run_shell",
                    "arguments": {
                        "cmd": [
                            sys.executable,
                            "-m",
                            "unittest",
                            str(second),
                        ],
                        "parallel": True,
                    },
                },
            ],
            ToolContext(tmp_path),
        )
    )

    assert [result.ok for result in results] == [True, True]
    assert "OK" in results[0].output
    assert "OK" in results[1].output
    starts = [int(first_started.read_text()), int(second_started.read_text())]
    finishes = [int(first_finished.read_text()), int(second_finished.read_text())]
    assert max(starts) < min(finishes)


def test_parallel_shell_batch_rejects_mixed_or_unopted_calls(tmp_path: Path) -> None:
    results = asyncio.run(
        run_shell_batch(
            [
                {
                    "name": "run_shell",
                    "arguments": {"cmd": ["pytest", "-q"], "parallel": True},
                },
                {"name": "run_shell", "arguments": {"cmd": ["pytest", "-q"]}},
            ],
            ToolContext(tmp_path),
        )
    )

    assert len(results) == 2
    assert all(not result.ok for result in results)
    assert {result.error for result in results} == {
        "run_shell parallel batch rejected: every call must set parallel=true and use "
        "an allowlisted local check/test command"
    }


def test_read_batch_returns_bounded_files_and_windows(tmp_path: Path) -> None:
    (tmp_path / "hello.txt").write_text("hello\n", encoding="utf-8")
    (tmp_path / "lines.txt").write_text(
        "".join(f"line {number}\n" for number in range(1, 6)),
        encoding="utf-8",
    )
    (tmp_path / "large.txt").write_bytes(b"x" * (READ_BATCH_MAX_BYTES_PER_FILE + 1))

    whole = _run("read_batch", {"paths": ["hello.txt"]}, ToolContext(tmp_path))
    window = _run(
        "read_batch",
        {"paths": ["lines.txt"], "offset": 2, "limit": 2},
        ToolContext(tmp_path),
    )
    capped = _run("read_batch", {"paths": ["large.txt"]}, ToolContext(tmp_path))

    assert whole.ok and whole.output == "--- hello.txt ---\nhello\n"
    assert window.ok and window.output == (
        "--- lines.txt ---\nshowing lines 2-3 of 5\nline 2\nline 3\n"
    )
    assert capped.ok
    assert "[file truncated]" in capped.output
    assert len(capped.output.encode()) <= MAX_OUTPUT_BYTES


def test_truncated_reads_validate_utf8_without_rejecting_split_characters(tmp_path: Path) -> None:
    path = tmp_path / "large.txt"
    for prefix, expected_ok in ((b"\xff", False), (b"", True)):
        path.write_bytes(prefix + "😀".encode() * READ_BATCH_MAX_BYTES_PER_FILE)
        result = _run("read_batch", {"paths": [path.name]}, ToolContext(tmp_path))
        assert result.ok is expected_ok
        if expected_ok:
            assert "[file truncated]" in result.output
            assert "😀" in result.output
        else:
            assert "file is not valid UTF-8: large.txt" in result.output


def test_read_batch_reports_an_empty_window_past_eof(tmp_path: Path) -> None:
    (tmp_path / "lines.txt").write_text("one\ntwo\n", encoding="utf-8")

    result = _run(
        "read_batch",
        {"paths": ["lines.txt"], "offset": 10, "limit": 2},
        ToolContext(tmp_path),
    )

    assert result.ok
    assert result.output == ("--- lines.txt ---\nshowing no lines from 10; file has 2 lines\n")


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


def test_read_batch_allows_external_read_but_blocks_session_internals(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    session = tmp_path / "session"
    worktree = session / "worktree"
    worktree.mkdir(parents=True)
    external = tmp_path / "external.txt"
    external.write_text("external\n", encoding="utf-8")
    internal = session / ".cambium" / "events.db"
    internal.parent.mkdir()
    internal.write_text("secret\n", encoding="utf-8")
    monkeypatch.setenv("CAMBIUM_SESSION_ID", str(session))

    readable = _run("read_batch", {"paths": [str(external)]}, ToolContext(worktree))
    refused = _run("read_batch", {"paths": [str(internal)]}, ToolContext(worktree))

    assert readable.ok and readable.output.endswith("external\n")
    assert not refused.ok
    assert "read refused" in refused.output
    assert "secret" not in refused.output


def test_read_batch_rejects_invalid_windows_and_non_utf8(
    tmp_path: Path,
) -> None:
    (tmp_path / "lines.txt").write_text("line\n", encoding="utf-8")
    (tmp_path / "binary.bin").write_bytes(b"valid\n\xff\n")

    for field, value in (
        ("offset", 0),
        ("limit", 0),
        ("limit", MAX_READ_BYTES + 1),
    ):
        result = _run(
            "read_batch",
            {"paths": ["lines.txt"], field: value},
            ToolContext(tmp_path),
        )
        assert not result.ok
        assert result.output == ""
        assert f"'{field}'" in (result.error or "")

    binary = _run(
        "read_batch",
        {"paths": ["binary.bin"], "offset": 1},
        ToolContext(tmp_path),
    )
    assert not binary.ok
    assert binary.error is None
    assert binary.output == "--- binary.bin ---\nfile is not valid UTF-8: binary.bin"


class _LintFeedback:
    def lint_file(self, path: Path) -> list[dict]:
        return [{"path": str(path), "line": 1, "col": 1, "code": "E999", "message": "bad"}]

    def format_diags(self, diagnostics: list[dict]) -> str:
        return "\n".join(
            f"{item['path']}:{item['line']}:{item['col']}: {item['code']} {item['message']}"
            for item in diagnostics
        )


def test_write_and_edit_apply_atomic_worktree_changes(tmp_path: Path) -> None:
    write = _run(
        "write_file",
        {"path": "nested/new.py", "content": "broken(:\n"},
        ToolContext(tmp_path, lint=cast(LintDiag, _LintFeedback())),
    )
    edit = _run(
        "edit_file",
        {"path": "nested/new.py", "old_string": "broken(:", "new_string": "value = 1"},
        ToolContext(tmp_path),
    )

    assert write.ok and "E999 bad" in write.output
    assert edit.ok
    assert (tmp_path / "nested/new.py").read_text(encoding="utf-8") == "value = 1\n"
    assert not list((tmp_path / "nested").glob("*.tmp"))


@pytest.mark.parametrize(
    ("tool", "path", "arguments"),
    [
        ("write_file", "ABSOLUTE", {"content": "outside"}),
        ("edit_file", "../outside.txt", {"old_string": "one", "new_string": "two"}),
        ("write_file", ".git/config", {"content": "unsafe"}),
        ("write_file", "escape/outside.txt", {"content": "unsafe"}),
    ],
)
def test_mutating_tools_stay_inside_normal_worktree_files(
    tmp_path: Path,
    tool: str,
    path: str,
    arguments: dict[str, str],
) -> None:
    outside = tmp_path.parent / "outside.txt"
    outside.write_text("one\n", encoding="utf-8")
    (tmp_path / ".git").mkdir()
    (tmp_path / "escape").symlink_to(tmp_path.parent, target_is_directory=True)
    requested = str(outside) if path == "ABSOLUTE" else path

    result = _run(tool, {"path": requested, **arguments}, ToolContext(tmp_path))

    assert not result.ok
    assert "mutation path must stay inside the assigned worktree" in (result.error or "")
    assert outside.read_text(encoding="utf-8") == "one\n"
    assert not (tmp_path / ".git/config").exists()


@pytest.mark.parametrize("size", [MAX_READ_BYTES + 1, MAX_EDIT_BYTES, MAX_EDIT_BYTES + 1])
def test_edit_file_source_limit_is_separate_from_read_output(tmp_path: Path, size: int) -> None:
    path = tmp_path / "large.txt"
    original = "needle\n" + "x" * (size - 7)
    path.write_text(original, encoding="utf-8")
    result = _run(
        "edit_file",
        {"path": path.name, "old_string": "needle", "new_string": "edited"},
        ToolContext(tmp_path),
    )
    assert result.ok == (size <= MAX_EDIT_BYTES)
    assert path.read_text() == (
        original.replace("needle", "edited") if result.ok else original
    )
    if not result.ok:
        assert result.error is not None
        assert "MAX_EDIT_BYTES" in result.error


def test_edit_file_requires_exactly_one_occurrence(tmp_path: Path) -> None:
    path = tmp_path / "edit.txt"
    path.write_text("one\none\n", encoding="utf-8")

    result = _run(
        "edit_file",
        {"path": "edit.txt", "old_string": "one", "new_string": "two"},
        ToolContext(tmp_path),
    )

    assert not result.ok
    assert "exactly one occurrence" in (result.error or "")
    assert "Context:" in (result.error or "")
    assert path.read_text(encoding="utf-8") == "one\none\n"


@pytest.mark.slow
def test_run_shell_returns_bounded_output_and_complete_spill(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    session = tmp_path / "session"
    worktree = session / "worktree"
    worktree.mkdir(parents=True)
    monkeypatch.setenv("CAMBIUM_SESSION_ID", str(session))
    expected = "HEAD\n" + ("middle\n" * 5000) + "TAIL\n"

    result = _run(
        "run_shell",
        {"cmd": [sys.executable, "-c", "import sys; sys.stdout.write(sys.argv[1])", expected]},
        ToolContext(worktree),
    )

    assert result.ok
    assert result.output.startswith(expected[:SHELL_OUTPUT_HEAD_BYTES])
    assert result.output.endswith(expected[-SHELL_OUTPUT_TAIL_BYTES:])
    spill_files = sorted((session / ".cambium" / "spill").glob("run-*.txt"))
    assert len(spill_files) == 1
    assert spill_files[0].read_text(encoding="utf-8") == expected
    assert result.output_ref == f".cambium/spill/{spill_files[0].name}"
    assert result.output_sha256 == hashlib.sha256(expected.encode()).hexdigest()
    assert result.output_bytes == len(expected.encode())
    assert len(result.output.encode()) <= MAX_OUTPUT_BYTES


@pytest.mark.slow
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


@pytest.mark.slow
def test_tool_subprocess_boundaries_do_not_leak_provider_credentials(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("CAMBIUM_PROVIDER_OPENAI_API_KEY", "provider-secret")
    shell = _run(
        "run_shell",
        {
            "cmd": [
                sys.executable,
                "-c",
                "import os; print(os.environ.get('CAMBIUM_PROVIDER_OPENAI_API_KEY', 'clean'))",
            ]
        },
        ToolContext(tmp_path),
    )
    lint_script = (
        "import json, os, sys\n"
        "secret = os.environ.get('CAMBIUM_PROVIDER_OPENAI_API_KEY', 'clean')\n"
        "print(json.dumps([{'code': 'E001', 'message': secret, "
        "'filename': sys.argv[1], 'location': {'row': 1, 'column': 1}}]))\n"
    )
    lint = _run(
        "write_file",
        {"path": "new.py", "content": "ok\n"},
        ToolContext(tmp_path, lint=LintDiag(lint_cmd=[sys.executable, "-c", lint_script])),
    )

    assert shell.ok and shell.output == "clean\n"
    assert lint.ok
    assert "provider-secret" not in lint.output
    assert "clean" in lint.output
