"""Observed quota/cooldown and actual lane release drive admission."""

from __future__ import annotations

import asyncio
import json
import time
from types import SimpleNamespace

import pytest

from cambium.diffundo import ProviderConfig, ProviderTier
from cambium.provider_scheduler import QuotaWindowSnapshot
from cambium.routing import DebtStore, LaneState, resolve_assignment
from cambium.supervisor import _release_lane, _Runtime


def _provider(name):
    return ProviderConfig(
        name=name,
        tier=ProviderTier.FAST,
        model=name,
        base_url="http://127.0.0.1:1",
        api_key_env="",
        max_in_flight=1,
        requests_per_minute=120,
    )


@pytest.mark.parametrize("requirements", [None, {"needs_python_tool": True}])
def test_quota_expiry_and_cooldown_use_observations_not_decayed_debt(tmp_path, requirements):
    now = time.time()
    providers = [_provider("a"), _provider("b")]
    lanes = {p.name: LaneState.from_provider(p) for p in providers}
    windows = [QuotaWindowSnapshot("a", "week", now + 300, 1000, 1000, 0, 0, 0.0)]
    assignment = resolve_assignment(
        providers,
        ["a", "b"],
        {},
        lanes,
        quota_windows=windows,
        requirements=requirements,
    )
    assert assignment.provider == "b"
    expired = [QuotaWindowSnapshot("a", "week", now - 1, 1000, 1000, 0, 0, 0.0)]
    assignment = resolve_assignment(
        providers,
        ["a", "b"],
        {},
        lanes,
        quota_windows=expired,
        requirements=requirements,
    )
    assert assignment.provider == "a"
    store = DebtStore(tmp_path / "debt.json")
    store.record({"provider": "a", "failure_reason": "quota: HTTP 429", "retry_after_s": 30})
    store.save()
    reloaded = DebtStore(tmp_path / "debt.json")
    reloaded.load()
    assert reloaded.as_mapping()["a"].retry_at > now
    assert (
        resolve_assignment(
            providers,
            ["a", "b"],
            reloaded.as_mapping(),
            lanes,
            requirements=requirements,
        ).provider
        == "b"
    )
    # A zero-price provider is still unavailable while its actual token window is exhausted.
    assert (
        resolve_assignment(
            providers[:1],
            ["a"],
            {},
            lanes,
            quota_windows=windows,
            requirements=requirements,
        )
        is None
    )
    # With comparable pressure, consume allowance that renews sooner.
    windows = [
        QuotaWindowSnapshot(p.name, "week", now + seconds, 1000, 100, 0, 0, 0.0)
        for p, seconds in zip(providers, [600, 60], strict=True)
    ]
    assignment = resolve_assignment(
        providers,
        ["a", "b"],
        {},
        lanes,
        quota_windows=windows,
        requirements=requirements,
    )
    assert assignment.provider == "b"


def test_serving_provider_moves_reservation_only_with_fallback_provenance(tmp_path):
    config = tmp_path / "providers.json"
    config.write_text(
        json.dumps(
            {
                "providers": [
                    {
                        "name": name,
                        "model": name,
                        "tier": "fast",
                        "auth": "none",
                        "base_url": "http://127.0.0.1:1",
                        "max_in_flight": 1,
                    }
                    for name in ("a", "b")
                ]
            }
        )
    )

    async def exercise():
        runtime = _Runtime(tmp_path, None, debt_store=DebtStore(tmp_path / "debt.json"))
        runtime._lanes = {name: LaneState.from_provider(_provider(name)) for name in ("a", "b")}
        runtime._lanes["a"].in_flight = 1
        spec = {
            "task_id": "task",
            "assigned_provider": "a",
            "_lane_reserved": True,
            "fanout_config": {"model": "a", "tier": "fast"},
            "provider_config_path": str(config),
        }
        state = SimpleNamespace(task_id="task", generation=1, spec=spec)
        event = {
            "type": "usage_event",
            "task_id": "task",
            "generation": 1,
            "provider": "b",
            "model": "b",
            "call_kind": "agent",
            "turn": 1,
            "usage": {"prompt_tokens": 2, "completion_tokens": 3, "total_tokens": 5},
        }
        await runtime._handle_usage_event_message(state, event)
        assert spec["assigned_provider"] == spec["fanout_config"]["model"] == "a"
        assert runtime._lanes["a"].in_flight == 1
        assert runtime._lanes["b"].in_flight == 0
        await runtime._handle_usage_event_message(
            state, {**event, "turn": 2, "fell_back_from": "a"}
        )
        assert spec["assigned_provider"] == spec["fanout_config"]["model"] == "b"
        assert runtime._lanes["a"].in_flight == 0
        assert runtime._lanes["b"].in_flight == 1
        _release_lane(runtime._lanes, spec)
        assert runtime._lanes["b"].in_flight == 0
        await runtime._handle_usage_event_message(state, {**event, "turn": 3})
        assert spec["assigned_provider"] == "b"
        assert runtime._lanes["a"].in_flight == 0
        assert runtime._lanes["b"].in_flight == 1
        await runtime._handle_usage_event_message(
            state,
            {
                **event,
                "provider": "a",
                "failure_reason": "quota: HTTP 429",
                "retry_after_s": 5,
            },
        )
        assert spec["assigned_provider"] == "b"
        assert runtime._lanes["a"].in_flight == 0

    asyncio.run(exercise())


def test_summary_serving_provider_does_not_move_coding_reservation(tmp_path):
    config = tmp_path / "providers.json"
    config.write_text(
        json.dumps(
            {
                "providers": [
                    {
                        "name": "coding",
                        "model": "coding-model",
                        "tier": "fast",
                        "auth": "none",
                        "base_url": "http://127.0.0.1:1",
                        "max_in_flight": 1,
                    },
                    {
                        "name": "summary",
                        "model": "summary-model",
                        "tier": "strong",
                        "auth": "none",
                        "base_url": "http://127.0.0.1:1",
                        "max_in_flight": 1,
                    },
                ]
            }
        )
    )

    async def exercise():
        events = []
        debt_store = DebtStore(tmp_path / "debt.json")
        runtime = _Runtime(
            tmp_path,
            None,
            on_event=events.append,
            debt_store=debt_store,
        )
        runtime._lanes = {
            name: LaneState.from_provider(_provider(name)) for name in ("coding", "summary")
        }
        runtime._lanes["coding"].in_flight = 1
        spec = {
            "task_id": "task",
            "assigned_provider": "coding",
            "_lane_reserved": True,
            "fanout_config": {"model": "coding-model", "tier": "fast"},
            "provider_config_path": str(config),
        }
        state = SimpleNamespace(task_id="task", generation=1, spec=spec)
        event = {
            "type": "usage_event",
            "task_id": "task",
            "generation": 1,
            "provider": "summary",
            "model": "summary-model",
            "fell_back_from": "coding",
            "call_kind": "summary",
            "turn": 1,
            "usage": {"prompt_tokens": 2, "completion_tokens": 3, "total_tokens": 5},
            "estimated_cost_usd": 0.004,
            "account_quota_owner": "acct-summary",
            "request_rate_status": "ok",
            "quota_windows": [{"provider": "summary", "name": "week"}],
        }

        await runtime._handle_usage_event_message(state, event)

        assert spec["assigned_provider"] == "coding"
        assert spec["fanout_config"] == {"model": "coding-model", "tier": "fast"}
        assert spec["_lane_reserved"] is True
        assert runtime._lanes["coding"].in_flight == 1
        assert runtime._lanes["summary"].in_flight == 0
        assert [item["kind"] for item in events] == ["usage_event"]
        payload = events[0]["payload"]
        assert payload["provider"] == "summary"
        assert payload["model"] == "summary-model"
        assert payload["fell_back_from"] == "coding"
        assert payload["call_kind"] == "summary"
        assert payload["usage"] == {"prompt_tokens": 2, "completion_tokens": 3, "total_tokens": 5}
        assert payload["estimated_cost_usd"] == 0.004
        assert payload["account_quota_owner"] == "acct-summary"
        assert payload["request_rate_status"] == "ok"
        assert payload["quota_windows"] == [{"provider": "summary", "name": "week"}]
        summary_debt = debt_store.as_mapping()["summary"]
        assert summary_debt.requests == 1
        assert summary_debt.tokens == 5
        assert summary_debt.cost == 0.004
        assert "coding" not in debt_store.as_mapping()

    asyncio.run(exercise())


def test_generation_init_carries_durable_provider_blocks(tmp_path):
    store = DebtStore(tmp_path / "debt.json")
    store.record({"provider": "auth-dead", "failure_reason": "auth_error: credential rejected"})
    store.record(
        {
            "provider": "cooling",
            "failure_reason": "quota: HTTP 429",
            "request_rate_status": "cooldown",
            "retry_after_s": 60.0,
        }
    )
    runtime = _Runtime(tmp_path, None, debt_store=store)
    spec = {
        "task_id": "task",
        "task": "test",
        "repo": str(tmp_path),
        "worktree_path": str(tmp_path / "wt"),
        "branch": "task",
        "base_commit": "a" * 40,
    }

    _request_id, init = runtime._build_generation_init_message(
        spec,
        tmp_path / "wt",
        "task",
        1,
        15.0,
        90.0,
        120.0,
    )

    auth = init["debt"]["auth-dead"]
    assert auth["disable_reason"] == "auth_error: credential rejected"
    assert isinstance(auth["disable_at"], float)
    cooling = init["debt"]["cooling"]
    assert cooling["retry_at"] > time.time()


def test_retry_after_is_durable_even_without_cooldown_status(tmp_path):
    now = time.time()
    store = DebtStore(tmp_path / "retry-after.json")
    store.record(
        {
            "provider": "a",
            "failure_reason": "error: HTTP 503 unavailable",
            "request_rate_status": "available",
            "retry_after_s": 45.0,
        }
    )
    store.save()

    reloaded = DebtStore(tmp_path / "retry-after.json")
    reloaded.load()
    debt = reloaded.as_mapping()["a"]
    assert debt.retry_after_count == 1
    assert debt.retry_at is not None
    assert debt.retry_at >= now + 45.0


def test_busy_lane_queues_until_release_without_starting_another_worker(tmp_path):
    config = tmp_path / "providers.json"
    config.write_text(
        json.dumps(
            {
                "providers": [
                    {
                        "name": "single",
                        "model": "single",
                        "tier": "fast",
                        "auth": "none",
                        "base_url": "http://127.0.0.1:1",
                        "max_in_flight": 1,
                        "requests_per_minute": 120,
                    }
                ]
            }
        )
    )

    async def exercise():
        queued = asyncio.Event()
        events = []

        def observe(event):
            events.append(event)
            if event["kind"] == "task_queued":
                queued.set()

        runtime = _Runtime(
            tmp_path, None, debt_store=DebtStore(tmp_path / "routing.json"), on_event=observe
        )
        spec = {
            "task_id": "first",
            "fanout_config": {},
            "model_candidates": ["single"],
            "authorized_providers": ["single"],
            "provider_config_path": str(config),
        }
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
