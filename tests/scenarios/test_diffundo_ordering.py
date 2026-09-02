"""Canaries pinning Diffundo's priority-ascending cascade contract.

The provider-routing contract rejects weighted round-robin/LRU rotation.
Within a tier the lower ``ProviderConfig.priority`` is tried first; equal-priority
providers pick a sticky primary per instance
(diffundo.py:_candidates, fixed offset seeded by the caller — the worker
seeds it from the task id, and each worker runs one task, so concurrent
subagents spread across providers at task granularity while a task's context
stays on one provider, preserving per-provider prompt-prefix caching),
priority ordering across runs is preserved, and selection stays stateless
otherwise — provider outcomes change eligibility (health / token bucket),
never the primary.

No mocks, no network: each scenario drives real ``Diffundo.call`` against fake
OpenAI-compatible ``http.server`` backends in background threads, reusing the
loopback FakeServer pattern from tests/scenarios/test_diffundo.py. These
canaries are GREEN on current main.
"""

from __future__ import annotations

import asyncio
import time

from diffundo_helpers import PROMPT, FakeServer, _config, _error_payload, _ok_payload

from cambium.diffundo import (
    Diffundo,
    HealthState,
    ProviderStatus,
    ProviderTier,
)

# --------------------------------------------------------------------------- #
# 1. distinct priorities -> lower priority serves first
# --------------------------------------------------------------------------- #


def test_two_healthy_providers_distinct_priorities_try_priority_order() -> None:
    # p_high sits FIRST in config order but carries the HIGHER priority; the
    # priority sort, not config order, must decide the cascade winner.
    low = FakeServer([(200, _ok_payload("low"), 0.0)])
    high = FakeServer([(200, _ok_payload("high"), 0.0)])
    router = Diffundo(
        (
            _config("p_high", high, "K_HIGH", priority=5),
            _config("p_low", low, "K_LOW", priority=0),
        )
    )
    try:
        result = asyncio.run(router.call(ProviderTier.FAST, PROMPT))
        assert result.provider == "p_low"
        assert len(low.calls) == 1
        assert len(high.calls) == 0  # never even dispatched
    finally:
        low.close()
        high.close()


# --------------------------------------------------------------------------- #
# 2. equal priorities -> sticky primaries, priority order across runs preserved
# --------------------------------------------------------------------------- #


def test_rotation_seed_spreads_primaries_across_instances_and_keeps_priority_order() -> None:
    first = FakeServer([(200, _ok_payload("first"), 0.0)])
    second = FakeServer([(200, _ok_payload("second"), 0.0)])
    low = FakeServer([(200, _ok_payload("low"), 0.0)])
    seeded = Diffundo(
        (
            _config("p_first", first, "K_FIRST", priority=0),
            _config("p_second", second, "K_SECOND", priority=0),
            _config("p_low", low, "K_LOW", priority=5),
        ),
        rotation_seed=1,
    )
    unseeded = Diffundo(
        (
            _config("p_first", first, "K_FIRST", priority=0),
            _config("p_second", second, "K_SECOND", priority=0),
            _config("p_low", low, "K_LOW", priority=5),
        ),
    )
    try:
        # seed 1 picks the second provider as this instance's sticky primary.
        assert asyncio.run(seeded.call(ProviderTier.FAST, PROMPT)).provider == "p_second"
        assert asyncio.run(seeded.call(ProviderTier.FAST, PROMPT)).provider == "p_second"
        # the unseeded instance sticks to the first provider: concurrent
        # subagents spread across providers at task granularity.
        assert asyncio.run(unseeded.call(ProviderTier.FAST, PROMPT)).provider == "p_first"
        assert asyncio.run(unseeded.call(ProviderTier.FAST, PROMPT)).provider == "p_first"
        # priority order across runs is preserved: p_low never precedes.
        assert len(low.calls) == 0
    finally:
        first.close()
        second.close()
        low.close()


def test_fallback_moves_association_and_never_bounces_back() -> None:
    """A task's context follows the provider that served; a recovered former
    primary does not reclaim the task (prompt-prefix caching preserved)."""
    first = FakeServer([(500, {"error": "boom"}, 0.0), (200, _ok_payload("first"), 0.0)])
    second = FakeServer([(200, _ok_payload("second"), 0.0)])
    router = Diffundo(
        (
            _config("p_first", first, "K_FIRST", priority=0, cooldown_s=0.05, max_retries=0),
            _config("p_second", second, "K_SECOND", priority=0),
        )
    )
    try:
        # call 1: p_first fails (500 -> cooldown), p_second serves and becomes
        # the task's associated provider.
        r1 = asyncio.run(router.call(ProviderTier.FAST, PROMPT))
        assert r1.provider == "p_second"
        # call 2: p_first's cooldown expired and it is eligible again, but the
        # association leads, so the task stays on p_second — no bounce-back
        # that would cold-start the context at p_first.
        router._runtime("p_first").cooldown_until = time.monotonic() - 1.0
        r2 = asyncio.run(router.call(ProviderTier.FAST, PROMPT))
        assert r2.provider == "p_second"
        assert len(first.calls) == 1
        assert len(second.calls) == 2
    finally:
        first.close()
        second.close()


# --------------------------------------------------------------------------- #
# 4. provider outcome changes eligibility, never selection order
# --------------------------------------------------------------------------- #


def test_provider_outcome_does_not_change_selection_order() -> None:
    flaky = FakeServer([(500, _error_payload("boom"), 0.0)])
    good = FakeServer([(200, _ok_payload("good"), 0.0)])
    router = Diffundo(
        (
            _config("p_flaky", flaky, "K_FLAKY", priority=0),
            _config("p_good", good, "K_GOOD", priority=5),
        )
    )
    try:
        # p_flaky is tried first (priority 0) exactly once; a retryable 500
        # drives it to COOLDOWN (cascade-design §2.4) and the cascade falls
        # through to p_good — no infinite retry on the failing provider
        result = asyncio.run(router.call(ProviderTier.FAST, PROMPT))
        assert result.provider == "p_good"
        assert len(flaky.calls) == 1
        assert len(good.calls) == 1
        assert router.health("p_flaky") is HealthState.COOLDOWN
        assert router.status("p_flaky") is ProviderStatus.COOLDOWN

        # the failing provider is skipped, not re-ordered: the remaining
        # candidates still serve in priority-ascending order
        result2 = asyncio.run(router.call(ProviderTier.FAST, PROMPT))
        assert result2.provider == "p_good"
        assert len(flaky.calls) == 1  # never re-dispatched
        assert len(good.calls) == 2
    finally:
        flaky.close()
        good.close()
