"""Public orchestrator entry point.

The async surface (``run`` / event callback) is stabilized here so callers
can depend on it. ``Orchestrator.run(session_dir, plan)`` drives the real
Custos multi-worker runtime (``cambium.supervisor.run_plan``) and forwards
each canonical redacted event dict to the caller-provided callback.
"""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable
from typing import Any

EventHandler = Callable[[dict[str, Any]], Awaitable[None] | None]


class Orchestrator:
    """Drives Cambium sessions.

    Runs a plan through the supervised lifecycle and forwards lifecycle
    events to a caller-provided callback. ``architectus`` and
    ``conversations`` forward the dynamic-decomposition decision port and
    the revision conversation store to the supervisor (both default off,
    matching ``run_plan``).
    """

    def __init__(
        self,
        on_event: EventHandler | None = None,
        *,
        architectus: Any = None,
        conversations: bool | None = None,
    ) -> None:
        self._on_event = on_event
        self._architectus = architectus
        self._conversations = conversations

    async def _emit(self, record: dict[str, Any]) -> None:
        if self._on_event is not None:
            result = self._on_event(record)
            if asyncio.iscoroutine(result):
                await result

    async def run(
        self,
        session_dir: str | None = None,
        plan: dict[str, Any] | list[dict[str, Any]] | None = None,
        *,
        architectus: Any = None,
        conversations: bool | None = None,
    ) -> Any:
        """Drive a session through the Custos multi-worker runtime.

        ``plan`` is required; the result is the ``PlanResult`` from
        ``cambium.supervisor.run_plan``. ``architectus`` (an
        ``ArchitectusCore`` or an ``aggregate``/``step`` adapter) and
        ``conversations`` are forwarded to ``run_plan``; per-call values
        override the constructor defaults.
        """
        if plan is None:
            raise ValueError("run(session_dir=..., plan=...) requires a plan")
        if session_dir is None:
            raise ValueError("run(session_dir=..., plan=...) requires a session_dir")
        from .supervisor import run_plan

        return await run_plan(
            session_dir,
            plan,
            on_event=self._emit,
            architectus=self._architectus if architectus is None else architectus,
            conversations=self._conversations if conversations is None else conversations,
        )
