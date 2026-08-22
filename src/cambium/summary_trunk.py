"""Append-only semantic summaries for cache-friendly agent context trunks.

The active context is a stable two-message head, followed by immutable summary
entries and a small raw working tail.  Every summary entry covers one disjoint
raw tail exactly once.  Previous summaries are never summarized again.
"""

from __future__ import annotations

import hashlib
import json
from collections.abc import Mapping, Sequence
from dataclasses import asdict, dataclass
from typing import Any

SUMMARY_ENTRY_OPEN = "<cambium-summary-entry>\n"
SUMMARY_ENTRY_CLOSE = "\n</cambium-summary-entry>"
SUMMARY_CONTROL_OPEN = "<cambium-summary-control>\n"
SUMMARY_CONTROL_CLOSE = "\n</cambium-summary-control>"

SUMMARY_MAX_ITEMS = 32
SUMMARY_MAX_TEXT_BYTES = 2_000
SUMMARY_MAX_ENTRY_BYTES = 24 * 1024
_SUMMARY_DIGEST_LENGTH = 64

SUMMARY_LIST_FIELDS = (
    "decisions_added",
    "decisions_superseded",
    "facts_added",
    "facts_invalidated",
    "files_and_symbols_changed",
    "verification_results",
    "relevant_failed_approaches",
    "open_items",
)
SUMMARY_ENTRY_FIELDS = frozenset(
    {
        "type",
        "sequence",
        "source_sha256",
        "source_message_count",
        "through_turn",
        "objective",
        "outcome",
        *SUMMARY_LIST_FIELDS,
    }
)

SUMMARY_PROTOCOL_LINES = (
    "Two output modes exist; the final user control block selects the mode.",
    "- Normal mode: return one plan, tool_call, or finish action as specified below.",
    "- Summary mode: when the final message is <cambium-summary-control>, return "
    "exactly one summary_entry JSON object and no tool call, markdown, or prose.",
    "In summary mode, summarize only the raw messages after the last "
    "<cambium-summary-entry> and before the control block. Existing summary entries "
    "are immutable history: do not rewrite, restate, or summarize them.",
    "Discard transient execution noise: malformed actions, routine command mistakes, "
    "repeated reads, superseded outputs, and abandoned scratch plans. Preserve failed "
    "approaches only when they constrain future work.",
    "Use decisions_superseded and facts_invalidated to make changed conclusions explicit.",
    "The summary_entry object has exactly: type, sequence, source_sha256, "
    "source_message_count, through_turn, objective, outcome, decisions_added, "
    "decisions_superseded, facts_added, facts_invalidated, files_and_symbols_changed, "
    "verification_results, relevant_failed_approaches, open_items.",
)


class SummaryTrunkError(ValueError):
    """A summary trunk, control block, or model-produced entry is invalid."""


@dataclass(frozen=True, slots=True)
class SummaryEntry:
    """One immutable semantic delta over one exact raw message range."""

    type: str
    sequence: int
    source_sha256: str
    source_message_count: int
    through_turn: int
    objective: str
    outcome: str
    decisions_added: tuple[str, ...]
    decisions_superseded: tuple[str, ...]
    facts_added: tuple[str, ...]
    facts_invalidated: tuple[str, ...]
    files_and_symbols_changed: tuple[str, ...]
    verification_results: tuple[str, ...]
    relevant_failed_approaches: tuple[str, ...]
    open_items: tuple[str, ...]


@dataclass(frozen=True, slots=True)
class SummaryExpectation:
    """Values the model must copy from the summary control block exactly."""

    sequence: int
    source_sha256: str
    source_message_count: int
    through_turn: int


def _canonical_json_bytes(value: Any) -> bytes:
    try:
        return json.dumps(
            value,
            sort_keys=True,
            separators=(",", ":"),
            ensure_ascii=False,
            allow_nan=False,
        ).encode("utf-8")
    except (TypeError, ValueError, UnicodeEncodeError) as exc:
        raise SummaryTrunkError("summary data is not canonical JSON") from exc


def _copy_message(value: Any, location: str) -> dict[str, str]:
    if not isinstance(value, Mapping) or set(value) != {"role", "content"}:
        raise SummaryTrunkError(f"{location} must have exactly role/content")
    role = value.get("role")
    content = value.get("content")
    if role not in {"system", "user", "assistant", "tool"}:
        raise SummaryTrunkError(f"{location}.role is invalid")
    if not isinstance(content, str):
        raise SummaryTrunkError(f"{location}.content must be a string")
    return {"role": role, "content": content}


def raw_tail_sha256(messages: Sequence[Mapping[str, Any]]) -> str:
    """Content identity of the exact ordered raw tail being summarized."""
    copied = [
        _copy_message(message, f"raw_tail[{index}]")
        for index, message in enumerate(messages)
    ]
    return hashlib.sha256(_canonical_json_bytes(copied)).hexdigest()


def _bounded_text(value: Any, field: str, *, allow_empty: bool = False) -> str:
    if not isinstance(value, str):
        raise SummaryTrunkError(f"summary entry {field} must be a string")
    if not allow_empty and not value.strip():
        raise SummaryTrunkError(f"summary entry {field} must be non-empty")
    if len(value.encode("utf-8")) > SUMMARY_MAX_TEXT_BYTES:
        raise SummaryTrunkError(f"summary entry {field} exceeds the byte cap")
    return value


def _bounded_items(value: Any, field: str) -> tuple[str, ...]:
    if not isinstance(value, list):
        raise SummaryTrunkError(f"summary entry {field} must be a list")
    if len(value) > SUMMARY_MAX_ITEMS:
        raise SummaryTrunkError(f"summary entry {field} exceeds the item cap")
    items: list[str] = []
    for index, item in enumerate(value):
        items.append(_bounded_text(item, f"{field}[{index}]"))
    return tuple(items)


def _positive_int(value: Any, field: str, *, allow_zero: bool = False) -> int:
    minimum = 0 if allow_zero else 1
    if isinstance(value, bool) or not isinstance(value, int) or value < minimum:
        qualifier = "non-negative" if allow_zero else "positive"
        raise SummaryTrunkError(f"summary entry {field} must be a {qualifier} integer")
    return value


def _entry_from_mapping(value: Any) -> SummaryEntry:
    if not isinstance(value, Mapping):
        raise SummaryTrunkError("summary entry must be a JSON object")
    if set(value) != SUMMARY_ENTRY_FIELDS:
        missing = sorted(SUMMARY_ENTRY_FIELDS - set(value))
        unknown = sorted(set(value) - SUMMARY_ENTRY_FIELDS)
        details: list[str] = []
        if missing:
            details.append(f"missing={missing}")
        if unknown:
            details.append(f"unknown={unknown}")
        raise SummaryTrunkError("summary entry field set is invalid: " + "; ".join(details))
    entry_type = value.get("type")
    if entry_type != "summary_entry":
        raise SummaryTrunkError("summary entry type must be 'summary_entry'")
    digest = value.get("source_sha256")
    if (
        not isinstance(digest, str)
        or len(digest) != _SUMMARY_DIGEST_LENGTH
        or any(character not in "0123456789abcdef" for character in digest)
    ):
        raise SummaryTrunkError("summary entry source_sha256 must be lowercase sha256 hex")
    kwargs: dict[str, Any] = {
        "type": entry_type,
        "sequence": _positive_int(value.get("sequence"), "sequence"),
        "source_sha256": digest,
        "source_message_count": _positive_int(
            value.get("source_message_count"),
            "source_message_count",
            allow_zero=True,
        ),
        "through_turn": _positive_int(value.get("through_turn"), "through_turn"),
        "objective": _bounded_text(value.get("objective"), "objective"),
        "outcome": _bounded_text(value.get("outcome"), "outcome"),
    }
    for field in SUMMARY_LIST_FIELDS:
        kwargs[field] = _bounded_items(value.get(field), field)
    entry = SummaryEntry(**kwargs)
    if len(_canonical_json_bytes(entry_mapping(entry))) > SUMMARY_MAX_ENTRY_BYTES:
        raise SummaryTrunkError("summary entry exceeds the total byte cap")
    return entry


def entry_mapping(entry: SummaryEntry) -> dict[str, Any]:
    """Plain canonical JSON representation of one entry."""
    value = asdict(entry)
    for field in SUMMARY_LIST_FIELDS:
        value[field] = list(value[field])
    return value


def render_summary_message(entry: SummaryEntry) -> dict[str, str]:
    """Render one entry as immutable, delimited user-role data."""
    content = _canonical_json_bytes(entry_mapping(entry)).decode("utf-8")
    return {
        "role": "user",
        "content": SUMMARY_ENTRY_OPEN + content + SUMMARY_ENTRY_CLOSE,
    }


def _summary_payload(content: str) -> str | None:
    if not content.startswith(SUMMARY_ENTRY_OPEN) or not content.endswith(
        SUMMARY_ENTRY_CLOSE
    ):
        return None
    return content[len(SUMMARY_ENTRY_OPEN) : -len(SUMMARY_ENTRY_CLOSE)]


def parse_summary_message(message: Mapping[str, Any]) -> SummaryEntry | None:
    """Parse a rendered entry, or return None for an ordinary message."""
    copied = _copy_message(message, "summary_message")
    payload = _summary_payload(copied["content"])
    if payload is None:
        return None
    if copied["role"] != "user":
        raise SummaryTrunkError("summary entries must use the user role")
    try:
        decoded = json.loads(payload)
    except (json.JSONDecodeError, RecursionError) as exc:
        raise SummaryTrunkError("summary entry wrapper contains invalid JSON") from exc
    return _entry_from_mapping(decoded)


def partition_summary_trunk(
    messages: Sequence[Mapping[str, Any]],
    *,
    stable_head_messages: int = 2,
) -> tuple[list[dict[str, str]], list[dict[str, str]]]:
    """Split a context into stable head + contiguous summaries and raw tail.

    Legacy checkpoints are migrated safely: every message after the contiguous
    summary prefix is returned as raw tail and will be summarized at the next
    flush.  No existing message is mutated.
    """
    if stable_head_messages < 1:
        raise SummaryTrunkError("stable_head_messages must be positive")
    copied = [
        _copy_message(message, f"messages[{index}]")
        for index, message in enumerate(messages)
    ]
    if len(copied) < stable_head_messages:
        raise SummaryTrunkError("context is shorter than its stable head")
    if copied[0]["role"] != "system":
        raise SummaryTrunkError("summary trunk must start with a system message")
    trunk = list(copied[:stable_head_messages])
    expected_sequence = 1
    index = stable_head_messages
    while index < len(copied):
        entry = parse_summary_message(copied[index])
        if entry is None:
            break
        if entry.sequence != expected_sequence:
            raise SummaryTrunkError(
                "summary entry sequence is not contiguous: "
                f"expected {expected_sequence}, got {entry.sequence}"
            )
        trunk.append(copied[index])
        expected_sequence += 1
        index += 1
    return trunk, copied[index:]


def summary_entries(
    trunk_messages: Sequence[Mapping[str, Any]], *, stable_head_messages: int = 2
) -> tuple[SummaryEntry, ...]:
    """Validate and return all entries from a summary-only trunk."""
    trunk, raw_tail = partition_summary_trunk(
        trunk_messages, stable_head_messages=stable_head_messages
    )
    if raw_tail:
        raise SummaryTrunkError("summary trunk contains a non-summary raw tail")
    entries: list[SummaryEntry] = []
    for message in trunk[stable_head_messages:]:
        entry = parse_summary_message(message)
        if entry is None:  # pragma: no cover - partition already guarantees this
            raise SummaryTrunkError("summary trunk contains an ordinary message")
        entries.append(entry)
    return tuple(entries)


def semantic_summary_messages(
    checkpoint_messages: Sequence[Mapping[str, Any]],
    *,
    stable_head_messages: int = 2,
) -> list[dict[str, str]]:
    """Provider-neutral summary history from a summary-only checkpoint.

    A checkpoint with legacy raw transcript material is not exported to a cold
    provider as semantic memory; it first needs one normal trunk flush.
    """
    trunk, raw_tail = partition_summary_trunk(
        checkpoint_messages, stable_head_messages=stable_head_messages
    )
    if raw_tail:
        raise SummaryTrunkError("checkpoint is not a summary-only trunk")
    summaries = trunk[stable_head_messages:]
    if not summaries:
        raise SummaryTrunkError("checkpoint has no semantic summary entries")
    return summaries


def build_summary_request(
    trunk_messages: Sequence[Mapping[str, Any]],
    raw_tail: Sequence[Mapping[str, Any]],
    *,
    through_turn: int,
    stable_head_messages: int = 2,
) -> tuple[dict[str, Any], SummaryExpectation]:
    """Build a cache-friendly request that summarizes only ``raw_tail``."""
    trunk, unexpected_tail = partition_summary_trunk(
        trunk_messages, stable_head_messages=stable_head_messages
    )
    if unexpected_tail:
        raise SummaryTrunkError("build_summary_request requires a summary-only trunk")
    raw = [
        _copy_message(message, f"raw_tail[{index}]")
        for index, message in enumerate(raw_tail)
    ]
    if not raw:
        raise SummaryTrunkError("cannot summarize an empty raw tail")
    entries = summary_entries(trunk, stable_head_messages=stable_head_messages)
    expectation = SummaryExpectation(
        sequence=len(entries) + 1,
        source_sha256=raw_tail_sha256(raw),
        source_message_count=len(raw),
        through_turn=_positive_int(through_turn, "through_turn"),
    )
    control = {
        "type": "summarize_tail",
        "sequence": expectation.sequence,
        "source_sha256": expectation.source_sha256,
        "source_message_count": expectation.source_message_count,
        "through_turn": expectation.through_turn,
    }
    control_message = {
        "role": "user",
        "content": (
            SUMMARY_CONTROL_OPEN
            + _canonical_json_bytes(control).decode("utf-8")
            + SUMMARY_CONTROL_CLOSE
        ),
    }
    return {"messages": [*trunk, *raw, control_message]}, expectation


def parse_summary_response(content: str, expected: SummaryExpectation) -> SummaryEntry:
    """Validate model-owned summary CONTENT and stamp OUR identity onto it.

    The control block's sequence/hash/count/through_turn fields describe the
    range WE chose to summarize — bookkeeping the caller already knows.
    Requiring the model to echo them verbatim added no integrity (a prompt
    injection could copy them too) while failing small models constantly.
    Identity therefore comes from ``expected``; the model owns only the
    semantic delta, and content bounds are still enforced.
    """
    if not isinstance(content, str) or not content.strip():
        raise SummaryTrunkError("summary response must be non-empty JSON")
    try:
        decoded = json.loads(content)
    except (json.JSONDecodeError, RecursionError) as exc:
        raise SummaryTrunkError("summary response must be exactly one JSON object") from exc
    if not isinstance(decoded, dict):
        raise SummaryTrunkError("summary response must be exactly one JSON object")
    if decoded.get("type", "summary_entry") != "summary_entry":
        raise SummaryTrunkError("summary entry type must be 'summary_entry'")
    objective = decoded.get("objective")
    outcome = decoded.get("outcome")
    if not isinstance(objective, str) or not objective.strip():
        raise SummaryTrunkError(
            "summary response objective must be a non-empty string"
        )
    if not isinstance(outcome, str) or not outcome.strip():
        raise SummaryTrunkError("summary response outcome must be a non-empty string")

    def _items(field: str) -> tuple[str, ...]:
        value = decoded.get(field)
        if value is None:
            return ()
        if (
            not isinstance(value, list)
            or len(value) > SUMMARY_MAX_ITEMS
            or any(not isinstance(item, str) or not item for item in value)
        ):
            raise SummaryTrunkError(
                f"summary response {field} must be at most {SUMMARY_MAX_ITEMS} "
                "non-empty strings"
            )
        return tuple(value)

    return SummaryEntry(
        type="summary_entry",
        sequence=expected.sequence,
        source_sha256=expected.source_sha256,
        source_message_count=expected.source_message_count,
        through_turn=expected.through_turn,
        objective=objective.strip(),
        outcome=outcome.strip(),
        decisions_added=_items("decisions_added"),
        decisions_superseded=_items("decisions_superseded"),
        facts_added=_items("facts_added"),
        facts_invalidated=_items("facts_invalidated"),
        files_and_symbols_changed=_items("files_and_symbols_changed"),
        verification_results=_items("verification_results"),
        relevant_failed_approaches=_items("relevant_failed_approaches"),
        open_items=_items("open_items"),
    )


def append_summary_entry(
    trunk_messages: Sequence[Mapping[str, Any]], entry: SummaryEntry
) -> list[dict[str, str]]:
    """Append exactly one next entry; all prior bytes remain unchanged."""
    trunk, raw_tail = partition_summary_trunk(trunk_messages)
    if raw_tail:
        raise SummaryTrunkError("cannot append to a trunk with a raw tail")
    expected_sequence = len(summary_entries(trunk)) + 1
    if entry.sequence != expected_sequence:
        raise SummaryTrunkError(
            f"summary append expected sequence {expected_sequence}, got {entry.sequence}"
        )
    appended = [*trunk, render_summary_message(entry)]
    summary_entries(appended)
    return appended


__all__ = [
    "SUMMARY_PROTOCOL_LINES",
    "SummaryEntry",
    "SummaryExpectation",
    "SummaryTrunkError",
    "append_summary_entry",
    "build_summary_request",
    "entry_mapping",
    "parse_summary_message",
    "parse_summary_response",
    "partition_summary_trunk",
    "raw_tail_sha256",
    "render_summary_message",
    "semantic_summary_messages",
    "summary_entries",
]
