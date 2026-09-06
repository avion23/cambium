"""Shared split-loader contract, exercised once by the reference module."""

from __future__ import annotations

import asyncio
import json
import shutil
from pathlib import Path

import pytest

from cambium.modules.base import DatasetError
from cambium.modules.example import ExampleDatasetLoader, ShouldDecomposeModule, Split

DATASETS_DIR = Path(__file__).resolve().parents[1] / "datasets"
EXPECTED_COUNTS = {Split.TRAIN: 200, Split.EVAL: 50, Split.CANARIES: 10}


def _fresh_copy(tmp_path: Path) -> Path:
    dst = tmp_path / "datasets"
    shutil.copytree(DATASETS_DIR, dst)
    return dst


def _record(record_id: str, *, task: str = "Do a thing.", canary: bool = False) -> dict:
    return {
        "id": record_id,
        "input": {"task": task, "context": ""},
        "expected": {"decompose": False, "reason": "atomic"},
        **({"canary": True} if canary else {}),
    }


def test_committed_splits_form_the_expected_bundle() -> None:
    loader = ExampleDatasetLoader(DATASETS_DIR)
    bundle = loader.load_all()

    assert loader.dataset_version == "1.1.0"
    assert {split: len(loader.load_split(split)) for split in EXPECTED_COUNTS} == EXPECTED_COUNTS
    assert len(bundle.train) == EXPECTED_COUNTS[Split.TRAIN]
    assert len(bundle.eval) == EXPECTED_COUNTS[Split.EVAL]
    assert len(bundle.canaries) == EXPECTED_COUNTS[Split.CANARIES]
    assert all(not example.canary for example in (*bundle.train, *bundle.eval))
    assert all(example.canary for example in bundle.canaries)


def test_canary_flag_is_excluded_from_training_split(tmp_path: Path) -> None:
    datasets = tmp_path / "datasets"
    datasets.mkdir()
    (datasets / "train.jsonl").write_text(
        json.dumps(_record("normal")) + "\n" + json.dumps(_record("trap", canary=True)) + "\n",
        encoding="utf-8",
    )

    examples = ExampleDatasetLoader(datasets).load_split(Split.TRAIN)

    assert [example.canary for example in examples] == [False]


def test_missing_split_is_rejected(tmp_path: Path) -> None:
    datasets = tmp_path / "datasets"
    datasets.mkdir()

    with pytest.raises(DatasetError, match="split file is missing"):
        ExampleDatasetLoader(datasets).load_split(Split.EVAL)


def test_record_version_drift_is_rejected(tmp_path: Path) -> None:
    datasets = tmp_path / "datasets"
    datasets.mkdir()
    (datasets / "meta.json").write_text(
        json.dumps({"schema_version": 1, "dataset_version": "1.1.0"}) + "\n",
        encoding="utf-8",
    )
    record = {
        **_record("train-1"),
        "schema_version": 999,
        "dataset_version": "0.0.0",
    }
    (datasets / "train.jsonl").write_text(json.dumps(record) + "\n", encoding="utf-8")

    with pytest.raises(DatasetError, match="version drift"):
        ExampleDatasetLoader(datasets).load_split(Split.TRAIN)


@pytest.mark.parametrize("constant", ["NaN", "Infinity", "-Infinity"])
def test_nonstandard_meta_constants_are_rejected(tmp_path: Path, constant: str) -> None:
    datasets = _fresh_copy(tmp_path)
    (datasets / "meta.json").write_text(f'{{"corrupt": {constant}}}\n', encoding="utf-8")

    with pytest.raises(DatasetError, match="invalid JSON"):
        ExampleDatasetLoader(datasets).load_all()


def test_meta_must_be_an_object(tmp_path: Path) -> None:
    datasets = _fresh_copy(tmp_path)
    (datasets / "meta.json").write_text("[]\n", encoding="utf-8")

    with pytest.raises(DatasetError, match="JSON object"):
        ExampleDatasetLoader(datasets).load_all()


def test_duplicate_ids_are_rejected(tmp_path: Path) -> None:
    datasets = tmp_path / "datasets"
    datasets.mkdir()
    record = json.dumps(_record("duplicate"))
    (datasets / "train.jsonl").write_text(f"{record}\n{record}\n", encoding="utf-8")

    with pytest.raises(DatasetError, match="duplicate id"):
        ExampleDatasetLoader(datasets).load_split(Split.TRAIN)


def test_split_records_require_ids(tmp_path: Path) -> None:
    datasets = tmp_path / "datasets"
    datasets.mkdir()
    record = _record("unused")
    record.pop("id")
    (datasets / "eval.jsonl").write_text(json.dumps(record) + "\n", encoding="utf-8")

    with pytest.raises(DatasetError, match="non-empty string 'id'"):
        ExampleDatasetLoader(datasets).load_split(Split.EVAL)


def test_cross_split_content_collision_is_rejected(tmp_path: Path) -> None:
    datasets = tmp_path / "datasets"
    datasets.mkdir()
    (datasets / "train.jsonl").write_text(
        json.dumps(_record("train", task="Same task")) + "\n", encoding="utf-8"
    )
    (datasets / "eval.jsonl").write_text(
        json.dumps(_record("eval", task="Same task")) + "\n", encoding="utf-8"
    )
    (datasets / "canaries.jsonl").write_text(
        json.dumps(_record("canary", task="Different task", canary=True)) + "\n",
        encoding="utf-8",
    )

    with pytest.raises(DatasetError, match="cross-split collision"):
        ExampleDatasetLoader(datasets).load_all()


def test_schema_errors_name_the_actual_split(tmp_path: Path) -> None:
    datasets = tmp_path / "datasets"
    datasets.mkdir()
    bad = _record("eval-1")
    bad["input"]["task"] = 42
    (datasets / "eval.jsonl").write_text(json.dumps(bad) + "\n", encoding="utf-8")

    with pytest.raises(DatasetError, match=r"eval\.jsonl.*input\.task must be a string"):
        ExampleDatasetLoader(datasets).load_split(Split.EVAL)


def test_reference_rule_engine_scores_the_committed_dataset() -> None:
    loader = ExampleDatasetLoader(DATASETS_DIR)
    module = ShouldDecomposeModule()

    async def mismatches() -> list[str]:
        bad: list[str] = []
        for split in EXPECTED_COUNTS:
            for example in loader.load_split(split):
                prediction = await module.decide(example.input)
                if module.metric(example.with_prediction(prediction)) != 1.0:
                    bad.append(example.input.task)
        return bad

    assert asyncio.run(mismatches()) == []
