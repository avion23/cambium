from __future__ import annotations

import asyncio
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

from cambium.provider_scheduler import (
    BillingMode,
    ProviderEvidence,
    ProviderLease,
    ProviderPolicy,
    ProviderScheduler,
    QuotaLedger,
    QuotaWindowSpec,
    RoutingRequest,
    rank_policies,
)


def _policy(name: str, **kwargs) -> ProviderPolicy:
    return ProviderPolicy(name=name, model=kwargs.pop("model", "m"), **kwargs)


def test_root_lease_is_a_hard_constraint() -> None:
    policies = [_policy("a"), _policy("b", throughput_hint_tps=100)]
    lease = ProviderLease("a", "m", "root")
    ranked = rank_policies(policies, RoutingRequest("child", "m", lease=lease))
    assert [item.name for item in ranked] == ["a"]


def test_model_pin_is_strict_unless_substitution_is_explicit() -> None:
    policies = [_policy("a", model="wanted"), _policy("b", model="other")]
    strict = rank_policies(policies, RoutingRequest("t", "wanted"))
    substituted = rank_policies(
        policies, RoutingRequest("t", "missing", allow_model_substitution=True)
    )
    assert [item.name for item in strict] == ["a"]
    assert {item.name for item in substituted} == {"a", "b"}


def test_throughput_refines_only_equal_priority() -> None:
    policies = [
        _policy("slow", priority=0, throughput_hint_tps=1),
        _policy("fast", priority=0, throughput_hint_tps=50),
        _policy("lower-class", priority=1, throughput_hint_tps=1000),
    ]
    evidence = {
        "slow": ProviderEvidence(attempts=100, successes=95, ewma_tps=2),
        "fast": ProviderEvidence(attempts=100, successes=95, ewma_tps=40),
    }
    ranked = rank_policies(
        policies,
        RoutingRequest("t", "m", expected_output_tokens=1000),
        evidence=evidence,
    )
    assert [item.name for item in ranked] == ["fast", "slow", "lower-class"]


def test_rpm_and_concurrency_have_independent_units() -> None:
    policy = _policy("p", max_concurrency=2)
    assert rank_policies([policy], RoutingRequest("t", "m"), in_flight={"p": 2}) == []


def test_quota_ledger_reservation_is_atomic_across_threads(tmp_path: Path) -> None:
    ledger = QuotaLedger(tmp_path / "quota.db")
    window = QuotaWindowSpec("five-hour", 5 * 3600, request_allowance=10)

    def reserve(index: int):
        return ledger.reserve("zai", (window,), index, now=100.0)

    with ThreadPoolExecutor(max_workers=20) as pool:
        reservations = list(pool.map(reserve, range(20)))
    assert sum(item is not None for item in reservations) == 10
    assert ledger.snapshots("zai")[0].used_requests == 10


def test_scheduler_mailbox_serializes_lane_admission(tmp_path: Path) -> None:
    async def scenario() -> None:
        scheduler = ProviderScheduler(
            [_policy("free", billing_mode=BillingMode.FREE, max_concurrency=1)],
            quota_ledger=QuotaLedger(tmp_path / "quota.db"),
        )
        first = await scheduler.acquire(RoutingRequest("a", "m"))
        try:
            try:
                await scheduler.acquire(RoutingRequest("b", "m"))
            except RuntimeError as exc:
                assert "no provider" in str(exc)
            else:
                raise AssertionError("second acquire unexpectedly succeeded")
        finally:
            await scheduler.release(first, actual_tokens=10, success=True, latency_s=1.0)
        second = await scheduler.acquire(RoutingRequest("b", "m"))
        await scheduler.release(second, actual_tokens=5, success=True, latency_s=1.0)
        await scheduler.close()

    asyncio.run(scenario())
