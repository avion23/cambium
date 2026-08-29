"""Wave-3 budget multiplier and forced-finalization coverage."""

from __future__ import annotations

import asyncio

import pytest
from test_worker_agent_loop import (
    _agent_config,
    _drive_loop,
    _make_worktree,
    _UsageScriptedRouter,
)

from cambium import worker
from cambium.diffundo import ProviderConfig, ProviderTier, _attempt_budget


@pytest.mark.parametrize(
    ("base", "reasoning_effort", "expected"),
    [
        (30.0, None, 30.0),
        (180.0, "high", 180.0),
        (360.0, "max", 720.0),
    ],
)
def test_attempt_budget_multiplier_table(
    base: float, reasoning_effort: str | None, expected: float
) -> None:
    provider = ProviderConfig(
        name="budget-provider",
        tier=ProviderTier.FAST,
        base_url="",
        api_key_env="",
        reasoning_effort=reasoning_effort,
    )

    assert _attempt_budget(base, provider) == pytest.approx(expected)


@pytest.mark.parametrize(
    ("total_budget", "consumed", "generated", "finalizes", "succeeds"),
    [
        pytest.param(10_000, 9_000, 5_000, True, True, id="minimum-headroom-boundary"),
        pytest.param(10_000, 9_001, 4_999, True, True, id="above-soft-cap"),
        pytest.param(50_000, 44_999, 10_001, False, True, id="below-ninety-percent"),
        pytest.param(50_000, 45_000, 10_000, True, True, id="ten-percent-boundary"),
        pytest.param(50_000, 45_000, 10_001, True, False, id="headroom-overrun"),
        pytest.param(100_000, 90_000, 20_000, True, True, id="large-budget-boundary"),
    ],
)
def test_soft_cap_budget_multiplier_table(
    tmp_path,
    total_budget: int,
    consumed: int,
    generated: int,
    finalizes: bool,
    succeeds: bool,
) -> None:
    soft_cap = int(total_budget * worker.SOFT_TOKEN_CAP_RATIO)
    headroom = max(
        worker.FINAL_SYNTHESIS_MIN_HEADROOM_TOKENS,
        int(total_budget * worker.FINAL_SYNTHESIS_HEADROOM_RATIO),
    )
    assert finalizes is (consumed >= soft_cap)
    assert succeeds is (not finalizes or consumed + generated <= total_budget + headroom)

    fresh_input = 100
    first_output = 50
    second_output = 25
    prompt_delta = consumed - fresh_input - first_output - second_output
    assert prompt_delta > 0
    first_usage = {
        "prompt_tokens": fresh_input,
        "cached_tokens": 0,
        "completion_tokens": first_output,
        "total_tokens": fresh_input + first_output,
    }
    second_prompt = fresh_input + prompt_delta + 175
    second_usage = {
        "prompt_tokens": second_prompt,
        "cached_tokens": 175,
        "completion_tokens": second_output,
        "total_tokens": second_prompt + second_output,
    }
    final_prompt = second_prompt + 500
    final_usage = {
        "prompt_tokens": final_prompt,
        "cached_tokens": final_prompt,
        "completion_tokens": generated,
        "total_tokens": final_prompt + generated,
    }

    baseline = 0
    first_charge, baseline = worker._usage_budget_charge(first_usage, baseline)
    second_charge, baseline = worker._usage_budget_charge(second_usage, baseline)
    final_charge, _ = worker._usage_budget_charge(final_usage, baseline)
    assert first_charge + second_charge == consumed
    assert final_charge == generated

    worktree = _make_worktree(tmp_path / "repo")
    config = _agent_config(worktree, max_tokens=total_budget)
    router = _UsageScriptedRouter(
        [
            '{"type":"tool_call","name":"read_batch","arguments":{"paths":["alpha.txt"]}}',
            '{"type":"tool_call","name":"read_batch","arguments":{"paths":["beta.txt"]}}',
            '{"type":"finish","summary":"budget bounded","objective_met":true}',
        ],
        [first_usage, second_usage, final_usage],
    )

    outcome = asyncio.run(_drive_loop(config, worktree, router))

    assert outcome["status"] == ("succeeded" if succeeds else "failed")
    forced_messages = [
        message
        for prompt in router.prompts
        for message in prompt["messages"]
        if message.get("content") == worker.FINAL_SYNTHESIS_DIRECTIVE
    ]
    assert len(forced_messages) == int(finalizes)
