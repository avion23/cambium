"""Source-level pins for the current public worker vocabulary."""

from __future__ import annotations

from cambium import branch_state, prompts, schemas, tools
from cambium.child_policy import ContextMode, Placement

EXPECTED_WORKER_TOOLS = (
    "write_file",
    "edit_file",
    "git_op",
    "run_shell",
    "read_batch",
    "repo_query",
    "branch_history",
    "delegate",
)
EXPECTED_PROMPT_EXPORTS = frozenset(
    {
        "CODING_AGENT",
        "PROMPTS_VERSION",
        "SEMANTIC_SUMMARIZER",
        "SUMMARY_PROTOCOL_LINES",
    }
)
EXPECTED_CONTEXT_MODES = ("trunk", "semantic", "fresh")
EXPECTED_PLACEMENTS = ("inherit", "spread")
EXPECTED_LIFECYCLES = (
    "unknown",
    "queued",
    "starting",
    "active",
    "suspended",
    "joining",
    "verifying",
    "publishing",
    "succeeded",
    "failed",
    "cancelled",
    "rejected",
)


def test_source_truth_matches_public_catalogue() -> None:
    schema_names = tuple(schema["name"] for schema in schemas.TOOL_SCHEMAS)
    dispatch_names = frozenset(tools.TOOL_DISPATCH)

    assert len(schema_names) == len(set(schema_names)) == len(EXPECTED_WORKER_TOOLS)
    assert frozenset(schema_names) == dispatch_names == frozenset(EXPECTED_WORKER_TOOLS)
    assert len(prompts.__all__) == len(set(prompts.__all__))
    assert frozenset(prompts.__all__) == EXPECTED_PROMPT_EXPORTS
    assert tuple(member.value for member in ContextMode) == EXPECTED_CONTEXT_MODES
    assert tuple(member.value for member in Placement) == EXPECTED_PLACEMENTS
    assert tuple(member.value for member in branch_state.Lifecycle) == EXPECTED_LIFECYCLES
