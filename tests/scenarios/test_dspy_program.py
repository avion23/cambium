"""Offline Cambium contracts for the optional DSPy decision adapter."""

from __future__ import annotations

import asyncio
import subprocess
import sys
from types import SimpleNamespace
from typing import Any

import pytest

from cambium.modules.base import Example
from cambium.modules.example.decide import Decision, DecomposeOutput, TaskInput
from cambium.modules.example.dspy_program import ShouldDecomposeModuleDSPy
from cambium.modules.should_review.decide import Decision as ReviewDecision
from cambium.modules.should_review.decide import ReviewOutput
from cambium.modules.should_review.decide import TaskInput as ReviewTaskInput
from cambium.modules.should_review.dspy_program import ShouldReviewModuleDSPy

pytestmark = [pytest.mark.slow, pytest.mark.xdist_group("dspy-runtime")]


class _Predictor:
    """Return already-decoded DSPy fields; DSPy's parser is DSPy's responsibility."""

    def __init__(self, decision: str, reason: str) -> None:
        self.decision = decision
        self.reason = reason

    async def acall(self, **_kwargs: object) -> SimpleNamespace:
        return SimpleNamespace(decision=self.decision, reason=self.reason)


def _decide(module_cls: type[Any], task_input: Any, decision: str, reason: str) -> Any:
    module = module_cls(None)
    module._predict = _Predictor(decision, reason)
    return asyncio.run(module.decide(task_input))


def test_importing_programs_does_not_import_provider_sdk() -> None:
    probe = (
        "import sys; "
        "import cambium.modules.example.dspy_program; "
        "import cambium.modules.should_review.dspy_program; "
        "assert 'openai' not in sys.modules, sys.modules.get('openai')"
    )
    result = subprocess.run([sys.executable, "-c", probe], capture_output=True, text=True)

    assert result.returncode == 0, result.stdout + result.stderr


@pytest.mark.parametrize(
    ("module_cls", "task_input", "decision", "reason", "expected"),
    (
        (
            ShouldDecomposeModuleDSPy,
            TaskInput(task="task"),
            Decision.DECOMPOSE.value,
            "The task has independent work.",
            DecomposeOutput(
                decision=Decision.DECOMPOSE,
                reason="The task has independent work.",
                confidence=0.5,
            ),
        ),
        (
            ShouldReviewModuleDSPy,
            ReviewTaskInput(task="task"),
            ReviewDecision.REVIEW.value,
            "The result needs an adversarial pass.",
            ReviewOutput(
                decision=ReviewDecision.REVIEW,
                reason="The result needs an adversarial pass.",
                confidence=0.5,
            ),
        ),
    ),
)
def test_response_maps_to_domain_output(
    module_cls: type[Any], task_input: Any, decision: str, reason: str, expected: Any
) -> None:
    assert _decide(module_cls, task_input, decision, reason) == expected


@pytest.mark.parametrize(
    ("module_cls", "task_input", "fallback", "output_cls"),
    (
        (
            ShouldDecomposeModuleDSPy,
            TaskInput(task="task"),
            Decision.DO_NOT_DECOMPOSE,
            DecomposeOutput,
        ),
        (
            ShouldReviewModuleDSPy,
            ReviewTaskInput(task="task"),
            ReviewDecision.REVIEW,
            ReviewOutput,
        ),
    ),
)
def test_invalid_domain_decision_uses_conservative_fallback(
    module_cls: type[Any], task_input: Any, fallback: Any, output_cls: type[Any]
) -> None:
    output = _decide(module_cls, task_input, "garbage", "not a domain value")

    assert output == output_cls(
        decision=fallback,
        reason="DSPy output unparseable",
        confidence=0.0,
    )


@pytest.mark.parametrize(
    ("module_cls", "input_cls", "output_cls", "label", "matching", "mismatching"),
    (
        (
            ShouldDecomposeModuleDSPy,
            TaskInput,
            DecomposeOutput,
            "decompose",
            Decision.DECOMPOSE,
            Decision.DO_NOT_DECOMPOSE,
        ),
        (
            ShouldReviewModuleDSPy,
            ReviewTaskInput,
            ReviewOutput,
            "review",
            ReviewDecision.REVIEW,
            ReviewDecision.DO_NOT_REVIEW,
        ),
    ),
)
def test_metric_wires_each_domain_label(
    module_cls: type[Any],
    input_cls: type[Any],
    output_cls: type[Any],
    label: str,
    matching: Any,
    mismatching: Any,
) -> None:
    module = module_cls(None)
    example = Example(
        input=input_cls(task="task"),
        expected={label: matching, "reason": "expected"},
    )

    assert (
        module.metric(example.with_prediction(output_cls(decision=matching, reason="predicted")))
        == 1.0
    )
    assert (
        module.metric(example.with_prediction(output_cls(decision=mismatching, reason="predicted")))
        == 0.0
    )
