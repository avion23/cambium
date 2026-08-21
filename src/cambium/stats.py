"""Pure aggregation of one session's provider-usage events."""

from __future__ import annotations

import json
import math
import sqlite3
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any, TypeGuard
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


def _is_count(value: Any) -> TypeGuard[int | float]:
    """Return whether a usage value is a finite, non-negative number, not a bool.

    Negative values are not valid usage counts: a corrupt or mis-encoded row
    must not subtract from a total.
    """
    if isinstance(value, bool):
        return False
    if isinstance(value, int):
        return value >= 0
    return isinstance(value, float) and math.isfinite(value) and value >= 0


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


def _row_cost(payload: Mapping[str, Any]) -> float:
    value = payload.get("estimated_cost_usd")
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return 0.0
    value = float(value)
    return value if math.isfinite(value) and value >= 0 else 0.0


@dataclass
class _UsageAccumulator:
    """Mutable per-group usage accumulator (total, task, or provider)."""

    calls: int = 0
    turns: int | None = None
    input_tokens: int = 0
    output_tokens: int = 0
    cached_tokens: int = 0
    total_tokens: int = 0
    last_turn_tokens: int = 0
    model: str | None = None
    provider: str | None = None
    estimated_cost_usd: float = 0.0


def _accumulate(acc: _UsageAccumulator, payload: Mapping[str, Any]) -> None:
    """Fold one usage_event payload's numbers into ``acc`` in place."""
    usage = payload.get("usage")
    if not isinstance(usage, Mapping):
        usage = {}
    acc.calls += 1
    row_input = _row_input(usage)
    row_output = _row_output(usage)
    row_total = _row_total(usage)
    acc.input_tokens += row_input
    acc.output_tokens += row_output
    acc.cached_tokens += _row_cached(usage)
    acc.total_tokens += row_total
    acc.estimated_cost_usd += _row_cost(payload)
    turn = _row_turn(payload)
    if turn is not None:
        if acc.turns is None or turn > acc.turns:
            acc.turns = turn
            acc.last_turn_tokens = row_total
        elif turn == acc.turns:
            acc.last_turn_tokens += row_total
    model = payload.get("model")
    if isinstance(model, str) and model:
        acc.model = model
    provider = payload.get("provider")
    if isinstance(provider, str) and provider:
        acc.provider = provider


def _stats_from_accumulator(acc: _UsageAccumulator) -> UsageStats:
    return UsageStats(
        calls=acc.calls,
        turns=acc.turns,
        input_tokens=acc.input_tokens,
        output_tokens=acc.output_tokens,
        cached_tokens=acc.cached_tokens,
        total_tokens=acc.total_tokens,
        last_turn_tokens=acc.last_turn_tokens,
        model=acc.model,
        provider=acc.provider,
        estimated_cost_usd=round(acc.estimated_cost_usd, 6),
    )


def usage_stats_from_events(events: Sequence[Mapping[str, Any]]) -> UsageStats | None:
    """Aggregate the usage_event records of one session's event log.

    ``events`` is the already-redacted record sequence produced by
    ``cambium.supervisor.read_events``; input order is authoritative for the
    last model/provider selection. None when no usage_event record exists.
    """
    acc = _UsageAccumulator()
    for event in events:
        if not isinstance(event, Mapping) or event.get("kind") != _USAGE_EVENT_KIND:
            continue
        payload = event.get("payload")
        if not isinstance(payload, Mapping):
            continue
        _accumulate(acc, payload)
    return None if acc.calls == 0 else _stats_from_accumulator(acc)


def _events_table_exists(connection: sqlite3.Connection) -> bool:
    row = connection.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' AND name='events'"
    ).fetchone()
    return row is not None


def _read_usage_rows(
    db: Path, *, with_task_id: bool
) -> list[Mapping[str, Any]] | None:
    """Read usage_event rows from ``db``; None when the events table is absent.

    Connects read-only and returns None only for a missing ``events`` table
    (equivalent to no usage rows). Any other database error — corrupt file,
    unreadable rows, or inaccessible storage — propagates so callers can tell
    corruption apart from absence. A row whose payload is not valid JSON is
    corruption and raises ``ValueError`` rather than disappearing.
    """
    uri = f"file:{quote(str(db.resolve()), safe='/:')}?mode=ro"
    connection = sqlite3.connect(uri, uri=True)
    try:
        if not _events_table_exists(connection):
            return None
        if with_task_id:
            rows = connection.execute(
                "SELECT kind, task_id, payload FROM events WHERE kind = ? ORDER BY seq",
                (_USAGE_EVENT_KIND,),
            ).fetchall()
        else:
            rows = connection.execute(
                "SELECT kind, payload FROM events WHERE kind = ? ORDER BY seq",
                (_USAGE_EVENT_KIND,),
            ).fetchall()
    finally:
        connection.close()
    events: list[Mapping[str, Any]] = []
    for row in rows:
        kind = row[0]
        raw_payload = row[-1]
        payload = json.loads(raw_payload)
        if not isinstance(payload, Mapping):
            raise ValueError("usage_event payload is not a JSON object")
        record: dict[str, Any] = {"kind": kind, "payload": payload}
        if with_task_id and row[1] is not None:
            record["task_id"] = row[1]
        events.append(record)
    return events


def session_usage_stats(session_dir: str | Path) -> UsageStats | None:
    """Aggregate the usage_event rows of one session's durable event log.

    Opens ``<session_dir>/.cambium/events.db`` read-only and returns None when
    the file or the events table is absent (no usage rows). Corrupt or
    inaccessible databases, and rows whose payload is not valid JSON, raise
    instead of being hidden. Never writes or creates files.
    """
    db = Path(session_dir) / _EVENTS_DB_REL
    if not db.is_file():
        return None
    events = _read_usage_rows(db, with_task_id=False)
    if events is None:
        return None
    return usage_stats_from_events(events)


@dataclass(frozen=True, slots=True)
class UsageBreakdown:
    """Per-task and per-provider usage totals over one session's event log."""

    by_task: tuple[tuple[str, UsageStats], ...]
    by_provider: tuple[tuple[str, UsageStats], ...]
    total: UsageStats


def usage_breakdown_from_events(events: Sequence[Mapping[str, Any]]) -> UsageBreakdown | None:
    """Aggregate usage_event records grouped by task and by provider in one pass.

    ``events`` is the already-redacted record sequence produced by
    ``cambium.supervisor.read_events``; input order is authoritative for
    group ordering (first-appearance order). Each group's ``UsageStats``
    aggregates only the rows that share that task_id (record ``task_id``
    key) or provider (payload ``provider`` key). Rows without a usable
    task_id or provider contribute only to the totals. None when no
    usage_event record exists.
    """
    total = _UsageAccumulator()
    task_accs: dict[str, _UsageAccumulator] = {}
    task_order: list[str] = []
    provider_accs: dict[str, _UsageAccumulator] = {}
    provider_order: list[str] = []
    for event in events:
        if not isinstance(event, Mapping) or event.get("kind") != _USAGE_EVENT_KIND:
            continue
        payload = event.get("payload")
        if not isinstance(payload, Mapping):
            continue
        _accumulate(total, payload)
        task_id = event.get("task_id")
        if isinstance(task_id, str) and task_id:
            acc = task_accs.get(task_id)
            if acc is None:
                acc = _UsageAccumulator()
                task_accs[task_id] = acc
                task_order.append(task_id)
            _accumulate(acc, payload)
        provider = payload.get("provider")
        if isinstance(provider, str) and provider:
            acc = provider_accs.get(provider)
            if acc is None:
                acc = _UsageAccumulator()
                provider_accs[provider] = acc
                provider_order.append(provider)
            _accumulate(acc, payload)
    if total.calls == 0:
        return None
    return UsageBreakdown(
        by_task=tuple(
            (task_id, _stats_from_accumulator(task_accs[task_id]))
            for task_id in task_order
        ),
        by_provider=tuple(
            (provider, _stats_from_accumulator(provider_accs[provider]))
            for provider in provider_order
        ),
        total=_stats_from_accumulator(total),
    )


def session_usage_breakdown(session_dir: str | Path) -> UsageBreakdown | None:
    """Aggregate a session's usage_event rows grouped by task and provider.

    Opens ``<session_dir>/.cambium/events.db`` read-only and returns None when
    the file or the events table is absent (no usage rows). Corrupt or
    inaccessible databases, and rows whose payload is not valid JSON, raise
    instead of being hidden. Never writes or creates files.
    """
    db = Path(session_dir) / _EVENTS_DB_REL
    if not db.is_file():
        return None
    events = _read_usage_rows(db, with_task_id=True)
    if events is None:
        return None
    return usage_breakdown_from_events(events)


__all__ = [
    "UsageBreakdown",
    "UsageStats",
    "session_usage_breakdown",
    "session_usage_stats",
    "usage_breakdown_from_events",
    "usage_stats_from_events",
]
