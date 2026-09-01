from __future__ import annotations

from typing import Any, cast

import pytest

from cambium.schemas import TOOL_SCHEMAS, validate_tool_call


def _delegate_schema() -> dict[str, Any]:
    return cast(
        dict[str, Any],
        next(schema for schema in TOOL_SCHEMAS if schema["name"] == "delegate"),
    )


def _policy() -> dict[str, str]:
    return {"context_mode": "fresh", "placement": "spread"}


def test_delegate_schema_requires_a_scoped_workload() -> None:
    errors = validate_tool_call(
        _delegate_schema(),
        {
            "child_task_id": "child-review",
            "kind": "investigation",
            "spec": _policy(),
        },
    )

    assert errors == ["validation failed: missing 'spec.task' (string)"]


def test_delegate_schema_requires_explicit_context_and_placement() -> None:
    errors = validate_tool_call(
        _delegate_schema(),
        {
            "child_task_id": "child-review",
            "kind": "investigation",
            "spec": {"task": "inspect provider routing"},
        },
    )

    assert errors == [
        "validation failed: missing 'spec.context_mode' (string)",
        "validation failed: missing 'spec.placement' (string)",
    ]


def test_delegate_schema_accepts_explicit_provider_constraints() -> None:
    errors = validate_tool_call(
        _delegate_schema(),
        {
            "child_task_id": "child-review",
            "kind": "investigation",
            "spec": {
                "task": (
                    "Inspect provider routing only; own no files; report concrete "
                    "violations and the tests that reproduce them."
                ),
                **_policy(),
                "requirements": {
                    "quality": "strong",
                    "needs_native_tools": True,
                },
                "model_candidates": ["gpt-5.6", "claude-opus"],
                "authorized_providers": ["openai", "anthropic"],
                "authorized_providers_explicit": True,
                "child_only": True,
            },
        },
    )

    assert errors == []


@pytest.mark.parametrize(
    ("field", "value", "expected"),
    [
        ("max_turns", 0, ">= 1"),
        ("max_turns", -1, ">= 1"),
        ("max_turns", "two", "integer"),
        ("max_wall_s", 0, ">= 30"),
        ("max_wall_s", -1, ">= 30"),
        ("max_wall_s", "slow", "integer"),
    ],
)
def test_delegate_schema_rejects_invalid_budget_values(
    field: str, value: Any, expected: str
) -> None:
    errors = validate_tool_call(
        _delegate_schema(),
        {
            "child_task_id": "child-review",
            "kind": "investigation",
            "spec": {"task": "inspect the routing", **_policy(), field: value},
        },
    )

    assert errors == [f"validation failed: 'spec.{field}' must be {expected}"]
