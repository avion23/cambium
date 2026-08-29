from __future__ import annotations

from typing import Any, cast

from cambium.schemas import TOOL_SCHEMAS, validate_tool_call


def _delegate_schema() -> dict[str, Any]:
    return cast(
        dict[str, Any],
        next(schema for schema in TOOL_SCHEMAS if schema["name"] == "delegate"),
    )


def test_delegate_schema_describes_the_proposal_boundary() -> None:
    schema = _delegate_schema()
    description = schema["description"]

    assert "work you should not do yourself" in description
    assert "separable subproblem with its own files or investigation area" in description
    assert "full Cambium worker" in description
    assert "isolated git worktree" in description
    assert "IMPORTANT: this call only PROPOSES the child" in description
    assert "it starts only after your task finishes, and you never see its output" in description
    assert "it inherits only immutable summaries, not your conversation" in description
    assert "An exact compatible fork" not in description

    task_description = schema["parameters"]["properties"]["spec"]["properties"]["task"][
        "description"
    ]
    assert (
        "Example: 'In src/parser/, add offset paging to read_lines(); done when "
        "tests/test_parser.py::test_paging passes.'"
    ) in task_description


def test_delegate_schema_requires_a_scoped_workload() -> None:
    errors = validate_tool_call(
        _delegate_schema(),
        {
            "child_task_id": "child-review",
            "kind": "investigation",
            "spec": {},
        },
    )

    assert errors == ["validation failed: missing 'spec.task' (string)"]


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
