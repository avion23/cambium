"""Strict, env-keyed provider configuration loading for Diffundo.

Provider entries carry a tagged ``auth``/``protocol`` mode: the legacy
``api_key`` + ``chat_completions`` pair is unchanged, and ``codex_chatgpt``
entries are pinned to ``CODEX_CHATGPT_PROFILE`` and must not carry
``base_url``/``api_key_env`` (the profile fixes the endpoint and token flow).

``DEFAULT_SAMPLE`` documents the JSON shape without creating or writing a
configuration file. Provider files contain API-key environment variable names
only. The values are deliberately not inspected while loading; Diffundo reads
them from the environment when it makes a call. The optional ``required`` flag
is doctor metadata: it defaults to ``False`` so a missing key is a warning;
``cambium doctor`` reports a missing key as a failure only when it is ``True``.
``select_provider`` is a stateless one-shot picker: it chooses one enabled
configured provider by explicit name or by existing priority/tier order and
never reads the environment or a key value.
"""

from __future__ import annotations

import json
import math
import os
import re
from collections.abc import Sequence
from dataclasses import dataclass, fields
from enum import Enum
from pathlib import Path
from typing import TYPE_CHECKING
from urllib.parse import urlparse

from .auth import validate_derived_env_name, validate_provider_id

if TYPE_CHECKING:
    from .diffundo import ProviderConfig, ProviderTier


class AuthMode(Enum):
    """How a provider authenticates; ``API_KEY`` is the unchanged default."""

    API_KEY = "api_key"
    CODEX_CHATGPT = "codex_chatgpt"


class Protocol(Enum):
    """Wire protocol spoken with a provider; ``CHAT_COMPLETIONS`` is the
    unchanged default."""

    CHAT_COMPLETIONS = "chat_completions"
    CODEX_RESPONSES = "codex_responses"


# Pinned Codex-ChatGPT OAuth profile (codex-oauth plan W1). A provider tagged
# ``auth=codex_chatgpt`` always targets this endpoint; providers.json must not
# override it (see _validate_provider_mapping) so a modified provider file can
# never redirect the bearer token to an attacker URL. Tests inject a loopback
# profile only by constructing ProviderConfig directly; the pinned constants
# are not configurable from providers.json.
CODEX_CHATGPT_PROFILE: dict[str, object] = {
    "issuer": "https://auth.openai.com",
    "api_origin": "https://chatgpt.com",
    "api_path": "/backend-api/codex/responses",
    "scopes": ["openid", "profile", "email", "offline_access"],
}

DEFAULT_PROVIDER_PATH = Path(".cambium/providers.json")
_ENV_NAME = re.compile(r"[A-Za-z_][A-Za-z0-9_]*\Z")
_TOP_LEVEL_FIELDS = frozenset({"providers"})
_PROVIDER_FIELDS = frozenset(
    {
        "name",
        "tier",
        "base_url",
        "api_key_env",
        "required",
        "timeout_s",
        "max_retries",
        "rpm",
        "enabled",
        "model",
        "priority",
        "cooldown_s",
        "price",
        "token_window_allowance",
        "auth",
        "protocol",
        "reasoning_effort",
    }
)
_DEFAULTS: dict[str, object] = {
    "timeout_s": 30.0,
    "max_retries": 2,
    "rpm": 60,
    "enabled": True,
    "required": False,
    "model": "",
    "priority": 0,
    "cooldown_s": 60.0,
    "price": 0.0,
    # Optional admission-balancing window (solution C); 0/absent falls back
    # to routing.DEFAULT_TOKEN_WINDOW_ALLOWANCE.
    "token_window_allowance": 0.0,
}


DEFAULT_SAMPLE: dict[str, list[dict[str, object]]] = {
    "providers": [
        {
            "name": "openai",
            "tier": "strong",
            "base_url": "https://api.openai.com/v1",
            "api_key_env": "CAMBIUM_PROVIDER_OPENAI_API_KEY",
            "required": False,
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
            "api_key_env": "CAMBIUM_PROVIDER_LLAMA_CPP_API_KEY",
            "required": False,
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

_VALID_TIERS = frozenset({"fast", "balanced", "strong", "reasoning"})
# Plaintext http transport is permitted only for loopback hosts; every remote
# provider must use https so the Authorization: Bearer key stays encrypted.
_LOOPBACK_HOSTS = frozenset({"localhost", "127.0.0.1", "::1"})


def is_loopback_host(hostname: str) -> bool:
    """True for the loopback host names that may use plaintext http transport."""
    return hostname in _LOOPBACK_HOSTS


@dataclass(frozen=True, slots=True)
class ProviderEnvSpec:
    """Validated provider fields needed by environment diagnostics."""

    name: str
    api_key_env: str
    required: bool


def _error(location: str, message: str) -> ValueError:
    return ValueError(f"provider config {location}: {message}")


def _require_string(value: object, location: str) -> str:
    if not isinstance(value, str):
        raise _error(location, "must be a string")
    return value


def _require_number(value: object, location: str) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise _error(location, "must be a number")
    if not math.isfinite(value):
        raise _error(location, "must be a finite number")
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
    if parsed.scheme.lower() == "http" and not is_loopback_host(parsed.hostname or ""):
        raise _error(
            location,
            "http transport is allowed only for loopback hosts "
            "(localhost, 127.0.0.1, ::1); remote providers require https",
        )
    return base_url


def _validate_api_key_env(value: object, location: str, provider: str) -> str:
    api_key_env = _require_string(value, location)
    if _ENV_NAME.fullmatch(api_key_env) is None:
        raise _error(location, "must be a non-empty environment-variable NAME")
    try:
        return validate_derived_env_name(provider, api_key_env)
    except ValueError as exc:
        raise _error(location, "must be the derived CAMBIUM provider environment name") from exc


def _diffundo_types() -> tuple[type[ProviderConfig], type[ProviderTier]]:
    try:
        from .diffundo import ProviderConfig, ProviderTier
    except ModuleNotFoundError as exc:
        if exc.name != "cambium.diffundo":
            raise
        raise ImportError("diffundo not merged yet") from exc
    return ProviderConfig, ProviderTier


def _parse_auth_mode(raw: dict[str, object], location: str) -> AuthMode:
    """Parse the optional ``auth`` tag; an explicit malformed value fails closed.

    An absent tag defaults to ``AuthMode.API_KEY``; a present tag that is not a
    valid mode name is an error, never a silent default.
    """
    value = raw.get("auth", AuthMode.API_KEY.value)
    if not isinstance(value, str):
        raise _error(f"{location}.auth", "must be an auth mode name")
    try:
        return AuthMode(value)
    except ValueError as exc:
        choices = ", ".join(sorted(member.value for member in AuthMode))
        raise _error(
            f"{location}.auth", f"invalid auth mode {value!r}; expected {choices}"
        ) from exc


def _parse_protocol(raw: dict[str, object], location: str) -> Protocol:
    """Parse the optional ``protocol`` tag; an explicit malformed value fails
    closed. An absent tag defaults to ``Protocol.CHAT_COMPLETIONS``; a present
    tag that is not a valid protocol name is an error, never a silent default.
    """
    value = raw.get("protocol", Protocol.CHAT_COMPLETIONS.value)
    if not isinstance(value, str):
        raise _error(f"{location}.protocol", "must be a protocol name")
    try:
        return Protocol(value)
    except ValueError as exc:
        choices = ", ".join(sorted(member.value for member in Protocol))
        raise _error(
            f"{location}.protocol", f"invalid protocol {value!r}; expected {choices}"
        ) from exc


def _validate_provider_mapping(raw: object, index: int) -> dict[str, object]:
    location = f"providers[{index}]"
    if not isinstance(raw, dict):
        raise _error(location, "must be an object")

    unknown = sorted(set(raw) - _PROVIDER_FIELDS)
    if unknown:
        raise _error(location, f"unknown field(s): {', '.join(map(repr, unknown))}")

    missing = sorted(field for field in ("name", "tier") if field not in raw)
    if missing:
        raise _error(location, f"missing required field(s): {', '.join(missing)}")

    name = _require_string(raw["name"], f"{location}.name")
    try:
        validate_provider_id(name)
    except ValueError as exc:
        raise _error(f"{location}.name", "must be a valid provider id") from exc

    tier_value = raw["tier"]
    if not isinstance(tier_value, str):
        raise _error(f"{location}.tier", "must be a tier name")
    if tier_value not in _VALID_TIERS:
        choices = ", ".join(sorted(_VALID_TIERS))
        raise _error(f"{location}.tier", f"invalid tier {tier_value!r}; expected {choices}")

    auth = _parse_auth_mode(raw, location)
    protocol = _parse_protocol(raw, location)
    if auth is AuthMode.CODEX_CHATGPT:
        if protocol is not Protocol.CODEX_RESPONSES:
            raise _error(
                f"{location}.protocol",
                f"auth {AuthMode.CODEX_CHATGPT.value!r} requires protocol "
                f"{Protocol.CODEX_RESPONSES.value!r}",
            )
        # The pinned profile fixes the endpoint and token flow; a modified
        # provider file must never redirect the bearer token to another URL.
        for forbidden in ("base_url", "api_key_env"):
            if forbidden in raw:
                raise _error(
                    f"{location}.{forbidden}",
                    f"must not be set with auth {AuthMode.CODEX_CHATGPT.value!r}: "
                    "the pinned CODEX_CHATGPT_PROFILE fixes the endpoint and token flow",
                )
        base_url = ""
        api_key_env = ""
    else:
        missing = sorted(
            field for field in ("base_url", "api_key_env") if field not in raw
        )
        if missing:
            raise _error(location, f"missing required field(s): {', '.join(missing)}")
        base_url = _validate_base_url(raw["base_url"], f"{location}.base_url")
        api_key_env = _validate_api_key_env(
            raw["api_key_env"], f"{location}.api_key_env", name
        )

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

    required = values["required"]
    if not isinstance(required, bool):
        raise _error(f"{location}.required", "must be a boolean")

    model = _require_string(values["model"], f"{location}.model")
    priority = _require_integer(values["priority"], f"{location}.priority")

    cooldown_s = _require_number(values["cooldown_s"], f"{location}.cooldown_s")
    if cooldown_s < 0:
        raise _error(f"{location}.cooldown_s", "must not be negative")

    price = _require_number(values["price"], f"{location}.price")
    if price < 0:
        raise _error(f"{location}.price", "must not be negative")

    token_window_allowance = _require_number(
        values["token_window_allowance"], f"{location}.token_window_allowance"
    )
    if token_window_allowance < 0:
        raise _error(f"{location}.token_window_allowance", "must not be negative")

    # Optional Responses-API reasoning effort (codex_responses providers); an
    # absent value keeps the request body free of the reasoning field.
    reasoning_effort = raw.get("reasoning_effort")
    if reasoning_effort is not None:
        if not isinstance(reasoning_effort, str) or not reasoning_effort.strip():
            raise _error(f"{location}.reasoning_effort", "must be a non-empty string")
        reasoning_effort = reasoning_effort.strip()

    return {
        "name": name,
        "tier": tier_value,
        "base_url": base_url,
        "api_key_env": api_key_env,
        "required": required,
        "timeout_s": timeout_s,
        "max_retries": max_retries,
        "rpm": rpm,
        "enabled": enabled,
        "model": model,
        "priority": priority,
        "cooldown_s": cooldown_s,
        "price": price,
        "token_window_allowance": token_window_allowance,
        "auth": auth,
        "protocol": protocol,
        "reasoning_effort": reasoning_effort,
    }


def _validated_provider_mappings(raw: object) -> list[dict[str, object]]:
    if not isinstance(raw, dict):
        raise _error("root", "must be an object with a 'providers' field")

    unknown = sorted(set(raw) - _TOP_LEVEL_FIELDS)
    if unknown:
        raise _error("root", f"unknown field(s): {', '.join(map(repr, unknown))}")

    entries = raw.get("providers")
    if not isinstance(entries, list):
        raise _error("providers", "must be a list")

    mappings: list[dict[str, object]] = []
    names: set[str] = set()
    env_names: dict[str, str] = {}
    for index, entry in enumerate(entries):
        mapping = _validate_provider_mapping(entry, index)
        name = mapping["name"]
        if not isinstance(name, str):  # _validate_provider_mapping guarantees this.
            raise TypeError("validated provider name is not a string")
        if name in names:
            raise _error(f"providers[{index}].name", f"duplicate provider name {name!r}")
        names.add(name)
        env_name = mapping["api_key_env"]
        if not isinstance(env_name, str):
            raise TypeError("validated provider environment name is not a string")
        # Only api_key providers carry an env-var name; codex_chatgpt providers
        # share the empty marker and must not collide with each other.
        if env_name:
            previous = env_names.get(env_name)
            if previous is not None:
                raise _error(
                    f"providers[{index}].name",
                    f"provider mapping collides with provider {previous!r}",
                )
            env_names[env_name] = name
        mappings.append(mapping)
    return mappings


def validate_provider_specs(raw: object) -> tuple[ProviderEnvSpec, ...]:
    """Validate a provider document without importing the Diffundo router."""

    return tuple(
        ProviderEnvSpec(
            name=mapping["name"],
            api_key_env=mapping["api_key_env"],
            required=mapping["required"],
        )
        for mapping in _validated_provider_mappings(raw)
    )


def _provider_from_values(values: dict[str, object], index: int) -> ProviderConfig:
    ProviderConfig, ProviderTier = _diffundo_types()
    location = f"providers[{index}]"
    tier_value = values["tier"]
    try:
        tier = ProviderTier(tier_value)
    except ValueError as exc:
        choices = ", ".join(member.value for member in ProviderTier)
        raise _error(
            f"{location}.tier", f"invalid tier {tier_value!r}; expected {choices}"
        ) from exc

    config_values: dict[str, object] = {
        "name": values["name"],
        "tier": tier,
        "base_url": values["base_url"],
        "api_key_env": values["api_key_env"],
        "timeout_s": values["timeout_s"],
        "max_retries": values["max_retries"],
        "rpm": values["rpm"],
        "enabled": values["enabled"],
        "model": values["model"],
        "priority": values["priority"],
        "cooldown_s": values["cooldown_s"],
        "token_window_allowance": values["token_window_allowance"],
        "auth": values["auth"],
        "protocol": values["protocol"],
        "reasoning_effort": values["reasoning_effort"],
    }
    price = values["price"]
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
        path = Path(configured) if configured else DEFAULT_PROVIDER_PATH
    else:
        path = Path(source)

    if not path.is_file():
        raise FileNotFoundError(
            f"provider config file not found: {path} "
            "(set CAMBIUM_PROVIDERS or create .cambium/providers.json)"
        )

    try:
        text = path.read_text(encoding="utf-8")
        return json.loads(
            text,
            object_pairs_hook=_reject_duplicate_pairs,
            parse_constant=_reject_non_standard_constant,
        )
    except json.JSONDecodeError as exc:
        raise ValueError(f"invalid provider config JSON in {path}: {exc}") from exc


def _reject_duplicate_pairs(pairs: list[tuple[str, object]]) -> dict[str, object]:
    values: dict[str, object] = {}
    for key, value in pairs:
        if key in values:
            raise ValueError("provider config contains duplicate JSON fields")
        values[key] = value
    return values


def _reject_non_standard_constant(value: object) -> object:
    """Reject the non-standard JSON constants NaN/Infinity/-Infinity.

    ``json.loads`` accepts them by default; provider config is machine-written
    numeric metadata, so the non-standard constants are always wrong and must
    not reach the numeric field validators.
    """
    raise ValueError(f"provider config root: non-standard JSON constant {value!r}")


def load_provider_specs(source: str | Path | None = None) -> tuple[ProviderEnvSpec, ...]:
    """Load and validate provider environment metadata without Diffundo."""

    return validate_provider_specs(_read_config(source))


def load_providers(source: str | Path | None = None) -> list[ProviderConfig]:
    """Load and strictly validate providers from a JSON configuration file.

    ``source`` overrides ``CAMBIUM_PROVIDERS``. With neither set, the loader
    reads ``.cambium/providers.json`` relative to the current directory. The
    presence of each ``api_key_env`` variable is intentionally not checked;
    Diffundo resolves key values at call time.
    """
    mappings = _validated_provider_mappings(_read_config(source))
    providers: list[ProviderConfig] = []
    for index, mapping in enumerate(mappings):
        providers.append(_provider_from_values(mapping, index))

    return providers


class ProviderSelectionError(ValueError):
    """A requested provider cannot be selected from the configured set."""


def select_provider(
    providers: Sequence[ProviderConfig],
    *,
    name: str | None = None,
    tier: ProviderTier | None = None,
) -> ProviderConfig:
    """Deterministically select one enabled configured provider.

    With an explicit ``name``, return that provider when it is configured and
    enabled. Otherwise select the first enabled provider by ascending
    ``priority`` order — optionally restricted to ``tier`` — matching the
    ordering Diffundo applies to cascade candidates. A missing name, a disabled
    choice, or no enabled candidate raises ``ProviderSelectionError``. The
    decision is pure: it reads only validated config fields and never the
    environment or a secret value.
    """
    if name is not None:
        for provider in providers:
            if provider.name != name:
                continue
            if not provider.enabled:
                raise ProviderSelectionError(
                    f"provider selection: provider {name!r} is disabled"
                )
            return provider
        raise ProviderSelectionError(
            f"provider selection: no provider named {name!r} is configured"
        )

    candidates = sorted(
        (
            provider
            for provider in providers
            if provider.enabled and (tier is None or provider.tier is tier)
        ),
        key=lambda provider: provider.priority,
    )
    if not candidates:
        if tier is not None:
            raise ProviderSelectionError(
                f"provider selection: no enabled provider configured for tier {tier.value!r}"
            )
        raise ProviderSelectionError("provider selection: no enabled provider is configured")
    return candidates[0]


def env_report(
    providers: list[ProviderConfig | ProviderEnvSpec]
    | tuple[ProviderConfig | ProviderEnvSpec, ...]
) -> dict[str, bool]:
    """Return whether each provider's key environment variable is usable.

    A variable is usable only when set to a non-empty value, matching
    Diffundo's call-time check: ``_post_sync`` rejects an empty key with
    ``ProviderOutcome.AUTH_ERROR``. The report contains only provider names and
    booleans. It never returns an environment-variable name or value.
    """
    return {
        provider.name: bool(os.environ.get(provider.api_key_env))
        for provider in providers
    }


def __getattr__(name: str) -> object:
    if name == "ProviderConfig":
        provider_config, _ = _diffundo_types()
        return provider_config
    if name == "ProviderTier":
        _, provider_tier = _diffundo_types()
        return provider_tier
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")


__all__ = [
    "AuthMode",
    "CODEX_CHATGPT_PROFILE",
    "DEFAULT_PROVIDER_PATH",
    "DEFAULT_SAMPLE",
    "Protocol",
    "ProviderConfig",
    "ProviderEnvSpec",
    "ProviderSelectionError",
    "ProviderTier",
    "env_report",
    "is_loopback_host",
    "load_provider_specs",
    "load_providers",
    "select_provider",
    "validate_provider_specs",
]
