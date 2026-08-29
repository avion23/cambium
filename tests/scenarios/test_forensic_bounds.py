"""Regression coverage for worker forensic-read boundaries."""

from __future__ import annotations

import asyncio
import sys
from pathlib import Path

from cambium.prompts import CODING_AGENT
from cambium.tools import (
    READ_BATCH_MAX_BYTES_PER_FILE,
    READ_BATCH_MAX_FILES,
    ToolContext,
    run_tool,
)


def _run(name: str, args: dict, ctx: ToolContext):
    return asyncio.run(run_tool(name, args, ctx))


def test_coding_prompt_forbids_recursive_session_investigation() -> None:
    assert (
        "Do not recursively investigate the worker's own session artifacts, logs, or spill files; "
        "stay focused on the assigned task."
        in CODING_AGENT
    )


def test_read_batch_refuses_own_active_session(tmp_path: Path, monkeypatch) -> None:
    sessions = tmp_path / ".cambium" / "sessions"
    current = sessions / "run-current"
    current.mkdir(parents=True)
    artifact = current / ".cambium" / "events.db"
    artifact.parent.mkdir()
    artifact.write_text("private", encoding="utf-8")
    monkeypatch.setenv("CAMBIUM_SESSION_ID", str(current))

    result = _run("read_batch", {"paths": [str(artifact)]}, ToolContext(tmp_path))

    assert not result.ok
    assert "worker's own active session" in result.output
    assert "assigned task" in result.output
    assert "private" not in result.output


def test_read_batch_allows_an_earlier_session(tmp_path: Path, monkeypatch) -> None:
    sessions = tmp_path / ".cambium" / "sessions"
    current = sessions / "run-current"
    earlier = sessions / "run-earlier"
    current.mkdir(parents=True)
    earlier.mkdir()
    artifact = earlier / ".cambium" / "events.db"
    artifact.parent.mkdir()
    artifact.write_text("earlier-session", encoding="utf-8")
    monkeypatch.setenv("CAMBIUM_SESSION_ID", str(current))

    result = _run("read_batch", {"paths": [str(artifact)]}, ToolContext(tmp_path))

    assert result.ok
    assert "earlier-session" in result.output


def test_read_batch_caps_files_and_bytes_without_error_spiral(tmp_path: Path) -> None:
    paths = []
    for index in range(READ_BATCH_MAX_FILES + 2):
        path = tmp_path / f"file-{index}.txt"
        path.write_text(str(index), encoding="utf-8")
        paths.append(path.name)
    oversized = tmp_path / "oversized.txt"
    oversized.write_bytes(b"x" * (READ_BATCH_MAX_BYTES_PER_FILE + 1))

    result = _run(
        "read_batch",
        {"paths": paths + [oversized.name]},
        ToolContext(tmp_path),
    )

    assert result.ok
    assert result.output.count("--- ") == READ_BATCH_MAX_FILES
    assert "read_batch capped" in result.output
    assert "[file truncated]" not in result.output

    oversized_result = _run("read_batch", {"paths": [oversized.name]}, ToolContext(tmp_path))
    assert oversized_result.ok
    assert "[file truncated]" in oversized_result.output
    assert "[output truncated]" not in oversized_result.output
    assert len(oversized_result.output.encode()) < READ_BATCH_MAX_BYTES_PER_FILE + 100


def test_run_shell_can_still_read_session_artifacts(tmp_path: Path, monkeypatch) -> None:
    current = tmp_path / ".cambium" / "sessions" / "run-current"
    current.mkdir(parents=True)
    artifact = current / ".cambium" / "events.db"
    artifact.parent.mkdir()
    artifact.write_text("shell-access", encoding="utf-8")
    monkeypatch.setenv("CAMBIUM_SESSION_ID", str(current))

    result = _run(
        "run_shell",
        {
            "cmd": [
                sys.executable,
                "-c",
                "import pathlib, sys; print(pathlib.Path(sys.argv[1]).read_text())",
                str(artifact),
            ]
        },
        ToolContext(tmp_path),
    )

    assert result.ok
    assert result.output == "shell-access\n"
