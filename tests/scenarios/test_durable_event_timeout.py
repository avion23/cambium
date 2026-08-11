"""Durable terminal-event timeout scenario (supervisor hardening).

``persist_terminal`` bounds the wait for a durable merge event with
``CAMBIUM_DURABLE_EVENT_TIMEOUT_S`` (default 5.0). When the store append
path is slower than the bound, the merge fails closed (a ``RuntimeError``
from ``persist_terminal``) while the in-flight emit still appends the event
to the store: only the wait is bounded, never the append itself.
"""

from __future__ import annotations

import asyncio
import subprocess
import time
from pathlib import Path

import pytest

from cambium.merge import StagingCleanupError
from cambium.store import EventStore
from cambium.supervisor import _Runtime


def _run(cwd: Path, *args: str, check: bool = True) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ["git", *args], cwd=str(cwd), capture_output=True, text=True, check=check
    )


def _rev(cwd: Path, rev: str) -> str:
    return _run(cwd, "rev-parse", "--verify", f"{rev}^{{commit}}").stdout.strip()


def _init_repo(repo: Path) -> str:
    subprocess.run(["git", "init", "-b", "main", str(repo)], check=True, capture_output=True)
    for key, value in (("user.name", "durable-test"), ("user.email", "durable@test"),
                       ("gc.auto", "0")):
        _run(repo, "config", key, value)
    (repo / "base.txt").write_text("base\n")
    _run(repo, "add", "base.txt")
    _run(repo, "commit", "-m", "initial")
    return _rev(repo, "HEAD")


def _worker_commit(repo: Path, branch: str, wt: Path, files: dict[str, str], from_: str) -> str:
    _run(repo, "worktree", "add", "-b", branch, str(wt), from_)
    for name, content in files.items():
        (wt / name).write_text(content)
        _run(wt, "add", name)
    _run(wt, "commit", "-m", f"{branch}: {','.join(files)}")
    return _rev(wt, "HEAD")


def test_durable_event_timeout_fails_merge_closed_but_event_lands(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A slow store append exceeds the durable-emit bound: the merge fails
    closed while the in-flight emit still lands the terminal event.

    The append path is slowed by monkeypatching ``EventStore.append`` with a
    bounded 0.5s sleep (no async hook exists between emit and append).
    """
    monkeypatch.setenv("CAMBIUM_DURABLE_EVENT_TIMEOUT_S", "0.2")
    repo = tmp_path / "repo"
    base = _init_repo(repo)
    _worker_commit(repo, "worker", tmp_path / "worker", {"worker.txt": "ok\n"}, base)
    staging = tmp_path / "staging"
    store = EventStore(tmp_path / ".cambium" / "events.db")
    real_append = EventStore.append

    def slow_append(instance: EventStore, event: dict) -> int | None:
        time.sleep(0.5)  # comfortably exceed the 0.2s durable-emit wait
        return real_append(instance, event)

    monkeypatch.setattr(EventStore, "append", slow_append)

    async def quarantine() -> None:
        runtime = _Runtime(tmp_path, store)
        seq = runtime._make_sequencer("durable-timeout")
        seq.prepare_staging(repo, staging, "worker", "main")
        (staging / "evidence.bin").write_bytes(b"must not be stranded")
        try:
            await asyncio.to_thread(seq.cleanup_staging, repo)
        finally:
            # Keep the loop alive so the in-flight emit (still waiting on the
            # slow append) completes and appends the event before shutdown.
            await asyncio.sleep(1.0)

    try:
        with pytest.raises(StagingCleanupError) as excinfo:
            asyncio.run(quarantine())
        cause = excinfo.value.__cause__
        assert isinstance(cause, RuntimeError)
        assert "not persisted within 0.2s" in str(cause)
        deadline = time.monotonic() + 10.0
        landed: list[dict] = []
        while time.monotonic() < deadline:
            landed = [
                event for event in store.events_after(0)
                if event["kind"] == "merge_staging_quarantined"
            ]
            if landed:
                break
            time.sleep(0.05)
        assert len(landed) == 1
        assert landed[0]["task_id"] == "durable-timeout"
        assert landed[0]["payload"]["quarantine_id"]
    finally:
        store.close()
