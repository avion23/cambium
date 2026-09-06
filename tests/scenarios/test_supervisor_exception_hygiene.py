from __future__ import annotations

import asyncio
from pathlib import Path
from typing import Any, cast

import pytest

import cambium.supervisor as supervisor_module
from cambium.supervisor import WorkerHandle


class _Store:
    def __init__(self, append_error: BaseException | None = None) -> None:
        self.records: list[dict[str, Any]] = []
        self.append_error = append_error

    def append(self, record: dict[str, Any]) -> int:
        if self.append_error is not None:
            raise self.append_error
        self.records.append(record)
        return len(self.records)

    def events_after(self, _seq: int) -> list[dict[str, Any]]:
        return list(self.records)


class _PrepareCancelled:
    def prepare_staging(self, *_args: Any) -> str:
        raise asyncio.CancelledError


class _CleanupCancelled:
    def prepare_staging(self, *_args: Any) -> str:
        return "staging-tip"

    def publish_merge(self, *_args: Any) -> None:
        return

    def cleanup_staging(self, *_args: Any) -> None:
        raise asyncio.CancelledError


class _ObserverCancelled:
    def __init__(self) -> None:
        self.drained = False

    def prepare_staging(self, *_args: Any) -> str:
        return "staging-tip"

    def publish_merge(self, *_args: Any) -> None:
        return

    def cleanup_staging(self, *_args: Any) -> None:
        return

    def drain_events(self) -> list[tuple[str, dict[str, Any]]]:
        if self.drained:
            return []
        self.drained = True
        return [("merge_reconciled", {"task": "task", "new": "staging-tip"})]


def _merge_spec(tmp_path: Path) -> dict[str, Any]:
    return {
        "task_id": "task",
        "repo": str(tmp_path),
        "branch": "task",
        "base_commit": "base",
    }


def _runtime(
    tmp_path: Path,
    *,
    store: _Store | None = None,
    on_event: Any = None,
) -> supervisor_module._Runtime:
    runtime = supervisor_module._Runtime(
        tmp_path,
        _Store() if store is None else store,
        on_event=on_event,
    )

    async def git_stdout(*_args: Any, **_kwargs: Any) -> str:
        return "base"

    cast(Any, runtime)._git_stdout = git_stdout
    return runtime


def _set_sequencer(runtime: supervisor_module._Runtime, sequencer: Any) -> None:
    def make_sequencer(
        _task_id: str,
        _deferred_observers: list[tuple[dict[str, Any], bool]] | None = None,
    ) -> Any:
        return sequencer

    cast(Any, runtime)._make_sequencer = make_sequencer


def test_emit_propagates_cancellation_from_store(tmp_path: Path) -> None:
    runtime = _runtime(
        tmp_path,
        store=_Store(asyncio.CancelledError()),
    )

    with pytest.raises(asyncio.CancelledError):
        asyncio.run(runtime.emit("log", task_id="task"))


def test_cancel_message_propagates_cancellation(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    worker = tmp_path / "worker.py"
    worker.write_text("import time\ntime.sleep(30)\n", encoding="utf-8")
    runtime = _runtime(tmp_path)
    spec = {
        "task_id": "task",
        "worker": str(worker),
        "repo": str(tmp_path),
        "worktree_path": str(tmp_path),
        "branch": "task",
        "base_commit": "base",
        "provider_env_keys": [],
    }

    async def write_json(
        _proc: Any, message: dict[str, Any], *, deadline: float | None = None
    ) -> bool:
        del deadline
        if message["type"] == "cancel":
            raise asyncio.CancelledError
        return True

    monkeypatch.setattr(supervisor_module, "_write_json", write_json)
    with pytest.raises(asyncio.CancelledError):
        asyncio.run(
            runtime._drive_generation(
                spec,
                WorkerHandle(task_id="task", generation=1),
                ready_timeout=0.0,
                heartbeat_interval=1.0,
                heartbeat_timeout=1.0,
                wall_budget=10.0,
            )
        )


def test_merge_prepare_propagates_cancellation(tmp_path: Path) -> None:
    runtime = _runtime(tmp_path)
    _set_sequencer(runtime, _PrepareCancelled())

    with pytest.raises(asyncio.CancelledError):
        asyncio.run(
            runtime._merge_task(
                _merge_spec(tmp_path),
                WorkerHandle(task_id="task", generation=1),
            )
        )


def test_merge_cleanup_propagates_cancellation(tmp_path: Path) -> None:
    runtime = _runtime(tmp_path)
    _set_sequencer(runtime, _CleanupCancelled())

    with pytest.raises(asyncio.CancelledError):
        asyncio.run(
            runtime._merge_task(
                _merge_spec(tmp_path),
                WorkerHandle(task_id="task", generation=1),
            )
        )


def test_deferred_observer_propagates_cancellation(tmp_path: Path) -> None:
    def observer(event: dict[str, Any]) -> None:
        if event["kind"] == "merge_reconciled":
            raise asyncio.CancelledError

    runtime = _runtime(tmp_path, on_event=observer)
    _set_sequencer(runtime, _ObserverCancelled())

    with pytest.raises(asyncio.CancelledError):
        asyncio.run(
            runtime._merge_task(
                _merge_spec(tmp_path),
                WorkerHandle(task_id="task", generation=1),
            )
        )
