from __future__ import annotations

from typing import Any, cast

import pytest

from cambium.diffundo import ProviderConfig, ProviderTier
from cambium.routing import (
    LaneCapacityExhausted,
    LaneState,
    ProviderAssignment,
    ProviderDebt,
    resolve_assignment,
    select_lane,
    validate_requirements,
)


def _provider(name: str, model: str) -> ProviderConfig:
    return ProviderConfig(
        name=name,
        tier=ProviderTier.FAST,
        base_url="http://127.0.0.1:1",
        api_key_env=f"KEY_{name.upper()}",
        model=model,
    )


def test_provider_debt_record_accepts_an_epoch_timestamp() -> None:
    debt = ProviderDebt()

    debt.record({"failure_reason": "auth_error: rejected"}, now=100.25)

    assert debt.disable_at == 100.25
    assert debt.last_seen == 100.25

    debt.record({}, now=101.5)

    assert debt.disable_at is None
    assert debt.disable_reason is None
    assert debt.last_seen == 101.5


def test_select_lane_allows_the_slot_before_capacity() -> None:
    provider = _provider("a", "m1")
    lane = LaneState(in_flight=59, rpm_allowance=60.0)

    assert select_lane([provider], ["m1"], {}, {"a": lane}) == ("a", "m1")


def test_select_lane_raises_only_for_a_matching_full_lane() -> None:
    provider = _provider("a", "m1")
    lane = LaneState(in_flight=60, rpm_allowance=60.0)

    with pytest.raises(LaneCapacityExhausted) as exc_info:
        select_lane([provider], ["m1"], {}, {"a": lane})

    assert str(exc_info.value) == (
        "model_candidates ['m1'] match no enabled configured provider with a spare lane"
    )


def test_select_lane_does_not_call_a_non_matching_pool_exhausted() -> None:
    with pytest.raises(ValueError) as exc_info:
        select_lane([_provider("a", "other")], ["m1"], {}, {})

    assert type(exc_info.value) is ValueError
    assert str(exc_info.value) == (
        "model_candidates ['m1'] match no enabled configured provider"
    )


def test_resolve_assignment_handles_empty_single_and_zero_lane_pools() -> None:
    provider = _provider("a", "m1")

    assignment = cast(
        ProviderAssignment, resolve_assignment([provider], ["m1"], {}, {})
    )
    assert assignment.provider == "a"
    assert assignment.model == "m1"
    assert assignment.tier == "fast"
    assert cast(
        ProviderAssignment, resolve_assignment([provider], ["m1"], {}, None)
    ).provider == "a"

    with pytest.raises(ValueError, match="match no enabled configured provider"):
        resolve_assignment([], ["m1"], {}, {})


def test_resolve_assignment_reports_all_matching_lanes_exhausted() -> None:
    provider = _provider("a", "m1")

    with pytest.raises(LaneCapacityExhausted):
        resolve_assignment(
            [provider],
            ["m1"],
            {},
            {"a": LaneState(in_flight=1, rpm_allowance=1.0)},
        )


def test_resolve_assignment_breaks_equal_cost_ties_by_config_order() -> None:
    providers = [_provider("a", "m1"), _provider("b", "m2")]
    debt = {
        "a": ProviderDebt(requests=2, cost=4.0),
        "b": ProviderDebt(requests=2, cost=4.0),
    }
    lanes = {"a": LaneState(), "b": LaneState()}

    assignments = [
        cast(
            ProviderAssignment,
            resolve_assignment(
                providers,
                ["m1", "m2"],
                debt,
                lanes,
                requirements={"quality": "normal"},
            ),
        )
        for _ in range(3)
    ]

    assert [assignment.provider for assignment in assignments] == ["a", "a", "a"]
    assert assignments[0].model == "m1"
    assert assignments[0].tier == "fast"


@pytest.mark.parametrize(
    ("requirements", "message"),
    [
        ("high", "requirements must be a mapping"),
        ({"quality": None}, "requirements.quality must be 'high' or 'normal'"),
        ({"quality": "ultra"}, "requirements.quality must be 'high' or 'normal'"),
        ({"quality": 1}, "requirements.quality must be 'high' or 'normal'"),
        (
            {"min_context_window": None},
            "requirements.min_context_window must be a positive int",
        ),
        (
            {"min_context_window": 0},
            "requirements.min_context_window must be a positive int",
        ),
        (
            {"min_context_window": True},
            "requirements.min_context_window must be a positive int",
        ),
        (
            {"min_context_window": 1.5},
            "requirements.min_context_window must be a positive int",
        ),
        (
            {"context": 8000, "tier": "strong"},
            "unknown requirement key(s): 'context', 'tier'",
        ),
    ],
)
def test_validate_requirements_rejects_invalid_values_precisely(
    requirements: Any, message: str
) -> None:
    with pytest.raises(ValueError) as exc_info:
        validate_requirements(requirements)

    assert str(exc_info.value) == message


def test_validate_requirements_preserves_valid_values() -> None:
    requirements = {"quality": "normal", "min_context_window": 128_000}

    assert validate_requirements(requirements) == requirements
    assert validate_requirements(None) == {}
    assert validate_requirements({}) == {}
