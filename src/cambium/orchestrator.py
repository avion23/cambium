"""Public orchestrator entry point.

Skeleton — architecture doc pending. The async surface (``run`` /
``submit`` / event callback) is stabilized here so callers can depend on
it before the Architectus design lands. No orchestration logic exists
yet beyond a minimal submit/drain loop.
"""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable
from typing import Any

from .events import Event, WorkerFinished, WorkerStarted

EventHandler = Callable[[Event], Awaitable[None] | None]


class Orchestrator:
    """Skeleton — architecture doc pending.

    Submits task specs, runs them through a supervised lifecycle, and
    emits lifecycle events to a caller-provided callback.
    """

    def __init__(self, on_event: EventHandler | None = None) -> None:
        self._on_event = on_event
        self._queue: asyncio.Queue[dict[str, Any]] = asyncio.Queue()
        self._next_task_id = 0

    async def _emit(self, event: Event) -> None:
        if self._on_event is not None:
            result = self._on_event(event)
            if asyncio.iscoroutine(result):
                await result

    async def submit(self, task_spec: dict[str, Any]) -> str:
        """Enqueue a task spec and return its stable task id.

        Skeleton — architecture doc pending.
        """
        task_spec = dict(task_spec)
        task_id = str(task_spec.get("task_id") or f"task-{self._next_task_id}")
        task_spec.setdefault("task_id", task_id)
        self._next_task_id += 1
        await self._queue.put(task_spec)
        return task_id

    async def run(self) -> None:
        """Drain submitted tasks, emitting lifecycle events.

        Skeleton — architecture doc pending. Placeholder lifecycle only.
        """
        while not self._queue.empty():
            spec = await self._queue.get()
            task_id = spec["task_id"]
            await self._emit(WorkerStarted(task_id=task_id))
            await self._emit(WorkerFinished(task_id=task_id))
