"""Append-only semantic summaries for cache-friendly agent context trunks.

The active context is a stable two-message head, followed by immutable summary
entries and a small raw working tail.  Every summary entry covers one disjoint
raw tail exactly once.  Previous summaries are never summarized again.
"""

from __future__ import annotations

import hashlib
import json
import math
import re
from collections.abc import Mapping, Sequence
from dataclasses import asdict, dataclass, replace
from typing import Any

from .provider_scheduler import (
    CacheCapability,
    CastConfig,
    RolloverDecision,
    decide_rollover,
)

SUMMARY_ENTRY_OPEN = "<cambium-summary-entry>\n"
SUMMARY_ENTRY_CLOSE = "\n</cambium-summary-entry>"
SUMMARY_ENTRY_PROVENANCE = "cambium-summary-provenance: rendered-v1\n"
SUMMARY_CONTROL_OPEN = "<cambium-summary-control>\n"
SUMMARY_CONTROL_CLOSE = "\n</cambium-summary-control>"

SUMMARY_MAX_ITEMS = 32
SUMMARY_MAX_TEXT_BYTES = 2_000
SUMMARY_MAX_ENTRY_BYTES = 24 * 1024
SUMMARY_MAX_COERCE_DEPTH = 64
SUMMARY_TRUNCATION_MARKER = "…[truncated]"
_SUMMARY_DIGEST_LENGTH = 64
_SUMMARY_DEEP_PLACEHOLDER = "<deep:unrepresentable>"

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


@dataclass(frozen=True, slots=True)
class K0Projection:
    """The active semantic state carried into a new CAST cache epoch.

    K0 is deliberately represented with the same bounded semantic values as a
    normal :class:`SummaryEntry`.  It is a materialized view, not a new
    summary tier: superseded decisions and invalidated facts are omitted, and
    the source entries remain immutable historical storage.

    ``constraints`` is sourced from ``relevant_failed_approaches``.  That
    field is the existing summary format's durable representation for failed
    approaches which constrain future work, so K0 does not add a field to the
    wire summary schema.
    """

    decisions: tuple[str, ...]
    facts: tuple[str, ...]
    constraints: tuple[str, ...]
    verification_state: tuple[str, ...]
    open_work: tuple[str, ...]

    @property
    def verification_results(self) -> tuple[str, ...]:
        """Compatibility spelling used by the existing summary field."""
        return self.verification_state

    @property
    def open_items(self) -> tuple[str, ...]:
        """Compatibility spelling used by the existing summary field."""
        return self.open_work

    def as_mapping(self) -> dict[str, list[str]]:
        """Return a JSON-safe projection using K0's semantic names."""
        return {
            "decisions": list(self.decisions),
            "facts": list(self.facts),
            "constraints": list(self.constraints),
            "verification_state": list(self.verification_state),
            "open_work": list(self.open_work),
        }


def _canonical_json_bytes(value: Any) -> bytes:
    try:
        return json.dumps(
            value,
            sort_keys=True,
            separators=(",", ":"),
            ensure_ascii=False,
            allow_nan=False,
        ).encode("utf-8")
    except (TypeError, ValueError, UnicodeEncodeError, RecursionError) as exc:
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
        _copy_message(message, f"raw_tail[{index}]") for index, message in enumerate(messages)
    ]
    return hashlib.sha256(_canonical_json_bytes(copied)).hexdigest()


_SUMMARY_FORBIDDEN_MARKERS = (
    SUMMARY_ENTRY_OPEN.strip(),
    SUMMARY_ENTRY_CLOSE.strip(),
    SUMMARY_ENTRY_PROVENANCE.strip(),
    SUMMARY_CONTROL_OPEN.strip(),
    SUMMARY_CONTROL_CLOSE.strip(),
)

_SUMMARY_LIST_TRIM_ORDER = (
    "open_items",
    "relevant_failed_approaches",
    "verification_results",
    "files_and_symbols_changed",
    "facts_invalidated",
    "facts_added",
    "decisions_superseded",
    "decisions_added",
)
_SUMMARY_TEXT_TRIM_ORDER = (
    *SUMMARY_LIST_FIELDS,
    "objective",
    "outcome",
)


def _truncate_text(value: str, field: str, *, max_bytes: int) -> str:
    """Keep a visible, UTF-8-safe prefix of one model-owned text field."""
    try:
        encoded = value.encode("utf-8")
    except UnicodeEncodeError as exc:
        raise SummaryTrunkError(f"summary entry {field} must be valid UTF-8") from exc
    if len(encoded) <= max_bytes:
        return value

    marker = SUMMARY_TRUNCATION_MARKER
    marker_bytes = marker.encode("utf-8")
    if max_bytes < len(marker_bytes):
        raise SummaryTrunkError(
            f"summary entry {field} cannot fit the truncation marker within the byte cap"
        )
    prefix = encoded[: max_bytes - len(marker_bytes)].decode("utf-8", errors="ignore")
    return prefix + marker


def _bounded_text(value: Any, field: str, *, allow_empty: bool = False) -> str:
    if not isinstance(value, str):
        raise SummaryTrunkError(f"summary entry {field} must be a string")
    try:
        value.encode("utf-8")
    except UnicodeEncodeError:
        # Model-owned JSON can contain lone UTF-16 surrogates even though the
        # surrounding response is otherwise valid.  Keep compaction alive by
        # retaining the code point's printable escape rather than letting the
        # filesystem/provider boundary see an unencodable string.
        value = value.encode("utf-8", errors="backslashreplace").decode("ascii")
    if not allow_empty and not value.strip():
        raise SummaryTrunkError(f"summary entry {field} must be non-empty")
    for marker in _SUMMARY_FORBIDDEN_MARKERS:
        if marker and marker in value:
            raise SummaryTrunkError(
                f"summary entry {field} must not contain the reserved marker {marker!r}"
            )
    return _truncate_text(value, field, max_bytes=SUMMARY_MAX_TEXT_BYTES)


def _coerced_summary_text(item: Any) -> Any:
    """Coerce one untrusted summary item to text without losing information.

    Summary CONTENT is model-owned JSON, so list items are occasionally
    emitted as objects or numbers.  Compaction is recovery infrastructure:
    coerce such items to their canonical JSON spelling instead of killing
    the session.  Bounds and forbidden-marker checks still apply afterwards.
    Objects deeper than :data:`SUMMARY_MAX_COERCE_DEPTH` are represented by a
    bounded placeholder so recovery never recurses through attacker-shaped
    data.
    """
    if isinstance(item, str):
        return item

    # Walk containers iteratively.  ``json.dumps`` has its own recursion
    # guard, but checking first keeps a deeply nested JSON value out of that
    # implementation-specific limit and also handles cyclic values supplied
    # by direct callers of this recovery helper.
    pending: list[tuple[Any, int]] = [(item, 0)]
    seen_containers: set[int] = set()
    while pending:
        candidate, depth = pending.pop()
        if depth > SUMMARY_MAX_COERCE_DEPTH:
            return _SUMMARY_DEEP_PLACEHOLDER
        if isinstance(candidate, Mapping):
            identity = id(candidate)
            if identity in seen_containers:
                return _SUMMARY_DEEP_PLACEHOLDER
            seen_containers.add(identity)
            pending.extend((child, depth + 1) for child in candidate.values())
        elif isinstance(candidate, list | tuple):
            identity = id(candidate)
            if identity in seen_containers:
                return _SUMMARY_DEEP_PLACEHOLDER
            seen_containers.add(identity)
            pending.extend((child, depth + 1) for child in candidate)
    try:
        return json.dumps(item, sort_keys=True, ensure_ascii=False, allow_nan=False)
    except (TypeError, ValueError, UnicodeEncodeError, RecursionError):
        try:
            return str(item)
        except (TypeError, ValueError, UnicodeEncodeError, RecursionError):
            return _SUMMARY_DEEP_PLACEHOLDER


def _bounded_items(value: Any, field: str) -> tuple[str, ...]:
    """Normalize one untrusted summary list field.

    Models occasionally emit a bare scalar where a list belongs (live:
    'summary entry verification_results must be a list') or embed objects in
    the list ('...[0] must be a string').  Compaction is recovery
    infrastructure, so scalars are wrapped and non-string items are coerced
    to canonical JSON text rather than killing the session.
    """
    if isinstance(value, tuple | list):
        items_raw: list[Any] = list(value)
    elif value is None:
        items_raw = []
    else:
        # Strings, numbers, dicts — every scalar/object shape gets wrapped and
        # coerced below.  Live failures arrived as list-items-not-strings,
        # then bare strings, then objects; normalize all shapes uniformly.
        items_raw = [value]
    if len(items_raw) > SUMMARY_MAX_ITEMS:
        raise SummaryTrunkError(f"summary entry {field} exceeds the item cap")
    items: list[str] = []
    for index, item in enumerate(items_raw):
        coerced = _coerced_summary_text(item)
        items.append(_bounded_text(coerced, f"{field}[{index}]"))
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
        ),
        "through_turn": _positive_int(value.get("through_turn"), "through_turn"),
        "objective": _bounded_text(value.get("objective"), "objective"),
        "outcome": _bounded_text(value.get("outcome"), "outcome"),
    }
    for field in SUMMARY_LIST_FIELDS:
        kwargs[field] = _bounded_items(value.get(field), field)
    entry = SummaryEntry(**kwargs)
    return _fit_entry_size(entry)


def _entry_size_bytes(entry: SummaryEntry) -> int:
    return len(_canonical_json_bytes(entry_mapping(entry)))


def _replace_entry_field(entry: SummaryEntry, field: str, value: Any) -> SummaryEntry:
    return replace(entry, **{field: value})


def _shrink_entry_text_field(entry: SummaryEntry, field: str, index: int | None) -> SummaryEntry:
    """Shrink one already-bounded text field to its smallest visible form."""
    current = getattr(entry, field)
    current_field = f"{field}[{index}]" if index is not None else field
    if index is not None:
        current_value = current[index]
    else:
        current_value = current
    marker_bytes = len(SUMMARY_TRUNCATION_MARKER.encode("utf-8"))
    if len(current_value.encode("utf-8")) <= marker_bytes:
        return entry

    minimum = _truncate_text(current_value, current_field, max_bytes=marker_bytes)
    if index is not None:
        minimum_values = list(current)
        minimum_values[index] = minimum
        candidate = _replace_entry_field(entry, field, tuple(minimum_values))
    else:
        candidate = _replace_entry_field(entry, field, minimum)
    if _entry_size_bytes(candidate) > SUMMARY_MAX_ENTRY_BYTES:
        return candidate

    low = marker_bytes
    high = len(current_value.encode("utf-8"))
    best = candidate
    while low <= high:
        limit = (low + high) // 2
        shortened = _truncate_text(current_value, current_field, max_bytes=limit)
        if index is not None:
            values = list(current)
            values[index] = shortened
            candidate = _replace_entry_field(entry, field, tuple(values))
        else:
            candidate = _replace_entry_field(entry, field, shortened)
        if _entry_size_bytes(candidate) <= SUMMARY_MAX_ENTRY_BYTES:
            best = candidate
            low = limit + 1
        else:
            high = limit - 1
    return best


def _fit_entry_size(entry: SummaryEntry) -> SummaryEntry:
    """Trim low-priority list items before shortening core summary text."""
    if _entry_size_bytes(entry) <= SUMMARY_MAX_ENTRY_BYTES:
        return entry

    fitted = entry
    for field in _SUMMARY_LIST_TRIM_ORDER:
        items = getattr(fitted, field)
        while len(items) > 1 and _entry_size_bytes(fitted) > SUMMARY_MAX_ENTRY_BYTES:
            fitted = _replace_entry_field(fitted, field, items[:-1])
            items = getattr(fitted, field)
        if _entry_size_bytes(fitted) <= SUMMARY_MAX_ENTRY_BYTES:
            return fitted

    for field in _SUMMARY_TEXT_TRIM_ORDER:
        values = getattr(fitted, field)
        if field in SUMMARY_LIST_FIELDS:
            for index in range(len(values)):
                fitted = _shrink_entry_text_field(fitted, field, index)
                if _entry_size_bytes(fitted) <= SUMMARY_MAX_ENTRY_BYTES:
                    return fitted
        else:
            fitted = _shrink_entry_text_field(fitted, field, None)
            if _entry_size_bytes(fitted) <= SUMMARY_MAX_ENTRY_BYTES:
                return fitted

    if _entry_size_bytes(fitted) > SUMMARY_MAX_ENTRY_BYTES:
        raise SummaryTrunkError("summary entry exceeds the total byte cap")
    return fitted


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
        "content": (SUMMARY_ENTRY_OPEN + SUMMARY_ENTRY_PROVENANCE + content + SUMMARY_ENTRY_CLOSE),
    }


def _summary_payload(content: str) -> str | None:
    if not content.startswith(SUMMARY_ENTRY_OPEN) or not content.endswith(SUMMARY_ENTRY_CLOSE):
        return None
    payload = content[len(SUMMARY_ENTRY_OPEN) : -len(SUMMARY_ENTRY_CLOSE)]
    if not payload.startswith(SUMMARY_ENTRY_PROVENANCE):
        return None
    return payload[len(SUMMARY_ENTRY_PROVENANCE) :]


def parse_summary_message(message: Mapping[str, Any]) -> SummaryEntry | None:
    """Parse a rendered entry, or return None for an ordinary message.

    A wrapper-shaped message whose payload does not decode is treated as an
    ordinary message so one poisoned entry demotes the remainder to raw tail
    instead of permanently breaking partitioning; strict contexts still catch
    that via ``summary_entries``.
    """
    copied = _copy_message(message, "summary_message")
    payload = _summary_payload(copied["content"])
    if payload is None:
        return None
    if copied["role"] != "user":
        raise SummaryTrunkError("summary entries must use the user role")
    try:
        decoded = json.loads(payload)
    except (json.JSONDecodeError, RecursionError):
        return None
    return _entry_from_mapping(decoded)


_SEMANTIC_ID_RE = re.compile(r"^[DF]\d+$")


def _entry_id_sets(
    entry: SummaryEntry,
) -> tuple[set[str], set[str]]:
    """Split ID-shaped items into (added, referenced) sets; prose passes through."""
    added: set[str] = set()
    for item in (*entry.decisions_added, *entry.facts_added):
        if _SEMANTIC_ID_RE.match(item):
            added.add(item)
    referenced: set[str] = set()
    for item in (*entry.decisions_superseded, *entry.facts_invalidated):
        if _SEMANTIC_ID_RE.match(item):
            referenced.add(item)
    return added, referenced


def _validate_entry_refs(entry: SummaryEntry, seen_added: set[str]) -> None:
    """ID-shaped supersede/invalidation items must reference known earlier IDs.

    Prose-style items are exempt so pre-existing trunks stay readable.  Added
    IDs must be globally unique across segments.
    """
    added, referenced = _entry_id_sets(entry)
    unknown = sorted(referenced - added - seen_added)
    if unknown:
        raise SummaryTrunkError(
            "summary entry supersedes/invalidates unknown IDs: " + ", ".join(unknown)
        )
    duplicates = sorted(added & seen_added)
    if duplicates:
        raise SummaryTrunkError("summary entry re-adds existing IDs: " + ", ".join(duplicates))
    seen_added.update(added)


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
        _copy_message(message, f"messages[{index}]") for index, message in enumerate(messages)
    ]
    if len(copied) < stable_head_messages:
        raise SummaryTrunkError("context is shorter than its stable head")
    if copied[0]["role"] != "system":
        raise SummaryTrunkError("summary trunk must start with a system message")
    for index, message in enumerate(copied[:stable_head_messages]):
        if parse_summary_message(message) is not None:
            raise SummaryTrunkError(f"stable head message {index} must not be a summary entry")

    trunk = list(copied[:stable_head_messages])
    expected_sequence = 1
    seen_digests: set[str] = set()
    seen_semantic_ids: set[str] = set()
    previous_through_turn: int | None = None
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
        if entry.source_sha256 in seen_digests:
            raise SummaryTrunkError("summary entry source_sha256 must be unique")
        _validate_entry_refs(entry, seen_semantic_ids)
        if previous_through_turn is not None and entry.through_turn <= previous_through_turn:
            raise SummaryTrunkError("summary entry through_turn must increase monotonically")
        seen_digests.add(entry.source_sha256)
        previous_through_turn = entry.through_turn
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


def estimate_message_tokens(messages: Sequence[Mapping[str, Any]]) -> int:
    """Estimate serialized message tokens using the existing byte projection.

    Provider tokenizers are intentionally not pulled into the trunk layer.
    Four UTF-8 bytes per token is the same bounded approximation used by the
    CAST UI for context sizing; callers should treat the result as a policy
    hint, not provider accounting.
    """
    copied = [
        _copy_message(message, f"messages[{index}]") for index, message in enumerate(messages)
    ]
    if not copied:
        return 0
    return max(1, math.ceil(len(_canonical_json_bytes(copied)) / 4))


def summary_trunk_tokens(
    trunk_messages: Sequence[Mapping[str, Any]], *, stable_head_messages: int = 2
) -> int:
    """Return the approximate token size of a validated active trunk."""
    trunk, raw_tail = partition_summary_trunk(
        trunk_messages, stable_head_messages=stable_head_messages
    )
    if raw_tail:
        raise SummaryTrunkError("summary trunk contains a non-summary raw tail")
    return estimate_message_tokens(trunk)


def _semantic_item_key(item: str) -> str:
    """Use an explicit semantic ID when present, otherwise the item itself."""
    match = re.match(r"^([DF]\d+)(?=\b|:)", item)
    return match.group(1) if match is not None else item


def _active_semantic_items(
    entries: Sequence[SummaryEntry],
    added_fields: tuple[str, ...],
    invalidated_fields: tuple[str, ...],
) -> tuple[str, ...]:
    """Fold one add/invalidate pair without changing the summary schema."""
    active: dict[str, str] = {}
    for entry in entries:
        for field in added_fields:
            for item in getattr(entry, field):
                active[_semantic_item_key(item)] = item
        for field in invalidated_fields:
            for item in getattr(entry, field):
                active.pop(_semantic_item_key(item), None)
    return tuple(active.values())


def _unique_semantic_items(entries: Sequence[SummaryEntry], field: str) -> tuple[str, ...]:
    """Fold an append-only semantic field while retaining source order."""
    values: dict[str, str] = {}
    for entry in entries:
        for item in getattr(entry, field):
            values.setdefault(_semantic_item_key(item), item)
    return tuple(values.values())


def compile_k0_projection(entries: Sequence[SummaryEntry]) -> K0Projection:
    """Compile the active state of immutable summary segments into K0.

    The fold is intentionally conservative.  Decisions and facts honor the
    existing supersede/invalidation fields; constraints, verification state,
    and open work are append-only fields in the current summary format and are
    retained once.  No source entry is changed or discarded by this helper.
    """
    normalized = tuple(
        entry if isinstance(entry, SummaryEntry) else _entry_from_mapping(entry)
        for entry in entries
    )
    if not normalized:
        raise SummaryTrunkError("cannot compile K0 from an empty summary trunk")
    return K0Projection(
        decisions=_active_semantic_items(
            normalized,
            ("decisions_added",),
            ("decisions_superseded",),
        ),
        facts=_active_semantic_items(
            normalized,
            ("facts_added",),
            ("facts_invalidated",),
        ),
        constraints=_unique_semantic_items(normalized, "relevant_failed_approaches"),
        verification_state=_unique_semantic_items(normalized, "verification_results"),
        open_work=_unique_semantic_items(normalized, "open_items"),
    )


def _k0_source_sha256(entries: Sequence[SummaryEntry]) -> str:
    return hashlib.sha256(
        _canonical_json_bytes([entry_mapping(entry) for entry in entries])
    ).hexdigest()


def k0_entry(
    entries: Sequence[SummaryEntry], projection: K0Projection | None = None
) -> SummaryEntry:
    """Encode K0 with the existing bounded ``SummaryEntry`` wire shape."""
    normalized = tuple(
        entry if isinstance(entry, SummaryEntry) else _entry_from_mapping(entry)
        for entry in entries
    )
    if not normalized:
        raise SummaryTrunkError("cannot encode K0 from an empty summary trunk")
    active = projection if projection is not None else compile_k0_projection(normalized)
    entry = SummaryEntry(
        type="summary_entry",
        sequence=1,
        source_sha256=_k0_source_sha256(normalized),
        source_message_count=len(normalized),
        through_turn=max(item.through_turn for item in normalized),
        objective="CAST K0 active semantic projection",
        outcome=f"compacted {len(normalized)} immutable semantic segment(s)",
        decisions_added=active.decisions,
        decisions_superseded=(),
        facts_added=active.facts,
        facts_invalidated=(),
        files_and_symbols_changed=(),
        verification_results=active.verification_state,
        relevant_failed_approaches=active.constraints,
        open_items=active.open_work,
    )
    return _entry_from_mapping(entry_mapping(entry))


def is_k0_entry(entry: SummaryEntry) -> bool:
    """Return whether an entry is the CAST K0 materialized projection."""
    return entry.objective == "CAST K0 active semantic projection"


def rollover_summary_trunk(
    trunk_messages: Sequence[Mapping[str, Any]], *, stable_head_messages: int = 2
) -> tuple[list[dict[str, str]], K0Projection, tuple[SummaryEntry, ...]]:
    """Replace an active flat trunk with one K0 entry and preserve its head.

    The returned third value is the exact historical segment set used to build
    K0.  Callers use it for rollover provenance while retaining the original
    checkpoint/segment files unchanged.
    """
    trunk, raw_tail = partition_summary_trunk(
        trunk_messages, stable_head_messages=stable_head_messages
    )
    if raw_tail:
        raise SummaryTrunkError("cannot roll over a trunk with a raw tail")
    entries = summary_entries(trunk, stable_head_messages=stable_head_messages)
    if not entries:
        raise SummaryTrunkError("cannot roll over a trunk with no summary entries")
    compacted = entries
    replacement = k0_entry(compacted)
    return (
        [*trunk[:stable_head_messages], render_summary_message(replacement)],
        compile_k0_projection(compacted),
        compacted,
    )


def k0_rollover_decision(
    trunk_messages: Sequence[Mapping[str, Any]],
    cast_config: CastConfig,
    *,
    expected_remaining_calls: float,
    cache_capability: CacheCapability | Mapping[str, Any] | None,
    cache_expired: bool = True,
    event_sink: Any = None,
    task_id: str | None = None,
    epoch: int | None = None,
    checkpoint_ref: str | None = None,
) -> RolloverDecision:
    """Evaluate K0 economics from an immutable summary-only trunk.

    The function only reads and validates the supplied checkpoint projection.
    It computes the post-rollover prefix size without publishing a successor,
    then optionally sends one redacted ``cast_rollover_decision`` event to a
    callable sink or appends it to a list-like sink.
    """
    trunk, raw_tail = partition_summary_trunk(trunk_messages)
    if raw_tail:
        raise SummaryTrunkError("K0 rollover decision requires a summary-only trunk")
    entries = summary_entries(trunk)
    active_tokens = summary_trunk_tokens(trunk)
    new_tokens = active_tokens
    if entries:
        compacted, _projection, _historical = rollover_summary_trunk(trunk)
        new_tokens = summary_trunk_tokens(compacted)
    decision = decide_rollover(
        cast_config,
        len(entries),
        active_tokens,
        new_prefix_tokens=new_tokens,
        expected_remaining_calls=expected_remaining_calls,
        cache_capability=cache_capability,
        cache_expired=cache_expired,
    )
    if event_sink is not None:
        event = decision.event(
            task_id=task_id,
            epoch=epoch,
            checkpoint_ref=checkpoint_ref,
        )
        if callable(event_sink):
            event_sink(event)
        elif hasattr(event_sink, "append"):
            event_sink.append(event)
        else:
            raise TypeError("event_sink must be callable or appendable")
    return decision


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
    raw = [_copy_message(message, f"raw_tail[{index}]") for index, message in enumerate(raw_tail)]
    if not raw:
        raise SummaryTrunkError("cannot summarize an empty raw tail")
    entries = summary_entries(trunk, stable_head_messages=stable_head_messages)
    source_sha256 = raw_tail_sha256(raw)
    if any(entry.source_sha256 == source_sha256 for entry in entries):
        raise SummaryTrunkError("summary request source_sha256 is a duplicate")
    validated_through_turn = _positive_int(through_turn, "through_turn")
    if entries and validated_through_turn <= entries[-1].through_turn:
        raise SummaryTrunkError("summary request through_turn must increase monotonically")
    expectation = SummaryExpectation(
        sequence=len(entries) + 1,
        source_sha256=source_sha256,
        source_message_count=len(raw),
        through_turn=validated_through_turn,
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
    Identity therefore comes from ``expected``; the echoed ``type`` marker is
    likewise ignored and normalized to ``"summary_entry"`` whether it is
    missing or malformed.  The model owns only the semantic delta, and
    content bounds are still enforced.  Unknown fields remain rejected so
    arbitrary model content cannot cross the summary schema boundary as
    prompt-injection text.
    """
    if not isinstance(content, str) or not content.strip():
        raise SummaryTrunkError("summary response must be non-empty JSON")
    try:
        try:
            decoded = json.loads(content)
        except (json.JSONDecodeError, RecursionError) as exc:
            raise SummaryTrunkError("summary response must be exactly one JSON object") from exc
        if not isinstance(decoded, dict):
            raise SummaryTrunkError("summary response must be exactly one JSON object")
        # Unlike the echoed type marker, unknown fields are model-owned content
        # outside the schema.  Reject them rather than silently carrying
        # arbitrary prompt-injection text into durable summary history.
        unknown = set(decoded) - SUMMARY_ENTRY_FIELDS
        if unknown:
            raise SummaryTrunkError(
                f"summary response field set is invalid: unknown={sorted(unknown)}"
            )

        normalized = dict(decoded)
        normalized.update(
            {
                "type": "summary_entry",
                "sequence": expected.sequence,
                "source_sha256": expected.source_sha256,
                "source_message_count": expected.source_message_count,
                "through_turn": expected.through_turn,
                "objective": _bounded_text(decoded.get("objective"), "objective").strip(),
                "outcome": _bounded_text(decoded.get("outcome"), "outcome").strip(),
            }
        )
        for field in SUMMARY_LIST_FIELDS:
            value = decoded[field] if field in decoded else []
            normalized[field] = list(_bounded_items(value, field))
        return _entry_from_mapping(normalized)
    except SummaryTrunkError:
        raise
    except Exception as exc:
        # This is an untrusted model boundary.  Keep the public contract
        # closed even if a future normalizer or the JSON implementation adds a
        # new data-dependent failure mode.
        raise SummaryTrunkError("summary response could not be normalized") from exc


def append_summary_entry(
    trunk_messages: Sequence[Mapping[str, Any]], entry: SummaryEntry
) -> list[dict[str, str]]:
    """Append exactly one next entry; all prior bytes remain unchanged."""
    trunk, raw_tail = partition_summary_trunk(trunk_messages)
    if raw_tail:
        raise SummaryTrunkError("cannot append to a trunk with a raw tail")
    entries = summary_entries(trunk)
    validated_entry = _entry_from_mapping(entry_mapping(entry))
    expected_sequence = len(entries) + 1
    if validated_entry.sequence != expected_sequence:
        raise SummaryTrunkError(
            f"summary append expected sequence {expected_sequence}, got {validated_entry.sequence}"
        )
    if any(existing.source_sha256 == validated_entry.source_sha256 for existing in entries):
        raise SummaryTrunkError("summary append source_sha256 is a duplicate")
    seen_semantic_ids: set[str] = set()
    for existing in entries:
        _validate_entry_refs(existing, seen_semantic_ids)
    _validate_entry_refs(validated_entry, seen_semantic_ids)
    if entries and validated_entry.through_turn <= entries[-1].through_turn:
        raise SummaryTrunkError("summary append through_turn must increase monotonically")
    appended = [*trunk, render_summary_message(validated_entry)]
    summary_entries(appended)
    return appended


__all__ = [
    "SUMMARY_ENTRY_PROVENANCE",
    "SUMMARY_PROTOCOL_LINES",
    "K0Projection",
    "SummaryEntry",
    "SummaryExpectation",
    "SummaryTrunkError",
    "append_summary_entry",
    "build_summary_request",
    "compile_k0_projection",
    "entry_mapping",
    "estimate_message_tokens",
    "is_k0_entry",
    "k0_rollover_decision",
    "k0_entry",
    "parse_summary_message",
    "parse_summary_response",
    "partition_summary_trunk",
    "raw_tail_sha256",
    "render_summary_message",
    "rollover_summary_trunk",
    "semantic_summary_messages",
    "summary_entries",
    "summary_trunk_tokens",
]
