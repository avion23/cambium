"""Metric for the should_decompose module."""

from cambium.modules.base import evaluate_split, evaluate_split_async, score_decision


def should_decompose_metric(example) -> float:
    """Score one example by exact match on the module decision."""
    from .decide import Decision

    return score_decision(example, label_field="decompose", decision_type=Decision)


__all__ = ["evaluate_split", "evaluate_split_async", "should_decompose_metric"]
