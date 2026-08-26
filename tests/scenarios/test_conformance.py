"""Conformance pins for the normative Cambium architecture contracts.

These checks intentionally use the shipped modules, SQLite schema, git, and a
real worker process.  The supervisor environment checks are the one exception
to runtime exercise: they parse the module with :mod:`ast` so every direct
``create_subprocess_exec`` and ``subprocess.run`` call is checked without
starting a full supervisor.
"""

from __future__ import annotations

import asyncio
import json
import os
import sqlite3
import subprocess
import sys
from datetime import UTC, datetime
from pathlib import Path

import pytest

from cambium.fencing import write_generation
from cambium.ipc import read_message
from cambium.merge import ZERO_SHA, MergeSequencer, NonFastForwardError
from cambium.store import EventStore

EXPECTED_EVENT_COLUMNS = {
    "seq": "INTEGER",
    "kind": "TEXT",
    "payload": "TEXT",
    "ts": "TEXT",
    "monotonic_ms": "INTEGER",
    "task_id": "TEXT",
    "worker_id": "TEXT",
    "generation": "INTEGER",
    "request_id": "TEXT",
}


def _git(cwd: Path, *args: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(["git", *args], cwd=cwd, check=True, capture_output=True, text=True)


def _init_repo(repo: Path) -> str:
    _git(repo.parent, "init", "-b", "main", str(repo))
    _git(repo, "config", "user.name", "cambium-conformance")
    _git(repo, "config", "user.email", "conformance@example.invalid")
    _git(repo, "config", "gc.auto", "0")
    (repo / "base.txt").write_text("base\n")
    _git(repo, "add", "base.txt")
    _git(repo, "commit", "-m", "initial")
    return _git(repo, "rev-parse", "HEAD").stdout.strip()


def _worker_commit(repo: Path, base: str, branch: str, worktree: Path, name: str) -> str:
    _git(repo, "worktree", "add", "-b", branch, str(worktree), base)
    (worktree / name).write_text(f"{name}\n")
    _git(worktree, "add", name)
    _git(worktree, "commit", "-m", branch)
    return _git(worktree, "rev-parse", "HEAD").stdout.strip()


@pytest.mark.slow
def test_real_worker_result_correlates_and_exit_has_no_request_id(tmp_path: Path) -> None:
    session_dir = tmp_path / "session"
    scratch = session_dir / "scratch"
    scratch.mkdir(parents=True)
    _init_repo(scratch)
    worktree = session_dir / "worker-wt"
    _git(scratch, "worktree", "add", "-b", "wt-conformance-worker", str(worktree), "main")
    write_generation(worktree, 1)
    task_id = "conformance-worker"
    init_request_id = "conformance-init"
    run_request_id = "conformance-run"

    async def scenario() -> None:
        proc = await asyncio.create_subprocess_exec(
            sys.executable,
            "-u",
            "-m",
            "cambium.worker",
            stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            env={**os.environ, "PYTHONUNBUFFERED": "1"},
            start_new_session=True,
        )
        try:
            assert proc.stdin is not None
            assert proc.stdout is not None

            proc.stdin.write(
                (
                    f'{{"type":"init","request_id":"{init_request_id}",'
                    f'"task_id":"{task_id}","generation":1,"proto":1}}\n'
                ).encode()
            )
            await proc.stdin.drain()
            ready = await asyncio.wait_for(read_message(proc.stdout), 15.0)
            assert ready is not None
            assert ready["type"] == "ready"
            assert ready["request_id"] == init_request_id

            run_message = {
                "type": "run_task",
                "request_id": run_request_id,
                "task_id": task_id,
                "scratch_repo": str(scratch),
                "worktree_path": str(worktree),
                "branch": "wt-conformance-worker",
                "target_file": "base.txt",
                "marker": "conformance marker",
                "write_marker": True,
            }
            proc.stdin.write((json.dumps(run_message) + "\n").encode())
            await proc.stdin.drain()

            result: dict | None = None
            exit_message: dict | None = None
            while result is None or exit_message is None:
                message = await asyncio.wait_for(read_message(proc.stdout), 15.0)
                assert message is not None
                if message.get("type") == "result_envelope":
                    result = message
                elif message.get("type") == "exit_message":
                    exit_message = message

            assert result["request_id"] == run_request_id
            assert "request_id" not in exit_message
            assert await proc.wait() == 0
        finally:
            if proc.returncode is None:
                proc.kill()
                await proc.wait()

    asyncio.run(scenario())


def test_event_store_ddl_matches_architecture_and_keeps_iso_ts_text(tmp_path: Path) -> None:
    path = tmp_path / "events.db"
    store = EventStore(path, fsync_interval_s=0.01)
    try:
        iso_ts = datetime.now(UTC).isoformat()
        store.append({"kind": "result", "payload": {}, "ts": iso_ts})
    finally:
        store.close()

    with sqlite3.connect(path) as connection:
        rows = connection.execute("PRAGMA table_info(events)").fetchall()
        columns = {row[1]: row[2].upper() for row in rows}
        assert columns == EXPECTED_EVENT_COLUMNS
        assert connection.execute("SELECT ts FROM events").fetchone()[0] == iso_ts


@pytest.mark.slow
def test_merge_rejects_invalid_old_values_non_fast_forward_and_quarantine(tmp_path):
    repo = tmp_path / "repo"
    base = _init_repo(repo)
    tip_a = _worker_commit(repo, base, "wt-conformance-a", tmp_path / "wt-a", "a.txt")
    tip_b = _worker_commit(repo, base, "wt-conformance-b", tmp_path / "wt-b", "b.txt")
    sequencer = MergeSequencer(task_id="conformance-merge")

    for invalid_old in (None, "", ZERO_SHA):
        with pytest.raises(NonFastForwardError):
            sequencer.publish_merge(repo, tip_a, invalid_old)
        assert _git(repo, "rev-parse", "refs/heads/main").stdout.strip() == base

    sequencer.publish_merge(repo, tip_a, base)
    with pytest.raises(NonFastForwardError):
        sequencer.publish_merge(repo, tip_b, tip_a)
    assert _git(repo, "rev-parse", "refs/heads/main").stdout.strip() == tip_a

    quarantine_key = "GIT_QUARANTINE_PATH"
    previous_quarantine = os.environ.get(quarantine_key)
    os.environ[quarantine_key] = str(tmp_path / "quarantine")
    try:
        assert quarantine_key not in MergeSequencer._git_env()
        assert quarantine_key not in MergeSequencer._rebase_env()
    finally:
        if previous_quarantine is None:
            os.environ.pop(quarantine_key, None)
        else:
            os.environ[quarantine_key] = previous_quarantine


def test_worker_env_drops_api_key_names_and_controls_path(tmp_path: Path) -> None:
    redact = pytest.importorskip("cambium.redact")
    env = redact.build_worker_env(
        {
            "TEST_API_KEY_DEMO": "not-for-workers",
            "PATH": "/host/bin",
            "HOME": "/home/host",
        },
        worktree=tmp_path / "worker",
    )
    assert "TEST_API_KEY_DEMO" not in env
    assert env["PATH"] == os.defpath
    assert "HOME" not in env
    assert "/host/bin" not in env.values()
    assert "/home/host" not in env.values()
