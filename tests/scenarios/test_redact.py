"""Adversarial scenario coverage for the standalone redaction boundary."""

from __future__ import annotations

import re
from collections import UserDict

import pytest

from cambium.redact import (
    DEFAULT_PATTERNS,
    NON_SECRET_BASICS,
    REDACT_KEYS,
    REDACT_VALUES,
    Redactor,
    build_worker_env,
    is_secret_name,
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

    assert output["headers"]["Authorization"] == "***"
    assert output["headers"]["X-Api-Key"] == "***"
    assert output["headers"]["Content-Type"] == "application/json"
    assert output["cookies"]["sessionid"] == "***"
    assert output["cookies"]["theme"] == "***"
    assert output["nested"][0]["refresh_token"] == "***"
    assert output["nested"][1] == ("plain", "not-a-secret")


def test_mapping_sequences_and_key_redaction_do_not_mutate_input() -> None:
    payload = UserDict(
        {
            "api_key": {"deep": "do-not-inspect"},
            "items": [{"token": [SK, 3]}, ("alice@example.com", "ok")],
            "plain": {"path": "/tmp", "count": 3},
        }
    )
    original_items = payload["items"]

    output = R.redact_mapping(payload)

    assert output["api_key"] == "***"
    assert output["items"][0]["token"] == "***"
    assert output["items"][1] == ("***", "ok")
    assert output["plain"] == {"path": "/tmp", "count": 3}
    assert payload["api_key"] == {"deep": "do-not-inspect"}
    assert payload["items"] is original_items


def test_cycles_are_finite_and_preserved_for_mutable_containers() -> None:
    payload: dict[str, object] = {"token": "short"}
    items: list[object] = [payload]
    payload["self"] = payload
    payload["items"] = items
    items.append(items)

    output = R.redact_mapping(payload)

    assert output["token"] == "***"
    assert output["self"] is output
    assert output["items"][0] is output
    assert output["items"][1] is output["items"]
    assert payload["token"] == "short"


def test_tuple_list_cycle_does_not_retain_a_placeholder() -> None:
    items: list[object] = []
    source = (items,)
    items.append(source)

    output = R.redact_mapping(source)

    assert isinstance(output, tuple)
    assert output[0][0] is output


def test_unknown_objects_are_not_stringified_or_reprd() -> None:
    class Explosive:
        def __repr__(self) -> str:
            raise AssertionError("repr must not be called")

        def __str__(self) -> str:
            raise AssertionError("str must not be called")

    value = Explosive()
    payload = {"safe": [value], "api_key": value}

    output = R.redact_mapping(payload)

    assert output["safe"][0] is value
    assert output["api_key"] == "***"


def test_default_patterns_and_explicit_default_copy_are_equivalent() -> None:
    text = (
        f"{SK} api_key=short! Authorization: x! Cookie: sid=s!; "
        f"alice@example.com commit {'c' * 40}"
    )
    default = Redactor()
    explicit = Redactor(list(DEFAULT_PATTERNS))

    assert default.redact(text) == explicit.redact(text)
    assert default.redact_mapping({"token": [text]}) == explicit.redact_mapping({"token": [text]})


def test_custom_patterns_are_authoritative_and_replacement_is_literal() -> None:
    custom = Redactor([re.compile(r"foo\d+")], replacement=r"\1")

    assert custom.redact("foo123 sk-" + "A" * 30) == r"\1 sk-" + "A" * 30
    assert custom.redact_mapping({"token": "short", "message": "foo42"}) == {
        "token": r"\1",
        "message": r"\1",
    }


def test_default_patterns_are_compiled_and_public_value_expression_is_compiled() -> None:
    assert isinstance(DEFAULT_PATTERNS, tuple)
    assert all(isinstance(pattern, re.Pattern) for pattern in DEFAULT_PATTERNS)
    assert isinstance(REDACT_KEYS, re.Pattern)
    assert isinstance(REDACT_VALUES, re.Pattern)


def test_secret_name_classifier_has_boundaries_and_metadata_exceptions() -> None:
    for name in (
        "API_KEY",
        "X-Api-Key",
        "OPENAI_API_KEY",
        "refreshToken",
        "Authorization",
        "Cookie",
        "sessionid",
        "private_key",
        "signing_secret",
    ):
        assert is_secret_name(name), name
    for name in (
        "PATH",
        "DATABASE_URL",
        "token_count",
        "token_metrics",
        "prompt_tokens",
        "signature",
        "commit_signature",
        "author",
        "author_name",
        "api_key_env",
        "api_key_length",
    ):
        assert not is_secret_name(name), name
    assert not REDACT_KEYS.search("author=Alice")


def test_strict_worker_env_requires_explicit_nonbasic_names() -> None:
    base = {
        "OPENAI_API_KEY": "provider-secret",
        "AWS_SECRET_ACCESS_KEY": "other-secret",
        "DATABASE_URL": "postgres://user:pass@db",
        "PATH": "/usr/bin",
        "HOME": "/home/worker",
        "PYTHONUNBUFFERED": "1",
        "CAMBIUM_TASK_ID": "task-1",
        "SHLVL": "9",
    }

    safe_default = build_worker_env(base)
    explicit = build_worker_env(base, allowlist={"OPENAI_API_KEY", "DATABASE_URL"})

    assert set(safe_default) == {
        "PATH",
        "HOME",
        "PYTHONUNBUFFERED",
        "CAMBIUM_TASK_ID",
    }
    assert set(explicit) == set(safe_default) | {"OPENAI_API_KEY", "DATABASE_URL"}
    assert safe_default["PATH"] == "/usr/bin"
    assert explicit["OPENAI_API_KEY"] == "provider-secret"
    assert "AWS_SECRET_ACCESS_KEY" not in explicit
    assert "SHLVL" not in explicit


def test_worker_env_does_not_mutate_base_and_rejects_string_allowlist() -> None:
    base = {"PATH": "/bin", "UNLISTED": "value"}
    snapshot = dict(base)

    assert build_worker_env(base, allowlist=set()) == {"PATH": "/bin"}
    assert base == snapshot
    with pytest.raises(TypeError):
        build_worker_env(base, allowlist="PATH")


def test_large_mixed_text_is_deterministic() -> None:
    chunk = (
        f"ordinary token_count=4 {SK} api_key=short! "
        f"{('d' * 40)} author=Ada alice@example.com\n"
    )
    text = chunk * 5000

    first = R.redact(text)
    second = R.redact(text)

    assert first == second
    assert SK not in first
    assert "api_key=***" in first
    assert "token_count=4" in first
    assert "author=Ada" in first
    assert "d" * 40 in first


def test_non_secret_basics_are_secret_free_names() -> None:
    assert {"PATH", "HOME", "PYTHONUNBUFFERED"} <= NON_SECRET_BASICS
    assert all(not is_secret_name(name) for name in NON_SECRET_BASICS)
