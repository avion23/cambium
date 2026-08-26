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
import os
import shutil
import sys
import unicodedata
from collections.abc import Callable, Mapping
from dataclasses import asdict, is_dataclass
from pathlib import Path
from typing import Any

from .results import CHILD_RESULT_KEYS, ROOT_RESULT_KEYS, Result, result_to_dict
from .stats import usage_stats_from_events
from .terminal import sanitize_terminal_text

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
        "provider",
        "fell_back_from",
        "results",
    }
)

_SAFE_KEYS = frozenset(ROOT_RESULT_KEYS) | frozenset(CHILD_RESULT_KEYS) | _SUPERVISOR_RESULT_FIELDS

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
        ensure_ascii=True,
        allow_nan=False,
        separators=(",", ":"),
    )


_RESET = "\x1b[0m"
_OK_GREEN = "\x1b[32m"
_FAIL_RED = "\x1b[31m"
_DIM = "\x1b[2m"


def should_color(stream: Any = None) -> bool:
    """Return whether ``stream`` (default ``sys.stdout``) may receive ANSI color.

    Mirrors the ``render_markdown_if_tty`` gate: a tty stream, no
    ``NO_COLOR`` in the environment, and ``TERM`` other than ``dumb``.
    """
    target = sys.stdout if stream is None else stream
    try:
        if not bool(target.isatty()):
            return False
    except (AttributeError, OSError, ValueError):
        return False
    if os.environ.get("NO_COLOR"):
        return False
    return os.environ.get("TERM", "") != "dumb"


def _accent(text: str, code: str, stream: Any = None) -> str:
    safe_text = _sanitize_field(text)
    if stream is None or not should_color(stream):
        return safe_text
    return f"{code}{safe_text}{_RESET}"


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
            if not isinstance(item, list | tuple):
                continue
            item = [_filter_safe(entry) for entry in item if isinstance(entry, Mapping)]
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

    def safe(value: Any) -> str:
        return _sanitize_field(str(value))

    status = record.get("status")
    if isinstance(status, str) and status:
        parts.append(f"status={_sanitize_field(status)}")
    exit_code = record.get("exit_code")
    if isinstance(exit_code, int) and not isinstance(exit_code, bool):
        parts.append(f"exit={exit_code}")
    summary = record.get("summary")
    if isinstance(summary, str) and summary:
        parts.append(f"summary={_sanitize_field(summary)!r}")
    reason = record.get("failure_reason") or record.get("reason")
    if isinstance(reason, str) and reason:
        parts.append(f"reason={_sanitize_field(reason)!r}")
    for label, key in (("commits", "commits"), ("files", "files_changed")):
        items = record.get(key)
        if isinstance(items, list | tuple) and items:
            parts.append(f"{label}={len(items)}")
    metric = record.get("metric_score")
    if isinstance(metric, int | float) and not isinstance(metric, bool) and metric != 0:
        parts.append(f"metric={metric:g}")
    merge_sha = record.get("merge_sha")
    if isinstance(merge_sha, str) and merge_sha:
        parts.append(f"merge={_sanitize_field(merge_sha)[:12]}")
    results = record.get("results")
    if isinstance(results, list | tuple) and results:
        parts.append(f"plan=tasks:{len(results)}")
        statuses = ", ".join(
            safe(entry.get("status")) for entry in results if isinstance(entry, Mapping)
        )
        if statuses:
            parts.append(f"plan_status={{{statuses}}}")
        failures = [
            f"{safe(entry.get('task_id') or index)}:{_sanitize_field(entry['reason'])!r}"
            for index, entry in enumerate(results)
            if isinstance(entry, Mapping)
            and entry.get("status") != "succeeded"
            and isinstance(entry.get("reason"), str)
            and entry["reason"]
        ]
        if failures:
            parts.append(f"plan_failures={{{', '.join(failures)}}}")
        summaries = [
            f"{safe(entry.get('task_id') or index)}:{_sanitize_field(entry['summary'])!r}"
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
        if isinstance(value, bool) or not isinstance(value, int | float):
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
        groups.append(f"model={_sanitize_field(model)}")
    if not (isinstance(worktree, str) and worktree):
        worktree = record.get("worktree")
    if isinstance(worktree, str) and worktree:
        clean_worktree = _sanitize_field(worktree)
        groups.append(f"worktree={_sanitize_field(_short_worktree(clean_worktree))}")
    if not groups:
        return "stats:"
    return "stats: " + " · ".join(groups)


def render_usage_breakdown(breakdown: Any) -> str:
    """Render a ``cambium.stats.UsageBreakdown`` as a deterministic report.

    Accepts the ``UsageBreakdown`` dataclass or a JSON-like mapping with the
    same fields (``total`` plus ``by_task``/``by_provider`` as iterables of
    ``(name, UsageStats)`` pairs). One header line with the session totals, one
    line per task group and one per provider group. Returns ``""`` when the
    breakdown is None.
    """
    if breakdown is None:
        return ""
    by_task: Any = ()
    by_provider: Any = ()
    total: Any = None
    if is_dataclass(breakdown) and not isinstance(breakdown, type):
        by_task = getattr(breakdown, "by_task", ())
        by_provider = getattr(breakdown, "by_provider", ())
        total = getattr(breakdown, "total", None)
    elif isinstance(breakdown, Mapping):
        raw = dict(breakdown)
        by_task = list(raw.get("by_task") or ())
        by_provider = list(raw.get("by_provider") or ())
        total = raw.get("total")
    else:
        raise TypeError(
            "render_usage_breakdown requires a cambium.stats.UsageBreakdown "
            "dataclass or a JSON-like mapping"
        )

    def _amount(value: Any) -> float:
        if isinstance(value, bool) or not isinstance(value, int | float):
            return 0.0
        value = float(value)
        if not math.isfinite(value) or value <= 0:
            return 0.0
        return value

    def _group_line(name: str, stats: Any) -> str:
        line = render_usage_stats_line(stats)
        cost = 0.0
        if is_dataclass(stats) and not isinstance(stats, type):
            cost = _amount(getattr(stats, "estimated_cost_usd", None))
        elif isinstance(stats, Mapping):
            cost = _amount(stats.get("estimated_cost_usd"))
        if cost > 0:
            line = f"{line} · cost=${cost:.6f}"
        return f"{_sanitize_field(str(name))}: {line}"

    lines: list[str] = [f"usage: {render_usage_stats_line(total)}"]
    if total is not None and (isinstance(total, Mapping) or is_dataclass(total)):
        cost = 0.0
        if is_dataclass(total) and not isinstance(total, type):
            cost = _amount(getattr(total, "estimated_cost_usd", None))
        elif isinstance(total, Mapping):
            cost = _amount(total.get("estimated_cost_usd"))
        if cost > 0:
            lines[0] = f"{lines[0]} · cost=${cost:.6f}"
    for name, stats in by_task:
        lines.append(_group_line(name, stats))
    for name, stats in by_provider:
        lines.append(_group_line(name, stats))
    return "\n".join(lines)


def _sanitize_field(text: str) -> str:
    """Make one interpolated field safe for single-line terminal output.

    Remove ANSI CSI/OSC sequences and the remaining C0/C1 controls; collapse
    tab/newline runs to one space so a model-controlled value can neither emit
    escape sequences nor break the line.
    """
    return sanitize_terminal_text(text, single_line=True)


def _display_width(text: str) -> int:
    return sum(
        0
        if unicodedata.combining(char)
        else 2
        if unicodedata.east_asian_width(char) in {"W", "F"}
        else 1
        for char in text
    )


def _scalar(value: Any) -> str:
    if isinstance(value, str):
        return _sanitize_field(value)
    if isinstance(value, bool):
        return str(value)
    if isinstance(value, float) and math.isfinite(value):
        return f"{value:g}"
    if isinstance(value, list | tuple | dict):
        return _dumps(value)
    return _sanitize_field(str(value))


def _pair(payload: Mapping[str, Any], key: str, label: str | None = None) -> str | None:
    value = payload.get(key)
    if value is None or value == "":
        return None
    return f"{label or key}={_scalar(value)}"


def _text(payload: Mapping[str, Any], key: str) -> str | None:
    value = payload.get(key)
    if not isinstance(value, str) or not value:
        return None
    return _sanitize_field(value)


def _join(*parts: str | None) -> str:
    return " ".join(part for part in parts if part)


_TOOL_CMD_MAX_CHARS = 60


def _format_tool_event(payload: Mapping[str, Any], stream: Any = None) -> str:
    cmd = _text(payload, "cmd") or ""
    cmd = cmd[:_TOOL_CMD_MAX_CHARS]
    duration = payload.get("duration_ms")
    duration_text = (
        f"{duration}ms"
        if isinstance(duration, int | float) and not isinstance(duration, bool)
        else "?"
    )
    status = (
        _accent("OK", _OK_GREEN, stream)
        if payload.get("ok") is True
        else _accent("FAIL", _FAIL_RED, stream)
    )
    return f"{_text(payload, 'tool') or '?'} {cmd} {status} {duration_text}"


def _format_context_checkpoint(payload: Mapping[str, Any]) -> str:
    return _join(
        _pair(payload, "epoch"),
        _pair(payload, "turn"),
        _text(payload, "checkpoint_ref"),
    )


def _format_context_epoch_advanced(payload: Mapping[str, Any]) -> str:
    return _join(
        _format_context_checkpoint(payload),
        _pair(payload, "reason"),
        _pair(payload, "folded_from_epoch", label="folded_from"),
    )


def _format_checkpoint(payload: Mapping[str, Any]) -> str:
    turn = _pair(payload, "turn")
    return "ckpt" if turn is None else f"ckpt {turn}"


def _format_usage_event(payload: Mapping[str, Any]) -> str:
    failure_reason = _text(payload, "failure_reason")
    if failure_reason is None:
        return ""
    provider = _text(payload, "provider")
    head = f"provider {provider}" if provider else ""
    return _join(head, "FAILED", failure_reason)


def _format_result(payload: Mapping[str, Any], stream: Any = None) -> str:
    reason = _text(payload, "reason") or _text(payload, "failure_reason")
    status = payload.get("status")
    if not isinstance(status, str) or not status:
        status_part = None
    elif status == "succeeded":
        status_part = f"status={_accent(status, _OK_GREEN, stream)}"
    elif status == "failed":
        status_part = f"status={_accent(status, _FAIL_RED, stream)}"
    else:
        status_part = f"status={_sanitize_field(status)}"
    return _join(status_part, f"reason={reason}" if reason else None)


_STREAM_FORMATTERS = frozenset({_format_tool_event, _format_result})


def _format_spawned(payload: Mapping[str, Any]) -> str:
    worker = (_text(payload, "worker") or "")[:_TOOL_CMD_MAX_CHARS]
    return f"worker={worker}" if worker else ""


def _format_merge_committed(payload: Mapping[str, Any]) -> str:
    old = _text(payload, "old")
    new = _text(payload, "new")
    return _join(
        _pair(payload, "branch"),
        f"old={old[:12]}" if old else None,
        f"new={new[:12]}" if new else None,
    )


_EventFormatter = Callable[..., str]


_EVENT_FORMATTERS: dict[str, _EventFormatter] = {
    "tool_event": _format_tool_event,
    "context_checkpoint": _format_context_checkpoint,
    "context_epoch_advanced": _format_context_epoch_advanced,
    "checkpoint": _format_checkpoint,
    "heartbeat": lambda payload: "",
    "log": lambda payload: "",
    "ping": lambda payload: "",
    "pong": lambda payload: "",
    "usage_event": _format_usage_event,
    "spawned": _format_spawned,
    "init": lambda payload: _join(_pair(payload, "request_id")),
    "run_task": lambda payload: _join(_pair(payload, "request_id")),
    "ready": lambda payload: _join(_pair(payload, "pid")),
    "reuse_ready": lambda payload: _join(_pair(payload, "pid")),
    "exit": lambda payload: _join(_pair(payload, "reason")),
    "worker_failed": lambda payload: _join(_pair(payload, "reason")),
    "worker_terminated": lambda payload: _join(_pair(payload, "reason"), _pair(payload, "status")),
    "task_failed": lambda payload: _join(_pair(payload, "reason")),
    "result": _format_result,
    "session_ended": lambda payload: _join(_pair(payload, "session_status", "status")),
    "task_assigned": lambda payload: _join(
        _pair(payload, "branch"), _pair(payload, "assigned_provider")
    ),
    "merge_started": lambda payload: _join(_pair(payload, "branch")),
    "merge_committed": _format_merge_committed,
    "worktree_created": lambda payload: _join(_pair(payload, "branch")),
    "worktree_pruned": lambda payload: _join(_pair(payload, "branch")),
    "context_fork": lambda payload: _join(
        _pair(payload, "child_task_id", "child"), _pair(payload, "epoch")
    ),
    "context_resume": lambda payload: _join(
        _pair(payload, "epoch"), _pair(payload, "child_count", "children")
    ),
    "child_admitted": lambda payload: _join(
        _pair(payload, "child_task_id", "child"), _pair(payload, "branch")
    ),
    "protocol": lambda payload: _join(
        _pair(payload, "error_type"), _pair(payload, "note"), _pair(payload, "message", "msg")
    ),
    "parse_error": lambda payload: _join(_pair(payload, "message", "msg")),
    "compaction_failed": lambda payload: _join(_pair(payload, "epoch"), _pair(payload, "reason")),
    "context_resume_failed": lambda payload: _join(_pair(payload, "reason")),
    "child_rejected": lambda payload: _join(
        _pair(payload, "child_task_id", "child"),
        _pair(payload, "reason"),
        _pair(payload, "message", "msg"),
    ),
}


def render_event_line(event: Mapping[str, Any], *, stream: Any = None) -> str:
    """Render one redacted event record as one concise human-readable line.

    The line keeps the ``{seq:>6} {kind:>16} {task}  {body}`` prefix shape,
    with ``seq`` and ``task`` omitted when absent. Envelope ``kind`` and
    ``task_id`` pass through ``_sanitize_field`` before the prefix is padded,
    so padding aligns on the sanitized width. The body comes from the
    module-level ``_EVENT_FORMATTERS`` table for known kinds (an empty body
    prints nothing); unknown kinds fall back to a compact-JSON dump with
    non-ASCII characters escaped so unseen payloads stay visible and
    single-line.  Severity accents (tool OK|FAIL, result succeeded|failed)
    are emitted only when ``stream`` is the writing stream and
    ``should_color(stream)`` holds; ``stream=None`` renders plain.
    """
    if not isinstance(event, Mapping):
        raise TypeError("render_event_line requires an event mapping")
    kind = event.get("kind")
    if not isinstance(kind, str) or not kind:
        kind = "event"
    payload = event.get("payload")
    if not isinstance(payload, Mapping):
        payload = {key: value for key, value in event.items() if key not in _EVENT_ENVELOPE_KEYS}
    formatter = _EVENT_FORMATTERS.get(kind)
    if formatter is None:
        body = json.dumps(payload, sort_keys=True, separators=(",", ":"))
    elif formatter in _STREAM_FORMATTERS:
        body = formatter(payload, stream)
    else:
        body = formatter(payload)
    if not body:
        return ""
    clean_kind = _sanitize_field(kind)
    prefix = f"{' ' * max(0, 16 - _display_width(clean_kind))}{clean_kind}"
    seq = event.get("seq")
    if isinstance(seq, int) and not isinstance(seq, bool):
        prefix = f"{seq:>6} {prefix}"
    task_id = event.get("task_id")
    if isinstance(task_id, str) and task_id:
        prefix = f"{prefix} {_sanitize_field(task_id)}"
    return f"{prefix}  {body}"


def _finite_number(value: Any) -> float | None:
    if isinstance(value, bool) or not isinstance(value, int | float):
        return None
    try:
        number = float(value)
    except OverflowError:
        return None
    return number if math.isfinite(number) else None


def _event_timestamp(event: Mapping[str, Any], key: str) -> float | None:
    value = event.get(key)
    if isinstance(value, str):
        try:
            value = float(value)
        except ValueError:
            return None
    return _finite_number(value)


def _format_elapsed(seconds: float) -> str:
    total_seconds = max(0, int(seconds))
    hours, remainder = divmod(total_seconds, 3600)
    minutes, seconds = divmod(remainder, 60)
    if hours:
        return f"elapsed={hours}h{minutes}m{seconds}s"
    if minutes:
        return f"elapsed={minutes}m{seconds}s"
    return f"elapsed={seconds}s"


def render_elapsed(events: Any) -> str:
    """Render elapsed time from the first and last event timestamps.

    ``monotonic_ms`` is preferred when both endpoints provide a finite value;
    otherwise numeric ``ts`` values are interpreted as seconds. Returns ``""``
    when the first and last events have no usable common timestamp.
    """
    if events is None:
        return ""
    records = [event for event in events if isinstance(event, Mapping)]
    if not records:
        return ""
    for key, divisor in (("monotonic_ms", 1000.0), ("ts", 1.0)):
        first = _event_timestamp(records[0], key)
        last = _event_timestamp(records[-1], key)
        if first is not None and last is not None:
            return _format_elapsed(max(0.0, (last - first) / divisor))
    return ""


_WORKER_ACTIVE_INC = frozenset({"spawned"})
_WORKER_ACTIVE_DEC = frozenset({"exit", "reuse_ready", "worker_failed", "worker_terminated"})


def render_subagent_status(events: Any) -> str:
    """Render one deterministic per-agent status report from durable events."""

    if events is None:
        return ""
    records = [event for event in events if isinstance(event, Mapping)]
    from .monitor import render_agent_lines
    from .observability import snapshot_from_events

    snapshot = snapshot_from_events(records)
    if not snapshot.agents:
        return ""
    return "\n".join(render_agent_lines(snapshot))


def render_tokens_per_s(events: Any) -> str:
    """Render generation throughput in tokens per second from the latest
    usable ``usage_event``.

    ``events`` is a sequence of already-redacted event records (mappings with
    at least ``kind`` and ``payload`` keys).  For every ``usage_event`` whose
    payload ``latency_s`` is a positive finite number, the rate is that
    event's ``payload.usage.completion_tokens`` divided by its ``latency_s``
    -- generation throughput, so cache-served prompt tokens do not inflate
    it; only when ``completion_tokens`` is absent or non-numeric does the
    rate fall back to ``payload.usage.total_tokens``.  The LATEST such rate
    is returned as ``"tokens/s=12.3"`` with one decimal.  The guard is
    applied per event, so a single usage_event is handled identically: its
    own token count and ``latency_s`` define the rate.  Events without a
    usable token count (both ``completion_tokens`` and ``total_tokens``
    missing, non-numeric, or non-finite), or with a missing, zero, negative,
    or non-finite ``latency_s``, are skipped.  Returns ``""`` when no usable
    usage_event exists.
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
        latency = _finite_number(latency_s)
        if latency is None or latency <= 0:
            continue
        usage = payload.get("usage")
        if not isinstance(usage, Mapping):
            continue
        tokens = _finite_number(usage.get("completion_tokens"))
        if tokens is None:
            tokens = _finite_number(usage.get("total_tokens"))
        if tokens is None:
            continue
        rate = tokens / latency
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
    parts = [part for part in (render_tokens_per_s(events), render_active_workers(events)) if part]
    if not parts:
        return ""
    return "live: " + " · ".join(parts)


def render_status_bar(events: Any, *, session_label: str) -> str:
    """Render one live status bar line justified to the terminal width.

    ``events`` is a sequence of already-redacted event records.  LEFT is
    ``session=<label>``, ``render_elapsed(events)`` and the latest event
    ``task_id``; RIGHT is ``render_tokens_per_s(events)``, the aggregate
    ``in``/``out``/``cached`` token counts plus ``cost`` from the same
    ``cambium.stats`` primitives that back ``render_usage_stats_line``
    (via ``usage_stats_from_events``), and ``render_active_workers``.
    Segments whose renderer returns ``""`` are dropped, and ``session_label``
    and the task id pass through ``_sanitize_field``.  LEFT and RIGHT are
    joined with spacing so the line spans ``shutil.get_terminal_size()``
    columns exactly when both sides fit with at least one space between;
    RIGHT segments are dropped from the end until it fits.  Returns ``""``
    when there are no events at all.
    """
    records = [event for event in (events or ()) if isinstance(event, Mapping)]
    if not records:
        return ""
    left_parts = [f"session={_sanitize_field(session_label)}"]
    elapsed = render_elapsed(records)
    if elapsed:
        left_parts.append(elapsed)
    task_id = ""
    for event in reversed(records):
        candidate = event.get("task_id")
        if isinstance(candidate, str) and candidate:
            task_id = _sanitize_field(candidate)
            break
    if task_id:
        left_parts.append(f"task={task_id}")
    heartbeats = [record for record in records if record.get("kind") == "heartbeat"]
    if heartbeats:
        last = heartbeats[-1]
        status = (
            last.get("payload", {}).get("status")
            if isinstance(last.get("payload"), Mapping)
            else None
        )
        if status == "working":
            frames = ("/", "-", "\\", "|")
            left_parts.append(frames[(len(heartbeats) - 1) % len(frames)])
    right_parts: list[str] = []
    rate = render_tokens_per_s(records)
    if rate:
        right_parts.append(rate)
    stats = usage_stats_from_events(records)
    if stats is not None:
        right_parts.append(
            f"in={stats.input_tokens} out={stats.output_tokens} cached={stats.cached_tokens}"
        )
        right_parts.append(f"cost=${stats.estimated_cost_usd:.6f}")
    workers = render_active_workers(records)
    if workers:
        right_parts.append(workers)
    left = " · ".join(left_parts)
    columns = shutil.get_terminal_size().columns
    while True:
        if not right_parts:
            return left
        right = " · ".join(right_parts)
        gap = columns - _display_width(left) - _display_width(right)
        if gap >= 1:
            return left + " " * gap + right
        right_parts.pop()


__all__ = [
    "render_active_workers",
    "render_event_line",
    "render_elapsed",
    "render_json_result",
    "render_live_status_line",
    "render_status_bar",
    "render_subagent_status",
    "render_text_result",
    "render_tokens_per_s",
    "render_usage_breakdown",
    "render_usage_stats_line",
]
