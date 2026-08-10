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
import json
import os
import signal
import subprocess
import sys
import time
from pathlib import Path

from cambium.supervisor import read_events, run_plan

ROOT = Path(__file__).resolve().parents[2]
WORKER = str(ROOT / "scripts" / "fake_worker.py")
CRASH_WORKER = str(ROOT / "tests" / "fixtures" / "crash_worker.py")


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
    worktree: str, branch: str, target_file: str, marker: str, gate: str,
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
    }
    spec.update(extra)
    return spec


def _protocol(events: list[dict], task_id: str) -> list[str]:
    wanted = {"init", "ready", "run_task", "result", "exit"}
    return [e["kind"] for e in events if e["task_id"] == task_id and e["kind"] in wanted]


def _kinds(events: list[dict], kind: str) -> list[dict]:
    return [e for e in events if e["kind"] == kind]


# ---------------------------------------------------------------------------
# T1: fan-out — three workers, disjoint files, all merged, coherent log.
# ---------------------------------------------------------------------------


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

    events = read_events(session_dir)
    assert len(_kinds(events, "merge_committed")) == 3
    for tid in ("t-a", "t-b", "t-c"):
        assert _protocol(events, tid) == ["init", "ready", "run_task", "result", "exit"]
    assert events[-1]["kind"] == "session_ended"


# ---------------------------------------------------------------------------
# T2: hang — a worker that never sends ready is killed and restarted, then the
# task fails on the restart cap with no merge.
# ---------------------------------------------------------------------------


def test_t2_never_ready_restarts_to_cap_no_merge(tmp_path, monkeypatch) -> None:
    monkeypatch.setenv("FAKE_MODE", "noready")
    monkeypatch.setenv("CAMBIUM_READY_TIMEOUT_S", "2")
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


def test_t3_crash_mid_edit_recovered_worktree(tmp_path) -> None:
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


def test_t5_pure_garbage_fails_cleanly_on_cap(tmp_path, monkeypatch) -> None:
    monkeypatch.setenv("FAKE_MODE", "garbage_only")
    monkeypatch.setenv("CAMBIUM_READY_TIMEOUT_S", "2")
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


# ---------------------------------------------------------------------------
# T6: SIGTERM mid-run terminates cleanly and the event store flushes.
# ---------------------------------------------------------------------------


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
    time.sleep(1.2)
    proc.send_signal(signal.SIGTERM)
    out, err = proc.communicate(timeout=30)

    assert proc.returncode == 130, f"returncode={proc.returncode} out={out} err={err}"

    events = read_events(session_dir)  # reopens the DB; integrity must hold
    kinds = [e["kind"] for e in events]
    assert "task_assigned" in kinds
    assert "spawned" in kinds
    assert "init" in kinds
    assert "session_ended" in kinds
    assert kinds[-1] == "session_ended"
