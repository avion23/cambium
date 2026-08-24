"""Typed policy for bounded CAST semantic projections.

The policy is intentionally provider-neutral. It decides only whether an active
semantic trunk has exceeded a declared resource bound and whether a deterministic
K0 projection restored that bound. Provider cache TTLs, prices, and transport
capabilities belong to provider configuration, not to this module.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any

DEFAULT_MAX_SEGMENTS = 16


def _non_negative_int(value: Any, name: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        raise ValueError(f"{name} must be a non-negative integer")
    return value


@dataclass(frozen=True, slots=True)
class CastPolicy:
    """Hard bounds for one active CAST trunk.

    Bounds are inclusive: rollover becomes due only after a value exceeds its
    configured maximum. Zero disables that dimension. ``max_segments`` is
    enabled by default because an unbounded append-only projection eventually
    recreates the long-context problem CAST is intended to avoid.
    """

    max_segments: int = DEFAULT_MAX_SEGMENTS
    max_trunk_tokens: int = 0
    min_rollover_savings_tokens: int = 0

    def __post_init__(self) -> None:
        for name in (
            "max_segments",
            "max_trunk_tokens",
            "min_rollover_savings_tokens",
        ):
            object.__setattr__(self, name, _non_negative_int(getattr(self, name), name))

    @classmethod
    def from_mapping(cls, value: Mapping[str, Any]) -> CastPolicy:
        allowed = {
            "max_segments",
            "max_trunk_tokens",
            "min_rollover_savings_tokens",
        }
        unknown = sorted(set(value) - allowed)
        if unknown:
            raise ValueError(f"unknown CAST policy field(s): {unknown}")
        return cls(**dict(value))

    def rollover_due(self, segment_count: int, active_trunk_tokens: int) -> bool:
        segments = _non_negative_int(segment_count, "segment_count")
        tokens = _non_negative_int(active_trunk_tokens, "active_trunk_tokens")
        return bool(
            (self.max_segments and segments > self.max_segments)
            or (self.max_trunk_tokens and tokens > self.max_trunk_tokens)
        )

    def validate_rollover(
        self,
        *,
        before_segments: int,
        before_tokens: int,
        after_segments: int,
        after_tokens: int,
    ) -> None:
        """Fail closed unless a due rollover restores every hard bound."""
        before_segment_count = _non_negative_int(before_segments, "before_segments")
        before_token_count = _non_negative_int(before_tokens, "before_tokens")
        after_segment_count = _non_negative_int(after_segments, "after_segments")
        after_token_count = _non_negative_int(after_tokens, "after_tokens")
        if not self.rollover_due(before_segment_count, before_token_count):
            raise ValueError("CAST rollover was requested while the trunk was within policy")
        if self.rollover_due(after_segment_count, after_token_count):
            raise ValueError("CAST K0 rollover did not restore the configured trunk bounds")
        if (
            self.min_rollover_savings_tokens
            and before_token_count - after_token_count < self.min_rollover_savings_tokens
        ):
            raise ValueError("CAST K0 rollover did not meet the minimum token saving")


__all__ = ["CastPolicy"]
