"""Quota-aware provider scheduling with explicit leases and single-owner admission.

The module separates five dimensions that must not be collapsed into one score:
semantic eligibility, health, concurrent capacity, quota windows, and economic
preference. Pure ranking is deterministic; mutable admission state belongs to
``ProviderScheduler``, an asyncio mailbox/actor. ``QuotaLedger`` uses SQLite
``BEGIN IMMEDIATE`` transactions so independent Cambium processes cannot lose
updates while consuming the same subscription window.
"""

from __future__ import annotations

import asyncio
import hashlib
import math
import os
import sqlite3
import time
import uuid
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import asdict, dataclass, field
from enum import StrEnum
from pathlib import Path
from typing import Any


class BillingMode(StrEnum):
    """How a provider consumes scarce resources."""

    SUBSCRIPTION = "subscription"
    METERED = "metered"
    FREE = "free"
    LOCAL = "local"


@dataclass(frozen=True, slots=True)
class QuotaWindowSpec:
    """One independently enforced request/token allowance."""

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
        if isinstance(duration, bool) or not isinstance(duration, (int, float)):
            raise ValueError("quota window duration_s must be a number")
        if isinstance(tokens, bool) or not isinstance(tokens, int):
            raise ValueError("quota window token_allowance must be an integer")
        if isinstance(requests, bool) or not isinstance(requests, int):
            raise ValueError("quota window request_allowance must be an integer")
        if isinstance(reserve, bool) or not isinstance(reserve, (int, float)):
            raise ValueError("quota window reserve_fraction must be a number")
        return cls(name, float(duration), tokens, requests, float(reserve))


@dataclass(frozen=True, slots=True)
class ProviderLease:
    """A strict provider/model/cache branch lease for one recursive trunk."""

    provider: str
    model: str
    root_task_id: str
    cache_identity: str = ""
    acquired_at: float = field(default_factory=time.time)

    def __post_init__(self) -> None:
        if not self.provider or not self.model or not self.root_task_id:
            raise ValueError("provider lease fields must be non-empty")


@dataclass(frozen=True, slots=True)
class ProviderPolicy:
    """Static scheduling facts for one provider/model lane."""

    name: str
    model: str
    priority: int = 0
    max_concurrency: int = 1
    billing_mode: BillingMode = BillingMode.METERED
    quota_windows: tuple[QuotaWindowSpec, ...] = ()
    price_per_1m_in: float = 0.0
    price_per_1m_cached_in: float = 0.0
    price_per_1m_out: float = 0.0
    pricing_known: bool = False
    throughput_hint_tps: float = 0.0
    quality_weight: float = 1.0
    context_window: int = 0
    supports_native_tools: bool = True
    supports_python_tool: bool = True
    enabled: bool = True

    def __post_init__(self) -> None:
        if not self.name or not self.model:
            raise ValueError("provider policy name/model must be non-empty")
        if self.max_concurrency <= 0:
            raise ValueError("max_concurrency must be positive")
        if self.context_window < 0:
            raise ValueError("context_window must be non-negative")
        for value in (
            self.price_per_1m_in,
            self.price_per_1m_cached_in,
            self.price_per_1m_out,
            self.throughput_hint_tps,
            self.quality_weight,
        ):
            if not math.isfinite(value) or value < 0:
                raise ValueError("provider policy numeric values must be finite/non-negative")


@dataclass(frozen=True, slots=True)
class ProviderEvidence:
    """Bounded empirical evidence; priors prevent one sample from dominating."""

    attempts: int = 0
    successes: int = 0
    ewma_tps: float = 0.0
    ewma_latency_s: float = 0.0

    @property
    def success_probability(self) -> float:
        return (self.successes + 2.0) / (self.attempts + 3.0)


@dataclass(frozen=True, slots=True)
class RoutingRequest:
    """Hard requirements plus one scheduling identity."""

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
    lease: ProviderLease | None = None


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


@dataclass(frozen=True, slots=True)
class AdmissionGrant:
    policy: ProviderPolicy
    lease: ProviderLease
    quota_reservation: QuotaReservation | None


@dataclass(frozen=True, slots=True)
class SchedulerSnapshot:
    in_flight: Mapping[str, int]
    queued: int
    quota_windows: tuple[QuotaWindowSnapshot, ...]


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
        connection = sqlite3.connect(self.path, timeout=30.0, isolation_level=None)
        connection.execute("PRAGMA busy_timeout=30000")
        connection.execute("PRAGMA journal_mode=WAL")
        connection.execute("PRAGMA synchronous=FULL")
        return connection

    def _initialize(self) -> None:
        with self._connect() as connection:
            connection.executescript(
                """
                CREATE TABLE IF NOT EXISTS quota_windows (
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
                );
                CREATE TABLE IF NOT EXISTS quota_reservations (
                    reservation_id TEXT PRIMARY KEY,
                    provider TEXT NOT NULL,
                    estimated_tokens INTEGER NOT NULL,
                    requests INTEGER NOT NULL,
                    reconciled INTEGER NOT NULL DEFAULT 0,
                    created_at REAL NOT NULL
                );
                """
            )
        try:
            os.chmod(self.path, 0o600)
        except OSError:
            pass

    @staticmethod
    def _window_reset(now: float, duration_s: float) -> float:
        return (math.floor(now / duration_s) + 1) * duration_s

    def reserve(
        self,
        provider: str,
        windows: Sequence[QuotaWindowSpec],
        estimated_tokens: int,
        *,
        requests: int = 1,
        now: float | None = None,
    ) -> QuotaReservation | None:
        if not windows:
            return None
        if estimated_tokens < 0 or requests < 0:
            raise ValueError("quota reservation values must be non-negative")
        timestamp = time.time() if now is None else float(now)
        reservation_id = uuid.uuid4().hex
        connection = self._connect()
        try:
            connection.execute("BEGIN IMMEDIATE")
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
                    "used_tokens=excluded.used_tokens, allowance_requests=excluded.allowance_requests, "
                    "used_requests=excluded.used_requests, reserve_fraction=excluded.reserve_fraction, "
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
            connection.commit()
        except BaseException:
            connection.rollback()
            raise
        finally:
            connection.close()
        return QuotaReservation(reservation_id, provider, estimated_tokens, requests)

    def reconcile(
        self,
        reservation: QuotaReservation,
        windows: Sequence[QuotaWindowSpec],
        actual_tokens: int,
        *,
        now: float | None = None,
    ) -> None:
        if actual_tokens < 0:
            raise ValueError("actual_tokens must be non-negative")
        timestamp = time.time() if now is None else float(now)
        connection = self._connect()
        try:
            connection.execute("BEGIN IMMEDIATE")
            row = connection.execute(
                "SELECT estimated_tokens,reconciled FROM quota_reservations "
                "WHERE reservation_id=? AND provider=?",
                (reservation.reservation_id, reservation.provider),
            ).fetchone()
            if row is None or int(row[1]) != 0:
                connection.commit()
                return
            delta = actual_tokens - int(row[0])
            if delta:
                for spec in windows:
                    connection.execute(
                        "UPDATE quota_windows SET used_tokens=MAX(0,used_tokens+?),updated_at=? "
                        "WHERE provider=? AND name=?",
                        (delta, timestamp, reservation.provider, spec.name),
                    )
            connection.execute(
                "UPDATE quota_reservations SET reconciled=1 WHERE reservation_id=?",
                (reservation.reservation_id,),
            )
            connection.commit()
        except BaseException:
            connection.rollback()
            raise
        finally:
            connection.close()

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
        timestamp = time.time() if now is None else float(now)
        if reset_at <= timestamp:
            return
        used_tokens = 0 if remaining_tokens is None else max(0, allowance_tokens - remaining_tokens)
        used_requests = 0 if remaining_requests is None else max(0, allowance_requests - remaining_requests)
        connection = self._connect()
        try:
            connection.execute("BEGIN IMMEDIATE")
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
            connection.commit()
        except BaseException:
            connection.rollback()
            raise
        finally:
            connection.close()

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
        with self._connect() as connection:
            rows = connection.execute(sql, params).fetchall()
        return tuple(QuotaWindowSnapshot(*row) for row in rows)


def _stable_unit_interval(task_id: str, provider: str) -> float:
    digest = hashlib.sha256(f"{task_id}\0{provider}".encode()).digest()
    return int.from_bytes(digest[:8], "big") / float(2**64 - 1)


def _estimated_money(policy: ProviderPolicy, request: RoutingRequest) -> float:
    if not policy.pricing_known:
        return 0.0
    return (
        request.expected_input_tokens * policy.price_per_1m_in
        + request.expected_output_tokens * policy.price_per_1m_out
    ) / 1_000_000.0


def _eligible(
    policy: ProviderPolicy,
    request: RoutingRequest,
    in_flight: Mapping[str, int],
) -> bool:
    if not policy.enabled or in_flight.get(policy.name, 0) >= policy.max_concurrency:
        return False
    if request.lease is not None:
        return policy.name == request.lease.provider and policy.model == request.lease.model
    if not request.allow_model_substitution and policy.model != request.model:
        return False
    if request.required_context_tokens and (
        policy.context_window <= 0 or policy.context_window < request.required_context_tokens
    ):
        return False
    if request.needs_native_tools and not policy.supports_native_tools:
        return False
    if request.needs_python_tool and not policy.supports_python_tool:
        return False
    if policy.billing_mode in {BillingMode.METERED, BillingMode.SUBSCRIPTION}:
        if not request.allow_paid:
            return False
    elif not request.allow_free:
        return False
    return True


def rank_policies(
    policies: Iterable[ProviderPolicy],
    request: RoutingRequest,
    *,
    in_flight: Mapping[str, int] | None = None,
    evidence: Mapping[str, ProviderEvidence] | None = None,
) -> list[ProviderPolicy]:
    """Return a deterministic hard-feasible, lexicographically ranked list."""

    load = {} if in_flight is None else in_flight
    observations = {} if evidence is None else evidence
    feasible = [policy for policy in policies if _eligible(policy, request, load)]

    def key(policy: ProviderPolicy) -> tuple[float, ...]:
        sample = observations.get(policy.name, ProviderEvidence())
        success = sample.success_probability
        tps = sample.ewma_tps or policy.throughput_hint_tps or 1.0
        useful_tps = max(1e-6, tps * success * max(policy.quality_weight, 1e-6))
        service_penalty = request.expected_output_tokens / useful_tps
        money = _estimated_money(policy, request)
        unknown_price = 1.0 if not policy.pricing_known else 0.0
        switch = 1.0 if request.incumbent_provider not in (None, policy.name) else 0.0
        utilization = load.get(policy.name, 0) / policy.max_concurrency
        tie = -_stable_unit_interval(request.task_id, policy.name)
        return (
            float(policy.priority),
            switch,
            1.0 - success,
            service_penalty,
            utilization,
            money,
            unknown_price,
            tie,
        )

    return sorted(feasible, key=key)


@dataclass(slots=True)
class _Acquire:
    request: RoutingRequest
    future: asyncio.Future[AdmissionGrant]


@dataclass(slots=True)
class _Release:
    grant: AdmissionGrant
    actual_tokens: int
    success: bool
    latency_s: float
    future: asyncio.Future[None]


@dataclass(slots=True)
class _Snapshot:
    future: asyncio.Future[SchedulerSnapshot]


@dataclass(slots=True)
class _Close:
    future: asyncio.Future[None]


class ProviderScheduler:
    """Single-owner admission mailbox for provider lanes."""

    def __init__(
        self,
        policies: Sequence[ProviderPolicy],
        *,
        quota_ledger: QuotaLedger | None = None,
    ) -> None:
        names = [policy.name for policy in policies]
        if len(names) != len(set(names)):
            raise ValueError("provider policy names must be unique")
        self._policies = tuple(policies)
        self._ledger = quota_ledger
        self._mailbox: asyncio.Queue[_Acquire | _Release | _Snapshot | _Close] = asyncio.Queue()
        self._task: asyncio.Task[None] | None = None
        self._in_flight = {policy.name: 0 for policy in policies}
        self._evidence = {policy.name: ProviderEvidence() for policy in policies}

    async def start(self) -> None:
        if self._task is None:
            self._task = asyncio.create_task(self._serve(), name="provider-scheduler")

    async def acquire(self, request: RoutingRequest) -> AdmissionGrant:
        await self.start()
        future: asyncio.Future[AdmissionGrant] = asyncio.get_running_loop().create_future()
        await self._mailbox.put(_Acquire(request, future))
        return await future

    async def release(
        self,
        grant: AdmissionGrant,
        *,
        actual_tokens: int,
        success: bool,
        latency_s: float,
    ) -> None:
        future: asyncio.Future[None] = asyncio.get_running_loop().create_future()
        await self._mailbox.put(_Release(grant, actual_tokens, success, latency_s, future))
        await future

    async def snapshot(self) -> SchedulerSnapshot:
        await self.start()
        future: asyncio.Future[SchedulerSnapshot] = asyncio.get_running_loop().create_future()
        await self._mailbox.put(_Snapshot(future))
        return await future

    async def close(self) -> None:
        if self._task is None:
            return
        future: asyncio.Future[None] = asyncio.get_running_loop().create_future()
        await self._mailbox.put(_Close(future))
        await future
        await self._task
        self._task = None

    async def _serve(self) -> None:
        while True:
            message = await self._mailbox.get()
            try:
                if isinstance(message, _Acquire):
                    await self._handle_acquire(message)
                elif isinstance(message, _Release):
                    await self._handle_release(message)
                elif isinstance(message, _Snapshot):
                    windows = () if self._ledger is None else await asyncio.to_thread(self._ledger.snapshots)
                    message.future.set_result(
                        SchedulerSnapshot(dict(self._in_flight), self._mailbox.qsize(), windows)
                    )
                else:
                    message.future.set_result(None)
                    return
            except BaseException as exc:
                if not message.future.done():
                    message.future.set_exception(exc)

    async def _handle_acquire(self, message: _Acquire) -> None:
        request = message.request
        ranked = rank_policies(
            self._policies,
            request,
            in_flight=self._in_flight,
            evidence=self._evidence,
        )
        if not ranked:
            raise RuntimeError("no provider satisfies the routing request")
        estimated = max(0, request.expected_input_tokens + request.expected_output_tokens)
        for policy in ranked:
            reservation = None
            if policy.quota_windows:
                if self._ledger is None:
                    raise RuntimeError("quota-window provider has no quota ledger")
                reservation = await asyncio.to_thread(
                    self._ledger.reserve,
                    policy.name,
                    policy.quota_windows,
                    estimated,
                )
                if reservation is None:
                    continue
            self._in_flight[policy.name] += 1
            lease = ProviderLease(policy.name, policy.model, request.task_id)
            message.future.set_result(AdmissionGrant(policy, lease, reservation))
            return
        raise RuntimeError("all feasible providers are quota-exhausted")

    async def _handle_release(self, message: _Release) -> None:
        name = message.grant.policy.name
        self._in_flight[name] = max(0, self._in_flight.get(name, 0) - 1)
        sample = self._evidence.get(name, ProviderEvidence())
        alpha = 0.2
        output_tps = message.actual_tokens / message.latency_s if message.latency_s > 0 else 0.0
        self._evidence[name] = ProviderEvidence(
            attempts=sample.attempts + 1,
            successes=sample.successes + int(message.success),
            ewma_tps=(
                output_tps
                if sample.ewma_tps <= 0
                else (1 - alpha) * sample.ewma_tps + alpha * output_tps
            ),
            ewma_latency_s=(
                message.latency_s
                if sample.ewma_latency_s <= 0
                else (1 - alpha) * sample.ewma_latency_s + alpha * message.latency_s
            ),
        )
        reservation = message.grant.quota_reservation
        if reservation is not None and self._ledger is not None:
            await asyncio.to_thread(
                self._ledger.reconcile,
                reservation,
                message.grant.policy.quota_windows,
                message.actual_tokens,
            )
        message.future.set_result(None)


def quota_snapshot_json(snapshot: QuotaWindowSnapshot) -> dict[str, Any]:
    """Stable JSON projection for durable observability events."""

    value = asdict(snapshot)
    value["remaining_tokens"] = snapshot.remaining_tokens
    value["remaining_requests"] = snapshot.remaining_requests
    return value


__all__ = [
    "AdmissionGrant",
    "BillingMode",
    "ProviderEvidence",
    "ProviderLease",
    "ProviderPolicy",
    "ProviderScheduler",
    "QuotaLedger",
    "QuotaReservation",
    "QuotaWindowSnapshot",
    "QuotaWindowSpec",
    "RoutingRequest",
    "SchedulerSnapshot",
    "quota_snapshot_json",
    "rank_policies",
]
