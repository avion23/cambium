"""Public orchestrator entry point.

The async surface (``run`` / ``submit`` / event callback) is stabilized
here so callers can depend on it. ``Orchestrator.run(session_dir, plan)``
drives the real Custos multi-worker runtime (``cambium.supervisor.run_plan``);
the skeleton submit/drain loop is kept for backward compatibility.
"""

from __future__ import annotations

import asyncio
import time
from collections.abc import Awaitable, Callable
from typing import Any

from .events import Event, WorkerFinished, WorkerStarted

EventHandler = Callable[[Event], Awaitable[None] | None]


class Orchestrator:
    """Drives Cambium sessions.

    Submits task specs, runs them through the supervised lifecycle, and
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
        """Enqueue a task spec and return its stable task id."""
        task_spec = dict(task_spec)
        task_id = str(task_spec.get("task_id") or f"task-{self._next_task_id}")
        task_spec.setdefault("task_id", task_id)
        self._next_task_id += 1
        await self._queue.put(task_spec)
        return task_id

    async def run(
        self,
        session_dir: str | None = None,
        plan: dict[str, Any] | list[dict[str, Any]] | None = None,
    ) -> Any:
        """Drive a session.

        When ``plan`` is given, runs it through the Custos multi-worker
        runtime (``cambium.supervisor.run_plan``) and returns the
        ``PlanResult``. Otherwise drains the submit() queue with the
        placeholder lifecycle.
        """
        if plan is not None:
            if session_dir is None:
                raise ValueError("run(session_dir=..., plan=...) requires a session_dir")
            from .supervisor import run_plan

            async def forward(record: dict[str, Any]) -> None:
                await self._emit(
                    Event(type=record["kind"], timestamp=record.get("ts") or time.time())
                )

            return await run_plan(session_dir, plan, on_event=forward)
        while not self._queue.empty():
            spec = await self._queue.get()
            task_id = spec["task_id"]
            await self._emit(WorkerStarted(task_id=task_id))
            await self._emit(WorkerFinished(task_id=task_id))
        return None
