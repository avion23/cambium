"""Drift checks for the versioned prompt constants."""

import cambium.prompts as prompts


def test_semantic_summarizer_preserves_findings_and_next_actions() -> None:
    assert prompts.PROMPTS_VERSION == 2
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
    assert 'The object must contain "objective" and "outcome"' in contract
    assert "source_sha256, source_message_count, and through_turn" in contract
    assert "<cambium-summary-entry>" in contract
    assert 'Example: {"objective": "find the paging bug"' in contract
    assert "hard limit 32" in contract
    assert "relevant_failed_approaches and verification_results first" in contract
