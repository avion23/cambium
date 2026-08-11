"""Deterministic smoke test for scripts/measure_worker_coldstart.py.

Drives the measurement script end-to-end with --tasks 1 against a minimal
throwaway source repo (no provider credentials, no network) and asserts the
script exits 0 and emits a parseable SUMMARY_JSON with the expected phase
deltas. This is a scenario test of the measurement harness, not of the
supervisor: it only locks the script's output contract.
"""

from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
SCRIPT = ROOT / "scripts" / "measure_worker_coldstart.py"
FIXTURE_REL = "tests/fixtures/e2e/cambium-e2e-marker.txt"


def _make_source_repo(repo: Path) -> None:
    """Build a minimal source repo containing the e2e marker fixture.

    The measurement script clones ``--source`` and the marker worker edits
    ``tests/fixtures/e2e/cambium-e2e-marker.txt`` inside the clone, so the
    source only needs to carry that one fixture file on the main branch.
    """
    subprocess.run(["git", "init", "-b", "main", str(repo)], check=True, capture_output=True)
    subprocess.run(["git", "-C", str(repo), "config", "user.name", "coldstart-test"], check=True)
    subprocess.run(["git", "-C", str(repo), "config", "user.email", "coldstart@test"], check=True)
    fixture = repo / FIXTURE_REL
    fixture.parent.mkdir(parents=True, exist_ok=True)
    fixture.write_text("coldstart-test fixture baseline\n", encoding="utf-8")
    subprocess.run(["git", "-C", str(repo), "add", "-A"], check=True, capture_output=True)
    subprocess.run(
        ["git", "-C", str(repo), "commit", "-m", "baseline"], check=True, capture_output=True
    )


@pytest.mark.slow
def test_measure_worker_coldstart_runs_and_emits_parseable_summary(tmp_path) -> None:
    source = tmp_path / "source"
    _make_source_repo(source)
    clone = tmp_path / "clone"

    cmd = [
        sys.executable, str(SCRIPT),
        "--repo", str(clone),
        "--source", str(source),
        "--tasks", "1",
        "--python", sys.executable,
        "--pythonpath", str(ROOT / "src"),
    ]
    env = dict(__import__("os").environ)
    result = subprocess.run(cmd, capture_output=True, text=True, env=env, timeout=120)

    assert result.returncode == 0, (
        f"script exited {result.returncode}\nstdout:\n{result.stdout}\nstderr:\n{result.stderr}"
    )

    # The summary block is emitted after a literal "SUMMARY_JSON" banner.
    assert "SUMMARY_JSON" in result.stdout
    assert "COLD vs WARM" in result.stdout
    assert "REUSE PROJECTION" in result.stdout
    assert "spawn_to_ready" in result.stdout

    json_block = result.stdout.split("SUMMARY_JSON", 1)[1]
    start = json_block.find("{")
    assert start != -1, f"no JSON object in SUMMARY_JSON block:\n{json_block[:400]}"
    summary, _end = json.JSONDecoder().raw_decode(json_block[start:])

    assert summary["tasks"] == 1
    assert summary["reuse_floor_ms"] == 5.0
    assert summary["dspy_spawn_to_ready_ms"] == 2221.2
    assert isinstance(summary["records"], list) and len(summary["records"]) == 1
    record = summary["records"][0]
    assert record["returncode"] == 0
    # Every phase the script promises must be present and non-null for a
    # successful one-task marker run.
    for key in (
        "worktree_ms",
        "spawn_to_init_ms",
        "init_to_ready_ms",
        "spawn_to_ready_ms",
        "work_ms",
        "merge_ms",
        "setup_ms",
        "total_task_ms",
    ):
        assert key in record, f"missing phase {key}"
        assert record[key] is not None, f"phase {key} is None"
        assert record[key] >= 0, f"phase {key} negative: {record[key]}"
    # Cold spawn-to-ready is the headline measurement; it must be plausible.
    assert 10 <= record["spawn_to_ready_ms"] <= 60_000
