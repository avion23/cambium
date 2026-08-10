"""End-to-end tasktree plan -> supervisor -> worker -> gate -> merge scenarios."""

from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

from cambium.supervisor import read_events
from cambium.tasktree import build_tree, topological_order

ROOT = Path(__file__).resolve().parents[2]
SRC_DIR = str(ROOT / "src")
WORKER = str(ROOT / "scripts" / "fake_worker.py")


def _pythonpath_env() -> dict[str, str]:
    env = dict(os.environ)
    env["PYTHONPATH"] = os.pathsep.join(
        filter(None, [SRC_DIR, env.get("PYTHONPATH")])
    )
    return env


def _make_repo(repo: Path, files: dict[str, str]) -> str:
    subprocess.run(["git", "init", "-b", "main", str(repo)], check=True, capture_output=True)
    subprocess.run(["git", "-C", str(repo), "config", "user.name", "pipeline-test"], check=True)
    subprocess.run(
        ["git", "-C", str(repo), "config", "user.email", "pipeline@test"], check=True
    )
    subprocess.run(["git", "-C", str(repo), "config", "gc.auto", "0"], check=True)
    for name, content in files.items():
        (repo / name).write_text(content)
    subprocess.run(["git", "-C", str(repo), "add", "-A"], check=True, capture_output=True)
    subprocess.run(
        ["git", "-C", str(repo), "commit", "-m", "initial"], check=True, capture_output=True
    )
    return subprocess.run(
        ["git", "-C", str(repo), "rev-parse", "HEAD"],
        check=True,
        capture_output=True,
        text=True,
    ).stdout.strip()


def _show(repo: Path, ref: str, path: str) -> str:
    return subprocess.run(
        ["git", "-C", str(repo), "show", f"{ref}:{path}"],
        check=True,
        capture_output=True,
        text=True,
    ).stdout


def _task(
    session_dir: Path,
    repo: Path,
    base: str,
    task_id: str,
    target_file: str,
    marker: str,
) -> dict[str, object]:
    return {
        "task_id": task_id,
        "task": f"edit {target_file}",
        "repo": str(repo),
        "worktree_path": str(session_dir / f"wt-{task_id}"),
        "branch": f"wt-{task_id}",
        "worker": WORKER,
        "target_file": target_file,
        "marker": marker,
        "write_marker": True,
        "gate": f"grep -q '{marker}' {target_file}",
        "base_commit": base,
        "provider_env_keys": ["FAKE_MODE"],
    }


def _protocol(events: list[dict], task_id: str) -> list[str]:
    wanted = {"init", "ready", "run_task", "result", "exit"}
    return [
        event["kind"]
        for event in events
        if event["task_id"] == task_id and event["kind"] in wanted
    ]


def test_tasktree_plan_runs_supervisor_subprocess_to_three_merges(tmp_path) -> None:
    session_dir = tmp_path / "session"
    repo = session_dir / "repo"
    base = _make_repo(
        repo,
        {"a.txt": "file a\n", "b.txt": "file b\n", "c.txt": "file c\n"},
    )

    specs = {
        "task-a": _task(session_dir, repo, base, "task-a", "a.txt", "// pipeline-a"),
        "task-b": _task(session_dir, repo, base, "task-b", "b.txt", "// pipeline-b"),
        "task-c": _task(session_dir, repo, base, "task-c", "c.txt", "// pipeline-c"),
    }
    planner_payload = {
        "tasks": [
            {"task_id": "task-a", "kind": "TEST", "depends_on": [], "spec": specs["task-a"]},
            {
                "task_id": "task-b",
                "kind": "TEST",
                "depends_on": ["task-a"],
                "spec": specs["task-b"],
            },
            {
                "task_id": "task-c",
                "kind": "TEST",
                "depends_on": ["task-a"],
                "spec": specs["task-c"],
            },
        ]
    }

    tree = build_tree(planner_payload)
    order = topological_order(tree)
    by_id = {node.task_id: node for node in tree.nodes}
    # The tasktree carries planning dependencies; this supervisor seam runs
    # the generated worker specs as one flat fan-out until DAG scheduling lands.
    plan = {"tasks": [dict(by_id[task_id].spec) for task_id in order]}
    plan_path = tmp_path / "plan.json"
    plan_path.write_text(json.dumps(plan, indent=2))

    written_plan = json.loads(plan_path.read_text())
    assert [task["task_id"] for task in written_plan["tasks"]] == order

    env = _pythonpath_env()
    env.pop("FAKE_MODE", None)
    env.update(
        {
            "CAMBIUM_READY_TIMEOUT_S": "5",
            "CAMBIUM_GATE_TIMEOUT_S": "10",
            "CAMBIUM_WALL_BUDGET_S": "60",
            "CAMBIUM_HEARTBEAT_TIMEOUT_S": "30",
        }
    )
    completed = subprocess.run(
        [
            sys.executable,
            "-m",
            "cambium.supervisor",
            "--plan",
            str(plan_path),
            "--session-dir",
            str(session_dir),
        ],
        cwd=str(ROOT),
        env=env,
        capture_output=True,
        text=True,
        timeout=120,
    )
    assert completed.returncode == 0, f"stdout={completed.stdout}\nstderr={completed.stderr}"

    for filename, marker in (
        ("a.txt", "// pipeline-a"),
        ("b.txt", "// pipeline-b"),
        ("c.txt", "// pipeline-c"),
    ):
        assert marker in _show(repo, "main", filename)

    event_db = session_dir / ".cambium" / "events.db"
    assert event_db.is_file()
    events = read_events(session_dir)
    task_ids = set(specs)
    assert {event["task_id"] for event in events if event["kind"] == "merge_committed"} == task_ids
    for task_id in task_ids:
        assert _protocol(events, task_id) == ["init", "ready", "run_task", "result", "exit"]
        task_events = [event["kind"] for event in events if event["task_id"] == task_id]
        positions = {
            kind: task_events.index(kind)
            for kind in ("task_assigned", "gate", "merge_started", "merge_committed")
        }
        assert (
            positions["task_assigned"]
            < positions["gate"]
            < positions["merge_started"]
            < positions["merge_committed"]
        )
    assert events[-1]["kind"] == "session_ended"


def test_tasktree_cli_rejects_cycle_before_supervisor(tmp_path) -> None:
    cyclic_plan = {
        "tasks": [
            {"task_id": "root", "kind": "FEATURE", "depends_on": []},
            {"task_id": "cycle-a", "kind": "TEST", "depends_on": ["cycle-c"]},
            {"task_id": "cycle-b", "kind": "TEST", "depends_on": ["cycle-a"]},
            {"task_id": "cycle-c", "kind": "TEST", "depends_on": ["cycle-b"]},
        ]
    }
    result = subprocess.run(
        [sys.executable, "-m", "cambium.tasktree"],
        cwd=str(ROOT),
        env=_pythonpath_env(),
        input=json.dumps(cyclic_plan),
        capture_output=True,
        text=True,
        timeout=30,
    )

    assert result.returncode == 1
    assert result.stdout == ""
    assert "cycle" in result.stderr
    assert all(task_id in result.stderr for task_id in ("cycle-a", "cycle-b", "cycle-c"))
    assert not (tmp_path / "session").exists()
