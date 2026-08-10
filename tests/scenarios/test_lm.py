"""Integration scenarios for the DSPy-to-Diffundo provider boundary."""

from __future__ import annotations

import asyncio
from typing import Any

from cambium.architectus import ArchitectusCore
from cambium.diffundo import CallResult, ProviderTier
from cambium.lm import ArchitectusLM, CambiumLM
from cambium.tasktree import build_tree


class FakeDiffundo:
    def __init__(self) -> None:
        self.calls: list[dict[str, Any]] = []
        self.events: list[dict[str, Any]] = []

    async def call(
        self,
        tier: ProviderTier,
        prompt: dict[str, Any],
        *,
        model: str | None = None,
        budget_usd: float | None = None,
    ) -> CallResult:
        self.calls.append(
            {"tier": tier, "prompt": prompt, "model": model, "budget_usd": budget_usd}
        )
        content = "completion text"
        if prompt["messages"][0]["role"] == "system":
            content = '[{"action":"spawn","task_id":"root"}]'
        self.events.append(
            {
                "kind": "llm_call",
                "provider": "fake",
                "model": model or "fake-model",
                "tier": tier.value,
                "cache_hit": False,
            }
        )
        return CallResult(
            provider="fake",
            model=model or "fake-model",
            tier=tier,
            content=content,
            latency_s=0.01,
            usage={"prompt_tokens": 2, "completion_tokens": 2},
            estimated_cost_usd=0.0,
        )


def test_all_dspy_calls_use_diffundo_without_local_cache_or_prompt_events() -> None:
    diffundo = FakeDiffundo()
    lm = CambiumLM(diffundo, ProviderTier.FAST, temperature=0.0)  # type: ignore[arg-type]
    secret_prompt = "PROMPT-CANARY-never-an-event"

    assert lm.cache is False
    assert lm(messages=[{"role": "user", "content": secret_prompt}]) == ["completion text"]
    assert lm(
        messages=[{"role": "user", "content": secret_prompt}],
        cache=True,
        prompt_cache_key="CACHE-KEY-CANARY",
    ) == ["completion text"]
    tree = build_tree(
        {
            "tasks": [
                {"task_id": "root", "kind": "FEATURE", "depends_on": [], "spec": {}}
            ]
        }
    )
    actions = asyncio.run(ArchitectusCore(ArchitectusLM(lm), tree=tree).step([{"kind": "tick"}]))

    assert actions == [{"action": "spawn", "task_id": "root"}]
    assert len(diffundo.calls) == 3
    assert all(call["tier"] is ProviderTier.FAST for call in diffundo.calls)
    assert all("cache" not in call["prompt"] for call in diffundo.calls)
    assert all("prompt_cache" not in call["prompt"] for call in diffundo.calls)
    assert secret_prompt in diffundo.calls[0]["prompt"]["messages"][0]["content"]
    assert secret_prompt not in repr(diffundo.events)
    assert "CACHE-KEY-CANARY" not in repr(diffundo.calls)
    assert "CACHE-KEY-CANARY" not in repr(diffundo.events)
    assert "tree_state" not in repr(diffundo.events)
    assert all(event["cache_hit"] is False for event in diffundo.events)
    assert lm.history == []
