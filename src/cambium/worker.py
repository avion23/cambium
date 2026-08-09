"""Worker runtime (Opifex seed) — ``python -m cambium.worker``.

Speaks the Nuntius JSON-Lines wire protocol over stdio
(docs/architecture.md §5, docs/research/ipc-protocol-draft.md). One worker
executes one task and then exits:

    init                       ->  ready (echoes the init request_id and the
                                   generation fencing token)
    run_task                   ->  heartbeat(s) every ~1s while working
                                ->  result_envelope (echoes the run_task
                                    request_id) -> exit_message (connection
                                    level; carries NO request_id)
    steer / cancel             ->  cooperatively abort the current task with
                                   status "cancelled"

Task spec (the ``run_task`` body) is compatible with
``scripts/fake_worker.py``'s task spec:

    task_id         stable task id (echoed everywhere)
    scratch_repo    git repo the throwaway worktree is branched from
    worktree_path   where the throwaway worktree is created (must stay under
                    the scratch repo's parent — path safety)
    branch          name of the throwaway branch
    target_file     file inside the worktree to edit (must not escape it)
    marker          line appended to the target file
    write_marker    bool; false forces the task to fail
    work_delay_s    optional float; pause before the edit (test hook so
                    cancellation is observable)

Malformed wire input is fatal: the worker emits ``fatal_error``, then
``exit_message`` (reason "fatal"), and exits nonzero (let-it-crash). The
process exit code is 0 only when the task status is "succeeded".
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import subprocess
import sys
import threading
import time
from pathlib import Path
from typing import Any

from cambium.ipc import MAX_LINE_BYTES, MessageTooLong, read_message, write_message

PROTO = 1
HEARTBEAT_INTERVAL_S = 1.0
MAX_SUMMARY_CHARS = 2_000
MAX_DIFF_BYTES = 64 * 1024  # 64 KiB diff cap (ipc-protocol-draft.md §3)
EXIT_CODES = {"succeeded": 0, "failed": 1, "cancelled": 4}

logger = logging.getLogger(__name__)


def _monotonic_ms() -> int:
    return time.monotonic_ns() // 1_000_000


async def send(writer: asyncio.StreamWriter, msg: dict[str, Any]) -> None:
    write_message(writer, msg)
    await writer.drain()


def git(*args: str, cwd: str | Path | None = None) -> tuple[int, str, str]:
    proc = subprocess.run(["git", *args], cwd=cwd, capture_output=True, text=True)
    return proc.returncode, proc.stdout.strip(), proc.stderr.strip()


def do_work(run: dict[str, Any], stop: threading.Event) -> dict[str, Any]:
    """Execute one task: throwaway worktree, one-file edit, commit.

    Returns the outcome dict:

        status          "succeeded" | "failed" | "cancelled"
        failure_reason  str | None (set when status != "succeeded")
        commits         list[str] of SHAs produced
        files_changed   list[str] of paths changed
        diff            ``git diff <base_commit>..HEAD`` in the worktree,
                        capped at ``MAX_DIFF_BYTES``
        summary         worker-authored, <= ``MAX_SUMMARY_CHARS``

    Cooperative cancellation via ``stop``: the worker checks it between git
    steps and reports status "cancelled" if it was set.
    """
    outcome: dict[str, Any] = {
        "status": "failed",
        "failure_reason": None,
        "commits": [],
        "files_changed": [],
        "diff": "",
        "summary": "",
    }
    try:
        scratch = Path(run["scratch_repo"]).resolve()
        worktree = Path(run["worktree_path"]).resolve()
        branch = run["branch"]
        target_file = run["target_file"]
        marker = run["marker"]
        write_marker = bool(run.get("write_marker", True))

        session_root = scratch.parent
        if not worktree.is_relative_to(session_root):
            outcome["failure_reason"] = (
                f"worktree_path {worktree} outside session scratch root {session_root}")
            return outcome
        target = (worktree / target_file).resolve()
        if not target.is_relative_to(worktree):
            outcome["failure_reason"] = f"target_file {target_file!r} escapes the worktree"
            return outcome

        rc, _out, err = git("rev-parse", "main", cwd=scratch)
        if rc != 0:
            outcome["failure_reason"] = f"no main branch in scratch repo: {err}"
            return outcome
        base_commit = _out

        if worktree.exists():
            git("worktree", "remove", "--force", str(worktree), cwd=scratch)
        git("branch", "-D", branch, cwd=scratch)
        rc, _out, err = git("worktree", "add", "-b", branch, str(worktree), "main", cwd=scratch)
        if rc != 0:
            outcome["failure_reason"] = f"worktree add failed: {err}"
            return outcome

        # Optional work_delay_s pauses before the edit (testing hook); the
        # pause polls ``stop`` so cancellation stays responsive.
        delay = float(run.get("work_delay_s", 0.0) or 0.0)
        deadline = time.monotonic() + delay
        while time.monotonic() < deadline:
            if stop.is_set():
                outcome["status"] = "cancelled"
                return outcome
            time.sleep(min(0.05, deadline - time.monotonic()))

        if stop.is_set():
            outcome["status"] = "cancelled"
            return outcome
        if not write_marker:
            outcome["failure_reason"] = "marker not written (write_marker=false)"
            return outcome
        if not target.exists():
            outcome["failure_reason"] = f"target file missing: {target_file}"
            return outcome
        target.write_text(target.read_text().rstrip("\n") + "\n" + marker + "\n")
        if marker not in target.read_text():
            outcome["failure_reason"] = "edit missing: marker not present after write"
            return outcome
        if stop.is_set():
            outcome["status"] = "cancelled"
            return outcome

        git("add", target_file, cwd=worktree)
        rc, _out, err = git("commit", "-m", f"cambium-ipc: {run['task_id']}", cwd=worktree)
        if rc != 0:
            outcome["failure_reason"] = f"commit failed: {err}"
            return outcome
        _rc, sha, _err = git("rev-parse", "HEAD", cwd=worktree)
        _rc, diff, _err = git("diff", f"{base_commit}..HEAD", cwd=worktree)
        outcome.update(
            status="succeeded",
            failure_reason=None,
            commits=[sha],
            files_changed=[target_file],
            diff=diff[:MAX_DIFF_BYTES],
            summary=f"appended marker to {target_file}"[:MAX_SUMMARY_CHARS],
        )
        return outcome
    except Exception as exc:  # let-it-crash: report as a failure, not a hang
        outcome["failure_reason"] = f"task crashed: {exc}"
        return outcome


async def _heartbeat_loop(
    writer: asyncio.StreamWriter,
    task_id: str,
    generation: int,
    stop: threading.Event,
) -> None:
    turn = 0
    while not stop.is_set():
        await send(writer, {
            "type": "heartbeat",
            "task_id": task_id,
            "generation": generation,
            "turn": turn,
            "tool": None,
            "status": "working",
            "monotonic_ms": _monotonic_ms(),
        })
        turn += 1
        await asyncio.sleep(HEARTBEAT_INTERVAL_S)


async def _run_task(
    writer: asyncio.StreamWriter,
    run: dict[str, Any],
    task_id: str,
    generation: int,
    stop: threading.Event,
) -> dict[str, Any]:
    """Run the task body with heartbeats; returns the terminal outcome."""
    started_at = time.time()
    run_rid = run["request_id"]

    hb = asyncio.create_task(_heartbeat_loop(writer, task_id, generation, stop))
    try:
        outcome = await asyncio.to_thread(do_work, run, stop)
    finally:
        stop.set()
        hb.cancel()
        try:
            await hb
        except asyncio.CancelledError:
            pass
    outcome["request_id"] = run_rid
    outcome["task_id"] = task_id
    outcome["generation"] = generation
    outcome["started_at"] = started_at
    outcome["ended_at"] = time.time()
    return outcome


async def _emit_result(writer: asyncio.StreamWriter, outcome: dict[str, Any]) -> None:
    status = outcome["status"]
    envelope = {
        "type": "result_envelope",
        "request_id": outcome["request_id"],
        "task_id": outcome["task_id"],
        "generation": outcome["generation"],
        "status": status,
        "exit_code": EXIT_CODES.get(status, 1),
        "commits": outcome.get("commits", []),
        "files_changed": outcome.get("files_changed", []),
        "diff": outcome.get("diff", ""),
        "summary": (outcome.get("summary") or "")[:MAX_SUMMARY_CHARS],
        "failure_reason": outcome.get("failure_reason"),
        "started_at": outcome.get("started_at"),
        "ended_at": outcome.get("ended_at"),
    }
    await send(writer, envelope)
    reason = {"succeeded": "done", "failed": "failed", "cancelled": "cancelled"}.get(
        status, "failed")
    await send(writer, {
        "type": "exit_message",
        "task_id": outcome["task_id"],
        "generation": outcome["generation"],
        "reason": reason,
        "monotonic_ms": _monotonic_ms(),
    })


async def _fatal(writer: asyncio.StreamWriter, msg: Any, message: str) -> int:
    context = msg if isinstance(msg, dict) else {}
    await send(writer, {
        "type": "fatal_error",
        "request_id": context.get("request_id"),
        "task_id": context.get("task_id"),
        "generation": context.get("generation"),
        "error_type": "invalid_message",
        "message": message[:500],
        "recoverable": False,
    })
    await send(writer, {
        "type": "exit_message",
        "task_id": context.get("task_id"),
        "generation": context.get("generation"),
        "reason": "fatal",
        "monotonic_ms": _monotonic_ms(),
    })
    return 1


async def run(reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> int:
    """The worker wire loop. Returns the process exit code."""
    try:
        first = await read_message(reader)
    except MessageTooLong:
        return await _fatal(writer, {}, "wire line exceeded the length cap")
    if first is None:
        return 1
    if not isinstance(first, dict) or first.get("type") != "init" or "request_id" not in first:
        return await _fatal(writer, first, "expected init as the first message")

    init_rid = first["request_id"]
    task_id = first.get("task_id", "unknown")
    generation = first.get("generation", 1)
    await send(writer, {
        "type": "ready",
        "request_id": init_rid,
        "task_id": task_id,
        "pid": os.getpid(),
        "generation": generation,
        "proto": first.get("proto", PROTO),
        "monotonic_ms": _monotonic_ms(),
    })

    current: asyncio.Task[dict[str, Any]] | None = None
    stop = threading.Event()

    while True:
        read_task = asyncio.create_task(read_message(reader))
        pending = {read_task}
        if current is not None:
            pending.add(current)
        done, _ = await asyncio.wait(pending, return_when=asyncio.FIRST_COMPLETED)

        if current is not None and current in done:
            task = current
            current = None
            read_task.cancel()
            try:
                await read_task
            except BaseException:
                pass
            try:
                outcome = task.result()
            except Exception as exc:
                return await _fatal(writer, {}, f"task crashed: {exc}")
            await _emit_result(writer, outcome)
            return EXIT_CODES.get(outcome["status"], 1)

        try:
            msg = read_task.result()
        except MessageTooLong:
            return await _fatal(writer, {}, "wire line exceeded the length cap")
        except Exception as exc:
            return await _fatal(writer, {}, f"wire read failed: {exc}")

        if msg is None:
            # stdin closed: no further requests can arrive.
            await send(writer, {
                "type": "exit_message",
                "task_id": task_id,
                "generation": generation,
                "reason": "crash",
                "monotonic_ms": _monotonic_ms(),
            })
            return 1

        mtype = msg.get("type") if isinstance(msg, dict) else None
        if mtype == "run_task":
            if current is not None:
                return await _fatal(writer, msg, "run_task while a task is already running")
            if "request_id" not in msg:
                return await _fatal(writer, msg, "run_task without a request_id")
            stop = threading.Event()
            current = asyncio.create_task(
                _run_task(writer, msg, task_id, generation, stop))
        elif mtype == "steer":
            payload = msg.get("payload") or {}
            if "cancel" in json.dumps(payload):
                logger.info("steer: cancel requested")
                stop.set()
            else:
                logger.info("steer (v2.1 hook; continuing): %s",
                            json.dumps(payload)[:200])
        elif mtype == "cancel":
            logger.info("cancel: aborting current task")
            stop.set()
        else:
            return await _fatal(writer, msg, f"unknown message type {mtype!r}")


class _WriterProtocol(asyncio.streams.FlowControlMixin):
    """Flow-control protocol for the stdout write transport.

    ``StreamWriter.wait_closed`` resolves the protocol's ``_closed`` future,
    so it must be tied to THIS transport's ``connection_lost`` — not the
    reader's (whose stdin pipe stays open for the worker's lifetime).
    """

    def __init__(self) -> None:
        super().__init__()
        self._closed = asyncio.get_running_loop().create_future()

    def connection_lost(self, exc: BaseException | None) -> None:
        if not self._closed.done():
            self._closed.set_result(exc)
        super().connection_lost(exc)

    def _get_close_waiter(self, stream: asyncio.StreamWriter) -> asyncio.Future:
        return self._closed


async def _open_stdio() -> tuple[asyncio.StreamReader, asyncio.StreamWriter]:
    """Wrap stdin/stdout in asyncio streams (protocol stream = stdout)."""
    loop = asyncio.get_running_loop()
    reader = asyncio.StreamReader(limit=MAX_LINE_BYTES)
    read_protocol = asyncio.StreamReaderProtocol(reader)
    await loop.connect_read_pipe(lambda: read_protocol, sys.stdin.buffer)
    write_protocol = _WriterProtocol()
    transport, _ = await loop.connect_write_pipe(
        lambda: write_protocol, sys.stdout.buffer)
    writer = asyncio.StreamWriter(transport, write_protocol, reader, loop)
    return reader, writer


async def _amain() -> int:
    reader, writer = await _open_stdio()
    try:
        return await run(reader, writer)
    finally:
        writer.close()
        await writer.wait_closed()


def main() -> int:
    logging.basicConfig(
        stream=sys.stderr,
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )
    try:
        return asyncio.run(_amain())
    except KeyboardInterrupt:
        return 1


if __name__ == "__main__":
    sys.exit(main())
