"""Adversarial scenarios for supervisor critical hardening."""

from __future__ import annotations

import asyncio
import json
import os
import shlex
import subprocess
import sys
import time
from pathlib import Path

import pytest

import cambium.supervisor as supervisor_module
from cambium.fencing import read_generation, write_generation
from cambium.supervisor import DuplicateTaskIDError, read_events, run_plan, run_session

ROOT = Path(__file__).resolve().parents[2]
ENV_PROBE_WORKER = str(ROOT / "tests" / "fixtures" / "env_probe_worker.py")
NOREAD_WORKER = str(ROOT / "tests" / "fixtures" / "noread_worker.py")
EOF_QUIET_WORKER = str(ROOT / "tests" / "fixtures" / "eof_quiet_worker.py")
EOF_STALE_PONG_WORKER = str(ROOT / "tests" / "fixtures" / "eof_pong_worker.py")
GATE_DESCENDANT = str(ROOT / "tests" / "fixtures" / "gate_descendant.py")
TOO_LONG_WORKER = str(ROOT / "tests" / "fixtures" / "too_long_worker.py")
CRASH_ONCE_WORKER = str(ROOT / "tests" / "fixtures" / "crash_once_worker.py")
FAKE_WORKER = str(ROOT / "scripts" / "fake_worker.py")

PROBE_ENV = {
    "TEST_API_KEY_DEMO": "sk-demo-value",
    "TEST_DB_PWD_DEMO": "demo-password",
    "TEST_DATABASE_URL_DEMO": "postgres://demo:password@example/db",
    "CAMBIUM_TEST_PROVIDER_KEY": "authorized-provider-value",
}


def _make_repo(repo: Path, files: dict[str, str]) -> str:
    subprocess.run(["git", "init", "-b", "main", str(repo)], check=True, capture_output=True)
    for key, value in (
        ("user.name", "hardening-test"),
        ("user.email", "hardening@test"),
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


def _make_scratch(repo: Path) -> str:
    return _make_repo(repo, {"hello.txt": "hello from hardening\n"})


def _task(
    session_dir: Path,
    repo: Path,
    base: str,
    task_id: str,
    *,
    worker: str,
    gate: str,
    provider_env_keys: list[str] | None = None,
    **extra: object,
) -> dict[str, object]:
    task: dict[str, object] = {
        "task_id": task_id,
        "task": "edit hello.txt",
        "repo": str(repo),
        "worktree_path": str(session_dir / f"wt-{task_id}"),
        "branch": f"wt-{task_id}",
        "worker": worker,
        "target_file": "hello.txt",
        "marker": f"// {task_id}",
        "write_marker": True,
        "gate": gate,
        "base_commit": base,
        "provider_env_keys": provider_env_keys or [],
    }
    task.update(extra)
    return task


def _kinds(events: list[dict], kind: str) -> list[dict]:
    return [event for event in events if event["kind"] == kind]


def _install_env_hooks(repo: Path, worker_report: Path, merge_report: Path) -> None:
    hooks = repo / ".git" / "hooks"
    pre_commit = (
        "#!/bin/sh\n"
        "if [ -n \"${CAMBIUM_TEST_PROVIDER_KEY:-}\" ]; then provider=present; "
        "else provider=absent; fi\n"
        "if [ -n \"${TEST_API_KEY_DEMO:-}\" ]; then unrelated=present; "
        "else unrelated=absent; fi\n"
        f"printf '%s %s\\n' \"$provider\" \"$unrelated\" > {shlex.quote(str(worker_report))}\n"
    )
    pre_rebase = (
        "#!/bin/sh\n"
        "if [ -n \"${CAMBIUM_TEST_PROVIDER_KEY:-}\" ]; then provider=present; "
        "else provider=absent; fi\n"
        "if [ -n \"${TEST_API_KEY_DEMO:-}\" ]; then unrelated=present; "
        "else unrelated=absent; fi\n"
        f"printf '%s %s\\n' \"$provider\" \"$unrelated\" > {shlex.quote(str(merge_report))}\n"
    )
    for name, content in (
        ("pre-commit", pre_commit),
        ("pre-rebase", pre_rebase),
        ("post-checkout", pre_rebase),
    ):
        path = hooks / name
        path.write_text(content, encoding="utf-8")
        path.chmod(0o755)


def _wait_pid_gone(pid: int, timeout_s: float = 2.0) -> None:
    deadline = time.monotonic() + timeout_s
    while time.monotonic() < deadline:
        try:
            os.kill(pid, 0)
        except ProcessLookupError:
            return
        time.sleep(0.02)
    pytest.fail(f"process {pid} survived process-group cleanup")


def test_strict_env_worker_gate_and_merge_hooks_allow_only_named_provider_key(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    for name, value in PROBE_ENV.items():
        monkeypatch.setenv(name, value)
    session_dir = tmp_path / "session"
    repo = session_dir / "repo"
    base = _make_repo(repo, {"hello.txt": "hello\n"})
    worker_report = tmp_path / "worker-hook.txt"
    merge_report = tmp_path / "merge-hook.txt"
    _install_env_hooks(repo, worker_report, merge_report)

    gate = (
        'test -z "${CAMBIUM_TEST_PROVIDER_KEY:-}" '
        '&& test -z "${TEST_API_KEY_DEMO:-}" '
        '&& test -z "${TEST_DB_PWD_DEMO:-}" '
        '&& test -z "${TEST_DATABASE_URL_DEMO:-}" '
        "&& grep -q '// t-env' hello.txt"
    )
    plan = {
        "tasks": [
            _task(
                session_dir,
                repo,
                base,
                "t-env",
                worker=ENV_PROBE_WORKER,
                gate=gate,
                provider_env_keys=["CAMBIUM_TEST_PROVIDER_KEY"],
                marker="// t-env",
            )
        ]
    }

    result = asyncio.run(run_plan(session_dir, plan))

    assert result.exit_code == 0
    assert result.results[0].status == "succeeded"
    assert worker_report.read_text(encoding="utf-8").strip() == "present absent"
    assert merge_report.read_text(encoding="utf-8").strip() == "absent absent"


def test_stdin_write_deadline_kills_non_reader_group(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setenv("CAMBIUM_WRITE_TIMEOUT_S", "0.1")
    session_dir = tmp_path / "session"
    repo = session_dir / "repo"
    base = _make_repo(repo, {"hello.txt": "hello\n"})
    task = _task(
        session_dir,
        repo,
        base,
        "t-noread",
        worker=NOREAD_WORKER,
        gate="true",
        task="x" * 300_000,
        max_restarts=0,
        max_wall_s=5.0,
    )

    started = time.monotonic()
    result = asyncio.run(run_plan(session_dir, {"tasks": [task]}))
    elapsed = time.monotonic() - started

    assert elapsed < 4.0
    assert result.results[0].status == "failed"
    assert "stdin" in (result.results[0].reason or "")
    events = read_events(session_dir)
    assert any(
        event["kind"] == "protocol"
        and "write failed" in event["payload"].get("note", "")
        for event in events
    )


def test_gate_timeout_kills_descendant_process_group(tmp_path: Path) -> None:
    session_dir = tmp_path / "session"
    repo = session_dir / "repo"
    base = _make_repo(repo, {"hello.txt": "hello\n"})
    pid_file = tmp_path / "gate-child.pid"
    gate = (
        f"{shlex.quote(sys.executable)} {shlex.quote(GATE_DESCENDANT)} "
        f"{shlex.quote(str(pid_file))}"
    )
    task = _task(
        session_dir,
        repo,
        base,
        "t-gate-tree",
        worker=FAKE_WORKER,
        gate=gate,
        gate_timeout_s=0.2,
        max_wall_s=5.0,
        marker="// t-gate-tree",
    )

    result = asyncio.run(run_plan(session_dir, {"tasks": [task]}))

    assert result.results[0].status == "failed"
    assert result.results[0].reason == "gate_failed"
    child_pid = int(pid_file.read_text(encoding="ascii"))
    _wait_pid_gone(child_pid)
    assert any(event["payload"].get("timed_out") for event in read_events(session_dir)
               if event["kind"] == "gate")


def test_generation_seven_advances_and_never_rolls_back_on_restart(tmp_path: Path) -> None:
    session_dir = tmp_path / "session"
    repo = session_dir / "repo"
    base = _make_repo(repo, {"hello.txt": "hello\n"})
    worktree = session_dir / "wt-t-generation"
    subprocess.run(
        ["git", "-C", str(repo), "worktree", "add", "-b", "wt-t-generation",
         str(worktree), base],
        check=True,
        capture_output=True,
    )
    write_generation(worktree, 7)
    task = _task(
        session_dir,
        repo,
        base,
        "t-generation",
        worker=CRASH_ONCE_WORKER,
        gate="true",
        max_restarts=1,
        marker="// t-generation",
    )

    result = asyncio.run(run_plan(session_dir, {"tasks": [task]}))

    assert result.results[0].status == "succeeded"
    assert result.results[0].restarts == 1
    assert read_generation(worktree) == 9
    generations = [
        event["generation"]
        for event in read_events(session_dir)
        if event["kind"] == "init"
    ]
    assert generations == [8, 9]
    assert 7 not in generations


@pytest.mark.parametrize("worker", [EOF_QUIET_WORKER, EOF_STALE_PONG_WORKER])
def test_eof_requires_exact_fresh_pong_and_kills_stale_or_silent_worker(
    tmp_path: Path, monkeypatch, worker: str
) -> None:
    monkeypatch.setattr(supervisor_module, "EOF_GRACE_S", 0.05)
    monkeypatch.setattr(supervisor_module, "PONG_DEADLINE_S", 0.2)
    session_dir = tmp_path / "session"
    repo = session_dir / "repo"
    base = _make_repo(repo, {"hello.txt": "hello\n"})
    task = _task(
        session_dir,
        repo,
        base,
        "t-eof",
        worker=worker,
        gate="true",
        max_restarts=0,
        max_wall_s=3.0,
    )

    result = asyncio.run(run_plan(session_dir, {"tasks": [task]}))

    assert result.results[0].status == "failed"
    events = read_events(session_dir)
    assert _kinds(events, "ping")
    assert not _kinds(events, "pong")
    assert any(
        event["kind"] == "protocol"
        and "correlated pong" in event["payload"].get("note", "")
        for event in events
    )


def test_duplicate_task_id_is_rejected_before_store_or_spawn(tmp_path: Path, monkeypatch) -> None:
    def fail_spawn(*args, **kwargs):
        raise AssertionError("duplicate plan spawned a subprocess")

    monkeypatch.setattr(asyncio, "create_subprocess_exec", fail_spawn)
    plan = {"tasks": [{"task_id": "duplicate"}, {"task_id": "duplicate"}]}

    with pytest.raises(DuplicateTaskIDError, match="duplicate"):
        asyncio.run(run_plan(tmp_path / "session", plan))
    assert not (tmp_path / "session" / ".cambium").exists()


def _slice_spec(session_dir: Path, worker: str) -> dict[str, object]:
    return {
        "task_id": "slice-too-long",
        "worker": worker,
        "scratch_repo": str(session_dir / "scratch"),
        "worktree_path": str(session_dir / "wt"),
        "branch": "wt-slice-too-long",
        "target_file": "hello.txt",
        "marker": "// slice-too-long",
        "write_marker": True,
        "gate": "true",
        "provider_env_keys": [],
    }


def test_oversized_stdout_line_fails_slice_reader(tmp_path: Path) -> None:
    session_dir = tmp_path / "session"
    _make_scratch(session_dir / "scratch")

    result = asyncio.run(run_session(session_dir, _slice_spec(session_dir, TOO_LONG_WORKER)))

    assert result.status == "failed"
    assert result.exit_code == 1
    events = [
        json.loads(line)
        for line in (session_dir / ".cambium" / "events.jsonl").read_text().splitlines()
    ]
    assert any(
        event["kind"] == "protocol" and event["payload"].get("note") == "MessageTooLong"
        for event in events
    )


def test_oversized_stdout_line_fails_custos_reader(tmp_path: Path) -> None:
    session_dir = tmp_path / "session"
    repo = session_dir / "repo"
    base = _make_repo(repo, {"hello.txt": "hello\n"})
    task = _task(
        session_dir,
        repo,
        base,
        "t-too-long",
        worker=TOO_LONG_WORKER,
        gate="true",
        max_restarts=0,
    )

    result = asyncio.run(run_plan(session_dir, {"tasks": [task]}))

    assert result.results[0].status == "failed"
    assert "message_too_long" in (result.results[0].reason or "")
    assert any(
        event["kind"] == "protocol" and event["payload"].get("note") == "MessageTooLong"
        for event in read_events(session_dir)
    )
