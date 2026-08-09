"""Vertical-slice end-to-end scenario: real supervisor + real worker process.

Drives the real spawn path (asyncio subprocess + pipes + git) through
the public ``cambium.supervisor.run_session`` API, with the fake worker
script as the worker process. No mocks, no network.

S01-aligned happy path: ready -> run_task -> result_envelope
(status=succeeded) -> exit_message, gate passes, ff-only merge lands the
edit on ``main``, supervisor exits 0.

Negative path: the worker is told not to write the marker; the gate
fails, the result is failed, and nothing is merged.
"""

from __future__ import annotations

import asyncio
import json
import subprocess
from pathlib import Path

from cambium.supervisor import run_session

WORKER = str(Path(__file__).resolve().parents[2] / "scripts" / "fake_worker.py")
MARKER = "// cambium-slice"
GATE = "grep -q '// cambium-slice' hello.txt"


def _make_scratch(repo: Path) -> str:
    subprocess.run(["git", "init", "-b", "main", str(repo)], check=True, capture_output=True)
    subprocess.run(["git", "-C", str(repo), "config", "user.name", "slice-test"], check=True)
    subprocess.run(["git", "-C", str(repo), "config", "user.email", "slice@test"], check=True)
    subprocess.run(["git", "-C", str(repo), "config", "gc.auto", "0"], check=True)
    (repo / "hello.txt").write_text("hello from the vertical slice\n")
    subprocess.run(["git", "-C", str(repo), "add", "hello.txt"], check=True)
    subprocess.run(["git", "-C", str(repo), "commit", "-m", "initial"], check=True, capture_output=True)
    return subprocess.run(
        ["git", "-C", str(repo), "rev-parse", "HEAD"], check=True, capture_output=True, text=True
    ).stdout.strip()


def _spec(session_dir: Path, *, write_marker: bool) -> dict:
    return {
        "task_id": "slice-001",
        "worker": WORKER,
        "scratch_repo": str(session_dir / "scratch"),
        "worktree_path": str(session_dir / "wt"),
        "branch": "wt-slice-001",
        "target_file": "hello.txt",
        "marker": MARKER,
        "write_marker": write_marker,
        "gate": GATE,
        "spec": "append the cambium-slice marker line to the target file",
    }


def _load_events(session_dir: Path) -> list[dict]:
    path = session_dir / ".cambium" / "events.jsonl"
    return [json.loads(line) for line in path.read_text().splitlines() if line.strip()]


def _protocol_sequence(events: list[dict]) -> list[str]:
    kinds = {"init", "ready", "run_task", "result", "exit"}
    return [e["kind"] for e in events if e["kind"] in kinds]


def test_vertical_slice_happy_path(tmp_path) -> None:
    session_dir = tmp_path / "session"
    scratch = session_dir / "scratch"
    _make_scratch(scratch)
    spec = _spec(session_dir, write_marker=True)

    result = asyncio.run(run_session(session_dir, spec))

    assert result.status == "succeeded"
    assert result.exit_code == 0
    assert result.worker_exit_code == 0
    assert result.worker_status == "succeeded"
    assert result.gate_exit_code == 0
    assert result.merge_sha is not None
    assert (scratch / "hello.txt").read_text() == "hello from the vertical slice\n// cambium-slice\n"

    events = _load_events(session_dir)
    assert _protocol_sequence(events) == ["init", "ready", "run_task", "result", "exit"]


def test_vertical_slice_gate_failure_no_merge(tmp_path) -> None:
    session_dir = tmp_path / "session"
    scratch = session_dir / "scratch"
    base = _make_scratch(scratch)
    spec = _spec(session_dir, write_marker=False)

    result = asyncio.run(run_session(session_dir, spec))

    assert result.status == "failed"
    assert result.exit_code == 1
    assert result.worker_exit_code == 0
    assert result.worker_status == "failed"
    assert result.gate_exit_code is not None and result.gate_exit_code != 0
    assert result.merge_sha is None
    assert MARKER not in (scratch / "hello.txt").read_text()
    tip = subprocess.run(
        ["git", "-C", str(scratch), "rev-parse", "main"], check=True, capture_output=True, text=True
    ).stdout.strip()
    assert tip == base  # main never advanced; no merge

    events = _load_events(session_dir)
    assert _protocol_sequence(events) == ["init", "ready", "run_task", "result", "exit"]
