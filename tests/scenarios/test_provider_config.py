"""Scenario tests for the strict Diffundo provider-config loader."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from cambium.diffundo import ProviderTier
from cambium.provider_config import env_report, load_providers


def _provider(name: str = "openai", **overrides: object) -> dict[str, object]:
    value: dict[str, object] = {
        "name": name,
        "tier": "strong",
        "base_url": "https://api.example.test/v1",
        "api_key_env": "OPENAI_API_KEY",
        "timeout_s": 30.0,
        "max_retries": 2,
        "rpm": 60,
        "enabled": True,
        "model": "example-model",
        "priority": 0,
        "cooldown_s": 60.0,
        "price": 0.0,
    }
    value.update(overrides)
    return value


def _write(path: Path, providers: list[dict[str, object]]) -> Path:
    path.write_text(json.dumps({"providers": providers}), encoding="utf-8")
    return path


def test_valid_config_loads_without_key_in_environment(tmp_path: Path) -> None:
    path = _write(tmp_path / "providers.json", [_provider()])

    providers = load_providers(path)

    assert len(providers) == 1
    assert providers[0].name == "openai"
    assert providers[0].tier is ProviderTier.STRONG
    assert providers[0].api_key_env == "OPENAI_API_KEY"


def test_invalid_tier_is_rejected(tmp_path: Path) -> None:
    path = _write(tmp_path / "providers.json", [_provider(tier="premium")])

    with pytest.raises(ValueError, match="invalid tier"):
        load_providers(path)


def test_unknown_field_is_rejected(tmp_path: Path) -> None:
    path = _write(tmp_path / "providers.json", [_provider(timeuot_s=30.0)])

    with pytest.raises(ValueError, match="unknown field.*timeuot_s"):
        load_providers(path)


def test_duplicate_name_is_rejected(tmp_path: Path) -> None:
    path = _write(tmp_path / "providers.json", [_provider(), _provider("openai")])

    with pytest.raises(ValueError, match="duplicate provider name"):
        load_providers(path)


def test_env_report_only_returns_presence_booleans(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    path = _write(
        tmp_path / "providers.json",
        [
            _provider("present", api_key_env="PRESENT_KEY"),
            _provider("missing", api_key_env="MISSING_KEY"),
        ],
    )
    monkeypatch.setenv("PRESENT_KEY", "secret-value-that-must-not-be-reported")
    monkeypatch.delenv("MISSING_KEY", raising=False)

    providers = load_providers(path)

    assert env_report(providers) == {"present": True, "missing": False}


def test_default_path_missing_has_clear_error(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.chdir(tmp_path)
    monkeypatch.delenv("CAMBIUM_PROVIDERS", raising=False)

    with pytest.raises(
        FileNotFoundError,
        match=r"provider config file not found.*\.cambium/providers\.json",
    ):
        load_providers()


def test_non_positive_rpm_is_rejected(tmp_path: Path) -> None:
    path = _write(tmp_path / "providers.json", [_provider(rpm=0)])

    with pytest.raises(ValueError, match="rpm.*greater than 0"):
        load_providers(path)
