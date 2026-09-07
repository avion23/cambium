"""Should-review invariants not covered by the shared module contracts."""

from __future__ import annotations

from pathlib import Path

import pytest

from cambium.modules.base import DatasetError
from cambium.modules.should_review import Decision, ExampleDatasetLoader, Split
from cambium.modules.should_review.decide import should_review

DATASETS_DIR = Path(__file__).resolve().parents[1] / "datasets"


def test_loader_maps_review_label_to_domain_decision() -> None:
    example = ExampleDatasetLoader(DATASETS_DIR).load_split(Split.TRAIN)[0]

    assert isinstance(example.expected["review"], Decision)
    assert isinstance(example.expected["decompose"], bool)


def test_decompose_mirror_must_agree_with_review(tmp_path: Path) -> None:
    bad = tmp_path / "bad_mirror.jsonl"
    bad.write_text(
        '{"input": {"task": "Do the thing.", "context": ""}, '
        '"expected": {"review": true, "decompose": false, "reason": "drift"}}\n'
    )

    with pytest.raises(DatasetError, match="decompose must mirror"):
        ExampleDatasetLoader(bad).load()


def test_engine_tolerates_leading_separators() -> None:
    result = should_review("; hello world", "")

    assert result.decision is Decision.DO_NOT_REVIEW
