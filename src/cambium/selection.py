"""Pure provider-quality scoring and candidate ordering."""

from __future__ import annotations

import math
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from typing import Any, Protocol, cast


class Candidate(Protocol):
    """The provider fields required by :func:`order_candidates`."""

    name: str
    priority: int


@dataclass(frozen=True)
class QualityWeights:
    """Tunable boundaries and tie-break weights for measured quality."""

    latency_slo_s: float = 10.0
    latency_weight: float = 1.0
    cache_weight: float = 0.1
    stale_after_s: float = 24.0 * 3600.0
    utilization_weight: float = 0.6
    shadow_weight: float = 0.1


DEFAULT_WEIGHTS = QualityWeights()
QualityScore = tuple[float, int, float, float]


def _field(entry: Any, name: str, default: Any) -> Any:
    if isinstance(entry, Mapping):
        return entry.get(name, default)
    return getattr(entry, name, default)


def _number(value: Any, default: float = 0.0) -> float:
    if isinstance(value, int | float) and not isinstance(value, bool):
        try:
            parsed = float(value)
        except (OverflowError, ValueError):
            return default
        if math.isfinite(parsed):
            return max(0.0, parsed)
    return default


def quality_score(
    entry: Any,
    *,
    now: float,
    weights: QualityWeights = DEFAULT_WEIGHTS,
) -> QualityScore | None:
    """Return a lower-is-better lexicographic quality key, or no evidence.

    The key orders empirical failure probability first, then latency SLO
    compliance, expected cost per successful turn, and finally a normalized
    throughput/latency/cache tie-break. The last component is only reached
    after the hard statistical and SLO comparisons. Missing and stale evidence
    returns ``None`` so it cannot move a provider from its configured position.
    """
    if entry is None:
        return None
    requests = _field(entry, "requests", 0)
    if not isinstance(requests, int) or isinstance(requests, bool) or requests <= 0:
        return None
    last_seen = _field(entry, "last_seen", None)
    if (
        isinstance(last_seen, bool)
        or not isinstance(last_seen, int | float)
        or not math.isfinite(float(last_seen))
        or now - float(last_seen) > weights.stale_after_s
    ):
        return None

    failures = min(requests, int(_number(_field(entry, "failed_requests", 0))))
    successes = requests - failures
    failure_probability = failures / requests

    latency_count = int(_number(_field(entry, "latency_count", 0)))
    latency = 0.0
    if latency_count:
        latency = _number(_field(entry, "latency_total_s", 0.0)) / latency_count
    latency_ratio = latency / weights.latency_slo_s if weights.latency_slo_s > 0 else latency
    slo_miss = int(latency_count > 0 and latency > weights.latency_slo_s)

    cost = _number(_field(entry, "cost", 0.0))
    expected_cost = cost / successes if cost > 0.0 and successes else float("inf")
    cache_hits = min(requests, int(_number(_field(entry, "cache_hit_count", 0))))
    cache_fraction = cache_hits / requests
    # ``tokens_per_s`` is folded by routing.ProviderDebt from the existing
    # usage-event output-token and latency fields.  Keep it in the final
    # tie-break dimension so configured priority, failure/SLO evidence, and
    # equal-cost ordering retain their existing precedence while equal
    # candidates prefer the measured-faster provider.  Missing evidence is
    # neutral (zero) and therefore cannot demote a provider by itself.
    throughput = _number(_field(entry, "tokens_per_s", 0.0))
    tie_break = (
        weights.latency_weight * latency_ratio
        - weights.cache_weight * cache_fraction
        - throughput
    )
    return (failure_probability, slo_miss, expected_cost, tie_break)


def _quality_entry(
    candidate: Candidate,
    debt: Mapping[str, Any] | None,
    *,
    now: float,
    weights: QualityWeights,
) -> QualityScore | None:
    entry = debt.get(candidate.name) if debt else None
    measured = quality_score(entry, now=now, weights=weights)
    if measured is not None:
        return measured

    # A declared hint is only a fallback for providers without fresh usage
    # evidence. Observed DebtStore evidence always wins and remains the source
    # of truth for measured routing quality.
    hint = _number(
        _field(candidate, "tokens_per_s", _field(candidate, "throughput_hint_tps", 0.0))
    )
    if hint <= 0:
        return None
    return quality_score(
        {"requests": 1, "last_seen": now, "tokens_per_s": hint},
        now=now,
        weights=weights,
    )


def order_candidates[T: Candidate](
    eligible: Sequence[T],
    *,
    debt: Mapping[str, Any] | None,
    incumbent: str | None,
    rotation_offset: int,
    now: float,
    weights: QualityWeights = DEFAULT_WEIGHTS,
) -> list[T]:
    """Order eligible providers without reading state or a clock.

    Configured priority is the first ordering key and is never crossed.
    Quality reorders only adjacent measured providers inside an equal-priority
    run. A provider without current evidence is a barrier and therefore keeps
    its configured position. An eligible incumbent is moved to the front of
    its own equal-priority run, expressing cache-locality switching cost without
    bypassing configured priority. Deterministic rotation is applied only to
    runs with no current quality evidence, so load spreading cannot undo a
    measured quality order.
    """
    ordered = sorted(eligible, key=lambda candidate: candidate.priority)
    start = 0
    while start < len(ordered):
        end = start + 1
        while end < len(ordered) and ordered[end].priority == ordered[start].priority:
            end += 1
        measured_start = start
        while measured_start < end:
            score = _quality_entry(ordered[measured_start], debt, now=now, weights=weights)
            if score is None:
                measured_start += 1
                continue
            measured_end = measured_start + 1
            while measured_end < end:
                next_score = _quality_entry(ordered[measured_end], debt, now=now, weights=weights)
                if next_score is None:
                    break
                measured_end += 1
            ordered[measured_start:measured_end] = sorted(
                ordered[measured_start:measured_end],
                key=lambda candidate: cast(
                    QualityScore,
                    _quality_entry(candidate, debt, now=now, weights=weights),
                ),
            )
            measured_start = measured_end + 1
        start = end

    if incumbent is not None:
        incumbent_index = next(
            (index for index, item in enumerate(ordered) if item.name == incumbent),
            None,
        )
        if incumbent_index is None:
            return ordered
        incumbent_priority = ordered[incumbent_index].priority
        run_start = incumbent_index
        while run_start > 0 and ordered[run_start - 1].priority == incumbent_priority:
            run_start -= 1
        bound = ordered.pop(incumbent_index)
        ordered.insert(run_start, bound)
        return ordered

    rotated: list[T] = []
    start = 0
    while start < len(ordered):
        end = start + 1
        while end < len(ordered) and ordered[end].priority == ordered[start].priority:
            end += 1
        run = ordered[start:end]
        has_evidence = any(
            _quality_entry(candidate, debt, now=now, weights=weights) is not None
            for candidate in run
        )
        if len(run) > 1 and not has_evidence:
            offset = rotation_offset % len(run)
            run = run[offset:] + run[:offset]
        rotated.extend(run)
        start = end
    return rotated


__all__ = ["DEFAULT_WEIGHTS", "QualityWeights", "order_candidates", "quality_score"]
