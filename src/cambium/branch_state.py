"""Canonical, read-only branch state projection.

The supervisor event log is the authority.  This module only folds event
records into immutable values; it does not open a session, inspect Git, or
perform any other I/O.  ``inspect_state`` is the small read path used by the
CLI and by later model/operator projections.
"""

from __future__ import annotations

import json
import math
from collections.abc import Callable, Iterable, Mapping
from dataclasses import dataclass, field, replace
from enum import StrEnum
from typing import Any
from urllib.parse import quote

SCHEMA_VERSION = 1


class Lifecycle(StrEnum):
    """Public lifecycle vocabulary shared by branch projections."""

    UNKNOWN = "unknown"
    QUEUED = "queued"
    STARTING = "starting"
    ACTIVE = "active"
    SUSPENDED = "suspended"
    JOINING = "joining"
    VERIFYING = "verifying"
    PUBLISHING = "publishing"
    SUCCEEDED = "succeeded"
    FAILED = "failed"
    CANCELLED = "cancelled"
    REJECTED = "rejected"


_LIFECYCLE_RANK = {
    Lifecycle.UNKNOWN: 0,
    Lifecycle.QUEUED: 1,
    Lifecycle.STARTING: 2,
    Lifecycle.ACTIVE: 3,
    Lifecycle.SUSPENDED: 4,
    Lifecycle.JOINING: 5,
    Lifecycle.VERIFYING: 6,
    Lifecycle.PUBLISHING: 7,
    Lifecycle.SUCCEEDED: 8,
    Lifecycle.FAILED: 8,
    Lifecycle.CANCELLED: 8,
    Lifecycle.REJECTED: 8,
}
_TERMINAL_LIFECYCLES = frozenset(
    {
        Lifecycle.SUCCEEDED,
        Lifecycle.FAILED,
        Lifecycle.CANCELLED,
        Lifecycle.REJECTED,
    }
)


@dataclass(frozen=True, slots=True)
class Identity:
    """Stable runtime identity and lifecycle for one branch."""

    session_id: str | None = None
    branch_id: str | None = None
    parent_branch_id: str | None = None
    generation: int | None = None
    lifecycle: Lifecycle = Lifecycle.UNKNOWN
    turn: int | None = None


@dataclass(frozen=True, slots=True)
class Mission:
    """Admitted task contract fields present in durable task records."""

    objective: str | None = None
    constraints: tuple[str, ...] = ()
    done_when: tuple[str, ...] = ()
    verification_contract: tuple[str, ...] = ()


@dataclass(frozen=True, slots=True)
class Authority:
    """Repository, worktree, branch, and declared effect scope."""

    repo: str | None = None
    worktree: str | None = None
    branch: str | None = None
    writable_scope: tuple[str, ...] = ()
    tools: tuple[str, ...] = ()
    authorized_providers: tuple[str, ...] = ()


@dataclass(frozen=True, slots=True)
class CacheDescriptor:
    """The redacted provider/cache descriptor on an epoch checkpoint."""

    provider: str | None = None
    model: str | None = None
    protocol: str | None = None
    reasoning_effort: str | None = None
    system_sha256: str | None = None
    tools_sha256: str | None = None
    prefix_sha256: str | None = None
    suffix_sha256: str | None = None
    full_sha256: str | None = None
    prefix_bytes: int | None = None
    message_count: int | None = None
    redacted: bool | None = None
    provider_boundary: tuple[tuple[str, Any], ...] = ()


@dataclass(frozen=True, slots=True)
class Context:
    """Accepted context epoch and bounded context-shape facts."""

    epoch: int | None = None
    checkpoint_ref: str | None = None
    lineage: str = "unknown"
    cache_key: CacheDescriptor | None = None
    folded_from_epoch: int | None = None
    summary_segments: int | None = None
    raw_tail_tokens: int | None = None
    raw_tail_bytes: int | None = None
    raw_tail_messages: int | None = None
    last_error: str | None = None


@dataclass(frozen=True, slots=True)
class Artifacts:
    """Git/artifact facts with separate accepted-head fields."""

    base_head: str | None = None
    worktree_head: str | None = None
    accepted_integration_head: str | None = None
    dirty: bool | None = None


@dataclass(frozen=True, slots=True)
class Control:
    """Current plan and bounded open-work projection."""

    plan: tuple[str, ...] = ()
    current_step: int | None = None
    open_obligations: tuple[str, ...] = ()
    blockers: tuple[str, ...] = ()
    last_meaningful_delta: str | None = None


@dataclass(frozen=True, slots=True)
class Knowledge:
    """Evidence-linked references reserved for current and future projections."""

    observations: tuple[str, ...] = ()
    claims: tuple[str, ...] = ()
    decisions: tuple[str, ...] = ()
    obligations: tuple[str, ...] = ()
    verifications: tuple[str, ...] = ()


@dataclass(frozen=True, slots=True)
class Usage:
    """Cumulative provider usage facts for this branch."""

    calls: int = 0
    summary_calls: int = 0
    failed_calls: int = 0
    input_tokens: int = 0
    output_tokens: int = 0
    cached_tokens: int = 0
    total_tokens: int = 0
    estimated_cost_usd: float = 0.0
    last_latency_s: float | None = None
    last_output_tokens_per_s: float | None = None
    provider_cache_hits: int = 0


@dataclass(frozen=True, slots=True)
class Resources:
    """Decision-relevant resource and provider facts."""

    remaining_turns: int | None = None
    remaining_wall_s: float | None = None
    context_pressure: str = "unknown"
    uncached_token_pressure: str = "unknown"
    provider: str | None = None
    model: str | None = None
    provider_lease: str | None = None
    cache_affinity: str = "unknown"
    cache_warmth: str = "unknown"
    quota_pressure: str = "unknown"
    cash_pressure: str = "unknown"
    delegation_overhead: str = "unknown"
    alternative_lane_available: bool | None = None


@dataclass(frozen=True, slots=True)
class TerminalAction:
    """Bounded terminal action emitted inside a result envelope."""

    type: str = "finish"
    objective_met: bool | None = None
    summary_present: bool = False
    summary: str = ""


@dataclass(frozen=True, slots=True)
class ResultEnvelope:
    """The supervisor's bounded projection of a worker result envelope."""

    status: str | None = None
    summary: str | None = None
    failure_reason: str | None = None
    commits: tuple[str, ...] = ()
    files_changed: tuple[str, ...] = ()
    diff_truncated: bool | None = None
    requires_commit: bool | None = None
    checkpoint_ref: str | None = None
    epoch: int | None = None
    terminal_action: TerminalAction | None = None
    provider_metadata: tuple[tuple[str, Any], ...] = ()


@dataclass(frozen=True, slots=True)
class ToolObservation:
    """One redacted durable tool invocation/result."""

    tool: str | None = None
    command: str | None = None
    turn: int | None = None
    batch_index: int | None = None
    batch_size: int | None = None
    ok: bool | None = None
    duration_ms: float | None = None
    evidence_ref: str | None = None


@dataclass(frozen=True, slots=True)
class Child:
    """A child branch in deterministic admission order."""

    branch_id: str
    parent_branch_id: str | None = None
    admission_index: int = 0
    kind: str | None = None
    generation: int | None = None
    turn: int | None = None
    epoch: int | None = None
    lifecycle: Lifecycle = Lifecycle.UNKNOWN
    context_mode: str = "unknown"
    placement: str = "unknown"
    lineage: str = "unknown"
    checkpoint_ref: str | None = None
    critical: bool | None = None
    provider: str | None = None
    model: str | None = None
    current_tool: str | None = None
    artifact_status: str = "unknown"
    accepted_artifact_head: str | None = None
    result: ResultEnvelope | None = None
    usage: Usage = field(default_factory=Usage)
    tool_events: tuple[ToolObservation, ...] = ()


@dataclass(frozen=True, slots=True)
class BranchState:
    """Immutable canonical state for one branch/event prefix."""

    schema_version: int = SCHEMA_VERSION
    source_watermark: int = 0
    identity: Identity = field(default_factory=Identity)
    mission: Mission = field(default_factory=Mission)
    authority: Authority = field(default_factory=Authority)
    context: Context = field(default_factory=Context)
    artifacts: Artifacts = field(default_factory=Artifacts)
    control: Control = field(default_factory=Control)
    knowledge: Knowledge = field(default_factory=Knowledge)
    children: tuple[Child, ...] = ()
    resources: Resources = field(default_factory=Resources)
    usage: Usage = field(default_factory=Usage)
    anchors: tuple[str, ...] = ()
    current_tool: str | None = None
    last_tool_output: str | None = None
    tool_events: tuple[ToolObservation, ...] = ()
    result: ResultEnvelope | None = None
    unknown_events: int = 0
    unknown_event_kinds: tuple[str, ...] = ()
    last_event_kind: str | None = None
    last_event_seq: int | None = None

    @property
    def version(self) -> int:
        """Alias for callers that use the reference document's ``version``."""

        return self.schema_version

    @property
    def task_id(self) -> str | None:
        return self.identity.branch_id

    @property
    def branch_id(self) -> str | None:
        return self.identity.branch_id

    @property
    def parent_task_id(self) -> str | None:
        return self.identity.parent_branch_id

    @property
    def generation(self) -> int | None:
        return self.identity.generation

    @property
    def lifecycle(self) -> Lifecycle:
        return self.identity.lifecycle

    @property
    def turn(self) -> int | None:
        return self.identity.turn

    @property
    def context_epoch(self) -> int | None:
        return self.context.epoch

    @property
    def checkpoint_ref(self) -> str | None:
        return self.context.checkpoint_ref

    @property
    def artifact_head(self) -> str | None:
        return self.artifacts.accepted_integration_head

    @property
    def accepted_artifact_head(self) -> str | None:
        return self.artifacts.accepted_integration_head

    @property
    def provider(self) -> str | None:
        return self.resources.provider

    @property
    def model(self) -> str | None:
        return self.resources.model

    @property
    def calls(self) -> int:
        return self.usage.calls

    @property
    def input_tokens(self) -> int:
        return self.usage.input_tokens

    @property
    def output_tokens(self) -> int:
        return self.usage.output_tokens

    @property
    def cached_tokens(self) -> int:
        return self.usage.cached_tokens

    @property
    def total_tokens(self) -> int:
        return self.usage.total_tokens

    @property
    def tool_event_count(self) -> int:
        return len(self.tool_events)

    @property
    def unknown_event_count(self) -> int:
        return self.unknown_events

    @property
    def last_tool(self) -> ToolObservation | None:
        return self.tool_events[-1] if self.tool_events else None

    def to_dict(self) -> dict[str, Any]:
        """Return the canonical JSON-compatible state object."""

        return {
            "schema_version": self.schema_version,
            "source_watermark": self.source_watermark,
            "identity": {
                "session_id": self.identity.session_id,
                "branch_id": self.identity.branch_id,
                "parent_branch_id": self.identity.parent_branch_id,
                "generation": self.identity.generation,
                "lifecycle": self.identity.lifecycle.value,
                "turn": self.identity.turn,
            },
            "mission": {
                "objective": self.mission.objective,
                "constraints": list(self.mission.constraints),
                "done_when": list(self.mission.done_when),
                "verification_contract": list(self.mission.verification_contract),
            },
            "authority": {
                "repo": self.authority.repo,
                "worktree": self.authority.worktree,
                "branch": self.authority.branch,
                "writable_scope": list(self.authority.writable_scope),
                "tools": list(self.authority.tools),
                "authorized_providers": list(self.authority.authorized_providers),
            },
            "context": _context_to_dict(self.context),
            "artifacts": {
                "base_head": self.artifacts.base_head,
                "worktree_head": self.artifacts.worktree_head,
                "accepted_integration_head": self.artifacts.accepted_integration_head,
                "dirty": self.artifacts.dirty,
            },
            "control": {
                "plan": list(self.control.plan),
                "current_step": self.control.current_step,
                "open_obligations": list(self.control.open_obligations),
                "blockers": list(self.control.blockers),
                "last_meaningful_delta": self.control.last_meaningful_delta,
            },
            "knowledge": {
                "observations": list(self.knowledge.observations),
                "claims": list(self.knowledge.claims),
                "decisions": list(self.knowledge.decisions),
                "obligations": list(self.knowledge.obligations),
                "verifications": list(self.knowledge.verifications),
            },
            "children": [_child_to_dict(child) for child in self.children],
            "resources": {
                "remaining_turns": self.resources.remaining_turns,
                "remaining_wall_s": self.resources.remaining_wall_s,
                "context_pressure": self.resources.context_pressure,
                "uncached_token_pressure": self.resources.uncached_token_pressure,
                "provider": self.resources.provider,
                "model": self.resources.model,
                "provider_lease": self.resources.provider_lease,
                "cache_affinity": self.resources.cache_affinity,
                "cache_warmth": self.resources.cache_warmth,
                "quota_pressure": self.resources.quota_pressure,
                "cash_pressure": self.resources.cash_pressure,
                "delegation_overhead": self.resources.delegation_overhead,
                "alternative_lane_available": self.resources.alternative_lane_available,
            },
            "usage": _usage_to_dict(self.usage),
            "anchors": list(self.anchors),
            "current_tool": self.current_tool,
            "last_tool_output": self.last_tool_output,
            "tool_events": [_tool_to_dict(tool_event) for tool_event in self.tool_events],
            "result": _result_to_dict(self.result) if self.result is not None else None,
            "unknown_events": self.unknown_events,
            "unknown_event_kinds": list(self.unknown_event_kinds),
            "last_event_kind": self.last_event_kind,
            "last_event_seq": self.last_event_seq,
        }

    def to_json(self) -> str:
        """Return deterministic canonical JSON for projection tests and CLI output."""

        return json.dumps(
            self.to_dict(),
            ensure_ascii=False,
            allow_nan=False,
            separators=(",", ":"),
            sort_keys=True,
        )

    @classmethod
    def from_dict(cls, document: Mapping[str, Any]) -> BranchState:
        """Build a state from one schema-versioned JSON object."""

        if not isinstance(document, Mapping):
            raise TypeError("branch state document must be an object")
        schema_version = document.get("schema_version", document.get("version"))
        if type(schema_version) is not int or schema_version != SCHEMA_VERSION:
            raise ValueError(f"unsupported branch state schema_version: {schema_version!r}")

        identity_data = _object(document.get("identity"))
        mission_data = _object(document.get("mission"))
        authority_data = _object(document.get("authority"))
        context_data = _object(document.get("context"))
        artifacts_data = _object(document.get("artifacts"))
        control_data = _object(document.get("control"))
        knowledge_data = _object(document.get("knowledge"))
        resources_data = _object(document.get("resources"))

        return cls(
            schema_version=schema_version,
            source_watermark=_required_nonnegative_int(document.get("source_watermark", 0)),
            identity=Identity(
                session_id=_optional_string(identity_data.get("session_id")),
                branch_id=_optional_string(identity_data.get("branch_id")),
                parent_branch_id=_optional_string(identity_data.get("parent_branch_id")),
                generation=_optional_nonnegative_int(identity_data.get("generation")),
                lifecycle=_parse_lifecycle(identity_data.get("lifecycle")),
                turn=_optional_nonnegative_int(identity_data.get("turn")),
            ),
            mission=Mission(
                objective=_optional_string(mission_data.get("objective")),
                constraints=_required_string_tuple(mission_data.get("constraints", ())),
                done_when=_required_string_tuple(mission_data.get("done_when", ())),
                verification_contract=_required_string_tuple(
                    mission_data.get("verification_contract", ())
                ),
            ),
            authority=Authority(
                repo=_optional_string(authority_data.get("repo")),
                worktree=_optional_string(authority_data.get("worktree")),
                branch=_optional_string(authority_data.get("branch")),
                writable_scope=_required_string_tuple(authority_data.get("writable_scope", ())),
                tools=_required_string_tuple(authority_data.get("tools", ())),
                authorized_providers=_required_string_tuple(
                    authority_data.get("authorized_providers", ())
                ),
            ),
            context=_context_from_dict(context_data),
            artifacts=Artifacts(
                base_head=_optional_string(artifacts_data.get("base_head")),
                worktree_head=_optional_string(artifacts_data.get("worktree_head")),
                accepted_integration_head=_optional_string(
                    artifacts_data.get("accepted_integration_head")
                ),
                dirty=_optional_bool(artifacts_data.get("dirty")),
            ),
            control=Control(
                plan=_required_string_tuple(control_data.get("plan", ())),
                current_step=_optional_nonnegative_int(control_data.get("current_step")),
                open_obligations=_required_string_tuple(control_data.get("open_obligations", ())),
                blockers=_required_string_tuple(control_data.get("blockers", ())),
                last_meaningful_delta=_optional_string(control_data.get("last_meaningful_delta")),
            ),
            knowledge=Knowledge(
                observations=_required_string_tuple(knowledge_data.get("observations", ())),
                claims=_required_string_tuple(knowledge_data.get("claims", ())),
                decisions=_required_string_tuple(knowledge_data.get("decisions", ())),
                obligations=_required_string_tuple(knowledge_data.get("obligations", ())),
                verifications=_required_string_tuple(knowledge_data.get("verifications", ())),
            ),
            children=tuple(
                _child_from_dict(item) for item in _required_sequence(document, "children")
            ),
            resources=Resources(
                remaining_turns=_optional_nonnegative_int(resources_data.get("remaining_turns")),
                remaining_wall_s=_optional_nonnegative_number(
                    resources_data.get("remaining_wall_s")
                ),
                context_pressure=_known_or_unknown_string(
                    resources_data.get("context_pressure"), "unknown"
                ),
                uncached_token_pressure=_known_or_unknown_string(
                    resources_data.get("uncached_token_pressure"), "unknown"
                ),
                provider=_optional_string(resources_data.get("provider")),
                model=_optional_string(resources_data.get("model")),
                provider_lease=_optional_string(resources_data.get("provider_lease")),
                cache_affinity=_known_or_unknown_string(
                    resources_data.get("cache_affinity"), "unknown"
                ),
                cache_warmth=_known_or_unknown_string(
                    resources_data.get("cache_warmth"), "unknown"
                ),
                quota_pressure=_known_or_unknown_string(
                    resources_data.get("quota_pressure"), "unknown"
                ),
                cash_pressure=_known_or_unknown_string(
                    resources_data.get("cash_pressure"), "unknown"
                ),
                delegation_overhead=_known_or_unknown_string(
                    resources_data.get("delegation_overhead"), "unknown"
                ),
                alternative_lane_available=_optional_bool(
                    resources_data.get("alternative_lane_available")
                ),
            ),
            usage=_usage_from_dict(_object(document.get("usage"))),
            anchors=_required_string_tuple(document.get("anchors", ())),
            current_tool=_optional_string(document.get("current_tool")),
            last_tool_output=_optional_string(document.get("last_tool_output")),
            tool_events=tuple(
                _tool_from_dict(item) for item in _required_sequence(document, "tool_events")
            ),
            result=(
                _result_from_dict(_object(document.get("result")))
                if document.get("result") is not None
                else None
            ),
            unknown_events=_required_nonnegative_int(document.get("unknown_events", 0)),
            unknown_event_kinds=_required_string_tuple(document.get("unknown_event_kinds", ())),
            last_event_kind=_optional_string(document.get("last_event_kind")),
            last_event_seq=_optional_nonnegative_int(document.get("last_event_seq")),
        )

    @classmethod
    def from_json(cls, document: str | bytes | bytearray | Mapping[str, Any]) -> BranchState:
        """Parse one schema-versioned JSON state object."""

        if isinstance(document, Mapping):
            value: Any = document
        else:
            value = json.loads(document)
        if not isinstance(value, Mapping):
            raise ValueError("branch state JSON must contain an object")
        return cls.from_dict(value)


def _optional_string(value: Any) -> str | None:
    return value if isinstance(value, str) and value else None


def _required_string(value: Any, field_name: str) -> str:
    result = _optional_string(value)
    if result is None:
        raise ValueError(f"{field_name} must be a non-empty string")
    return result


def _required_string_tuple(value: Any) -> tuple[str, ...]:
    if isinstance(value, str):
        return (value,) if value else ()
    if not isinstance(value, list | tuple):
        raise ValueError("state string collection must be an array")
    if any(not isinstance(item, str) or not item for item in value):
        raise ValueError("state string collection contains a non-string or empty value")
    return tuple(value)


def _required_nonnegative_int(value: Any) -> int:
    if type(value) is not int or value < 0:
        raise ValueError("state integer must be a non-negative integer")
    return value


def _optional_nonnegative_int(value: Any) -> int | None:
    if value is None:
        return None
    return _required_nonnegative_int(value)


def _optional_nonnegative_number(value: Any) -> float | None:
    if value is None:
        return None
    if isinstance(value, bool) or not isinstance(value, int | float):
        raise ValueError("state number must be numeric")
    number = float(value)
    if not math.isfinite(number) or number < 0:
        raise ValueError("state number must be finite and non-negative")
    return number


def _optional_bool(value: Any) -> bool | None:
    if value is None:
        return None
    if type(value) is not bool:
        raise ValueError("state boolean must be boolean or null")
    return value


def _known_or_unknown_string(value: Any, default: str) -> str:
    return value if isinstance(value, str) and value else default


def _parse_lifecycle(value: Any) -> Lifecycle:
    try:
        return Lifecycle(value)
    except (TypeError, ValueError):
        return Lifecycle.UNKNOWN


def _object(value: Any) -> Mapping[str, Any]:
    if value is None:
        return {}
    if not isinstance(value, Mapping):
        raise ValueError("state section must be an object")
    return value


def _required_sequence(document: Mapping[str, Any], key: str) -> list[Any] | tuple[Any, ...]:
    value = document.get(key, ())
    if not isinstance(value, list | tuple):
        raise ValueError(f"state {key} must be an array")
    return value


def _json_copy(value: Any) -> Any:
    """Copy only JSON values and make non-finite numbers explicit unknowns."""

    if value is None or isinstance(value, str | bool | int):
        return value
    if isinstance(value, float):
        return value if math.isfinite(value) else None
    if isinstance(value, Mapping):
        return {
            str(key): _json_copy(item)
            for key, item in sorted(value.items(), key=lambda pair: str(pair[0]))
        }
    if isinstance(value, list | tuple):
        return [_json_copy(item) for item in value]
    return repr(value)


def _mapping_pairs(value: Any) -> tuple[tuple[str, Any], ...]:
    if not isinstance(value, Mapping):
        return ()
    return tuple(
        (str(key), _json_copy(item))
        for key, item in sorted(value.items(), key=lambda pair: str(pair[0]))
    )


def _pairs_mapping(value: tuple[tuple[str, Any], ...]) -> dict[str, Any]:
    return {key: _json_copy(item) for key, item in value}


def _cache_from_value(value: Any) -> CacheDescriptor | None:
    if not isinstance(value, Mapping):
        return None
    return CacheDescriptor(
        provider=_optional_string(value.get("provider")),
        model=_optional_string(value.get("model")),
        protocol=_optional_string(value.get("protocol")),
        reasoning_effort=_optional_string(value.get("reasoning_effort")),
        system_sha256=_optional_string(value.get("system_sha256")),
        tools_sha256=_optional_string(value.get("tools_sha256")),
        prefix_sha256=_optional_string(value.get("prefix_sha256")),
        suffix_sha256=_optional_string(value.get("suffix_sha256")),
        full_sha256=_optional_string(value.get("full_sha256")),
        prefix_bytes=_optional_nonnegative_int(value.get("prefix_bytes")),
        message_count=_optional_nonnegative_int(value.get("message_count")),
        redacted=_optional_bool(value.get("redacted")),
        provider_boundary=_mapping_pairs(value.get("provider_boundary")),
    )


def _cache_to_dict(value: CacheDescriptor) -> dict[str, Any]:
    return {
        "provider": value.provider,
        "model": value.model,
        "protocol": value.protocol,
        "reasoning_effort": value.reasoning_effort,
        "system_sha256": value.system_sha256,
        "tools_sha256": value.tools_sha256,
        "prefix_sha256": value.prefix_sha256,
        "suffix_sha256": value.suffix_sha256,
        "full_sha256": value.full_sha256,
        "prefix_bytes": value.prefix_bytes,
        "message_count": value.message_count,
        "redacted": value.redacted,
        "provider_boundary": _pairs_mapping(value.provider_boundary),
    }


def _cache_from_dict(value: Mapping[str, Any]) -> CacheDescriptor:
    return _cache_from_value(value) or CacheDescriptor()


def _context_to_dict(value: Context) -> dict[str, Any]:
    return {
        "epoch": value.epoch,
        "checkpoint_ref": value.checkpoint_ref,
        "lineage": value.lineage,
        "cache_key": _cache_to_dict(value.cache_key) if value.cache_key is not None else None,
        "folded_from_epoch": value.folded_from_epoch,
        "summary_segments": value.summary_segments,
        "raw_tail_tokens": value.raw_tail_tokens,
        "raw_tail_bytes": value.raw_tail_bytes,
        "raw_tail_messages": value.raw_tail_messages,
        "last_error": value.last_error,
    }


def _context_from_dict(value: Mapping[str, Any]) -> Context:
    cache_key = value.get("cache_key")
    return Context(
        epoch=_optional_nonnegative_int(value.get("epoch")),
        checkpoint_ref=_optional_string(value.get("checkpoint_ref")),
        lineage=_known_or_unknown_string(value.get("lineage"), "unknown"),
        cache_key=_cache_from_dict(_object(cache_key)) if cache_key is not None else None,
        folded_from_epoch=_optional_nonnegative_int(value.get("folded_from_epoch")),
        summary_segments=_optional_nonnegative_int(value.get("summary_segments")),
        raw_tail_tokens=_optional_nonnegative_int(value.get("raw_tail_tokens")),
        raw_tail_bytes=_optional_nonnegative_int(value.get("raw_tail_bytes")),
        raw_tail_messages=_optional_nonnegative_int(value.get("raw_tail_messages")),
        last_error=_optional_string(value.get("last_error")),
    )


def _usage_to_dict(value: Usage) -> dict[str, Any]:
    return {
        "calls": value.calls,
        "summary_calls": value.summary_calls,
        "failed_calls": value.failed_calls,
        "input_tokens": value.input_tokens,
        "output_tokens": value.output_tokens,
        "cached_tokens": value.cached_tokens,
        "total_tokens": value.total_tokens,
        "estimated_cost_usd": value.estimated_cost_usd,
        "last_latency_s": value.last_latency_s,
        "last_output_tokens_per_s": value.last_output_tokens_per_s,
        "provider_cache_hits": value.provider_cache_hits,
    }


def _usage_from_dict(value: Mapping[str, Any]) -> Usage:
    return Usage(
        calls=_required_nonnegative_int(value.get("calls", 0)),
        summary_calls=_required_nonnegative_int(value.get("summary_calls", 0)),
        failed_calls=_required_nonnegative_int(value.get("failed_calls", 0)),
        input_tokens=_required_nonnegative_int(value.get("input_tokens", 0)),
        output_tokens=_required_nonnegative_int(value.get("output_tokens", 0)),
        cached_tokens=_required_nonnegative_int(value.get("cached_tokens", 0)),
        total_tokens=_required_nonnegative_int(value.get("total_tokens", 0)),
        estimated_cost_usd=(
            _optional_nonnegative_number(value.get("estimated_cost_usd", 0.0)) or 0.0
        ),
        last_latency_s=_optional_nonnegative_number(value.get("last_latency_s")),
        last_output_tokens_per_s=_optional_nonnegative_number(
            value.get("last_output_tokens_per_s")
        ),
        provider_cache_hits=_required_nonnegative_int(value.get("provider_cache_hits", 0)),
    )


def _terminal_action_to_dict(value: TerminalAction) -> dict[str, Any]:
    return {
        "type": value.type,
        "objective_met": value.objective_met,
        "summary_present": value.summary_present,
        "summary": value.summary,
    }


def _terminal_action_from_value(value: Any) -> TerminalAction | None:
    if not isinstance(value, Mapping):
        return None
    action_type = _optional_string(value.get("type"))
    if action_type is None:
        return None
    return TerminalAction(
        type=action_type,
        objective_met=_optional_bool(value.get("objective_met")),
        summary_present=bool(value.get("summary_present", False)),
        summary=_optional_string(value.get("summary")) or "",
    )


def _result_to_dict(value: ResultEnvelope) -> dict[str, Any]:
    return {
        "status": value.status,
        "summary": value.summary,
        "failure_reason": value.failure_reason,
        "commits": list(value.commits),
        "files_changed": list(value.files_changed),
        "diff_truncated": value.diff_truncated,
        "requires_commit": value.requires_commit,
        "checkpoint_ref": value.checkpoint_ref,
        "epoch": value.epoch,
        "terminal_action": (
            _terminal_action_to_dict(value.terminal_action)
            if value.terminal_action is not None
            else None
        ),
        "provider_metadata": _pairs_mapping(value.provider_metadata),
    }


def _result_from_dict(value: Mapping[str, Any]) -> ResultEnvelope:
    terminal_action = value.get("terminal_action")
    return ResultEnvelope(
        status=_optional_string(value.get("status")),
        summary=_optional_string(value.get("summary")),
        failure_reason=_optional_string(value.get("failure_reason")),
        commits=_required_string_tuple(value.get("commits", ())),
        files_changed=_required_string_tuple(value.get("files_changed", ())),
        diff_truncated=_optional_bool(value.get("diff_truncated")),
        requires_commit=_optional_bool(value.get("requires_commit")),
        checkpoint_ref=_optional_string(value.get("checkpoint_ref")),
        epoch=_optional_nonnegative_int(value.get("epoch")),
        terminal_action=_terminal_action_from_value(terminal_action),
        provider_metadata=_mapping_pairs(value.get("provider_metadata")),
    )


def _tool_to_dict(value: ToolObservation) -> dict[str, Any]:
    return {
        "tool": value.tool,
        "command": value.command,
        "turn": value.turn,
        "batch_index": value.batch_index,
        "batch_size": value.batch_size,
        "ok": value.ok,
        "duration_ms": value.duration_ms,
        "evidence_ref": value.evidence_ref,
    }


def _tool_from_dict(value: Any) -> ToolObservation:
    data = _object(value)
    return ToolObservation(
        tool=_optional_string(data.get("tool")),
        command=_optional_string(data.get("command")),
        turn=_optional_nonnegative_int(data.get("turn")),
        batch_index=_optional_nonnegative_int(data.get("batch_index")),
        batch_size=_optional_nonnegative_int(data.get("batch_size")),
        ok=_optional_bool(data.get("ok")),
        duration_ms=_optional_nonnegative_number(data.get("duration_ms")),
        evidence_ref=_optional_string(data.get("evidence_ref")),
    )


def _child_to_dict(value: Child) -> dict[str, Any]:
    return {
        "branch_id": value.branch_id,
        "parent_branch_id": value.parent_branch_id,
        "admission_index": value.admission_index,
        "kind": value.kind,
        "generation": value.generation,
        "turn": value.turn,
        "epoch": value.epoch,
        "lifecycle": value.lifecycle.value,
        "context_mode": value.context_mode,
        "placement": value.placement,
        "lineage": value.lineage,
        "checkpoint_ref": value.checkpoint_ref,
        "critical": value.critical,
        "provider": value.provider,
        "model": value.model,
        "current_tool": value.current_tool,
        "artifact_status": value.artifact_status,
        "accepted_artifact_head": value.accepted_artifact_head,
        "result": _result_to_dict(value.result) if value.result is not None else None,
        "usage": _usage_to_dict(value.usage),
        "tool_events": [_tool_to_dict(tool_event) for tool_event in value.tool_events],
    }


def _child_from_dict(value: Any) -> Child:
    data = _object(value)
    branch_id = _required_string(data.get("branch_id"), "child.branch_id")
    result = data.get("result")
    return Child(
        branch_id=branch_id,
        parent_branch_id=_optional_string(data.get("parent_branch_id")),
        admission_index=_required_nonnegative_int(data.get("admission_index", 0)),
        kind=_optional_string(data.get("kind")),
        generation=_optional_nonnegative_int(data.get("generation")),
        turn=_optional_nonnegative_int(data.get("turn")),
        epoch=_optional_nonnegative_int(data.get("epoch")),
        lifecycle=_parse_lifecycle(data.get("lifecycle")),
        context_mode=_known_or_unknown_string(data.get("context_mode"), "unknown"),
        placement=_known_or_unknown_string(data.get("placement"), "unknown"),
        lineage=_known_or_unknown_string(data.get("lineage"), "unknown"),
        checkpoint_ref=_optional_string(data.get("checkpoint_ref")),
        critical=_optional_bool(data.get("critical")),
        provider=_optional_string(data.get("provider")),
        model=_optional_string(data.get("model")),
        current_tool=_optional_string(data.get("current_tool")),
        artifact_status=_known_or_unknown_string(data.get("artifact_status"), "unknown"),
        accepted_artifact_head=_optional_string(data.get("accepted_artifact_head")),
        result=_result_from_dict(_object(result)) if result is not None else None,
        usage=_usage_from_dict(_object(data.get("usage"))),
        tool_events=tuple(
            _tool_from_dict(item) for item in _required_sequence(data, "tool_events")
        ),
    )


def _finite_nonnegative(value: Any) -> float | None:
    if isinstance(value, bool) or not isinstance(value, int | float):
        return None
    number = float(value)
    return number if math.isfinite(number) and number >= 0 else None


def _nonnegative_int(value: Any) -> int | None:
    number = _finite_nonnegative(value)
    return None if number is None else int(number)


def _value(values: Mapping[str, Any], *keys: str) -> Any:
    for key in keys:
        if key in values:
            return values[key]
    return None


def _has_value(values: Mapping[str, Any], *keys: str) -> tuple[bool, Any]:
    for key in keys:
        if key in values:
            return True, values[key]
    return False, None


def _strings_value(values: Mapping[str, Any], *keys: str) -> tuple[str, ...] | None:
    found, value = _has_value(values, *keys)
    if not found:
        return None
    if isinstance(value, str):
        return (value,) if value else ()
    if isinstance(value, list | tuple):
        return tuple(item for item in value if isinstance(item, str) and item)
    return ()


def _task_id(values: Mapping[str, Any]) -> str | None:
    return _optional_string(values.get("task_id"))


def _parent_id(values: Mapping[str, Any]) -> str | None:
    return _optional_string(_value(values, "parent_task_id", "parent_branch_id"))


def _generation(values: Mapping[str, Any]) -> int | None:
    return _nonnegative_int(values.get("generation"))


def _turn(values: Mapping[str, Any]) -> int | None:
    return _nonnegative_int(values.get("turn"))


def _event_values(event: Mapping[str, Any]) -> dict[str, Any]:
    payload = event.get("payload")
    values = dict(payload) if isinstance(payload, Mapping) else {}
    for key, value in event.items():
        if key not in {"kind", "type", "seq", "payload", "ts", "monotonic_ms"}:
            values.setdefault(key, value)
    return values


def _event_kind(event: Any) -> str | None:
    if not isinstance(event, Mapping):
        return None
    value = event.get("kind", event.get("type"))
    return value if isinstance(value, str) and value else None


def _event_record(kind: str, event: Mapping[str, Any]) -> dict[str, Any]:
    values = _event_values(event)
    values.setdefault("kind", kind)
    return values


def _advance_metadata(state: BranchState, event: Any, kind: str | None) -> BranchState:
    if not isinstance(event, Mapping):
        return state
    sequence = _nonnegative_int(event.get("seq"))
    if sequence == 0:
        sequence = None
    return replace(
        state,
        source_watermark=max(state.source_watermark, sequence or 0),
        last_event_kind=kind,
        last_event_seq=sequence if sequence is not None else state.last_event_seq,
    )


def _prepare_identity(
    state: BranchState, values: Mapping[str, Any], *, prefer_parent: bool = False
) -> BranchState:
    task_id = _task_id(values)
    parent_id = _parent_id(values)
    candidate = _optional_string(values.get("branch_id")) or task_id
    if prefer_parent and parent_id is not None:
        candidate = parent_id
    identity = state.identity
    session_id = _optional_string(values.get("session_id")) or identity.session_id
    branch_id = identity.branch_id or candidate
    if branch_id is None and task_id is not None:
        branch_id = task_id
    if (
        identity.session_id == session_id
        and identity.branch_id == branch_id
        and identity.parent_branch_id == identity.parent_branch_id
    ):
        return state
    return replace(
        state,
        identity=replace(
            identity,
            session_id=session_id,
            branch_id=branch_id,
        ),
    )


def _find_child_index(state: BranchState, task_id: str | None) -> int | None:
    if task_id is None or task_id == state.identity.branch_id:
        return None
    for index, child in enumerate(state.children):
        if child.branch_id == task_id:
            return index
    return None


def _ensure_child(
    state: BranchState,
    child_id: str,
    parent_id: str | None,
    *,
    kind: str | None = None,
) -> BranchState:
    if any(child.branch_id == child_id for child in state.children):
        return state
    child = Child(
        branch_id=child_id,
        parent_branch_id=parent_id,
        admission_index=len(state.children),
        kind=kind,
    )
    return replace(state, children=(*state.children, child))


def _replace_child(state: BranchState, child_id: str, child: Child) -> BranchState:
    children = list(state.children)
    for index, existing in enumerate(children):
        if existing.branch_id == child_id:
            children[index] = child
            return replace(state, children=tuple(children))
    return state


def _transition(
    current: Lifecycle,
    desired: Lifecycle,
    *,
    allow_regression: bool = False,
    restart: bool = False,
) -> Lifecycle:
    if current in _TERMINAL_LIFECYCLES and not restart:
        return current
    if allow_regression or _LIFECYCLE_RANK[desired] >= _LIFECYCLE_RANK[current]:
        return desired
    return current


def _root_event(state: BranchState, task_id: str | None) -> bool:
    return task_id is None or task_id == state.identity.branch_id


def _provider_values(values: Mapping[str, Any]) -> tuple[str | None, str | None]:
    metadata = values.get("provider_metadata")
    metadata_map = metadata if isinstance(metadata, Mapping) else {}
    provider = _optional_string(_value(values, "provider", "assigned_provider"))
    model = _optional_string(values.get("model"))
    if provider is None:
        provider = _optional_string(metadata_map.get("provider"))
    if model is None:
        model = _optional_string(metadata_map.get("model"))
    return provider, model


def _provider_lease(provider: str | None, model: str | None) -> str | None:
    if provider is None:
        return None
    return f"{provider}/{model}" if model is not None else provider


def _progress(
    state: BranchState,
    values: Mapping[str, Any],
    desired: Lifecycle | None = None,
    *,
    allow_regression: bool = False,
    restart: bool = False,
) -> BranchState:
    task_id = _task_id(values)
    if task_id is None:
        return state
    child_index = _find_child_index(state, task_id)
    generation = _generation(values)
    turn = _turn(values)
    provider, model = _provider_values(values)
    if child_index is not None:
        child = state.children[child_index]
        if generation is not None:
            generation = max(child.generation or 0, generation)
        else:
            generation = child.generation
        if turn is not None:
            turn = max(child.turn or 0, turn)
        else:
            turn = child.turn
        lifecycle = (
            _transition(
                child.lifecycle,
                desired,
                allow_regression=allow_regression,
                restart=restart,
            )
            if desired is not None
            else child.lifecycle
        )
        current_tool = child.current_tool
        if "tool" in values:
            current_tool = _optional_string(values.get("tool"))
        return _replace_child(
            state,
            task_id,
            replace(
                child,
                generation=generation,
                turn=turn,
                lifecycle=lifecycle,
                provider=provider or child.provider,
                model=model or child.model,
                current_tool=current_tool,
            ),
        )
    if not _root_event(state, task_id):
        return state
    identity = state.identity
    if generation is not None:
        generation = max(identity.generation or 0, generation)
    else:
        generation = identity.generation
    if turn is not None:
        turn = max(identity.turn or 0, turn)
    else:
        turn = identity.turn
    lifecycle = (
        _transition(
            identity.lifecycle,
            desired,
            allow_regression=allow_regression,
            restart=restart,
        )
        if desired is not None
        else identity.lifecycle
    )
    current_tool = state.current_tool
    if "tool" in values:
        current_tool = _optional_string(values.get("tool"))
    resources = state.resources
    if provider is not None or model is not None:
        resources = replace(
            resources,
            provider=provider or resources.provider,
            model=model or resources.model,
            provider_lease=_provider_lease(
                provider or resources.provider, model or resources.model
            ),
        )
    return replace(
        state,
        identity=replace(identity, generation=generation, turn=turn, lifecycle=lifecycle),
        current_tool=current_tool,
        resources=resources,
    )


def _add_anchor(state: BranchState, anchor: str | None) -> BranchState:
    if not anchor or anchor in state.anchors:
        return state
    return replace(state, anchors=(*state.anchors, anchor))


def _event_anchor(state: BranchState, values: Mapping[str, Any], kind: str) -> str | None:
    sequence = _nonnegative_int(values.get("seq"))
    if sequence is None:
        sequence = state.last_event_seq
    if sequence is None or state.identity.session_id is None:
        return None
    return f"event:{quote(state.identity.session_id, safe='')}:{sequence}"


def _tool_anchor(state: BranchState, values: Mapping[str, Any]) -> str | None:
    task_id = _task_id(values)
    generation = _generation(values) or state.generation
    turn = _turn(values)
    if turn is None:
        turn = state.turn
    if task_id is None or generation is None or turn is None:
        return None
    batch_index = _nonnegative_int(values.get("batch_index")) or 0
    return f"tool:{quote(task_id, safe='')}:{generation}:{turn}:{batch_index}"


def _set_delta(state: BranchState, delta: str | None) -> BranchState:
    if not isinstance(delta, str) or not delta:
        return state
    return replace(state, control=replace(state.control, last_meaningful_delta=delta[:512]))


def _add_blocker(state: BranchState, blocker: str | None) -> BranchState:
    if not isinstance(blocker, str) or not blocker:
        return state
    if blocker in state.control.blockers:
        return state
    return replace(
        state,
        control=replace(state.control, blockers=(*state.control.blockers, blocker[:512])),
    )


def _contract_values(values: Mapping[str, Any]) -> dict[str, Any]:
    nested = values.get("spec")
    contract = dict(nested) if isinstance(nested, Mapping) else {}
    contract.update(values)
    return contract


def _reduce_task_assigned(state: BranchState, values: Mapping[str, Any]) -> BranchState:  # noqa: C901
    task_id = _task_id(values)
    parent_id = _parent_id(values)
    if task_id is None:
        return state
    if parent_id is not None and parent_id != task_id:
        state = _prepare_identity(state, values, prefer_parent=True)
        state = _ensure_child(state, task_id, parent_id, kind=_optional_string(values.get("kind")))
        child = next(child for child in state.children if child.branch_id == task_id)
        return _replace_child(
            state,
            task_id,
            replace(
                child,
                parent_branch_id=parent_id,
                lifecycle=_transition(child.lifecycle, Lifecycle.QUEUED),
                kind=_optional_string(values.get("kind")) or child.kind,
            ),
        )

    state = _prepare_identity(state, values)
    contract = _contract_values(values)
    mission = state.mission
    objective = _optional_string(_value(contract, "objective", "task", "goal"))
    constraints = _strings_value(contract, "constraints", "requirements")
    done_when = _strings_value(contract, "done_when", "done_criteria", "done")
    verification = _strings_value(
        contract, "verification_contract", "verification", "verification_commands"
    )
    if objective is not None:
        mission = replace(mission, objective=objective)
    if constraints is not None:
        mission = replace(mission, constraints=constraints)
    if done_when is not None:
        mission = replace(mission, done_when=done_when)
    if verification is not None:
        mission = replace(mission, verification_contract=verification)

    authority = state.authority
    authority_values = {
        "repo": _value(contract, "repo", "repository"),
        "worktree": _value(contract, "worktree", "worktree_path"),
        "branch": contract.get("branch"),
    }
    for field_name, raw_value in authority_values.items():
        value = _optional_string(raw_value)
        if value is not None:
            authority = replace(authority, **{field_name: value})
    for field_name, keys in {
        "writable_scope": ("writable_scope", "write_scope"),
        "tools": ("tools", "tool_allowlist"),
        "authorized_providers": ("authorized_providers", "provider_allowlist"),
    }.items():
        collection = _strings_value(contract, *keys)
        if collection is not None:
            authority = replace(authority, **{field_name: collection})

    control = state.control
    plan = _strings_value(contract, "plan", "steps")
    obligations = _strings_value(contract, "open_obligations", "obligations", "open_items")
    blockers = _strings_value(contract, "blockers")
    current_step = _nonnegative_int(contract.get("current_step"))
    if plan is not None:
        control = replace(control, plan=plan)
    if obligations is not None:
        control = replace(control, open_obligations=obligations)
    if blockers is not None:
        control = replace(control, blockers=blockers)
    if current_step is not None:
        control = replace(control, current_step=current_step)

    provider, model = _provider_values(contract)
    resources = state.resources
    max_turns = _nonnegative_int(contract.get("max_turns"))
    max_wall = _finite_nonnegative(contract.get("max_wall_s"))
    if max_turns is not None:
        resources = replace(resources, remaining_turns=max_turns)
    if max_wall is not None:
        resources = replace(resources, remaining_wall_s=max_wall)
    if provider is not None or model is not None:
        resources = replace(
            resources,
            provider=provider or resources.provider,
            model=model or resources.model,
            provider_lease=_provider_lease(
                provider or resources.provider, model or resources.model
            ),
        )

    artifacts = state.artifacts
    base_head = _optional_string(_value(contract, "base_commit", "base_head"))
    if base_head is not None:
        artifacts = replace(artifacts, base_head=base_head)
    state = replace(
        state,
        identity=replace(
            state.identity,
            generation=_generation(values) or state.generation,
            parent_branch_id=None,
            lifecycle=_transition(state.lifecycle, Lifecycle.QUEUED),
        ),
        mission=mission,
        authority=authority,
        artifacts=artifacts,
        control=control,
        resources=resources,
    )
    return _set_delta(state, "task admitted")


def _reduce_child_admitted(state: BranchState, values: Mapping[str, Any]) -> BranchState:
    child_id = _optional_string(values.get("child_task_id"))
    parent_id = _parent_id(values) or _task_id(values)
    if child_id is None:
        return state
    state = _prepare_identity(state, values, prefer_parent=True)
    state = _ensure_child(
        state, child_id, parent_id, kind=_optional_string(values.get("child_kind"))
    )
    child = next(child for child in state.children if child.branch_id == child_id)
    context_mode = _optional_string(values.get("context_mode")) or child.context_mode
    placement = _optional_string(values.get("placement")) or child.placement
    critical = _optional_bool(values.get("critical"))
    result_ref = _optional_string(values.get("result_ref"))
    state = _replace_child(
        state,
        child_id,
        replace(
            child,
            parent_branch_id=parent_id or child.parent_branch_id,
            kind=_optional_string(values.get("child_kind")) or child.kind,
            lifecycle=_transition(child.lifecycle, Lifecycle.QUEUED),
            context_mode=context_mode,
            placement=placement,
            critical=critical if critical is not None else child.critical,
            result=(
                replace(child.result, status="admitted")
                if child.result is not None and result_ref is not None
                else child.result
            ),
        ),
    )
    return _set_delta(state, f"child admitted: {child_id}")


def _result_status_lifecycle(status: str | None) -> Lifecycle | None:
    if status == Lifecycle.SUCCEEDED.value:
        return Lifecycle.SUCCEEDED
    if status == Lifecycle.CANCELLED.value:
        return Lifecycle.CANCELLED
    if status == Lifecycle.SUSPENDED.value:
        return Lifecycle.SUSPENDED
    if status in {Lifecycle.FAILED.value, "unresolvable"}:
        return Lifecycle.FAILED
    return None


def _result_from_values(values: Mapping[str, Any]) -> ResultEnvelope:
    metadata = values.get("provider_metadata")
    return ResultEnvelope(
        status=_optional_string(values.get("status")),
        summary=_optional_string(values.get("summary")),
        failure_reason=_optional_string(_value(values, "failure_reason", "reason")),
        commits=_strings_value(values, "commits") or (),
        files_changed=_strings_value(values, "files_changed", "files") or (),
        diff_truncated=_optional_bool(values.get("diff_truncated")),
        requires_commit=_optional_bool(values.get("requires_commit")),
        checkpoint_ref=_optional_string(values.get("checkpoint_ref")),
        epoch=_nonnegative_int(values.get("epoch")),
        terminal_action=_terminal_action_from_value(values.get("terminal_action")),
        provider_metadata=_mapping_pairs(metadata),
    )


def _reduce_result(state: BranchState, values: Mapping[str, Any]) -> BranchState:
    task_id = _task_id(values)
    if task_id is None:
        return state
    state = _prepare_identity(state, values)
    result = _result_from_values(values)
    lifecycle = _result_status_lifecycle(result.status)
    child_index = _find_child_index(state, task_id)
    if child_index is not None:
        child = state.children[child_index]
        child = replace(
            child,
            result=result,
            lifecycle=(
                _transition(child.lifecycle, lifecycle)
                if lifecycle is not None
                else child.lifecycle
            ),
            checkpoint_ref=result.checkpoint_ref or child.checkpoint_ref,
            epoch=result.epoch if result.epoch is not None else child.epoch,
        )
        return _replace_child(state, task_id, child)
    if not _root_event(state, task_id):
        return state
    state = replace(state, result=result)
    state = _progress(state, values, lifecycle)
    if result.status == Lifecycle.SUSPENDED.value:
        state = replace(
            state,
            context=replace(
                state.context,
                epoch=result.epoch if result.epoch is not None else state.context.epoch,
                checkpoint_ref=result.checkpoint_ref or state.context.checkpoint_ref,
            ),
        )
    obligations = _strings_value(values, "open_obligations", "obligations", "open_items")
    blockers = _strings_value(values, "blockers")
    if obligations is not None:
        state = replace(state, control=replace(state.control, open_obligations=obligations))
    if blockers is not None:
        state = replace(state, control=replace(state.control, blockers=blockers))
    if result.failure_reason is not None:
        state = _add_blocker(state, result.failure_reason)
    return _set_delta(state, f"result: {result.status or 'unknown'}")


def _usage_counts(value: Any) -> tuple[int, int, int, int]:
    usage = value if isinstance(value, Mapping) else {}

    def first_count(*keys: str) -> int:
        for key in keys:
            if key in usage:
                number = _nonnegative_int(usage.get(key))
                if number is not None:
                    return number
        return 0

    input_tokens = first_count("input_tokens", "prompt_tokens")
    output_tokens = first_count("output_tokens", "completion_tokens")
    cached_tokens = 0
    for details_key in ("input_tokens_details", "prompt_tokens_details"):
        details = usage.get(details_key)
        if isinstance(details, Mapping) and "cached_tokens" in details:
            cached_tokens = _nonnegative_int(details.get("cached_tokens")) or 0
            break
    else:
        cached_tokens = first_count("cache_read_input_tokens", "cached_tokens")
    total_tokens = first_count("total_tokens")
    if total_tokens == 0 and (input_tokens or output_tokens):
        total_tokens = input_tokens + output_tokens
    return input_tokens, output_tokens, cached_tokens, total_tokens


def _usage_update(usage: Usage, values: Mapping[str, Any]) -> Usage:
    input_tokens, output_tokens, cached_tokens, total_tokens = _usage_counts(values.get("usage"))
    latency = _finite_nonnegative(values.get("latency_s"))
    cost = _finite_nonnegative(values.get("estimated_cost_usd")) or 0.0
    output_rate = output_tokens / latency if latency and latency > 0 else None
    return replace(
        usage,
        calls=usage.calls + 1,
        summary_calls=usage.summary_calls + (1 if values.get("call_kind") == "summary" else 0),
        failed_calls=usage.failed_calls
        + (1 if _optional_string(values.get("failure_reason")) else 0),
        input_tokens=usage.input_tokens + input_tokens,
        output_tokens=usage.output_tokens + output_tokens,
        cached_tokens=usage.cached_tokens + cached_tokens,
        total_tokens=usage.total_tokens + total_tokens,
        estimated_cost_usd=round(usage.estimated_cost_usd + cost, 12),
        last_latency_s=latency,
        last_output_tokens_per_s=output_rate,
        provider_cache_hits=usage.provider_cache_hits
        + (1 if values.get("provider_cache_hit") is True else 0),
    )


def _update_resources_from_usage(resources: Resources, values: Mapping[str, Any]) -> Resources:
    provider, model = _provider_values(values)
    updated = resources
    remaining_turns = _nonnegative_int(values.get("remaining_turns"))
    remaining_wall_s = _finite_nonnegative(values.get("remaining_wall_s"))
    if remaining_turns is not None:
        updated = replace(updated, remaining_turns=remaining_turns)
    if remaining_wall_s is not None:
        updated = replace(updated, remaining_wall_s=remaining_wall_s)
    if provider is not None or model is not None:
        updated = replace(
            updated,
            provider=provider or updated.provider,
            model=model or updated.model,
            provider_lease=_provider_lease(provider or updated.provider, model or updated.model),
        )
    for field_name in (
        "context_pressure",
        "uncached_token_pressure",
        "cache_affinity",
        "cache_warmth",
        "quota_pressure",
        "cash_pressure",
        "delegation_overhead",
    ):
        value = _optional_string(values.get(field_name))
        if value is not None:
            updated = replace(updated, **{field_name: value})
    alternative = _optional_bool(values.get("alternative_lane_available"))
    if alternative is not None:
        updated = replace(updated, alternative_lane_available=alternative)
    if values.get("provider_cache_hit") is True:
        updated = replace(updated, cache_affinity="exact")
    return updated


def _update_context_metrics(context: Context, values: Mapping[str, Any]) -> Context:
    updated = context
    metric_map = {
        "summary_segments": "summary_segments",
        "raw_tail_tokens": "raw_tail_tokens",
        "raw_tail_bytes": "raw_tail_bytes",
        "raw_tail_messages": "raw_tail_messages",
    }
    for source, target in metric_map.items():
        number = _nonnegative_int(values.get(source))
        if number is not None:
            updated = replace(updated, **{target: number})
    active_messages = _nonnegative_int(values.get("active_context_messages"))
    if active_messages is not None:
        updated = replace(updated, raw_tail_messages=active_messages)
    return updated


def _reduce_usage(state: BranchState, values: Mapping[str, Any]) -> BranchState:
    task_id = _task_id(values)
    if task_id is None:
        return state
    state = _prepare_identity(state, values)
    child_index = _find_child_index(state, task_id)
    if child_index is not None:
        child = state.children[child_index]
        child_usage = _usage_update(child.usage, values)
        provider, model = _provider_values(values)
        child = replace(
            child,
            usage=child_usage,
            generation=max(child.generation or 0, _generation(values) or 0) or child.generation,
            turn=max(child.turn or 0, _turn(values) or 0) or child.turn,
            provider=provider or child.provider,
            model=model or child.model,
            lifecycle=_transition(child.lifecycle, Lifecycle.ACTIVE),
        )
        return _replace_child(state, task_id, child)
    if not _root_event(state, task_id):
        return state
    state = _progress(state, values, Lifecycle.ACTIVE)
    state = replace(
        state,
        usage=_usage_update(state.usage, values),
        resources=_update_resources_from_usage(state.resources, values),
        context=_update_context_metrics(state.context, values),
    )
    epoch = _nonnegative_int(values.get("epoch"))
    if epoch is not None:
        state = replace(state, context=replace(state.context, epoch=epoch))
    return _set_delta(state, "provider usage recorded")


def _tool_from_values(values: Mapping[str, Any], evidence_ref: str | None) -> ToolObservation:
    return ToolObservation(
        tool=_optional_string(values.get("tool")),
        command=_optional_string(_value(values, "cmd", "command")),
        turn=_turn(values),
        batch_index=_nonnegative_int(values.get("batch_index")),
        batch_size=_nonnegative_int(values.get("batch_size")),
        ok=_optional_bool(values.get("ok")),
        duration_ms=_finite_nonnegative(values.get("duration_ms")),
        evidence_ref=evidence_ref,
    )


def _reduce_tool(state: BranchState, values: Mapping[str, Any]) -> BranchState:
    task_id = _task_id(values)
    if task_id is None:
        return state
    state = _prepare_identity(state, values)
    state = _progress(state, values, Lifecycle.ACTIVE)
    observation = _tool_from_values(values, _tool_anchor(state, values))
    child_index = _find_child_index(state, task_id)
    if child_index is not None:
        child = state.children[child_index]
        return _replace_child(
            state,
            task_id,
            replace(
                child,
                tool_events=(*child.tool_events, observation),
                current_tool=observation.tool,
            ),
        )
    if not _root_event(state, task_id):
        return state
    state = replace(
        state,
        current_tool=observation.tool,
        tool_events=(*state.tool_events, observation),
    )
    tool_name = observation.tool or "unknown"
    outcome = "ok" if observation.ok is True else "failed" if observation.ok is False else "unknown"
    return _set_delta(state, f"tool {tool_name}: {outcome}")


def _reduce_tool_output(state: BranchState, values: Mapping[str, Any]) -> BranchState:
    task_id = _task_id(values)
    if task_id is None:
        return state
    state = _prepare_identity(state, values)
    state = _progress(state, values, Lifecycle.ACTIVE)
    delta = _optional_string(values.get("delta"))
    child_index = _find_child_index(state, task_id)
    if child_index is not None:
        child = state.children[child_index]
        return _replace_child(
            state,
            task_id,
            replace(child, current_tool=_optional_string(values.get("tool")) or child.current_tool),
        )
    if not _root_event(state, task_id):
        return state
    return replace(state, last_tool_output=delta)


def _reduce_context_fork(state: BranchState, values: Mapping[str, Any]) -> BranchState:
    child_id = _optional_string(values.get("child_task_id"))
    parent_id = _parent_id(values) or _task_id(values)
    if child_id is None:
        return state
    state = _prepare_identity(state, values, prefer_parent=True)
    state = _ensure_child(state, child_id, parent_id)
    child = next(child for child in state.children if child.branch_id == child_id)
    compatible = values.get("compatible") is True
    semantic = values.get("semantic_reuse") is True
    lineage = "exact" if compatible else "semantic" if semantic else "unknown"
    context_mode = _optional_string(values.get("resolved_context_mode")) or _optional_string(
        values.get("context_mode")
    )
    placement = _optional_string(values.get("resolved_placement")) or _optional_string(
        values.get("placement")
    )
    epoch = _nonnegative_int(values.get("epoch"))
    child = replace(
        child,
        parent_branch_id=parent_id or child.parent_branch_id,
        epoch=epoch if epoch is not None else child.epoch,
        lifecycle=_transition(child.lifecycle, Lifecycle.QUEUED),
        context_mode=context_mode or child.context_mode,
        placement=placement or child.placement,
        lineage=lineage,
    )
    return _replace_child(state, child_id, child)


def _reduce_context(state: BranchState, values: Mapping[str, Any]) -> BranchState:
    kind = values.get("kind")
    task_id = _task_id(values)
    if task_id is None:
        return state
    state = _prepare_identity(state, values)
    if kind in {"context_fork_skipped"}:
        return _reduce_context_fork(
            state,
            {
                **values,
                "semantic_reuse": False,
                "compatible": False,
                "resolved_context_mode": "fresh",
            },
        )
    if kind == "context_resume_failed":
        state = _progress(state, values, Lifecycle.FAILED)
        return _add_blocker(state, _optional_string(_value(values, "reason", "message")))
    child_index = _find_child_index(state, task_id)
    epoch = _nonnegative_int(values.get("epoch"))
    checkpoint_ref = _optional_string(values.get("checkpoint_ref"))
    cache_key = _cache_from_value(values.get("cache_key"))
    if child_index is not None:
        child = state.children[child_index]
        return _replace_child(
            state,
            task_id,
            replace(
                child,
                epoch=epoch if epoch is not None else child.epoch,
                checkpoint_ref=checkpoint_ref or child.checkpoint_ref,
                lineage=(
                    "exact"
                    if kind in {"context_checkpoint", "context_epoch_advanced"}
                    else child.lineage
                ),
            ),
        )
    if not _root_event(state, task_id):
        return state
    allow_regression = kind == "context_resume"
    state = _progress(state, values, Lifecycle.ACTIVE, allow_regression=allow_regression)
    context = state.context
    if epoch is not None:
        context = replace(context, epoch=epoch)
    if checkpoint_ref is not None:
        context = replace(context, checkpoint_ref=checkpoint_ref)
    if cache_key is not None:
        context = replace(context, cache_key=cache_key)
    if kind in {"context_checkpoint", "context_epoch_advanced"}:
        context = replace(context, lineage="exact")
    if kind == "context_epoch_advanced":
        context = replace(
            context, folded_from_epoch=_nonnegative_int(values.get("folded_from_epoch"))
        )
    if kind == "checkpoint" and checkpoint_ref is None:
        state_ref = _optional_string(values.get("state_ref"))
        if state_ref is not None:
            context = replace(context, checkpoint_ref=state_ref)
    if kind == "compaction_failed":
        context = replace(context, last_error=_optional_string(values.get("reason")))
    state = replace(state, context=_update_context_metrics(context, values))
    return _set_delta(state, f"context {kind}")


def _reduce_artifact(state: BranchState, values: Mapping[str, Any]) -> BranchState:
    kind = values.get("kind")
    task_id = _task_id(values)
    if task_id is None:
        return state
    state = _prepare_identity(state, values)
    desired = {
        "merge_started": Lifecycle.PUBLISHING,
        "child_integration_prepared": Lifecycle.JOINING,
        "child_integrated": Lifecycle.JOINING,
        "merge_committed": Lifecycle.SUCCEEDED,
        "merge_reconciled": Lifecycle.SUCCEEDED,
        "merge_failed": Lifecycle.FAILED,
        "join_invariant_failed": Lifecycle.FAILED,
    }.get(kind)
    state = _progress(state, values, desired)
    new_head = _optional_string(
        _value(values, "accepted_integration_head", "artifact_head", "new", "head")
    )
    explicit_worktree_head = _optional_string(
        _value(values, "worktree_head", "parent_head", "current_head")
    )
    dirty = _optional_bool(values.get("dirty"))
    if dirty is None and "clean" in values:
        clean = _optional_bool(values.get("clean"))
        dirty = None if clean is None else not clean

    child_index = _find_child_index(state, task_id)
    if child_index is not None:
        child = state.children[child_index]
        status = child.artifact_status
        if kind == "child_integrated":
            status = "integrated"
        elif kind in {"merge_committed", "merge_reconciled"}:
            status = "published"
        child = replace(
            child,
            artifact_status=status,
            accepted_artifact_head=new_head or child.accepted_artifact_head,
        )
        state = _replace_child(state, task_id, child)
        if kind == "child_integrated" and new_head is not None:
            state = replace(
                state,
                artifacts=replace(state.artifacts, accepted_integration_head=new_head),
            )
        return _add_blocker(
            state,
            _optional_string(_value(values, "reason", "message"))
            if kind in {"merge_failed", "join_invariant_failed"}
            else None,
        )

    if not _root_event(state, task_id):
        return state
    artifacts = state.artifacts
    if kind == "parent_snapshot":
        artifacts = replace(
            artifacts,
            base_head=new_head or artifacts.base_head,
            worktree_head=new_head or artifacts.worktree_head,
            dirty=False if dirty is None else dirty,
        )
    elif kind in {"child_integrated", "merge_committed", "merge_reconciled"}:
        artifacts = replace(
            artifacts,
            accepted_integration_head=new_head or artifacts.accepted_integration_head,
            worktree_head=explicit_worktree_head or artifacts.worktree_head,
            dirty=dirty if dirty is not None else artifacts.dirty,
        )
    elif explicit_worktree_head is not None or dirty is not None:
        artifacts = replace(
            artifacts,
            worktree_head=explicit_worktree_head or artifacts.worktree_head,
            dirty=dirty if dirty is not None else artifacts.dirty,
        )
    if kind == "worktree_salvaged":
        artifacts = replace(artifacts, dirty=True)
    state = replace(state, artifacts=artifacts)
    state = _add_blocker(
        state,
        _optional_string(_value(values, "reason", "message"))
        if kind in {"merge_failed", "join_invariant_failed"}
        else None,
    )
    return _set_delta(state, f"artifact event: {kind}")


def _reduce_child_failure(state: BranchState, values: Mapping[str, Any]) -> BranchState:
    task_id = _task_id(values)
    child_id = _optional_string(values.get("child_task_id")) or task_id
    parent_id = _parent_id(values)
    if child_id is None:
        return state
    state = _prepare_identity(state, values, prefer_parent=parent_id is not None)
    state = _ensure_child(state, child_id, parent_id)
    child = next(child for child in state.children if child.branch_id == child_id)
    lifecycle = Lifecycle.REJECTED if values.get("kind") == "child_rejected" else Lifecycle.FAILED
    result_status = "rejected" if lifecycle is Lifecycle.REJECTED else "failed"
    child = replace(
        child,
        parent_branch_id=parent_id or child.parent_branch_id,
        lifecycle=_transition(child.lifecycle, lifecycle),
        result=ResultEnvelope(
            status=result_status,
            failure_reason=_optional_string(_value(values, "reason", "message")),
            summary=_optional_string(values.get("message")),
        ),
    )
    state = _replace_child(state, child_id, child)
    reason = _optional_string(_value(values, "reason", "message")) or lifecycle.value
    return _add_blocker(state, f"child {child_id}: {reason}")


def _reduce_generic_lifecycle(state: BranchState, values: Mapping[str, Any]) -> BranchState:
    kind = values.get("kind")
    desired = {
        "spawned": Lifecycle.STARTING,
        "init": Lifecycle.STARTING,
        "ready": Lifecycle.ACTIVE,
        "run_task": Lifecycle.ACTIVE,
        "worker_reused": Lifecycle.STARTING,
        "reuse_ready": Lifecycle.SUCCEEDED,
        "heartbeat": Lifecycle.ACTIVE,
        "checkpoint": Lifecycle.ACTIVE,
        "provider_boundary_degraded": Lifecycle.ACTIVE,
        "compaction_failed": Lifecycle.ACTIVE,
        "context_resume": Lifecycle.ACTIVE,
        "context_checkpoint": Lifecycle.ACTIVE,
        "context_epoch_advanced": Lifecycle.ACTIVE,
        "context_fork": Lifecycle.ACTIVE,
        "context_fork_skipped": Lifecycle.ACTIVE,
        "restart_scheduled": Lifecycle.STARTING,
        "recover": Lifecycle.STARTING,
        "timeout": Lifecycle.FAILED,
        "fatal_error": Lifecycle.FAILED,
        "provider_infeasible": Lifecycle.FAILED,
        "resource_denied": Lifecycle.FAILED,
        "worker_failed": Lifecycle.FAILED,
        "worker_terminated": Lifecycle.FAILED,
        "task_failed": Lifecycle.FAILED,
        "exit": Lifecycle.FAILED,
        "cancelled": Lifecycle.CANCELLED,
        "task_cancelled": Lifecycle.CANCELLED,
    }.get(kind)
    state = _prepare_identity(state, values)
    state = _progress(
        state,
        values,
        desired,
        restart=kind in {"restart_scheduled", "recover"},
    )
    if kind in {
        "timeout",
        "fatal_error",
        "provider_infeasible",
        "resource_denied",
        "worker_failed",
        "worker_terminated",
        "task_failed",
    }:
        state = _add_blocker(
            state, _optional_string(_value(values, "reason", "message", "error_type"))
        )
    if kind == "compaction_failed":
        state = replace(
            state,
            context=replace(state.context, last_error=_optional_string(values.get("reason"))),
        )
    return _set_delta(state, f"lifecycle event: {kind}")


def _reduce_noop(state: BranchState, values: Mapping[str, Any]) -> BranchState:
    return state


_GLOSSARY_KINDS = frozenset(
    {
        "task_assigned",
        "child_admitted",
        "child_rejected",
        "child_result",
        "child_failed",
        "spawned",
        "init",
        "ready",
        "reuse_ready",
        "run_task",
        "worker_reused",
        "ping",
        "pong",
        "protocol",
        "parse_error",
        "log",
        "heartbeat",
        "tool_event",
        "tool_output_delta",
        "usage_event",
        "provider_boundary_degraded",
        "checkpoint",
        "context_checkpoint",
        "context_epoch_advanced",
        "context_fork",
        "context_fork_skipped",
        "context_resume",
        "context_resume_failed",
        "compaction_failed",
        "compaction_deferred",
        "worktree_salvaged",
        "worktree_pruned",
        "worktree_cleanup_deferred",
        "provider_infeasible",
        "resource_denied",
        "merge_started",
        "parent_snapshot",
        "child_integration_prepared",
        "child_integrated",
        "join_invariant_failed",
        "merge_committed",
        "merge_failed",
        "merge_reconciled",
        "merge_staging_prune_started",
        "merge_staging_pruned",
        "merge_staging_quarantined",
        "merge_staging_cleanup_failed",
        "resolver_staging_prepared",
        "resolver_child_admitted",
        "resolver_succeeded",
        "resolver_failed",
        "resolver_cleanup_failed",
        "result",
        "exit",
        "worker_failed",
        "restart_scheduled",
        "worker_terminated",
        "timeout",
        "recover",
        "session_ended",
        # These are current wire/session values.  They are not all persisted by
        # the supervisor today, but recognizing them keeps the vocabulary
        # explicit when an interactive session contributes a record.
        "result_envelope",
        "fatal_error",
        "session_started",
        "session_resumed",
        "session_cancelled",
        "shutdown",
        "task_failed",
        "task_cancelled",
        "cancelled",
        "worker_exit",
        "merge_progress",
    }
)


def _handler_for(kind: str) -> Callable[[BranchState, Mapping[str, Any]], BranchState]:
    if kind == "task_assigned":
        return _reduce_task_assigned
    if kind == "child_admitted":
        return _reduce_child_admitted
    if kind in {"child_rejected", "child_failed"}:
        return _reduce_child_failure
    if kind in {"child_result"}:
        return _reduce_result
    if kind in {"result", "result_envelope"}:
        return _reduce_result
    if kind == "usage_event":
        return _reduce_usage
    if kind == "tool_event":
        return _reduce_tool
    if kind == "tool_output_delta":
        return _reduce_tool_output
    if kind == "context_fork":
        return _reduce_context_fork
    if kind in {
        "checkpoint",
        "context_checkpoint",
        "context_epoch_advanced",
        "context_fork_skipped",
        "context_resume",
        "context_resume_failed",
        "compaction_failed",
    }:
        return _reduce_context
    if kind in {
        "merge_started",
        "parent_snapshot",
        "child_integration_prepared",
        "child_integrated",
        "join_invariant_failed",
        "merge_committed",
        "merge_failed",
        "merge_reconciled",
        "worktree_salvaged",
        "worktree_pruned",
        "worktree_cleanup_deferred",
    }:
        return _reduce_artifact
    if kind in {
        "spawned",
        "init",
        "ready",
        "reuse_ready",
        "run_task",
        "worker_reused",
        "heartbeat",
        "provider_boundary_degraded",
        "restart_scheduled",
        "recover",
        "timeout",
        "fatal_error",
        "provider_infeasible",
        "resource_denied",
        "worker_failed",
        "worker_terminated",
        "task_failed",
        "exit",
        "cancelled",
        "task_cancelled",
        "compaction_deferred",
    }:
        return _reduce_generic_lifecycle
    return _reduce_noop


def reduce(state: BranchState, event: Mapping[str, Any]) -> BranchState:
    """Fold one event into ``state`` without performing I/O.

    Unknown kinds do not affect branch semantics.  They do advance the durable
    watermark and increment ``unknown_events`` (with the kind retained in
    ``unknown_event_kinds``), so schema drift is visible rather than silent.
    """

    if not isinstance(state, BranchState):
        raise TypeError("state must be a BranchState")
    kind = _event_kind(event)
    updated = _advance_metadata(state, event, kind)
    if kind is None or kind not in _GLOSSARY_KINDS:
        unknown_kind = kind or "<missing>"
        return replace(
            updated,
            unknown_events=updated.unknown_events + 1,
            unknown_event_kinds=(*updated.unknown_event_kinds, unknown_kind),
        )
    values = _event_record(kind, event)
    handler = _handler_for(kind)
    return handler(updated, values)


def inspect_state(events: Iterable[Mapping[str, Any]]) -> BranchState:
    """Fold an ordered event iterable into one immutable ``BranchState``."""

    state = BranchState()
    for event in events:
        state = reduce(state, event)
    return state


def to_json(state: BranchState) -> str:
    """Module-level serialization helper matching :meth:`BranchState.to_json`."""

    if not isinstance(state, BranchState):
        raise TypeError("state must be a BranchState")
    return state.to_json()


def from_json(document: str | bytes | bytearray | Mapping[str, Any]) -> BranchState:
    """Module-level deserialization helper."""

    return BranchState.from_json(document)


__all__ = [
    "SCHEMA_VERSION",
    "Lifecycle",
    "Identity",
    "Mission",
    "Authority",
    "CacheDescriptor",
    "Context",
    "Artifacts",
    "Control",
    "Knowledge",
    "Usage",
    "Resources",
    "TerminalAction",
    "ResultEnvelope",
    "ToolObservation",
    "Child",
    "BranchState",
    "reduce",
    "inspect_state",
    "to_json",
    "from_json",
]
