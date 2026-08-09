"""Tooling scenario tests: `cambium doctor` diagnostics + ruff hygiene.

No mocks: drive the real ``python -m cambium.doctor`` subprocess in the repo
root, both against a healthy repo and a session dir whose event store is
deliberately corrupt, and run ruff over ``src`` with the project's rules.
"""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]
DOCTOR = [sys.executable, "-m", "cambium.doctor"]


def _run_doctor(*args: str, cwd: Path = REPO_ROOT) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [*DOCTOR, *args], cwd=cwd, capture_output=True, text=True, timeout=300
    )


def test_doctor_exits_zero_on_healthy_repo() -> None:
    result = _run_doctor()
    assert result.returncode == 0, result.stdout + result.stderr
    assert "Summary:" in result.stdout
    assert "0 fail" in result.stdout
    assert "ALL CHECKS PASSED" in result.stdout


def test_doctor_fails_on_corrupt_event_store(tmp_path) -> None:
    session_dir = tmp_path / "session"
    db = session_dir / ".cambium" / "events.db"
    db.parent.mkdir(parents=True)
    db.write_bytes(b"this is not a sqlite database\ncorrupted\x00payload\n")

    result = _run_doctor("--session-dir", str(session_dir))

    assert result.returncode == 1, result.stdout + result.stderr
    assert "FAIL" in result.stdout
    assert "events.db" in result.stdout
    assert "1 fail" in result.stdout


def test_ruff_check_clean_on_src() -> None:
    result = subprocess.run(
        ["uv", "run", "--python", "3.14.7", "--with", "ruff", "ruff", "check", "src"],
        cwd=REPO_ROOT, capture_output=True, text=True, timeout=600,
    )
    assert result.returncode == 0, result.stdout + result.stderr
