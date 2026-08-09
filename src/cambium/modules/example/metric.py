"""Metric for the should_decompose module."""

from __future__ import annotations

from cambium.modules.base import Example


def should_decompose_metric(example: Example) -> float:
    """Score one example in [0, 1]; exact match on the decision wins.

    Returns 0.0 for unprocessed examples (no prediction) and for records
    whose expected value is not a boolean. The ``reason`` field is not
    scored; the decision is what matters.
    """
    prediction = example.prediction
    if prediction is None:
        return 0.0
    expected = example.expected.get("decompose")
    if not isinstance(expected, bool):
        return 0.0
    return 1.0 if prediction.decompose == expected else 0.0
