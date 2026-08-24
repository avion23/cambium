"""Codex ChatGPT-subscription OAuth scenarios.

Covers the hardened store (0600/0700, fail-closed corruption, repair), the
codex CLI session import, the flock'd refresh transaction (including a real
two-process rotating-refresh race), refresh failure/invalid-grant policy, and
the device flow against a loopback fake issuer. No real network and no
monkeypatching: the fake issuer is a local HTTP server and concurrency uses
real processes and flock.
"""

from __future__ import annotations

import base64
import json
import multiprocessing
import os
import stat
import threading
import time
from contextlib import contextmanager
from dataclasses import replace
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any, cast

import pytest

from cambium import oauth
from cambium.oauth import (
    DeviceFlow,
    DeviceFlowCanceled,
    DeviceFlowError,
    DeviceFlowExpired,
    InvalidGrantError,
    OAuthDoc,
    OAuthError,
    OAuthMissingError,
    OAuthRecord,
    OAuthStore,
    OAuthStoreError,
    RefreshUnavailableError,
    TokenManager,
    import_codex_cli_session,
)

STALE_ACCESS = "stale-access"
STALE_REFRESH = "stale-refresh"
FAKE_CLIENT_ID = "cli-client"
USER_CODE = "USER-CODE-1"
DEVICE_AUTH_ID = "device-auth-1"
AUTH_CODE = "auth-code-1"
CODE_VERIFIER = "verifier-1"
ACCOUNT_ID = "acct-refreshed"


def _jwt(payload: dict[str, Any]) -> str:
    def _enc(obj: Any) -> str:
        encoded = base64.urlsafe_b64encode(json.dumps(obj).encode("utf-8"))
        return encoded.rstrip(b"=").decode("ascii")

    return f"{_enc({'alg': 'none'})}.{_enc(payload)}.signature"


ID_TOKEN = _jwt({"chatgpt_account_id": ACCOUNT_ID, "exp": 1900000000})


def _store_path(root: Path) -> Path:
    return root / ".local" / "share" / "cambium" / "oauth.json"


def _doc(
    provider: str = "codex",
    access: str = "access-1",
    refresh: str = "refresh-1",
    expires_at: float | None = None,
    account_id: str | None = ACCOUNT_ID,
) -> OAuthDoc:
    return OAuthDoc(
        provider,
        access,
        refresh,
        time.time() + 3600 if expires_at is None else expires_at,
        account_id,
    )


def _stale_doc() -> OAuthDoc:
    return _doc(access=STALE_ACCESS, refresh=STALE_REFRESH, expires_at=time.time() - 3600)


# --------------------------------------------------------------------------- #
# Fake issuer
# --------------------------------------------------------------------------- #


class _FakeState:
    def __init__(self) -> None:
        self.reset()

    def reset(self) -> None:
        self.usercode_interval: str = "1"
        self.poll_mode: str = "approve"  # approve | pending_forever
        self.poll_pending: int = 2
        self.exchange_status: int = 200
        self.refresh_status: int = 200
        self.refresh_sleep_s: float = 0.0
        self.device_token_sleep_s: float = 0.0
        self.refresh_include_refresh: bool = True
        self.usercode_count: int = 0
        self.poll_count: int = 0
        self.exchange_count: int = 0
        self.refresh_count: int = 0
        self.refresh_bodies: list[str] = []


_FAKE = _FakeState()
_FAKE_LOCK = threading.Lock()


class _FakeIssuerHandler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.0"

    def do_POST(self) -> None:  # noqa: N802 (http.server API)
        length = int(self.headers.get("Content-Length") or 0)
        raw = self.rfile.read(length) if length else b""
        if self.path == "/api/accounts/deviceauth/usercode":
            with _FAKE_LOCK:
                _FAKE.usercode_count += 1
                body: dict[str, Any] = {
                    "device_auth_id": DEVICE_AUTH_ID,
                    "user_code": USER_CODE,
                    "interval": _FAKE.usercode_interval,
                }
            status = 200
        elif self.path == "/api/accounts/deviceauth/token":
            with _FAKE_LOCK:
                _FAKE.poll_count += 1
                pending = (
                    _FAKE.poll_mode == "pending_forever" or _FAKE.poll_count <= _FAKE.poll_pending
                )
                sleep_s = _FAKE.device_token_sleep_s
            if sleep_s > 0:
                time.sleep(sleep_s)
            if pending:
                status = 403
                body = {"error": "authorization_pending"}
            else:
                status = 200
                body = {
                    "authorization_code": AUTH_CODE,
                    "code_verifier": CODE_VERIFIER,
                    "code_challenge": "challenge-1",
                }
        elif self.path == "/oauth/token":
            text = raw.decode("utf-8", "replace")
            if "grant_type=refresh_token" in text:
                with _FAKE_LOCK:
                    _FAKE.refresh_count += 1
                    _FAKE.refresh_bodies.append(text)
                    status = _FAKE.refresh_status
                    sleep_s = _FAKE.refresh_sleep_s
                    include_refresh = _FAKE.refresh_include_refresh
                    n = _FAKE.refresh_count
                if sleep_s > 0:
                    time.sleep(sleep_s)
                if status == 200:
                    body = {
                        "access_token": f"new-access-{n}",
                        "refresh_token": f"new-refresh-{n}" if include_refresh else "omitted",
                        "expires_in": 3600,
                        "id_token": ID_TOKEN,
                    }
                    if not include_refresh:
                        del body["refresh_token"]
                elif status == 400:
                    body = {"error": "invalid_grant"}
                else:
                    body = {"error": "server_error"}
            else:
                with _FAKE_LOCK:
                    _FAKE.exchange_count += 1
                    status = _FAKE.exchange_status
                if status == 200:
                    body = {
                        "access_token": "flow-access",
                        "refresh_token": "flow-refresh",
                        "expires_in": 3600,
                        "id_token": ID_TOKEN,
                    }
                else:
                    body = {"error": "server_error"}
        else:
            self.send_error(404)
            return
        encoded = json.dumps(body).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(encoded)))
        self.send_header("Connection", "close")
        self.end_headers()
        self.wfile.write(encoded)

    def log_message(self, format: str = "", *args: object) -> None:
        pass


class _FakeIssuer:
    """Loopback-only fake of the codex issuer's device-auth and token endpoints."""

    def __init__(self) -> None:
        _FAKE.reset()
        self.fake = _FAKE
        self._httpd = ThreadingHTTPServer(("127.0.0.1", 0), _FakeIssuerHandler)
        self._thread = threading.Thread(
            target=self._httpd.serve_forever,
            kwargs={"poll_interval": 0.005},
            daemon=True,
        )
        self._thread.start()

    @property
    def issuer(self) -> str:
        return f"http://127.0.0.1:{self._httpd.server_port}"

    def close(self) -> None:
        self._httpd.shutdown()
        self._httpd.server_close()


@contextmanager
def _fake_issuer():
    server = _FakeIssuer()
    try:
        yield server
    finally:
            server.close()


def _use_fast_device_polling(flow: DeviceFlow, monkeypatch: pytest.MonkeyPatch) -> None:
    """Keep real device-flow polling while shortening the fake issuer interval."""
    request_user_code = flow.request_user_code

    def request() -> Any:
        return replace(request_user_code(), interval=0.005)

    monkeypatch.setattr(flow, "request_user_code", request)


# --------------------------------------------------------------------------- #
# Store hardening
# --------------------------------------------------------------------------- #


def test_store_round_trip_permissions_and_remove(tmp_path: Path) -> None:
    path = _store_path(tmp_path)
    store = OAuthStore(path)
    assert store.read().records == ()
    assert store.providers() == ()

    store.save_provider(_doc())
    directory_stat = path.parent.stat()
    file_stat = path.stat()
    assert stat.S_IMODE(directory_stat.st_mode) == 0o700
    assert stat.S_IMODE(file_stat.st_mode) == 0o600
    assert file_stat.st_uid == os.geteuid()
    assert file_stat.st_nlink == 1

    record = store.read_provider("codex")
    assert record is not None and not record.disabled
    assert record.doc.access_token == "access-1"
    assert record.doc.account_id == ACCOUNT_ID

    store.save_provider(_doc(provider="other", access="a2", refresh="r2", account_id=None))
    assert set(store.providers()) == {"codex", "other"}
    other = cast(OAuthRecord, store.read().by_provider("other"))
    assert other.doc.account_id is None

    assert store.remove_provider("codex") is True
    assert store.read_provider("codex") is None
    assert store.remove_provider("codex") is False
    assert not list(path.parent.glob("*.tmp-*"))
    assert not list(path.parent.glob("*.corrupt-*"))


def test_store_disabled_round_trip(tmp_path: Path) -> None:
    path = _store_path(tmp_path)
    store = OAuthStore(path)
    store.save_provider(_doc(), disabled=True)
    record = cast(OAuthRecord, store.read_provider("codex"))
    assert record.disabled is True
    store.save_provider(_doc(), disabled=False)
    record = cast(OAuthRecord, store.read_provider("codex"))
    assert record.disabled is False


def test_store_corrupt_file_fails_closed_and_repair(tmp_path: Path) -> None:
    path = _store_path(tmp_path)
    store = OAuthStore(path)
    store.save_provider(_doc())
    path.write_text('{"version":1,"providers":{"codex":{broken', encoding="utf-8")

    with pytest.raises(OAuthStoreError):
        store.read()
    with pytest.raises(OAuthStoreError):
        store.read_provider("codex")

    store.repair()
    assert store.read().records == ()
    quarantined = list(path.parent.glob("oauth.json.corrupt-*"))
    assert len(quarantined) == 1
    assert "broken" in quarantined[0].read_text(encoding="utf-8")


def test_store_repair_quarantines_insecure_file(tmp_path: Path) -> None:
    path = _store_path(tmp_path)
    store = OAuthStore(path)
    store.save_provider(_doc())
    path.chmod(0o644)
    with pytest.raises(OAuthStoreError):
        store.read()
    store.repair()
    assert store.read().records == ()
    assert len(list(path.parent.glob("oauth.json.corrupt-*"))) == 1


def test_store_repair_is_noop_on_valid_and_missing(tmp_path: Path) -> None:
    path = _store_path(tmp_path)
    store = OAuthStore(path)
    store.repair()  # missing file -> empty store, no-op
    store.save_provider(_doc())
    store.repair()
    record = cast(OAuthRecord, store.read_provider("codex"))
    assert record.doc.access_token == "access-1"
    assert not list(path.parent.glob("oauth.json.corrupt-*"))


@pytest.mark.parametrize(
    "document",
    [
        b'{"version":1,"version":1,"providers":{}}',
        b'{"version":2,"providers":{}}',
        b'{"version":1,"providers":{},"extra":{}}',
        b'{"version":1,"providers":{"codex":{"access_token":"a","refresh_token":"r",'
        b'"expires_at":1,"account_id":null,"disabled":false,"extra":"x"}}}',
        b'{"version":1,"providers":{"codex":{"access_token":"","refresh_token":"r",'
        b'"expires_at":1,"account_id":null,"disabled":false}}}',
        b'{"version":1,"providers":{"Codex":{"access_token":"a","refresh_token":"r",'
        b'"expires_at":1,"account_id":null,"disabled":false}}}',
        b'{"version":1,"providers":{"codex":{"access_token":"a","refresh_token":"r",'
        b'"expires_at":"soon","account_id":null,"disabled":false}}}',
        b'{"version":1,"providers":{"codex":{"access_token":"a","refresh_token":"r",'
        b'"expires_at":1,"account_id":null,"disabled":1}}}',
        b'{"version":1,"providers":{"codex":{"access_token":"a","refresh_token":"r",'
        b'"expires_at":1,"account_id":"","disabled":false}}}',
    ],
)
def test_store_schema_rejects_invalid_documents(document: bytes) -> None:
    with pytest.raises(OAuthStoreError):
        oauth.parse_document(document)


def test_store_parse_rejects_oversized_document() -> None:
    with pytest.raises(OAuthStoreError):
        oauth.parse_document(b"x" * (oauth.MAX_OAUTH_DOC_BYTES + 1))


def test_store_save_rejects_oversized_document_without_replacing_current(
    tmp_path: Path,
) -> None:
    store = OAuthStore(_store_path(tmp_path))
    token = "t" * oauth.MAX_TOKEN_BYTES
    store.save_provider(OAuthDoc("codex", token, token, 1.0, None))
    before = store.path.read_bytes()

    with pytest.raises(OAuthStoreError):
        store.save_provider(OAuthDoc("other", token, token, 1.0, None))

    assert store.path.read_bytes() == before
    assert not list(store.path.parent.glob(".oauth.json.tmp-*"))


def test_store_rejects_insecure_file_mode(tmp_path: Path) -> None:
    path = _store_path(tmp_path)
    store = OAuthStore(path)
    store.save_provider(_doc())
    path.chmod(0o644)
    with pytest.raises(OAuthStoreError):
        store.read()


def test_store_rejects_symlink_hardlink_and_directory_mode(tmp_path: Path) -> None:
    path = _store_path(tmp_path)
    store = OAuthStore(path)
    store.save_provider(_doc())

    hardlink = path.parent / "linked-oauth.json"
    os.link(path, hardlink)
    with pytest.raises(OAuthStoreError):
        store.read()
    hardlink.unlink()

    path.unlink()
    target = tmp_path / "outside.json"
    target.write_text("{}", encoding="utf-8")
    path.symlink_to(target)
    with pytest.raises(OAuthStoreError):
        store.read()
    with pytest.raises(OAuthStoreError):
        store.save_provider(_doc())
    path.unlink()

    path.parent.chmod(0o755)
    with pytest.raises(OAuthStoreError):
        store.read()


def test_store_rejects_symlinked_directory_component(tmp_path: Path) -> None:
    home = tmp_path / "home"
    home.mkdir()
    outside = tmp_path / "outside"
    outside.mkdir()
    (home / ".local").symlink_to(outside, target_is_directory=True)
    path = home / ".local" / "share" / "cambium" / "oauth.json"
    store = OAuthStore(path)

    with pytest.raises(OAuthStoreError, match="symlink"):
        store.save_provider(_doc())

    assert not (outside / "share").exists()
    assert not (home / ".local" / "share").exists()


def test_doc_rejects_oversized_tokens() -> None:
    with pytest.raises(OAuthStoreError):
        OAuthDoc("codex", "a" * (oauth.MAX_TOKEN_BYTES + 1), "r", 1.0, None)
    with pytest.raises(OAuthStoreError):
        OAuthDoc("codex", "a", "r", 1.0, "x" * (oauth.MAX_TOKEN_BYTES + 1))


def test_oauth_doc_representation_hides_tokens() -> None:
    doc = _doc()
    assert STALE_ACCESS not in repr(doc)
    assert "refresh-1" not in repr(doc)
    assert ACCOUNT_ID not in repr(doc)


def test_refreshed_tokens_representation_hides_tokens() -> None:
    refreshed = oauth.RefreshedTokens(
        "fresh-access-secret", 3600.0, "fresh-refresh-secret", "account-secret"
    )
    output = repr(refreshed)

    assert "fresh-access-secret" not in output
    assert "fresh-refresh-secret" not in output
    assert "account-secret" not in output


def test_issuer_validation_rejects_remote_http_and_credentials() -> None:
    with pytest.raises(OAuthError):
        oauth.validate_issuer("http://auth.example.com")
    with pytest.raises(OAuthError):
        oauth.validate_issuer("https://user:pass@auth.openai.com")
    with pytest.raises(OAuthError):
        oauth.validate_issuer("not a url")
    assert oauth.validate_issuer("http://127.0.0.1:9999") == "http://127.0.0.1:9999"
    assert oauth.validate_issuer("https://auth.openai.com") == "https://auth.openai.com"


# --------------------------------------------------------------------------- #
# Codex CLI session import
# --------------------------------------------------------------------------- #


def _codex_session(auth_mode: str = "chatgpt", **overrides: Any) -> dict[str, Any]:
    session: dict[str, Any] = {
        "auth_mode": auth_mode,
        "OPENAI_API_KEY": None,
        "tokens": {
            "id_token": _jwt({"exp": 1900000000, "email": "nobody@example.test"}),
            "access_token": "cli-access",
            "refresh_token": "cli-refresh",
            "account_id": "cli-acct",
        },
        "last_refresh": "2026-08-09T20:53:18.221278873Z",
    }
    for key, value in overrides.items():
        if key == "tokens" and isinstance(value, dict):
            session["tokens"] = {**session["tokens"], **value}
        else:
            session[key] = value
    return session


def test_import_codex_cli_session_parses_real_format(tmp_path: Path) -> None:
    session_path = tmp_path / "auth.json"
    session_path.write_text(json.dumps(_codex_session()), encoding="utf-8")

    doc = import_codex_cli_session(session_path)

    assert doc.provider == "codex"
    assert doc.access_token == "cli-access"
    assert doc.refresh_token == "cli-refresh"
    assert doc.account_id == "cli-acct"
    assert doc.expires_at == 1900000000.0
    # The id_token and the email in it are never stored or echoed.
    assert "cli-access" not in repr(doc)
    assert "cli-refresh" not in repr(doc)
    assert "nobody@example.test" not in repr(doc)
    assert "id_token" not in str(doc)


def test_import_codex_cli_session_edge_cases(tmp_path: Path) -> None:
    with pytest.raises(OAuthError):
        import_codex_cli_session(tmp_path / "missing.json")

    path = tmp_path / "auth.json"
    path.write_text(
        json.dumps(
            _codex_session(tokens={"id_token": _jwt({"exp": 1900000000}), "account_id": None})
        ),
        encoding="utf-8",
    )
    doc = import_codex_cli_session(path)
    assert doc.account_id is None
    assert doc.expires_at == 1900000000.0

    path.write_text(json.dumps(_codex_session(auth_mode="api_key")), encoding="utf-8")
    with pytest.raises(OAuthError, match="not a ChatGPT"):
        import_codex_cli_session(path)

    path.write_text(
        json.dumps(_codex_session(tokens={"access_token": "only", "refresh_token": None})),
        encoding="utf-8",
    )
    with pytest.raises(OAuthError):
        import_codex_cli_session(path)

    path.write_text(json.dumps(_codex_session(tokens={"id_token": "not-a-jwt"})), encoding="utf-8")
    doc = import_codex_cli_session(path)
    assert doc.expires_at == 0.0  # no derivable expiry -> refresh on first use


def test_import_codex_cli_session_expiry_prefers_access_token(tmp_path: Path) -> None:
    path = tmp_path / "auth.json"
    path.write_text(
        json.dumps(
            _codex_session(
                tokens={
                    "access_token": _jwt({"exp": 2000000000}),
                    "id_token": _jwt({"exp": 1900000000}),
                }
            )
        ),
        encoding="utf-8",
    )
    doc = import_codex_cli_session(path)
    assert doc.expires_at == 2000000000.0  # access-token exp wins over id_token exp


def test_import_codex_cli_session_expiry_falls_back_to_id_token(tmp_path: Path) -> None:
    path = tmp_path / "auth.json"
    path.write_text(
        json.dumps(
            _codex_session(
                tokens={
                    "access_token": _jwt({}),
                    "id_token": _jwt({"exp": 1900000000}),
                }
            )
        ),
        encoding="utf-8",
    )
    doc = import_codex_cli_session(path)
    assert doc.expires_at == 1900000000.0  # access token has no exp -> id_token used


def test_import_codex_cli_session_expiry_zero_when_neither_token_usable(tmp_path: Path) -> None:
    path = tmp_path / "auth.json"
    path.write_text(
        json.dumps(
            _codex_session(
                tokens={
                    "access_token": _jwt({}),
                    "id_token": _jwt({}),
                }
            )
        ),
        encoding="utf-8",
    )
    doc = import_codex_cli_session(path)
    assert doc.expires_at == 0.0  # no usable exp -> refresh on first use


# --------------------------------------------------------------------------- #
# TokenManager refresh transaction
# --------------------------------------------------------------------------- #


def test_refresh_single_process_stale_doc(tmp_path: Path) -> None:
    with _fake_issuer() as server:
        path = _store_path(tmp_path)
        store = OAuthStore(path)
        store.save_provider(_stale_doc())

        manager = TokenManager("codex", store, client_id=FAKE_CLIENT_ID, issuer=server.issuer)
        access, account_id = manager.ensure_fresh()

        assert access == "new-access-1"
        assert account_id == ACCOUNT_ID  # rotated from the id_token claims
        assert server.fake.refresh_count == 1
        assert "refresh_token=stale-refresh" in server.fake.refresh_bodies[0]
        record = store.read_provider("codex")
        assert record is not None and not record.disabled
        assert record.doc.access_token == "new-access-1"
        assert record.doc.refresh_token == "new-refresh-1"
        assert record.doc.expires_at > time.time() + 3500


def test_refresh_fresh_doc_skips_network(tmp_path: Path) -> None:
    with _fake_issuer() as server:
        store = OAuthStore(_store_path(tmp_path))
        store.save_provider(_doc(access="fresh-access", refresh="fresh-refresh"))

        manager = TokenManager("codex", store, client_id=FAKE_CLIENT_ID, issuer=server.issuer)
        access, account_id = manager.ensure_fresh()

        assert access == "fresh-access"
        assert account_id == ACCOUNT_ID
        assert server.fake.refresh_count == 0


def test_refresh_uses_concurrent_rotation_for_rejected_token(tmp_path: Path) -> None:
    with _fake_issuer() as server:
        store = OAuthStore(_store_path(tmp_path))
        store.save_provider(_stale_doc())
        # Another process already rotated past the token the caller saw rejected.
        store.save_provider(
            _doc(access="rotated-access", refresh="rotated-refresh", expires_at=time.time() + 3600)
        )

        manager = TokenManager("codex", store, client_id=FAKE_CLIENT_ID, issuer=server.issuer)
        access, _ = manager.ensure_fresh(rejected=STALE_ACCESS)

        assert access == "rotated-access"
        assert server.fake.refresh_count == 0


def test_refresh_preserves_old_refresh_when_response_omits_it(tmp_path: Path) -> None:
    with _fake_issuer() as server:
        server.fake.refresh_include_refresh = False
        store = OAuthStore(_store_path(tmp_path))
        store.save_provider(_stale_doc())

        manager = TokenManager("codex", store, client_id=FAKE_CLIENT_ID, issuer=server.issuer)
        access, _ = manager.ensure_fresh()

        assert access == "new-access-1"
        record = cast(OAuthRecord, store.read_provider("codex"))
        assert record.doc.access_token == "new-access-1"
        assert record.doc.refresh_token == STALE_REFRESH  # preserved


def test_refresh_missing_credentials(tmp_path: Path) -> None:
    with _fake_issuer() as server:
        store = OAuthStore(_store_path(tmp_path))
        manager = TokenManager("codex", store, client_id=FAKE_CLIENT_ID, issuer=server.issuer)
        with pytest.raises(OAuthMissingError):
            manager.ensure_fresh()


def _refresh_worker(
    store_path: str,
    issuer: str,
    barrier: Any,
    out_queue: Any,
) -> None:
    store = OAuthStore(Path(store_path))
    manager = TokenManager("codex", store, client_id=FAKE_CLIENT_ID, issuer=issuer)
    barrier.wait()
    try:
        access, account_id = manager.ensure_fresh()
    except Exception as exc:  # pragma: no cover - failure diagnostics
        out_queue.put(("error", repr(exc)))
    else:
        out_queue.put(("ok", access, account_id))


def test_refresh_multiprocess_rotating_race(tmp_path: Path) -> None:
    """Two processes start from the same stale doc: exactly one refresh POST.

    Both processes use the real per-provider flock file and the fake issuer;
    the loser of the lock re-reads the store and returns the rotated document
    without refreshing, and the old refresh token is never reused after the
    rotation.
    """
    with _fake_issuer() as server:
        # Hold the first refresh long enough for the other process to contend
        # for the real flock, without spending half a second in the fake HTTP
        # handler on every run.
        server.fake.refresh_sleep_s = 0.1
        path = _store_path(tmp_path)
        store = OAuthStore(path)
        store.save_provider(_stale_doc())

        context = multiprocessing.get_context("spawn")
        barrier = context.Barrier(2)
        out_queue = context.Queue()
        workers = [
            context.Process(
                target=_refresh_worker,
                args=(str(path), server.issuer, barrier, out_queue),
            )
            for _ in range(2)
        ]
        for worker in workers:
            worker.start()
        for worker in workers:
            worker.join(timeout=60)
        assert all(worker.exitcode == 0 for worker in workers)

        results = [out_queue.get(timeout=5) for _ in range(2)]
        assert all(result[0] == "ok" for result in results), results
        assert results[0][1] == results[1][1]  # both end with the SAME access token
        assert results[0][2] == results[1][2]

        assert server.fake.refresh_count == 1  # exactly one refresh POST
        assert len(server.fake.refresh_bodies) == 1
        assert "refresh_token=stale-refresh" in server.fake.refresh_bodies[0]
        # The rotated refresh token is never sent back to the issuer.
        assert "new-refresh" not in server.fake.refresh_bodies[0]

        record = store.read_provider("codex")
        assert record is not None and not record.disabled
        assert record.doc.refresh_token == "new-refresh-1"
        assert record.doc.access_token == "new-access-1"
        # The persistent per-provider lock file survives (never deleted).
        lock_file = path.parent / "oauth.codex.lock"
        assert lock_file.exists()
        assert stat.S_IMODE(lock_file.stat().st_mode) == 0o600


@pytest.mark.parametrize("status", [429, 500, 503, 302])
def test_refresh_transient_failures_keep_last_good(tmp_path: Path, status: int) -> None:
    with _fake_issuer() as server:
        server.fake.refresh_status = status
        path = _store_path(tmp_path)
        store = OAuthStore(path)
        store.save_provider(_stale_doc())
        before = path.read_bytes()

        manager = TokenManager("codex", store, client_id=FAKE_CLIENT_ID, issuer=server.issuer)
        with pytest.raises(RefreshUnavailableError):
            manager.ensure_fresh()

        assert path.read_bytes() == before  # last-good document untouched
        record = store.read_provider("codex")
        assert record is not None and not record.disabled
        assert record.doc.access_token == STALE_ACCESS


def test_refresh_timeout_keeps_last_good(tmp_path: Path) -> None:
    with _fake_issuer() as server:
        server.fake.refresh_sleep_s = 2.0
        server.fake.refresh_status = 200
        path = _store_path(tmp_path)
        store = OAuthStore(path)
        store.save_provider(_stale_doc())
        before = path.read_bytes()

        manager = TokenManager(
            "codex",
            store,
            client_id=FAKE_CLIENT_ID,
            issuer=server.issuer,
            refresh_timeout_s=0.3,
        )
        with pytest.raises(RefreshUnavailableError):
            manager.ensure_fresh()

        assert path.read_bytes() == before
        record = cast(OAuthRecord, store.read_provider("codex"))
        assert record.doc.access_token == STALE_ACCESS


def test_refresh_invalid_grant_disables_provider(tmp_path: Path) -> None:
    with _fake_issuer() as server:
        server.fake.refresh_status = 400
        store = OAuthStore(_store_path(tmp_path))
        store.save_provider(_stale_doc())

        manager = TokenManager("codex", store, client_id=FAKE_CLIENT_ID, issuer=server.issuer)
        with pytest.raises(InvalidGrantError):
            manager.ensure_fresh()
        record = cast(OAuthRecord, store.read_provider("codex"))
        assert record.disabled is True

        # Disabled until re-login: no further network attempts.
        with pytest.raises(InvalidGrantError):
            manager.ensure_fresh()
        assert server.fake.refresh_count == 1


def test_mark_invalid_grant_disables_until_relogin(tmp_path: Path) -> None:
    with _fake_issuer() as server:
        store = OAuthStore(_store_path(tmp_path))
        store.save_provider(_doc())

        manager = TokenManager("codex", store, client_id=FAKE_CLIENT_ID, issuer=server.issuer)
        manager.mark_invalid_grant()
        record = cast(OAuthRecord, store.read_provider("codex"))
        assert record.disabled is True

        with pytest.raises(InvalidGrantError):
            manager.ensure_fresh()
        assert server.fake.refresh_count == 0


# --------------------------------------------------------------------------- #
# Device flow
# --------------------------------------------------------------------------- #


def test_device_flow_happy_path(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    with _fake_issuer() as server:
        store = OAuthStore(_store_path(tmp_path))
        shown: list[tuple[str, str]] = []
        flow = DeviceFlow("codex", client_id=FAKE_CLIENT_ID, issuer=server.issuer, store=store)
        _use_fast_device_polling(flow, monkeypatch)

        doc = flow.run(max_wait_s=10.0, on_code=lambda url, code: shown.append((url, code)))

        assert shown == [(f"{server.issuer}/codex/device", USER_CODE)]
        assert doc.access_token == "flow-access"
        assert doc.refresh_token == "flow-refresh"
        assert doc.account_id == ACCOUNT_ID
        assert doc.expires_at > time.time() + 3500
        assert server.fake.poll_count == 3  # two pending polls, then approval
        assert server.fake.exchange_count == 1
        record = store.read_provider("codex")
        assert record is not None and not record.disabled
        assert record.doc.access_token == "flow-access"


def test_device_flow_poll_expiry_persists_nothing(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    with _fake_issuer() as server:
        server.fake.poll_mode = "pending_forever"
        store = OAuthStore(_store_path(tmp_path))
        flow = DeviceFlow("codex", client_id=FAKE_CLIENT_ID, issuer=server.issuer, store=store)
        _use_fast_device_polling(flow, monkeypatch)

        with pytest.raises(DeviceFlowExpired):
            flow.run(max_wait_s=0.05)

        assert store.read().records == ()


def test_device_flow_approval_after_deadline_is_rejected(tmp_path: Path) -> None:
    with _fake_issuer() as server:
        server.fake.device_token_sleep_s = 0.2
        store = OAuthStore(_store_path(tmp_path))
        flow = DeviceFlow("codex", client_id=FAKE_CLIENT_ID, issuer=server.issuer, store=store)

        with pytest.raises(DeviceFlowExpired):
            flow.run(max_wait_s=0.05)

        assert store.read().records == ()


def test_device_flow_cancel(tmp_path: Path) -> None:
    with _fake_issuer() as server:
        server.fake.poll_mode = "pending_forever"
        store = OAuthStore(_store_path(tmp_path))
        flow = DeviceFlow("codex", client_id=FAKE_CLIENT_ID, issuer=server.issuer, store=store)
        cancel = threading.Event()
        cancel.set()

        with pytest.raises(DeviceFlowCanceled):
            flow.run(max_wait_s=10.0, cancel=cancel)

        assert store.read().records == ()


def test_device_flow_http_timeout(tmp_path: Path) -> None:
    with _fake_issuer() as server:
        server.fake.device_token_sleep_s = 2.0
        store = OAuthStore(_store_path(tmp_path))
        flow = DeviceFlow(
            "codex",
            client_id=FAKE_CLIENT_ID,
            issuer=server.issuer,
            store=store,
            http_timeout_s=0.3,
        )

        with pytest.raises(DeviceFlowError):
            flow.run(max_wait_s=10.0)


def test_oauth_response_larger_than_cap_is_rejected() -> None:
    class OversizedHandler(BaseHTTPRequestHandler):
        protocol_version = "HTTP/1.0"

        def do_POST(self) -> None:  # noqa: N802
            length = int(self.headers.get("Content-Length") or 0)
            self.rfile.read(length)
            body = b"x" * (oauth.MAX_OAUTH_RESPONSE_BYTES + 1)
            self.send_response(200)
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            try:
                self.wfile.write(body)
            except (BrokenPipeError, ConnectionResetError):
                pass

        def log_message(self, format: str = "", *args: object) -> None:
            pass

    server = ThreadingHTTPServer(("127.0.0.1", 0), OversizedHandler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    issuer = f"http://127.0.0.1:{server.server_port}"
    try:
        with pytest.raises(OAuthError, match="response exceeds"):
            oauth._request(issuer, "/token", payload={}, form=False, timeout_s=2.0)
    finally:
        server.shutdown()
        server.server_close()
        thread.join()


def test_transient_secrets_never_appear_in_reprs_or_errors(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    with _fake_issuer() as server:
        server.fake.exchange_status = 500
        store = OAuthStore(_store_path(tmp_path))
        flow = DeviceFlow("codex", client_id=FAKE_CLIENT_ID, issuer=server.issuer, store=store)
        _use_fast_device_polling(flow, monkeypatch)

        code = flow.request_user_code()
        assert USER_CODE not in repr(code)
        assert DEVICE_AUTH_ID not in repr(code)

        approved = flow.poll(code, max_wait_s=10.0)
        assert AUTH_CODE not in repr(approved)
        assert CODE_VERIFIER not in repr(approved)

        with pytest.raises(DeviceFlowError) as raised:
            flow.exchange(approved)
        assert AUTH_CODE not in str(raised.value)
        assert CODE_VERIFIER not in str(raised.value)
