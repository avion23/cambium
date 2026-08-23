from __future__ import annotations

import sqlite3
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import pytest

import cambium.provider_scheduler as provider_state
from cambium.provider_scheduler import (
    ProviderLease,
    QuotaLedger,
    QuotaWindowSpec,
    quota_snapshot_json,
)


def test_module_has_one_state_role_and_no_competing_scheduler() -> None:
    assert not hasattr(provider_state, "ProviderScheduler")
    assert not hasattr(provider_state, "rank_policies")
    assert not hasattr(provider_state, "CastConfig")
    assert not hasattr(provider_state, "CacheHorizonConfig")
    assert set(provider_state.__all__) == {
        "BillingMode",
        "ProviderLease",
        "QuotaLedger",
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
