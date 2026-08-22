"""Scenario tests for the strict Diffundo provider-config loader."""

from __future__ import annotations

import json
from pathlib import Path
from typing import cast

import pytest

diffundo = pytest.importorskip("cambium.diffundo")

from cambium import provider_config  # noqa: E402
from cambium.auth import derived_env_name, effective_home  # noqa: E402
from cambium.provider_config import (  # noqa: E402
    DEFAULT_PROVIDER_PATH,
    AuthMode,
    Protocol,
    env_report,
    load_providers,
    select_provider,
)


def _provider(name: object = "openai", **overrides: object) -> dict[str, object]:
    value: dict[str, object] = {
        "name": cast(str, name),
        "tier": "strong",
        "base_url": "https://api.example.test/v1",
        "api_key_env": derived_env_name(cast(str, name)),
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


def test_default_path_uses_effective_home_not_home_env(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("HOME", "/path-that-must-not-be-used")

    assert DEFAULT_PROVIDER_PATH == effective_home() / ".config" / "cambium" / "providers.json"


def test_valid_config_loads_without_key_in_environment(tmp_path: Path) -> None:
    path = _write(tmp_path / "providers.json", [_provider()])

    providers = load_providers(path)

    assert len(providers) == 1
    assert providers[0].name == "openai"
    assert providers[0].tier is diffundo.ProviderTier.STRONG
    assert providers[0].api_key_env == "CAMBIUM_PROVIDER_OPENAI_API_KEY"


def test_explicit_source_overrides_environment_path(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    environment_path = _write(tmp_path / "environment.json", [_provider("environment")])
    source_path = _write(tmp_path / "source.json", [_provider("source")])
    monkeypatch.setenv("CAMBIUM_PROVIDERS", str(environment_path))

    providers = load_providers(source_path)

    assert [provider.name for provider in providers] == ["source"]


def test_environment_path_overrides_default(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    path = _write(tmp_path / "environment.json", [_provider("environment")])
    monkeypatch.setenv("CAMBIUM_PROVIDERS", str(path))

    providers = load_providers()

    assert [provider.name for provider in providers] == ["environment"]


def test_generic_api_key_environment_name_is_rejected(tmp_path: Path) -> None:
    path = _write(tmp_path / "providers.json", [_provider(api_key_env="OPENAI_API_KEY")])

    with pytest.raises(ValueError, match="derived CAMBIUM"):
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
    monkeypatch.setenv("CAMBIUM_PROVIDER_PRESENT_API_KEY", "secret-value-that-must-not-be-reported")
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
    expected_path = tmp_path / "effective-home" / ".config" / "cambium" / "providers.json"
    monkeypatch.setattr(provider_config, "DEFAULT_PROVIDER_PATH", expected_path)
    monkeypatch.delenv("CAMBIUM_PROVIDERS", raising=False)
    monkeypatch.setenv("HOME", str(tmp_path / "home-secret"))

    with pytest.raises(FileNotFoundError) as raised:
        load_providers()

    message = str(raised.value)
    assert f"provider config file not found: {expected_path}" in message
    assert f"create {expected_path}" in message
    assert "home-secret" not in message


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


def test_non_finite_numeric_field_is_rejected(tmp_path: Path) -> None:
    path = tmp_path / "providers.json"
    path.write_text(
        json.dumps({"providers": [_provider()]}).replace('"price": 0.0', '"price": NaN'),
        encoding="utf-8",
    )

    with pytest.raises(ValueError, match="non-standard JSON constant"):
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
            price_per_1m_cached_in=0.25,
            pricing_known=True,
        )
    ]


# --------------------------------------------------------------------------- #
# Tagged auth/protocol modes (codex-oauth plan W1)
# --------------------------------------------------------------------------- #


def _codex_provider(name: str = "codex", **overrides: object) -> dict[str, object]:
    value: dict[str, object] = {
        "name": name,
        "tier": "strong",
        "auth": "codex_chatgpt",
        "protocol": "codex_responses",
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


def test_auth_protocol_round_trip_from_providers_json(tmp_path: Path) -> None:
    path = _write(tmp_path / "providers.json", [_codex_provider()])

    providers = load_providers(path)

    assert len(providers) == 1
    assert providers[0].auth is AuthMode.CODEX_CHATGPT
    assert providers[0].protocol is Protocol.CODEX_RESPONSES
    # The profile pins the endpoint and the OAuth flow: a codex provider never
    # carries a base_url or an api_key_env through the loader.
    assert providers[0].base_url == ""
    assert providers[0].api_key_env == ""


def test_codex_chatgpt_without_codex_responses_protocol_is_rejected(
    tmp_path: Path,
) -> None:
    path = _write(
        tmp_path / "providers.json",
        [_codex_provider(protocol="chat_completions")],
    )

    with pytest.raises(ValueError, match="requires protocol 'codex_responses'"):
        load_providers(path)


def test_codex_chatgpt_base_url_in_file_is_rejected(tmp_path: Path) -> None:
    # Token-exfiltration guard: a modified provider file must never redirect
    # the bearer token away from the pinned profile endpoint.
    path = _write(
        tmp_path / "providers.json",
        [_codex_provider(base_url="https://attacker.example.test/v1")],
    )

    with pytest.raises(ValueError, match="must not be set with auth 'codex_chatgpt'"):
        load_providers(path)


def test_codex_chatgpt_api_key_env_in_file_is_rejected(tmp_path: Path) -> None:
    path = _write(
        tmp_path / "providers.json",
        [_codex_provider(api_key_env="CAMBIUM_PROVIDER_CODEX_API_KEY")],
    )

    with pytest.raises(ValueError, match="must not be set with auth 'codex_chatgpt'"):
        load_providers(path)


def test_api_key_provider_without_api_key_env_is_rejected(tmp_path: Path) -> None:
    value = _provider()
    del value["api_key_env"]
    path = _write(tmp_path / "providers.json", [value])

    with pytest.raises(ValueError, match=r"missing required field\(s\).*api_key_env"):
        load_providers(path)


@pytest.mark.parametrize(
    ("overrides", "match"),
    [
        ({"auth": "bearer"}, "invalid auth mode"),
        ({"auth": 1}, "must be an auth mode name"),
        ({"auth": None}, "must be an auth mode name"),
        ({"protocol": "responses"}, "invalid protocol"),
        ({"protocol": 2}, "must be a protocol name"),
        ({"protocol": None}, "must be a protocol name"),
    ],
)
def test_malformed_auth_protocol_values_fail_closed(
    tmp_path: Path, overrides: dict[str, object], match: str
) -> None:
    # An explicit malformed tag is an error, never a silent default.
    path = _write(tmp_path / "providers.json", [_provider(**overrides)])

    with pytest.raises(ValueError, match=match):
        load_providers(path)


def test_mixed_api_key_and_codex_providers_load_and_select(tmp_path: Path) -> None:
    path = _write(
        tmp_path / "providers.json",
        [
            _provider("openai"),
            _codex_provider("codex"),
        ],
    )

    providers = load_providers(path)

    assert [provider.name for provider in providers] == ["openai", "codex"]
    assert select_provider(providers, name="openai").auth is AuthMode.API_KEY
    assert select_provider(providers, name="codex").auth is AuthMode.CODEX_CHATGPT
    assert select_provider(providers, name="codex").protocol is Protocol.CODEX_RESPONSES


@pytest.mark.parametrize(
    "value",
    [5, "", "   ", True],
)
def test_malformed_reasoning_effort_fails_closed(tmp_path: Path, value: object) -> None:
    path = _write(
        tmp_path / "providers.json",
        [_codex_provider(reasoning_effort=value)],
    )

    with pytest.raises(ValueError, match="reasoning_effort"):
        load_providers(path)
