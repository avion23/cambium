"""Provider lane admission with quota-aware backpressure (H1) scenarios.

Provider lanes add concurrency-aware admission on top of the solution-C
usage-debt ledger: one :class:`LaneState` per provider (in-flight tasks plus
an ``rpm``-derived concurrency allowance) and :func:`select_lane` (max-min
utilization among providers with a spare lane). ``run_plan`` pre-assigns every
un-pinned ``model_candidates`` task in one batch pass from the persisted debt
snapshot, so a wave of concurrent admissions spreads across providers instead
of all picking the same max-min winner; 429 pressure shrinks a lane's
effective in-flight cap, admitting fewer tasks to the pressured provider.

These scenarios run in the fast tier: they exercise the pure selector, the
batch pre-assignment pass, the lane release path, and the new ledger fields
without worker subprocesses.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest

from cambium.diffundo import ProviderConfig, ProviderTier
from cambium.routing import (
    DebtStore,
    LaneState,
    ProviderDebt,
    select_lane,
    select_primary,
)
from cambium.supervisor import _preassign_lanes, _release_lane


def _pc(name: str, model: str, **overrides: Any) -> ProviderConfig:
    base: dict[str, Any] = dict(
        tier=ProviderTier.FAST,
        base_url="http://127.0.0.1:1",
        api_key_env=f"CAMBIUM_PROVIDER_{name.upper()}_API_KEY",
        model=model,
    )
    base.update(overrides)
    return ProviderConfig(name=name, **base)


def _config_file(path: Path, providers: list[tuple[str, str, int]]) -> Path:
    """Write a minimal valid provider config; (name, model, rpm) entries."""
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(
            {
                "providers": [
                    {
                        "name": name,
                        "tier": "fast",
                        "base_url": "http://127.0.0.1:1",
                        "api_key_env": f"CAMBIUM_PROVIDER_{name.upper()}_API_KEY",
                        "rpm": rpm,
                        "enabled": True,
                        "model": model,
                    }
                    for name, model, rpm in providers
                ]
            }
        ),
        encoding="utf-8",
    )
    return path


def _spec(task_id: str, config_path: Path) -> dict[str, Any]:
    return {
        "task_id": task_id,
        "fanout_config": {"tier": "fast"},
        "model_candidates": ["m1", "m2"],
        "provider_config_path": str(config_path),
    }


def _equal_debt(*names: str) -> dict[str, ProviderDebt]:
    return {name: ProviderDebt() for name in names}


# --------------------------------------------------------------------------- #
# 1. select_lane: max-min utilization with lane caps and idle-lane tiebreaks
# --------------------------------------------------------------------------- #


def test_select_lane_ranks_utilization_then_idle_lanes_then_config_order() -> None:
    providers = [_pc("a", "m1"), _pc("b", "m2"), _pc("c", "m1")]
    debt = {
        "a": ProviderDebt(tokens=5_000_000),
        "b": ProviderDebt(tokens=1_000_000),
        "c": ProviderDebt(tokens=2_000_000, requests=1),
    }
    lanes = {"a": LaneState(), "b": LaneState(), "c": LaneState()}
    # max-min utilization first: b (1M/20M) wins across both candidates
    assert select_lane(providers, ["m1", "m2"], debt, lanes) == ("b", "m2")
    # equal utilization -> idle lane wins over the busy one, then config order
    busy = {"a": LaneState(in_flight=2), "c": LaneState(in_flight=0)}
    tied = {"a": ProviderDebt(tokens=100), "c": ProviderDebt(tokens=100, requests=2)}
    assert select_lane(providers, ["m1"], tied, busy) == ("c", "m1")


def test_select_lane_applies_configured_max_concurrency_cap() -> None:
    provider = _pc("a", "m1", rpm=60, max_concurrency=1)
    lane = LaneState(in_flight=1, rpm_allowance=60.0, max_concurrency=1)

    with pytest.raises(ValueError):
        select_lane([provider], ["m1"], {}, {"a": lane})

    lane.in_flight = 0
    assert select_lane([provider], ["m1"], {}, {"a": lane}) == ("a", "m1")


def test_select_lane_skips_capped_lanes_and_raises_when_all_are_capped() -> None:
    providers = [_pc("a", "m1", rpm=1), _pc("b", "m2")]
    lanes = {"a": LaneState(in_flight=1, rpm_allowance=1.0), "b": LaneState()}
    # a's lane is full (1 in flight, cap 1): only b can serve
    assert select_lane(providers, ["m1", "m2"], {}, lanes) == ("b", "m2")
    # both lanes full -> no provider with a spare lane
    full = {"a": LaneState(in_flight=1, rpm_allowance=1.0), "b": LaneState(in_flight=60)}
    with pytest.raises(ValueError):
        select_lane(providers, ["m1", "m2"], {}, full)
    # without any lane state every provider is assumed available (back-compat)
    assert select_lane(providers, ["m1"], {}, {}) == ("a", "m1")


def test_partial_lane_map_fails_closed_for_unknown_provider() -> None:
    providers = [_pc("a", "m1"), _pc("b", "m1")]
    lanes = {"a": LaneState()}

    assert select_lane(providers, ["m1"], {}, lanes) == ("a", "m1")
    lanes["a"].in_flight = lanes["a"].effective_in_flight_cap(0)
    with pytest.raises(ValueError):
        select_lane(providers, ["m1"], {}, lanes)


def test_effective_in_flight_cap_decays_with_429_pressure() -> None:
    assert LaneState(rpm_allowance=60.0).effective_in_flight_cap(0) == 60
    # decay = max(0.5, 1 - 25/50) = 0.5 -> cap 30
    assert LaneState(rpm_allowance=60.0).effective_in_flight_cap(25) == 30
    assert LaneState(rpm_allowance=60.0, max_concurrency=1).effective_in_flight_cap(0) == 1
    # the decay floor keeps a pressured lane open at one task
    assert LaneState(rpm_allowance=60.0).effective_in_flight_cap(1000) == 30
    # a tiny rpm can never drop below one in-flight slot
    assert LaneState(rpm_allowance=1.0).effective_in_flight_cap(25) == 1


def test_select_lane_applies_429_backpressure_from_debt() -> None:
    providers = [_pc("a", "m1", rpm=3), _pc("b", "m2", rpm=3)]
    debt = {
        "a": ProviderDebt(retry_after_count=25),  # cap floor(3 * 0.5) = 1
        "b": ProviderDebt(),                      # cap 3
    }
    lanes = {"a": LaneState(rpm_allowance=3.0), "b": LaneState(rpm_allowance=3.0)}
    # a's 429-decayed cap is one slot; once it is full only b can serve
    lanes["a"].in_flight = 1
    picks = [select_lane(providers, ["m1", "m2"], debt, lanes)[0] for _ in range(4)]
    assert picks == ["b"] * 4


# --------------------------------------------------------------------------- #
# 2. batch pre-assignment: one wave spreads across providers (fixes C)
# --------------------------------------------------------------------------- #


def test_batch_preassignment_spreads_across_equal_debt_providers(tmp_path) -> None:
    config_path = _config_file(tmp_path / "providers.json", [("a", "m1", 60), ("b", "m2", 60)])
    specs = [_spec(f"t-{i}", config_path) for i in range(4)]
    lanes: dict[str, LaneState] = {}

    _preassign_lanes(specs, _equal_debt("a", "b"), lanes)

    assigned = [spec["assigned_provider"] for spec in specs]
    assert assigned.count("a") == 2
    assert assigned.count("b") == 2
    # alternating picks (config-order tiebreak on the first pick)
    assert assigned == ["a", "b", "a", "b"]
    # each task carries the provider's model, and lanes hold the wave's counts
    for spec in specs:
        expected = "m1" if spec["assigned_provider"] == "a" else "m2"
        assert spec["fanout_config"]["model"] == expected
    assert lanes["a"].in_flight == 2
    assert lanes["b"].in_flight == 2


def test_batch_preassignment_respects_full_lane_cap(tmp_path) -> None:
    config_path = _config_file(tmp_path / "providers.json", [("a", "m1", 1), ("b", "m2", 60)])
    specs = [_spec(f"t-{i}", config_path) for i in range(4)]
    lanes = {"a": LaneState(in_flight=1, rpm_allowance=1.0), "b": LaneState()}

    _preassign_lanes(specs, _equal_debt("a", "b"), lanes)

    # a's lane was already full before the batch: every task goes to b
    assert [spec["assigned_provider"] for spec in specs] == ["b"] * 4
    assert lanes["a"].in_flight == 1
    assert lanes["b"].in_flight == 4


def test_batch_preassignment_429_pressure_reduces_admissions(tmp_path) -> None:
    config_path = _config_file(tmp_path / "providers.json", [("a", "m1", 3), ("b", "m2", 3)])
    specs = [_spec(f"t-{i}", config_path) for i in range(4)]
    debt = {
        "a": ProviderDebt(retry_after_count=25),  # cap floor(3 * 0.5) = 1
        "b": ProviderDebt(),                      # cap 3
    }
    lanes: dict[str, LaneState] = {}

    _preassign_lanes(specs, debt, lanes)

    # the pressured provider admits only one task; the clean provider takes
    # the rest
    assigned = [spec["assigned_provider"] for spec in specs]
    assert assigned.count("a") == 1
    assert assigned.count("b") == 3
    assert lanes["a"].in_flight == 1
    assert lanes["b"].in_flight == 3


def test_batch_preassignment_skips_pinned_and_no_fanout_tasks(tmp_path) -> None:
    config_path = _config_file(tmp_path / "providers.json", [("a", "m1", 60)])
    pinned = _spec("t-pinned", config_path)
    pinned["fanout_config"] = {"tier": "fast", "model": "m1"}
    no_fanout = {"task_id": "t-nofanout", "provider_config_path": str(config_path)}
    specs = [pinned, _spec("t-free", config_path), no_fanout]
    lanes: dict[str, LaneState] = {}

    _preassign_lanes(specs, _equal_debt("a"), lanes)

    assert pinned.get("assigned_provider") is None
    assert no_fanout.get("assigned_provider") is None
    assert specs[1]["assigned_provider"] == "a"
    assert lanes["a"].in_flight == 1


# --------------------------------------------------------------------------- #
# 3. lane release: reservations return to 0 on task completion
# --------------------------------------------------------------------------- #


def test_release_lane_returns_in_flight_to_zero_and_noops_without_reservation() -> None:
    lanes = {"a": LaneState(in_flight=1), "b": LaneState()}
    spec = {
        "task_id": "t", "assigned_provider": "a", "fanout_config": {"model": "m1"},
        "_lane_reserved": True,
    }

    _release_lane(lanes, spec)
    assert lanes["a"].in_flight == 0
    assert spec["_lane_reserved"] is False
    # a task without an assignment never held a reservation
    _release_lane(lanes, {"task_id": "t2"})
    assert lanes["b"].in_flight == 0
    # after release, admission picks the freed lane again
    providers = [_pc("a", "m1"), _pc("b", "m2")]
    assert select_lane(providers, ["m1", "m2"], {}, lanes) == ("a", "m1")
    # a pinned task that never booked a lane must not decrement one
    pinned = {"task_id": "t3", "assigned_provider": "a", "_lane_reserved": False}
    lanes["a"].in_flight += 1
    _release_lane(lanes, pinned)
    assert lanes["a"].in_flight == 1


# --------------------------------------------------------------------------- #
# 4. ledger folding: cache hits and latency (recorded for H2)
# --------------------------------------------------------------------------- #


def test_provider_debt_folds_cache_hits_and_latency() -> None:
    debt = ProviderDebt()
    debt.record({"provider": "a", "provider_cache_hit": True, "latency_s": 1.5})
    debt.record({"provider": "a", "provider_cache_hit": False, "latency_s": 0.5})
    debt.record({"provider": "a"})  # no cache field, no latency field
    assert debt.cache_hit_count == 1
    assert debt.latency_total_s == 2.0
    assert debt.latency_count == 2
    # a bogus latency value is ignored, not folded
    debt.record({"provider": "a", "latency_s": -3})
    assert debt.latency_total_s == 2.0
    assert debt.latency_count == 2


def test_debt_store_round_trips_cache_and_latency_fields(tmp_path) -> None:
    path = tmp_path / "routing-state.json"
    store = DebtStore(path)
    store.record({"provider": "a", "provider_cache_hit": True, "latency_s": 1.5})
    store.save()

    loaded = DebtStore(path)
    loaded.load()
    entry = loaded.as_mapping()["a"]
    assert entry.cache_hit_count == 1
    assert entry.latency_total_s == 1.5
    assert entry.latency_count == 1


def test_debt_store_persists_and_clears_quarantine_record(tmp_path) -> None:
    path = tmp_path / "routing.json"
    store = DebtStore(path)
    store.record(
        {
            "provider": "p1",
            "usage": {"total_tokens": 10},
            "failure_reason": "config_error: The model x was not found",
        }
    )
    assert store.as_mapping()["p1"].disable_reason == "config_error: The model x was not found"
    assert store.as_mapping()["p1"].disable_at is not None
    store.save()

    loaded = DebtStore(path)
    loaded.load()
    entry = loaded.as_mapping()["p1"]
    assert entry.disable_reason == "config_error: The model x was not found"
    assert entry.disable_at is not None

    # an auth_error failure reason also sets the record
    store.record({"provider": "p1", "failure_reason": "auth_error: credential rejected"})
    store.save()
    loaded = DebtStore(path)
    loaded.load()
    entry = loaded.as_mapping()["p1"]
    assert entry.disable_reason == "auth_error: credential rejected"
    assert entry.disable_at is not None

    # a success event (no failure_reason) clears both fields on the reloaded store
    store.record({"provider": "p1", "usage": {"total_tokens": 5}})
    store.save()
    loaded = DebtStore(path)
    loaded.load()
    entry = loaded.as_mapping()["p1"]
    assert entry.disable_reason is None
    assert entry.disable_at is None

    # a transient error leaves a previously-set record untouched
    store.record({"provider": "p1", "failure_reason": "config_error: model not found again"})
    store.save()
    loaded = DebtStore(path)
    loaded.load()
    entry = loaded.as_mapping()["p1"]
    assert entry.disable_reason == "config_error: model not found again"
    loaded.record({"provider": "p1", "failure_reason": "error: transport failed"})
    assert loaded.as_mapping()["p1"].disable_reason == "config_error: model not found again"
    assert loaded.as_mapping()["p1"].disable_at is not None
    loaded.save()
    reloaded = DebtStore(path)
    reloaded.load()
    assert reloaded.as_mapping()["p1"].disable_reason == "config_error: model not found again"


# --------------------------------------------------------------------------- #
# 5. regression: select_primary keeps its contract alongside select_lane
# --------------------------------------------------------------------------- #


def test_select_primary_unchanged_contract() -> None:
    providers = [_pc("a", "m1"), _pc("b", "m2")]
    debt = {"a": ProviderDebt(tokens=100), "b": ProviderDebt(tokens=100, requests=2)}
    assert select_primary(providers, ["m1"], debt) == ("a", "m1")
