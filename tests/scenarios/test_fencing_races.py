from __future__ import annotations

import threading
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import pytest

from cambium.fencing import (
    GenerationConflictError,
    is_cache_artifact_path,
    next_generation,
    read_generation,
    write_generation,
)


@pytest.mark.parametrize("write_order", ("low-high", "high-low"))
def test_next_and_write_generation_keep_the_highest_token(
    tmp_path: Path, write_order: str
) -> None:
    worktree = tmp_path / write_order
    low = next_generation(worktree)

    if write_order == "low-high":
        assert write_generation(worktree, low) == low
        high = next_generation(worktree)
        assert write_generation(worktree, high) == high
    else:
        high = next_generation(worktree)
        assert write_generation(worktree, high) == high
        with pytest.raises(GenerationConflictError):
            write_generation(worktree, low)

    assert read_generation(worktree) == high


def test_simultaneous_writes_are_atomic_and_lose_safe(tmp_path: Path) -> None:
    worktree = tmp_path / "worktree"
    start = threading.Barrier(2)

    def publish(generation: int) -> tuple[int, int | None]:
        start.wait()
        try:
            return generation, write_generation(worktree, generation)
        except GenerationConflictError:
            return generation, None

    with ThreadPoolExecutor(max_workers=2) as pool:
        outcomes = list(pool.map(publish, (3, 9)))

    assert read_generation(worktree) == 9
    assert {generation for generation, result in outcomes if result is None} <= {3}
    assert any(generation == 9 and result == 9 for generation, result in outcomes)


def test_cache_artifact_paths_do_not_follow_symlink_aliases(tmp_path: Path) -> None:
    artifact = tmp_path / ".pytest_cache"
    artifact.mkdir()
    link = tmp_path / "cache-link"
    link.symlink_to(artifact, target_is_directory=True)

    assert is_cache_artifact_path(f"{link.name}/state") is False


@pytest.mark.parametrize(
    ("path", "expected"),
    (
        (".pytest_cache/CACHEDIR.TAG", True),
        ("src/__pycache__/module.py", True),
        ("src/.mypy_cache/nested/state", True),
        ("./src/__pycache__/module.py", True),
        ("src/../.pytest_cache/CACHEDIR.TAG", True),
        ("src/module.pyc", True),
        ("cache-link/__pycache__/module.py", True),
        (".pytest_cache_backup/CACHEDIR.TAG", False),
        ("src/.pytest_cache_backup/state", False),
        ("src/.pytest_cache/../tracked.py", False),
        ("../.pytest_cache/CACHEDIR.TAG", False),
        ("/tmp/.pytest_cache/CACHEDIR.TAG", False),
        ("cache-link/state", False),
        ("src/module.pyc.bak", False),
    ),
)
def test_cache_artifact_paths_use_exact_normalized_components(
    path: str, expected: bool
) -> None:
    assert is_cache_artifact_path(path) is expected
