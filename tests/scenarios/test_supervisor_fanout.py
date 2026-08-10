"""Custos multi-worker supervisor scenarios (T1-T6).

Real supervisor (``cambium.supervisor.run_plan``) driving real worker
subprocesses and real git operations. No mocks, no network. The worker
runtime uses ``scripts/fake_worker.py`` as the fallback for the missing
``cambium.worker`` module (skipped via importorskip when present), plus a
dedicated in-place crash fixture for the recovery scenario.

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
import sqlite3
import subprocess
import sys
import time
from pathlib import Path

from cambium import supervisor
from cambium.supervisor import read_events, run_plan

ROOT = Path(__file__).resolve().parents[2]
WORKER = str(ROOT / "scripts" / "fake_worker.py")
CRASH_WORKER = str(ROOT / "tests" / "fixtures" / "crash_worker.py")
ENV_WORKER = str(ROOT / "tests" / "fixtures" / "env_worker.py")


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

    # The loser is task 0 so it edits the '// replace-me' line first (branch
    # based on base); its gate sleeps, letting the winner's merge land first.
    # The loser then rebases onto the winner's tip and conflicts on the same
    # line -> merge_failed. Deterministic.
    loser = _task(session_dir, repo, base, "t-loser", worktree="wt-loser", branch="wt-loser",
                  target_file="hello.txt", marker="// cambium-loser",
                  gate="sleep 2.5 && grep -q '// cambium-loser' hello.txt")
    winner = _task(session_dir, repo, base, "t-winner", worktree="wt-winner", branch="wt-winner",
                   target_file="hello.txt", marker="// cambium-winner",
                   gate="sleep 1 && grep -q '// cambium-winner' hello.txt")

    result = asyncio.run(run_plan(session_dir, {"tasks": [loser, winner]}))

    assert result.exit_code != 0  # the loser failed
    by_id = {r.task_id: r for r in result.results}
    assert by_id["t-winner"].status == "succeeded"
    assert by_id["t-loser"].status == "failed"
    assert by_id["t-loser"].reason == "merge_failed"

    merged = _show(repo, "main", "hello.txt")
    assert "// cambium-winner" in merged
    assert "// cambium-loser" not in merged

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


# ---------------------------------------------------------------------------
# T7: env confinement — the actually spawned worker env carries only the
# authorized canonical provider keys, and supervisor git hooks see no key.
# ---------------------------------------------------------------------------


def test_t7_spawned_worker_env_has_only_authorized_provider_keys(tmp_path, monkeypatch) -> None:
    monkeypatch.setenv("CAMBIUM_PROVIDER_OPENAI_API_KEY", "authorized-secret")
    monkeypatch.setenv("OPENAI_API_KEY", "generic-secret")
    monkeypatch.setenv("CAMBIUM_PROVIDER_bad_API_KEY", "noncanonical-secret")
    dump_path = tmp_path / "worker-env.json"
    monkeypatch.setenv("ENV_DUMP_PATH", str(dump_path))

    session_dir = tmp_path / "session"
    repo = session_dir / "repo"
    base = _make_repo(repo, {"a.txt": "file a\n"})
    plan = {
        "tasks": [
            _task(session_dir, repo, base, "t-env", worktree="wt-env", branch="wt-env",
                  target_file="a.txt", marker="// cambium-env",
                  gate="grep -q '// cambium-env' a.txt", worker=ENV_WORKER),
        ]
    }

    result = asyncio.run(run_plan(session_dir, plan))

    (task,) = result.results
    assert task.status == "succeeded"
    spawned_env = json.loads(dump_path.read_text(encoding="utf-8"))
    assert spawned_env["CAMBIUM_PROVIDER_OPENAI_API_KEY"] == "authorized-secret"
    assert "OPENAI_API_KEY" not in spawned_env
    assert "CAMBIUM_PROVIDER_bad_API_KEY" not in spawned_env
    assert spawned_env["CAMBIUM_TASK_ID"] == "t-env"
    assert spawned_env["CAMBIUM_GENERATION"] == "1"


def test_t8_supervisor_git_sync_post_checkout_hook_sees_no_provider_key(
    tmp_path, monkeypatch
) -> None:
    """A post-checkout hook executed by ``_Runtime._git_sync`` git operations
    must not see any provider credential in its env."""
    monkeypatch.setenv("CAMBIUM_PROVIDER_OPENAI_API_KEY", "hook-secret")
    session_dir = tmp_path / "session"
    repo = session_dir / "repo"
    base = _make_repo(repo, {"a.txt": "file a\n"})

    hook_dump = tmp_path / "hook-env.jsonl"
    hook = repo / ".git" / "hooks" / "post-checkout"
    hook.write_text(
        f"#!{sys.executable}\n"
        "import json, os\n"
        f"with open({str(hook_dump)!r}, 'a', encoding='utf-8') as f:\n"
        "    f.write(json.dumps(dict(os.environ)) + chr(10))\n"
    )
    hook.chmod(0o755)

    runtime = supervisor._Runtime.__new__(supervisor._Runtime)
    worktree = session_dir / "wt-hook"
    runtime._git_sync(
        repo, ("worktree", "add", "-b", "wt-hook", str(worktree), base), check=True
    )

    records = [
        json.loads(line)
        for line in hook_dump.read_text(encoding="utf-8").splitlines()
        if line.strip()
    ]
    assert records, "the post-checkout hook never ran"
    for record in records:
        assert "CAMBIUM_PROVIDER_OPENAI_API_KEY" not in record
        assert "hook-secret" not in json.dumps(record)
