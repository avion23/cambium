"""Validated dynamic child admission scenarios (implementation-plan step 2).

A worker may propose a child task with the ``propose_child`` wire message
({request_id, parent_task_id, child_task_id, kind, spec}); the supervisor
validates the revision against the session tree via ``tasktree.build_tree``
on the accumulated tasks list (root = the single plan root) and either
durably records ``child_admitted`` and spawns the child, or durably records
``child_rejected`` with the structural reason and spawns nothing.

Scenarios:
  DC1 rejected revisions spawn nothing: a duplicate proposal and an
      over-depth chain proposal are rejected; only the valid chain admits
      and runs.
  DC2 one valid child is admitted; its context is its own spec plus the
      parent's strict-key envelope, and its upward envelope uses exactly the
      strict key set and is visible only to its parent.
"""

from __future__ import annotations

import asyncio
import json
import subprocess
from pathlib import Path

import pytest

from cambium.supervisor import read_events, run_plan

ROOT = Path(__file__).resolve().parents[2]
FAKE_WORKER = str(ROOT / "scripts" / "fake_worker.py")
TEST_RESOURCE_THRESHOLDS = {
    "mem_available_frac": 0.0,
    "load1_per_cpu": 1_000_000.0,
    "disk_free": 0,
}

_STRICT_ENVELOPE_KEYS = {
    "parent_task_id",
    "unified_diff",
    "diff_truncated",
    "summary",
    "metric_score",
    "metric_breakdown",
    "commits",
    "files_changed",
    "status",
}


def _make_repo(repo: Path, files: dict[str, str]) -> str:
    subprocess.run(["git", "init", "-b", "main", str(repo)], check=True, capture_output=True)
    subprocess.run(["git", "-C", str(repo), "config", "user.name", "dc-test"], check=True)
    subprocess.run(["git", "-C", str(repo), "config", "user.email", "dc@test"], check=True)
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
    worker: str = FAKE_WORKER,
    **extra,
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
        "base_commit": base,
        "provider_env_keys": ["FAKE_MODE"],
        "resource_thresholds": TEST_RESOURCE_THRESHOLDS,
    }
    spec.update(extra)
    return spec


def _kinds(events: list[dict], kind: str) -> list[dict]:
    return [e for e in events if e["kind"] == kind]


def _child_proposal(spec: dict) -> dict:
    """A propose_child entry referencing ``spec`` under its own task id."""
    return {
        "child_task_id": spec["task_id"],
        "kind": spec.get("kind", "test"),
        "spec": spec,
    }


def _show(repo: Path, ref: str, path: str) -> str:
    return subprocess.run(
        ["git", "-C", str(repo), "show", f"{ref}:{path}"],
        check=True,
        capture_output=True,
        text=True,
    ).stdout


def _write_child_dump_worker(dump_worker: Path) -> None:
    """Fixture worker: dump its full run payload, then do fake-worker work.

    Proves the child's context is limited to its own spec plus the parent's
    envelope: the whole ``run_task`` payload is persisted as JSON.
    """
    dump_worker.write_text(
        "#!/usr/bin/env python3\n"
        "import json\n"
        "import os\n"
        "import sys\n"
        "from pathlib import Path\n"
        f"ROOT = Path({str(ROOT)!r})\n"
        "sys.path.insert(0, str(ROOT / 'scripts'))\n"
        "from fake_worker import do_work, read_msg, send  # noqa: E402\n"
        "def main() -> int:\n"
        "    init = read_msg()\n"
        "    if init is None or init.get('type') != 'init':\n"
        "        return 1\n"
        "    dump_path = Path(os.environ['CONTEXT_DUMP_PATH'])\n"
        "    dump_path.parent.mkdir(parents=True, exist_ok=True)\n"
        "    init_rid = init['request_id']\n"
        "    task_id = init['task_id']\n"
        "    send({'type': 'ready', 'request_id': init_rid, 'task_id': task_id,\n"
        "          'pid': os.getpid(), 'generation': init.get('generation', 1),\n"
        "          'proto': 1})\n"
        "    run = read_msg()\n"
        "    if run is None or run.get('type') != 'run_task':\n"
        "        send({'type': 'exit_message', 'task_id': task_id,\n"
        "              'generation': init.get('generation', 1), 'reason': 'crash'})\n"
        "        return 1\n"
        "    dump_path.write_text(json.dumps(run))\n"
        "    run_rid = run['request_id']\n"
        "    status, failure_reason, commits, files_changed, diff = do_work(run)\n"
        "    send({'type': 'result_envelope', 'request_id': run_rid,\n"
        "          'task_id': task_id, 'generation': init.get('generation', 1),\n"
        "          'status': status, 'commits': commits,\n"
        "          'files_changed': files_changed, 'diff': diff,\n"
        "          'failure_reason': failure_reason})\n"
        "    send({'type': 'exit_message', 'task_id': task_id,\n"
        "          'generation': init.get('generation', 1), 'reason': 'done'})\n"
        "    return 0\n"
        "if __name__ == '__main__':\n"
        "    sys.exit(main())\n",
        encoding="utf-8",
    )
    dump_worker.chmod(0o755)


# ---------------------------------------------------------------------------
# DC1: rejected revisions spawn nothing.
#
# The root proposes a duplicate (child_task_id == its own id) plus a valid
# child; the valid chain c1 -> c2 -> c3 -> c4 runs, and the over-depth c4
# proposal is rejected. Only the valid chain is admitted and spawned.
# ---------------------------------------------------------------------------


@pytest.mark.slow
def test_dc1_rejected_revisions_spawn_nothing(tmp_path) -> None:
    session_dir = tmp_path / "session"
    repo = session_dir / "repo"
    base = _make_repo(
        repo, {"a.txt": "file a\n", "b.txt": "file b\n", "c.txt": "file c\n", "d.txt": "file d\n"}
    )

    c4 = _task(
        session_dir,
        repo,
        base,
        "c4",
        worktree="wt-c4",
        branch="wt-c4",
        target_file="d.txt",
        marker="// c4",
    )
    c3 = _task(
        session_dir,
        repo,
        base,
        "c3",
        worktree="wt-c3",
        branch="wt-c3",
        target_file="d.txt",
        marker="// c3",
        proposed_children=[_child_proposal(c4)],
    )
    c2 = _task(
        session_dir,
        repo,
        base,
        "c2",
        worktree="wt-c2",
        branch="wt-c2",
        target_file="c.txt",
        marker="// c2",
        proposed_children=[_child_proposal(c3)],
    )
    c1 = _task(
        session_dir,
        repo,
        base,
        "c1",
        worktree="wt-c1",
        branch="wt-c1",
        target_file="b.txt",
        marker="// c1",
        proposed_children=[_child_proposal(c2)],
    )
    duplicate = dict(c1, task_id="t-root")  # spec shape only; rejected as dup
    root = _task(
        session_dir,
        repo,
        base,
        "t-root",
        worktree="wt-root",
        branch="wt-root",
        target_file="a.txt",
        marker="// root",
        proposed_children=[
            _child_proposal(duplicate),
            _child_proposal(c1),
        ],
    )
    # c1 runs from the same base as the root but must not touch the root's
    # file: disjoint files keep every admitted merge conflict-free.

    result = asyncio.run(run_plan(session_dir, {"tasks": [root]}))

    assert result.exit_code == 0
    assert {r.task_id for r in result.results} == {"t-root", "c1", "c2", "c3"}
    assert all(r.status == "succeeded" for r in result.results)

    events = read_events(session_dir)
    admitted = _kinds(events, "child_admitted")
    rejected = _kinds(events, "child_rejected")
    assert [e["payload"]["child_task_id"] for e in admitted] == ["c1", "c2", "c3"]
    assert {e["payload"]["child_task_id"] for e in rejected} == {"t-root", "c4"}
    by_child = {e["payload"]["child_task_id"]: e for e in rejected}
    assert by_child["t-root"]["payload"]["reason"] == "DuplicateTaskError"
    assert by_child["t-root"]["task_id"] == "t-root"
    assert by_child["c4"]["payload"]["reason"] == "DepthBoundError"
    assert by_child["c4"]["task_id"] == "c3"

    spawned = _kinds(events, "spawned")
    spawned_ids = {e["task_id"] for e in spawned}
    assert "c4" not in spawned_ids
    assert "t-root" in spawned_ids
    assert not (session_dir / "wt-c4").exists()
    refs = subprocess.run(
        ["git", "-C", str(repo), "show-ref", "--verify", "--quiet", "refs/heads/wt-c4"],
        check=False,
    )
    assert refs.returncode != 0
    # The valid chain merged: every admitted marker is on main.
    for name, marker in (
        ("a.txt", "// root"),
        ("b.txt", "// c1"),
        ("c.txt", "// c2"),
        ("d.txt", "// c3"),
    ):
        assert marker in _show(repo, "main", name)


# ---------------------------------------------------------------------------
# DC2: one valid child is admitted; its context is its own spec plus the
# parent's strict-key envelope, and its upward envelope uses exactly the
# strict key set and reaches only its parent.
# ---------------------------------------------------------------------------


@pytest.mark.slow
def test_dc2_valid_child_context_and_envelope_reach(tmp_path, monkeypatch) -> None:
    session_dir = tmp_path / "session"
    repo = session_dir / "repo"
    base = _make_repo(repo, {"a.txt": "file a\n", "b.txt": "file b\n"})
    dump_worker = tmp_path / "dump_worker.py"
    _write_child_dump_worker(dump_worker)
    context_dump = tmp_path / "child-context.json"
    monkeypatch.setenv("CONTEXT_DUMP_PATH", str(context_dump))

    child = _task(
        session_dir,
        repo,
        base,
        "c1",
        worktree="wt-c1",
        branch="wt-c1",
        target_file="b.txt",
        marker="// child-marker",
        worker=str(dump_worker),
        provider_env_keys=["FAKE_MODE", "CONTEXT_DUMP_PATH"],
    )
    # The root declares CONTEXT_DUMP_PATH itself so the child may inherit it:
    # children inherit, never exceed, the parent's provider_env_keys.
    root = _task(
        session_dir,
        repo,
        base,
        "t-root",
        worktree="wt-root",
        branch="wt-root",
        target_file="a.txt",
        marker="// parent-marker",
        provider_env_keys=["FAKE_MODE", "CONTEXT_DUMP_PATH"],
        proposed_children=[_child_proposal(child)],
    )

    result = asyncio.run(run_plan(session_dir, {"tasks": [root]}))

    assert result.exit_code == 0
    assert {r.task_id for r in result.results} == {"t-root", "c1"}
    assert all(r.status == "succeeded" for r in result.results)
    assert "// parent-marker" in _show(repo, "main", "a.txt")
    assert "// child-marker" in _show(repo, "main", "b.txt")

    events = read_events(session_dir)
    admitted = _kinds(events, "child_admitted")
    assert len(admitted) == 1
    assert admitted[0]["payload"]["parent_task_id"] == "t-root"
    assert admitted[0]["payload"]["child_task_id"] == "c1"
    assert not _kinds(events, "child_rejected")

    # --- child context: its own spec + the parent's strict envelope only ---
    payload = json.loads(context_dump.read_text(encoding="utf-8"))
    assert payload["task_id"] == "c1"
    assert payload["target_file"] == "b.txt"
    assert payload["marker"] == "// child-marker"
    envelope = payload["parent_envelope"]
    assert set(envelope) == _STRICT_ENVELOPE_KEYS
    assert envelope["parent_task_id"] is None  # the root's own parent
    assert envelope["status"] == "succeeded"
    assert "// parent-marker" in envelope["unified_diff"]
    assert envelope["commits"]
    assert envelope["files_changed"] == ["a.txt"]
    serialized = json.dumps(payload)
    for forbidden in (
        "transcript",
        "scratchpad",
        "trajectory",
        "chain_of_thought",
        "sibling",
        "parent_transcript",
    ):
        assert forbidden not in serialized

    # --- child upward envelope: strict key set, reaches only its parent ---
    child_results = _kinds(events, "child_result")
    assert len(child_results) == 1
    child_result = child_results[0]
    assert child_result["task_id"] == "c1"
    assert set(child_result["payload"]) == _STRICT_ENVELOPE_KEYS
    assert child_result["payload"]["parent_task_id"] == "t-root"
    assert child_result["payload"]["status"] == "succeeded"
    assert "// child-marker" in child_result["payload"]["unified_diff"]
    assert child_result["payload"]["commits"]
    # Not visible to the parent's own records: no t-root event carries the
    # child's diff or marker.
    parent_events = [e for e in events if e["task_id"] == "t-root"]
    parent_serialized = json.dumps([e["payload"] for e in parent_events])
    assert "// child-marker" not in parent_serialized


# ---------------------------------------------------------------------------
# DC3: a failing child is reported to its parent (child_failed correlation).
# ---------------------------------------------------------------------------


@pytest.mark.slow
def test_dc3_failed_child_emits_child_failed_for_parent(tmp_path) -> None:
    session_dir = tmp_path / "session"
    repo = session_dir / "repo"
    base = _make_repo(repo, {"a.txt": "file a\n", "b.txt": "file b\n"})
    # write_marker=false forces the child's worker to report failed, so the
    # child ends with a recorded "failed" result.
    child = _task(
        session_dir,
        repo,
        base,
        "c1",
        worktree="wt-c1",
        branch="wt-c1",
        target_file="b.txt",
        marker="// child-marker",
        write_marker=False,
    )
    root = _task(
        session_dir,
        repo,
        base,
        "t-root",
        worktree="wt-root",
        branch="wt-root",
        target_file="a.txt",
        marker="// parent-marker",
        proposed_children=[_child_proposal(child)],
    )

    result = asyncio.run(run_plan(session_dir, {"tasks": [root]}))

    assert {r.task_id for r in result.results} == {"t-root", "c1"}
    statuses = {r.task_id: r.status for r in result.results}
    assert statuses["t-root"] == "succeeded"
    assert statuses["c1"] == "failed"

    events = read_events(session_dir)
    failed = _kinds(events, "child_failed")
    assert len(failed) == 1
    assert failed[0]["task_id"] == "c1"
    assert failed[0]["payload"]["parent_task_id"] == "t-root"
    assert failed[0]["payload"]["reason"] == "marker not written (write_marker=false)"
