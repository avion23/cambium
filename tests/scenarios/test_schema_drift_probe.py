"""Boundary regressions for tool refusal, numeric evaluation, and optimization."""

from __future__ import annotations

import asyncio
from pathlib import Path
from types import SimpleNamespace

import pytest

from cambium.modules.base import (
    Example,
    evaluate_split_async,
)
from cambium.modules.example.decide import Decision, DecomposeOutput, TaskInput
from cambium.tools import ToolContext, run_tool


def _run(name: str, args: dict, ctx: ToolContext):
    return asyncio.run(run_tool(name, args, ctx))


def test_git_op_rejects_unsafe_operations(tmp_path: Path) -> None:
    ctx = ToolContext(tmp_path)
    for op in ("checkout", "reset"):
        result = _run("git_op", {"op": op, "args": ""}, ctx)
        assert not result.ok
        assert result.error


def test_evaluate_split_async_rejects_bool_scores() -> None:
    class BoolMetric:
        name = "bool-metric"

        async def decide(self, inputs):
            return DecomposeOutput(decision=Decision.DECOMPOSE, reason="r")

        def metric(self, example: Example) -> float:
            return True  # type: ignore[return-value]

    class FakeLoader:
        def load_split(self, split):
            return [
                Example(
                    input=TaskInput(task="t", context=""),
                    expected={"decompose": Decision.DECOMPOSE, "reason": "r"},
                )
            ]

    with pytest.raises(TypeError, match="not numeric"):
        asyncio.run(evaluate_split_async(BoolMetric(), FakeLoader(), None))


def test_prompt_optimize_metric_rejects_bool() -> None:
    pytest.importorskip("dspy")
    from cambium.prompt_optimize import metric

    assert metric(None, SimpleNamespace(score=True, report="r")).score == 0.0
    assert metric(None, SimpleNamespace(score=False, report="r")).score == 0.0
    assert metric(None, SimpleNamespace(score=0.9, report="r")).score == 0.9
