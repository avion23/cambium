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
from pathlib import Path

from cambium import doctor

REPO_ROOT = Path(__file__).resolve().parents[2]
SRC_DIR = str(REPO_ROOT / "src")
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
    env = dict(os.environ)
    env["PYTHONPATH"] = os.pathsep.join(filter(None, [SRC_DIR, env.get("PYTHONPATH")]))
    return subprocess.run(
        DOCTOR, cwd=cwd, env=env, capture_output=True, text=True, timeout=300
    )


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

    assert result.returncode == 0, result.stdout + result.stderr
    assert "Provider env" in result.stdout
    assert "missing provider credential is WARN" in result.stdout
    assert "0 fail" in result.stdout


def test_doctor_opens_session_databases_with_special_character_paths(tmp_path) -> None:
    session_dir = tmp_path / "session?query#fragment"
    events_db = session_dir / doctor.EVENTS_DB_REL
    conversations_db = session_dir / doctor.CONVERSATIONS_DB_REL
    events_db.parent.mkdir(parents=True)
    conversations_db.parent.mkdir(parents=True)

    with sqlite3.connect(events_db) as connection:
        connection.execute("CREATE TABLE events (id INTEGER PRIMARY KEY)")
    with sqlite3.connect(conversations_db):
        pass

    event_status, event_detail = doctor.check_event_store(session_dir)
    conversation_status, conversation_detail = doctor.check_conversation_store(session_dir)

    assert event_status is doctor.Status.PASS, event_detail
    assert conversation_status is doctor.Status.PASS, conversation_detail
