"""Codex OAuth wiring: supervisor preflight/env handoff, CLI, and doctor.

Covers the W3/W4 integration against the real ``cambium.oauth`` store at a
temporary path (constructor/path injection only — no monkeypatching): the
fail-closed supervisor preflight (missing/corrupt/disabled stores), the
spawn-time worker env handoff (access token + account id, never the refresh
token), redactor registration of injected access tokens, the ``cambium auth
oauth`` status/logout/import/device-flow paths, and the opt-in
``doctor --oauth-live`` probe against a loopback fake issuer.
"""

from __future__ import annotations

import base64
import json
import os
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any

import pytest

from cambium import cli, doctor, supervisor
from cambium.oauth import (
    OAuthDoc,
    OAuthStore,
)
from cambium.redact import Redactor

ACCESS = "access-token-1"
REFRESH = "refresh-token-1"
ACCOUNT = "account-id-1"
CLIENT_ID = "cli-client"
USER_CODE = "USER-CODE-1"
DEVICE_AUTH_ID = "device-auth-1"
AUTH_CODE = "auth-code-1"
CODE_VERIFIER = "verifier-1"
FLOW_ACCESS = "flow-access"
FLOW_REFRESH = "flow-refresh"


def _jwt(payload: dict[str, Any]) -> str:
    def _enc(obj: Any) -> str:
        encoded = base64.urlsafe_b64encode(json.dumps(obj).encode("utf-8"))
        return encoded.rstrip(b"=").decode("ascii")

    return f"{_enc({'alg': 'none'})}.{_enc(payload)}.signature"


ID_TOKEN = _jwt({"chatgpt_account_id": ACCOUNT, "exp": 1900000000})


def _store_path(root: Path) -> Path:
    return root / ".local" / "share" / "cambium" / "oauth.json"


def _doc(
    *,
    access: str = ACCESS,
    refresh: str = REFRESH,
    expires_at: float | None = None,
    account_id: str | None = ACCOUNT,
) -> OAuthDoc:
    return OAuthDoc(
        "codex",
        access,
        refresh,
        time.time() + 3600 if expires_at is None else expires_at,
        account_id,
    )


def _codex_config(path: Path) -> Path:
    """A providers.json with one codex_chatgpt provider (and one api-key peer)."""
    path.write_text(
        json.dumps(
            {
                "providers": [
                    {
                        "name": "codex",
                        "tier": "strong",
                        "auth": "codex_chatgpt",
                        "protocol": "codex_responses",
                        "model": "gpt-5.6-sol",
                        "required": True,
                    },
                    {
                        "name": "openai",
                        "tier": "strong",
                        "base_url": "https://api.openai.com/v1",
                        "api_key_env": "CAMBIUM_PROVIDER_OPENAI_API_KEY",
                        "model": "gpt-5.6",
                    },
                ]
            }
        ),
        encoding="utf-8",
    )
    return path


def _codex_spec(config: Path, tmp_path: Path) -> dict[str, Any]:
    return {
        "task_id": "codex-task",
        "task": "do the thing",
        "repo": str(tmp_path / "repo"),
        "worktree_path": str(tmp_path / "wt"),
        "branch": "cambium-oauth-task",
        "fanout_config": {
            "providers": [{"name": "codex"}],
            "tier": "strong",
            "model": "gpt-5.6-sol",
        },
        "provider_config_path": str(config),
    }


# --------------------------------------------------------------------------- #
# Fake issuer (http.server in a thread — loopback only, no network)
# --------------------------------------------------------------------------- #


class _FakeIssuerState:
    def __init__(self) -> None:
        self.lock = threading.Lock()
        self.usercode_count = 0
        self.poll_count = 0
        self.exchange_count = 0
        self.refresh_count = 0
        self.poll_approve_immediately = True
        self.refresh_status = 200


class _FakeIssuerHandler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.0"

    def do_GET(self) -> None:  # noqa: N802 (http.server API)
        body = b'{"ok":true}'
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Connection", "close")
        self.end_headers()
        self.wfile.write(body)

    def do_POST(self) -> None:  # noqa: N802 (http.server API)
        length = int(self.headers.get("Content-Length") or 0)
        raw = self.rfile.read(length) if length else b""
        state: _FakeIssuerState = self.server.fake  # type: ignore[attr-defined]
        if self.path == "/api/accounts/deviceauth/usercode":
            with state.lock:
                state.usercode_count += 1
            status = 200
            body: dict[str, Any] = {
                "device_auth_id": DEVICE_AUTH_ID,
                "user_code": USER_CODE,
                "interval": "0.01",
            }
        elif self.path == "/api/accounts/deviceauth/token":
            with state.lock:
                state.poll_count += 1
                approved = state.poll_approve_immediately
            if not approved:
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
                with state.lock:
                    state.refresh_count += 1
                    status = state.refresh_status
                if status == 200:
                    body = {
                        "access_token": "refreshed-access",
                        "refresh_token": "refreshed-refresh",
                        "expires_in": 3600,
                        "id_token": ID_TOKEN,
                    }
                elif status == 400:
                    body = {"error": "invalid_grant"}
                else:
                    body = {"error": "server_error"}
            else:
                with state.lock:
                    state.exchange_count += 1
                status = 200
                body = {
                    "access_token": FLOW_ACCESS,
                    "refresh_token": FLOW_REFRESH,
                    "expires_in": 3600,
                    "id_token": ID_TOKEN,
                }
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

    def log_message(self, *args: object) -> None:
        pass


class _FakeIssuer:
    """Loopback-only fake of the codex issuer device-auth and token endpoints."""

    def __init__(self) -> None:
        self.fake = _FakeIssuerState()
        self._httpd = ThreadingHTTPServer(("127.0.0.1", 0), _FakeIssuerHandler)
        self._httpd.fake = self.fake  # type: ignore[attr-defined]
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


@pytest.fixture
def fake_issuer() -> _FakeIssuer:
    server = _FakeIssuer()
    try:
        yield server
    finally:
        server.close()


# --------------------------------------------------------------------------- #
# Supervisor preflight (fail closed, LOCAL read only)
# --------------------------------------------------------------------------- #


def test_preflight_rejects_missing_store(tmp_path: Path) -> None:
    config = _codex_config(tmp_path / "providers.json")
    spec = _codex_spec(config, tmp_path)
    store = OAuthStore(_store_path(tmp_path))

    with pytest.raises(ValueError, match="no oauth session is stored"):
        supervisor._validate_provider_environment([spec], None, oauth_store=store)


def test_preflight_rejects_corrupt_store(tmp_path: Path) -> None:
    config = _codex_config(tmp_path / "providers.json")
    spec = _codex_spec(config, tmp_path)
    store = OAuthStore(_store_path(tmp_path))
    store.save_provider(_doc())
    target = store.path
    target.write_text('{"version":1,"providers":{"codex":{broken', encoding="utf-8")
    os.chmod(target, 0o600)

    with pytest.raises(ValueError, match="oauth store is invalid"):
        supervisor._validate_provider_environment([spec], None, oauth_store=store)


def test_preflight_rejects_disabled_store(tmp_path: Path) -> None:
    config = _codex_config(tmp_path / "providers.json")
    spec = _codex_spec(config, tmp_path)
    store = OAuthStore(_store_path(tmp_path))
    store.save_provider(_doc(expires_at=time.time() - 100), disabled=True)

    with pytest.raises(ValueError, match="disabled"):
        supervisor._validate_provider_environment([spec], None, oauth_store=store)


def test_preflight_accepts_fresh_and_expired_refreshable_stores(tmp_path: Path) -> None:
    config = _codex_config(tmp_path / "providers.json")
    spec = _codex_spec(config, tmp_path)
    store = OAuthStore(_store_path(tmp_path))
    store.save_provider(_doc())
    # No exception: the preflight is a local read with no network probe.
    supervisor._validate_provider_environment([spec], None, oauth_store=store)

    store.save_provider(_doc(expires_at=time.time() - 100))
    supervisor._validate_provider_environment([spec], None, oauth_store=store)


def test_preflight_ignores_non_codex_tasks(tmp_path: Path) -> None:
    """A fanout task that references only api_key providers is untouched."""
    config = _codex_config(tmp_path / "providers.json")
    spec = _codex_spec(config, tmp_path)
    spec["fanout_config"] = {"providers": [{"name": "openai"}], "tier": "strong", "model": "m"}
    store = OAuthStore(_store_path(tmp_path))  # empty store

    supervisor._validate_provider_environment([spec], None, oauth_store=store)


# --------------------------------------------------------------------------- #
# Worker env handoff: access token + account id, NEVER the refresh token
# --------------------------------------------------------------------------- #


def test_worker_environment_injects_access_and_account_never_refresh(
    tmp_path: Path,
) -> None:
    config = _codex_config(tmp_path / "providers.json")
    spec = _codex_spec(config, tmp_path)
    store = OAuthStore(_store_path(tmp_path))
    store.save_provider(_doc())

    env = supervisor._worker_environment(spec, 1, oauth_store=store)

    assert env["CAMBIUM_OAUTH_ACCESS_CODEX"] == ACCESS
    assert env["CAMBIUM_OAUTH_ACCOUNT_CODEX"] == ACCOUNT
    assert env["CAMBIUM_PROVIDERS"] == str(config.resolve())
    # The refresh token never leaves the supervisor process.
    assert REFRESH not in env.values()
    assert not any("REFRESH" in name for name in env)


def test_worker_environment_registers_access_token_with_redactor(
    tmp_path: Path,
) -> None:
    config = _codex_config(tmp_path / "providers.json")
    spec = _codex_spec(config, tmp_path)
    store = OAuthStore(_store_path(tmp_path))
    store.save_provider(_doc())
    redactor = Redactor()

    supervisor._worker_environment(spec, 1, oauth_store=store, redactor=redactor)

    assert redactor.redact(f"bearer {ACCESS}") == f"bearer {redactor.replacement}"
    assert redactor.redact("account-id-1") == "account-id-1"


def test_worker_environment_non_codex_task_gets_no_oauth_env(tmp_path: Path) -> None:
    spec = {
        "task_id": "marker",
        "worktree_path": str(tmp_path / "wt"),
        "fanout_config": {"tier": "fast", "model": "loopback-model"},
    }
    env = supervisor._worker_environment(spec, 1)
    assert not any(name.startswith("CAMBIUM_OAUTH_") for name in env)


def test_worker_environment_injects_for_empty_authorized_set(tmp_path: Path) -> None:
    """An empty authorized_providers list (validator normalization) is no
    restriction: the codex provider referenced through fanout_config still gets
    its token injected, matching the worker's unrestricted semantics."""
    config = _codex_config(tmp_path / "providers.json")
    spec = _codex_spec(config, tmp_path)
    spec["authorized_providers"] = []
    store = OAuthStore(_store_path(tmp_path))
    store.save_provider(_doc())

    env = supervisor._worker_environment(spec, 1, oauth_store=store)

    assert env["CAMBIUM_OAUTH_ACCESS_CODEX"] == ACCESS
    assert env["CAMBIUM_OAUTH_ACCOUNT_CODEX"] == ACCOUNT


# --------------------------------------------------------------------------- #
# CLI: status / logout / import / device flow
# --------------------------------------------------------------------------- #


def test_cli_status_has_fingerprint_and_no_secrets(tmp_path: Path) -> None:
    store = OAuthStore(_store_path(tmp_path))
    store.save_provider(_doc())

    assert cli._run_auth_oauth_status(store, "codex") == 0
    text = cli._oauth_status_text(store, "codex")

    assert "oauth session" in text
    assert "fingerprint" in text
    assert ACCESS not in text
    assert REFRESH not in text
    assert ACCOUNT not in text
    # The fingerprint is the first 8 hex of SHA-256 over the account id.
    import hashlib

    expected = hashlib.sha256(ACCOUNT.encode("utf-8")).hexdigest()[:8]
    assert expected in text


def test_cli_status_missing_session_fails_without_secrets(tmp_path: Path) -> None:
    store = OAuthStore(_store_path(tmp_path))
    assert cli._run_auth_oauth_status(store, "codex") == 1


def test_cli_logout_removes_locally_without_revoke_claim(tmp_path: Path) -> None:
    store = OAuthStore(_store_path(tmp_path))
    store.save_provider(_doc())

    assert cli._run_auth_oauth_logout(store, "codex") == 0
    assert store.read_provider("codex") is None
    assert cli._run_auth_oauth_logout(store, "codex") == 0


def test_cli_import_codex_cli_parses_real_shape(tmp_path: Path) -> None:
    session_path = tmp_path / "auth.json"
    session_path.write_text(
        json.dumps(
            {
                "auth_mode": "chatgpt",
                "tokens": {
                    "access_token": "cli-access",
                    "refresh_token": "cli-refresh",
                    "account_id": "cli-acct",
                    "id_token": ID_TOKEN,
                },
                "last_refresh": "2026-08-09T20:53:18.221278873Z",
            }
        ),
        encoding="utf-8",
    )
    store = OAuthStore(_store_path(tmp_path))

    assert cli._run_auth_oauth_import(store, path=session_path) == 0

    record = store.read_provider("codex")
    assert record is not None and not record.disabled
    assert record.doc.access_token == "cli-access"
    assert record.doc.refresh_token == "cli-refresh"
    assert record.doc.account_id == "cli-acct"
    assert record.doc.expires_at == 1900000000.0


def test_cli_device_flow_stores_session_and_keeps_code_off_stdout(
    tmp_path: Path, fake_issuer: _FakeIssuer, capsys: pytest.CaptureFixture[str]
) -> None:
    store = OAuthStore(_store_path(tmp_path))
    tty_lines: list[str] = []

    exit_code = cli._run_auth_oauth_device(
        "codex",
        CLIENT_ID,
        store=store,
        issuer=fake_issuer.issuer,
        tty=lambda text: tty_lines.append(text),
    )

    assert exit_code == 0
    record = store.read_provider("codex")
    assert record is not None and not record.disabled
    assert record.doc.access_token == FLOW_ACCESS
    assert record.doc.refresh_token == FLOW_REFRESH
    assert record.doc.account_id == ACCOUNT
    assert fake_issuer.fake.usercode_count == 1
    assert fake_issuer.fake.poll_count == 1
    assert fake_issuer.fake.exchange_count == 1
    # The verification URL and user code reached only the injected TTY writer.
    assert any(
        f"{fake_issuer.issuer}/codex/device" in line and USER_CODE in line
        for line in tty_lines
    )
    # stdout/stderr carry the outcome line only — never a token or code.
    captured = capsys.readouterr()
    assert "stored oauth session for provider codex" in captured.out
    assert FLOW_ACCESS not in captured.out and FLOW_ACCESS not in captured.err
    assert FLOW_REFRESH not in captured.out
    assert USER_CODE not in captured.out
    assert "".join(tty_lines) != "" and USER_CODE in "".join(tty_lines)


def test_cli_device_flow_requires_client_id(tmp_path: Path) -> None:
    assert cli._run_auth_oauth_device("codex", "", store=OAuthStore(_store_path(tmp_path))) == 1


def test_cli_oauth_parser_rejects_conflicting_subcommands(capsys) -> None:
    with pytest.raises(SystemExit) as raised:
        cli.main(["auth", "oauth", "status", "codex", "logout"])

    assert raised.value.code == 2
    assert "invalid command arguments" in capsys.readouterr().err


# --------------------------------------------------------------------------- #
# doctor --oauth-live (opt-in; fake issuer, no network)
# --------------------------------------------------------------------------- #


def test_doctor_oauth_live_refreshable_and_reachable(
    tmp_path: Path, fake_issuer: _FakeIssuer
) -> None:
    config = _codex_config(tmp_path / "providers.json")
    store = OAuthStore(_store_path(tmp_path))
    store.save_provider(_doc(expires_at=time.time() - 100))  # stale -> real refresh

    status, detail = doctor.check_oauth_live(
        tmp_path,
        provider_config=config,
        oauth_store=store,
        client_id=CLIENT_ID,
        issuer=fake_issuer.issuer,
        timeout_s=5.0,
    )

    assert status is doctor.Status.PASS
    assert "codex=refreshable" in detail
    assert fake_issuer.fake.refresh_count == 1
    assert "issuer HTTP 200" in detail


def test_doctor_oauth_live_rejected_grant_fails(
    tmp_path: Path, fake_issuer: _FakeIssuer
) -> None:
    config = _codex_config(tmp_path / "providers.json")
    store = OAuthStore(_store_path(tmp_path))
    store.save_provider(_doc(expires_at=time.time() - 100))
    fake_issuer.fake.refresh_status = 400

    status, detail = doctor.check_oauth_live(
        tmp_path,
        provider_config=config,
        oauth_store=store,
        client_id=CLIENT_ID,
        issuer=fake_issuer.issuer,
        timeout_s=5.0,
    )

    assert status is doctor.Status.FAIL
    assert "codex=refresh-rejected" in detail


def test_doctor_oauth_live_missing_session_warns(
    tmp_path: Path, fake_issuer: _FakeIssuer
) -> None:
    config = _codex_config(tmp_path / "providers.json")
    store = OAuthStore(_store_path(tmp_path))  # empty

    status, detail = doctor.check_oauth_live(
        tmp_path,
        provider_config=config,
        oauth_store=store,
        client_id=CLIENT_ID,
        issuer=fake_issuer.issuer,
        timeout_s=5.0,
    )

    assert status is doctor.Status.WARN
    assert "codex=no-session" in detail


def test_doctor_oauth_live_without_client_id_skips_refresh(
    tmp_path: Path, fake_issuer: _FakeIssuer
) -> None:
    config = _codex_config(tmp_path / "providers.json")
    store = OAuthStore(_store_path(tmp_path))
    store.save_provider(_doc(expires_at=time.time() - 100))

    status, detail = doctor.check_oauth_live(
        tmp_path,
        provider_config=config,
        oauth_store=store,
        client_id="",
        issuer=fake_issuer.issuer,
        timeout_s=5.0,
    )

    assert status is doctor.Status.WARN
    assert "codex=refresh-skipped(no client id)" in detail
    assert fake_issuer.fake.refresh_count == 0


def test_doctor_oauth_live_skips_without_codex_providers(tmp_path: Path) -> None:
    config = tmp_path / "providers.json"
    config.write_text(
        json.dumps(
            {
                "providers": [
                    {
                        "name": "openai",
                        "tier": "strong",
                        "base_url": "https://api.openai.com/v1",
                        "api_key_env": "CAMBIUM_PROVIDER_OPENAI_API_KEY",
                    }
                ]
            }
        ),
        encoding="utf-8",
    )
    status, detail = doctor.check_oauth_live(tmp_path, provider_config=config)
    assert status is doctor.Status.PASS
    assert "nothing to probe live" in detail


# --------------------------------------------------------------------------- #
# P0 bridge (glm-5.2 review): the worker must consume the injected
# CAMBIUM_OAUTH_ACCESS_/ACCOUNT_<SUFFIX> env vars and fail closed without them.
# --------------------------------------------------------------------------- #


def _codex_providers_file(path: Path) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(
            {
                "providers": [
                    {
                        "name": "codex",
                        "tier": "strong",
                        "auth": "codex_chatgpt",
                        "protocol": "codex_responses",
                        "model": "gpt-5.6-luna",
                        "priority": 0,
                    }
                ]
            }
        ),
        encoding="utf-8",
    )
    return path


def test_provider_router_wires_credential_from_env(tmp_path: Path) -> None:
    """The worker's _provider_router builds Diffundo with the injected
    CredentialSource (access token + account id) — the P0 bridge."""
    from cambium import worker
    from cambium.diffundo import CredentialSource

    config_path = _codex_providers_file(tmp_path / "providers.json")
    previous = {
        "CAMBIUM_PROVIDERS": os.environ.get("CAMBIUM_PROVIDERS"),
        "CAMBIUM_OAUTH_ACCESS_CODEX": os.environ.get("CAMBIUM_OAUTH_ACCESS_CODEX"),
        "CAMBIUM_OAUTH_ACCOUNT_CODEX": os.environ.get("CAMBIUM_OAUTH_ACCOUNT_CODEX"),
    }
    try:
        os.environ["CAMBIUM_PROVIDERS"] = str(config_path.resolve())
        os.environ["CAMBIUM_OAUTH_ACCESS_CODEX"] = ACCESS
        os.environ["CAMBIUM_OAUTH_ACCOUNT_CODEX"] = ACCOUNT
        router, tier, model, identity = worker._provider_router(
            {"diffundo": {"tier": "strong", "model": "gpt-5.6-luna"}}
        )
    finally:
        for key, value in previous.items():
            if value is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = value

    assert tier.value == "strong"
    assert model == "gpt-5.6-luna"
    source = router._credential_source
    assert isinstance(source, CredentialSource)
    assert source.access_token == ACCESS
    assert source.account_id == ACCOUNT


def test_provider_router_fails_closed_without_oauth_env(tmp_path: Path) -> None:
    """A codex provider without the injected access-token env var fails at
    router construction — never a request-time guess."""
    from cambium import worker

    config_path = _codex_providers_file(tmp_path / "providers.json")
    previous = {
        "CAMBIUM_PROVIDERS": os.environ.get("CAMBIUM_PROVIDERS"),
        "CAMBIUM_OAUTH_ACCESS_CODEX": os.environ.get("CAMBIUM_OAUTH_ACCESS_CODEX"),
    }
    try:
        os.environ["CAMBIUM_PROVIDERS"] = str(config_path.resolve())
        os.environ.pop("CAMBIUM_OAUTH_ACCESS_CODEX", None)
        try:
            worker._provider_router(
                {"diffundo": {"tier": "strong", "model": "gpt-5.6-luna"}}
            )
        except ValueError as exc:
            assert "CAMBIUM_OAUTH_ACCESS_CODEX" in str(exc)
        else:
            raise AssertionError("missing oauth env must fail closed")
    finally:
        for key, value in previous.items():
            if value is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = value
