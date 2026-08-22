"""Pure provider economics, quota, affinity, and throughput policy.

The scheduler is deliberately split from transports and the supervisor.  This
module contains immutable values and pure ranking functions; callers own the
mutable usage ledger and lane reservations.  A provider cache is a switching
cost, never correctness state.
"""

from __future__ import annotations

import math
import time
from dataclasses import dataclass, replace
from enum import StrEnum
from typing import Any, Iterable, Mapping, Sequence

USAGE_BUCKET_SECONDS = 300
MAX_USAGE_BUCKET_AGE_S = 35 * 24 * 60 * 60


class BillingMode(StrEnum):
    """How scarce provider capacity is paid for."""

    UNKNOWN = "unknown"
    SUBSCRIPTION = "subscription"
    PREPAID = "prepaid"
    METERED = "metered"
    FREE = "free"
    LOCAL = "local"


class ProviderRole(StrEnum):
    """Work classes a provider is allowed to serve."""

    TRUNK = "trunk"
    SUBAGENT = "subagent"
    SUMMARY = "summary"
    REVIEW = "review"


class AffinityMode(StrEnum):
    """How strongly one task is bound to its incumbent provider."""

    STRICT = "strict"
    STICKY = "sticky"
    OPPORTUNISTIC = "opportunistic"


class QuotaUnit(StrEnum):
    TOKENS = "tokens"
    REQUESTS = "requests"
    USD = "usd"


ALL_PROVIDER_ROLES = tuple(ProviderRole)


@dataclass(frozen=True, slots=True)
class QuotaWindow:
    """One locally-accounted quota window.

    ``reset_at`` is optional provider evidence for a fixed window.  Without it,
    the window is treated as rolling.  The usable limit excludes the configured
    reserve so trunk/critical work can keep capacity in hand.
    """

    name: str
    duration_s: float
    limit: float
    unit: QuotaUnit
    reserve_fraction: float = 0.0
    reset_at: float | None = None

    @property
    def usable_limit(self) -> float:
        return self.limit * (1.0 - self.reserve_fraction)


@dataclass(frozen=True, slots=True)
class UsageBucket:
    """Five-minute provider usage bucket retained for rolling-window queries."""

    start_s: int
    input_tokens: int = 0
    output_tokens: int = 0
    requests: int = 0
    cost_usd: float = 0.0

    @property
    def tokens(self) -> int:
        return self.input_tokens + self.output_tokens


@dataclass(frozen=True, slots=True)
class DispatchRequest:
    """Provider-independent scheduling request."""

    role: ProviderRole = ProviderRole.SUBAGENT
    affinity: AffinityMode = AffinityMode.OPPORTUNISTIC
    incumbent: str | None = None
    expected_input_tokens: int = 8_000
    expected_output_tokens: int = 1_000
    expected_cached_tokens: int = 0
    budget_usd: float | None = None
    now: float = 0.0

    def at(self, now: float | None = None) -> DispatchRequest:
        value = time.time() if now is None else now
        return replace(self, now=float(value))


@dataclass(frozen=True, slots=True)
class ProviderRank:
    """Lower-is-better lexicographic provider rank plus diagnostics."""

    feasible: bool
    key: tuple[float | int, ...]
    quota_pressure: float
    expected_cost_usd: float
    useful_output_tokens_per_s: float
    reason: str | None = None


def _non_negative_number(value: Any, default: float = 0.0) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return default
    parsed = float(value)
    return parsed if math.isfinite(parsed) and parsed >= 0 else default


def _count(value: Any) -> int:
    return int(_non_negative_number(value))


def usage_bucket_from_event(
    event: Mapping[str, Any], *, now: float | None = None
) -> UsageBucket:
    """Normalize one redacted usage event into its five-minute bucket."""

    timestamp = time.time() if now is None else float(now)
    start_s = int(timestamp // USAGE_BUCKET_SECONDS) * USAGE_BUCKET_SECONDS
    usage = event.get("usage")
    if not isinstance(usage, Mapping):
        usage = {}
    input_tokens = 0
    for key in ("input_tokens", "prompt_tokens"):
        if key in usage:
            input_tokens = _count(usage.get(key))
            break
    output_tokens = 0
    for key in ("output_tokens", "completion_tokens"):
        if key in usage:
            output_tokens = _count(usage.get(key))
            break
    return UsageBucket(
        start_s=start_s,
        input_tokens=input_tokens,
        output_tokens=output_tokens,
        requests=1,
        cost_usd=_non_negative_number(event.get("estimated_cost_usd")),
    )


def add_usage_bucket(
    buckets: Sequence[UsageBucket],
    event: Mapping[str, Any],
    *,
    now: float | None = None,
) -> tuple[UsageBucket, ...]:
    """Add one event, coalescing the current bucket and pruning old history."""

    timestamp = time.time() if now is None else float(now)
    incoming = usage_bucket_from_event(event, now=timestamp)
    retained = [
        bucket
        for bucket in buckets
        if bucket.start_s >= timestamp - MAX_USAGE_BUCKET_AGE_S
    ]
    if retained and retained[-1].start_s == incoming.start_s:
        prior = retained[-1]
        retained[-1] = UsageBucket(
            start_s=prior.start_s,
            input_tokens=prior.input_tokens + incoming.input_tokens,
            output_tokens=prior.output_tokens + incoming.output_tokens,
            requests=prior.requests + incoming.requests,
            cost_usd=prior.cost_usd + incoming.cost_usd,
        )
    else:
        retained.append(incoming)
    return tuple(retained)


def merge_usage_buckets(
    base: Sequence[UsageBucket],
    local: Sequence[UsageBucket],
    baseline: Sequence[UsageBucket],
    *,
    now: float | None = None,
) -> tuple[UsageBucket, ...]:
    """Merge this session's per-bucket delta into a newer on-disk snapshot."""

    timestamp = time.time() if now is None else float(now)

    def mapping(values: Sequence[UsageBucket]) -> dict[int, UsageBucket]:
        return {bucket.start_s: bucket for bucket in values}

    merged = mapping(base)
    before = mapping(baseline)
    for start, current in mapping(local).items():
        prior = before.get(start, UsageBucket(start))
        target = merged.get(start, UsageBucket(start))
        merged[start] = UsageBucket(
            start_s=start,
            input_tokens=max(0, target.input_tokens + current.input_tokens - prior.input_tokens),
            output_tokens=max(0, target.output_tokens + current.output_tokens - prior.output_tokens),
            requests=max(0, target.requests + current.requests - prior.requests),
            cost_usd=max(0.0, target.cost_usd + current.cost_usd - prior.cost_usd),
        )
    cutoff = timestamp - MAX_USAGE_BUCKET_AGE_S
    return tuple(merged[key] for key in sorted(merged) if key >= cutoff)


def window_usage(
    window: QuotaWindow,
    buckets: Sequence[UsageBucket],
    *,
    now: float,
) -> float:
    """Usage in one rolling or fixed provider window."""

    reset_at = window.reset_at
    if reset_at is not None and math.isfinite(reset_at):
        while reset_at <= now:
            reset_at += window.duration_s
        start = reset_at - window.duration_s
    else:
        start = now - window.duration_s
    selected = (bucket for bucket in buckets if bucket.start_s >= start)
    if window.unit is QuotaUnit.TOKENS:
        return float(sum(bucket.tokens for bucket in selected))
    if window.unit is QuotaUnit.REQUESTS:
        return float(sum(bucket.requests for bucket in selected))
    return float(sum(bucket.cost_usd for bucket in selected))


def expected_call_cost(provider: Any, request: DispatchRequest) -> float:
    """Expected marginal cash charge for a request under provider tariffs."""

    cached = min(
        max(0, request.expected_cached_tokens),
        max(0, request.expected_input_tokens),
    )
    uncached = max(0, request.expected_input_tokens - cached)
    input_price = _non_negative_number(getattr(provider, "price_per_1m_in", 0.0))
    output_price = _non_negative_number(getattr(provider, "price_per_1m_out", 0.0))
    cache_read_price = _non_negative_number(
        getattr(provider, "price_per_1m_cache_read", input_price)
    )
    return (
        uncached * input_price
        + cached * cache_read_price
        + max(0, request.expected_output_tokens) * output_price
    ) / 1_000_000.0


def _provider_roles(provider: Any) -> tuple[ProviderRole, ...]:
    values = getattr(provider, "roles", ALL_PROVIDER_ROLES)
    roles: list[ProviderRole] = []
    if isinstance(values, Iterable) and not isinstance(values, (str, bytes)):
        for value in values:
            try:
                role = value if isinstance(value, ProviderRole) else ProviderRole(str(value))
            except ValueError:
                continue
            if role not in roles:
                roles.append(role)
    return tuple(roles) or ALL_PROVIDER_ROLES


def _provider_windows(provider: Any) -> tuple[QuotaWindow, ...]:
    values = getattr(provider, "quota_windows", ())
    return tuple(item for item in values if isinstance(item, QuotaWindow))


def _buckets(debt: Any) -> tuple[UsageBucket, ...]:
    values = getattr(debt, "usage_buckets", ()) if debt is not None else ()
    return tuple(item for item in values if isinstance(item, UsageBucket))


def quota_pressure(
    provider: Any,
    debt: Any,
    request: DispatchRequest,
) -> tuple[bool, float, str | None]:
    """Return feasibility and dominant projected quota utilization."""

    windows = _provider_windows(provider)
    now = request.now or time.time()
    buckets = _buckets(debt)
    projected_cost = expected_call_cost(provider, request)
    dominant = 0.0
    for window in windows:
        usable = window.usable_limit
        if usable <= 0:
            return False, math.inf, f"quota window {window.name!r} has no usable capacity"
        used = window_usage(window, buckets, now=now)
        increment = {
            QuotaUnit.TOKENS: request.expected_input_tokens + request.expected_output_tokens,
            QuotaUnit.REQUESTS: 1,
            QuotaUnit.USD: projected_cost,
        }[window.unit]
        pressure = (used + increment) / usable
        dominant = max(dominant, pressure)
        if pressure > 1.0:
            return False, pressure, f"quota window {window.name!r} is exhausted"

    billing = getattr(provider, "billing_mode", BillingMode.UNKNOWN)
    try:
        billing = billing if isinstance(billing, BillingMode) else BillingMode(str(billing))
    except ValueError:
        billing = BillingMode.UNKNOWN
    balance = getattr(provider, "balance_usd", None)
    if billing is BillingMode.PREPAID and isinstance(balance, (int, float)):
        spent = _non_negative_number(getattr(debt, "cost", 0.0)) if debt is not None else 0.0
        if spent + projected_cost > float(balance):
            return False, math.inf, "prepaid balance is exhausted"
        if balance > 0:
            dominant = max(dominant, (spent + projected_cost) / float(balance))

    legacy_allowance = _non_negative_number(
        getattr(provider, "token_window_allowance", 0.0)
    )
    if not windows and legacy_allowance > 0:
        tokens = _non_negative_number(getattr(debt, "tokens", 0.0)) if debt is not None else 0.0
        dominant = (tokens + request.expected_input_tokens + request.expected_output_tokens) / legacy_allowance
        if dominant > 1.0:
            return False, dominant, "legacy token allowance is exhausted"
    return True, dominant, None


def expected_useful_output_tps(provider: Any, debt: Any) -> float:
    """Shrink observed throughput/success toward provider priors."""

    prior_tps = _non_negative_number(getattr(provider, "throughput_prior_tps", 0.0))
    if prior_tps <= 0:
        tier = getattr(getattr(provider, "tier", None), "value", "")
        prior_tps = {
            "fast": 80.0,
            "balanced": 50.0,
            "strong": 30.0,
            "reasoning": 12.0,
        }.get(str(tier), 20.0)
    prior_seconds = 5.0
    observed_output = _non_negative_number(getattr(debt, "output_tokens", 0.0))
    observed_latency = _non_negative_number(getattr(debt, "latency_total_s", 0.0))
    throughput = (observed_output + prior_tps * prior_seconds) / (
        observed_latency + prior_seconds
    )
    requests = _count(getattr(debt, "requests", 0)) if debt is not None else 0
    failures = min(requests, _count(getattr(debt, "failed_requests", 0)))
    success_probability = (requests - failures + 2.0) / (requests + 3.0)
    quality = _non_negative_number(getattr(provider, "quality_prior", 0.75), 0.75)
    quality = min(1.0, quality)
    return throughput * success_probability * quality


def rank_provider(
    provider: Any,
    *,
    index: int,
    debt: Any,
    lane: Any,
    request: DispatchRequest,
) -> ProviderRank:
    """Compute a role/affinity/quota-aware lower-is-better rank."""

    if request.role not in _provider_roles(provider):
        return ProviderRank(False, (math.inf,), math.inf, 0.0, 0.0, "role unsupported")
    name = str(getattr(provider, "name", ""))
    if request.affinity is AffinityMode.STRICT and request.incumbent:
        if name != request.incumbent:
            return ProviderRank(False, (math.inf,), math.inf, 0.0, 0.0, "strict affinity")
    feasible, pressure, reason = quota_pressure(provider, debt, request)
    if not feasible:
        return ProviderRank(False, (math.inf,), pressure, expected_call_cost(provider, request), 0.0, reason)

    expected_cost = expected_call_cost(provider, request)
    if request.budget_usd is not None and expected_cost > request.budget_usd:
        return ProviderRank(False, (math.inf,), pressure, expected_cost, 0.0, "request budget exceeded")
    useful_tps = expected_useful_output_tps(provider, debt)
    requests = _count(getattr(debt, "requests", 0)) if debt is not None else 0
    failures = min(requests, _count(getattr(debt, "failed_requests", 0)))
    failure_risk = (failures + 1.0) / (requests + 3.0)
    in_flight = _count(getattr(lane, "in_flight", 0)) if lane is not None else 0
    priority = int(getattr(provider, "priority", 0))
    affinity_penalty = 0
    if request.affinity is AffinityMode.STICKY and request.incumbent:
        affinity_penalty = 0 if name == request.incumbent else 1

    billing = getattr(provider, "billing_mode", BillingMode.UNKNOWN)
    try:
        billing = billing if isinstance(billing, BillingMode) else BillingMode(str(billing))
    except ValueError:
        billing = BillingMode.UNKNOWN
    if billing in {BillingMode.SUBSCRIPTION, BillingMode.FREE, BillingMode.LOCAL}:
        cash_pressure = 0.0
    elif billing is BillingMode.UNKNOWN and expected_cost == 0.0:
        # Zero with unknown pricing is not proof that a provider is free.
        cash_pressure = 0.25
    else:
        cash_pressure = expected_cost

    # Priority is a configured policy class. Inside it, preserve a trunk's
    # incumbent, then minimize scarce quota/cash/failure risk and maximize
    # useful output throughput. The final keys are deterministic tie-breaks.
    key: tuple[float | int, ...] = (
        priority,
        affinity_penalty,
        pressure,
        failure_risk,
        cash_pressure,
        -useful_tps,
        in_flight,
        requests,
        index,
    )
    return ProviderRank(True, key, pressure, expected_cost, useful_tps)


__all__ = [
    "ALL_PROVIDER_ROLES",
    "AffinityMode",
    "BillingMode",
    "DispatchRequest",
    "MAX_USAGE_BUCKET_AGE_S",
    "ProviderRank",
    "ProviderRole",
    "QuotaUnit",
    "QuotaWindow",
    "USAGE_BUCKET_SECONDS",
    "UsageBucket",
    "add_usage_bucket",
    "expected_call_cost",
    "expected_useful_output_tps",
    "merge_usage_buckets",
    "quota_pressure",
    "rank_provider",
    "usage_bucket_from_event",
    "window_usage",
]
