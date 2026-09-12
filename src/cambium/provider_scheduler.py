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
import threading
import time
import uuid
from collections.abc import Callable, Mapping, Sequence
from contextlib import closing
from dataclasses import asdict, dataclass
from enum import StrEnum
from pathlib import Path
from typing import Any, TypeVar

_RESERVATION_RETENTION_S = 24 * 60 * 60
_QUOTA_SNAPSHOT_SQL = (
    "SELECT provider,name,reset_at,allowance_tokens,used_tokens,"
    "allowance_requests,used_requests,reserve_fraction FROM quota_windows"
)

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
        *,
        deadline: float | None = None,
        cancel_event: threading.Event | None = None,
    ) -> _ResultT:
        """Run one SQLite action with bounded, whole-operation busy retries.

        ``deadline`` is a monotonic caller deadline. It can shorten the
        ledger's own busy-retry window but never extend it. ``cancel_event``
        wakes a busy retry promptly; an already-running SQLite transaction is
        still allowed to finish so reservation accounting cannot be torn in
        half.
        """
        retry_deadline = time.monotonic() + _BUSY_RETRY_S
        if deadline is not None:
            if (
                isinstance(deadline, bool)
                or not isinstance(deadline, int | float)
                or not math.isfinite(float(deadline))
            ):
                raise ValueError("quota retry deadline must be finite")
            retry_deadline = min(retry_deadline, float(deadline))
            if retry_deadline <= time.monotonic():
                raise QuotaLedgerBusyError(
                    f"quota ledger {operation} remained busy before its deadline"
                )
        delay = _BUSY_RETRY_INITIAL_SLEEP_S
        while True:
            if cancel_event is not None and cancel_event.is_set():
                raise InterruptedError(f"quota ledger {operation} cancelled")
            connection: sqlite3.Connection | None = None
            try:
                connection = self._connect()
                return action(connection)
            except (sqlite3.Error, OSError) as exc:
                if self._is_busy(exc):
                    remaining = retry_deadline - time.monotonic()
                    if remaining <= 0:
                        raise QuotaLedgerBusyError(
                            f"quota ledger {operation} remained busy until its deadline"
                        ) from exc
                    sleep_for = min(delay, remaining)
                    if cancel_event is not None:
                        if cancel_event.wait(sleep_for):
                            raise InterruptedError(f"quota ledger {operation} cancelled") from exc
                    else:
                        time.sleep(sleep_for)
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
        *,
        deadline: float | None = None,
        cancel_event: threading.Event | None = None,
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

        return self._run_with_retry(
            operation,
            transactional,
            deadline=deadline,
            cancel_event=cancel_event,
        )

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
        deadline: float | None = None,
        cancel_event: threading.Event | None = None,
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

        return self._run_transaction(
            "reserve",
            reserve_transaction,
            deadline=deadline,
            cancel_event=cancel_event,
        )

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

    def snapshots(
        self, provider: str | None = None, *, deadline: float | None = None
    ) -> tuple[QuotaWindowSnapshot, ...]:
        sql = _QUOTA_SNAPSHOT_SQL
        params: tuple[Any, ...] = ()
        if provider is not None:
            sql += " WHERE provider=?"
            params = (provider,)
        sql += " ORDER BY provider,name"
        rows = self._run_with_retry(
            "snapshot",
            lambda connection: connection.execute(sql, params).fetchall(),
            deadline=deadline,
        )
        return tuple(QuotaWindowSnapshot(*row) for row in rows)


def read_quota_snapshots(
    path: str | Path | None = None, provider: str | None = None
) -> tuple[QuotaWindowSnapshot, ...]:
    """Read operator quota state without creating a ledger or changing its mode."""
    path = _state_path() if path is None else Path(path).expanduser().resolve()
    if not path.is_file():
        return ()
    sql = _QUOTA_SNAPSHOT_SQL
    params: tuple[str, ...] = ()
    if provider is not None:
        sql += " WHERE provider=?"
        params = (provider,)
    try:
        with closing(
            sqlite3.connect(f"{path.as_uri()}?mode=ro", uri=True, timeout=0.1)
        ) as connection:
            rows = connection.execute(sql + " ORDER BY provider,name", params).fetchall()
    except sqlite3.Error as exc:
        raise QuotaLedgerError(f"quota ledger read failed: {exc}") from exc
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
    "read_quota_snapshots",
]
