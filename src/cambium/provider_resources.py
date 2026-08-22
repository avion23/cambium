"""Provider task capabilities, prepaid budgets, and quota telemetry adapters.

This module owns resource types that are orthogonal to provider health and
semantic cache affinity. Money is stored as integer micro-dollars; quota/header
observations are content-free. All persistent mutations use SQLite
``BEGIN IMMEDIATE`` so multiple Cambium processes cannot overwrite one another.
"""

from __future__ import annotations

import math
import os
import re
import sqlite3
import time
import uuid
from collections.abc import Mapping, Sequence
from dataclasses import asdict, dataclass
from decimal import Decimal, InvalidOperation, ROUND_HALF_UP
from enum import StrEnum
from pathlib import Path
from typing import Any, Protocol

from .provider_scheduler import AdmissionGrant, RoutingRequest, quota_db_path


class TaskClass(StrEnum):
    """Semantic work classes used as hard provider capability constraints."""

    ROOT = "root"
    CODE = "code"
    RESEARCH = "research"
    SEARCH = "search"
    REVIEW = "review"
    TEST = "test"
    TRIAGE = "triage"
    SUMMARY = "summary"


_DEFAULT_TASK_CLASSES = frozenset(TaskClass)


def parse_task_classes(value: object) -> frozenset[TaskClass]:
    if value is None:
        return _DEFAULT_TASK_CLASSES
    if not isinstance(value, (list, tuple)) or not value:
        raise ValueError("task_classes must be a non-empty string list")
    parsed: set[TaskClass] = set()
    for item in value:
        if not isinstance(item, str):
            raise ValueError("task_classes must contain strings")
        try:
            parsed.add(TaskClass(item))
        except ValueError as exc:
            choices = ", ".join(member.value for member in TaskClass)
            raise ValueError(f"invalid task class {item!r}; expected {choices}") from exc
    return frozenset(parsed)


class ProviderAdmissionPort(Protocol):
    """Narrow async admission interface consumed by supervisors/workers."""

    async def acquire(self, request: RoutingRequest) -> AdmissionGrant: ...

    async def release(
        self,
        grant: AdmissionGrant,
        *,
        actual_tokens: int,
        success: bool,
        latency_s: float,
    ) -> None: ...


_MICROS_PER_USD = Decimal("1000000")


def usd_to_micros(value: float | int | str | Decimal) -> int:
    try:
        decimal = Decimal(str(value))
    except (InvalidOperation, ValueError) as exc:
        raise ValueError("USD value must be decimal-compatible") from exc
    if not decimal.is_finite() or decimal < 0:
        raise ValueError("USD value must be finite and non-negative")
    return int((decimal * _MICROS_PER_USD).quantize(Decimal("1"), rounding=ROUND_HALF_UP))


def micros_to_usd(value: int) -> float:
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        raise ValueError("micro-dollar value must be a non-negative integer")
    return float(Decimal(value) / _MICROS_PER_USD)


@dataclass(frozen=True, slots=True)
class MoneyReservation:
    reservation_id: str
    provider: str
    estimated_micros: int


@dataclass(frozen=True, slots=True)
class BalanceSnapshot:
    provider: str
    balance_micros: int
    reserved_micros: int
    floor_micros: int
    updated_at: float

    @property
    def available_micros(self) -> int:
        return max(0, self.balance_micros - self.reserved_micros - self.floor_micros)

    @property
    def balance_usd(self) -> float:
        return micros_to_usd(self.balance_micros)

    @property
    def reserved_usd(self) -> float:
        return micros_to_usd(self.reserved_micros)

    @property
    def floor_usd(self) -> float:
        return micros_to_usd(self.floor_micros)

    @property
    def available_usd(self) -> float:
        return micros_to_usd(self.available_micros)


class BudgetLedger:
    """Transactional prepaid-balance reservations in the quota database."""

    def __init__(self, path: str | Path | None = None) -> None:
        self.path = quota_db_path() if path is None else Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        try:
            os.chmod(self.path.parent, 0o700)
        except OSError:
            pass
        with self._connect() as connection:
            connection.executescript(
                """
                CREATE TABLE IF NOT EXISTS provider_balances (
                    provider TEXT PRIMARY KEY,
                    balance_micros INTEGER NOT NULL,
                    reserved_micros INTEGER NOT NULL,
                    floor_micros INTEGER NOT NULL,
                    updated_at REAL NOT NULL
                );
                CREATE TABLE IF NOT EXISTS money_reservations (
                    reservation_id TEXT PRIMARY KEY,
                    provider TEXT NOT NULL,
                    estimated_micros INTEGER NOT NULL,
                    reconciled INTEGER NOT NULL DEFAULT 0,
                    created_at REAL NOT NULL
                );
                """
            )
        try:
            os.chmod(self.path, 0o600)
        except OSError:
            pass

    def _connect(self) -> sqlite3.Connection:
        connection = sqlite3.connect(self.path, timeout=30.0, isolation_level=None)
        connection.execute("PRAGMA busy_timeout=30000")
        connection.execute("PRAGMA journal_mode=WAL")
        connection.execute("PRAGMA synchronous=FULL")
        return connection

    def observe_balance(
        self,
        provider: str,
        balance_usd: float,
        *,
        floor_usd: float = 0.0,
        now: float | None = None,
    ) -> None:
        if not provider:
            raise ValueError("provider must be non-empty")
        balance = usd_to_micros(balance_usd)
        floor = usd_to_micros(floor_usd)
        if floor > balance:
            raise ValueError("reserve floor cannot exceed the observed balance")
        timestamp = time.time() if now is None else float(now)
        connection = self._connect()
        try:
            connection.execute("BEGIN IMMEDIATE")
            row = connection.execute(
                "SELECT reserved_micros FROM provider_balances WHERE provider=?",
                (provider,),
            ).fetchone()
            reserved = 0 if row is None else min(int(row[0]), balance)
            connection.execute(
                "INSERT INTO provider_balances(provider,balance_micros,reserved_micros,"
                "floor_micros,updated_at) VALUES(?,?,?,?,?) "
                "ON CONFLICT(provider) DO UPDATE SET "
                "balance_micros=excluded.balance_micros, "
                "reserved_micros=MIN(provider_balances.reserved_micros,excluded.balance_micros), "
                "floor_micros=excluded.floor_micros, updated_at=excluded.updated_at",
                (provider, balance, reserved, floor, timestamp),
            )
            connection.commit()
        except BaseException:
            connection.rollback()
            raise
        finally:
            connection.close()

    def reserve(
        self,
        provider: str,
        estimated_usd: float,
        *,
        now: float | None = None,
    ) -> MoneyReservation | None:
        estimate = usd_to_micros(estimated_usd)
        timestamp = time.time() if now is None else float(now)
        reservation_id = uuid.uuid4().hex
        connection = self._connect()
        try:
            connection.execute("BEGIN IMMEDIATE")
            row = connection.execute(
                "SELECT balance_micros,reserved_micros,floor_micros "
                "FROM provider_balances WHERE provider=?",
                (provider,),
            ).fetchone()
            if row is None:
                connection.rollback()
                return None
            balance, reserved, floor = map(int, row)
            if estimate > max(0, balance - reserved - floor):
                connection.rollback()
                return None
            connection.execute(
                "UPDATE provider_balances SET reserved_micros=reserved_micros+?,updated_at=? "
                "WHERE provider=?",
                (estimate, timestamp, provider),
            )
            connection.execute(
                "INSERT INTO money_reservations(reservation_id,provider,estimated_micros,"
                "created_at) VALUES(?,?,?,?)",
                (reservation_id, provider, estimate, timestamp),
            )
            connection.commit()
        except BaseException:
            connection.rollback()
            raise
        finally:
            connection.close()
        return MoneyReservation(reservation_id, provider, estimate)

    def reconcile(
        self,
        reservation: MoneyReservation,
        actual_usd: float,
        *,
        now: float | None = None,
    ) -> None:
        actual = usd_to_micros(actual_usd)
        timestamp = time.time() if now is None else float(now)
        connection = self._connect()
        try:
            connection.execute("BEGIN IMMEDIATE")
            row = connection.execute(
                "SELECT estimated_micros,reconciled FROM money_reservations "
                "WHERE reservation_id=? AND provider=?",
                (reservation.reservation_id, reservation.provider),
            ).fetchone()
            if row is None or int(row[1]) != 0:
                connection.commit()
                return
            estimated = int(row[0])
            connection.execute(
                "UPDATE provider_balances SET "
                "balance_micros=MAX(0,balance_micros-?), "
                "reserved_micros=MAX(0,reserved_micros-?), updated_at=? "
                "WHERE provider=?",
                (actual, estimated, timestamp, reservation.provider),
            )
            connection.execute(
                "UPDATE money_reservations SET reconciled=1 WHERE reservation_id=?",
                (reservation.reservation_id,),
            )
            connection.commit()
        except BaseException:
            connection.rollback()
            raise
        finally:
            connection.close()

    def snapshot(self, provider: str) -> BalanceSnapshot | None:
        with self._connect() as connection:
            row = connection.execute(
                "SELECT provider,balance_micros,reserved_micros,floor_micros,updated_at "
                "FROM provider_balances WHERE provider=?",
                (provider,),
            ).fetchone()
        return None if row is None else BalanceSnapshot(*row)

    def snapshots(self) -> tuple[BalanceSnapshot, ...]:
        with self._connect() as connection:
            rows = connection.execute(
                "SELECT provider,balance_micros,reserved_micros,floor_micros,updated_at "
                "FROM provider_balances ORDER BY provider"
            ).fetchall()
        return tuple(BalanceSnapshot(*row) for row in rows)


@dataclass(frozen=True, slots=True)
class QuotaHeaderMapping:
    """Provider-specific mapping from response headers to one quota window."""

    name: str
    duration_s: float
    token_limit_header: str | None = None
    token_remaining_header: str | None = None
    request_limit_header: str | None = None
    request_remaining_header: str | None = None
    reset_header: str | None = None

    def __post_init__(self) -> None:
        if not self.name or not math.isfinite(self.duration_s) or self.duration_s <= 0:
            raise ValueError("quota header mapping requires name and positive duration")
        if not any(
            (
                self.token_limit_header,
                self.token_remaining_header,
                self.request_limit_header,
                self.request_remaining_header,
            )
        ):
            raise ValueError("quota header mapping declares no limit/remaining headers")


_DURATION_PART = re.compile(r"(?P<number>\d+(?:\.\d+)?)(?P<unit>ms|s|m|h|d)")


def parse_reset_at(value: str | None, *, now: float | None = None, fallback_s: float) -> float:
    timestamp = time.time() if now is None else float(now)
    if value is None or not value.strip():
        return timestamp + fallback_s
    raw = value.strip().lower()
    try:
        number = float(raw)
    except ValueError:
        number = math.nan
    if math.isfinite(number):
        return number if number > timestamp + 60 else timestamp + max(0.0, number)
    total = 0.0
    position = 0
    for match in _DURATION_PART.finditer(raw):
        if match.start() != position:
            total = 0.0
            break
        amount = float(match.group("number"))
        unit = match.group("unit")
        total += amount * {"ms": 0.001, "s": 1, "m": 60, "h": 3600, "d": 86400}[unit]
        position = match.end()
    if total > 0 and position == len(raw):
        return timestamp + total
    return timestamp + fallback_s


def _header(headers: Mapping[str, Any], name: str | None) -> str | None:
    if name is None:
        return None
    lowered = name.lower()
    for key, value in headers.items():
        if str(key).lower() == lowered:
            return str(value)
    return None


def _integer_header(headers: Mapping[str, Any], name: str | None) -> int | None:
    value = _header(headers, name)
    if value is None:
        return None
    try:
        parsed = int(value)
    except ValueError:
        return None
    return max(0, parsed)


def parse_quota_headers(
    provider: str,
    headers: Mapping[str, Any],
    mappings: Sequence[QuotaHeaderMapping],
    *,
    now: float | None = None,
) -> tuple[dict[str, Any], ...]:
    """Content-free durable quota snapshots from configured response headers."""

    timestamp = time.time() if now is None else float(now)
    snapshots: list[dict[str, Any]] = []
    for mapping in mappings:
        token_limit = _integer_header(headers, mapping.token_limit_header) or 0
        token_remaining = _integer_header(headers, mapping.token_remaining_header)
        request_limit = _integer_header(headers, mapping.request_limit_header) or 0
        request_remaining = _integer_header(headers, mapping.request_remaining_header)
        if not any((token_limit, token_remaining is not None, request_limit, request_remaining is not None)):
            continue
        reset_at = parse_reset_at(
            _header(headers, mapping.reset_header),
            now=timestamp,
            fallback_s=mapping.duration_s,
        )
        snapshots.append(
            {
                "provider": provider,
                "name": mapping.name,
                "reset_at": reset_at,
                "allowance_tokens": token_limit,
                "used_tokens": (
                    0 if token_remaining is None else max(0, token_limit - token_remaining)
                ),
                "allowance_requests": request_limit,
                "used_requests": (
                    0
                    if request_remaining is None
                    else max(0, request_limit - request_remaining)
                ),
                "reserve_fraction": 0.0,
                "remaining_tokens": token_remaining,
                "remaining_requests": request_remaining,
            }
        )
    return tuple(snapshots)


def balance_snapshot_json(snapshot: BalanceSnapshot) -> dict[str, Any]:
    value = asdict(snapshot)
    value.update(
        balance_usd=snapshot.balance_usd,
        reserved_usd=snapshot.reserved_usd,
        floor_usd=snapshot.floor_usd,
        available_usd=snapshot.available_usd,
    )
    return value


__all__ = [
    "BalanceSnapshot",
    "BudgetLedger",
    "MoneyReservation",
    "ProviderAdmissionPort",
    "QuotaHeaderMapping",
    "TaskClass",
    "balance_snapshot_json",
    "micros_to_usd",
    "parse_quota_headers",
    "parse_reset_at",
    "parse_task_classes",
    "usd_to_micros",
]
