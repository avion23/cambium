"""Pure renderers for Cambium results and event lines.

The three public functions are side-effect free and deterministic.  They
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
from collections.abc import Mapping
from dataclasses import asdict, is_dataclass
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
    """Render one result record as one concise human-readable line."""
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
    return " ".join(parts)


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


__all__ = ["render_event_line", "render_json_result", "render_text_result"]
