"""Adversarial scenario coverage for the standalone redaction boundary."""

from __future__ import annotations

import hashlib
import os
import re
import threading
from typing import cast

import pytest

from cambium.redact import (
    Redactor,
    build_session_redactor,
    build_worker_env,
    sanitize_oauth_document,
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

    output = cast(
        dict[object, object], R.redact_mapping({key: "safe" for key in secret_keys})
    )

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


def test_generic_mapping_redacts_structure_and_protocol_allowlist_is_explicit() -> None:
    redactor = Redactor(secret_values={"merge", "result", "succeeded"})
    generic = redactor.redact_mapping(
        {
            "status": "merge",
            "kind": "result",
            "type": "succeeded",
            "nested": {"status": "merge", "kind": "result", "message": "succeeded"},
        }
    )
    assert generic == {
        "status": "***",
        "kind": "***",
        "type": "***",
        "nested": {"status": "***", "kind": "***", "message": "***"},
    }

    protocol = redactor.redact_protocol_record(
        {
            "kind": "merge",
            "type": "result",
            "status": "succeeded",
            "task_id": "task-merge",
            "request_id": "request-result",
            "summary": "merge result succeeded",
            "reason": "result merge succeeded",
            "payload": {
                "status": "merge",
                "kind": "result",
                "message": "succeeded",
                "api_key": "merge",
            },
            "api_key": "merge",
            "password": "result",
        },
        structural_fields={"kind", "type", "status", "task_id", "request_id", "payload"},
    )
    assert protocol["kind"] == "merge"
    assert protocol["type"] == "result"
    assert protocol["status"] == "succeeded"
    assert protocol["task_id"] == "task-merge"
    assert protocol["request_id"] == "request-result"
    assert protocol["summary"] == "*** *** ***"
    assert protocol["reason"] == "*** *** ***"
    assert protocol["payload"] == {
        "status": "***",
        "kind": "***",
        "message": "***",
        "***": "***",
    }
    assert "api_key" not in protocol
    assert "password" not in protocol
    assert protocol["***"] == "***"

    contextual = redactor.redact_protocol_record(
        {"status": "api_key=merge"}, structural_fields={"status"}
    )
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
    output = cast(dict[object, object], R.redact_mapping(payload))
    token_metrics = cast(dict[object, object], output["token_metrics"])
    assert output["token_count"] == 17
    assert token_metrics == {"prompt_tokens": 4, "completion_tokens": 7}
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

    output = cast(dict[object, object], R.redact_mapping(payload))
    headers = cast(dict[object, object], output["headers"])
    cookies = cast(dict[object, object], output["cookies"])
    nested = cast(list[object], output["nested"])

    assert headers["***"] == "***"
    assert headers["Content-Type"] == "application/json"
    assert cookies["***"] == "***"
    assert cookies["theme"] == "***"
    assert cast(dict[object, object], nested[0])["***"] == "***"
    assert nested[1] == ("plain", "not-a-secret")


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

def test_register_secret_redacts_values_registered_after_construction() -> None:
    old = "opaque-old-value"
    rotated = "opaque-rotated-" + "R" * 24
    redactor = Redactor(secret_values={old})

    assert rotated in redactor.redact(f"provider failed with {rotated}")

    redactor.register_secret(rotated)
    redactor.register_secret(rotated)  # idempotent

    output = redactor.redact(f"provider failed with {rotated}")
    assert rotated not in output
    assert output == "provider failed with ***"
    assert redactor.secret_values == frozenset({old, rotated})


def test_register_secret_is_thread_safe_and_rejects_bad_input() -> None:
    redactor = Redactor()
    barrier = threading.Barrier(2)

    def register(prefix: str) -> None:
        barrier.wait()
        for index in range(20):
            redactor.register_secret(f"{prefix}-{index}-" + "T" * 16)

    threads = [
        threading.Thread(target=register, args=(name,)) for name in ("t0", "t1")
    ]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()

    text = " ".join(
        f"t{name}-{index}-" + "T" * 16
        for name in (0, 1)
        for index in range(20)
    )
    output = redactor.redact(text)
    assert all(
        f"t{name}-{index}-" + "T" * 16 not in output
        for name in (0, 1)
        for index in range(20)
    )
    assert len(redactor.secret_values) == 40
    with pytest.raises(TypeError):
        redactor.register_secret(123)  # type: ignore[arg-type]
    with pytest.raises(ValueError):
        redactor.register_secret("")


def _hex_escape(text: str) -> str:
    return "".join(f"\\x{ord(character):02x}" for character in text)


def _unicode_escape(text: str) -> str:
    return "".join(f"\\u{ord(character):04x}" for character in text)


def test_escaped_provider_shapes_are_redacted() -> None:
    token = "sk-proj-" + "S" * 40
    wire = "".join(f"\\u{ord(character):04x}" for character in token)

    output = R.redact_escaped(f"provider={wire}")

    assert token not in output
    assert wire not in output
    assert output == "provider=***"


def test_old_and_rotated_opaque_tokens_redacted_in_all_wire_forms() -> None:
    old = "opaque-old-token-" + "A" * 20
    rotated = "opaque-rotated-token-" + "B" * 20
    redactor = Redactor(secret_values={old})
    redactor.register_secret(rotated)

    plain = f"stderr: {old} then {rotated}"
    bearer = f"Authorization: Bearer {rotated}"
    json_text = f'{{"data": "{rotated}", "note": "{old}"}}'
    exception_text = f"HTTPError 401 invalid_grant for {old}: {rotated}"
    hex_wire = '"value": "' + _hex_escape(old) + '"'
    unicode_wire = '{"payload": "' + _unicode_escape(rotated) + '"}'

    assert old not in redactor.redact(plain)
    assert rotated not in redactor.redact(plain)
    assert rotated not in redactor.redact(bearer)
    assert rotated not in redactor.redact(json_text)
    assert old not in redactor.redact(exception_text)
    assert old not in redactor.redact_escaped(hex_wire)
    assert rotated not in redactor.redact_escaped(unicode_wire)


def test_sanitize_oauth_document_redacts_tokens_and_fingerprints_account() -> None:
    rotated = "opaque-rotated-" + "C" * 20
    redactor = Redactor(secret_values={rotated})
    doc = {
        "access_token": "raw-access-token",
        "refresh_token": rotated,
        "id_token": "raw-id-token",
        "authorization_code": "raw-auth-code",
        "code_verifier": "raw-code-verifier",
        "device_auth_id": "raw-device-auth",
        "user_code": "raw-user-code",
        "account_id": "acc-12345",
        "token_type": "Bearer",
        "expires_in": 3600,
        "scope": "openid profile",
        "error": f"invalid_grant: {rotated}",
        "nested": {
            "access_token": "raw-nested-token",
            "refresh_token": "raw-nested-refresh",
        },
    }

    output = sanitize_oauth_document(doc, redactor=redactor)
    nested = cast(dict[object, object], output["nested"])
    account_id = cast(str, output["account_id"])
    error = cast(str, output["error"])

    serialized = repr(output)
    for raw in (
        "raw-access-token",
        rotated,
        "raw-id-token",
        "raw-auth-code",
        "raw-code-verifier",
        "raw-device-auth",
        "raw-user-code",
        "raw-nested-token",
        "raw-nested-refresh",
    ):
        assert raw not in serialized

    for name in (
        "access_token",
        "refresh_token",
        "id_token",
        "authorization_code",
        "code_verifier",
        "device_auth_id",
        "user_code",
    ):
        assert output[name] == "<redacted>"
    assert nested["access_token"] == "<redacted>"
    assert nested["refresh_token"] == "<redacted>"
    assert output["account_id"] == hashlib.sha256(b"acc-12345").hexdigest()[:8]
    assert len(account_id) == 8
    assert output["token_type"] == "Bearer"
    assert output["expires_in"] == 3600
    assert output["scope"] == "openid profile"
    assert "opaque-rotated" not in error
    assert doc["access_token"] == "raw-access-token"  # input untouched

    default = sanitize_oauth_document(doc)
    assert default["access_token"] == "<redacted>"
    assert default["account_id"] == output["account_id"]
    assert "raw-access-token" not in repr(default)
    with pytest.raises(TypeError):
        sanitize_oauth_document(["not", "a", "mapping"])  # type: ignore[arg-type]


def test_oauth_structured_field_names_redact_values_in_json_text() -> None:
    text = (
        '{"access_token": "at1", "refresh_token": "rt1", "id_token": "it1", '
        '"authorization_code": "ac1", "code_verifier": "cv1", '
        '"device_auth_id": "da1", "user_code": "uc1"}'
    )

    output = R.redact(text)

    for value in ("at1", "rt1", "it1", "ac1", "cv1", "da1", "uc1"):
        assert value not in output
    assert '"access_token": "***"' in output
    assert '"code_verifier": "***"' in output
    assert '"user_code": "***"' in output

    mapping = R.redact_mapping(
        {
            "authorization_code": "ac2",
            "code_verifier": "cv2",
            "user_code": "uc2",
            "token_type": "Bearer",
        }
    )
    assert mapping == {
        "***": "***",
        "token_type": "Bearer",
    }


def test_oauth_names_do_not_redact_benign_token_and_code_words() -> None:
    text = (
        "a token of appreciation; token_count=3; code_verifier_length=43; "
        "status_code=200; error_code=404; country_code=US; "
        "verifier=local-check; user code review; the secret garden; Bearer bonds"
    )

    assert R.redact(text) == text
