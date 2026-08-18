"""Offline scenario tests for the example module's DSPy program."""

from __future__ import annotations

import asyncio
import importlib
import sys
from pathlib import Path

from cambium.modules.base import Example
from cambium.modules.example.decide import Decision, DecomposeOutput, TaskInput
from cambium.modules.example.dspy_program import ShouldDecomposeModuleDSPy

PROGRAM_PATH = (
    Path(__file__).resolve().parents[2]
    / "src"
    / "cambium"
    / "modules"
    / "example"
    / "dspy_program.py"
)


def _fake_lm(response: str):
    import dspy

    class FakeLM(dspy.LM):
        def __init__(self) -> None:
            super().__init__("fake/test", cache=False, num_retries=0)

        def __call__(self, *args, **kwargs):
            return [response]

        async def acall(self, *args, **kwargs):
            return [response]

    return FakeLM()


def _decide(module: ShouldDecomposeModuleDSPy, task: str = "task") -> DecomposeOutput:
    return asyncio.run(module.decide(TaskInput(task=task)))


def test_importing_program_does_not_import_provider_sdk() -> None:
    assert "openai" not in sys.modules
    importlib.import_module("cambium.modules.example.dspy_program")
    assert "openai" not in sys.modules


def test_dspy_import_is_lazy() -> None:
    source = PROGRAM_PATH.read_text(encoding="utf-8")
    definition_positions = [source.find("def "), source.find("class ")]
    first_definition = min(position for position in definition_positions if position >= 0)
    assert "import dspy" not in source[:first_definition]


def test_decompose_response_maps_to_domain_output() -> None:
    response = """[[ ## decision ## ]]
decompose

[[ ## reason ## ]]
The task has independent work.

[[ ## completed ## ]]"""
    output = _decide(ShouldDecomposeModuleDSPy(_fake_lm(response)))

    assert output == DecomposeOutput(
        decision=Decision.DECOMPOSE,
        reason="The task has independent work.",
        confidence=0.5,
    )


def test_do_not_decompose_response_maps_to_domain_output() -> None:
    response = """[[ ## decision ## ]]
do_not_decompose

[[ ## reason ## ]]
The task is atomic.

[[ ## completed ## ]]"""
    output = _decide(ShouldDecomposeModuleDSPy(_fake_lm(response)))

    assert output == DecomposeOutput(
        decision=Decision.DO_NOT_DECOMPOSE,
        reason="The task is atomic.",
        confidence=0.5,
    )


def test_unparseable_decision_uses_conservative_fallback() -> None:
    response = '{"decision": "garbage", "reason": "not a domain value"}'
    output = _decide(ShouldDecomposeModuleDSPy(_fake_lm(response)))

    assert output == DecomposeOutput(
        decision=Decision.DO_NOT_DECOMPOSE,
        reason="DSPy output unparseable",
        confidence=0.0,
    )


def test_metric_scores_matching_and_mismatching_predictions() -> None:
    module = ShouldDecomposeModuleDSPy(_fake_lm("unused"))
    example = Example(
        input=TaskInput(task="task"),
        expected={"decompose": Decision.DECOMPOSE, "reason": "expected"},
    )

    matching = example.with_prediction(
        DecomposeOutput(decision=Decision.DECOMPOSE, reason="predicted")
    )
    mismatching = example.with_prediction(
        DecomposeOutput(decision=Decision.DO_NOT_DECOMPOSE, reason="predicted")
    )

    assert module.metric(matching) == 1.0
    assert module.metric(mismatching) == 0.0
