"""Drift checks for the versioned prompt constants."""

import cambium.prompts as prompts
from cambium import worker


def test_prompt_constants_match_worker_text() -> None:
    """Keep the central constants byte-for-byte aligned with worker prompts."""
    system = worker._build_agent_prompt("prompt drift test", [], [])["messages"][0]["content"]

    assert prompts.PROMPTS_VERSION == 1
    assert prompts.SEMANTIC_SUMMARIZER == "\n".join(worker.SUMMARY_PROTOCOL_LINES)
    expected = prompts.CODING_AGENT.replace(
        '  finish:    {"type": "finish", "summary": <non-empty summary>}',
        '  finish:    {"type": "finish", "summary": <non-empty summary>, '
        '"objective_met": <boolean>}',
    ).replace(
        '{"type": "finish", "summary": "implemented and verified the change"}',
        '{"type": "finish", "summary": "implemented and verified the change", '
        '"objective_met": true}',
    )
    assert system == expected + "\n[]"


def test_semantic_summarizer_preserves_findings_and_next_actions() -> None:
    contract = prompts.SEMANTIC_SUMMARIZER

    for keyword in (
        "PRESERVE",
        "file:line",
        "defect hypotheses",
        "evidence for/against",
        "approaches tried and outcomes",
        "exact reproduction steps/status",
        "measurements",
        "test results",
        "thresholds",
        "open questions",
        "NEXT ACTION",
        "symbol, or exact lines to inspect next",
        "truncation note",
        "what was dropped",
    ):
        assert keyword in contract

    assert "Never discard preserved findings" in contract
