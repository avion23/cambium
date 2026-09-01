"""Pure policy types for recursive context branches.

Model-originated child proposals must name two orthogonal properties:

``context_mode``
    Which parent context representation seeds the child.

``placement``
    Whether routing preserves provider affinity or prefers another feasible
    provider lane.

The supervisor still accepts an undeclared policy from harness-originated
``proposed_children`` fixtures. That internal automatic-compatibility path is
represented by ``None`` and is deliberately kept out of the model tool
contract. Model boundaries must call :func:`require_child_policy`.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from enum import StrEnum
from typing import Any


class ContextMode(StrEnum):
    """Parent context representation supplied to a child branch."""

    TRUNK = "trunk"
    SEMANTIC = "semantic"
    FRESH = "fresh"


class Placement(StrEnum):
    """Provider-affinity preference for a child branch."""

    INHERIT = "inherit"
    SPREAD = "spread"


@dataclass(frozen=True, slots=True)
class ChildPolicy:
    """Validated child context and placement decision."""

    context_mode: ContextMode
    placement: Placement


class ChildPolicyError(ValueError):
    """A delegated child did not declare a coherent branch policy."""


def _enum_value[T: StrEnum](spec: Mapping[str, Any], key: str, enum: type[T]) -> T:
    value = spec.get(key)
    if not isinstance(value, str):
        raise ChildPolicyError(f"child {key} must be a string")
    try:
        return enum(value)
    except ValueError:
        choices = ", ".join(item.value for item in enum)
        raise ChildPolicyError(f"child {key} must be one of: {choices}") from None


def parse_child_policy(spec: Mapping[str, Any]) -> ChildPolicy | None:
    """Parse a declared policy or the harness-only automatic path.

    ``None`` means neither policy dimension was supplied by an internal
    harness-originated proposal. Partial declarations and contradictory
    ``trunk + spread`` requests are always rejected.
    """
    if not isinstance(spec, Mapping):
        raise ChildPolicyError("child spec must be an object")
    if spec.get("context_mode") is None and spec.get("placement") is None:
        return None
    context_mode = _enum_value(spec, "context_mode", ContextMode)
    placement = _enum_value(spec, "placement", Placement)
    if context_mode is ContextMode.TRUNK and placement is Placement.SPREAD:
        raise ChildPolicyError("child context_mode=trunk requires placement=inherit")
    return ChildPolicy(context_mode=context_mode, placement=placement)


def require_child_policy(spec: Mapping[str, Any]) -> ChildPolicy:
    """Return the explicit policy required at every model-facing boundary."""
    policy = parse_child_policy(spec)
    if policy is None:
        raise ChildPolicyError("child context_mode and placement are required")
    return policy


__all__ = [
    "ChildPolicy",
    "ChildPolicyError",
    "ContextMode",
    "Placement",
    "parse_child_policy",
    "require_child_policy",
]
