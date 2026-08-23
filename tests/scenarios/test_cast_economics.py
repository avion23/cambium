"""CAST cache capability and economics contracts."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from cambium.provider_config import load_providers
from cambium.provider_scheduler import CacheCapability, CastConfig, decide_rollover
from cambium.summary_trunk import (
    SummaryEntry,
    k0_rollover_decision,
    render_summary_message,
)


def _provider(**overrides: object) -> dict[str, object]:
    value: dict[str, object] = {
        "name": "cache-provider",
        "tier": "fast",
        "base_url": "http://127.0.0.1:1",
        "api_key_env": "CAMBIUM_PROVIDER_CACHE_PROVIDER_API_KEY",
        "model": "cache-model",
    }
    value.update(overrides)
    return value


def _write(path: Path, provider: dict[str, object]) -> Path:
    path.write_text(json.dumps({"providers": [provider]}), encoding="utf-8")
    return path


def test_cache_capability_accepts_aliases_and_rounds_cache_blocks() -> None:
    capability = CacheCapability(
        min_cacheable_block_tokens=100,
        ttl_seconds=60,
        granularity=128,
        cache_read_price_per_1m=0.02,
        cache_write_price_per_1m=0.08,
    )

    assert capability.minimum_cacheable_tokens == 100
    assert capability.ttl_s == 60.0
    assert capability.cache_granularity_tokens == 128
    assert capability.cacheable_tokens(129) == 256
    assert capability.cost(129) == pytest.approx(256 / 1_000_000 * 0.02)
    assert capability.cost(129, write=True) == pytest.approx(256 / 1_000_000 * 0.08)


def test_provider_config_loads_nested_cache_capability(tmp_path: Path) -> None:
    path = _write(
        tmp_path / "providers.json",
        _provider(
            cache_capability={
                "minimum_cacheable_tokens": 1024,
                "cache_ttl_s": 45,
                "cache_granularity_tokens": 256,
                "cache_read_price": 0.01,
                "cache_write_price": 0.04,
            }
        ),
    )

    provider = load_providers(path)[0]

    assert provider.cache_capability == CacheCapability(
        minimum_cacheable_tokens=1024,
        cache_ttl_s=45,
        cache_granularity_tokens=256,
        cache_read_price=0.01,
        cache_write_price=0.04,
    )


def test_provider_config_rejects_unknown_cache_capability_field(tmp_path: Path) -> None:
    path = _write(
        tmp_path / "providers.json",
        _provider(cache={"minimum_cacheable_tokens": 1, "unexpected": True}),
    )

    with pytest.raises(ValueError, match="unknown cache-capability field"):
        load_providers(path)


def test_rollover_decision_compares_write_with_expired_cache_reads() -> None:
    capability = CacheCapability(
        minimum_cacheable_tokens=1,
        cache_granularity_tokens=1,
        cache_read_price=0.10,
        cache_write_price=0.40,
    )
    policy = CastConfig(max_segments=1)

    decision = decide_rollover(
        policy,
        segment_count=2,
        active_trunk_tokens=10_000,
        new_prefix_tokens=3_000,
        expected_remaining_calls=100,
        cache_capability=capability,
    )

    assert decision.thresholds_hit is True
    assert decision.rollover is True
    assert decision.rollover_cost == pytest.approx(0.0012)
    assert decision.continue_cost == pytest.approx(0.1)
    assert decision.n_star == pytest.approx(1.2)
    assert decision.event(task_id="task", epoch=3)["kind"] == "cast_rollover_decision"


def test_k0_rollover_decision_emits_content_free_evidence() -> None:
    head = [
        {"role": "system", "content": "stable"},
        {"role": "user", "content": "contract"},
    ]
    entries = [
        SummaryEntry(
            "summary_entry",
            1,
            "a" * 64,
            1,
            1,
            "objective",
            "outcome",
            ("D1",),
            (),
            (),
            (),
            (),
            ("verified",),
            (),
            ("open",),
        ),
        SummaryEntry(
            "summary_entry",
            2,
            "b" * 64,
            1,
            2,
            "objective two",
            "outcome two",
            ("D2",),
            (),
            (),
            (),
            (),
            ("verified two",),
            (),
            ("open two",),
        ),
    ]
    events: list[dict[str, object]] = []
    decision = k0_rollover_decision(
        [*head, *(render_summary_message(entry) for entry in entries)],
        CastConfig(max_segments=1),
        expected_remaining_calls=100,
        cache_capability=CacheCapability(cache_read_price=0.10, cache_write_price=0.40),
        event_sink=events,
        task_id="task",
        epoch=1,
    )

    assert decision.thresholds_hit is True
    assert events[0]["kind"] == "cast_rollover_decision"
    payload = events[0]["payload"]
    assert isinstance(payload, dict)
    assert payload["task_id"] == "task"
    assert "prompt" not in payload
