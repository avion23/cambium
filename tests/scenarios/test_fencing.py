"""Scenario tests for the worktree-local generation fence."""

from __future__ import annotations

import subprocess
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

from cambium.fencing import (
    FENCE_FILE,
    next_generation,
    read_generation,
    validate_worker_generation,
    write_generation,
)


def test_read_generation_defaults_to_zero_for_missing_or_invalid_file(tmp_path: Path) -> None:
    worktree = tmp_path / "worktree"

    assert read_generation(worktree) == 0

    fence_path = worktree / FENCE_FILE
    fence_path.parent.mkdir(parents=True)
    fence_path.write_text("not a generation\n", encoding="ascii")
    assert read_generation(worktree) == 0


def test_write_and_read_generation_round_trip_and_create_directory(tmp_path: Path) -> None:
    worktree = tmp_path / "worktree"

    assert write_generation(worktree, 7) == 7
    assert (worktree / ".cambium").is_dir()
    assert read_generation(worktree) == 7


def test_next_generation_increments_from_missing_and_existing_fence(tmp_path: Path) -> None:
    worktree = tmp_path / "worktree"

    assert next_generation(worktree) == 1
    assert next_generation(worktree) == 2
    assert read_generation(worktree) == 2


def test_concurrent_writes_are_atomic_and_an_ordered_write_wins(tmp_path: Path) -> None:
    worktree = tmp_path / "worktree"
    fence_path = worktree / FENCE_FILE
    writer_count = 4
    writes_per_writer = 40
    values = {
        writer * writes_per_writer + offset + 1
        for writer in range(writer_count)
        for offset in range(writes_per_writer)
    }
    expected_contents = {f"{value}\n" for value in values}
    start = threading.Barrier(writer_count)
    finished = threading.Event()
    corrupt_contents: list[str] = []

    def observe() -> None:
        while not finished.is_set():
            try:
                content = fence_path.read_text(encoding="ascii")
            except FileNotFoundError:
                continue
            if content not in expected_contents:
                corrupt_contents.append(content)
                return
            time.sleep(0)

    observer = threading.Thread(target=observe)
    observer.start()

    def write_series(writer: int) -> None:
        start.wait()
        first = writer * writes_per_writer
        for offset in range(writes_per_writer):
            write_generation(worktree, first + offset + 1)

    try:
        with ThreadPoolExecutor(max_workers=writer_count) as pool:
            futures = [pool.submit(write_series, writer) for writer in range(writer_count)]
            for future in futures:
                future.result()
    finally:
        finished.set()
        observer.join()

    assert corrupt_contents == []
    final_generation = 10_000
    assert write_generation(worktree, final_generation) == final_generation
    assert read_generation(worktree) == final_generation


def test_validate_worker_generation_rejects_stale_missing_and_zero_claims(
    tmp_path: Path,
) -> None:
    worktree = tmp_path / "worktree"

    assert not validate_worker_generation(worktree, None)
    assert not validate_worker_generation(worktree, 0)
    assert not validate_worker_generation(worktree, 1)

    write_generation(worktree, 3)
    assert validate_worker_generation(worktree, 3)
    assert not validate_worker_generation(worktree, 2)
    assert not validate_worker_generation(worktree, 4)
    assert not validate_worker_generation(worktree, None)


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
