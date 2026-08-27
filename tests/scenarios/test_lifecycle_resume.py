"""Checkpoint-bound restart and dirty-worktree salvage scenarios."""

from __future__ import annotations

import asyncio
import json
import subprocess
import textwrap
from pathlib import Path

import pytest

from cambium.supervisor import read_events, run_plan

TEST_RESOURCE_THRESHOLDS = {
    "mem_available_frac": 0.0,
    "load1_per_cpu": 1_000_000.0,
    "disk_free": 0,
}


def _make_repo(repo: Path) -> str:
    subprocess.run(["git", "init", "-b", "main", str(repo)], check=True, capture_output=True)
    subprocess.run(["git", "-C", str(repo), "config", "user.name", "lifecycle-test"], check=True)
    subprocess.run(
        ["git", "-C", str(repo), "config", "user.email", "lifecycle@test"], check=True
    )
    (repo / "state.txt").write_text("base\n", encoding="utf-8")
    subprocess.run(["git", "-C", str(repo), "add", "state.txt"], check=True)
    subprocess.run(
        ["git", "-C", str(repo), "commit", "-m", "initial"], check=True, capture_output=True
    )
    return subprocess.run(
        ["git", "-C", str(repo), "rev-parse", "HEAD"],
        check=True,
        capture_output=True,
        text=True,
    ).stdout.strip()


def _restart_worker(path: Path, *, mismatch: bool = False) -> None:
    mutation = "state.write_text('mismatch\\n', encoding='utf-8')" if mismatch else "pass"
    path.write_text(
        textwrap.dedent(
            f"""
            import hashlib
            import json
            import os
            import subprocess
            import sys
            import time
            from pathlib import Path

            init = json.loads(sys.stdin.readline())
            generation = init.get('generation', 1)
            print(json.dumps({{'type': 'ready', 'request_id': init['request_id'],
                              'task_id': init['task_id'], 'generation': generation,
                              'proto': 1}}), flush=True)
            run = json.loads(sys.stdin.readline())
            worktree = Path(run['worktree_path'])
            state = worktree / 'state.txt'
            if generation == 1:
                state.write_text('checkpointed\\n', encoding='utf-8')
                diff = subprocess.check_output(
                    ['git', '-c', 'core.hooksPath=/dev/null', 'diff', 'HEAD',
                     '--no-ext-diff', '--no-color'], cwd=worktree)
                checkpoint_dir = (Path(os.environ['CAMBIUM_SESSION_ID']) / '.cambium'
                                  / 'checkpoints' / init['task_id'])
                checkpoint_dir.mkdir(parents=True, exist_ok=True)
                (checkpoint_dir / 'turn-001.json').write_text(json.dumps({{
                    'schema': 1, 'task': run['task'], 'generation': 1, 'turn': 1,
                    'transcript': [], 'usage': {{}}, 'commits_so_far': [],
                    'workspace_hash': hashlib.sha256(diff).hexdigest(),
                }}), encoding='utf-8')
                {mutation}
                time.sleep(1000)
            if init.get('resume') is not None:
                (worktree.parent / 'resume.json').write_text(
                    json.dumps(init['resume']), encoding='utf-8')
                assert state.read_text(encoding='utf-8') == 'checkpointed\\n'
            else:
                assert state.read_text(encoding='utf-8') == 'base\\n'
            state.write_text('resumed\\n', encoding='utf-8')
            subprocess.run(['git', 'add', 'state.txt'], cwd=worktree, check=True)
            subprocess.run(['git', 'commit', '-m', 'restart'], cwd=worktree,
                           check=True, capture_output=True)
            sha = subprocess.check_output(['git', 'rev-parse', 'HEAD'], cwd=worktree,
                                          text=True).strip()
            print(json.dumps({{'type': 'result_envelope', 'request_id': run['request_id'],
                              'task_id': init['task_id'], 'generation': generation,
                              'status': 'succeeded', 'commits': [sha],
                              'files_changed': ['state.txt'], 'diff': 'resumed'}}), flush=True)
            print(json.dumps({{'type': 'exit_message', 'task_id': init['task_id'],
                              'generation': generation, 'reason': 'done'}}), flush=True)
            """
        ),
        encoding="utf-8",
    )


def _dirty_exit_worker(path: Path) -> None:
    path.write_text(
        """import json, sys
from pathlib import Path

init = json.loads(sys.stdin.readline())
print(json.dumps({'type': 'ready', 'request_id': init['request_id'],
                  'task_id': init['task_id'], 'generation': init.get('generation', 1),
                  'proto': 1}), flush=True)
run = json.loads(sys.stdin.readline())
(Path(run['worktree_path']) / 'state.txt').write_text('dirty\\n', encoding='utf-8')
raise SystemExit(7)
""",
        encoding="utf-8",
    )


def _task(session: Path, repo: Path, base: str, worker: Path, task_id: str = "task") -> dict:
    return {
        "task_id": task_id,
        "task": "preserve lifecycle state",
        "repo": str(repo),
        "worktree_path": str(session / "worktree"),
        "branch": task_id,
        "base_commit": base,
        "worker": str(worker),
        "provider_env_keys": [],
        "resource_thresholds": TEST_RESOURCE_THRESHOLDS,
        "heartbeat_interval_s": 0.02,
        "heartbeat_timeout_s": 0.12,
        "ready_timeout_s": 2.0,
        "max_wall_s": 20.0,
        "max_restarts": 1,
    }


@pytest.mark.slow
def test_stall_respawn_matching_hash_resumes_without_reset(tmp_path: Path, monkeypatch) -> None:
    session = tmp_path / "session"
    repo = session / "repo"
    base = _make_repo(repo)
    worker = tmp_path / "restart_worker.py"
    _restart_worker(worker)
    monkeypatch.setattr("cambium.supervisor.RESTART_BASE_DELAY_S", 0.01)
    task = _task(session, repo, base, worker, "matching")

    result = asyncio.run(run_plan(session, {"tasks": [task]}))

    assert result.exit_code == 0
    assert result.results[0].restarts == 1
    assert result.results[0].salvage_ref is None
    events = read_events(session)
    inits = [event for event in events if event["kind"] == "init"]
    assert len(inits) == 2
    assert inits[1]["generation"] == 2
    resume = json.loads((session / "resume.json").read_text(encoding="utf-8"))
    assert resume["checkpoint_ref"] == "matching/turn-001.json"
    assert resume["child_results"] == []
    assert not any(event["kind"] == "worktree_salvaged" for event in events)


@pytest.mark.slow
def test_stall_respawn_mismatched_hash_salvages_then_resets(tmp_path: Path, monkeypatch) -> None:
    session = tmp_path / "session"
    repo = session / "repo"
    base = _make_repo(repo)
    worker = tmp_path / "mismatch_worker.py"
    _restart_worker(worker, mismatch=True)
    monkeypatch.setattr("cambium.supervisor.RESTART_BASE_DELAY_S", 0.01)
    task = _task(session, repo, base, worker, "mismatch")

    result = asyncio.run(run_plan(session, {"tasks": [task]}))

    assert result.exit_code == 0
    salvage_ref = result.results[0].salvage_ref
    assert salvage_ref == "salvage/mismatch/1/workspace.diff"
    salvage_dir = session / salvage_ref.removesuffix("/workspace.diff")
    assert "mismatch" in (salvage_dir / "workspace.diff").read_text(encoding="utf-8")
    metadata = json.loads((salvage_dir / "salvage.json").read_text(encoding="utf-8"))
    assert metadata["task_id"] == "mismatch"
    assert metadata["generation"] == 1
    assert metadata["base_commit"] == base
    events = read_events(session)
    assert any(event["kind"] == "worktree_salvaged" for event in events)
    assert any(event["kind"] == "recover" and event["generation"] == 2 for event in events)


@pytest.mark.slow
def test_abnormal_exit_dirty_worktree_is_salvaged_before_prune(tmp_path: Path) -> None:
    session = tmp_path / "session"
    repo = session / "repo"
    base = _make_repo(repo)
    worker = tmp_path / "dirty_exit_worker.py"
    _dirty_exit_worker(worker)
    task = _task(session, repo, base, worker, "abnormal")
    task["max_restarts"] = 0

    result = asyncio.run(run_plan(session, {"tasks": [task]}))

    assert result.exit_code != 0
    assert result.results[0].status == "failed"
    salvage_ref = result.results[0].salvage_ref
    assert salvage_ref == "salvage/abnormal/1/workspace.diff"
    salvage_dir = session / salvage_ref.removesuffix("/workspace.diff")
    assert "dirty" in (salvage_dir / "workspace.diff").read_text(encoding="utf-8")
    metadata = json.loads((salvage_dir / "salvage.json").read_text(encoding="utf-8"))
    assert set(metadata) == {
        "task_id",
        "generation",
        "base_commit",
        "branch",
        "captured_at",
        "truncated",
    }
    events = read_events(session)
    salvaged = [event for event in events if event["kind"] == "worktree_salvaged"]
    assert len(salvaged) == 1
    assert salvaged[0]["payload"]["path"] == salvage_ref
    assert salvaged[0]["payload"]["bytes"] == (salvage_dir / "workspace.diff").stat().st_size
