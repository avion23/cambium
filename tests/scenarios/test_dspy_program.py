"""Offline scenario tests for the example module's DSPy program."""

from __future__ import annotations

import asyncio
import subprocess
import sys
from typing import Any

import pytest

from cambium.modules.base import Example
from cambium.modules.example.decide import Decision, DecomposeOutput, TaskInput
from cambium.modules.example.dspy_program import ShouldDecomposeModuleDSPy
from cambium.modules.should_review.decide import Decision as ReviewDecision
from cambium.modules.should_review.decide import ReviewOutput
from cambium.modules.should_review.decide import TaskInput as ReviewTaskInput
from cambium.modules.should_review.dspy_program import ShouldReviewModuleDSPy


def _fake_lm(response: str):
    import dspy  # type: ignore[import-untyped]

    class FakeLM(dspy.LM):
        def __init__(self) -> None:
            super().__init__("fake/test", cache=False, num_retries=0)

        def __call__(self, *args, **kwargs) -> list[dict[str, Any] | str]:
            return [response]

        async def acall(self, *args, **kwargs) -> list[dict[str, Any] | str]:
            return [response]

    return FakeLM()


def _decide(module: ShouldDecomposeModuleDSPy, task: str = "task") -> DecomposeOutput:
    return asyncio.run(module.decide(TaskInput(task=task)))


def _review_decide(module: ShouldReviewModuleDSPy, task: str = "task") -> ReviewOutput:
    return asyncio.run(module.decide(ReviewTaskInput(task=task)))


@pytest.mark.parametrize("module_name", ("example", "should_review"))
def test_importing_program_does_not_import_provider_sdk(module_name: str) -> None:
    probe = (
        "import sys; "
        f"import cambium.modules.{module_name}.dspy_program; "
        "assert 'openai' not in sys.modules, sys.modules.get('openai')"
    )
    result = subprocess.run(
        [sys.executable, "-c", probe],
        capture_output=True,
        text=True,
    )

    assert result.returncode == 0, result.stdout + result.stderr


@pytest.mark.parametrize(
    ("module_cls", "decide", "decision", "reason", "output_cls"),
    (
        (
            ShouldDecomposeModuleDSPy,
            _decide,
            Decision.DECOMPOSE,
            "The task has independent work.",
            DecomposeOutput,
        ),
        (
            ShouldDecomposeModuleDSPy,
            _decide,
            Decision.DO_NOT_DECOMPOSE,
            "The task is atomic.",
            DecomposeOutput,
        ),
        (
            ShouldReviewModuleDSPy,
            _review_decide,
            ReviewDecision.REVIEW,
            "The result needs an adversarial pass.",
            ReviewOutput,
        ),
    ),
)
def test_response_maps_to_domain_output(
    module_cls: type[Any], decide: Any, decision: Any, reason: str, output_cls: type[Any]
) -> None:
    response = f"""[[ ## decision ## ]]
{decision.value}

[[ ## reason ## ]]
{reason}

[[ ## completed ## ]]"""
    output = decide(module_cls(_fake_lm(response)))

    assert output == output_cls(decision=decision, reason=reason, confidence=0.5)


@pytest.mark.parametrize(
    ("module_cls", "decide", "fallback", "output_cls"),
    (
        (ShouldDecomposeModuleDSPy, _decide, Decision.DO_NOT_DECOMPOSE, DecomposeOutput),
        (ShouldReviewModuleDSPy, _review_decide, ReviewDecision.REVIEW, ReviewOutput),
    ),
)
def test_unparseable_decision_uses_conservative_fallback(
    module_cls: type[Any], decide: Any, fallback: Any, output_cls: type[Any]
) -> None:
    # Keep the DSPy wire format valid so the adapter does not spend time trying
    # its JSON fallback before the domain enum rejects the decision value.
    response = """[[ ## decision ## ]]
garbage

[[ ## reason ## ]]
not a domain value

[[ ## completed ## ]]"""
    output = decide(module_cls(_fake_lm(response)))

    assert output == output_cls(
        decision=fallback,
        reason="DSPy output unparseable",
        confidence=0.0,
    )


@pytest.mark.parametrize(
    (
        "module_cls",
        "input_cls",
        "output_cls",
        "label",
        "matching_decision",
        "mismatching_decision",
    ),
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
def test_metric_scores_matching_and_mismatching_predictions(
    module_cls: type[Any],
    input_cls: type[Any],
    output_cls: type[Any],
    label: str,
    matching_decision: Any,
    mismatching_decision: Any,
) -> None:
    module = module_cls(_fake_lm("unused"))
    example = Example(
        input=input_cls(task="task"),
        expected={label: matching_decision, "reason": "expected"},
    )

    matching = example.with_prediction(output_cls(decision=matching_decision, reason="predicted"))
    mismatching = example.with_prediction(
        output_cls(decision=mismatching_decision, reason="predicted")
    )

    assert module.metric(matching) == 1.0
    assert module.metric(mismatching) == 0.0
