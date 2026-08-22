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
    quota_pressure,
    rank_policies,
)


def policy_from_config(provider: Any) -> ProviderPolicy:
    """Project the narrow immutable scheduling facts from a provider config."""

    billing = getattr(provider, "billing_mode", BillingMode.METERED)
    if not isinstance(billing, BillingMode):
        billing = BillingMode(str(billing))
    task_classes = getattr(provider, "task_classes", frozenset(TaskClass))
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
        task_classes=frozenset(task_classes),
        quality_score=float(getattr(provider, "quality_score", 1.0)),
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
    policies = [policy_from_config(provider) for provider in candidates]
    pressures = {
        policy.name: quota_pressure(policy, quota_snapshots) for policy in policies
    }
    model = requested_model or policies[0].model
    request = RoutingRequest(
        task_id=task_id,
        model=model,
        expected_input_tokens=estimate_prompt_tokens(prompt),
        expected_output_tokens=max(0, int(expected_output_tokens)),
        required_context_tokens=estimate_prompt_tokens(prompt),
        needs_native_tools=bool(prompt.get("tools")),
        needs_python_tool=False,
        allow_model_substitution=any(
            bool(getattr(provider, "allow_model_substitution", False))
            for provider in candidates
        ),
        incumbent_provider=None if lease is None else lease.provider,
        lease=lease,
        task_class=semantic_class,
        min_quality_score=min_quality_score,
    )
    ranked = rank_policies(
        policies,
        request,
        evidence=evidence,
        quota_pressure_by_provider=pressures,
    )
    by_name = {str(provider.name): provider for provider in candidates}
    return [by_name[policy.name] for policy in ranked if policy.name in by_name]


__all__ = ["estimate_prompt_tokens", "order_provider_configs", "policy_from_config"]
