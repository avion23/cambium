"""Pure renderers for Cambium results, events, and usage stats lines.

The seven public functions are side-effect free and deterministic.  They
accept the canonical ``cambium.results.Result``, the supervisor result
dataclasses (``TaskResult``, ``SliceResult``, ``PlanResult``), or a
JSON-like mapping holding one of those records.  Untyped mappings are
fail-closed: only the union of the canonical result boundaries
(``ROOT_RESULT_KEYS``, ``CHILD_RESULT_KEYS``, and the supervisor result
fields) is emitted, so provider metadata, wire envelopes, stdout/stderr,
scratchpads, and reasoning never cross a renderer.

Event records are assumed already redacted by the session boundary
(``supervisor._Runtime.emit`` redacts before the store and observers).
This module adds no observability and no dependencies beyond the standard
library.  JSON output is deterministic: stable key order, compact
separators, no NaN, and UTF-8 without ASCII escaping.
"""

from __future__ import annotations

import json
import math
from collections.abc import Mapping
from dataclasses import asdict, is_dataclass
from pathlib import Path
from typing import Any

from .results import CHILD_RESULT_KEYS, ROOT_RESULT_KEYS, Result, result_to_dict

_SUPERVISOR_RESULT_FIELDS = frozenset(
    {
        "task_id",
        "reason",
        "merge_sha",
        "restarts",
        "worker_exit_code",
        "worker_status",
        "timed_out",
        "timeout_phase",
        "results",
    }
)

_SAFE_KEYS = (
    frozenset(ROOT_RESULT_KEYS) | frozenset(CHILD_RESULT_KEYS) | _SUPERVISOR_RESULT_FIELDS
)

_EVENT_ENVELOPE_KEYS = frozenset(
    {
        "event_id",
        "seq",
        "kind",
        "ts",
        "monotonic_ms",
        "task_id",
        "worker_id",
        "generation",
        "request_id",
        "schema_version",
    }
)


def _dumps(value: Any) -> str:
    return json.dumps(
        value,
        sort_keys=True,
        ensure_ascii=False,
        allow_nan=False,
        separators=(",", ":"),
    )


def _filter_safe(record: Mapping[str, Any]) -> dict[str, Any]:
    """Keep only canonical result fields, recursing into ``PlanResult.results``.

    ``results`` must be a sequence of mappings; a non-sequence value is dropped
    wholesale and every non-mapping entry of a sequence is rejected, so a
    mapping-, scalar-, or nested-list-valued ``results`` field cannot carry
    arbitrary data through the renderer.
    """
    filtered: dict[str, Any] = {}
    for key, item in record.items():
        if key not in _SAFE_KEYS:
            continue
        if key == "results":
            if not isinstance(item, (list, tuple)):
                continue
            item = [
                _filter_safe(entry)
                for entry in item
                if isinstance(entry, Mapping)
            ]
        filtered[key] = item
    return filtered


def _result_record(result: Any) -> dict[str, Any]:
    if isinstance(result, Result):
        record = result_to_dict(result)
    elif is_dataclass(result) and not isinstance(result, type):
        record = asdict(result)
    elif isinstance(result, Mapping):
        record = dict(result)
    else:
        raise TypeError(
            "render requires a cambium.results.Result, a supervisor result "
            "dataclass, or a JSON-like mapping"
        )
    return _filter_safe(record)


def render_json_result(result: Any) -> str:
    """Render one result record as deterministic, valid, compact JSON."""
    return _dumps(_result_record(result))


def render_text_result(result: Any) -> str:
    """Render one result record as one concise human-readable line.

    Nested plan entries surface their non-succeeded ``reason`` values and any
    non-empty worker ``summary`` values without widening the safe field set.
    """
    record = _result_record(result)
    parts: list[str] = []
    status = record.get("status")
    if isinstance(status, str) and status:
        parts.append(f"status={status}")
    exit_code = record.get("exit_code")
    if isinstance(exit_code, int) and not isinstance(exit_code, bool):
        parts.append(f"exit={exit_code}")
    summary = record.get("summary")
    if isinstance(summary, str) and summary:
        parts.append(f"summary={summary!r}")
    reason = record.get("failure_reason") or record.get("reason")
    if isinstance(reason, str) and reason:
        parts.append(f"reason={reason!r}")
    for label, key in (("commits", "commits"), ("files", "files_changed")):
        items = record.get(key)
        if isinstance(items, (list, tuple)) and items:
            parts.append(f"{label}={len(items)}")
    metric = record.get("metric_score")
    if (
        isinstance(metric, (int, float))
        and not isinstance(metric, bool)
        and metric != 0
    ):
        parts.append(f"metric={metric:g}")
    merge_sha = record.get("merge_sha")
    if isinstance(merge_sha, str) and merge_sha:
        parts.append(f"merge={merge_sha[:12]}")
    results = record.get("results")
    if isinstance(results, (list, tuple)) and results:
        parts.append(f"plan=tasks:{len(results)}")
        statuses = ", ".join(
            str(entry.get("status")) for entry in results if isinstance(entry, Mapping)
        )
        if statuses:
            parts.append(f"plan_status={{{statuses}}}")
        failures = [
            f"{entry.get('task_id') or index}:{entry['reason']!r}"
            for index, entry in enumerate(results)
            if isinstance(entry, Mapping)
            and entry.get("status") != "succeeded"
            and isinstance(entry.get("reason"), str)
            and entry["reason"]
        ]
        if failures:
            parts.append(f"plan_failures={{{', '.join(failures)}}}")
        summaries = [
            f"{entry.get('task_id') or index}:{entry['summary']!r}"
            for index, entry in enumerate(results)
            if isinstance(entry, Mapping)
            and isinstance(entry.get("summary"), str)
            and entry["summary"]
        ]
        if summaries:
            parts.append(f"plan_summaries={{{', '.join(summaries)}}}")
    return " ".join(parts)


def _human_count(n: int) -> str:
    """Format a count for humans: ``3217`` -> ``3.2k``, ``347`` -> ``347``."""
    if n < 1000:
        return str(n)
    value = f"{n / 1000:.1f}"
    if value.endswith(".0"):
        value = value[:-2]
    return f"{value}k"


def _short_worktree(worktree: str) -> str:
    """Shorten a worktree path to its last two segments (``…/run-x/wt``)."""
    parts = [p for p in Path(worktree.replace("\\", "/")).parts if p != "/"]
    if len(parts) <= 1:
        return worktree
    return "…/" + "/".join(parts[-2:])


def render_usage_stats_line(stats: Any, *, worktree: str | None = None) -> str:
    """Render one usage-stats record as one deterministic line.

    Accepts a ``cambium.stats.UsageStats`` dataclass or a JSON-like mapping
    with the same fields. Untyped mapping values are validated; fields whose
    values have the wrong type are skipped rather than raising. Counts render
    human-readable (``3.2k``, ``347``) except ``calls``; ``tokens``/``in``/
    ``out``/``cached`` form one group, and ``worktree`` is shortened to its
    last two path segments. ``worktree`` is appended only when the argument
    (or the record) provides a non-empty string.
    """
    if stats is None:
        return ""
    if is_dataclass(stats) and not isinstance(stats, type):
        record = asdict(stats)
    elif isinstance(stats, Mapping):
        record = dict(stats)
    else:
        raise TypeError(
            "render_usage_stats_line requires a cambium.stats.UsageStats "
            "dataclass or a JSON-like mapping"
        )

    def count(key: str) -> int | None:
        value = record.get(key)
        if isinstance(value, bool) or not isinstance(value, (int, float)):
            return None
        if isinstance(value, float) and not math.isfinite(value):
            return None
        return int(value)

    groups: list[str] = []
    calls = count("calls")
    if calls is not None:
        groups.append(f"calls={calls}")
    tokens_group: list[str] = []
    total = count("total_tokens")
    if total is not None:
        tokens_group.append(f"tokens={_human_count(total)}")
    inner: list[str] = []
    for key, label in (
        ("input_tokens", "in"),
        ("output_tokens", "out"),
        ("cached_tokens", "cached"),
    ):
        value = count(key)
        if value is not None:
            inner.append(f"{label}={_human_count(value)}")
    if inner:
        tokens_group.append(f"({' '.join(inner)})")
    if tokens_group:
        groups.append(" ".join(tokens_group))
    turns = record.get("turns")
    if isinstance(turns, int) and not isinstance(turns, bool):
        last_turn = count("last_turn_tokens")
        if last_turn is not None:
            groups.append(f"last_turn=+{_human_count(last_turn)}")
    model = record.get("model")
    if isinstance(model, str) and model:
        groups.append(f"model={model}")
    if not (isinstance(worktree, str) and worktree):
        worktree = record.get("worktree")
    if isinstance(worktree, str) and worktree:
        groups.append(f"worktree={_short_worktree(worktree)}")
    if not groups:
        return "stats:"
    return "stats: " + " · ".join(groups)


def render_event_line(event: Mapping[str, Any]) -> str:
    """Render one redacted event record as one deterministic line.

    The line is ``seq kind task  payload`` with ``seq`` and ``task`` omitted
    when absent; the JSON payload is always the last field.
    """
    if not isinstance(event, Mapping):
        raise TypeError("render_event_line requires an event mapping")
    kind = event.get("kind")
    if not isinstance(kind, str) or not kind:
        kind = "event"
    payload = event.get("payload")
    if not isinstance(payload, Mapping):
        payload = {
            key: value
            for key, value in event.items()
            if key not in _EVENT_ENVELOPE_KEYS
        }
    prefix = f"{kind:>16}"
    seq = event.get("seq")
    if isinstance(seq, int) and not isinstance(seq, bool):
        prefix = f"{seq:>6} {prefix}"
    task_id = event.get("task_id")
    if isinstance(task_id, str) and task_id:
        prefix = f"{prefix} {task_id}"
    return f"{prefix}  {_dumps(payload)}"


_WORKER_ACTIVE_INC = frozenset({"spawned"})
_WORKER_ACTIVE_DEC = frozenset({"exit", "reuse_ready", "worker_failed"})


def render_tokens_per_s(events: Any) -> str:
    """Render tokens/second throughput from the latest usable ``usage_event``.

    ``events`` is a sequence of already-redacted event records (mappings with
    at least ``kind`` and ``payload`` keys).  For every ``usage_event`` whose
    payload ``latency_s`` is a positive finite number, the rate is that
    event's ``payload.usage.total_tokens`` divided by its ``latency_s``; the
    LATEST such rate is returned as ``"tokens/s=12.3"`` with one decimal.
    The guard is applied per event, so a single usage_event is handled
    identically: its own ``total_tokens`` and ``latency_s`` define the rate.
    Events with a missing, non-numeric, or non-finite ``total_tokens``, or
    with a missing, zero, negative, or non-finite ``latency_s``, are skipped.
    Returns ``""`` when no usable usage_event exists.
    """
    rate: float | None = None
    for event in events:
        if not isinstance(event, Mapping):
            continue
        if event.get("kind") != "usage_event":
            continue
        payload = event.get("payload")
        if not isinstance(payload, Mapping):
            continue
        latency_s = payload.get("latency_s")
        if isinstance(latency_s, bool) or not isinstance(latency_s, (int, float)):
            continue
        if not math.isfinite(latency_s) or latency_s <= 0:
            continue
        usage = payload.get("usage")
        if not isinstance(usage, Mapping):
            continue
        total_tokens = usage.get("total_tokens")
        if isinstance(total_tokens, bool) or not isinstance(total_tokens, (int, float)):
            continue
        if not math.isfinite(total_tokens):
            continue
        rate = float(total_tokens) / float(latency_s)
    if rate is None:
        return ""
    return f"tokens/s={rate:.1f}"


def render_active_workers(events: Any) -> str:
    """Render the count of concurrently-active worker processes.

    ``events`` is a sequence of already-redacted event records (mappings with
    at least ``kind`` and ``payload`` keys).  A running count starts at 0 and
    is clamped at 0 (never negative).  Each ``spawned`` event increments the
    count by one: it announces that a worker process was launched.  A
    ``ready`` event marks that the same process reached its ready state and
    does not change the count (the process was already counted at
    ``spawned``).  Each ``exit``, ``reuse_ready``, or ``worker_failed`` event
    decrements the count by one: a worker that exits, is recycled into the
    idle pool, or fails is no longer actively running one task.  Returns
    ``"subagents=3"`` when the count is nonzero, else ``""``.
    """
    count = 0
    for event in events:
        if not isinstance(event, Mapping):
            continue
        kind = event.get("kind")
        if kind in _WORKER_ACTIVE_INC:
            count += 1
        elif kind in _WORKER_ACTIVE_DEC:
            count = max(0, count - 1)
    if count == 0:
        return ""
    return f"subagents={count}"


def render_live_status_line(events: Any) -> str:
    """Render the tokens/s and active-worker counts as one live line.

    ``events`` is a sequence of already-redacted event records (see
    ``render_tokens_per_s``).  The non-empty parts from
    ``render_tokens_per_s`` and ``render_active_workers`` are joined with
    ``" · "`` and prefixed with ``"live: "``.  Returns ``""`` when both parts
    are empty.
    """
    parts = [
        part
        for part in (render_tokens_per_s(events), render_active_workers(events))
        if part
    ]
    if not parts:
        return ""
    return "live: " + " · ".join(parts)


__all__ = [
    "render_active_workers",
    "render_event_line",
    "render_json_result",
    "render_live_status_line",
    "render_text_result",
    "render_tokens_per_s",
    "render_usage_stats_line",
]
