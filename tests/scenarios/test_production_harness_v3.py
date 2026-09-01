"""Scenario coverage for subscription resource dimensions in provider config."""

from __future__ import annotations

import json
from pathlib import Path

from cambium.provider_config import load_providers


def test_provider_config_loads_subscription_resource_dimensions(tmp_path: Path) -> None:
    path = tmp_path / "providers.json"
    path.write_text(
        json.dumps(
            {
                "providers": [
                    {
                        "name": "zai",
                        "tier": "fast",
                        "base_url": "https://example.com/v1",
                        "api_key_env": "CAMBIUM_PROVIDER_ZAI_API_KEY",
                        "api_key": "sk-test-zai",
                        "model": "glm",
                        "rpm": 100,
                        "max_concurrency": 3,
                        "billing_mode": "subscription",
                        "pricing_known": True,
                        "price_per_1m_in": 0,
                        "price_per_1m_cached_in": 0,
                        "price_per_1m_out": 0,
                        "throughput_hint_tps": 40,
                        "quota_windows": [
                            {
                                "name": "five-hour",
                                "duration_s": 18000,
                                "token_allowance": 1000000,
                                "reserve_fraction": 0.05,
                            },
                            {
                                "name": "weekly",
                                "duration_s": 604800,
                                "token_allowance": 5000000,
                            },
                        ],
                    }
                ]
            }
        ),
        encoding="utf-8",
    )

    provider = load_providers(path)[0]

    assert provider.rpm == 100
    assert provider.max_concurrency == 3
    assert [window.name for window in provider.quota_windows] == ["five-hour", "weekly"]
