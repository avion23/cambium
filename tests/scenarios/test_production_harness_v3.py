from __future__ import annotations

import json
from pathlib import Path

from cambium.diffundo import Diffundo, ProviderConfig, ProviderTier
from cambium.provider_config import load_providers
from cambium.schemas import TOOL_SCHEMAS


def _tool_names() -> set[str]:
    names = set()
    for item in TOOL_SCHEMAS:
        function = item.get("function") if isinstance(item, dict) else None
        if isinstance(function, dict):
            names.add(function.get("name"))
        elif isinstance(item, dict):
            names.add(item.get("name"))
    return {name for name in names if isinstance(name, str)}


def test_run_python_is_a_portable_structured_tool() -> None:
    assert "run_python" in _tool_names()


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
    assert len(provider.quota_windows) == 2
    assert provider.quota_windows[0].name == "five-hour"


def test_diffundo_lease_filters_candidates_to_one_continuous_branch() -> None:
    providers = [
        ProviderConfig(
            name="a",
            tier=ProviderTier.FAST,
            base_url="http://127.0.0.1:1/v1",
            api_key_env="A",
            model="m",
        ),
        ProviderConfig(
            name="b",
            tier=ProviderTier.FAST,
            base_url="http://127.0.0.1:2/v1",
            api_key_env="B",
            model="m",
        ),
    ]
    router = Diffundo(providers)
    router.bind_provider("a", "m", root_task_id="root")
    assert [item.name for item in router._candidates(ProviderTier.FAST, "m")] == ["a"]
