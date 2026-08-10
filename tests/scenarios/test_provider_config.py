"""Scenario tests for the strict Diffundo provider-config loader."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

diffundo = pytest.importorskip("cambium.diffundo")

from cambium.auth import derived_env_name  # noqa: E402
from cambium.provider_config import env_report, load_providers  # noqa: E402


def _provider(name: str = "openai", **overrides: object) -> dict[str, object]:
    value: dict[str, object] = {
        "name": name,
        "tier": "strong",
        "base_url": "https://api.example.test/v1",
        "api_key_env": derived_env_name(name),
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
    assert providers[0].tier is diffundo.ProviderTier.STRONG
    assert providers[0].api_key_env == "CAMBIUM_PROVIDER_OPENAI_API_KEY"


def test_invalid_tier_is_rejected(tmp_path: Path) -> None:
    path = _write(tmp_path / "providers.json", [_provider(tier="premium")])

    with pytest.raises(ValueError, match="invalid tier"):
        load_providers(path)


def test_unknown_field_is_rejected(tmp_path: Path) -> None:
    path = _write(tmp_path / "providers.json", [_provider(timeuot_s=30.0)])

    with pytest.raises(ValueError, match="unknown field.*timeuot_s"):
        load_providers(path)


def test_generic_api_key_environment_name_is_rejected(tmp_path: Path) -> None:
    path = _write(tmp_path / "providers.json", [_provider(api_key_env="OPENAI_API_KEY")])

    with pytest.raises(ValueError, match="derived CAMBIUM"):
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
            _provider("present"),
            _provider("missing"),
        ],
    )
    monkeypatch.setenv(
        "CAMBIUM_PROVIDER_PRESENT_API_KEY", "secret-value-that-must-not-be-reported"
    )
    monkeypatch.delenv("CAMBIUM_PROVIDER_MISSING_API_KEY", raising=False)

    providers = load_providers(path)

    assert env_report(providers) == {"present": True, "missing": False}


def test_env_report_treats_empty_value_as_unconfigured(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    path = _write(
        tmp_path / "providers.json",
        [
            _provider("empty"),
            _provider("present"),
        ],
    )
    monkeypatch.setenv("CAMBIUM_PROVIDER_EMPTY_API_KEY", "")
    monkeypatch.setenv("CAMBIUM_PROVIDER_PRESENT_API_KEY", "non-empty-secret")

    providers = load_providers(path)

    assert env_report(providers) == {"empty": False, "present": True}


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


def test_loopback_http_base_url_is_accepted(tmp_path: Path) -> None:
    for base_url in (
        "http://localhost:8080/v1",
        "http://127.0.0.1:8080/v1",
        "http://[::1]:8080/v1",
    ):
        path = _write(tmp_path / "providers.json", [_provider(base_url=base_url)])

        providers = load_providers(path)

        assert len(providers) == 1
        assert providers[0].base_url == base_url


def test_remote_http_base_url_is_rejected(tmp_path: Path) -> None:
    path = _write(tmp_path / "providers.json", [_provider(base_url="http://api.example.test/v1")])

    with pytest.raises(ValueError, match="http transport is allowed only for loopback hosts"):
        load_providers(path)


def test_remote_https_base_url_is_accepted(tmp_path: Path) -> None:
    path = _write(tmp_path / "providers.json", [_provider(base_url="https://api.example.test/v1")])

    providers = load_providers(path)

    assert len(providers) == 1
    assert providers[0].base_url == "https://api.example.test/v1"


def test_url_credentials_in_base_url_are_rejected(tmp_path: Path) -> None:
    path = _write(
        tmp_path / "providers.json",
        [_provider(base_url="https://user:pass@api.example.test/v1")],
    )

    with pytest.raises(ValueError, match="must not contain URL credentials"):
        load_providers(path)
