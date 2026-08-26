"""Drift checks for the versioned prompt constants."""

import cambium.prompts as prompts
from cambium import worker


def test_prompt_constants_match_worker_text() -> None:
    """Keep the central constants byte-for-byte aligned with worker prompts."""
    system = worker._build_agent_prompt("prompt drift test", [], [])["messages"][0]["content"]

    assert prompts.PROMPTS_VERSION == 1
    assert prompts.SEMANTIC_SUMMARIZER == "\n".join(worker.SUMMARY_PROTOCOL_LINES)
    assert system == prompts.CODING_AGENT + "\n[]"
