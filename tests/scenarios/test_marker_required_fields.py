"""Scenario: a minimal flat plan (five required fields only) fails cleanly.

The supervisor's ``_run_payload`` forwards ``target_file`` and ``marker`` as
``None`` when a deterministic marker task omits them. ``_do_work_marker`` must
reject that with a task-level error instead of crashing on
``worktree / None`` (``TypeError: unsupported operand type(s) for /:
'PosixPath' and 'NoneType'``). This drives the real ``cambium.worker`` over
stdio with the exact payload shape ``_run_payload`` produces for a spec that
has only task_id / task / repo / worktree_path / branch.
"""

from __future__ import annotations

import asyncio
import json
import os
import sys
from pathlib import Path

from cambium.ipc import MAX_LINE_BYTES, read_message

MARKER_ERROR = "marker task requires target_file and marker"


async def _drive_worker(session_dir: Path) -> dict:
    proc = await asyncio.create_subprocess_exec(
        sys.executable, "-u", "-m", "cambium.worker",
        stdin=asyncio.subprocess.PIPE,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
        env={**os.environ, "PYTHONUNBUFFERED": "1"},
        start_new_session=True,
    )

    async def _drain_stderr() -> list[str]:
        lines: list[str] = []
        while True:
            raw = await proc.stderr.readline()
            if not raw:
                break
            lines.append(raw.decode("utf-8", "replace").rstrip())
        return lines

    stderr_task = asyncio.create_task(_drain_stderr())

    assert proc.stdin is not None, (
        f"worker stdin missing: returncode={proc.returncode!r}"
    )
    proc.stdin.write((json.dumps({
        "type": "init", "request_id": "init-minimal-001", "task_id": "minimal-001",
        "generation": 1, "proto": 1,
    }) + "\n").encode("utf-8"))
    await proc.stdin.drain()
    ready = await read_message(proc.stdout, limit=MAX_LINE_BYTES)
    assert ready is not None and ready["type"] == "ready"

    payload = {
        "type": "run_task", "request_id": "run-minimal-001",
        "task_id": "minimal-001",
        "task": "do the minimal thing",
        "repo": str(session_dir / "scratch"),
        "scratch_repo": str(session_dir / "scratch"),
        "worktree_path": str(session_dir / "wt"),
        "branch": "wt-minimal-001",
        "gate": "",
        "base_commit": None,
        "generation": 1,
        "max_turns": 20,
        "max_tokens": 200_000,
        "max_wall_s": 300.0,
        "target_file": None,
        "marker": None,
        "write_marker": True,
    }
    proc.stdin.write((json.dumps(payload) + "\n").encode("utf-8"))
    await proc.stdin.drain()

    envelope = None
    while True:
        msg = await read_message(proc.stdout, limit=MAX_LINE_BYTES)
        assert msg is not None, "EOF before result_envelope"
        if msg["type"] == "result_envelope":
            envelope = msg
            break
    exit_msg = await read_message(proc.stdout, limit=MAX_LINE_BYTES)
    returncode = await proc.wait()
    stderr = await stderr_task
    return {"envelope": envelope, "exit_msg": exit_msg, "returncode": returncode,
            "stderr": stderr}


def test_minimal_plan_missing_target_file_fails_cleanly(tmp_path) -> None:
    outcome = asyncio.run(_drive_worker(tmp_path / "session"))

    envelope = outcome["envelope"]
    assert envelope is not None
    assert envelope["status"] == "failed"
    assert envelope["exit_code"] == 1
    assert envelope["failure_reason"] == MARKER_ERROR
    assert "TypeError" not in envelope["failure_reason"]
    assert "task crashed" not in envelope["failure_reason"]

    assert outcome["exit_msg"]["type"] == "exit_message"
    assert outcome["exit_msg"]["reason"] == "failed"
    assert outcome["returncode"] == 1  # clean failed verdict, not a process crash
    assert outcome["stderr"] == [], f"worker stderr must be clean: {outcome['stderr']!r}"
