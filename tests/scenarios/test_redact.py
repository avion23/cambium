"""Adversarial scenario coverage for the standalone redaction boundary."""

from __future__ import annotations

import os
import re

import pytest

from cambium.redact import (
    Redactor,
    build_session_redactor,
    build_worker_env,
)

R = Redactor()

SK = "sk-proj-" + "A" * 40
NVAPI = "nvapi-" + "B" * 32
JWT = "eyJhbGciOiJIUzI1NiJ9.eyJzdWIiOiIxIn0.signature"
AWS_KEY = "AKIA" + "1A2B3C4D5E6F7G8H"
PRIVATE_KEY = (
    "-----BEGIN RSA PRIVATE KEY-----\n"
    "MIIEowIBAAKCAQEA4aXNuVdGllQAbN2oZ\n"
    "-----END RSA PRIVATE KEY-----"
)


def test_provider_values_jwt_private_key_and_email_are_scrubbed() -> None:
    text = " ".join(
        (
            SK,
            NVAPI,
            "AIza" + "G" * 35,
            "ghp_" + "H" * 36,
            AWS_KEY,
            "Bearer " + "T" * 24,
            "Basic " + "dXNlcjpwYXNz" * 2,
            JWT,
            PRIVATE_KEY,
            "alice@example.com",
        )
    )

    output = R.redact(text)

    for secret in (
        SK,
        NVAPI,
        "AIza" + "G" * 35,
        "ghp_" + "H" * 36,
        AWS_KEY,
        JWT,
        PRIVATE_KEY,
        "alice@example.com",
    ):
        assert secret not in output
    assert "Bearer " not in output
    assert "Basic " not in output


def test_mapping_keys_are_redacted_for_all_secret_shapes() -> None:
    url_with_credentials = "https://user:pw@host.example"
    email = "alice@example.com"
    secret_keys = (
        SK,
        JWT,
        PRIVATE_KEY,
        AWS_KEY,
        url_with_credentials,
        email,
        "OPENAI_API_KEY",
    )

    output = R.redact_mapping({key: "safe" for key in secret_keys})

    serialized = repr(output)
    for secret in secret_keys:
        assert secret not in serialized
    assert all(secret not in output for secret in secret_keys)
    assert "***" in output

    named = R.redact_mapping({"api_key": "short"})
    assert named == {"***": "***"}


def test_short_and_punctuation_values_are_scrubbed_only_in_context() -> None:
    text = (
        "api_key=a!b:c; password: p@ss!; client-secret='q?u#x'; "
        '"X-Api-Key": "z/9!"; Authorization: Bearer s!; '
        "ordinary=a!b:c"
    )

    output = R.redact(text)

    for secret in ("p@ss!", "q?u#x", "z/9!", "Bearer s!"):
        assert secret not in output
    assert "ordinary=a!b:c" in output


def test_registered_values_are_exact_without_enabling_generic_context_redaction() -> None:
    secret = "opaque-short-value"
    redactor = Redactor(secret_values={secret})

    output = redactor.redact(f"stderr={secret} ordinary=value api_key=short!")

    assert "opaque-short-value" not in output
    assert "stderr=***" in output
    assert "ordinary=value" in output
    assert "api_key=***" in output


def test_registered_values_are_redacted_in_nested_tool_payloads() -> None:
    secret = "opaque-short-value"
    redactor = Redactor(secret_values={secret})
    payload = {
        "stderr": f"provider failed with {secret}",
        "tool_output": {
            "stdout": secret,
            "nested": [f"prefix:{secret}", {f"key-{secret}": f"suffix:{secret}"}],
        },
        "ordinary": "value",
    }

    output = redactor.redact_mapping(payload)

    assert output == {
        "stderr": "provider failed with ***",
        "tool_output": {
            "stdout": "***",
            "nested": ["prefix:***", {"key-***": "suffix:***"}],
        },
        "ordinary": "value",
    }


def test_registered_values_skip_protocol_structure_but_redact_free_text() -> None:
    redactor = Redactor(secret_values={"merge", "result", "succeeded"})
    payload = {
        "kind": "merge",
        "type": "result",
        "status": "succeeded",
        "task_id": "task-merge",
        "worker_id": "worker-result",
        "request_id": "request-succeeded",
        "parent_task_id": "parent-merge",
        "child_task_id": "child-result",
        "generation": "generation-merge",
        "schema_version": "schema-result",
        "session_status": "succeeded",
        "timeout_phase": "merge",
        "parent": ["merge", "result"],
        "summary": "merge result succeeded",
        "reason": "result merge succeeded",
        "nested": {"output": "merge result succeeded"},
        "api_key": "merge",
        "password": "result",
    }

    output = redactor.redact_mapping(payload)

    for field in (
        "kind",
        "type",
        "status",
        "task_id",
        "worker_id",
        "request_id",
        "parent_task_id",
        "child_task_id",
        "generation",
        "schema_version",
        "session_status",
        "timeout_phase",
        "parent",
    ):
        assert output[field] == payload[field]
    assert output["summary"] == "*** *** ***"
    assert output["reason"] == "*** *** ***"
    assert output["nested"] == {"output": "*** *** ***"}
    assert "api_key" not in output
    assert "password" not in output
    assert output["***"] == "***"

    contextual = redactor.redact_mapping({"status": "api_key=merge"})
    assert contextual["status"] == "api_key=***"


def test_one_character_secret_does_not_corrupt_unicode_escape_syntax() -> None:
    """A short registered value must not corrupt ``\\uXXXX`` escape syntax.

    A one-character registered secret (e.g. a numeric session id leaked from
    the parent environment) previously matched the digits *inside* a
    ``\\uXXXX`` escape, mangling the escape so a longer escaped credential was
    no longer decoded and leaked its ``\\u005c`` wire form.
    """
    short = "2"
    sk = 'sk-proj-A"B\\C'
    redactor = Redactor(secret_values={short, sk})

    output = redactor.redact_escaped('the wire shows "sk-proj-A\\u0022B\\u005cC" here')

    assert output == 'the wire shows "***" here'
    assert "\\u0022" not in output
    assert "\\u005c" not in output
    assert sk not in output


def test_one_character_secret_preserves_benign_escape() -> None:
    """A short registered value never matches inside an unrelated escape."""

    redactor = Redactor(secret_values={"2"})

    assert redactor.redact_escaped('code="\\u0022"') == 'code="\\u0022"'


def test_semicolon_can_be_secret_punctuation_without_swallowing_next_field() -> None:
    text = "api_key=part;with;punct next=value; password=p@ss; author=Ada"

    output = R.redact(text)

    assert "part;with;punct" not in output
    assert "password=***" in output
    assert "author=Ada" in output
    assert "next=value" in output


def test_headers_and_cookies_redact_short_values_but_keep_cookie_attributes() -> None:
    text = (
        "Authorization: s!; Content-Type: application/json\n"
        "Cookie: session=s!@#; theme=dark\n"
        "Set-Cookie: sid=q?; Path=/; HttpOnly"
    )

    output = R.redact(text)

    assert "Authorization: ***" in output
    assert "s!@#" not in output
    assert "q?" not in output
    assert "theme=***" in output
    assert "Path=/" in output
    assert "HttpOnly" in output


def test_benign_metrics_signatures_and_author_fields_survive() -> None:
    text = (
        "token_count: 17 token_usage=42 signature=deadbeef "
        "author=Ada author_email=alice@example.com"
    )
    payload = {
        "token_count": 17,
        "token_metrics": {"prompt_tokens": 4, "completion_tokens": 7},
        "signature": "deadbeef",
        "author": "Ada",
        "author_email": "alice@example.com",
    }

    assert R.redact(text) == (
        "token_count: 17 token_usage=42 signature=deadbeef author=Ada author_email=***"
    )
    output = R.redact_mapping(payload)
    assert output["token_count"] == 17
    assert output["token_metrics"] == {"prompt_tokens": 4, "completion_tokens": 7}
    assert output["signature"] == "deadbeef"
    assert output["author"] == "Ada"
    assert output["author_email"] == "***"


def test_bare_hashes_and_ordinary_text_are_not_corrupted() -> None:
    sha1 = "a" * 40
    sha256 = "b" * 64
    text = (
        f"commit {sha1} merge {sha256}; token of appreciation; "
        "the secret garden; Bearer bonds; Basic algebra"
    )

    assert R.redact(text) == text


def test_contextual_short_values_do_not_require_secret_punctuation() -> None:
    payload = {
        "headers": {
            "Authorization": "x",
            "X-Api-Key": "!",
            "Content-Type": "application/json",
        },
        "cookies": {"sessionid": "s", "theme": "dark"},
        "nested": [{"refresh_token": "r?"}, ("plain", "not-a-secret")],
    }

    output = R.redact_mapping(payload)

    assert output["headers"]["***"] == "***"
    assert output["headers"]["Content-Type"] == "application/json"
    assert output["cookies"]["***"] == "***"
    assert output["cookies"]["theme"] == "***"
    assert output["nested"][0]["***"] == "***"
    assert output["nested"][1] == ("plain", "not-a-secret")


def test_context_delimiters_do_not_leak_or_corrupt_prose() -> None:
    assert R.redact("api_key={s!}") == "api_key=***"

    text = "explain Authorization: bearer syntax"
    assert R.redact(text) == text


def test_nested_mixed_delimiters_are_consumed_as_one_secret_value() -> None:
    text = 'api_key={"outer":[1,{"inner":"value"}]} ordinary=value'

    assert R.redact(text) == "api_key=*** ordinary=value"


def test_custom_patterns_are_authoritative_and_replacement_is_literal() -> None:
    custom = Redactor([re.compile(r"foo\d+")], replacement=r"\1")

    assert custom.redact("foo123 sk-" + "A" * 30) == r"\1 sk-" + "A" * 30
    assert custom.redact_mapping({"token": "short", "message": "foo42"}) == {
        r"\1": r"\1",
        "message": r"\1",
    }


def test_strict_worker_env_requires_explicit_nonbasic_names(tmp_path) -> None:
    base = {
        "OPENAI_API_KEY": "provider-secret",
        "AWS_SECRET_ACCESS_KEY": "other-secret",
        "DATABASE_URL": "postgres://user:pass@db",
        "PATH": "/host/bin",
        "HOME": "/home/host",
        "PYTHONUNBUFFERED": "0",
        "CAMBIUM_TASK_ID": "host-task",
        "CAMBIUM_GENERATION": "host-generation",
        "CAMBIUM_SESSION_ID": "host-session",
        "SHLVL": "9",
    }
    worktree = tmp_path / "worker"
    overrides = {
        "CAMBIUM_TASK_ID": "task-1",
        "CAMBIUM_GENERATION": "1",
        "CAMBIUM_SESSION_ID": "session-1",
    }

    safe_default = build_worker_env(base, worktree=worktree, overrides=overrides)
    explicit = build_worker_env(
        base,
        allowlist={"OPENAI_API_KEY", "DATABASE_URL", "PATH", "HOME"},
        worktree=worktree,
        overrides=overrides,
    )

    assert set(safe_default) == {
        "PATH",
        "HOME",
        "PYTHONUNBUFFERED",
        "CAMBIUM_TASK_ID",
        "CAMBIUM_GENERATION",
        "CAMBIUM_SESSION_ID",
    }
    assert set(explicit) == set(safe_default) | {"OPENAI_API_KEY", "DATABASE_URL"}
    assert safe_default["PATH"] == os.defpath
    assert safe_default["HOME"] == str(worktree.resolve() / ".cambium" / "home")
    assert safe_default["PYTHONUNBUFFERED"] == "1"
    assert safe_default["CAMBIUM_TASK_ID"] == "task-1"
    assert explicit["OPENAI_API_KEY"] == "provider-secret"
    assert "AWS_SECRET_ACCESS_KEY" not in explicit
    assert "SHLVL" not in explicit
    assert "host-task" not in explicit.values()
    assert "host-generation" not in explicit.values()
    assert "host-session" not in explicit.values()
    assert "/home/host" not in explicit.values()
    assert "/host/bin" not in explicit.values()


def test_worker_env_does_not_mutate_base_and_rejects_string_allowlist() -> None:
    base = {"PATH": "/host/bin", "HOME": "/home/host", "UNLISTED": "value"}
    snapshot = dict(base)
    worktree = "/tmp/cambium-test-worktree"

    assert build_worker_env(base, allowlist=set(), worktree=worktree) == {
        "PATH": os.defpath,
        "PYTHONUNBUFFERED": "1",
        "HOME": "/tmp/cambium-test-worktree/.cambium/home",
    }
    assert base == snapshot
    with pytest.raises(TypeError):
        build_worker_env(base, allowlist="PATH")


def test_build_session_redactor_defaults_to_provider_patterns() -> None:
    redactor = build_session_redactor()

    assert redactor.secret_values == frozenset()
    output = redactor.redact(f"sk-proj-{'A' * 40} ordinary=value")

    assert SK not in output
    assert "ordinary=value" in output
