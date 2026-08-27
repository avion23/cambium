"""Redacted checkpoint recovery and normal checkpoint resume scenarios."""

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
    subprocess.run(["git", "-C", str(repo), "config", "user.name", "checkpoint-test"], check=True)
    subprocess.run(["git", "-C", str(repo), "config", "user.email", "checkpoint@test"], check=True)
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


def _checkpoint_worker(path: Path, *, redaction: str | None = None) -> None:
    checkpoint_mutation = {
        "missing": "checkpoint.pop('workspace_hash')",
        "none": "checkpoint['workspace_hash'] = None",
        None: "pass",
    }[redaction]
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
            generation = init['generation']
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
                checkpoint = {{
                    'schema': 1, 'task': run['task'], 'generation': 1, 'turn': 1,
                    'transcript': [], 'usage': {{}}, 'commits_so_far': [],
                    'workspace_hash': hashlib.sha256(diff).hexdigest(),
                }}
                {checkpoint_mutation}
                checkpoint_dir = (Path(os.environ['CAMBIUM_SESSION_ID']) / '.cambium'
                                  / 'checkpoints' / init['task_id'])
                checkpoint_dir.mkdir(parents=True, exist_ok=True)
                (checkpoint_dir / 'turn-001.json').write_text(
                    json.dumps(checkpoint), encoding='utf-8')
                time.sleep(1000)
            if init.get('resume') is not None:
                (worktree.parent / 'resume.json').write_text(
                    json.dumps(init['resume']), encoding='utf-8')
                assert state.read_text(encoding='utf-8') == 'checkpointed\\n'
                state.write_text('resumed\\n', encoding='utf-8')
                subprocess.run(['git', 'add', 'state.txt'], cwd=worktree, check=True)
                subprocess.run(['git', 'commit', '-m', 'restart'], cwd=worktree,
                               check=True, capture_output=True)
                sha = subprocess.check_output(['git', 'rev-parse', 'HEAD'], cwd=worktree,
                                              text=True).strip()
                print(json.dumps({{'type': 'result_envelope',
                                  'request_id': run['request_id'],
                                  'task_id': init['task_id'], 'generation': generation,
                                  'status': 'succeeded', 'commits': [sha],
                                  'files_changed': ['state.txt'], 'diff': 'resumed'}}),
                      flush=True)
                print(json.dumps({{'type': 'exit_message', 'task_id': init['task_id'],
                                  'generation': generation, 'reason': 'done'}}), flush=True)
            elif generation == 2:
                assert state.read_text(encoding='utf-8') == 'base\\n'
                raise SystemExit(7)
            """
        ),
        encoding="utf-8",
    )


def _task(session: Path, repo: Path, base: str, worker: Path, task_id: str) -> dict:
    return {
        "task_id": task_id,
        "task": "preserve checkpoint state",
        "repo": str(repo),
        "worktree_path": str(session / "worktree"),
        "branch": task_id,
        "base_commit": base,
        "worker": str(worker),
        "provider_env_keys": [],
        "resource_thresholds": TEST_RESOURCE_THRESHOLDS,
        "heartbeat_interval_s": 0.02,
        "heartbeat_timeout_s": 2.0,
        "ready_timeout_s": 2.0,
        "max_wall_s": 20.0,
        "max_restarts": 1,
    }


@pytest.mark.parametrize(
    "redaction",
    ["none", "missing"],
    ids=["workspace_hash_none", "workspace_hash_missing"],
)
def test_redacted_checkpoint_salvages_before_failure_prune(tmp_path: Path, redaction: str) -> None:
    session = tmp_path / "session"
    repo = session / "repo"
    base = _make_repo(repo)
    worker = tmp_path / "checkpoint_worker.py"
    _checkpoint_worker(worker, redaction=redaction)
    task_id = f"redacted-{redaction}"
    task = _task(session, repo, base, worker, task_id)

    result = asyncio.run(run_plan(session, {"tasks": [task]}))

    assert result.exit_code != 0
    task_result = result.results[0]
    assert task_result.status == "failed"
    assert task_result.reason and "max_restarts" in task_result.reason
    salvage_ref = f"salvage/{task_id}/1/workspace.diff"
    assert task_result.salvage_ref == salvage_ref
    salvage_dir = session / salvage_ref.removesuffix("/workspace.diff")
    assert "checkpointed" in (salvage_dir / "workspace.diff").read_text(encoding="utf-8")

    events = read_events(session)
    salvaged = next(event for event in events if event["kind"] == "worktree_salvaged")
    failed = next(event for event in events if event["kind"] == "worker_failed")
    pruned = next(event for event in events if event["kind"] == "worktree_pruned")
    assert salvaged["payload"]["path"] == salvage_ref
    assert salvaged["generation"] == 1
    assert failed["generation"] == 2
    assert salvaged["seq"] < failed["seq"] < pruned["seq"]
    assert not (session / "worktree").exists()


def test_normal_checkpoint_resumes_by_workspace_hash_without_salvage(tmp_path: Path) -> None:
    session = tmp_path / "session"
    repo = session / "repo"
    base = _make_repo(repo)
    worker = tmp_path / "checkpoint_worker.py"
    _checkpoint_worker(worker)
    task_id = "normal"
    task = _task(session, repo, base, worker, task_id)

    result = asyncio.run(run_plan(session, {"tasks": [task]}))

    assert result.exit_code == 0
    task_result = result.results[0]
    assert task_result.status == "succeeded"
    assert task_result.restarts == 1
    assert task_result.salvage_ref is None
    resume = json.loads((session / "resume.json").read_text(encoding="utf-8"))
    assert resume == {
        "checkpoint_ref": "normal/turn-001.json",
        "epoch": 1,
        "child_results": [],
        "child_results_truncated": False,
        "workspace_changed": False,
    }
    events = read_events(session)
    assert not any(event["kind"] == "worktree_salvaged" for event in events)
    assert not (session / "salvage").exists()
    assert not (session / "worktree").exists()
