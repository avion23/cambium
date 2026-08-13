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
    estimated_cost_usd: float = 0.0


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
    estimated_cost_usd = 0.0
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
        estimated_cost_usd += _row_cost(payload)
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
        estimated_cost_usd=round(estimated_cost_usd, 6),
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


def _row_cost(payload: Mapping[str, Any]) -> float:
    value = payload.get("estimated_cost_usd")
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return 0.0
    value = float(value)
    return value if math.isfinite(value) and value >= 0 else 0.0


@dataclass(frozen=True, slots=True)
class UsageBreakdown:
    """Per-task and per-provider usage totals over one session's event log."""

    by_task: tuple[tuple[str, UsageStats], ...]
    by_provider: tuple[tuple[str, UsageStats], ...]
    total: UsageStats


def usage_breakdown_from_events(events: Sequence[Mapping[str, Any]]) -> UsageBreakdown | None:
    """Aggregate usage_event records grouped by task and by provider.

    ``events`` is the already-redacted record sequence produced by
    ``cambium.supervisor.read_events``; input order is authoritative for
    group ordering (first-appearance order). Each group's ``UsageStats``
    aggregates only the rows that share that task_id (record ``task_id``
    key) or provider (payload ``provider`` key). Rows without a usable
    task_id or provider contribute only to the totals. None when no
    usage_event record exists.
    """
    task_events: dict[str, list[Mapping[str, Any]]] = {}
    provider_events: dict[str, list[Mapping[str, Any]]] = {}
    task_order: list[str] = []
    provider_order: list[str] = []
    for event in events:
        if not isinstance(event, Mapping) or event.get("kind") != _USAGE_EVENT_KIND:
            continue
        payload = event.get("payload")
        if not isinstance(payload, Mapping):
            continue
        task_id = event.get("task_id")
        if isinstance(task_id, str) and task_id:
            if task_id not in task_events:
                task_events[task_id] = []
                task_order.append(task_id)
            task_events[task_id].append(event)
        provider = payload.get("provider")
        if isinstance(provider, str) and provider:
            if provider not in provider_events:
                provider_events[provider] = []
                provider_order.append(provider)
            provider_events[provider].append(event)
    total = usage_stats_from_events(events)
    if total is None:
        return None
    return UsageBreakdown(
        by_task=tuple(
            (task_id, usage_stats_from_events(task_events[task_id]) or _ZERO_STATS)
            for task_id in task_order
        ),
        by_provider=tuple(
            (provider, usage_stats_from_events(provider_events[provider]) or _ZERO_STATS)
            for provider in provider_order
        ),
        total=total,
    )


_ZERO_STATS = UsageStats(0, None, 0, 0, 0, 0, 0, None, None)


def session_usage_breakdown(session_dir: str | Path) -> UsageBreakdown | None:
    """Aggregate a session's usage_event rows grouped by task and provider.

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
            "SELECT kind, task_id, payload FROM events WHERE kind = ? ORDER BY seq",
            (_USAGE_EVENT_KIND,),
        ).fetchall()
    except sqlite3.Error:
        return None
    finally:
        if connection is not None:
            connection.close()
    events: list[dict[str, Any]] = []
    for kind, task_id, raw_payload in rows:
        try:
            payload = json.loads(raw_payload)
        except (TypeError, ValueError):
            continue
        record: dict[str, Any] = {"kind": kind, "payload": payload}
        if task_id is not None:
            record["task_id"] = task_id
        events.append(record)
    return usage_breakdown_from_events(events)


__all__ = [
    "UsageBreakdown",
    "UsageStats",
    "session_usage_breakdown",
    "session_usage_stats",
    "usage_breakdown_from_events",
    "usage_stats_from_events",
]
