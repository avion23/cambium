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


def test_gate_output_overflow_is_bounded_and_kills_process_group(tmp_path: Path) -> None:
    session_dir = tmp_path / "session"
    repo = session_dir / "repo"
    base = _make_repo(repo, {"hello.txt": "hello\n"})
    pid_file = tmp_path / "noisy-gate.pid"
    program = (
        "import os,sys; "
        f"open({str(pid_file)!r},'w').write(str(os.getpid())); "
        "chunk='x'*4096; "
        "exec(\"while True:\\n sys.stdout.write(chunk)\\n sys.stdout.flush()\")"
    )
    gate = f"{shlex.quote(sys.executable)} -c {shlex.quote(program)}"
    task = _task(
        session_dir,
        repo,
        base,
        "t-gate-overflow",
        worker=FAKE_WORKER,
        gate=gate,
        gate_timeout_s=5.0,
        max_wall_s=10.0,
    )

    started = time.monotonic()
    result = asyncio.run(run_plan(session_dir, {"tasks": [task]}))

    assert time.monotonic() - started < 4.0
    assert result.results[0].status == "failed"
    assert result.results[0].reason == "gate_failed"
    _wait_pid_gone(int(pid_file.read_text(encoding="ascii")))
    gate_events = _kinds(read_events(session_dir), "gate")
    assert any(event["payload"].get("output_overflow") for event in gate_events)


def test_gate_overflow_after_leader_exit_is_bounded_and_kills_process_group(
    tmp_path: Path,
) -> None:
    session_dir = tmp_path / "session"
    repo = session_dir / "repo"
    base = _make_repo(repo, {"hello.txt": "hello\n"})
    pid_file = tmp_path / "background-noisy-gate.pid"
    program = (
        "import os,sys; "
        f"open({str(pid_file)!r},'w').write(str(os.getpid())); "
        "chunk='x'*4096; "
        "exec(\"while True:\\n sys.stdout.write(chunk)\\n sys.stdout.flush()\")"
    )
    gate = f"{shlex.quote(sys.executable)} -c {shlex.quote(program)} &"
    task = _task(
        session_dir,
        repo,
        base,
        "t-gate-background-overflow",
        worker=FAKE_WORKER,
        gate=gate,
        gate_timeout_s=2.0,
        max_wall_s=5.0,
    )

    started = time.monotonic()
    result = asyncio.run(run_plan(session_dir, {"tasks": [task]}))

    assert time.monotonic() - started < 1.5
    assert result.results[0].status == "failed"
    assert result.results[0].reason == "gate_failed"
    assert result.results[0].gate_exit_code == 125
    _wait_pid_gone(int(pid_file.read_text(encoding="ascii")))
    gate_events = _kinds(read_events(session_dir), "gate")
    assert any(event["payload"].get("output_overflow") for event in gate_events)


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


def test_registered_worktree_path_with_literal_newline_is_not_deleted(
    tmp_path: Path,
) -> None:
    session_dir = tmp_path / "session"
    repo = session_dir / "repo"
    base = _make_repo(repo, {"hello.txt": "hello\n"})
    worktree = session_dir / "wt\nname"
    subprocess.run(
        ["git", "-C", str(repo), "worktree", "add", "-b", "wt-newline", str(worktree), base],
        check=True,
        capture_output=True,
    )
    task = _task(
        session_dir,
        repo,
        base,
        "t-newline-path",
        worker=FAKE_WORKER,
        gate="true",
        worktree_path=str(worktree),
        branch="wt-newline",
    )
    runtime = supervisor_module._Runtime(session_dir, None)

    asyncio.run(runtime._ensure_worktree(task))

    assert worktree.is_dir()
    listing = subprocess.run(
        ["git", "-C", str(repo), "worktree", "list", "--porcelain", "-z"],
        check=True,
        capture_output=True,
    ).stdout
    assert f"worktree {worktree}\0".encode() in listing
    assert (worktree / "hello.txt").read_text(encoding="utf-8") == "hello\n"


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


def test_ready_without_proto_is_terminal_without_run_gate_or_merge(tmp_path: Path) -> None:
    session_dir = tmp_path / "session"
    repo = session_dir / "repo"
    base = _make_repo(repo, {"hello.txt": "hello\n"})
    worker = tmp_path / "missing-proto-worker.py"
    worker.write_text(
        "import json, sys, time\n"
        "init = json.loads(sys.stdin.readline())\n"
        "print(json.dumps({'type': 'ready', 'request_id': init['request_id']}), "
        "flush=True)\n"
        "time.sleep(30)\n",
        encoding="utf-8",
    )
    task = _task(
        session_dir,
        repo,
        base,
        "t-missing-proto",
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
        and event["payload"].get("got") is None
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


def test_wrong_ready_request_id_with_correlated_result_is_terminal_without_merge(
    tmp_path: Path,
) -> None:
    session_dir = tmp_path / "session"
    repo = session_dir / "repo"
    base = _make_repo(repo, {"hello.txt": "hello\n"})
    worker = tmp_path / "wrong-ready-result-worker.py"
    worker.write_text(
        "import json, sys\n"
        "def send(message):\n"
        "    print(json.dumps(message), flush=True)\n"
        "init = json.loads(sys.stdin.readline())\n"
        "send({'type': 'ready', 'request_id': 'wrong-request-id', "
        "'task_id': init['task_id'], 'generation': init['generation'], 'proto': 1})\n"
        "run = json.loads(sys.stdin.readline())\n"
        "if run.get('type') == 'run_task':\n"
        "    with open(run['target_file'], 'a', encoding='utf-8') as handle:\n"
        "        handle.write('\\n// wrong-ready-result\\n')\n"
        "    send({'type': 'result_envelope', 'request_id': run['request_id'], "
        "'status': 'succeeded'})\n"
        "    send({'type': 'exit_message', 'reason': 'done'})\n",
        encoding="utf-8",
    )
    task = _task(
        session_dir,
        repo,
        base,
        "t-wrong-ready-result",
        worker=str(worker),
        gate="grep -q '// wrong-ready-result' hello.txt",
        max_restarts=2,
        max_wall_s=5.0,
        marker="// wrong-ready-result",
    )

    result = asyncio.run(run_plan(session_dir, {"tasks": [task]}))

    task_result = result.results[0]
    assert task_result.status == "failed"
    assert task_result.reason == "ready_request_id_mismatch"
    assert task_result.restarts == 0
    events = read_events(session_dir)
    protocol = _kinds(events, "protocol")
    assert len(protocol) == 1
    assert protocol[0]["payload"]["code"] == supervisor_module.PROTO_UNKNOWN_REQUEST_ID
    assert protocol[0]["payload"]["expected"] != protocol[0]["payload"]["got"]
    assert not _kinds(events, "run_task")
    assert not _kinds(events, "gate")
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


@pytest.mark.parametrize("task_value", [None, 42, "", "   ", "\n\t "])
def test_plan_task_requires_non_empty_task_string(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, task_value
) -> None:
    def fail_spawn(*args, **kwargs):
        raise AssertionError("invalid task spawned a subprocess")

    monkeypatch.setattr(asyncio, "create_subprocess_exec", fail_spawn)
    plan = {"tasks": [{"task_id": "t-invalid", "task": task_value}]}

    with pytest.raises(ValueError, match="requires a non-empty 'task'"):
        asyncio.run(run_plan(tmp_path / "session", plan))
    assert not (tmp_path / "session" / ".cambium").exists()


def test_plan_task_missing_task_is_rejected_before_store_or_spawn(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    def fail_spawn(*args, **kwargs):
        raise AssertionError("missing task spawned a subprocess")

    monkeypatch.setattr(asyncio, "create_subprocess_exec", fail_spawn)
    plan = {"tasks": [{"task_id": "t-missing-task"}]}

    with pytest.raises(ValueError, match="requires a non-empty 'task'"):
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


def test_default_spec_selects_installed_worker_module() -> None:
    spec = supervisor_module._default_spec(Path("/tmp/session"))

    assert spec["worker"] == "cambium.worker"


def test_worker_command_prefers_installed_module_and_preserves_script_paths() -> None:
    module_cmd = [sys.executable, "-u", "-m", "cambium.worker"]
    runtime = supervisor_module._Runtime(Path("/tmp/session"), None)
    assert (
        runtime._worker_command({"task_id": "t", "worker": "cambium.worker"}) == module_cmd
    )
    assert runtime._worker_command({"task_id": "t"}) == module_cmd
    script = "/some/worker/script.py"
    assert runtime._worker_command({"task_id": "t", "worker": script}) == [
        sys.executable,
        "-u",
        script,
    ]


def test_slice_runtime_runs_the_installed_worker_module(tmp_path) -> None:
    session_dir = tmp_path / "session"
    _make_scratch(session_dir / "scratch")
    spec = _slice_spec(session_dir, "cambium.worker")
    spec["marker"] = "// slice-module-worker"
    spec["gate"] = "grep -q '// slice-module-worker' hello.txt"

    result = asyncio.run(run_session(session_dir, spec))

    assert result.status == "succeeded"
    assert result.exit_code == 0
    assert result.worker_exit_code == 0
    events = [
        json.loads(line)
        for line in (session_dir / ".cambium" / "events.jsonl").read_text().splitlines()
        if line.strip()
    ]
    spawned = [event for event in events if event["kind"] == "spawned"]
    assert spawned
    assert "cambium.worker" in spawned[0]["payload"]["worker"]


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


def test_slice_wrong_ready_request_id_with_correlated_result_is_terminal_without_merge(
    tmp_path: Path,
) -> None:
    session_dir = tmp_path / "session"
    _make_scratch(session_dir / "scratch")
    worker = tmp_path / "wrong-ready-result-slice-worker.py"
    worker.write_text(
        "import json, subprocess, sys\n"
        "from pathlib import Path\n"
        "def send(message):\n"
        "    print(json.dumps(message), flush=True)\n"
        "init = json.loads(sys.stdin.readline())\n"
        "send({'type': 'ready', 'request_id': 'wrong-request-id', 'proto': 1})\n"
        "run = json.loads(sys.stdin.readline())\n"
        "if run.get('type') != 'run_task':\n"
        "    raise SystemExit(1)\n"
        "repo = Path(run['scratch_repo'])\n"
        "worktree = Path(run['worktree_path'])\n"
        "subprocess.run(['git', 'worktree', 'add', '-b', run['branch'],\n"
        "                str(worktree), 'main'], cwd=repo, check=True,\n"
        "                capture_output=True)\n"
        "target = worktree / run['target_file']\n"
        "target.write_text(target.read_text() + '\\n// slice-wrong-ready\\n')\n"
        "subprocess.run(['git', 'add', run['target_file']], cwd=worktree, check=True,\n"
        "                capture_output=True)\n"
        "subprocess.run(['git', 'commit', '-m', 'wrong ready'], cwd=worktree, check=True,\n"
        "                capture_output=True)\n"
        "send({'type': 'result_envelope', 'request_id': run['request_id'],\n"
        "      'status': 'succeeded'})\n"
        "send({'type': 'exit_message', 'reason': 'done'})\n",
        encoding="utf-8",
    )
    spec = _slice_spec(session_dir, str(worker))
    spec.update({
        "marker": "// slice-wrong-ready",
        "gate": "grep -q '// slice-wrong-ready' hello.txt",
    })

    result = asyncio.run(run_session(session_dir, spec))

    assert result.status == "failed"
    assert result.merge_sha is None
    events = [
        json.loads(line)
        for line in (session_dir / ".cambium" / "events.jsonl").read_text().splitlines()
    ]
    protocol = [event for event in events if event["kind"] == "protocol"]
    assert len(protocol) == 1
    assert protocol[0]["payload"]["code"] == supervisor_module.PROTO_UNKNOWN_REQUEST_ID
    assert not any(event["kind"] in {"gate", "merge"} for event in events)
    assert "// slice-wrong-ready" not in subprocess.run(
        ["git", "show", "main:hello.txt"],
        cwd=session_dir / "scratch",
        check=True,
        capture_output=True,
        text=True,
    ).stdout


def test_slice_ready_without_proto_is_terminal_without_run_gate_or_merge(
    tmp_path: Path,
) -> None:
    session_dir = tmp_path / "session"
    _make_scratch(session_dir / "scratch")
    worker = tmp_path / "missing-proto-slice-worker.py"
    worker.write_text(
        "import json, sys, time\n"
        "init = json.loads(sys.stdin.readline())\n"
        "print(json.dumps({'type': 'ready', 'request_id': init['request_id']}), "
        "flush=True)\n"
        "time.sleep(30)\n",
        encoding="utf-8",
    )

    result = asyncio.run(run_session(session_dir, _slice_spec(session_dir, str(worker))))

    assert result.status == "failed"
    assert result.merge_sha is None
    events = [
        json.loads(line)
        for line in (session_dir / ".cambium" / "events.jsonl").read_text().splitlines()
    ]
    assert any(
        event["kind"] == "protocol"
        and event["payload"].get("error_type") == "PROTO_VERSION_MISMATCH"
        and event["payload"].get("got") is None
        for event in events
    )
    assert not any(event["kind"] in {"run_task", "gate", "merge"} for event in events)


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
