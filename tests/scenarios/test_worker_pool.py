"""Eval-3 ADOPT: warm worker-reuse pool scenarios.

The supervisor keeps a bounded session-scoped pool of idle reuse-ready worker
processes and rebinds them to new worktrees instead of spawning + importing a
fresh interpreter per task (spawn-to-ready dominates per-task latency).

Scenarios:
  (a) worker rebind unit: one cambium.worker process serves TWO worktrees
      sequentially with ``worker_reuse`` enabled — same PID (single spawn),
      both results correct, clean exit on stdin close.
  (b) supervisor pool: two sequential tasks in one run_plan session with
      CAMBIUM_WARM_POOL_SIZE=2; the second task reuses the pooled process
      (``worker_reused`` event with task_id + pid).
  (c) restarts: a restarted generation always spawns a fresh process; the
      pooled process is never rebound to the restarted generation.
  (d) pool disabled (CAMBIUM_WARM_POOL_SIZE=0): single-init behavior and the
      full protocol (init -> ready -> run_task -> result -> exit) unchanged,
      zero ``worker_reused`` events.
  (e) session end kills idle pooled workers (no child processes remain).
"""

from __future__ import annotations

import asyncio
import json
import os
import signal
import subprocess
import sys
from pathlib import Path

from cambium import supervisor as supervisor_module
from cambium.fencing import write_generation
from cambium.ipc import MAX_LINE_BYTES, read_message
from cambium.supervisor import read_events, run_plan

ROOT = Path(__file__).resolve().parents[2]
MARKER = "// cambium-pool"
HEARTBEAT_INTERVAL_S = 0.05


def _make_repo(repo: Path, files: dict[str, str]) -> str:
    subprocess.run(["git", "init", "-b", "main", str(repo)], check=True, capture_output=True)
    for key, value in (
        ("user.name", "pool-test"),
        ("user.email", "pool@test"),
        ("gc.auto", "0"),
    ):
        subprocess.run(["git", "-C", str(repo), "config", key, value], check=True)
    for name, content in files.items():
        (repo / name).write_text(content, encoding="utf-8")
    subprocess.run(["git", "-C", str(repo), "add", "-A"], check=True, capture_output=True)
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


def _show(repo: Path, ref: str, path: str) -> str:
    return subprocess.run(
        ["git", "-C", str(repo), "show", f"{ref}:{path}"],
        check=True, capture_output=True, text=True,
    ).stdout


def _marker_task(
    session_dir: Path, repo: Path, base: str, task_id: str, *,
    worktree: str, branch: str, marker: str, target_file: str = "a.txt",
    depends_on: list[str] | None = None, **extra: object,
) -> dict[str, object]:
    task: dict[str, object] = {
        "task_id": task_id,
        "task": f"edit {target_file} and commit",
        "repo": str(repo),
        "worktree_path": str(session_dir / worktree),
        "branch": branch,
        "worker": "cambium.worker",
        "target_file": target_file,
        "marker": marker,
        "write_marker": True,
        "base_commit": base,
        "provider_env_keys": [],
        "max_restarts": 0,
        "depends_on": list(depends_on or []),
    }
    task.update(extra)
    return task


def _kinds(events: list[dict], kind: str) -> list[dict]:
    return [event for event in events if event["kind"] == kind]


def _protocol(events: list[dict], task_id: str) -> list[str]:
    return [
        event["kind"]
        for event in events
        if event["task_id"] == task_id
        and event["kind"] in ("init", "ready", "run_task", "result", "exit")
    ]


def _pid_is_alive(pid: int) -> bool:
    try:
        os.kill(pid, 0)
    except (ProcessLookupError, PermissionError):
        return False
    return True


def _worker_pids() -> list[int]:
    """Pids of live processes whose command line names the cambium worker."""
    pids: list[int] = []
    for entry in Path("/proc").iterdir():
        if not entry.name.isdigit():
            continue
        try:
            cmdline = (entry / "cmdline").read_bytes().decode("utf-8", "replace")
        except OSError:
            continue
        if "cambium.worker" in cmdline:
            pids.append(int(entry.name))
    return pids


# ---------------------------------------------------------------------------
# (a) worker rebind unit: one process, two worktrees, reuse enabled
# ---------------------------------------------------------------------------


class _WorkerDriver:
    """Scripted supervisor side driving one reuse-enabled cambium.worker."""

    def __init__(self, cwd: Path) -> None:
        self.proc: asyncio.subprocess.Process | None = None
        self.cwd = cwd
        self.stderr_lines: list[str] = []
        self._stderr_task: asyncio.Task | None = None

    async def start(self) -> None:
        self.proc = await asyncio.create_subprocess_exec(
            sys.executable, "-u", "-m", "cambium.worker",
            stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            env={
                **os.environ,
                "PYTHONPATH": str(ROOT / "src"),
                "PYTHONUNBUFFERED": "1",
            },
            cwd=str(self.cwd),
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

    async def send(self, msg: dict[str, object]) -> None:
        assert self.proc is not None
        self.proc.stdin.write((json.dumps(msg) + "\n").encode("utf-8"))
        await self.proc.stdin.drain()

    async def recv_until(self, mtype: str, timeout: float = 30.0) -> dict:
        assert self.proc is not None
        deadline = asyncio.get_running_loop().time() + timeout
        while True:
            remaining = deadline - asyncio.get_running_loop().time()
            if remaining <= 0:
                raise AssertionError(
                    f"timed out waiting for {mtype}; stderr={self.stderr_lines!r}"
                )
            msg = await asyncio.wait_for(
                read_message(self.proc.stdout, limit=MAX_LINE_BYTES), remaining
            )
            if msg is None:
                raise AssertionError(
                    f"EOF while waiting for {mtype}; stderr={self.stderr_lines!r}"
                )
            if msg["type"] == mtype:
                return msg
            if msg["type"] != "heartbeat":
                raise AssertionError(
                    f"unexpected {msg['type']!r} while waiting for {mtype}; "
                    f"stderr={self.stderr_lines!r}"
                )

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


def _init_msg(
    task_id: str, worktree: Path, *, generation: int = 1, worker_reuse: bool = True
) -> dict[str, object]:
    return {
        "type": "init",
        "request_id": f"init-{task_id}",
        "task_id": task_id,
        "proto": 1,
        "generation": generation,
        "worktree": str(worktree),
        "spec": f"append the {task_id} marker to a.txt",
        "max_turns": 10,
        "max_tokens": 100_000,
        "heartbeat": {"interval_s": HEARTBEAT_INTERVAL_S},
        "budget": {"max_wall_s": 60.0},
        "permissions": {"shell": True, "network": False},
        "provider_env_keys": [],
        "worker_reuse": worker_reuse,
    }


def _run_msg(task_id: str, scratch: Path, worktree: Path, branch: str) -> dict[str, object]:
    return {
        "type": "run_task",
        "request_id": f"run-{task_id}",
        "task_id": task_id,
        "scratch_repo": str(scratch),
        "worktree_path": str(worktree),
        "branch": branch,
        "target_file": "a.txt",
        "marker": f"// pool-{task_id}",
        "write_marker": True,
        "generation": 1,
    }


def test_worker_rebind_serves_two_worktrees_from_one_process(tmp_path: Path) -> None:
    """(a) One reuse-enabled worker process serves two sequential worktrees."""
    session = tmp_path / "session"
    scratch = session / "scratch"
    base = _make_repo(scratch, {"a.txt": "file a\n"})
    wt_a = session / "wt-a"
    wt_b = session / "wt-b"
    for wt, branch in ((wt_a, "wt-a"), (wt_b, "wt-b")):
        subprocess.run(
            ["git", "-C", str(scratch), "worktree", "add", "-b", branch, str(wt), base],
            check=True,
            capture_output=True,
        )
        write_generation(wt, 1)

    async def scenario() -> None:
        driver = _WorkerDriver(cwd=wt_a)
        await driver.start()
        try:
            await driver.send(_init_msg("t-a", wt_a))
            ready1 = await driver.recv_until("ready")
            pid = int(ready1["pid"])
            assert pid > 0

            await driver.send(_run_msg("t-a", scratch, wt_a, "wt-a"))
            result1 = await driver.recv_until("result_envelope")
            assert result1["status"] == "succeeded"
            assert len(result1["commits"]) == 1
            reuse1 = await driver.recv_until("reuse_ready")
            assert reuse1["pid"] == pid

            # Rebind: same process, new worktree, fresh per-task state.
            await driver.send(_init_msg("t-b", wt_b))
            ready2 = await driver.recv_until("ready")
            assert ready2["pid"] == pid
            await driver.send(_run_msg("t-b", scratch, wt_b, "wt-b"))
            result2 = await driver.recv_until("result_envelope")
            assert result2["status"] == "succeeded"
            reuse2 = await driver.recv_until("reuse_ready")
            assert reuse2["pid"] == pid

            # Clean exit on stdin close.
            assert driver.proc is not None
            driver.proc.stdin.close()
            rc = await asyncio.wait_for(driver.proc.wait(), 30.0)
            assert rc == 0
        finally:
            await driver.stop()

        # Both tasks landed in their own worktree (state never crosses tasks).
        assert "// pool-t-a" in (wt_a / "a.txt").read_text(encoding="utf-8")
        assert "// pool-t-b" in (wt_b / "a.txt").read_text(encoding="utf-8")

    asyncio.run(scenario())


# ---------------------------------------------------------------------------
# (b) supervisor pool: second sequential task reuses the pooled process
# ---------------------------------------------------------------------------


def test_supervisor_pool_reuses_worker_across_tasks(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setenv("CAMBIUM_WARM_POOL_SIZE", "2")
    session_dir = tmp_path / "session"
    repo = session_dir / "repo"
    base = _make_repo(repo, {"a.txt": "file a\n", "b.txt": "file b\n"})
    # depends_on makes the waves deterministic: t-a runs first (spawns and
    # pools its worker), then t-b must pop that pooled process.
    plan = {
        "tasks": [
            _marker_task(session_dir, repo, base, "t-a", worktree="wt-a",
                         branch="wt-a", marker="// cambium-a", target_file="a.txt"),
            _marker_task(session_dir, repo, base, "t-b", worktree="wt-b",
                         branch="wt-b", marker="// cambium-b", target_file="b.txt",
                         depends_on=["t-a"]),
        ]
    }

    result = asyncio.run(
        run_plan(session_dir, plan, max_concurrent_tasks=1, warm_pool_size=2)
    )

    assert result.exit_code == 0
    assert {r.task_id for r in result.results} == {"t-a", "t-b"}
    assert all(r.status == "succeeded" for r in result.results)
    events = read_events(session_dir)

    # Exactly one spawn for both tasks; the second task reused the pooled
    # process (worker_reused carries the new task_id and the pooled pid).
    assert len(_kinds(events, "spawned")) == 1
    reused = _kinds(events, "worker_reused")
    assert len(reused) == 1, f"expected one reuse, got {reused}"
    assert reused[0]["task_id"] == "t-b"
    assert reused[0]["generation"] == 1
    ready_a = next(e for e in _kinds(events, "ready") if e["task_id"] == "t-a")
    assert reused[0]["payload"]["pid"] == ready_a["payload"]["pid"]
    # Both markers landed on main.
    assert "// cambium-a" in _show(repo, "main", "a.txt")
    assert "// cambium-b" in _show(repo, "main", "b.txt")


# ---------------------------------------------------------------------------
# (c) restarts: the restarted generation never reuses a pooled process
# ---------------------------------------------------------------------------


def test_restarted_generation_spawns_fresh_never_reuses_pool(
    tmp_path: Path, monkeypatch
) -> None:
    monkeypatch.setattr(supervisor_module, "RESTART_BASE_DELAY_S", 0.01)
    monkeypatch.setattr(supervisor_module, "RESTART_MAX_DELAY_S", 0.02)
    monkeypatch.setenv("CAMBIUM_WARM_POOL_SIZE", "2")
    session_dir = tmp_path / "session"
    repo = session_dir / "repo"
    base = _make_repo(
        repo,
        {
            "root.txt": "file root\n",
            "a.txt": "file a\n",
            "b.txt": "file b\n",
            "c.txt": "file c\n",
        },
    )
    # One rooted tree: wave 1 spawns t-root (pooled); wave 2 runs t-a and
    # t-b concurrently — one pops the pooled process, the other spawns fresh,
    # and both return, so two idle processes are pooled when t-crash starts.
    # Wave 3: t-crash's first generation pops one pooled process and is
    # killed by the tiny wall budget; its RESTARTED generation must spawn
    # fresh (allow_pool=False) instead of popping the second pooled process.
    plan = {
        "tasks": [
            _marker_task(session_dir, repo, base, "t-root", worktree="wt-root",
                         branch="wt-root", marker="// cambium-root",
                         target_file="root.txt"),
            _marker_task(session_dir, repo, base, "t-a", worktree="wt-a",
                         branch="wt-a", marker="// cambium-a", target_file="a.txt",
                         depends_on=["t-root"]),
            _marker_task(session_dir, repo, base, "t-b", worktree="wt-b",
                         branch="wt-b", marker="// cambium-b", target_file="b.txt",
                         depends_on=["t-root"]),
            _marker_task(session_dir, repo, base, "t-crash", worktree="wt-crash",
                         branch="wt-crash", marker="// cambium-crash",
                         target_file="c.txt", max_restarts=1, max_wall_s=0.001,
                         depends_on=["t-b"]),
        ]
    }

    result = asyncio.run(run_plan(session_dir, plan, warm_pool_size=2))

    crash_result = next(r for r in result.results if r.task_id == "t-crash")
    assert crash_result.status == "failed"
    assert crash_result.restarts == 1
    assert {r.task_id for r in result.results if r.status == "succeeded"} == {
        "t-root", "t-a", "t-b"
    }
    events = read_events(session_dir)

    # The pool held idle processes before the crash task: root and the two
    # wave-2 tasks all reported reuse-ready (bounded to the pool size).
    assert len(_kinds(events, "reuse_ready")) == 3

    # The restarted generation spawned fresh; it never fired worker_reused.
    spawned = _kinds(events, "spawned")
    assert [e["generation"] for e in spawned if e["task_id"] == "t-crash"] == [2]
    assert [e["generation"] for e in spawned if e["task_id"] == "t-root"] == [1]
    reused = _kinds(events, "worker_reused")
    crash_reused = [e for e in reused if e["task_id"] == "t-crash"]
    assert [e["generation"] for e in crash_reused] == [1]
    assert not [
        e for e in crash_reused if e["generation"] == 2
    ]


# ---------------------------------------------------------------------------
# (d) pool disabled: zero behavior change (single-init protocol preserved)
# ---------------------------------------------------------------------------


def test_pool_disabled_size_zero_keeps_single_init_behavior(
    tmp_path: Path, monkeypatch
) -> None:
    monkeypatch.setenv("CAMBIUM_WARM_POOL_SIZE", "0")
    session_dir = tmp_path / "session"
    repo = session_dir / "repo"
    base = _make_repo(repo, {"a.txt": "file a\n", "b.txt": "file b\n"})
    plan = {
        "tasks": [
            _marker_task(session_dir, repo, base, "t-a", worktree="wt-a",
                         branch="wt-a", marker="// cambium-a", target_file="a.txt"),
            _marker_task(session_dir, repo, base, "t-b", worktree="wt-b",
                         branch="wt-b", marker="// cambium-b", target_file="b.txt"),
        ]
    }

    result = asyncio.run(
        run_plan(session_dir, plan, max_concurrent_tasks=1, warm_pool_size=0)
    )

    assert result.exit_code == 0
    assert all(r.status == "succeeded" for r in result.results)
    events = read_events(session_dir)
    assert not _kinds(events, "worker_reused")
    assert not _kinds(events, "reuse_ready")
    for tid in ("t-a", "t-b"):
        assert _protocol(events, tid) == ["init", "ready", "run_task", "result", "exit"]
    assert "// cambium-a" in _show(repo, "main", "a.txt")
    assert "// cambium-b" in _show(repo, "main", "b.txt")


# ---------------------------------------------------------------------------
# (e) session end kills idle pooled workers
# ---------------------------------------------------------------------------


def test_session_end_kills_idle_pooled_workers(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setenv("CAMBIUM_WARM_POOL_SIZE", "2")
    session_dir = tmp_path / "session"
    repo = session_dir / "repo"
    base = _make_repo(repo, {"a.txt": "file a\n"})
    plan = {
        "tasks": [
            _marker_task(session_dir, repo, base, "t-a", worktree="wt-a",
                         branch="wt-a", marker="// cambium-a"),
        ]
    }

    result = asyncio.run(run_plan(session_dir, plan, warm_pool_size=2))

    assert result.exit_code == 0
    assert result.results[0].status == "succeeded"
    events = read_events(session_dir)
    # The worker was pooled idle at task completion...
    assert _kinds(events, "reuse_ready")
    ready = _kinds(events, "ready")
    assert len(ready) == 1
    pid = int(ready[0]["payload"]["pid"])

    # ...and killed when the session ended (run_plan finally -> shutdown).
    assert not _pid_is_alive(pid), f"pooled worker {pid} still alive after session"
    assert pid not in _worker_pids(), f"cambium worker {pid} still running"
