from __future__ import annotations

import json

import pytest

from cambium.diffundo import (
    PromptStructureError,
    ProviderConfig,
    ProviderTier,
    _codex_request_body,
    _parse_codex_sse,
    prompt_prefix_bytes,
    validate_prompt_structure,
)


def _prompt(*messages: tuple[str, str]) -> dict[str, object]:
    return {"messages": [{"role": role, "content": content} for role, content in messages]}


def _volatile_messages() -> list[tuple[str, str]]:
    return [
        ("system", "future 2099-12-31T23:59:59Z"),
        ("developer", "request_id=first"),
        ("assistant", "trace-id=second"),
        ("tool", "550e8400-e29b-51d4-a716-446655440000"),
    ]


def test_prompt_prefix_bytes_counts_unicode_header() -> None:
    payload = "CJK " + chr(0x4E2D) + " emoji " + chr(0x1F600)
    content = "stable " + payload + " header"
    prompt = _prompt(("system", content), ("user", "task"))
    prefix_len = len(content.encode("utf-8"))

    assert prompt_prefix_bytes(prompt) == prefix_len


def test_codex_reconstruction_preserves_unicode_and_mixed_line_endings() -> None:
    content = "first\r\n" + chr(0x4E2D) + "\n" + chr(0x1F600) + "\r" + "last"
    config = ProviderConfig(
        name="p",
        tier=ProviderTier.FAST,
        base_url="",
        api_key_env="",
        model="m",
    )
    body = _codex_request_body(
        config,
        _prompt(("system", content), ("user", "task")),
    )

    assert body["input"][0]["content"][0]["text"] == content


def test_header_validator_aggregates_volatile_indexes_and_accepts_tail_ids() -> None:
    messages = _volatile_messages()
    messages.extend(
        [
            ("user", "task 2099-12-31T23:59:59Z request_id=tail"),
            ("assistant", "550e8400-e29b-71d4-8123-123456789abc"),
        ]
    )

    with pytest.raises(PromptStructureError) as raised:
        validate_prompt_structure(_prompt(*messages))

    assert "message indexes [0, 1, 2, 3]" in str(raised.value)

    validate_prompt_structure(
        _prompt(
            ("system", "stable"),
            ("user", "task 2099-12-31T23:59:59Z request_id=tail"),
            ("assistant", "trace-id=tail 550e8400-e29b-71d4-8123-123456789abc"),
        )
    )


@pytest.mark.parametrize(
    "token",
    [
        "2026-08-01T01:02:03Z",
        "2024-02-29T01:02:03+00:00",
        "00000000-0000-0000-0000-000000000000",
        "018f0f00-1234-7123-8123-123456789abc",
    ],
)
def test_header_validator_rejects_zero_padded_leap_day_and_uuid_shapes(token: str) -> None:
    with pytest.raises(PromptStructureError):
        validate_prompt_structure(_prompt(("system", token)))


def test_header_validator_counts_mixed_line_endings() -> None:
    with pytest.raises(PromptStructureError, match=r"line 4:"):
        validate_prompt_structure(_prompt(("system", "stable\r\nnext\nthird\rrequest_id=req-1")))


def test_codex_sse_reconstruction_accepts_mixed_line_endings() -> None:
    completed = {
        "type": "response.completed",
        "response": {"model": "m", "usage": {}},
    }
    delta = {"type": "response.output_text.delta", "delta": "done"}
    stream = f"data: {json.dumps(delta)}\r\n\r\ndata: {json.dumps(completed)}\n\n"
    provider = ProviderConfig(
        name="p",
        tier=ProviderTier.FAST,
        base_url="",
        api_key_env="",
        model="m",
    )

    payload, text, error = _parse_codex_sse(provider, stream, "token")

    assert payload == completed
    assert text == "done"
    assert error is None
