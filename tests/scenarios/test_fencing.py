"""Scenario tests for the worktree-local generation fence."""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path

from cambium.fencing import (
    FENCE_FILE,
    read_generation,
    write_generation,
)


def test_generation_is_written_after_git_clean_fd_during_recovery(tmp_path: Path) -> None:
    """``git clean -fd`` removes the fence; recovery writes it afterwards."""
    repo = tmp_path / "repo"
    subprocess.run(
        ["git", "init", "-b", "main", str(repo)],
        check=True,
        capture_output=True,
    )

    # Writing before clean is intentionally removed because .cambium is untracked.
    write_generation(repo, 1)
    subprocess.run(
        ["git", "-C", str(repo), "clean", "-fd"],
        check=True,
        capture_output=True,
    )
    assert read_generation(repo) == 0
    assert not (repo / FENCE_FILE).exists()

    # Architecture §7.5 writes the new fence after reset/clean, so it survives.
    assert write_generation(repo, 2) == 2
    assert read_generation(repo) == 2


def test_fence_dir_and_file_are_private_under_permissive_umask(tmp_path: Path) -> None:
    """umask 0022 must not widen .cambium (0700) or the generation file (0600)."""
    session = tmp_path / "session"
    script = (
        "import os, stat, sys\n"
        "from pathlib import Path\n"
        "os.umask(0o022)\n"
        "from cambium.fencing import next_generation, read_generation, write_generation\n"
        "root = Path(sys.argv[1])\n"
        "next_generation(root / 'wt')\n"
        "assert stat.S_IMODE((root / 'wt' / '.cambium').stat().st_mode) == 0o700\n"
        "assert stat.S_IMODE((root / 'wt' / '.cambium' / 'generation').stat().st_mode) == 0o600\n"
        "write_generation(root / 'wt2', 5)\n"
        "assert stat.S_IMODE((root / 'wt2' / '.cambium').stat().st_mode) == 0o700\n"
        "assert stat.S_IMODE((root / 'wt2' / '.cambium' / 'generation').stat().st_mode) == 0o600\n"
        "assert read_generation(root / 'wt2') == 5\n"
    )
    proc = subprocess.run(
        [sys.executable, "-c", script, str(session)], capture_output=True, text=True, timeout=120
    )
    assert proc.returncode == 0, proc.stderr
