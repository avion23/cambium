from __future__ import annotations

import pytest

from cambium.child_policy import (
    ChildPolicyError,
    ContextMode,
    Placement,
    parse_child_policy,
)


def test_policy_requires_both_explicit_dimensions() -> None:
    with pytest.raises(ChildPolicyError, match="context_mode"):
        parse_child_policy({"placement": "inherit"})
    with pytest.raises(ChildPolicyError, match="placement"):
        parse_child_policy({"context_mode": "trunk"})


def test_trunk_inherit_is_the_cache_affine_default_choice() -> None:
    policy = parse_child_policy(
        {"context_mode": "trunk", "placement": "inherit"}
    )

    assert policy.context_mode is ContextMode.TRUNK
    assert policy.placement is Placement.INHERIT


def test_semantic_and_fresh_children_can_prefer_spread() -> None:
    semantic = parse_child_policy(
        {"context_mode": "semantic", "placement": "spread"}
    )
    fresh = parse_child_policy(
        {"context_mode": "fresh", "placement": "spread"}
    )

    assert semantic == (ContextMode.SEMANTIC, Placement.SPREAD)
    assert fresh == (ContextMode.FRESH, Placement.SPREAD)


def test_trunk_cannot_claim_another_provider() -> None:
    with pytest.raises(
        ChildPolicyError,
        match="context_mode=trunk requires placement=inherit",
    ):
        parse_child_policy({"context_mode": "trunk", "placement": "spread"})
