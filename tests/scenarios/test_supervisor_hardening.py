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
from cambium.fencing import read_generation, validate_worker_generation, write_generation
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
        event["kind"] == "timeout"
        and event["payload"].get("phase") == "stdin"
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


def test_generation_survives_crash_after_worktree_clean(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    session_dir = tmp_path / "session"
    repo = session_dir / "repo"
    base = _make_repo(repo, {"hello.txt": "hello\n"})
    worktree = session_dir / "wt-t-crash-window"
    subprocess.run(
        ["git", "-C", str(repo), "worktree", "add", "-b", "wt-t-crash-window",
         str(worktree), base],
        check=True,
        capture_output=True,
    )
    write_generation(worktree, 9)
    task = _task(
        session_dir,
        repo,
        base,
        "t-crash-window",
        worker=CRASH_ONCE_WORKER,
        gate="true",
        max_restarts=1,
    )
    runtime = supervisor_module._Runtime(session_dir, None)
    real_git = runtime._git

    async def crash_after_clean(path, *args, check=True):
        result = await real_git(path, *args, check=check)
        if args[:2] == ("clean", "-fd"):
            raise RuntimeError("simulated supervisor crash after git clean")
        return result

    monkeypatch.setattr(runtime, "_git", crash_after_clean)
    with pytest.raises(RuntimeError, match="simulated supervisor crash"):
        asyncio.run(runtime._recover_worktree(task))

    after_crash = read_generation(worktree)
    assert after_crash >= 10
    assert not validate_worker_generation(worktree, 1)

    result = asyncio.run(run_plan(session_dir, {"tasks": [task]}))

    after_restart = read_generation(worktree)
    assert result.results[0].status == "succeeded"
    assert [9, after_crash, after_restart] == [9, 10, 12]
    assert not validate_worker_generation(worktree, 1)


def test_worktree_registration_requires_an_exact_path_match(tmp_path: Path) -> None:
    session_dir = tmp_path / "session"
    repo = session_dir / "repo"
    base = _make_repo(repo, {"hello.txt": "hello\n"})
    registered_extra = session_dir / "wt-task-extra"
    subprocess.run(
        ["git", "-C", str(repo), "worktree", "add", "-b", "wt-task-extra",
         str(registered_extra), base],
        check=True,
        capture_output=True,
    )
    task = _task(
        session_dir,
        repo,
        base,
        "task",
        worker=CRASH_ONCE_WORKER,
        gate="true",
        max_restarts=1,
        max_wall_s=5.0,
    )

    result = asyncio.run(run_plan(session_dir, {"tasks": [task]}))

    assert result.results[0].status == "succeeded"
    assert result.results[0].restarts == 1
    events = read_events(session_dir)
    assert [event["generation"] for event in _kinds(events, "spawned")] == [1, 2]
    assert (registered_extra / "hello.txt").read_text(encoding="utf-8") == "hello\n"


def test_invalid_base_commit_rejects_registered_dirty_worktree_without_spawn(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    session_dir = tmp_path / "session"
    repo = session_dir / "repo"
    base = _make_repo(repo, {"hello.txt": "published\n"})
    worktree = session_dir / "wt-t-invalid-base"
    subprocess.run(
        ["git", "-C", str(repo), "worktree", "add", "-b", "wt-t-invalid-base",
         str(worktree), base],
        check=True,
        capture_output=True,
    )
    (worktree / "hello.txt").write_text("stale dirty content\n", encoding="utf-8")
    task = _task(
        session_dir,
        repo,
        "not-a-real-commit",
        "t-invalid-base",
        worker=FAKE_WORKER,
        gate="true",
    )

    def fail_spawn(*args, **kwargs):
        raise AssertionError("invalid base spawned a worker")

    monkeypatch.setattr(asyncio, "create_subprocess_exec", fail_spawn)
    result = asyncio.run(run_plan(session_dir, {"tasks": [task]}))

    assert result.results[0].status == "failed"
    assert not _kinds(read_events(session_dir), "spawned")
    assert subprocess.run(
        ["git", "-C", str(repo), "rev-parse", "refs/heads/main"],
        check=True,
        capture_output=True,
        text=True,
    ).stdout.strip() == base
    assert subprocess.run(
        ["git", "-C", str(repo), "show", "refs/heads/main:hello.txt"],
        check=True,
        capture_output=True,
        text=True,
    ).stdout == "published\n"


@pytest.mark.parametrize("failed_command", ["reset", "clean"])
def test_recovery_git_failure_fails_task_without_spawn_or_publish(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, failed_command: str
) -> None:
    session_dir = tmp_path / "session"
    repo = session_dir / "repo"
    base = _make_repo(repo, {"hello.txt": "published\n"})
    worktree = session_dir / "wt-t-recovery-failure"
    subprocess.run(
        ["git", "-C", str(repo), "worktree", "add", "-b", "wt-t-recovery-failure",
         str(worktree), base],
        check=True,
        capture_output=True,
    )
    (worktree / "hello.txt").write_text("stale dirty content\n", encoding="utf-8")
    task = _task(
        session_dir,
        repo,
        base,
        "t-recovery-failure",
        worker=FAKE_WORKER,
        gate="true",
    )
    real_git = supervisor_module._Runtime._git

    async def fail_recovery_git(self, path, *args, check=True):
        if args and args[0] == failed_command:
            return subprocess.CompletedProcess(
                ["git", *args], 23, stdout="", stderr=f"forced {failed_command} failure"
            )
        return await real_git(self, path, *args, check=check)

    def fail_spawn(*args, **kwargs):
        raise AssertionError("failed recovery spawned a worker")

    monkeypatch.setattr(supervisor_module._Runtime, "_git", fail_recovery_git)
    monkeypatch.setattr(asyncio, "create_subprocess_exec", fail_spawn)
    result = asyncio.run(run_plan(session_dir, {"tasks": [task]}))

    assert result.results[0].status == "failed"
    assert not _kinds(read_events(session_dir), "spawned")
    assert subprocess.run(
        ["git", "-C", str(repo), "rev-parse", "refs/heads/main"],
        check=True,
        capture_output=True,
        text=True,
    ).stdout.strip() == base
    assert subprocess.run(
        ["git", "-C", str(repo), "show", "refs/heads/main:hello.txt"],
        check=True,
        capture_output=True,
        text=True,
    ).stdout == "published\n"


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


def test_ready_protocol_version_mismatch_is_terminal_without_run_gate_or_merge(
    tmp_path: Path,
) -> None:
    session_dir = tmp_path / "session"
    repo = session_dir / "repo"
    base = _make_repo(repo, {"hello.txt": "hello\n"})
    worker = tmp_path / "wrong-proto-worker.py"
    worker.write_text(
        "import json, sys, time\n"
        "init = json.loads(sys.stdin.readline())\n"
        "print(json.dumps({'type': 'ready', 'request_id': init['request_id'], "
        "'task_id': init['task_id'], 'generation': init['generation'], "
        "'proto': 999}), flush=True)\n"
        "time.sleep(30)\n",
        encoding="utf-8",
    )
    task = _task(
        session_dir,
        repo,
        base,
        "t-wrong-proto",
        worker=str(worker),
        gate="true",
        max_restarts=2,
        max_wall_s=5.0,
    )

    result = asyncio.run(run_plan(session_dir, {"tasks": [task]}))

    task_result = result.results[0]
    assert task_result.status == "failed"
    assert task_result.reason == "PROTO_VERSION_MISMATCH"
    assert task_result.restarts == 0
    events = read_events(session_dir)
    assert any(
        event["kind"] == "protocol"
        and event["payload"].get("error_type") == "PROTO_VERSION_MISMATCH"
        and event["payload"].get("expected") == supervisor_module.PROTO
        and event["payload"].get("got") == 999
        for event in events
    )
    assert not _kinds(events, "run_task")
    assert not _kinds(events, "gate")
    assert not _kinds(events, "merge_started")
    assert not _kinds(events, "merge_committed")
    assert subprocess.run(
        ["git", "-C", str(repo), "rev-parse", "refs/heads/main"],
        check=True,
        capture_output=True,
        text=True,
    ).stdout.strip() == base


def test_duplicate_task_id_is_rejected_before_store_or_spawn(tmp_path: Path, monkeypatch) -> None:
    def fail_spawn(*args, **kwargs):
        raise AssertionError("duplicate plan spawned a subprocess")

    monkeypatch.setattr(asyncio, "create_subprocess_exec", fail_spawn)
    plan = {"tasks": [{"task_id": "duplicate"}, {"task_id": "duplicate"}]}

    with pytest.raises(DuplicateTaskIDError, match="duplicate"):
        asyncio.run(run_plan(tmp_path / "session", plan))
    assert not (tmp_path / "session" / ".cambium").exists()


def test_cli_rejects_duplicate_before_repo_bootstrap_hook(tmp_path: Path, monkeypatch) -> None:
    session_dir = tmp_path / "session"
    repo = tmp_path / "repo"
    subprocess.run(["git", "init", "-b", "main", str(repo)], check=True, capture_output=True)
    hook_report = tmp_path / "hook-report.txt"
    hook = repo / ".git" / "hooks" / "pre-commit"
    hook.write_text(
        "#!/bin/sh\n"
        f"printf '%s\\n' \"${{DATABASE_URL:-absent}}\" > {shlex.quote(str(hook_report))}\n",
        encoding="utf-8",
    )
    hook.chmod(0o755)
    monkeypatch.setenv("DATABASE_URL", "postgres://host-secret")
    task = {
        "task_id": "duplicate",
        "repo": str(repo),
        "worktree_path": str(session_dir / "wt-duplicate"),
        "branch": "wt-duplicate",
    }
    plan_path = tmp_path / "plan.json"
    plan_path.write_text(json.dumps({"tasks": [task, task]}), encoding="utf-8")

    with pytest.raises(DuplicateTaskIDError, match="duplicate"):
        supervisor_module.main(["--session-dir", str(session_dir), "--plan", str(plan_path)])

    assert not hook_report.exists()
    assert subprocess.run(
        ["git", "-C", str(repo), "rev-parse", "--verify", "HEAD"],
        capture_output=True,
    ).returncode != 0
    assert not session_dir.exists()


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
