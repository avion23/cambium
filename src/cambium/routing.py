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
each provider owns one :class:`LaneState` with independent request-rate tokens
and in-flight capacity, and :func:`select_lane` picks the provider with the
lowest normalized utilization among lanes with both slots available, so a wave
of concurrent admissions spreads across providers instead of all picking the
same max-min winner. 429 pressure reduces request-token availability for
new-style lanes (legacy direct lanes retain their rpm-derived compatibility
cap), which is the admission-side backpressure that prevents retry storms.

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
from dataclasses import dataclass, field, replace
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

    model: str
    required_context_tokens: int = 0
    # Kept for task/config compatibility. Native tools are an optional
    # per-provider transport mode, not an admission requirement.
    needs_native_tools: bool = False
    needs_python_tool: bool = False
    allow_model_substitution: bool = False
    allow_paid: bool = True
    allow_free: bool = True
    quality: str | None = None
    lease: Any | None = None


@dataclass
class ProviderDebt:
    """Per-provider rolling usage state, folded from redacted usage events.

    ``tokens`` accumulates prompt+completion (or ``total_tokens`` when the
    provider reports it); ``tokens_per_s`` records output-generation evidence
    from completion/output tokens divided by call latency; and
    ``retry_after_count`` counts 429-style events (``request_rate_status ==
    "cooldown"`` or a ``failure_reason`` containing ``429``). Only
    counts/tokens/evidence — never credentials — ever enter the ledger.

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
    # Generation throughput evidence.  ``tokens_per_s`` is the public
    # measured value used by quality ordering; the total/count pair lets the
    # ledger merge concurrent sessions without treating a running average as
    # a cumulative counter.
    tokens_per_s: float = 0.0
    tokens_per_s_total: float = 0.0
    tokens_per_s_count: int = 0
    last_seen: float | None = None
    # Durable quarantine record: reason + timestamp of the last
    # config_error/auth_error call, cleared by a later success.
    disable_reason: str | None = None
    disable_at: float | None = None

    def record(self, event: Mapping[str, Any], *, now: float | None = None) -> None:
        """Fold one usage_event payload into this provider's debt."""
        timestamp = time.time() if now is None else now
        self.requests += 1
        if self.tokens_per_s_count <= 0 and self.tokens_per_s > 0:
            # A hand-built or older ledger entry may carry only the public
            # average. Treat it as one prior sample before adding new usage so
            # the first live event does not erase the existing evidence.
            self.tokens_per_s_total = self.tokens_per_s
            self.tokens_per_s_count = 1
        usage = event.get("usage")
        output_tokens: float | None = None
        if isinstance(usage, Mapping):
            total = usage.get("total_tokens")
            valid_total = False
            if isinstance(total, int | float) and not isinstance(total, bool):
                try:
                    parsed_total = float(total)
                except (OverflowError, ValueError):
                    parsed_total = -1.0
                if math.isfinite(parsed_total) and parsed_total >= 0:
                    self.tokens += int(total)
                    valid_total = True
            if not valid_total:
                inputs = usage.get("input_tokens", usage.get("prompt_tokens"))
                outputs = usage.get("output_tokens", usage.get("completion_tokens"))
                if (
                    isinstance(inputs, int | float)
                    and not isinstance(inputs, bool)
                    and isinstance(outputs, int | float)
                    and not isinstance(outputs, bool)
                ):
                    self.tokens += int(inputs) + int(outputs)
            # Generation throughput deliberately uses output-side tokens so
            # prompt/cache tokens do not make a provider look faster.  Older
            # events may only carry total_tokens, matching the existing
            # render_tokens_per_s fallback.
            for key in ("completion_tokens", "output_tokens", "total_tokens"):
                value = usage.get(key)
                if isinstance(value, int | float) and not isinstance(value, bool):
                    try:
                        parsed = float(value)
                    except (OverflowError, ValueError):
                        continue
                    if math.isfinite(parsed) and parsed >= 0:
                        output_tokens = parsed
                        break
        cost = event.get("estimated_cost_usd")
        if isinstance(cost, int | float) and not isinstance(cost, bool):
            self.cost += float(cost)
        failure_reason = event.get("failure_reason")
        if isinstance(failure_reason, str) and failure_reason:
            self.failed_requests += 1
            if failure_reason.startswith("config_error:") or failure_reason.startswith(
                "auth_error:"
            ):
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
        if isinstance(latency, int | float) and not isinstance(latency, bool):
            try:
                parsed_latency = float(latency)
            except (OverflowError, ValueError):
                parsed_latency = -1.0
        else:
            parsed_latency = -1.0
        if math.isfinite(parsed_latency) and parsed_latency >= 0:
            self.latency_total_s += parsed_latency
            self.latency_count += 1
            if output_tokens is not None and parsed_latency > 0:
                rate = output_tokens / parsed_latency
                if math.isfinite(rate):
                    self.tokens_per_s_total += rate
                    self.tokens_per_s_count += 1
                    self.tokens_per_s = self.tokens_per_s_total / self.tokens_per_s_count
        self.last_seen = timestamp


def _debt_from_mapping(entry: Mapping[str, Any]) -> ProviderDebt:
    """Parse one ledger entry, ignoring malformed fields (tolerate corruption)."""
    debt = ProviderDebt()
    for field_name, converter in (
        ("tokens", int),
        ("requests", int),
        ("failed_requests", int),
        ("retry_after_count", int),
        ("cache_hit_count", int),
        ("latency_count", int),
        ("tokens_per_s_count", int),
    ):
        value = entry.get(field_name)
        if isinstance(value, int | float) and not isinstance(value, bool) and value >= 0:
            setattr(debt, field_name, converter(value))
    cost = entry.get("cost")
    if isinstance(cost, int | float) and not isinstance(cost, bool) and cost >= 0:
        debt.cost = float(cost)
    latency_total_s = entry.get("latency_total_s")
    if (
        isinstance(latency_total_s, int | float)
        and not isinstance(latency_total_s, bool)
        and latency_total_s >= 0
    ):
        debt.latency_total_s = float(latency_total_s)
    for field_name in ("tokens_per_s", "tokens_per_s_total"):
        value = entry.get(field_name)
        if isinstance(value, int | float) and not isinstance(value, bool) and value >= 0:
            try:
                parsed = float(value)
            except (OverflowError, ValueError):
                continue
            if math.isfinite(parsed):
                setattr(debt, field_name, parsed)
    if debt.tokens_per_s_count > 0 and debt.tokens_per_s_total >= 0:
        debt.tokens_per_s = debt.tokens_per_s_total / debt.tokens_per_s_count
    elif debt.tokens_per_s > 0:
        debt.tokens_per_s_total = debt.tokens_per_s
        debt.tokens_per_s_count = 1
    last_seen = entry.get("last_seen")
    if isinstance(last_seen, int | float) and not isinstance(last_seen, bool):
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
    if isinstance(disable_at, int | float) and not isinstance(disable_at, bool) and disable_at >= 0:
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
        if path is not None:
            self._path = Path(path)
        else:
            # CAMBIUM_ROUTING_STATE lets a parent process (e.g. the test
            # conftest) redirect every descendant supervisor's ledger without
            # touching each call site.
            configured = os.environ.get("CAMBIUM_ROUTING_STATE", "")
            self._path = Path(configured) if configured else DEFAULT_ROUTING_STATE_PATH
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
            name: _debt_from_mapping(entry)
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
            "tokens_per_s_count",
        )
        float_fields = ("cost", "latency_total_s", "tokens_per_s_total")
        updates: dict[str, Any] = {}
        for field_name in (*int_fields, *float_fields):
            before = getattr(baseline, field_name, 0) if baseline is not None else 0
            updates[field_name] = getattr(base, field_name) + getattr(local, field_name) - before

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
        if local.last_seen is not None and (last_seen is None or local.last_seen > last_seen):
            last_seen = local.last_seen
        updates["last_seen"] = last_seen
        merged = replace(base, **updates)
        if merged.tokens_per_s_count > 0 and merged.tokens_per_s_total >= 0:
            merged.tokens_per_s = merged.tokens_per_s_total / merged.tokens_per_s_count
        return merged

    def _merge_with_current(self, current: Mapping[str, ProviderDebt]) -> dict[str, ProviderDebt]:
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
                    tokens_per_s_total=debt.tokens_per_s_total * factor,
                    tokens_per_s_count=round(debt.tokens_per_s_count * factor),
                    cost=debt.cost * factor,
                )
                if debt.tokens_per_s_count > 0 and debt.tokens_per_s_total >= 0:
                    debt.tokens_per_s = debt.tokens_per_s_total / debt.tokens_per_s_count
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
                        "tokens_per_s": debt.tokens_per_s,
                        "tokens_per_s_total": debt.tokens_per_s_total,
                        "tokens_per_s_count": debt.tokens_per_s_count,
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
                    with os.fdopen(descriptor, "w", encoding="utf-8", newline="\n") as handle:
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
    if isinstance(allowance, bool) or not isinstance(allowance, int | float):
        return float(DEFAULT_TOKEN_WINDOW_ALLOWANCE)
    if allowance <= 0:
        return float(DEFAULT_TOKEN_WINDOW_ALLOWANCE)
    return float(allowance)


def _normalized_utilization(provider: Any, debt: Mapping[str, ProviderDebt] | None) -> float:
    current = debt.get(provider.name) if debt is not None else None
    tokens = current.tokens if current is not None else 0
    return tokens / _window_allowance(provider)


def _provider_is_quarantined(provider_name: str, debt: Mapping[str, Any] | None) -> bool:
    """Return whether routing debt carries a proven auth/config quarantine."""
    if debt is None:
        return False
    entry = debt.get(provider_name)
    reason = (
        entry.get("disable_reason")
        if isinstance(entry, Mapping)
        else getattr(entry, "disable_reason", None)
    )
    return isinstance(reason, str) and reason.startswith(("auth_error:", "config_error:"))


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


def _assignment_tier(providers: Sequence[Any], provider_name: str, pinned_tier: str | None) -> str:
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
        if (
            isinstance(model, str)
            and model in candidates
            and not _provider_is_quarantined(provider.name, debt)
        ):
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
    """Per-provider admission state with separate rate and concurrency.

    ``requests_per_minute`` is a replenishing request-token bucket;
    ``max_in_flight`` is a hard simultaneous-request cap.  They are separate
    dimensions: a provider can be request-rate limited while still supporting
    many concurrent requests, or have a generous request rate while allowing
    only one slow request at a time.

    ``rpm_allowance``/``max_concurrency`` retain the direct-construction API
    used by older callers.  A legacy lane keeps its historical rpm-derived
    cap; provider-configured new-style lanes use only ``max_in_flight`` for
    their in-flight cap and consume request tokens independently.  The
    supervisor's existing ``lane.in_flight += 1`` reservation also consumes a
    request token for a new-style lane, so the admission flow needs no second
    scheduler or alternate counter owner.
    """

    in_flight: int = 0
    rpm_allowance: float = 60.0
    max_concurrency: int | None = None
    requests_per_minute: float | None = None
    max_in_flight: int | None = None
    request_slots: float | None = None
    _request_slots_updated_at: float | None = field(default=None, repr=False, compare=False)
    _tracks_request_slots: bool = field(default=False, init=False, repr=False, compare=False)

    def __post_init__(self) -> None:
        if isinstance(self.in_flight, bool) or not isinstance(self.in_flight, int):
            raise ValueError("in_flight must be an integer")
        if self.in_flight < 0:
            raise ValueError("in_flight must not be negative")
        canonical = self.requests_per_minute
        if canonical is not None:
            if isinstance(canonical, bool) or not isinstance(canonical, int | float):
                raise ValueError("requests_per_minute must be a positive number")
            canonical = float(canonical)
            if not math.isfinite(canonical) or canonical <= 0:
                raise ValueError("requests_per_minute must be a positive number")
            object.__setattr__(self, "requests_per_minute", canonical)
        if self.max_in_flight is not None:
            if isinstance(self.max_in_flight, bool) or not isinstance(self.max_in_flight, int):
                raise ValueError("max_in_flight must be a positive integer")
            if self.max_in_flight <= 0:
                raise ValueError("max_in_flight must be a positive integer")
        if canonical is not None or self.request_slots is not None:
            if self.request_slots is None:
                slots = canonical if canonical is not None else max(self.rpm_allowance, 1.0)
                object.__setattr__(self, "request_slots", float(slots))
            elif (
                isinstance(self.request_slots, bool)
                or not isinstance(self.request_slots, int | float)
                or not math.isfinite(float(self.request_slots))
                or self.request_slots < 0
            ):
                raise ValueError("request_slots must be a non-negative number")
            object.__setattr__(
                self,
                "_request_slots_updated_at",
                time.monotonic(),
            )
            object.__setattr__(self, "_tracks_request_slots", True)
            if self.in_flight:
                self._consume_request_slots(self.in_flight)

    @classmethod
    def from_provider(cls, provider: Any, *, in_flight: int = 0) -> LaneState:
        """Build an independently modeled lane from a provider config.

        ``ProviderConfig`` normalizes legacy rpm-only providers to a
        conservative one in-flight slot.  The factory intentionally uses that
        effective value, while retaining the configured request rate as a
        separate token bucket dimension.
        """
        rate = getattr(provider, "requests_per_minute", getattr(provider, "rpm", 60))
        max_in_flight = getattr(provider, "max_in_flight", None)
        if max_in_flight is None:
            max_in_flight = 1
        return cls(
            in_flight=in_flight,
            requests_per_minute=rate,
            max_in_flight=max_in_flight,
        )

    def configure_from_provider(self, provider: Any) -> None:
        """Apply a provider's explicit dimensions to an existing lane.

        This is used when a legacy supervisor-created lane is first observed
        by routing.  rpm-only providers intentionally remain on the old lane
        path; a provider declaring either new capacity field opts in through
        ``ProviderConfig._independent_capacity_model``.
        """
        marker = getattr(provider, "_independent_capacity_model", None)
        if marker is False:
            return
        if marker is None and not (
            getattr(provider, "requests_per_minute", None) is not None
            or getattr(provider, "max_in_flight", None) is not None
        ):
            return
        rate = getattr(provider, "requests_per_minute", getattr(provider, "rpm", 60))
        max_in_flight = getattr(provider, "max_in_flight", None) or 1
        if self._tracks_request_slots:
            return
        object.__setattr__(self, "requests_per_minute", float(rate))
        object.__setattr__(self, "max_in_flight", int(max_in_flight))
        object.__setattr__(self, "request_slots", float(rate))
        object.__setattr__(self, "_request_slots_updated_at", time.monotonic())
        object.__setattr__(self, "_tracks_request_slots", True)
        if self.in_flight:
            self._consume_request_slots(self.in_flight)

    def _consume_request_slots(self, count: int = 1) -> None:
        if self.request_slots is None or count <= 0:
            return
        self._refill_request_slots()
        object.__setattr__(self, "request_slots", max(0.0, self.request_slots - count))

    def _refill_request_slots(self, now: float | None = None) -> None:
        if self.request_slots is None or self.requests_per_minute is None:
            return
        timestamp = time.monotonic() if now is None else float(now)
        started = self._request_slots_updated_at
        if started is None:
            object.__setattr__(self, "_request_slots_updated_at", timestamp)
            return
        elapsed = max(0.0, timestamp - started)
        if elapsed <= 0:
            return
        replenished = self.request_slots + elapsed * self.requests_per_minute / 60.0
        object.__setattr__(
            self,
            "request_slots",
            min(self.requests_per_minute, replenished),
        )
        object.__setattr__(self, "_request_slots_updated_at", timestamp)

    def __setattr__(self, name: str, value: Any) -> None:
        """Treat external in-flight reservations as request-slot spends."""
        if name == "in_flight" and getattr(self, "_tracks_request_slots", False):
            previous = getattr(self, "in_flight", 0)
            object.__setattr__(self, name, value)
            if isinstance(value, int) and value > previous:
                self._consume_request_slots(value - previous)
            return
        object.__setattr__(self, name, value)

    def effective_request_rate(self, retry_after_count: int = 0) -> float:
        """Return the adaptive request-rate allowance, independent of in-flight."""
        base = self.requests_per_minute
        if base is None:
            base = self.rpm_allowance
        decay = max(0.5, 1.0 - retry_after_count / 50.0)
        return max(1.0, float(base) * decay)

    def effective_request_slot_cap(self, retry_after_count: int = 0) -> int:
        """Return the integer request-token capacity after 429 pressure."""
        return max(1, int(self.effective_request_rate(retry_after_count)))

    def has_request_slot(self, retry_after_count: int = 0, *, now: float | None = None) -> bool:
        """Whether one request token is available without consuming it."""
        if self.request_slots is None:
            return True
        self._refill_request_slots(now)
        cap = self.effective_request_slot_cap(retry_after_count)
        if self.request_slots > cap:
            object.__setattr__(self, "request_slots", float(cap))
        return self.request_slots >= 1.0

    def effective_in_flight_cap(self, retry_after_count: int) -> int:
        """Return only concurrent capacity for independently modeled lanes.

        Legacy direct lanes retain the old rpm-derived cap for compatibility.
        New-style lanes apply 429 pressure to request tokens instead; a high
        concurrency provider therefore does not lose its in-flight capacity
        merely because its request rate is low.
        """
        if self.requests_per_minute is not None or self.max_in_flight is not None:
            configured = self.max_in_flight
            if configured is None:
                configured = self.max_concurrency
            if isinstance(configured, bool) or not isinstance(configured, int):
                return 1
            return max(1, configured)
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

    def can_admit(self, retry_after_count: int = 0, *, now: float | None = None) -> bool:
        """Return whether admission has both a request and in-flight slot."""
        return self.in_flight < self.effective_in_flight_cap(
            retry_after_count
        ) and self.has_request_slot(
            retry_after_count,
            now=now,
        )

    def reserve(self, retry_after_count: int = 0, *, now: float | None = None) -> bool:
        """Atomically consume one request slot and one in-flight slot."""
        if not self.can_admit(retry_after_count, now=now):
            return False
        self._consume_request_slots()
        object.__setattr__(self, "in_flight", self.in_flight + 1)
        return True

    def release(self) -> None:
        """Release one in-flight slot; request tokens refill by elapsed time."""
        if self.in_flight > 0:
            object.__setattr__(self, "in_flight", self.in_flight - 1)


def _lane_has_capacity(provider: Any, lane: LaneState, retry_after_count: int) -> bool:
    """Apply provider dimensions before checking both lane slot types."""
    lane.configure_from_provider(provider)
    return lane.can_admit(retry_after_count)


def select_lane(
    providers: Sequence[Any],
    candidates: Sequence[str],
    debt: Mapping[str, ProviderDebt] | None,
    lanes: Mapping[str, LaneState],
) -> tuple[str, str]:
    """Max-min admission pick over per-provider lanes (H1).

    Like :func:`select_primary`, but a provider is skipped unless its lane has
    both a request token and an in-flight slot. New-style lanes use
    ``requests_per_minute`` only for the request-token bucket and
    ``max_in_flight`` only for concurrent capacity. Legacy lanes retain the
    old rpm-derived cap. Remaining providers rank by (normalized utilization,
    lane in_flight, requests, config index): max-min utilization semantics
    with idle lanes winning ties. Raises ``ValueError`` when no enabled
    provider with a spare lane serves a candidate model.
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
        if _provider_is_quarantined(provider.name, debt):
            continue
        lane = lanes.get(provider.name)
        if lanes and lane is None:
            # An empty lane map is the legacy "capacity not tracked" value;
            # once a map has entries, a missing provider is an unknown lane and
            # must not be treated as unlimited capacity.
            continue
        if lane is not None:
            current = debt.get(provider.name) if debt is not None else None
            retry_after_count = current.retry_after_count if current is not None else 0
            if not _lane_has_capacity(provider, lane, retry_after_count):
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
        and isinstance(capacity, int | float)
        and capacity >= required_context_tokens
    )


def provider_satisfies_request(provider: Any, request: RoutingRequest) -> bool:
    """Apply the request's static hard constraints to one provider.

    Health, token buckets, and tier membership are intentionally outside this
    pure predicate.  They are live state and remain in Diffundo's candidate
    loop.  A lease is never permission to bypass context, Python tools, or
    billing; native tools are an optional per-provider wire mode.
    """
    if not getattr(provider, "enabled", True):
        return False
    lease = request.lease
    if lease is not None:
        if getattr(provider, "name", None) != getattr(lease, "provider", None) or getattr(
            provider, "model", None
        ) != getattr(lease, "model", None):
            return False
    elif (
        request.model
        and not request.allow_model_substitution
        and getattr(provider, "model", None) != request.model
    ):
        return False
    if request.quality == "high" and getattr(provider, "tier", None) is not ProviderTier.STRONG:
        return False
    if not context_window_satisfies(provider, request.required_context_tokens):
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
    must be a positive int.  Python-tool and billing flags, when declared, must
    be booleans. ``needs_native_tools`` remains accepted for compatibility but
    does not make native wire support a hard requirement. Unknown keys raise
    ``ValueError`` — a task that declares a requirement the selector does not
    understand fails closed instead of silently downgrading the task. Returns
    a plain dict copy (``{}`` for ``None``/absent).
    """
    if requirements is None:
        return {}
    if not isinstance(requirements, Mapping):
        raise ValueError("requirements must be a mapping")
    unknown = [key for key in requirements if key not in _REQUIREMENT_KEYS]
    if unknown:
        unknown.sort(key=repr)
        raise ValueError("unknown requirement key(s): " + ", ".join(map(repr, unknown)))
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
    cannot fit its context. Python-tool and billing flags are hard filters;
    native tools remain an optional per-provider wire mode. Unknown requirement
    keys raise ``ValueError``, so a cheaper/underused provider that fails the
    task's constraints is never substituted (and no eligible provider raises,
    fail-closed).

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
    min_context_window = requirements.get("min_context_window")
    needs_native_tools = requirements.get("needs_native_tools", False)
    needs_python_tool = requirements.get("needs_python_tool", False)
    allow_paid = requirements.get("allow_paid", True)
    allow_free = requirements.get("allow_free", True)
    capability_request = RoutingRequest(
        model="",
        required_context_tokens=min_context_window or 0,
        needs_native_tools=needs_native_tools,
        needs_python_tool=needs_python_tool,
        allow_model_substitution=True,
        allow_paid=allow_paid,
        allow_free=allow_free,
        quality=requirements.get("quality"),
    )
    eligible: list[Any] = []
    capability_matches = 0
    models: dict[str, str] = {}
    for provider in providers:
        if not getattr(provider, "enabled", True):
            continue
        model = getattr(provider, "model", "")
        if not (isinstance(model, str) and model in candidates):
            continue
        if not provider_satisfies_request(provider, capability_request):
            continue
        capability_matches += 1
        if _provider_is_quarantined(provider.name, debt):
            continue
        if lanes is not None:
            lane = lanes.get(provider.name)
            if lanes and lane is None:
                # As in select_lane, only an entirely empty map means that
                # legacy callers did not request lane tracking. A partial map
                # is an incomplete admission view and fails closed.
                continue
            if lane is not None:
                current = debt.get(provider.name) if debt is not None else None
                retry_after_count = current.retry_after_count if current is not None else 0
                if not _lane_has_capacity(provider, lane, retry_after_count):
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
        (provider.name, models[provider.name], float(rank)) for rank, provider in enumerate(ordered)
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
    "RoutingRequest",
    "context_window_satisfies",
    "provider_satisfies_request",
    "score_providers",
    "select_lane",
    "select_primary",
    "resolve_assignment",
    "validate_requirements",
]
