"""Scenario test for the should_review decision module.

No unit-testing ceremony: load the real dataset, run the module over
every record, and check the metric. No mocking, no network.
"""

from __future__ import annotations

import asyncio
import os
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

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
    assert len(examples.train) + len(examples.eval) + len(examples.canaries) == 55
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


def test_module_scores_perfect_on_its_dataset() -> None:
    loader = ExampleDatasetLoader(DATASETS_DIR)
    canaries = loader.load_split(Split.CANARIES)
    assert len(canaries) == 5
    scored = _run_all()
    assert len(scored) == 55
    assert all(item["metric"] == 1.0 for item in scored)
    processed_canaries = [item for item in scored if item["example"].canary]
    assert len(processed_canaries) == len(canaries)
    assert all(item["prediction"] is not None for item in processed_canaries)
    assert all(item["metric"] == 1.0 for item in processed_canaries)


def test_eval_aggregate_meets_declared_threshold() -> None:
    loader = ExampleDatasetLoader(DATASETS_DIR)
    module = ShouldReviewModule()

    async def run() -> float:
        scores = []
        for example in loader.load_split(Split.EVAL):
            prediction = await module.decide(example.input)
            scores.append(module.metric(example.with_prediction(prediction)))
        return sum(scores) / len(scores)

    mean = asyncio.run(run())
    assert len(loader.load_split(Split.EVAL)) == 10
    assert mean >= 0.95  # declared aggregate threshold (architecture.md §9.1)


def test_subprocess_network_client_is_denied() -> None:
    """The module gate must protect subprocesses, not only this pytest process."""
    if os.environ.get("CAMBIUM_MODULE_OFFLINE") != "1":
        pytest.skip("requires the isolated module-test environment")
    if shutil.which("curl") is None:
        pytest.skip("curl is not installed; cannot probe network-client denial")

    try:
        result = subprocess.run(
            ["curl", "--fail", "http://127.0.0.1:9/"],
            capture_output=True,
            text=True,
            check=False,
            timeout=5,
        )
    except PermissionError as exc:
        assert "network client denied" in str(exc)
    else:
        assert result.returncode != 0
        assert "network client denied" in result.stderr

    python_probe = subprocess.run(
        [
            sys.executable,
            "-c",
            "import socket; socket.create_connection(('127.0.0.1', 9), timeout=1)",
        ],
        capture_output=True,
        text=True,
        check=False,
        timeout=5,
    )
    assert python_probe.returncode != 0
    assert "network access is forbidden" in python_probe.stderr
