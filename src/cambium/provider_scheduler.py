"""Shared provider leases and durable quota accounting.

Provider admission policy lives exclusively in :mod:`cambium.routing`.  This
module intentionally contains no second scheduler, ranking function, mailbox,
or lane state.  It owns only the provider-domain values shared by configuration,
Diffundo, and the operator quota CLI:

* immutable provider/model leases for one semantic trunk;
* validated quota-window specifications;
* transactional, cross-process quota reservations and reconciliation;
* stable quota snapshots for observability.

The historical ``ProviderScheduler`` actor was never wired into the supervisor
and duplicated ``cambium.routing``.  Keeping the state primitives here preserves
the existing import boundary without retaining a competing scheduling policy.
"""

from __future__ import annotations

import math
import os
import sqlite3
import time
import uuid
from collections.abc import Callable, Mapping, Sequence
from dataclasses import asdict, dataclass
from enum import StrEnum
from pathlib import Path
from typing import Any, TypeVar

_RESERVATION_RETENTION_S = 24 * 60 * 60

# SQLite's built-in busy timeout is deliberately kept short.  The ledger
# retries the *whole transaction* instead of allowing one connection to sleep
# inside SQLite for an unbounded amount of the caller's time budget.  Retrying
# the transaction (rather than an individual statement) is important: a
# writer may have already changed one quota window before a later statement
# reports SQLITE_BUSY.
_SQLITE_CONNECT_TIMEOUT_S = 0.1
_BUSY_RETRY_S = 2.0
_BUSY_RETRY_INITIAL_SLEEP_S = 0.01
_BUSY_RETRY_MAX_SLEEP_S = 0.25

_ResultT = TypeVar("_ResultT")

# CAST scheduling defaults are deliberately conservative.  A zero rollover
# threshold disables automatic rollover, preserving the flat trunk behavior
# for callers that have not opted into an epoch policy.  A zero breakpoint
# target means every completed delta is eligible, which is the historical
# behavior; non-zero targets batch small deltas until the target or horizon.
DEFAULT_CACHE_HORIZON_S = 60.0
DEFAULT_MINIMUM_BREAKPOINT_TOKENS = 0


@dataclass(frozen=True, slots=True, init=False)
class CacheCapability:
    """Normalized provider prefix-cache capability and tariff metadata.

    Prices are USD per million cache tokens.  The constructor accepts the
    vocabulary used by the architecture documents (for example
    ``minimum_cacheable_tokens``/``cache_ttl_s``) as well as the shorter names
    used in provider files.  The stored fields stay canonical so routing and
    rollover code do not need provider-specific aliases.
    """

    minimum_cacheable_tokens: int
    cache_ttl_s: float
    cache_granularity_tokens: int
    cache_read_price: float
    cache_write_price: float

    def __init__(
        self,
        minimum_cacheable_tokens: int = 0,
        cache_ttl_s: float = 0.0,
        cache_granularity_tokens: int = 1,
        cache_read_price: float = 0.0,
        cache_write_price: float = 0.0,
        *,
        min_cacheable_tokens: int | None = None,
        min_cacheable_block_tokens: int | None = None,
        ttl_s: float | None = None,
        ttl_seconds: float | None = None,
        cache_ttl_seconds: float | None = None,
        granularity: int | None = None,
        granularity_tokens: int | None = None,
        cache_block_granularity_tokens: int | None = None,
        cache_read_price_per_1m: float | None = None,
        cache_write_price_per_1m: float | None = None,
    ) -> None:
        minimum = _coalesce_alias(
            "minimum_cacheable_tokens",
            minimum_cacheable_tokens,
            0,
            (min_cacheable_tokens, min_cacheable_block_tokens),
        )
        ttl = _coalesce_alias(
            "cache_ttl_s",
            cache_ttl_s,
            0.0,
            (ttl_s, ttl_seconds, cache_ttl_seconds),
        )
        block = _coalesce_alias(
            "cache_granularity_tokens",
            cache_granularity_tokens,
            1,
            (granularity, granularity_tokens, cache_block_granularity_tokens),
        )
        read_price = _coalesce_alias(
            "cache_read_price", cache_read_price, 0.0, (cache_read_price_per_1m,)
        )
        write_price = _coalesce_alias(
            "cache_write_price", cache_write_price, 0.0, (cache_write_price_per_1m,)
        )
        if isinstance(minimum, bool) or not isinstance(minimum, int) or minimum < 0:
            raise ValueError("minimum_cacheable_tokens must be a non-negative integer")
        if isinstance(block, bool) or not isinstance(block, int) or block <= 0:
            raise ValueError("cache_granularity_tokens must be a positive integer")
        for field_name, value in (
            ("cache_ttl_s", ttl),
            ("cache_read_price", read_price),
            ("cache_write_price", write_price),
        ):
            if isinstance(value, bool) or not isinstance(value, int | float):
                raise ValueError(f"{field_name} must be a number")
            if not math.isfinite(float(value)) or float(value) < 0:
                raise ValueError(f"{field_name} must be finite and non-negative")
        object.__setattr__(self, "minimum_cacheable_tokens", minimum)
        object.__setattr__(self, "cache_ttl_s", float(ttl))
        object.__setattr__(self, "cache_granularity_tokens", block)
        object.__setattr__(self, "cache_read_price", float(read_price))
        object.__setattr__(self, "cache_write_price", float(write_price))

    @classmethod
    def from_mapping(cls, value: Mapping[str, Any]) -> CacheCapability:
        """Parse one strict provider cache-capability mapping."""
        allowed = {
            "minimum_cacheable_tokens",
            "min_cacheable_tokens",
            "min_cacheable_block_tokens",
            "cache_ttl_s",
            "ttl_s",
            "ttl_seconds",
            "cache_ttl_seconds",
            "cache_granularity_tokens",
            "granularity",
            "granularity_tokens",
            "cache_block_granularity_tokens",
            "cache_read_price",
            "cache_read_price_per_1m",
            "cache_write_price",
            "cache_write_price_per_1m",
        }
        unknown = sorted(set(value) - allowed)
        if unknown:
            raise ValueError(f"unknown cache-capability field(s): {unknown}")
        return cls(**dict(value))

    def cacheable_tokens(self, tokens: int) -> int:
        """Round a prefix up to the provider's cache block granularity."""
        if isinstance(tokens, bool) or not isinstance(tokens, int) or tokens < 0:
            raise ValueError("cache token count must be a non-negative integer")
        if tokens == 0:
            return 0
        return math.ceil(tokens / self.cache_granularity_tokens) * self.cache_granularity_tokens

    def supports_prefix(self, tokens: int) -> bool:
        """Return whether a prefix is large enough for provider caching."""
        if isinstance(tokens, bool) or not isinstance(tokens, int) or tokens < 0:
            raise ValueError("cache token count must be a non-negative integer")
        return tokens >= self.minimum_cacheable_tokens

    def cost(self, tokens: int, *, write: bool = False) -> float:
        """Return the tariff for *tokens* cache tokens."""
        rounded = self.cacheable_tokens(tokens)
        price = self.cache_write_price if write else self.cache_read_price
        return rounded / 1_000_000 * price


def _coalesce_alias(
    field_name: str,
    canonical: int | float,
    default: int | float,
    aliases: Sequence[int | float | None],
) -> int | float:
    """Choose one alias while rejecting contradictory declarations."""
    selected = canonical
    for alias in aliases:
        if alias is None:
            continue
        if selected != default and selected != alias:
            raise ValueError(f"{field_name} aliases disagree")
        if any(other is not None and other != alias for other in aliases):
            raise ValueError(f"{field_name} aliases disagree")
        selected = alias
    return selected


@dataclass(frozen=True, slots=True)
class CastConfig:
    """CAST rollover and cache-horizon policy for one semantic trunk.

    ``max_segments`` and ``max_active_trunk_tokens`` are inclusive budgets:
    rollover is due only once the active trunk exceeds either configured
    value.  Zero disables that threshold.  Alias fields keep configuration
    compatible with the terminology used by older architecture drafts.
    """

    cache_horizon_s: float = DEFAULT_CACHE_HORIZON_S
    minimum_breakpoint_tokens: int = DEFAULT_MINIMUM_BREAKPOINT_TOKENS
    # Friendly aliases used by provider capability documents.
    horizon_s: float | None = None
    min_breakpoint_tokens: int | None = None
    max_segments: int = 0
    max_active_trunk_tokens: int = 0
    max_summary_segments: int | None = None
    max_trunk_tokens: int | None = None

    def __post_init__(self) -> None:
        horizon = self.cache_horizon_s
        if self.horizon_s is not None:
            if horizon != DEFAULT_CACHE_HORIZON_S and horizon != self.horizon_s:
                raise ValueError("cache horizon aliases disagree")
            horizon = self.horizon_s
        minimum = self.minimum_breakpoint_tokens
        if self.min_breakpoint_tokens is not None:
            if (
                minimum != DEFAULT_MINIMUM_BREAKPOINT_TOKENS
                and minimum != self.min_breakpoint_tokens
            ):
                raise ValueError("minimum breakpoint aliases disagree")
            minimum = self.min_breakpoint_tokens
        if isinstance(horizon, bool) or not isinstance(horizon, int | float):
            raise ValueError("cache_horizon_s must be a number")
        if not math.isfinite(float(horizon)) or float(horizon) <= 0:
            raise ValueError("cache_horizon_s must be positive and finite")
        if isinstance(minimum, bool) or not isinstance(minimum, int) or minimum < 0:
            raise ValueError("minimum_breakpoint_tokens must be a non-negative integer")
        object.__setattr__(self, "cache_horizon_s", float(horizon))
        object.__setattr__(self, "minimum_breakpoint_tokens", minimum)
        object.__setattr__(self, "horizon_s", float(horizon))
        object.__setattr__(self, "min_breakpoint_tokens", minimum)
        segments = self.max_segments
        if self.max_summary_segments is not None:
            if segments != 0 and segments != self.max_summary_segments:
                raise ValueError("max segment aliases disagree")
            segments = self.max_summary_segments
        tokens = self.max_active_trunk_tokens
        if self.max_trunk_tokens is not None:
            if tokens != 0 and tokens != self.max_trunk_tokens:
                raise ValueError("max trunk-token aliases disagree")
            tokens = self.max_trunk_tokens
        for name, value in (
            ("max_segments", segments),
            ("max_active_trunk_tokens", tokens),
        ):
            if isinstance(value, bool) or not isinstance(value, int) or value < 0:
                raise ValueError(f"{name} must be a non-negative integer")
        object.__setattr__(self, "max_segments", segments)
        object.__setattr__(self, "max_active_trunk_tokens", tokens)
        object.__setattr__(self, "max_summary_segments", segments)
        object.__setattr__(self, "max_trunk_tokens", tokens)

    @classmethod
    def from_mapping(cls, value: Mapping[str, Any]) -> CastConfig:
        """Parse a strict CAST policy mapping, accepting documented aliases."""
        allowed = {
            "cache_horizon_s",
            "minimum_breakpoint_tokens",
            "horizon_s",
            "min_breakpoint_tokens",
            "max_segments",
            "max_active_trunk_tokens",
            "max_summary_segments",
            "max_trunk_tokens",
        }
        unknown = sorted(set(value) - allowed)
        if unknown:
            raise ValueError(f"unknown CAST field(s): {unknown}")
        return cls(**dict(value))

    def breakpoint_due(
        self,
        pending_tokens: int,
        started_at: float,
        *,
        now: float | None = None,
        force: bool = False,
    ) -> bool:
        """Whether a pending delta should create a new breakpoint."""
        if isinstance(pending_tokens, bool) or not isinstance(pending_tokens, int):
            raise ValueError("pending_tokens must be a non-negative integer")
        if pending_tokens < 0:
            raise ValueError("pending_tokens must be a non-negative integer")
        if force:
            return pending_tokens > 0
        if pending_tokens == 0:
            return False
        if self.minimum_breakpoint_tokens == 0:
            return True
        if pending_tokens >= self.minimum_breakpoint_tokens:
            return True
        timestamp = time.time() if now is None else float(now)
        if not math.isfinite(timestamp) or not math.isfinite(float(started_at)):
            raise ValueError("cache breakpoint times must be finite")
        return timestamp - float(started_at) >= self.cache_horizon_s

    def rollover_due(self, segment_count: int, active_trunk_tokens: int) -> bool:
        """Return true when either CAST rollover threshold is exceeded."""
        if (
            isinstance(segment_count, bool)
            or not isinstance(segment_count, int)
            or segment_count < 0
        ):
            raise ValueError("segment_count must be a non-negative integer")
        if (
            isinstance(active_trunk_tokens, bool)
            or not isinstance(active_trunk_tokens, int)
            or active_trunk_tokens < 0
        ):
            raise ValueError("active_trunk_tokens must be a non-negative integer")
        return bool(
            (self.max_segments and segment_count > self.max_segments)
            or (
                self.max_active_trunk_tokens
                and active_trunk_tokens > self.max_active_trunk_tokens
            )
        )

    def rollover_decision(
        self,
        segment_count: int,
        active_trunk_tokens: int,
        *,
        new_prefix_tokens: int,
        expected_remaining_calls: float,
        cache_capability: CacheCapability | Mapping[str, Any] | None,
        cache_expired: bool = True,
    ) -> RolloverDecision:
        """Compare K0 write cost with the cost of continuing this epoch."""
        return decide_rollover(
            self,
            segment_count,
            active_trunk_tokens,
            new_prefix_tokens=new_prefix_tokens,
            expected_remaining_calls=expected_remaining_calls,
            cache_capability=cache_capability,
            cache_expired=cache_expired,
        )

@dataclass(frozen=True, slots=True)
class RolloverDecision:
    """Auditable economic decision for one threshold-triggered K0 rollover."""

    should_rollover: bool
    thresholds_hit: bool
    rollover_cost: float
    continue_cost: float
    n_star: float
    expected_remaining_calls: float
    old_prefix_tokens: int
    new_prefix_tokens: int
    cache_expired: bool
    reason: str

    @property
    def rollover(self) -> bool:
        """Short boolean spelling used by CAST callers."""
        return self.should_rollover

    @property
    def decision(self) -> str:
        """Stable wire spelling for evidence consumers."""
        return "rollover" if self.should_rollover else "continue"

    @property
    def evidence(self) -> dict[str, Any]:
        """JSON-safe decision evidence without provider or prompt content."""
        return {
            "decision": self.decision,
            "should_rollover": self.should_rollover,
            "thresholds_hit": self.thresholds_hit,
            "rollover_cost_usd": self.rollover_cost,
            "continue_cost_usd": self.continue_cost,
            "n_star": self.n_star,
            "expected_remaining_calls": self.expected_remaining_calls,
            "old_prefix_tokens": self.old_prefix_tokens,
            "new_prefix_tokens": self.new_prefix_tokens,
            "cache_expired": self.cache_expired,
            "reason": self.reason,
        }

    def event(
        self,
        *,
        task_id: str | None = None,
        epoch: int | None = None,
        checkpoint_ref: str | None = None,
    ) -> dict[str, Any]:
        """Build a redacted durable decision-evidence event."""
        payload = dict(self.evidence)
        if task_id is not None:
            payload["task_id"] = task_id
        if epoch is not None:
            payload["epoch"] = epoch
        if checkpoint_ref is not None:
            payload["checkpoint_ref"] = checkpoint_ref
        return {
            "type": "cast_rollover_decision",
            "kind": "cast_rollover_decision",
            "payload": payload,
        }


def _cache_capability(value: CacheCapability | Mapping[str, Any] | None) -> CacheCapability:
    if value is None:
        return CacheCapability()
    if isinstance(value, CacheCapability):
        return value
    if isinstance(value, Mapping):
        return CacheCapability.from_mapping(value)
    raise TypeError("cache_capability must be a CacheCapability or mapping")


def decide_rollover(
    cast_config: CastConfig,
    segment_count: int,
    active_trunk_tokens: int,
    *,
    new_prefix_tokens: int,
    expected_remaining_calls: float,
    cache_capability: CacheCapability | Mapping[str, Any] | None,
    cache_expired: bool = True,
) -> RolloverDecision:
    """Apply CAST thresholds and compare cache write/read projections.

    The one-time rollover projection writes the new K0 prefix.  Continuing an
    expired epoch pays a cache read for the old prefix on each expected future
    call.  ``n_star`` is the break-even remaining-call count; a rollover is
    selected only when thresholds are due and the projected continuation cost
    is at least the one-time write cost.  With no expired cache there is no
    economic pressure to rebuild, so threshold evaluation remains observable
    but chooses ``continue``.
    """
    if not isinstance(cast_config, CastConfig):
        raise TypeError("cast_config must be a CastConfig")
    thresholds_hit = cast_config.rollover_due(segment_count, active_trunk_tokens)
    for field_name, value in (
        ("new_prefix_tokens", new_prefix_tokens),
        ("active_trunk_tokens", active_trunk_tokens),
    ):
        if isinstance(value, bool) or not isinstance(value, int) or value < 0:
            raise ValueError(f"{field_name} must be a non-negative integer")
    if (
        isinstance(expected_remaining_calls, bool)
        or not isinstance(expected_remaining_calls, int | float)
        or not math.isfinite(float(expected_remaining_calls))
        or expected_remaining_calls < 0
    ):
        raise ValueError("expected_remaining_calls must be finite and non-negative")
    if type(cache_expired) is not bool:
        raise ValueError("cache_expired must be a boolean")
    capability = _cache_capability(cache_capability)
    old_tokens = capability.cacheable_tokens(active_trunk_tokens)
    new_tokens = capability.cacheable_tokens(new_prefix_tokens)
    rollover_cost = capability.cost(new_tokens, write=True)
    per_call_continue = capability.cost(old_tokens) if cache_expired else 0.0
    continue_cost = float(expected_remaining_calls) * per_call_continue
    if per_call_continue > 0:
        n_star = rollover_cost / per_call_continue
    elif rollover_cost == 0:
        n_star = 0.0
    else:
        n_star = math.inf
    if not thresholds_hit:
        should_rollover = False
        reason = "thresholds_not_hit"
    elif not cache_expired:
        should_rollover = False
        reason = "cache_still_warm"
    elif continue_cost >= rollover_cost:
        should_rollover = True
        reason = "rollover_write_is_amortized"
    else:
        should_rollover = False
        reason = "continue_read_is_cheaper"
    return RolloverDecision(
        should_rollover=should_rollover,
        thresholds_hit=thresholds_hit,
        rollover_cost=rollover_cost,
        continue_cost=continue_cost,
        n_star=n_star,
        expected_remaining_calls=float(expected_remaining_calls),
        old_prefix_tokens=old_tokens,
        new_prefix_tokens=new_tokens,
        cache_expired=cache_expired,
        reason=reason,
    )


class BillingMode(StrEnum):
    """How a configured provider consumes scarce capacity."""

    SUBSCRIPTION = "subscription"
    METERED = "metered"
    FREE = "free"
    LOCAL = "local"


@dataclass(frozen=True, slots=True)
class QuotaWindowSpec:
    """One independently enforced token and/or request allowance."""

    name: str
    duration_s: float
    token_allowance: int = 0
    request_allowance: int = 0
    reserve_fraction: float = 0.0

    def __post_init__(self) -> None:
        if not self.name:
            raise ValueError("quota window name must be non-empty")
        if not math.isfinite(self.duration_s) or self.duration_s <= 0:
            raise ValueError("quota window duration_s must be positive and finite")
        if self.token_allowance < 0 or self.request_allowance < 0:
            raise ValueError("quota allowances must be non-negative")
        if not math.isfinite(self.reserve_fraction) or not 0 <= self.reserve_fraction < 1:
            raise ValueError("quota reserve_fraction must be in [0, 1)")
        if self.token_allowance == 0 and self.request_allowance == 0:
            raise ValueError("a quota window must constrain tokens or requests")

    @classmethod
    def from_mapping(cls, value: Mapping[str, Any]) -> QuotaWindowSpec:
        allowed = {
            "name",
            "duration_s",
            "token_allowance",
            "request_allowance",
            "reserve_fraction",
        }
        unknown = sorted(set(value) - allowed)
        if unknown:
            raise ValueError(f"unknown quota-window field(s): {unknown}")
        name = value.get("name")
        duration = value.get("duration_s")
        tokens = value.get("token_allowance", 0)
        requests = value.get("request_allowance", 0)
        reserve = value.get("reserve_fraction", 0.0)
        if not isinstance(name, str):
            raise ValueError("quota window name must be a string")
        if isinstance(duration, bool) or not isinstance(duration, int | float):
            raise ValueError("quota window duration_s must be a number")
        if isinstance(tokens, bool) or not isinstance(tokens, int):
            raise ValueError("quota window token_allowance must be an integer")
        if isinstance(requests, bool) or not isinstance(requests, int):
            raise ValueError("quota window request_allowance must be an integer")
        if isinstance(reserve, bool) or not isinstance(reserve, int | float):
            raise ValueError("quota window reserve_fraction must be a number")
        return cls(name, float(duration), tokens, requests, float(reserve))


@dataclass(frozen=True, slots=True)
class ProviderLease:
    """Strict provider/model ownership for one recursive semantic trunk."""

    provider: str
    model: str
    root_task_id: str
    cache_identity: str = ""

    def __post_init__(self) -> None:
        if not self.provider or not self.model or not self.root_task_id:
            raise ValueError("provider lease fields must be non-empty")


@dataclass(frozen=True, slots=True)
class QuotaWindowSnapshot:
    provider: str
    name: str
    reset_at: float
    allowance_tokens: int
    used_tokens: int
    allowance_requests: int
    used_requests: int
    reserve_fraction: float

    @property
    def remaining_tokens(self) -> int | None:
        if self.allowance_tokens <= 0:
            return None
        return max(0, self.allowance_tokens - self.used_tokens)

    @property
    def remaining_requests(self) -> int | None:
        if self.allowance_requests <= 0:
            return None
        return max(0, self.allowance_requests - self.used_requests)


@dataclass(frozen=True, slots=True)
class QuotaReservation:
    reservation_id: str
    provider: str
    estimated_tokens: int
    requests: int


class QuotaLedgerError(RuntimeError):
    """The durable quota ledger could not complete an operation."""


class QuotaLedgerBusyError(QuotaLedgerError):
    """The quota database stayed locked after bounded transaction retries."""


class QuotaLedgerDiskFullError(QuotaLedgerError):
    """The quota database could not persist state because storage is full."""


def _state_path() -> Path:
    configured = os.environ.get("CAMBIUM_QUOTA_DB")
    if configured:
        return Path(configured).expanduser().resolve()
    state_home = os.environ.get("XDG_STATE_HOME")
    root = Path(state_home).expanduser() if state_home else Path.home() / ".local" / "state"
    return root / "cambium" / "provider-quota.db"


class QuotaLedger:
    """Cross-process quota reservations with transactional reconciliation."""

    def __init__(self, path: str | Path | None = None) -> None:
        self.path = _state_path() if path is None else Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        try:
            os.chmod(self.path.parent, 0o700)
        except OSError:
            pass
        self._initialize()

    def _connect(self) -> sqlite3.Connection:
        connection = sqlite3.connect(
            self.path, timeout=_SQLITE_CONNECT_TIMEOUT_S, isolation_level=None
        )
        connection.execute("PRAGMA busy_timeout=0")
        journal_mode = connection.execute("PRAGMA journal_mode=WAL").fetchone()[0]
        if str(journal_mode).lower() != "wal":
            raise QuotaLedgerError(f"SQLite did not enable WAL mode: {journal_mode!r}")
        connection.execute("PRAGMA synchronous=FULL")
        return connection

    @staticmethod
    def _is_busy(exc: BaseException) -> bool:
        code = getattr(exc, "sqlite_errorcode", None)
        if isinstance(code, int) and code & 0xFF == getattr(sqlite3, "SQLITE_BUSY", 5):
            return True
        if not isinstance(exc, sqlite3.Error):
            return False
        message = str(exc).lower()
        return "database is locked" in message or "database table is locked" in message

    @staticmethod
    def _is_disk_full(exc: BaseException) -> bool:
        code = getattr(exc, "sqlite_errorcode", None)
        if isinstance(code, int) and code & 0xFF == getattr(sqlite3, "SQLITE_FULL", 13):
            return True
        message = str(exc).lower()
        return (
            "database or disk is full" in message
            or "database is full" in message
            or "no space left on device" in message
        )

    @classmethod
    def _storage_failure(cls, operation: str, exc: BaseException) -> QuotaLedgerError:
        if cls._is_disk_full(exc):
            return QuotaLedgerDiskFullError(
                f"quota ledger {operation} failed: database or disk is full"
            )
        return QuotaLedgerError(f"quota ledger {operation} failed: {exc}")

    def _run_with_retry(
        self,
        operation: str,
        action: Callable[[sqlite3.Connection], _ResultT],
    ) -> _ResultT:
        """Run one SQLite action with bounded, whole-operation busy retries."""
        deadline = time.monotonic() + _BUSY_RETRY_S
        delay = _BUSY_RETRY_INITIAL_SLEEP_S
        while True:
            connection: sqlite3.Connection | None = None
            try:
                connection = self._connect()
                return action(connection)
            except (sqlite3.Error, OSError) as exc:
                if self._is_busy(exc):
                    remaining = deadline - time.monotonic()
                    if remaining <= 0:
                        raise QuotaLedgerBusyError(
                            f"quota ledger {operation} remained busy for "
                            f"{_BUSY_RETRY_S}s"
                        ) from exc
                    time.sleep(min(delay, remaining))
                    delay = min(delay * 2, _BUSY_RETRY_MAX_SLEEP_S)
                    continue
                raise self._storage_failure(operation, exc) from exc
            finally:
                if connection is not None:
                    connection.close()

    def _run_transaction(
        self,
        operation: str,
        action: Callable[[sqlite3.Connection], _ResultT],
    ) -> _ResultT:
        """Run ``action`` in a retryable ``BEGIN IMMEDIATE`` transaction."""

        def transactional(connection: sqlite3.Connection) -> _ResultT:
            connection.execute("BEGIN IMMEDIATE")
            try:
                result = action(connection)
                connection.commit()
                return result
            except BaseException:
                if connection.in_transaction:
                    connection.rollback()
                raise

        return self._run_with_retry(operation, transactional)

    def _initialize(self) -> None:
        def initialize(connection: sqlite3.Connection) -> None:
            connection.execute(
                """CREATE TABLE IF NOT EXISTS quota_windows (
                    provider TEXT NOT NULL,
                    name TEXT NOT NULL,
                    reset_at REAL NOT NULL,
                    allowance_tokens INTEGER NOT NULL,
                    used_tokens INTEGER NOT NULL,
                    allowance_requests INTEGER NOT NULL,
                    used_requests INTEGER NOT NULL,
                    reserve_fraction REAL NOT NULL,
                    updated_at REAL NOT NULL,
                    PRIMARY KEY(provider, name)
                )"""
            )
            connection.execute(
                """CREATE TABLE IF NOT EXISTS quota_reservations (
                    reservation_id TEXT PRIMARY KEY,
                    provider TEXT NOT NULL,
                    estimated_tokens INTEGER NOT NULL,
                    requests INTEGER NOT NULL,
                    reconciled INTEGER NOT NULL DEFAULT 0,
                    created_at REAL NOT NULL
                )"""
            )
            connection.execute(
                """CREATE TABLE IF NOT EXISTS quota_reservation_windows (
                    reservation_id TEXT NOT NULL,
                    provider TEXT NOT NULL,
                    name TEXT NOT NULL,
                    reset_at REAL NOT NULL,
                    PRIMARY KEY(reservation_id, provider, name)
                )"""
            )
            self._prune_reconciled(connection, time.time() - _RESERVATION_RETENTION_S)

        self._run_transaction("initialization", initialize)
        try:
            os.chmod(self.path, 0o600)
        except OSError:
            pass

    @staticmethod
    def _window_reset(now: float, duration_s: float) -> float:
        return (math.floor(now / duration_s) + 1) * duration_s

    @staticmethod
    def _prune_reconciled(connection: sqlite3.Connection, before: float) -> None:
        connection.execute(
            "DELETE FROM quota_reservation_windows WHERE reservation_id IN ("
            "SELECT reservation_id FROM quota_reservations "
            "WHERE reconciled=1 AND created_at < ?)",
            (before,),
        )
        connection.execute(
            "DELETE FROM quota_reservations WHERE reconciled=1 AND created_at < ?",
            (before,),
        )

    @staticmethod
    def _reservation_window_reset(
        connection: sqlite3.Connection,
        reservation_id: str,
        provider: str,
        name: str,
    ) -> float | None:
        row = connection.execute(
            "SELECT reset_at FROM quota_reservation_windows "
            "WHERE reservation_id=? AND provider=? AND name=?",
            (reservation_id, provider, name),
        ).fetchone()
        return None if row is None else float(row[0])

    def reserve(
        self,
        provider: str,
        windows: Sequence[QuotaWindowSpec],
        estimated_tokens: int,
        *,
        requests: int = 1,
        now: float | None = None,
    ) -> QuotaReservation | None:
        """Atomically reserve every configured window or reserve none of them."""

        if not provider:
            raise ValueError("provider must be non-empty")
        if not windows:
            return None
        if estimated_tokens < 0 or requests < 0:
            raise ValueError("quota reservation values must be non-negative")
        timestamp = time.time() if now is None else float(now)
        if not math.isfinite(timestamp):
            raise ValueError("quota reservation time must be finite")
        reservation_id = uuid.uuid4().hex
        def reserve_transaction(connection: sqlite3.Connection) -> QuotaReservation | None:
            self._prune_reconciled(connection, timestamp - _RESERVATION_RETENTION_S)
            normalized: list[tuple[QuotaWindowSpec, float, int, int]] = []
            for spec in windows:
                row = connection.execute(
                    "SELECT reset_at, used_tokens, used_requests FROM quota_windows "
                    "WHERE provider=? AND name=?",
                    (provider, spec.name),
                ).fetchone()
                if row is None or float(row[0]) <= timestamp:
                    reset_at = self._window_reset(timestamp, spec.duration_s)
                    used_tokens = 0
                    used_requests = 0
                else:
                    reset_at = float(row[0])
                    used_tokens = int(row[1])
                    used_requests = int(row[2])
                token_cap = math.floor(spec.token_allowance * (1.0 - spec.reserve_fraction))
                request_cap = math.floor(spec.request_allowance * (1.0 - spec.reserve_fraction))
                if spec.token_allowance and used_tokens + estimated_tokens > token_cap:
                    connection.rollback()
                    return None
                if spec.request_allowance and used_requests + requests > request_cap:
                    connection.rollback()
                    return None
                normalized.append((spec, reset_at, used_tokens, used_requests))

            for spec, reset_at, used_tokens, used_requests in normalized:
                connection.execute(
                    "INSERT INTO quota_windows(provider,name,reset_at,allowance_tokens,"
                    "used_tokens,allowance_requests,used_requests,reserve_fraction,updated_at) "
                    "VALUES(?,?,?,?,?,?,?,?,?) ON CONFLICT(provider,name) DO UPDATE SET "
                    "reset_at=excluded.reset_at, allowance_tokens=excluded.allowance_tokens, "
                    "used_tokens=excluded.used_tokens, "
                    "allowance_requests=excluded.allowance_requests, "
                    "used_requests=excluded.used_requests, "
                    "reserve_fraction=excluded.reserve_fraction, "
                    "updated_at=excluded.updated_at",
                    (
                        provider,
                        spec.name,
                        reset_at,
                        spec.token_allowance,
                        used_tokens + estimated_tokens,
                        spec.request_allowance,
                        used_requests + requests,
                        spec.reserve_fraction,
                        timestamp,
                    ),
                )
            connection.execute(
                "INSERT INTO quota_reservations(reservation_id,provider,estimated_tokens,"
                "requests,created_at) VALUES(?,?,?,?,?)",
                (reservation_id, provider, estimated_tokens, requests, timestamp),
            )
            connection.executemany(
                "INSERT INTO quota_reservation_windows(reservation_id,provider,name,reset_at) "
                "VALUES(?,?,?,?)",
                [
                    (reservation_id, provider, spec.name, reset_at)
                    for spec, reset_at, _, _ in normalized
                ],
            )
            return QuotaReservation(reservation_id, provider, estimated_tokens, requests)

        return self._run_transaction("reserve", reserve_transaction)

    def reconcile(
        self,
        reservation: QuotaReservation,
        windows: Sequence[QuotaWindowSpec],
        actual_tokens: int,
        *,
        now: float | None = None,
    ) -> None:
        """Replace an estimate with actual token usage exactly once."""

        if actual_tokens < 0:
            raise ValueError("actual_tokens must be non-negative")
        timestamp = time.time() if now is None else float(now)
        if not math.isfinite(timestamp):
            raise ValueError("quota reconciliation time must be finite")
        def reconcile_transaction(connection: sqlite3.Connection) -> None:
            row = connection.execute(
                "SELECT estimated_tokens,reconciled FROM quota_reservations "
                "WHERE reservation_id=? AND provider=?",
                (reservation.reservation_id, reservation.provider),
            ).fetchone()
            if row is None or int(row[1]) != 0:
                return
            delta = actual_tokens - int(row[0])
            if delta:
                for spec in windows:
                    reset_at = self._reservation_window_reset(
                        connection, reservation.reservation_id, reservation.provider, spec.name
                    )
                    if reset_at is None:
                        continue
                    connection.execute(
                        "UPDATE quota_windows SET used_tokens=MAX(0,used_tokens+?),updated_at=? "
                        "WHERE provider=? AND name=? AND reset_at=? AND reset_at>?",
                        (delta, timestamp, reservation.provider, spec.name, reset_at, timestamp),
                    )
            connection.execute(
                "UPDATE quota_reservations SET reconciled=1 WHERE reservation_id=?",
                (reservation.reservation_id,),
            )
            self._prune_reconciled(connection, timestamp - _RESERVATION_RETENTION_S)

        self._run_transaction("reconcile", reconcile_transaction)

    def observe(
        self,
        provider: str,
        name: str,
        *,
        reset_at: float,
        allowance_tokens: int = 0,
        remaining_tokens: int | None = None,
        allowance_requests: int = 0,
        remaining_requests: int | None = None,
        reserve_fraction: float = 0.0,
        now: float | None = None,
    ) -> None:
        """Replace a window with trusted provider/header/dashboard evidence."""

        timestamp = time.time() if now is None else float(now)
        if not provider or not name:
            raise ValueError("provider and quota window name must be non-empty")
        if not all(math.isfinite(value) for value in (timestamp, reset_at, reserve_fraction)):
            raise ValueError("quota observation values must be finite")
        if reset_at <= timestamp:
            return
        if allowance_tokens < 0 or allowance_requests < 0:
            raise ValueError("quota allowances must be non-negative")
        if remaining_tokens is not None and remaining_tokens < 0:
            raise ValueError("remaining_tokens must be non-negative")
        if remaining_requests is not None and remaining_requests < 0:
            raise ValueError("remaining_requests must be non-negative")
        if not 0 <= reserve_fraction < 1:
            raise ValueError("reserve_fraction must be in [0, 1)")
        observed_tokens = (
            0 if remaining_tokens is None else max(0, allowance_tokens - remaining_tokens)
        )
        observed_requests = (
            0 if remaining_requests is None else max(0, allowance_requests - remaining_requests)
        )

        def observe_transaction(connection: sqlite3.Connection) -> None:
            self._prune_reconciled(connection, timestamp - _RESERVATION_RETENTION_S)
            pending = connection.execute(
                "SELECT COALESCE(SUM(reservation.estimated_tokens),0), "
                "COALESCE(SUM(reservation.requests),0) "
                "FROM quota_reservations AS reservation "
                "JOIN quota_reservation_windows AS reservation_window ON "
                "reservation_window.reservation_id=reservation.reservation_id AND "
                "reservation_window.provider=reservation.provider "
                "WHERE reservation.provider=? AND reservation_window.name=? "
                "AND reservation_window.reset_at=? AND reservation.reconciled=0",
                (provider, name, reset_at),
            ).fetchone()
            used_tokens = observed_tokens + int(pending[0])
            used_requests = observed_requests + int(pending[1])
            connection.execute(
                "INSERT INTO quota_windows(provider,name,reset_at,allowance_tokens,used_tokens,"
                "allowance_requests,used_requests,reserve_fraction,updated_at) "
                "VALUES(?,?,?,?,?,?,?,?,?) ON CONFLICT(provider,name) DO UPDATE SET "
                "reset_at=excluded.reset_at, allowance_tokens=excluded.allowance_tokens, "
                "used_tokens=excluded.used_tokens, allowance_requests=excluded.allowance_requests, "
                "used_requests=excluded.used_requests, reserve_fraction=excluded.reserve_fraction, "
                "updated_at=excluded.updated_at",
                (
                    provider,
                    name,
                    reset_at,
                    allowance_tokens,
                    used_tokens,
                    allowance_requests,
                    used_requests,
                    reserve_fraction,
                    timestamp,
                ),
            )
        self._run_transaction("observe", observe_transaction)

    def snapshots(self, provider: str | None = None) -> tuple[QuotaWindowSnapshot, ...]:
        sql = (
            "SELECT provider,name,reset_at,allowance_tokens,used_tokens,"
            "allowance_requests,used_requests,reserve_fraction FROM quota_windows"
        )
        params: tuple[Any, ...] = ()
        if provider is not None:
            sql += " WHERE provider=?"
            params = (provider,)
        sql += " ORDER BY provider,name"
        rows = self._run_with_retry(
            "snapshot",
            lambda connection: connection.execute(sql, params).fetchall(),
        )
        return tuple(QuotaWindowSnapshot(*row) for row in rows)


def quota_snapshot_json(snapshot: QuotaWindowSnapshot) -> dict[str, Any]:
    """Stable JSON projection for CLI and durable observability records."""

    value = asdict(snapshot)
    value["remaining_tokens"] = snapshot.remaining_tokens
    value["remaining_requests"] = snapshot.remaining_requests
    return value


__all__ = [
    "BillingMode",
    "ProviderLease",
    "QuotaLedger",
    "QuotaLedgerBusyError",
    "QuotaLedgerDiskFullError",
    "QuotaLedgerError",
    "QuotaReservation",
    "QuotaWindowSnapshot",
    "QuotaWindowSpec",
    "quota_snapshot_json",
]
