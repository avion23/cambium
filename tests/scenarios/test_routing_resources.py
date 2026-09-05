"""Observed quota/cooldown and actual lane release drive admission."""
from __future__ import annotations

import asyncio
import json
import time
from types import SimpleNamespace

from cambium.diffundo import ProviderConfig, ProviderTier
from cambium.provider_scheduler import QuotaWindowSnapshot
from cambium.routing import DebtStore, LaneState, resolve_assignment
from cambium.supervisor import _release_lane, _Runtime


def _provider(name):
    return ProviderConfig(
        name=name, tier=ProviderTier.FAST, model=name, base_url="http://127.0.0.1:1",
        api_key_env="", max_in_flight=1, requests_per_minute=120,
    )


def test_quota_expiry_and_cooldown_use_observations_not_decayed_debt(tmp_path):
    now = time.time()
    providers = [_provider("a"), _provider("b")]
    lanes = {p.name: LaneState.from_provider(p) for p in providers}
    windows = [QuotaWindowSnapshot("a", "week", now + 300, 1000, 1000, 0, 0, 0.0)]
    assignment = resolve_assignment(providers, ["a", "b"], {}, lanes, quota_windows=windows)
    assert assignment.provider == "b"
    expired = [QuotaWindowSnapshot("a", "week", now - 1, 1000, 1000, 0, 0, 0.0)]
    assignment = resolve_assignment(providers, ["a", "b"], {}, lanes, quota_windows=expired)
    assert assignment.provider == "a"
    store = DebtStore(tmp_path / "debt.json")
    store.record({"provider": "a", "failure_reason": "quota: HTTP 429", "retry_after_s": 30})
    store.save()
    reloaded = DebtStore(tmp_path / "debt.json")
    reloaded.load()
    assert reloaded.as_mapping()["a"].retry_at > now
    assert resolve_assignment(providers, ["a", "b"], reloaded.as_mapping(), lanes).provider == "b"
    # A zero-price provider is still unavailable while its actual token window is exhausted.
    assert resolve_assignment(providers[:1], ["a"], {}, lanes, quota_windows=windows) is None
    # With comparable pressure, consume allowance that renews sooner.
    windows = [QuotaWindowSnapshot(p.name, "week", now + seconds, 1000, 100, 0, 0, 0.0)
               for p, seconds in zip(providers, [600, 60], strict=True)]
    assignment = resolve_assignment(providers, ["a", "b"], {}, lanes, quota_windows=windows)
    assert assignment.provider == "b"


def test_serving_provider_moves_the_reservation_once(tmp_path):
    config = tmp_path / "providers.json"
    config.write_text(json.dumps({"providers": [{
        "name": name, "model": name, "tier": "fast", "auth": "none",
        "base_url": "http://127.0.0.1:1", "max_in_flight": 1,
    } for name in ("a", "b")]}))

    async def exercise():
        runtime = _Runtime(tmp_path, None, debt_store=DebtStore(tmp_path / "debt.json"))
        runtime._lanes = {name: LaneState.from_provider(_provider(name)) for name in ("a", "b")}
        runtime._lanes["a"].in_flight = 1
        spec = {"task_id": "task", "assigned_provider": "a", "_lane_reserved": True,
                "fanout_config": {"model": "a", "tier": "fast"},
                "provider_config_path": str(config)}
        state = SimpleNamespace(task_id="task", generation=1, spec=spec)
        event = {"type": "usage_event", "task_id": "task", "generation": 1,
                 "provider": "b", "model": "b", "turn": 1,
                 "usage": {"prompt_tokens": 2, "completion_tokens": 3, "total_tokens": 5}}
        await runtime._handle_usage_event_message(state, event)
        assert spec["assigned_provider"] == spec["fanout_config"]["model"] == "b"
        assert runtime._lanes["a"].in_flight == 0
        assert runtime._lanes["b"].in_flight == 1
        await runtime._handle_usage_event_message(state, {**event, "turn": 2})
        assert runtime._lanes["b"].in_flight == 1
        await runtime._handle_usage_event_message(state, {
            **event, "provider": "a", "failure_reason": "quota: HTTP 429", "retry_after_s": 5,
        })
        assert spec["assigned_provider"] == "b"
        assert runtime._lanes["a"].in_flight == 0

    asyncio.run(exercise())


def test_busy_lane_queues_until_release_without_starting_another_worker(tmp_path):
    config = tmp_path / "providers.json"
    config.write_text(json.dumps({"providers": [{
        "name": "single", "model": "single", "tier": "fast", "auth": "none",
        "base_url": "http://127.0.0.1:1", "max_in_flight": 1, "requests_per_minute": 120,
    }]}))

    async def exercise():
        queued = asyncio.Event()
        events = []

        def observe(event):
            events.append(event)
            if event["kind"] == "task_queued":
                queued.set()

        runtime = _Runtime(tmp_path, None, debt_store=DebtStore(tmp_path / "routing.json"),
                           on_event=observe)
        spec = {"task_id": "first", "fanout_config": {}, "model_candidates": ["single"],
                "authorized_providers": ["single"], "provider_config_path": str(config)}
        second = {**spec, "task_id": "second", "fanout_config": {}}
        runtime._resolve_assignment(spec)
        assert runtime._lanes["single"].in_flight == 1
        waiter = asyncio.create_task(runtime._await_assignment(second, time.monotonic() + 2))
        try:
            await asyncio.wait_for(queued.wait(), 1)
            assert not waiter.done()
            assert "assigned_provider" not in second
            _release_lane(runtime._lanes, spec)
            runtime._lane_changed.set()
            await asyncio.wait_for(waiter, 1)
            assert second["assigned_provider"] == "single"
            assert runtime._lanes["single"].in_flight == 1
            assert [e["kind"] for e in events] == ["task_queued"]
        finally:
            waiter.cancel()
            await asyncio.gather(waiter, return_exceptions=True)

    asyncio.run(exercise())
