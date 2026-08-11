"""Supervisor-level task admission balancing (solution C) — model-selector engine.

Balances (model, provider) selection *before* the model filter partitions the
provider pool. Tasks that declare ``model_candidates`` (instead of a pinned
``fanout_config.model``) are resolved at admission from a usage-debt ledger:
``select_primary`` picks the provider serving a candidate model with the
lowest normalized utilization (tokens consumed / window allowance), so
provider subscriptions deplete at similar rates while every task stays bound
to its assigned provider (prompt-prefix caching preserved).

The ledger is a :class:`DebtStore`: durable counts/tokens only (never
credentials) at ``~/.config/cambium/routing-state.json``, written atomically
(temp file + ``os.replace``), plus an in-memory session accumulator the
supervisor feeds live as redacted ``usage_event`` rows arrive, so later
admissions in the same session see updated debt. A missing or corrupt ledger
file loads as an empty ledger.

The window allowance defaults to :data:`DEFAULT_TOKEN_WINDOW_ALLOWANCE`
(20M tokens, a placeholder until real quota contracts are measured,
implementation-plan step 3); a provider config may override it per provider
with the optional ``token_window_allowance`` field.

Provider lanes (H1) add concurrency-aware admission on top of the ledger:
each provider owns one :class:`LaneState` (in-flight tasks plus an
``rpm``-derived concurrency allowance) and :func:`select_lane` picks the
provider with the lowest normalized utilization among lanes with spare
capacity, so a wave of concurrent admissions spreads across providers instead
of all picking the same max-min winner. 429 pressure shrinks a lane's
effective in-flight cap (placeholder adaptive rule, see
``LaneState.effective_in_flight_cap``), which is the admission-side
backpressure that prevents retry storms.

Capability/quality-constrained selection (H2): when a task declares
``requirements``, :func:`score_providers` filters candidates strictly by
capability (``quality == "high"`` keeps only ``ProviderTier.STRONG``
providers; unknown requirement keys raise ``ValueError`` so a task never
silently downgrades) and then ranks the eligible providers by a weighted
score of normalized utilization, cache-hit rate, expected latency, and a
shadow price (utilization squared — tokens grow scarcer as a window fills).
The weights are module constants (:data:`W_UTIL` etc.), documented
placeholders until measured quality/latency data exists (implementation-plan
step 3/5). Without ``requirements``, ``select_primary``/``select_lane`` keep
their exact pre-H2 behavior.
"""

from __future__ import annotations

import json
import os
import tempfile
import time
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, replace
from pathlib import Path
from typing import Any

from .diffundo import ProviderTier

# Placeholder weekly-equivalent token window per provider. No measured quota
# contract exists yet (implementation-plan step 3); a provider config may
# override this per provider with ``token_window_allowance``.
DEFAULT_TOKEN_WINDOW_ALLOWANCE = 20_000_000
DEFAULT_ROUTING_STATE_PATH = Path.home() / ".config" / "cambium" / "routing-state.json"
_ROUTING_STATE_VERSION = 1

# Placeholder scoring weights for score_providers (H2). No measured
# quality/latency evidence exists yet (implementation-plan step 3/5): these
# are documented placeholders until usage/quota data is stable.
W_UTIL = 0.6
W_CACHE = 0.2
W_LATENCY = 0.1
W_SHADOW = 0.1
REFERENCE_LATENCY_S = 30.0
_REQUIREMENT_KEYS = frozenset({"quality", "min_context_window"})


@dataclass
class ProviderDebt:
    """Per-provider rolling usage state, folded from redacted usage events.

    ``tokens`` accumulates prompt+completion (or ``total_tokens`` when the
    provider reports it); ``retry_after_count`` counts 429-style events
    (``request_rate_status == "cooldown"`` or a ``failure_reason`` containing
    ``429``). Only counts/tokens — never credentials — ever enter the ledger.
    """

    tokens: int = 0
    requests: int = 0
    failed_requests: int = 0
    cost: float = 0.0
    retry_after_count: int = 0
    # Provider-reported cache hits and call latency, folded for later routing
    # evidence (H2 uses them; H1 only records them).
    cache_hit_count: int = 0
    latency_total_s: float = 0.0
    latency_count: int = 0
    last_seen: float | None = None

    def record(self, event: Mapping[str, Any]) -> None:
        """Fold one usage_event payload into this provider's debt."""
        self.requests += 1
        usage = event.get("usage")
        if isinstance(usage, Mapping):
            total = usage.get("total_tokens")
            if isinstance(total, (int, float)) and not isinstance(total, bool):
                self.tokens += int(total)
            else:
                inputs = usage.get("input_tokens", usage.get("prompt_tokens"))
                outputs = usage.get("output_tokens", usage.get("completion_tokens"))
                if (
                    isinstance(inputs, (int, float))
                    and not isinstance(inputs, bool)
                    and isinstance(outputs, (int, float))
                    and not isinstance(outputs, bool)
                ):
                    self.tokens += int(inputs) + int(outputs)
        cost = event.get("estimated_cost_usd")
        if isinstance(cost, (int, float)) and not isinstance(cost, bool):
            self.cost += float(cost)
        failure_reason = event.get("failure_reason")
        if isinstance(failure_reason, str) and failure_reason:
            self.failed_requests += 1
        if event.get("request_rate_status") == "cooldown" or (
            isinstance(failure_reason, str) and "429" in failure_reason
        ):
            self.retry_after_count += 1
        if event.get("provider_cache_hit") is True:
            self.cache_hit_count += 1
        latency = event.get("latency_s")
        if (
            isinstance(latency, (int, float))
            and not isinstance(latency, bool)
            and latency >= 0
        ):
            self.latency_total_s += float(latency)
            self.latency_count += 1
        self.last_seen = time.time()


def _debt_from_mapping(name: str, entry: Mapping[str, Any]) -> ProviderDebt:
    """Parse one ledger entry, ignoring malformed fields (tolerate corruption)."""
    debt = ProviderDebt()
    for field, converter in (
        ("tokens", int),
        ("requests", int),
        ("failed_requests", int),
        ("retry_after_count", int),
        ("cache_hit_count", int),
        ("latency_count", int),
    ):
        value = entry.get(field)
        if isinstance(value, (int, float)) and not isinstance(value, bool) and value >= 0:
            setattr(debt, field, converter(value))
    cost = entry.get("cost")
    if isinstance(cost, (int, float)) and not isinstance(cost, bool) and cost >= 0:
        debt.cost = float(cost)
    latency_total_s = entry.get("latency_total_s")
    if (
        isinstance(latency_total_s, (int, float))
        and not isinstance(latency_total_s, bool)
        and latency_total_s >= 0
    ):
        debt.latency_total_s = float(latency_total_s)
    last_seen = entry.get("last_seen")
    if isinstance(last_seen, (int, float)) and not isinstance(last_seen, bool):
        debt.last_seen = float(last_seen)
    return debt


class DebtStore:
    """Usage-debt ledger: durable file plus in-memory session accumulator.

    ``load`` replaces memory with the persisted ledger (a missing or corrupt
    file is an empty ledger); ``record`` folds live usage events into the
    in-memory accumulator; ``save`` atomically rewrites the ledger file
    (``mkstemp`` in the same directory + fsync + ``os.replace``).
    """

    def __init__(self, path: str | Path | None = None) -> None:
        if path is None:
            env_path = os.environ.get("CAMBIUM_ROUTING_STATE_PATH")
            path = env_path if env_path else DEFAULT_ROUTING_STATE_PATH
        self._path = Path(path).expanduser()
        self._debts: dict[str, ProviderDebt] = {}
        self._dirty = False

    @property
    def path(self) -> Path:
        return self._path

    @property
    def dirty(self) -> bool:
        """True when live usage events have been folded since load/save."""
        return self._dirty

    #: Age half-life for the persisted ledger (hours): a provider's recorded
    #: debt halves every 24h of wall time since ``last_seen`` so historical
    #: burn cannot saturate utilization forever (placeholder until real quota
    #: windows are measured; see module docstring).
    _DECAY_HALF_LIFE_HOURS = 24.0

    def load(self) -> None:
        """Replace memory with the persisted ledger; tolerate a bad file.

        Applies exponential time-decay to recorded debt so cross-session
        accumulation cannot permanently skew max-min selection: each counter
        is scaled by ``0.5 ** (age_hours / 24)`` where age is measured from
        the entry's ``last_seen`` timestamp.
        """
        try:
            text = self._path.read_text(encoding="utf-8")
        except OSError:
            self._debts = {}
            return
        try:
            raw = json.loads(text)
        except ValueError:
            self._debts = {}
            return
        debts: dict[str, ProviderDebt] = {}
        if (
            isinstance(raw, Mapping)
            and raw.get("version") == _ROUTING_STATE_VERSION
            and isinstance(raw.get("providers"), Mapping)
        ):
            now = time.time()
            for name, entry in raw["providers"].items():
                if isinstance(name, str) and isinstance(entry, Mapping):
                    debt = _debt_from_mapping(name, entry)
                    age_hours = max(0.0, (now - debt.last_seen) / 3600.0)
                    factor = 0.5 ** (age_hours / self._DECAY_HALF_LIFE_HOURS)
                    # Only decay meaningfully-aged entries: fresh data (same
                    # session) must round-trip untouched, and a <1% decay is
                    # below any selection-relevant resolution anyway.
                    if factor <= 0.99:
                        debt = replace(
                            debt,
                            tokens=round(debt.tokens * factor),
                            requests=round(debt.requests * factor),
                            failed_requests=round(debt.failed_requests * factor),
                            retry_after_count=round(debt.retry_after_count * factor),
                            cost=debt.cost * factor,
                        )
                    debts[name] = debt
        self._debts = debts

    def record(self, event: Mapping[str, Any]) -> None:
        """Fold one usage event into the in-memory accumulator."""
        provider = event.get("provider")
        if not isinstance(provider, str) or not provider:
            return
        debt = self._debts.get(provider)
        if debt is None:
            debt = ProviderDebt()
            self._debts[provider] = debt
        debt.record(event)
        self._dirty = True

    def as_mapping(self) -> dict[str, ProviderDebt]:
        """Snapshot of per-provider debt for a pure selection call."""
        return dict(self._debts)

    def save(self) -> None:
        """Atomically persist the ledger (redacted counts/tokens only)."""
        payload = {
            "version": _ROUTING_STATE_VERSION,
            "providers": {
                name: {
                    "tokens": debt.tokens,
                    "requests": debt.requests,
                    "failed_requests": debt.failed_requests,
                    "cost": debt.cost,
                    "retry_after_count": debt.retry_after_count,
                    "cache_hit_count": debt.cache_hit_count,
                    "latency_total_s": debt.latency_total_s,
                    "latency_count": debt.latency_count,
                    "last_seen": debt.last_seen,
                }
                for name, debt in sorted(self._debts.items())
            },
        }
        content = json.dumps(payload, indent=2, sort_keys=True) + "\n"
        self._path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        descriptor, temporary_name = tempfile.mkstemp(
            prefix=f".{self._path.name}.", suffix=".tmp", dir=self._path.parent
        )
        temporary_path = Path(temporary_name)
        try:
            with os.fdopen(descriptor, "w", encoding="utf-8", newline="\n") as handle:
                handle.write(content)
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(temporary_path, self._path)
            temporary_path = None
        finally:
            if temporary_path is not None:
                try:
                    temporary_path.unlink()
                except FileNotFoundError:
                    pass


def _window_allowance(provider: Any) -> float:
    allowance = getattr(provider, "token_window_allowance", 0.0) or 0.0
    if isinstance(allowance, bool) or not isinstance(allowance, (int, float)):
        return float(DEFAULT_TOKEN_WINDOW_ALLOWANCE)
    if allowance <= 0:
        return float(DEFAULT_TOKEN_WINDOW_ALLOWANCE)
    return float(allowance)


def _normalized_utilization(
    provider: Any, debt: Mapping[str, ProviderDebt] | None
) -> float:
    current = debt.get(provider.name) if debt is not None else None
    tokens = current.tokens if current is not None else 0
    return tokens / _window_allowance(provider)


def select_primary(
    providers: Sequence[Any],
    candidates: Sequence[str],
    debt: Mapping[str, ProviderDebt] | None = None,
) -> tuple[str, str]:
    """Max-min admission pick: the provider serving a candidate model with the
    lowest normalized utilization (tokens consumed / window allowance) wins.

    Only enabled providers whose ``model`` is one of ``candidates`` are
    considered; the returned ``(provider_name, model)`` binds the task to the
    chosen provider. Ties break by fewer requests, then config order. Raises
    ``ValueError`` when no enabled provider serves a candidate model.
    """
    candidates = tuple(candidates)
    if not candidates:
        raise ValueError("model_candidates must be a non-empty list of model ids")
    serving: list[tuple[int, Any]] = []
    for index, provider in enumerate(providers):
        if not getattr(provider, "enabled", True):
            continue
        model = getattr(provider, "model", "")
        if isinstance(model, str) and model in candidates:
            serving.append((index, provider))
    if not serving:
        raise ValueError(
            f"model_candidates {list(candidates)!r} match no enabled configured provider"
        )

    def rank(item: tuple[int, Any]) -> tuple[float, int, int]:
        index, provider = item
        current = debt.get(provider.name) if debt is not None else None
        requests = current.requests if current is not None else 0
        return (_normalized_utilization(provider, debt), requests, index)

    _, winner = min(serving, key=rank)
    return winner.name, winner.model


@dataclass
class LaneState:
    """One concurrency lane per provider subscription (H1).

    ``in_flight`` counts tasks admitted onto the lane (batch pre-assignment or
    admission-time assignment); ``rpm_allowance`` is the provider's configured
    requests-per-minute, the concurrency budget the lane may keep in flight.
    """

    in_flight: int = 0
    rpm_allowance: float = 60.0

    def effective_in_flight_cap(self, retry_after_count: int) -> int:
        """In-flight cap under 429 pressure: ``max(1, floor(rpm * decay))``.

        Placeholder adaptive rule: each 429-style usage event shrinks the
        budget by ``1/50`` of the allowance, floor 50%, until live 429 events
        stop and the debt stops accumulating. The cap never drops below 1 so
        a pressured provider keeps serving at least one task instead of being
        fully starved (the 429 is the provider's own backpressure signal).
        """
        decay = max(0.5, 1.0 - retry_after_count / 50.0)
        return max(1, int(self.rpm_allowance * decay))


def select_lane(
    providers: Sequence[Any],
    candidates: Sequence[str],
    debt: Mapping[str, ProviderDebt] | None,
    lanes: Mapping[str, LaneState],
) -> tuple[str, str]:
    """Max-min admission pick over per-provider lanes (H1).

    Like :func:`select_primary`, but a provider whose lane is at or above its
    effective in-flight cap (``rpm_allowance`` decayed by that provider's
    ``retry_after_count`` in ``debt``) is skipped entirely, so concurrent
    admissions in one wave spread across providers and a 429-pressured
    provider admits fewer tasks instead of colliding and retrying. Remaining
    providers rank by (normalized utilization, lane in_flight, requests,
    config index): max-min utilization semantics with idle lanes winning
    ties. Raises ``ValueError`` when no enabled provider with a spare lane
    serves a candidate model.
    """
    candidates = tuple(candidates)
    if not candidates:
        raise ValueError("model_candidates must be a non-empty list of model ids")
    serving: list[tuple[int, Any]] = []
    for index, provider in enumerate(providers):
        if not getattr(provider, "enabled", True):
            continue
        model = getattr(provider, "model", "")
        if not (isinstance(model, str) and model in candidates):
            continue
        lane = lanes.get(provider.name)
        if lane is not None:
            current = debt.get(provider.name) if debt is not None else None
            retry_after_count = current.retry_after_count if current is not None else 0
            if lane.in_flight >= lane.effective_in_flight_cap(retry_after_count):
                continue
        serving.append((index, provider))
    if not serving:
        raise ValueError(
            f"model_candidates {list(candidates)!r} match no enabled configured "
            "provider with a spare lane"
        )

    def rank(item: tuple[int, Any]) -> tuple[float, int, int, int]:
        index, provider = item
        lane = lanes.get(provider.name)
        current = debt.get(provider.name) if debt is not None else None
        requests = current.requests if current is not None else 0
        in_flight = lane.in_flight if lane is not None else 0
        return (_normalized_utilization(provider, debt), in_flight, requests, index)

    _, winner = min(serving, key=rank)
    return winner.name, winner.model


def validate_requirements(requirements: Mapping[str, Any] | None) -> dict[str, Any]:
    """Validate a task's capability/quality requirements, fail-closed.

    ``quality`` must be ``"high"`` or ``"normal"``; ``min_context_window``
    must be a positive int. Unknown keys raise ``ValueError`` — a task that
    declares a requirement the selector does not understand fails closed
    instead of silently downgrading the task. Returns a plain dict copy
    (``{}`` for ``None``/absent).
    """
    if requirements is None:
        return {}
    if not isinstance(requirements, Mapping):
        raise ValueError("requirements must be a mapping")
    unknown = sorted(set(requirements) - _REQUIREMENT_KEYS)
    if unknown:
        raise ValueError(
            "unknown requirement key(s): " + ", ".join(map(repr, unknown))
        )
    quality = requirements.get("quality")
    if quality is not None and (
        not isinstance(quality, str) or quality not in ("high", "normal")
    ):
        raise ValueError("requirements.quality must be 'high' or 'normal'")
    min_context_window = requirements.get("min_context_window")
    if min_context_window is not None and (
        isinstance(min_context_window, bool)
        or not isinstance(min_context_window, int)
        or min_context_window <= 0
    ):
        raise ValueError("requirements.min_context_window must be a positive int")
    return dict(requirements)


def score_providers(
    providers: Sequence[Any],
    candidates: Sequence[str],
    debt: Mapping[str, ProviderDebt] | None,
    lanes: Mapping[str, LaneState] | None = None,
    *,
    requirements: Mapping[str, Any] | None = None,
) -> list[tuple[str, str, float]]:
    """Capability-constrained scoring of providers serving a candidate model (H2).

    The capability filter is STRICT and never violated: ``quality == "high"``
    restricts candidates to providers whose tier is
    :attr:`ProviderTier.STRONG`; ``"normal"`` or absent applies no tier
    restriction. ``min_context_window`` is validated (positive int) but has no
    effect today because :class:`~cambium.diffundo.ProviderConfig` carries no
    ``context_window`` field — the check is skipped and documented until the
    config grows the field. Unknown requirement keys raise ``ValueError``, so
    a cheaper/underused provider that fails the task's constraints is never
    substituted (and no eligible provider raises, fail-closed).

    Eligible providers then score with the documented placeholder weights
    (:data:`W_UTIL`, :data:`W_CACHE`, :data:`W_LATENCY`, :data:`W_SHADOW`):

    ``score = W_UTIL*utilization_norm + W_CACHE*(1 - cache_hit_rate)
    + W_LATENCY*latency_norm + W_SHADOW*shadow_price``

    where ``utilization_norm`` is tokens/window allowance (existing),
    ``cache_hit_rate`` is ``cache_hit_count/requests`` (0.0 when no requests),
    ``latency_norm`` is average call latency divided by
    :data:`REFERENCE_LATENCY_S` (0.0 when no data), and ``shadow_price`` is
    ``utilization_norm**2`` — tokens grow scarcer as a window fills. Lower
    score wins. H1 lane filtering applies when ``lanes`` is passed: a provider
    whose lane is at or above its effective in-flight cap is skipped. Returns
    the eligible ``(provider_name, model, score)`` triples sorted ascending by
    (score, config index), so the caller takes the head deterministically.
    """
    requirements = validate_requirements(requirements)
    candidates = tuple(candidates)
    if not candidates:
        raise ValueError("model_candidates must be a non-empty list of model ids")
    require_strong = requirements.get("quality") == "high"
    scored: list[tuple[float, int, str, str]] = []
    for index, provider in enumerate(providers):
        if not getattr(provider, "enabled", True):
            continue
        model = getattr(provider, "model", "")
        if not (isinstance(model, str) and model in candidates):
            continue
        if require_strong and getattr(provider, "tier", None) is not ProviderTier.STRONG:
            continue
        if lanes is not None:
            lane = lanes.get(provider.name)
            if lane is not None:
                current = debt.get(provider.name) if debt is not None else None
                retry_after_count = (
                    current.retry_after_count if current is not None else 0
                )
                if lane.in_flight >= lane.effective_in_flight_cap(retry_after_count):
                    continue
        current = debt.get(provider.name) if debt is not None else None
        utilization = _normalized_utilization(provider, debt)
        requests = current.requests if current is not None else 0
        cache_hit_rate = (current.cache_hit_count / requests) if requests else 0.0
        latency_norm = 0.0
        if current is not None and current.latency_count:
            latency_norm = (
                current.latency_total_s / current.latency_count
            ) / REFERENCE_LATENCY_S
        shadow_price = utilization ** 2
        score = (
            W_UTIL * utilization
            + W_CACHE * (1.0 - cache_hit_rate)
            + W_LATENCY * latency_norm
            + W_SHADOW * shadow_price
        )
        scored.append((score, index, provider.name, model))
    if not scored:
        raise ValueError(
            f"model_candidates {list(candidates)!r} match no enabled configured "
            "provider satisfying task requirements"
        )
    scored.sort(key=lambda item: (item[0], item[1]))
    return [(name, model, score) for score, _index, name, model in scored]



__all__ = [
    "DEFAULT_ROUTING_STATE_PATH",
    "DEFAULT_TOKEN_WINDOW_ALLOWANCE",
    "DebtStore",
    "LaneState",
    "ProviderDebt",
    "REFERENCE_LATENCY_S",
    "W_CACHE",
    "W_LATENCY",
    "W_SHADOW",
    "W_UTIL",
    "score_providers",
    "select_lane",
    "select_primary",
    "validate_requirements",
]
