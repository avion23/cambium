"""Seed of the Cambium event-log contract.

Every state transition in the harness (worker spawn, heartbeat, result,
crash, log line) is an ``Event`` appended to the event log. These four
types are the seed; the contract will grow with the architecture doc.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from time import time


@dataclass(frozen=True, slots=True, kw_only=True)
class Event:
    """Base event. Every log entry carries a type and a timestamp."""

    type: str
    timestamp: float = field(default_factory=time)


@dataclass(frozen=True, slots=True, kw_only=True)
class WorkerStarted(Event):
    """A worker process was spawned for a task."""

    task_id: str
    pid: int | None = None
    type: str = "worker_started"


@dataclass(frozen=True, slots=True, kw_only=True)
class WorkerFinished(Event):
    """A worker finished or permanently failed a task."""

    task_id: str
    status: str = "finished"
    exit_code: int | None = None
    type: str = "worker_finished"


@dataclass(frozen=True, slots=True, kw_only=True)
class LogEvent(Event):
    """An unstructured advisory log line attached to a task."""

    level: str
    message: str
    type: str = "log"
