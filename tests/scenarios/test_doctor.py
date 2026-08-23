"""Doctor provider-env checks: an empty provider key is not configured.

Diffundo rejects an empty ``api_key_env`` value at call time with
``ProviderOutcome.AUTH_ERROR``. ``cambium doctor`` must report the same
failure for a required key set to ``""`` that it reports for a missing key,
and keep a non-required empty key a WARN.
"""

from __future__ import annotations

import json
import os
import re
import sqlite3
import subprocess
import sys
import time
from pathlib import Path
from types import SimpleNamespace
from typing import cast

from cambium import doctor, routing
from cambium.process_env import build_subprocess_env
from cambium.routing import DebtStore

REPO_ROOT = Path(__file__).resolve().parents[2]
DOCTOR = [sys.executable, "-m", "cambium.doctor"]


def _provider(*, required: bool) -> dict[str, object]:
    return {
        "name": "test-provider",
        "tier": "fast",
        "base_url": "https://api.example.test/v1",
        "api_key_env": "CAMBIUM_PROVIDER_TEST_PROVIDER_API_KEY",
        "required": required,
        "timeout_s": 30.0,
        "max_retries": 2,
        "rpm": 60,
        "enabled": True,
        "model": "example-model",
        "priority": 0,
        "cooldown_s": 60.0,
        "price": 0.0,
    }


def _write_config(tmp_path: Path, required: bool) -> Path:
    config = tmp_path / ".cambium" / "providers.json"
    config.parent.mkdir(parents=True)
    config.write_text(json.dumps({"providers": [_provider(required=required)]}), encoding="utf-8")
    return config


def _run_doctor(cwd: Path) -> subprocess.CompletedProcess[str]:
    env = build_subprocess_env(
        os.environ,
        allowed_keys=("CAMBIUM_PROVIDERS", "CAMBIUM_PROVIDER_TEST_PROVIDER_API_KEY"),
        worktree=cwd,
    )
    return subprocess.run(DOCTOR, cwd=cwd, env=env, capture_output=True, text=True, timeout=300)


def test_doctor_fails_on_empty_required_provider_key(tmp_path, monkeypatch) -> None:
    monkeypatch.delenv("CAMBIUM_PROVIDERS", raising=False)
    _write_config(tmp_path, required=True)
    monkeypatch.setenv("CAMBIUM_PROVIDER_TEST_PROVIDER_API_KEY", "")

    result = _run_doctor(tmp_path)

    assert result.returncode == 1, result.stdout + result.stderr
    assert "Provider env" in result.stdout
    assert "required provider credential missing" in result.stdout
    assert re.search(r"Summary: .* [1-9]\d* fail", result.stdout)


def test_doctor_warns_on_empty_optional_provider_key(tmp_path, monkeypatch) -> None:
    monkeypatch.delenv("CAMBIUM_PROVIDERS", raising=False)
    _write_config(tmp_path, required=False)
    monkeypatch.setenv("CAMBIUM_PROVIDER_TEST_PROVIDER_API_KEY", "")

    result = _run_doctor(tmp_path)

    assert "Provider env" in result.stdout
    assert "missing provider credential is WARN" in result.stdout
    assert re.search(r"Summary: .* [0-9]+ fail", result.stdout)


def test_doctor_provider_row_shows_configured_model(tmp_path, monkeypatch) -> None:
    monkeypatch.delenv("CAMBIUM_PROVIDERS", raising=False)
    _write_config(tmp_path, required=False)
    monkeypatch.setenv("CAMBIUM_PROVIDER_TEST_PROVIDER_API_KEY", "")

    status, detail = doctor.check_provider_env(tmp_path)

    assert status is doctor.Status.WARN, detail
    assert "test-provider(model=example-model)=missing" in detail


def test_doctor_provider_row_surfaces_durable_quarantine_reason(tmp_path, monkeypatch) -> None:
    monkeypatch.delenv("CAMBIUM_PROVIDERS", raising=False)
    _write_config(tmp_path, required=False)
    monkeypatch.setenv("CAMBIUM_PROVIDER_TEST_PROVIDER_API_KEY", "")
    ledger_path = tmp_path / "routing-state.json"
    ledger = DebtStore(ledger_path)
    ledger.record(
        {
            "provider": "test-provider",
            "failure_reason": "config_error: The model example-model was not found",
        }
    )
    ledger.save()
    monkeypatch.setattr(routing, "DEFAULT_ROUTING_STATE_PATH", ledger_path)

    status, detail = doctor.check_provider_env(tmp_path)

    assert status is doctor.Status.WARN, detail
    assert "test-provider(model=example-model)=missing (disabled: config_error:" in detail


def test_doctor_ignores_corrupt_routing_ledger(tmp_path, monkeypatch) -> None:
    monkeypatch.delenv("CAMBIUM_PROVIDERS", raising=False)
    _write_config(tmp_path, required=False)
    monkeypatch.setenv("CAMBIUM_PROVIDER_TEST_PROVIDER_API_KEY", "")
    ledger_path = tmp_path / "routing-state.json"
    ledger_path.write_text("{not valid json", encoding="utf-8")
    monkeypatch.setattr(routing, "DEFAULT_ROUTING_STATE_PATH", ledger_path)

    status, detail = doctor.check_provider_env(tmp_path)

    assert status is doctor.Status.WARN, detail
    assert "(disabled:" not in detail


def test_oauth_session_presence_matches_runtime_usability() -> None:
    now = time.time()

    class Store:
        def __init__(self, record: object) -> None:
            self.record = record

        def read_provider(self, name: str) -> object:
            return self.record

    cases = [
        (now + 3600, "refresh", False, True),
        (now - 1, "refresh", False, True),
        (now - 1, "", False, False),
        (now + 3600, "refresh", True, False),
    ]
    for expires_at, refresh_token, disabled, expected in cases:
        record = SimpleNamespace(
            doc=SimpleNamespace(expires_at=expires_at, refresh_token=refresh_token),
            disabled=disabled,
        )
        assert (
            doctor._oauth_session_present(cast(doctor.OAuthStore, Store(record)), "codex")
            is expected
        )


def test_check_secrets_uses_effective_home_not_home_env(tmp_path, monkeypatch) -> None:
    effective_home = tmp_path / "effective-home"
    spoofed_home = tmp_path / "spoofed-home"
    models = effective_home / ".omp" / "agent" / "models.yml"
    models.parent.mkdir(parents=True)
    models.write_text("models: []", encoding="utf-8")
    monkeypatch.setattr(doctor.auth, "effective_home", lambda: effective_home)
    monkeypatch.setenv("HOME", str(spoofed_home))

    status, detail = doctor.check_secrets()

    assert status is doctor.Status.PASS
    assert str(models) in detail
    assert str(spoofed_home) not in detail


def test_doctor_fails_when_conversation_schema_is_missing(tmp_path) -> None:
    db = tmp_path / "session" / doctor.CONVERSATIONS_DB_REL
    db.parent.mkdir(parents=True)
    with sqlite3.connect(db):
        pass

    status, detail = doctor.check_conversation_store(tmp_path / "session")

    assert status is doctor.Status.FAIL
    assert "missing conversations table" in detail


def test_doctor_opens_session_databases_with_special_character_paths(tmp_path) -> None:
    session_dir = tmp_path / "session?query#fragment"
    events_db = session_dir / doctor.EVENTS_DB_REL
    conversations_db = session_dir / doctor.CONVERSATIONS_DB_REL
    events_db.parent.mkdir(parents=True)
    conversations_db.parent.mkdir(parents=True, exist_ok=True)

    with sqlite3.connect(events_db) as connection:
        connection.execute("CREATE TABLE events (id INTEGER PRIMARY KEY)")
    with sqlite3.connect(conversations_db) as connection:
        connection.execute(
            """CREATE TABLE conversations (
                id INTEGER PRIMARY KEY,
                node_id TEXT NOT NULL,
                parent_id INTEGER NULL,
                turn INTEGER NOT NULL,
                role TEXT NOT NULL,
                content TEXT NOT NULL,
                ts TEXT NOT NULL,
                seq INTEGER NOT NULL,
                tokens INTEGER NULL,
                kind TEXT NOT NULL DEFAULT 'turn',
                meta TEXT NULL
            )"""
        )

    event_status, event_detail = doctor.check_event_store(session_dir)
    conversation_status, conversation_detail = doctor.check_conversation_store(session_dir)

    assert event_status is doctor.Status.PASS, event_detail
    assert conversation_status is doctor.Status.PASS, conversation_detail
