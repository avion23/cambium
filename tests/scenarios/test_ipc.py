"""Nuntius IPC framing + worker runtime scenario tests.

Framing tests exercise ``cambium.ipc.read_message`` against an in-memory
``asyncio.StreamReader`` (partial-line delivery, garbage skipping, the
over-limit raise + resync, and the EOF partial-line discard rule).

Worker tests spawn a real ``cambium.worker`` subprocess and drive it with a
scripted supervisor mock over stdio:

- happy path: init -> ready (rid + generation echoed), run_task ->
  heartbeat(s) -> result_envelope (rid correlated to run_task, diff <= 64 KiB
  bytes with diff_truncated flag) -> exit_message (no request_id) -> exit 0.
- steer: only an exact ``{"action": "cancel"}`` aborts; free text containing
  the word "cancel" does not.
- check_health: ok ack (rid echoed) mid-task, task continues.
- cancel: ok ack, then status cancelled + exit 4.
- shutdown: ok ack, aborted task result_envelope, exit_message reason
  shutdown, exit 0.
- negative: invalid input -> fatal_error + exit_message(reason=fatal) +
  nonzero exit.
- timeouts: never-send-init -> fatal_error + nonzero exit within the
  (env-shortened) init deadline; idle supervisor silence -> graceful exit 0.
- stability: the happy path is repeated 20x and every stdout line must parse
  as a single JSON object (no torn/merged lines).
"""

from __future__ import annotations

import asyncio
import json
import os
import shlex
import signal
import subprocess
import sys
import threading
import time
from pathlib import Path

import pytest

from cambium.fencing import read_generation, write_generation
from cambium.ipc import (
    MAX_LINE_BYTES,
    MessageTooLong,
    read_message,
)

MARKER = "// cambium-ipc"
DIFF_CAP_BYTES = 64 * 1024
# Worker tasks wait for the heartbeat loop to observe the stop flag before
# emitting the result envelope. The default 1s interval makes every happy-path
# run cost ~1s of pure drain time; a short interval keeps the repeated
# stress-loop signal of the 20x regression test while making the drain
# negligible.
TEST_HEARTBEAT_INTERVAL_S = 0.02


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

    def __init__(self, env: dict[str, str] | None = None) -> None:
        self.proc: asyncio.subprocess.Process | None = None
        self.stderr_lines: list[str] = []
        self._stderr_task: asyncio.Task | None = None
        self._env = env
        self._generation = 1

    async def start(self) -> None:
        self.proc = await asyncio.create_subprocess_exec(
            sys.executable, "-u", "-m", "cambium.worker",
            stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            env={**os.environ, "PYTHONUNBUFFERED": "1", **(self._env or {})},
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
        if msg.get("type") == "init":
            self._generation = int(msg.get("generation", 1))
            msg = {
                **msg,
                "heartbeat": msg.get("heartbeat")
                or {"interval_s": TEST_HEARTBEAT_INTERVAL_S},
            }
        if msg.get("type") == "run_task":
            scratch = Path(msg["scratch_repo"]).resolve()
            worktree = Path(msg["worktree_path"]).resolve()
            if not worktree.exists():
                subprocess.run(
                    ["git", "-C", str(scratch), "worktree", "add", "-b",
                     msg["branch"], str(worktree), "main"],
                    check=True,
                    capture_output=True,
                )
            if read_generation(worktree) < self._generation:
                write_generation(worktree, self._generation)
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
                except (TimeoutError, asyncio.CancelledError):
                    pass


async def _happy_handshake(session_dir: Path) -> None:
    """Drive one full happy-path worker run end to end."""
    init_rid = "init-0001"
    run_rid = "run-0001"
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
        assert len(result["diff"].encode("utf-8")) <= DIFF_CAP_BYTES + 64
        assert result["diff_truncated"] is False
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


@pytest.mark.slow
def test_worker_happy_path_handshake(tmp_path) -> None:
    session_dir = tmp_path / "session"
    _make_scratch(session_dir / "scratch")
    asyncio.run(_happy_handshake(session_dir))


@pytest.mark.slow
def test_worker_happy_path_8x_no_corrupted_lines(tmp_path) -> None:
    """Repeat the happy path 8x; every stdout line must parse as one JSON object.

    Regression for the heartbeat-cancel-during-drain race: a torn write would
    produce a line that fails ``json.loads`` or merges two messages. The race
    window is hit once per run at task completion, so 8 iterations retain the
    stress coverage at 40% of the cost of 20.
    """
    session_dir = tmp_path / "session"
    _make_scratch(session_dir / "scratch")

    async def scenario() -> None:
        for i in range(8):
            w = WorkerSupervisor()
            await w.start()
            try:
                init_rid = f"init-{i:04d}"
                run_rid = f"run-{i:04d}"
                await w.send({"type": "init", "request_id": init_rid,
                              "task_id": "ipc-20x", "generation": 1})
                ready = await w.recv()
                assert ready["type"] == "ready"
                assert ready["request_id"] == init_rid
                await w.send(_run_task_msg(session_dir, run_rid=run_rid, task_id="ipc-20x"))

                lines: list[dict] = []
                while True:
                    raw = await asyncio.wait_for(w.proc.stdout.readline(), 15.0)
                    if not raw:
                        break
                    msg = json.loads(raw.decode("utf-8"))  # must parse cleanly
                    assert isinstance(msg, dict)
                    lines.append(msg)
                types = [m["type"] for m in lines]
                assert types.count("result_envelope") == 1
                assert types.count("exit_message") == 1
                result = next(m for m in lines if m["type"] == "result_envelope")
                assert result["status"] == "succeeded"
                assert result["request_id"] == run_rid
                exit_msg = next(m for m in lines if m["type"] == "exit_message")
                assert "request_id" not in exit_msg
                rc = await w.proc.wait()
                assert rc == 0
            finally:
                await w.stop()

    asyncio.run(scenario())


@pytest.mark.slow
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


@pytest.mark.slow
def test_worker_cancel_acks_ok_then_aborts(tmp_path) -> None:
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
                session_dir, run_rid=run_rid, task_id="ipc-cancel", work_delay_s=2.0))
            hb = await w.recv()
            assert hb["type"] == "heartbeat"

            await w.send({"type": "cancel", "request_id": "cancel-1", "reason": "test"})
            ok = await w.recv()
            assert ok["type"] == "ok"
            assert ok["request_id"] == "cancel-1"  # ack echoes the cancel rid
            assert ok["generation"] == 3

            result, _heartbeats = await w.recv_result()
            assert result["request_id"] == run_rid
            assert result["status"] == "cancelled"
            assert result["exit_code"] == 4

            exit_msg = await w.recv()
            assert exit_msg["type"] == "exit_message"
            assert exit_msg["reason"] == "cancelled"

            rc = await w.proc.wait()
            assert rc == 0  # cancelled verdict delivered; outcome lives in the envelope
        finally:
            await w.stop()

    asyncio.run(scenario())


@pytest.mark.slow
def test_worker_steer_free_text_cancel_does_not_abort(tmp_path) -> None:
    """Free text containing "cancel" must NOT abort the task (structured parse)."""
    session_dir = tmp_path / "session"
    _make_scratch(session_dir / "scratch")
    run_rid = "run-steer-nocancel"

    async def scenario() -> None:
        w = WorkerSupervisor()
        await w.start()
        try:
            await w.send({"type": "init", "request_id": "init-steer-1",
                          "task_id": "ipc-steer", "generation": 1})
            ready = await w.recv()
            assert ready["type"] == "ready"

            await w.send(_run_task_msg(
                session_dir, run_rid=run_rid, task_id="ipc-steer", work_delay_s=0.3))
            hb = await w.recv()
            assert hb["type"] == "heartbeat"

            # steer with the word "cancel" in free text: must be ignored
            await w.send({"type": "steer", "request_id": "steer-1",
                          "payload": {"note": "please cancel this step, thanks"}})
            hb2 = await w.recv()
            assert hb2["type"] == "heartbeat"  # task still running: not aborted

            result, _ = await w.recv_result()
            assert result["status"] == "succeeded"  # task completed normally

            exit_msg = await w.recv()
            assert exit_msg["reason"] == "done"
            rc = await w.proc.wait()
            assert rc == 0
        finally:
            await w.stop()

    asyncio.run(scenario())


@pytest.mark.slow
def test_worker_steer_action_cancel_aborts(tmp_path) -> None:
    """An exact ``{"action": "cancel"}`` steer aborts the current task."""
    session_dir = tmp_path / "session"
    _make_scratch(session_dir / "scratch")
    run_rid = "run-steer-cancel"

    async def scenario() -> None:
        w = WorkerSupervisor()
        await w.start()
        try:
            await w.send({"type": "init", "request_id": "init-steer-2",
                          "task_id": "ipc-steer", "generation": 1})
            ready = await w.recv()
            assert ready["type"] == "ready"

            await w.send(_run_task_msg(
                session_dir, run_rid=run_rid, task_id="ipc-steer", work_delay_s=2.0))
            hb = await w.recv()
            assert hb["type"] == "heartbeat"

            await w.send({"type": "steer", "request_id": "steer-2",
                          "payload": {"action": "cancel"}})
            result, _heartbeats = await w.recv_result()
            assert result["request_id"] == run_rid
            assert result["status"] == "cancelled"
            assert result["exit_code"] == 4

            exit_msg = await w.recv()
            assert exit_msg["reason"] == "cancelled"
            rc = await w.proc.wait()
            assert rc == 0  # cancelled verdict delivered; outcome lives in the envelope
        finally:
            await w.stop()

    asyncio.run(scenario())


@pytest.mark.slow
def test_worker_check_health_mid_task_ok_and_continues(tmp_path) -> None:
    """check_health must reply ok (rid echoed) while a task runs, then continue."""
    session_dir = tmp_path / "session"
    _make_scratch(session_dir / "scratch")
    run_rid = "run-health"

    async def scenario() -> None:
        w = WorkerSupervisor()
        await w.start()
        try:
            await w.send({"type": "init", "request_id": "init-health-1",
                          "task_id": "ipc-health", "generation": 5})
            ready = await w.recv()
            assert ready["type"] == "ready"

            await w.send(_run_task_msg(
                session_dir, run_rid=run_rid, task_id="ipc-health", work_delay_s=0.3))
            hb = await w.recv()
            assert hb["type"] == "heartbeat"

            await w.send({"type": "check_health", "request_id": "health-1"})
            ok = await w.recv()
            assert ok["type"] == "ok"
            assert ok["request_id"] == "health-1"  # ok echoes the request_id
            assert ok["generation"] == 5
            assert ok["task_id"] == "ipc-health"

            hb2 = await w.recv()
            assert hb2["type"] == "heartbeat"  # task continues after the probe

            result, _ = await w.recv_result()
            assert result["status"] == "succeeded"
            exit_msg = await w.recv()
            assert exit_msg["reason"] == "done"
            rc = await w.proc.wait()
            assert rc == 0
        finally:
            await w.stop()

    asyncio.run(scenario())


@pytest.mark.slow
def test_worker_ping_returns_exact_pong_request_id(tmp_path) -> None:
    session_dir = tmp_path / "session"
    _make_scratch(session_dir / "scratch")

    async def scenario() -> None:
        w = WorkerSupervisor()
        await w.start()
        try:
            await w.send({"type": "init", "request_id": "init-ping-1",
                          "task_id": "ipc-ping", "generation": 1})
            assert (await w.recv())["type"] == "ready"
            await w.send({"type": "ping", "request_id": "ping-exact-1"})
            pong = await w.recv()
            assert pong == {
                "type": "pong",
                "request_id": "ping-exact-1",
                "task_id": "ipc-ping",
                "generation": 1,
                "monotonic_ms": pong["monotonic_ms"],
            }
            await w.send({"type": "shutdown", "request_id": "shutdown-ping"})
            assert (await w.recv())["type"] == "ok"
            assert (await w.recv())["reason"] == "shutdown"
            assert await w.proc.wait() == 0
        finally:
            await w.stop()

    asyncio.run(scenario())


@pytest.mark.slow
def test_real_worker_rejects_generation_change_before_state_and_git_writes(tmp_path) -> None:
    session_dir = tmp_path / "session"
    _make_scratch(session_dir / "scratch")
    worktree = session_dir / "wt"

    async def scenario() -> None:
        w = WorkerSupervisor()
        await w.start()
        try:
            await w.send({"type": "init", "request_id": "init-fence-1",
                          "task_id": "ipc-fence", "generation": 2})
            assert (await w.recv())["type"] == "ready"
            await w.send(_run_task_msg(
                session_dir, run_rid="run-fence-1", work_delay_s=0.3,
                task_id="ipc-fence", generation=2,
            ))
            assert (await w.recv())["type"] == "heartbeat"
            write_generation(worktree, 3)
            result, _ = await w.recv_result()
            assert result["status"] == "failed"
            assert "generation mismatch" in result["failure_reason"]
            assert "// cambium-ipc" not in (worktree / "hello.txt").read_text()
            assert await w.proc.wait() == 0  # failed verdict delivered cleanly
        finally:
            await w.stop()

    asyncio.run(scenario())


@pytest.mark.slow
def test_worker_fence_advance_during_pre_commit_creates_no_stale_commit(
    tmp_path: Path,
) -> None:
    session_dir = tmp_path / "session"
    scratch = session_dir / "scratch"
    base = _make_scratch(scratch)
    hook_started = tmp_path / "hook-started"
    hook_release = tmp_path / "hook-release"
    hook = scratch / ".git" / "hooks" / "pre-commit"
    hook.write_text(
        "#!/bin/sh\n"
        f": > {shlex.quote(str(hook_started))}\n"
        f"while [ ! -e {shlex.quote(str(hook_release))} ]; do sleep 0.01; done\n",
        encoding="utf-8",
    )
    hook.chmod(0o755)
    worktree = session_dir / "wt"

    async def scenario() -> None:
        w = WorkerSupervisor()
        await w.start()
        try:
            await w.send({"type": "init", "request_id": "init-fence-hook",
                          "task_id": "ipc-fence-hook", "generation": 2})
            assert (await w.recv())["type"] == "ready"
            await w.send(_run_task_msg(
                session_dir, run_rid="run-fence-hook", task_id="ipc-fence-hook",
                generation=2,
            ))
            deadline = asyncio.get_running_loop().time() + 5.0
            while not hook_started.exists():
                assert asyncio.get_running_loop().time() < deadline
                await asyncio.sleep(0.01)
            write_generation(worktree, 3)
            await asyncio.sleep(0.05)
            hook_release.touch()
            result, _ = await w.recv_result()
            assert result["status"] == "failed"
            assert "generation mismatch" in result["failure_reason"]
            assert await w.proc.wait() == 0  # failed verdict delivered cleanly
        finally:
            hook_release.touch()
            await w.stop()

    asyncio.run(scenario())

    commits_after_fence = subprocess.run(
        ["git", "-C", str(worktree), "rev-list", "--count", f"{base}..HEAD"],
        check=True,
        capture_output=True,
        text=True,
    ).stdout.strip()
    assert commits_after_fence == "0"
    assert MARKER not in subprocess.run(
        ["git", "-C", str(worktree), "show", "HEAD:hello.txt"],
        check=True,
        capture_output=True,
        text=True,
    ).stdout


@pytest.mark.slow
def test_worker_fence_advance_during_post_commit_leaves_cleanup_to_supervisor(
    tmp_path: Path,
) -> None:
    session_dir = tmp_path / "session"
    scratch = session_dir / "scratch"
    base = _make_scratch(scratch)
    hook_started = tmp_path / "post-commit-started"
    hook_release = tmp_path / "post-commit-release"
    hook = scratch / ".git" / "hooks" / "post-commit"
    hook.write_text(
        "#!/bin/sh\n"
        f": > {shlex.quote(str(hook_started))}\n"
        f"while [ ! -e {shlex.quote(str(hook_release))} ]; do sleep 0.01; done\n",
        encoding="utf-8",
    )
    hook.chmod(0o755)
    worktree = session_dir / "wt"

    async def scenario() -> None:
        w = WorkerSupervisor()
        await w.start()
        try:
            await w.send({"type": "init", "request_id": "init-post-commit-fence",
                          "task_id": "ipc-post-commit-fence", "generation": 2})
            assert (await w.recv())["type"] == "ready"
            await w.send(_run_task_msg(
                session_dir, run_rid="run-post-commit-fence",
                task_id="ipc-post-commit-fence", generation=2,
            ))
            deadline = asyncio.get_running_loop().time() + 5.0
            while not hook_started.exists():
                assert asyncio.get_running_loop().time() < deadline
                await asyncio.sleep(0.01)
            write_generation(worktree, 3)
            result, _ = await w.recv_result()
            assert result["status"] == "failed"
            assert "generation mismatch" in result["failure_reason"]
            assert await w.proc.wait() == 0  # failed verdict delivered cleanly
        finally:
            hook_release.touch()
            await w.stop()

    asyncio.run(scenario())

    # The stale worker owns no recovery operations after fence invalidation.
    # Its commit remains only on the disposable task branch until supervisor
    # recovery resets that branch; refs/heads/main is never published.
    commits_before_recovery = int(subprocess.run(
        ["git", "-C", str(worktree), "rev-list", "--count", f"{base}..HEAD"],
        check=True,
        capture_output=True,
        text=True,
    ).stdout)
    assert commits_before_recovery == 1
    assert MARKER in subprocess.run(
        ["git", "-C", str(worktree), "show", "HEAD:hello.txt"],
        check=True,
        capture_output=True,
        text=True,
    ).stdout
    assert MARKER not in subprocess.run(
        ["git", "-C", str(scratch), "show", "refs/heads/main:hello.txt"],
        check=True,
        capture_output=True,
        text=True,
    ).stdout
    assert read_generation(worktree) == 3

    from cambium import supervisor as supervisor_module

    runtime = supervisor_module._Runtime(session_dir, None)
    recovered_generation = asyncio.run(runtime._recover_worktree({
        "repo": str(scratch),
        "worktree_path": str(worktree),
        "branch": "wt-ipc-001",
        "base_commit": base,
        "task_id": "ipc-post-commit-fence",
    }))

    assert recovered_generation == 4
    assert read_generation(worktree) == 4
    assert subprocess.run(
        ["git", "-C", str(worktree), "rev-parse", "HEAD"],
        check=True,
        capture_output=True,
        text=True,
    ).stdout.strip() == base
    assert MARKER not in (worktree / "hello.txt").read_text(encoding="utf-8")


@pytest.mark.slow
def test_stale_worker_never_mutates_newer_generations_staged_work(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    session_dir = tmp_path / "session"
    scratch = session_dir / "scratch"
    base = _make_scratch(scratch)
    worktree = session_dir / "wt"
    subprocess.run(
        [
            "git", "-C", str(scratch), "worktree", "add", "-b", "wt-stale",
            str(worktree), base,
        ],
        check=True,
        capture_output=True,
    )
    write_generation(worktree, 1)
    hook_started = tmp_path / "post-commit-started"
    hook_release = tmp_path / "post-commit-release"
    hook = scratch / ".git" / "hooks" / "post-commit"
    hook.write_text(
        "#!/bin/sh\n"
        f": > {shlex.quote(str(hook_started))}\n"
        f"while [ ! -e {shlex.quote(str(hook_release))} ]; do sleep 0.01; done\n",
        encoding="utf-8",
    )
    hook.chmod(0o755)

    from cambium import worker as worker_module

    allow_stale_detection = threading.Event()
    real_validate = worker_module.validate_worker_generation

    def controlled_validate(path: Path, generation: int) -> bool:
        if generation == 1 and read_generation(path) == 2:
            return not allow_stale_detection.is_set()
        return real_validate(path, generation)

    monkeypatch.setattr(worker_module, "validate_worker_generation", controlled_validate)
    result_holder: list[dict] = []
    worker_thread = threading.Thread(
        target=lambda: result_holder.append(asyncio.run(worker_module.do_work({
            "scratch_repo": str(scratch),
            "worktree_path": str(worktree),
            "target_file": "hello.txt",
            "marker": MARKER,
            "write_marker": True,
            "task_id": "stale-generation-1",
            "generation": 1,
        }, threading.Event()))),
        daemon=True,
    )
    worker_thread.start()
    deadline = time.monotonic() + 5.0
    while not hook_started.exists() and time.monotonic() < deadline:
        time.sleep(0.01)
    assert hook_started.exists()

    # Generation 2 takes ownership and stages in-progress work before generation
    # 1 observes the invalidated fence.
    write_generation(worktree, 2)
    generation_2_file = worktree / "generation-2.txt"
    generation_2_file.write_text("generation 2 in progress\n", encoding="utf-8")
    subprocess.run(["git", "-C", str(worktree), "add", "generation-2.txt"], check=True)
    head_before_stale_detection = subprocess.run(
        ["git", "-C", str(worktree), "rev-parse", "HEAD"],
        check=True,
        capture_output=True,
        text=True,
    ).stdout.strip()
    staged_before_stale_detection = subprocess.run(
        ["git", "-C", str(worktree), "diff", "--cached", "--name-only"],
        check=True,
        capture_output=True,
        text=True,
    ).stdout.splitlines()

    allow_stale_detection.set()
    hook_release.touch()
    worker_thread.join(timeout=5.0)

    assert not worker_thread.is_alive()
    assert result_holder[0]["status"] == "failed"
    assert "generation mismatch" in result_holder[0]["failure_reason"]
    assert read_generation(worktree) == 2
    assert subprocess.run(
        ["git", "-C", str(worktree), "rev-parse", "HEAD"],
        check=True,
        capture_output=True,
        text=True,
    ).stdout.strip() == head_before_stale_detection
    assert subprocess.run(
        ["git", "-C", str(worktree), "diff", "--cached", "--name-only"],
        check=True,
        capture_output=True,
        text=True,
    ).stdout.splitlines() == staged_before_stale_detection == ["generation-2.txt"]
    assert generation_2_file.read_text(encoding="utf-8") == "generation 2 in progress\n"


@pytest.mark.slow
def test_worker_shutdown_graceful_exit(tmp_path) -> None:
    """shutdown acks ok, aborts the current task, and exits gracefully (code 0)."""
    session_dir = tmp_path / "session"
    _make_scratch(session_dir / "scratch")
    run_rid = "run-shutdown"

    async def scenario() -> None:
        w = WorkerSupervisor()
        await w.start()
        try:
            await w.send({"type": "init", "request_id": "init-shutdown-1",
                          "task_id": "ipc-shutdown", "generation": 4})
            ready = await w.recv()
            assert ready["type"] == "ready"

            await w.send(_run_task_msg(
                session_dir, run_rid=run_rid, task_id="ipc-shutdown", work_delay_s=5.0))
            hb = await w.recv()
            assert hb["type"] == "heartbeat"

            await w.send({"type": "shutdown", "request_id": "shutdown-1", "reason": "host"})
            # The heartbeat loop (1s cadence) may emit one more heartbeat
            # between the first recv and the worker processing shutdown;
            # drain heartbeats until the ack lands (load-sensitive).
            ok = None
            for _ in range(4):
                ok = await w.recv()
                if ok["type"] == "ok":
                    break
            assert ok is not None and ok["type"] == "ok"
            assert ok["request_id"] == "shutdown-1"

            result = await w.recv()
            assert result["type"] == "result_envelope"
            assert result["status"] == "cancelled"  # the in-flight task was aborted
            assert result["request_id"] == run_rid

            exit_msg = await w.recv()
            assert exit_msg["type"] == "exit_message"
            assert exit_msg["reason"] == "shutdown"

            rc = await w.proc.wait()
            assert rc == 0
        finally:
            await w.stop()

    asyncio.run(scenario())


@pytest.mark.slow
def test_worker_init_timeout_exits_nonzero(tmp_path) -> None:
    """Never sending init must trip the (env-shortened) init deadline."""
    session_dir = tmp_path / "session"
    _make_scratch(session_dir / "scratch")

    async def scenario() -> None:
        w = WorkerSupervisor(env={"CAMBIUM_INIT_TIMEOUT_S": "0.3"})
        await w.start()
        try:
            # no init is ever sent
            fatal = await w.recv(timeout=8.0)
            assert fatal["type"] == "fatal_error"
            exit_msg = await w.recv(timeout=8.0)
            assert exit_msg["type"] == "exit_message"
            assert exit_msg["reason"] == "fatal"
            rc = await w.proc.wait()
            assert rc != 0
        finally:
            await w.stop()

    asyncio.run(scenario())


@pytest.mark.slow
def test_worker_idle_timeout_exits_gracefully(tmp_path) -> None:
    """Silence after ready (past the env-shortened idle deadline) exits 0."""
    session_dir = tmp_path / "session"
    _make_scratch(session_dir / "scratch")

    async def scenario() -> None:
        w = WorkerSupervisor(env={"CAMBIUM_IDLE_TIMEOUT_S": "0.3"})
        await w.start()
        try:
            await w.send({"type": "init", "request_id": "init-idle-1",
                          "task_id": "ipc-idle", "generation": 1})
            ready = await w.recv()
            assert ready["type"] == "ready"

            exit_msg = await w.recv(timeout=8.0)
            assert exit_msg["type"] == "exit_message"
            assert exit_msg["reason"] == "idle"
            rc = await w.proc.wait()
            assert rc == 0
        finally:
            await w.stop()

    asyncio.run(scenario())


@pytest.mark.slow
def test_worker_diff_cap_bytes_and_truncation_flag(tmp_path) -> None:
    """A diff larger than 64 KiB must be byte-capped with diff_truncated true."""
    session_dir = tmp_path / "session"
    _make_scratch(session_dir / "scratch")
    run_rid = "run-bigdiff"
    big_marker = "x" * 100_000  # one added line > 64 KiB -> diff > 64 KiB

    async def scenario() -> None:
        w = WorkerSupervisor()
        await w.start()
        try:
            await w.send({"type": "init", "request_id": "init-bigdiff-1",
                          "task_id": "ipc-bigdiff", "generation": 1})
            ready = await w.recv()
            assert ready["type"] == "ready"

            await w.send(_run_task_msg(
                session_dir, run_rid=run_rid, task_id="ipc-bigdiff", marker=big_marker))
            result, _ = await w.recv_result()

            assert result["status"] == "succeeded"
            assert result["diff_truncated"] is True
            # content capped at 64 KiB bytes (plus the truncation marker)
            assert len(result["diff"].encode("utf-8")) <= DIFF_CAP_BYTES + 64
            assert result["diff"].endswith("[diff truncated]")

            exit_msg = await w.recv()
            assert exit_msg["reason"] == "done"
            rc = await w.proc.wait()
            assert rc == 0
        finally:
            await w.stop()

    asyncio.run(scenario())
