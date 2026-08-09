"""Nuntius IPC framing + worker runtime scenario tests.

Framing tests exercise ``cambium.ipc.read_message`` against an in-memory
``asyncio.StreamReader`` (partial-line delivery, garbage skipping, the
over-limit raise + resync, and the EOF partial-line discard rule).

Worker tests spawn a real ``cambium.worker`` subprocess and drive it with a
scripted supervisor mock over stdio: init -> ready (rid + generation echoed),
run_task -> heartbeat(s) -> result_envelope (rid correlated to run_task) ->
exit_message (no request_id) -> exit 0. Negative path: invalid input ->
fatal_error + exit_message(reason=fatal) + nonzero exit. Cancel path: cancel
during a running task -> status cancelled + exit 4.
"""

from __future__ import annotations

import asyncio
import json
import os
import signal
import socket
import subprocess
import sys
from pathlib import Path

import pytest

from cambium.ipc import (
    MAX_LINE_BYTES,
    MessageTooLong,
    make_request_id,
    read_message,
    write_message,
)

MARKER = "// cambium-ipc"


def _make_scratch(repo: Path) -> str:
    subprocess.run(["git", "init", "-b", "main", str(repo)], check=True, capture_output=True)
    subprocess.run(["git", "-C", str(repo), "config", "user.name", "ipc-test"], check=True)
    subprocess.run(["git", "-C", str(repo), "config", "user.email", "ipc@test"], check=True)
    subprocess.run(["git", "-C", str(repo), "config", "gc.auto", "0"], check=True)
    (repo / "hello.txt").write_text("hello from the ipc test\n")
    subprocess.run(["git", "-C", str(repo), "add", "hello.txt"], check=True)
    subprocess.run(
        ["git", "-C", str(repo), "commit", "-m", "initial"], check=True, capture_output=True
    )
    return subprocess.run(
        ["git", "-C", str(repo), "rev-parse", "HEAD"], check=True, capture_output=True, text=True
    ).stdout.strip()


def _run_task_msg(session_dir: Path, *, run_rid: str, **overrides: object) -> dict:
    msg: dict = {
        "type": "run_task",
        "request_id": run_rid,
        "task_id": "ipc-001",
        "scratch_repo": str(session_dir / "scratch"),
        "worktree_path": str(session_dir / "wt"),
        "branch": "wt-ipc-001",
        "target_file": "hello.txt",
        "marker": MARKER,
        "write_marker": True,
    }
    msg.update(overrides)
    return msg


class WorkerSupervisor:
    """Scripted supervisor side: drives one cambium.worker subprocess."""

    def __init__(self) -> None:
        self.proc: asyncio.subprocess.Process | None = None
        self.stderr_lines: list[str] = []
        self._stderr_task: asyncio.Task | None = None

    async def start(self) -> None:
        self.proc = await asyncio.create_subprocess_exec(
            sys.executable, "-u", "-m", "cambium.worker",
            stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            env={**os.environ, "PYTHONUNBUFFERED": "1"},
            start_new_session=True,
        )
        self._stderr_task = asyncio.create_task(self._drain_stderr())

    async def _drain_stderr(self) -> None:
        assert self.proc is not None
        while True:
            raw = await self.proc.stderr.readline()
            if not raw:
                break
            self.stderr_lines.append(raw.decode("utf-8", "replace").rstrip())

    async def send(self, msg: dict) -> None:
        assert self.proc is not None
        self.proc.stdin.write((json.dumps(msg) + "\n").encode("utf-8"))
        await self.proc.stdin.drain()

    async def recv(self, timeout: float = 30.0) -> dict | None:
        assert self.proc is not None
        return await asyncio.wait_for(
            read_message(self.proc.stdout, limit=MAX_LINE_BYTES), timeout
        )

    async def recv_result(self, timeout: float = 30.0) -> tuple[dict, list[dict]]:
        """Read until result_envelope; assert intervening messages are heartbeats."""
        assert self.proc is not None
        deadline = asyncio.get_running_loop().time() + timeout
        heartbeats: list[dict] = []
        while True:
            remaining = deadline - asyncio.get_running_loop().time()
            if remaining <= 0:
                raise AssertionError("timed out waiting for result_envelope")
            msg = await asyncio.wait_for(
                read_message(self.proc.stdout, limit=MAX_LINE_BYTES), remaining
            )
            if msg is None:
                raise AssertionError(f"EOF while waiting for result_envelope; "
                                     f"stderr={self.stderr_lines!r}")
            mtype = msg["type"]
            if mtype == "result_envelope":
                return msg, heartbeats
            if mtype != "heartbeat":
                raise AssertionError(
                    f"unexpected {mtype!r} while waiting for result_envelope; "
                    f"stderr={self.stderr_lines!r}"
                )
            heartbeats.append(msg)

    async def stop(self) -> None:
        if self.proc is not None:
            if self.proc.returncode is None:
                try:
                    os.killpg(self.proc.pid, signal.SIGKILL)
                except (ProcessLookupError, PermissionError):
                    pass
                await self.proc.wait()
            if self._stderr_task is not None:
                try:
                    await asyncio.wait_for(self._stderr_task, 5.0)
                except (asyncio.TimeoutError, asyncio.CancelledError):
                    pass


# ── framing ────────────────────────────────────────────────────────────────


def test_read_message_partial_line_delivery_across_reads() -> None:
    async def scenario() -> None:
        reader = asyncio.StreamReader()
        reader.feed_data(b'{"type": "ready", "reque')
        reader.feed_data(b'st_id": "abc"}\n')
        reader.feed_eof()
        assert await read_message(reader) == {"type": "ready", "request_id": "abc"}
        assert await read_message(reader) is None

    asyncio.run(scenario())


def test_read_message_skips_garbage_and_non_object_lines() -> None:
    async def scenario() -> None:
        reader = asyncio.StreamReader()
        reader.feed_data(b"this is not json\n")
        reader.feed_data(b'["not", "an", "object"]\n')
        reader.feed_data(b'{"type": "ok", "request_id": "r1"}\n')
        reader.feed_eof()
        assert await read_message(reader) == {"type": "ok", "request_id": "r1"}

    asyncio.run(scenario())


def test_read_message_skips_blank_lines() -> None:
    async def scenario() -> None:
        reader = asyncio.StreamReader()
        reader.feed_data(b"\n   \n\n")
        reader.feed_data(b'{"type": "ok", "request_id": "r5"}\n')
        reader.feed_eof()
        assert await read_message(reader) == {"type": "ok", "request_id": "r5"}

    asyncio.run(scenario())


def test_read_message_over_limit_raises_and_resyncs() -> None:
    async def scenario() -> None:
        reader = asyncio.StreamReader()
        reader.feed_data(b"x" * 4096)  # no newline yet: over a 64-byte cap
        reader.feed_data(b"\n")
        reader.feed_data(b'{"type": "ok", "request_id": "r2"}\n')
        reader.feed_eof()
        with pytest.raises(MessageTooLong):
            await read_message(reader, limit=64)
        # the oversized line was discarded and the stream resynced
        assert await read_message(reader, limit=64) == {"type": "ok", "request_id": "r2"}

    asyncio.run(scenario())


def test_read_message_complete_line_over_limit_raises() -> None:
    async def scenario() -> None:
        reader = asyncio.StreamReader()
        reader.feed_data(b"y" * 100 + b"\n")
        reader.feed_data(b'{"type": "ok", "request_id": "r3"}\n')
        reader.feed_eof()
        with pytest.raises(MessageTooLong):
            await read_message(reader, limit=64)
        assert await read_message(reader, limit=64) == {"type": "ok", "request_id": "r3"}

    asyncio.run(scenario())


def test_read_message_eof_mid_line_returns_none() -> None:
    async def scenario() -> None:
        reader = asyncio.StreamReader()
        reader.feed_data(b'{"type": "bro')
        reader.feed_eof()
        assert await read_message(reader) is None

    asyncio.run(scenario())


def test_read_message_eof_after_newline_returns_message_then_none() -> None:
    async def scenario() -> None:
        reader = asyncio.StreamReader()
        reader.feed_data(b'{"type": "ok", "request_id": "r4"}\n{"type": "pa')
        reader.feed_eof()
        assert await read_message(reader) == {"type": "ok", "request_id": "r4"}
        # trailing partial line is discarded at EOF
        assert await read_message(reader) is None

    asyncio.run(scenario())


def test_message_too_long_is_value_error() -> None:
    assert issubclass(MessageTooLong, ValueError)


def test_make_request_id_is_unique_and_prefixed() -> None:
    ids = {make_request_id() for _ in range(100)}
    assert len(ids) == 100
    assert all(i.startswith("req-") for i in ids)
    assert make_request_id("init").startswith("init-")


def test_write_message_roundtrip() -> None:
    async def scenario() -> None:
        rsock, wsock = socket.socketpair()
        loop = asyncio.get_running_loop()
        rsock.setblocking(False)
        wsock.setblocking(False)
        reader, writer = await asyncio.open_connection(sock=rsock)
        try:
            write_message(writer, {"type": "ok", "request_id": "abc"})
            await writer.drain()
            data = await loop.sock_recv(wsock, 4096)
            assert data == b'{"type": "ok", "request_id": "abc"}\n'
        finally:
            writer.close()
            await writer.wait_closed()
            rsock.close()
            wsock.close()

    asyncio.run(scenario())


# ── worker handshake ───────────────────────────────────────────────────────


def test_worker_happy_path_handshake(tmp_path) -> None:
    session_dir = tmp_path / "session"
    _make_scratch(session_dir / "scratch")
    init_rid = "init-0001"
    run_rid = "run-0001"

    async def scenario() -> None:
        w = WorkerSupervisor()
        await w.start()
        try:
            await w.send({"type": "init", "request_id": init_rid, "task_id": "ipc-001",
                          "generation": 2, "proto": 1})
            ready = await w.recv()
            assert ready["type"] == "ready"
            assert ready["request_id"] == init_rid  # ready echoes init rid
            assert ready["generation"] == 2
            assert ready["proto"] == 1
            assert ready["task_id"] == "ipc-001"

            await w.send(_run_task_msg(session_dir, run_rid=run_rid))
            result, heartbeats = await w.recv_result()

            # correlation: result_envelope echoes run_task rid
            assert result["request_id"] == run_rid
            assert result["task_id"] == "ipc-001"
            assert result["generation"] == 2
            assert result["status"] == "succeeded"
            assert result["exit_code"] == 0
            assert result["failure_reason"] is None
            assert len(result["commits"]) == 1
            assert result["files_changed"] == ["hello.txt"]
            assert result["diff"]
            assert len(result["diff"]) <= 64 * 1024
            assert len(result["summary"]) <= 2000

            assert heartbeats, "expected at least one heartbeat while working"
            for hb in heartbeats:
                assert hb["generation"] == 2
                assert hb["task_id"] == "ipc-001"

            exit_msg = await w.recv()
            assert exit_msg["type"] == "exit_message"
            assert "request_id" not in exit_msg  # connection-level: no rid
            assert exit_msg["reason"] == "done"
            assert exit_msg["task_id"] == "ipc-001"

            rc = await w.proc.wait()
            assert rc == 0
        finally:
            await w.stop()

    asyncio.run(scenario())


def test_worker_invalid_input_fatal_error(tmp_path) -> None:
    session_dir = tmp_path / "session"
    _make_scratch(session_dir / "scratch")
    init_rid = "init-neg-1"

    async def scenario() -> None:
        w = WorkerSupervisor()
        await w.start()
        try:
            await w.send({"type": "init", "request_id": init_rid, "task_id": "ipc-neg",
                          "generation": 1})
            ready = await w.recv()
            assert ready["type"] == "ready"

            await w.send({"type": "bogus_request", "request_id": "x"})
            fatal = await w.recv()
            assert fatal["type"] == "fatal_error"
            assert fatal["request_id"] == "x"  # echoes the parseable request's rid
            assert fatal["recoverable"] is False

            exit_msg = await w.recv()
            assert exit_msg["type"] == "exit_message"
            assert exit_msg["reason"] == "fatal"

            rc = await w.proc.wait()
            assert rc != 0
        finally:
            await w.stop()

    asyncio.run(scenario())


def test_worker_cancel_aborts_current_task(tmp_path) -> None:
    session_dir = tmp_path / "session"
    _make_scratch(session_dir / "scratch")
    init_rid = "init-cancel-1"
    run_rid = "run-cancel-1"

    async def scenario() -> None:
        w = WorkerSupervisor()
        await w.start()
        try:
            await w.send({"type": "init", "request_id": init_rid, "task_id": "ipc-cancel",
                          "generation": 3})
            ready = await w.recv()
            assert ready["type"] == "ready"

            await w.send(_run_task_msg(
                session_dir, run_rid=run_rid, task_id="ipc-cancel", work_delay_s=5.0))
            hb = await w.recv()
            assert hb["type"] == "heartbeat"

            await w.send({"type": "cancel", "request_id": "cancel-1", "reason": "test"})
            result, _heartbeats = await w.recv_result()
            assert result["request_id"] == run_rid
            assert result["status"] == "cancelled"
            assert result["exit_code"] == 4

            exit_msg = await w.recv()
            assert exit_msg["type"] == "exit_message"
            assert exit_msg["reason"] == "cancelled"

            rc = await w.proc.wait()
            assert rc == 4
        finally:
            await w.stop()

    asyncio.run(scenario())
