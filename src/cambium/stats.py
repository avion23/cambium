"""Pure aggregation of one session's provider-usage events."""

from __future__ import annotations

import json
import math
import sqlite3
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any
from urllib.parse import quote

_USAGE_EVENT_KIND = "usage_event"
_EVENTS_DB_REL = Path(".cambium") / "events.db"


@dataclass(frozen=True, slots=True)
class UsageStats:
    """Aggregated provider usage over one session's usage_event rows."""

    calls: int
    turns: int | None
    input_tokens: int
    output_tokens: int
    cached_tokens: int
    total_tokens: int
    last_turn_tokens: int
    model: str | None
    provider: str | None


def _is_count(value: Any) -> bool:
    """Return whether a usage value is a countable (finite) number, not a bool."""
    if isinstance(value, bool):
        return False
    if isinstance(value, int):
        return True
    return isinstance(value, float) and math.isfinite(value)


def _row_turn(payload: Mapping[str, Any]) -> int | None:
    turn = payload.get("turn")
    if isinstance(turn, bool) or not isinstance(turn, int):
        return None
    return turn


def _row_input(usage: Mapping[str, Any]) -> int:
    for key in ("input_tokens", "prompt_tokens"):
        value = usage.get(key)
        if _is_count(value):
            return int(value)
    return 0


def _row_output(usage: Mapping[str, Any]) -> int:
    for key in ("output_tokens", "completion_tokens"):
        value = usage.get(key)
        if _is_count(value):
            return int(value)
    return 0


def _row_total(usage: Mapping[str, Any]) -> int:
    value = usage.get("total_tokens")
    if _is_count(value):
        return int(value)
    return _row_input(usage) + _row_output(usage)


def _row_cached(usage: Mapping[str, Any]) -> int:
    value = usage.get("cached_tokens")
    if not _is_count(value):
        return 0
    return int(value)


def usage_stats_from_events(events: Sequence[Mapping[str, Any]]) -> UsageStats | None:
    """Aggregate the usage_event records of one session's event log.

    ``events`` is the already-redacted record sequence produced by
    ``cambium.supervisor.read_events``; input order is authoritative for the
    last model/provider selection. None when no usage_event record exists.
    """
    calls = 0
    turns: int | None = None
    input_tokens = 0
    output_tokens = 0
    cached_tokens = 0
    total_tokens = 0
    last_turn_tokens = 0
    model: str | None = None
    provider: str | None = None
    for event in events:
        if not isinstance(event, Mapping) or event.get("kind") != _USAGE_EVENT_KIND:
            continue
        payload = event.get("payload")
        if not isinstance(payload, Mapping):
            continue
        calls += 1
        usage = payload.get("usage")
        if not isinstance(usage, Mapping):
            usage = {}
        row_input = _row_input(usage)
        row_output = _row_output(usage)
        row_total = _row_total(usage)
        input_tokens += row_input
        output_tokens += row_output
        cached_tokens += _row_cached(usage)
        total_tokens += row_total
        turn = _row_turn(payload)
        if turn is not None:
            if turns is None or turn > turns:
                turns = turn
                last_turn_tokens = row_total
            elif turn == turns:
                last_turn_tokens += row_total
        value = payload.get("model")
        if isinstance(value, str) and value:
            model = value
        value = payload.get("provider")
        if isinstance(value, str) and value:
            provider = value
    if calls == 0:
        return None
    return UsageStats(
        calls=calls,
        turns=turns,
        input_tokens=input_tokens,
        output_tokens=output_tokens,
        cached_tokens=cached_tokens,
        total_tokens=total_tokens,
        last_turn_tokens=last_turn_tokens,
        model=model,
        provider=provider,
    )


def session_usage_stats(session_dir: str | Path) -> UsageStats | None:
    """Aggregate the usage_event rows of one session's durable event log.

    Opens ``<session_dir>/.cambium/events.db`` read-only and returns None
    when the file, the events table, or any usage_event row is absent. Never
    writes or creates files.
    """
    db = Path(session_dir) / _EVENTS_DB_REL
    if not db.is_file():
        return None
    uri = f"file:{quote(str(db.resolve()), safe='/:')}?mode=ro"
    connection: sqlite3.Connection | None = None
    try:
        connection = sqlite3.connect(uri, uri=True)
        rows = connection.execute(
            "SELECT kind, payload FROM events WHERE kind = ? ORDER BY seq",
            (_USAGE_EVENT_KIND,),
        ).fetchall()
    except sqlite3.Error:
        return None
    finally:
        if connection is not None:
            connection.close()
    events: list[dict[str, Any]] = []
    for kind, raw_payload in rows:
        try:
            payload = json.loads(raw_payload)
        except (TypeError, ValueError):
            continue
        events.append({"kind": kind, "payload": payload})
    return usage_stats_from_events(events)


__all__ = ["UsageStats", "session_usage_stats", "usage_stats_from_events"]
