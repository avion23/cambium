"""Incremental durable-event tailing for attached operator interfaces."""

from __future__ import annotations

from collections.abc import Callable, Sequence
from pathlib import Path
from typing import Any, TypeVar

from .store import read_events_file

T = TypeVar("T")


class IncrementalEventTail:
    """Read each durable sequence range once and retain deterministic order."""

    def __init__(self, db_path: str | Path) -> None:
        self.db_path = Path(db_path)
        self.last_seq = 0
        self._events: list[dict[str, Any]] = []

    @property
    def events(self) -> tuple[dict[str, Any], ...]:
        return tuple(self._events)

    def poll(self) -> tuple[dict[str, Any], ...]:
        new_events = read_events_file(self.db_path, after_seq=self.last_seq)
        accepted: list[dict[str, Any]] = []
        for event in new_events:
            sequence = event.get("seq")
            if isinstance(sequence, bool) or not isinstance(sequence, int) or sequence <= 0:
                raise ValueError("durable event has an invalid sequence")
            if sequence <= self.last_seq:
                continue
            if accepted and sequence <= accepted[-1]["seq"]:
                raise ValueError("durable event tail is not strictly ordered")
            accepted.append(event)
        if accepted:
            self._events.extend(accepted)
            self.last_seq = accepted[-1]["seq"]
        return tuple(accepted)

    def recent(self, limit: int = 100) -> tuple[dict[str, Any], ...]:
        if limit <= 0:
            return ()
        return tuple(self._events[-limit:])


class IncrementalSnapshotCache[T]:
    """Rebuild an immutable projection only when a new durable event arrives."""

    def __init__(
        self,
        tail: IncrementalEventTail,
        builder: Callable[[Sequence[dict[str, Any]]], T],
    ) -> None:
        self.tail = tail
        self.builder = builder
        self._snapshot: T | None = None

    def poll(self) -> T:
        changed = self.tail.poll()
        if self._snapshot is None or changed:
            self._snapshot = self.builder(self.tail.events)
        return self._snapshot


__all__ = ["IncrementalEventTail", "IncrementalSnapshotCache"]
