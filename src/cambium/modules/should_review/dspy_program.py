"""DSPy program implementing the should_review decision."""

from __future__ import annotations

from typing import Any, Literal, cast

from cambium.modules.base import Example

from .decide import Decision, ReviewOutput, TaskInput
from .metric import should_review_metric


class _LazyDSPyBase:
    """Placeholder base kept until DSPy is first used."""


class ShouldReviewModuleDSPy(_LazyDSPyBase):
    """DSPy classifier with the same decision and metric interface."""

    name = "should_review"
    label_field = "review"
    fallback_decision = Decision.REVIEW

    def __init__(self, lm) -> None:
        import dspy  # type: ignore[import-untyped]

        if _LazyDSPyBase in ShouldReviewModuleDSPy.__bases__:
            ShouldReviewModuleDSPy.__bases__ = (dspy.Module,)
        dspy.Module.__init__(cast(Any, self))

        class ShouldReviewSignature(dspy.Signature):
            task: str = dspy.InputField()
            context: str = dspy.InputField()
            decision: Literal["review", "do_not_review"] = dspy.OutputField()
            reason: str = dspy.OutputField()

        self._predict = dspy.Predict(ShouldReviewSignature)
        self._lm = lm

    async def decide(self, input: TaskInput) -> ReviewOutput:
        """Run the DSPy predictor and map its output to the domain enum."""
        import dspy  # type: ignore[import-untyped]

        try:
            with dspy.context(lm=self._lm):
                pred = await self._predict.acall(task=input.task, context=input.context)
            decision = Decision(str(pred.decision))
        except (ValueError, dspy.AdapterParseError):
            return ReviewOutput(
                decision=Decision.REVIEW,
                reason="DSPy output unparseable",
                confidence=0.0,
            )
        return ReviewOutput(decision=decision, reason=str(pred.reason), confidence=0.5)

    def metric(self, example: Example) -> float:
        """Score a prediction with the example module's metric."""
        return should_review_metric(example)
