"""Worktree-local generation fencing.

The generation file is the durable fence for a worker's worktree. Recovery
must call :func:`write_generation` *after* ``git reset --hard`` and
``git clean -fd``: the latter removes an untracked ``.cambium`` directory.
"""

from __future__ import annotations

import os
import tempfile
from pathlib import Path

try:
    import fcntl
except ImportError:  # pragma: no cover - exercised on Windows
    fcntl = None

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
    """Advance the worktree fence and return the new generation.

    Recovery is expected to have one recovery process.  On POSIX, an exclusive
    ``fcntl.flock`` on the fence file also serializes concurrent callers while
    they read and write the generation.  ``fcntl`` is not available in the
    standard library on Windows, so the Windows fallback relies on the
    single-recovery-process assumption and does not provide cross-process
    locking.
    """
    fence_dir = Path(worktree) / ".cambium"
    fence_dir.mkdir(parents=True, exist_ok=True)
    fence_path = fence_dir / "generation"

    with fence_path.open("a+", encoding="ascii", newline="\n") as fence:
        if fcntl is not None:
            fcntl.flock(fence.fileno(), fcntl.LOCK_EX)
        try:
            fence.seek(0)
            try:
                current = int(fence.read().strip(), 10)
            except (UnicodeError, ValueError):
                current = 0
            if current < 0:
                current = 0

            generation = current + 1
            fence.seek(0)
            fence.truncate()
            fence.write(f"{generation}\n")
            fence.flush()
            os.fsync(fence.fileno())
            return generation
        finally:
            if fcntl is not None:
                fcntl.flock(fence.fileno(), fcntl.LOCK_UN)


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
