"""Terminal presentation model for Cambium's interactive frontend.

The linear timeline is intentionally a presentation layer over immutable session and
observability snapshots.  It owns no provider, worker, branch, or context
state.  The only mutable value is a bounded local transcript used for the
operator's current terminal view.  Live output is appended to the terminal's
primary buffer so the terminal, rather than a private alternate screen, owns
scrollback.

``LinearTimeline`` appends timeline rows to the terminal primary buffer and keeps
only one transient status row plus one input row.  There is no alternate
screen, fixed frame, or side rail: terminal scrollback is the timeline.
"""

from __future__ import annotations

import json
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
from functools import lru_cache
from typing import Any, TextIO

from .render_markdown import render_markdown_lines as _shared_markdown_lines
from .terminal import (
    clip_terminal_text,
    sanitize_terminal_text,
    supports_cursor_controls,
    terminal_color_depth,
    terminal_display_width,
    terminal_grapheme_spans,
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
        "response_chunk",
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
        "response_chunk",
        "response.output_text.delta",
        "stream_chunk",
        "text_delta",
        "tool_message_delta",
        "tool_output_delta",
    }
)
_STREAM_TEXT_LIMIT = 16_384
# Supervisor transport frames are capped at 32 KiB.  Durable response chunks
# bypass the transient stream tail, but each retained entry must stay within
# that existing per-frame bound.
_RESPONSE_CHUNK_LIMIT_BYTES = 32 * 1024
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
    "path",
    "file_path",
    "paths",
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
_SPINNER_FRAMES = ("⠋", "⠙", "⠹", "⠸", "⠼", "⠴", "⠦", "⠧", "⠇", "⠏")
_ACTIVITY_PHASE_GLYPHS = {"thinking": "◌", "streaming": "▸", "waiting": "◒"}
_STALL_AFTER_S = 12.0
_ACTIVITY_TAIL_MAX_CHARS = 120
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

# Durable lifecycle events are concise timeline facts.  Heartbeats remain
# status-only and are intentionally absent from this set.
_CHILD_START_KINDS = frozenset(
    {
        "child_started",
        "child_start",
        "task_started",
        "task_start",
        "task_assigned",
        "spawned",
        "run_task",
    }
)
_CHILD_RESULT_KINDS = frozenset({"child_result", "child_succeeded", "child_completed"})
_PARENT_WAIT_KINDS = frozenset(
    {"parent_wait", "parent_waiting", "suspended_parent", "context_suspend"}
)
_PARENT_RESUME_KINDS = frozenset({"parent_resume", "parent_resumed", "context_resume"})
_PRIVATE_PAYLOAD_KEYS = frozenset(
    {
        "analysis",
        "analysis_content",
        "arguments",
        "chain_of_thought",
        "function_calls",
        "function_call",
        "reasoning",
        "reasoning_content",
        "thought",
        "thoughts",
        "tool_call",
        "tool_calls",
    }
)
_PRIVATE_ACTION_TYPES = frozenset(
    {
        "action",
        "apply_patch",
        "delegate",
        "finish",
        "function",
        "function_call",
        "plan",
        "read_file",
        "run_shell",
        "shell",
        "tool_call",
        "tool_use",
        "write_file",
    }
)
_PRIVATE_CONTENT_TYPES = _PRIVATE_ACTION_TYPES | frozenset({"analysis", "reasoning", "thinking"})


def _sanitize(value: Any) -> str:
    clean = sanitize_terminal_text(value)
    return clean.encode("utf-8", errors="backslashreplace").decode("utf-8")


def _single_line(value: Any) -> str:
    return sanitize_terminal_text(value, single_line=True).strip()


def _activity_tail(value: Any) -> str:
    if not isinstance(value, str):
        return ""
    clean = sanitize_terminal_text(value, single_line=True).strip()
    if _is_private_runtime_text(clean):
        return ""
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
    return terminal_display_width(text)


def _rendered_visible_offsets(rendered: str) -> tuple[str, list[int]]:
    """Return visible text and rendered offsets for each visible code point."""
    visible_chars: list[str] = []
    visible_offsets: list[int] = []
    index = 0
    while index < len(rendered):
        match = _ANSI_STYLE.match(rendered, index)
        if match is not None and _safe_sgr(match.group(0)):
            index = match.end()
            continue
        visible_chars.append(rendered[index])
        visible_offsets.append(index)
        index += 1
    return "".join(visible_chars), visible_offsets


def _take_first_grapheme(text: str) -> tuple[str, str]:
    """Split styled text after its first grapheme, retaining renderer SGR."""
    rendered = _safe_rendered(text)
    visible, visible_offsets = _rendered_visible_offsets(rendered)
    spans = terminal_grapheme_spans(visible)
    if not spans:
        return rendered, ""
    end = visible_offsets[spans[0][1] - 1] + 1
    return rendered[:end], rendered[end:]


def _take_display_width(text: str, width: int) -> tuple[str, str]:
    """Split styled text at a terminal column boundary without splitting graphemes."""
    rendered = _safe_rendered(text)
    if width <= 0:
        return "", rendered

    # Keep renderer-owned SGR sequences in the returned halves while asking
    # the shared terminal helper for grapheme boundaries and cell widths.
    visible, visible_offsets = _rendered_visible_offsets(rendered)
    spans = terminal_grapheme_spans(visible)
    if not spans:
        return rendered, ""
    if terminal_display_width(rendered) <= width:
        return rendered, ""

    used = 0
    for start, end, span_width in spans:
        if used and used + span_width > width:
            split = visible_offsets[start]
            return rendered[:split], rendered[split:]
        if not used and span_width > width:
            # Keep any leading SGR with the tail.  The caller can then render
            # the grapheme as a whole instead of receiving a partial cluster.
            return "", rendered
        used += span_width
        split = visible_offsets[end - 1] + 1
    return rendered[:split], rendered[split:]


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


def _fmt_secs(seconds: float) -> str:
    """Whole-second duration label; durations never render decimal delimiters."""
    return f"{int(seconds)}s"


def _human_count(value: int) -> str:
    if value < 1_000:
        return str(value)
    if value < 1_000_000:
        return f"{value / 1_000:.1f}".rstrip("0").rstrip(".") + "k"
    return f"{value / 1_000_000:.1f}".rstrip("0").rstrip(".") + "m"


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


def _is_private_runtime_text(value: str) -> bool:
    """Reject reasoning and raw action envelopes at the terminal boundary."""
    clean = value.strip()
    if not clean:
        return False
    if re.search(
        r'"type"\s*:\s*"(?:action|apply_patch|delegate|finish|function(?:_call)?|'
        r'plan|read_file|run_shell|shell|tool_(?:call|use)|write_file)"',
        clean,
        re.IGNORECASE,
    ) or re.search(
        r'"(?:analysis|arguments|calls|chain[_ -]?of[_ -]?thought|reasoning|thought|'
        r'tool[_ -]?call|function[_ -]?call)"\s*:',
        clean,
        re.IGNORECASE,
    ):
        return True
    if not clean.startswith(("{", "[")):
        return False
    try:
        parsed = json.loads(clean)
    except (TypeError, ValueError, json.JSONDecodeError):
        return False
    return _is_private_payload(parsed)


def _is_private_payload(value: Any) -> bool:
    """Return whether a payload contains private reasoning or an action envelope."""
    if isinstance(value, Mapping):
        keys = {str(key).casefold().replace("-", "_") for key in value}
        if keys & _PRIVATE_PAYLOAD_KEYS:
            return True
        for key in ("type", "kind", "event"):
            kind = value.get(key)
            if (
                isinstance(kind, str)
                and kind.casefold().replace("-", "_") in _PRIVATE_CONTENT_TYPES
            ):
                return True
        return any(_is_private_payload(item) for item in value.values())
    if isinstance(value, list | tuple):
        return any(_is_private_payload(item) for item in value)
    return isinstance(value, str) and _is_private_runtime_text(value)


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
        if _is_private_payload(data):
            return None
        text = _result_text(data)
        if text is None or _is_private_runtime_text(text):
            return None
        return "assistant", text, False, None
    if kind in _ASSISTANT_STREAM_KINDS or kind in _TOOL_STREAM_KINDS:
        tool_status = _tool_status(data)
        if kind == "tool_event" and tool_status is not None and not tool_status:
            return None
        role = _message_role(kind, data)
        if role is None:
            return None
        if _is_private_payload(data):
            return None
        text = _text_value(data)
        if not text or _is_private_runtime_text(text):
            return None
        append = kind in _STREAM_DELTA_KINDS
        if data.get("cumulative") or data.get("replace"):
            append = False
        if data.get("append"):
            append = True
        message_id = data.get("message_id") or data.get("id") or record.get("request_id")
        return role, text, append, message_id if isinstance(message_id, str) else None
    return None


def _response_stream_key(record: Mapping[str, Any]) -> str | None:
    """Return a bounded internal key for one response stream identity."""
    task_id = record.get("task_id")
    generation = record.get("generation")
    request_id = record.get("request_id")
    if (
        isinstance(task_id, str)
        and task_id
        and type(generation) is int
        and generation > 0
        and isinstance(request_id, str)
        and request_id
    ):
        return repr((task_id, generation, request_id))
    return request_id if isinstance(request_id, str) and request_id else None


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
        if _is_private_runtime_text(value):
            return None
        return value
    if _is_private_payload(value):
        return None
    text = _text_value(value)
    if text is not None:
        if _is_private_runtime_text(text):
            return None
        return text
    if isinstance(value, list | tuple):
        parts = [part for item in value if (part := _tool_detail_value(item))]
        return " ".join(parts) if parts else None
    if isinstance(value, Mapping):
        return None
    return _sanitize(value)


def _tool_entry_text(
    data: Mapping[str, Any],
    tool: str,
    ok: bool | None,
    duration_ms: int | float | None,
) -> str:
    tool = _safe_bounded_text(tool, 72) or "tool"
    state = "ok" if ok else "failed" if ok is not None else "done"
    duration = f" · {_format_duration(duration_ms)}" if duration_ms is not None else ""
    lines = [f"{tool}: {state}{duration}"]
    for key in _TOOL_DETAIL_KEYS:
        detail = _tool_detail_value(data.get(key))
        if detail:
            lines.append(f"{key}: {_clip(_single_line(detail), 512)}")
    return "\n".join(lines)


def _safe_bounded_text(value: Any, limit: int) -> str | None:
    """Classify the complete value before clipping it for a terminal field."""
    if not isinstance(value, str) or not value.strip():
        return None
    clean = _single_line(value)
    if not clean or _is_private_runtime_text(clean):
        return None
    return _clip(clean, limit)


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
    name = _safe_bounded_text(entry.tool_name, 72) or "?"
    owner = _safe_bounded_text(entry.owner_task_id, 72)
    if owner:
        name = f"{owner}/{name}"
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
    owner_task_id: str | None = None


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
        task_id = _safe_bounded_text(value, 72)
        if task_id:
            return task_id
    return None


def _event_turn(data: Mapping[str, Any]) -> int | None:
    value = data.get("turn")
    return value if type(value) is int and value >= 0 else None


def _event_field(data: Mapping[str, Any], *keys: str, limit: int = 96) -> str | None:
    """Return one bounded scalar event field; nested payloads stay private."""
    for key in keys:
        value = data.get(key)
        clean = _safe_bounded_text(value, limit)
        if clean:
            return clean
        if type(value) in (int, float) and not isinstance(value, bool):
            return _clip(_single_line(value), limit)
    return None


def _event_metadata(data: Mapping[str, Any]) -> dict[str, str]:
    """Collect safe status metadata without copying reasoning or action JSON."""
    metadata: dict[str, str] = {}
    aliases = {
        "provider": ("assigned_provider", "provider"),
        "model": ("model",),
        "tool": ("tool", "tool_name"),
        "cmd": ("cmd", "command"),
        "path": ("path", "file_path"),
        "rate": ("output_tokens_per_s", "tokens_per_s", "out_per_s"),
        "phase": ("phase",),
    }
    for name, keys in aliases.items():
        value = _event_field(data, *keys)
        if value is not None:
            metadata[name] = value
    if "path" not in metadata:
        paths = data.get("paths")
        if isinstance(paths, list | tuple):
            for path in paths:
                clean = _safe_bounded_text(path, 96)
                if clean:
                    metadata["path"] = clean
                    break
    duration = _event_field(data, "duration_ms")
    if duration is not None:
        metadata["elapsed"] = _format_duration(_usage_float(duration))
    else:
        elapsed = _event_field(data, "elapsed_s", "elapsed")
        if elapsed is not None:
            number = _usage_float(elapsed, default=-1.0)
            metadata["elapsed"] = f"{number:g}s" if number >= 0 else elapsed
    return metadata


def _child_id(record: Mapping[str, Any], data: Mapping[str, Any]) -> str:
    value = data.get("child_task_id") or data.get("child_id")
    child_id = _safe_bounded_text(value, 72)
    if child_id:
        return child_id
    value = record.get("task_id") or data.get("task_id")
    child_id = _safe_bounded_text(value, 72)
    if child_id:
        return child_id
    return "child"


def _child_descriptor(record: Mapping[str, Any], data: Mapping[str, Any]) -> str:
    child = _child_id(record, data)
    parts = [child]
    task = _event_field(data, "task", "description", "child_task", limit=120)
    if task:
        parts.append(f"task={task}")
    provider = _event_field(data, "assigned_provider", "provider")
    model = _event_field(data, "model")
    if provider or model:
        parts.append(f"provider={provider or '?'}" if not model else f"{provider or '?'}/{model}")
    return " · ".join(parts)


def _failure_context_line(kind: str, data: Mapping[str, Any]) -> str | None:
    """Return a short, safe line for a failure's preceding-event context."""
    tool_status = _tool_status(data)
    if kind == "tool_event" and tool_status is not None and not tool_status:
        tool = data.get("tool") or data.get("tool_name")
        clean_tool = _safe_bounded_text(tool, 72)
        if not clean_tool:
            return "tool failed"
        line = f"{clean_tool}: failed"
        for key in _TOOL_DETAIL_KEYS:
            detail = _tool_detail_value(data.get(key))
            if detail:
                line += f" · {key}: {detail}"
        return _sanitize(line)

    if kind == "timeout":
        phase = _event_field(data, "phase")
        return f"timeout: {phase}" if phase else "timeout"

    if kind == "restart_scheduled":
        count = data.get("restart_count")
        maximum = data.get("max_restarts")
        if type(count) is int and type(maximum) is int:
            return f"restart scheduled: {count}/{maximum}"
        return "restart scheduled"

    if kind == "usage_event":
        reason = _event_field(data, "failure_reason", limit=160)
        if reason:
            return f"provider call failed: {reason}"

    if kind == "protocol":
        detail = _event_field(data, "note", "error_type", limit=160)
        if detail:
            return f"protocol: {detail}"

    if kind == "log" and data.get("stream") == "worker-error":
        detail = _event_field(data, "message", "error_type", limit=160)
        if detail:
            return f"worker error: {detail}"
    return None


def _failure_cause(kind: str, data: Mapping[str, Any]) -> str | None:
    """Extract the most actionable failure cause from one terminal event."""
    cause: str | None = None
    for key in ("failure_reason", "reason", "message", "error"):
        value = data.get(key)
        if isinstance(value, str) and value.strip() and not _is_private_runtime_text(value):
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
            task_id = _safe_bounded_text(pair.group(1), 72)
            cause = pair.group(3)

    if task_id is None:
        task_match = re.search(r"\btask(?:_id)?=([^\s]+)", clean)
        if task_match is not None:
            task_id = _safe_bounded_text(task_match.group(1).strip("'\""), 72)
    if cause is None:
        reason = re.search(r"\b(?:failure_reason|reason)=((['\"])(.*?)\2|[^\s]+)", clean)
        if reason is not None:
            cause = reason.group(3) if reason.group(3) is not None else reason.group(1)
            if cause is not None:
                cause = cause.strip("'\"")
    if cause:
        cause = _sanitize(cause).strip()
        if _is_private_runtime_text(cause):
            cause = None
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
        self._stream_owner_task_id: str | None = None
        self._stream_tool_name: str | None = None
        self._turn_serial = 0
        self._turn_by_task: dict[str, int] = {}
        self._failure_context: dict[tuple[str, int | None, int], list[str]] = {}
        self._failure_blocks: dict[tuple[str, int | None, int], _FailureBlock] = {}
        self._failure_order: deque[tuple[str, int | None, int]] = deque()
        self._tool_failure_key: int | None = None
        self._tool_failure_count = 0
        self._tool_error_total = 0
        self._tool_count = 0
        self._turn_tool_count = 0
        self._last_tool_name: str | None = None
        self._last_tool_duration_ms: int | float | None = None
        self._tool_details_expanded = False
        self._last_event_task_id: str | None = None
        self._last_event_metadata: dict[str, str] = {}

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
        self._tool_count = 0
        self._turn_tool_count = 0
        self._last_tool_name = None
        self._last_tool_duration_ms = None
        self._last_event_task_id = None
        self._last_event_metadata.clear()

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
        if clean and not _is_private_runtime_text(clean):
            self._entries.append(TranscriptEntry(role=role, text=clean))

    def user(self, text: str) -> None:
        self._turn_serial += 1
        self._turn_by_task.clear()
        self._tool_failure_key = None
        self._tool_failure_count = 0
        self._turn_tool_count = 0
        self._last_tool_name = None
        self._last_tool_duration_ms = None
        self._last_event_task_id = None
        self._last_event_metadata.clear()
        self._clear_stream()
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
    def status_metadata(self) -> Mapping[str, str]:
        """Return the latest safe event metadata for the transient status row."""
        return self._last_event_metadata

    @property
    def status_task_id(self) -> str | None:
        return self._last_event_task_id

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
        return _safe_bounded_text(value, 72) or ""

    @staticmethod
    def _event_tool_id(data: Mapping[str, Any]) -> str | None:
        """Return the wire identity for one tool stream when it is present."""
        for key in ("tool_call_id", "tool_id", "call_id", "request_id", "id"):
            value = data.get(key)
            tool_id = _safe_bounded_text(value, 72)
            if tool_id:
                return tool_id
        return None

    def _clear_stream(self) -> None:
        self._stream_role = None
        self._stream_text = ""
        self._stream_message_id = None
        self._stream_truncated = False
        self._stream_tool_key = None
        self._stream_owner_task_id = None
        self._stream_tool_name = None

    def _commit_stream(self) -> None:
        if (
            self._stream_role is not None
            and self._stream_text
            and not _is_private_runtime_text(self._stream_text)
        ):
            text = self._stream_text
            if self._stream_role == "tool" and self._stream_tool_name:
                text = f"[{self._stream_tool_name}] {text}"
            self._entries.append(
                TranscriptEntry(
                    role=self._stream_role,
                    text=_sanitize(text).strip("\n"),
                    owner_task_id=self._stream_owner_task_id,
                )
            )
        self._clear_stream()

    def _bounded_stream_text(self, text: str) -> str:
        if len(text) <= _STREAM_TEXT_LIMIT:
            self._stream_truncated = False
            return text
        self._stream_truncated = True
        return "…\n" + text[-(_STREAM_TEXT_LIMIT - 2) :]

    def _append_response_chunk(
        self, role: str, text: str, *, stream_key: str | None = None
    ) -> None:
        """Append one bounded durable response frame to normal timeline history.

        Durable response chunks are already redacted and split by the
        supervisor.  Keep their frame boundaries instead of joining them into
        the 16 KiB transient stream tail; the transcript deque still bounds
        total retained memory.
        """
        try:
            if len(text.encode("utf-8")) > _RESPONSE_CHUNK_LIMIT_BYTES:
                return
        except UnicodeEncodeError:
            return
        clean = _sanitize(text)
        if not clean or _is_private_runtime_text(clean):
            return
        self._append_response_entry(role, clean, stream_key=stream_key)

    def _append_response_entry(
        self, role: str, text: str, *, stream_key: str | None = None
    ) -> None:
        if self._stream_role == "tool":
            self._commit_stream()
        elif self._stream_role is not None:
            # A durable response is canonical for its assistant stream. Keep a
            # concurrent unrelated stream instead of dropping its transient
            # text when response identities overlap in one replayed turn.
            same_stream = stream_key is None or self._stream_message_id == stream_key
            if role == "assistant" and not same_stream:
                self._commit_stream()
            else:
                self._clear_stream()
        self._entries.append(TranscriptEntry(role=role, text=text))

    def _append_validated_response(self, text: str, *, stream_key: str | None = None) -> None:
        """Append one supervisor-validated durable response prefix to history."""
        clean = _sanitize(text)
        if clean and not _is_private_runtime_text(clean):
            self._append_response_entry("assistant", clean, stream_key=stream_key)

    def _update_stream(
        self,
        role: str,
        text: str,
        *,
        append: bool,
        message_id: str | None,
        tool_key: str | None = None,
        owner_task_id: str | None = None,
        tool_name: str | None = None,
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
        self._stream_owner_task_id = owner_task_id
        self._stream_tool_name = tool_name

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
        tool_name = self._stream_tool_name
        owner_task_id = self._stream_owner_task_id
        truncated = self._stream_truncated
        self._clear_stream()
        final = _sanitize(final_text).strip("\n") if isinstance(final_text, str) else ""
        if final and _is_private_runtime_text(final):
            return
        summary_failure = _failure_summary(final) if final else None
        if summary_failure is not None:
            task_id, cause = summary_failure
            self._record_failure(task_id, None, cause)
            # The detailed task/cause/context is already in the red failure
            # block. Never render the worker's failure summary as model text.
            return
        if not final:
            if current and role is not None and not _is_private_runtime_text(current):
                text = f"[{tool_name}] {current}" if role == "tool" and tool_name else current
                self._entries.append(
                    TranscriptEntry(role=role, text=text, owner_task_id=owner_task_id)
                )
            return
        if current and role == "assistant" and not truncated:
            if final.startswith(current) or current.startswith(final):
                final = final if len(final) >= len(current) else current
            elif current != final:
                final = f"{current}\n{final}"
        elif current and role is not None:
            if not _is_private_runtime_text(current):
                text = f"[{tool_name}] {current}" if role == "tool" and tool_name else current
                self._entries.append(
                    TranscriptEntry(role=role, text=text, owner_task_id=owner_task_id)
                )
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
        del turn
        self._tool_error_total += 1
        key = self._turn_serial
        if self._tool_failure_key != key:
            self._tool_failure_key = key
            self._tool_failure_count = 0
        self._tool_failure_count += 1
        name = _safe_bounded_text(tool, 72) or "tool"
        self._entries.append(
            TranscriptEntry(
                role="tool",
                text=f"{name}: failed",
                tool_name=name,
                tool_ok=False,
                owner_task_id=task_id,
            )
        )

    def _remember_tool_activity(self, tool: Any, duration: int | float | None) -> None:
        self._tool_count += 1
        self._turn_tool_count += 1
        self._last_tool_name = (
            _safe_bounded_text(tool, 72) or "tool"
        )
        self._last_tool_duration_ms = duration

    def _clear_tool_failure_notice(self, task_id: str | None, turn: int | None) -> None:
        del task_id, turn
        self._tool_failure_key = None
        self._tool_failure_count = 0

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

    def observe_event(
        self, record: Mapping[str, Any], *, suppress_assistant_stream: bool = False
    ) -> None:
        """Promote only operator-relevant runtime events into the transcript."""
        kind = record.get("kind")
        if not isinstance(kind, str):
            return
        data = _event_data(record)
        task_id = _task_id(record, data)
        turn = _event_turn(data)
        self._remember_turn(task_id, turn)
        self._last_event_task_id = task_id
        metadata = _event_metadata(data)
        for name, keys in (
            ("provider", ("provider", "assigned_provider")),
            ("model", ("model",)),
            ("tool", ("tool", "tool_name")),
            ("cmd", ("cmd", "command")),
            ("path", ("path", "file_path", "paths")),
            ("elapsed", ("elapsed_s", "elapsed", "duration_ms")),
            ("rate", ("output_tokens_per_s", "tokens_per_s", "out_per_s")),
            ("phase", ("phase",)),
        ):
            if not any(key in data for key in keys):
                if name in {"tool", "cmd", "path", "elapsed"}:
                    self._last_event_metadata.pop(name, None)
                continue
            value = metadata.get(name)
            if value:
                self._last_event_metadata[name] = value
            else:
                self._last_event_metadata.pop(name, None)

        if kind == "tool_event" and self._stream_role == "tool":
            owner = task_id or "?"
            tool_name = self._event_tool(data) or ""
            tool_id = self._event_tool_id(data) or ""
            active_tool_key = f"{owner}:run:{tool_name}:{tool_id}"
            if self._stream_tool_key == active_tool_key:
                self._commit_stream()

        update = _stream_update(record)
        if kind == "response_chunk":
            if update is not None:
                role, text, _, _ = update
                self._append_response_chunk(
                    role,
                    text,
                    stream_key=_response_stream_key(record),
                )
            return
        if suppress_assistant_stream and update is not None and update[0] == "assistant":
            update = None
        if update is not None:
            role, text, append, message_id = update
            if role == "assistant":
                message_id = _response_stream_key(record) or message_id
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
                owner_task_id=task_id if role == "tool" else None,
                tool_name=self._event_tool(data) if role == "tool" else None,
            )

        if kind == "tool_event":
            tool = self._event_tool(data)
            ok = _tool_status(data)
            duration = _duration_ms(data.get("duration_ms"))
            self._remember_tool_activity(tool, duration)
            if ok is not None and not ok:
                context_line = _failure_context_line(kind, data)
                if context_line is not None:
                    self._remember_failure_context(task_id, turn, context_line)
                self._remember_tool_failure(task_id, turn, tool)
                return
            if tool:
                text = _sanitize(_tool_entry_text(data, tool, ok, duration)).strip("\n")
                if text:
                    self._entries.append(
                        TranscriptEntry(
                            role="tool",
                            text=text,
                            tool_name=tool,
                            tool_ok=ok,
                            duration_ms=duration,
                            owner_task_id=task_id,
                        )
                    )
            return

        if kind in {"child_admitted", "child_rejected"}:
            message = f"{kind.replace('_', ' ')}: {_child_descriptor(record, data)}"
            reason = _event_field(data, "reason", limit=96)
            if reason:
                message += f" · {reason}"
            detail = _event_field(data, "message", limit=160)
            if detail:
                message += f" · {detail}"
            self.system(_clip(message, 320))
            return

        if kind in _CHILD_START_KINDS and (
            data.get("child_task_id") is not None
            or data.get("parent_task_id") is not None
            or kind.startswith("child")
        ):
            message = f"child task started: {_child_descriptor(record, data)}"
            self.system(_clip(message, 320))
            return

        if kind in _CHILD_RESULT_KINDS:
            status = _event_field(data, "status") or "done"
            message = f"child {_child_id(record, data)}: {status}"
            summary = _event_field(data, "summary", "failure_reason", "reason", limit=160)
            if summary:
                message += f" · {summary}"
            self.system(_clip(message, 320))
            return

        if kind == "child_failed":
            reason = _event_field(data, "reason", "failure_reason", limit=160)
            message = f"child {_child_id(record, data)} failed"
            if reason:
                message += f" · {reason}"
            self.system(_clip(message, 320))
            self._remember_failure_context(task_id, turn, _sanitize(message))
            return

        if kind in _PARENT_WAIT_KINDS or (
            kind == "result"
            and (_event_field(data, "status") or "").casefold() == "suspended"
        ):
            child_count = _event_field(data, "child_count")
            message = "parent waiting for children"
            if task_id:
                message += f" · task={task_id}"
            if child_count:
                message += f" · children={child_count}"
            self.system(_clip(message, 240))
            return

        if kind in _PARENT_RESUME_KINDS:
            child_count = _event_field(data, "child_count")
            message = "parent resumed"
            if task_id:
                message += f" · task={task_id}"
            if child_count:
                message += f" · children={child_count}"
            epoch = _event_field(data, "epoch")
            if epoch:
                message += f" · epoch={epoch}"
            self.system(_clip(message, 240))
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
            sha = _event_field(data, "merge_sha", "commit", "sha", limit=12)
            self.system(f"repository integrated{f' · {sha}' if sha else ''}")


class ActivityState:
    """Small mutable view of the work currently keeping a turn busy."""

    def __init__(self) -> None:
        self._active = False
        self._finished = False
        self._state = "IDLE"
        self._turn_started_at = 0.0
        self._spinner_index = 0
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
        self._spinner_index = 0
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
        """Record a terminal state before the final status redraw."""
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
            clean = _safe_bounded_text(value, 72)
            if clean:
                return clean
        function = data.get("function")
        if isinstance(function, Mapping):
            value = function.get("name")
            clean = _safe_bounded_text(value, 72)
            if clean:
                return clean
        return "tool"

    @staticmethod
    def _tool_id(data: Mapping[str, Any]) -> str | None:
        for key in ("tool_call_id", "tool_id", "call_id", "request_id", "id"):
            value = data.get(key)
            tool_id = _safe_bounded_text(value, 72)
            if tool_id:
                return tool_id
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
        clean_provider = _safe_bounded_text(provider, 96)
        if clean_provider:
            self._provider = clean_provider
        clean_model = _safe_bounded_text(model, 96)
        if clean_model:
            self._model = clean_model
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
                _safe_bounded_text(provider, 96),
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
            self._spinner_index = (self._spinner_index + 1) % len(_SPINNER_FRAMES)
        current = time.monotonic() if now is None else now
        turn_elapsed = max(0.0, current - self._turn_started_at)
        quiet_for = max(0.0, current - self._last_progress_at)
        spinner = _SPINNER_FRAMES[self._spinner_index]

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
                _, tail = _take_first_grapheme(remaining)
                if tail == remaining:
                    tail = remaining[1:]
                head = "?"
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
                    # Keep this helper bounded for direct callers too.
                    _, tail = _take_first_grapheme(chunk)
                    if tail == chunk:
                        tail = chunk[1:]
                    head = "?"
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
    if _is_private_runtime_text(entry.text):
        return []
    label = _ROLE_LABELS[entry.role]
    owner = _safe_bounded_text(entry.owner_task_id, 72)
    if entry.role == "tool" and owner and entry.tool_name is None:
        label = f"{label}[{owner}]"
    label_prefix = f"{label} ▸ "
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
    if role in {"tool", "dim"} or text.lstrip().startswith(("✓ ", "✗ ", "• ")):
        return "tool"
    return role


def _side_clean(value: Any) -> str:
    """Return one terminal-safe, single-line field value."""
    clean = _sanitize(value).replace("\n", " ")
    return "" if _is_private_runtime_text(clean) else clean


def _quota_row(kind: str, text: Any, width: int) -> tuple[str, str]:
    """Build one width-safe quota row."""
    return kind, clip_terminal_text(_side_clean(text), max(1, width))


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
            rows.append(_quota_row("normal", full, panel_width))
        elif terminal_display_width(compact) <= panel_width:
            rows.append(_quota_row("normal", compact, panel_width))
        else:
            rows.append(_quota_row("normal", f" {subject}", panel_width))
            rows.extend(_quota_row("dim", f"   {field}", panel_width) for field in fields)
    return rows


def render_quota_rows(snapshot: Any, width: int = 44) -> list[str]:
    """Return width-safe quota rows for explicit timeline inspection."""
    return [text for _, text in _quota_rows(snapshot, width)]


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


def _active_agent(snapshot: Any, task_id: str | None = None) -> Any | None:
    """Select the lane that owns the latest event, then the active main lane."""
    agents = tuple(getattr(snapshot, "agents", ()))
    if task_id:
        for agent in agents:
            if _side_clean(getattr(agent, "task_id", "")).strip() == task_id:
                return agent
    selected = getattr(snapshot, "selected_task_id", None)
    if isinstance(selected, str):
        for agent in agents:
            if getattr(agent, "task_id", None) == selected:
                return agent
    active = {"starting", "active", "merging"}
    main = next((agent for agent in agents if getattr(agent, "role", "") == "main"), None)
    if main is not None and _side_clean(getattr(main, "state", "")).casefold() in active:
        return main
    return next(
        (
            agent
            for agent in agents
            if _side_clean(getattr(agent, "state", "")).casefold() in active
        ),
        main or (agents[-1] if agents else None),
    )


def _status_fields(
    snapshot: Any,
    *,
    session_description: str,
    branch_line: str,
    cumulative_line: str,
    transcript: Transcript | None = None,
) -> dict[str, str]:
    """Collect bounded status facts from snapshots and safe event metadata."""
    fields: dict[str, str] = {}
    for source in (session_description, branch_line, cumulative_line):
        clean = _sanitize(source).replace("\n", " ")
        if _is_private_runtime_text(clean):
            continue
        for match in re.finditer(r"(?<![\w/])([\w/]+)=([^\s·]+)", clean):
            key, value = match.groups()
            if key in _STATUS_KEYS:
                candidate = _safe_bounded_text(value, 96)
                if candidate:
                    fields.setdefault(key, candidate)

    task_id = transcript.status_task_id if transcript is not None else None
    lane = _active_agent(snapshot, task_id)
    if lane is not None:
        task = _safe_bounded_text(getattr(lane, "task_id", ""), 72)
        if task:
            fields["owner"] = task
        for name, key in (("provider", "provider"), ("model", "model"), ("tool", "tool")):
            value = getattr(lane, key, None)
            clean = _safe_bounded_text(value, 96)
            if clean:
                fields[name] = clean
        turn = getattr(lane, "turn", None)
        if type(turn) is int and turn >= 0:
            fields["turn"] = str(turn)
        fields.setdefault(
            "tokens",
            _human_count(
                _usage_int(getattr(lane, "total_tokens", getattr(snapshot, "total_tokens", 0)))
            ),
        )
        fields.setdefault(
            "calls", str(_usage_int(getattr(lane, "calls", getattr(snapshot, "calls", 0))))
        )
        rate = getattr(lane, "output_tokens_per_s", None)
        if isinstance(rate, int | float) and math.isfinite(float(rate)):
            fields["out/s"] = f"{float(rate):.1f}"
    context = getattr(snapshot, "context", None)
    if context is not None:
        fields["epoch"] = str(_usage_int(getattr(context, "epoch", 0)))
        checkpoint = getattr(context, "checkpoint_ref", None)
        clean = _safe_bounded_text(checkpoint, 96)
        if clean:
            fields.setdefault("checkpoint", clean)
    fields.setdefault("tokens", _human_count(_usage_int(getattr(snapshot, "total_tokens", 0))))
    fields.setdefault("out/s", f"{_usage_float(getattr(snapshot, "output_tokens_per_s", 0.0)):.1f}")
    fields.setdefault("rate", fields.get("out/s", ""))
    if transcript is not None:
        for key, value in transcript.status_metadata.items():
            clean = _safe_bounded_text(value, 96)
            if clean:
                fields[key] = clean
    return fields


def _activity_status(snapshot: Any, activity_line: str) -> str:
    clean = _single_line(activity_line)
    if _is_private_runtime_text(clean):
        clean = ""
    if clean:
        return clean
    status = _side_clean(getattr(snapshot, "session_status", "idle")).casefold()
    if status in {"done", "ended", "succeeded", "complete", "completed"}:
        return "✓ done"
    if status in {"error", "failed", "failure"}:
        return "✗ error"
    if not getattr(snapshot, "active_agents", 0):
        if getattr(snapshot, "failed_agents", 0):
            return "✗ error"
        if getattr(snapshot, "succeeded_agents", 0):
            return "✓ done"
    return "⠋ orchestrating" if getattr(snapshot, "active_agents", 0) else "⠋ idle"


def _status_activity(activity_line: str, color: bool) -> str:
    clean = _safe_rendered(activity_line)
    match = _STATUS_PHASE_RE.match(clean)
    if match is None:
        return clean
    style = _STATUS_PHASE_STYLES.get(match.group(3).casefold())
    if style is None:
        return clean
    return (
        f"{match.group(1)}{match.group(2)}"
        f"{_status_paint(match.group(3), style, color)}{match.group(4)}"
    )


def _short_model(value: Any) -> str:
    return _side_clean(value).strip().replace("/", "-") or "?"


def _compact_checkpoint(value: str) -> str:
    clean = _side_clean(value).strip().rstrip("/")
    if not clean or clean == "none":
        return "none"
    filename = clean.rsplit("/", 1)[-1]
    hashes = re.findall(r"(?i)(?<![a-z0-9])[0-9a-f]{9,}(?![a-z0-9])", filename)
    return hashes[0][:8] if hashes else filename


def _status_line(
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
    fields = _status_fields(
        snapshot,
        session_description=session_description,
        branch_line=branch_line,
        cumulative_line=cumulative_line,
        transcript=transcript,
    )
    activity_source = _activity_status(snapshot, activity_line)
    activity = _status_activity(activity_source, color)
    parts = [activity]
    provider = _side_clean(fields.get("provider", ""))
    model = _side_clean(fields.get("model", ""))
    if provider or model:
        parts.append(
            _status_paint(f"{provider or '?'}/{_short_model(model or '?')}", "cyan", color)
        )
    if transcript is not None and transcript.current_tool_error_count:
        parts.append(f"err{transcript.current_tool_error_count}")
    if token_count := fields.get("tokens"):
        parts.append(_status_paint(f"{token_count} tok", "dim", color))
    if owner := _side_clean(fields.get("owner", "")):
        parts.append(f"owner={owner}")
    if turn := fields.get("turn"):
        parts.append(f"t{_side_clean(turn)}")
    if tool := fields.get("tool"):
        parts.append(f"tool={_side_clean(tool)}")
    for key, label in (("cmd", "cmd"), ("path", "path"), ("elapsed", "elapsed"), ("rate", "rate")):
        value = fields.get(key)
        if value:
            parts.append(f"{label}={_side_clean(value)}")
    calls = _usage_int(fields.get("calls"))
    if calls:
        parts.append(f"{calls} calls")
    if show_detail:
        agent_count = len(getattr(snapshot, "agents", ()))
        active_count = _usage_int(getattr(snapshot, "active_agents", 0))
        if agent_count:
            parts.append(f"agents={active_count}/{agent_count}")
        epoch = fields.get("epoch")
        if epoch:
            parts.append(f"ctx=e{_side_clean(epoch)}")
        for key, label in (("checkpoint", "ckpt"), ("cost", "cost")):
            value = fields.get(key)
            if value:
                rendered = _compact_checkpoint(value) if key == "checkpoint" else _side_clean(value)
                parts.append(f"{label}={rendered}")
    width = max(1, width)
    prefix_text = "Cambium · " if prefix else ""
    visible_parts: list[str] = []
    for part in parts:
        candidate = " · ".join((*visible_parts, part))
        if _display_width(prefix_text + candidate) <= width:
            visible_parts.append(part)
            continue
        if visible_parts:
            break
        visible_parts.append(part)
    return _clip(prefix_text + " · ".join(visible_parts), width)


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


def _stream_entry_content(entry: TranscriptEntry) -> str:
    """Return stream content without the presentation-only tool label."""
    text = entry.text
    if entry.role == "tool" and text.startswith("["):
        separator = text.find("] ")
        if separator > 0:
            return text[separator + 2 :]
    return text


class LinearTimeline:
    """Append-only primary-buffer terminal presentation.

    The terminal owns the timeline and its scrollback.  Only the final two
    rows are transient: one status row and one input row.  A redraw replaces
    those rows in place, then leaves the cursor on the input row so new output
    follows the terminal's normal bottom-scroll behaviour.
    """

    _Request = tuple[Any, Transcript, str, str, str, str, str]

    def __init__(self, stream: TextIO, *, enabled: bool = True) -> None:
        self.stream = stream
        # Keep the line renderer usable for direct draws to pipes and dumb
        # terminals, but reserve cursor choreography for terminals that expose
        # it explicitly.  The interactive TUI only constructs this class for
        # capable terminals; direct callers still get safe plain lines.
        self.enabled = bool(enabled)
        self._cursor_controls = self.enabled and supports_cursor_controls(stream)
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
        self._pending_draw: LinearTimeline._Request | None = None
        self._last_request: LinearTimeline._Request | None = None
        self._timeline_initialized = False
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

    def __enter__(self) -> LinearTimeline:
        if self.enabled and self._cursor_controls:
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
                self.stream.write("\r\n" if self._cursor_controls else "\n")
                self.stream.flush()
            else:
                self.flush()
                # Commit the blank input row and return to the next prompt or
                # shell line.  No alternate screen or full-frame cleanup is
                # needed because all history already belongs to scrollback.
                self.stream.write("\r\n" if self._cursor_controls else "\n")
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
        rendered_complete = complete
        if role == "tool" and transcript._stream_tool_name:
            rendered_complete = f"[{transcript._stream_tool_name}] {complete}"
        rows = tuple(
            _entry_lines(
                TranscriptEntry(
                    role=role,
                    text=rendered_complete,
                    owner_task_id=transcript._stream_owner_task_id,
                ),
                width,
                color=bool(color),
            )
        )
        return rows, emitted + complete

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
        spans = terminal_grapheme_spans(text)
        # The editor normally keeps the cursor on a grapheme boundary. Clamp
        # defensively when display sanitization changes a code-point index.
        cursor = min(cursor, len(text))
        for span_start, span_end, _span_width in spans:
            if span_start < cursor < span_end:
                cursor = span_start
                break
        start, cells = cursor, 0
        for span_start, span_end, span_width in reversed(spans):
            if span_end > cursor:
                continue
            if cells + span_width >= room - 1:
                break
            start = span_start
            cells += span_width
        marker = "‹" if start else ""
        rendered = _clip(marker + text[start:], room)
        cursor_cells = _display_width(marker + text[start:cursor])
        if (
            not force
            and self._last_restored_input_text == text
            and self._last_restored_input_label == label
        ):
            return
        if not self._cursor_controls:
            # Without cursor addressing, input is line-oriented.  Keep each
            # draft readable and avoid carriage-return/erase sequences.
            self.stream.write(f"{label} {rendered}\n")
            self._last_restored_input_text = text
            self._last_restored_input_label = label
            return
        self.stream.write(f"\r{_CLEAR_LINE}{label} {rendered}")
        back = _display_width(rendered) - cursor_cells
        if back > 0:
            self.stream.write(f"\x1b[{back}D")
        self._last_restored_input_text = text
        self._last_restored_input_label = label

    def _write_input_blank(self) -> None:
        if not self._cursor_controls:
            return
        self.stream.write(f"\r{_CLEAR_LINE}")

    def _move_to_status(self) -> None:
        if self._cursor_controls and self._timeline_initialized:
            self.stream.write("\r\x1b[1A")

    def _write_status_and_input(self, status: str, input_text: str = "") -> None:
        rendered = _paint(status, _DIM_CYAN, self.color)
        if self._cursor_controls:
            self.stream.write(f"\r{_CLEAR_LINE}{rendered}\n")
        else:
            self.stream.write(f"{rendered}\n")
        self._write_input_blank()
        if self._input_active:
            self._restore_input_line(input_text, force=True)

    def _redraw_status_only(self, status: str) -> None:
        if not self._timeline_initialized:
            return
        if not self._cursor_controls:
            self.stream.write(f"{_paint(status, _DIM_CYAN, self.color)}\n")
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
            if self._cursor_controls:
                self.stream.write(f"\r{_CLEAR_LINE}{rendered}\n")
            else:
                self.stream.write(f"{rendered}\n")

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
        request: LinearTimeline._Request = (
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

    def _draw_now(self, request: LinearTimeline._Request, *, force: bool = False) -> None:
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
        current_entries = transcript.entries
        width_changed = self._last_rendered_width not in {None, width}
        entries_changed = current_entries != self._last_history_entries

        # Never render retained rows for a status-only update.  New rows are
        # rendered from only the entries added since the last draw.  A resize
        # changes the width of future rows, not the already committed timeline.
        if not self._timeline_initialized:
            history_new = self._entry_rows(current_entries, width, self.color)
        elif entries_changed:
            history_new = self._entry_delta_rows(
                current_entries,
                self._last_history_entries,
                width,
                self.color,
            )
        else:
            history_new = ()

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
        previous_stream_emitted = self._stream_emitted_text
        stream_ended_or_switched = (
            not current_stream_text
            or current_stream_role != self._last_stream_role
            or current_stream_key != self._last_stream_key
        )

        # finish_stream() promotes streamed text into history.  It was already
        # emitted line-by-line, so append only a genuine suffix of the commit.
        if history_new and previous_stream_emitted and stream_ended_or_switched:
            role = self._last_stream_role
            if role is not None:
                streamed_entry = next(
                    (
                        entry
                        for entry in reversed(current_entries)
                        if entry.role == role
                        and (
                            entry.text.startswith(self._last_stream_text)
                            or self._last_stream_text.startswith(entry.text)
                            or _stream_entry_content(entry).startswith(
                                self._stream_emitted_text.rstrip("\n")
                            )
                            or self._stream_emitted_text.rstrip("\n").startswith(
                                _stream_entry_content(entry)
                            )
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
                        if history_new[index : index + block_size] != rendered_stream:
                            continue
                        committed_text = _stream_entry_content(streamed_entry)
                        emitted_text = previous_stream_emitted.rstrip("\n")
                        if committed_text.startswith(emitted_text):
                            suffix = committed_text[len(emitted_text) :].lstrip("\n")
                            replacement_rows = (
                                tuple(
                                    _entry_lines(
                                        TranscriptEntry(
                                            role=role,
                                            text=suffix,
                                            owner_task_id=streamed_entry.owner_task_id,
                                        ),
                                        width,
                                        color=bool(self.color),
                                    )
                                )
                                if suffix
                                else ()
                            )
                        elif emitted_text.startswith(committed_text):
                            replacement_rows = ()
                        else:
                            continue
                        history_new = (
                            *history_new[:index],
                            *replacement_rows,
                            *history_new[index + block_size :],
                        )
                        break

        status = _status_line(
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
            if self._timeline_initialized and (
                status != self._last_status_line or force or width_changed
            ):
                self._redraw_status_only(status)
            self._last_request = request
            self._last_status_line = status
            self._last_rendered_width = width
            return

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
        self._last_history_entries = current_entries
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
            if self._cursor_controls:
                self.stream.write(f"{label_text} ")
            else:
                self._restore_input_line("", force=True)
        self.stream.flush()

    def hide_cursor(self, *, commit: bool = False) -> None:
        # POSIX ``TerminalInput`` uses a managed draft while the legacy
        # readline path owns an echoed native line.  Only the latter leaves
        # the cursor one row below the prompt after Enter.
        readline_echoed = self._native_input and not self._managed_input_active
        self._input_active = False
        self._managed_input_active = False
        self._last_restored_input_text = None
        if not self.enabled:
            return
        if not self._cursor_controls:
            if commit:
                self.stream.write("\n")
            self.stream.flush()
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
    "LinearTimeline",
    "Transcript",
    "TranscriptEntry",
    "render_quota_rows",
    "render_markdown_lines",
]
