"""Pure full-screen terminal view model for Cambium's interactive frontend.

The cockpit is intentionally a presentation layer over immutable session and
observability snapshots.  It owns no provider, worker, branch, or context
state.  The only mutable value is a bounded local transcript used for the
operator's current terminal view.
"""

from __future__ import annotations

import os
import re
import shutil
import signal
import textwrap
from collections import deque
from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any, TextIO

_RESET = "\x1b[0m"
_DIM = "\x1b[2m"
_CYAN = "\x1b[1;36m"
_DIM_CYAN = "\x1b[2;36m"
_BLUE = "\x1b[1;34m"
_GREEN = "\x1b[1;32m"
_YELLOW = "\x1b[1;33m"
_RED = "\x1b[1;31m"
_WHITE = "\x1b[1;37m"

_ALT_ENTER = "\x1b[?1049h\x1b[?25l"
_ALT_EXIT = "\x1b[?25h\x1b[?1049l"
_HOME_CLEAR = "\x1b[H\x1b[2J"
_CLEAR_LINE = "\x1b[2K"

_CONTROLS = re.compile(r"[\x00-\x08\x0b-\x1f\x7f-\x9f]")
_ANSI = re.compile(r"\x1b\[[0-?]*[ -/]*[@-~]")

_ROLE_COLORS = {
    "user": _BLUE,
    "assistant": _WHITE,
    "tool": _YELLOW,
    "system": _DIM,
    "error": _RED,
    "dim": _DIM,
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


def _is_tty(stream: Any) -> bool:
    try:
        return bool(getattr(stream, "isatty", lambda: False)())
    except (AttributeError, OSError, ValueError):
        return False


def _sanitize(value: Any) -> str:
    text = str(value)
    text = _ANSI.sub("", text)
    return _CONTROLS.sub("", text).replace("\r", "")


def _clip(text: str, width: int) -> str:
    clean = _sanitize(text)
    if width <= 0:
        return ""
    if len(clean) <= width:
        return clean
    if width == 1:
        return "…"
    return clean[: width - 1] + "…"


def _pad(text: str, width: int) -> str:
    clean = _clip(text, width)
    return clean + " " * max(0, width - len(clean))


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


def _color_enabled(stream: Any) -> bool:
    return (
        _is_tty(stream)
        and not os.environ.get("NO_COLOR")
        and os.environ.get("TERM", "") != "dumb"
    )


def _paint(text: str, color: str, enabled: bool) -> str:
    return f"{color}{text}{_RESET}" if enabled else text


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
        if data.get("status") in _FAILURE_STATUSES:
            return None
        text = _result_text(data)
        if text is None:
            return None
        return "assistant", text, False, None
    if kind == "tool_event" or kind in _ASSISTANT_STREAM_KINDS or kind in _TOOL_STREAM_KINDS:
        if kind == "tool_event" and data.get("ok") is False:
            return None
        role = _message_role(kind, data)
        if role is None:
            return None
        text = _text_value(data)
        if not text:
            return None
        append = kind in _STREAM_DELTA_KINDS
        if data.get("cumulative") is True or data.get("replace") is True:
            append = False
        if data.get("append") is True:
            append = True
        message_id = data.get("message_id") or data.get("id")
        return role, text, append, message_id if isinstance(message_id, str) else None
    return None


@dataclass(frozen=True, slots=True)
class TranscriptEntry:
    """One bounded, terminal-only transcript item."""

    role: str
    text: str


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
    if kind == "tool_event" and data.get("ok") is False:
        tool = data.get("tool")
        if not isinstance(tool, str) or not tool:
            return "tool failed"
        detail = data.get("failure_reason") or data.get("reason") or data.get("error")
        line = f"{tool}: failed"
        if isinstance(detail, str) and detail:
            line += f" · {detail}"
        return _sanitize(line)

    if kind == "timeout":
        phase = data.get("phase")
        return _sanitize(
            f"timeout: {phase}" if isinstance(phase, str) and phase else "timeout"
        )

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
        self._turn_serial = 0
        self._turn_by_task: dict[str, int] = {}
        self._failure_context: dict[tuple[str, int | None, int], list[str]] = {}
        self._failure_blocks: dict[tuple[str, int | None, int], _FailureBlock] = {}
        self._failure_order: deque[tuple[str, int | None, int]] = deque()

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

    def add(self, role: str, text: str) -> None:
        if role not in _ROLE_LABELS:
            raise ValueError(f"unknown transcript role: {role}")
        clean = _sanitize(text).strip("\n")
        if clean:
            self._entries.append(TranscriptEntry(role=role, text=clean))

    def user(self, text: str) -> None:
        self._turn_serial += 1
        self._turn_by_task.clear()
        self.add("user", text)

    def assistant(self, text: str) -> None:
        self.add("assistant", text)

    def system(self, text: str) -> None:
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

    def _clear_stream(self) -> None:
        self._stream_role = None
        self._stream_text = ""
        self._stream_message_id = None
        self._stream_truncated = False

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
    ) -> None:
        if self._stream_role != role or (
            message_id is not None
            and self._stream_message_id is not None
            and message_id != self._stream_message_id
        ):
            self._commit_stream()
        self._stream_role = role
        self._stream_message_id = message_id

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
        summary_failure = _failure_summary(final) if final else None
        if summary_failure is not None:
            task_id, cause = summary_failure
            self._record_failure(task_id, None, cause)
            # The detailed task/cause/context is already in the red failure
            # block. Keep the model footer useful without repeating it.
            final = "plan=failed"
        if not final:
            if current and role is not None:
                self.add(role, current)
            return
        if current and role == "assistant" and not truncated:
            if final.startswith(current) or current.startswith(final):
                final = final if len(final) >= len(current) else current
            elif current != final:
                final = f"{current}\n\n{final}"
        elif current and role is not None:
            self.add(role, current)
        self.assistant(final)

    def _failure_key(
        self, task_id: str | None, turn: int | None
    ) -> tuple[str, int | None, int]:
        return task_id or "?", turn, self._turn_serial

    def _remember_turn(self, task_id: str | None, turn: int | None) -> None:
        if task_id is not None and turn is not None:
            self._turn_by_task[task_id] = turn

    def _context_key(
        self, task_id: str | None, turn: int | None
    ) -> tuple[str, int | None, int]:
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
            line
            for line in context
            if "failed" in line.lower() or "timeout" in line.lower()
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

    def _remember_failure_context(
        self, task_id: str | None, turn: int | None, line: str
    ) -> None:
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

        update = _stream_update(record)
        if update is not None:
            role, text, append, message_id = update
            self._update_stream(
                role,
                text,
                append=append,
                message_id=message_id,
            )

        if kind == "tool_event":
            tool = data.get("tool")
            ok = data.get("ok")
            duration = data.get("duration_ms")
            if ok is False:
                context_line = _failure_context_line(kind, data)
                if context_line is not None:
                    self._remember_failure_context(task_id, turn, context_line)
                return
            if isinstance(tool, str):
                state = "ok" if ok is True else "failed" if ok is False else "done"
                suffix = f" · {duration}ms" if isinstance(duration, int) else ""
                self.add("tool", f"{tool}: {state}{suffix}")
            return

        if kind in {"child_admitted", "child_rejected"}:
            child = data.get("child_task_id") or data.get("task_id") or "child"
            reason = data.get("reason")
            message = f"{kind.replace('_', ' ')}: {child}"
            if isinstance(reason, str) and reason:
                message += f" · {reason}"
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


def _wrap_markdown(text: str, width: int) -> list[str]:
    """Render Markdown structure as safe plain terminal lines before coloring."""
    width = max(8, width)
    output: list[str] = []
    in_fence = False
    for raw_line in _sanitize(text).splitlines() or [""]:
        stripped = raw_line.strip()
        if stripped.startswith("```"):
            in_fence = not in_fence
            output.append(_clip(raw_line, width))
            continue
        if in_fence:
            output.append(_clip("  " + raw_line, width))
            continue
        if stripped.startswith("#"):
            heading = stripped.lstrip("#").strip()
            output.extend(textwrap.wrap(heading, width=width) or [""])
            continue
        prefix = ""
        body = raw_line
        if stripped.startswith(("- ", "* ")):
            prefix, body = "• ", stripped[2:]
        elif stripped.startswith(">"):
            prefix, body = "│ ", stripped[1:].lstrip()
        continuation = " " * len(prefix)
        wrapped = textwrap.wrap(
            body,
            width=max(4, width - len(prefix)),
            replace_whitespace=False,
            drop_whitespace=True,
            break_long_words=True,
            break_on_hyphens=False,
        )
        if not wrapped:
            output.append("")
        else:
            output.append(prefix + wrapped[0])
            output.extend(continuation + line for line in wrapped[1:])
    return output


def _entry_lines(entry: TranscriptEntry, width: int) -> list[tuple[str, str]]:
    label = _ROLE_LABELS[entry.role]
    body_width = max(8, width - 3)
    rendered = [(entry.role, f" {label}")]
    for line in _wrap_markdown(entry.text, body_width):
        role = (
            "dim"
            if entry.role == "error" and line.lstrip().startswith(_FAILURE_CONTEXT_PREFIX)
            else entry.role
        )
        rendered.append((role, "   " + line))
    rendered.append((entry.role, ""))
    return rendered


def _stream_lines(
    transcript: Transcript, width: int, capacity: int
) -> list[tuple[str, str]]:
    if not transcript.streaming_text or transcript.streaming_role is None:
        return []
    body_width = max(8, width - 3)
    text = transcript.streaming_text
    if len(text) > _STREAM_RENDER_LIMIT:
        text = "…\n" + text[-_STREAM_RENDER_LIMIT:]
    label = _ROLE_LABELS[transcript.streaming_role]
    rendered = [(transcript.streaming_role, f" {label} · generating")]
    rendered.extend(
        (transcript.streaming_role, "   " + line)
        for line in _wrap_markdown(text, body_width)
    )
    return rendered[-max(1, capacity) :]


def _transcript_lines(transcript: Transcript, width: int, capacity: int) -> list[tuple[str, str]]:
    capacity = max(1, capacity)
    active = _stream_lines(transcript, width, capacity)
    remaining = max(0, capacity - len(active))
    rendered: list[tuple[str, str]] = []
    if remaining:
        for entry in reversed(transcript.entries):
            block = _entry_lines(entry, width)
            if len(block) >= remaining:
                rendered = block[-remaining:] + rendered
                break
            rendered = block + rendered
            remaining -= len(block)
    rendered.extend(active)
    if not rendered:
        rendered = [("system", " Waiting for a prompt. Type /help for commands.")]
    return rendered[-capacity:]


def _agent_model(agent: Any) -> str:
    provider = getattr(agent, "provider", None)
    model = getattr(agent, "model", None)
    if provider and model:
        return f"{provider}/{model}"
    return model or provider or "?"


def _side_sections(
    snapshot: Any, cumulative_line: str, width: int, capacity: int
) -> list[tuple[str, str]]:
    lines: list[tuple[str, str]] = []
    agents = tuple(getattr(snapshot, "agents", ()))
    lines.append(("heading", " AGENTS"))
    if not agents:
        lines.append(("dim", " no agents yet"))
    else:
        for agent in agents[-6:]:
            role = "M" if getattr(agent, "role", "") == "main" else "S"
            state = str(getattr(agent, "state", "?"))
            task = _clip(getattr(agent, "task_id", "?"), 15)
            lines.append((state, f" {role} {task:<15} {state}"))
            lines.append(("dim", f"   {_clip(_agent_model(agent), width - 4)}"))
            tool = getattr(agent, "tool", None)
            tokens = int(getattr(agent, "total_tokens", 0))
            rate = getattr(agent, "output_tokens_per_s", None)
            suffix = f" · {rate:.1f} out/s" if isinstance(rate, int | float) else ""
            tool_suffix = f" · {tool}" if tool else ""
            lines.append(
                ("dim", f"   {_human_count(tokens)} tok{suffix}{tool_suffix}")
            )

    context = getattr(snapshot, "context", None)
    lines.append(("heading", " CONTEXT"))
    if context is None:
        lines.append(("dim", " unavailable"))
    else:
        approx = "≈" if getattr(context, "approximate", False) else ""
        lines.extend(
            [
                (
                    "normal",
                    f" epoch {getattr(context, 'epoch', 0)} · "
                    f"segments {getattr(context, 'summary_segments', 0)}",
                ),
                (
                    "normal",
                    " trunk "
                    f"{approx}{_human_count(getattr(context, 'estimated_trunk_tokens', 0))} tok",
                ),
                ("dim", f" {_human_bytes(getattr(context, 'summary_trunk_bytes', 0))} serialized"),
                (
                    "normal",
                    " raw "
                    f"{approx}{_human_count(getattr(context, 'estimated_raw_tail_tokens', 0))} tok",
                ),
                (
                    "dim",
                    " checkpoint "
                    + _clip(
                        getattr(context, "checkpoint_ref", None) or "none",
                        width - 13,
                    ),
                ),
            ]
        )

    lines.append(("heading", " SESSION USAGE"))
    for part in cumulative_line.replace("usage: ", "", 1).split(" "):
        if part:
            lines.append(("normal", " " + part))

    recent = tuple(getattr(snapshot, "recent_events", ()))
    if recent:
        lines.append(("heading", " RECENT"))
        for event in recent[-4:]:
            kind = _clip(str(getattr(event, "kind", "event")), 18)
            detail = _clip(str(getattr(event, "detail", "")), width - 4)
            lines.append(("dim", f" {kind}"))
            if detail:
                lines.append(("dim", f"   {detail}"))

    return lines[: max(1, capacity)]


def _style_kind(kind: str) -> str:
    if kind in {"failed", "cancelled", "error"}:
        return _RED
    if kind in {"active", "starting", "merging", "running"}:
        return _YELLOW
    if kind in {"succeeded", "done"}:
        return _GREEN
    if kind == "heading":
        return _CYAN
    if kind == "dim":
        return _DIM
    return ""


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
) -> list[str]:
    """Render one deterministic full-screen frame without cursor controls."""
    width = max(64, width)
    height = max(20, height)
    inner = width - 2
    lines: list[str] = []

    status = str(getattr(snapshot, "session_status", "idle"))
    title = f" Cambium · {status} "
    lines.append(_paint("┌" + _pad(title, inner) + "┐", _CYAN, color))
    meta = _clip(f" {_sanitize(session_description)}", inner)
    lines.append("│" + _pad(meta, inner) + "│")
    branch = _clip(" " + _sanitize(branch_line), inner)
    lines.append("│" + _pad(branch, inner) + "│")

    body_height = height - 8
    wide = width >= 104
    if wide:
        side_width = min(44, max(34, width // 3))
        transcript_width = inner - side_width - 1
        lines.append(
            _paint(
                "├" + "─" * transcript_width + "┬" + "─" * side_width + "┤",
                _DIM_CYAN,
                color,
            )
        )
        transcript_rows = _transcript_lines(transcript, transcript_width, body_height)
        side_rows = _side_sections(snapshot, cumulative_line, side_width, body_height)
        for index in range(body_height):
            if index < len(transcript_rows):
                role, left = transcript_rows[index]
                left_plain = _pad(left, transcript_width)
                left_text = _paint(left_plain, _ROLE_COLORS.get(role, ""), color)
            else:
                left_text = " " * transcript_width
            if index < len(side_rows):
                kind, right = side_rows[index]
                right_plain = _pad(right, side_width)
                right_text = _paint(right_plain, _style_kind(kind), color)
            else:
                right_text = " " * side_width
            lines.append("│" + left_text + "│" + right_text + "│")
        lines.append(
            _paint(
                "├" + "─" * transcript_width + "┴" + "─" * side_width + "┤",
                _DIM_CYAN,
                color,
            )
        )
    else:
        summary = (
            f" agents={getattr(snapshot, 'active_agents', 0)} active "
            f"tokens={_human_count(getattr(snapshot, 'total_tokens', 0))} "
            f"out/s={getattr(snapshot, 'output_tokens_per_s', 0.0):.1f} "
            f"epoch={getattr(getattr(snapshot, 'context', None), 'epoch', 0)} "
            f"segments={getattr(getattr(snapshot, 'context', None), 'summary_segments', 0)}"
        )
        lines.append(_paint("├" + "─" * inner + "┤", _DIM_CYAN, color))
        lines.append("│" + _pad(summary, inner) + "│")
        transcript_capacity = max(1, body_height - 1)
        for role, text in _transcript_lines(transcript, inner, transcript_capacity):
            plain = _pad(text, inner)
            lines.append("│" + _paint(plain, _ROLE_COLORS.get(role, ""), color) + "│")
        while len(lines) < height - 4:
            lines.append("│" + " " * inner + "│")
        lines.append(_paint("├" + "─" * inner + "┤", _DIM_CYAN, color))

    help_line = " /help commands · <<< multiline >>> · Ctrl-C cancel frontend · /exit close"
    lines.append("│" + _pad(help_line, inner) + "│")
    input_line = f" {input_label} "
    lines.append("│" + _pad(input_line, inner) + "│")
    lines.append(_paint("└" + "─" * inner + "┘", _CYAN, color))
    return lines[:height]


class Cockpit:
    """Persistent alternate-screen owner for one interactive frontend."""

    def __init__(self, stream: TextIO, *, enabled: bool = True) -> None:
        self.stream = stream
        self.enabled = enabled and _is_tty(stream)
        self.color = self.enabled and _color_enabled(stream)
        self._entered = False
        self._previous_sigterm_handler: Any = None
        self._last_size = os.terminal_size((120, 40))

    @property
    def size(self) -> os.terminal_size:
        return self._last_size

    def __enter__(self) -> Cockpit:
        if self.enabled:
            self._previous_sigterm_handler = signal.getsignal(signal.SIGTERM)
            try:
                signal.signal(signal.SIGTERM, self._handle_sigterm)
            except (OSError, ValueError):
                self._previous_sigterm_handler = None
            self.stream.write(_ALT_ENTER)
            self.stream.flush()
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
        if self._entered:
            self.stream.write(_ALT_EXIT)
            self.stream.flush()
            self._entered = False
        if self._previous_sigterm_handler is not None:
            try:
                signal.signal(signal.SIGTERM, self._previous_sigterm_handler)
            except (OSError, ValueError):
                pass
            self._previous_sigterm_handler = None

    def draw(
        self,
        snapshot: Any,
        transcript: Transcript,
        *,
        session_description: str,
        branch_line: str,
        cumulative_line: str,
        input_label: str = "›",
    ) -> None:
        if not self.enabled:
            return
        self._last_size = shutil.get_terminal_size((120, 40))
        frame = render_cockpit(
            snapshot,
            transcript,
            session_description=session_description,
            branch_line=branch_line,
            cumulative_line=cumulative_line,
            width=self._last_size.columns,
            height=self._last_size.lines,
            color=self.color,
            input_label=input_label,
        )
        self.stream.write(_HOME_CLEAR)
        self.stream.write("\n".join(frame))
        self.stream.flush()

    def move_to_input(self, *, label: str = "›") -> None:
        if not self.enabled:
            return
        row = max(1, self._last_size.lines - 1)
        column = 4 + len(label)
        self.stream.write(f"\x1b[{row};1H{_CLEAR_LINE}│ {label} ")
        self.stream.write("\x1b[?25h")
        self.stream.write(f"\x1b[{row};{column}H")
        self.stream.flush()

    def hide_cursor(self) -> None:
        if self.enabled:
            self.stream.write("\x1b[?25l")
            self.stream.flush()


__all__ = [
    "Cockpit",
    "Transcript",
    "TranscriptEntry",
    "render_cockpit",
]
