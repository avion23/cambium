"""Regression coverage for quarantined lanes in admission ordering."""

from __future__ import annotations

from typing import Any

import pytest

from cambium.diffundo import Diffundo, ProviderConfig, ProviderTier
from cambium.routing import (
    LaneState,
    ProviderDebt,
    resolve_assignment,
    score_providers,
    select_lane,
)


def _provider(name: str, model: str, **overrides: Any) -> ProviderConfig:
    values: dict[str, Any] = {
        "name": name,
        "tier": ProviderTier.FAST,
        "base_url": "http://127.0.0.1:1",
        "api_key_env": f"KEY_{name.upper()}",
        "api_key": f"sk-dead-order-{name}",
        "model": model,
    }
    values.update(overrides)
    return ProviderConfig(**values)


@pytest.mark.parametrize("reason", ["auth_error: credential rejected", "config_error: bad model"])
def test_auth_or_config_dead_lane_is_skipped_before_ordering(reason: str) -> None:
    dead = _provider("dead", "dead-model", priority=0)
    healthy = _provider("healthy", "healthy-model", priority=1)
    debt = {
        "dead": ProviderDebt(disable_reason=reason),
        "healthy": ProviderDebt(),
    }
    lanes = {"dead": LaneState(), "healthy": LaneState()}

    assert select_lane([dead, healthy], ["dead-model", "healthy-model"], debt, lanes) == (
        "healthy",
        "healthy-model",
    )
    assignment = resolve_assignment([dead, healthy], ["dead-model", "healthy-model"], debt, lanes)
    assert assignment is not None
    assert assignment.provider == "healthy"
    assert [
        name
        for name, _model, _score in score_providers(
            [dead, healthy], ["dead-model", "healthy-model"], debt, lanes
        )
    ] == ["healthy"]
    router = Diffundo([dead, healthy], debt=debt)
    assert [provider.name for provider in router._candidates(ProviderTier.FAST, None)] == [
        "healthy"
    ]
    assert lanes["dead"].in_flight == 0
    assert debt["dead"].requests == 0
