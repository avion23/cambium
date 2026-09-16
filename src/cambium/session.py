"""Read-only view of completed Cambium supervisor sessions.

A session is one caller-owned directory whose artifacts live in its
``.cambium/`` state directory: the canonical root result at
``.cambium/result.json`` (written by :func:`cambium.results.write_result`) and
the durable event log at ``.cambium/events.db`` (written by
:class:`cambium.store.EventStore`). Interactive roots keep those artifacts in
``turn-NNNN/`` children; readers expose the newest completed turn as the root
session view.

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
import re
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
    parses to a JSON object (``record`` then holds it), or for an interactive
    root whose newest completed ``turn-NNNN/.cambium/result.json`` parses to a
    JSON object. A directory that looks like a session but cannot be read or
    parsed is ``valid=False`` with a ``reason``; such entries are surfaced,
    never silently dropped. A directory without a result file (root or turn)
    is not a session at all and produces no entry.
    """

    path: Path
    valid: bool
    record: dict[str, Any] | None = None
    reason: str | None = None


_TURN_DIR_RE = re.compile(r"turn-(\d+)")


def list_session_entries(root: Path) -> list[SessionEntry]:
    """Return typed listing records for every session under ``root``.

    A child directory without a root result or a completed interactive turn
    result contributes no entry. A result file that exists but cannot be read
    or parsed becomes an invalid entry with a ``reason``; invalid sessions are
    surfaced rather than hidden. Valid entries are ordered by ascending
    ``(ended_at, started_at, name)``; invalid entries follow, ordered by name.
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

    A directory is a completed session when its root result, or the newest
    completed interactive turn result, parses to a JSON object. Ordering is
    deterministic: ascending ``(ended_at, started_at, name)`` read from each
    selected result record.

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

    The session result (``.cambium/result.json``), or the newest completed
    interactive turn result, is the only artifact this view surfaces. The
    durable event log is not part of the result view; readers that need the
    durable log stream it through ``cambium.supervisor.read_events``. For
    interactive roots that function merges the immutable ``turn-NNNN`` stores
    without changing the result's session-root event-log reference.
    """
    session_path = Path(path)
    result_path = _result_path(session_path)
    if result_path.is_file():
        record = _read_result_record(result_path)
    else:
        interactive = _interactive_result_record(session_path)
        if interactive is None:
            # Preserve the ordinary missing-root-result error and avoid
            # treating an incomplete interactive allocation as a session.
            with open(result_path, encoding="utf-8") as stream:
                record = json.load(stream)
            if not isinstance(record, dict):
                raise ValueError(f"session result is not a JSON object: {result_path}")
        else:
            _turn_path, record = interactive
    return SessionView(path=session_path.resolve(), result=record)


@dataclass(frozen=True, slots=True)
class SessionView:
    """Renderer-friendly snapshot of one completed session's result record.

    ``result`` is the parsed root ``.cambium/result.json`` record, or the
    newest completed turn result for an interactive root. The durable event
    log is intentionally not materialized here; readers that need events
    stream them through ``cambium.supervisor.read_events``.
    """

    path: Path
    result: dict[str, Any]


def _result_path(path: Path) -> Path:
    return path / ".cambium" / "result.json"


def _read_result_record(result_path: Path) -> dict[str, Any]:
    """Read one canonical result record, preserving its path in errors."""
    with open(result_path, encoding="utf-8") as stream:
        record = json.load(stream)
    if not isinstance(record, dict):
        raise ValueError(f"session result is not a JSON object: {result_path}")
    return record


def _interactive_turn_result_paths(path: Path) -> list[tuple[int, Path]]:
    """Return canonical turn result files under an interactive root."""
    try:
        children = tuple(path.iterdir())
    except OSError:
        return []
    turns: list[tuple[int, Path]] = []
    for child in children:
        if child.is_symlink() or not child.is_dir():
            continue
        match = _TURN_DIR_RE.fullmatch(child.name)
        if match is None:
            continue
        number = int(match.group(1))
        if child.name != f"turn-{number:04d}":
            continue
        result_path = _result_path(child)
        if result_path.is_file():
            turns.append((number, result_path))
    turns.sort(key=lambda item: item[0])
    return turns


def _interactive_result_record(path: Path) -> tuple[Path, dict[str, Any]] | None:
    """Read the newest completed turn result for an interactive root.

    The newest turn is authoritative.  If an older turn is corrupt, listing
    still surfaces that corruption through :func:`_session_entry`; ``show``
    reads only the turn it is asked to display.
    """
    turns = _interactive_turn_result_paths(path)
    if not turns:
        return None
    _number, result_path = turns[-1]
    return result_path, _read_result_record(result_path)


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
        return _interactive_session_entry(path)
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


def _interactive_session_entry(path: Path) -> SessionEntry | None:
    """Return one listing record for a root whose results live in turns."""
    turns = _interactive_turn_result_paths(path)
    if not turns:
        return None
    invalid: list[str] = []
    valid: list[tuple[int, dict[str, Any]]] = []
    for number, result_path in turns:
        try:
            valid.append((number, _read_result_record(result_path)))
        except OSError as exc:
            invalid.append(f"turn-{number:04d} unreadable: {exc}")
        except ValueError as exc:
            invalid.append(f"turn-{number:04d} invalid: {exc}")
    if invalid:
        return SessionEntry(
            path=path,
            valid=False,
            reason="; ".join(invalid),
        )
    if not valid:
        return None
    _number, record = valid[-1]
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
