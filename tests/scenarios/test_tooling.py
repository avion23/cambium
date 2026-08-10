"""Tooling scenario tests: `cambium doctor` diagnostics + ruff hygiene.

No mocks: drive the real ``python -m cambium.doctor`` subprocess in the repo
root against healthy and deliberately corrupt provider/session artifacts, and
run ruff over ``src`` with the project's rules.
"""

from __future__ import annotations

import json
import os
import re
import shutil
import sqlite3
import subprocess
import sys
from pathlib import Path

from cambium import doctor

REPO_ROOT = Path(__file__).resolve().parents[2]
DOCTOR = [sys.executable, "-m", "cambium.doctor"]
_CREDENTIAL_ENV_RE = re.compile(
    r"(api|key|token|secret|password|passwd|credential|authorization)", re.IGNORECASE
)


def _run_doctor(*args: str, cwd: Path = REPO_ROOT) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [*DOCTOR, *args], cwd=cwd, capture_output=True, text=True, timeout=300
    )


def _provider(
    *, required: bool = False, api_key_env: str = "CAMBIUM_PROVIDER_TEST_PROVIDER_API_KEY"
) -> dict:
    return {
        "name": "test-provider",
        "tier": "fast",
        "base_url": "https://api.example.test/v1",
        "api_key_env": api_key_env,
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


def test_doctor_exits_zero_on_healthy_repo() -> None:
    result = _run_doctor()
    assert result.returncode == 0, result.stdout + result.stderr
    assert "Summary:" in result.stdout
    assert "0 fail" in result.stdout
    assert "Dataset integrity" in result.stdout
    assert "module-owned JSONL" in result.stdout


def test_doctor_exits_zero_without_example_module(tmp_path, monkeypatch) -> None:
    monkeypatch.setenv("CAMBIUM_PROVIDERS", str(tmp_path / "missing-providers.json"))
    package = tmp_path / "cambium"
    shutil.copytree(
        REPO_ROOT / "src" / "cambium",
        package,
        ignore=shutil.ignore_patterns("example", "__pycache__"),
    )
    environment = {
        name: value
        for name, value in os.environ.items()
        if name != "CAMBIUM_PROVIDERS" and _CREDENTIAL_ENV_RE.search(name) is None
    }
    environment["PYTHONPATH"] = str(tmp_path)

    result = subprocess.run(
        DOCTOR,
        cwd=tmp_path,
        env=environment,
        capture_output=True,
        text=True,
        timeout=300,
    )

    assert result.returncode == 0, result.stdout + result.stderr
    assert "no module-owned JSONL datasets" in result.stdout
    assert "0 fail" in result.stdout


def test_doctor_skips_absent_module_directory(tmp_path, monkeypatch) -> None:
    monkeypatch.setattr(doctor, "MODULES_ROOT", tmp_path / "missing-modules")

    status, detail = doctor.check_dataset()

    assert status is doctor.Status.SKIP
    assert "no module-owned JSONL datasets" in detail


def test_doctor_fails_on_invalid_module_dataset(tmp_path, monkeypatch) -> None:
    dataset = tmp_path / "modules" / "custom" / "datasets" / "records.jsonl"
    dataset.parent.mkdir(parents=True)
    dataset.write_text('{"id": "ok"}\nnot-json\n', encoding="utf-8")
    monkeypatch.setattr(doctor, "MODULES_ROOT", tmp_path / "modules")

    status, detail = doctor.check_dataset()

    assert status is doctor.Status.FAIL
    assert "invalid JSON" in detail
    assert "records.jsonl:2" in detail


def test_doctor_fails_on_unreadable_module_directory(tmp_path, monkeypatch) -> None:
    module = tmp_path / "modules" / "custom"
    dataset = module / "datasets" / "records.jsonl"
    dataset.parent.mkdir(parents=True)
    dataset.write_text("not-json\n", encoding="utf-8")
    module.chmod(0)
    monkeypatch.setattr(doctor, "MODULES_ROOT", tmp_path / "modules")

    try:
        status, detail = doctor.check_dataset()
    finally:
        module.chmod(0o755)

    assert status is doctor.Status.FAIL
    assert "dataset integrity check failed" in detail
    assert "cannot read directory" in detail


def test_doctor_fails_on_corrupt_event_store(tmp_path) -> None:
    session_dir = tmp_path / "session"
    db = session_dir / ".cambium" / "events.db"
    db.parent.mkdir(parents=True)
    db.write_bytes(b"this is not a sqlite database\ncorrupted\x00payload\n")

    result = _run_doctor("--session-dir", str(session_dir))

    assert result.returncode == 1, result.stdout + result.stderr
    assert "FAIL" in result.stdout
    assert "events.db" in result.stdout
    assert "1 fail" in result.stdout


def test_doctor_fails_on_corrupt_conversation_store(tmp_path) -> None:
    session_dir = tmp_path / "session"
    db = session_dir / ".cambium" / "sessions" / "conversations.db"
    db.parent.mkdir(parents=True)
    db.write_bytes(b"this is not a sqlite database\ncorrupted\x00payload\n")

    result = _run_doctor("--session-dir", str(session_dir))

    assert result.returncode == 1, result.stdout + result.stderr
    assert "FAIL" in result.stdout
    assert "conversations.db" in result.stdout
    assert "1 fail" in result.stdout


def test_doctor_warns_on_missing_optional_provider_key(tmp_path, monkeypatch) -> None:
    monkeypatch.delenv("CAMBIUM_PROVIDERS", raising=False)
    monkeypatch.delenv("CAMBIUM_PROVIDER_TEST_PROVIDER_API_KEY", raising=False)
    config = tmp_path / ".cambium" / "providers.json"
    config.parent.mkdir(parents=True)
    config.write_text(json.dumps({"providers": [_provider()]}), encoding="utf-8")

    result = _run_doctor(cwd=tmp_path)

    assert result.returncode == 0, result.stdout + result.stderr
    assert "Provider env" in result.stdout
    assert "missing provider key is WARN" in result.stdout
    assert "0 fail" in result.stdout


def test_doctor_fails_on_missing_required_provider_key(tmp_path, monkeypatch) -> None:
    monkeypatch.delenv("CAMBIUM_PROVIDERS", raising=False)
    monkeypatch.delenv("CAMBIUM_PROVIDER_TEST_PROVIDER_API_KEY", raising=False)
    config = tmp_path / ".cambium" / "providers.json"
    config.parent.mkdir(parents=True)
    config.write_text(json.dumps({"providers": [_provider(required=True)]}), encoding="utf-8")

    result = _run_doctor(cwd=tmp_path)

    assert result.returncode == 1, result.stdout + result.stderr
    assert "required provider key missing" in result.stdout
    assert "1 fail" in result.stdout


def test_doctor_fails_on_invalid_provider_config(tmp_path, monkeypatch) -> None:
    monkeypatch.delenv("CAMBIUM_PROVIDERS", raising=False)
    monkeypatch.delenv("CAMBIUM_PROVIDER_TEST_PROVIDER_API_KEY", raising=False)
    invalid = _provider()
    invalid["required"] = "yes"
    config = tmp_path / ".cambium" / "providers.json"
    config.parent.mkdir(parents=True)
    config.write_text(json.dumps({"providers": [invalid]}), encoding="utf-8")

    result = _run_doctor(cwd=tmp_path)

    assert result.returncode == 1, result.stdout + result.stderr
    assert "provider config validation failed" in result.stdout
    assert "required" in result.stdout
    assert "1 fail" in result.stdout


def test_doctor_exits_zero_on_healthy_session_artifacts(tmp_path, monkeypatch) -> None:
    monkeypatch.delenv("CAMBIUM_PROVIDERS", raising=False)
    session_dir = tmp_path / "session"
    conversations = session_dir / ".cambium" / "sessions" / "conversations.db"
    conversations.parent.mkdir(parents=True)
    with sqlite3.connect(conversations) as connection:
        connection.execute("CREATE TABLE conversations (id INTEGER PRIMARY KEY)")

    result = _run_doctor("--session-dir", str(session_dir), cwd=tmp_path)

    assert result.returncode == 0, result.stdout + result.stderr
    assert "conversations.db: integrity ok" in result.stdout
    assert "0 fail" in result.stdout


def test_ruff_check_clean_on_src() -> None:
    result = subprocess.run(
        ["uv", "run", "--python", "3.14.7", "--with", "ruff", "ruff", "check", "src"],
        cwd=REPO_ROOT, capture_output=True, text=True, timeout=600,
    )
    assert result.returncode == 0, result.stdout + result.stderr
