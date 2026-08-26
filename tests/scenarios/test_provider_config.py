"""Scenario tests for the Diffundo provider-config loader and quarantine policy."""

from __future__ import annotations

import json
import logging
import re
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


def _assert_quarantined(
    path: Path, match: str, caplog: pytest.LogCaptureFixture
) -> list[dict[str, object]]:
    """Assert the invalid entry is dropped, recorded, and warned about."""

    caplog.set_level(logging.WARNING, logger=provider_config.__name__)
    providers = load_providers(path)

    assert providers == []
    sidecar = path.with_name(path.name + ".quarantine")
    records = json.loads(sidecar.read_text(encoding="utf-8"))
    assert isinstance(records, list)
    assert len(records) == 1
    record = records[0]
    assert isinstance(record, dict)
    assert {"entry", "reason", "quarantined_at"} <= set(record)
    assert re.search(match, str(record["reason"]))
    assert any(
        item.name == provider_config.__name__
        and item.levelno == logging.WARNING
        and getattr(item, "event", None) == "provider_config_quarantined"
        for item in caplog.records
    )
    return records


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


def test_generic_api_key_environment_name_is_quarantined(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    path = _write(tmp_path / "providers.json", [_provider(api_key_env="OPENAI_API_KEY")])

    _assert_quarantined(path, "derived CAMBIUM", caplog)


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


def test_remote_http_base_url_is_quarantined(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    path = _write(tmp_path / "providers.json", [_provider(base_url="http://api.example.test/v1")])

    _assert_quarantined(path, "http transport is allowed only for loopback hosts", caplog)


def test_remote_https_base_url_is_accepted(tmp_path: Path) -> None:
    path = _write(tmp_path / "providers.json", [_provider(base_url="https://api.example.test/v1")])

    providers = load_providers(path)

    assert len(providers) == 1
    assert providers[0].base_url == "https://api.example.test/v1"


def test_url_credentials_in_base_url_are_quarantined(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    path = _write(
        tmp_path / "providers.json",
        [_provider(base_url="https://user:pass@api.example.test/v1")],
    )

    _assert_quarantined(path, "must not contain URL credentials", caplog)


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


def test_codex_chatgpt_without_codex_responses_protocol_is_quarantined(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    path = _write(
        tmp_path / "providers.json",
        [_codex_provider(protocol="chat_completions")],
    )

    _assert_quarantined(path, "requires protocol 'codex_responses'", caplog)


def test_codex_responses_without_codex_chatgpt_auth_is_quarantined(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    path = _write(
        tmp_path / "providers.json",
        [_provider(protocol="codex_responses")],
    )

    _assert_quarantined(path, "requires auth 'codex_chatgpt'", caplog)


def test_codex_chatgpt_base_url_in_file_is_quarantined(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    # Token-exfiltration guard: a modified provider file must never redirect
    # the bearer token away from the pinned profile endpoint.
    path = _write(
        tmp_path / "providers.json",
        [_codex_provider(base_url="https://attacker.example.test/v1")],
    )

    _assert_quarantined(path, "must not be set with auth 'codex_chatgpt'", caplog)


def test_codex_chatgpt_api_key_env_in_file_is_quarantined(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    path = _write(
        tmp_path / "providers.json",
        [_codex_provider(api_key_env="CAMBIUM_PROVIDER_CODEX_API_KEY")],
    )

    _assert_quarantined(path, "must not be set with auth 'codex_chatgpt'", caplog)


def test_api_key_provider_without_api_key_env_is_quarantined(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    value = _provider()
    del value["api_key_env"]
    path = _write(tmp_path / "providers.json", [value])

    _assert_quarantined(path, r"missing required field\(s\).*api_key_env", caplog)


@pytest.mark.parametrize("model", ["", "   ", "\t\n"])
def test_blank_model_is_quarantined(
    tmp_path: Path, model: str, caplog: pytest.LogCaptureFixture
) -> None:
    path = _write(tmp_path / "providers.json", [_provider(model=model)])

    _assert_quarantined(path, r"providers\[0\]\.model: must not be blank", caplog)


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
def test_malformed_auth_protocol_values_are_quarantined(
    tmp_path: Path,
    overrides: dict[str, object],
    match: str,
    caplog: pytest.LogCaptureFixture,
) -> None:
    # An explicit malformed tag is an error, never a silent default.
    path = _write(tmp_path / "providers.json", [_provider(**overrides)])

    _assert_quarantined(path, match, caplog)


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
def test_malformed_reasoning_effort_is_quarantined(
    tmp_path: Path, value: object, caplog: pytest.LogCaptureFixture
) -> None:
    path = _write(
        tmp_path / "providers.json",
        [_codex_provider(reasoning_effort=value)],
    )

    _assert_quarantined(path, "reasoning_effort", caplog)


def test_reasoning_effort_requires_codex_responses_protocol_is_quarantined(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    path = _write(
        tmp_path / "providers.json",
        [_provider(reasoning_effort="high")],
    )

    _assert_quarantined(path, "only supported with protocol 'codex_responses'", caplog)


def test_cached_input_price_round_trips_independently(tmp_path: Path) -> None:
    path = _write(
        tmp_path / "providers.json",
        [
            _provider(
                price=0.40, price_per_1m_in=0.30, price_per_1m_cached_in=0.05, price_per_1m_out=0.80
            )
        ],
    )

    providers = load_providers(path)

    assert providers[0].price_per_1m_in == 0.30
    assert providers[0].price_per_1m_cached_in == 0.05
    assert providers[0].price_per_1m_out == 0.80


def test_valid_entries_continue_after_invalid_entry_is_quarantined(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    invalid = _provider("broken", api_key_env="OPENAI_API_KEY")
    path = _write(tmp_path / "providers.json", [_provider("healthy"), invalid])

    caplog.set_level(logging.WARNING, logger=provider_config.__name__)
    providers = load_providers(path)

    assert [provider.name for provider in providers] == ["healthy"]
    records = json.loads(path.with_name(path.name + ".quarantine").read_text(encoding="utf-8"))
    expected_entry = {**invalid, "api_key_env": "<redacted:14>"}
    assert records[0]["entry"] == expected_entry
    assert records[0]["reason"] == (
        "provider config providers[1].api_key_env: "
        "must be the derived CAMBIUM provider environment name"
    )
    assert records[0]["quarantined_at"].endswith("Z")
    assert any(
        item.name == provider_config.__name__
        and item.levelno == logging.WARNING
        and getattr(item, "event", None) == "provider_config_quarantined"
        for item in caplog.records
    )


def test_quarantine_sidecar_merge_appends_existing_records(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    path = _write(tmp_path / "providers.json", [_provider(api_key_env="OPENAI_API_KEY")])
    sidecar = path.with_name(path.name + ".quarantine")
    existing = [
        {"entry": {"name": "previous"}, "reason": "previous reason", "quarantined_at": "old"}
    ]
    sidecar.write_text(json.dumps(existing), encoding="utf-8")

    caplog.set_level(logging.WARNING, logger=provider_config.__name__)
    assert load_providers(path) == []

    records = json.loads(sidecar.read_text(encoding="utf-8"))
    assert records[0] == existing[0]
    assert len(records) == 2
    assert records[1]["entry"]["name"] == "openai"
    assert re.search("derived CAMBIUM", records[1]["reason"])
    assert any(
        item.name == provider_config.__name__
        and item.levelno == logging.WARNING
        and getattr(item, "event", None) == "provider_config_quarantined"
        for item in caplog.records
    )


def test_all_quarantined_loads_zero_and_selection_names_sidecar(tmp_path: Path) -> None:
    path = _write(tmp_path / "providers.json", [_provider(api_key_env="OPENAI_API_KEY")])

    providers = load_providers(path)

    assert providers == []
    with pytest.raises(
        ValueError,
        match=r"all providers quarantined to .*providers\.json\.quarantine; fix or remove entries",
    ):
        select_provider(providers)


def test_quarantine_redacts_secret_shaped_values_but_keeps_reason_and_structure(
    tmp_path: Path,
) -> None:
    sentinel = "sk-audit-sentinel-that-must-not-be-persisted"
    invalid = _provider(
        "broken",
        api_key_env="OPENAI_API_KEY",
        annotations={
            "apiToken": sentinel,
            "benign": "keep this field",
        },
    )
    path = _write(tmp_path / "providers.json", [invalid])

    assert load_providers(path) == []

    sidecar = path.with_name(path.name + ".quarantine")
    text = sidecar.read_text(encoding="utf-8")
    assert sentinel not in text
    records = json.loads(text)
    entry = records[0]["entry"]
    assert entry["name"] == "broken"
    assert entry["annotations"]["benign"] == "keep this field"
    assert entry["annotations"]["apiToken"].startswith("<redacted:")
    assert records[0]["reason"] == ("provider config providers[0]: unknown field(s): 'annotations'")


def test_symlinked_quarantine_sidecar_is_not_followed(
    tmp_path: Path,
) -> None:
    path = _write(tmp_path / "providers.json", [_provider(api_key_env="OPENAI_API_KEY")])
    target = tmp_path / "target.json"
    target.write_text('{"must": "remain untouched"}', encoding="utf-8")
    sidecar = path.with_name(path.name + ".quarantine")
    sidecar.symlink_to(target)

    with pytest.raises(OSError, match="must not be a symlink"):
        load_providers(path)

    assert target.read_text(encoding="utf-8") == '{"must": "remain untouched"}'
    assert sidecar.is_symlink()


def test_oversized_quarantine_entry_is_stubbed(tmp_path: Path) -> None:
    invalid = _provider("broken", notes="x " * 40_000)
    path = _write(tmp_path / "providers.json", [invalid])

    assert load_providers(path) == []

    sidecar = path.with_name(path.name + ".quarantine")
    records = json.loads(sidecar.read_text(encoding="utf-8"))
    assert records[0]["entry"].startswith("<oversized: ")
    assert records[0]["entry"].endswith(" bytes>")
    assert sidecar.stat().st_size < provider_config.MAX_QUARANTINE_RECORD_BYTES


def test_quarantine_sidecar_limit_stops_append_and_warns(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    path = _write(tmp_path / "providers.json", [_provider(api_key_env="OPENAI_API_KEY")])
    monkeypatch.setattr(provider_config, "MAX_QUARANTINE_SIDECAR_BYTES", 200)
    caplog.set_level(logging.WARNING, logger=provider_config.__name__)

    assert load_providers(path) == []

    sidecar = path.with_name(path.name + ".quarantine")
    assert not sidecar.exists()
    assert any("byte limit" in record.getMessage() for record in caplog.records)


def test_quarantine_surrogates_and_deep_entries_are_cleanly_encoded(tmp_path: Path) -> None:
    surrogate_entry = {"name": "\ud800", "tier": "strong"}
    source = tmp_path / "surrogate.json"
    source.write_text("{}", encoding="utf-8")
    sidecar = source.with_name(source.name + ".quarantine")

    provider_config._append_quarantine(
        source, [provider_config._quarantine_record(surrogate_entry, "surrogate reason")]
    )
    records = json.loads(sidecar.read_text(encoding="utf-8"))
    assert records[0]["entry"]["name"] == "\ud800"
    assert records[0]["reason"] == "surrogate reason"

    deep: object = {"leaf": "depth sentinel"}
    for _ in range(1_100):
        deep = {"nested": deep}
    deep_record = provider_config._quarantine_record(deep, "deep reason")
    deep_source = tmp_path / "deep.json"
    deep_source.write_text("{}", encoding="utf-8")
    deep_sidecar = deep_source.with_name(deep_source.name + ".quarantine")
    provider_config._append_quarantine(deep_source, [deep_record])

    deep_text = deep_sidecar.read_text(encoding="utf-8")
    assert "depth sentinel" not in deep_text
    assert json.loads(deep_text)[0]["reason"] == "deep reason"
