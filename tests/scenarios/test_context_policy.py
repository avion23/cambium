from __future__ import annotations

import pytest

from cambium.context_policy import CastPolicy


def test_default_policy_bounds_segment_growth() -> None:
    policy = CastPolicy()
    assert policy.rollover_due(16, 1_000) is False
    assert policy.rollover_due(17, 1_000) is True


def test_policy_mapping_is_strict_and_boolean_safe() -> None:
    policy = CastPolicy.from_mapping(
        {
            "max_segments": 4,
            "max_trunk_tokens": 8_000,
            "min_rollover_savings_tokens": 100,
        }
    )
    assert policy.rollover_due(5, 1) is True
    assert policy.rollover_due(4, 8_001) is True
    with pytest.raises(ValueError, match="unknown CAST"):
        CastPolicy.from_mapping({"max_segments": 4, "ttl": 60})
    with pytest.raises(ValueError, match="non-negative integer"):
        CastPolicy(max_segments=True)  # type: ignore[arg-type]


def test_zero_thresholds_disable_automatic_rollover() -> None:
    policy = CastPolicy(max_segments=0, max_trunk_tokens=0)
    assert policy.rollover_due(1_000_000, 1_000_000_000) is False


def test_rollover_validation_requires_restored_bounds() -> None:
    policy = CastPolicy(max_segments=2, max_trunk_tokens=100)
    policy.validate_rollover(
        before_segments=3,
        before_tokens=90,
        after_segments=1,
        after_tokens=80,
    )
    with pytest.raises(ValueError, match="did not restore"):
        policy.validate_rollover(
            before_segments=3,
            before_tokens=120,
            after_segments=1,
            after_tokens=110,
        )


def test_optional_savings_floor_is_enforced() -> None:
    policy = CastPolicy(max_segments=1, min_rollover_savings_tokens=10)
    with pytest.raises(ValueError, match="minimum token saving"):
        policy.validate_rollover(
            before_segments=2,
            before_tokens=100,
            after_segments=1,
            after_tokens=95,
        )
