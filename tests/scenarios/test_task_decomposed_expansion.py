"""Canary: dynamic tree growth — ``task_decomposed`` must schedule children.

Claim 1 (M5 target; architecture §7.6, docs/research/event-schema-draft.md
§3.10): a worker may emit a ``task_decomposed`` message carrying its child task
specs before its result envelope. The supervisor must append those children to
the running plan, schedule them, and merge every task's result.

A fake worker emits ``{"type": "task_decomposed", "request_id": run_rid,
"children": [<two child specs>]}`` for the parent task, then does its own
marker edit, sends ``result_envelope``, then ``exit_message``. The children are
full plan task specs (distinct worktrees/branches/files) reusing the same fake
worker so a landed M5 branch can schedule them as ordinary tasks.

Expected on the M5 branch: ``len(result.results) == 3`` (parent + two children),
every task succeeded, and the event log shows three ``merge_committed``.

This currently FAILS on main: ``_drive_generation`` has no ``task_decomposed``
branch, so the message is logged as an unhandled protocol message
(supervisor.py:2160-2164) and the parent is the only task. Skipped until
F6-01/M5 dynamic tree growth lands.
"""

from __future__ import annotations

import asyncio
import subprocess
import textwrap
from pathlib import Path

import pytest

from cambium.supervisor import read_events, run_plan

ROOT = Path(__file__).resolve().parents[2]
SCRIPTS = ROOT / "scripts"
PARENT_TASK_ID = "t-decompose"


def _make_repo(repo: Path, files: dict[str, str]) -> str:
    subprocess.run(["git", "init", "-b", "main", str(repo)], check=True, capture_output=True)
    subprocess.run(["git", "-C", str(repo), "config", "user.name", "decompose-test"], check=True)
    subprocess.run(["git", "-C", str(repo), "config", "user.email", "decompose@test"], check=True)
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
        check=True, capture_output=True, text=True,
    ).stdout.strip()


def _task(
    session_dir: Path, repo: Path, base: str, task_id: str, *,
    worktree: str, branch: str, target_file: str, marker: str, gate: str,
    worker: str,
) -> dict:
    return {
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


def _write_decompose_worker(tmp_path: Path) -> Path:
    """Parent fake worker: emits task_decomposed, then completes normally.

    Children (whose task ids are not ``PARENT_TASK_ID``) reuse this script via
    ``worker = sys.argv[0]`` and skip the decomposition message.
    """
    script = tmp_path / "decompose_worker.py"
    script.write_text(
        textwrap.dedent(
            f"""
            import json
            import os
            import sys
            from pathlib import Path

            sys.path.insert(0, {str(SCRIPTS)!r})
            from fake_worker import do_work, read_msg, send  # noqa: E402

            PARENT_TASK_ID = {PARENT_TASK_ID!r}


            def child_specs(run):
                worktree = Path(run["worktree_path"]).parent
                return [
                    {{
                        "task_id": run["task_id"] + "-child-1",
                        "task": "edit child1.txt",
                        "repo": run["repo"],
                        "worktree_path": str(worktree / "wt-child-1"),
                        "branch": "wt-child-1",
                        "worker": sys.argv[0],
                        "target_file": "child1.txt",
                        "marker": "// child-1",
                        "write_marker": True,
                        "gate": "grep -q '// child-1' child1.txt",
                        "base_commit": run["base_commit"],
                        "provider_env_keys": ["FAKE_MODE"],
                    }},
                    {{
                        "task_id": run["task_id"] + "-child-2",
                        "task": "edit child2.txt",
                        "repo": run["repo"],
                        "worktree_path": str(worktree / "wt-child-2"),
                        "branch": "wt-child-2",
                        "worker": sys.argv[0],
                        "target_file": "child2.txt",
                        "marker": "// child-2",
                        "write_marker": True,
                        "gate": "grep -q '// child-2' child2.txt",
                        "base_commit": run["base_commit"],
                        "provider_env_keys": ["FAKE_MODE"],
                    }},
                ]


            def main():
                init = read_msg()
                if init is None or init.get("type") != "init":
                    return 1
                task_id = init["task_id"]
                init_rid = init["request_id"]
                send({{"type": "ready", "request_id": init_rid, "task_id": task_id,
                      "pid": os.getpid(), "generation": init.get("generation", 1),
                      "proto": 1}})
                run = read_msg()
                if run is None or run.get("type") != "run_task":
                    return 1
                run_rid = run["request_id"]
                if task_id == PARENT_TASK_ID:
                    send({{"type": "task_decomposed", "request_id": run_rid,
                          "task_id": task_id, "children": child_specs(run)}})
                status, failure_reason, commits, files_changed, diff = do_work(run)
                send({{"type": "result_envelope", "request_id": run_rid,
                      "task_id": task_id, "generation": init.get("generation", 1),
                      "status": status, "commits": commits,
                      "files_changed": files_changed, "diff": diff,
                      "failure_reason": failure_reason}})
                send({{"type": "exit_message", "task_id": task_id,
                      "generation": init.get("generation", 1), "reason": "done"}})
                return 0


            if __name__ == "__main__":
                sys.exit(main())
            """
        ).lstrip(),
        encoding="utf-8",
    )
    return script


def _kinds(events: list[dict], kind: str) -> list[dict]:
    return [e for e in events if e["kind"] == kind]


@pytest.mark.skip(reason="F6-01/M5 dynamic tree growth not implemented")
def test_task_decomposed_schedules_two_children(tmp_path) -> None:
    worker = _write_decompose_worker(tmp_path)
    session_dir = tmp_path / "session"
    repo = session_dir / "repo"
    base = _make_repo(
        repo,
        {
            "a.txt": "file a\n",
            "child1.txt": "child file one\n",
            "child2.txt": "child file two\n",
        },
    )
    plan = {
        "tasks": [
            _task(
                session_dir, repo, base, PARENT_TASK_ID,
                worktree="wt-parent", branch="wt-parent", target_file="a.txt",
                marker="// cambium-decompose",
                gate="grep -q '// cambium-decompose' a.txt",
                worker=str(worker),
            )
        ]
    }

    result = asyncio.run(run_plan(session_dir, plan))
    events = read_events(session_dir)

    assert result.exit_code == 0
    assert len(result.results) == 3
    assert {r.task_id for r in result.results} == {
        PARENT_TASK_ID,
        f"{PARENT_TASK_ID}-child-1",
        f"{PARENT_TASK_ID}-child-2",
    }
    assert all(r.status == "succeeded" for r in result.results)
    assert all(r.merge_sha is not None for r in result.results)
    assert len(_kinds(events, "merge_committed")) == 3
    unhandled = [
        e for e in _kinds(events, "protocol") if e["payload"].get("note") == "unhandled message"
    ]
    assert not unhandled
