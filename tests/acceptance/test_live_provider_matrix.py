"""Opt-in live-provider acceptance checks.

The tests in this module deliberately keep live credentials at the acceptance
boundary. A test skips until its named provider configuration and credential
are available. API keys may be supplied through the provider's configured
environment variable or read-only from the local OpenCode and pi auth stores.
When ``CAMBIUM_ACCEPTANCE_ALLOW_MUTATION=1`` is set, the acceptance conftest
copies the pi Codex OAuth record into a private temporary store for each
Codex check. It never points a test at the developer's normal store. No token
value is constructed, printed, or placed in a test constant.

Run the suite with ``python -m pytest -m acceptance tests/acceptance -s``.
The ``-s`` is useful only for an operator-supplied interactive fresh-login
command; all other checks keep their normal pytest capture behavior.
"""

from __future__ import annotations

import asyncio
import json
import multiprocessing
import os
import shlex
import subprocess
import time
from collections.abc import Mapping
from dataclasses import dataclass, replace
from pathlib import Path
from queue import Empty
from typing import Any

import pytest

from cambium import supervisor
from cambium.auth import effective_home, oauth_env_suffix
from cambium.diffundo import (
    AllProvidersFailed,
    CredentialSource,
    Diffundo,
    ProviderConfig,
    ProviderError,
    ProviderOutcome,
)
from cambium.oauth import (
    DEFAULT_REFRESH_MARGIN_S,
    InvalidGrantError,
    OAuthError,
    OAuthStore,
    TokenManager,
    oauth_store_path,
)
from cambium.provider_config import (
    DEFAULT_PROVIDER_PATH,
    AuthMode,
    Protocol,
    load_providers,
)
from cambium.provider_scheduler import BillingMode

CODEX_CONFIG_ENV = "CAMBIUM_ACCEPTANCE_CODEX_CONFIG"
CODEX_PROVIDER_ENV = "CAMBIUM_ACCEPTANCE_CODEX_PROVIDER"
CODEX_STORE_ENV = "CAMBIUM_ACCEPTANCE_CODEX_OAUTH_STORE"
CODEX_FRESH_STORE_ENV = "CAMBIUM_ACCEPTANCE_CODEX_FRESH_STORE"
CODEX_EXPIRED_STORE_ENV = "CAMBIUM_ACCEPTANCE_CODEX_EXPIRED_STORE"
CODEX_ROTATED_STORE_ENV = "CAMBIUM_ACCEPTANCE_CODEX_ROTATED_STORE"
CODEX_REVOKED_STORE_ENV = "CAMBIUM_ACCEPTANCE_CODEX_REVOKED_STORE"
CODEX_CONCURRENT_STORE_ENV = "CAMBIUM_ACCEPTANCE_CODEX_CONCURRENT_STORE"
CODEX_RESTART_STORE_ENV = "CAMBIUM_ACCEPTANCE_CODEX_RESTART_STORE"
CODEX_LOGIN_COMMAND_ENV = "CAMBIUM_ACCEPTANCE_CODEX_LOGIN_COMMAND"
CODEX_CLIENT_ID_ENV = "CAMBIUM_ACCEPTANCE_CODEX_CLIENT_ID"
ALLOW_MUTATION_ENV = "CAMBIUM_ACCEPTANCE_ALLOW_MUTATION"
ALLOW_OAUTH_MUTATIONS_ENV = "CAMBIUM_ACCEPTANCE_ALLOW_OAUTH_MUTATIONS"
CODEX_FIXTURE_ROOT_ENV = "CAMBIUM_ACCEPTANCE_CODEX_FIXTURE_ROOT"
CODEX_PI_AUTH_ENV = "CAMBIUM_ACCEPTANCE_CODEX_PI_AUTH"
QUOTA_DB_ENV = "CAMBIUM_ACCEPTANCE_QUOTA_DB"
PROBE_TIMEOUT_ENV = "CAMBIUM_ACCEPTANCE_TIMEOUT_S"

ZAI_CONFIG_ENV = "CAMBIUM_ACCEPTANCE_ZAI_CONFIG"
ZAI_PROVIDER_ENV = "CAMBIUM_ACCEPTANCE_ZAI_PROVIDER"
OPENROUTER_CONFIG_ENV = "CAMBIUM_ACCEPTANCE_OPENROUTER_CONFIG"
OPENROUTER_PAID_PROVIDER_ENV = "CAMBIUM_ACCEPTANCE_OPENROUTER_PAID_PROVIDER"
OPENROUTER_FREE_PROVIDER_ENV = "CAMBIUM_ACCEPTANCE_OPENROUTER_FREE_PROVIDER"
OPENCODE_ZEN_CONFIG_ENV = "CAMBIUM_ACCEPTANCE_OPENCODE_ZEN_CONFIG"
OPENCODE_ZEN_PROVIDER_ENV = "CAMBIUM_ACCEPTANCE_OPENCODE_ZEN_PROVIDER"
CACHE_CONFIG_ENV = "CAMBIUM_ACCEPTANCE_CACHE_CONFIG"
CACHE_PROVIDER_ENV = "CAMBIUM_ACCEPTANCE_CACHE_PROVIDER"
OPENCODE_AUTH_ENV = "CAMBIUM_ACCEPTANCE_OPENCODE_AUTH"
PI_AUTH_ENV = "CAMBIUM_ACCEPTANCE_PI_AUTH"

_MUTATING_CODEX_STORE_ENVS = frozenset(
    {
        CODEX_EXPIRED_STORE_ENV,
        CODEX_ROTATED_STORE_ENV,
        CODEX_REVOKED_STORE_ENV,
        CODEX_CONCURRENT_STORE_ENV,
    }
)

_AUTH_SOURCE_NAMES = ("OpenCode", "pi")
_AUTH_SOURCE_ENV = (OPENCODE_AUTH_ENV, PI_AUTH_ENV)
_AUTH_SOURCE_RELATIVE_PATHS = (
    Path(".local/share/opencode/auth.json"),
    Path(".pi/agent/auth.json"),
)
_PROVIDER_AUTH_ALIASES: dict[str, tuple[str, ...]] = {
    # OpenCode's credential store calls the Zen API key ``opencode-go`` while
    # Cambium's provider profile is named ``opencode-zen``. Keep the alias
    # here, at the acceptance boundary, rather than changing the production
    # provider identifiers.
    "opencode": ("opencode-zen", "opencode-go"),
    "opencode-go": ("opencode-zen", "opencode"),
    "opencode-zen": ("opencode-go", "opencode"),
    "zai": ("zai-coding-plan",),
    "zai-coding-plan": ("zai",),
}

# z.ai's coding plan exposes shared five-hour and seven-day rolling pools.
# These are deliberately conservative acceptance-side capacities: the probe
# verifies that Cambium carries both rolling reset windows through the live
# call, while the disposable local ledger prevents the check from touching a
# developer's normal quota state.
_ZAI_STANDARD_QUOTA_WINDOWS: tuple[dict[str, object], ...] = (
    {
        "name": "five-hour",
        "duration_s": 5 * 60 * 60,
        "token_allowance": 1_000_000,
        "reserve_fraction": 0.05,
    },
    {
        "name": "weekly",
        "duration_s": 7 * 24 * 60 * 60,
        "token_allowance": 5_000_000,
    },
)

_PROBE_PROMPT = {
    "messages": [
        {
            "role": "user",
            "content": "Reply with exactly one short word: acceptance.",
        }
    ]
}


@dataclass(frozen=True, slots=True)
class _ProviderContext:
    config_path: Path
    provider: ProviderConfig
    config_entry: dict[str, object]
    using_default_config: bool = False


@dataclass(frozen=True, slots=True)
class _CodexContext:
    provider_context: _ProviderContext
    store_path: Path
    store: OAuthStore

    @property
    def provider(self) -> ProviderConfig:
        return self.provider_context.provider

    @property
    def config_path(self) -> Path:
        return self.provider_context.config_path


def _required_env(name: str) -> str:
    value = os.environ.get(name, "").strip()
    if not value:
        pytest.skip(f"set {name} to opt in to this live acceptance check")
    return value


def _auth_source_paths() -> tuple[Path, ...]:
    """Return the supported local auth paths without reading their contents."""

    try:
        home = effective_home()
    except OSError:
        home = Path.home()
    paths: list[Path] = []
    for environment_name, relative_path in zip(
        _AUTH_SOURCE_ENV, _AUTH_SOURCE_RELATIVE_PATHS, strict=True
    ):
        configured = os.environ.get(environment_name, "").strip()
        paths.append(Path(configured).expanduser() if configured else home / relative_path)
    return tuple(paths)


def _read_api_key_entries(path: Path) -> Mapping[str, object]:
    """Read provider metadata from one auth store without exposing key values.

    The OpenCode store uses ``{"type": "api", "key": ...}``, while the pi
    store uses ``{"type": "api_key", "key": ...}``. The acceptance harness
    needs only the common ``key`` field and intentionally ignores OAuth-shaped
    ``access``/``refresh`` records. Missing or malformed optional sources act
    like an unavailable credential and therefore preserve skip-gating.
    """

    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}
    if not isinstance(raw, dict):
        return {}
    entries: dict[str, object] = {}
    for provider, entry in raw.items():
        if not isinstance(provider, str) or not isinstance(entry, dict):
            continue
        key = entry.get("key")
        if isinstance(key, str) and key:
            entries[provider] = key
    return entries


def _auth_provider_names(provider_name: str) -> tuple[str, ...]:
    """Return exact and compatibility names used by local auth stores."""

    names = (provider_name, *_PROVIDER_AUTH_ALIASES.get(provider_name.casefold(), ()))
    return tuple(dict.fromkeys(names))


def _api_key_from_auth_sources(provider_name: str) -> str | None:
    """Find one API key for ``provider_name`` in the supported auth stores."""

    names = _auth_provider_names(provider_name)
    for path in _auth_source_paths():
        entries = _read_api_key_entries(path)
        for name in names:
            value = entries.get(name)
            if isinstance(value, str) and value:
                return value
    return None


def _api_key_for_provider(provider: ProviderConfig) -> str | None:
    """Resolve a configured API key from the environment or local auth stores."""

    if provider.api_key_env:
        configured = os.environ.get(provider.api_key_env, "")
        if configured:
            return configured
    return _api_key_from_auth_sources(provider.name)


def _required_file(name: str) -> Path:
    path = Path(_required_env(name)).expanduser()
    if not path.is_file():
        pytest.fail(f"{name} must name an existing file")
    return path


def _config_file(name: str) -> Path:
    """Use an explicit acceptance config or the normal local provider file."""

    configured = os.environ.get(name, "").strip()
    if configured:
        return _required_file(name)
    if not DEFAULT_PROVIDER_PATH.is_file():
        pytest.skip(f"set {name} or create the default provider config")
    return DEFAULT_PROVIDER_PATH


def _load_config_entries(path: Path) -> list[dict[str, object]]:
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        pytest.fail(f"provider config cannot be read: {type(exc).__name__}")
    entries = raw.get("providers") if isinstance(raw, dict) else None
    if not isinstance(entries, list):
        pytest.fail("provider config does not contain a providers list")
    return [entry for entry in entries if isinstance(entry, dict)]


def _load_config_entry(path: Path, provider_name: str) -> dict[str, object]:
    for entry in _load_config_entries(path):
        if entry.get("name") == provider_name:
            return entry
    pytest.fail("provider config does not contain the requested acceptance provider")


def _default_provider_name(provider_env: str, entries: list[dict[str, object]]) -> str | None:
    """Choose a conservative default provider from a local config.

    Explicit provider variables remain authoritative. These defaults only
    remove boilerplate for the checked-in family names and never invent a
    paid/cache lane when the config does not describe one.
    """

    if provider_env == CODEX_PROVIDER_ENV:
        return "codex"
    if provider_env == ZAI_PROVIDER_ENV:
        return "zai"
    if provider_env == OPENCODE_ZEN_PROVIDER_ENV:
        return "opencode-zen"
    if provider_env == CACHE_PROVIDER_ENV:
        for entry in entries:
            name = entry.get("name")
            if (
                isinstance(name, str)
                and "price_per_1m_cached_in" in entry
                and entry.get("pricing_known") is True
            ):
                return name
        return "opencode-zen"
    if provider_env in (OPENROUTER_FREE_PROVIDER_ENV, OPENROUTER_PAID_PROVIDER_ENV):
        candidates: list[dict[str, object]] = []
        for entry in entries:
            name = entry.get("name")
            if not isinstance(name, str) or "openrouter" not in name.casefold():
                continue
            billing = entry.get("billing_mode")
            model = entry.get("model")
            is_free = billing == "free" or (
                isinstance(model, str) and model.casefold().endswith(":free")
            )
            if provider_env == OPENROUTER_FREE_PROVIDER_ENV and is_free:
                candidates.append(entry)
            elif (
                provider_env == OPENROUTER_PAID_PROVIDER_ENV
                and billing
                in {
                    "metered",
                    "subscription",
                }
                and not is_free
            ):
                candidates.append(entry)
        if candidates:
            name = candidates[0].get("name")
            return name if isinstance(name, str) else None
        return None
    return None


def _provider_context(config_env: str, provider_env: str) -> _ProviderContext:
    using_default_config = not os.environ.get(config_env, "").strip()
    config_path = _config_file(config_env)
    entries = _load_config_entries(config_path)
    provider_name = os.environ.get(provider_env, "").strip()
    if not provider_name:
        provider_name = _default_provider_name(provider_env, entries) or ""
    if not provider_name:
        pytest.skip(f"set {provider_env} to select a configured acceptance provider")
    try:
        providers = load_providers(config_path)
    except (OSError, ValueError) as exc:
        pytest.fail(f"{config_env} is invalid: {type(exc).__name__}: {exc}")
    provider = next((item for item in providers if item.name == provider_name), None)
    if provider is None:
        pytest.fail(f"{provider_env} does not name a provider in {config_env}")
    return _ProviderContext(
        config_path=config_path,
        provider=provider,
        config_entry=_load_config_entry(config_path, provider_name),
        using_default_config=using_default_config,
    )


def _synthesize_zai_provider_config(
    context: _ProviderContext, destination: Path
) -> _ProviderContext:
    """Build a disposable one-provider config for an old local z.ai entry.

    Local provider files predate the quota-window metadata, so adding that
    metadata in the acceptance harness must not edit the user's trusted
    config. The copied entry contains only provider settings and environment
    variable names; credentials remain in the process environment.
    """

    config_entry = dict(context.config_entry)
    config_entry["quota_windows"] = [dict(window) for window in _ZAI_STANDARD_QUOTA_WINDOWS]
    config_path = destination / "zai-acceptance-providers.json"
    try:
        config_path.write_text(
            json.dumps({"providers": [config_entry]}, indent=2) + "\n",
            encoding="utf-8",
        )
        providers = load_providers(config_path)
    except (OSError, ValueError) as exc:
        pytest.fail(f"synthesized z.ai acceptance config is invalid: {type(exc).__name__}: {exc}")
    provider = next((item for item in providers if item.name == context.provider.name), None)
    if provider is None:
        pytest.fail("synthesized z.ai acceptance config lost the selected provider")
    return _ProviderContext(
        config_path=config_path,
        provider=provider,
        config_entry=config_entry,
        using_default_config=True,
    )


def _api_provider_context(
    config_env: str,
    provider_env: str,
    *,
    generated_config_dir: Path | None = None,
) -> _ProviderContext:
    context = _provider_context(config_env, provider_env)
    if context.provider.auth is not AuthMode.API_KEY:
        pytest.fail("live provider skeletons require an api_key provider entry")
    if not _api_key_for_provider(context.provider):
        pytest.skip(
            f"set {context.provider.api_key_env} or add {context.provider.name} "
            "to the supported OpenCode/pi auth stores"
        )
    if context.using_default_config and provider_env == ZAI_PROVIDER_ENV:
        if not context.provider.quota_windows:
            if generated_config_dir is None:
                pytest.fail("z.ai acceptance config synthesis requires a temporary directory")
            context = _synthesize_zai_provider_config(context, generated_config_dir)
    if (
        context.using_default_config
        and provider_env == OPENROUTER_FREE_PROVIDER_ENV
        and context.provider.model.casefold().endswith(":free")
        and context.provider.billing_mode is BillingMode.METERED
    ):
        # Older local provider files predate the billing metadata used by the
        # routing requirements. A :free model is an unambiguous compatibility
        # signal, but only infer it for the implicit local config; an explicit
        # acceptance config remains strict and fails if its metadata is wrong.
        context = replace(
            context, provider=replace(context.provider, billing_mode=BillingMode.FREE)
        )
    return context


def _codex_provider_context() -> _ProviderContext:
    context = _provider_context(CODEX_CONFIG_ENV, CODEX_PROVIDER_ENV)
    if context.provider.auth is not AuthMode.CODEX_CHATGPT:
        pytest.fail("Codex acceptance requires auth=codex_chatgpt")
    if context.provider.protocol is not Protocol.CODEX_RESPONSES:
        pytest.fail("Codex acceptance requires protocol=codex_responses")
    return context


def _codex_context(store_env: str = CODEX_STORE_ENV) -> _CodexContext:
    provider_context = _codex_provider_context()
    store_path = _required_file(store_env)
    if store_env in _MUTATING_CODEX_STORE_ENVS:
        _assert_disposable_store(store_path, store_env)
    store = OAuthStore(store_path)
    try:
        record = store.read_provider(provider_context.provider.name)
    except OAuthError as exc:
        pytest.fail(f"{store_env} cannot be read: {type(exc).__name__}")
    if record is None:
        pytest.fail(f"{store_env} has no record for the configured Codex provider")
    if record.disabled:
        pytest.fail(f"{store_env} is already disabled; use a fresh disposable store")
    return _CodexContext(provider_context, store_path, store)


def _client_id() -> str | None:
    return os.environ.get(CODEX_CLIENT_ID_ENV) or None


def _probe_timeout() -> float:
    raw = os.environ.get(PROBE_TIMEOUT_ENV, "90")
    try:
        value = float(raw)
    except ValueError:
        pytest.fail(f"{PROBE_TIMEOUT_ENV} must be a positive number")
    if value <= 0:
        pytest.fail(f"{PROBE_TIMEOUT_ENV} must be a positive number")
    return value


def _manager(context: _CodexContext) -> TokenManager:
    return TokenManager(
        context.provider.name,
        store=context.store,
        client_id=_client_id(),
        refresh_timeout_s=_probe_timeout(),
    )


def _fresh_doc(context: _CodexContext) -> Any:
    try:
        doc = context.store.validate(context.provider.name)
    except OAuthError as exc:
        pytest.fail(f"{context.store_path} is not a usable OAuth store: {type(exc).__name__}")
    if doc.expires_at - time.time() <= DEFAULT_REFRESH_MARGIN_S:
        pytest.fail("acceptance store does not contain a valid, unexpired access token")
    return doc


def _expired_doc(context: _CodexContext) -> Any:
    try:
        doc = context.store.validate(context.provider.name)
    except OAuthError as exc:
        pytest.fail(f"{context.store_path} is not a usable OAuth store: {type(exc).__name__}")
    if doc.expires_at > time.time():
        pytest.fail("acceptance store does not contain an expired access token")
    return doc


def _live_prompt() -> dict[str, object]:
    return json.loads(json.dumps(_PROBE_PROMPT))


def _skip_if_provider_unavailable(error: BaseException) -> None:
    """Skip live probes only when the provider infrastructure is unavailable."""

    provider_error: BaseException | None = error
    if isinstance(error, AllProvidersFailed):
        provider_error = error.last_error
    if not isinstance(provider_error, ProviderError):
        return

    status = provider_error.http_status
    if status is not None:
        if not 500 <= status <= 599:
            return
        detail = f"HTTP {status}"
    elif provider_error.outcome is ProviderOutcome.TIMEOUT:
        detail = "request timeout"
    elif provider_error.outcome is ProviderOutcome.ERROR and provider_error.is_real_death:
        detail = "connection failure"
    else:
        return
    pytest.skip(
        f"live acceptance skipped: provider {provider_error.provider!r} "
        f"endpoint unavailable ({detail})"
    )


def _codex_probe(
    context: _CodexContext,
    access_token: str,
    account_id: str | None,
) -> Any:
    router = Diffundo(
        (context.provider,),
        call_budget_s=_probe_timeout(),
        pause_timeout_s=min(5.0, _probe_timeout()),
        credential_source=CredentialSource(access_token, account_id),
    )
    try:
        return asyncio.run(
            router.call(
                context.provider.tier,
                _live_prompt(),
                model=context.provider.model,
            )
        )
    except (AllProvidersFailed, ProviderError) as exc:
        _skip_if_provider_unavailable(exc)
        raise


def _api_router(context: _ProviderContext, monkeypatch: pytest.MonkeyPatch) -> Diffundo:
    api_key = _api_key_for_provider(context.provider)
    if not api_key:
        pytest.skip(
            f"set {context.provider.api_key_env} or add {context.provider.name} "
            "to the supported OpenCode/pi auth stores"
        )
    # ``api_key_env`` remains a live-harness discovery fallback for real
    # operator credentials; runtime calls use the file-backed provider value.
    provider = replace(context.provider, api_key=api_key)
    quota_db = os.environ.get(QUOTA_DB_ENV)
    if context.provider.quota_windows and not quota_db:
        pytest.skip(f"set {QUOTA_DB_ENV} to an isolated quota database path")
    if quota_db:
        monkeypatch.setenv("CAMBIUM_QUOTA_DB", quota_db)
    return Diffundo(
        (provider,),
        call_budget_s=_probe_timeout(),
        pause_timeout_s=min(5.0, _probe_timeout()),
        task_id="acceptance",
    )


def _api_probe(
    context: _ProviderContext,
    monkeypatch: pytest.MonkeyPatch,
    *,
    requirements: Mapping[str, object] | None = None,
) -> Any:
    router = _api_router(context, monkeypatch)
    try:
        return asyncio.run(
            router.call(
                context.provider.tier,
                _live_prompt(),
                model=context.provider.model,
                requirements=requirements,
            )
        )
    except (AllProvidersFailed, ProviderError) as exc:
        _skip_if_provider_unavailable(exc)
        raise


def _assert_provider_result(result: Any, provider: ProviderConfig) -> None:
    assert result.provider == provider.name
    assert result.model == provider.model
    assert isinstance(result.usage, dict), "live response did not report usage"


def _allow_oauth_mutation() -> None:
    if (
        os.environ.get(ALLOW_MUTATION_ENV) != "1"
        and os.environ.get(ALLOW_OAUTH_MUTATIONS_ENV) != "1"
    ):
        pytest.skip(f"set {ALLOW_MUTATION_ENV}=1 only with disposable copied OAuth stores/accounts")


def _fixture_root() -> Path:
    configured = os.environ.get(CODEX_FIXTURE_ROOT_ENV, "").strip()
    if not configured:
        pytest.fail(
            "Codex OAuth mutation checks require the disposable fixture; "
            f"set {ALLOW_MUTATION_ENV}=1"
        )
    return Path(configured).expanduser().resolve()


def _assert_disposable_store(path: Path, store_env: str) -> None:
    """Refuse any mutation store that was not created by the fixture helper."""

    resolved = path.expanduser().resolve()
    root = _fixture_root()
    if not resolved.is_relative_to(root):
        pytest.fail(f"{store_env} is not inside the disposable Codex OAuth fixture")
    if resolved == oauth_store_path().resolve():
        pytest.fail(f"{store_env} resolves to the production OAuth store")
    source_candidates = [
        os.environ.get(CODEX_PI_AUTH_ENV, "").strip(),
        os.environ.get(PI_AUTH_ENV, "").strip(),
    ]
    source = next((Path(value).expanduser() for value in source_candidates if value), None)
    if source is None:
        source = Path.home() / ".pi" / "agent" / "auth.json"
    if resolved == source.resolve():
        pytest.fail(f"{store_env} points at the read-only pi OAuth source")


def _fresh_store_target() -> Path:
    target = Path(_required_env(CODEX_FRESH_STORE_ENV)).expanduser()
    if target.exists():
        pytest.fail("fresh-login target must not already exist")
    if target.resolve() == oauth_store_path().resolve():
        pytest.fail("fresh-login refuses to use the production OAuth store")
    fixture_root = os.environ.get(CODEX_FIXTURE_ROOT_ENV, "").strip()
    if fixture_root and not target.resolve().is_relative_to(Path(fixture_root).resolve()):
        pytest.fail("fresh-login target must be inside the disposable fixture")
    return target


def _child_ensure_fresh(
    store_path: str,
    provider: str,
    client_id: str | None,
    timeout_s: float,
    ready: Any,
    start: Any,
    results: Any,
) -> None:
    ready.set()
    if not start.wait(timeout_s):
        results.put(("error", "startup_timeout"))
        return
    try:
        manager = TokenManager(
            provider,
            store=OAuthStore(Path(store_path)),
            client_id=client_id,
            refresh_timeout_s=timeout_s,
        )
        _access, account_id = manager.ensure_fresh()
    except Exception as exc:  # pragma: no cover - exercised in child processes
        results.put(("error", type(exc).__name__))
    else:
        results.put(("ok", account_id is not None))


def _run_ensure_processes(
    context: _CodexContext,
    *,
    count: int,
    concurrent: bool,
) -> list[tuple[str, object]]:
    process_context = multiprocessing.get_context("spawn")
    start = process_context.Event()
    results = process_context.Queue()
    ready_events = [process_context.Event() for _ in range(count)]
    processes = [
        process_context.Process(
            target=_child_ensure_fresh,
            args=(
                str(context.store_path),
                context.provider.name,
                _client_id(),
                _probe_timeout(),
                ready,
                start,
                results,
            ),
        )
        for ready in ready_events
    ]
    try:
        for process in processes:
            process.start()
        for ready in ready_events:
            assert ready.wait(_probe_timeout()), "child did not reach OAuth startup"
        if concurrent:
            start.set()
        else:
            start.set()
        deadline = time.monotonic() + max(30.0, _probe_timeout() * 2)
        for process in processes:
            remaining = max(0.0, deadline - time.monotonic())
            process.join(remaining)
        if any(process.is_alive() for process in processes):
            pytest.fail("OAuth child process did not exit within the acceptance timeout")
        outcomes: list[tuple[str, object]] = []
        for _ in processes:
            try:
                outcomes.append(results.get(timeout=5.0))
            except Empty:
                pytest.fail("OAuth child process returned no acceptance result")
        assert all(process.exitcode == 0 for process in processes)
        return outcomes
    finally:
        for process in processes:
            if process.is_alive():
                process.terminate()
            process.join(timeout=5.0)
        results.close()
        results.join_thread()


def _cached_tokens(usage: Mapping[str, object] | None) -> int | None:
    if not isinstance(usage, Mapping):
        return None
    for details_name in ("prompt_tokens_details", "input_tokens_details"):
        details = usage.get(details_name)
        if isinstance(details, Mapping):
            value = details.get("cached_tokens")
            if isinstance(value, int) and not isinstance(value, bool) and value >= 0:
                return value
    for name in ("cache_read_input_tokens", "cached_tokens"):
        value = usage.get(name)
        if isinstance(value, int) and not isinstance(value, bool) and value >= 0:
            return value
    return None


@pytest.mark.acceptance
def test_codex_fresh_login() -> None:
    """Run an operator-supplied device login and validate its stored session."""
    login_command = os.environ.get(CODEX_LOGIN_COMMAND_ENV, "").strip()
    if not login_command:
        pytest.skip(
            f"set {CODEX_LOGIN_COMMAND_ENV} to run the interactive Codex device-consent "
            "flow; fresh login is never seeded from a copied credential"
        )
    _allow_oauth_mutation()
    provider_context = _codex_provider_context()
    try:
        command = shlex.split(login_command)
    except ValueError as exc:
        pytest.fail(f"{CODEX_LOGIN_COMMAND_ENV} is not a valid command: {exc}")
    if not command:
        pytest.fail(f"{CODEX_LOGIN_COMMAND_ENV} must contain an executable command")
    target = _fresh_store_target()
    child_environment = os.environ.copy()
    child_environment.update(
        {
            CODEX_FRESH_STORE_ENV: str(target),
            CODEX_CONFIG_ENV: str(provider_context.config_path),
            CODEX_PROVIDER_ENV: provider_context.provider.name,
        }
    )
    completed = subprocess.run(
        command,
        env=child_environment,
        timeout=max(900.0, _probe_timeout()),
        check=False,
    )
    assert completed.returncode == 0
    store = OAuthStore(target)
    try:
        doc = store.validate(provider_context.provider.name)
    except OAuthError as exc:
        pytest.fail(f"fresh-login command did not create a valid OAuth store: {type(exc).__name__}")
    assert doc.expires_at > time.time()
    manager = TokenManager(
        provider_context.provider.name,
        store=store,
        client_id=_client_id(),
        refresh_timeout_s=_probe_timeout(),
    )
    access_token, account_id = manager.ensure_fresh()
    result = _codex_probe(
        _CodexContext(provider_context, target, store),
        access_token,
        account_id,
    )
    _assert_provider_result(result, provider_context.provider)


@pytest.mark.acceptance
def test_codex_valid_stored_token() -> None:
    context = _codex_context()
    doc = _fresh_doc(context)
    before = context.store_path.read_bytes()
    access_token, account_id = _manager(context).ensure_fresh()
    if access_token != doc.access_token:
        pytest.fail("a fresh stored token was unexpectedly replaced")
    if context.store_path.read_bytes() != before:
        pytest.fail("a valid stored-token check changed the OAuth store")
    result = _codex_probe(context, access_token, account_id)
    _assert_provider_result(result, context.provider)


@pytest.mark.acceptance
def test_codex_expired_access_with_valid_refresh() -> None:
    _allow_oauth_mutation()
    context = _codex_context(CODEX_EXPIRED_STORE_ENV)
    old_doc = _expired_doc(context)
    access_token, account_id = _manager(context).ensure_fresh()
    new_doc = context.store.validate(context.provider.name)
    if access_token != new_doc.access_token:
        pytest.fail("refresh returned an access token different from the stored token")
    if new_doc.access_token == old_doc.access_token:
        pytest.fail("refresh did not replace the expired access token")
    assert new_doc.expires_at - time.time() > DEFAULT_REFRESH_MARGIN_S
    result = _codex_probe(context, access_token, account_id)
    _assert_provider_result(result, context.provider)


@pytest.mark.acceptance
def test_codex_rotated_refresh() -> None:
    _allow_oauth_mutation()
    context = _codex_context(CODEX_ROTATED_STORE_ENV)
    old_doc = _expired_doc(context)
    _manager(context).ensure_fresh()
    new_doc = context.store.validate(context.provider.name)
    if new_doc.refresh_token == old_doc.refresh_token:
        pytest.fail("issuer did not rotate the refresh token")
    if new_doc.access_token == old_doc.access_token:
        pytest.fail("issuer did not replace the access token")
    assert new_doc.expires_at - time.time() > DEFAULT_REFRESH_MARGIN_S


@pytest.mark.acceptance
def test_codex_revoked_refresh() -> None:
    _allow_oauth_mutation()
    context = _codex_context(CODEX_REVOKED_STORE_ENV)
    _expired_doc(context)
    with pytest.raises(InvalidGrantError):
        _manager(context).ensure_fresh()
    record = context.store.read_provider(context.provider.name)
    assert record is not None and record.disabled


@pytest.mark.acceptance
def test_codex_concurrent_child_startup() -> None:
    _allow_oauth_mutation()
    context = _codex_context(CODEX_CONCURRENT_STORE_ENV)
    _expired_doc(context)
    outcomes = _run_ensure_processes(context, count=2, concurrent=True)
    assert len(outcomes) == 2
    assert all(status == "ok" for status, _detail in outcomes)
    doc = context.store.validate(context.provider.name)
    assert doc.expires_at - time.time() > DEFAULT_REFRESH_MARGIN_S


@pytest.mark.acceptance
def test_codex_account_id_propagation() -> None:
    context = _codex_context()
    doc = _fresh_doc(context)
    assert doc.account_id
    spec = {
        "task_id": "acceptance-account-id",
        "provider_config_path": str(context.config_path),
        "worktree_path": str(Path.cwd()),
        "fanout_config": {
            "providers": [{"name": context.provider.name}],
            "tier": context.provider.tier.value,
            "model": context.provider.model,
        },
        "authorized_providers": [context.provider.name],
        "authorized_providers_explicit": True,
        "provider_env_keys": [],
    }
    environment = supervisor._worker_environment(
        spec,
        generation=1,
        provider_environment={},
        oauth_store=context.store,
    )
    suffix = oauth_env_suffix(context.provider.name)
    access_name = f"CAMBIUM_OAUTH_ACCESS_{suffix}"
    account_name = f"CAMBIUM_OAUTH_ACCOUNT_{suffix}"
    if environment.get(account_name) != doc.account_id:
        pytest.fail("stored account id was not propagated to the worker environment")
    if environment.get(access_name) != doc.access_token:
        pytest.fail("stored access token was not propagated to the worker environment")
    if not context.store.path.read_bytes():
        pytest.fail("OAuth store unexpectedly became empty")
    refresh_name = f"CAMBIUM_OAUTH_REFRESH_{suffix}"
    assert refresh_name not in environment
    result = _codex_probe(context, environment[access_name], environment[account_name])
    _assert_provider_result(result, context.provider)


@pytest.mark.acceptance
def test_codex_restart_and_reuse() -> None:
    context = _codex_context(CODEX_RESTART_STORE_ENV)
    _fresh_doc(context)
    before = context.store.path.read_bytes()
    first = _run_ensure_processes(context, count=1, concurrent=False)
    second = _run_ensure_processes(context, count=1, concurrent=False)
    assert len(first) == 1 and first[0][0] == "ok"
    assert len(second) == 1 and second[0][0] == "ok"
    if context.store.path.read_bytes() != before:
        pytest.fail("a restarted child changed a valid reusable OAuth store")


@pytest.mark.acceptance
def test_zai_rolling_windows(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    context = _api_provider_context(
        ZAI_CONFIG_ENV,
        ZAI_PROVIDER_ENV,
        generated_config_dir=tmp_path,
    )
    if "zai" not in context.provider.name.casefold():
        pytest.fail("z.ai acceptance provider name must contain 'zai'")
    if not context.provider.quota_windows:
        pytest.fail("z.ai acceptance config must declare quota_windows")
    result = _api_probe(context, monkeypatch)
    _assert_provider_result(result, context.provider)
    snapshots = result.quota_windows
    assert isinstance(snapshots, tuple) and snapshots
    assert all(
        isinstance(window, dict)
        and isinstance(window.get("reset_at"), int | float)
        and window["reset_at"] > time.time()
        for window in snapshots
    )


@pytest.mark.acceptance
def test_openrouter_paid(monkeypatch: pytest.MonkeyPatch) -> None:
    context = _api_provider_context(OPENROUTER_CONFIG_ENV, OPENROUTER_PAID_PROVIDER_ENV)
    if "openrouter" not in context.provider.name.casefold():
        pytest.fail("OpenRouter paid provider name must contain 'openrouter'")
    billing = getattr(context.provider.billing_mode, "value", context.provider.billing_mode)
    assert billing in {"metered", "subscription"}
    result = _api_probe(
        context,
        monkeypatch,
        requirements={"allow_paid": True, "allow_free": False},
    )
    _assert_provider_result(result, context.provider)


@pytest.mark.acceptance
def test_openrouter_free(monkeypatch: pytest.MonkeyPatch) -> None:
    context = _api_provider_context(OPENROUTER_CONFIG_ENV, OPENROUTER_FREE_PROVIDER_ENV)
    if "openrouter" not in context.provider.name.casefold():
        pytest.fail("OpenRouter free provider name must contain 'openrouter'")
    billing = getattr(context.provider.billing_mode, "value", context.provider.billing_mode)
    assert billing == "free"
    result = _api_probe(
        context,
        monkeypatch,
        requirements={"allow_paid": False, "allow_free": True},
    )
    _assert_provider_result(result, context.provider)


@pytest.mark.acceptance
@pytest.mark.skipif(
    os.environ.get("CAMBIUM_LIVE") != "1",
    reason="OpenCode Zen acceptance requires CAMBIUM_LIVE=1",
)
def test_opencode_zen(monkeypatch: pytest.MonkeyPatch) -> None:
    context = _api_provider_context(OPENCODE_ZEN_CONFIG_ENV, OPENCODE_ZEN_PROVIDER_ENV)
    if "opencode" not in context.provider.name.casefold():
        pytest.fail("OpenCode Zen provider name must contain 'opencode'")
    result = _api_probe(context, monkeypatch)
    _assert_provider_result(result, context.provider)


@pytest.mark.acceptance
def test_provider_reported_cache_tokens(monkeypatch: pytest.MonkeyPatch) -> None:
    context = _api_provider_context(CACHE_CONFIG_ENV, CACHE_PROVIDER_ENV)
    if "price_per_1m_cached_in" not in context.config_entry:
        pytest.skip("cache-token acceptance requires price_per_1m_cached_in in provider config")
    if context.config_entry.get("pricing_known") is not True:
        pytest.skip("cache-token acceptance requires pricing_known=true in provider config")
    router = _api_router(context, monkeypatch)

    async def call_twice() -> tuple[Any, Any]:
        first = await router.call(
            context.provider.tier,
            _live_prompt(),
            model=context.provider.model,
        )
        second = await router.call(
            context.provider.tier,
            _live_prompt(),
            model=context.provider.model,
        )
        return first, second

    first, second = asyncio.run(call_twice())
    _assert_provider_result(first, context.provider)
    _assert_provider_result(second, context.provider)
    assert _cached_tokens(second.usage) is not None
    assert isinstance(second.provider_cache_hit, bool)
