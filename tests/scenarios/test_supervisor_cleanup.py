"""Soak coverage for supervisor cancellation, deadlines, and hard crashes.

These scenarios deliberately use real worker processes and real Git worktrees.
The assertions are about the two resources a failed turn must not leave behind:
the worker process (including its process group) and the session-owned worktree.
"""

from __future__ import annotations

import asyncio
import json
import os
import re
import signal
import sqlite3
import subprocess
import sys
import time
from pathlib import Path

import pytest

from cambium.supervisor import read_events, run_plan

pytestmark = [
    pytest.mark.slow,
    pytest.mark.skipif(os.name != "posix", reason="process-group cleanup is POSIX-specific"),
]

TEST_RESOURCE_THRESHOLDS = {
    "mem_available_frac": 0.0,
    "load1_per_cpu": 1_000_000.0,
    "disk_free": 0,
}


def _make_repo(repo: Path) -> str:
    subprocess.run(["git", "init", "-b", "main", str(repo)], check=True, capture_output=True)
    for key, value in (("user.name", "cleanup-test"), ("user.email", "cleanup@test")):
        subprocess.run(["git", "-C", str(repo), "config", key, value], check=True)
    subprocess.run(["git", "-C", str(repo), "config", "gc.auto", "0"], check=True)
    (repo / "hello.txt").write_text("hello\n", encoding="utf-8")
    subprocess.run(["git", "-C", str(repo), "add", "hello.txt"], check=True)
    subprocess.run(
        ["git", "-C", str(repo), "commit", "-m", "initial"],
        check=True,
        capture_output=True,
    )
    return subprocess.run(
        ["git", "-C", str(repo), "rev-parse", "HEAD"],
        check=True,
        capture_output=True,
        text=True,
    ).stdout.strip()


def _worktree_paths(repo: Path) -> list[Path]:
    output = subprocess.run(
        ["git", "-C", str(repo), "worktree", "list", "--porcelain"],
        check=True,
        capture_output=True,
        text=True,
    ).stdout
    return [
        Path(line.removeprefix("worktree ")).resolve()
        for line in output.splitlines()
        if line.startswith("worktree ")
    ]


def _branch_exists(repo: Path, branch: str) -> bool:
    return (
        subprocess.run(
            ["git", "-C", str(repo), "show-ref", "--verify", "--quiet", f"refs/heads/{branch}"],
            check=False,
        ).returncode
        == 0
    )


def _hanging_worker(path: Path, *, state_path: Path | None = None) -> Path:
    state_literal = repr(str(state_path)) if state_path is not None else "None"
    path.write_text(
        "import json, os, sys, time\n"
        "from pathlib import Path\n"
        f"state = Path({state_literal}) if {state_literal} is not None else None\n"
        "init = json.loads(sys.stdin.readline())\n"
        "if state is not None and state.exists():\n"
        "    sys.stdout.write(json.dumps({'type': 'ready', 'request_id': init['request_id'], "
        "'task_id': init['task_id'], 'generation': init.get('generation', 1), "
        "'pid': os.getpid(), 'proto': 1}) + '\\n')\n"
        "    sys.stdout.flush()\n"
        "    run = json.loads(sys.stdin.readline())\n"
        "    sys.stdout.write(json.dumps({'type': 'result_envelope', "
        "'request_id': run['request_id'], 'task_id': init['task_id'], "
        "'generation': init.get('generation', 1), 'status': 'succeeded'}) + '\\n')\n"
        "    sys.stdout.write(json.dumps({'type': 'exit_message', 'task_id': init['task_id'], "
        "'generation': init.get('generation', 1), 'reason': 'done'}) + '\\n')\n"
        "    sys.stdout.flush()\n"
        "    raise SystemExit(0)\n"
        "if state is not None:\n"
        "    state.write_text(str(os.getpid()), encoding='ascii')\n"
        "sys.stdout.write(json.dumps({'type': 'ready', 'request_id': init['request_id'], "
        "'task_id': init['task_id'], 'generation': init.get('generation', 1), "
        "'pid': os.getpid(), 'proto': 1}) + '\\n')\n"
        "sys.stdout.flush()\n"
        "time.sleep(3600)\n",
        encoding="utf-8",
    )
    return path


def _task(
    session_dir: Path,
    repo: Path,
    base: str,
    task_id: str,
    worker: Path,
    *,
    max_wall_s: float,
    max_restarts: int = 0,
) -> dict[str, object]:
    return {
        "task_id": task_id,
        "task": "wait for cleanup",
        "repo": str(repo),
        "worktree_path": str(session_dir / f"wt-{task_id}"),
        "branch": f"wt-{task_id}",
        "base_commit": base,
        "worker": str(worker),
        "provider_env_keys": [],
        "resource_thresholds": TEST_RESOURCE_THRESHOLDS,
        "ready_timeout_s": 30.0,
        "heartbeat_timeout_s": 30.0,
        "max_wall_s": max_wall_s,
        "max_restarts": max_restarts,
    }


def _worker_is_alive(pid: int, worker: Path) -> bool:
    proc_dir = Path("/proc") / str(pid)
    if not proc_dir.exists():
        return False
    try:
        state = (proc_dir / "stat").read_text(encoding="ascii").split()[2]
        command = (proc_dir / "cmdline").read_bytes().replace(b"\0", b" ").decode(errors="replace")
    except (FileNotFoundError, OSError, UnicodeError, IndexError):
        return False
    return state != "Z" and str(worker) in command


def _wait_worker_gone(pid: int, worker: Path) -> None:
    deadline = time.monotonic() + 5.0
    while time.monotonic() < deadline:
        if not _worker_is_alive(pid, worker):
            return
        time.sleep(0.02)
    assert not _worker_is_alive(pid, worker), f"worker {pid} survived cleanup"


def _assert_no_task_worktree(session_dir: Path, repo: Path, branch: str, worktree: Path) -> None:
    assert not worktree.exists()
    assert _worktree_paths(repo) == [repo.resolve()]
    assert not _branch_exists(repo, branch)
    assert not [path for path in session_dir.glob("wt-*") if path.is_dir()]


@pytest.mark.parametrize("mode", ["cancelled", "wall"])
def test_terminal_turn_prunes_worktree_and_reaps_worker(tmp_path: Path, mode: str) -> None:
    session_dir = tmp_path / mode
    repo = session_dir / "repo"
    base = _make_repo(repo)
    worker = _hanging_worker(tmp_path / f"{mode}-worker.py")
    task_id = f"t-{mode}"
    task_spec = _task(
        session_dir,
        repo,
        base,
        task_id,
        worker,
        max_wall_s=5.0 if mode == "wall" else 30.0,
    )
    worktree = Path(task_spec["worktree_path"])
    ready = asyncio.Event()
    worker_pid: int | None = None

    async def observe(event: dict[str, object]) -> None:
        nonlocal worker_pid
        if event["kind"] == "ready":
            payload = event["payload"]
            assert isinstance(payload, dict)
            worker_pid = int(payload["pid"])
            ready.set()

    async def run_and_cancel() -> None:
        run = asyncio.create_task(run_plan(session_dir, {"tasks": [task_spec]}, observe))
        await asyncio.wait_for(ready.wait(), timeout=30.0)
        assert worker_pid is not None
        if mode == "cancelled":
            run.cancel()
            with pytest.raises(asyncio.CancelledError):
                await run
        else:
            result = await asyncio.wait_for(run, timeout=45.0)
            reason = result.results[0].reason or ""
            assert reason.startswith("max_restarts (0): wall (timeout: wall (elapsed=")
            assert re.search(r"elapsed=\d+(?:\.\d+)?s", reason)
            assert "budget=5s, restarts=0))" in reason

    asyncio.run(run_and_cancel())
    assert worker_pid is not None
    _wait_worker_gone(worker_pid, worker)
    _assert_no_task_worktree(session_dir, repo, str(task_spec["branch"]), worktree)

    events = read_events(session_dir)
    assert any(event["kind"] == "worktree_pruned" for event in events)
    if mode == "wall":
        assert any(
            event["kind"] == "timeout" and event["payload"].get("phase") == "wall"
            for event in events
        )


def test_hard_supervisor_crash_is_reclaimed_on_session_resume(tmp_path: Path) -> None:
    session_dir = tmp_path / "crash"
    repo = session_dir / "repo"
    base = _make_repo(repo)
    state = tmp_path / "crash-worker.pid"
    worker = _hanging_worker(tmp_path / "crash-worker.py", state_path=state)
    task_spec = _task(session_dir, repo, base, "t-crash", worker, max_wall_s=30.0)
    plan_path = session_dir / "plan.json"
    session_dir.mkdir(exist_ok=True)
    plan_path.write_text(json.dumps({"tasks": [task_spec]}), encoding="utf-8")
    environment = dict(os.environ)
    environment["PYTHONPATH"] = os.pathsep.join(
        filter(
            None,
            [str(Path(__file__).resolve().parents[2] / "src"), environment.get("PYTHONPATH")],
        )
    )
    command = [
        sys.executable,
        "-m",
        "cambium.supervisor",
        "--session-dir",
        str(session_dir),
        "--plan",
        str(plan_path),
    ]
    first = subprocess.Popen(
        command,
        cwd=Path(__file__).resolve().parents[2],
        env=environment,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )
    try:
        deadline = time.monotonic() + 45.0
        while time.monotonic() < deadline and not state.exists():
            time.sleep(0.02)
        assert state.exists(), "crash worker never started"
        worker_pid = int(state.read_text(encoding="ascii"))
        deadline = time.monotonic() + 45.0
        while time.monotonic() < deadline:
            events_db = session_dir / ".cambium" / "events.db"
            if events_db.exists():
                with sqlite3.connect(events_db) as connection:
                    spawned = connection.execute(
                        "SELECT COUNT(*) FROM events WHERE kind = 'spawned'"
                    ).fetchone()[0]
                if spawned:
                    break
            time.sleep(0.02)
        assert first.poll() is None
        os.kill(first.pid, signal.SIGKILL)
        assert first.wait(timeout=10.0) == -signal.SIGKILL
        assert (session_dir / "wt-t-crash").exists()

        resumed = subprocess.run(
            command,
            cwd=Path(__file__).resolve().parents[2],
            env=environment,
            capture_output=True,
            text=True,
            timeout=60.0,
            check=False,
        )
        assert resumed.returncode == 0, resumed.stdout + resumed.stderr
        _wait_worker_gone(worker_pid, worker)
        _assert_no_task_worktree(
            session_dir,
            repo,
            str(task_spec["branch"]),
            Path(task_spec["worktree_path"]),
        )
        events = read_events(session_dir)
        terminated = [event for event in events if event["kind"] == "worker_terminated"]
        assert len(terminated) == 1
        assert terminated[0]["payload"]["status"] == "terminated"
        assert terminated[0]["payload"]["reason"] == "orphaned_supervisor"
        assert sum(event["kind"] == "worktree_pruned" for event in events) >= 2
        assert events[-1]["kind"] == "session_ended"
    finally:
        if first.poll() is None:
            first.kill()
            first.wait(timeout=10.0)
