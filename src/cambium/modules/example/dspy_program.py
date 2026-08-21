"""DSPy program implementing the should_decompose decision."""

from __future__ import annotations

from typing import Any, Literal, cast

from cambium.modules.base import Example

from .decide import Decision, DecomposeOutput, TaskInput
from .metric import should_decompose_metric


class _LazyDSPyBase:
    """Placeholder base kept until DSPy is first used."""


class ShouldDecomposeModuleDSPy(_LazyDSPyBase):
    """DSPy classifier with the same decision and metric interface."""

    name = "should_decompose"

    def __init__(self, lm) -> None:
        import dspy  # type: ignore[import-untyped]

        if _LazyDSPyBase in ShouldDecomposeModuleDSPy.__bases__:
            ShouldDecomposeModuleDSPy.__bases__ = (dspy.Module,)
        dspy.Module.__init__(cast(Any, self))

        class ShouldDecomposeSignature(dspy.Signature):
            task: str = dspy.InputField()
            context: str = dspy.InputField()
            decision: Literal["decompose", "do_not_decompose"] = dspy.OutputField()
            reason: str = dspy.OutputField()

        self._predict = dspy.Predict(ShouldDecomposeSignature)
        self._lm = lm

    async def decide(self, input: TaskInput) -> DecomposeOutput:
        """Run the DSPy predictor and map its output to the domain enum."""
        import dspy  # type: ignore[import-untyped]

        try:
            with dspy.context(lm=self._lm):
                pred = await self._predict.acall(task=input.task, context=input.context)
            decision = Decision(str(pred.decision))
        except ValueError:
            return DecomposeOutput(
                decision=Decision.DO_NOT_DECOMPOSE,
                reason="DSPy output unparseable",
                confidence=0.0,
            )
        return DecomposeOutput(decision=decision, reason=str(pred.reason), confidence=0.5)

    def metric(self, example: Example) -> float:
        """Score a prediction with the example module's metric."""
        return should_decompose_metric(example)
