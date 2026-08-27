"""Focused content-flag classification and cascade scenarios."""

from __future__ import annotations

import asyncio
import json
from dataclasses import replace
from typing import Any, cast

import pytest
from diffundo_helpers import FakeServer, _config, _ok_payload, _set_keys

from cambium.diffundo import (
    AllProvidersFailed,
    AuthMode,
    Diffundo,
    HealthState,
    Protocol,
    ProviderConfig,
    ProviderError,
    ProviderOutcome,
    ProviderTier,
    _attempt_budget,
    _codex_stream_error,
)


def _codex_provider() -> ProviderConfig:
    return ProviderConfig(
        name="p_codex",
        tier=ProviderTier.FAST,
        base_url="",
        api_key_env="",
        model="gpt-test",
        auth=AuthMode.CODEX_CHATGPT,
        protocol=Protocol.CODEX_RESPONSES,
    )


@pytest.mark.parametrize(
    ("reasoning_effort", "expected"),
    [(None, 180.0), ("high", 180.0), ("max", 360.0)],
)
def test_attempt_budget_scales_max_reasoning_effort(
    reasoning_effort: str | None, expected: float
) -> None:
    provider = replace(_codex_provider(), reasoning_effort=reasoning_effort)
    assert _attempt_budget(180.0, provider) == expected


@pytest.mark.parametrize(
    ("error", "expected"),
    [
        (
            {
                "type": "invalid_request_error",
                "code": "invalid_prompt",
                "message": "Prompt was blocked by the usage policy",
            },
            ProviderOutcome.CONTENT_FLAGGED,
        ),
        (
            {"code": "invalid_prompt", "message": "disallowed content"},
            ProviderOutcome.CONTENT_FLAGGED,
        ),
        (
            {
                "type": "unknown_request_error",
                "code": "invalid_prompt",
                "message": "Prompt was blocked by the usage policy",
            },
            ProviderOutcome.ERROR,
        ),
        (
            {"type": "invalid_request_error", "code": "invalid_prompt", "message": "bad request"},
            ProviderOutcome.ERROR,
        ),
        (
            {
                "type": "invalid_request_error",
                "code": "unsupported_parameter",
                "message": "Unsupported parameter: stream",
            },
            ProviderOutcome.CONFIG_ERROR,
        ),
        (
            {"type": "server_error", "code": "server_is_overloaded", "message": "busy"},
            ProviderOutcome.ERROR,
        ),
        (
            {"type": "content_policy_error", "code": "content_policy", "message": "blocked"},
            ProviderOutcome.REFUSAL,
        ),
        (
            {"message": "invalid prompt: blocked by the usage policy"},
            ProviderOutcome.ERROR,
        ),
    ],
)
def test_codex_stream_error_classification_table(
    error: dict[str, Any], expected: ProviderOutcome
) -> None:
    classified = _codex_stream_error(_codex_provider(), error, "access-token")
    assert classified.outcome is expected
    assert "codex stream error:" in classified.message


def test_plural_policy_wording_matches_http_and_sse() -> None:
    body = {
        "type": "invalid_request_error",
        "code": "invalid_prompt",
        "message": "Prompt was blocked by the usage policies",
    }
    assert _codex_stream_error(_codex_provider(), body, "access-token").outcome is (
        ProviderOutcome.CONTENT_FLAGGED
    )
    assert (
        Diffundo(())._classify_http(_codex_provider(), 400, json.dumps(body)).outcome
        is ProviderOutcome.CONTENT_FLAGGED
    )


def test_anthropic_nested_error_envelope_matches_http_and_sse() -> None:
    body = {
        "type": "error",
        "error": {
            "type": "invalid_request_error",
            "message": "Output blocked by content filtering policy",
        },
    }
    assert _codex_stream_error(_codex_provider(), body["error"], "access-token").outcome is (
        ProviderOutcome.CONTENT_FLAGGED
    )
    assert (
        Diffundo(())._classify_http(_codex_provider(), 400, json.dumps(body)).outcome
        is ProviderOutcome.CONTENT_FLAGGED
    )


def test_config_text_beats_content_filter_error_flag() -> None:
    body = {
        "error": {
            "type": "content_filter_error",
            "param": "stream",
            "message": "Unsupported parameter: stream",
        }
    }
    assert _codex_stream_error(_codex_provider(), body["error"], "access-token").outcome is (
        ProviderOutcome.CONFIG_ERROR
    )
    assert (
        Diffundo(())._classify_http(_codex_provider(), 400, json.dumps(body)).outcome
        is ProviderOutcome.CONFIG_ERROR
    )


def test_policy_flag_beats_http_rate_and_auth_statuses() -> None:
    body = json.dumps(
        {
            "type": "invalid_request_error",
            "code": "invalid_prompt",
            "message": "Prompt was blocked by the safety system",
        }
    )
    router = Diffundo(())
    for status in (401, 429):
        assert router._classify_http(_codex_provider(), status, body).outcome is (
            ProviderOutcome.CONTENT_FLAGGED
        )


@pytest.mark.parametrize("code", ["content_policy_violation", "content_filter_violation"])
def test_policy_violation_codes_match_http_and_sse(code: str) -> None:
    body = {"code": code, "message": "blocked"}
    assert _codex_stream_error(_codex_provider(), body, "access-token").outcome is (
        ProviderOutcome.CONTENT_FLAGGED
    )
    assert (
        Diffundo(())._classify_http(_codex_provider(), 400, json.dumps(body)).outcome
        is ProviderOutcome.CONTENT_FLAGGED
    )


def test_string_error_policy_marker_keeps_legacy_sse_refusal() -> None:
    body = {"error": "content_policy"}
    assert _codex_stream_error(_codex_provider(), body, "access-token").outcome is (
        ProviderOutcome.REFUSAL
    )


@pytest.mark.parametrize(
    ("body", "expected"),
    [
        (
            json.dumps(
                {
                    "error": {
                        "type": "invalid_request_error",
                        "code": "invalid_prompt",
                        "message": "Prompt was flagged by the usage policy",
                    }
                }
            ),
            ProviderOutcome.CONTENT_FLAGGED,
        ),
        (
            json.dumps(
                {
                    "error": {
                        "type": "invalid_request_error",
                        "code": "invalid_prompt",
                        "message": "request shape is invalid",
                    }
                }
            ),
            ProviderOutcome.REFUSAL,
        ),
        ("Invalid prompt: prompt was flagged by the usage policy", ProviderOutcome.REFUSAL),
        (
            json.dumps(
                {
                    "error": {
                        "type": "invalid_request_error",
                        "code": "unsupported_parameter",
                        "message": "Unsupported parameter: stream",
                    }
                }
            ),
            ProviderOutcome.CONFIG_ERROR,
        ),
    ],
)
def test_http_error_classification_prefers_structured_fields(
    body: str, expected: ProviderOutcome
) -> None:
    router = Diffundo(())
    error = router._classify_http(_codex_provider(), 400, body)
    assert error.outcome is expected


def test_content_flagged_error_cascades_without_health_damage(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    flagged = FakeServer(
        [
            (
                400,
                {
                    "error": {
                        "type": "invalid_request_error",
                        "code": "invalid_prompt",
                        "message": "Prompt was flagged by the usage policy",
                    }
                },
                0.0,
            )
        ]
    )
    good = FakeServer([(200, _ok_payload("fallback"), 0.0)])
    _set_keys(monkeypatch, "K_FLAGGED", "K_GOOD")
    router = Diffundo(
        (
            _config("p_flagged", flagged, "K_FLAGGED", max_retries=2),
            _config("p_good", good, "K_GOOD"),
        )
    )
    try:
        prompt = {"messages": [{"role": "user", "content": "x"}]}
        result = asyncio.run(router.call(ProviderTier.FAST, prompt))
        assert result.provider == "p_good"
        assert router.health("p_flagged") is HealthState.UNKNOWN
        assert len(flagged.calls) == 1
        assert len(good.calls) == 1
    finally:
        flagged.close()
        good.close()


def test_all_content_flagged_providers_preserve_terminal_outcome(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    body = {
        "error": {
            "type": "invalid_request_error",
            "code": "invalid_prompt",
            "message": "Prompt was flagged by the usage policy",
        }
    }
    first = FakeServer([(400, body, 0.0)])
    second = FakeServer([(400, body, 0.0)])
    _set_keys(monkeypatch, "K_FLAGGED_A", "K_FLAGGED_B")
    router = Diffundo(
        (
            _config("p_flagged_a", first, "K_FLAGGED_A"),
            _config("p_flagged_b", second, "K_FLAGGED_B"),
        )
    )
    try:
        with pytest.raises(AllProvidersFailed) as raised:
            prompt = {"messages": [{"role": "user", "content": "x"}]}
            asyncio.run(router.call(ProviderTier.FAST, prompt))
        error = cast(ProviderError, raised.value.last_error)
        assert error.provider == "p_flagged_b"
        assert error.outcome is ProviderOutcome.CONTENT_FLAGGED
        assert router.health("p_flagged_a") is HealthState.UNKNOWN
        assert router.health("p_flagged_b") is HealthState.UNKNOWN
    finally:
        first.close()
        second.close()
