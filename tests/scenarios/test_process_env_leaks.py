from __future__ import annotations

import os
from pathlib import Path

from cambium.auth import oauth_env_suffix
from cambium.process_env import build_subprocess_env


def test_subprocess_environment_rejects_credential_shaped_inheritance(
    monkeypatch, tmp_path: Path
) -> None:
    suffixes = tuple(oauth_env_suffix(provider) for provider in ("codex.chatgpt", "oauth-provider"))
    adversarial_names = {
        "OPENAI_API_KEY",
        "ANTHROPIC_API_KEY",
        "AZURE_OPENAI_API_KEY",
        "COHERE_API_KEY",
        "GOOGLE_API_KEY",
        "HUGGINGFACE_TOKEN",
        "GITHUB_TOKEN",
        "GITLAB_TOKEN",
        "NPM_TOKEN",
        "PYPI_TOKEN",
        "SLACK_TOKEN",
        "STRIPE_SECRET_KEY",
        "DATABASE_PASSWORD",
        "DB_PASSWORD",
        "APP_SECRET",
        "JWT_SECRET",
        "SESSION_TOKEN",
        "ACCESS_TOKEN",
        "ID_TOKEN",
        "REFRESH_TOKEN",
        "CLIENT_SECRET",
        "AUTHORIZATION",
        "BEARER_TOKEN",
        "AWS_ACCESS_KEY_ID",
        "AWS_SECRET_ACCESS_KEY",
        "AWS_SESSION_TOKEN",
        "AWS_SECURITY_TOKEN",
        "GOOGLE_APPLICATION_CREDENTIALS",
        "GOOGLE_CLOUD_PROJECT",
        "GCP_ACCESS_TOKEN",
        "GCP_SERVICE_ACCOUNT_KEY",
        "CAMBIUM_PROVIDER_OPENAI_API_KEY",
        "CAMBIUM_PROVIDER_ANTHROPIC_API_KEY",
        *(f"CAMBIUM_OAUTH_REFRESH_{suffix}" for suffix in suffixes),
        *(f"CAMBIUM_OAUTH_ACCESS_{suffix}" for suffix in suffixes),
        *(f"CAMBIUM_OAUTH_ACCOUNT_{suffix}" for suffix in suffixes),
    }
    assert len(adversarial_names) >= 30

    adversarial_values = {
        name: f"credential-probe-{index}" for index, name in enumerate(sorted(adversarial_names))
    }
    for name, value in adversarial_values.items():
        monkeypatch.setenv(name, value)

    monkeypatch.setenv("PATH", "/host/path")
    monkeypatch.setenv("HOME", "/host/home")
    monkeypatch.setenv("TMPDIR", "/host/tmp")
    monkeypatch.setenv("EMPTY_ALLOWED", "")

    worktree = tmp_path / "worker"
    environment = build_subprocess_env(
        allowed_keys={"PATH", "HOME", "TMPDIR", "EMPTY_ALLOWED"},
        worktree=worktree,
    )

    assert adversarial_names.isdisjoint(environment)
    assert set(adversarial_values.values()).isdisjoint(environment.values())
    assert environment["PATH"].startswith(os.defpath)
    assert environment["PATH"] != "/host/path"
    assert environment["PYTHONPATH"] == str(Path(__file__).resolve().parents[2] / "src")
    assert environment["PYTHONUNBUFFERED"] == "1"
    assert environment["LANG"] == "C"
    assert environment["LC_ALL"] == "C"
    assert environment["GIT_TERMINAL_PROMPT"] == "0"
    assert environment["GIT_CONFIG_NOSYSTEM"] == "1"
    assert "HOME" not in environment
    assert environment["TMPDIR"] == "/host/tmp"
    assert environment["EMPTY_ALLOWED"] == ""
