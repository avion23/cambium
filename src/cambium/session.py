"""Read-only view of completed Cambium supervisor sessions.

A session is one caller-owned directory whose artifacts live in its
``.cambium/`` state directory: the canonical root result at
``.cambium/result.json`` (written by :func:`cambium.results.write_result`) and
the durable event log at ``.cambium/events.db`` (written by
:class:`cambium.store.EventStore`).

Sessions for one repository live in ``<repo>/.cambium/sessions/``
(:func:`session_root`).  :func:`list_sessions` returns the completed sessions
there in deterministic order; :func:`latest_session` returns the newest one;
:func:`show_session` reads one session's current result record into a
renderer-friendly :class:`SessionView`.

This module is read-only: it never creates or opens for writing the artifacts
it inspects.
"""

from __future__ import annotations

import json
import math
from dataclasses import dataclass
from pathlib import Path
from typing import Any


def session_root(repo: Path) -> Path:
    """Return the directory that holds the sessions for ``repo``."""
    return Path(repo).resolve() / ".cambium" / "sessions"


class InvalidSessionError(ValueError):
    """One or more session directories have an unreadable or invalid result.

    Raised by :func:`list_sessions` (strict listing) so a corrupt session is
    not silently hidden. The ``entries`` attribute carries the typed
    :class:`SessionEntry` records; callers that want partial results use
    :func:`list_session_entries` directly.
    """

    def __init__(self, entries: list[SessionEntry]) -> None:
        self.entries = entries
        described = ", ".join(str(entry.path) for entry in entries[:3])
        super().__init__(f"{len(entries)} invalid session(s) under the session root: {described}")


@dataclass(frozen=True, slots=True)
class SessionEntry:
    """One session-directory listing record.

    ``valid`` is True for a completed session whose ``.cambium/result.json``
    parses to a JSON object (``record`` then holds it). A directory that
    looks like a session (its ``.cambium/result.json`` exists) but cannot be
    read or parsed is ``valid=False`` with a ``reason``; such entries are
    surfaced, never silently dropped. A directory without a result file is
    not a session at all and produces no entry.
    """

    path: Path
    valid: bool
    record: dict[str, Any] | None = None
    reason: str | None = None


def list_session_entries(root: Path) -> list[SessionEntry]:
    """Return typed listing records for every session under ``root``.

    A child directory without ``.cambium/result.json`` is not a session and
    contributes no entry. A child whose result file exists but cannot be
    read or parsed becomes an invalid entry with a ``reason``; invalid
    sessions are surfaced rather than hidden. Valid entries are ordered by
    ascending ``(ended_at, started_at, name)``; invalid entries follow,
    ordered by name.
    """
    sessions_root = Path(root).resolve()
    if not sessions_root.is_dir():
        return []
    valid: list[tuple[dict[str, Any], SessionEntry]] = []
    invalid: list[SessionEntry] = []
    for child in sessions_root.iterdir():
        entry = _session_entry(child)
        if entry is None:
            continue
        if entry.valid and entry.record is not None:
            valid.append((entry.record, entry))
        else:
            invalid.append(entry)
    valid.sort(key=lambda item: _sort_key(item[0], item[1].path))
    invalid.sort(key=lambda item: item.path.name)
    return [entry for _record, entry in valid] + invalid


def list_sessions(root: Path) -> list[Path]:
    """Return completed sessions under ``root``, oldest first.

    A directory is a completed session when its ``.cambium/result.json``
    parses to a JSON object.  Ordering is deterministic: ascending
    ``(ended_at, started_at, name)`` read from each result record.

    Strict: a session directory whose result file exists but cannot be read
    or parsed raises :class:`InvalidSessionError` instead of being hidden;
    use :func:`list_session_entries` for the typed partial listing.
    """
    entries = list_session_entries(root)
    invalid = [entry for entry in entries if not entry.valid]
    if invalid:
        raise InvalidSessionError(invalid)
    return [entry.path for entry in entries]


def latest_session(root: Path) -> Path | None:
    """Return the most recent completed session under ``root``, or ``None``."""
    sessions = list_sessions(root)
    return sessions[-1] if sessions else None


def show_session(path: Path) -> SessionView:
    """Read one session's current result record into a view.

    The session result (``.cambium/result.json``) is the only artifact this
    view surfaces. The durable event log is not part of the result view;
    readers that need the durable log stream it through
    ``cambium.supervisor.read_events``.
    """
    session_path = Path(path)
    result_path = session_path / ".cambium" / "result.json"
    with open(result_path, encoding="utf-8") as stream:
        record = json.load(stream)
    if not isinstance(record, dict):
        raise ValueError(f"session result is not a JSON object: {result_path}")
    return SessionView(path=session_path.resolve(), result=record)


@dataclass(frozen=True, slots=True)
class SessionView:
    """Renderer-friendly snapshot of one completed session's result record.

    ``result`` is the parsed ``.cambium/result.json`` record. The durable
    event log is intentionally not materialized here; readers that need
    events stream them through ``cambium.supervisor.read_events``.
    """

    path: Path
    result: dict[str, Any]


def _result_path(path: Path) -> Path:
    return path / ".cambium" / "result.json"


def _session_entry(path: Path) -> SessionEntry | None:
    """Return the typed listing record for ``path``, or None when not a session."""
    result_path = _result_path(path)
    try:
        is_dir = path.is_dir()
    except OSError:
        return None
    if not is_dir:
        return None
    if not result_path.is_file():
        return None
    try:
        with open(result_path, encoding="utf-8") as stream:
            record = json.load(stream)
    except OSError as exc:
        return SessionEntry(path=path, valid=False, reason=f"unreadable: {exc}")
    except ValueError as exc:
        return SessionEntry(path=path, valid=False, reason=f"invalid JSON: {exc}")
    if not isinstance(record, dict):
        return SessionEntry(
            path=path, valid=False, reason=f"not a JSON object: {type(record).__name__}"
        )
    return SessionEntry(path=path, valid=True, record=record)


def _timestamp(value: Any) -> float:
    if isinstance(value, bool) or not isinstance(value, int | float):
        return float("-inf")
    try:
        number = float(value)
    except (OverflowError, ValueError):
        return float("-inf")
    return number if math.isfinite(number) else float("-inf")


def _sort_key(record: dict[str, Any], path: Path) -> tuple[float, float, str]:
    return (
        _timestamp(record.get("ended_at")),
        _timestamp(record.get("started_at")),
        path.name,
    )


__all__ = [
    "InvalidSessionError",
    "SessionEntry",
    "SessionView",
    "latest_session",
    "list_session_entries",
    "list_sessions",
    "session_root",
    "show_session",
]
