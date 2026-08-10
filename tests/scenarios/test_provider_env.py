"""Smoke tests for provider configuration against environment key presence.

The provider landscape records environment-variable names only. This module
uses those names, plus valid absent names for identities whose documented
credentials are inline, OAuth-backed, or absent. It never reads or prints a
credential value.
"""

from __future__ import annotations

import copy
import json
import os
from pathlib import Path

import pytest

diffundo = pytest.importorskip("cambium.diffundo")

from cambium.auth import derived_env_name  # noqa: E402
from cambium.provider_config import DEFAULT_SAMPLE, env_report, load_providers  # noqa: E402

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
_KNOWN_PRESENT_ENV_KEY = "CAMBIUM_PROVIDER_GOOGLE_API_KEY"
_KEY_NAME_PARTS = ("API", "KEY", "TOKEN")


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


def _credential_env_items() -> tuple[tuple[str, str], ...]:
    return tuple(
        sorted(
            (name, value)
            for name, value in os.environ.items()
            if value and any(part in name.upper() for part in _KEY_NAME_PARTS)
        )
    )


def _report_text(report: dict[str, bool]) -> str:
    return "".join((*report.keys(), *(str(value) for value in report.values())))


def test_provider_landscape_config_loads_and_validates(tmp_path: Path) -> None:
    providers = load_providers(_write_config(tmp_path / "providers.json", _landscape_config()))

    assert [provider.name for provider in providers] == [
        name for name, _ in _LANDSCAPE_PROVIDER_KEYS
    ]
    assert [provider.api_key_env for provider in providers] == [
        api_key_env for _, api_key_env in _LANDSCAPE_PROVIDER_KEYS
    ]
    assert all(isinstance(provider, diffundo.ProviderConfig) for provider in providers)


def test_env_report_is_boolean_presence_only(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv(_KNOWN_PRESENT_ENV_KEY, "secret-value-that-must-not-be-reported")
    providers = load_providers(_write_config(tmp_path / "providers.json", _landscape_config()))
    report = env_report(providers)

    provider_names = {name for name, _ in _LANDSCAPE_PROVIDER_KEYS}
    assert set(report) == provider_names
    assert all(type(present) is bool for present in report.values())
    assert report == {
        provider.name: bool(os.environ.get(provider.api_key_env)) for provider in providers
    }

    assert bool(os.environ.get(_KNOWN_PRESENT_ENV_KEY)), (
        f"expected {_KNOWN_PRESENT_ENV_KEY} to be present on this machine"
    )
    assert report["google"] is bool(os.environ.get(_KNOWN_PRESENT_ENV_KEY))

    report_text = _report_text(report)
    leaked_names = [
        name for name, value in _credential_env_items() if value in report_text
    ]
    assert leaked_names == [], f"env_report exposed a value for {leaked_names!r}"


def test_env_report_treats_empty_key_as_unconfigured(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    env_name = derived_env_name("opencode-go")
    monkeypatch.setenv(env_name, "")

    providers = load_providers(_write_config(tmp_path / "providers.json", _landscape_config()))

    assert env_report(providers)["opencode-go"] is False


def test_missing_api_key_env_loads_but_reports_false(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    missing_env_name = derived_env_name("missing-key")
    monkeypatch.delenv(missing_env_name, raising=False)

    config = copy.deepcopy(DEFAULT_SAMPLE)
    provider = copy.deepcopy(config["providers"][0])
    provider.update(name="missing-key", api_key_env=missing_env_name)
    config["providers"] = [provider]

    providers = load_providers(_write_config(tmp_path / "providers.json", config))

    assert [provider.name for provider in providers] == ["missing-key"]
    assert env_report(providers) == {"missing-key": False}
