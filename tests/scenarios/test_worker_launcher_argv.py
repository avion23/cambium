"""Canary: pluggable worker launcher argv and spawn-env contract (Claim 6).

The supervisor must spawn a plan task's worker with an exact argv:

  (a) ``worker = <script path>``
        -> ``[sys.executable, "-u", str(script)]``
  (b) default (no ``worker``, or the literal ``"cambium.worker"``)
        -> ``[sys.executable, "-u", "-m", "cambium.worker"]``
  (c) the spawned worker env carries only the task's declared
      ``provider_env_keys`` (fail-closed allowlist, T7-style) plus the
      supervisor's ``CAMBIUM_*`` overrides.

Contract sources: ``_worker_command`` (supervisor.py:1635-1645) and
``_worker_environment`` (supervisor.py:843-865) plus
``process_env.build_subprocess_env``.

Evidence: the durable ``spawned`` event records the exact joined command in
``payload["worker"]``, and the fake worker script additionally dumps its own
``sys.argv`` and ``os.environ`` so the assertion is on what the child process
actually saw, not just on the event log.
"""

from __future__ import annotations

import asyncio
import json
import subprocess
import sys
import textwrap
from pathlib import Path

import pytest

from cambium.supervisor import read_events, run_plan

ROOT = Path(__file__).resolve().parents[2]
SCRIPTS = ROOT / "scripts"


def _make_repo(repo: Path, files: dict[str, str]) -> str:
    subprocess.run(["git", "init", "-b", "main", str(repo)], check=True, capture_output=True)
    subprocess.run(["git", "-C", str(repo), "config", "user.name", "argv-test"], check=True)
    subprocess.run(["git", "-C", str(repo), "config", "user.email", "argv@test"], check=True)
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
        ["git", "-C", str(repo), "rev-parse", "HEAD"],
        check=True,
        capture_output=True,
        text=True,
    ).stdout.strip()


def _task(
    session_dir: Path,
    repo: Path,
    base: str,
    task_id: str,
    *,
    worktree: str,
    branch: str,
    target_file: str,
    marker: str,
    gate: str,
    worker: str | None = None,
    provider_env_keys: list[str] | None = None,
) -> dict:
    spec = {
        "task_id": task_id,
        "task": f"edit {target_file}",
        "repo": str(repo),
        "worktree_path": str(session_dir / worktree),
        "branch": branch,
        "target_file": target_file,
        "marker": marker,
        "write_marker": True,
        "gate": gate,
        "base_commit": base,
        "provider_env_keys": provider_env_keys or [],
    }
    if worker is not None:
        spec["worker"] = worker
    return spec


def _write_dump_worker(tmp_path: Path) -> Path:
    """A fake worker that dumps its argv/env, then does the fake-worker work.

    ``ARGV_DUMP_PATH`` and ``ENV_DUMP_PATH`` must be forwarded to the child
    through ``provider_env_keys``; the dumped files prove what the spawned
    process actually saw.
    """
    script = tmp_path / "dump_worker.py"
    script.write_text(
        textwrap.dedent(
            f"""
            import json
            import os
            import sys
            from pathlib import Path

            sys.path.insert(0, {str(SCRIPTS)!r})
            from fake_worker import do_work, read_msg, send  # noqa: E402

            argv_path = Path(os.environ["ARGV_DUMP_PATH"])
            argv_path.parent.mkdir(parents=True, exist_ok=True)
            argv_path.write_text(json.dumps(sys.argv))

            env_path = Path(os.environ["ENV_DUMP_PATH"])
            env_path.parent.mkdir(parents=True, exist_ok=True)
            env_path.write_text(json.dumps(dict(os.environ)))

            init = read_msg()
            if init is None or init.get("type") != "init":
                sys.exit(1)
            send({{"type": "ready", "request_id": init["request_id"],
                  "task_id": init["task_id"], "pid": os.getpid(),
                  "generation": init.get("generation", 1), "proto": 1}})
            run = read_msg()
            if run is None or run.get("type") != "run_task":
                sys.exit(1)
            status, failure_reason, commits, files_changed, diff = do_work(run)
            send({{"type": "result_envelope", "request_id": run["request_id"],
                  "task_id": init["task_id"], "generation": init.get("generation", 1),
                  "status": status, "commits": commits,
                  "files_changed": files_changed, "diff": diff,
                  "failure_reason": failure_reason}})
            send({{"type": "exit_message", "task_id": init["task_id"],
                  "generation": init.get("generation", 1), "reason": "done"}})
            sys.exit(0)
            """
        ).lstrip(),
        encoding="utf-8",
    )
    return script


def _spawned(events: list[dict]) -> list[dict]:
    return [e for e in events if e["kind"] == "spawned"]


@pytest.mark.slow
def test_script_worker_spawns_exact_argv_and_confined_env(tmp_path, monkeypatch) -> None:
    script = _write_dump_worker(tmp_path)
    argv_dump = tmp_path / "argv.json"
    env_dump = tmp_path / "env.json"
    monkeypatch.setenv("ARGV_DUMP_PATH", str(argv_dump))
    monkeypatch.setenv("ENV_DUMP_PATH", str(env_dump))
    monkeypatch.setenv("CAMBIUM_PROVIDER_OPENAI_API_KEY", "authorized-secret")
    monkeypatch.setenv("CAMBIUM_PROVIDER_ANTHROPIC_API_KEY", "undeclared-secret")
    monkeypatch.setenv("OPENAI_API_KEY", "generic-secret")

    session_dir = tmp_path / "session"
    repo = session_dir / "repo"
    base = _make_repo(repo, {"a.txt": "file a\n"})
    plan = {
        "tasks": [
            _task(
                session_dir,
                repo,
                base,
                "t-argv",
                worktree="wt-argv",
                branch="wt-argv",
                target_file="a.txt",
                marker="// cambium-argv",
                gate="grep -q '// cambium-argv' a.txt",
                worker=str(script),
                provider_env_keys=[
                    "CAMBIUM_PROVIDER_OPENAI_API_KEY",
                    "ARGV_DUMP_PATH",
                    "ENV_DUMP_PATH",
                ],
            )
        ]
    }

    result = asyncio.run(run_plan(session_dir, plan))

    assert result.exit_code == 0
    assert result.results[0].status == "succeeded"
    events = read_events(session_dir)
    spawned = _spawned(events)
    assert len(spawned) == 1
    assert spawned[0]["payload"]["worker"] == " ".join([sys.executable, "-u", str(script)])

    seen_argv = json.loads(argv_dump.read_text(encoding="utf-8"))
    assert seen_argv == [str(script)]

    seen_env = json.loads(env_dump.read_text(encoding="utf-8"))
    assert seen_env["CAMBIUM_PROVIDER_OPENAI_API_KEY"] == "authorized-secret"
    assert seen_env["CAMBIUM_TASK_ID"] == "t-argv"
    assert seen_env["CAMBIUM_GENERATION"] == "1"
    assert seen_env["CAMBIUM_SESSION_ID"] == str(session_dir.resolve())
    assert "CAMBIUM_PROVIDER_ANTHROPIC_API_KEY" not in seen_env
    assert "OPENAI_API_KEY" not in seen_env
    assert "CAMBIUM_PROVIDER_bad_API_KEY" not in seen_env
