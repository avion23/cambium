"""Terminal presentation model for Cambium's interactive frontend.

The cockpit is intentionally a presentation layer over immutable session and
observability snapshots.  It owns no provider, worker, branch, or context
state.  The only mutable value is a bounded local transcript used for the
operator's current terminal view.  Live output is appended to the terminal's
primary buffer so the terminal, rather than a private alternate screen, owns
scrollback.

``render_cockpit`` remains available as a deterministic framed renderer for
presentation tests and callers that need a bounded snapshot.  ``Cockpit``
uses the same primary-row helpers for the live interactive path.
"""

from __future__ import annotations

import math
import os
import re
import shutil
import signal
import textwrap
import threading
import time
import unicodedata
from collections import deque
from collections.abc import Mapping
from dataclasses import dataclass
from decimal import Decimal
from functools import lru_cache
from typing import Any, TextIO

from .render_markdown import render_markdown_lines as _shared_markdown_lines
from .terminal import (
    clip_terminal_text,
    pad_terminal_text,
    sanitize_terminal_text,
    terminal_color_depth,
    terminal_display_width,
)

try:
    import readline as _readline
except ImportError:  # pragma: no cover - platform dependent
    _readline = None

_RESET = "\x1b[0m"
_DIM = "\x1b[2m"
_CYAN = "\x1b[1;36m"
_DIM_CYAN = "\x1b[2;36m"
_CLEAR_LINE = "\x1b[2K"
_BLUE = "\x1b[1;34m"
_GREEN = "\x1b[1;32m"
_YELLOW = "\x1b[1;33m"
_RED = "\x1b[1;31m"
_MAGENTA = "\x1b[1;35m"
_WHITE = "\x1b[1;37m"
_SKY = "\x1b[38;5;81m"
_TEAL = "\x1b[38;5;44m"
_VIOLET = "\x1b[38;5;141m"
_AMBER = "\x1b[38;5;179m"
_CORAL = "\x1b[38;5;203m"
_PINK = "\x1b[38;5;213m"
_MINT = "\x1b[38;5;84m"
_STEEL = "\x1b[38;5;110m"
_BRIGHT = "\x1b[38;5;255m"

# Model/provider text is sanitized before Rich adds terminal styling. Keep only
# harmless SGR text attributes/foreground colors after that boundary; cursor,
# background, OSC, and other control sequences remain forbidden.
_MD_BOLD = "\x1b[1m"
_SAFE_SGR_ATTRIBUTES = frozenset({1, 2, 3, 4, 9, 21, 53})
_STATUS_PALETTE = {
    "cyan": (_CYAN, _TEAL),
    "dim": (_DIM, _DIM),
    "blue": (_BLUE, _SKY),
    "green": (_GREEN, _MINT),
    "yellow": (_YELLOW, _AMBER),
    "red": (_RED, _CORAL),
    "magenta": (_MAGENTA, _VIOLET),
    "white": (_WHITE, _BRIGHT),
    "pink": (_MAGENTA, _PINK),
    "bold": (_MD_BOLD, _MD_BOLD),
}
_ANSI_STYLE = re.compile(r"\x1b\[[0-9;]*m")

_ROLE_COLORS = {
    "user": _BLUE,
    "assistant": _WHITE,
    "tool": _YELLOW,
    "system": _DIM_CYAN,
    "error": _RED,
    "dim": _DIM,
    "live": _CYAN,
}
_ROLE_VIVID = {
    "user": _SKY,
    "assistant": _TEAL,
    "tool": _AMBER,
    "system": _STEEL,
    "error": _CORAL,
    "dim": _DIM,
    "live": _MINT,
}
_ROLE_LABELS = {
    "user": "YOU",
    "assistant": "CAMBIUM",
    "tool": "TOOL",
    "system": "SYSTEM",
    "error": "ERROR",
}

# The supervisor currently exposes lifecycle/tool events rather than a
# provider-specific token stream.  Keep the accepted presentation events
# explicit: arbitrary log payloads must never become terminal prose.
_ASSISTANT_STREAM_KINDS = frozenset(
    {
        "assistant_delta",
        "assistant_message",
        "assistant_output",
        "assistant_output_delta",
        "assistant_text",
        "assistant_text_delta",
        "content_delta",
        "message",
        "message_delta",
        "output_text_delta",
        "partial_output",
        "response",
        "response.output_text.delta",
        "stream_chunk",
        "text_delta",
    }
)
_TOOL_STREAM_KINDS = frozenset(
    {
        "tool_message",
        "tool_message_delta",
        "tool_output",
        "tool_output_delta",
    }
)
_STREAM_DELTA_KINDS = frozenset(
    {
        "assistant_delta",
        "assistant_output_delta",
        "assistant_text_delta",
        "content_delta",
        "message_delta",
        "output_text_delta",
        "partial_output",
        "response.output_text.delta",
        "stream_chunk",
        "text_delta",
        "tool_message_delta",
        "tool_output_delta",
    }
)
_STREAM_TEXT_LIMIT = 16_384
_STREAM_RENDER_LIMIT = 8_192
_TOOL_DETAIL_RENDER_LIMIT = 40
_TOOL_DETAIL_KEYS = (
    "cmd",
    "command",
    "error",
    "failure_reason",
    "reason",
    "message",
    "output",
    "stdout",
    "stderr",
    "detail",
)
_FAILURE_CONTEXT_PREFIX = "↳ "
_FAILURE_BLOCK_LIMIT = 64
_FAILURE_EVENT_KINDS = frozenset(
    {
        "child_failed",
        "compaction_failed",
        "context_resume_failed",
        "error",
        "fatal_error",
        "merge_failed",
        "plan_failed",
        "session_failed",
        "task_failed",
        "turn_failed",
        "turn_failure",
        "worker_failed",
    }
)
_FAILURE_STATUSES = frozenset({"error", "failed", "timeout"})
_TOOL_ERROR_PREFIX = "tool errors:"
_SPINNER_FRAMES = ("⠋", "⠙", "⠹", "⠸", "⠼", "⠴", "⠦", "⠧", "⠇", "⠏")
_ACTIVITY_PHASE_GLYPHS = {"thinking": "◌", "streaming": "▸", "waiting": "◒"}
_STALL_AFTER_S = 12.0
_ACTIVITY_TAIL_MAX_CHARS = 120
_ACTIVITY_PHASE_RE = re.compile(
    r"^[◌▸…]\s+(thinking|streaming|waiting)\s+(\d+(?:\.\d+)?)s(?:\s+·\s*(.*))?$",
    re.IGNORECASE,
)
_STATUS_PHASE_RE = re.compile(r"^(\S+)(\s+)([a-z][a-z-]*)(.*)$", re.IGNORECASE)
_STATUS_PHASE_STYLES = {
    "idle": "dim",
    "thinking": "magenta",
    "streaming": "green",
    "responding": "green",
    "running": "cyan",
    "tool": "cyan",
    "provider": "blue",
    "routing": "cyan",
    "children": "pink",
    "orchestrating": "cyan",
    "stalled": "yellow",
    "done": "green",
    "failed": "red",
    "error": "red",
    "queued": "yellow",
    "waiting": "blue",
    "cooldown": "yellow",
    "suspended": "yellow",
}
_FIRST_TOKEN_KINDS = frozenset(
    {
        "assistant_first_token",
        "first_token",
        "first_token_received",
    }
)
_TURN_DONE_KINDS = frozenset(
    {
        "complete",
        "done",
        "result",
        "turn_complete",
        "turn_completed",
        "turn_finished",
    }
)
_TURN_ERROR_KINDS = frozenset(
    {
        "error",
        "fatal_error",
        "session_failed",
        "task_failed",
        "turn_failed",
        "turn_failure",
        "worker_failed",
    }
)
_COOLDOWN_STATUSES = frozenset(
    {
        "cooldown",
        "cooling_down",
        "rate_limited",
        "rate-limited",
        "throttled",
    }
)
_TOOL_START_KINDS = frozenset(
    {
        "tool_begin",
        "tool_call",
        "tool_call_started",
        "tool_request",
        "tool_start",
        "tool_started",
        "tool_invoked",
    }
)
_TOOL_END_KINDS = frozenset(
    {
        "tool_complete",
        "tool_completed",
        "tool_end",
        "tool_ended",
        "tool_event",
        "tool_finished",
        "tool_result",
    }
)
_TOOL_PHASE_STARTS = frozenset(
    {"begin", "in-flight", "in_flight", "pending", "running", "start", "started"}
)
_TOOL_PHASE_ENDS = frozenset(
    {
        "cancelled",
        "canceled",
        "complete",
        "completed",
        "done",
        "end",
        "ended",
        "failed",
        "finished",
        "ok",
        "success",
        "succeeded",
    }
)
_LIVE_WINDOW_ROWS = 2
_LIVE_EVENT_KINDS = frozenset(
    {
        "child_admitted",
        "child_rejected",
        "checkpoint",
        "compaction_failed",
        "context_checkpoint",
        "context_epoch_advanced",
        "context_resume_failed",
        "error",
        "fatal_error",
        "heartbeat",
        "log",
        "result",
        "run_task",
        "task_assigned",
        "task_failed",
        "timeout",
        "tool_event",
        "tool_output_delta",
        "turn_failed",
        "usage_event",
        "worker_failed",
    }
)
_LIVE_TEXT_LIMIT = 4_096


def _is_tty(stream: Any) -> bool:
    try:
        return bool(getattr(stream, "isatty", lambda: False)())
    except (AttributeError, OSError, ValueError):
        return False


def _sanitize(value: Any) -> str:
    clean = sanitize_terminal_text(value)
    return clean.encode("utf-8", errors="backslashreplace").decode("utf-8")


def _single_line(value: Any) -> str:
    return sanitize_terminal_text(value, single_line=True).strip()


def _utf8_size(value: str) -> int:
    return len(value.encode("utf-8", errors="replace"))


def _activity_tail(value: Any) -> str:
    if not isinstance(value, str):
        return ""
    clean = sanitize_terminal_text(value, single_line=True).strip()
    return _clip(clean, _ACTIVITY_TAIL_MAX_CHARS) if clean else ""


def _safe_sgr(code: str) -> bool:
    """Accept only text-style/foreground SGR emitted after source sanitization."""
    if code == _RESET:
        return True
    match = _ANSI_STYLE.fullmatch(code)
    if match is None:
        return False
    try:
        values = [int(value) for value in code[2:-1].split(";")]
    except ValueError:
        return False
    while values and values[0] in _SAFE_SGR_ATTRIBUTES:
        values.pop(0)
    if not values:
        return True
    if len(values) == 1:
        return values[0] == 39 or 30 <= values[0] <= 37 or 90 <= values[0] <= 97
    if len(values) == 3 and values[:2] == [38, 5]:
        return 0 <= values[2] <= 255
    if len(values) == 5 and values[:2] == [38, 2]:
        return all(0 <= component <= 255 for component in values[2:])
    return False


def _safe_rendered(text: Any) -> str:
    """Sanitize text while retaining safe renderer-owned foreground styling."""
    parts: list[str] = []
    for part in re.split(r"(\x1b\[[0-9;]*m)", str(text)):
        parts.append(part if _safe_sgr(part) else _sanitize(part))
    return "".join(parts)


def _visible(text: str) -> str:
    return _ANSI_STYLE.sub("", _safe_rendered(text))


def _char_width(char: str) -> int:
    if unicodedata.combining(char) or unicodedata.category(char) == "Cf":
        return 0
    return 2 if unicodedata.east_asian_width(char) in {"W", "F"} else 1


def _display_width(text: str) -> int:
    return sum(_char_width(char) for char in _visible(text))


def _take_display_width(text: str, width: int) -> tuple[str, str]:
    """Split text at a terminal column boundary without splitting code points."""
    if width <= 0:
        return "", text
    used = 0
    rendered = _safe_rendered(text)
    left: list[str] = []
    index = 0
    while index < len(rendered):
        match = _ANSI_STYLE.match(rendered, index)
        if match is not None:
            code = match.group(0)
            if _safe_sgr(code):
                left.append(code)
                index = match.end()
                continue
        char = rendered[index]
        char_width = _char_width(char)
        if used and used + char_width > width:
            return "".join(left), rendered[index:]
        if not used and char_width > width:
            return "", rendered[index:]
        left.append(char)
        used += char_width
        index += 1
    return rendered, ""


def _clip(text: str, width: int) -> str:
    clean = _safe_rendered(text)
    if width <= 0:
        return ""
    if _display_width(clean) <= width:
        return clean
    if width == 1:
        return _sanitize("…")
    head, _ = _take_display_width(clean, width - 1)
    reset = _RESET if _ANSI_STYLE.search(head) and not head.endswith(_RESET) else ""
    return head + _sanitize("…") + reset


def _pad(text: str, width: int) -> str:
    clean = _clip(text, width)
    return clean + " " * max(0, width - _display_width(clean))


def _fmt_secs(seconds: float) -> str:
    """Whole-second duration label; durations never render decimal delimiters."""
    return f"{int(seconds)}s"


def _human_count(value: int) -> str:
    if value < 1_000:
        return str(value)
    if value < 1_000_000:
        return f"{value / 1_000:.1f}".rstrip("0").rstrip(".") + "k"
    return f"{value / 1_000_000:.1f}".rstrip("0").rstrip(".") + "m"


def _human_bytes(value: int) -> str:
    if value < 1_024:
        return f"{value}B"
    if value < 1_024 * 1_024:
        return f"{value / 1_024:.1f}".rstrip("0").rstrip(".") + "KiB"
    return f"{value / (1_024 * 1_024):.1f}".rstrip("0").rstrip(".") + "MiB"


def _paint(text: str, color: str, enabled: bool | int) -> str:
    clean = _safe_rendered(text)
    return f"{color}{clean}{_RESET}" if enabled else clean


def _vivid_color(enabled: bool | int) -> bool:
    return type(enabled) is int and (enabled == 24 or enabled >= 256)


def _status_color(style: str, enabled: bool | int) -> str:
    standard, vivid = _STATUS_PALETTE[style]
    return vivid if _vivid_color(enabled) else standard


def _status_paint(text: str, style: str, enabled: bool | int) -> str:
    """Apply one semantic palette entry at the terminal's available color depth."""
    return _paint(text, _status_color(style, enabled), enabled)


def _role_color(role: str, enabled: bool | int) -> str:
    palette = _ROLE_VIVID if _vivid_color(enabled) else _ROLE_COLORS
    return palette.get(role, "")


def _event_data(record: Mapping[str, Any]) -> Mapping[str, Any]:
    payload = record.get("payload")
    return payload if isinstance(payload, Mapping) else record


def _message_role(kind: str, data: Mapping[str, Any]) -> str | None:
    role = data.get("role")
    if role not in {"assistant", "tool"}:
        message = data.get("message")
        if isinstance(message, Mapping):
            role = message.get("role")
    if role in {"assistant", "tool"}:
        return role
    if kind == "tool_event" or kind in _TOOL_STREAM_KINDS:
        return "tool"
    if kind in _ASSISTANT_STREAM_KINDS and kind != "message":
        return "assistant"
    return None


def _text_value(value: Any, *, depth: int = 0) -> str | None:
    """Extract text from the small set of provider message shapes we display."""
    if depth > 4:
        return None
    if isinstance(value, str):
        return value
    if isinstance(value, Mapping):
        for key in ("delta", "text", "content", "output_text", "partial", "chunk"):
            if key in value:
                text = _text_value(value[key], depth=depth + 1)
                if text is not None:
                    return text
        message = value.get("message")
        if message is not None:
            return _text_value(message, depth=depth + 1)
        return None
    if isinstance(value, list | tuple):
        parts: list[str] = []
        for item in value:
            text = _text_value(item, depth=depth + 1)
            if text is not None:
                parts.append(text)
        return "".join(parts) if parts else None
    return None


def _result_text(data: Mapping[str, Any]) -> str | None:
    parts: list[str] = []
    for key in ("summary", "assistant_text", "output_text"):
        value = data.get(key)
        if isinstance(value, str) and value:
            parts.append(value)
    results = data.get("results")
    if isinstance(results, list | tuple):
        for result in results:
            if not isinstance(result, Mapping):
                continue
            summary = result.get("summary")
            if isinstance(summary, str) and summary:
                parts.append(summary)
    return "\n\n".join(parts) if parts else None


def _stream_update(
    record: Mapping[str, Any],
) -> tuple[str, str, bool, str | None] | None:
    """Return ``(role, text, append, message_id)`` for displayable output."""
    kind = record.get("kind")
    if not isinstance(kind, str):
        return None
    data = _event_data(record)
    if kind == "result":
        if data.get("status") in _FAILURE_STATUSES or data.get("status") == "suspended":
            return None
        text = _result_text(data)
        if text is None:
            return None
        return "assistant", text, False, None
    if kind in _ASSISTANT_STREAM_KINDS or kind in _TOOL_STREAM_KINDS:
        tool_status = _tool_status(data)
        if kind == "tool_event" and tool_status is not None and not tool_status:
            return None
        role = _message_role(kind, data)
        if role is None:
            return None
        text = _text_value(data)
        if not text:
            return None
        append = kind in _STREAM_DELTA_KINDS
        if data.get("cumulative") or data.get("replace"):
            append = False
        if data.get("append"):
            append = True
        message_id = data.get("message_id") or data.get("id")
        return role, text, append, message_id if isinstance(message_id, str) else None
    return None


def _duration_ms(value: Any) -> int | float | None:
    if type(value) not in (int, float):
        return None
    return value


def _tool_status(data: Mapping[str, Any]) -> bool | None:
    status = data.get("ok")
    if type(status) is bool:
        return status
    status_name = data.get("status")
    if isinstance(status_name, str):
        normalized = status_name.casefold()
        if normalized in {"failed", "failure", "error"}:
            return False
        if normalized in {"ok", "success", "succeeded"}:
            return True
    for key in ("error", "failure_reason"):
        value = data.get(key)
        if isinstance(value, str) and value:
            return False
    return None


def _tool_detail_value(value: Any) -> str | None:
    if value is None:
        return None
    if isinstance(value, str):
        return value
    text = _text_value(value)
    if text is not None:
        return text
    if isinstance(value, list | tuple):
        return " ".join(_sanitize(item) for item in value)
    if isinstance(value, Mapping):
        parts = [f"{key}={_sanitize(item)}" for key, item in value.items()]
        return ", ".join(parts) if parts else None
    return _sanitize(value)


def _tool_entry_text(
    data: Mapping[str, Any],
    tool: str,
    ok: bool | None,
    duration_ms: int | float | None,
) -> str:
    state = "ok" if ok else "failed" if ok is not None else "done"
    duration = f" · {_format_duration(duration_ms)}" if duration_ms is not None else ""
    lines = [f"{tool}: {state}{duration}"]
    for key in _TOOL_DETAIL_KEYS:
        detail = _tool_detail_value(data.get(key))
        if detail:
            lines.append(f"{key}: {detail}")
    return "\n".join(lines)


def _format_duration(duration_ms: int | float | None) -> str:
    """Format milliseconds as integer milliseconds or truncated seconds.

    Durations at or above one second intentionally truncate fractional seconds
    (for example, ``1500ms`` renders as ``1s``).
    """
    if duration_ms is None:
        return ""
    milliseconds = max(0.0, _usage_float(duration_ms))
    if milliseconds < 1_000:
        return f"{int(milliseconds)}ms"
    return f"{int(milliseconds / 1_000)}s"


def _tool_line(
    entry: TranscriptEntry,
    *,
    count: int = 1,
    last_duration_ms: int | float | None = None,
) -> str:
    glyph = "✓" if entry.tool_ok else "✗" if entry.tool_ok is not None else "•"
    name = entry.tool_name or "?"
    if count > 1:
        duration = _format_duration(last_duration_ms) if _usage_float(last_duration_ms) > 0 else ""
        prefix = f"{duration:>7} " if duration else ""
        line = f"{prefix}{glyph} {name} ×{count}"
    else:
        duration = (
            _format_duration(entry.duration_ms) if _usage_float(entry.duration_ms) > 0 else ""
        )
        prefix = f"{duration:>7} " if duration else ""
        line = f"{prefix}{glyph} {name}"
    return line


@dataclass(frozen=True, slots=True)
class TranscriptEntry:
    """One bounded, terminal-only transcript item."""

    role: str
    text: str
    tool_name: str | None = None
    tool_ok: bool | None = None
    duration_ms: int | float | None = None


@dataclass(slots=True)
class _FailureBlock:
    """Presentation state for one task failure within one turn."""

    task_id: str
    turn: int | None
    cause: str | None
    context: list[str]
    entry: TranscriptEntry | None = None


def _task_id(record: Mapping[str, Any], data: Mapping[str, Any]) -> str | None:
    for value in (record.get("task_id"), data.get("task_id")):
        if isinstance(value, str) and value:
            return value
    return None


def _event_turn(data: Mapping[str, Any]) -> int | None:
    value = data.get("turn")
    return value if type(value) is int and value >= 0 else None


def _failure_context_line(kind: str, data: Mapping[str, Any]) -> str | None:
    """Return a short, safe line for a failure's preceding-event context."""
    tool_status = _tool_status(data)
    if kind == "tool_event" and tool_status is not None and not tool_status:
        tool = data.get("tool")
        if not isinstance(tool, str) or not tool:
            return "tool failed"
        line = f"{tool}: failed"
        for key in _TOOL_DETAIL_KEYS:
            detail = _tool_detail_value(data.get(key))
            if detail:
                line += f" · {key}: {detail}"
        return _sanitize(line)

    if kind == "timeout":
        phase = data.get("phase")
        return _sanitize(f"timeout: {phase}" if isinstance(phase, str) and phase else "timeout")

    if kind == "restart_scheduled":
        count = data.get("restart_count")
        maximum = data.get("max_restarts")
        if type(count) is int and type(maximum) is int:
            return f"restart scheduled: {count}/{maximum}"
        return "restart scheduled"

    if kind == "usage_event":
        reason = data.get("failure_reason")
        if isinstance(reason, str) and reason:
            return _sanitize(f"provider call failed: {reason}")

    if kind == "protocol":
        detail = data.get("note") or data.get("error_type")
        if isinstance(detail, str) and detail:
            return _sanitize(f"protocol: {detail}")

    if kind == "log" and data.get("stream") == "worker-error":
        detail = data.get("message") or data.get("error_type")
        if isinstance(detail, str) and detail:
            return _sanitize(f"worker error: {detail}")
    return None


def _failure_cause(kind: str, data: Mapping[str, Any]) -> str | None:
    """Extract the most actionable failure cause from one terminal event."""
    cause: str | None = None
    for key in ("failure_reason", "reason", "message", "error"):
        value = data.get(key)
        if isinstance(value, str) and value.strip():
            cause = _sanitize(value).strip()
            break
    maximum = data.get("max_restarts")
    if (
        kind == "worker_failed"
        and type(maximum) is int
        and maximum >= 0
        and cause
        and not cause.startswith("max_restarts (")
    ):
        cause = f"max_restarts ({maximum}): {cause}"
    if cause:
        return cause
    if kind == "result":
        status = data.get("status")
        if isinstance(status, str) and status:
            return _sanitize(f"worker reported {status}")
    return _sanitize(kind.replace("_", " ")) if kind else None


def _failure_summary(text: str) -> tuple[str | None, str | None] | None:
    """Parse a rendered result summary without exposing its detail twice."""
    clean = _sanitize(text)
    if not clean:
        return None
    failed = bool(
        re.search(r"\bstatus=(?:error|failed|timeout)\b", clean)
        or re.search(r"\bplan_status=\{[^}]*\b(?:error|failed|timeout)\b", clean)
        or "plan_failures={" in clean
    )
    if not failed:
        return None

    task_id: str | None = None
    cause: str | None = None
    plan_failures = re.search(r"\bplan_failures=\{([^}]*)\}", clean)
    if plan_failures is not None:
        pair = re.search(r"([^,\s:{}]+)\s*:\s*(['\"])(.*?)\2", plan_failures.group(1))
        if pair is not None:
            task_id = pair.group(1)
            cause = pair.group(3)

    if task_id is None:
        task_match = re.search(r"\btask(?:_id)?=([^\s]+)", clean)
        if task_match is not None:
            task_id = task_match.group(1).strip("'\"")
    if cause is None:
        reason = re.search(r"\b(?:failure_reason|reason)=((['\"])(.*?)\2|[^\s]+)", clean)
        if reason is not None:
            cause = reason.group(3) if reason.group(3) is not None else reason.group(1)
            if cause is not None:
                cause = cause.strip("'\"")
    if cause:
        cause = _sanitize(cause).strip()
    return task_id, cause or None


class Transcript:
    """Bounded semantic transcript for the current interactive frontend."""

    def __init__(self, *, max_entries: int = 160) -> None:
        if max_entries < 8:
            raise ValueError("max_entries must be at least 8")
        self._entries: deque[TranscriptEntry] = deque(maxlen=max_entries)
        self._stream_role: str | None = None
        self._stream_text = ""
        self._stream_message_id: str | None = None
        self._stream_truncated = False
        self._stream_tool_key: str | None = None
        self._turn_serial = 0
        self._turn_by_task: dict[str, int] = {}
        self._failure_context: dict[tuple[str, int | None, int], list[str]] = {}
        self._failure_blocks: dict[tuple[str, int | None, int], _FailureBlock] = {}
        self._failure_order: deque[tuple[str, int | None, int]] = deque()
        self._tool_failure_key: int | None = None
        self._tool_failure_count = 0
        self._tool_error_total = 0
        self._tool_failure_entry: TranscriptEntry | None = None
        self._tool_count = 0
        self._turn_tool_count = 0
        self._last_tool_name: str | None = None
        self._last_tool_duration_ms: int | float | None = None
        self._tool_details_expanded = False
        self._live_revision = 0
        self._live_task_id: str | None = None
        self._live_kind = ""
        self._live_role = "live"
        self._live_text = ""
        self._live_status = ""
        self._live_phase = ""
        self._live_tool: str | None = None
        self._live_command = ""
        self._live_duration_ms: int | float | None = None
        self._live_turn: int | None = None
        self._live_calls = 0
        self._live_bytes = 0
        self._live_age_s = 0.0
        self._live_started_at: float | None = None
        self._live_provider_call_open = False
        self._live_cache_hit: bool | None = None
        self._live_final = False

    @property
    def entries(self) -> tuple[TranscriptEntry, ...]:
        return tuple(self._entries)

    def clear(self) -> None:
        self._entries.clear()
        self._clear_stream()
        self._turn_serial = 0
        self._turn_by_task.clear()
        self._failure_context.clear()
        self._failure_blocks.clear()
        self._failure_order.clear()
        self._tool_failure_key = None
        self._tool_failure_count = 0
        self._tool_error_total = 0
        self._tool_failure_entry = None
        self._tool_count = 0
        self._turn_tool_count = 0
        self._last_tool_name = None
        self._last_tool_duration_ms = None
        self._clear_live_window()

    @property
    def tool_details_expanded(self) -> bool:
        """Whether tool command/output details are shown instead of compact lines."""
        return self._tool_details_expanded

    def toggle_tool_details(self) -> bool:
        """Toggle the presentation-only expand-all tool detail view."""
        self._tool_details_expanded = not self._tool_details_expanded
        return self._tool_details_expanded

    def add(self, role: str, text: str) -> None:
        if role not in _ROLE_LABELS:
            raise ValueError(f"unknown transcript role: {role}")
        clean = _sanitize(text).strip("\n")
        if clean:
            self._entries.append(TranscriptEntry(role=role, text=clean))

    def user(self, text: str) -> None:
        self._turn_serial += 1
        self._turn_by_task.clear()
        self._tool_failure_key = None
        self._tool_failure_count = 0
        self._tool_failure_entry = None
        self._turn_tool_count = 0
        self._last_tool_name = None
        self._last_tool_duration_ms = None
        self._clear_stream()
        self._clear_live_window()
        self.add("user", text)

    def assistant(self, text: str) -> None:
        self.add("assistant", text)

    def system(self, text: str) -> None:
        clean = _sanitize(text).strip("\n")
        if (
            clean.startswith("queued:")
            and self._entries
            and self._entries[-1].role == "system"
            and self._entries[-1].text == clean
        ):
            return
        self.add("system", text)

    def error(self, text: str) -> None:
        self.add("error", text)

    @property
    def streaming_text(self) -> str:
        """The bounded in-flight model text, if a turn is still generating."""
        return self._stream_text

    @property
    def streaming_role(self) -> str | None:
        return self._stream_role

    @property
    def streaming_key(self) -> str | None:
        """Return the identity of the in-flight stream, when one is known."""
        return self._stream_tool_key or self._stream_message_id

    @property
    def tool_error_count(self) -> int:
        """Return routine tool failures observed during this session."""
        return self._tool_error_total

    @property
    def current_tool_error_count(self) -> int:
        """Return routine tool failures observed in the current turn."""
        return self._tool_failure_count

    @property
    def tool_count(self) -> int:
        """Return tool calls observed during this session."""
        return self._tool_count

    @property
    def current_tool_count(self) -> int:
        """Return tool calls observed in the current turn."""
        return self._turn_tool_count

    @property
    def last_tool_name(self) -> str | None:
        return self._last_tool_name

    @property
    def last_tool_duration_ms(self) -> int | float | None:
        return self._last_tool_duration_ms

    @property
    def live_revision(self) -> int:
        """Return the revision of the latest real progress/result event."""
        return self._live_revision

    @property
    def live_final(self) -> bool:
        """Whether the fixed live window currently holds terminal result text."""
        return self._live_final

    def _clear_live_window(self) -> None:
        self._live_revision += 1
        self._live_task_id = None
        self._live_kind = ""
        self._live_role = "live"
        self._live_text = ""
        self._live_status = ""
        self._live_phase = ""
        self._live_tool = None
        self._live_command = ""
        self._live_duration_ms = None
        self._live_turn = None
        self._live_calls = 0
        self._live_bytes = 0
        self._live_age_s = 0.0
        self._live_started_at = None
        self._live_provider_call_open = False
        self._live_cache_hit = None
        self._live_final = False

    @staticmethod
    def _event_clock(record: Mapping[str, Any]) -> float | None:
        monotonic_ms = record.get("monotonic_ms")
        if isinstance(monotonic_ms, int | float) and math.isfinite(monotonic_ms):
            return max(0.0, float(monotonic_ms) / 1000.0)
        timestamp = record.get("ts")
        if isinstance(timestamp, int | float) and math.isfinite(timestamp):
            return max(0.0, float(timestamp))
        return None

    @staticmethod
    def _live_event_text(kind: str, data: Mapping[str, Any]) -> str:
        if kind == "heartbeat":
            return _text_value(data.get("tail")) or ""
        if kind == "tool_event":
            for key in ("output", "stdout", "stderr", "message", "error", "cmd", "tool"):
                value = _text_value(data.get(key))
                if value:
                    return value
            return ""
        if kind == "usage_event":
            # Usage/cache values already have dedicated status and rail rows.
            # Repeating them as LIVE transcript text adds noise without state.
            return ""
        if kind == "result":
            return _result_text(data) or _single_line(data.get("status"))
        for key in (
            "message",
            "reason",
            "failure_reason",
            "error",
            "summary",
            "output",
            "cmd",
            "tool",
            "phase",
            "status",
        ):
            value = _text_value(data.get(key))
            if value:
                return value
        return ""

    def _start_live_operation(
        self,
        task_id: str | None,
        event_clock: float | None,
        *,
        force: bool = False,
    ) -> None:
        if not force and task_id == self._live_task_id:
            return
        self._live_task_id = task_id
        self._live_kind = ""
        self._live_role = "live"
        self._live_text = ""
        self._live_status = ""
        self._live_phase = ""
        self._live_tool = None
        self._live_command = ""
        self._live_duration_ms = None
        self._live_turn = None
        self._live_calls = 0
        self._live_bytes = 0
        self._live_age_s = 0.0
        self._live_started_at = event_clock
        self._live_provider_call_open = False
        self._live_cache_hit = None
        self._live_final = False

    @staticmethod
    def _event_tool(data: Mapping[str, Any]) -> str | None:
        # ``tool=None`` is a deliberate wire signal (a heartbeat that reports
        # the next provider phase, not a completed tool) and must clear the
        # previous tool instead of being ignored like an absent key.
        if "tool" not in data and "tool_name" not in data:
            return None
        value = data.get("tool")
        if value is None:
            value = data.get("tool_name")
        if value is None:
            return ""
        return _single_line(value) if isinstance(value, str) and value.strip() else ""

    @staticmethod
    def _event_tool_id(data: Mapping[str, Any]) -> str | None:
        """Return the wire identity for one tool stream when it is present."""
        for key in ("tool_call_id", "tool_id", "call_id", "request_id", "id"):
            value = data.get(key)
            if isinstance(value, str) and value:
                return _single_line(value)
        return None

    def _observe_live_event(
        self,
        record: Mapping[str, Any],
        kind: str,
        data: Mapping[str, Any],
    ) -> None:
        if kind not in _LIVE_EVENT_KINDS and _stream_update(record) is None:
            return
        event_clock = self._event_clock(record)
        task_id = _task_id(record, data)
        turn = _event_turn(data)
        if task_id is not None or self._live_task_id is None:
            self._start_live_operation(task_id, event_clock)
        if self._live_final and kind != "result":
            return
        incoming_tool = self._event_tool(data)
        if (turn is not None and self._live_turn is not None and turn != self._live_turn) or (
            kind == "tool_output_delta" and self._live_kind == "tool_event"
        ):
            self._start_live_operation(task_id, event_clock, force=True)
        elif (
            kind in _FAILURE_EVENT_KINDS | {"error", "fatal_error", "timeout"}
            and self._live_tool is not None
        ):
            self._start_live_operation(task_id, event_clock, force=True)
        elif (
            kind not in {"heartbeat", "tool_event", "tool_output_delta"}
            and self._live_tool is not None
        ):
            self._start_live_operation(task_id, event_clock, force=True)
        if incoming_tool and incoming_tool != self._live_tool:
            self._start_live_operation(task_id, event_clock, force=True)
        elif (
            kind == "heartbeat"
            and not incoming_tool
            and (
                self._live_tool is not None
                or (
                    self._live_kind == "heartbeat"
                    and _single_line(data.get("phase")) != self._live_phase
                )
                or self._live_kind not in {"", "heartbeat"}
            )
        ):
            # A heartbeat with no tool is the next provider phase. Clear the
            # completed operation's tail/duration instead of displaying it as live.
            self._start_live_operation(task_id, event_clock, force=True)
        if self._live_started_at is None:
            self._live_started_at = event_clock
        if event_clock is not None and self._live_started_at is not None:
            self._live_age_s = max(0.0, event_clock - self._live_started_at)

        if turn is not None:
            self._live_turn = turn
        self._live_kind = kind
        self._live_phase = _single_line(data.get("phase"))
        status = data.get("status")
        if isinstance(status, str) and status:
            self._live_status = _single_line(status)
        elif kind == "tool_event":
            tool_status = _tool_status(data)
            self._live_status = (
                "ok" if tool_status is True else "failed" if tool_status is False else "done"
            )
        elif kind == "heartbeat":
            heartbeat_status = data.get("status")
            if isinstance(heartbeat_status, str) and heartbeat_status:
                self._live_status = _single_line(heartbeat_status)

        if incoming_tool:
            self._live_tool = incoming_tool
        elif incoming_tool is not None:
            # Explicit ``tool: null`` on the wire clears the completed tool
            # without resetting the rest of the live operation; an event with
            # no tool key at all carries no tool information and leaves the
            # current tool display alone.
            self._live_tool = None
        command = data.get("cmd") or data.get("command")
        if isinstance(command, str) and command.strip():
            self._live_command = _single_line(command)
        duration = _duration_ms(data.get("duration_ms"))
        if duration is not None:
            self._live_duration_ms = duration

        if kind == "heartbeat":
            phase = _single_line(data.get("phase")).casefold().replace("_", "-")
            if phase == "waiting" and not incoming_tool and not self._live_provider_call_open:
                self._live_calls += 1
                self._live_provider_call_open = True
        elif kind == "usage_event":
            if not self._live_provider_call_open:
                self._live_calls += 1
            self._live_provider_call_open = False
            cache_hit = data.get("provider_cache_hit")
            if type(cache_hit) is bool:
                self._live_cache_hit = cache_hit

        text = self._live_event_text(kind, data)
        if kind in _ASSISTANT_STREAM_KINDS or kind in _TOOL_STREAM_KINDS:
            update = _stream_update(record)
            if update is not None:
                self._live_role = update[0]
                text = self._stream_text or text
        elif kind == "tool_event":
            self._live_role = "tool"
        elif kind in _FAILURE_EVENT_KINDS or kind in {"error", "fatal_error", "timeout"}:
            self._live_role = "error"
        elif kind == "result":
            self._live_role = "assistant"

        clean_text = _single_line(text)
        if clean_text:
            self._live_text = clean_text[-_LIVE_TEXT_LIMIT:]
            self._live_bytes = _utf8_size(self._live_text)
        if kind == "usage_event":
            active_bytes = data.get("active_context_bytes")
            if type(active_bytes) is int and active_bytes >= 0:
                self._live_bytes = max(self._live_bytes, active_bytes)
        self._live_final = self._live_final or (
            kind == "result" and data.get("status") != "suspended"
        )
        self._live_revision += 1

    def _set_live_result(self, text: str | None) -> None:
        clean = _single_line(text) if isinstance(text, str) else ""
        self._live_tool = None
        self._live_command = ""
        self._live_duration_ms = None
        if clean:
            self._live_text = clean[-_LIVE_TEXT_LIMIT:]
            self._live_bytes = _utf8_size(self._live_text)
        self._live_kind = "result"
        self._live_role = "assistant"
        if _failure_summary(clean or "") is not None:
            self._live_status = "failed"
        elif self._live_status.casefold() not in {"succeeded", "failed", "cancelled", "error"}:
            self._live_status = "succeeded"
        self._live_final = True
        self._live_revision += 1

    def _set_live_status(self, status: str) -> None:
        """Close the fixed live window when the activity state is terminal."""
        if self._live_final and self._live_status == status:
            return
        self._live_kind = self._live_kind or "result"
        self._live_tool = None
        self._live_command = ""
        self._live_duration_ms = None
        self._live_text = ""
        self._live_bytes = 0
        self._live_status = status
        self._live_role = "error" if status == "error" else "assistant"
        self._live_final = True
        self._live_revision += 1

    def _clear_stream(self) -> None:
        self._stream_role = None
        self._stream_text = ""
        self._stream_message_id = None
        self._stream_truncated = False
        self._stream_tool_key = None

    def _commit_stream(self) -> None:
        if self._stream_role is not None and self._stream_text:
            self.add(self._stream_role, self._stream_text)
        self._clear_stream()

    def _bounded_stream_text(self, text: str) -> str:
        if len(text) <= _STREAM_TEXT_LIMIT:
            self._stream_truncated = False
            return text
        self._stream_truncated = True
        return "…\n" + text[-(_STREAM_TEXT_LIMIT - 2) :]

    def _update_stream(
        self,
        role: str,
        text: str,
        *,
        append: bool,
        message_id: str | None,
        tool_key: str | None = None,
    ) -> None:
        if self._stream_role != role or (
            message_id is not None
            and self._stream_message_id is not None
            and message_id != self._stream_message_id
        ):
            self._commit_stream()
        elif tool_key is not None and tool_key != self._stream_tool_key:
            # Tool output streams are per-tool: a second tool's first delta
            # commits the previous tool's tail so history can never hold a
            # cross-tool mixture ("OLD-ANEW-B").
            self._commit_stream()
        self._stream_role = role
        self._stream_message_id = message_id
        self._stream_tool_key = tool_key

        current = self._stream_text
        if not current:
            merged = text
        elif text == current or (append and current.endswith(text)):
            merged = current
        elif text.startswith(current):
            # Some transports call a cumulative snapshot a delta.  Prefer the
            # longer snapshot so a redraw never duplicates the existing tail.
            merged = text
        elif not append:
            merged = text
        else:
            merged = current + text
        self._stream_text = self._bounded_stream_text(_sanitize(merged))

    def finish_stream(self, final_text: str | None = None) -> None:
        """Commit the active stream and optionally replace it with final text."""
        current = self._stream_text
        role = self._stream_role
        truncated = self._stream_truncated
        self._clear_stream()
        final = _sanitize(final_text).strip("\n") if isinstance(final_text, str) else ""
        if final:
            self._set_live_result(final)
        summary_failure = _failure_summary(final) if final else None
        if summary_failure is not None:
            task_id, cause = summary_failure
            self._record_failure(task_id, None, cause)
            # The detailed task/cause/context is already in the red failure
            # block. Never render the worker's failure summary as model text.
            return
        if not final:
            if current and role is not None:
                self.add(role, current)
            return
        if current and role == "assistant" and not truncated:
            if final.startswith(current) or current.startswith(final):
                final = final if len(final) >= len(current) else current
            elif current != final:
                final = f"{current}\n{final}"
        elif current and role is not None:
            self.add(role, current)
        self.assistant(final)

    def _failure_key(self, task_id: str | None, turn: int | None) -> tuple[str, int | None, int]:
        return task_id or "?", turn, self._turn_serial

    def _remember_turn(self, task_id: str | None, turn: int | None) -> None:
        if task_id is not None and turn is not None:
            self._turn_by_task[task_id] = turn

    def _context_key(self, task_id: str | None, turn: int | None) -> tuple[str, int | None, int]:
        if turn is None and task_id is not None:
            turn = self._turn_by_task.get(task_id)
        return self._failure_key(task_id, turn)

    def _failure_block_for(
        self, task_id: str | None, turn: int | None
    ) -> tuple[tuple[str, int | None, int], _FailureBlock] | None:
        key = self._context_key(task_id, turn)
        block = self._failure_blocks.get(key)
        if block is not None:
            return key, block
        wanted = task_id or "?"
        for candidate_key in reversed(self._failure_order):
            candidate = self._failure_blocks.get(candidate_key)
            if (
                candidate is not None
                and candidate_key[2] == self._turn_serial
                and candidate.task_id == wanted
            ):
                return candidate_key, candidate
        return None

    @staticmethod
    def _selected_failure_context(context: list[str]) -> list[str]:
        relevant = [
            line for line in context if "failed" in line.lower() or "timeout" in line.lower()
        ]
        selected = relevant if relevant else context
        return selected[-3:]

    def _failure_text(self, block: _FailureBlock) -> str:
        lines = [
            "turn failed",
            f"task_id={block.task_id}",
            f"cause={block.cause or 'unknown failure'}",
        ]
        lines.extend(
            f"{_FAILURE_CONTEXT_PREFIX}{line}"
            for line in self._selected_failure_context(block.context)
        )
        return "\n".join(lines)

    def _remember_tool_failure(
        self,
        task_id: str | None,
        turn: int | None,
        tool: Any,
    ) -> None:
        del task_id, turn
        self._tool_error_total += 1
        key = self._turn_serial
        if self._tool_failure_key != key or self._tool_failure_entry not in self._entries:
            self._tool_failure_key = key
            self._tool_failure_count = 0
            self._tool_failure_entry = TranscriptEntry(role="tool", text="")
            self._entries.append(self._tool_failure_entry)

        self._tool_failure_count += 1
        name = _sanitize(tool).strip() if isinstance(tool, str) else "tool"
        previous_entry = self._tool_failure_entry
        self._tool_failure_entry = TranscriptEntry(
            role="tool",
            text=f"{_TOOL_ERROR_PREFIX} {self._tool_failure_count} (last: {name} …)",
        )
        for index, current in enumerate(self._entries):
            if current is previous_entry:
                self._entries[index] = self._tool_failure_entry
                return

    def _remember_tool_activity(self, tool: Any, duration: int | float | None) -> None:
        self._tool_count += 1
        self._turn_tool_count += 1
        self._last_tool_name = (
            _sanitize(tool).strip() if isinstance(tool, str) and tool.strip() else "tool"
        )
        self._last_tool_duration_ms = duration

    def _clear_tool_failure_notice(self, task_id: str | None, turn: int | None) -> None:
        del task_id, turn
        if self._tool_failure_entry is None:
            return
        self._tool_failure_key = None
        self._tool_failure_count = 0
        self._tool_failure_entry = None

    @staticmethod
    def _prefer_failure_cause(current: str | None, candidate: str | None) -> bool:
        if not candidate or not current or candidate == current:
            return bool(candidate) and not current
        if current.startswith("worker reported "):
            return True
        return candidate.startswith("max_restarts (") and not current.startswith("max_restarts (")

    def _refresh_failure_entry(self, block: _FailureBlock) -> None:
        entry = TranscriptEntry(role="error", text=self._failure_text(block))
        if block.entry is not None:
            for index, current in enumerate(self._entries):
                if current is block.entry:
                    self._entries[index] = entry
                    block.entry = entry
                    return
        self._entries.append(entry)
        block.entry = entry

    def _record_failure(
        self,
        task_id: str | None,
        turn: int | None,
        cause: str | None,
    ) -> None:
        self._clear_tool_failure_notice(task_id, turn)
        found = self._failure_block_for(task_id, turn)
        if found is None:
            key = self._context_key(task_id, turn)
            block = _FailureBlock(
                task_id=key[0],
                turn=key[1],
                cause=cause,
                context=list(self._failure_context.get(key, ())),
            )
            self._failure_blocks[key] = block
            self._failure_order.append(key)
            while len(self._failure_order) > _FAILURE_BLOCK_LIMIT:
                expired = self._failure_order.popleft()
                self._failure_blocks.pop(expired, None)
                self._failure_context.pop(expired, None)
        else:
            key, block = found
            if self._prefer_failure_cause(block.cause, cause):
                block.cause = cause
            block.context = list(self._failure_context.get(key, block.context))
        self._refresh_failure_entry(block)

    def _remember_failure_context(self, task_id: str | None, turn: int | None, line: str) -> None:
        key = self._context_key(task_id, turn)
        context = self._failure_context.setdefault(key, [])
        if line not in context:
            context.append(line)
        if len(context) > 12:
            del context[:-12]
        found = self._failure_blocks.get(key)
        if found is not None:
            found.context = list(context)
            self._refresh_failure_entry(found)

    def observe_event(self, record: dict[str, Any]) -> None:
        """Promote only operator-relevant runtime events into the transcript."""
        kind = record.get("kind")
        if not isinstance(kind, str):
            return
        data = _event_data(record)
        task_id = _task_id(record, data)
        turn = _event_turn(data)
        self._remember_turn(task_id, turn)

        if kind == "tool_event" and self._stream_role == "tool":
            owner = task_id or "?"
            tool_name = self._event_tool(data) or ""
            tool_id = self._event_tool_id(data) or ""
            active_tool_key = f"{owner}:run:{tool_name}:{tool_id}"
            if self._stream_tool_key == active_tool_key:
                self._commit_stream()

        update = _stream_update(record)
        if update is not None:
            role, text, append, message_id = update
            tool_key: str | None = None
            if role == "tool":
                # One stream identity per task/tool operation: concurrent
                # children running the same tool must never share one tail.
                owner = task_id or "?"
                tool_name = self._event_tool(data) or ""
                tool_id = self._event_tool_id(data) or ""
                if kind == "tool_event":
                    tool_key = f"{owner}:end:{tool_name}:{tool_id}"
                else:
                    tool_key = f"{owner}:run:{tool_name}:{tool_id}"
            self._update_stream(
                role,
                text,
                append=append,
                message_id=message_id,
                tool_key=tool_key,
            )
        self._observe_live_event(record, kind, data)

        if kind == "tool_event":
            tool = data.get("tool")
            ok = _tool_status(data)
            duration = _duration_ms(data.get("duration_ms"))
            self._remember_tool_activity(tool, duration)
            if ok is not None and not ok:
                context_line = _failure_context_line(kind, data)
                if context_line is not None:
                    self._remember_failure_context(task_id, turn, context_line)
                self._remember_tool_failure(task_id, turn, tool)
                return
            if isinstance(tool, str):
                text = _sanitize(_tool_entry_text(data, tool, ok, duration)).strip("\n")
                if text:
                    self._entries.append(
                        TranscriptEntry(
                            role="tool",
                            text=text,
                            tool_name=tool,
                            tool_ok=ok,
                            duration_ms=duration,
                        )
                    )
            return

        if kind in {"child_admitted", "child_rejected"}:
            child = data.get("child_task_id") or data.get("task_id") or "child"
            reason = data.get("reason")
            message = f"{kind.replace('_', ' ')}: {child}"
            if isinstance(reason, str) and reason:
                message += f" · {reason}"
            detail = _activity_tail(data.get("message"))
            if detail:
                message += f" · {detail}"
            self.system(message)
            return

        if kind in {"context_epoch_advanced", "context_checkpoint"}:
            epoch = data.get("epoch")
            segments = data.get("summary_segments")
            message = f"context checkpoint · epoch={epoch if isinstance(epoch, int) else '?'}"
            if isinstance(segments, int):
                message += f" · summaries={segments}"
            self.system(message)
            return

        context_line = _failure_context_line(kind, data)
        if context_line is not None:
            self._remember_failure_context(task_id, turn, context_line)

        failed_result = kind == "result" and data.get("status") in _FAILURE_STATUSES
        if kind in _FAILURE_EVENT_KINDS or failed_result:
            self._record_failure(task_id, turn, _failure_cause(kind, data))
            return

        if kind in {"merge_committed", "merge_published"}:
            sha = data.get("merge_sha") or data.get("commit") or data.get("sha")
            self.system(f"repository integrated{f' · {str(sha)[:12]}' if sha else ''}")


class ActivityState:
    """Small mutable view of the work currently keeping a turn busy."""

    def __init__(self) -> None:
        self._active = False
        self._finished = False
        self._state = "IDLE"
        self._turn_started_at = 0.0
        self._frame = 0
        self._responding = False
        self._stream_tokens = 0
        self._stream_rate = 0.0
        self._cooldown: tuple[str | None, float | None] | None = None
        self._provider: str | None = None
        self._model: str | None = None
        self._cache_hit: bool | None = None
        self._next_tool_id = 0
        self._tools: dict[str, tuple[str, float]] = {}
        self._heartbeat_tool: tuple[str, float] | None = None
        self._heartbeat_phase: str | None = None
        self._heartbeat_tail = ""
        self._last_progress_at = 0.0
        self._progress_signature: tuple[Any, ...] | None = None

    @property
    def active(self) -> bool:
        return self._active

    @property
    def state(self) -> str:
        """Return the explicit operator-facing turn state."""
        return self._state

    def start(self, *, now: float | None = None) -> None:
        """Start a fresh turn clock and reset any previous in-flight work."""
        self._active = True
        self._finished = False
        self._state = "WAITING"
        self._turn_started_at = time.monotonic() if now is None else now
        self._frame = 0
        self._responding = False
        self._stream_tokens = 0
        self._stream_rate = 0.0
        self._cooldown = None
        self._provider = None
        self._model = None
        self._cache_hit = None
        self._next_tool_id = 0
        self._tools.clear()
        self._heartbeat_tool = None
        self._heartbeat_phase = None
        self._heartbeat_tail = ""
        self._last_progress_at = self._turn_started_at
        self._progress_signature = None

    def stop(self) -> None:
        """Stop the line without leaving stale tool state for a later turn."""
        self._active = False
        self._finished = True
        if self._state not in {"DONE", "ERROR"}:
            self._state = "IDLE"
        self._responding = False
        self._tools.clear()
        self._heartbeat_tool = None

    def complete(self, *, succeeded: bool = True) -> None:
        """Record a terminal state before the final frame is drawn."""
        self._state = "DONE" if succeeded else "ERROR"
        self._active = False
        self._finished = True
        self._responding = False
        self._tools.clear()
        self._heartbeat_tool = None

    def cancel(self) -> None:
        """Record a cancelled turn as idle rather than as a provider error."""
        self._state = "IDLE"
        self._active = False
        self._finished = True
        self._responding = False
        self._tools.clear()
        self._heartbeat_tool = None

    def status_line(self) -> str:
        """Return a final status label suitable for the bottom status pane."""
        if self._state == "DONE":
            return "✓ DONE"
        if self._state == "ERROR":
            return "✗ ERROR"
        if self._state == "IDLE":
            return "IDLE"
        return self.render()

    @staticmethod
    def _tool_name(data: Mapping[str, Any]) -> str:
        for key in ("tool", "tool_name", "name"):
            value = data.get(key)
            if isinstance(value, str) and value:
                return value
        function = data.get("function")
        if isinstance(function, Mapping):
            value = function.get("name")
            if isinstance(value, str) and value:
                return value
        return "tool"

    @staticmethod
    def _tool_id(data: Mapping[str, Any]) -> str | None:
        for key in ("tool_call_id", "tool_id", "call_id", "request_id", "id"):
            value = data.get(key)
            if isinstance(value, str) and value:
                return value
        return None

    @staticmethod
    def _phase(data: Mapping[str, Any]) -> str | None:
        for key in ("phase", "state", "status", "event"):
            value = data.get(key)
            if isinstance(value, str) and value:
                return value.lower().replace("_", "-")
        return None

    def _is_tool_start(self, kind: str, data: Mapping[str, Any]) -> bool:
        phase = self._phase(data)
        if kind in _TOOL_START_KINDS:
            return True
        return phase in _TOOL_PHASE_STARTS and kind.startswith("tool")

    def _is_tool_end(self, kind: str, data: Mapping[str, Any]) -> bool:
        phase = self._phase(data)
        if kind == "tool_event":
            return (
                isinstance(data.get("ok"), bool)
                or isinstance(data.get("duration_ms"), int | float)
                or phase in _TOOL_PHASE_ENDS
            )
        if kind in _TOOL_END_KINDS:
            return phase not in _TOOL_PHASE_STARTS
        return phase in _TOOL_PHASE_ENDS and kind.startswith("tool")

    def _mark_progress(self, signature: tuple[Any, ...], now: float) -> None:
        if signature != self._progress_signature:
            self._progress_signature = signature
            self._last_progress_at = now

    def _start_tool(self, data: Mapping[str, Any], now: float) -> None:
        tool_id = self._tool_id(data)
        if tool_id is None:
            self._next_tool_id += 1
            key = f"anonymous:{self._next_tool_id}"
        else:
            key = f"id:{tool_id}"
        tool_name = self._tool_name(data)
        self._tools[key] = (tool_name, now)
        self._responding = False
        self._mark_progress(("tool", tool_name), now)

    def _finish_tool(self, data: Mapping[str, Any], now: float) -> None:
        tool_id = self._tool_id(data)
        if tool_id is not None:
            key = f"id:{tool_id}"
            if key in self._tools:
                del self._tools[key]
                self._heartbeat_tool = None
                self._responding = False
                if not self._tools:
                    self._state = "WAITING"
                self._mark_progress(("tool-finished", tool_id), now)
                return
            return

        tool_name = data.get("tool") or data.get("tool_name") or data.get("name")
        if not isinstance(tool_name, str):
            return
        for key in reversed(self._tools):
            if self._tools[key][0] == tool_name:
                del self._tools[key]
                self._heartbeat_tool = None
                self._responding = False
                if not self._tools:
                    self._state = "WAITING"
                self._mark_progress(("tool-finished", tool_name), now)
                return

    @staticmethod
    def _number(data: Mapping[str, Any], *keys: str) -> float | None:
        for key in keys:
            value = data.get(key)
            if isinstance(value, bool) or not isinstance(value, int | float):
                continue
            try:
                number = float(value)
            except (OverflowError, ValueError):
                continue
            if math.isfinite(number) and number >= 0:
                return number
        usage = data.get("usage")
        if isinstance(usage, Mapping):
            for key in keys:
                value = usage.get(key)
                if isinstance(value, bool) or not isinstance(value, int | float):
                    continue
                try:
                    number = float(value)
                except (OverflowError, ValueError):
                    continue
                if math.isfinite(number) and number >= 0:
                    return number
        return None

    def _observe_provider(self, data: Mapping[str, Any]) -> None:
        provider = data.get("provider")
        model = data.get("model")
        if isinstance(provider, str) and provider:
            self._provider = provider
        if isinstance(model, str) and model:
            self._model = model
        cache_hit = data.get("provider_cache_hit")
        if type(cache_hit) is bool:
            self._cache_hit = cache_hit

    def _observe_cooldown(self, data: Mapping[str, Any]) -> None:
        status = data.get("request_rate_status")
        if not isinstance(status, str):
            return
        normalized = status.casefold().replace(" ", "_")
        if normalized in _COOLDOWN_STATUSES:
            retry_after = self._number(data, "retry_after_s")
            provider = data.get("provider") or data.get("assigned_provider")
            self._cooldown = (
                provider if isinstance(provider, str) else None,
                retry_after,
            )
        elif normalized not in _COOLDOWN_STATUSES:
            self._cooldown = None

    def _observe_stream_rate(
        self,
        data: Mapping[str, Any],
        text: str,
        event_now: float,
    ) -> None:
        direct_rate = self._number(
            data,
            "output_tokens_per_s",
            "tokens_per_s",
            "out_per_s",
        )
        if direct_rate is not None:
            self._stream_rate = direct_rate
        else:
            tokens = self._number(data, "output_tokens", "completion_tokens")
            self._stream_tokens += int(tokens if tokens is not None else max(1, len(text) // 4))
            elapsed = max(0.001, event_now - self._turn_started_at)
            self._stream_rate = self._stream_tokens / elapsed

    def _observe_heartbeat(self, data: Mapping[str, Any], event_now: float) -> bool:
        phase = data.get("phase")
        tool = data.get("tool")
        self._observe_provider(data)
        if phase == "waiting" and type(data.get("provider_cache_hit")) is not bool:
            # A new provider attempt has no cache result yet. Do not carry the
            # previous request's HIT/MISS into this one.
            self._cache_hit = None
        if isinstance(tool, str) and tool.strip():
            tool_name = _sanitize(tool).strip() or "tool"
            if self._heartbeat_tool is None or self._heartbeat_tool[0] != tool_name:
                self._heartbeat_tool = (tool_name, event_now)
        else:
            self._heartbeat_tool = None
        if not isinstance(phase, str):
            self._heartbeat_phase = None
            self._heartbeat_tail = ""
            self._mark_progress(("heartbeat", None, self._heartbeat_tool), event_now)
            self._state = "RUNNING" if self._heartbeat_tool is not None else "WAITING"
            self._responding = False
            return True
        phase = phase.casefold().replace("_", "-")
        if phase not in _ACTIVITY_PHASE_GLYPHS:
            self._heartbeat_phase = None
            self._heartbeat_tail = ""
            self._mark_progress(("heartbeat", None, self._heartbeat_tool), event_now)
            self._state = "RUNNING" if self._heartbeat_tool is not None else "WAITING"
            self._responding = False
            return True
        self._heartbeat_phase = phase
        self._heartbeat_tail = _activity_tail(data.get("tail"))
        revision = data.get("phase_revision")
        revision = revision if type(revision) is int else None
        self._mark_progress(
            ("heartbeat", phase, revision, self._heartbeat_tool, self._heartbeat_tail), event_now
        )
        self._state = "STREAMING" if phase == "streaming" else "WAITING"
        if self._heartbeat_tool is not None:
            self._state = "RUNNING"
        self._responding = phase == "streaming"
        return True

    def observe_event(self, record: Mapping[str, Any], *, now: float | None = None) -> None:
        """Fold one synthetic or durable event into the activity view."""
        if not self._active:
            if self._finished:
                return
            self.start(now=now)
        kind = record.get("kind")
        if not isinstance(kind, str):
            return
        data = _event_data(record)
        event_now = time.monotonic() if now is None else now
        self._observe_provider(data)
        self._observe_cooldown(data)

        if (
            kind in _TURN_ERROR_KINDS
            or kind in _FAILURE_EVENT_KINDS
            or data.get("status") in _FAILURE_STATUSES
        ):
            self._state = "ERROR"
            self._active = False
            self._finished = True
            self._responding = False
            self._tools.clear()
            return
        if kind in _TURN_DONE_KINDS:
            status = data.get("status")
            if status == "suspended":
                self._state = "SUSPENDED"
                self._active = True
                self._finished = False
                self._responding = False
                self._cooldown = None
                self._tools.clear()
                return
            self._state = "ERROR" if status in _FAILURE_STATUSES else "DONE"
            self._active = False
            self._finished = True
            self._responding = False
            self._tools.clear()
            return

        if kind == "heartbeat" and self._observe_heartbeat(data, event_now):
            return

        if self._is_tool_start(kind, data):
            self._start_tool(data, event_now)
            return
        if self._is_tool_end(kind, data):
            self._finish_tool(data, event_now)
            return

        update = _stream_update(record)
        if update is not None and update[0] == "assistant" and update[1]:
            self._responding = True
            self._state = "STREAMING"
            self._mark_progress(("assistant", update[1][-80:]), event_now)
            self._observe_stream_rate(data, update[1], event_now)
        elif update is not None and update[0] == "tool" and update[1]:
            tool_name = self._tool_name(data)
            if not any(name == tool_name for name, _started in self._tools.values()):
                self._start_tool(data, event_now)
            self._state = "RUNNING"
            self._responding = False
            self._mark_progress(("tool-output", tool_name, update[1][-80:]), event_now)
        elif kind in _FIRST_TOKEN_KINDS:
            self._state = "STREAMING"

    def render(self, *, now: float | None = None, advance: bool = False) -> str:
        """Render the concrete resource currently blocking or producing progress."""
        if not self._active:
            return ""
        if advance:
            self._frame = (self._frame + 1) % len(_SPINNER_FRAMES)
        current = time.monotonic() if now is None else now
        turn_elapsed = max(0.0, current - self._turn_started_at)
        quiet_for = max(0.0, current - self._last_progress_at)
        spinner = _SPINNER_FRAMES[self._frame]

        tool = self._heartbeat_tool or next(
            (self._tools[key] for key in reversed(self._tools)),
            None,
        )
        if tool is not None:
            tool_name, tool_started_at = tool
            tool_elapsed = _fmt_secs(max(0.0, current - tool_started_at))
            label = f"TOOL · {_sanitize(tool_name)} {tool_elapsed}"
            if quiet_for >= _STALL_AFTER_S:
                label += f" · no output {_fmt_secs(quiet_for)}"
            return f"{spinner} {label} · turn {_fmt_secs(turn_elapsed)}"

        if self._cooldown is not None:
            provider, retry_after = self._cooldown
            owner = f" · {provider}" if provider else ""
            delay = f" · retry {_fmt_secs(retry_after)}" if retry_after is not None else ""
            return f"{spinner} COOLDOWN{owner}{delay} · turn {_fmt_secs(turn_elapsed)}"

        provider = "/".join(part for part in (self._provider, self._model) if part)
        cache = (
            " · cache HIT"
            if self._cache_hit is True
            else " · cache MISS"
            if self._cache_hit is False
            else ""
        )
        owner = f" · {provider}" if provider else ""

        if self._heartbeat_phase == "waiting":
            detail = self._heartbeat_tail
            if detail == "selecting provider":
                label = f"ROUTING · {_fmt_secs(turn_elapsed)}"
            else:
                label = f"PROVIDER{owner} · waiting {_fmt_secs(turn_elapsed)}"
                if detail and provider and detail.startswith(provider + " · "):
                    label += " · " + detail[len(provider) + 3 :]
            if quiet_for >= _STALL_AFTER_S:
                label += f" · silent {_fmt_secs(quiet_for)}"
            return f"{_ACTIVITY_PHASE_GLYPHS['waiting']} {label}{cache}"

        if self._heartbeat_phase == "thinking":
            label = f"THINKING{owner} · {_fmt_secs(turn_elapsed)}"
            if quiet_for >= _STALL_AFTER_S:
                label += f" · stalled {_fmt_secs(quiet_for)}"
            return f"{_ACTIVITY_PHASE_GLYPHS['thinking']} {label}{cache}"

        if self._heartbeat_phase == "streaming" or self._state == "STREAMING" or self._responding:
            elapsed = max(0.001, turn_elapsed)
            rate = self._stream_rate or self._stream_tokens / elapsed
            label = f"STREAMING{owner} · {_fmt_secs(turn_elapsed)}"
            if rate >= 0.1:
                label += f" · {rate:5.1f} tok/s"
            if quiet_for >= _STALL_AFTER_S:
                label += f" · stalled {_fmt_secs(quiet_for)}"
            if self._heartbeat_tail:
                label += f" · {self._heartbeat_tail}"
            return f"{_ACTIVITY_PHASE_GLYPHS['streaming']} {label}{cache}"

        if self._state == "DONE":
            return "✓ DONE"
        if self._state == "ERROR":
            return "✗ ERROR"
        if self._state == "SUSPENDED":
            return f"{spinner} CHILDREN · waiting {_fmt_secs(turn_elapsed)}"
        return f"{spinner} ORCHESTRATING · {_fmt_secs(turn_elapsed)}"

    def tick(self, *, now: float | None = None) -> str:
        """Advance the spinner once and return the resulting row."""
        return self.render(now=now, advance=True)


_MD_FENCE_RE = re.compile(r"^\s*```([^`]*)\s*$")


def _wrap_display_cells(line: str, width: int) -> list[str]:
    """Wrap one line without treating wide code points as one cell."""
    width = max(1, width)
    chunks = re.findall(r"\s+|\S+", _sanitize(line).replace("\t", " "))
    output: list[str] = []
    current = ""
    current_width = 0

    for chunk in chunks:
        chunk_width = _display_width(chunk)
        if chunk.isspace():
            if not current:
                continue
            if current_width + chunk_width <= width:
                current += chunk
                current_width += chunk_width
            else:
                output.append(current.rstrip())
                current = ""
                current_width = 0
            continue

        if current and current_width + chunk_width <= width:
            current += chunk
            current_width += chunk_width
            continue
        if current:
            output.append(current.rstrip())
            current = ""
            current_width = 0

        if chunk_width <= width:
            current = chunk
            current_width = chunk_width
            continue

        pieces: list[str] = []
        remaining = chunk
        while remaining:
            head, tail = _take_display_width(remaining, width)
            if not head:
                head, tail = "?", remaining[1:]
            pieces.append(head)
            remaining = tail
        output.extend(pieces[:-1])
        current = pieces[-1]
        current_width = _display_width(current)

    if current or not output:
        output.append(current.rstrip())
    return output


_MD_TABLE_DELIMITER_RE = re.compile(r"^:?-{3,}:?$")


def _markdown_table_cells(line: str) -> tuple[str, ...] | None:
    stripped = line.strip()
    if stripped.count("|") < 2 or not (stripped.startswith("|") or stripped.endswith("|")):
        return None
    if stripped.startswith("|"):
        stripped = stripped[1:]
    if stripped.endswith("|"):
        stripped = stripped[:-1]
    return tuple(cell.strip() for cell in stripped.split("|"))


def _render_narrow_table_lines(text: str, width: int, color: bool) -> list[str]:
    """Flatten only tables that Rich cannot fit; preserve every cell."""
    output: list[str] = []
    for row_index, line in enumerate(text.splitlines()):
        cells = _markdown_table_cells(line)
        if cells is None:
            continue
        if row_index == 1 and all(
            _MD_TABLE_DELIMITER_RE.fullmatch(cell.replace(" ", "")) for cell in cells
        ):
            continue
        prefix = "▸ " if row_index == 0 else "  "
        for part in _wrap_display_cells(" · ".join(cells), max(1, width - 2)):
            style = _CYAN if row_index == 0 else ""
            output.append(_paint(_clip(prefix + part, width), style, color))
            prefix = "  "
    return output


def _narrow_table_ranges(text: str, width: int) -> list[tuple[int, int]]:
    """Find tables whose natural Rich width would silently clip cells."""
    lines = text.splitlines()
    ranges: list[tuple[int, int]] = []
    in_fence = False
    index = 0
    while index + 1 < len(lines):
        if _MD_FENCE_RE.match(lines[index]) is not None:
            in_fence = not in_fence
            index += 1
            continue
        if in_fence:
            index += 1
            continue
        header = _markdown_table_cells(lines[index])
        delimiter = _markdown_table_cells(lines[index + 1])
        if (
            header is None
            or delimiter is None
            or len(header) != len(delimiter)
            or not all(
                _MD_TABLE_DELIMITER_RE.fullmatch(cell.replace(" ", "")) for cell in delimiter
            )
        ):
            index += 1
            continue

        end = index + 2
        rows = [header, delimiter]
        while end < len(lines):
            row = _markdown_table_cells(lines[end])
            if row is None:
                break
            rows.append(row)
            end += 1
        column_widths = [0] * len(header)
        for row in rows:
            for column, cell in enumerate(row):
                if column < len(column_widths):
                    column_widths[column] = max(column_widths[column], _display_width(cell))
        # ponytail: conservative width estimate; use Rich's measure API if its
        # table layout changes and this starts falling back too early.
        minimum_width = sum(column_width + 2 for column_width in column_widths)
        if width < minimum_width:
            ranges.append((index, end))
        index = end
    return ranges


def _render_markdown_lines_rich_document(text: str, width: int, color: bool | int) -> list[str]:
    """Use the shared Rich renderer at the caller's actual terminal color depth."""
    color_depth = color if type(color) is int else (16 if color else 0)
    return _shared_markdown_lines(text, width=width, color_depth=color_depth)


def _render_markdown_lines_rich(text: str, width: int, color: bool | int) -> list[str]:
    """Render sanitized Markdown through Rich, falling back for narrow tables."""
    table_ranges = _narrow_table_ranges(text, width)
    if not table_ranges:
        return _render_markdown_lines_rich_document(text, width, color)

    lines = text.splitlines()
    rendered: list[str] = []
    cursor = 0
    for start, end in table_ranges:
        prefix = "\n".join(lines[cursor:start])
        if prefix.strip():
            rendered.extend(_render_markdown_lines_rich_document(prefix, width, color))
        rendered.extend(_render_narrow_table_lines("\n".join(lines[start:end]), width, color))
        cursor = end
    suffix = "\n".join(lines[cursor:])
    if suffix.strip():
        rendered.extend(_render_markdown_lines_rich_document(suffix, width, color))
    while rendered and not _visible(rendered[-1]).strip():
        rendered.pop()
    return rendered


@lru_cache(maxsize=512)
def _render_markdown_lines_cached(text: str, width: int, color: bool | int) -> tuple[str, ...]:
    """Cache immutable per-entry Markdown rows by source, width, and color."""
    return tuple(_render_markdown_lines_rich(text, width, color))


def render_markdown_lines(
    text: str,
    width: int = 80,
    *,
    color: bool | int = True,
) -> list[str]:
    """Render sanitized Markdown through the shared Rich theme and narrow-table adapter."""
    width = max(1, width)
    clean = _sanitize(text)
    if not clean:
        return []
    return [_clip(line, width) for line in _render_markdown_lines_cached(clean, width, color)]


# Keep the private name convenient for presentation tests and old callers.
_render_markdown_lines = render_markdown_lines


def _wrap_markdown(text: str, width: int) -> list[str]:
    """Render Markdown structure as safe plain terminal lines before coloring."""
    width = max(1, width)
    output: list[str] = []
    in_fence = False

    def wrap(line: str, line_width: int) -> list[str]:
        line_width = max(1, line_width)
        clean = _sanitize(line).replace("\t", " ")
        if any(_char_width(char) != 1 for char in clean):
            wrapped = _wrap_display_cells(clean, line_width)
        else:
            wrapped = textwrap.wrap(
                clean,
                width=line_width,
                replace_whitespace=False,
                drop_whitespace=True,
                break_long_words=False,
                break_on_hyphens=False,
            ) or [""]
        output_lines: list[str] = []
        for chunk in wrapped:
            while _display_width(chunk) > line_width:
                head, tail = _take_display_width(chunk, line_width)
                if not head:
                    # The framed renderers never pass a one-column body, but
                    # keep this helper bounded for direct callers too.
                    head, tail = "?", chunk[1:]
                output_lines.append(head)
                chunk = tail
            output_lines.append(chunk)
        return output_lines

    for raw_line in _sanitize(text).splitlines() or [""]:
        stripped = raw_line.strip()
        if stripped.startswith("```"):
            in_fence = not in_fence
            output.extend(wrap(raw_line, width))
            continue
        if in_fence:
            output.extend(wrap("  " + raw_line, width))
            continue
        if stripped.startswith("#"):
            heading = stripped.lstrip("#").strip()
            output.extend(wrap(heading, width))
            continue
        prefix = ""
        body = raw_line
        if stripped.startswith(("- ", "* ")):
            prefix, body = "• ", stripped[2:]
        elif stripped.startswith(">"):
            prefix, body = "│ ", stripped[1:].lstrip()
        prefix_width = _display_width(prefix)
        visible_prefix_width = min(prefix_width, width)
        continuation = " " * visible_prefix_width
        wrapped = wrap(body, max(1, width - visible_prefix_width))
        output.append(_clip(prefix + wrapped[0], width))
        output.extend(_clip(continuation + line, width) for line in wrapped[1:])
    return output


def _bounded_render_lines(lines: list[str], limit: int) -> list[str]:
    """Bound rendered detail without changing the transcript's source text."""
    if len(lines) <= limit:
        return lines
    hidden = len(lines) - max(1, limit - 1)
    return [f"… {hidden} lines hidden", *lines[-max(1, limit - 1) :]]


def _bounded_markdown_lines(
    text: str,
    width: int,
    limit: int,
    *,
    color: bool,
) -> list[str]:
    """Bound source detail and then wrapped rows without changing entry state."""
    limit = max(1, limit)
    source_lines = text.splitlines()
    hidden_source = 0
    if len(source_lines) > limit:
        keep = max(1, limit - 1)
        hidden_source = len(source_lines) - keep
        text = "\n".join(source_lines[-keep:])

    rendered = render_markdown_lines(text, width, color=color)
    if hidden_source or len(rendered) > limit:
        keep = max(1, limit - 1)
        hidden_rendered = max(0, len(rendered) - keep)
        hidden = hidden_source + hidden_rendered
        return [f"… {hidden} lines hidden", *rendered[-keep:]]
    return rendered


def _dense_rendered_lines(lines: list[str]) -> list[str]:
    """Remove markdown's visual spacer rows from the transcript view."""
    return [line for line in lines if _visible(line).strip()]


def _entry_lines(
    entry: TranscriptEntry,
    width: int,
    *,
    detail_limit: int | None = None,
    color: bool = False,
) -> list[tuple[str, str]]:
    width = max(1, width)
    if entry.role == "tool" and entry.text.startswith(_TOOL_ERROR_PREFIX):
        return []
    label_prefix = f"{_ROLE_LABELS[entry.role]} ▸ "
    body_width = max(1, width - _display_width(label_prefix))
    if entry.tool_name is not None:
        summary_lines = _dense_rendered_lines(_wrap_markdown(_tool_line(entry), body_width))
        rendered = [
            (entry.role, _clip(label_prefix + (summary_lines[0] if summary_lines else ""), width))
        ]
        rendered.extend((entry.role, _clip("    " + line, width)) for line in summary_lines[1:])
        summary = f"{entry.tool_name}: "
        detail = entry.text
        if detail.startswith(summary):
            detail = detail.split("\n", 1)[1] if "\n" in detail else ""
        if detail:
            detail_lines = (
                _bounded_markdown_lines(detail, body_width, detail_limit, color=color)
                if detail_limit is not None
                else render_markdown_lines(detail, body_width, color=color)
            )
            rendered.extend(
                (entry.role, _clip("    " + line, width))
                for line in _dense_rendered_lines(detail_lines)
            )
    else:
        long_field = any(
            "=" in word and _display_width(word) > body_width for word in entry.text.split()
        )
        if entry.role == "system" and detail_limit is None and long_field:
            lines = _wrap_markdown(entry.text, body_width)
        else:
            lines = (
                _bounded_markdown_lines(entry.text, body_width, detail_limit, color=color)
                if detail_limit is not None
                else render_markdown_lines(entry.text, body_width, color=color)
            )
        lines = _dense_rendered_lines(lines)
        rendered = [(entry.role, _clip(label_prefix + (lines[0] if lines else ""), width))]
        for line in lines[1:]:
            role = (
                "dim"
                if entry.role == "error" and line.lstrip().startswith(_FAILURE_CONTEXT_PREFIX)
                else entry.role
            )
            rendered.append((role, _clip("    " + line, width)))
    return rendered


def _tool_compact_lines(
    entry: TranscriptEntry,
    width: int,
    *,
    count: int = 1,
    last_duration_ms: int | float | None = None,
) -> list[tuple[str, str]]:
    width = max(1, width)
    line = _tool_line(entry, count=count, last_duration_ms=last_duration_ms)
    return [("dim", _clip("  " + line, width))]


def _transcript_blocks(
    entries: tuple[TranscriptEntry, ...],
    width: int,
    *,
    expanded: bool = False,
    color: bool = False,
) -> list[list[tuple[str, str]]]:
    blocks: list[list[tuple[str, str]]] = []
    index = 0
    while index < len(entries):
        entry = entries[index]
        if entry.role != "tool" or entry.tool_name is None:
            if entry.role == "tool" and entry.text.startswith(_TOOL_ERROR_PREFIX):
                index += 1
                continue
            detail_limit = _TOOL_DETAIL_RENDER_LIMIT if entry.role == "error" else None
            blocks.append(_entry_lines(entry, width, detail_limit=detail_limit, color=color))
            index += 1
            continue

        if expanded:
            blocks.append(
                _entry_lines(
                    entry,
                    width,
                    detail_limit=_TOOL_DETAIL_RENDER_LIMIT,
                    color=color,
                )
            )
            index += 1
            continue

        if not entry.tool_ok:
            blocks.append(_entry_lines(entry, width, color=color))
            index += 1
            continue

        end = index + 1
        while (
            end < len(entries)
            and entries[end].role == "tool"
            and entries[end].tool_name == entry.tool_name
            and entries[end].tool_ok
        ):
            end += 1
        last = entries[end - 1]
        blocks.append(
            _tool_compact_lines(
                entry,
                width,
                count=end - index,
                last_duration_ms=last.duration_ms,
            )
        )
        index = end
    return blocks


def _transcript_block_kind(block: list[tuple[str, str]]) -> str:
    if not block:
        return ""
    role, text = block[0]
    if role in {"tool", "dim"} or text.lstrip().startswith(("✓ ", "✗ ", "• ", _TOOL_ERROR_PREFIX)):
        return "tool"
    return role


def _stream_lines(
    transcript: Transcript,
    width: int,
    capacity: int,
    *,
    color: bool = False,
) -> list[tuple[str, str]]:
    if not transcript.streaming_text or transcript.streaming_role is None:
        return []
    width = max(1, width)
    text = transcript.streaming_text
    if len(text) > _STREAM_RENDER_LIMIT:
        text = "…\n" + text[-_STREAM_RENDER_LIMIT:]
    label_prefix = f"{_ROLE_LABELS[transcript.streaming_role]} ▸ "
    body_width = max(1, width - _display_width(label_prefix))
    lines = _dense_rendered_lines(render_markdown_lines(text, body_width, color=color))
    rendered = [
        (
            transcript.streaming_role,
            _clip(label_prefix + (lines[0] if lines else "generating…"), width),
        )
    ]
    rendered.extend((transcript.streaming_role, _clip("    " + line, width)) for line in lines[1:])
    return rendered[-max(1, capacity) :]


def _activity_parts(activity_line: str) -> tuple[str, str, str] | None:
    clean = _single_line(activity_line)
    if not clean:
        return None
    phase_match = _ACTIVITY_PHASE_RE.match(clean)
    if phase_match is not None:
        return (
            clean[0],
            phase_match.group(1).casefold(),
            _fmt_secs(_usage_float(phase_match.group(2))),
        )
    spinner = next((frame for frame in _SPINNER_FRAMES if clean.startswith(frame)), "⠋")
    upper = clean.upper()
    if "RUNNING" in upper:
        phase = "running"
    elif "STREAMING" in upper or "RESPONDING" in upper:
        phase = "streaming"
    elif "WAITING" in upper:
        phase = "waiting"
    elif "THINKING" in upper:
        phase = "thinking"
    else:
        return None
    match = re.search(
        r"\b(?:running\s+\S+\s+|(?:thinking|streaming|waiting|turn)\s*…?\s*)"
        r"(\d+(?:\.\d+)?)s",
        clean,
        re.IGNORECASE,
    )
    elapsed = _fmt_secs(_usage_float(match.group(1))) if match is not None else "0s"
    return spinner, phase, elapsed


def _terminal_activity_status(activity_line: str) -> str | None:
    clean = _single_line(activity_line)
    upper = clean.upper()
    if "✗" in clean or "ERROR" in upper or "FAILED" in upper:
        return "error"
    if "CANCEL" in upper:
        return "cancelled"
    if "✓" in clean or any(word in upper for word in ("DONE", "SUCCEEDED", "COMPLETE")):
        return "succeeded"
    return None


def _latest_live_text(transcript: Transcript) -> str:
    if transcript._live_text:
        return transcript._live_text
    for entry in reversed(transcript.entries):
        if entry.role in {"assistant", "error", "system"}:
            text = _single_line(entry.text)
            if text:
                return text
    return "status recorded"


def _live_window_lines(
    transcript: Transcript,
    width: int,
    *,
    activity_line: str = "",
) -> list[str]:
    """Render a stable phase row plus the evidence currently backing that phase."""
    width = max(1, width)
    activity = _single_line(activity_line)
    if not transcript._live_kind and not activity:
        return [_status_row([], width), _status_row([], width)]

    terminal_status = _terminal_activity_status(activity)
    if terminal_status is not None and (transcript._live_kind or transcript.live_final):
        glyph = {"error": "✗", "cancelled": "•"}.get(terminal_status, "✓")
        row_one = activity or f"{glyph} {terminal_status}"
        label = _ROLE_LABELS.get(transcript._live_role, "RESULT")
        return [
            _status_row([row_one], width),
            _status_row([f"{label} ▸ {_latest_live_text(transcript)}"], width),
        ]

    if not transcript._live_kind:
        local = activity
        if not local or "WAITING" in local.upper() or "PROVIDER" in local.upper():
            parsed = _activity_parts(activity) if activity else None
            local = "⠋ STARTING" + (f" · {parsed[2]}" if parsed is not None else "")
        return [
            _status_row([local], width),
            _status_row(["runtime handshake pending"], width),
        ]

    meta: list[str] = []
    if transcript._live_turn is not None:
        meta.append(f"t{transcript._live_turn}")
    if transcript._live_calls:
        meta.append(f"call {transcript._live_calls}")
    if transcript._live_cache_hit is not None:
        meta.append("cache HIT" if transcript._live_cache_hit else "cache MISS")
    if transcript._live_bytes:
        meta.append(f"ctx {_human_bytes(transcript._live_bytes)}")

    if transcript.live_final:
        row_one = activity or f"✓ result {transcript._live_status or 'done'}"
        row_two = f"{_ROLE_LABELS.get(transcript._live_role, 'RESULT')} ▸ "
        row_two += transcript._live_text or "status recorded"
        if meta:
            row_two += " · " + " · ".join(meta)
        return [_status_row([row_one], width), _status_row([row_two], width)]

    phase = transcript._live_phase.casefold().replace("_", "-")
    if activity:
        row_one = activity
    elif transcript._live_tool:
        row_one = f"⠋ TOOL · {transcript._live_tool}"
    elif phase == "waiting":
        row_one = (
            f"{_ACTIVITY_PHASE_GLYPHS['waiting']} PROVIDER · waiting "
            f"{_fmt_secs(transcript._live_age_s)}"
        )
    elif phase == "thinking":
        row_one = (
            f"{_ACTIVITY_PHASE_GLYPHS['thinking']} THINKING · {_fmt_secs(transcript._live_age_s)}"
        )
    elif phase == "streaming":
        row_one = (
            f"{_ACTIVITY_PHASE_GLYPHS['streaming']} STREAMING · {_fmt_secs(transcript._live_age_s)}"
        )
    else:
        row_one = "⠋ ORCHESTRATING"

    details: list[str] = []
    if transcript._live_tool:
        if transcript._live_kind == "tool_event":
            glyph = {"ok": "✓", "failed": "✗"}.get(transcript._live_status, "•")
            tool_result = f"{glyph} {transcript._live_tool}"
            if transcript._live_duration_ms is not None:
                tool_result += f" {_format_duration(transcript._live_duration_ms)}"
            details.append(tool_result)
        else:
            details.append(f"tool {transcript._live_tool}")
        if transcript._live_text:
            label = _ROLE_LABELS.get(transcript._live_role, "TOOL")
            details.append(f"{label} ▸ {transcript._live_text}")
    elif transcript._live_text:
        label = _ROLE_LABELS.get(transcript._live_role, "LIVE")
        details.append(f"{label} ▸ {transcript._live_text}")
    elif phase == "waiting" or "PROVIDER" in row_one.upper():
        details.append("waiting for provider response")
    elif "CHILDREN" in row_one.upper() or "SUSPENDED" in row_one.upper():
        details.append("waiting for child results")
    elif transcript._live_command:
        details.append(transcript._live_command)
    else:
        details.append("processing runtime events")
    details.extend(meta)
    return [_status_row([row_one], width), _status_row([" · ".join(details)], width)]


def _transcript_lines(
    transcript: Transcript,
    width: int,
    capacity: int,
    *,
    color: bool = False,
    include_stream: bool = True,
) -> list[tuple[str, str]]:
    capacity = max(1, capacity)
    active = _stream_lines(transcript, width, capacity, color=color) if include_stream else []
    remaining = max(0, capacity - len(active))
    rendered: list[tuple[str, str]] = []
    if remaining:
        history: list[tuple[str, str]] = []
        previous_kind = ""
        for block in _transcript_blocks(
            transcript.entries,
            width,
            expanded=transcript.tool_details_expanded,
            color=color,
        ):
            kind = _transcript_block_kind(block)
            if history and kind == "tool" and previous_kind != "tool":
                history.append(("system", ""))
            history.extend(block)
            previous_kind = kind
        while len(history) > remaining:
            try:
                history.remove(next(row for row in history if not row[1]))
            except StopIteration:
                break
        rendered = history[-remaining:]
    rendered.extend(active)
    if not rendered:
        rendered = [("system", _clip(" Waiting for a prompt. Type /help for commands.", width))]
    return rendered[-capacity:]


def _agent_model(agent: Any) -> str:
    provider = getattr(agent, "provider", None)
    model = getattr(agent, "model", None)
    if provider and model:
        return f"{provider}/{model}"
    return model or provider or "?"


def _side_clean(value: Any) -> str:
    """Return one terminal-safe, single-line value for the side panel."""
    return _sanitize(value).replace("\n", " ")


def _side_row(kind: str, text: Any, width: int) -> tuple[str, str]:
    """Build a side-panel row that can never wrap at the panel boundary."""
    return kind, clip_terminal_text(_side_clean(text), max(1, width))


def _usage_field(line: str, key: str) -> str | None:
    match = re.search(rf"(?<![\w/]){re.escape(key)}=([^\s()]+)", _side_clean(line))
    return match.group(1) if match is not None else None


def _usage_int(value: Any, default: int = 0) -> int:
    if isinstance(value, bool):
        return default
    if isinstance(value, int):
        return max(0, value)
    if isinstance(value, float):
        return max(0, int(value)) if math.isfinite(value) else default
    if value is None:
        return default
    text = _side_clean(value).strip().lower().replace(",", "")
    if not text or text == "free":
        return default if not text else 0
    multiplier = 1
    if text[-1:] in {"k", "m"}:
        multiplier = 1_000 if text[-1] == "k" else 1_000_000
        text = text[:-1]
    try:
        number = float(text)
    except ValueError:
        return default
    return max(0, int(number * multiplier)) if math.isfinite(number) else default


def _usage_float(value: Any, default: float = 0.0) -> float:
    if isinstance(value, bool):
        return default
    if isinstance(value, str):
        value = value.strip().lstrip("$")
    try:
        number = float(value)
    except (TypeError, ValueError):
        return default
    return number if math.isfinite(number) else default


def _format_cost(value: Any) -> str:
    """Format estimated cash cost, not a claim about free tokens or quota."""
    cost = _usage_float(value)
    if cost <= 0:
        return "$0"
    rendered = f"{cost:.4g}"
    if "e" in rendered.lower():
        rendered = format(Decimal(rendered), "f")
        if "." in rendered:
            rendered = rendered.rstrip("0").rstrip(".")
    return f"${rendered}"


def _cache_rate(cached_tokens: int, input_tokens: int) -> float | None:
    if cached_tokens > 0 and input_tokens > 0:
        return min(1.0, cached_tokens / input_tokens)
    return None


def _usage_rows(
    snapshot: Any, cumulative_line: str, width: int, *, compact: bool = False
) -> list[tuple[str, str]]:
    """Render cumulative usage as aligned, compact rows."""

    def field(key: str) -> str | None:
        return _usage_field(cumulative_line, key)

    snapshot_calls = _usage_int(getattr(snapshot, "calls", 0))
    snapshot_tokens = _usage_int(getattr(snapshot, "total_tokens", 0))
    snapshot_input = _usage_int(getattr(snapshot, "input_tokens", 0))
    snapshot_output = _usage_int(getattr(snapshot, "output_tokens", 0))
    snapshot_cached = _usage_int(getattr(snapshot, "cached_tokens", 0))
    calls = _usage_int(field("calls"), snapshot_calls)
    total_tokens = _usage_int(field("tokens"), snapshot_tokens)
    input_tokens = _usage_int(field("in"), snapshot_input)
    output_tokens = _usage_int(field("out"), snapshot_output)
    cached_tokens = _usage_int(field("cached"), snapshot_cached)
    rate = _usage_float(
        field("out/s"),
        _usage_float(getattr(snapshot, "output_tokens_per_s", 0.0)),
    )
    cost = _usage_float(
        field("cost"),
        _usage_float(getattr(snapshot, "estimated_cost_usd", 0.0)),
    )
    lane = _current_lane(snapshot)
    last_cache_hit = getattr(lane, "last_provider_cache_hit", None) if lane is not None else None
    cache_rate = _cache_rate(cached_tokens, input_tokens)
    cache_share = f"{cache_rate:.0%}" if cache_rate is not None else "n/a"
    if last_cache_hit is True:
        cache_last, cache_kind = "HIT", "cache-hit"
    elif last_cache_hit is False:
        cache_last, cache_kind = "MISS", "cache-miss"
    else:
        cache_last, cache_kind = "?", "dim"

    if compact:
        return [
            _side_row("normal", f" out {_human_count(output_tokens)} · {rate:.1f} tok/s", width),
            _side_row(
                cache_kind,
                " cache "
                f"{cache_share} · {_human_count(cached_tokens)}/{_human_count(input_tokens)} "
                f"· {cache_last}",
                width,
            ),
            _side_row("dim", f" {calls} calls · est {_format_cost(cost)}", width),
        ]

    label_width = 7

    def row(label: str, value: str) -> tuple[str, str]:
        label_column = max(label_width, _display_width(label) + 1)
        return _side_row("normal", f" {label:<{label_column}}{value}", width)

    token_line = f" {'tokens':<{label_width}}{_human_count(total_tokens)}"
    has_details = any(
        field(key) is not None or hasattr(snapshot, snapshot_key)
        for key, snapshot_key in (
            ("in", "input_tokens"),
            ("out", "output_tokens"),
            ("cached", "cached_tokens"),
        )
    )
    detail_rows: list[tuple[str, str]] = []
    if has_details:
        input_value = _human_count(input_tokens)
        output_value = _human_count(output_tokens)
        cached_value = _human_count(cached_tokens)
        details = (
            (f"(in {input_value} · out {output_value} · cached {cached_value})", True),
            (f"(in {input_value}/out {output_value}/cached {cached_value})", True),
            (f"(in {input_value} · out {output_value})", False),
            (f"(in {input_value}/out {output_value})", False),
        )
        selected_detail = False
        for detail, includes_cached in details:
            candidate = f"{token_line} {detail}"
            if _display_width(candidate) <= max(1, width):
                token_line = candidate
                selected_detail = True
                if not includes_cached:
                    detail_rows.append(_side_row("normal", f" cached {cached_value}", width))
                break
        if not selected_detail:
            detail_rows.extend(
                [
                    row("in", input_value),
                    row("out", output_value),
                    _side_row("normal", f" cached {cached_value}", width),
                ]
            )

    rows = [
        row("calls", str(calls)),
        _side_row("normal", token_line, width),
        *detail_rows,
        row("out/s", f"{rate:.1f}"),
        row("cost", _format_cost(cost)),
    ]
    if cache_rate is not None or last_cache_hit is not None:
        rows.append(_side_row(cache_kind, f" {'cache':<7}{cache_share} · last {cache_last}", width))
    return rows


def _agent_rows(agents: tuple[Any, ...], width: int) -> list[tuple[str, str]]:
    """Render agents with stable glyph, task, and provider/model columns."""
    panel_width = max(1, width)
    name_start = 3
    states = [_side_clean(getattr(agent, "state", "?")).strip() or "?" for agent in agents]
    tasks = [_side_clean(getattr(agent, "task_id", "?")).strip() or "?" for agent in agents]
    state_width = min(
        max((terminal_display_width(state) for state in states), default=1),
        max(1, panel_width - name_start - 1),
    )
    task_width = max(
        1,
        min(
            max((terminal_display_width(task) for task in tasks), default=1),
            panel_width - name_start - 1 - state_width,
        ),
    )
    rows: list[tuple[str, str]] = []
    for agent, state, task in zip(agents, states, tasks, strict=True):
        role = "M" if getattr(agent, "role", "") == "main" else "S"
        rows.append(
            _side_row(
                state,
                f" {role} {pad_terminal_text(task, task_width)} "
                f"{pad_terminal_text(state, state_width)}",
                panel_width,
            )
        )
        rows.append(
            _side_row(
                "dim",
                " " * name_start + _agent_model(agent),
                panel_width,
            )
        )

        tokens = _usage_int(getattr(agent, "total_tokens", 0))
        parts = [f"{_human_count(tokens)} tok"]
        rate = getattr(agent, "output_tokens_per_s", None)
        if isinstance(rate, int | float) and math.isfinite(float(rate)):
            parts.append(f"{rate:.1f} out/s")
        tool = _side_clean(getattr(agent, "tool", "")).strip()
        if tool:
            parts.append(tool)
        stats = " " * name_start + parts[0]
        for part in parts[1:]:
            candidate = f"{stats} · {part}"
            if terminal_display_width(candidate) <= panel_width:
                stats = candidate
        rows.append(_side_row("dim", stats, panel_width))
    return rows


_RAIL_FULL_WIDTH = 32
_RAIL_COMPACT_WIDTH = 6
_RAIL_DETAIL_MIN_WIDTH = 24
_RAIL_STATE_GLYPHS = {
    "suspended": "Ⅱ",
    "active": "●",
    "queued": "○",
    "admitted": "○",
    "starting": "◐",
    "restarting": "↻",
    "merging": "↻",
    "succeeded": "✓",
    "done": "✓",
    "exited": "✓",
    "failed": "✗",
    "cancelled": "✗",
    "rejected": "✗",
}
_RAIL_LINEAGE_GLYPHS = {"exact": "=", "semantic": "~", "fresh": "∅", "": "?"}


def _rail_width(columns: int) -> int:
    if columns >= 100:
        return _RAIL_FULL_WIDTH
    if columns >= 80:
        return _RAIL_COMPACT_WIDTH
    return 0


def _frame_content_width(columns: int) -> int:
    columns = max(8, columns)
    rail_width = _rail_width(columns)
    separator = 1 if rail_width else 0
    return max(1, columns - 2 - rail_width - separator)


def _rail_state_glyph(state: Any) -> str:
    value = _side_clean(state).strip().casefold()
    return _RAIL_STATE_GLYPHS.get(value, "○")


def _rail_parent_id(agent: Any) -> str | None:
    value = getattr(agent, "parent_task_id", None)
    if value is None:
        return None
    return _side_clean(value).strip() or None


def _rail_lineage_glyph(lineage: Any) -> str:
    value = _side_clean(lineage).strip().casefold()
    return _RAIL_LINEAGE_GLYPHS.get(value, "?")


def _rail_depth(
    task_id: str,
    parents: Mapping[str, str | None],
    depths: dict[str, int] | None = None,
) -> int:
    depths = {} if depths is None else depths
    if task_id in depths:
        return depths[task_id]

    path: list[str] = []
    positions: dict[str, int] = {}
    current: str | None = task_id
    while current is not None and current not in depths and current not in positions:
        positions[current] = len(path)
        path.append(current)
        current = parents.get(current)

    if current is None:
        depth = 0
        if path:
            depths[path.pop()] = depth
    elif current in depths:
        depth = depths[current]
    else:
        cycle_start = positions[current]
        depth = len(path) - cycle_start
        for node in path[cycle_start:]:
            depths[node] = depth
        del path[cycle_start:]

    for node in reversed(path):
        depth += 1
        depths[node] = depth
    return depths[task_id]


def _rail_tree_order(
    agents: tuple[Any, ...], depths: dict[str, int] | None = None
) -> tuple[Any, ...]:
    tasks = [_side_clean(getattr(agent, "task_id", "?")).strip() or "?" for agent in agents]
    parents = {task: _rail_parent_id(agent) for agent, task in zip(agents, tasks, strict=True)}
    depths = {} if depths is None else depths
    order = sorted(
        range(len(agents)),
        key=lambda index: (_rail_depth(tasks[index], parents, depths), index),
    )
    return tuple(agents[index] for index in order)


def _rail_lane_rows(agents: tuple[Any, ...], width: int) -> list[tuple[str, str]]:
    panel_width = max(1, width)
    if not agents:
        return [_side_row("dim", " no agents yet", panel_width)]
    depths: dict[str, int] = {}
    agents = _rail_tree_order(agents, depths)

    tasks = [_side_clean(getattr(agent, "task_id", "?")).strip() or "?" for agent in agents]
    parents: dict[str, str | None] = {}
    children: dict[str | None, list[str]] = {}
    for agent, task in zip(agents, tasks, strict=True):
        parent = _rail_parent_id(agent)
        parents[task] = parent
        children.setdefault(parent, []).append(task)

    rows: list[tuple[str, str]] = []
    for agent, task in zip(agents, tasks, strict=True):
        parent = parents.get(task)
        siblings = children.get(parent, ())
        connector = "└" if not siblings or task == siblings[-1] else "├"
        indent = "  " * min(_rail_depth(task, parents, depths), panel_width // 2)
        state = _side_clean(getattr(agent, "state", "queued")).strip().casefold() or "queued"
        lineage = _rail_lineage_glyph(getattr(agent, "lineage", ""))
        prefix = f"{indent}{connector}{_rail_state_glyph(state)}{lineage} "
        suffix = f" E{_usage_int(getattr(agent, 'epoch', 0))}"
        name_width = max(1, panel_width - _display_width(prefix) - _display_width(suffix))
        rows.append(_side_row(state, prefix + _clip(task, name_width) + suffix, panel_width))
    return rows


def _context_bar(context: Any, width: int) -> str:
    """Render head / semantic trunk / raw tail as a compact CAST block strip."""
    head = _usage_int(getattr(context, "stable_head_bytes", 0))
    trunk = _usage_int(getattr(context, "summary_trunk_bytes", 0))
    raw = _usage_int(getattr(context, "raw_tail_bytes", 0))
    segments = _usage_int(getattr(context, "summary_segments", 0))
    head_known = head > 0
    if not head_known and trunk > 0 and segments == 0:
        # With no semantic segments, the entire trunk is the stable head.
        head = trunk
        head_known = True
    semantic = max(0, trunk - head) if head_known else trunk
    values = [head, semantic, raw]
    total = sum(values)
    if total <= 0:
        return " H· S· R·"
    cells = max(3, min(14, max(3, width - 9)))
    counts = [max(1, round(cells * value / total)) if value else 0 for value in values]
    while sum(counts) > cells:
        index = max((i for i, count in enumerate(counts) if count > 1), key=counts.__getitem__)
        counts[index] -= 1
    while sum(counts) < cells:
        index = max(range(3), key=lambda i: values[i] / total - counts[i] / cells)
        counts[index] += 1
    head_label = "H" if head_known else "H?"
    semantic_label = "S" if head_known else "S?"
    return f" {head_label}{'█' * counts[0]} {semantic_label}{'▓' * counts[1]} R{'░' * counts[2]}"


def _context_rows(
    snapshot: Any,
    width: int,
    *,
    compact_epoch: bool = False,
) -> list[tuple[str, str]]:
    panel_width = max(1, width)
    context = getattr(snapshot, "context", None)
    if context is None:
        return [_side_row("dim", " unavailable", panel_width)]
    approx = "≈" if getattr(context, "approximate", False) else ""
    epoch = _usage_int(getattr(context, "epoch", 0))
    epoch_text = f"e{epoch}" if compact_epoch else str(epoch)
    return [
        _side_row(
            "normal",
            f" CAST {epoch_text} · {getattr(context, 'summary_segments', 0)} seg",
            panel_width,
        ),
        _side_row("cast", _context_bar(context, panel_width), panel_width),
        _side_row(
            "normal",
            f" trunk {approx}{_human_count(getattr(context, 'estimated_trunk_tokens', 0))} tok",
            panel_width,
        ),
        _side_row(
            "dim",
            f" {_human_bytes(getattr(context, 'summary_trunk_bytes', 0))} serialized",
            panel_width,
        ),
        _side_row(
            "normal",
            f" raw {approx}{_human_count(getattr(context, 'estimated_raw_tail_tokens', 0))} tok",
            panel_width,
        ),
        _side_row(
            "dim",
            f" {_human_bytes(getattr(context, 'raw_tail_bytes', 0))} tail bytes",
            panel_width,
        ),
        _side_row(
            "dim",
            " checkpoint " + _side_clean(getattr(context, "checkpoint_ref", None) or "none"),
            panel_width,
        ),
    ]


def _rail_fold_rows(snapshot: Any, width: int) -> list[tuple[str, str]]:
    panel_width = max(1, width)
    context = getattr(snapshot, "context", None)
    epoch = _usage_int(getattr(context, "epoch", 0)) if context is not None else 0
    rows: list[tuple[str, str]] = []
    for event in tuple(getattr(snapshot, "recent_events", ())):
        kind = _side_clean(getattr(event, "kind", "")).strip()
        if kind not in {"context_epoch_advanced", "compaction_failed"}:
            continue
        detail = _side_clean(getattr(event, "detail", "")).strip()
        if kind == "context_epoch_advanced":
            text = f" │ {kind} e{epoch}"
            row_kind = "active"
        else:
            text = f" ! {kind}"
            row_kind = "failed"
        if detail:
            text += f" · {detail}"
        rows.append(_side_row(row_kind, text, panel_width))
    return rows[-4:]


def _compact_rail_rows(
    snapshot: Any,
    width: int = _RAIL_COMPACT_WIDTH,
    capacity: int = 32,
) -> list[tuple[str, str]]:
    panel_width = max(1, width)
    agents = _rail_tree_order(tuple(getattr(snapshot, "agents", ())))
    rows: list[tuple[str, str]] = []
    for agent in agents:
        parent = _rail_parent_id(agent)
        connector = "├" if parent is not None else "└"
        state = _rail_state_glyph(getattr(agent, "state", "queued"))
        lineage = _side_clean(getattr(agent, "lineage", "")).strip().casefold()
        lineage_suffix = "" if lineage == "exact" else _rail_lineage_glyph(lineage)
        text = f"{connector}{state}={lineage_suffix}E{_usage_int(getattr(agent, 'epoch', 0))}"
        rows.append(_side_row(getattr(agent, "state", "queued"), text, panel_width))
    if not rows:
        context = getattr(snapshot, "context", None)
        epoch = _usage_int(getattr(context, "epoch", 0)) if context is not None else 0
        rows.append(_side_row("dim", f"└○=?E{epoch}", panel_width))
    else:
        context = getattr(snapshot, "context", None)
        epoch = _usage_int(getattr(context, "epoch", 0)) if context is not None else 0
    for kind, _ in _rail_fold_rows(snapshot, panel_width):
        tick = "!" if kind == "failed" else "│"
        rows.append(_side_row(kind, "├" + tick + "E" + str(epoch), panel_width))
    return rows[: max(1, capacity)]


def _rail_selected_agent(snapshot: Any, agents: tuple[Any, ...]) -> Any | None:
    """Return the run currently represented by the live activity ticker."""
    if not agents:
        return None
    by_task = {
        _side_clean(getattr(agent, "task_id", "?")).strip() or "?": agent for agent in agents
    }
    for key in ("selected_task_id", "cursor_task_id", "selected_run_id"):
        value = getattr(snapshot, key, None)
        if isinstance(value, str) and value in by_task:
            return by_task[value]
    for key in ("selected_agent", "selected_run"):
        value = getattr(snapshot, key, None)
        task_id = getattr(value, "task_id", None)
        if isinstance(task_id, str) and task_id in by_task:
            return by_task[task_id]
    cursor = getattr(snapshot, "cursor", None)
    if type(cursor) is int and 0 <= cursor < len(agents):
        return agents[cursor]
    for agent in agents:
        if any(
            getattr(agent, key, False) is True
            for key in ("selected", "is_selected", "focused", "cursor")
        ):
            return agent
    return next(
        (agent for agent in agents if getattr(agent, "role", "") == "main"),
        agents[0],
    )


def _rail_detail_rows(
    agent: Any,
    width: int,
    *,
    activity_line: str = "",
) -> list[tuple[str, str]]:
    """Two useful rows per lane; streamed text already has a live window."""
    if width < _RAIL_DETAIL_MIN_WIDTH:
        return []
    state = _side_clean(getattr(agent, "state", "unknown"))
    rows: list[tuple[str, str]] = []
    if getattr(agent, "provider", None) or getattr(agent, "model", None):
        identity = "   " + _agent_model(agent)
        cache_hit = getattr(agent, "last_provider_cache_hit", None)
        if cache_hit is True:
            identity += " · cache hit"
            identity_kind = "cache-hit"
        elif cache_hit is False:
            identity += " · cache miss"
            identity_kind = "cache-miss"
        else:
            identity_kind = "dim"
        rows.append(_side_row(identity_kind, identity, width))
    if state == "suspended":
        detail, detail_kind = "waiting for children", "children"
    elif state in {"succeeded", "failed", "cancelled", "exited", "rejected"}:
        detail, detail_kind = state, state
    elif activity_line:
        detail = _side_clean(activity_line).strip()
        if " " in detail:
            detail = detail.split(" ", 1)[1]
        upper = detail.upper()
        if "STREAMING" in upper:
            detail_kind = "streaming"
        elif "THINKING" in upper:
            detail_kind = "thinking"
        elif "ROUTING" in upper:
            detail_kind = "routing"
        elif "PROVIDER" in upper:
            detail_kind = "provider"
        elif "CHILDREN" in upper:
            detail_kind = "children"
        elif "TOOL" in upper:
            detail_kind = "tool"
        elif "STALL" in upper or "SILENT" in upper or "NO OUTPUT" in upper:
            detail_kind = "stalled"
        else:
            detail_kind = state
    else:
        tool = getattr(agent, "tool", None)
        phase = _side_clean(getattr(agent, "phase", "")).strip().casefold()
        if tool:
            detail, detail_kind = f"tool {_side_clean(tool)}", "tool"
        elif phase == "waiting":
            detail, detail_kind = "provider wait", "provider"
        elif phase in {"thinking", "streaming"}:
            detail, detail_kind = phase, phase
        else:
            detail, detail_kind = state, state
    rows.append(_side_row(detail_kind, "   " + detail, width))
    return rows


def _rail_rows(
    snapshot: Any,
    width: int = _RAIL_FULL_WIDTH,
    capacity: int = 32,
    *,
    activity_line: str = "",
    cumulative_line: str = "",
) -> list[tuple[str, str]]:
    panel_width = max(1, width)
    if panel_width <= _RAIL_COMPACT_WIDTH:
        return _compact_rail_rows(snapshot, panel_width, capacity)
    agents = _rail_tree_order(tuple(getattr(snapshot, "agents", ())))
    selected = _rail_selected_agent(snapshot, agents)
    context = [_side_row("heading", " CONTEXT", panel_width)]
    # Byte counts and checkpoint paths are available through /context.
    context.extend(
        row
        for index, row in enumerate(_context_rows(snapshot, panel_width, compact_epoch=True))
        if index in {0, 1, 2, 4}
    )
    context.extend(_rail_fold_rows(snapshot, panel_width))
    resources: list[tuple[str, str]] = []
    if capacity >= 8 and (cumulative_line or hasattr(snapshot, "calls")):
        resources.append(_side_row("heading", " RESOURCES", panel_width))
        resources.extend(_usage_rows(snapshot, cumulative_line, panel_width, compact=True))
        quota = _quota_rows(snapshot, panel_width)
        if quota and capacity >= 14:
            resources.append(_side_row("heading", " QUOTA", panel_width))
            resources.extend(quota[:4])
            if len(quota) > 4:
                resources.append(_side_row("dim", " more: /quota", panel_width))
    body_capacity = max(2, capacity - len(resources))
    context = context[: max(0, body_capacity - 1 - min(4, len(agents)))]
    if len(context) < 2:
        context = []
    lane_capacity = max(2, body_capacity - len(context))
    terminal = {"succeeded", "failed", "cancelled", "exited", "rejected"}
    # Preserve the selected parent and live children before historical rows.
    slots = lane_capacity - 1
    if len(agents) > slots:
        slots = max(0, slots - 1)  # one row explains what is omitted
    order = sorted(
        range(len(agents)),
        key=lambda i: (
            agents[i] is not selected,
            getattr(agents[i], "state", "") in terminal,
            i,
        ),
    )
    visible = set(order[:slots])
    hidden = len(agents) - len(visible)
    rows = _rail_lane_rows(agents, panel_width) if agents else []
    lines = [_side_row("heading", " LANES", panel_width)]
    remaining = len(visible)
    crowded = len(agents) * 3 + 1 > lane_capacity
    for index, (agent, row) in enumerate(zip(agents, rows, strict=True)):
        if index not in visible:
            continue
        lines.append(row)
        remaining -= 1
        if crowded and agent is not selected and getattr(agent, "state", "") in terminal:
            continue
        room = max(0, lane_capacity - len(lines) - remaining - bool(hidden))
        lines.extend(
            _rail_detail_rows(
                agent,
                panel_width,
                activity_line=activity_line if agent is selected else "",
            )[:room]
        )
    if not agents:
        lines.append(_side_row("dim", " no agents yet", panel_width))
    if hidden:
        lines.append(_side_row("dim", f" {hidden} more: /agents", panel_width))
    return (lines + context + resources)[: max(1, capacity)]


def _recent_rows(event: Any, width: int) -> list[tuple[str, str]]:
    """Keep each recent event attached to its detail or omit that detail."""
    panel_width = max(1, width)
    kind = _side_clean(getattr(event, "kind", "event")).strip() or "event"
    detail = _side_clean(getattr(event, "detail", "")).strip()
    if not detail:
        return [_side_row("dim", f" {kind}", panel_width)]

    delimiter = " · "
    kind_width = (
        panel_width - 1 - terminal_display_width(delimiter) - terminal_display_width(detail)
    )
    if kind_width >= 1:
        return [
            _side_row(
                "dim",
                f" {_clip(kind, kind_width)}{delimiter}{detail}",
                panel_width,
            )
        ]

    kind_row = _side_row("dim", f" {kind}", panel_width)
    detail_row = f"   {detail}"
    if terminal_display_width(detail_row) <= panel_width:
        return [kind_row, _side_row("dim", detail_row, panel_width)]
    return [kind_row]


def _append_side_rows(
    lines: list[tuple[str, str]], rows: list[tuple[str, str]], capacity: int
) -> None:
    """Append a row block without leaving a trailing detail fragment."""
    room = max(0, capacity - len(lines))
    if room <= 0:
        return
    if len(rows) <= room:
        lines.extend(rows)
    else:
        lines.append(rows[0])


def _quota_field(window: Any, key: str, default: Any = None) -> Any:
    if isinstance(window, Mapping):
        return window.get(key, default)
    return getattr(window, key, default)


def _quota_windows(snapshot: Any) -> tuple[Any, ...]:
    windows = getattr(snapshot, "quota_windows", None)
    if windows is None:
        windows = getattr(snapshot, "quota_snapshots", None)
    if windows is None:
        windows = tuple(
            window
            for agent in getattr(snapshot, "agents", ())
            for window in (getattr(agent, "quota_windows", ()) or ())
        )
    if isinstance(windows, Mapping):
        if "provider" in windows and "name" in windows:
            return (windows,)
        windows = windows.values()
    try:
        return tuple(windows)
    except TypeError:
        return ()


def _quota_rows(snapshot: Any, width: int) -> list[tuple[str, str]]:
    """Render known provider windows without clipping quota field labels."""
    panel_width = max(1, width)
    rows: list[tuple[str, str]] = []
    seen: set[tuple[str, str]] = set()
    for window in _quota_windows(snapshot):
        provider = _side_clean(_quota_field(window, "provider", "")).strip()
        name = _side_clean(_quota_field(window, "name", "")).strip()
        if not provider or not name or (provider, name) in seen:
            continue
        seen.add((provider, name))

        token_allowance = _usage_int(
            _quota_field(
                window,
                "allowance_tokens",
                _quota_field(window, "token_allowance", 0),
            )
        )
        request_allowance = _usage_int(
            _quota_field(
                window,
                "allowance_requests",
                _quota_field(window, "request_allowance", 0),
            )
        )
        fields: list[str] = []
        if token_allowance:
            remaining = _quota_field(window, "remaining_tokens")
            if remaining is None:
                used = _usage_int(_quota_field(window, "used_tokens"))
                remaining = max(0, token_allowance - used)
            fields.append(f"{_usage_int(remaining)}/{token_allowance} tokens")
        if request_allowance:
            remaining = _quota_field(window, "remaining_requests")
            if remaining is None:
                used = _usage_int(_quota_field(window, "used_requests"))
                remaining = max(0, request_allowance - used)
            fields.append(f"{_usage_int(remaining)}/{request_allowance} requests")
        if not fields:
            continue

        subject = f"{provider}/{name}"
        full = f" {subject}: {', '.join(fields)}"
        compact_fields = [
            field.replace(" tokens", " tok").replace(" requests", " req") for field in fields
        ]
        compact = f" {subject}: {', '.join(compact_fields)}"
        if terminal_display_width(full) <= panel_width:
            rows.append(_side_row("normal", full, panel_width))
        elif terminal_display_width(compact) <= panel_width:
            rows.append(_side_row("normal", compact, panel_width))
        else:
            rows.append(_side_row("normal", f" {subject}", panel_width))
            rows.extend(_side_row("dim", f"   {field}", panel_width) for field in fields)
    return rows


def render_quota_rows(snapshot: Any, width: int = 44) -> list[str]:
    """Return quota rows in the compact format used by the side panel."""
    return [text for _, text in _quota_rows(snapshot, width)]


def _side_sections(
    snapshot: Any, cumulative_line: str, width: int, capacity: int
) -> list[tuple[str, str]]:
    panel_width = max(1, width)
    capacity = max(1, capacity)
    lines: list[tuple[str, str]] = []
    agents = tuple(getattr(snapshot, "agents", ()))
    lines.append(_side_row("heading", " AGENTS", panel_width))
    if not agents:
        lines.append(_side_row("dim", " no agents yet", panel_width))
    else:
        lines.extend(_agent_rows(agents[-6:], panel_width))

    lines.append(_side_row("heading", " CONTEXT", panel_width))
    lines.extend(_context_rows(snapshot, panel_width))

    lines.append(_side_row("heading", " SESSION USAGE", panel_width))
    lines.extend(_usage_rows(snapshot, cumulative_line, panel_width))

    lines.append(_side_row("heading", " QUOTA", panel_width))
    quota_rows = _quota_rows(snapshot, panel_width)
    lines.extend(quota_rows or [_side_row("dim", " unavailable", panel_width)])

    recent = tuple(
        event
        for event in getattr(snapshot, "recent_events", ())
        if (
            (kind := _side_clean(getattr(event, "kind", "")).strip()) != "dirty"
            and not kind.startswith("worktree_cleanup")
        )
    )
    if recent:
        lines.append(_side_row("heading", " RECENT", panel_width))
        for event in recent[-4:]:
            _append_side_rows(lines, _recent_rows(event, panel_width), capacity)

    return [_side_row(kind, text, panel_width) for kind, text in lines[:capacity]]


def _style_kind(kind: Any, enabled: bool | int = True) -> str:
    if not isinstance(kind, str):
        kind = _side_clean(kind)
    if kind in {"failed", "cancelled", "rejected", "error"}:
        style = "red"
    elif kind in {"streaming", "succeeded", "done", "cache-hit"}:
        style = "green"
    elif kind in {"thinking", "cast"}:
        style = "magenta"
    elif kind in {"children"}:
        style = "pink"
    elif kind in {"provider", "waiting"}:
        style = "blue"
    elif kind in {"stalled", "cooldown", "cache-miss"}:
        style = "yellow"
    elif kind in {"active", "starting", "merging", "running", "tool", "heading"}:
        style = "cyan"
    elif kind == "dim":
        style = "dim"
    else:
        return ""
    return _status_color(style, enabled)


def _primary_rows(
    transcript: Transcript,
    width: int,
    *,
    color: bool = False,
    include_stream: bool = True,
) -> list[tuple[str, str]]:
    """Return safe, labelled transcript rows for the append-only view."""
    width = max(8, width)
    # The transcript itself is bounded, but a large Markdown entry may occupy
    # many wrapped rows.  Leave enough capacity to render the complete local
    # view so the Cockpit can append only the suffix it has not emitted yet.
    capacity = max(64, len(transcript.entries) * 16 + 64)
    rows = _transcript_lines(
        transcript,
        width,
        capacity,
        color=color,
        include_stream=include_stream,
    )
    rendered: list[tuple[str, str]] = []
    for role, text in rows:
        rendered.append((role, _clip(_safe_rendered(text), width)))
    return rendered


_STATUS_KEYS = frozenset(
    {
        "session",
        "turn",
        "branch",
        "generation",
        "provider",
        "model",
        "epoch",
        "checkpoint",
        "calls",
        "tokens",
        "out/s",
        "cost",
        "in",
        "out",
        "cached",
    }
)


def _snapshot_is_active(snapshot: Any) -> bool:
    session_status = _side_clean(getattr(snapshot, "session_status", "")).strip().casefold()
    if session_status in {
        "done",
        "ended",
        "succeeded",
        "complete",
        "completed",
        "failed",
        "error",
        "cancelled",
    }:
        return False
    return bool(
        getattr(snapshot, "active_agents", 0)
        or any(
            _side_clean(getattr(agent, "state", "")).strip().casefold()
            in {"starting", "active", "merging"}
            for agent in getattr(snapshot, "agents", ())
        )
    )


def _current_lane(snapshot: Any) -> Any | None:
    agents = tuple(getattr(snapshot, "agents", ()))
    main = next((agent for agent in agents if getattr(agent, "role", "") == "main"), None)
    active_main = (
        main
        if main is not None
        and _side_clean(getattr(main, "state", "")).strip().casefold()
        in {"starting", "active", "merging"}
        else None
    )
    active = next(
        (
            agent
            for agent in agents
            if _side_clean(getattr(agent, "state", "")).strip().casefold()
            in {"starting", "active", "merging"}
        ),
        None,
    )
    return active_main or active or main or (agents[-1] if agents else None)


def _status_fields(
    snapshot: Any,
    *,
    session_description: str,
    branch_line: str,
    cumulative_line: str,
) -> dict[str, str]:
    """Collect status facts once instead of rendering three verbose source rows."""
    fields: dict[str, str] = {}
    for source in (session_description, branch_line, cumulative_line):
        clean = _sanitize(source).replace("\n", " ")
        for match in re.finditer(r"(?<![\w/])([\w/]+)=([^\s·]+)", clean):
            key, value = match.groups()
            if key in _STATUS_KEYS:
                fields.setdefault(key, value)

    agents = tuple(getattr(snapshot, "agents", ()))
    main = next((agent for agent in agents if getattr(agent, "role", "") == "main"), None)
    provider = getattr(main, "provider", None) if main is not None else None
    model = getattr(main, "model", None) if main is not None else None
    if isinstance(provider, str) and provider:
        fields["provider"] = _sanitize(provider)
    if isinstance(model, str) and model:
        fields["model"] = _sanitize(model)
    if main is not None:
        main_turn = getattr(main, "turn", None)
        if isinstance(main_turn, int) and main_turn >= 0:
            fields["turn"] = str(main_turn)

    if _snapshot_is_active(snapshot):
        lane = _current_lane(snapshot)
        if "tokens" not in fields:
            fields["tokens"] = _human_count(
                _usage_int(getattr(lane, "total_tokens", getattr(snapshot, "total_tokens", 0)))
            )
        if "calls" not in fields:
            fields["calls"] = str(_usage_int(getattr(lane, "calls", getattr(snapshot, "calls", 0))))

    context = getattr(snapshot, "context", None)
    if context is not None:
        fields["epoch"] = str(getattr(context, "epoch", 0))
        checkpoint = getattr(context, "checkpoint_ref", None)
        if isinstance(checkpoint, str) and checkpoint:
            fields.setdefault("checkpoint", _sanitize(checkpoint))
    fields.setdefault("tokens", _human_count(_usage_int(getattr(snapshot, "total_tokens", 0))))
    rate = _usage_float(getattr(snapshot, "output_tokens_per_s", 0.0))
    fields.setdefault("out/s", f"{rate:.1f}")
    return fields


def _compact_checkpoint(value: str) -> str:
    """Show a checkpoint filename and keep each content hash to eight chars."""
    clean = _side_clean(value).strip().rstrip("/")
    if not clean or clean == "none":
        return "none"
    filename = clean.rsplit("/", 1)[-1]
    hashes = re.findall(r"(?i)(?<![a-z0-9])[0-9a-f]{9,}(?![a-z0-9])", filename)
    return hashes[0][:8] if hashes else filename


def _status_parts(fields: Mapping[str, str], previous: Mapping[str, str] | None) -> list[str]:
    status = fields.get("status", "idle")
    parts = ["┌ Cambium", f"status={status}"]
    provider = fields.get("provider")
    model = fields.get("model")
    if provider or model:
        parts.append(f"provider={provider or '?'} model={model or '?'}")
    if session := fields.get("session"):
        parts.append(f"session={_clip(session, 28)}")
    if turn := fields.get("turn"):
        parts.append(f"t={turn}")

    def changed(key: str) -> bool:
        return previous is None or fields.get(key) != previous.get(key)

    if fields.get("branch") and changed("branch"):
        parts.append(f"b={fields['branch']}")
    if fields.get("generation") and changed("generation"):
        parts.append(f"g={fields['generation']}")
    if fields.get("epoch") and changed("epoch"):
        parts.append(f"e={fields['epoch']}")
    if checkpoint := fields.get("checkpoint"):
        parts.append(f"ckpt={_compact_checkpoint(checkpoint)}")

    usage = []
    if calls := fields.get("calls"):
        usage.append(f"calls={calls}")
    if tokens := fields.get("tokens"):
        usage.append(f"{tokens} tok")
    if rate := fields.get("out/s"):
        usage.append(f"{rate}/s")
    if usage:
        parts.append(" ".join(usage))
    return parts


def _primary_status_line(
    snapshot: Any,
    *,
    session_description: str,
    branch_line: str,
    cumulative_line: str,
    width: int,
    previous: Mapping[str, str] | None = None,
    transcript: Transcript | None = None,
    activity_line: str = "",
    color: bool = False,
) -> str:
    """Build the one-line status strip used by the framed renderer."""
    fields = _status_fields(
        snapshot,
        session_description=session_description,
        branch_line=branch_line,
        cumulative_line=cumulative_line,
    )
    del previous
    width = max(8, width)
    parts = [_status_activity(_activity_status(snapshot, activity_line), color)]
    provider = fields.get("provider")
    model = fields.get("model")
    if provider or model:
        provider = _side_clean(provider or "?").strip()
        model = _short_model(model or "?")
        parts.append(_status_paint(f"{provider}/{model}", "cyan", color))
    if turn := fields.get("turn"):
        parts.append(f"t{_side_clean(turn)}")
    tokens = _human_count(
        _usage_int(fields.get("tokens"), _usage_int(getattr(snapshot, "total_tokens", 0)))
    )
    parts.append(_status_paint(f"{tokens} tok", "dim", color))
    if transcript is not None and transcript.current_tool_error_count > 0:
        parts.append(f"err{transcript.current_tool_error_count}")
    return _clip(" · ".join(parts), width)


def _activity_status(snapshot: Any, activity_line: str) -> str:
    """Keep the concrete ActivityState label; invent only an idle/terminal fallback."""
    clean = sanitize_terminal_text(activity_line, single_line=True).strip()
    if clean:
        return clean
    status = _side_clean(getattr(snapshot, "session_status", "idle")).casefold()
    if status in {"done", "ended", "succeeded", "complete", "completed"}:
        return "✓ done"
    if status in {"error", "failed", "failure"}:
        return "✗ error"
    return "⠋ orchestrating" if getattr(snapshot, "active_agents", 0) else "⠋ idle"


def _status_activity(activity_line: str, color: bool) -> str:
    clean = _safe_rendered(activity_line)
    match = _STATUS_PHASE_RE.match(clean)
    if match is None:
        return clean
    phase = match.group(3).casefold()
    style = _STATUS_PHASE_STYLES.get(phase)
    if style is None:
        return clean
    return (
        f"{match.group(1)}{match.group(2)}"
        f"{_status_paint(match.group(3), style, color)}{match.group(4)}"
    )


def _short_model(value: Any) -> str:
    model = _side_clean(value).strip() or "?"
    return model.replace("/", "-")


def _running_tool(activity_line: str) -> tuple[str, str] | None:
    match = re.search(
        r"(?:\brunning\s+|\bTOOL\s+·\s+)(\S+)(?:\s+(\d+(?:\.\d+)?)s)?",
        _side_clean(activity_line),
        re.IGNORECASE,
    )
    if match is None:
        return None
    duration = (
        _format_duration(_usage_float(match.group(2)) * 1000) if match.group(2) is not None else ""
    )
    return _sanitize(match.group(1)), duration


def _live_status_line(
    snapshot: Any,
    transcript: Transcript | None,
    *,
    session_description: str,
    branch_line: str,
    cumulative_line: str,
    width: int,
    activity_line: str = "",
    show_detail: bool = False,
    color: bool = False,
    prefix: bool = False,
) -> str:
    """Build one transient status row for the primary-buffer cockpit.

    The row carries the current resource first.  Agent/context metadata is an
    optional suffix and is clipped as one row; it never reserves a permanent
    pane or width in the live terminal.
    """
    width = max(1, width)
    fields = _status_fields(
        snapshot,
        session_description=session_description,
        branch_line=branch_line,
        cumulative_line=cumulative_line,
    )
    activity_source = activity_line
    if not activity_source and transcript is not None and transcript.live_final:
        activity_source = "✗ error" if transcript._live_status in {"error", "failed"} else "✓ done"
    activity = _status_activity(_activity_status(snapshot, activity_source), color)
    lane = _current_lane(snapshot)
    owner = transcript._live_task_id if transcript is not None else None
    if not owner and lane is not None:
        candidate = getattr(lane, "task_id", None)
        owner = _side_clean(candidate).strip() if candidate is not None else ""
    owner = owner or ""

    # A child event names its owner explicitly. Prefer that lane over the
    # main lane selected by the general snapshot helper so provider/model/tool
    # facts describe the resource that is actually producing the event.
    if owner:
        for candidate in getattr(snapshot, "agents", ()):
            task = _side_clean(getattr(candidate, "task_id", "")).strip()
            if task == owner:
                lane = candidate
                break

    upper_activity = _side_clean(activity).upper()
    if "TOOL" in upper_activity:
        phase = "tool"
    elif "STREAMING" in upper_activity or "RESPONDING" in upper_activity:
        phase = "streaming"
    elif "THINKING" in upper_activity:
        phase = "thinking"
    elif "WAITING" in upper_activity or "PROVIDER" in upper_activity:
        phase = "waiting"
    else:
        activity_name = _side_clean(activity).casefold()
        if "orchestrating" in activity_name:
            phase = "orchestrating"
        elif "cooldown" in activity_name:
            phase = "cooldown"
        elif "suspended" in activity_name or "children" in activity_name:
            phase = "children"
        else:
            phase = (
                _side_clean(
                    getattr(lane, "phase", None)
                    or getattr(lane, "state", None)
                    or getattr(snapshot, "session_status", "idle")
                )
                .strip()
                .casefold()
                .replace("_", "-")
                or "idle"
            )

    terminal = _terminal_activity_status(activity_line)
    if terminal is None and transcript is not None and transcript.live_final:
        terminal = "error" if transcript._live_status in {"error", "failed"} else "succeeded"
    if terminal is not None:
        phase = {
            "succeeded": "done",
            "error": "error",
            "cancelled": "cancelled",
        }.get(terminal, terminal)
        activity = {
            "succeeded": "✓ done",
            "error": "✗ error",
            "cancelled": "• cancelled",
        }.get(terminal, activity)
    tool = transcript._live_tool if transcript is not None else None
    if terminal is not None:
        tool = None
    elif not tool and lane is not None:
        candidate = getattr(lane, "tool", None)
        tool = _side_clean(candidate).strip() if candidate else ""
    if terminal is None and not tool:
        running = _running_tool(activity_line)
        tool = running[0] if running is not None else ""

    provider = getattr(lane, "provider", None) or fields.get("provider")
    model = getattr(lane, "model", None) or fields.get("model")
    provider_model = "/".join(_side_clean(value).strip() for value in (provider, model) if value)
    tokens = _human_count(
        _usage_int(fields.get("tokens"), _usage_int(getattr(snapshot, "total_tokens", 0)))
    )
    calls = _usage_int(fields.get("calls"), _usage_int(getattr(snapshot, "calls", 0)))

    required = [activity, _status_paint(f"{tokens} tok", "dim", color)]
    # Keep phase explicit even when ActivityState is unavailable (for example
    # during a resize or a queued inspection command).
    if phase and phase not in upper_activity.casefold():
        required.append(f"phase={phase}")
    if owner:
        required.append(f"owner={_side_clean(owner)}")
    if provider_model:
        required.append(_status_paint(provider_model, "cyan", color))
    if tool:
        required.append(f"tool={_side_clean(tool)}")

    optional: list[str] = []
    if calls:
        optional.append(f"{calls} calls")
    if transcript is not None and transcript.current_tool_error_count > 0:
        optional.append(f"err{transcript.current_tool_error_count}")
    if show_detail:
        optional.append(
            "agents="
            f"{_usage_int(getattr(snapshot, 'active_agents', 0))}/"
            f"{_usage_int(getattr(snapshot, 'queued_agents', 0))}"
        )
        context = getattr(snapshot, "context", None)
        if context is not None:
            optional.append(f"ctx=e{_usage_int(getattr(context, 'epoch', 0))}")
        input_tokens = _usage_int(
            getattr(lane, "input_tokens", getattr(snapshot, "input_tokens", 0))
        )
        output_tokens = _usage_int(
            getattr(lane, "output_tokens", getattr(snapshot, "output_tokens", 0))
        )
        if input_tokens or output_tokens:
            optional.append(f"in/out={_human_count(input_tokens)}/{_human_count(output_tokens)}")

    def join(parts: list[str]) -> str:
        return " · ".join(part for part in parts if part)

    parts = [*required]
    for item in optional:
        candidate = join([*parts, item])
        if _display_width(candidate) <= width:
            parts.append(item)
        else:
            break

    text = join(parts)
    if _display_width(text) > width and len(required) > 1:
        # Preserve the owner/provider/tool tail when the terminal is narrow;
        # optional usage is already omitted above.
        tail = required[1:]
        tail_text = join(tail)
        separator_width = _display_width(" · ") if tail_text else 0
        activity_width = max(1, width - _display_width(tail_text) - separator_width)
        required[0] = _clip(required[0], activity_width)
        text = join(required)
    if prefix:
        text = f"Cambium · {text}"
    return _clip(text, width)


def _tool_activity_row(transcript: Transcript, activity_line: str, width: int) -> str:
    count = transcript.current_tool_count
    name = transcript.last_tool_name
    duration = _format_duration(transcript.last_tool_duration_ms)
    running = _running_tool(activity_line)
    if running is not None:
        count += 1
        name, duration = running
    if count <= 0:
        return _status_row(["· 0 tools"], width)
    parts = [f"✓ {count} tools"]
    if name:
        last = f"last {name}"
        if duration:
            last += f" {duration}"
        parts.append(last)
    return _status_row(parts, width)


def _detail_status_line(
    snapshot: Any,
    cumulative_line: str,
    width: int,
    *,
    color: bool = False,
) -> str:
    """Render the one-line ambient agent, usage, and context summary."""

    lane = _current_lane(snapshot)

    def field(key: str, snapshot_key: str) -> int:
        if _snapshot_is_active(snapshot):
            return _usage_int(getattr(lane, snapshot_key, getattr(snapshot, snapshot_key, 0)))
        return _usage_int(
            _usage_field(cumulative_line, key),
            _usage_int(getattr(snapshot, snapshot_key, 0)),
        )

    input_tokens = field("in", "input_tokens")
    output_tokens = field("out", "output_tokens")
    cached_tokens = field("cached", "cached_tokens")
    total_tokens = field("tokens", "total_tokens")
    calls = field("calls", "calls")
    summaries = field("summaries", "summary_calls")
    rate = (
        _usage_float(
            getattr(lane, "output_tokens_per_s", getattr(snapshot, "output_tokens_per_s", 0.0))
        )
        if _snapshot_is_active(snapshot)
        else _usage_float(
            _usage_field(cumulative_line, "out/s"),
            _usage_float(getattr(snapshot, "output_tokens_per_s", 0.0)),
        )
    )
    cost_field = _usage_field(cumulative_line, "cost")
    cost = (
        cost_field
        if cost_field in {"free", "subscription"} and not _snapshot_is_active(snapshot)
        else _format_cost(
            _usage_float(
                getattr(lane, "estimated_cost_usd", getattr(snapshot, "estimated_cost_usd", 0.0))
                if _snapshot_is_active(snapshot)
                else cost_field,
                _usage_float(getattr(snapshot, "estimated_cost_usd", 0.0)),
            )
        )
    )
    cache_rate = _cache_rate(cached_tokens, input_tokens)
    last_cache_hit = getattr(lane, "last_provider_cache_hit", None) if lane is not None else None
    cache = f"{cache_rate:.0%}" if cache_rate is not None else "n/a"
    if last_cache_hit is True:
        cache += "/HIT"
    elif last_cache_hit is False:
        cache += "/MISS"

    agents = " ".join(
        (
            "agents",
            _status_paint(
                f"active={_usage_int(getattr(snapshot, 'active_agents', 0))}",
                "bold",
                color,
            ),
            _status_paint(
                f"queued={_usage_int(getattr(snapshot, 'queued_agents', 0))}",
                "yellow",
                color,
            ),
            _status_paint(
                f"ok={_usage_int(getattr(snapshot, 'succeeded_agents', 0))}",
                "green",
                color,
            ),
            _status_paint(
                f"failed={_usage_int(getattr(snapshot, 'failed_agents', 0))}",
                "red",
                color,
            ),
        )
    )
    cache_style = (
        "green"
        if last_cache_hit is True
        else "yellow"
        if last_cache_hit is False
        else "green"
        if cache_rate is not None and cache_rate >= 0.5
        else "yellow"
    )
    cache_text = _status_paint(cache, cache_style, color) if cache_rate is not None else cache
    usage = (
        "usage "
        f"{_status_paint(f'in={_human_count(input_tokens)}', 'dim', color)} "
        f"{_status_paint(f'out={_human_count(output_tokens)}', 'dim', color)} "
        f"{_status_paint(f'cached={_human_count(cached_tokens)}', 'dim', color)} "
        f"({cache_text}) "
        f"{_status_paint(f'total={_human_count(total_tokens)}', 'dim', color)} "
        f"calls={calls} summaries={summaries} "
        f"out/s={rate:.1f} cost={cost}"
    )
    line = f"{agents} · {usage}"

    context = getattr(snapshot, "context", None)
    if context is not None:
        trunk_prefix = "≈" if getattr(context, "approximate", False) else "="
        context_line = (
            f"context epoch={_usage_int(getattr(context, 'epoch', 0))} "
            f"trunk{trunk_prefix}{_human_count(getattr(context, 'estimated_trunk_tokens', 0))}tok "
            f"segments={_usage_int(getattr(context, 'summary_segments', 0))}"
        )
        context_line = _status_paint(context_line, "dim", color)
        candidate = f"{line} · {context_line}"
        if _display_width(candidate) <= max(1, width):
            line = candidate
    return _status_row([line], width)


_FIXED_MIN_HEIGHT = 12
_STATUS_ROW_COUNT = 5
_BOTTOM_RESERVED_ROWS = 1 + _STATUS_ROW_COUNT + 1
_FRAME_OVERHEAD = 2 + _BOTTOM_RESERVED_ROWS


def _frame_overhead(show_detail: bool) -> int:
    return _FRAME_OVERHEAD if show_detail else _FRAME_OVERHEAD - 1


def _status_row(parts: list[str], width: int) -> str:
    text = " · ".join(part for part in parts if part)
    return _clip(f" {text}", max(1, width))


def _status_rows(
    snapshot: Any,
    transcript: Transcript,
    *,
    session_description: str,
    branch_line: str,
    cumulative_line: str,
    width: int,
    activity_line: str = "",
    show_detail: bool = True,
    include_live: bool = False,
    color: bool = False,
) -> list[str]:
    """Render the rolling tool row, fixed live window, and status rows."""
    width = max(1, width)
    rows = [_tool_activity_row(transcript, activity_line, width)]
    if include_live:
        rows.extend(_live_window_lines(transcript, width, activity_line=activity_line))
    rows.append(
        _status_row(
            [
                _primary_status_line(
                    snapshot,
                    session_description=session_description,
                    branch_line=branch_line,
                    cumulative_line=cumulative_line,
                    width=width,
                    transcript=transcript,
                    activity_line=activity_line if not include_live else "",
                    color=color,
                )
            ],
            width,
        ),
    )
    if show_detail:
        rows.append(_detail_status_line(snapshot, cumulative_line, width, color=color))
    return rows


def _frame_inside(text: str, width: int) -> str:
    inner = max(1, width - 2)
    clean = _safe_rendered(text).replace("\n", " ").replace("\r", " ").replace("\t", " ")
    return "│" + _pad(clean, inner) + "│"


def _split_frame_row(
    text: str,
    width: int,
    rail_width: int,
    *,
    rail_text: str = "",
    left_color: str = "",
    rail_kind: str = "dim",
    color: bool = False,
) -> str:
    width = max(8, width)
    left_width = _frame_content_width(width)
    left = _paint(_frame_inside(text, left_width + 2), left_color, color)
    if not rail_width:
        return left
    right = _pad(_paint(rail_text, _style_kind(rail_kind, color), color), rail_width)
    return left + right + _paint("│", _DIM_CYAN, color)


def _cockpit_frame_lines(
    snapshot: Any,
    transcript: Transcript,
    *,
    session_description: str,
    branch_line: str,
    cumulative_line: str,
    width: int,
    height: int,
    color: bool,
    input_label: str,
    activity_line: str,
    show_detail: bool = True,
    primary_rows: tuple[tuple[str, str], ...] | None = None,
) -> list[str]:
    width = max(8, width)
    height = max(_FIXED_MIN_HEIGHT, height)
    inner = max(1, width - 2)
    rail_width = _rail_width(width)
    left_inner = _frame_content_width(width)
    status_rows = _status_rows(
        snapshot,
        transcript,
        session_description=session_description,
        branch_line=branch_line,
        cumulative_line=cumulative_line,
        width=left_inner,
        activity_line=activity_line,
        show_detail=show_detail,
        include_live=True,
        color=color,
    )
    conversation_capacity = max(1, height - _frame_overhead(show_detail))
    status = _single_line(getattr(snapshot, "session_status", "idle")) or "idle"
    conversation = (
        list(primary_rows[-conversation_capacity:])
        if primary_rows is not None
        else _transcript_lines(
            transcript,
            left_inner,
            conversation_capacity,
            color=color,
            include_stream=True,
        )
    )
    if not rail_width:
        lines = [
            _paint("┌" + _pad(f" Cambium · conversation · {status} ", inner) + "┐", _CYAN, color)
        ]
        for role, text in conversation[:conversation_capacity]:
            lines.append(
                _paint(
                    _frame_inside(text, width),
                    _role_color(role, color),
                    color,
                )
            )
        while len(lines) < 1 + conversation_capacity:
            lines.append(_frame_inside("", width))

        lines.append(_paint("├" + "─" * inner + "┤", _DIM_CYAN, color))
        for text in status_rows:
            lines.append(_paint(_frame_inside(text, width), _DIM_CYAN, color))
        label = _clip(_sanitize(input_label).replace(chr(10), " "), max(1, inner - 8))
        lines.append(_paint(_frame_inside(f" input {label} ", width), _BLUE, color))
        lines.append(_paint("└" + "─" * inner + "┘", _CYAN, color))
        return lines[:height]

    rail_rows = _rail_rows(
        snapshot,
        rail_width,
        conversation_capacity,
        activity_line=activity_line,
        cumulative_line=cumulative_line,
    )
    rail_heading = (
        _pad("", rail_width)
        if rail_width == _RAIL_COMPACT_WIDTH
        else _pad(_paint(" OPERATOR RAIL", _CYAN, color), rail_width)
    )
    heading = _paint(
        "┌" + _pad(f" Cambium · conversation · {status} ", left_inner) + "┬",
        _CYAN,
        color,
    )
    lines = [heading + rail_heading + _paint("┐", _CYAN, color)]
    for index in range(conversation_capacity):
        role, text = conversation[index] if index < len(conversation) else ("", "")
        rail_kind, rail_text = rail_rows[index] if index < len(rail_rows) else ("", "")
        lines.append(
            _split_frame_row(
                text,
                width,
                rail_width,
                rail_text=rail_text,
                left_color=_role_color(role, color),
                rail_kind=rail_kind,
                color=color,
            )
        )
    lines.append(
        _paint(
            "├" + "─" * left_inner + "┼" + "─" * rail_width + "┤",
            _DIM_CYAN,
            color,
        )
    )
    for text in status_rows:
        lines.append(
            _split_frame_row(
                text,
                width,
                rail_width,
                left_color=_DIM_CYAN,
                color=color,
            )
        )
    label = _clip(_sanitize(input_label).replace(chr(10), " "), max(1, left_inner - 8))
    lines.append(
        _split_frame_row(
            f" input {label} ",
            width,
            rail_width,
            left_color=_BLUE,
            color=color,
        )
    )
    lines.append(
        _paint(
            "└" + "─" * left_inner + "┴" + "─" * rail_width + "┘",
            _CYAN,
            color,
        )
    )
    return lines[:height]


def render_primary(
    snapshot: Any,
    transcript: Transcript,
    *,
    session_description: str,
    branch_line: str,
    cumulative_line: str,
    width: int,
    color: bool = False,
    activity_line: str = "",
    show_detail: bool = True,
) -> list[str]:
    """Render an append-only transcript followed by one compact status row."""
    width = max(8, width)
    lines = [
        _paint(text, _role_color(role, color), color)
        for role, text in _primary_rows(
            transcript,
            width,
            color=color,
            include_stream=True,
        )
    ]
    status = _live_status_line(
        snapshot,
        transcript,
        session_description=session_description,
        branch_line=branch_line,
        cumulative_line=cumulative_line,
        width=width,
        activity_line=activity_line,
        show_detail=show_detail,
        color=color,
        prefix=True,
    )
    lines.append(_paint(status, _DIM_CYAN, color))
    return lines


def render_cockpit(
    snapshot: Any,
    transcript: Transcript,
    *,
    session_description: str,
    branch_line: str,
    cumulative_line: str,
    width: int,
    height: int,
    color: bool = False,
    input_label: str = "›",
    activity_line: str = "",
    show_detail: bool = True,
) -> list[str]:
    """Render one deterministic conversation/status frame without controls."""
    width = max(8, width)
    if height < _FIXED_MIN_HEIGHT:
        return render_primary(
            snapshot,
            transcript,
            session_description=session_description,
            branch_line=branch_line,
            cumulative_line=cumulative_line,
            width=width,
            color=color,
            activity_line=activity_line,
            show_detail=show_detail,
        )
    return _cockpit_frame_lines(
        snapshot,
        transcript,
        session_description=session_description,
        branch_line=branch_line,
        cumulative_line=cumulative_line,
        width=width,
        height=height,
        color=color,
        input_label=input_label,
        activity_line=activity_line,
        show_detail=show_detail,
    )


def _suffix_prefix_overlap(previous: str, current: str) -> int:
    """Return the longest prefix of ``current`` matching ``previous``'s suffix."""
    if not previous or not current:
        return 0
    pattern = current
    prefix = [0] * len(pattern)
    for index in range(1, len(pattern)):
        length = prefix[index - 1]
        while length and pattern[index] != pattern[length]:
            length = prefix[length - 1]
        if pattern[index] == pattern[length]:
            length += 1
        prefix[index] = length

    length = 0
    for char in previous[-len(pattern) :]:
        while length and (length == len(pattern) or char != pattern[length]):
            length = prefix[length - 1]
        if char == pattern[length]:
            length += 1
    return length


class Cockpit:
    """Append-only primary-buffer terminal presentation.

    The terminal owns the timeline and its scrollback.  Only the final two
    rows are transient: one status row and one input row.  A redraw replaces
    those rows in place, then leaves the cursor on the input row so new output
    follows the terminal's normal bottom-scroll behaviour.
    """

    _Request = tuple[Any, Transcript, str, str, str, str, str]

    def __init__(self, stream: TextIO, *, enabled: bool = True) -> None:
        self.stream = stream
        self.enabled = enabled and _is_tty(stream)
        self.color = terminal_color_depth(stream) if self.enabled else 0
        self._entered = False
        self._previous_sigterm_handler: Any = None
        self._last_size = os.terminal_size((120, 40))
        self._input_active = False
        self._native_input = False
        self._input_owner: int | None = None
        self._input_prompt_label = "›"
        self._managed_input: tuple[str, int] = ("", 0)
        self._managed_input_active = False
        self._last_restored_input_text: str | None = None
        self._last_restored_input_label = "›"
        self._pending_draw: Cockpit._Request | None = None
        self._last_request: Cockpit._Request | None = None
        self._timeline_initialized = False
        self._last_history_rows: tuple[tuple[str, str], ...] = ()
        self._last_history_entries: tuple[TranscriptEntry, ...] = ()
        self._last_stream_text = ""
        self._last_stream_role: str | None = None
        self._last_stream_key: str | None = None
        self._stream_emitted_text = ""
        self._last_status_line = ""
        self._last_rendered_width: int | None = None
        self._draw_in_flight = False
        self._show_detail = False

    @property
    def size(self) -> os.terminal_size:
        return self._last_size

    @property
    def show_detail(self) -> bool:
        return self._show_detail

    def toggle_detail(self) -> bool:
        """Toggle the denser single-row status suffix."""
        self._show_detail = not self._show_detail
        return self._show_detail

    def __enter__(self) -> Cockpit:
        if self.enabled:
            self._previous_sigterm_handler = signal.getsignal(signal.SIGTERM)
            try:
                signal.signal(signal.SIGTERM, self._handle_sigterm)
            except (OSError, ValueError):
                self._previous_sigterm_handler = None
            self._entered = True
        return self

    def __exit__(self, exc_type: Any, exc: Any, traceback: Any) -> None:
        del exc_type, exc, traceback
        self.close()

    def _handle_sigterm(self, signum: int, frame: Any) -> None:
        del frame
        self.close()
        raise SystemExit(128 + signum)

    def close(self) -> None:
        if self.enabled and self._timeline_initialized:
            if self._input_active:
                self.hide_cursor(commit=True)
                # Hiding the managed draft releases the deferred-draw gate;
                # flush the latest timeline before leaving the terminal row.
                self.flush()
                self.stream.write("\r\n")
                self.stream.flush()
            else:
                self.flush()
                # Commit the blank input row and return to the next prompt or
                # shell line.  No alternate screen or full-frame cleanup is
                # needed because all history already belongs to scrollback.
                self.stream.write("\r\n")
                self.stream.flush()
            self._timeline_initialized = False
        if self._entered:
            self._entered = False
        if self._previous_sigterm_handler is not None:
            try:
                signal.signal(signal.SIGTERM, self._previous_sigterm_handler)
            except (OSError, ValueError):
                pass
            self._previous_sigterm_handler = None

    @staticmethod
    def _history_rows(
        transcript: Transcript, width: int, color: bool | int
    ) -> tuple[tuple[str, str], ...]:
        if not transcript.entries:
            return ()
        return tuple(
            _primary_rows(
                transcript,
                width,
                color=color,
                include_stream=False,
            )
        )

    def _stream_delta_rows(
        self,
        transcript: Transcript,
        width: int,
        previous_text: str,
        previous_role: str | None,
        previous_key: str | None,
        color: bool | int,
    ) -> tuple[tuple[tuple[str, str], ...], str]:
        text = transcript.streaming_text
        role = transcript.streaming_role
        key = transcript.streaming_key
        if not text or role is None:
            return (), ""

        same_stream = key == previous_key and role == previous_role
        emitted = self._stream_emitted_text if same_stream else ""
        if emitted and not text.startswith(emitted):
            # A bounded snapshot starts with an ellipsis and retains only the
            # stream tail.  Keep the already-emitted overlap out of scrollback
            # instead of replaying that retained tail after rollover.
            candidate = text[2:] if text.startswith("…\n") else text
            overlap = _suffix_prefix_overlap(emitted, candidate)
            if overlap:
                pending = candidate[overlap:]
            else:
                # An unrelated replacement is a new baseline.  Wait for a
                # complete line before appending it to normal scrollback.
                emitted = ""
                pending = text
        else:
            pending = text[len(emitted) :] if emitted else text
        newline = pending.rfind("\n")
        if newline < 0:
            # Keep partial words/lines out of scrollback. They are rendered
            # once the provider supplies a newline or commits the result.
            return (), emitted
        complete = pending[: newline + 1]
        rows = tuple(
            _entry_lines(
                TranscriptEntry(role=role, text=complete),
                width,
                color=bool(color),
            )
        )
        return rows, emitted + complete

    @staticmethod
    def _history_delta(
        current: tuple[tuple[str, str], ...],
        previous: tuple[tuple[str, str], ...],
    ) -> tuple[tuple[str, str], ...]:
        if current[: len(previous)] == previous:
            return current[len(previous) :]
        if not current:
            return ()
        # A bounded transcript can evict its oldest rows between draws.  Keep
        # only the suffix after the largest shared overlap instead of replaying
        # the whole retained view into native scrollback.
        maximum = min(len(previous), len(current))
        for size in range(maximum, 0, -1):
            if previous[-size:] == current[:size]:
                return current[size:]
        return (("dim", "··· transcript view refreshed ···"), *current)

    @staticmethod
    def _entry_rows(
        entries: tuple[TranscriptEntry, ...], width: int, color: bool | int
    ) -> tuple[tuple[str, str], ...]:
        rows: list[tuple[str, str]] = []
        previous_kind = ""
        for block in _transcript_blocks(entries, width, expanded=False, color=bool(color)):
            kind = _transcript_block_kind(block)
            if rows and kind == "tool" and previous_kind != "tool":
                rows.append(("system", ""))
            rows.extend(block)
            previous_kind = kind
        return tuple(rows)

    @classmethod
    def _entry_delta_rows(
        cls,
        current: tuple[TranscriptEntry, ...],
        previous: tuple[TranscriptEntry, ...],
        width: int,
        color: bool | int,
    ) -> tuple[tuple[str, str], ...]:
        if current[: len(previous)] == previous:
            return cls._entry_rows(current[len(previous) :], width, color)
        maximum = min(len(previous), len(current))
        for size in range(maximum, 0, -1):
            if previous[-size:] == current[:size]:
                return cls._entry_rows(current[size:], width, color)
        return cls._entry_rows(current, width, color)

    def _input_line_text(self) -> str | None:
        if not self._input_active:
            return ""
        # The POSIX TerminalInput editor is event-loop managed even though its
        # caller marks the row ``native``.  Only the legacy readline path must
        # read editor-owned storage here.
        if self._managed_input_active:
            return _sanitize(self._managed_input[0])
        if not self._native_input or _readline is None:
            return ""
        if self._input_owner != threading.get_ident():
            # Never inspect native editor storage from the event-loop thread.
            return None
        try:
            value = _readline.get_line_buffer()
        except (AttributeError, OSError, RuntimeError):
            return ""
        value = _sanitize(value)
        if value.endswith("\n"):
            return ""
        return value.replace("\r", " ").replace("\n", " ").replace("\t", " ")

    def _restore_input_line(self, text: str, *, force: bool = False) -> None:
        if not self._input_active:
            return
        managed = getattr(self, "_managed_input", None)
        cursor = managed[1] if managed is not None and self._managed_input_active else len(text)
        label = _sanitize(self._input_prompt_label)
        if "\n" in text:
            label += f" {text.count(chr(10), 0, cursor) + 1}/{text.count(chr(10)) + 1}"
            start = text.rfind("\n", 0, cursor) + 1
            end = text.find("\n", cursor)
            text, cursor = text[start : len(text) if end < 0 else end], cursor - start

        def display(value: str) -> str:
            return _sanitize(value).replace("\r", " ").replace("\n", "↵").replace("\t", " ")

        before = display(text[:cursor])
        text = display(text)
        cursor = len(before)
        room = max(1, self._last_size.columns - _display_width(label) - 3)
        start, cells = cursor, 0
        while start and cells + _display_width(text[start - 1]) < room - 1:
            start -= 1
            cells += _display_width(text[start])
        marker = "‹" if start else ""
        rendered = _clip(marker + text[start:], room)
        cursor_cells = _display_width(marker + text[start:cursor])
        if (
            not force
            and self._last_restored_input_text == text
            and self._last_restored_input_label == label
        ):
            return
        self.stream.write(f"\r{_CLEAR_LINE}{label} {rendered}")
        back = _display_width(rendered) - cursor_cells
        if back > 0:
            self.stream.write(f"\x1b[{back}D")
        self._last_restored_input_text = text
        self._last_restored_input_label = label

    def _write_input_blank(self) -> None:
        self.stream.write(f"\r{_CLEAR_LINE}")

    def _move_to_status(self) -> None:
        if self._timeline_initialized:
            self.stream.write("\r\x1b[1A")

    def _write_status_and_input(self, status: str, input_text: str = "") -> None:
        rendered = _paint(status, _DIM_CYAN, self.color)
        self.stream.write(f"\r{_CLEAR_LINE}{rendered}\n")
        self._write_input_blank()
        if self._input_active:
            self._restore_input_line(input_text, force=True)

    def _redraw_status_only(self, status: str) -> None:
        if not self._timeline_initialized:
            return
        # Preserve a native editor's exact cursor column while replacing the
        # row above it.  This is needed for wrapped/mid-line readline drafts.
        self.stream.write("\x1b[s")
        self._move_to_status()
        self.stream.write(f"\r{_CLEAR_LINE}{_paint(status, _DIM_CYAN, self.color)}")
        self.stream.write("\x1b[1B\r\x1b[u")

    def _append_timeline(self, rows: tuple[tuple[str, str], ...]) -> None:
        for role, text in rows:
            clean = _clip(_safe_rendered(text), max(1, self._last_size.columns))
            rendered = _paint(clean, _role_color(role, self.color), self.color)
            self.stream.write(f"\r{_CLEAR_LINE}{rendered}\n")

    def draw(
        self,
        snapshot: Any,
        transcript: Transcript,
        *,
        session_description: str,
        branch_line: str,
        cumulative_line: str,
        input_label: str = "›",
        activity_line: str = "",
        turn_active: bool = False,
        force: bool = False,
    ) -> None:
        """Append new timeline rows and refresh only status/input rows."""
        if not self.enabled:
            return
        self._last_size = shutil.get_terminal_size((120, 40))
        label = _clip(_sanitize(input_label).replace(chr(10), " "), 8)
        request: Cockpit._Request = (
            snapshot,
            transcript,
            _sanitize(session_description).replace(chr(10), " "),
            _sanitize(branch_line).replace(chr(10), " "),
            _sanitize(cumulative_line).replace(chr(10), " "),
            label,
            _sanitize(activity_line).replace(chr(10), " "),
        )
        if self._draw_in_flight:
            self._pending_draw = request
            return
        if (
            self._input_active
            and not force
            and (
                self._input_line_text() is None
                or (not self._native_input and not self._managed_input_active)
            )
        ):
            # Native readline owns its mutable line.  Keep the newest event
            # until Enter/Ctrl-C releases that storage.
            self._pending_draw = request
            return
        self._draw_in_flight = True
        try:
            self._draw_now(request, force=force)
        finally:
            self._draw_in_flight = False

    def _draw_now(self, request: Cockpit._Request, *, force: bool = False) -> None:
        (
            snapshot,
            transcript,
            session_description,
            branch_line,
            cumulative_line,
            input_label,
            activity,
        ) = request
        width = max(8, self._last_size.columns)
        history = self._history_rows(transcript, width, self.color)
        history_new = self._history_delta(history, self._last_history_rows)

        stream_new, stream_emitted = self._stream_delta_rows(
            transcript,
            width,
            self._last_stream_text,
            self._last_stream_role,
            self._last_stream_key,
            self.color,
        )
        current_stream_text = transcript.streaming_text
        current_stream_role = transcript.streaming_role
        current_stream_key = transcript.streaming_key
        current_history_entries = transcript.entries
        if current_history_entries == self._last_history_entries:
            # Presentation-only detail toggles must not replay immutable
            # transcript rows into normal-buffer scrollback.
            history_new = ()

        previous_stream_emitted = self._stream_emitted_text

        stream_ended_or_switched = (
            not current_stream_text
            or current_stream_role != self._last_stream_role
            or current_stream_key != self._last_stream_key
        )

        status = _live_status_line(
            snapshot,
            transcript,
            session_description=session_description,
            branch_line=branch_line,
            cumulative_line=cumulative_line,
            width=width,
            activity_line=activity,
            show_detail=self._show_detail,
            color=self.color,
            prefix=False,
        )
        input_text = self._input_line_text()
        if input_text is None:
            # Keep native input untouched.  Status still follows activity and
            # resize changes because it occupies a separate row.
            if self._timeline_initialized and (status != self._last_status_line or force):
                self._redraw_status_only(status)
            self._last_request = request
            self._last_status_line = status
            return

        width_changed = self._last_rendered_width not in {None, width}
        if width_changed:
            # Reflowing a bounded transcript is a viewport change, not new
            # output. Render only entries added since the previous draw; old
            # rows become the new-width baseline without replaying history.
            history_new = self._entry_delta_rows(
                current_history_entries,
                self._last_history_entries,
                width,
                self.color,
            )
            # The stream tracker still describes the previous draw.  Render
            # its pending suffix at the new width instead of dropping it.

        # finish_stream() promotes the stream into one history entry.  The
        # entry has already been emitted chunk-by-chunk, so do not duplicate it
        # when the committed text matches the last streamed value.
        if history_new and previous_stream_emitted and stream_ended_or_switched:
            role = self._last_stream_role
            if role is not None and transcript.entries:
                # ``finish_stream`` may commit a tool tail and then append the
                # assistant's final result.  Locate the matching tail block,
                # not only the last entry, before deciding whether it was
                # already emitted above.
                streamed_entry = next(
                    (
                        entry
                        for entry in reversed(transcript.entries)
                        if entry.role == role
                        and (
                            entry.text.startswith(self._last_stream_text)
                            or self._last_stream_text.startswith(entry.text)
                        )
                    ),
                    None,
                )
                if streamed_entry is not None:
                    rendered_stream = tuple(
                        _entry_lines(streamed_entry, width, color=bool(self.color))
                    )
                    block_size = len(rendered_stream)
                    for index in range(len(history_new) - block_size + 1):
                        if history_new[index : index + block_size] == rendered_stream:
                            if streamed_entry.text.startswith(previous_stream_emitted):
                                suffix = streamed_entry.text[len(previous_stream_emitted) :]
                                replacement = (
                                    tuple(
                                        _entry_lines(
                                            TranscriptEntry(role=role, text=suffix),
                                            width,
                                            color=bool(self.color),
                                        )
                                    )
                                    if suffix
                                    else ()
                                )
                            elif previous_stream_emitted.startswith(streamed_entry.text):
                                replacement = ()
                            else:
                                continue
                            history_new = (
                                *history_new[:index],
                                *replacement,
                                *history_new[index + block_size :],
                            )
                            break
        timeline_new = (*history_new, *stream_new)
        if not self._timeline_initialized:
            self._append_timeline(timeline_new)
            self.stream.write(f"{_paint(status, _DIM_CYAN, self.color)}\n")
            self._write_input_blank()
            self._timeline_initialized = True
        elif timeline_new:
            self._move_to_status()
            self._append_timeline(timeline_new)
            self._write_status_and_input(status, input_text)
        elif status != self._last_status_line or width_changed or force:
            self._move_to_status()
            self._write_status_and_input(status, input_text)

        if self._input_active:
            self._restore_input_line(input_text, force=False)
        self.stream.flush()
        self._last_request = request
        self._last_status_line = status
        self._last_history_rows = history
        self._last_history_entries = current_history_entries
        self._last_stream_text = current_stream_text
        self._last_stream_role = current_stream_role
        self._last_stream_key = current_stream_key
        self._stream_emitted_text = stream_emitted
        self._last_rendered_width = width

    def set_input(self, text: str, cursor: int, *, paint: bool = True) -> None:
        """Update the event-loop-owned draft; native memory is never read."""
        self._managed_input = (text, cursor)
        self._managed_input_active = True
        if paint:
            self._restore_input_line(text, force=True)
            self.stream.flush()

    def flush(self) -> None:
        """Flush a deferred draw after input ownership ends."""
        if (
            not self.enabled
            or self._input_active
            or self._pending_draw is None
            or self._draw_in_flight
        ):
            return
        request = self._pending_draw
        self._pending_draw = None
        self._draw_in_flight = True
        try:
            self._draw_now(request, force=True)
        finally:
            self._draw_in_flight = False

    def draw_activity(self, activity_line: str) -> None:
        """Refresh the transient status row without touching timeline history."""
        if not self.enabled or self._last_request is None or self._draw_in_flight:
            return
        previous_size = self._last_size
        self._last_size = shutil.get_terminal_size((120, 40))
        activity = _sanitize(activity_line).replace(chr(10), " ")
        request = (*self._last_request[:6], activity)
        if self._input_active and (
            self._input_line_text() is None
            or (not self._native_input and not self._managed_input_active)
        ):
            self._pending_draw = request
            return
        self._draw_in_flight = True
        try:
            self._draw_now(request, force=previous_size != self._last_size)
        finally:
            self._draw_in_flight = False

    def move_to_input(self, *, label: str = "›", native: bool = False) -> None:
        if not self.enabled:
            return
        self._input_active = True
        self._input_owner = threading.get_ident()
        self._native_input = native
        label_text = _clip(_sanitize(label).replace(chr(10), " "), 8)
        self._input_prompt_label = label_text
        self._last_restored_input_text = None
        self._last_restored_input_label = label_text
        if self._timeline_initialized:
            self._write_input_blank()
            self._restore_input_line("", force=True)
        else:
            self.stream.write(f"{label_text} ")
        self.stream.flush()

    def hide_cursor(self, *, commit: bool = False) -> None:
        del commit
        # POSIX ``TerminalInput`` uses a managed draft while the legacy
        # readline path owns an echoed native line.  Only the latter leaves
        # the cursor one row below the prompt after Enter.
        readline_echoed = self._native_input and not self._managed_input_active
        self._input_active = False
        self._managed_input_active = False
        self._last_restored_input_text = None
        if not self.enabled:
            return
        if readline_echoed:
            # readline echoes Enter and leaves the cursor one row below the
            # input.  Return to that row before clearing the stale prompt.
            self.stream.write(f"\x1b[1A\r{_CLEAR_LINE}")
        elif self._timeline_initialized:
            self._write_input_blank()
        else:
            self.stream.write("\r\x1b[2K")
        self.stream.flush()


__all__ = [
    "ActivityState",
    "Cockpit",
    "Transcript",
    "TranscriptEntry",
    "render_quota_rows",
    "render_markdown_lines",
    "render_primary",
    "render_cockpit",
]
