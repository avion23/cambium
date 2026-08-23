from __future__ import annotations

import sqlite3
import time
from concurrent.futures import ProcessPoolExecutor, ThreadPoolExecutor
from multiprocessing import get_context
from pathlib import Path

import pytest

import cambium.provider_scheduler as provider_state
from cambium.provider_scheduler import (
    ProviderLease,
    QuotaLedger,
    QuotaLedgerBusyError,
    QuotaWindowSpec,
    quota_snapshot_json,
)


def _reserve_from_process(path: str, index: int) -> bool:
    ledger = QuotaLedger(path)
    window = QuotaWindowSpec("process-window", 3600, request_allowance=12)
    return ledger.reserve("zai", (window,), index, now=100.0) is not None


class _ExecuteFaultConnection:
    def __init__(self, connection: sqlite3.Connection, failures: dict[str, int]) -> None:
        self._connection = connection
        self._failures = failures

    def execute(self, sql: str, *parameters):
        for marker, remaining in tuple(self._failures.items()):
            if remaining and marker in sql:
                self._failures[marker] = remaining - 1
                raise sqlite3.OperationalError("database is locked")
        return self._connection.execute(sql, *parameters)

    def close(self) -> None:
        self._connection.close()

    def __getattr__(self, name: str):
        return getattr(self._connection, name)


def test_module_has_one_state_role_and_no_competing_scheduler() -> None:
    assert not hasattr(provider_state, "ProviderScheduler")
    assert not hasattr(provider_state, "rank_policies")
    assert set(provider_state.__all__) == {
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
    }


def test_provider_lease_is_strictly_identified() -> None:
    lease = ProviderLease("openai", "gpt-5.6", "root", cache_identity="prefix")
    assert lease.provider == "openai"
    assert lease.model == "gpt-5.6"
    assert lease.root_task_id == "root"
    with pytest.raises(ValueError, match="must be non-empty"):
        ProviderLease("", "gpt-5.6", "root")


def test_quota_window_mapping_is_strict() -> None:
    window = QuotaWindowSpec.from_mapping(
        {
            "name": "five-hour",
            "duration_s": 5 * 3600,
            "token_allowance": 100,
            "request_allowance": 10,
            "reserve_fraction": 0.1,
        }
    )
    assert window.token_allowance == 100
    assert window.request_allowance == 10
    with pytest.raises(ValueError, match="unknown quota-window"):
        QuotaWindowSpec.from_mapping(
            {"name": "bad", "duration_s": 1, "token_allowance": 1, "extra": True}
        )


def test_quota_ledger_reservation_is_atomic_across_threads(tmp_path: Path) -> None:
    ledger = QuotaLedger(tmp_path / "quota.db")
    window = QuotaWindowSpec("five-hour", 5 * 3600, request_allowance=10)

    def reserve(index: int):
        return ledger.reserve("zai", (window,), index, now=100.0)

    with ThreadPoolExecutor(max_workers=20) as pool:
        reservations = list(pool.map(reserve, range(20)))
    assert sum(item is not None for item in reservations) == 10
    assert ledger.snapshots("zai")[0].used_requests == 10


@pytest.mark.slow
def test_quota_ledger_reservations_are_atomic_across_processes(tmp_path: Path) -> None:
    path = tmp_path / "quota.db"
    context = get_context("spawn")
    with ProcessPoolExecutor(max_workers=8, mp_context=context) as pool:
        reservations = list(pool.map(_reserve_from_process, [str(path)] * 24, range(24)))

    assert sum(reservations) == 12
    ledger = QuotaLedger(path)
    assert ledger.snapshots("zai")[0].used_requests == 12


def test_busy_reservation_retries_with_backoff_and_commits_once(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    ledger = QuotaLedger(tmp_path / "quota.db")
    window = QuotaWindowSpec("tokens", 300, token_allowance=100)
    real_connect = ledger._connect
    failures = {"INSERT INTO quota_reservations": 1}
    sleeps: list[float] = []

    def connect_with_one_busy() -> _ExecuteFaultConnection:
        return _ExecuteFaultConnection(real_connect(), failures)

    monkeypatch.setattr(ledger, "_connect", connect_with_one_busy)
    monkeypatch.setattr(provider_state, "_BUSY_RETRY_S", 0.5)
    monkeypatch.setattr(provider_state.time, "sleep", lambda delay: sleeps.append(delay))

    reservation = ledger.reserve("p", (window,), 20, requests=0, now=1.0)

    assert reservation is not None
    assert sleeps == [provider_state._BUSY_RETRY_INITIAL_SLEEP_S]
    assert ledger.snapshots("p")[0].used_tokens == 20
    with sqlite3.connect(tmp_path / "quota.db") as connection:
        assert connection.execute("PRAGMA integrity_check").fetchone()[0] == "ok"
        assert connection.execute("SELECT COUNT(*) FROM quota_reservations").fetchone()[0] == 1


def test_busy_reservation_fails_structured_after_bounded_backoff(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    ledger = QuotaLedger(tmp_path / "quota.db")
    real_connect = ledger._connect
    failures = {"BEGIN IMMEDIATE": 1_000_000}
    sleeps: list[float] = []

    def connect_always_busy() -> _ExecuteFaultConnection:
        return _ExecuteFaultConnection(real_connect(), failures)

    monkeypatch.setattr(ledger, "_connect", connect_always_busy)
    monkeypatch.setattr(provider_state, "_BUSY_RETRY_S", 0.08)
    monkeypatch.setattr(provider_state, "_BUSY_RETRY_INITIAL_SLEEP_S", 0.01)
    monkeypatch.setattr(provider_state, "_BUSY_RETRY_MAX_SLEEP_S", 0.02)
    real_sleep = provider_state.time.sleep

    def record_sleep(delay: float) -> None:
        sleeps.append(delay)
        real_sleep(delay)

    monkeypatch.setattr(
        provider_state.time,
        "sleep", record_sleep,
    )

    started = time.monotonic()
    with pytest.raises(QuotaLedgerBusyError, match="remained busy") as excinfo:
        ledger.reserve("p", (QuotaWindowSpec("requests", 60, request_allowance=1),), 0)
    elapsed = time.monotonic() - started

    assert elapsed < 1.0
    assert sleeps
    assert sleeps[0] == pytest.approx(0.01)
    assert max(sleeps) == pytest.approx(0.02)
    assert sum(sleeps) <= 0.08
    assert isinstance(excinfo.value.__cause__, sqlite3.OperationalError)
    with sqlite3.connect(tmp_path / "quota.db") as connection:
        assert connection.execute("PRAGMA integrity_check").fetchone()[0] == "ok"
        assert connection.execute("SELECT COUNT(*) FROM quota_reservations").fetchone()[0] == 0


def test_observe_preserves_pending_reservations(tmp_path: Path) -> None:
    ledger = QuotaLedger(tmp_path / "quota.db")
    window = QuotaWindowSpec("short", 10, token_allowance=100, request_allowance=10)
    reservation = ledger.reserve("p", (window,), 20, now=1.0)
    assert reservation is not None

    ledger.observe(
        "p",
        "short",
        reset_at=10.0,
        allowance_tokens=100,
        remaining_tokens=70,
        allowance_requests=10,
        remaining_requests=8,
        now=2.0,
    )

    snapshot = ledger.snapshots("p")[0]
    assert snapshot.used_tokens == 50
    assert snapshot.used_requests == 3


def test_late_reconciliation_does_not_touch_new_window(tmp_path: Path) -> None:
    ledger = QuotaLedger(tmp_path / "quota.db")
    window = QuotaWindowSpec("short", 10, token_allowance=100)
    old = ledger.reserve("p", (window,), 20, requests=0, now=1.0)
    assert old is not None
    assert ledger.reserve("p", (window,), 0, requests=0, now=11.0) is not None

    ledger.reconcile(old, (window,), 40, now=11.0)

    snapshot = ledger.snapshots("p")[0]
    assert snapshot.reset_at == 20.0
    assert snapshot.used_tokens == 0


def test_reconciled_reservations_are_pruned(tmp_path: Path) -> None:
    ledger = QuotaLedger(tmp_path / "quota.db")
    window = QuotaWindowSpec("long", 1_000_000, token_allowance=100)
    reservation = ledger.reserve("p", (window,), 20, requests=0, now=0.0)
    assert reservation is not None
    ledger.reconcile(reservation, (window,), 20, now=24 * 60 * 60 + 1)

    with sqlite3.connect(tmp_path / "quota.db") as connection:
        assert connection.execute("SELECT COUNT(*) FROM quota_reservations").fetchone()[0] == 0
        assert (
            connection.execute("SELECT COUNT(*) FROM quota_reservation_windows").fetchone()[0] == 0
        )


def test_quota_reconciliation_is_idempotent(tmp_path: Path) -> None:
    ledger = QuotaLedger(tmp_path / "quota.db")
    window = QuotaWindowSpec("tokens", 300, token_allowance=100)
    reservation = ledger.reserve("openai", (window,), 20, now=10.0)
    assert reservation is not None

    ledger.reconcile(reservation, (window,), 7, now=11.0)
    ledger.reconcile(reservation, (window,), 99, now=12.0)

    snapshot = ledger.snapshots("openai")[0]
    assert snapshot.used_tokens == 7
    assert quota_snapshot_json(snapshot)["remaining_tokens"] == 93


def test_quota_observation_validates_and_replaces_window(tmp_path: Path) -> None:
    ledger = QuotaLedger(tmp_path / "quota.db")
    ledger.observe(
        "openai",
        "requests",
        reset_at=200.0,
        allowance_requests=50,
        remaining_requests=40,
        reserve_fraction=0.1,
        now=100.0,
    )
    snapshot = ledger.snapshots("openai")[0]
    assert snapshot.used_requests == 10
    assert snapshot.remaining_requests == 40

    with pytest.raises(ValueError, match="reserve_fraction"):
        ledger.observe(
            "openai",
            "bad",
            reset_at=200.0,
            allowance_requests=1,
            reserve_fraction=1.0,
            now=100.0,
        )
