"""Worktree-local generation fencing.

The generation file is the durable fence for a worker's worktree. Recovery
must call :func:`write_generation` *after* ``git reset --hard`` and
``git clean -fd``: the latter removes an untracked ``.cambium`` directory.
"""

from __future__ import annotations

import os
import posixpath
import tempfile
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path
from typing import Any, cast

try:
    import fcntl
except ImportError:  # pragma: no cover - exercised on Windows
    fcntl = cast(Any, None)

FENCE_FILE = ".cambium/generation"
GENERATION_LOCK_FILE = ".cambium/.generation.lock"

_CACHE_ARTIFACT_COMPONENTS = frozenset({
    ".pytest_cache", "__pycache__", ".mypy_cache", ".ruff_cache", ".tox",
    ".coverage", ".cache", ".venv", ".mise", ".python-version",
})


def is_cache_artifact_path(path: str) -> bool:
    """Whether a porcelain status path is an incidental cache/build artifact."""
    if not isinstance(path, str):
        return False
    normalized = posixpath.normpath(path)
    if normalized in {"", ".", ".."} or normalized.startswith(("/", "../")):
        return False
    components = normalized.split("/")
    return normalized.endswith(".pyc") or any(
        component in _CACHE_ARTIFACT_COMPONENTS for component in components
    )


class GenerationConflictError(RuntimeError):
    pass


def _fence_dir(worktree: Path) -> Path:
    fence_dir = Path(worktree) / ".cambium"
    fence_dir.mkdir(parents=True, exist_ok=True)
    try:
        os.chmod(fence_dir, 0o700)
    except OSError:
        pass
    return fence_dir


@contextmanager
def _generation_lock(worktree: Path) -> Iterator[Path]:
    fence_dir = _fence_dir(worktree)
    lock_path = fence_dir / Path(GENERATION_LOCK_FILE).name
    fd = os.open(
        lock_path,
        os.O_RDWR | os.O_CREAT | getattr(os, "O_CLOEXEC", 0),
        0o600,
    )
    try:
        try:
            os.chmod(lock_path, 0o600)
        except OSError:
            pass
        if fcntl is not None:
            fcntl.flock(fd, fcntl.LOCK_EX)
        try:
            yield fence_dir
        finally:
            if fcntl is not None:
                fcntl.flock(fd, fcntl.LOCK_UN)
    finally:
        os.close(fd)


def _read_generation_path(path: Path) -> int:
    try:
        generation = int(path.read_text(encoding="ascii").strip(), 10)
    except (OSError, UnicodeError, ValueError):
        return 0
    return generation if generation >= 0 else 0


def _write_generation_unlocked(fence_dir: Path, generation: int) -> None:
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


def read_generation(worktree: Path) -> int:
    """Return the worktree's generation, or ``0`` when the fence is invalid."""
    return _read_generation_path(Path(worktree) / FENCE_FILE)


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

    with _generation_lock(worktree) as fence_dir:
        current = _read_generation_path(fence_dir / "generation")
        if generation < current:
            raise GenerationConflictError(
                f"generation {generation} is older than persisted generation {current}"
            )
        if generation == current and generation > 0:
            return generation
        _write_generation_unlocked(fence_dir, generation)
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
    with _generation_lock(worktree) as fence_dir:
        generation = _read_generation_path(fence_dir / "generation") + 1
        _write_generation_unlocked(fence_dir, generation)
        return generation


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
