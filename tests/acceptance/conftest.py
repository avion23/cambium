"""Disposable Codex OAuth stores for the live acceptance checks.

The pi auth file is a read-only source.  The fixture translates only its
Codex entry into Cambium's OAuth schema and writes each test's copy below
pytest's disposable ``tmp_path``.  It is intentionally inactive unless the
operator explicitly opts into OAuth activity.
"""

from __future__ import annotations

import json
import math
import os
from collections.abc import Mapping
from dataclasses import dataclass, replace
from pathlib import Path
from typing import Literal

import pytest

from cambium.oauth import OAuthDoc, OAuthError, OAuthStore

ALLOW_MUTATION_ENV = "CAMBIUM_ACCEPTANCE_ALLOW_MUTATION"
LEGACY_ALLOW_MUTATION_ENV = "CAMBIUM_ACCEPTANCE_ALLOW_OAUTH_MUTATIONS"
CODEX_SOURCE_ENV = "CAMBIUM_ACCEPTANCE_CODEX_PI_AUTH"
PI_AUTH_ENV = "CAMBIUM_ACCEPTANCE_PI_AUTH"
CODEX_PROVIDER_ENV = "CAMBIUM_ACCEPTANCE_CODEX_PROVIDER"
CODEX_CONFIG_ENV = "CAMBIUM_ACCEPTANCE_CODEX_CONFIG"
CODEX_STORE_ENV = "CAMBIUM_ACCEPTANCE_CODEX_OAUTH_STORE"
CODEX_FRESH_STORE_ENV = "CAMBIUM_ACCEPTANCE_CODEX_FRESH_STORE"
CODEX_EXPIRED_STORE_ENV = "CAMBIUM_ACCEPTANCE_CODEX_EXPIRED_STORE"
CODEX_ROTATED_STORE_ENV = "CAMBIUM_ACCEPTANCE_CODEX_ROTATED_STORE"
CODEX_REVOKED_STORE_ENV = "CAMBIUM_ACCEPTANCE_CODEX_REVOKED_STORE"
CODEX_CONCURRENT_STORE_ENV = "CAMBIUM_ACCEPTANCE_CODEX_CONCURRENT_STORE"
CODEX_RESTART_STORE_ENV = "CAMBIUM_ACCEPTANCE_CODEX_RESTART_STORE"
CODEX_FIXTURE_ROOT_ENV = "CAMBIUM_ACCEPTANCE_CODEX_FIXTURE_ROOT"

FixtureState = Literal["valid", "expired", "rotated", "revoked", "concurrent", "restart"]

_EXPIRED_STATES = frozenset({"expired", "rotated", "revoked", "concurrent"})
_TEST_STORES: dict[str, tuple[str, FixtureState]] = {
    "test_codex_valid_stored_token": (CODEX_STORE_ENV, "valid"),
    "test_codex_expired_access_with_valid_refresh": (CODEX_EXPIRED_STORE_ENV, "expired"),
    "test_codex_rotated_refresh": (CODEX_ROTATED_STORE_ENV, "rotated"),
    "test_codex_revoked_refresh": (CODEX_REVOKED_STORE_ENV, "revoked"),
    "test_codex_concurrent_child_startup": (CODEX_CONCURRENT_STORE_ENV, "concurrent"),
    "test_codex_account_id_propagation": (CODEX_STORE_ENV, "valid"),
    "test_codex_restart_and_reuse": (CODEX_RESTART_STORE_ENV, "restart"),
}


class CodexOAuthFixtureError(RuntimeError):
    """The operator's source cannot seed a disposable Codex store."""


@dataclass(frozen=True, slots=True)
class CodexOAuthFixture:
    """One disposable OAuth file and the source from which it was copied."""

    path: Path
    source: Path
    state: FixtureState


def _mutation_enabled() -> bool:
    return os.environ.get(ALLOW_MUTATION_ENV) == "1" or os.environ.get(
        LEGACY_ALLOW_MUTATION_ENV
    ) == "1"


def _read_json(path: Path) -> object:
    try:
        raw_text = path.read_text(encoding="utf-8")
    except OSError as exc:
        raise CodexOAuthFixtureError(f"cannot read source {path}: {type(exc).__name__}") from exc
    try:
        return json.loads(raw_text)
    except json.JSONDecodeError as exc:
        raise CodexOAuthFixtureError(f"source {path} is not valid JSON") from exc


def _pi_oauth_doc(raw: Mapping[str, object], provider: str) -> OAuthDoc:
    entry: Mapping[str, object] | None = None
    for name in ("openai-codex", "codex"):
        candidate = raw.get(name)
        if isinstance(candidate, Mapping):
            entry = candidate
            break
    if entry is None:
        raise CodexOAuthFixtureError("pi auth source has no Codex OAuth entry")

    access_token = entry.get("access")
    refresh_token = entry.get("refresh")
    expires = entry.get("expires")
    account_id = entry.get("accountId")
    if not isinstance(access_token, str) or not access_token:
        raise CodexOAuthFixtureError("pi Codex entry has no access token")
    if not isinstance(refresh_token, str) or not refresh_token:
        raise CodexOAuthFixtureError("pi Codex entry has no refresh token")
    if (
        isinstance(expires, bool)
        or not isinstance(expires, int | float)
        or not math.isfinite(expires)
    ):
        raise CodexOAuthFixtureError("pi Codex entry has no valid expiry")
    if account_id is not None and (not isinstance(account_id, str) or not account_id):
        raise CodexOAuthFixtureError("pi Codex entry has an invalid account id")

    # pi stores OAuth expiry in milliseconds.  Accept seconds as well so an
    # operator can use a compatible exported store without editing a token.
    expires_at = (
        float(expires) / 1000.0
        if abs(float(expires)) > 100_000_000_000
        else float(expires)
    )
    return OAuthDoc(provider, access_token, refresh_token, expires_at, account_id)


def _cambium_oauth_doc(path: Path, provider: str) -> OAuthDoc | None:
    """Read an explicitly supplied Cambium store without modifying it."""

    try:
        document = OAuthStore(path).read()
    except OAuthError as exc:
        raise CodexOAuthFixtureError(
            f"source {path} is not a readable Cambium OAuth store: {type(exc).__name__}"
        ) from exc
    record = document.by_provider(provider)
    if record is None and len(document.records) == 1:
        record = document.records[0]
    if record is None:
        raise CodexOAuthFixtureError(f"source {path} has no OAuth record for {provider}")
    return replace(record.doc, provider=provider)


def _source_doc(path: Path, provider: str) -> OAuthDoc:
    raw = _read_json(path)
    if not isinstance(raw, Mapping):
        raise CodexOAuthFixtureError(f"source {path} must contain a JSON object")
    if isinstance(raw.get("providers"), Mapping) and "version" in raw:
        document = _cambium_oauth_doc(path, provider)
        if document is None:  # pragma: no cover - _cambium_oauth_doc always returns or raises
            raise CodexOAuthFixtureError(f"source {path} has no OAuth record")
        return document
    try:
        return _pi_oauth_doc(raw, provider)
    except (TypeError, ValueError) as exc:
        raise CodexOAuthFixtureError(f"source {path} has an invalid Codex OAuth entry") from exc


def _write_copy(target: Path, doc: OAuthDoc, source: Path) -> Path:
    target = target.expanduser()
    if target.resolve() == source.expanduser().resolve():
        raise CodexOAuthFixtureError("disposable OAuth target must differ from its source")
    if target.exists():
        raise CodexOAuthFixtureError(f"disposable OAuth target already exists: {target}")
    target.parent.mkdir(parents=True, exist_ok=False, mode=0o700)
    try:
        OAuthStore(target).save_provider(doc)
    except OAuthError as exc:
        raise CodexOAuthFixtureError(
            f"could not write disposable OAuth copy {target}: {type(exc).__name__}"
        ) from exc
    target.parent.chmod(0o700)
    target.chmod(0o600)
    if target.is_symlink() or target.stat().st_nlink != 1:
        raise CodexOAuthFixtureError("disposable OAuth copy is not a private regular file")
    return target


def build_codex_oauth_fixture(
    root: Path,
    source: Path,
    provider: str,
    *,
    state: FixtureState = "valid",
    source_is_prepared: bool = False,
) -> CodexOAuthFixture:
    """Copy one pi/Cambium OAuth record into a fresh disposable directory.

    ``source_is_prepared`` is used only for an explicitly supplied state
    store.  The default pi source is copied unchanged for valid/restart checks
    and receives an expired timestamp in the local copy for refresh checks;
    no source bytes are ever written or linked.
    """

    if state not in {"valid", "expired", "rotated", "revoked", "concurrent", "restart"}:
        raise CodexOAuthFixtureError(f"unsupported Codex fixture state: {state}")
    root = root.expanduser()
    root.mkdir(parents=True, exist_ok=True, mode=0o700)
    root.chmod(0o700)
    source = source.expanduser()
    doc = _source_doc(source, provider)
    if state in _EXPIRED_STATES and not source_is_prepared:
        doc = replace(doc, expires_at=0.0)
    path = root / state / "oauth.json"
    return CodexOAuthFixture(path=_write_copy(path, doc, source), source=source, state=state)


def build_codex_oauth_fixtures(
    root: Path,
    source: Path,
    provider: str,
    *,
    source_by_state: Mapping[FixtureState, Path] | None = None,
) -> dict[str, CodexOAuthFixture]:
    """Build all six reusable-store states as independent directory copies."""

    overrides = {} if source_by_state is None else dict(source_by_state)
    fixtures: dict[str, CodexOAuthFixture] = {}
    for state in ("valid", "expired", "rotated", "revoked", "concurrent", "restart"):
        selected_source = overrides.get(state, source)
        fixture = build_codex_oauth_fixture(
            root,
            selected_source,
            provider,
            state=state,
            source_is_prepared=state in overrides,
        )
        fixtures[state] = fixture
    return fixtures


def _source_path() -> Path:
    configured = os.environ.get(CODEX_SOURCE_ENV, "").strip() or os.environ.get(
        PI_AUTH_ENV, ""
    ).strip()
    return (
        Path(configured).expanduser()
        if configured
        else Path.home() / ".pi" / "agent" / "auth.json"
    )


@pytest.fixture(autouse=True)
def disposable_codex_oauth_store(
    request: pytest.FixtureRequest,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Give each Codex acceptance check a private copied store when opted in."""

    if not _mutation_enabled():
        return
    test_name = request.node.name
    root = tmp_path / "codex-oauth"
    if test_name == "test_codex_fresh_login":
        # The fresh-login check intentionally does not seed this path.  Its
        # operator command must obtain a new session with interactive consent.
        monkeypatch.setenv(CODEX_FRESH_STORE_ENV, str(root / "fresh" / "oauth.json"))
        monkeypatch.setenv(CODEX_FIXTURE_ROOT_ENV, str(root))
        return
    configured = _TEST_STORES.get(test_name)
    if configured is None:
        return
    store_env, state = configured
    configured_source = os.environ.get(store_env, "").strip()
    source = _source_path()
    source_overrides = {state: Path(configured_source).expanduser()} if configured_source else {}
    try:
        fixture = build_codex_oauth_fixture(
            root,
            source_overrides.get(state, source),
            os.environ.get(CODEX_PROVIDER_ENV, "").strip() or "codex",
            state=state,
            source_is_prepared=bool(configured_source),
        )
    except CodexOAuthFixtureError as exc:
        pytest.skip(f"disposable Codex OAuth fixture unavailable: {exc}")
    monkeypatch.setenv(store_env, str(fixture.path))
    monkeypatch.setenv(CODEX_FIXTURE_ROOT_ENV, str(root))
