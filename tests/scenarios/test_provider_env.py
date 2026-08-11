"""Smoke tests for provider configuration against environment key presence.

The provider landscape records environment-variable names only. This module
uses those names, plus valid absent names for identities whose documented
credentials are inline, OAuth-backed, or absent. It never reads or prints a
credential value.
"""

from __future__ import annotations

import copy
import json
from pathlib import Path

import pytest

diffundo = pytest.importorskip("cambium.diffundo")

from cambium.auth import derived_env_name  # noqa: E402
from cambium.provider_config import DEFAULT_SAMPLE, load_providers  # noqa: E402

# Every provider uses the canonical env name derived from its provider id.
_LANDSCAPE_PROVIDER_KEYS: tuple[tuple[str, str], ...] = tuple(
    (name, derived_env_name(name))
    for name in (
        "openai",
        "opencode-go",
        "google",
        "zai-coding-plan",
        "kimi-for-coding",
        "micu-free2",
        "micu-vip2",
        "openrouter",
        "nvidia",
        "tokenrouter",
        "groq",
        "llama-cpp",
        "zenmux",
    )
)


def _landscape_config() -> dict[str, list[dict[str, object]]]:
    """Build the landscape config by extending the DEFAULT_SAMPLE shape."""
    config = copy.deepcopy(DEFAULT_SAMPLE)
    providers: list[dict[str, object]] = []
    for priority, (name, api_key_env) in enumerate(_LANDSCAPE_PROVIDER_KEYS):
        template_name = "llama-cpp" if name == "llama-cpp" else "openai"
        provider = copy.deepcopy(
            next(entry for entry in DEFAULT_SAMPLE["providers"] if entry["name"] == template_name)
        )
        provider.update(name=name, api_key_env=api_key_env, priority=priority)
        providers.append(provider)
    config["providers"] = providers
    return config


def _write_config(path: Path, config: dict[str, list[dict[str, object]]]) -> Path:
    path.write_text(json.dumps(config), encoding="utf-8")
    return path


def test_provider_landscape_config_loads_and_validates(tmp_path: Path) -> None:
    providers = load_providers(_write_config(tmp_path / "providers.json", _landscape_config()))

    assert [provider.name for provider in providers] == [
        name for name, _ in _LANDSCAPE_PROVIDER_KEYS
    ]
    assert [provider.api_key_env for provider in providers] == [
        api_key_env for _, api_key_env in _LANDSCAPE_PROVIDER_KEYS
    ]
    assert all(isinstance(provider, diffundo.ProviderConfig) for provider in providers)
