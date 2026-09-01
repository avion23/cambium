from __future__ import annotations

import pytest

from cambium.child_policy import (
    ChildPolicyError,
    ContextMode,
    Placement,
    parse_child_policy,
    require_child_policy,
)


def test_model_policy_requires_both_explicit_dimensions() -> None:
    for spec, missing in (
        ({}, "context_mode and placement"),
        ({"placement": "inherit"}, "context_mode"),
        ({"context_mode": "trunk"}, "placement"),
    ):
        with pytest.raises(ChildPolicyError, match=missing):
            require_child_policy(spec)


def test_harness_automatic_policy_is_the_only_undeclared_form() -> None:
    assert parse_child_policy({}) is None

    with pytest.raises(ChildPolicyError, match="context_mode"):
        parse_child_policy({"placement": "inherit"})
    with pytest.raises(ChildPolicyError, match="placement"):
        parse_child_policy({"context_mode": "trunk"})


def test_trunk_inherit_is_cache_affine() -> None:
    policy = require_child_policy({"context_mode": "trunk", "placement": "inherit"})

    assert policy.context_mode is ContextMode.TRUNK
    assert policy.placement is Placement.INHERIT


def test_semantic_and_fresh_children_can_prefer_spread() -> None:
    semantic = require_child_policy({"context_mode": "semantic", "placement": "spread"})
    fresh = require_child_policy({"context_mode": "fresh", "placement": "spread"})

    assert semantic.context_mode is ContextMode.SEMANTIC
    assert semantic.placement is Placement.SPREAD
    assert fresh.context_mode is ContextMode.FRESH
    assert fresh.placement is Placement.SPREAD


def test_trunk_cannot_claim_another_provider() -> None:
    with pytest.raises(
        ChildPolicyError,
        match="context_mode=trunk requires placement=inherit",
    ):
        require_child_policy({"context_mode": "trunk", "placement": "spread"})
