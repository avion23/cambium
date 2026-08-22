"""Pure adapter from provider configs to the production admission objective."""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from typing import Any

from .provider_resources import TaskClass
from .provider_scheduler import (
    BillingMode,
    ProviderEvidence,
    ProviderLease,
    ProviderPolicy,
    QuotaWindowSnapshot,
    RoutingRequest,
    rank_policies,
)


def policy_from_config(provider: Any) -> ProviderPolicy:
    """Project the narrow immutable scheduling facts from a provider config."""

    billing = getattr(provider, "billing_mode", BillingMode.METERED)
    if not isinstance(billing, BillingMode):
        billing = BillingMode(str(billing))
    return ProviderPolicy(
        name=str(provider.name),
        model=str(provider.model),
        priority=int(getattr(provider, "priority", 0)),
        max_concurrency=max(1, int(getattr(provider, "max_concurrency", 1))),
        billing_mode=billing,
        quota_windows=tuple(getattr(provider, "quota_windows", ())),
        price_per_1m_in=float(getattr(provider, "price_per_1m_in", 0.0)),
        price_per_1m_cached_in=float(
            getattr(provider, "price_per_1m_cached_in", 0.0)
        ),
        price_per_1m_out=float(getattr(provider, "price_per_1m_out", 0.0)),
        pricing_known=bool(getattr(provider, "pricing_known", False)),
        throughput_hint_tps=float(getattr(provider, "throughput_hint_tps", 0.0)),
        quality_weight=float(getattr(provider, "quality_weight", 1.0)),
        context_window=int(getattr(provider, "context_window", 0)),
        supports_native_tools=bool(getattr(provider, "supports_native_tools", True)),
        supports_python_tool=bool(getattr(provider, "supports_python_tool", True)),
        enabled=bool(getattr(provider, "enabled", True)),
    )


def estimate_prompt_tokens(prompt: Mapping[str, Any]) -> int:
    """Conservative tokenizer-independent input estimate for admission only."""

    messages = prompt.get("messages")
    if not isinstance(messages, Sequence) or isinstance(messages, (str, bytes)):
        return 0
    total_bytes = 0
    for message in messages:
        if isinstance(message, Mapping):
            total_bytes += len(str(message.get("content", "")).encode("utf-8"))
    return max(1, total_bytes // 4) if total_bytes else 0


def _supports_task_class(provider: Any, task_class: TaskClass) -> bool:
    values = getattr(provider, "task_classes", None)
    if values is None:
        return True
    if isinstance(values, (str, bytes)):
        return False
    try:
        parsed = {
            value if isinstance(value, TaskClass) else TaskClass(str(value))
            for value in values
        }
    except (TypeError, ValueError):
        return False
    return task_class in parsed


def _quality_meets_threshold(provider: Any, minimum: float) -> bool:
    value = getattr(provider, "quality_score", 1.0)
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return False
    return float(value) >= minimum


def _quota_pressure(
    policy: ProviderPolicy,
    snapshots: Sequence[QuotaWindowSnapshot],
    *,
    expected_tokens: int,
    expected_requests: int = 1,
) -> float:
    """Return projected pressure from scheduler quota snapshots.

    ``QuotaWindowSpec`` carries token and request caps while snapshots carry
    both counters.  The scheduler's ranking API does not accept a pressure
    mapping, so the adapter applies this small hard-feasibility check before
    delegating the remaining ordering to ``rank_policies``.
    """

    by_name = {
        snapshot.name: snapshot
        for snapshot in snapshots
        if snapshot.provider == policy.name
    }
    dominant = 0.0
    for window in policy.quota_windows:
        snapshot = by_name.get(window.name)
        if snapshot is None:
            continue
        if window.token_allowance:
            capacity = window.token_allowance * (1.0 - window.reserve_fraction)
            pressure = (
                (snapshot.used_tokens + expected_tokens) / capacity
                if capacity > 0
                else float("inf")
            )
            dominant = max(dominant, pressure)
            if pressure > 1.0:
                return pressure
        if window.request_allowance:
            capacity = window.request_allowance * (1.0 - window.reserve_fraction)
            pressure = (
                (snapshot.used_requests + expected_requests) / capacity
                if capacity > 0
                else float("inf")
            )
            dominant = max(dominant, pressure)
            if pressure > 1.0:
                return pressure
    return dominant


def order_provider_configs(
    candidates: Sequence[Any],
    *,
    task_id: str,
    prompt: Mapping[str, Any],
    requested_model: str | None,
    task_class: TaskClass | str,
    lease: ProviderLease | None = None,
    evidence: Mapping[str, ProviderEvidence] | None = None,
    quota_snapshots: Sequence[QuotaWindowSnapshot] = (),
    expected_output_tokens: int = 4096,
    min_quality_score: float = 0.0,
) -> list[Any]:
    """Apply the real production objective to an already health-feasible set."""

    if not candidates:
        return []
    semantic_class = (
        task_class if isinstance(task_class, TaskClass) else TaskClass(str(task_class))
    )
    eligible_candidates = [
        provider
        for provider in candidates
        if _supports_task_class(provider, semantic_class)
        and _quality_meets_threshold(provider, min_quality_score)
    ]
    if not eligible_candidates:
        return []
    policies = [policy_from_config(provider) for provider in eligible_candidates]
    expected_input_tokens = estimate_prompt_tokens(prompt)
    expected_output = max(0, int(expected_output_tokens))
    quota_feasible = [
        policy
        for policy in policies
        if _quota_pressure(
            policy,
            quota_snapshots,
            expected_tokens=expected_input_tokens + expected_output,
        )
        <= 1.0
    ]
    if not quota_feasible:
        return []
    model = requested_model or quota_feasible[0].model
    request = RoutingRequest(
        task_id=task_id,
        model=model,
        expected_input_tokens=expected_input_tokens,
        expected_output_tokens=expected_output,
        required_context_tokens=expected_input_tokens,
        needs_native_tools=bool(prompt.get("tools")),
        needs_python_tool=False,
        allow_model_substitution=any(
            bool(getattr(provider, "allow_model_substitution", False))
            for provider in candidates
        ),
        incumbent_provider=None if lease is None else lease.provider,
        lease=lease,
    )
    ranked = rank_policies(
        quota_feasible,
        request,
        evidence=evidence,
    )
    by_name = {str(provider.name): provider for provider in eligible_candidates}
    return [by_name[policy.name] for policy in ranked if policy.name in by_name]


__all__ = ["estimate_prompt_tokens", "order_provider_configs", "policy_from_config"]
