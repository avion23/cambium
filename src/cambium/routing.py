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
file loads as empty only when absent. Unreadable, malformed, and unsupported
ledgers raise an error.

The ledger also carries an optional per-provider quarantine record: the
``failure_reason`` of the last ``config_error``/``auth_error`` call plus its
timestamp, cleared by a later success, so ``cambium doctor`` can surface a
durable disable reason.

The window allowance defaults to :data:`DEFAULT_TOKEN_WINDOW_ALLOWANCE`
(20M tokens, a placeholder until real quota contracts are measured,
implementation-plan step 3); a provider config may override it per provider
with the optional ``token_window_allowance`` field.

Provider lanes (H1) add concurrency-aware admission on top of the ledger:
each provider owns one :class:`LaneState` (in-flight tasks plus an
``rpm``-derived adaptive cap and the configured ``max_concurrency`` cap) and
:func:`select_lane` picks the provider with the lowest normalized utilization
among lanes with spare capacity, so a wave of concurrent admissions spreads
across providers instead of all picking the same max-min winner. 429 pressure
shrinks a lane's effective in-flight cap (placeholder adaptive rule, see
``LaneState.effective_in_flight_cap``), which is the admission-side
backpressure that prevents retry storms.

Capability/quality-constrained selection (H2): when a task declares
``requirements``, :func:`score_providers` filters candidates strictly by
capability (``quality == "high"`` keeps only ``ProviderTier.STRONG``
providers; ``min_context_window`` keeps only providers whose
``context_window`` capacity is declared and at least that large — a provider
without a declared capacity never satisfies it, so a task is never bound to a
provider that cannot fit its context; unknown requirement keys raise
``ValueError`` so a task never silently downgrades) and then ranks the
eligible providers with the pure lexicographic quality objective in
``cambium.selection``. Without ``requirements``,
``select_primary``/``select_lane`` keep their exact pre-H2 behavior and use
normalized utilization for admission balancing, not provider quality.
"""

from __future__ import annotations

import json
import math
import os
import tempfile
import time
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, replace
from pathlib import Path
from typing import Any, cast

try:
    fcntl: Any
    import fcntl
except ImportError:  # pragma: no cover - exercised on Windows
    fcntl = None

from .diffundo import ProviderTier
from .selection import DEFAULT_WEIGHTS, QualityWeights, order_candidates

# Placeholder weekly-equivalent token window per provider. No measured quota
# contract exists yet (implementation-plan step 3); a provider config may
# override this per provider with ``token_window_allowance``.
DEFAULT_TOKEN_WINDOW_ALLOWANCE = 20_000_000
DEFAULT_ROUTING_STATE_PATH = Path.home() / ".config" / "cambium" / "routing-state.json"
_ROUTING_STATE_VERSION = 1

_REQUIREMENT_KEYS = frozenset(
    {
        "quality",
        "min_context_window",
        "needs_native_tools",
        "needs_python_tool",
        "allow_paid",
        "allow_free",
    }
)


@dataclass(frozen=True, slots=True)
class RoutingRequest:
    """Immutable hard-admission facts for one provider request.

    This is deliberately only a request value object.  Mutable provider health,
    buckets, and task-to-provider binding remain owned by :mod:`diffundo`;
    keeping the request here lets both supervisor admission and live routing
    apply the same capability predicates without reinstating the old scheduler
    actor.
    """

    task_id: str
    model: str
    expected_input_tokens: int = 0
    expected_output_tokens: int = 0
    required_context_tokens: int = 0
    needs_native_tools: bool = True
    needs_python_tool: bool = False
    allow_model_substitution: bool = False
    allow_paid: bool = True
    allow_free: bool = True
    incumbent_provider: str | None = None
    lease: Any | None = None


@dataclass
class ProviderDebt:
    """Per-provider rolling usage state, folded from redacted usage events.

    ``tokens`` accumulates prompt+completion (or ``total_tokens`` when the
    provider reports it); ``retry_after_count`` counts 429-style events
    (``request_rate_status == "cooldown"`` or a ``failure_reason`` containing
    ``429``). Only counts/tokens — never credentials — ever enter the ledger.

    ``disable_reason``/``disable_at`` carry the durable quarantine record: a
    ``config_error:``/``auth_error:`` failure reason sets both, a success
    (no ``failure_reason``) clears both, and any other failure classification
    leaves them unchanged.
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
    # Durable quarantine record: reason + timestamp of the last
    # config_error/auth_error call, cleared by a later success.
    disable_reason: str | None = None
    disable_at: float | None = None

    def record(
        self, event: Mapping[str, Any], *, now: float | None = None
    ) -> None:
        """Fold one usage_event payload into this provider's debt."""
        timestamp = time.time() if now is None else now
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
            if failure_reason.startswith(
                "config_error:"
            ) or failure_reason.startswith("auth_error:"):
                # A quarantine-class failure: record the durable disable reason.
                self.disable_reason = failure_reason
                self.disable_at = timestamp
        else:
            # A success event clears any durable quarantine record; transient
            # failure classifications leave it untouched.
            self.disable_reason = None
            self.disable_at = None
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
        self.last_seen = timestamp


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
        try:
            parsed_last_seen = float(last_seen)
        except (OverflowError, ValueError):
            pass
        else:
            if math.isfinite(parsed_last_seen):
                debt.last_seen = parsed_last_seen
    disable_reason = entry.get("disable_reason")
    if isinstance(disable_reason, str) and disable_reason:
        debt.disable_reason = disable_reason
    disable_at = entry.get("disable_at")
    if (
        isinstance(disable_at, (int, float))
        and not isinstance(disable_at, bool)
        and disable_at >= 0
    ):
        try:
            parsed_disable_at = float(disable_at)
        except (OverflowError, ValueError):
            pass
        else:
            if math.isfinite(parsed_disable_at):
                debt.disable_at = parsed_disable_at
    return debt


def _copy_debts(debts: Mapping[str, ProviderDebt]) -> dict[str, ProviderDebt]:
    return {name: replace(debt) for name, debt in debts.items()}


class DebtStore:
    """Usage-debt ledger: durable file plus in-memory session accumulator.

    ``load`` replaces memory with the persisted ledger (a missing or corrupt
    file is an empty ledger); ``record`` folds live usage events into the
    in-memory accumulator; ``save`` atomically rewrites the ledger file
    (``mkstemp`` in the same directory + fsync + ``os.replace``). Saves take a
    per-ledger lock and merge session deltas with the current on-disk ledger so
    concurrent sessions do not overwrite one another's usage.
    """

    def __init__(self, path: str | Path | None = None) -> None:
        self._path = Path(path) if path is not None else DEFAULT_ROUTING_STATE_PATH
        self._debts: dict[str, ProviderDebt] = {}
        # ``_baseline_debts`` is the in-memory view loaded at session start;
        # ``_source_debts`` is the un-decayed on-disk snapshot used to tell an
        # unchanged ledger from one another session has already updated.
        self._baseline_debts: dict[str, ProviderDebt] = {}
        self._source_debts: dict[str, ProviderDebt] = {}
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

    def _read_persisted_debts(self) -> dict[str, ProviderDebt]:
        try:
            text = self._path.read_text(encoding="utf-8")
        except FileNotFoundError:
            return {}
        try:
            raw = json.loads(text)
        except ValueError as exc:
            raise ValueError(f"invalid routing ledger JSON: {self._path}") from exc
        if not (
            isinstance(raw, Mapping)
            and raw.get("version") == _ROUTING_STATE_VERSION
            and isinstance(raw.get("providers"), Mapping)
        ):
            raise ValueError(f"invalid routing ledger schema: {self._path}")
        return {
            name: _debt_from_mapping(name, entry)
            for name, entry in raw["providers"].items()
            if isinstance(name, str) and isinstance(entry, Mapping)
        }

    @staticmethod
    def _merge_provider_debt(
        base: ProviderDebt,
        local: ProviderDebt,
        baseline: ProviderDebt | None,
    ) -> ProviderDebt:
        """Add this session's cumulative-field delta to ``base``."""
        int_fields = (
            "tokens",
            "requests",
            "failed_requests",
            "retry_after_count",
            "cache_hit_count",
            "latency_count",
        )
        float_fields = ("cost", "latency_total_s")
        updates: dict[str, Any] = {}
        for field in (*int_fields, *float_fields):
            before = getattr(baseline, field, 0) if baseline is not None else 0
            updates[field] = getattr(base, field) + getattr(local, field) - before

        # Quarantine record: the most recent event wins. A config/auth error
        # this session sets it; a success this session clears it (a success
        # never increments ``failed_requests``, so ``failed < requests`` proves
        # one occurred); any other failure leaves the on-disk record intact.
        if local.disable_reason is not None:
            updates["disable_reason"] = local.disable_reason
            updates["disable_at"] = local.disable_at
        elif local.failed_requests < local.requests:
            updates["disable_reason"] = None
            updates["disable_at"] = None

        last_seen = base.last_seen
        if local.last_seen is not None and (
            last_seen is None or local.last_seen > last_seen
        ):
            last_seen = local.last_seen
        updates["last_seen"] = last_seen
        return replace(base, **updates)

    def _merge_with_current(
        self, current: Mapping[str, ProviderDebt]
    ) -> dict[str, ProviderDebt]:
        merged: dict[str, ProviderDebt] = {}
        for name in set(current) | set(self._debts):
            on_disk = current.get(name)
            local = self._debts.get(name)
            if local is None:
                if on_disk is not None:
                    merged[name] = replace(on_disk)
                continue

            baseline = self._baseline_debts.get(name)
            source = self._source_debts.get(name)
            if on_disk is None:
                base = baseline if baseline is not None else ProviderDebt()
            elif source is not None and on_disk == source:
                # No other session changed this provider. Preserve the local
                # load-time decay, then add this session's usage delta.
                base = baseline if baseline is not None else ProviderDebt()
            else:
                # Another session has already merged its usage. Add only this
                # session's delta to that newer snapshot.
                base = on_disk
            merged[name] = self._merge_provider_debt(base, local, baseline)
        return merged

    def load(self) -> None:
        """Replace memory with the persisted ledger.

        Applies exponential time-decay to recorded debt so cross-session
        accumulation cannot permanently skew max-min selection: each counter
        is scaled by ``0.5 ** (age_hours / 24)`` where age is measured from
        the entry's ``last_seen`` timestamp.
        """
        persisted = self._read_persisted_debts()
        debts: dict[str, ProviderDebt] = {}
        now = time.time()
        for name, debt in persisted.items():
            if debt.last_seen is None or not math.isfinite(debt.last_seen):
                # Missing or malformed timestamps cannot provide an age. Keep
                # the valid usage fields and skip decay for this entry.
                debts[name] = debt
                continue
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
                    cache_hit_count=round(debt.cache_hit_count * factor),
                    latency_total_s=debt.latency_total_s * factor,
                    latency_count=round(debt.latency_count * factor),
                    cost=debt.cost * factor,
                )
            debts[name] = debt
        self._debts = debts
        self._baseline_debts = _copy_debts(debts)
        self._source_debts = _copy_debts(persisted)
        self._dirty = False

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
        self._path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        lock_path = self._path.with_name(f".{self._path.name}.lock")
        with lock_path.open("a+", encoding="ascii", newline="\n") as lock:
            if fcntl is not None:
                fcntl.flock(lock.fileno(), fcntl.LOCK_EX)
            try:
                debts = self._merge_with_current(self._read_persisted_debts())
                providers: dict[str, dict[str, Any]] = {}
                for name, debt in sorted(debts.items()):
                    entry: dict[str, Any] = {
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
                    if debt.disable_reason is not None:
                        entry["disable_reason"] = debt.disable_reason
                        if debt.disable_at is not None:
                            entry["disable_at"] = debt.disable_at
                    providers[name] = entry
                payload = {
                    "version": _ROUTING_STATE_VERSION,
                    "providers": providers,
                }
                content = json.dumps(payload, indent=2, sort_keys=True) + "\n"
                descriptor, temporary_name = tempfile.mkstemp(
                    prefix=f".{self._path.name}.", suffix=".tmp", dir=self._path.parent
                )
                temporary_path: Path | None = Path(temporary_name)
                try:
                    with os.fdopen(
                        descriptor, "w", encoding="utf-8", newline="\n"
                    ) as handle:
                        handle.write(content)
                        handle.flush()
                        os.fsync(handle.fileno())
                    os.replace(cast(Path, temporary_path), self._path)
                    temporary_path = None
                finally:
                    if temporary_path is not None:
                        try:
                            temporary_path.unlink()
                        except FileNotFoundError:
                            pass
            finally:
                if fcntl is not None:
                    fcntl.flock(lock.fileno(), fcntl.LOCK_UN)
        self._debts = _copy_debts(debts)
        self._baseline_debts = _copy_debts(debts)
        self._source_debts = _copy_debts(debts)
        self._dirty = False


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


@dataclass(frozen=True)
class ProviderAssignment:
    """The resolved (provider, model, tier) for one task (AUDIT-063).

    A pure value: ``resolve_assignment`` computes it from the candidate set
    and ledger without reading state or mutating the task; the supervisor
    applies it to the task at the runtime edge only.
    """

    provider: str
    model: str
    tier: str


class LaneCapacityExhausted(ValueError):
    """No eligible provider currently has a spare admission lane."""


def resolve_assignment(
    providers: Sequence[Any],
    candidates: Sequence[str],
    debt: Mapping[str, ProviderDebt] | None,
    lanes: Mapping[str, LaneState] | None,
    *,
    requirements: Mapping[str, Any] | None = None,
    authorized: frozenset[str] | None = None,
    pinned_tier: str | None = None,
) -> ProviderAssignment | None:
    """Pure (provider, model, tier) selection for one un-pinned task.

    Filters the loaded ``providers`` to the authorized identity set (carried
    by name so OAuth providers are never dropped), the candidate models, an
    optional pinned tier, and lane capacity, then picks via
    :func:`select_lane` (max-min admission) or :func:`score_providers` (when
    the task declares requirements). Returns ``None`` when no provider
    remains; the caller decides whether that is a hard failure. Raises
    ``ValueError`` when the filtered pool is empty after authorization but a
    selection was required.
    """
    pool = list(providers)
    if authorized is not None:
        pool = [p for p in pool if p.name in authorized]
    if pinned_tier:
        pool = [p for p in pool if p.tier.value == pinned_tier]
    if requirements:
        provider_name, model, _score = score_providers(
            pool, candidates, debt, lanes, requirements=requirements
        )[0]
    else:
        provider_name, model = select_lane(pool, candidates, debt, lanes or {})
    tier = _assignment_tier(pool, provider_name, pinned_tier)
    return ProviderAssignment(provider=provider_name, model=model, tier=tier)


def _assignment_tier(
    providers: Sequence[Any], provider_name: str, pinned_tier: str | None
) -> str:
    """The call tier for an assigned provider (mirrors worker routing)."""
    if isinstance(pinned_tier, str) and pinned_tier:
        return pinned_tier
    for provider in providers:
        if provider.name == provider_name:
            return provider.tier.value
    raise ValueError(f"assigned provider {provider_name!r} not found among candidates")


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
    admission-time assignment). ``rpm_allowance`` is retained as the
    adaptive/backpressure allowance, while ``max_concurrency`` is the
    configured hard cap on simultaneous reservations. ``None`` preserves the
    legacy direct-construction behavior for callers that do not carry provider
    configuration into a lane; loaded provider lanes should always set it.
    """

    in_flight: int = 0
    rpm_allowance: float = 60.0
    max_concurrency: int | None = None

    def effective_in_flight_cap(self, retry_after_count: int) -> int:
        """Return the lower of the adaptive RPM cap and configured concurrency.

        Placeholder adaptive rule: each 429-style usage event shrinks the RPM
        allowance by ``1/50`` of the allowance, floor 50%, until live 429
        events stop and the debt stops accumulating. The cap never drops
        below 1 so a pressured provider keeps serving at least one task. A
        configured ``max_concurrency`` is an independent hard upper bound and
        is applied after the adaptive calculation.
        """
        decay = max(0.5, 1.0 - retry_after_count / 50.0)
        cap = max(1, int(self.rpm_allowance * decay))
        configured = self.max_concurrency
        if configured is not None:
            if isinstance(configured, bool) or not isinstance(configured, int):
                # A malformed lane state must fail closed rather than turn
                # into an unlimited admission lane.
                return 1
            cap = min(cap, max(1, configured))
        return cap


def select_lane(
    providers: Sequence[Any],
    candidates: Sequence[str],
    debt: Mapping[str, ProviderDebt] | None,
    lanes: Mapping[str, LaneState],
) -> tuple[str, str]:
    """Max-min admission pick over per-provider lanes (H1).

    Like :func:`select_primary`, but a provider whose lane is at or above its
    effective in-flight cap (the lower of configured ``max_concurrency`` and
    ``rpm_allowance`` decayed by that provider's ``retry_after_count`` in
    ``debt``) is skipped entirely, so concurrent
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
    matching = False
    for index, provider in enumerate(providers):
        if not getattr(provider, "enabled", True):
            continue
        model = getattr(provider, "model", "")
        if not (isinstance(model, str) and model in candidates):
            continue
        matching = True
        lane = lanes.get(provider.name)
        if lanes and lane is None:
            # An empty lane map is the legacy "capacity not tracked" value;
            # once a map has entries, a missing provider is an unknown lane and
            # must not be treated as unlimited capacity.
            continue
        if lane is not None:
            current = debt.get(provider.name) if debt is not None else None
            retry_after_count = current.retry_after_count if current is not None else 0
            if lane.in_flight >= lane.effective_in_flight_cap(retry_after_count):
                continue
        serving.append((index, provider))
    if not matching:
        raise ValueError(
            f"model_candidates {list(candidates)!r} match no enabled configured provider"
        )
    if not serving:
        raise LaneCapacityExhausted(
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


def context_window_satisfies(provider: Any, required_context_tokens: int) -> bool:
    """Return whether ``provider`` declares enough context capacity.

    Context is a hard capability boundary.  A missing/zero capacity is not
    treated as unlimited when a request declares a requirement; this is the
    same predicate used by H2 scoring and by Diffundo's live admission path.
    """
    if required_context_tokens <= 0:
        return True
    capacity = getattr(provider, "context_window", 0) or 0
    return (
        not isinstance(capacity, bool)
        and isinstance(capacity, (int, float))
        and capacity >= required_context_tokens
    )


def provider_satisfies_request(provider: Any, request: RoutingRequest) -> bool:
    """Apply the request's static hard constraints to one provider.

    Health, token buckets, and tier membership are intentionally outside this
    pure predicate.  They are live state and remain in Diffundo's candidate
    loop.  A lease is never permission to bypass context, tools, or billing.
    """
    if not getattr(provider, "enabled", True):
        return False
    lease = request.lease
    if lease is not None:
        if (
            getattr(provider, "name", None) != getattr(lease, "provider", None)
            or getattr(provider, "model", None) != getattr(lease, "model", None)
        ):
            return False
    elif (
        request.model
        and not request.allow_model_substitution
        and getattr(provider, "model", None) != request.model
    ):
        return False
    if not context_window_satisfies(provider, request.required_context_tokens):
        return False
    if request.needs_native_tools and not getattr(provider, "supports_native_tools", False):
        return False
    if request.needs_python_tool and not getattr(provider, "supports_python_tool", False):
        return False
    billing = getattr(provider, "billing_mode", "metered")
    billing_value = getattr(billing, "value", billing)
    if billing_value in {"metered", "subscription"}:
        if not request.allow_paid:
            return False
    elif not request.allow_free:
        return False
    return True


def validate_requirements(requirements: Mapping[str, Any] | None) -> dict[str, Any]:
    """Validate a task's capability/quality requirements, fail-closed.

    ``quality`` must be ``"high"`` or ``"normal"``; ``min_context_window``
    must be a positive int.  Tool and billing flags, when declared, must be
    booleans. Unknown keys raise ``ValueError`` — a task that declares a
    requirement the selector does not understand fails closed instead of
    silently downgrading the task. Returns a plain dict copy (``{}`` for
    ``None``/absent).
    """
    if requirements is None:
        return {}
    if not isinstance(requirements, Mapping):
        raise ValueError("requirements must be a mapping")
    unknown = [key for key in requirements if key not in _REQUIREMENT_KEYS]
    if unknown:
        unknown.sort(key=repr)
        raise ValueError(
            "unknown requirement key(s): " + ", ".join(map(repr, unknown))
        )
    if "quality" in requirements:
        quality = requirements["quality"]
        if not isinstance(quality, str) or quality not in ("high", "normal"):
            raise ValueError("requirements.quality must be 'high' or 'normal'")
    if "min_context_window" in requirements:
        min_context_window = requirements["min_context_window"]
        if (
            isinstance(min_context_window, bool)
            or not isinstance(min_context_window, int)
            or min_context_window <= 0
        ):
            raise ValueError("requirements.min_context_window must be a positive int")
    for key in (
        "needs_native_tools",
        "needs_python_tool",
        "allow_paid",
        "allow_free",
    ):
        if key in requirements and type(requirements[key]) is not bool:
            raise ValueError(f"requirements.{key} must be a boolean")
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
    restriction. ``min_context_window`` restricts candidates to providers
    whose ``context_window`` capacity is declared (a positive
    :attr:`~cambium.diffundo.ProviderConfig.context_window`) and at least as
    large as the requirement — a provider that declares no capacity can never
    satisfy the boundary, so the task is never bound to a provider that
    cannot fit its context. Declared tool and billing flags are hard filters as
    well. Unknown requirement keys raise ``ValueError``, so a cheaper/underused
    provider that fails the task's constraints is never substituted (and no
    eligible provider raises, fail-closed).

    Eligible providers use :func:`cambium.selection.order_candidates`, whose
    lexicographic quality key is success confidence, latency-SLO compliance,
    expected cost per successful turn, then a normalized latency/cache
    tie-break. Missing evidence preserves config position. H1 lane filtering
    applies when ``lanes`` is passed. Returns eligible
    ``(provider_name, model, score)`` triples, where the float score is the
    zero-based rank retained for API compatibility.
    """
    requirements = validate_requirements(requirements)
    candidates = tuple(candidates)
    if not candidates:
        raise ValueError("model_candidates must be a non-empty list of model ids")
    require_strong = requirements.get("quality") == "high"
    min_context_window = requirements.get("min_context_window")
    needs_native_tools = requirements.get("needs_native_tools", False)
    needs_python_tool = requirements.get("needs_python_tool", False)
    allow_paid = requirements.get("allow_paid", True)
    allow_free = requirements.get("allow_free", True)
    eligible: list[Any] = []
    capability_matches = 0
    models: dict[str, str] = {}
    for provider in providers:
        if not getattr(provider, "enabled", True):
            continue
        model = getattr(provider, "model", "")
        if not (isinstance(model, str) and model in candidates):
            continue
        if require_strong and getattr(provider, "tier", None) is not ProviderTier.STRONG:
            continue
        if min_context_window is not None and not context_window_satisfies(
            provider, min_context_window
        ):
            continue
        if needs_native_tools and not getattr(provider, "supports_native_tools", False):
            continue
        if needs_python_tool and not getattr(provider, "supports_python_tool", False):
            continue
        billing = getattr(provider, "billing_mode", "metered")
        billing_value = getattr(billing, "value", billing)
        if billing_value in {"metered", "subscription"}:
            if not allow_paid:
                continue
        elif not allow_free:
            continue
        capability_matches += 1
        if lanes is not None:
            lane = lanes.get(provider.name)
            if lanes and lane is None:
                # As in select_lane, only an entirely empty map means that
                # legacy callers did not request lane tracking. A partial map
                # is an incomplete admission view and fails closed.
                continue
            if lane is not None:
                current = debt.get(provider.name) if debt is not None else None
                retry_after_count = (
                    current.retry_after_count if current is not None else 0
                )
                if lane.in_flight >= lane.effective_in_flight_cap(retry_after_count):
                    continue
        eligible.append(provider)
        models[provider.name] = model
    if not eligible:
        if capability_matches:
            raise LaneCapacityExhausted(
                f"model_candidates {list(candidates)!r} match no enabled configured "
                "provider with a spare lane"
            )
        raise ValueError(
            f"model_candidates {list(candidates)!r} match no enabled configured "
            "provider satisfying task requirements"
        )
    ordered = order_candidates(
        eligible,
        debt=debt,
        incumbent=None,
        rotation_offset=0,
        now=time.time(),
        weights=DEFAULT_WEIGHTS,
    )
    return [
        (provider.name, models[provider.name], float(rank))
        for rank, provider in enumerate(ordered)
    ]



__all__ = [
    "DEFAULT_ROUTING_STATE_PATH",
    "DEFAULT_TOKEN_WINDOW_ALLOWANCE",
    "DebtStore",
    "LaneCapacityExhausted",
    "LaneState",
    "ProviderAssignment",
    "ProviderDebt",
    "QualityWeights",
    "score_providers",
    "select_lane",
    "select_primary",
    "resolve_assignment",
    "validate_requirements",
]
