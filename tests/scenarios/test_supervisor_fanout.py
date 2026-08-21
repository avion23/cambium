"""Custos multi-worker supervisor scenarios (T1-T6).

Real supervisor (``cambium.supervisor.run_plan``) driving real worker
subprocesses and real git operations. No mocks, no network. Workers come
from ``scripts/fake_worker.py`` plus dedicated fixtures for recovery and
hardening scenarios.

Scenarios:
  T1 fan-out: 3 workers, disjoint files -> all merged, coherent event log.
  T2 hang:    worker never ready -> killed + restarted, cap -> failed, no merge.
  T3 crash:   worker exits nonzero mid-edit -> restart with recovered worktree.
  T4 race:    two workers same file -> one wins, merge_failed for the loser.
  T5 garbage: stdout noise tolerated; pure garbage fails cleanly on cap.
  T6 shutdown: SIGTERM mid-run -> clean exit, store flushes (reopen OK).
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import os
import signal
import sqlite3
import subprocess
import sys
import textwrap
import time
from pathlib import Path

import pytest

from cambium import supervisor as supervisor_module
from cambium.merge import MergeSequencer
from cambium.store import EventStore
from cambium.supervisor import read_events, run_plan

ROOT = Path(__file__).resolve().parents[2]
WORKER = str(ROOT / "scripts" / "fake_worker.py")
CRASH_WORKER = str(ROOT / "tests" / "fixtures" / "crash_worker.py")
TEST_RESOURCE_THRESHOLDS = {
    "mem_available_frac": 0.0,
    "load1_per_cpu": 1_000_000.0,
    "disk_free": 0,
}


def _make_repo(repo: Path, files: dict[str, str]) -> str:
    subprocess.run(["git", "init", "-b", "main", str(repo)], check=True, capture_output=True)
    subprocess.run(["git", "-C", str(repo), "config", "user.name", "fanout-test"], check=True)
    subprocess.run(["git", "-C", str(repo), "config", "user.email", "fanout@test"], check=True)
    subprocess.run(["git", "-C", str(repo), "config", "gc.auto", "0"], check=True)
    for name, content in files.items():
        (repo / name).write_text(content)
    subprocess.run(["git", "-C", str(repo), "add", "-A"], check=True, capture_output=True)
    subprocess.run(
        ["git", "-C", str(repo), "commit", "-m", "initial"],
        check=True,
        capture_output=True,
    )
    return subprocess.run(
        ["git", "-C", str(repo), "rev-parse", "HEAD"], check=True, capture_output=True, text=True
    ).stdout.strip()


def _show(repo: Path, ref: str, path: str) -> str:
    return subprocess.run(
        ["git", "-C", str(repo), "show", f"{ref}:{path}"],
        check=True, capture_output=True, text=True,
    ).stdout


def _task(
    session_dir: Path, repo: Path, base: str, task_id: str, *,
    worktree: str, branch: str, target_file: str, marker: str, gate: str = "",
    worker: str = WORKER, **extra,
) -> dict:
    spec = {
        "task_id": task_id,
        "task": f"edit {target_file}",
        "repo": str(repo),
        "worktree_path": str(session_dir / worktree),
        "branch": branch,
        "worker": worker,
        "target_file": target_file,
        "marker": marker,
        "write_marker": True,
        "gate": gate,
        "base_commit": base,
        "provider_env_keys": ["FAKE_MODE"],
        "resource_thresholds": TEST_RESOURCE_THRESHOLDS,
    }
    spec.update(extra)
    return spec


def _protocol(events: list[dict], task_id: str) -> list[str]:
    wanted = {"init", "ready", "run_task", "result", "exit"}
    return [e["kind"] for e in events if e["task_id"] == task_id and e["kind"] in wanted]


def _kinds(events: list[dict], kind: str) -> list[dict]:
    return [e for e in events if e["kind"] == kind]


def _worktree_paths(repo: Path) -> list[Path]:
    output = subprocess.run(
        ["git", "-C", str(repo), "worktree", "list", "--porcelain"],
        check=True, capture_output=True, text=True,
    ).stdout
    return [
        Path(line.removeprefix("worktree ")).resolve()
        for line in output.splitlines()
        if line.startswith("worktree ")
    ]


def _branch_exists(repo: Path, branch: str) -> bool:
    return subprocess.run(
        ["git", "-C", str(repo), "show-ref", "--verify", "--quiet", f"refs/heads/{branch}"],
        check=False,
    ).returncode == 0


# ---------------------------------------------------------------------------
# T1: fan-out — three workers, disjoint files, all merged, coherent log.
# ---------------------------------------------------------------------------


@pytest.mark.slow
def test_t1_fanout_disjoint_files_all_merged(tmp_path) -> None:
    session_dir = tmp_path / "session"
    repo = session_dir / "repo"
    base = _make_repo(repo, {"a.txt": "file a\n", "b.txt": "file b\n", "c.txt": "file c\n"})

    plan = {
        "tasks": [
            _task(session_dir, repo, base, "t-a", worktree="wt-a", branch="wt-a",
                  target_file="a.txt", marker="// cambium-a", gate="grep -q '// cambium-a' a.txt"),
            _task(session_dir, repo, base, "t-b", worktree="wt-b", branch="wt-b",
                  target_file="b.txt", marker="// cambium-b", gate="grep -q '// cambium-b' b.txt"),
            _task(session_dir, repo, base, "t-c", worktree="wt-c", branch="wt-c",
                  target_file="c.txt", marker="// cambium-c", gate="grep -q '// cambium-c' c.txt"),
        ]
    }

    result = asyncio.run(run_plan(session_dir, plan))

    assert result.exit_code == 0
    assert {r.task_id for r in result.results} == {"t-a", "t-b", "t-c"}
    assert all(r.status == "succeeded" for r in result.results)
    assert all(r.merge_sha is not None for r in result.results)

    for name, marker in (("a.txt", "// cambium-a"), ("b.txt", "// cambium-b"),
                         ("c.txt", "// cambium-c")):
        assert marker in _show(repo, "main", name)

    assert _worktree_paths(repo) == [repo.resolve()]
    for branch in ("wt-a", "wt-b", "wt-c"):
        assert not _branch_exists(repo, branch)
    events = read_events(session_dir)
    assert len(_kinds(events, "merge_committed")) == 3
    pruned = _kinds(events, "worktree_pruned")
    deferred = _kinds(events, "worktree_cleanup_deferred")
    assert {event["task_id"] for event in pruned} == {"t-a", "t-b", "t-c"}
    assert not deferred
    for tid in ("t-a", "t-b", "t-c"):
        assert _protocol(events, tid) == ["init", "ready", "run_task", "result", "exit"]
        task_events = [event for event in events if event["task_id"] == tid]
        init = next(event for event in task_events if event["kind"] == "init")
        ready = next(event for event in task_events if event["kind"] == "ready")
        assert ready["request_id"] == init["request_id"]
    assert events[-1]["kind"] == "session_ended"
    with sqlite3.connect(session_dir / ".cambium" / "events.db") as connection:
        terminal = connection.execute(
            "SELECT kind, task_id FROM events "
            "WHERE kind IN ('result', 'merge_committed', 'session_ended', "
            "'worktree_pruned', 'worktree_cleanup_deferred')"
        ).fetchall()
    assert sum(kind == "result" for kind, _task_id in terminal) == 3
    assert sum(kind == "merge_committed" for kind, _task_id in terminal) == 3
    assert sum(kind == "worktree_pruned" for kind, _task_id in terminal) == 3
    assert not any(kind == "worktree_cleanup_deferred" for kind, _task_id in terminal)
    assert ("session_ended", None) in terminal
@pytest.mark.slow
def test_concurrency_cap_one_serializes_tasks(tmp_path) -> None:
    """max_concurrent_tasks=1: the second task spawns only after the first ends."""
    session_dir = tmp_path / "session"
    repo = session_dir / "repo"
    base = _make_repo(repo, {"a.txt": "file a\n", "b.txt": "file b\n"})
    plan = {
        "tasks": [
            _task(session_dir, repo, base, "t-a", worktree="wt-a", branch="wt-a",
                  target_file="a.txt", marker="// cambium-a"),
            _task(session_dir, repo, base, "t-b", worktree="wt-b", branch="wt-b",
                  target_file="b.txt", marker="// cambium-b"),
        ]
    }

    result = asyncio.run(run_plan(session_dir, plan, max_concurrent_tasks=1))

    assert result.exit_code == 0
    assert all(r.status == "succeeded" for r in result.results)
    events = read_events(session_dir)
    spawned = _kinds(events, "spawned")
    assert {e["task_id"] for e in spawned} == {"t-a", "t-b"}
    # Which task wins the single admission slot first is scheduler-dependent;
    # the invariant is that the second spawn happens only after the first
    # task's worker phase ended (result + exit both precede the semaphore
    # release, which happens before merge/prune).
    first, second = spawned[0], spawned[1]
    first_worker_end = max(
        e["seq"] for e in events
        if e["task_id"] == first["task_id"] and e["kind"] in ("result", "exit")
    )
    assert second["seq"] > first_worker_end



@pytest.mark.slow
def test_observer_barriers_do_not_hold_merge_or_worktree_locks(tmp_path) -> None:
    session_dir = tmp_path / "session"
    repo = session_dir / "repo"
    base = _make_repo(repo, {"a.txt": "file a\n", "b.txt": "file b\n"})
    plan = {
        "tasks": [
            _task(
                session_dir, repo, base, "t-a", worktree="wt-a", branch="wt-a",
                target_file="a.txt", marker="// cambium-a",
                gate="grep -q '// cambium-a' a.txt",
            ),
            _task(
                session_dir, repo, base, "t-b", worktree="wt-b", branch="wt-b",
                target_file="b.txt", marker="// cambium-b",
                gate="grep -q '// cambium-b' b.txt",
            ),
        ]
    }

    async def canary() -> None:
        merge_events: set[str] = set()
        prune_events: set[str] = set()
        both_merged = asyncio.Event()
        both_pruned = asyncio.Event()

        async def observer(event: dict) -> None:
            if event["kind"] == "merge_committed":
                merge_events.add(event["task_id"])
                if len(merge_events) == 2:
                    both_merged.set()
                await both_merged.wait()
            if event["kind"] == "worktree_pruned":
                prune_events.add(event["task_id"])
                if len(prune_events) == 2:
                    both_pruned.set()
                await both_pruned.wait()

        # Deadlock canary: the observer barrier must never hold merge/worktree
        # locks. The bound is generous (60s) so a healthy run under parallel
        # load (pytest-xdist) finishes, while a true deadlock still fails.
        result = await asyncio.wait_for(run_plan(session_dir, plan, observer), timeout=60)
        assert result.exit_code == 0
        assert merge_events == {"t-a", "t-b"}
        assert prune_events == {"t-a", "t-b"}

    asyncio.run(canary())
    assert len(_kinds(read_events(session_dir), "merge_committed")) == 2


# ---------------------------------------------------------------------------
# Worktree cleanup deferral without a gate — a branch ref lock held by a
# concurrent actor and a dirty worker tree both defer prune and keep the tree
# registered; no gate writes either artifact.
# ---------------------------------------------------------------------------


@pytest.mark.slow
def test_branch_delete_failure_defers_cleanup_without_false_prune(tmp_path) -> None:
    worker = tmp_path / "branch-lock-worker.py"
    worker.write_text(textwrap.dedent("""
        import json
        import subprocess
        import sys
        from pathlib import Path

        def send(message):
            sys.stdout.write(json.dumps(message) + "\\n")
            sys.stdout.flush()

        init = json.loads(sys.stdin.readline())
        send({
            "type": "ready",
            "request_id": init["request_id"],
            "task_id": init["task_id"],
            "pid": 0,
            "generation": init.get("generation", 1),
            "proto": 1,
        })
        run = json.loads(sys.stdin.readline())
        worktree = Path(run["worktree_path"])
        target = worktree / run["target_file"]
        target.write_text(target.read_text().rstrip("\\n") + "\\n" + run["marker"] + "\\n")
        subprocess.run(["git", "add", run["target_file"]], cwd=worktree, check=True)
        subprocess.run(["git", "commit", "-m", "branch-lock-worker"], cwd=worktree, check=True)
        lock = Path(run["repo"]) / ".git" / "refs" / "heads" / f"{run['branch']}.lock"
        lock.parent.mkdir(parents=True, exist_ok=True)
        lock.write_text("concurrent ref lock\\n")
        send({
            "type": "result_envelope",
            "request_id": run["request_id"],
            "task_id": init["task_id"],
            "status": "succeeded",
        })
        send({
            "type": "exit_message",
            "task_id": init["task_id"],
            "generation": init.get("generation", 1),
            "reason": "done",
        })
    """), encoding="utf-8")

    session_dir = tmp_path / "session"
    repo = session_dir / "repo"
    base = _make_repo(repo, {"a.txt": "file a\n"})
    branch = "wt-branch-lock"

    plan = {
        "tasks": [
            _task(
                session_dir, repo, base, "t-branch-lock", worktree="wt-branch-lock",
                branch=branch, target_file="a.txt", marker="// cambium-locked",
                worker=str(worker),
            )
        ]
    }

    result = asyncio.run(run_plan(session_dir, plan))

    (task,) = result.results
    assert task.status == "succeeded"
    assert (session_dir / "wt-branch-lock").exists()
    assert _worktree_paths(repo) == [repo.resolve(), (session_dir / "wt-branch-lock").resolve()]
    assert _branch_exists(repo, branch)

    events = read_events(session_dir)
    deferred = _kinds(events, "worktree_cleanup_deferred")
    assert len(deferred) == 1
    assert deferred[0]["payload"]["reason"] == "branch_delete_failed"
    assert deferred[0]["payload"]["restored"] is True
    assert not _kinds(events, "worktree_pruned")
    with sqlite3.connect(session_dir / ".cambium" / "events.db") as connection:
        terminal = connection.execute(
            "SELECT kind, task_id FROM events "
            "WHERE task_id = ? AND kind IN ('result', 'worktree_pruned', "
            "'worktree_cleanup_deferred')",
            ("t-branch-lock",),
        ).fetchall()
    assert "worktree_cleanup_deferred" in {kind for kind, _task_id in terminal}
    assert "worktree_pruned" not in {kind for kind, _task_id in terminal}


@pytest.mark.slow
def test_dirty_worker_tree_defers_cleanup_and_keeps_tree_registered(tmp_path) -> None:
    worker = tmp_path / "dirty-worker.py"
    worker.write_text(textwrap.dedent("""
        import json
        import subprocess
        import sys
        from pathlib import Path

        def send(message):
            sys.stdout.write(json.dumps(message) + "\\n")
            sys.stdout.flush()

        init = json.loads(sys.stdin.readline())
        send({
            "type": "ready",
            "request_id": init["request_id"],
            "task_id": init["task_id"],
            "pid": 0,
            "generation": init.get("generation", 1),
            "proto": 1,
        })
        run = json.loads(sys.stdin.readline())
        worktree = Path(run["worktree_path"])
        target = worktree / run["target_file"]
        target.write_text(target.read_text().rstrip("\\n") + "\\n" + run["marker"] + "\\n")
        (worktree / ".gitignore").write_text("leftover.txt\\n")
        subprocess.run(
            ["git", "add", run["target_file"], ".gitignore"], cwd=worktree, check=True
        )
        subprocess.run(["git", "commit", "-m", "dirty-worker"], cwd=worktree, check=True)
        (worktree / "leftover.txt").write_text("dirty content\\n")
        send({
            "type": "result_envelope",
            "request_id": run["request_id"],
            "task_id": init["task_id"],
            "status": "succeeded",
        })
        send({
            "type": "exit_message",
            "task_id": init["task_id"],
            "generation": init.get("generation", 1),
            "reason": "done",
        })
    """), encoding="utf-8")

    session_dir = tmp_path / "session"
    repo = session_dir / "repo"
    base = _make_repo(repo, {"a.txt": "file a\n"})
    branch = "wt-dirty-worker"
    worktree = session_dir / branch

    plan = {
        "tasks": [
            _task(
                session_dir, repo, base, "t-dirty-worker", worktree=branch, branch=branch,
                target_file="a.txt", marker="// cambium-dirty", worker=str(worker),
            )
        ]
    }

    result = asyncio.run(run_plan(session_dir, plan))

    (task,) = result.results
    # the worker left an ignored file behind (leftover.txt): the integrity
    # check must not accept that as a clean success — the task fails, and the
    # dirty retained tree defers cleanup as before.
    assert task.status == "failed"
    assert task.reason == "worker_tree_dirty"
    assert worktree.exists()
    assert (worktree / "leftover.txt").read_text() == "dirty content\n"
    assert _worktree_paths(repo) == [repo.resolve(), worktree.resolve()]
    assert _branch_exists(repo, branch)

    events = read_events(session_dir)
    deferred = _kinds(events, "worktree_cleanup_deferred")
    assert len(deferred) == 1
    assert deferred[0]["payload"]["reason"] == "dirty"
    assert not _kinds(events, "worktree_pruned")
    with sqlite3.connect(session_dir / ".cambium" / "events.db") as connection:
        terminal = connection.execute(
            "SELECT kind, task_id FROM events "
            "WHERE task_id = ? AND kind IN ('result', 'worktree_pruned', "
            "'worktree_cleanup_deferred')",
            ("t-dirty-worker",),
        ).fetchall()
    assert "worktree_cleanup_deferred" in {kind for kind, _task_id in terminal}
    assert "worktree_pruned" not in {kind for kind, _task_id in terminal}


# ---------------------------------------------------------------------------
# Ready correlation — a wrong ready request_id is a protocol violation and must
# not admit the worker to run_task.
# ---------------------------------------------------------------------------


@pytest.mark.slow
def test_wrong_ready_request_id_kills_worker_before_run(tmp_path) -> None:
    worker = tmp_path / "wrong-ready-worker.py"
    worker.write_text(textwrap.dedent("""
        import json
        import subprocess
        import sys
        from pathlib import Path

        def send(message):
            sys.stdout.write(json.dumps(message) + "\\n")
            sys.stdout.flush()

        init = json.loads(sys.stdin.readline())
        generation = init.get("generation", 1)
        send({
            "type": "ready",
            "request_id": init["request_id"] if generation > 1 else "wrong-request-id",
            "task_id": init["task_id"],
            "pid": 0,
            "generation": generation,
            "proto": 1,
        })
        run = json.loads(sys.stdin.readline())
        worktree = Path(run["worktree_path"])
        target = worktree / run["target_file"]
        target.write_text(target.read_text().rstrip("\\n") + "\\n" + run["marker"] + "\\n")
        subprocess.run(["git", "add", run["target_file"]], cwd=worktree, check=True)
        subprocess.run(["git", "commit", "-m", "wrong-ready"], cwd=worktree, check=True)
        send({
            "type": "result_envelope",
            "request_id": run["request_id"],
            "task_id": init["task_id"],
            "status": "succeeded",
        })
        send({
            "type": "exit_message",
            "task_id": init["task_id"],
            "generation": generation,
            "reason": "done",
        })
    """), encoding="utf-8")

    session_dir = tmp_path / "session"
    repo = session_dir / "repo"
    base = _make_repo(repo, {"a.txt": "file a\n"})
    plan = {
        "tasks": [
            _task(
                session_dir,
                repo,
                base,
                "t-wrong-ready",
                worktree="wt-wrong-ready",
                branch="wt-wrong-ready",
                target_file="a.txt",
                marker="// wrong-ready-must-not-run",
                gate="grep -q '// wrong-ready-must-not-run' a.txt",
                worker=str(worker),
            )
        ]
    }

    result = asyncio.run(run_plan(session_dir, plan))

    (task,) = result.results
    assert result.exit_code != 0
    assert task.status == "failed"
    assert task.restarts == 0
    assert "ready_request_id_mismatch" in (task.reason or "")
    events = read_events(session_dir)
    assert len(_kinds(events, "spawned")) == 1
    assert {event["generation"] for event in _kinds(events, "spawned")} == {1}
    assert not _kinds(events, "restart_scheduled")
    protocol = _kinds(events, "protocol")
    assert len(protocol) == 1
    assert protocol[0]["payload"]["code"] == "PROTO_UNKNOWN_REQUEST_ID"
    assert protocol[0]["payload"]["expected"] != protocol[0]["payload"]["got"]
    assert not _kinds(events, "run_task")
    assert not _kinds(events, "gate")
    assert not _kinds(events, "merge_committed")
    assert _show(repo, "main", "a.txt") == "file a\n"


@pytest.mark.slow
def test_merge_committed_observer_cancellation_is_nonfatal(tmp_path) -> None:
    session_dir = tmp_path / "session"
    repo = session_dir / "repo"
    base = _make_repo(repo, {"a.txt": "file a\n"})
    task_id = "t-observer-cancel"
    plan = {
        "tasks": [
            _task(
                session_dir, repo, base, task_id, worktree="wt-observer-cancel",
                branch="wt-observer-cancel", target_file="a.txt",
                marker="// observer-cancel", gate="grep -q '// observer-cancel' a.txt",
            )
        ]
    }

    async def cancel_merge_committed(event: dict) -> None:
        if event["kind"] == "merge_committed":
            raise asyncio.CancelledError

    result = asyncio.run(run_plan(session_dir, plan, on_event=cancel_merge_committed))
    events = read_events(session_dir)
    main_tip = subprocess.run(
        ["git", "-C", str(repo), "rev-parse", "refs/heads/main"],
        check=True, capture_output=True, text=True,
    ).stdout.strip()

    assert result.exit_code == 0
    assert len(result.results) == 1
    assert result.results[0].task_id == task_id
    assert result.results[0].status == "succeeded"
    assert main_tip != base
    assert len(_kinds(events, "spawned")) == 1
    committed = _kinds(events, "merge_committed")
    assert len(committed) == 1
    assert committed[0]["payload"]["new"] == main_tip


@pytest.mark.slow
def test_external_cancellation_during_critical_observer_aborts_plan(tmp_path) -> None:
    session_dir = tmp_path / "session"
    repo = session_dir / "repo"
    base = _make_repo(repo, {"a.txt": "file a\n"})
    plan = {
        "tasks": [
            _task(
                session_dir, repo, base, "t-external-cancel", worktree="wt-external-cancel",
                branch="wt-external-cancel", target_file="a.txt", marker="// must-not-merge",
                gate="grep -q '// must-not-merge' a.txt",
            )
        ]
    }
    observer_started = asyncio.Event()

    async def suspend_on_result(event: dict) -> None:
        if event["kind"] != "result":
            return
        observer_started.set()
        await asyncio.Event().wait()

    async def cancel_plan() -> None:
        task = asyncio.create_task(run_plan(session_dir, plan, on_event=suspend_on_result))
        await asyncio.wait_for(observer_started.wait(), 30)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        assert task.cancelled()

    asyncio.run(cancel_plan())
    events = read_events(session_dir)
    main_tip = subprocess.run(
        ["git", "-C", str(repo), "rev-parse", "refs/heads/main"],
        check=True, capture_output=True, text=True,
    ).stdout.strip()

    assert main_tip == base
    assert not _kinds(events, "gate")
    assert not _kinds(events, "merge_committed")
    assert _kinds(events, "session_ended")[-1]["payload"]["session_status"] == "cancelled"


# ---------------------------------------------------------------------------
# T2: hang — a worker that never sends ready is killed and restarted, then the
# task fails on the restart cap with no merge.
# ---------------------------------------------------------------------------


@pytest.mark.slow
def test_t2_never_ready_restarts_to_cap_no_merge(tmp_path, monkeypatch) -> None:
    monkeypatch.setenv("FAKE_MODE", "noready")
    monkeypatch.setenv("CAMBIUM_READY_TIMEOUT_S", "0.1")
    monkeypatch.setattr(supervisor_module, "RESTART_BASE_DELAY_S", 0.01)
    session_dir = tmp_path / "session"
    repo = session_dir / "repo"
    base = _make_repo(repo, {"a.txt": "file a\n"})

    plan = {
        "tasks": [
            _task(session_dir, repo, base, "t-hang", worktree="wt-a", branch="wt-a",
                  target_file="a.txt", marker="// cambium-hang",
                  gate="grep -q '// cambium-hang' a.txt", max_restarts=3),
        ]
    }

    result = asyncio.run(run_plan(session_dir, plan))

    assert result.exit_code != 0
    (task,) = result.results
    assert task.status == "failed"
    assert task.restarts == 3
    assert "max_restarts" in (task.reason or "")

    events = read_events(session_dir)
    assert len(_kinds(events, "restart_scheduled")) == 3
    assert len(_kinds(events, "worker_failed")) == 1
    assert not _kinds(events, "merge_committed")
    assert not _kinds(events, "result")
    tip = subprocess.run(
        ["git", "-C", str(repo), "rev-parse", "main"], check=True, capture_output=True, text=True
    ).stdout.strip()
    assert tip == base  # main never advanced


# ---------------------------------------------------------------------------
# T3: crash mid-edit — generation 1 commits then exits nonzero; the worktree is
# recovered (reset --hard + clean) and generation 2 succeeds. If recovery were
# skipped, the crashed edit would survive and double the marker on main.
# ---------------------------------------------------------------------------


@pytest.mark.slow
def test_t3_crash_mid_edit_recovered_worktree(tmp_path, monkeypatch) -> None:
    monkeypatch.setattr(supervisor_module, "EOF_GRACE_S", 0.05)
    monkeypatch.setattr(supervisor_module, "RESTART_BASE_DELAY_S", 0.01)
    session_dir = tmp_path / "session"
    repo = session_dir / "repo"
    base = _make_repo(repo, {"hello.txt": "hello from the slice\n"})

    plan = {
        "tasks": [
            _task(session_dir, repo, base, "t-crash", worktree="wt-crash", branch="wt-crash",
                  target_file="hello.txt", marker="// cambium-recovered",
                  gate="grep -q '// cambium-recovered' hello.txt", worker=CRASH_WORKER),
        ]
    }

    result = asyncio.run(run_plan(session_dir, plan))

    (task,) = result.results
    assert task.status == "succeeded"
    assert task.restarts >= 1
    assert task.merge_sha is not None

    merged = _show(repo, "main", "hello.txt")
    assert merged.count("// cambium-recovered") == 1  # recovery discarded the crashed edit

    events = read_events(session_dir)
    assert _kinds(events, "restart_scheduled")
    assert _kinds(events, "merge_committed")
    assert _protocol(events, "t-crash")[-5:] == ["init", "ready", "run_task", "result", "exit"]


# ---------------------------------------------------------------------------
# T4: merge race — two workers rewrite the same line; one merge wins, the loser
# gets a merge_failed event (no resolver sub-task in this version).
# ---------------------------------------------------------------------------


@pytest.mark.slow
def test_t4_same_file_race_one_wins_loser_merge_failed(tmp_path, monkeypatch) -> None:
    monkeypatch.setenv("FAKE_MODE", "overwrite")
    session_dir = tmp_path / "session"
    repo = session_dir / "repo"
    base = _make_repo(repo, {"hello.txt": "hello\n// replace-me\n"})

    # Both branches start from the fixed base. Whichever merge publishes first
    # wins; the other rebase conflicts on the same line.
    a = _task(session_dir, repo, base, "t-a", worktree="wt-a", branch="wt-a",
              target_file="hello.txt", marker="// cambium-a", gate="true")
    b = _task(session_dir, repo, base, "t-b", worktree="wt-b", branch="wt-b",
              target_file="hello.txt", marker="// cambium-b", gate="true")

    result = asyncio.run(run_plan(session_dir, {"tasks": [a, b]}))

    assert result.exit_code != 0
    assert {r.status for r in result.results} == {"succeeded", "failed"}
    winner = next(r for r in result.results if r.status == "succeeded")
    loser = next(r for r in result.results if r.status == "failed")
    assert winner.merge_sha is not None
    assert loser.reason == "merge_failed"

    merged = _show(repo, "main", "hello.txt")
    assert merged.count("// cambium-") == 1
    assert ("// cambium-a" in merged) != ("// cambium-b" in merged)

    events = read_events(session_dir)
    assert len(_kinds(events, "merge_committed")) == 1
    failed = _kinds(events, "merge_failed")
    assert len(failed) == 1
    assert failed[0]["payload"]["merge_error"] in ("NonFastForwardError", "MergeConflictError")


# ---------------------------------------------------------------------------
# T5: garbage stdout is tolerated when the protocol still works; a pure-garbage
# worker is killed and the task fails cleanly on the restart cap.
# ---------------------------------------------------------------------------


@pytest.mark.slow
def test_t5_garbage_stdout_tolerated(tmp_path, monkeypatch) -> None:
    monkeypatch.setenv("FAKE_MODE", "garbage")
    session_dir = tmp_path / "session"
    repo = session_dir / "repo"
    base = _make_repo(repo, {"a.txt": "file a\n"})

    plan = {
        "tasks": [
            _task(session_dir, repo, base, "t-garbage", worktree="wt-a", branch="wt-a",
                  target_file="a.txt", marker="// cambium-garbage",
                  gate="grep -q '// cambium-garbage' a.txt"),
        ]
    }

    result = asyncio.run(run_plan(session_dir, plan))

    (task,) = result.results
    assert task.status == "succeeded"
    assert task.merge_sha is not None
    events = read_events(session_dir)
    assert len(_kinds(events, "parse_error")) >= 3
    assert _protocol(events, "t-garbage") == ["init", "ready", "run_task", "result", "exit"]


@pytest.mark.slow
def test_t5_pure_garbage_fails_cleanly_on_cap(tmp_path, monkeypatch) -> None:
    monkeypatch.setenv("FAKE_MODE", "garbage_only")
    # 1.0s (not 0.1s): under parallel/loaded runs the ready-timeout must not
    # fire before the stdout reader has parsed at least one garbage line —
    # the test asserts parse_error events landed. The worker emits one line
    # per 10ms, so ~100 parse errors accrue within the window, still far
    # below the 500-event kill cap.
    monkeypatch.setenv("CAMBIUM_READY_TIMEOUT_S", "1.0")
    monkeypatch.setattr(supervisor_module, "RESTART_BASE_DELAY_S", 0.01)
    session_dir = tmp_path / "session"
    repo = session_dir / "repo"
    base = _make_repo(repo, {"a.txt": "file a\n"})

    plan = {
        "tasks": [
            _task(session_dir, repo, base, "t-noise", worktree="wt-a", branch="wt-a",
                  target_file="a.txt", marker="// cambium-noise",
                  gate="grep -q '// cambium-noise' a.txt", max_restarts=2),
        ]
    }

    result = asyncio.run(run_plan(session_dir, plan))

    (task,) = result.results
    assert task.status == "failed"
    assert task.restarts == 2
    events = read_events(session_dir)
    assert _kinds(events, "parse_error")
    assert len(_kinds(events, "restart_scheduled")) == 2
    assert len(_kinds(events, "worker_failed")) == 1
    assert not _kinds(events, "merge_committed")


@pytest.mark.slow
def test_t5_valid_non_object_stdout_tolerated(tmp_path, monkeypatch) -> None:
    """Valid JSON lines that are not objects are skipped, not fatal (agents.md
    boundary invariants: framing never fails supervision on non-object JSON)."""
    monkeypatch.setenv("FAKE_MODE", "valid_non_object")
    session_dir = tmp_path / "session"
    repo = session_dir / "repo"
    base = _make_repo(repo, {"a.txt": "file a\n"})

    plan = {
        "tasks": [
            _task(session_dir, repo, base, "t-valid-nobj", worktree="wt-a", branch="wt-a",
                  target_file="a.txt", marker="// cambium-valid-nobj",
                  gate="grep -q '// cambium-valid-nobj' a.txt"),
        ]
    }

    result = asyncio.run(run_plan(session_dir, plan))

    (task,) = result.results
    assert task.status == "succeeded"
    assert task.merge_sha is not None
    events = read_events(session_dir)
    # 9 valid non-object lines (3x [list, str, int]): all counted as
    # parse_errors, none fail the task.
    assert len(_kinds(events, "parse_error")) >= 9
    assert _protocol(events, "t-valid-nobj") == ["init", "ready", "run_task", "result", "exit"]


# ---------------------------------------------------------------------------
# T6: SIGTERM mid-run terminates cleanly and the event store flushes.
# ---------------------------------------------------------------------------


@pytest.mark.slow
def test_t6_sigterm_midrun_clean_shutdown_store_integrity(tmp_path) -> None:
    session_dir = tmp_path / "session"
    repo = session_dir / "repo"
    _make_repo(repo, {"a.txt": "file a\n"})

    plan = {
        "tasks": [
            {
                "task_id": "t-slow",
                "task": "never ready",
                "repo": str(repo),
                "worktree_path": str(session_dir / "wt-slow"),
                "branch": "wt-slow",
                    "worker": WORKER,
                    "gate": "true",
                    "provider_env_keys": ["FAKE_MODE"],
                    "resource_thresholds": TEST_RESOURCE_THRESHOLDS,
                }
        ]
    }
    plan_path = tmp_path / "plan.json"
    plan_path.write_text(json.dumps(plan))

    env = dict(os.environ)
    env["FAKE_MODE"] = "noready"
    env["PYTHONPATH"] = str(ROOT / "src")
    proc = subprocess.Popen(
        [sys.executable, "-m", "cambium.supervisor",
         "--plan", str(plan_path), "--session-dir", str(session_dir)],
        env=env, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
    )
    try:
        events_db = session_dir / ".cambium" / "events.db"
        deadline = time.monotonic() + 30
        while True:
            init_seen = False
            if events_db.is_file():
                try:
                    with sqlite3.connect(events_db) as connection:
                        init_seen = connection.execute(
                            "SELECT 1 FROM events WHERE kind = ? AND task_id = ? LIMIT 1",
                            ("init", "t-slow"),
                        ).fetchone() is not None
                except sqlite3.OperationalError:
                    pass
            if init_seen:
                break
            if time.monotonic() >= deadline:
                raise AssertionError("supervisor did not emit init event before timeout")
            time.sleep(0.01)

        proc.send_signal(signal.SIGTERM)
        out, err = proc.communicate(timeout=30)
    finally:
        if proc.poll() is None:
            proc.kill()
        proc.wait(timeout=30)

    assert proc.returncode == 130, f"returncode={proc.returncode} out={out} err={err}"

    events = read_events(session_dir)  # reopens the DB; integrity must hold
    kinds = [e["kind"] for e in events]
    assert "task_assigned" in kinds
    assert "spawned" in kinds
    assert "init" in kinds
    assert "session_ended" in kinds
    assert kinds[-1] == "session_ended"


@pytest.mark.slow
def test_restart_reconciles_publish_gap_and_preserves_dirty_staging(tmp_path) -> None:
    session_dir = tmp_path / "session"
    repo = session_dir / "repo"
    base = _make_repo(repo, {"a.txt": "file a\n"})
    task_id = "t-publish-gap"
    worker_tree = session_dir / "wt-gap"
    subprocess.run(
        ["git", "-C", str(repo), "worktree", "add", "-b", "wt-gap", str(worker_tree), base],
        check=True, capture_output=True,
    )
    (worker_tree / "precrash.txt").write_text("committed\n")
    subprocess.run(["git", "-C", str(worker_tree), "add", "precrash.txt"], check=True)
    subprocess.run(
        ["git", "-C", str(worker_tree), "commit", "-m", "precrash"],
        check=True, capture_output=True,
    )
    task_key = hashlib.sha256(task_id.encode()).hexdigest()[:16]
    staging = session_dir / ".cambium" / "merge-wt" / f"task-{task_key}"
    seq = MergeSequencer(task_id=task_id, session_dir=session_dir)
    staged = seq.prepare_staging(repo, staging, "wt-gap", "main")
    secret_name = "secret-kill-window.txt"
    (staging / secret_name).write_text("secret kill-window content")
    seq.publish_merge(repo, staged, base)  # simulated kill before merge_committed

    plan = {
        "tasks": [
            _task(
                session_dir, repo, base, task_id, worktree="wt-gap", branch="wt-gap",
                target_file="a.txt", marker="// after-restart",
                gate="grep -q '// after-restart' a.txt",
            )
        ]
    }
    result = asyncio.run(run_plan(session_dir, plan))
    assert result.exit_code == 0
    events = read_events(session_dir)
    reconciled = _kinds(events, "merge_reconciled")
    quarantined = _kinds(events, "merge_staging_quarantined")
    assert reconciled and reconciled[0]["payload"]["new"] == staged
    assert quarantined
    quarantine_id = quarantined[0]["payload"]["quarantine_id"]
    artifact = session_dir / ".cambium" / "quarantine" / quarantine_id
    assert (artifact / secret_name).read_text() == "secret kill-window content"
    payloads = json.dumps([event["payload"] for event in quarantined])
    assert secret_name not in payloads
    assert "secret kill-window content" not in payloads


@pytest.mark.slow
def test_restart_reconciles_publish_gap_with_clean_staging_without_rerun(
    tmp_path, monkeypatch
) -> None:
    credentials = {
        "CAMBIUM_TEST_MERGE": "merge",
        "CAMBIUM_TEST_RESULT": "result",
        "CAMBIUM_TEST_SUCCEEDED": "succeeded",
    }
    for name, value in credentials.items():
        monkeypatch.setenv(name, value)
    session_dir = tmp_path / "session"
    repo = session_dir / "repo"
    base = _make_repo(repo, {"a.txt": "file a\n"})
    task_id = "t-clean-publish-gap"
    worker_tree = session_dir / "wt-clean-gap"
    subprocess.run(
        ["git", "-C", str(repo), "worktree", "add", "-b", "wt-clean-gap", str(worker_tree), base],
        check=True, capture_output=True,
    )
    (worker_tree / "precrash.txt").write_text("committed\n")
    subprocess.run(["git", "-C", str(worker_tree), "add", "precrash.txt"], check=True)
    subprocess.run(
        ["git", "-C", str(worker_tree), "commit", "-m", "precrash"],
        check=True, capture_output=True,
    )
    task_key = hashlib.sha256(task_id.encode()).hexdigest()[:16]
    staging = session_dir / ".cambium" / "merge-wt" / f"task-{task_key}"
    seq = MergeSequencer(task_id=task_id, session_dir=session_dir)
    staged = seq.prepare_staging(repo, staging, "wt-clean-gap", "main")
    seq.publish_merge(repo, staged, base)

    plan = {
        "tasks": [
            _task(
                session_dir, repo, base, task_id, worktree="wt-clean-gap", branch="wt-clean-gap",
                target_file="a.txt", marker="// must-not-rerun",
                gate="grep -q '// must-not-rerun' a.txt",
                provider_env_keys=list(credentials),
            )
        ]
    }
    result = asyncio.run(run_plan(session_dir, plan))
    events = read_events(session_dir)

    assert result.exit_code == 0
    assert result.results[0].merge_sha == staged
    assert not staging.exists()
    assert not _kinds(events, "spawned")
    committed = _kinds(events, "merge_committed")
    assert len(committed) == 1
    assert committed[0]["payload"]["reason"] == "recovered-ref-advance"
    reconciled = _kinds(events, "merge_reconciled")
    assert len(reconciled) == 1
    assert reconciled[0]["task_id"] == task_id
    assert reconciled[0]["payload"]["new"] == staged


@pytest.mark.slow
def test_merge_reconciled_observer_failure_is_fatal(tmp_path) -> None:
    session_dir = tmp_path / "session"
    repo = session_dir / "repo"
    base = _make_repo(repo, {"a.txt": "file a\n"})
    task_id = "t-reconcile-observer-failure"
    worker_tree = session_dir / "wt-reconcile-observer-failure"
    subprocess.run(
        ["git", "-C", str(repo), "worktree", "add", "-b", "wt-reconcile-observer-failure",
         str(worker_tree), base],
        check=True, capture_output=True,
    )
    (worker_tree / "precrash.txt").write_text("committed\n")
    subprocess.run(["git", "-C", str(worker_tree), "add", "precrash.txt"], check=True)
    subprocess.run(
        ["git", "-C", str(worker_tree), "commit", "-m", "precrash"],
        check=True, capture_output=True,
    )
    task_key = hashlib.sha256(task_id.encode()).hexdigest()[:16]
    staging = session_dir / ".cambium" / "merge-wt" / f"task-{task_key}"
    seq = MergeSequencer(task_id=task_id, session_dir=session_dir)
    staged = seq.prepare_staging(repo, staging, "wt-reconcile-observer-failure", "main")
    seq.publish_merge(repo, staged, base)
    plan = {
        "tasks": [
            _task(
                session_dir, repo, base, task_id, worktree="wt-reconcile-observer-failure",
                branch="wt-reconcile-observer-failure", target_file="a.txt",
                marker="// must-not-run", gate="grep -q '// must-not-run' a.txt",
            )
        ]
    }

    def fail_on_reconciliation(event: dict) -> None:
        if event["kind"] == "merge_reconciled":
            raise RuntimeError("merge_reconciled observer failed")

    with pytest.raises(RuntimeError, match="merge_reconciled observer failed"):
        asyncio.run(run_plan(session_dir, plan, on_event=fail_on_reconciliation))

    events = read_events(session_dir)
    assert _kinds(events, "merge_reconciled")
    assert not _kinds(events, "spawned")


@pytest.mark.slow
def test_restart_after_lost_reconciliation_event_does_not_execute_twice(
    tmp_path, monkeypatch,
) -> None:
    session_dir = tmp_path / "session"
    repo = session_dir / "repo"
    base = _make_repo(repo, {"a.txt": "file a\n"})
    task_id = "t-lost-reconciliation"
    worker_tree = session_dir / "wt-lost-reconciliation"
    subprocess.run(
        ["git", "-C", str(repo), "worktree", "add", "-b", "wt-lost-reconciliation",
         str(worker_tree), base],
        check=True, capture_output=True,
    )
    (worker_tree / "precrash.txt").write_text("committed\n")
    subprocess.run(["git", "-C", str(worker_tree), "add", "precrash.txt"], check=True)
    subprocess.run(
        ["git", "-C", str(worker_tree), "commit", "-m", "precrash"],
        check=True, capture_output=True,
    )
    task_key = hashlib.sha256(task_id.encode()).hexdigest()[:16]
    staging = session_dir / ".cambium" / "merge-wt" / f"task-{task_key}"
    seq = MergeSequencer(task_id=task_id, session_dir=session_dir)
    staged = seq.prepare_staging(repo, staging, "wt-lost-reconciliation", "main")
    (staging / "recovery-evidence.bin").write_bytes(b"preserve this evidence")
    seq.publish_merge(repo, staged, base)
    plan = {
        "tasks": [
            _task(
                session_dir, repo, base, task_id, worktree="wt-lost-reconciliation",
                branch="wt-lost-reconciliation", target_file="a.txt",
                marker="// must-never-run", gate="grep -q '// must-never-run' a.txt",
            )
        ]
    }
    original_emit = supervisor_module._Runtime.emit

    async def lose_reconciliation(self, kind, **kwargs):
        if kind == "merge_reconciled":
            return None
        return await original_emit(self, kind, **kwargs)

    monkeypatch.setattr(supervisor_module._Runtime, "emit", lose_reconciliation)
    first = asyncio.run(run_plan(session_dir, plan))
    assert first.exit_code == 0
    assert not staging.exists()
    first_events = read_events(session_dir)
    assert not _kinds(first_events, "spawned")
    assert not _kinds(first_events, "merge_reconciled")
    committed = _kinds(first_events, "merge_committed")
    assert len(committed) == 1
    quarantined = _kinds(first_events, "merge_staging_quarantined")
    assert len(quarantined) == 1
    assert committed[0]["seq"] < quarantined[0]["seq"]
    artifact = (
        session_dir / ".cambium" / "quarantine"
        / quarantined[0]["payload"]["quarantine_id"]
    )
    assert (artifact / "recovery-evidence.bin").read_bytes() == b"preserve this evidence"
    expired = time.time_ns() - 8 * 24 * 60 * 60 * 1_000_000_000
    os.utime(artifact, ns=(expired, expired))
    commits_after_recovery = subprocess.run(
        ["git", "-C", str(repo), "rev-list", "--count", "refs/heads/main"],
        check=True, capture_output=True, text=True,
    ).stdout.strip()

    second = asyncio.run(run_plan(session_dir, plan))
    events = read_events(session_dir)

    assert second.exit_code == 0
    assert second.results[0].merge_sha == staged
    assert not _kinds(events, "spawned")
    assert len(_kinds(events, "merge_committed")) == 1
    assert subprocess.run(
        ["git", "-C", str(repo), "rev-list", "--count", "refs/heads/main"],
        check=True, capture_output=True, text=True,
    ).stdout.strip() == commits_after_recovery


@pytest.mark.slow
def test_merge_committed_persistence_failure_retains_staging(tmp_path, monkeypatch) -> None:
    session_dir = tmp_path / "session"
    repo = session_dir / "repo"
    base = _make_repo(repo, {"a.txt": "file a\n"})
    task_id = "t-store-failure"
    original_append = EventStore.append

    def fail_merge_committed(self, event):
        if event.get("kind") == "merge_committed":
            raise RuntimeError("injected merge_committed persistence failure")
        return original_append(self, event)

    monkeypatch.setattr(EventStore, "append", fail_merge_committed)
    plan = {
        "tasks": [
            _task(
                session_dir, repo, base, task_id, worktree="wt-store", branch="wt-store",
                target_file="a.txt", marker="// published-before-store-failure",
                gate="grep -q '// published-before-store-failure' a.txt",
            )
        ]
    }

    result = asyncio.run(run_plan(session_dir, plan))

    task_key = hashlib.sha256(task_id.encode()).hexdigest()[:16]
    staging = session_dir / ".cambium" / "merge-wt" / f"task-{task_key}"
    events = read_events(session_dir)

    assert result.exit_code != 0
    assert result.results[0].status == "failed"
    assert not _kinds(events, "merge_committed")
    assert _kinds(events, "merge_failed")
    assert staging.exists()
    assert (staging / "a.txt").read_text().endswith("// published-before-store-failure\n")
    assert _show(repo, "main", "a.txt").endswith("// published-before-store-failure\n")
    refs = subprocess.run(
        ["git", "-C", str(repo), "for-each-ref", "--format=%(refname)",
         f"refs/cambium/staging/{task_key}-*"],
        check=True, capture_output=True, text=True,
    ).stdout.splitlines()
    assert len(refs) == 1


@pytest.mark.slow
def test_resource_gate_fail_closed_preflight_creates_no_worktree(tmp_path) -> None:
    """An impossible memory threshold refuses admission before worktree creation."""
    session_dir = tmp_path / "session"
    repo = session_dir / "repo"
    base = _make_repo(repo, {"a.txt": "file a\n"})
    task = _task(
        session_dir,
        repo,
        base,
        "t-denied",
        worktree="wt-denied",
        branch="wt-denied",
        target_file="a.txt",
        marker="// denied",
        gate="make --version",
    )
    task["resource_thresholds"] = {
        "mem_available_frac": 1.0,
        "load1_per_cpu": 1_000_000.0,
        "disk_free": 0,
    }

    result = asyncio.run(
        run_plan(
            session_dir,
            {"tasks": [task]},
            resource_thresholds={
                "mem_available_frac": 0.0,
                "load1_per_cpu": 1_000_000.0,
                "disk_free": 0,
            },
        )
    )

    assert result.exit_code == 1
    assert result.results[0].exit_code == 126
    assert result.results[0].reason == "resource_denied"
    assert not Path(task["worktree_path"]).exists()
    denied = _kinds(read_events(session_dir), "resource_denied")
    assert len(denied) == 1
    assert denied[0]["payload"]["resource_denied"] is True
    assert any("mem_available_frac" in reason for reason in denied[0]["payload"]["reasons"])


@pytest.mark.slow
def test_resource_preflight_is_opt_in_and_skipped_without_thresholds(tmp_path, monkeypatch) -> None:
    """Without configured thresholds the health pre-flight is skipped entirely.

    The host health probe is an optional fail-closed pre-flight: callers that
    configure ``resource_thresholds`` get admission denial, but the default
    (no thresholds) must not make every task fail on a loaded host. The
    semaphore remains the always-on admission boundary.
    """
    import cambium.supervisor as supervisor_module

    forced_deny = (False, ["forced health denial"])
    monkeypatch.setattr(supervisor_module, "can_run_heavy", lambda thresholds: forced_deny)

    session_dir = tmp_path / "session"
    repo = session_dir / "repo"
    base = _make_repo(repo, {"a.txt": "file a\n"})
    task = _task(
        session_dir,
        repo,
        base,
        "t-optin",
        worktree="wt-optin",
        branch="wt-optin",
        target_file="a.txt",
        marker="// optin",
        gate="make --version",
        resource_thresholds=None,
    )

    result = asyncio.run(
        run_plan(session_dir, {"tasks": [task]}, resource_thresholds=None)
    )

    assert result.exit_code == 0
    assert result.results[0].exit_code == 0
    assert _show(repo, "main", "a.txt") == "file a\n// optin\n"
    assert not _kinds(read_events(session_dir), "resource_denied")

    denied_session = tmp_path / "denied-session"
    denied_task = dict(
        task,
        task_id="t-denied2",
        worktree_path=str(denied_session / "wt-denied2"),
        branch="wt-denied2",
    )
    denied_task["resource_thresholds"] = {
        "mem_available_frac": 1.0,
        "load1_per_cpu": 1_000_000.0,
        "disk_free": 0,
    }
    denied = asyncio.run(
        run_plan(
            denied_session,
            {"tasks": [denied_task]},
            resource_thresholds=None,
        )
    )
    assert denied.results[0].exit_code == 126
    assert denied.results[0].reason == "resource_denied"
