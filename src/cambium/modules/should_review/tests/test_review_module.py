"""Scenario test for the should_review decision module.

No unit-testing ceremony: load the real dataset, run the module over
every record, and check the metric. No mocking, no network.
"""

from __future__ import annotations

import asyncio
from pathlib import Path

from cambium.modules.base import DatasetError
from cambium.modules.should_review import (
    Decision,
    ExampleDatasetLoader,
    ShouldReviewModule,
    Split,
)

DATASETS_DIR = Path(__file__).resolve().parents[1] / "datasets"


def _run_all() -> list[dict]:
    loader = ExampleDatasetLoader(DATASETS_DIR)
    module = ShouldReviewModule()

    async def run() -> list[dict]:
        scored = []
        for split in (Split.TRAIN, Split.EVAL, Split.CANARIES):
            for example in loader.load_split(split):
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
    loader = ExampleDatasetLoader(DATASETS_DIR)
    examples = loader.load_all()
    assert len(examples.train) + len(examples.eval) + len(examples.canaries) == 57
    for example in (*examples.train, *examples.eval, *examples.canaries):
        assert isinstance(example.expected["review"], Decision)
        assert example.expected["decompose"] in (True, False)
        assert hasattr(example.input, "task") and hasattr(example.input, "context")


def test_loader_maps_wire_boolean_to_decision(tmp_path) -> None:
    dataset = tmp_path / "pairs.jsonl"
    dataset.write_text(
        '{"input": {"task": "I cannot finish the migration.", "context": ""}, '
        '"expected": {"review": true, "decompose": true, "reason": "refusal"}}\n'
    )

    examples = ExampleDatasetLoader(dataset).load()

    assert examples[0].expected["review"] is Decision.REVIEW
    assert examples[0].expected["decompose"] is True


def test_malformed_record_is_rejected(tmp_path) -> None:
    bad = tmp_path / "bad.jsonl"
    bad.write_text('{"input": {"task": 42}, "expected": {"review": true, "decompose": true}}\n')
    try:
        ExampleDatasetLoader(bad).load()
    except DatasetError:
        pass
    else:
        raise AssertionError("expected DatasetError for a schema-invalid record")


def test_missing_expected_reason_is_rejected(tmp_path) -> None:
    bad = tmp_path / "bad_reason.jsonl"
    bad.write_text(
        '{"input": {"task": "Do the thing."}, "expected": {"review": false, "decompose": false}}\n'
    )
    try:
        ExampleDatasetLoader(bad).load()
    except DatasetError:
        pass
    else:
        raise AssertionError("expected DatasetError for a missing expected.reason")


def test_decompose_mirror_must_agree_with_review(tmp_path) -> None:
    bad = tmp_path / "bad_mirror.jsonl"
    bad.write_text(
        '{"input": {"task": "Do the thing.", "context": ""}, '
        '"expected": {"review": true, "decompose": false, "reason": "drift"}}\n'
    )
    try:
        ExampleDatasetLoader(bad).load()
    except DatasetError as exc:
        assert "decompose must mirror" in str(exc)
    else:
        raise AssertionError("expected DatasetError for a diverging class-balance mirror")


def test_engine_tolerates_leading_separators() -> None:
    from cambium.modules.should_review.decide import should_review

    result = should_review("; hello world", "")
    assert result.decision is Decision.DO_NOT_REVIEW


def test_module_rule_engine_smoke_on_its_dataset() -> None:
    loader = ExampleDatasetLoader(DATASETS_DIR)
    canaries = loader.load_split(Split.CANARIES)
    assert len(canaries) == 6
    scored = _run_all()
    assert len(scored) == 57
    assert all(item["metric"] in (0.0, 1.0) for item in scored)
    assert any(item["metric"] == 0.0 for item in scored)
    processed_canaries = [item for item in scored if item["example"].canary]
    assert len(processed_canaries) == len(canaries)
    assert all(item["prediction"] is not None for item in processed_canaries)
    assert any(item["metric"] == 0.0 for item in processed_canaries)
