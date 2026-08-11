"""Metric for the should_review module."""

from __future__ import annotations

import asyncio
import statistics

from cambium.modules.base import Example, Module


def should_review_metric(example: Example) -> float:
    """Score one example in [0, 1]; exact match on the decision wins.

    Returns 0.0 for unprocessed examples (no prediction) and for records
    whose expected value is not a :class:`Decision`. The ``reason`` field is
    not scored; the decision is what matters.
    """
    prediction = example.prediction
    if prediction is None:
        return 0.0
    from .decide import Decision

    expected = example.expected.get("review")
    if not isinstance(expected, Decision) or not isinstance(prediction.decision, Decision):
        return 0.0
    return 1.0 if prediction.decision == expected else 0.0


async def evaluate_split_async(module: Module, loader, split) -> dict:
    """Score one dataset split with the module metric (async form).

    Runs the module over every example in the split and returns a
    ``{"mean", "std", "count"}`` summary for the bench harness baseline.
    Call from async code; the sync :func:`evaluate_split` wrapper must not be
    used from a running event loop.
    """
    scores: list[float] = []
    for example in loader.load_split(split):
        prediction = await module.decide(example.input)
        scores.append(module.metric(example.with_prediction(prediction)))
    if not scores:
        return {"mean": float("nan"), "std": float("nan"), "count": 0}
    return {
        "mean": statistics.fmean(scores),
        "std": statistics.pstdev(scores),
        "count": len(scores),
    }


def evaluate_split(module: Module, loader, split) -> dict:
    """Score one dataset split with the module metric.

    Must not be called from a running event loop; use
    :func:`evaluate_split_async` in async contexts.
    """
    try:
        asyncio.get_running_loop()
    except RuntimeError:
        return asyncio.run(evaluate_split_async(module, loader, split))
    raise RuntimeError(
        "evaluate_split must not be called from a running event loop; "
        "use evaluate_split_async in async contexts"
    )
