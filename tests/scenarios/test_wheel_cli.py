"""Clean-wheel scenario: a plain wheel install must be fully functional.

Builds a wheel, installs it into a throwaway ``--target`` directory, and
drives the installed ``cambium`` console script from an unrelated working
directory whose ``PYTHONPATH`` points only at the install.  The checkout,
its ``src/`` tree, and its ``scripts/fake_worker.py`` fixture are never on
the path, so any accidental checkout dependency fails loudly.

Covered commands:
  - default ``cambium supervisor`` (must spawn ``cambium.worker``, not
    ``scripts/fake_worker.py``),
  - ``cambium bench report`` and ``cambium bench gate``,
  - ``cambium module-test example``.
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]


def _require_reference_module() -> None:
    if not (REPO_ROOT / "src" / "cambium" / "modules" / "example").is_dir():
        pytest.skip("reference module cambium.modules.example is absent")


def _build_and_install_wheel(site_dir: Path) -> Path:
    dist = site_dir / "dist"
    dist.mkdir(parents=True, exist_ok=True)
    uv = shutil.which("uv")
    assert uv is not None
    subprocess.run(
        [uv, "build", "--wheel", "--out-dir", str(dist)],
        cwd=REPO_ROOT,
        check=True,
        capture_output=True,
        text=True,
    )
    wheel = next(dist.glob("cambium-*.whl"))
    subprocess.run(
        [
            uv,
            "pip",
            "install",
            "--python",
            sys.executable,
            "--target",
            str(site_dir),
            str(wheel),
        ],
        check=True,
        capture_output=True,
        text=True,
    )
    return wheel


def _installed_cli(site_dir: Path) -> Path:
    cli = site_dir / "bin" / "cambium"
    assert cli.is_file(), f"installed console script missing: {cli}"
    return cli


def _run(
    cli: Path,
    unrelated_cwd: Path,
    site_dir: Path,
    *args: str,
) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [str(cli), *args],
        cwd=unrelated_cwd,
        env={**os.environ, "PYTHONPATH": str(site_dir)},
        capture_output=True,
        text=True,
        timeout=300,
    )


def test_plain_wheel_install_runs_supervisor_bench_and_module_test(tmp_path) -> None:
    _require_reference_module()
    site = tmp_path / "site-packages"
    unrelated = tmp_path / "unrelated"
    unrelated.mkdir()
    _build_and_install_wheel(site)
    cli = _installed_cli(site)

    # Default `cambium supervisor`: must use cambium.worker, not scripts/fake_worker.py.
    session_dir = tmp_path / "session"
    supervisor = _run(
        cli, unrelated, site, "supervisor", "--session-dir", str(session_dir)
    )
    assert supervisor.returncode == 0, supervisor.stdout + supervisor.stderr
    assert "status=succeeded" in supervisor.stdout, supervisor.stdout
    spawned = [
        event for event in _read_events(session_dir) if event["kind"] == "spawned"
    ]
    assert spawned, "no spawned event recorded"
    assert "cambium.worker" in spawned[0]["payload"]["worker"], spawned
    assert "fake_worker.py" not in spawned[0]["payload"]["worker"], spawned

    # `cambium bench report` then `cambium bench gate` from the wheel.
    bench_root = tmp_path / "baselines"
    report = _run(cli, unrelated, site, "bench", "report", "--bench-root", str(bench_root))
    assert report.returncode == 0, report.stdout + report.stderr
    baseline = json.loads((bench_root / "should_decompose" / "baseline.json").read_text())
    assert baseline["module"] == "should_decompose"
    assert baseline["split_digests"]

    gate = _run(cli, unrelated, site, "bench", "gate", "--bench-root", str(bench_root))
    assert gate.returncode == 0, gate.stdout + gate.stderr
    assert "DRIFT" not in gate.stdout

    # `cambium module-test example` passes its own conformance gate.
    module_test = _run(cli, unrelated, site, "module-test", "example")
    assert module_test.returncode == 0, module_test.stdout + module_test.stderr
    assert "passed=" in module_test.stdout + module_test.stderr


def _read_events(session_dir: Path) -> list[dict]:
    import sqlite3

    with sqlite3.connect(session_dir / ".cambium" / "events.db") as connection:
        rows = connection.execute(
            "SELECT seq, kind, payload, ts, monotonic_ms, task_id, worker_id, "
            "generation, request_id FROM events ORDER BY seq"
        ).fetchall()
    return [
        {
            "seq": row[0],
            "kind": row[1],
            "payload": json.loads(row[2]),
            "ts": row[3],
            "monotonic_ms": row[4],
            "task_id": row[5],
            "worker_id": row[6],
            "generation": row[7],
            "request_id": row[8],
        }
        for row in rows
    ]
