"""Read-only view of completed Cambium supervisor sessions.

A session is one caller-owned directory whose artifacts live in its
``.cambium/`` state directory: the canonical root result at
``.cambium/result.json`` (written by :func:`cambium.results.write_result`) and
the durable event log at ``.cambium/events.db`` (written by
:class:`cambium.store.EventStore`).

Sessions for one repository live in ``<repo>/.cambium/sessions/``
(:func:`session_root`).  :func:`list_sessions` returns the completed sessions
there in deterministic order; :func:`latest_session` returns the newest one;
:func:`show_session` reads both current artifacts of one session into a
renderer-friendly :class:`SessionView`.

This module is read-only: it never creates or opens for writing the artifacts
it inspects.
"""

from __future__ import annotations

import json
import sqlite3
from dataclasses import dataclass
from pathlib import Path
from typing import Any

_SELECT_EVENTS = (
    "SELECT seq, kind, payload, ts, monotonic_ms, task_id, worker_id, "
    "generation, request_id FROM events WHERE seq > 0 ORDER BY seq"
)


def session_root(repo: Path) -> Path:
    """Return the directory that holds the sessions for ``repo``."""
    return Path(repo).resolve() / ".cambium" / "sessions"


def list_sessions(root: Path) -> list[Path]:
    """Return completed sessions under ``root``, oldest first.

    A directory is a completed session when its ``.cambium/result.json``
    parses to a JSON object.  Ordering is deterministic: ascending
    ``(ended_at, started_at, name)`` read from each result record.
    """
    sessions_root = Path(root).resolve()
    if not sessions_root.is_dir():
        return []
    sessions: list[tuple[dict[str, Any], Path]] = []
    for child in sessions_root.iterdir():
        record = _try_result_record(child)
        if record is not None:
            sessions.append((record, child))
    sessions.sort(key=lambda item: _sort_key(item[0], item[1]))
    return [path for _record, path in sessions]


def latest_session(root: Path) -> Path | None:
    """Return the most recent completed session under ``root``, or ``None``."""
    sessions = list_sessions(root)
    return sessions[-1] if sessions else None


def show_session(path: Path) -> SessionView:
    """Read one session's current result and event artifacts into a view."""
    session_path = Path(path)
    result_path = session_path / ".cambium" / "result.json"
    with open(result_path, encoding="utf-8") as stream:
        record = json.load(stream)
    if not isinstance(record, dict):
        raise ValueError(f"session result is not a JSON object: {result_path}")
    events = _read_events(session_path / ".cambium" / "events.db")
    return SessionView(path=session_path.resolve(), result=record, events=events)


@dataclass(frozen=True, slots=True)
class SessionView:
    """Renderer-friendly snapshot of one completed session.

    ``result`` is the parsed ``.cambium/result.json`` record; ``events`` are
    the durable event records in sequence order.
    """

    path: Path
    result: dict[str, Any]
    events: tuple[dict[str, Any], ...]


def _cambium_dir(path: Path) -> Path:
    return path / ".cambium"


def _result_path(path: Path) -> Path:
    return _cambium_dir(path) / "result.json"


def _try_result_record(path: Path) -> dict[str, Any] | None:
    """Return the parsed result record of a session directory, else ``None``."""
    result_path = _result_path(path)
    if not path.is_dir() or not result_path.is_file():
        return None
    try:
        with open(result_path, encoding="utf-8") as stream:
            record = json.load(stream)
    except (OSError, ValueError):
        return None
    if not isinstance(record, dict):
        return None
    return record


def _timestamp(value: Any) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return float("-inf")
    return float(value)


def _sort_key(record: dict[str, Any], path: Path) -> tuple[float, float, str]:
    return (
        _timestamp(record.get("ended_at")),
        _timestamp(record.get("started_at")),
        path.name,
    )


def _read_events(db: Path) -> tuple[dict[str, Any], ...]:
    if not db.is_file():
        return ()
    conn = sqlite3.connect(f"file:{db}?mode=ro", uri=True)
    try:
        rows = conn.execute(_SELECT_EVENTS).fetchall()
    finally:
        conn.close()
    return tuple(_event_record(row) for row in rows)


def _event_record(row: tuple) -> dict[str, Any]:
    """Map one events row to the canonical record shape of store.EventStore."""
    (seq, kind, payload, ts, monotonic_ms, task_id, worker_id, generation, request_id) = row
    return {
        "seq": seq,
        "kind": kind,
        "payload": json.loads(payload),
        "ts": ts,
        "monotonic_ms": monotonic_ms,
        "task_id": task_id,
        "worker_id": worker_id,
        "generation": generation,
        "request_id": request_id,
    }


__all__ = ["SessionView", "latest_session", "list_sessions", "session_root", "show_session"]
