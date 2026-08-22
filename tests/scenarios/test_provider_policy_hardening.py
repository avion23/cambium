from __future__ import annotations

from types import SimpleNamespace

import pytest

from cambium.dispatch_policy import order_provider_configs, policy_from_config
from cambium.provider_policy import (
    BillingMode,
    DispatchRequest,
    QuotaUnit,
    QuotaWindow,
    UsageBucket,
    expected_call_cost,
    quota_pressure,
    rank_provider,
    usage_bucket_from_event,
    window_usage,
)
from cambium.provider_resources import BudgetLedger
from cambium.provider_scheduler import BillingMode as SchedulerBillingMode
from cambium.provider_scheduler import QuotaWindowSpec


def test_total_only_usage_is_counted() -> None:
    bucket = usage_bucket_from_event({"usage": {"total_tokens": 123}}, now=1_000.0)

    assert bucket.tokens == 123
    assert bucket.requests == 1


def test_cached_input_price_uses_provider_config_field() -> None:
    provider = SimpleNamespace(
        price_per_1m_in=10.0,
        price_per_1m_cached_in=1.0,
        price_per_1m_out=0.0,
    )
    request = DispatchRequest(expected_input_tokens=100, expected_cached_tokens=100)

    assert expected_call_cost(provider, request) == pytest.approx(0.0001)


def test_scheduler_quota_window_spec_is_enforced() -> None:
    provider = SimpleNamespace(
        quota_windows=(QuotaWindowSpec("requests", 300.0, request_allowance=1),)
    )
    request = DispatchRequest(now=1_000.0)
    debt = SimpleNamespace(
        usage_buckets=(UsageBucket(900, requests=1),),
    )

    feasible, _, reason = quota_pressure(provider, debt, request)

    assert not feasible
    assert reason is not None


def test_window_usage_includes_overlapping_buckets_but_not_future() -> None:
    window = QuotaWindow("rolling", 600.0, 100.0, QuotaUnit.TOKENS)
    buckets = (
        UsageBucket(200, input_tokens=5),  # ends after the window starts at 400
        UsageBucket(500, input_tokens=7),
        UsageBucket(1_001, input_tokens=100),  # future relative to now
    )

    assert window_usage(window, buckets, now=1_000.0) == 12.0


def test_roles_are_explicit_and_fail_closed() -> None:
    request = DispatchRequest()
    empty = SimpleNamespace(name="empty", roles=[])
    malformed = SimpleNamespace(name="bad", roles=["not-a-role"])

    assert not rank_provider(empty, index=0, debt=None, lane=None, request=request).feasible
    with pytest.raises(ValueError, match="invalid provider role"):
        rank_provider(malformed, index=0, debt=None, lane=None, request=request)


def test_disabled_and_saturated_providers_are_infeasible() -> None:
    request = DispatchRequest()
    disabled = SimpleNamespace(name="disabled", enabled=False)
    saturated = SimpleNamespace(name="saturated")
    lane = SimpleNamespace(in_flight=2, capacity=2)

    assert not rank_provider(
        disabled, index=0, debt=None, lane=None, request=request
    ).feasible
    assert not rank_provider(
        saturated, index=0, debt=None, lane=lane, request=request
    ).feasible


def test_non_cash_billing_mode_is_applied_before_budget_check() -> None:
    provider = SimpleNamespace(
        name="subscription",
        billing_mode=BillingMode.SUBSCRIPTION,
        price_per_1m_in=100.0,
        price_per_1m_out=100.0,
    )
    request = DispatchRequest(expected_input_tokens=1_000, budget_usd=0.0)

    assert rank_provider(provider, index=0, debt=None, lane=None, request=request).feasible


def test_balance_observation_preserves_unreconciled_reservation(tmp_path) -> None:
    ledger = BudgetLedger(tmp_path / "budget.db")
    ledger.observe_balance("provider", 10.0)
    reservation = ledger.reserve("provider", 8.0)
    assert reservation is not None

    ledger.observe_balance("provider", 1.0)
    assert ledger.snapshot("provider").reserved_usd == pytest.approx(8.0)
    assert ledger.reserve("provider", 1.0) is None

    ledger.observe_balance("provider", 10.0)
    assert ledger.reserve("provider", 1.0) is not None


def test_dispatch_adapter_matches_scheduler_api() -> None:
    provider = SimpleNamespace(
        name="local",
        model="m",
        billing_mode=SchedulerBillingMode.FREE,
        quota_windows=(),
        price_per_1m_in=0.0,
        price_per_1m_cached_in=0.0,
        price_per_1m_out=0.0,
        pricing_known=False,
        enabled=True,
        max_concurrency=1,
        context_window=0,
        supports_native_tools=True,
        supports_python_tool=True,
    )

    assert policy_from_config(provider).name == "local"
    assert order_provider_configs(
        [provider],
        task_id="task",
        prompt={"messages": []},
        requested_model="m",
        task_class="code",
    ) == [provider]
