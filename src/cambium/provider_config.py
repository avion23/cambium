"""Env-keyed provider configuration loading for Diffundo.

File/document structure remains strict, while individual invalid provider
entries are quarantined to a sidecar so one typo does not disable every
provider.

Provider entries carry a tagged ``auth``/``protocol`` mode: the legacy
``api_key`` + ``chat_completions`` pair is unchanged, ``none`` entries send no
credential, and ``codex_chatgpt`` entries are pinned to
``CODEX_CHATGPT_PROFILE`` and must not carry ``base_url``/``api_key_env`` (the
profile fixes the endpoint and token flow).

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

import errno
import json
import logging
import math
import os
import re
import stat
import tempfile
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, fields
from datetime import UTC, datetime
from enum import Enum
from pathlib import Path
from typing import TYPE_CHECKING, Any, TypedDict, cast
from urllib.parse import urlparse

from .auth import effective_home, validate_derived_env_name, validate_provider_id
from .provider_scheduler import BillingMode, CacheCapability, QuotaWindowSpec

if TYPE_CHECKING:
    from .diffundo import ProviderConfig, ProviderTier


logger = logging.getLogger(__name__)


class AuthMode(Enum):
    """How a provider authenticates; ``API_KEY`` is the unchanged default."""

    API_KEY = "api_key"
    NONE = "none"
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
    # The official shared Codex/ChatGPT public OAuth client: the codex CLI
    # embeds this id and existing ChatGPT sessions are minted by it (verified:
    # the session's JWT carries this client_id, and a refresh with it succeeds
    # while the codex-native uuid id returns invalid_client). Refresh tokens
    # are bound to their issuing client, so this id is REQUIRED to refresh.
    "client_id": "app_EMoamEEZ73f0CkXaXp7hrann",
}

DEFAULT_PROVIDER_PATH = effective_home() / ".config" / "cambium" / "providers.json"
_ENV_NAME = re.compile(r"[A-Za-z_][A-Za-z0-9_]*\Z")
_TOP_LEVEL_FIELDS = frozenset({"providers"})
_PROVIDER_FIELDS = frozenset(
    {
        "name",
        "tier",
        "base_url",
        "api_key",
        "api_key_env",
        "required",
        "timeout_s",
        "max_retries",
        "rpm",
        "requests_per_minute",
        "enabled",
        "model",
        "priority",
        "cooldown_s",
        "price",
        "token_window_allowance",
        "auth",
        "protocol",
        "context_window",
        "reasoning_effort",
        "max_concurrency",
        "max_in_flight",
        "billing_mode",
        "quota_windows",
        "price_per_1m_in",
        "price_per_1m_cached_in",
        "price_per_1m_out",
        "pricing_known",
        "throughput_hint_tps",
        "tokens_per_s",
        "interactive_wall_budget_s",
        "supports_native_tools",
        "supports_python_tool",
        "allow_model_substitution",
        "cache_capability",
        "cache_capabilities",
        "cache",
        "minimum_cacheable_tokens",
        "min_cacheable_tokens",
        "min_cacheable_block_tokens",
        "cache_ttl_s",
        "ttl_s",
        "ttl_seconds",
        "cache_ttl_seconds",
        "cache_granularity_tokens",
        "granularity",
        "granularity_tokens",
        "cache_block_granularity_tokens",
        "cache_read_price",
        "cache_read_price_per_1m",
        "cache_write_price",
        "cache_write_price_per_1m",
    }
)
_DEFAULTS: dict[str, object] = {
    "timeout_s": 30.0,
    "max_retries": 2,
    "rpm": 60,
    # New routing spelling; ``None`` means use the legacy rpm value.
    "requests_per_minute": None,
    "enabled": True,
    "required": False,
    "model": "",
    "priority": 0,
    "cooldown_s": 60.0,
    "price": 0.0,
    # Optional admission-balancing window (solution C); 0/absent falls back
    # to routing.DEFAULT_TOKEN_WINDOW_ALLOWANCE.
    "token_window_allowance": 0.0,
    # Optional context-window capacity in tokens (H2); 0/absent means the
    # provider declares no capacity, so min_context_window tasks exclude it.
    "context_window": 0,
    "max_concurrency": 1,
    # New independent concurrency dimension; ``None`` preserves the
    # conservative legacy derivation in ProviderConfig.__post_init__.
    "max_in_flight": None,
    "billing_mode": "metered",
    "quota_windows": (),
    "price_per_1m_in": 0.0,
    "price_per_1m_cached_in": 0.0,
    "price_per_1m_out": 0.0,
    "pricing_known": False,
    "throughput_hint_tps": 0.0,
    "tokens_per_s": None,
    "interactive_wall_budget_s": None,
    "supports_native_tools": True,
    "supports_python_tool": True,
    "allow_model_substitution": False,
    "cache_capability": None,
}


class _ProviderMapping(TypedDict):
    name: str
    tier: str
    base_url: str
    api_key: str | None
    api_key_env: str
    required: bool
    timeout_s: float
    max_retries: int
    rpm: int
    requests_per_minute: int | None
    enabled: bool
    model: str
    priority: int
    cooldown_s: float
    price: float
    token_window_allowance: float
    auth: AuthMode
    protocol: Protocol
    context_window: int
    reasoning_effort: str | None
    max_concurrency: int
    max_in_flight: int | None
    billing_mode: BillingMode
    quota_windows: tuple[QuotaWindowSpec, ...]
    price_per_1m_in: float
    price_per_1m_cached_in: float
    price_per_1m_out: float
    pricing_known: bool
    throughput_hint_tps: float
    tokens_per_s: float | None
    interactive_wall_budget_s: float | None
    supports_native_tools: bool
    supports_python_tool: bool
    allow_model_substitution: bool
    cache_capability: CacheCapability


class _LoadedProviders(list[Any]):
    """List-compatible provider result carrying quarantine metadata."""

    def __init__(
        self,
        providers: Sequence[object],
        *,
        quarantine_path: Path | None,
        quarantined_count: int,
    ) -> None:
        super().__init__(providers)
        self._quarantine_path = quarantine_path
        self._quarantined_count = quarantined_count


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

# Quarantine files are diagnostics, not a second configuration store.  Keep
# them bounded and make the copy of an invalid entry safe to retain.
MAX_QUARANTINE_RECORD_BYTES = 64 * 1024
MAX_QUARANTINE_SIDECAR_BYTES = 1024 * 1024
_MAX_QUARANTINE_ENTRY_DEPTH = 128
_MAX_QUARANTINE_ENTRY_NODES = 100_000
_QUARANTINE_SECRET_KEY_RE = re.compile(r"(?:key|token|secret|credential)", re.IGNORECASE)
_QUARANTINE_LONG_ALNUM_RE = re.compile(r"[A-Za-z0-9]{60,}")
_QUARANTINE_SECRET_PREFIXES = ("sk-", "ghp_", "AKIA")
_QUARANTINE_MARKER_RE = re.compile(r"<(?:redacted:\d+|oversized: \d+ bytes)>")


def is_loopback_host(hostname: str) -> bool:
    """True for the loopback host names that may use plaintext http transport."""
    return hostname in _LOOPBACK_HOSTS


@dataclass(frozen=True, slots=True)
class ProviderEnvSpec:
    """Validated provider fields needed by environment diagnostics."""

    name: str
    api_key_env: str
    required: bool
    api_key: str | None = None


def _error(location: str, message: str) -> ValueError:
    return ValueError(f"provider config {location}: {message}")


def _config_path(source: str | Path | None) -> Path:
    if source is None:
        configured = os.environ.get("CAMBIUM_PROVIDERS")
        return Path(configured) if configured else DEFAULT_PROVIDER_PATH
    return Path(source)


def provider_quarantine_path(source: str | Path) -> Path:
    """Return the sidecar path used for invalid entries from ``source``."""

    path = Path(source)
    return path.with_name(path.name + ".quarantine")


def _quarantine_byte_length(value: str) -> int:
    """Measure text without allowing lone surrogates to escape the boundary."""

    return len(value.encode("utf-8", errors="surrogatepass"))


def _quarantine_string(value: str, key: str | None) -> str:
    if _QUARANTINE_MARKER_RE.fullmatch(value) is not None:
        return value
    secret_key = key is not None and _QUARANTINE_SECRET_KEY_RE.search(key) is not None
    secret_shape = value.startswith(_QUARANTINE_SECRET_PREFIXES) or (
        _QUARANTINE_LONG_ALNUM_RE.search(value) is not None
    )
    if secret_key or secret_shape:
        return f"<redacted:{_quarantine_byte_length(value)}>"
    return value


def _quarantine_size_hint(value: object) -> int:
    """Return a bounded, non-sensitive byte-size estimate for a deep value."""

    pending: list[object] = [value]
    seen: set[int] = set()
    total = 0
    nodes = 0
    while pending and nodes < _MAX_QUARANTINE_ENTRY_NODES:
        candidate = pending.pop()
        nodes += 1
        if isinstance(candidate, str):
            total += _quarantine_byte_length(candidate)
        elif isinstance(candidate, dict):
            identity = id(candidate)
            if identity in seen:
                total += 9  # len(json.dumps("<cyclic>"))
                continue
            seen.add(identity)
            total += 2
            for key, child in candidate.items():
                if isinstance(key, str):
                    total += _quarantine_byte_length(key) + 3
                pending.append(child)
        elif isinstance(candidate, list):
            identity = id(candidate)
            if identity in seen:
                total += 9
                continue
            seen.add(identity)
            total += 2
            pending.extend(candidate)
        else:
            total += 8
    if pending:
        total += len(pending) * 8
    return max(total, 1)


def _sanitize_quarantine_entry(entry: object) -> object:
    """Copy an entry iteratively, redacting secret-shaped string values."""

    def scalar(value: object, key: str | None) -> object:
        if isinstance(value, str):
            return _quarantine_string(value, key)
        if value is None or isinstance(value, bool | int):
            return value
        if isinstance(value, float):
            return value if math.isfinite(value) else "<non-finite>"
        return "<unserializable>"

    if not isinstance(entry, dict | list):
        return scalar(entry, None)

    root: object = {} if isinstance(entry, dict) else []
    active = {id(entry)}
    frames: list[tuple[object, object, Any, int, int]] = [
        (
            entry,
            root,
            iter(entry.items()) if isinstance(entry, dict) else iter(enumerate(entry)),
            0,
            id(entry),
        )
    ]
    nodes = 1
    while frames:
        original, output, iterator, depth, identity = frames[-1]
        try:
            raw_key, child = next(iterator)
        except StopIteration:
            active.discard(identity)
            frames.pop()
            continue

        key: str | None
        if isinstance(original, dict):
            key = raw_key if isinstance(raw_key, str) else None
            safe_key = (
                _quarantine_string(raw_key, None)
                if isinstance(raw_key, str)
                else f"<key:{type(raw_key).__name__}>"
            )
        else:
            key = None
            safe_key = None

        safe_child: object
        if isinstance(child, dict | list):
            if depth >= _MAX_QUARANTINE_ENTRY_DEPTH or nodes >= _MAX_QUARANTINE_ENTRY_NODES:
                safe_child = f"<oversized: {_quarantine_size_hint(child)} bytes>"
            elif id(child) in active:
                safe_child = "<cyclic>"
            else:
                safe_child = {} if isinstance(child, dict) else []
                if isinstance(output, dict):
                    output[safe_key] = safe_child
                else:
                    cast(list[object], output).append(safe_child)
                active.add(id(child))
                nodes += 1
                frames.append(
                    (
                        child,
                        safe_child,
                        iter(child.items()) if isinstance(child, dict) else iter(enumerate(child)),
                        depth + 1,
                        id(child),
                    )
                )
                continue
        else:
            safe_child = scalar(child, key)

        if isinstance(output, dict):
            output[safe_key] = safe_child
        else:
            cast(list[object], output).append(safe_child)
    return root


def _quarantine_json_bytes(value: object, *, indent: int | None = None) -> bytes:
    """Serialize quarantine data as ASCII-safe, finite JSON bytes."""

    if indent is None:
        rendered = json.dumps(
            value,
            ensure_ascii=True,
            allow_nan=False,
            sort_keys=True,
            separators=(",", ":"),
        )
    else:
        rendered = json.dumps(value, ensure_ascii=True, allow_nan=False, indent=indent)
    return rendered.encode("ascii")


def _quarantine_record(entry: object, reason: str) -> dict[str, object]:
    safe_entry = _bounded_quarantine_entry(entry)
    return {
        "entry": safe_entry,
        "reason": reason,
        "quarantined_at": datetime.now(UTC).isoformat().replace("+00:00", "Z"),
    }


def _bounded_quarantine_entry(entry: object) -> object:
    """Sanitize an entry and replace an over-large serialized copy with a stub."""

    safe_entry = _sanitize_quarantine_entry(entry)
    try:
        serialized_entry = _quarantine_json_bytes(safe_entry)
    except (OverflowError, RecursionError, TypeError, UnicodeEncodeError, ValueError):
        safe_entry = "<unserializable>"
        serialized_entry = _quarantine_json_bytes(safe_entry)
    if len(serialized_entry) > MAX_QUARANTINE_RECORD_BYTES:
        safe_entry = f"<oversized: {len(serialized_entry)} bytes>"
    return safe_entry


def _sanitize_quarantine_record(record: dict[str, object]) -> dict[str, object]:
    """Return a safe copy of a record while preserving its reason verbatim."""

    if "entry" not in record:
        return dict(record)
    safe_record = dict(record)
    safe_record["entry"] = _bounded_quarantine_entry(record["entry"])
    return safe_record


def _quarantine_record_key(record: object) -> tuple[str, str] | None:
    if not isinstance(record, dict):
        return None
    reason = record.get("reason")
    if not isinstance(reason, str):
        return None
    try:
        entry = _quarantine_json_bytes(record.get("entry")).decode("ascii")
    except (OverflowError, RecursionError, TypeError, UnicodeEncodeError, ValueError):
        return None
    return entry, reason


def _read_quarantine_sidecar(path: Path) -> tuple[list[object], bool]:
    """Read a regular sidecar without following a replaceable final symlink."""

    try:
        path_stat = path.lstat()
    except FileNotFoundError:
        return [], False
    if stat.S_ISLNK(path_stat.st_mode):
        raise OSError(f"provider quarantine path must not be a symlink: {path}")
    if not stat.S_ISREG(path_stat.st_mode):
        raise OSError(f"provider quarantine path is not a file: {path}")

    flags = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_NONBLOCK", 0)
    descriptor: int | None = None
    try:
        try:
            descriptor = os.open(path, flags)
        except FileNotFoundError:
            # A concurrent remover is equivalent to an absent sidecar.
            return [], False
        except OSError as exc:
            if exc.errno == getattr(errno, "ELOOP", 40):
                raise OSError(f"provider quarantine path must not be a symlink: {path}") from exc
            raise

        sidecar_stat = os.fstat(descriptor)
        if stat.S_ISLNK(sidecar_stat.st_mode):
            raise OSError(f"provider quarantine path must not be a symlink: {path}")
        if not stat.S_ISREG(sidecar_stat.st_mode):
            raise OSError(f"provider quarantine path is not a file: {path}")
        if sidecar_stat.st_size > MAX_QUARANTINE_SIDECAR_BYTES:
            logger.warning(
                "provider quarantine sidecar %s exceeds the %d-byte limit; "
                "new records were not appended",
                path,
                MAX_QUARANTINE_SIDECAR_BYTES,
            )
            return [], True

        sidecar = os.fdopen(descriptor, "rb")
        descriptor = None
        with sidecar:
            raw = sidecar.read(MAX_QUARANTINE_SIDECAR_BYTES + 1)
        if len(raw) > MAX_QUARANTINE_SIDECAR_BYTES:
            logger.warning(
                "provider quarantine sidecar %s exceeds the %d-byte limit; "
                "new records were not appended",
                path,
                MAX_QUARANTINE_SIDECAR_BYTES,
            )
            return [], True
        try:
            text = raw.decode("utf-8")
        except UnicodeDecodeError as exc:
            raise ValueError(f"invalid provider quarantine JSON in {path}: {exc}") from exc
    finally:
        if descriptor is not None:
            os.close(descriptor)

    try:
        existing_raw = json.loads(
            text,
            object_pairs_hook=_reject_duplicate_pairs,
            parse_constant=_reject_non_standard_constant,
        )
    except (json.JSONDecodeError, RecursionError) as exc:
        raise ValueError(f"invalid provider quarantine JSON in {path}: {exc}") from exc
    if not isinstance(existing_raw, list):
        raise ValueError(f"provider quarantine file {path}: must be a list")
    return existing_raw, False


def _append_quarantine(source: Path, records: Sequence[dict[str, object]]) -> Path | None:
    """Merge newly quarantined entries into the source's JSON-list sidecar."""

    if not records:
        return None
    path = provider_quarantine_path(source)
    existing, sidecar_limited = _read_quarantine_sidecar(path)
    if sidecar_limited:
        return path
    existing = [
        _sanitize_quarantine_record(item) if isinstance(item, dict) else item for item in existing
    ]
    safe_records = tuple(_sanitize_quarantine_record(record) for record in records)

    existing_keys = {key for item in existing if (key := _quarantine_record_key(item)) is not None}
    # Loading provider specs and then full providers is common (doctor does
    # both). Avoid repeating records already persisted by the earlier load.
    additions: list[dict[str, object]] = []
    for record in safe_records:
        key = _quarantine_record_key(record)
        if key is None or key not in existing_keys:
            additions.append(record)
            if key is not None:
                existing_keys.add(key)
    if not additions:
        return path

    accepted: list[dict[str, object]] = []
    payload: bytes | None = None
    for index, record in enumerate(additions):
        try:
            candidate = _quarantine_json_bytes([*existing, *accepted, record], indent=2) + b"\n"
        except (OverflowError, RecursionError, TypeError, UnicodeEncodeError, ValueError):
            logger.warning(
                "provider quarantine sidecar %s could not safely serialize %d new record(s); "
                "remaining records were not appended",
                path,
                len(additions) - index,
            )
            break
        if len(candidate) > MAX_QUARANTINE_SIDECAR_BYTES:
            logger.warning(
                "provider quarantine sidecar %s reached the %d-byte limit; "
                "%d new record(s) were not appended",
                path,
                MAX_QUARANTINE_SIDECAR_BYTES,
                len(additions) - index,
            )
            break
        accepted.append(record)
        payload = candidate
    if not accepted or payload is None:
        return path

    temporary_name: str | None = None
    try:
        descriptor, temporary_name = tempfile.mkstemp(
            prefix=f".{path.name}.", suffix=".tmp", dir=path.parent
        )
        with os.fdopen(descriptor, "wb") as temporary:
            temporary.write(payload)
            temporary.flush()
            os.fsync(temporary.fileno())
        os.replace(temporary_name, path)
        temporary_name = None
    finally:
        if temporary_name is not None:
            try:
                os.unlink(temporary_name)
            except FileNotFoundError:
                pass
    return path


def _log_quarantine(
    source: Path, quarantine_path: Path, records: Sequence[dict[str, object]]
) -> None:
    logger.warning(
        "provider config warning: quarantined %d invalid provider entr%s to %s",
        len(records),
        "y" if len(records) == 1 else "ies",
        quarantine_path,
        extra={
            "event": "provider_config_quarantined",
            "config_path": str(source),
            "quarantine_path": str(quarantine_path),
            "quarantined_count": len(records),
            "reasons": tuple(
                reason for record in records if isinstance(reason := record.get("reason"), str)
            ),
        },
    )


def provider_quarantine_notice(providers: Sequence[object]) -> str | None:
    """Return a content-free warning for a load that quarantined entries."""

    count = getattr(providers, "_quarantined_count", 0)
    path = getattr(providers, "_quarantine_path", None)
    if not isinstance(count, int) or count <= 0 or not isinstance(path, Path):
        return None
    noun = "entry" if count == 1 else "entries"
    return (
        f"quarantined {count} invalid provider {noun} to {path}; "
        "valid provider entries remain available"
    )


def all_providers_quarantined_path(providers: Sequence[object]) -> Path | None:
    """Return the sidecar path when a load produced no live providers."""

    path = getattr(providers, "_quarantine_path", None)
    count = getattr(providers, "_quarantined_count", 0)
    if not providers and isinstance(count, int) and count > 0 and isinstance(path, Path):
        return path
    return None


def _require_string(value: object, location: str) -> str:
    if not isinstance(value, str):
        raise _error(location, "must be a string")
    return value


def _require_number(value: object, location: str) -> float:
    if isinstance(value, bool) or not isinstance(value, int | float):
        raise _error(location, "must be a number")
    try:
        finite = math.isfinite(float(value))
    except (TypeError, ValueError, OverflowError):
        raise _error(location, "must be a number") from None
    if not finite:
        raise _error(location, "must be a finite number")
    try:
        return float(value)
    except (TypeError, ValueError, OverflowError):  # pragma: no cover - double guard
        raise _error(location, "must be a number") from None


def _require_integer(value: object, location: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int):
        raise _error(location, "must be an integer")
    return value


def _validate_base_url(value: object, location: str) -> str:
    base_url = _require_string(value, location)
    try:
        parsed = urlparse(base_url)
        hostname = parsed.hostname
        _port = parsed.port
    except ValueError as exc:
        raise _error(location, "must be an absolute http(s) URL") from exc
    if parsed.scheme.lower() not in {"http", "https"} or not hostname:
        raise _error(location, "must be an absolute http(s) URL")
    if parsed.username is not None or parsed.password is not None:
        raise _error(location, "must not contain URL credentials")
    if parsed.query or parsed.fragment:
        raise _error(location, "must not contain query parameters or a fragment")
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


def _validate_api_key(value: object, location: str) -> str:
    return _require_string(value, location)


def _diffundo_types() -> tuple[type[ProviderConfig], type[ProviderTier]]:
    from .diffundo import ProviderConfig, ProviderTier

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


def _parse_billing_mode(value: object, location: str) -> BillingMode:
    if not isinstance(value, str):
        raise _error(location, "must be a billing-mode string")
    try:
        return BillingMode(value)
    except ValueError as exc:
        choices = ", ".join(mode.value for mode in BillingMode)
        raise _error(location, f"invalid billing mode {value!r}; expected {choices}") from exc


def _parse_quota_windows(value: object, location: str) -> tuple[QuotaWindowSpec, ...]:
    if value in (None, ()):
        return ()
    if not isinstance(value, list):
        raise _error(location, "must be a list")
    windows: list[QuotaWindowSpec] = []
    names: set[str] = set()
    for index, item in enumerate(value):
        if not isinstance(item, dict):
            raise _error(f"{location}[{index}]", "must be an object")
        try:
            window = QuotaWindowSpec.from_mapping(item)
        except ValueError as exc:
            raise _error(f"{location}[{index}]", str(exc)) from exc
        if window.name in names:
            raise _error(f"{location}[{index}].name", "must be unique per provider")
        names.add(window.name)
        windows.append(window)
    return tuple(windows)


_CACHE_CAPABILITY_KEYS = frozenset(
    {
        "minimum_cacheable_tokens",
        "min_cacheable_tokens",
        "min_cacheable_block_tokens",
        "cache_ttl_s",
        "ttl_s",
        "ttl_seconds",
        "cache_ttl_seconds",
        "cache_granularity_tokens",
        "granularity",
        "granularity_tokens",
        "cache_block_granularity_tokens",
        "cache_read_price",
        "cache_read_price_per_1m",
        "cache_write_price",
        "cache_write_price_per_1m",
    }
)


def _parse_cache_capability(raw: Mapping[str, object], location: str) -> CacheCapability:
    """Parse nested or flat provider cache capability metadata.

    Provider files in the wild use both ``cache`` and
    ``cache_capability``.  Supporting both at this boundary keeps the rest of
    the runtime on one typed object while retaining strict unknown-field
    rejection inside the capability mapping.
    """
    nested_values: list[Mapping[str, object]] = []
    for key in ("cache_capability", "cache_capabilities", "cache"):
        if key not in raw:
            continue
        value = raw[key]
        if value is None:
            continue
        if not isinstance(value, dict):
            raise _error(f"{location}.{key}", "must be an object")
        nested_values.append(value)
    if len(nested_values) > 1:
        first = dict(nested_values[0])
        if any(dict(value) != first for value in nested_values[1:]):
            raise _error(location, "cache capability aliases disagree")

    flat_values = {key: raw[key] for key in _CACHE_CAPABILITY_KEYS if key in raw}
    nested = dict(nested_values[0]) if nested_values else {}
    overlap = set(flat_values) & set(nested)
    if overlap:
        raise _error(
            location,
            "cache capability fields are declared both nested and flat: "
            + ", ".join(sorted(overlap)),
        )
    values = {**nested, **flat_values}
    if not values:
        return CacheCapability()
    try:
        return CacheCapability.from_mapping(values)
    except (TypeError, ValueError) as exc:
        raise _error(f"{location}.cache_capability", str(exc)) from exc


def _require_bool(value: object, location: str) -> bool:
    if type(value) is not bool:
        raise _error(location, "must be a boolean")
    return value


def _validate_provider_mapping(raw: object, index: int) -> _ProviderMapping:
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
    api_key = (
        _validate_api_key(raw["api_key"], f"{location}.api_key")
        if "api_key" in raw
        else None
    )
    protocol = _parse_protocol(raw, location)
    if protocol is Protocol.CODEX_RESPONSES and auth is not AuthMode.CODEX_CHATGPT:
        raise _error(
            f"{location}.auth",
            f"protocol {Protocol.CODEX_RESPONSES.value!r} requires auth "
            f"{AuthMode.CODEX_CHATGPT.value!r}",
        )
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
        if "base_url" not in raw:
            raise _error(location, "missing required field(s): base_url")
        base_url = _validate_base_url(raw["base_url"], f"{location}.base_url")
        api_key_env = (
            _validate_api_key_env(raw["api_key_env"], f"{location}.api_key_env", name)
            if "api_key_env" in raw
            else ""
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

    raw_requests_per_minute = raw.get("requests_per_minute")
    requests_per_minute: int | None
    if raw_requests_per_minute is None:
        # An rpm-only provider stays on the legacy path.  ProviderConfig
        # derives its conservative in-flight default while retaining rpm for
        # the transport token bucket.
        requests_per_minute = None
    else:
        requests_per_minute = _require_integer(
            raw_requests_per_minute, f"{location}.requests_per_minute"
        )
        if requests_per_minute <= 0:
            raise _error(f"{location}.requests_per_minute", "must be greater than 0")
        if "rpm" in raw and requests_per_minute != rpm:
            raise _error(
                location,
                "rpm and requests_per_minute must agree when both are declared",
            )

    enabled = values["enabled"]
    if not isinstance(enabled, bool):
        raise _error(f"{location}.enabled", "must be a boolean")

    required = values["required"]
    if not isinstance(required, bool):
        raise _error(f"{location}.required", "must be a boolean")

    model = _require_string(values["model"], f"{location}.model")
    if not model.strip():
        raise _error(f"{location}.model", "must not be blank")
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

    context_window = _require_integer(values["context_window"], f"{location}.context_window")
    if context_window < 0:
        raise _error(f"{location}.context_window", "must not be negative")
    max_concurrency = _require_integer(values["max_concurrency"], f"{location}.max_concurrency")
    if max_concurrency <= 0:
        raise _error(f"{location}.max_concurrency", "must be greater than 0")
    raw_max_in_flight = raw.get("max_in_flight")
    max_in_flight: int | None
    if raw_max_in_flight is None:
        max_in_flight = None
    else:
        max_in_flight = _require_integer(raw_max_in_flight, f"{location}.max_in_flight")
        if max_in_flight <= 0:
            raise _error(f"{location}.max_in_flight", "must be greater than 0")
        if "max_concurrency" in raw and max_concurrency != max_in_flight:
            raise _error(
                location,
                "max_concurrency and max_in_flight must agree when both are declared",
            )
    billing_mode = _parse_billing_mode(values["billing_mode"], f"{location}.billing_mode")
    quota_windows = _parse_quota_windows(raw.get("quota_windows", []), f"{location}.quota_windows")
    legacy_price = price
    price_per_1m_in = _require_number(
        raw.get("price_per_1m_in", legacy_price), f"{location}.price_per_1m_in"
    )
    price_per_1m_cached_in = _require_number(
        raw.get("price_per_1m_cached_in", price_per_1m_in),
        f"{location}.price_per_1m_cached_in",
    )
    price_per_1m_out = _require_number(
        raw.get("price_per_1m_out", legacy_price), f"{location}.price_per_1m_out"
    )
    for key, amount in (
        ("price_per_1m_in", price_per_1m_in),
        ("price_per_1m_cached_in", price_per_1m_cached_in),
        ("price_per_1m_out", price_per_1m_out),
    ):
        if amount < 0:
            raise _error(f"{location}.{key}", "must not be negative")
    pricing_known = _require_bool(
        raw.get(
            "pricing_known",
            "price" in raw
            or any(
                key in raw
                for key in (
                    "price_per_1m_in",
                    "price_per_1m_cached_in",
                    "price_per_1m_out",
                )
            ),
        ),
        f"{location}.pricing_known",
    )
    throughput_hint_tps = _require_number(
        values["throughput_hint_tps"], f"{location}.throughput_hint_tps"
    )
    raw_tokens_per_s = raw.get("tokens_per_s")
    tokens_per_s: float | None
    if raw_tokens_per_s is None:
        tokens_per_s = None
    else:
        tokens_per_s = _require_number(raw_tokens_per_s, f"{location}.tokens_per_s")
        if tokens_per_s < 0:
            raise _error(f"{location}.tokens_per_s", "must be non-negative")
        if "throughput_hint_tps" in raw and throughput_hint_tps not in (0, tokens_per_s):
            raise _error(
                location,
                "throughput_hint_tps and tokens_per_s must agree when both are declared",
            )
    interactive_wall_budget_value = values["interactive_wall_budget_s"]
    if interactive_wall_budget_value is None:
        interactive_wall_budget_s = None
    else:
        interactive_wall_budget_s = _require_number(
            interactive_wall_budget_value, f"{location}.interactive_wall_budget_s"
        )
        if interactive_wall_budget_s <= 0:
            raise _error(f"{location}.interactive_wall_budget_s", "must be greater than 0")
    if throughput_hint_tps < 0:
        raise _error(f"{location}.throughput_hint_tps", "must be non-negative")
    supports_native_tools = _require_bool(
        values["supports_native_tools"], f"{location}.supports_native_tools"
    )
    supports_python_tool = _require_bool(
        values["supports_python_tool"], f"{location}.supports_python_tool"
    )
    allow_model_substitution = _require_bool(
        values["allow_model_substitution"], f"{location}.allow_model_substitution"
    )
    cache_capability = _parse_cache_capability(raw, location)
    # ``price_per_1m_cached_in`` predates the typed capability object.  Keep it
    # as the effective read tariff when a provider has not supplied one in the
    # new cache block, so existing configurations participate in CAST pricing
    # without a migration.
    if (
        "price_per_1m_cached_in" in raw
        and "cache_read_price" not in raw
        and "cache_read_price_per_1m" not in raw
        and not any(
            isinstance(raw.get(key), dict)
            and any(
                field_name in raw[key]
                for field_name in ("cache_read_price", "cache_read_price_per_1m")
            )
            for key in ("cache_capability", "cache_capabilities", "cache")
        )
    ):
        try:
            cache_capability = CacheCapability(
                minimum_cacheable_tokens=cache_capability.minimum_cacheable_tokens,
                cache_ttl_s=cache_capability.cache_ttl_s,
                cache_granularity_tokens=cache_capability.cache_granularity_tokens,
                cache_read_price=_require_number(
                    raw["price_per_1m_cached_in"],
                    f"{location}.price_per_1m_cached_in",
                ),
                cache_write_price=cache_capability.cache_write_price,
            )
        except ValueError as exc:
            raise _error(f"{location}.price_per_1m_cached_in", str(exc)) from exc

    # Optional Responses-API reasoning effort (codex_responses providers); an
    # absent value keeps the request body free of the reasoning field.
    reasoning_effort = raw.get("reasoning_effort")
    if reasoning_effort is not None:
        if not isinstance(reasoning_effort, str) or not reasoning_effort.strip():
            raise _error(f"{location}.reasoning_effort", "must be a non-empty string")
        if protocol is not Protocol.CODEX_RESPONSES:
            raise _error(
                f"{location}.reasoning_effort",
                f"is only supported with protocol {Protocol.CODEX_RESPONSES.value!r}",
            )
        reasoning_effort = reasoning_effort.strip()

    return {
        "name": name,
        "tier": tier_value,
        "base_url": base_url,
        "api_key": api_key,
        "api_key_env": api_key_env,
        "required": required,
        "timeout_s": timeout_s,
        "max_retries": max_retries,
        "rpm": rpm,
        "requests_per_minute": requests_per_minute,
        "enabled": enabled,
        "model": model,
        "priority": priority,
        "cooldown_s": cooldown_s,
        "price": price,
        "token_window_allowance": token_window_allowance,
        "auth": auth,
        "protocol": protocol,
        "context_window": context_window,
        "reasoning_effort": reasoning_effort,
        "max_concurrency": max_concurrency,
        "max_in_flight": max_in_flight,
        "billing_mode": billing_mode,
        "quota_windows": quota_windows,
        "price_per_1m_in": price_per_1m_in,
        "price_per_1m_cached_in": price_per_1m_cached_in,
        "price_per_1m_out": price_per_1m_out,
        "pricing_known": pricing_known,
        "throughput_hint_tps": throughput_hint_tps,
        "tokens_per_s": tokens_per_s,
        "interactive_wall_budget_s": interactive_wall_budget_s,
        "supports_native_tools": supports_native_tools,
        "supports_python_tool": supports_python_tool,
        "allow_model_substitution": allow_model_substitution,
        "cache_capability": cache_capability,
    }


def _validated_provider_mappings(
    raw: object,
    *,
    source: Path | None = None,
    quarantined: list[dict[str, object]] | None = None,
) -> list[_ProviderMapping]:
    if not isinstance(raw, dict):
        raise _error("root", "must be an object with a 'providers' field")

    unknown = sorted(set(raw) - _TOP_LEVEL_FIELDS)
    if unknown:
        raise _error("root", f"unknown field(s): {', '.join(map(repr, unknown))}")

    if "providers" not in raw:
        raise _error("root", "missing required field(s): providers")
    entries = raw.get("providers")
    if not isinstance(entries, list):
        raise _error("providers", "must be a list")

    # Duplicate names are a document-level invariant and remain fatal even if
    # one of the duplicate entries would otherwise be quarantined.
    names_by_value: dict[str, int] = {}
    for index, entry in enumerate(entries):
        if not isinstance(entry, dict):
            continue
        name = entry.get("name")
        if not isinstance(name, str):
            continue
        if name in names_by_value:
            raise _error(f"providers[{index}].name", f"duplicate provider name {name!r}")
        names_by_value[name] = index

    mappings: list[_ProviderMapping] = []
    names: set[str] = set()
    env_names: dict[str, str] = {}
    records = quarantined if quarantined is not None else []
    for index, entry in enumerate(entries):
        try:
            mapping = _validate_provider_mapping(entry, index)
        except ValueError as exc:
            if source is None:
                raise
            records.append(_quarantine_record(entry, str(exc)))
            continue
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
    if source is not None and records:
        quarantine_path = _append_quarantine(source, records)
        if quarantine_path is not None:
            _log_quarantine(source, quarantine_path, records)
    return mappings


def validate_provider_specs(raw: object) -> tuple[ProviderEnvSpec, ...]:
    """Validate a provider document without importing the Diffundo router."""

    return tuple(
        ProviderEnvSpec(
            name=mapping["name"],
            api_key_env=mapping["api_key_env"],
            required=mapping["required"],
            api_key=mapping["api_key"],
        )
        for mapping in _validated_provider_mappings(raw)
    )


def _provider_from_values(values: _ProviderMapping, index: int) -> ProviderConfig:
    ProviderConfigType, ProviderTier = _diffundo_types()
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
        "api_key": values["api_key"],
        "api_key_env": values["api_key_env"],
        "timeout_s": values["timeout_s"],
        "max_retries": values["max_retries"],
        "rpm": values["rpm"],
        "requests_per_minute": values["requests_per_minute"],
        "enabled": values["enabled"],
        "model": values["model"],
        "priority": values["priority"],
        "cooldown_s": values["cooldown_s"],
        "token_window_allowance": values["token_window_allowance"],
        "auth": values["auth"],
        "protocol": values["protocol"],
        "context_window": values["context_window"],
        "reasoning_effort": values["reasoning_effort"],
        "max_concurrency": values["max_concurrency"],
        "max_in_flight": values["max_in_flight"],
        "billing_mode": values["billing_mode"],
        "quota_windows": values["quota_windows"],
        "price_per_1m_cached_in": values["price_per_1m_cached_in"],
        "cache_capability": values["cache_capability"],
        "pricing_known": values["pricing_known"],
        "throughput_hint_tps": values["throughput_hint_tps"],
        "tokens_per_s": values["tokens_per_s"],
        "interactive_wall_budget_s": values["interactive_wall_budget_s"],
        "supports_native_tools": values["supports_native_tools"],
        "supports_python_tool": values["supports_python_tool"],
        "allow_model_substitution": values["allow_model_substitution"],
    }
    price = values["price"]
    provider_fields = {field.name for field in fields(ProviderConfigType)}
    if {"price_per_1m_in", "price_per_1m_out"} <= provider_fields:
        config_values["price_per_1m_in"] = values["price_per_1m_in"]
        config_values["price_per_1m_out"] = values["price_per_1m_out"]
    elif "price" in provider_fields:
        config_values["price"] = price
    else:
        raise RuntimeError("ProviderConfig has no supported price field")

    provider_constructor = cast(Callable[..., "ProviderConfig"], ProviderConfigType)
    return provider_constructor(**config_values)


def _read_config(source: str | Path | None) -> object:
    path = _config_path(source)

    if not path.is_file():
        raise FileNotFoundError(
            f"provider config file not found: {path} "
            f"(set CAMBIUM_PROVIDERS or create {DEFAULT_PROVIDER_PATH})"
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

    path = _config_path(source)
    records: list[dict[str, object]] = []
    mappings = _validated_provider_mappings(_read_config(path), source=path, quarantined=records)
    return tuple(
        ProviderEnvSpec(
            name=mapping["name"],
            api_key_env=mapping["api_key_env"],
            required=mapping["required"],
            api_key=mapping["api_key"],
        )
        for mapping in mappings
    )


def load_providers(source: str | Path | None = None) -> list[ProviderConfig]:
    """Load providers, quarantining invalid entries from a JSON config file.

    ``source`` overrides ``CAMBIUM_PROVIDERS``. With neither set, the loader
    reads :data:`DEFAULT_PROVIDER_PATH`, under the effective user's home. The
    presence of each ``api_key_env`` variable is intentionally not checked;
    Diffundo resolves key values at call time. File-level and document-level
    structural errors remain fatal; individual provider schema errors are
    recorded in ``<source>.quarantine`` and omitted from the returned list.
    """
    path = _config_path(source)
    records: list[dict[str, object]] = []
    mappings = _validated_provider_mappings(_read_config(path), source=path, quarantined=records)
    providers: list[ProviderConfig] = []
    for index, mapping in enumerate(mappings):
        providers.append(_provider_from_values(mapping, index))

    return _LoadedProviders(
        providers,
        quarantine_path=provider_quarantine_path(path) if records else None,
        quarantined_count=len(records),
    )


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
    quarantine_path = all_providers_quarantined_path(providers)
    if quarantine_path is not None:
        raise ProviderSelectionError(
            f"all providers quarantined to {quarantine_path}; fix or remove entries"
        )
    if name is not None:
        for provider in providers:
            if provider.name != name:
                continue
            if not provider.enabled:
                raise ProviderSelectionError(f"provider selection: provider {name!r} is disabled")
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
    providers: Sequence[ProviderConfig | ProviderEnvSpec],
) -> dict[str, bool]:
    """Return whether each provider's file-backed key is usable.

    The report contains only provider names and booleans. It never returns an
    environment-variable name or credential value.
    """
    return {provider.name: bool(provider.api_key) for provider in providers}


__all__ = [
    "AuthMode",
    "CODEX_CHATGPT_PROFILE",
    "DEFAULT_PROVIDER_PATH",
    "DEFAULT_SAMPLE",
    "Protocol",
    "ProviderEnvSpec",
    "ProviderSelectionError",
    "all_providers_quarantined_path",
    "env_report",
    "is_loopback_host",
    "load_provider_specs",
    "load_providers",
    "provider_quarantine_notice",
    "provider_quarantine_path",
    "select_provider",
    "validate_provider_specs",
]
