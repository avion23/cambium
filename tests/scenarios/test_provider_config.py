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


def test_non_positive_timeout_s_is_rejected(tmp_path: Path) -> None:
    path = _write(tmp_path / "providers.json", [_provider(timeout_s=0.0)])

    with pytest.raises(ValueError, match="timeout_s.*must be greater than 0"):
        load_providers(path)


def test_negative_max_retries_is_rejected(tmp_path: Path) -> None:
    path = _write(tmp_path / "providers.json", [_provider(max_retries=-1)])

    with pytest.raises(ValueError, match="max_retries.*must not be negative"):
        load_providers(path)


def test_non_integer_max_retries_is_rejected(tmp_path: Path) -> None:
    path = _write(tmp_path / "providers.json", [_provider(max_retries=1.5)])

    with pytest.raises(ValueError, match="max_retries.*must be an integer"):
        load_providers(path)


def test_non_boolean_enabled_is_rejected(tmp_path: Path) -> None:
    path = _write(tmp_path / "providers.json", [_provider(enabled="yes")])

    with pytest.raises(ValueError, match="enabled.*must be a boolean"):
        load_providers(path)


def test_non_boolean_required_is_rejected(tmp_path: Path) -> None:
    path = _write(tmp_path / "providers.json", [_provider(required="yes")])

    with pytest.raises(ValueError, match="required.*must be a boolean"):
        load_providers(path)


def test_non_string_model_is_rejected(tmp_path: Path) -> None:
    path = _write(tmp_path / "providers.json", [_provider(model=123)])

    with pytest.raises(ValueError, match="model.*must be a string"):
        load_providers(path)


def test_non_integer_priority_is_rejected(tmp_path: Path) -> None:
    path = _write(tmp_path / "providers.json", [_provider(priority=1.5)])

    with pytest.raises(ValueError, match="priority.*must be an integer"):
        load_providers(path)


def test_negative_cooldown_s_is_rejected(tmp_path: Path) -> None:
    path = _write(tmp_path / "providers.json", [_provider(cooldown_s=-1.0)])

    with pytest.raises(ValueError, match="cooldown_s.*must not be negative"):
        load_providers(path)


def test_negative_price_is_rejected(tmp_path: Path) -> None:
    path = _write(tmp_path / "providers.json", [_provider(price=-0.01)])

    with pytest.raises(ValueError, match="price.*must not be negative"):
        load_providers(path)


def test_top_level_unknown_field_is_rejected(tmp_path: Path) -> None:
    path = tmp_path / "providers.json"
    path.write_text(
        json.dumps({"providers": [_provider()], "unexpected": True}),
        encoding="utf-8",
    )

    with pytest.raises(ValueError, match="unknown field.*unexpected"):
        load_providers(path)


def test_duplicate_json_fields_are_rejected(tmp_path: Path) -> None:
    path = tmp_path / "providers.json"
    path.write_text(
        '{"providers": [{"name": "openai", "name": "openai", "tier": "strong", '
        '"base_url": "https://api.example.test/v1", '
        '"api_key_env": "CAMBIUM_PROVIDER_OPENAI_API_KEY"}]}',
        encoding="utf-8",
    )

    with pytest.raises(ValueError, match="duplicate JSON fields"):
        load_providers(path)


def test_duplicate_api_key_env_is_rejected(tmp_path: Path) -> None:
    path = _write(tmp_path / "providers.json", [_provider("a.b"), _provider("a_b")])

    with pytest.raises(ValueError, match="provider mapping collides with provider 'a.b'"):
        load_providers(path)


def test_missing_required_fields_are_rejected(tmp_path: Path) -> None:
    for field in ("name", "tier", "base_url", "api_key_env"):
        provider = _provider()
        del provider[field]
        path = _write(tmp_path / "providers.json", [provider])

        with pytest.raises(ValueError, match=f"missing required field.*{field}"):
            load_providers(path)


def test_valid_config_round_trips_all_fields(tmp_path: Path) -> None:
    path = _write(
        tmp_path / "providers.json",
        [
            {
                "name": "openai",
                "tier": "strong",
                "base_url": "https://api.example.test/v1",
                "api_key_env": "CAMBIUM_PROVIDER_OPENAI_API_KEY",
                "required": True,
                "timeout_s": 45.5,
                "max_retries": 3,
                "rpm": 120,
                "enabled": True,
                "model": "example-model",
                "priority": 5,
                "cooldown_s": 12.5,
                "price": 0.25,
            }
        ],
    )

    providers = load_providers(path)

    assert providers == [
        diffundo.ProviderConfig(
            name="openai",
            tier=diffundo.ProviderTier.STRONG,
            base_url="https://api.example.test/v1",
            api_key_env="CAMBIUM_PROVIDER_OPENAI_API_KEY",
            timeout_s=45.5,
            max_retries=3,
            rpm=120,
            enabled=True,
            model="example-model",
            priority=5,
            cooldown_s=12.5,
            price_per_1m_in=0.25,
            price_per_1m_out=0.25,
        )
    ]
