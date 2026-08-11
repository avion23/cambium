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
"""

from __future__ import annotations

import json
import os
import tempfile
import time
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any

# Placeholder weekly-equivalent token window per provider. No measured quota
# contract exists yet (implementation-plan step 3); a provider config may
# override this per provider with ``token_window_allowance``.
DEFAULT_TOKEN_WINDOW_ALLOWANCE = 20_000_000
DEFAULT_ROUTING_STATE_PATH = Path.home() / ".config" / "cambium" / "routing-state.json"
_ROUTING_STATE_VERSION = 1


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
        self.last_seen = time.time()


def _debt_from_mapping(name: str, entry: Mapping[str, Any]) -> ProviderDebt:
    """Parse one ledger entry, ignoring malformed fields (tolerate corruption)."""
    debt = ProviderDebt()
    for field, converter in (
        ("tokens", int),
        ("requests", int),
        ("failed_requests", int),
        ("retry_after_count", int),
    ):
        value = entry.get(field)
        if isinstance(value, (int, float)) and not isinstance(value, bool) and value >= 0:
            setattr(debt, field, converter(value))
    cost = entry.get("cost")
    if isinstance(cost, (int, float)) and not isinstance(cost, bool) and cost >= 0:
        debt.cost = float(cost)
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
        self._path = Path(path) if path is not None else DEFAULT_ROUTING_STATE_PATH
        self._debts: dict[str, ProviderDebt] = {}
        self._dirty = False

    @property
    def path(self) -> Path:
        return self._path

    @property
    def dirty(self) -> bool:
        """True when live usage events have been folded since load/save."""
        return self._dirty

    def load(self) -> None:
        """Replace memory with the persisted ledger; tolerate a bad file."""
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
            for name, entry in raw["providers"].items():
                if isinstance(name, str) and isinstance(entry, Mapping):
                    debts[name] = _debt_from_mapping(name, entry)
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


__all__ = [
    "DEFAULT_ROUTING_STATE_PATH",
    "DEFAULT_TOKEN_WINDOW_ALLOWANCE",
    "DebtStore",
    "ProviderDebt",
    "select_primary",
]
