from __future__ import annotations

import threading
from pathlib import Path
from typing import cast

import pytest

from cambium.oauth import (
    DEFAULT_REFRESH_MARGIN_S,
    InvalidGrantError,
    OAuthDoc,
    OAuthMissingError,
    OAuthRecord,
    OAuthStore,
    RefreshedTokens,
    TokenManager,
)


class _Clock:
    def __init__(self, value: float) -> None:
        self.value = value

    def __call__(self) -> float:
        return self.value


def _path(root: Path) -> Path:
    return root / ".local" / "share" / "cambium" / "oauth.json"


def _doc(expires_at: float) -> OAuthDoc:
    return OAuthDoc("codex", "stored-access", "stored-refresh", expires_at, None)


@pytest.mark.parametrize(
    ("now", "expires_at", "expected_access", "expected_refreshes"),
    [
        (1000.0, 1000.0, "refreshed-access", 1),
        (
            1000.0 + DEFAULT_REFRESH_MARGIN_S - 10.0,
            1000.0 + DEFAULT_REFRESH_MARGIN_S,
            "refreshed-access",
            1,
        ),
        (
            1000.0 + 1.0,
            1000.0 + DEFAULT_REFRESH_MARGIN_S,
            "refreshed-access",
            1,
        ),
        (
            1000.0 - 1.0,
            1000.0 + DEFAULT_REFRESH_MARGIN_S,
            "stored-access",
            0,
        ),
        (
            1000.0 + DEFAULT_REFRESH_MARGIN_S,
            1000.0 + DEFAULT_REFRESH_MARGIN_S,
            "refreshed-access",
            1,
        ),
    ],
)
def test_token_manager_refresh_margin_boundaries(
    tmp_path: Path,
    now: float,
    expires_at: float,
    expected_access: str,
    expected_refreshes: int,
) -> None:
    clock = _Clock(now)
    store = OAuthStore(_path(tmp_path))
    store.save_provider(_doc(expires_at))
    refreshes: list[str] = []

    def refresh(refresh_token: str) -> RefreshedTokens:
        refreshes.append(refresh_token)
        return RefreshedTokens("refreshed-access", 100.0, "refreshed-refresh")

    manager = TokenManager(
        "codex",
        store,
        client_id="client",
        issuer="http://127.0.0.1:1",
        refresh=refresh,
        clock=clock,
    )

    access, account_id = manager.ensure_fresh()

    assert access == expected_access
    assert account_id is None
    assert len(refreshes) == expected_refreshes


def test_token_manager_uses_injected_clock_after_backward_step(tmp_path: Path) -> None:
    clock = _Clock(1000.0)
    store = OAuthStore(_path(tmp_path))
    store.save_provider(_doc(1000.0))
    refreshes: list[str] = []

    def refresh(refresh_token: str) -> RefreshedTokens:
        refreshes.append(refresh_token)
        return RefreshedTokens("refreshed-access", 3600.0, "refreshed-refresh")

    manager = TokenManager(
        "codex",
        store,
        client_id="client",
        issuer="http://127.0.0.1:1",
        refresh=refresh,
        clock=clock,
    )

    assert manager.ensure_fresh() == ("refreshed-access", None)
    record = cast(OAuthRecord, store.read_provider("codex"))
    assert record.doc.expires_at == 4600.0

    clock.value = 900.0

    assert manager.ensure_fresh() == ("refreshed-access", None)
    assert refreshes == ["stored-refresh"]


def test_refreshed_tokens_repr_hides_all_token_values() -> None:
    refreshed = RefreshedTokens(
        "fresh-access-secret", 3600.0, "fresh-refresh-secret", "account-secret"
    )

    output = repr(refreshed)

    assert "fresh-access-secret" not in output
    assert "fresh-refresh-secret" not in output
    assert "account-secret" not in output
    assert output == "RefreshedTokens(expires_in=3600.0, refresh_token=True, account_id=True)"


def test_refresh_callback_failure_preserves_store_bytes(tmp_path: Path) -> None:
    store = OAuthStore(_path(tmp_path))
    store.save_provider(_doc(0.0))
    before = store.path.read_bytes()

    def refresh(_refresh_token: str) -> RefreshedTokens:
        raise RuntimeError("refresh failed")

    manager = TokenManager(
        "codex",
        store,
        client_id="client",
        issuer="http://127.0.0.1:1",
        refresh=refresh,
        clock=lambda: 1000.0,
    )

    with pytest.raises(RuntimeError, match="refresh failed"):
        manager.ensure_fresh()

    assert store.path.read_bytes() == before
    assert not list(store.path.parent.glob(".oauth.json.tmp-*"))


def test_refresh_does_not_overwrite_a_concurrent_login(tmp_path: Path) -> None:
    store_path = _path(tmp_path)
    store = OAuthStore(store_path)
    store.save_provider(_doc(0.0))
    refresh_started = threading.Event()
    release_refresh = threading.Event()
    result: list[tuple[str, str | None]] = []
    errors: list[BaseException] = []

    def refresh(_refresh_token: str) -> RefreshedTokens:
        refresh_started.set()
        assert release_refresh.wait(5.0)
        return RefreshedTokens("stale-refresh-access", 3600.0, "stale-refresh-token")

    manager = TokenManager(
        "codex",
        store,
        client_id="client",
        issuer="http://127.0.0.1:1",
        refresh=refresh,
        clock=lambda: 1000.0,
    )

    def run() -> None:
        try:
            result.append(manager.ensure_fresh())
        except BaseException as exc:  # pragma: no cover - failure diagnostics
            errors.append(exc)

    thread = threading.Thread(target=run)
    thread.start()
    assert refresh_started.wait(5.0)
    login = OAuthDoc("codex", "login-access", "login-refresh", 5000.0, None)
    store.save_provider(login)
    release_refresh.set()
    thread.join(5.0)

    assert not thread.is_alive()
    assert errors == []
    assert result == [("login-access", None)]
    record = store.read_provider("codex")
    assert record is not None
    assert record.doc == login


def test_refresh_does_not_restore_a_logged_out_provider(tmp_path: Path) -> None:
    store_path = _path(tmp_path)
    store = OAuthStore(store_path)
    store.save_provider(_doc(0.0))
    refresh_started = threading.Event()
    release_refresh = threading.Event()
    errors: list[BaseException] = []

    def refresh(_refresh_token: str) -> RefreshedTokens:
        refresh_started.set()
        assert release_refresh.wait(5.0)
        return RefreshedTokens("stale-refresh-access", 3600.0, "stale-refresh-token")

    manager = TokenManager(
        "codex",
        store,
        client_id="client",
        issuer="http://127.0.0.1:1",
        refresh=refresh,
        clock=lambda: 1000.0,
    )

    def run() -> None:
        try:
            manager.ensure_fresh()
        except BaseException as exc:
            errors.append(exc)

    thread = threading.Thread(target=run)
    thread.start()
    assert refresh_started.wait(5.0)
    assert store.remove_provider("codex") is True
    release_refresh.set()
    thread.join(5.0)

    assert not thread.is_alive()
    assert len(errors) == 1
    assert isinstance(errors[0], OAuthMissingError)
    assert store.read_provider("codex") is None


def test_invalid_grant_does_not_disable_a_concurrent_login(tmp_path: Path) -> None:
    store_path = _path(tmp_path)
    store = OAuthStore(store_path)
    store.save_provider(_doc(0.0))
    refresh_started = threading.Event()
    release_refresh = threading.Event()
    result: list[tuple[str, str | None]] = []
    errors: list[BaseException] = []

    def refresh(_refresh_token: str) -> RefreshedTokens:
        refresh_started.set()
        assert release_refresh.wait(5.0)
        raise InvalidGrantError("old grant")

    manager = TokenManager(
        "codex",
        store,
        client_id="client",
        issuer="http://127.0.0.1:1",
        refresh=refresh,
        clock=lambda: 1000.0,
    )

    def run() -> None:
        try:
            result.append(manager.ensure_fresh())
        except BaseException as exc:  # pragma: no cover - failure diagnostics
            errors.append(exc)

    thread = threading.Thread(target=run)
    thread.start()
    assert refresh_started.wait(5.0)
    login = OAuthDoc("codex", "login-access", "login-refresh", 5000.0, None)
    store.save_provider(login)
    release_refresh.set()
    thread.join(5.0)

    assert not thread.is_alive()
    assert errors == []
    assert result == [("login-access", None)]
    record = store.read_provider("codex")
    assert record is not None and not record.disabled
    assert record.doc == login


def test_two_managers_share_one_refresh_transaction(tmp_path: Path) -> None:
    store_path = _path(tmp_path)
    OAuthStore(store_path).save_provider(_doc(0.0))
    first_refresh_started = threading.Event()
    second_clock_called = threading.Event()
    release_first = threading.Event()
    refreshes: list[str] = []
    results: list[tuple[str, str | None]] = []
    errors: list[BaseException] = []

    def first_refresh(refresh_token: str) -> RefreshedTokens:
        refreshes.append(refresh_token)
        first_refresh_started.set()
        if not release_first.wait(5.0):
            raise RuntimeError("first refresh was not released")
        return RefreshedTokens("first-access", 3600.0, "first-refresh")

    def second_refresh(refresh_token: str) -> RefreshedTokens:
        refreshes.append(refresh_token)
        return RefreshedTokens("second-access", 3600.0, "second-refresh")

    def second_clock() -> float:
        second_clock_called.set()
        return 1000.0

    first = TokenManager(
        "codex",
        OAuthStore(store_path),
        client_id="client",
        issuer="http://127.0.0.1:1",
        refresh=first_refresh,
        clock=lambda: 1000.0,
    )
    second = TokenManager(
        "codex",
        OAuthStore(store_path),
        client_id="client",
        issuer="http://127.0.0.1:1",
        refresh=second_refresh,
        clock=second_clock,
    )

    def run(manager: TokenManager) -> None:
        try:
            results.append(manager.ensure_fresh())
        except BaseException as exc:
            errors.append(exc)

    first_thread = threading.Thread(target=run, args=(first,))
    second_thread = threading.Thread(target=run, args=(second,))
    first_thread.start()
    assert first_refresh_started.wait(5.0)
    second_thread.start()
    assert second_clock_called.wait(5.0)
    release_first.set()
    first_thread.join(5.0)
    second_thread.join(5.0)

    assert not first_thread.is_alive()
    assert not second_thread.is_alive()
    assert errors == []
    assert sorted(results) == [("first-access", None), ("first-access", None)]
    assert refreshes == ["stored-refresh"]
    record = OAuthStore(store_path).read_provider("codex")
    assert record is not None
    assert record.doc.access_token == "first-access"
    assert record.doc.refresh_token == "first-refresh"
