"""Static ready-node wave acceptance (implementation-plan §1).

Real supervisor (``cambium.supervisor.run_plan``) driving the
``tests/fixtures/hierarchy_worker.py`` marker worker through real git
operations. No mocks, no network. Proves the five §1 acceptance measures:

- exact ready waves (deterministic wave order, no unready dispatch),
- width enforcement (a wave never runs more than ``max_width`` concurrently),
- bounded child context (child sees own spec + allowed parent envelope keys),
- exact envelope keys (upward result carries exactly the strict key set),
- failed children stop dependent admission (failed nodes' descendants are
  never spawned, marked failed with ``dependency_failed:<parent>``).

Plus a focused unit test that proves the dispatcher uses deep-copied
snapshots: sibling specs handed to ``supervise_task`` are distinct objects
with unaliased nested values.
"""

from __future__ import annotations

import asyncio
import json
import os
import subprocess
from pathlib import Path
from typing import Any

from cambium.supervisor import (
    PlanResult,
    TaskResult,
    _dispatch_static_waves,
    _strict_parent_envelope,
    run_plan,
)
from cambium.tasktree import _ENVELOPE_KEYS, build_tree, topological_order

ROOT = Path(__file__).resolve().parents[2]
HIERARCHY_WORKER = str(ROOT / "tests" / "fixtures" / "hierarchy_worker.py")
TEST_RESOURCE_THRESHOLDS = {
    "mem_available_frac": 0.0,
    "load1_per_cpu": 1_000_000.0,
    "disk_free": 0,
}


def _make_repo(repo: Path, files: dict[str, str]) -> str:
    subprocess.run(["git", "init", "-b", "main", str(repo)], check=True, capture_output=True)
    for key, value in (("user.name", "hierarchy-test"), ("user.email", "hierarchy@test"),
                       ("gc.auto", "0")):
        subprocess.run(["git", "-C", str(repo), "config", key, value], check=True)
    for name, content in files.items():
        (repo / name).write_text(content, encoding="utf-8")
    subprocess.run(["git", "-C", str(repo), "add", "-A"], check=True, capture_output=True)
    subprocess.run(["git", "-C", str(repo), "commit", "-m", "initial"],
                   check=True, capture_output=True)
    return subprocess.run(["git", "-C", str(repo), "rev-parse", "HEAD"],
                          check=True, capture_output=True, text=True).stdout.strip()


def _task(
    session_dir: Path,
    repo: Path,
    base: str,
    task_id: str,
    *,
    target_file: str,
    depends_on: list[str] | None = None,
    fail: bool = False,
) -> dict[str, Any]:
    marker = f"// FAIL: {task_id}" if fail else f"// {task_id}"
    return {
        "task_id": task_id,
        "task": f"edit {target_file}",
        "repo": str(repo),
        "worktree_path": str(session_dir / f"wt-{task_id}"),
        "branch": f"wt-{task_id}",
        "worker": HIERARCHY_WORKER,
        "target_file": target_file,
        "marker": marker,
        "write_marker": True,
        "base_commit": base,
        "depends_on": list(depends_on or []),
        "provider_env_keys": [
            "TRACE_FILE",
            "PAYLOAD_DIR",
            "WORKER_DELAY_S",
        ],
        "resource_thresholds": TEST_RESOURCE_THRESHOLDS,
    }


def _run_with_env(plan: dict[str, Any], session_dir: Path, **env: str) -> PlanResult:
    """Run plan with the hierarchy worker env hooks declared via monkeypatching."""
    for name, value in env.items():
        if value is not None:
            os.environ[name] = value
        else:
            os.environ.pop(name, None)
    try:
        return asyncio.run(run_plan(session_dir, plan))
    finally:
        for name in ("TRACE_FILE", "PAYLOAD_DIR", "WORKER_DELAY_S"):
            os.environ.pop(name, None)


def _trace_lines(session_dir: Path) -> list[str]:
    path = session_dir / "trace.log"
    if not path.exists():
        return []
    return [line.strip() for line in path.read_text(encoding="utf-8").splitlines() if line.strip()]


def _enter_exit_intervals(lines: list[str]) -> dict[str, tuple[int, int]]:
    """Return ``{task_id: (enter_line, exit_line)}`` from a trace."""
    intervals: dict[str, tuple[int, int]] = {}
    for index, line in enumerate(lines):
        parts = line.split()
        if not parts:
            continue
        if parts[0] == "ENTER" and len(parts) >= 2:
            intervals.setdefault(parts[1], (index, -1))
        elif parts[0] == "EXIT" and len(parts) >= 2:
            entry = intervals.get(parts[1])
            if entry is not None:
                intervals[parts[1]] = (entry[0], index)
    return intervals


def _peak_concurrency(lines: list[str]) -> int:
    in_flight = 0
    peak = 0
    for line in lines:
        parts = line.split()
        if not parts:
            continue
        if parts[0] == "ENTER":
            in_flight += 1
            peak = max(peak, in_flight)
        elif parts[0] == "EXIT":
            in_flight -= 1
    return peak


# ---------------------------------------------------------------------------
# 1. Exact ready waves — dependency order is respected at the wave boundary.
# ---------------------------------------------------------------------------


def test_static_waves_dispatch_in_dependency_order(tmp_path: Path) -> None:
    session_dir = tmp_path / "session"
    repo = session_dir / "repo"
    files = {f"{name}.txt": f"file {name}\n" for name in ("root", "a", "b", "a1")}
    base = _make_repo(repo, files)

    plan = {
        "tasks": [
            _task(session_dir, repo, base, "root", target_file="root.txt"),
            _task(session_dir, repo, base, "a", target_file="a.txt", depends_on=["root"]),
            _task(session_dir, repo, base, "b", target_file="b.txt", depends_on=["root"]),
            _task(session_dir, repo, base, "a1", target_file="a1.txt", depends_on=["a"]),
        ]
    }

    result = _run_with_env(plan, session_dir, TRACE_FILE=str(session_dir / "trace.log"))

    assert result.exit_code == 0
    statuses = {r.task_id: r.status for r in result.results}
    assert statuses == {"root": "succeeded", "a": "succeeded",
                        "b": "succeeded", "a1": "succeeded"}

    intervals = _enter_exit_intervals(_trace_lines(session_dir))
    assert set(intervals) == {"root", "a", "b", "a1"}

    # Wave 1 (root) fully completes before any wave-2 node enters.
    root_enter, root_exit = intervals["root"]
    a_enter, _ = intervals["a"]
    b_enter, _ = intervals["b"]
    assert root_enter < root_exit
    assert root_exit < a_enter
    assert root_exit < b_enter

    # Wave 3 (a1) enters only after its parent a has exited.
    a_exit_index = intervals["a"][1]
    a1_enter = intervals["a1"][0]
    assert a_exit_index < a1_enter


def test_no_unready_dispatch_during_waves(tmp_path: Path) -> None:
    """A node never enters before its declared parent's exit (event-level)."""
    session_dir = tmp_path / "session"
    repo = session_dir / "repo"
    base = _make_repo(repo, {"d.txt": "d\n", "e.txt": "e\n"})
    plan = {
        "tasks": [
            _task(session_dir, repo, base, "d", target_file="d.txt"),
            _task(session_dir, repo, base, "e", target_file="e.txt", depends_on=["d"]),
        ]
    }
    result = _run_with_env(plan, session_dir, TRACE_FILE=str(session_dir / "trace.log"))

    assert result.exit_code == 0
    events = _event_kinds_by_task(session_dir)
    d_result = _seq_of(events, "d", "result")
    e_spawn = _seq_of(events, "e", "spawned")
    assert d_result is not None and e_spawn is not None
    assert d_result < e_spawn


def _event_kinds_by_task(session_dir: Path) -> dict[str, list[tuple[int, str]]]:
    """Read events.db directly and group ``(seq, kind)`` per task_id."""
    import sqlite3

    db = session_dir / ".cambium" / "events.db"
    out: dict[str, list[tuple[int, str]]] = {}
    with sqlite3.connect(db) as conn:
        for seq, kind, task_id in conn.execute(
            "SELECT seq, kind, task_id FROM events "
            "WHERE kind IN ('spawned', 'result', 'merge_committed') "
            "ORDER BY seq"
        ):
            out.setdefault(task_id or "", []).append((seq, kind))
    return out


def _seq_of(
    grouped: dict[str, list[tuple[int, str]]], task_id: str, kind: str
) -> int | None:
    for seq, current in grouped.get(task_id, []):
        if current == kind:
            return seq
    return None


# ---------------------------------------------------------------------------
# 2. Width enforcement — a wave never runs more than ``max_width`` concurrent.
# ---------------------------------------------------------------------------


def test_width_bound_caps_concurrent_dispatch(tmp_path: Path) -> None:
    session_dir = tmp_path / "session"
    repo = session_dir / "repo"
    files = {f"c{i}.txt": f"file c{i}\n" for i in range(1, 6)}
    files["root.txt"] = "root\n"
    base = _make_repo(repo, files)

    plan: dict[str, Any] = {
        "max_width": 2,
        "tasks": [
            _task(session_dir, repo, base, "root", target_file="root.txt"),
            *(
                _task(session_dir, repo, base, f"c{i}", target_file=f"c{i}.txt",
                      depends_on=["root"])
                for i in range(1, 6)
            ),
        ],
    }

    result = _run_with_env(
        plan,
        session_dir,
        TRACE_FILE=str(session_dir / "trace.log"),
        WORKER_DELAY_S="0.25000",
    )

    assert result.exit_code == 0
    statuses = {r.task_id: r.status for r in result.results}
    assert all(status == "succeeded" for status in statuses.values())

    lines = _trace_lines(session_dir)
    # Wave 2 (c1..c5, width=2) must never exceed 2 concurrent workers.
    assert _peak_concurrency(lines) <= 2
    # A non-trivial fan-out actually exercised the cap: at least two workers
    # were in flight at once during the wave.
    assert _peak_concurrency(lines) >= 2
    # And all five children plus the root were observed.
    intervals = _enter_exit_intervals(lines)
    assert set(intervals) == {"root", "c1", "c2", "c3", "c4", "c5"}


def test_width_bound_one_serializes_wave(tmp_path: Path) -> None:
    """``max_width=1`` makes each wave strictly serial (peak concurrency 1)."""
    session_dir = tmp_path / "session"
    repo = session_dir / "repo"
    files = {f"s{i}.txt": f"file s{i}\n" for i in range(1, 4)}
    files["root.txt"] = "root\n"
    base = _make_repo(repo, files)

    plan: dict[str, Any] = {
        "max_width": 1,
        "tasks": [
            _task(session_dir, repo, base, "root", target_file="root.txt"),
            *(
                _task(session_dir, repo, base, f"s{i}", target_file=f"s{i}.txt",
                      depends_on=["root"])
                for i in range(1, 4)
            ),
        ],
    }
    result = _run_with_env(
        plan,
        session_dir,
        TRACE_FILE=str(session_dir / "trace.log"),
        WORKER_DELAY_S="0.15000",
    )

    assert result.exit_code == 0
    assert _peak_concurrency(_trace_lines(session_dir)) == 1


# ---------------------------------------------------------------------------
# 3 + 4. Bounded child context and exact envelope keys.
# ---------------------------------------------------------------------------


def test_child_receives_bounded_parent_envelope(tmp_path: Path) -> None:
    session_dir = tmp_path / "session"
    repo = session_dir / "repo"
    base = _make_repo(repo, {"root.txt": "root\n", "child.txt": "child\n"})

    plan = {
        "tasks": [
            _task(session_dir, repo, base, "root", target_file="root.txt"),
            _task(session_dir, repo, base, "child", target_file="child.txt",
                  depends_on=["root"]),
        ]
    }
    result = _run_with_env(
        plan,
        session_dir,
        PAYLOAD_DIR=str(session_dir / "payloads"),
    )

    assert result.exit_code == 0
    child_payload = json.loads(
        (session_dir / "payloads" / "child.json").read_text(encoding="utf-8")
    )

    # The child sees its own spec fields.
    assert child_payload["task_id"] == "child"
    assert child_payload["target_file"] == "child.txt"
    assert child_payload["marker"] == "// child"

    # The child sees exactly one parent envelope, with the strict key set.
    assert "parent_envelope" in child_payload
    envelope = child_payload["parent_envelope"]
    assert set(envelope.keys()) == set(_ENVELOPE_KEYS)
    assert envelope["parent_task_id"] == "root"
    assert envelope["status"] == "succeeded"
    # The root worker produced one commit; it surfaces through the envelope.
    assert isinstance(envelope["commits"], list)
    assert len(envelope["commits"]) == 1
    assert envelope["files_changed"] == ["root.txt"]
    # No sibling context leaks: parent_envelope is one mapping, with root's id.
    assert envelope["summary"] == "" or isinstance(envelope["summary"], str)


def test_upward_envelope_carries_exactly_strict_keys() -> None:
    """The strict envelope projection never leaks a key outside the set."""
    worker_envelope = {
        "type": "result_envelope",
        "request_id": "rid",
        "task_id": "root",
        "generation": 1,
        "status": "succeeded",
        "exit_code": 0,
        "commits": ["sha-123"],
        "files_changed": ["root.txt"],
        "diff": "diff body",
        "diff_truncated": False,
        "summary": "did the work",
        "failure_reason": None,
        "started_at": 1.0,
        "ended_at": 2.0,
        "provider_metadata": {"provider": "demo", "model": "m"},
        # Scratchpad-style fields that must NEVER cross the boundary.
        "scratchpad": "secret reasoning",
        "trajectory": ["step1", "step2"],
    }
    projected = _strict_parent_envelope(worker_envelope, parent_task_id="root")
    assert set(projected.keys()) == set(_ENVELOPE_KEYS)
    assert projected["parent_task_id"] == "root"
    assert projected["unified_diff"] == "diff body"  # 'diff' -> 'unified_diff'
    assert projected["summary"] == "did the work"
    assert projected["commits"] == ["sha-123"]
    assert projected["files_changed"] == ["root.txt"]
    assert projected["metric_score"] is None
    assert projected["metric_breakdown"] == {}
    assert projected["status"] == "succeeded"
    # The scratchpad fields are not in the strict key set, so they are dropped.
    assert "scratchpad" not in projected
    assert "trajectory" not in projected
    assert "provider_metadata" not in projected


def test_strict_envelope_empty_when_no_parent_data() -> None:
    """A recovered parent (no retained envelope) yields defaults, not leaks."""
    projected = _strict_parent_envelope(None, parent_task_id="recovered")
    assert set(projected.keys()) == set(_ENVELOPE_KEYS)
    assert projected["parent_task_id"] == "recovered"
    assert projected["unified_diff"] == ""
    assert projected["commits"] == []
    assert projected["files_changed"] == []
    assert projected["status"] == ""


# ---------------------------------------------------------------------------
# 5. Failed children stop dependent admission.
# ---------------------------------------------------------------------------


def test_failed_node_cascades_skip_to_dependants(tmp_path: Path) -> None:
    session_dir = tmp_path / "session"
    repo = session_dir / "repo"
    files = {f"{name}.txt": f"file {name}\n" for name in ("root", "a", "b", "a1")}
    base = _make_repo(repo, files)

    plan = {
        "tasks": [
            _task(session_dir, repo, base, "root", target_file="root.txt"),
            _task(session_dir, repo, base, "a", target_file="a.txt", depends_on=["root"],
                  fail=True),
            _task(session_dir, repo, base, "b", target_file="b.txt", depends_on=["root"]),
            _task(session_dir, repo, base, "a1", target_file="a1.txt", depends_on=["a"]),
        ]
    }
    result = _run_with_env(
        plan,
        session_dir,
        TRACE_FILE=str(session_dir / "trace.log"),
    )

    assert result.exit_code != 0
    by_id = {r.task_id: r for r in result.results}
    assert by_id["root"].status == "succeeded"
    assert by_id["a"].status == "failed"
    assert by_id["b"].status == "succeeded"
    # The dependent was never spawned; it is marked failed with the cascade reason.
    assert by_id["a1"].status == "failed"
    assert by_id["a1"].reason == "dependency_failed:a"

    # The cascade never spawned a1.
    lines = _trace_lines(session_dir)
    assert not any(line.startswith("ENTER a1") for line in lines)
    assert not any(line.startswith("EXIT a1") for line in lines)
    intervals = _enter_exit_intervals(lines)
    assert "a1" not in intervals

    # Event log: a1 has no spawned/run_task events; a, b, root do.
    events = _event_kinds_by_task(session_dir)
    assert _seq_of(events, "a1", "spawned") is None
    assert _seq_of(events, "root", "spawned") is not None
    assert _seq_of(events, "a", "spawned") is not None
    assert _seq_of(events, "b", "spawned") is not None


def test_failed_root_cascades_to_whole_subtree(tmp_path: Path) -> None:
    """A failed root skips the entire tree (no node is ever spawned)."""
    session_dir = tmp_path / "session"
    repo = session_dir / "repo"
    base = _make_repo(repo, {"root.txt": "root\n", "a.txt": "a\n"})

    plan = {
        "tasks": [
            _task(session_dir, repo, base, "root", target_file="root.txt", fail=True),
            _task(session_dir, repo, base, "a", target_file="a.txt", depends_on=["root"]),
        ]
    }
    result = _run_with_env(
        plan, session_dir, TRACE_FILE=str(session_dir / "trace.log")
    )

    assert result.exit_code != 0
    by_id = {r.task_id: r for r in result.results}
    assert by_id["root"].status == "failed"
    assert by_id["a"].status == "failed"
    assert by_id["a"].reason == "dependency_failed:root"
    events = _event_kinds_by_task(session_dir)
    assert _seq_of(events, "a", "spawned") is None


# ---------------------------------------------------------------------------
# Deep-copy snapshot — the dispatcher hands distinct snapshot copies per node.
# ---------------------------------------------------------------------------


class _CapturingRuntime:
    """Minimal stand-in for ``_Runtime`` that records dispatched specs.

    Used only by the snapshot test to assert that ``_dispatch_static_waves``
    hands ``supervise_task`` the per-node deep-copied snapshot, not aliased
    input dicts.
    """

    def __init__(self) -> None:
        self._results: dict[str, TaskResult] = {}
        self._task_envelopes: dict[str, dict[str, Any]] = {}
        self.captured: list[dict[str, Any]] = []

    async def supervise_task(self, spec: dict[str, Any]) -> None:
        self.captured.append(spec)
        self._results[spec["task_id"]] = TaskResult(
            task_id=spec["task_id"], status="succeeded", exit_code=0
        )


def test_dispatcher_uses_independent_snapshots_per_node(tmp_path: Path) -> None:
    specs = [
        {
            "task_id": "root",
            "task": "r",
            "repo": "/tmp/repo",
            "worktree_path": "/tmp/wt-root",
            "branch": "wt-root",
            "target_file": "root.txt",
            "marker": "// root",
            "write_marker": True,
            "base_commit": "deadbeef",
            "depends_on": [],
            "extra_block": [1, 2, 3],
            "nested": {"items": [9, 8, 7]},
        },
        {
            "task_id": "child",
            "task": "c",
            "repo": "/tmp/repo",
            "worktree_path": "/tmp/wt-child",
            "branch": "wt-child",
            "target_file": "child.txt",
            "marker": "// child",
            "write_marker": True,
            "base_commit": "deadbeef",
            "depends_on": ["root"],
            "extra_block": [1, 2, 3],
            "nested": {"items": [9, 8, 7]},
        },
    ]

    runtime = _CapturingRuntime()
    # Build the same validated tree the supervisor builds.
    tree_payload = {
        "tasks": [
            {
                "task_id": s["task_id"],
                "kind": "feature",
                "depends_on": list(s["depends_on"]),
                "spec": s,
            }
            for s in specs
        ]
    }
    tree = build_tree(tree_payload)
    topological_order(tree)

    asyncio.run(_dispatch_static_waves(runtime, tree, width_limit=8))

    assert len(runtime.captured) == 2
    by_id = {spec["task_id"]: spec for spec in runtime.captured}
    assert set(by_id) == {"root", "child"}

    # Distinct top-level dict objects per node.
    assert by_id["root"] is not by_id["child"]
    # The input plan dicts were deep-copied by build_tree: nested mutables are
    # not aliased across the two dispatched specs.
    assert by_id["root"]["extra_block"] is not by_id["child"]["extra_block"]
    assert by_id["root"]["nested"] is not by_id["child"]["nested"]
    assert by_id["root"]["nested"]["items"] is not by_id["child"]["nested"]["items"]
    # Mutating one snapshot does not corrupt the other.
    by_id["root"]["extra_block"].append(999)
    by_id["root"]["nested"]["items"].append(999)
    assert by_id["child"]["extra_block"] == [1, 2, 3]
    assert by_id["child"]["nested"]["items"] == [9, 8, 7]
    # And the original plan dicts are not mutated retroactively.
    assert specs[0]["extra_block"] == [1, 2, 3]
    assert specs[1]["nested"]["items"] == [9, 8, 7]

    # Each dispatched spec carries a parent_envelope (None parent for the root).
    assert by_id["root"]["parent_envelope"]["parent_task_id"] is None
    assert by_id["child"]["parent_envelope"]["parent_task_id"] == "root"
    assert set(by_id["child"]["parent_envelope"].keys()) == set(_ENVELOPE_KEYS)


# ---------------------------------------------------------------------------
# Canary regression — a flat plan ignores ``max_width`` and fans out fully.
# ---------------------------------------------------------------------------


def test_flat_plan_ignores_max_width_and_preserves_canary(tmp_path: Path) -> None:
    """A flat plan (no ``depends_on``) never applies the wave width bound.

    Pass ``max_width=1`` explicitly: if the flat path applied it, only one
    worker could be spawned before the first result. The historical canary
    behavior is many workers in flight at once, so we assert several spawned
    events land before the first result.
    """
    session_dir = tmp_path / "session"
    repo = session_dir / "repo"
    files = {f"g{i}.txt": f"file g{i}\n" for i in range(1, 5)}
    base = _make_repo(repo, files)
    plan = {
        "tasks": [
            _task(session_dir, repo, base, f"g{i}", target_file=f"g{i}.txt")
            for i in range(1, 5)
        ]
    }

    os.environ["WORKER_DELAY_S"] = "0.50000"
    try:
        result = asyncio.run(run_plan(session_dir, plan, max_width=1))
    finally:
        os.environ.pop("WORKER_DELAY_S", None)

    assert result.exit_code == 0
    assert {r.task_id: r.status for r in result.results} == {
        f"g{i}": "succeeded" for i in range(1, 5)
    }

    events = _event_kinds_by_task_full(session_dir)
    spawned_seq = sorted(seq for seq, kind, _ in events if kind == "spawned")
    result_seq = sorted(seq for seq, kind, _ in events if kind == "result")
    assert spawned_seq and result_seq
    # ``max_width=1`` is ignored on the flat path: at least three workers are
    # spawned before the first result arrives (a width=1 wave would allow one).
    before_first_result = sum(1 for seq in spawned_seq if seq < result_seq[0])
    assert before_first_result >= 3


def _event_kinds_by_task_full(session_dir: Path) -> list[tuple[int, str, str]]:
    """Read events.db directly; return ``(seq, kind, task_id)`` rows."""
    import sqlite3

    db = session_dir / ".cambium" / "events.db"
    out: list[tuple[int, str, str]] = []
    with sqlite3.connect(db) as conn:
        for seq, kind, task_id in conn.execute(
            "SELECT seq, kind, task_id FROM events "
            "WHERE kind IN ('spawned', 'result') ORDER BY seq"
        ):
            out.append((seq, kind, task_id or ""))
    return out
