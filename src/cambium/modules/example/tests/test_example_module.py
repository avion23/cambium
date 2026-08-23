"""Scenario test for the should_decompose reference module.

No unit-testing ceremony: load the real dataset, run the module over
every pair, and check the metric. No mocking, no network.
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
from cambium.modules.example import (
    Decision,
    ExampleDatasetLoader,
    ShouldDecomposeModule,
)

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
    bad.write_text('{"input": {"task": "Do the thing."}, "expected": {"decompose": false}}\n')
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
    loader = ExampleDatasetLoader(DATASET_PATH)
    canaries = [ex for ex in loader.load() if ex.canary]
    assert len(canaries) >= 1
    scored = _run_all()
    assert len(scored) >= 8
    assert all(item["metric"] == 1.0 for item in scored)
    processed_canaries = [item for item in scored if item["example"].canary]
    assert len(processed_canaries) == len(canaries)
    assert all(item["prediction"] is not None for item in processed_canaries)
    assert all(item["metric"] == 1.0 for item in processed_canaries)


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
