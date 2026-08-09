"""Split-aware dataset tests for the should_decompose example module.

Covers the v1 three-file splits (train/eval/canaries), canary exclusion
from train/eval, backward-compat fallback to ``example_pairs.jsonl``,
the ``meta.json`` dataset version, and engine consistency over all 260
records (the check_dataset_v1.py methodology, inlined).
"""

from __future__ import annotations

import asyncio
import json
import shutil
from pathlib import Path

from cambium.modules.example import (
    DatasetBundle,
    ExampleDatasetLoader,
    ShouldDecomposeModule,
    Split,
)
from cambium.modules.example.metric import evaluate_split

DATASETS_DIR = (
    Path(__file__).resolve().parents[2]
    / "src"
    / "cambium"
    / "modules"
    / "example"
    / "datasets"
)

EXPECTED_COUNTS = {Split.TRAIN: 200, Split.EVAL: 50, Split.CANARIES: 10}
EXAMPLE_PAIRS_COUNT = 9
EXAMPLE_PAIRS_CANARIES = 2


def _fresh_copy(tmp_path: Path) -> Path:
    dst = tmp_path / "datasets"
    shutil.copytree(DATASETS_DIR, dst)
    return dst


def test_split_loads_return_expected_counts() -> None:
    loader = ExampleDatasetLoader(DATASETS_DIR)
    for split, expected in EXPECTED_COUNTS.items():
        assert len(loader.load_split(split)) == expected


def test_load_all_bundle() -> None:
    loader = ExampleDatasetLoader(DATASETS_DIR)
    bundle = loader.load_all()
    assert isinstance(bundle, DatasetBundle)
    assert len(bundle.train) == EXPECTED_COUNTS[Split.TRAIN]
    assert len(bundle.eval) == EXPECTED_COUNTS[Split.EVAL]
    assert len(bundle.canaries) == EXPECTED_COUNTS[Split.CANARIES]
    assert bundle.dataset_version == "1.0.0"


def test_split_loader_accepts_split_file_path() -> None:
    file_loader = ExampleDatasetLoader(DATASETS_DIR / "train.jsonl")
    dir_loader = ExampleDatasetLoader(DATASETS_DIR)
    assert len(file_loader.load_split(Split.TRAIN)) == len(
        dir_loader.load_split(Split.TRAIN)
    )


def test_canaries_excluded_from_train_and_eval() -> None:
    loader = ExampleDatasetLoader(DATASETS_DIR)
    train = loader.load_split(Split.TRAIN)
    eval_ = loader.load_split(Split.EVAL)
    canaries = loader.load_split(Split.CANARIES)
    assert all(not ex.canary for ex in train)
    assert all(not ex.canary for ex in eval_)
    assert all(ex.canary for ex in canaries)


def test_canary_flag_filtered_from_train_file(tmp_path) -> None:
    src = tmp_path / "datasets"
    src.mkdir()
    normal = {
        "input": {"task": "Fix the typo.", "context": ""},
        "expected": {"decompose": False, "reason": "atomic"},
    }
    trap = {
        "input": {"task": "Trap record.", "context": ""},
        "expected": {"decompose": False, "reason": "trap"},
        "canary": True,
    }
    (src / "train.jsonl").write_text(
        json.dumps(normal) + "\n" + json.dumps(trap) + "\n", encoding="utf-8"
    )
    examples = ExampleDatasetLoader(src).load_split(Split.TRAIN)
    assert len(examples) == 1
    assert not examples[0].canary


def test_backward_compat_falls_back_to_example_pairs(tmp_path) -> None:
    src = _fresh_copy(tmp_path)
    for name in ("train.jsonl", "eval.jsonl", "canaries.jsonl"):
        (src / name).unlink()
    loader = ExampleDatasetLoader(src)
    train = loader.load_split(Split.TRAIN)
    eval_ = loader.load_split(Split.EVAL)
    assert len(train) == EXAMPLE_PAIRS_COUNT - EXAMPLE_PAIRS_CANARIES
    assert len(eval_) == EXAMPLE_PAIRS_COUNT - EXAMPLE_PAIRS_CANARIES
    assert all(not ex.canary for ex in train)
    assert all(not ex.canary for ex in eval_)
    canaries = loader.load_split(Split.CANARIES)
    assert len(canaries) == EXAMPLE_PAIRS_CANARIES
    assert all(ex.canary for ex in canaries)


def test_legacy_load_still_returns_all_examples() -> None:
    loader = ExampleDatasetLoader(DATASETS_DIR / "example_pairs.jsonl")
    examples = loader.load()
    assert len(examples) == EXAMPLE_PAIRS_COUNT
    assert sum(1 for ex in examples if ex.canary) == EXAMPLE_PAIRS_CANARIES


def test_dataset_version_read_from_meta(tmp_path) -> None:
    loader = ExampleDatasetLoader(_fresh_copy(tmp_path))
    assert loader.dataset_version == "1.0.0"


def test_dataset_version_defaults_when_meta_missing(tmp_path) -> None:
    src = _fresh_copy(tmp_path)
    (src / "meta.json").unlink()
    loader = ExampleDatasetLoader(src)
    assert loader.dataset_version == "0.1.0"


def test_all_260_records_score_perfectly() -> None:
    loader = ExampleDatasetLoader(DATASETS_DIR)
    module = ShouldDecomposeModule()

    async def score(examples: list) -> list[str]:
        bad = []
        for example in examples:
            prediction = await module.decide(example.input)
            if module.metric(example.with_prediction(prediction)) != 1.0:
                bad.append(example.input.task)
        return bad

    total = 0
    for split in EXPECTED_COUNTS:
        examples = loader.load_split(split)
        total += len(examples)
        bad = asyncio.run(score(examples))
        assert not bad, f"{split}: {len(bad)} engine mismatches: {bad[:3]}"
    assert total == 260


def test_evaluate_split_reports_mean_std_count() -> None:
    module = ShouldDecomposeModule()
    loader = ExampleDatasetLoader(DATASETS_DIR)
    result = evaluate_split(module, loader, Split.TRAIN)
    assert result["count"] == EXPECTED_COUNTS[Split.TRAIN]
    assert result["mean"] == 1.0
    assert result["std"] == 0.0
