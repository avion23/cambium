"""Read-only projection over existing branch session artifacts.

This module deliberately creates no memory, evidence, or index database.  It
projects the event log and immutable checkpoint files that Cambium already
writes.  Tool-call references include the task branch, worker generation, turn,
and batch index, so a call can be listed globally and then reopened
independently.

The feature has no branch access-control model: every task in the current
session is visible.  Bounds below are resource and response-shape limits, not
permissions.
"""

from __future__ import annotations

import hashlib
import json
import re
import stat
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass
from enum import StrEnum
from pathlib import Path
from typing import Any
from urllib.parse import quote, unquote

from .store import StoreError, iter_event_pages

MAX_HISTORY_ROWS = 64
MAX_HISTORY_OUTPUT_BYTES = 32 * 1024
MAX_CHECKPOINT_BYTES = 4 * 1024 * 1024
MAX_MESSAGE_BYTES = 8 * 1024
MAX_ARTIFACT_PAGE_BYTES = 8 * 1024
_EVENT_PAGE_SIZE = 4096

_TERMINAL_EVENT_KINDS = frozenset(
    {
        "result",
        "result_envelope",
        "child_result",
        "task_failed",
        "worker_failed",
        "child_failed",
        "child_rejected",
        "worker_exit",
        "worker_terminated",
        "exit",
    }
)
_EXIT_SUCCESS_REASONS = frozenset({"done", "succeeded", "success"})
_EXIT_STATUS_REASONS = {
    "cancelled": "cancelled",
    "suspended": "suspended",
}


class HistoryAction(StrEnum):
    """Queries exposed by the branch-history tool."""

    BRANCHES = "branches"
    TOOLS = "tools"
    TOOL = "tool"
    TRANSCRIPT = "transcript"


class BranchHistoryError(ValueError):
    """A branch-history query or referenced artifact is invalid."""


@dataclass(frozen=True, slots=True)
class _Event:
    order: tuple[int, int]
    kind: str
    payload: dict[str, Any]
    task_id: str | None
    session: str = ""


@dataclass(slots=True)
class _Branch:
    task_id: str
    parent_task_id: str | None = None
    status: str = "unknown"
    provider: str | None = None
    context_mode: str | None = None
    placement: str | None = None
    last_turn: int = 0
    tool_count: int = 0


def branch_ref(task_id: str) -> str:
    """Stable printable reference for one task branch."""
    return f"branch:{quote(task_id, safe='')}"


def tool_ref(
    task_id: str, generation: int, turn: int, batch_index: int = 0, *, session: str = ""
) -> str:
    """Identify a call, including its interactive turn when counters can repeat."""
    suffix = f"@{session}" if session else ""
    return f"tool:{quote(task_id, safe='')}:{generation}:{turn}:{batch_index}{suffix}"


def _parse_tool_ref(value: Any) -> tuple[str, int, int, int, str | None]:
    if not isinstance(value, str):
        raise BranchHistoryError("branch_history action=tool requires ref")
    identity, separator, session = value.partition("@")
    if separator and not re.fullmatch(r"turn-[0-9]+", session):
        raise BranchHistoryError("tool ref session must be turn-<number>")
    parts = identity.split(":")
    if len(parts) != 5 or parts[0] != "tool":
        raise BranchHistoryError("tool ref must be tool:<task>:<generation>:<turn>:<index>")
    try:
        task_id = unquote(parts[1])
        generation = int(parts[2])
        turn = int(parts[3])
        batch_index = int(parts[4])
    except ValueError:
        raise BranchHistoryError("tool ref generation, turn, and index must be integers") from None
    if not task_id or generation < 0 or turn < 1 or batch_index < 0:
        raise BranchHistoryError("tool ref contains an invalid task, generation, turn, or index")
    return task_id, generation, turn, batch_index, session if separator else None


def _positive_limit(value: Any, default: int = 20) -> int:
    if value is None:
        return default
    if type(value) is not int or value < 1 or value > MAX_HISTORY_ROWS:
        raise BranchHistoryError(f"limit must be between 1 and {MAX_HISTORY_ROWS}")
    return value


def _non_negative_offset(value: Any) -> int:
    if value is None:
        return 0
    if type(value) is not int or value < 0:
        raise BranchHistoryError("offset must be a non-negative integer")
    return value


def _artifact_limit(value: Any) -> int:
    if value is None:
        return MAX_MESSAGE_BYTES
    if type(value) is not int or value < 1 or value > MAX_ARTIFACT_PAGE_BYTES:
        raise BranchHistoryError(
            f"output_limit must be between 1 and {MAX_ARTIFACT_PAGE_BYTES} bytes"
        )
    return value


def _session_event_stores(session_dir: Path) -> tuple[Path, ...]:
    """Return the root/turn event stores that form one visible session."""
    selected = session_dir.expanduser().resolve()
    root = selected.parent if re.fullmatch(r"turn-[0-9]+", selected.name) else selected
    candidates: list[Path] = []

    def add(path: Path) -> None:
        if path not in candidates and path.is_file() and not path.is_symlink():
            candidates.append(path)

    add(root / ".cambium" / "events.db")
    try:
        children = sorted(
            (
                child
                for child in root.iterdir()
                if re.fullmatch(r"turn-[0-9]+", child.name)
                and child.name == f"turn-{int(child.name[5:]):04d}"
            ),
            key=lambda path: int(path.name[5:]),
        )
    except OSError:
        children = []
    for child in children:
        if child.is_dir() and not child.is_symlink():
            add(child / ".cambium" / "events.db")
    return tuple(candidates)


def _event_payload(event: Mapping[str, Any]) -> dict[str, Any]:
    payload = event.get("payload")
    return dict(payload) if isinstance(payload, Mapping) else {}


def _events(session_dir: Path) -> Iterable[_Event]:
    """Yield normalized durable events page by page.

    Queries only retain the projection needed for their response.  A large
    turn therefore does not hit the capped materializing reader or require an
    in-memory copy of the complete event log.
    """

    stores = _session_event_stores(session_dir)
    for store_index, path in enumerate(stores):
        try:
            pages = iter_event_pages(path, page_size=_EVENT_PAGE_SIZE)
            row_index = 0
            for rows in pages:
                for row in rows:
                    current_index = row_index
                    row_index += 1
                    if not isinstance(row, Mapping):
                        continue
                    payload = _event_payload(row)
                    generation = row.get("generation")
                    if type(generation) is int:
                        payload["generation"] = generation
                    kind = row.get("kind")
                    if not isinstance(kind, str) or not kind:
                        candidate = payload.get("type")
                        kind = candidate if isinstance(candidate, str) else "unknown"
                    task_id = payload.get("task_id", row.get("task_id"))
                    if not isinstance(task_id, str) or not task_id:
                        task_id = None
                    seq = row.get("seq")
                    sequence = seq if type(seq) is int and seq >= 0 else current_index
                    name = path.parent.parent.name
                    session = name if re.fullmatch(r"turn-[0-9]+", name) else ""
                    yield _Event((store_index, sequence), kind, payload, task_id, session)
        except StoreError as exc:
            raise BranchHistoryError(f"cannot read branch event store {path}: {exc}") from exc


def _int(payload: Mapping[str, Any], key: str, default: int = 0) -> int:
    value = payload.get(key)
    return value if type(value) is int and value >= 0 else default


def _set_context_policy(branch: _Branch, event: _Event, *, resolved: bool) -> None:
    mode_key = "resolved_context_mode" if resolved else "context_mode"
    placement_key = "resolved_placement" if resolved else "placement"
    mode = event.payload.get(mode_key)
    if not isinstance(mode, str):
        mode = event.payload.get("context_mode")
    placement = event.payload.get(placement_key)
    if not isinstance(placement, str):
        placement = event.payload.get("placement")
    if resolved or branch.context_mode is None:
        if isinstance(mode, str):
            branch.context_mode = mode
    if resolved or branch.placement is None:
        if isinstance(placement, str):
            branch.placement = placement


def _terminal_task_id(event: _Event) -> str | None:
    """Return the branch that owns a terminal event.

    Child lifecycle verdicts are often emitted by the parent supervisor.  The
    payload's child identity is therefore authoritative when one is present;
    using the envelope owner for ``child_rejected`` would incorrectly mark the
    parent branch instead of the rejected child.
    """
    child_id = event.payload.get("child_task_id")
    if event.kind == "child_rejected":
        return child_id if isinstance(child_id, str) and child_id else None
    if event.kind == "child_failed":
        if isinstance(child_id, str) and child_id:
            return child_id
    return event.task_id


def _terminal_status(event: _Event) -> str:
    """Project one durable terminal event to a truthful branch status."""
    if event.kind in {"task_failed", "worker_failed", "child_failed"}:
        return "failed"
    if event.kind == "child_rejected":
        return "rejected"
    if event.kind == "worker_exit":
        status = event.payload.get("status")
        if isinstance(status, str) and status:
            return status
        exit_code = event.payload.get("exit_code")
        if type(exit_code) is int:
            return "succeeded" if exit_code == 0 else "failed"
        return "unknown"
    if event.kind == "exit":
        reason = event.payload.get("reason")
        if isinstance(reason, str):
            normalized = reason.casefold()
            if normalized in _EXIT_SUCCESS_REASONS:
                return "succeeded"
            status = _EXIT_STATUS_REASONS.get(normalized)
            if status is not None:
                return status
        return "failed"
    if event.kind == "worker_terminated":
        return "failed"
    status = event.payload.get("status")
    return status if isinstance(status, str) and status else "unknown"


def _record_child_relation(branches: dict[str, _Branch], get_branch: Any, event: _Event) -> None:
    if event.kind == "child_admitted":
        child = event.payload.get("child_task_id")
        parent = event.payload.get("parent_task_id") or event.task_id
        if isinstance(child, str) and child:
            branch = get_branch(child)
            branch.parent_task_id = parent if isinstance(parent, str) and parent else None
            _set_context_policy(branch, event, resolved=False)
        return
    if event.kind not in {"child_failed", "child_rejected"}:
        return
    child = _terminal_task_id(event)
    if child is None:
        return
    branch = get_branch(child)
    parent = event.payload.get("parent_task_id") or event.task_id
    if isinstance(parent, str) and parent and parent != child:
        branch.parent_task_id = parent


def _record_terminal_status(branches: dict[str, _Branch], get_branch: Any, event: _Event) -> None:
    if event.kind not in _TERMINAL_EVENT_KINDS:
        return
    task_id = _terminal_task_id(event)
    if task_id is not None:
        get_branch(task_id).status = _terminal_status(event)


def _branches(events: Iterable[_Event]) -> list[_Branch]:
    branches: dict[str, _Branch] = {}

    def get(task_id: str) -> _Branch:
        return branches.setdefault(task_id, _Branch(task_id))

    for event in events:
        if event.task_id is not None:
            branch = get(event.task_id)
            branch.last_turn = max(branch.last_turn, _int(event.payload, "turn"))
        _record_child_relation(branches, get, event)
        if event.kind == "context_fork":
            child = event.payload.get("child_task_id")
            if isinstance(child, str) and child:
                branch = get(child)
                _set_context_policy(branch, event, resolved=True)
        if event.kind == "tool_event" and event.task_id is not None:
            get(event.task_id).tool_count += 1
        if event.kind == "usage_event" and event.task_id is not None:
            provider = event.payload.get("provider")
            if isinstance(provider, str) and provider:
                get(event.task_id).provider = provider
        _record_terminal_status(branches, get, event)
    return sorted(
        branches.values(),
        key=lambda branch: (branch.parent_task_id or "", branch.task_id),
    )


def _bounded(text: str, limit: int = MAX_HISTORY_OUTPUT_BYTES) -> str:
    raw = text.encode("utf-8")
    if len(raw) <= limit:
        return text
    marker = "\n... [branch history truncated]"
    keep = max(0, limit - len(marker.encode("utf-8")))
    return raw[:keep].decode("utf-8", errors="ignore") + marker


def _page(header: str, rows: Sequence[str], offset: int, total: int) -> str:
    selected = list(rows)
    sizes = [len(row.encode("utf-8")) + 1 for row in selected]
    size = len(header.encode("utf-8")) + sum(sizes)
    # Reserve the cursor before fitting rows, not after truncating the response.
    budget = MAX_HISTORY_OUTPUT_BYTES - len(f"\nnext_offset={total}")
    while len(selected) > 1 and size > budget:
        selected.pop()
        size -= sizes.pop()
    suffix = f"\nnext_offset={offset + len(selected)}" if offset + len(selected) < total else ""
    return _bounded("\n".join((header, *selected)), budget) + suffix


def _list_branches(events: Iterable[_Event], offset: int, limit: int) -> str:
    rows = _branches(events)
    lines = [f"branches={len(rows)}"]
    selected = rows[offset : offset + limit]
    for branch in selected:
        lines.append(
            " ".join(
                (
                    branch_ref(branch.task_id),
                    f"parent={branch.parent_task_id or '-'}",
                    f"status={branch.status}",
                    f"provider={branch.provider or '-'}",
                    f"context={branch.context_mode or '-'}",
                    f"placement={branch.placement or '-'}",
                    f"tools={branch.tool_count}",
                    f"turn={branch.last_turn}",
                )
            )
        )
    return _page(lines[0], lines[1:], offset, len(rows))


def _tool_events(events: Iterable[_Event], task_id: str | None) -> Iterable[_Event]:
    for event in events:
        if (
            event.kind == "tool_event"
            and event.task_id is not None
            and (task_id is None or event.task_id == task_id)
        ):
            yield event


def _tool_identity(event: _Event) -> tuple[str, int, int, int]:
    if event.task_id is None:
        raise BranchHistoryError("tool event has no task branch")
    return (
        event.task_id,
        _int(event.payload, "generation"),
        _int(event.payload, "turn"),
        _int(event.payload, "batch_index"),
    )


def _list_tools(events: Iterable[_Event], task_id: str | None, offset: int, limit: int) -> str:
    lines: list[str] = []
    total = 0
    for event in _tool_events(events, task_id):
        if offset <= total < offset + limit:
            branch, generation, turn, batch_index = _tool_identity(event)
            lines.append(
                " ".join(
                    (
                        tool_ref(branch, generation, turn, batch_index, session=event.session),
                        f"branch={branch_ref(branch)}",
                        f"tool={event.payload.get('tool', '-')}",
                        f"ok={str(bool(event.payload.get('ok'))).lower()}",
                        f"duration_ms={_int(event.payload, 'duration_ms')}",
                        f"cmd={event.payload.get('cmd', '-')}",
                    )
                )
            )
        total += 1
    return _page(f"tool_calls={total}", lines, offset, total)


def _regular_json(path: Path) -> dict[str, Any]:
    try:
        info = path.lstat()
        if not stat.S_ISREG(info.st_mode) or stat.S_ISLNK(info.st_mode):
            raise BranchHistoryError(f"checkpoint is not a regular file: {path}")
        if info.st_size > MAX_CHECKPOINT_BYTES:
            raise BranchHistoryError(f"checkpoint exceeds {MAX_CHECKPOINT_BYTES} bytes")
        value = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError as exc:
        raise BranchHistoryError(f"checkpoint not found: {path}") from exc
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise BranchHistoryError(f"checkpoint is unreadable: {path}") from exc
    if not isinstance(value, dict):
        raise BranchHistoryError("checkpoint must contain a JSON object")
    return value


def _checkpoint_messages(
    path: Path,
    *,
    task_id: str | None = None,
    generation: int | None = None,
    turn: int | None = None,
) -> list[dict[str, str]]:
    value = _regular_json(path)
    for key in ("generation", "turn"):
        actual = value.get(key)
        if type(actual) is not int or actual <= 0:
            raise BranchHistoryError(f"checkpoint {key} is missing or invalid")
    for key, expected in (("task_id", task_id), ("generation", generation), ("turn", turn)):
        actual = value.get(key)
        if (
            expected is not None
            and actual is not None
            and (type(actual) is not type(expected) or actual != expected)
        ):
            raise BranchHistoryError(f"checkpoint {key} does not match the recorded tool exchange")
    candidates: Any = value.get("transcript")
    if candidates is None:
        content = value.get("content")
        if isinstance(content, Mapping):
            candidates = [
                *(content.get("provider_messages") or []),
                *(content.get("continuation_suffix") or []),
            ]
        else:
            candidates = [
                *(value.get("provider_messages") or []),
                *(value.get("continuation_suffix") or []),
            ]
    if not isinstance(candidates, list):
        raise BranchHistoryError("checkpoint has no transcript")
    messages: list[dict[str, str]] = []
    for message in candidates:
        if not isinstance(message, Mapping):
            continue
        role = message.get("role")
        content = message.get("content")
        if isinstance(role, str) and isinstance(content, str):
            messages.append({"role": role, "content": content})
    return messages


def _checkpoint_event(
    events: Iterable[_Event],
    task_id: str,
    generation: int | None,
    turn: int | None,
    *,
    session: str | None = None,
) -> _Event | None:
    latest: _Event | None = None
    for event in events:
        if event.kind != "checkpoint" or event.task_id != task_id:
            continue
        if session is not None and event.session != session:
            continue
        if generation is not None and _int(event.payload, "generation") != generation:
            continue
        if turn is not None and _int(event.payload, "turn") != turn:
            continue
        if isinstance(event.payload.get("state_ref"), str):
            latest = event
    return latest


def _extract_tool_exchange(
    messages: Sequence[Mapping[str, str]], tool: str, batch_index: int = 0
) -> tuple[str, str]:
    for index in range(len(messages) - 1, -1, -1):
        message = messages[index]
        if message.get("role") != "assistant":
            continue
        content = message.get("content", "")
        try:
            action = json.loads(content)
        except (json.JSONDecodeError, RecursionError):
            continue
        if not isinstance(action, Mapping) or action.get("type") != "tool_call":
            continue
        calls = action.get("calls") if "calls" in action else [action]
        if not isinstance(calls, list) or batch_index < 0 or batch_index >= len(calls):
            continue
        selected_call = calls[batch_index]
        if not isinstance(selected_call, Mapping) or selected_call.get("name") != tool:
            continue
        observation = ""
        observation_count = 0
        for candidate in messages[index + 1 :]:
            if candidate.get("role") == "assistant":
                break
            if candidate.get("role") != "user":
                continue
            candidate_content = candidate.get("content", "")
            if not candidate_content.startswith("tool "):
                continue
            if observation_count == batch_index:
                observation = candidate_content
                break
            observation_count += 1
        return content, observation
    return "", ""


_TOOL_OBSERVATION_RE = re.compile(
    r"\Atool (?P<name>\S+) ok=(?P<ok>true|false)(?:\n|\Z)", re.IGNORECASE
)


def _validate_tool_event(event: _Event) -> tuple[str, bool]:
    tool = event.payload.get("tool")
    if not isinstance(tool, str) or not tool.strip():
        raise BranchHistoryError("tool event has no valid tool name")
    ok = event.payload.get("ok")
    if type(ok) is not bool:
        raise BranchHistoryError("tool event has no valid boolean result")
    return tool, ok


def _validate_tool_observation(observation: str, tool: str, expected_ok: bool) -> None:
    match = _TOOL_OBSERVATION_RE.match(observation)
    if match is None:
        raise BranchHistoryError("recorded tool observation is missing its exact header")
    if match.group("name") != tool:
        raise BranchHistoryError("recorded tool observation does not match the tool event")
    observed_ok = match.group("ok").casefold() == "true"
    if observed_ok != expected_ok:
        raise BranchHistoryError("recorded tool observation disagrees with the tool event")


def _artifact_page(
    root: Path,
    event: _Event,
    *,
    offset: int,
    limit: int,
) -> tuple[str, int | None]:
    """Read one hash-verified page from a durable tool-output spill artifact."""
    output_ref = event.payload.get("output_ref")
    output_sha256 = event.payload.get("output_sha256")
    output_bytes = event.payload.get("output_bytes")
    if (
        not isinstance(output_ref, str)
        or not output_ref
        or not isinstance(output_sha256, str)
        or re.fullmatch(r"[0-9a-f]{64}", output_sha256) is None
        or type(output_bytes) is not int
        or output_bytes < 0
    ):
        raise BranchHistoryError("tool output artifact metadata is invalid")

    relative = Path(output_ref)
    if relative.is_absolute() or not relative.parts or ".." in relative.parts:
        raise BranchHistoryError("tool output artifact ref is invalid")
    session_root = root / event.session if event.session else root
    session_root = session_root.resolve()
    candidate = session_root / relative
    current = session_root
    for component in relative.parts:
        current /= component
        try:
            info = current.lstat()
        except FileNotFoundError as exc:
            raise BranchHistoryError("tool output artifact is missing") from exc
        if stat.S_ISLNK(info.st_mode):
            raise BranchHistoryError("tool output artifact must not traverse symlinks")
    try:
        candidate = candidate.resolve()
        candidate.relative_to(session_root)
    except (OSError, ValueError) as exc:
        raise BranchHistoryError("tool output artifact escapes its session") from exc
    if not candidate.is_file() or candidate.is_symlink():
        raise BranchHistoryError("tool output artifact is unavailable")
    if candidate.stat().st_size != output_bytes:
        raise BranchHistoryError("tool output artifact byte count does not match durable evidence")

    digest = hashlib.sha256()
    try:
        with candidate.open("rb") as handle:
            while chunk := handle.read(64 * 1024):
                digest.update(chunk)
            if digest.hexdigest() != output_sha256:
                raise BranchHistoryError(
                    "tool output artifact digest does not match durable evidence"
                )
            page_start = min(offset, output_bytes)
            handle.seek(page_start)
            raw = handle.read(limit)
    except OSError as exc:
        raise BranchHistoryError("tool output artifact cannot be read") from exc
    next_offset = page_start + len(raw)
    return (
        raw.decode("utf-8", errors="replace"),
        next_offset if next_offset < output_bytes else None,
    )


def _read_tool(
    events: Iterable[_Event],
    ref: Any,
    *,
    root: Path,
    output_offset: int = 0,
    output_limit: int = MAX_MESSAGE_BYTES,
) -> str:
    task_id, generation, turn, batch_index, session = _parse_tool_ref(ref)
    latest_tool: _Event | None = None
    latest_artifact: _Event | None = None
    sessions: set[str] = set()
    checkpoints: dict[str, _Event] = {}
    for event in events:
        matching_identity = (
            event.task_id == task_id
            and _tool_identity(event) == (task_id, generation, turn, batch_index)
            and (session is None or event.session == session)
        )
        if event.kind == "tool_event" and matching_identity:
            latest_tool = event
            sessions.add(event.session)
        elif event.kind == "tool_output_artifact" and matching_identity:
            latest_artifact = event
            sessions.add(event.session)
        if (
            event.kind == "checkpoint"
            and event.task_id == task_id
            and (session is None or event.session == session)
            and _int(event.payload, "generation") == generation
            and _int(event.payload, "turn") == turn
            and isinstance(event.payload.get("state_ref"), str)
        ):
            checkpoints[event.session] = event
    if latest_tool is None:
        raise BranchHistoryError(f"tool call not found: {ref}")
    if len(sessions) > 1:
        raise BranchHistoryError(
            "ambiguous tool ref across interactive turns; list tools for scoped refs"
        )
    event = latest_tool
    tool_name, tool_ok = _validate_tool_event(event)
    lines = [
        str(ref),
        f"branch={branch_ref(task_id)} generation={generation} turn={turn} "
        f"batch_index={batch_index}",
        f"tool={tool_name} ok={str(tool_ok).lower()} ",
        f"cmd={event.payload.get('cmd', '-')}",
    ]
    checkpoint = checkpoints.get(event.session)
    if checkpoint is None:
        raise BranchHistoryError(f"tool call has no matching checkpoint evidence: {ref}")
    state_ref = checkpoint.payload.get("state_ref")
    if not isinstance(state_ref, str) or not state_ref:
        raise BranchHistoryError(f"tool call checkpoint has no state_ref: {ref}")
    messages = _checkpoint_messages(
        Path(state_ref).expanduser(),
        task_id=task_id,
        generation=generation,
        turn=turn,
    )
    action, observation = _extract_tool_exchange(messages, tool_name, batch_index)
    if not action:
        raise BranchHistoryError(f"tool call has no matching assistant action: {ref}")
    if not observation:
        raise BranchHistoryError(f"tool call has no matching observation: {ref}")
    _validate_tool_observation(observation, tool_name, tool_ok)
    lines.extend(("assistant_action:", _bounded(action, MAX_MESSAGE_BYTES)))
    lines.extend(("tool_observation:", _bounded(observation, MAX_MESSAGE_BYTES)))
    if latest_artifact is not None:
        page, next_output_offset = _artifact_page(
            root,
            latest_artifact,
            offset=output_offset,
            limit=output_limit,
        )
        lines.extend(
            (
                "tool_output_artifact:",
                (
                    f"ref={latest_artifact.payload['output_ref']} "
                    f"sha256={latest_artifact.payload['output_sha256']} "
                    f"bytes={latest_artifact.payload['output_bytes']} offset={output_offset}"
                ),
                page,
            )
        )
        if next_output_offset is not None:
            lines.append(f"next_output_offset={next_output_offset}")
    return _bounded("\n".join(lines))


def _latest_transcript(events: Iterable[_Event], task_id: str, offset: int, limit: int) -> str:
    checkpoint = _checkpoint_event(events, task_id, None, None)
    if checkpoint is None:
        raise BranchHistoryError(f"branch has no retrievable checkpoint: {task_id}")
    state_ref = checkpoint.payload.get("state_ref")
    if not isinstance(state_ref, str):
        raise BranchHistoryError(f"branch checkpoint has no state_ref: {task_id}")
    messages = _checkpoint_messages(Path(state_ref).expanduser())
    lines = [f"branch={branch_ref(task_id)} messages={len(messages)}"]
    for index, message in enumerate(messages):
        content = _bounded(message["content"], MAX_MESSAGE_BYTES)
        lines.append(f"[{index}] {message['role']}\n{content}")
    return _page(lines[0], lines[1 + offset : 1 + offset + limit], offset, len(messages))


def query_branch_history(session_dir: Path | str, arguments: Mapping[str, Any]) -> str:
    """Execute one bounded branch-history query against existing artifacts."""
    action_value = arguments.get("action")
    if not isinstance(action_value, str):
        choices = ", ".join(action.value for action in HistoryAction)
        raise BranchHistoryError(f"action must be one of: {choices}")
    try:
        action = HistoryAction(action_value)
    except (TypeError, ValueError):
        choices = ", ".join(action.value for action in HistoryAction)
        raise BranchHistoryError(f"action must be one of: {choices}") from None
    root = Path(session_dir).expanduser().resolve()
    if re.fullmatch(r"turn-[0-9]+", root.name):
        root = root.parent
    events = _events(root)
    offset = _non_negative_offset(arguments.get("offset"))
    limit = _positive_limit(arguments.get("limit"))
    task_id = arguments.get("task_id")
    if task_id is not None and (not isinstance(task_id, str) or not task_id):
        raise BranchHistoryError("task_id must be a non-empty string")

    if action is HistoryAction.BRANCHES:
        return _list_branches(events, offset, limit)
    if action is HistoryAction.TOOLS:
        return _list_tools(events, task_id, offset, limit)
    if action is HistoryAction.TOOL:
        output_offset = _non_negative_offset(arguments.get("output_offset"))
        output_limit = _artifact_limit(arguments.get("output_limit"))
        return _read_tool(
            events,
            arguments.get("ref"),
            root=root,
            output_offset=output_offset,
            output_limit=output_limit,
        )
    if task_id is None:
        raise BranchHistoryError("branch_history action=transcript requires task_id")
    return _latest_transcript(events, task_id, offset, limit)


__all__ = [
    "BranchHistoryError",
    "HistoryAction",
    "branch_ref",
    "query_branch_history",
    "tool_ref",
]
