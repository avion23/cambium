"""Regression scenarios for Diffundo prompt-header hygiene."""

from __future__ import annotations

import pytest

from cambium.diffundo import PromptStructureError, validate_prompt_structure

VOLATILE_TOKENS = (
    "2026-08-20T12:34:56Z",
    "1700000000",
    "request_id=req-123",
    "trace-id=trace-456",
    "550e8400-e29b-41d4-a716-446655440000",
)


def _prompt(*messages: tuple[str, str]) -> dict[str, object]:
    return {"messages": [{"role": role, "content": content} for role, content in messages]}


@pytest.mark.parametrize("token", VOLATILE_TOKENS)
def test_volatile_tokens_in_first_header_message_are_rejected(token: str) -> None:
    with pytest.raises(PromptStructureError, match=r"message indexes \[0\]"):
        validate_prompt_structure(_prompt(("system", f"static header {token}")))


def test_volatile_tokens_in_later_header_messages_are_aggregated() -> None:
    prompt = _prompt(
        ("system", "stable system header"),
        ("developer", "created 2026-08-20T12:34:56Z"),
        ("assistant", "trace-id=trace-456 and 550e8400-e29b-41d4-a716-446655440000"),
        ("user", "task data 1700000000"),
    )

    with pytest.raises(PromptStructureError) as raised:
        validate_prompt_structure(prompt)

    assert "message indexes [1, 2]" in str(raised.value)


@pytest.mark.parametrize(
    "messages",
    (
        (
            ("system", "You are a coding assistant."),
            ("developer", "Use deterministic output."),
            ("assistant", "The immutable header ends before user data."),
        ),
        (
            ("system", "You are a coding assistant."),
            (
                "user",
                "task timestamp 2026-08-20T12:34:56Z "
                "uuid 550e8400-e29b-41d4-a716-446655440000 request_id=req-123",
            ),
        ),
    ),
)
def test_static_headers_and_dynamic_user_tail_pass(
    messages: tuple[tuple[str, str], ...],
) -> None:
    validate_prompt_structure(_prompt(*messages))
