"""Scenario tests for one-shot deterministic provider selection.

Selection is a pure decision over the loaded ``ProviderConfig`` objects: an
explicit ``name`` wins, otherwise the enabled providers (optionally restricted
to a ``tier``) are chosen by ascending ``priority`` — the same ordering Diffundo
applies to cascade candidates. Missing and disabled choices are rejected, and
the decision never reads the environment or a key value.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

diffundo = pytest.importorskip("cambium.diffundo")

from cambium.auth import derived_env_name  # noqa: E402
from cambium.provider_config import (  # noqa: E402
    ProviderSelectionError,
    load_providers,
    select_provider,
)


def _provider(name: str, **overrides: object) -> dict[str, object]:
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


def test_priority_order_picks_lowest_priority_enabled_provider(
    tmp_path: Path,
) -> None:
    path = _write(
        tmp_path / "providers.json",
        [
            _provider("openai", priority=2),
            _provider("llama", tier="fast", priority=1),
        ],
    )

    selected = select_provider(load_providers(path))

    assert selected.name == "llama"


def test_priority_ties_resolve_to_configuration_order(tmp_path: Path) -> None:
    path = _write(
        tmp_path / "providers.json",
        [
            _provider("openai", priority=0),
            _provider("llama", tier="fast", priority=0),
        ],
    )

    selected = select_provider(load_providers(path))

    assert selected.name == "openai"


def test_explicit_name_wins_regardless_of_priority(tmp_path: Path) -> None:
    path = _write(
        tmp_path / "providers.json",
        [
            _provider("openai", priority=5),
            _provider("llama", tier="fast", priority=0),
        ],
    )

    selected = select_provider(load_providers(path), name="openai")

    assert selected.name == "openai"


def test_tier_restricts_priority_order(tmp_path: Path) -> None:
    path = _write(
        tmp_path / "providers.json",
        [
            _provider("openai", priority=0),
            _provider("groq", tier="fast", priority=2),
            _provider("llama", tier="fast", priority=1),
        ],
    )

    selected = select_provider(
        load_providers(path), tier=diffundo.ProviderTier.FAST
    )

    assert selected.name == "llama"


def test_missing_explicit_name_is_rejected(tmp_path: Path) -> None:
    path = _write(tmp_path / "providers.json", [_provider("openai")])

    with pytest.raises(ProviderSelectionError, match="named.*is configured"):
        select_provider(load_providers(path), name="missing")


def test_disabled_explicit_name_is_rejected(tmp_path: Path) -> None:
    path = _write(tmp_path / "providers.json", [_provider("openai", enabled=False)])

    with pytest.raises(ProviderSelectionError, match="disabled"):
        select_provider(load_providers(path), name="openai")


def test_disabled_providers_are_skipped_in_priority_order(tmp_path: Path) -> None:
    path = _write(
        tmp_path / "providers.json",
        [
            _provider("openai", priority=0, enabled=False),
            _provider("llama", tier="fast", priority=1),
        ],
    )

    selected = select_provider(load_providers(path))

    assert selected.name == "llama"


def test_no_enabled_provider_for_tier_is_rejected(tmp_path: Path) -> None:
    path = _write(
        tmp_path / "providers.json",
        [
            _provider("openai", priority=0),
            _provider("llama", tier="fast", priority=1, enabled=False),
        ],
    )

    with pytest.raises(ProviderSelectionError, match="no enabled provider"):
        select_provider(load_providers(path), tier=diffundo.ProviderTier.FAST)


def test_no_enabled_provider_at_all_is_rejected(tmp_path: Path) -> None:
    path = _write(tmp_path / "providers.json", [_provider("openai", enabled=False)])

    with pytest.raises(ProviderSelectionError, match="no enabled provider"):
        select_provider(load_providers(path))


def test_selection_never_reads_the_environment_or_secret_values(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    secret = "top-secret-value-that-must-not-leak"
    for env_name in (
        "CAMBIUM_PROVIDER_OPENAI_API_KEY",
        "CAMBIUM_PROVIDER_LLAMA_API_KEY",
    ):
        monkeypatch.setenv(env_name, secret)
    path = _write(
        tmp_path / "providers.json",
        [
            _provider("openai", priority=1),
            _provider("llama", tier="fast", priority=0),
        ],
    )
    providers = load_providers(path)

    assert select_provider(providers).name == "llama"
    monkeypatch.delenv("CAMBIUM_PROVIDER_LLAMA_API_KEY", raising=False)
    assert select_provider(providers).name == "llama"

    with pytest.raises(ProviderSelectionError, match="named.*is configured") as excinfo:
        select_provider(providers, name="missing")
    assert secret not in str(excinfo.value)
    assert "CAMBIUM_PROVIDER_LLAMA_API_KEY" not in str(excinfo.value)
