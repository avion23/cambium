"""Worktree-local generation fencing.

The generation file is the durable fence for a worker's worktree. Recovery
must call :func:`write_generation` *after* ``git reset --hard`` and
``git clean -fd``: the latter removes an untracked ``.cambium`` directory.
"""

from __future__ import annotations

import os
import tempfile
from pathlib import Path

FENCE_FILE = ".cambium/generation"


def read_generation(worktree: Path) -> int:
    """Return the worktree's generation, or ``0`` when the fence is invalid."""
    path = Path(worktree) / FENCE_FILE
    try:
        generation = int(path.read_text(encoding="ascii").strip(), 10)
    except (OSError, UnicodeError, ValueError):
        return 0
    return generation if generation >= 0 else 0


def write_generation(worktree: Path, generation: int) -> int:
    """Atomically write and return a non-negative worktree generation.

    The temporary file is created in ``.cambium`` so ``os.replace`` is an
    atomic same-filesystem rename. The directory is created here because this
    function is the final step of worktree recovery, after ``git clean -fd``.
    """
    if isinstance(generation, bool) or not isinstance(generation, int):
        raise TypeError("generation must be an integer")
    if generation < 0:
        raise ValueError("generation must be non-negative")

    fence_dir = Path(worktree) / ".cambium"
    fence_dir.mkdir(parents=True, exist_ok=True)
    fence_path = fence_dir / "generation"
    fd, temporary_name = tempfile.mkstemp(
        prefix=".generation.", suffix=".tmp", dir=fence_dir
    )
    temporary_path = Path(temporary_name)
    try:
        with os.fdopen(fd, "w", encoding="ascii", newline="\n") as temporary:
            temporary.write(f"{generation}\n")
            temporary.flush()
            os.fsync(temporary.fileno())
        os.replace(temporary_path, fence_path)
    finally:
        try:
            temporary_path.unlink()
        except FileNotFoundError:
            pass
    return generation


def next_generation(worktree: Path) -> int:
    """Advance the worktree fence and return the new generation."""
    return write_generation(worktree, read_generation(worktree) + 1)


def validate_worker_generation(worktree: Path, worker_generation: int | None) -> bool:
    """Return whether a worker claims the current, present generation.

    Generation ``0`` is the missing/invalid-file sentinel, so it never
    validates a worker. A stale worker must be killed rather than trusted.
    """
    if (
        worker_generation is None
        or isinstance(worker_generation, bool)
        or not isinstance(worker_generation, int)
        or worker_generation <= 0
    ):
        return False
    current_generation = read_generation(worktree)
    return current_generation > 0 and worker_generation == current_generation
