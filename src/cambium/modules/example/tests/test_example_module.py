"""Scenario test for the should_decompose reference module.

No unit-testing ceremony: load the real dataset, run the module over
every pair, and check the metric. No mocking, no network.
"""

from __future__ import annotations

import asyncio
from pathlib import Path

import pytest

from cambium.modules.base import DatasetError, Example
from cambium.modules.example import (
    Decision,
    DecomposeOutput,
    ExampleDatasetLoader,
    ShouldDecomposeModule,
    TaskInput,
)
from cambium.modules.example.metric import should_decompose_metric

DATASET_PATH = Path(__file__).resolve().parents[1] / "datasets" / "example_pairs.jsonl"


def _run_all() -> list[dict]:
    loader = ExampleDatasetLoader(DATASET_PATH)
    module = ShouldDecomposeModule()

    async def run() -> list[dict]:
        scored = []
        for example in loader.load():
            prediction = await module.decide(example.input)
            scored_example = example.with_prediction(prediction)
            scored.append(
                {
                    "example": scored_example,
                    "prediction": prediction,
                    "metric": module.metric(scored_example),
                }
            )
        return scored

    return asyncio.run(run())


def test_dataset_is_loadable_and_schema_valid() -> None:
    examples = ExampleDatasetLoader(DATASET_PATH).load()
    assert len(examples) >= 8
    assert all(isinstance(ex.expected["decompose"], Decision) for ex in examples)
    assert all(hasattr(ex.input, "task") and hasattr(ex.input, "context") for ex in examples)


def test_decision_members_exist() -> None:
    assert set(Decision) == {Decision.DECOMPOSE, Decision.DO_NOT_DECOMPOSE}


def test_decompose_property_is_read_only_compat_shim() -> None:
    decomposed = DecomposeOutput(decision=Decision.DECOMPOSE, reason="parallel work")
    atomic = DecomposeOutput(decision=Decision.DO_NOT_DECOMPOSE, reason="atomic")

    assert decomposed.decompose is True
    assert atomic.decompose is False
    with pytest.raises(AttributeError):
        decomposed.decompose = False


def test_metric_matches_enum_decisions() -> None:
    expected = {"decompose": Decision.DECOMPOSE, "reason": "parallel work"}
    prediction = DecomposeOutput(decision=Decision.DECOMPOSE, reason="parallel work")
    example = Example(
        input=TaskInput("Split the work", ""), expected=expected, prediction=prediction
    )
    assert should_decompose_metric(example) == 1.0

    wrong = example.with_prediction(
        DecomposeOutput(decision=Decision.DO_NOT_DECOMPOSE, reason="atomic")
    )
    assert should_decompose_metric(wrong) == 0.0


def test_loader_maps_wire_boolean_to_decision(tmp_path) -> None:
    dataset = tmp_path / "pairs.jsonl"
    dataset.write_text(
        '{"input": {"task": "Split the work", "context": ""}, '
        '"expected": {"decompose": true, "reason": "parallel work"}}\n'
    )

    examples = ExampleDatasetLoader(dataset).load()

    assert examples[0].expected["decompose"] is Decision.DECOMPOSE


def test_malformed_record_is_rejected(tmp_path) -> None:
    bad = tmp_path / "bad.jsonl"
    bad.write_text('{"input": {"task": 42}, "expected": {"decompose": true}}\n')
    try:
        ExampleDatasetLoader(bad).load()
    except DatasetError:
        pass
    else:
        raise AssertionError("expected DatasetError for a schema-invalid record")


def test_missing_expected_reason_is_rejected(tmp_path) -> None:
    bad = tmp_path / "bad_reason.jsonl"
    bad.write_text(
        '{"input": {"task": "Do the thing."}, "expected": {"decompose": false}}\n'
    )
    try:
        ExampleDatasetLoader(bad).load()
    except DatasetError:
        pass
    else:
        raise AssertionError("expected DatasetError for a missing expected.reason")


def test_engine_tolerates_leading_separators() -> None:
    from cambium.modules.example.decide import should_decompose

    result = should_decompose("; hello world", "")
    assert result.decision is Decision.DO_NOT_DECOMPOSE


def test_module_scores_perfect_on_its_dataset() -> None:
    scored = _run_all()
    assert len(scored) >= 8
    assert all(item["metric"] == 1.0 for item in scored)


def test_canary_entries_are_processed() -> None:
    loader = ExampleDatasetLoader(DATASET_PATH)
    examples = loader.load()
    canaries = [ex for ex in examples if ex.canary]
    assert len(canaries) >= 1
    scored = _run_all()
    processed_canaries = [item for item in scored if item["example"].canary]
    assert len(processed_canaries) == len(canaries)
    assert all(item["prediction"] is not None for item in processed_canaries)
    assert all(item["metric"] == 1.0 for item in processed_canaries)
