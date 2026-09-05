"""Deterministic, bounded model projection of :class:`BranchState`."""

from __future__ import annotations

import hashlib
import html
from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any

from .branch_state import _TERMINAL_LIFECYCLES, BranchState, Child

SITUATION_PROJECTION_VERSION = 1
SECTION_ORDER = (
    "MISSION",
    "AUTHORITY",
    "ACCEPTED",
    "DELTA",
    "OPEN",
    "CHILDREN",
    "RESOURCES",
    "ANCHORS",
)

DEFAULT_FRAME_BYTES = 12 * 1024
DEFAULT_SECTION_BYTES = 2 * 1024
DEFAULT_SECTION_ITEMS = 12


def _positive_int(value: Any, name: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
        raise ValueError(f"{name} must be a positive integer")
    return value


def _section_caps(value: Any, name: str) -> tuple[tuple[str, int], ...]:
    if value is None:
        return ()
    items = value.items() if isinstance(value, Mapping) else value
    try:
        pairs = list(items)
    except TypeError as exc:
        raise ValueError(f"{name} must be a mapping or pair sequence") from exc
    normalized: dict[str, int] = {}
    for pair in pairs:
        if not isinstance(pair, tuple | list) or len(pair) != 2:
            raise ValueError(f"{name} must contain section/value pairs")
        section, cap = pair
        if section not in SECTION_ORDER:
            raise ValueError(f"{name} contains unknown section {section!r}")
        normalized[section] = _positive_int(cap, f"{name}[{section}]")
    return tuple(sorted(normalized.items()))


@dataclass(frozen=True, slots=True, init=False)
class SituationFrameLimits:
    """Hard byte and item bounds for one SituationFrame.

    ``section_bytes`` and ``section_items`` may override the common section
    caps by canonical section name.  ``max_bytes`` and ``max_items`` are
    accepted as short aliases for callers constructing limits from a small
    configuration object.
    """

    max_frame_bytes: int
    max_section_bytes: int
    max_section_items: int
    section_bytes: tuple[tuple[str, int], ...]
    section_items: tuple[tuple[str, int], ...]

    def __init__(
        self,
        max_frame_bytes: int = DEFAULT_FRAME_BYTES,
        max_section_bytes: int = DEFAULT_SECTION_BYTES,
        max_section_items: int = DEFAULT_SECTION_ITEMS,
        section_bytes: Mapping[str, int] | tuple[tuple[str, int], ...] | None = None,
        section_items: Mapping[str, int] | tuple[tuple[str, int], ...] | None = None,
        *,
        max_bytes: int | None = None,
        max_items: int | None = None,
    ) -> None:
        if max_bytes is not None:
            if max_frame_bytes != DEFAULT_FRAME_BYTES and max_frame_bytes != max_bytes:
                raise ValueError("max_bytes conflicts with max_frame_bytes")
            max_frame_bytes = max_bytes
        if max_items is not None:
            if max_section_items != DEFAULT_SECTION_ITEMS and max_section_items != max_items:
                raise ValueError("max_items conflicts with max_section_items")
            max_section_items = max_items
        object.__setattr__(
            self, "max_frame_bytes", _positive_int(max_frame_bytes, "max_frame_bytes")
        )
        object.__setattr__(
            self,
            "max_section_bytes",
            _positive_int(max_section_bytes, "max_section_bytes"),
        )
        object.__setattr__(
            self,
            "max_section_items",
            _positive_int(max_section_items, "max_section_items"),
        )
        object.__setattr__(self, "section_bytes", _section_caps(section_bytes, "section_bytes"))
        object.__setattr__(self, "section_items", _section_caps(section_items, "section_items"))

    @property
    def max_bytes(self) -> int:
        """Short alias for the whole-frame byte cap."""

        return self.max_frame_bytes

    @property
    def max_items(self) -> int:
        """Short alias for the common per-section item cap."""

        return self.max_section_items

    def bytes_for(self, section: str) -> int:
        return dict(self.section_bytes).get(section, self.max_section_bytes)

    def items_for(self, section: str) -> int:
        return dict(self.section_items).get(section, self.max_section_items)

    @classmethod
    def from_mapping(cls, value: Mapping[str, Any]) -> SituationFrameLimits:
        """Build limits from a JSON-shaped mapping."""

        if not isinstance(value, Mapping):
            raise TypeError("situation frame limits must be a mapping")
        frame_bytes = value.get(
            "max_frame_bytes",
            value.get("whole_frame_bytes", value.get("max_bytes", DEFAULT_FRAME_BYTES)),
        )
        section_bytes_value = value.get("section_bytes")
        section_bytes = value.get("per_section_bytes")
        if isinstance(section_bytes_value, Mapping):
            section_bytes = section_bytes_value
            section_bytes_value = DEFAULT_SECTION_BYTES
        section_bytes_value = (
            DEFAULT_SECTION_BYTES if section_bytes_value is None else section_bytes_value
        )
        section_items_value = value.get("section_items")
        section_items = value.get("per_section_items")
        if isinstance(section_items_value, Mapping):
            section_items = section_items_value
            section_items_value = DEFAULT_SECTION_ITEMS
        section_items_value = (
            DEFAULT_SECTION_ITEMS if section_items_value is None else section_items_value
        )
        return cls(
            max_frame_bytes=frame_bytes,
            max_section_bytes=section_bytes_value,
            max_section_items=value.get(
                "max_section_items", value.get("max_items", section_items_value)
            ),
            section_bytes=section_bytes,
            section_items=section_items,
        )


def _coerce_limits(value: SituationFrameLimits | Mapping[str, Any] | None) -> SituationFrameLimits:
    if value is None:
        return SituationFrameLimits()
    if isinstance(value, SituationFrameLimits):
        return value
    if isinstance(value, Mapping):
        return SituationFrameLimits.from_mapping(value)
    raise TypeError("limits must be SituationFrameLimits, a mapping, or None")


def _text(value: Any) -> str:
    if value is None:
        return "unknown"
    if isinstance(value, bool):
        return str(value).lower()
    result = str(value).replace("\r", "\\r").replace("\n", "\\n")
    return html.escape(result, quote=False)


def _header_text(value: Any) -> str:
    return _text(value)[:128]


def _scalar(label: str, value: Any) -> str:
    return f"  {label}: {_text(value)}"


def _collection(label: str, values: tuple[str, ...] | list[str]) -> list[str]:
    return [f"  {label}[{index}]: {_text(value)}" for index, value in enumerate(values)]


def _mission_rows(state: BranchState) -> list[str]:
    return [
        _scalar("objective", state.mission.objective),
        *_collection("constraint", state.mission.constraints),
        *_collection("done_when", state.mission.done_when),
        *_collection("verification_contract", state.mission.verification_contract),
    ]


def _authority_rows(state: BranchState) -> list[str]:
    return [
        _scalar("repo", state.authority.repo),
        _scalar("worktree", state.authority.worktree),
        _scalar("branch", state.authority.branch),
        *_collection("writable_scope", state.authority.writable_scope),
        *_collection("tool", state.authority.tools),
        *_collection("authorized_provider", state.authority.authorized_providers),
    ]


def _accepted_rows(state: BranchState) -> list[str]:
    verification_rows = _collection("verification", state.knowledge.verifications)
    if not verification_rows:
        verification_rows = [_scalar("verification", "unknown")]
    return [
        _scalar("lifecycle", state.lifecycle.value),
        _scalar("context_epoch", state.context.epoch),
        _scalar("checkpoint_ref", state.context.checkpoint_ref),
        _scalar("context_lineage", state.context.lineage),
        _scalar("base_head", state.artifacts.base_head),
        _scalar("worktree_head", state.artifacts.worktree_head),
        _scalar("accepted_integration_head", state.artifacts.accepted_integration_head),
        _scalar("dirty", state.artifacts.dirty),
        _scalar("provider_lease", state.resources.provider_lease),
        *verification_rows,
    ]


def _delta_rows(state: BranchState) -> list[str]:
    result = state.result
    return [
        _scalar("last_meaningful_delta", state.control.last_meaningful_delta),
        _scalar("last_event", state.last_event_kind),
        _scalar("current_tool", state.current_tool),
        _scalar("result", result.status if result is not None else None),
        _scalar("failure_reason", result.failure_reason if result is not None else None),
        _scalar("summary", result.summary if result is not None else None),
    ]


def _open_rows(state: BranchState) -> list[str]:
    rows = [
        *_collection("obligation", sorted(state.control.open_obligations)),
        *_collection("blocker", sorted(state.control.blockers)),
    ]
    if state.context.last_error is not None:
        rows.append(_scalar("context_status", f"stale: {state.context.last_error}"))
    return rows


def _child_row(child: Child) -> str:
    values = [
        f"branch_id={_text(child.branch_id)}",
        f"lifecycle={_text(child.lifecycle.value)}",
        f"context_mode={_text(child.context_mode)}",
        f"placement={_text(child.placement)}",
        f"artifact_status={_text(child.artifact_status)}",
        f"provider={_text(child.provider)}",
        f"model={_text(child.model)}",
    ]
    if child.current_tool is not None:
        values.append(f"current_tool={_text(child.current_tool)}")
    if child.result is not None:
        values.append(f"result={_text(child.result.status)}")
    return "  child: " + " ".join(values)


def _children_rows(state: BranchState) -> list[str]:
    children = sorted(state.children, key=lambda child: (
        child.lifecycle in _TERMINAL_LIFECYCLES, child.admission_index, child.branch_id,
    ))
    return [_child_row(child) for child in children]


def _resources_rows(state: BranchState) -> list[str]:
    resources = state.resources
    usage = state.usage
    return [
        _scalar("remaining_turns", resources.remaining_turns),
        _scalar("remaining_wall_s", resources.remaining_wall_s),
        _scalar("context_pressure", resources.context_pressure),
        _scalar("uncached_token_pressure", resources.uncached_token_pressure),
        _scalar("provider_lease", resources.provider_lease),
        _scalar("cache_affinity", resources.cache_affinity),
        _scalar("cache_warmth", resources.cache_warmth),
        _scalar("quota_pressure", resources.quota_pressure),
        _scalar("cash_pressure", resources.cash_pressure),
        _scalar("delegation_overhead", resources.delegation_overhead),
        _scalar("alternative_lane_available", resources.alternative_lane_available),
        _scalar("calls", usage.calls),
        _scalar("input_tokens", usage.input_tokens),
        _scalar("output_tokens", usage.output_tokens),
        _scalar("cached_tokens", usage.cached_tokens),
        _scalar("estimated_cost_usd", usage.estimated_cost_usd),
    ]


def _anchors_rows(state: BranchState) -> list[str]:
    if state.anchors:
        return _collection("anchor", sorted(state.anchors))
    return [_scalar("anchor", "unknown")]


_ROW_BUILDERS = {
    "MISSION": _mission_rows,
    "AUTHORITY": _authority_rows,
    "ACCEPTED": _accepted_rows,
    "DELTA": _delta_rows,
    "OPEN": _open_rows,
    "CHILDREN": _children_rows,
    "RESOURCES": _resources_rows,
    "ANCHORS": _anchors_rows,
}


def _continuation_marker(section: str) -> str:
    action = "branches" if section == "CHILDREN" else "tools"
    return f"  [truncated {section}; branch_history(action={action}) for recorded evidence]"


def _line_bytes(lines: list[str]) -> int:
    return len(("\n".join(lines) + "\n").encode("utf-8"))


def _bounded_section(
    section: str, rows: list[str], state: BranchState, limits: SituationFrameLimits
) -> list[str]:
    byte_cap = limits.bytes_for(section)
    item_cap = limits.items_for(section)
    selected = rows[:item_cap]
    truncated = len(rows) > item_cap
    body = [section, *selected]
    if not truncated and _line_bytes(body) <= byte_cap:
        return body
    marker = _continuation_marker(section)
    while selected and _line_bytes([section, *selected, marker]) > byte_cap:
        selected.pop()
    if _line_bytes([section, marker]) > byte_cap:
        raise ValueError(
            f"section byte cap for {section} is too small for its history anchor"
        )
    return [section, *selected, marker]


def _frame_header(state: BranchState, digest: str) -> str:
    attributes = {
        "version": SITUATION_PROJECTION_VERSION,
        "source_watermark": state.source_watermark,
        "frame_sha256": digest,
        "branch_id": state.branch_id,
        "generation": state.generation,
        "context_epoch": state.context_epoch,
        "artifact_head": state.artifact_head,
    }
    encoded = " ".join(
        f'{key}="{html.escape(_header_text(value), quote=True)}"'
        for key, value in attributes.items()
    )
    return f"<cambium-situation {encoded}>"


def _frame_lines(state: BranchState, sections: Mapping[str, list[str]]) -> list[str]:
    payload_lines = [line for section in SECTION_ORDER for line in sections[section]]
    payload = "\n".join(payload_lines)
    digest = hashlib.sha256(payload.encode("utf-8")).hexdigest()
    return [_frame_header(state, digest), *payload_lines, "</cambium-situation>"]


def _frame_bytes(state: BranchState, sections: Mapping[str, list[str]]) -> int:
    return _line_bytes(_frame_lines(state, sections))


def render_situation_frame(
    state: BranchState,
    limits: SituationFrameLimits | Mapping[str, Any] | None = None,
) -> str:
    """Render one deterministic, bounded SituationFrame.

    The renderer reads only immutable ``BranchState`` values.  It preserves all
    mandatory section headers, applies per-section item/byte caps first, then
    removes lower-priority section rows when the whole-frame cap requires it.
    Every omitted section names an existing ``branch_history`` operation for
    retrieving recorded evidence; local watermarks are not tool arguments.
    """

    if not isinstance(state, BranchState):
        raise TypeError("state must be a BranchState")
    frame_limits = _coerce_limits(limits)
    sections = {
        section: _bounded_section(section, _ROW_BUILDERS[section](state), state, frame_limits)
        for section in SECTION_ORDER
    }
    if _frame_bytes(state, sections) > frame_limits.max_frame_bytes:
        for section in reversed(SECTION_ORDER):
            marker = _continuation_marker(section)
            if sections[section][1:] == [marker]:
                continue
            if len(sections[section]) <= 1:
                continue
            sections[section] = [section, marker]
            if _frame_bytes(state, sections) <= frame_limits.max_frame_bytes:
                break
    if _frame_bytes(state, sections) > frame_limits.max_frame_bytes:
        raise ValueError("whole-frame byte cap is too small for the mandatory frame structure")
    return "\n".join(_frame_lines(state, sections)) + "\n"


__all__ = [
    "DEFAULT_FRAME_BYTES",
    "DEFAULT_SECTION_BYTES",
    "DEFAULT_SECTION_ITEMS",
    "SECTION_ORDER",
    "SITUATION_PROJECTION_VERSION",
    "SituationFrameLimits",
    "render_situation_frame",
]
