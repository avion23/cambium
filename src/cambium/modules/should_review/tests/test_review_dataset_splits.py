"""Domain-specific dataset checks for the should_review module.

The shared loader contract is exercised once by the reference example module.
These tests keep only should_review's committed corpus, label, and baseline
invariants.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
from pathlib import Path

from cambium.modules.should_review import ExampleDatasetLoader, ShouldReviewModule, Split

DATASETS_DIR = Path(__file__).resolve().parents[1] / "datasets"
BASELINE_PATH = Path(__file__).resolve().parents[1] / "tests" / "baselines" / "baseline.json"
EXPECTED_COUNTS = {Split.TRAIN: 40, Split.EVAL: 11, Split.CANARIES: 6}
EXPECTED_TOTAL = sum(EXPECTED_COUNTS.values())


def test_committed_splits_match_should_review_contract() -> None:
    loader = ExampleDatasetLoader(DATASETS_DIR)
    bundle = loader.load_all()

    assert loader.dataset_version == "2.0.0"
    assert {split: len(loader.load_split(split)) for split in EXPECTED_COUNTS} == EXPECTED_COUNTS
    assert len(bundle.train) + len(bundle.eval) + len(bundle.canaries) == EXPECTED_TOTAL
    assert all(not example.canary for example in (*bundle.train, *bundle.eval))
    assert all(example.canary for example in bundle.canaries)

    for split in ("train", "eval", "canaries"):
        for line in (DATASETS_DIR / f"{split}.jsonl").read_text(encoding="utf-8").splitlines():
            expected = json.loads(line)["expected"]
            assert expected["review"] == expected["decompose"]


def test_rule_engine_smoke_on_transcript_dataset() -> None:
    loader = ExampleDatasetLoader(DATASETS_DIR)
    module = ShouldReviewModule()

    async def count_mismatches() -> int:
        mismatches = 0
        for split in EXPECTED_COUNTS:
            for example in loader.load_split(split):
                prediction = await module.decide(example.input)
                mismatches += module.metric(example.with_prediction(prediction)) != 1.0
        return mismatches

    assert asyncio.run(count_mismatches()) > 0


def test_baseline_anchors_metadata_and_content() -> None:
    baseline = json.loads(BASELINE_PATH.read_text(encoding="utf-8"))
    meta = json.loads((DATASETS_DIR / "meta.json").read_text(encoding="utf-8"))
    actual_digests = {
        split: hashlib.sha256((DATASETS_DIR / f"{split}.jsonl").read_bytes()).hexdigest()
        for split in ("train", "eval", "canaries")
    }

    assert baseline["schema_version"] == 1
    assert baseline["module"] == "should_review"
    assert baseline["dataset_version"] == meta["dataset_version"]
    assert baseline["split_digests"] == meta["split_digests"] == actual_digests
    assert baseline["dataset"]["records"] == EXPECTED_TOTAL
    assert baseline["dataset"]["duplicate_ids"] == 0
    assert baseline["dataset"]["cross_split_leaks"] == 0
    assert baseline["dataset"]["canaries"] == EXPECTED_COUNTS[Split.CANARIES]
    assert baseline["dataset"]["label_true"] + baseline["dataset"]["label_false"] == EXPECTED_TOTAL
    assert baseline["canaries"]["failed"] == 3
    assert baseline["canaries"]["taxonomy_coverage"] > 0
    assert 0.0 <= baseline["metric"]["eval"]["mean"] <= 1.0
