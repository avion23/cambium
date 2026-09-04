"""Independent provider rate, concurrency, and measured-throughput scenarios."""

from __future__ import annotations

import json
import time
from pathlib import Path
from typing import Any

import pytest

from cambium.diffundo import ProviderConfig, ProviderTier
from cambium.provider_config import load_providers
from cambium.routing import (
    DebtStore,
    LaneCapacityExhausted,
    LaneState,
    ProviderDebt,
    score_providers,
    select_lane,
)


def _provider(name: str, model: str, **overrides: Any) -> ProviderConfig:
    values: dict[str, Any] = {
        "tier": ProviderTier.FAST,
        "base_url": "http://127.0.0.1:1",
        "api_key_env": f"CAMBIUM_PROVIDER_{name.upper()}_API_KEY",
        "api_key": f"sk-throughput-{name}",
        "model": model,
    }
    values.update(overrides)
    return ProviderConfig(name=name, **values)


def _config_provider(name: str, model: str, **overrides: Any) -> dict[str, object]:
    values: dict[str, object] = {
        "name": name,
        "tier": "fast",
        "base_url": "http://127.0.0.1:1",
        "api_key_env": f"CAMBIUM_PROVIDER_{name.upper()}_API_KEY",
        "api_key": f"sk-throughput-{name}",
        "model": model,
    }
    values.update(overrides)
    return values


def test_legacy_rpm_derives_conservative_in_flight_capacity(tmp_path: Path) -> None:
    path = tmp_path / "providers.json"
    path.write_text(
        json.dumps({"providers": [_config_provider("legacy", "m", rpm=120)]}),
        encoding="utf-8",
    )

    provider = load_providers(path)[0]
    lane = LaneState.from_provider(provider)

    assert provider.requests_per_minute == 120
    assert provider.max_in_flight == 1
    assert lane.effective_in_flight_cap(0) == 1
    assert lane.effective_request_slot_cap(0) == 120


def test_rate_and_in_flight_slots_are_independent() -> None:
    burst = _provider("burst", "m1", requests_per_minute=2, max_in_flight=8)
    serial = _provider("serial", "m2", requests_per_minute=60, max_in_flight=1)
    lanes = {
        burst.name: LaneState.from_provider(burst),
        serial.name: LaneState.from_provider(serial),
    }

    assert select_lane([burst, serial], ["m1", "m2"], {}, lanes) == ("burst", "m1")
    assert lanes["burst"].reserve()
    assert lanes["burst"].effective_in_flight_cap(0) == 8
    # The second request can still start concurrently, but the request-rate
    # bucket is then exhausted even though six in-flight slots remain.
    assert lanes["burst"].reserve()
    assert lanes["burst"].in_flight == 2
    assert not lanes["burst"].reserve()

    # The low-concurrency provider has ample request rate but only one
    # simultaneous slot, so its second admission is rejected for a different
    # reason than burst's rate exhaustion.
    assert lanes["serial"].reserve()
    assert lanes["serial"].request_slots is not None and lanes["serial"].request_slots > 1
    assert not lanes["serial"].reserve()
    lanes["serial"].release()
    lanes["burst"].release()
    lanes["burst"].release()
    assert select_lane([burst, serial], ["m1", "m2"], {}, lanes) == ("serial", "m2")

    # Once serial is occupied, burst is still unavailable because its request
    # tokens have not replenished; both dimensions participate in admission.
    lanes["serial"].reserve()
    with pytest.raises(LaneCapacityExhausted):
        select_lane([burst, serial], ["m1", "m2"], {}, lanes)


def test_provider_debt_records_and_persists_measured_tokens_per_second(
    tmp_path: Path,
) -> None:
    debt = ProviderDebt()
    debt.record(
        {
            "provider": "fast",
            "usage": {"input_tokens": 100, "completion_tokens": 50},
            "latency_s": 5.0,
        },
        now=100.0,
    )
    debt.record(
        {
            "provider": "fast",
            "usage": {"output_tokens": 300, "total_tokens": 300},
            "latency_s": 10.0,
        },
        now=101.0,
    )

    assert debt.tokens == 450
    assert debt.tokens_per_s_count == 2
    assert debt.tokens_per_s == pytest.approx(20.0)

    path = tmp_path / "routing-state.json"
    store = DebtStore(path)
    store.record(
        {
            "provider": "fast",
            "usage": {"completion_tokens": 50},
            "latency_s": 5.0,
        }
    )
    store.save()
    loaded = DebtStore(path)
    loaded.load()
    restored = loaded.as_mapping()["fast"]
    assert restored.tokens_per_s == pytest.approx(10.0)
    assert restored.tokens_per_s_count == 1


def test_equal_priority_and_cost_prefers_measured_faster_provider() -> None:
    now = time.time()
    providers = [_provider("slow", "m1"), _provider("fast", "m2")]
    debt = {
        "slow": ProviderDebt(
            requests=10,
            cost=1.0,
            latency_total_s=20.0,
            latency_count=10,
            tokens_per_s=10.0,
            last_seen=now,
        ),
        "fast": ProviderDebt(
            requests=10,
            cost=1.0,
            latency_total_s=20.0,
            latency_count=10,
            tokens_per_s=100.0,
            last_seen=now,
        ),
    }

    scored = score_providers(providers, ["m1", "m2"], debt)

    assert [name for name, _model, _rank in scored] == ["fast", "slow"]
