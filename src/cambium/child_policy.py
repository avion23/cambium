"""Pure policy types for recursive context branches.

The model chooses two orthogonal properties for every delegated child:

``context_mode``
    Which parent context representation seeds the child.

``placement``
    Whether routing preserves provider affinity or prefers another feasible
    provider lane.

There are deliberately no aliases or implicit defaults.  A child proposal is
an architectural decision and must name both values explicitly.
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


def _enum_value(spec: Mapping[str, Any], key: str, enum: type[StrEnum]) -> StrEnum:
    value = spec.get(key)
    if not isinstance(value, str):
        raise ChildPolicyError(f"child {key} must be a string")
    try:
        return enum(value)
    except ValueError:
        choices = ", ".join(item.value for item in enum)
        raise ChildPolicyError(f"child {key} must be one of: {choices}") from None


def parse_child_policy(spec: Mapping[str, Any]) -> ChildPolicy:
    """Return the one authoritative policy declared by a child spec.

    ``trunk`` means an exact same-provider checkpoint prefix.  Combining it
    with ``spread`` would promise both byte-identical provider cache affinity
    and another provider, so the combination is rejected rather than silently
    downgraded.
    """
    if not isinstance(spec, Mapping):
        raise ChildPolicyError("child spec must be an object")
    context_mode = _enum_value(spec, "context_mode", ContextMode)
    placement = _enum_value(spec, "placement", Placement)
    if context_mode is ContextMode.TRUNK and placement is Placement.SPREAD:
        raise ChildPolicyError("child context_mode=trunk requires placement=inherit")
    return ChildPolicy(context_mode=context_mode, placement=placement)


__all__ = [
    "ChildPolicy",
    "ChildPolicyError",
    "ContextMode",
    "Placement",
    "parse_child_policy",
]
