"""Optional DSPy program support; the rule-engine runtime does not import this."""

from __future__ import annotations

from enum import Enum
from typing import Any, ClassVar, Literal

import dspy

from .base import PARSE_FAILURE_REASON, Example, score_decision


class DSPyModuleBase(dspy.Module):
    """One predictor with the same decision/metric interface as a rule module."""

    name: ClassVar[str]
    label_field: ClassVar[str]
    fallback_decision: ClassVar[Enum]
    output_type: ClassVar[type]
    decision_type: ClassVar[type[Enum]]
    signature_name: ClassVar[str]
    signature_docstring: ClassVar[str]

    def __init__(self, lm: Any) -> None:
        super().__init__()
        decision_values = tuple(member.value for member in self.decision_type)
        signature = type(
            self.signature_name,
            (dspy.Signature,),
            {
                "__module__": type(self).__module__,
                "__doc__": self.signature_docstring,
                "__annotations__": {
                    "task": str,
                    "context": str,
                    "decision": Literal[decision_values],
                    "reason": str,
                },
                "task": dspy.InputField(),
                "context": dspy.InputField(),
                "decision": dspy.OutputField(desc="exactly one of the allowed values"),
                "reason": dspy.OutputField(desc="one short sentence naming the evidence"),
            },
        )
        self._predict = dspy.Predict(signature)
        self._lm = lm

    async def decide(self, input: Any) -> Any:
        try:
            with dspy.context(lm=self._lm):
                pred = await self._predict.acall(task=input.task, context=input.context)
            decision = self.decision_type(str(pred.decision))
        except (ValueError, dspy.AdapterParseError):
            return self.output_type(
                decision=self.fallback_decision,
                reason=PARSE_FAILURE_REASON,
                confidence=0.0,
            )
        return self.output_type(decision=decision, reason=str(pred.reason), confidence=0.5)

    def metric(self, example: Example) -> float:
        return score_decision(
            example, label_field=self.label_field, decision_type=self.decision_type
        )
