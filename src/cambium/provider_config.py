"""Strict, env-keyed provider configuration loading for Diffundo.

``DEFAULT_SAMPLE`` documents the JSON shape without creating or writing a
configuration file. Provider files contain API-key environment variable names
only. The values are deliberately not inspected while loading; Diffundo reads
them from the environment when it makes a call.
"""

from __future__ import annotations

import json
import os
import re
from dataclasses import fields
from pathlib import Path
from urllib.parse import urlparse

from .diffundo import ProviderConfig, ProviderTier

_DEFAULT_PATH = Path(".cambium/providers.json")
_ENV_NAME = re.compile(r"[A-Za-z_][A-Za-z0-9_]*\Z")
_TOP_LEVEL_FIELDS = frozenset({"providers"})
_PROVIDER_FIELDS = frozenset(
    {
        "name",
        "tier",
        "base_url",
        "api_key_env",
        "timeout_s",
        "max_retries",
        "rpm",
        "enabled",
        "model",
        "priority",
        "cooldown_s",
        "price",
    }
)
_DEFAULTS: dict[str, object] = {
    "timeout_s": 30.0,
    "max_retries": 2,
    "rpm": 60,
    "enabled": True,
    "model": "",
    "priority": 0,
    "cooldown_s": 60.0,
    "price": 0.0,
}


DEFAULT_SAMPLE: dict[str, list[dict[str, object]]] = {
    "providers": [
        {
            "name": "openai",
            "tier": "strong",
            "base_url": "https://api.openai.com/v1",
            "api_key_env": "OPENAI_API_KEY",
            "timeout_s": 30.0,
            "max_retries": 2,
            "rpm": 60,
            "enabled": True,
            "model": "gpt-5.6",
            "priority": 0,
            "cooldown_s": 60.0,
            "price": 0.0,
        },
        {
            "name": "llama-cpp",
            "tier": "fast",
            "base_url": "http://127.0.0.1:8080/v1",
            "api_key_env": "LOCAL_LLM_API_KEY",
            "timeout_s": 30.0,
            "max_retries": 0,
            "rpm": 120,
            "enabled": True,
            "model": "local-model",
            "priority": 1,
            "cooldown_s": 10.0,
            "price": 0.0,
        },
    ]
}


def _error(location: str, message: str) -> ValueError:
    return ValueError(f"provider config {location}: {message}")


def _require_string(value: object, location: str) -> str:
    if not isinstance(value, str):
        raise _error(location, "must be a string")
    return value


def _require_number(value: object, location: str) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise _error(location, "must be a number")
    return float(value)


def _require_integer(value: object, location: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int):
        raise _error(location, "must be an integer")
    return value


def _validate_base_url(value: object, location: str) -> str:
    base_url = _require_string(value, location)
    parsed = urlparse(base_url)
    if parsed.scheme.lower() not in {"http", "https"} or not parsed.netloc:
        raise _error(location, "must be an absolute http(s) URL")
    if parsed.username is not None or parsed.password is not None:
        raise _error(location, "must not contain URL credentials")
    return base_url


def _validate_api_key_env(value: object, location: str) -> str:
    api_key_env = _require_string(value, location)
    if _ENV_NAME.fullmatch(api_key_env) is None:
        raise _error(location, "must be a non-empty environment-variable NAME")
    return api_key_env


def _provider_from_mapping(raw: object, index: int) -> ProviderConfig:
    location = f"providers[{index}]"
    if not isinstance(raw, dict):
        raise _error(location, "must be an object")

    unknown = sorted(set(raw) - _PROVIDER_FIELDS)
    if unknown:
        raise _error(location, f"unknown field(s): {', '.join(map(repr, unknown))}")

    missing = sorted(
        field for field in ("name", "tier", "base_url", "api_key_env") if field not in raw
    )
    if missing:
        raise _error(location, f"missing required field(s): {', '.join(missing)}")

    name = _require_string(raw["name"], f"{location}.name")
    if not name:
        raise _error(f"{location}.name", "must be non-empty")

    tier_value = raw["tier"]
    if not isinstance(tier_value, str):
        raise _error(f"{location}.tier", "must be a tier name")
    try:
        tier = ProviderTier(tier_value)
    except ValueError as exc:
        choices = ", ".join(member.value for member in ProviderTier)
        raise _error(
            f"{location}.tier", f"invalid tier {tier_value!r}; expected {choices}"
        ) from exc

    base_url = _validate_base_url(raw["base_url"], f"{location}.base_url")
    api_key_env = _validate_api_key_env(raw["api_key_env"], f"{location}.api_key_env")

    values = {**_DEFAULTS, **raw}
    timeout_s = _require_number(values["timeout_s"], f"{location}.timeout_s")
    if timeout_s <= 0:
        raise _error(f"{location}.timeout_s", "must be greater than 0")

    max_retries = _require_integer(values["max_retries"], f"{location}.max_retries")
    if max_retries < 0:
        raise _error(f"{location}.max_retries", "must not be negative")

    rpm = _require_integer(values["rpm"], f"{location}.rpm")
    if rpm <= 0:
        raise _error(f"{location}.rpm", "must be greater than 0")

    enabled = values["enabled"]
    if not isinstance(enabled, bool):
        raise _error(f"{location}.enabled", "must be a boolean")

    model = _require_string(values["model"], f"{location}.model")
    priority = _require_integer(values["priority"], f"{location}.priority")

    cooldown_s = _require_number(values["cooldown_s"], f"{location}.cooldown_s")
    if cooldown_s < 0:
        raise _error(f"{location}.cooldown_s", "must not be negative")

    price = _require_number(values["price"], f"{location}.price")
    if price < 0:
        raise _error(f"{location}.price", "must not be negative")

    config_values: dict[str, object] = {
        "name": name,
        "tier": tier,
        "base_url": base_url,
        "api_key_env": api_key_env,
        "timeout_s": timeout_s,
        "max_retries": max_retries,
        "rpm": rpm,
        "enabled": enabled,
        "model": model,
        "priority": priority,
        "cooldown_s": cooldown_s,
    }
    provider_fields = {field.name for field in fields(ProviderConfig)}
    if "price" in provider_fields:
        config_values["price"] = price
    elif {"price_per_1m_in", "price_per_1m_out"} <= provider_fields:
        config_values["price_per_1m_in"] = price
        config_values["price_per_1m_out"] = price
    else:
        raise RuntimeError("ProviderConfig has no supported price field")

    return ProviderConfig(**config_values)


def _read_config(source: str | Path | None) -> object:
    if source is None:
        configured = os.environ.get("CAMBIUM_PROVIDERS")
        path = Path(configured) if configured else _DEFAULT_PATH
    else:
        path = Path(source)

    if not path.is_file():
        raise FileNotFoundError(
            f"provider config file not found: {path} "
            "(set CAMBIUM_PROVIDERS or create .cambium/providers.json)"
        )

    try:
        text = path.read_text(encoding="utf-8")
        return json.loads(text)
    except json.JSONDecodeError as exc:
        raise ValueError(f"invalid provider config JSON in {path}: {exc}") from exc


def load_providers(source: str | Path | None = None) -> list[ProviderConfig]:
    """Load and strictly validate providers from a JSON configuration file.

    ``source`` overrides ``CAMBIUM_PROVIDERS``. With neither set, the loader
    reads ``.cambium/providers.json`` relative to the current directory. The
    presence of each ``api_key_env`` variable is intentionally not checked;
    Diffundo resolves key values at call time.
    """
    raw = _read_config(source)
    if not isinstance(raw, dict):
        raise _error("root", "must be an object with a 'providers' field")

    unknown = sorted(set(raw) - _TOP_LEVEL_FIELDS)
    if unknown:
        raise _error("root", f"unknown field(s): {', '.join(map(repr, unknown))}")

    entries = raw.get("providers")
    if not isinstance(entries, list):
        raise _error("providers", "must be a list")

    providers: list[ProviderConfig] = []
    names: set[str] = set()
    for index, entry in enumerate(entries):
        provider = _provider_from_mapping(entry, index)
        if provider.name in names:
            raise _error(f"providers[{index}].name", f"duplicate provider name {provider.name!r}")
        names.add(provider.name)
        providers.append(provider)

    return providers


def env_report(providers: list[ProviderConfig] | tuple[ProviderConfig, ...]) -> dict[str, bool]:
    """Return whether each provider's key environment variable is present.

    The report contains only provider names and booleans. It never returns an
    environment-variable name or value.
    """
    return {provider.name: provider.api_key_env in os.environ for provider in providers}


__all__ = ["DEFAULT_SAMPLE", "ProviderConfig", "ProviderTier", "env_report", "load_providers"]
