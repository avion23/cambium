from __future__ import annotations

import pytest

from cambium import auth


def test_scrub_environment_removes_provider_and_sdk_credentials() -> None:
    oauth_suffixes = tuple(
        auth.oauth_env_suffix(provider) for provider in ("codex", "foo.bar-baz")
    )
    credential_names = [
        auth.derived_env_name("openai"),
        auth.derived_env_name("anthropic"),
        *(
            f"CAMBIUM_OAUTH_{kind}_{suffix}"
            for suffix in oauth_suffixes
            for kind in ("ACCESS", "ACCOUNT")
        ),
        "OPENAI_API_KEY",
        "OPENAI_API_TOKEN",
        "OPENAI_AUTH_TOKEN",
        "OPENAI_WEBHOOK_SECRET",
        "ANTHROPIC_API_KEY",
        "ANTHROPIC_API_TOKEN",
        "ANTHROPIC_AUTH_TOKEN",
        "ANTHROPIC_API_SECRET",
        "AWS_ACCESS_KEY_ID",
        "AWS_SECRET_ACCESS_KEY",
        "AWS_SESSION_TOKEN",
        "AWS_SECURITY_TOKEN",
        "AWS_ACCESS_TOKEN",
        "AWS_SECRET_KEY",
        "AWS_SHARED_CREDENTIALS_FILE",
        "GCP_API_KEY",
        "GCP_ACCESS_TOKEN",
        "GCP_AUTH_TOKEN",
        "GOOGLE_API_KEY",
        "GOOGLE_APPLICATION_CREDENTIALS",
        "GOOGLE_OAUTH_ACCESS_TOKEN",
        "GOOGLE_CLOUD_ACCESS_TOKEN",
        "AZURE_OPENAI_API_KEY",
        "AZURE_OPENAI_AD_TOKEN",
        "AZURE_ACCESS_TOKEN",
        "AZURE_CLIENT_SECRET",
        "AZURE_FEDERATED_TOKEN_FILE",
        "AZURE_AUTH_LOCATION",
        "DOCKER_AUTH_CONFIG",
    ]
    assert len(credential_names) >= 30
    source = {name: f"credential-{index}" for index, name in enumerate(credential_names)}
    source.update({"PATH": "/bin", "CAMBIUM_TASK_ID": "task"})

    environment = auth.scrub_environment(source)

    assert not set(credential_names).intersection(environment)
    assert environment["PATH"] == "/bin"
    assert environment["CAMBIUM_TASK_ID"] == "task"


@pytest.mark.parametrize(
    ("value", "accepted"),
    [
        ("x" * auth.MIN_API_KEY_BYTES, True),
        ("x" * (auth.MIN_API_KEY_BYTES - 1), False),
        ("", False),
        (" " * auth.MIN_API_KEY_BYTES, False),
        ("\u00e9" * 3, True),
        ("\u00e9" * 2, False),
    ],
)
def test_api_key_minimum_uses_utf8_bytes_and_rejects_blank(
    value: str, accepted: bool
) -> None:
    if accepted:
        credential = auth.ProviderCredential("test", value)
        assert credential.api_key == value
        return

    with pytest.raises(auth.AuthSchemaError):
        auth.ProviderCredential("test", value)
