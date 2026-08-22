"""Offline fast-tier scenarios for the DSPy optimizer spike."""

from __future__ import annotations

import asyncio
import importlib
import json
import shutil
from pathlib import Path
from types import SimpleNamespace
from typing import TYPE_CHECKING, Any, cast

import dspy  # type: ignore[import-untyped]
import pytest

from cambium import optimize
from cambium.modules.base import Example

if TYPE_CHECKING:
    from cambium.modules.example.dataset import Split as SplitType
    from cambium.modules.example.decide import DecomposeOutput as DecomposeOutputType
    from cambium.modules.example.decide import TaskInput as TaskInputType

_example_target = ".".join(("cambium", "modules", "example"))
_example = importlib.import_module(_example_target)
Decision = _example.Decision
DecomposeOutput = _example.DecomposeOutput
Split = _example.Split
TaskInput = _example.TaskInput
should_decompose_metric = _example.should_decompose_metric


class OfflineLM(dspy.LM):
    """A DSPy LM that returns a valid JSON completion without network I/O."""

    def __init__(self, decision: str = "do_not_decompose") -> None:
        super().__init__("offline/fake", cache=False, num_retries=0)
        self.decision = decision
        self.calls = 0

    def __call__(self, *args, **kwargs) -> list[dict[str, Any] | str]:
        del args, kwargs
        self.calls += 1
        return [
            json.dumps(
                {
                    "decision": self.decision,
                    "reason": "offline prediction",
                }
            )
        ]


def test_parser_defaults_to_fast_tier() -> None:
    args = optimize._parser().parse_args(["should_decompose"])

    assert args.tier == "fast"
    assert not args.include_transcript_candidates
    assert args.transcript_candidates is None


def test_parser_can_opt_in_to_transcript_candidates() -> None:
    args = optimize._parser().parse_args(
        ["should_decompose", "--include-transcript-candidates"]
    )

    assert args.include_transcript_candidates
    assert args.transcript_candidates is None


class OfflineProgram(dspy.Module):
    """Tiny real DSPy program used to exercise both optimizer stages."""

    name = "should_decompose"

    def __init__(self, lm: dspy.LM) -> None:
        super().__init__()
        self._lm = lm
        self.predict = dspy.Predict(
            "task: str, context: str -> decision: str, reason: str"
        )

    def forward(self, task: str, context: str = ""):
        with dspy.context(lm=self._lm):
            return self.predict(task=task, context=context)

    async def decide(self, input: TaskInputType) -> DecomposeOutputType:
        prediction = self.forward(input.task, input.context)
        decision = Decision.DO_NOT_DECOMPOSE
        try:
            decision = Decision(prediction.decision)
        except (AttributeError, ValueError):
            return DecomposeOutput(
                decision=Decision.DO_NOT_DECOMPOSE,
                reason="unparseable model output",
                confidence=0.0,
            )
        reason = prediction.reason if isinstance(prediction.reason, str) else ""
        return DecomposeOutput(decision=decision, reason=reason)

    def metric(self, example: Example) -> float:
        return should_decompose_metric(example)


class MemoryLoader:
    def __init__(self, train: list[Example], canaries: list[Example] | None = None) -> None:
        self._splits = {
            Split.TRAIN: list(train),
            Split.CANARIES: list(canaries or []),
        }

    def load_split(self, split: SplitType) -> list[Example]:
        return list(self._splits[split])


def _examples(count: int = 6) -> list[Example]:
    return [
        Example(
            input=TaskInput(task=f"Atomic task {index}", context=""),
            expected={
                "decompose": Decision.DO_NOT_DECOMPOSE,
                "reason": "atomic",
            },
        )
        for index in range(count)
    ]


def test_load_program_class_rejects_empty_manifest_field() -> None:
    manifest = SimpleNamespace(package_name="example", module_name="should_decompose")
    try:
        optimize.load_program_class(manifest)
    except optimize.OptimizeError as exc:
        assert "dspy_program" in str(exc)
    else:
        raise AssertionError("empty dspy_program must fail closed")


def test_make_dspy_metric_parses_matching_mismatching_and_bad_predictions() -> None:
    program = OfflineProgram(OfflineLM())
    metric = optimize.make_dspy_metric(program)
    gold = dspy.Example(
        task="atomic task",
        context="",
        decision="do_not_decompose",
        reason="atomic",
    ).with_inputs("task", "context")

    assert metric(gold, dspy.Prediction(decision="do_not_decompose", reason="ok")) == 1.0
    assert metric(gold, dspy.Prediction(decision="decompose", reason="wrong")) == 0.0
    assert metric(gold, dspy.Prediction(decision="not-a-decision", reason="bad")) == 0.0


def test_build_trainsets_is_deterministic_and_excludes_canaries() -> None:
    train = _examples(8)
    canary = Example(
        input=TaskInput(task="canary", context=""),
        expected={"decompose": Decision.DO_NOT_DECOMPOSE, "reason": "canary"},
        canary=True,
    )
    loader = MemoryLoader(train, [canary])

    first_train, first_val = optimize.build_trainsets(loader, seed=17)
    second_train, second_val = optimize.build_trainsets(loader, seed=17)

    assert [item.input.task for item in first_train] == [item.input.task for item in second_train]
    assert [item.input.task for item in first_val] == [item.input.task for item in second_val]
    assert {item.input.task for item in first_train}.isdisjoint(
        item.input.task for item in first_val
    )
    assert {item.input.task for item in first_train + first_val} == {
        item.input.task for item in train
    }
    assert all(not item.canary for item in first_train + first_val)


def test_run_stage_zero_completes_offline() -> None:
    program = OfflineProgram(OfflineLM())
    train = _examples(4)
    val = _examples(2)

    returned, report = optimize.run_stage_zero(program, train, val, seed=0)

    assert returned is program
    assert set(report) == {"eval_mean", "train_mean"}
    assert report["eval_mean"] == 1.0
    assert report["train_mean"] == 1.0


def test_run_stage_bootstrap_returns_working_compiled_program() -> None:
    program = OfflineProgram(OfflineLM())
    train = _examples(4)
    val = _examples(2)

    compiled, report = optimize.run_stage_bootstrap(program, train, val, seed=0)

    assert compiled is not None
    assert set(report) == {"eval_mean", "train_mean"}
    output = asyncio.run(
        cast(OfflineProgram, compiled).decide(TaskInput(task="new task", context=""))
    )
    assert isinstance(output, DecomposeOutput)
    assert output.decision is Decision.DO_NOT_DECOMPOSE


def _assert_single_artifact_set(artifact: Path) -> None:
    assert sorted(path.name for path in artifact.iterdir()) == [
        "lm.json",
        "program.json",
        "report.json",
    ]
    assert not (artifact / "current").exists()
    assert not any(path.name.startswith("v") for path in artifact.parent.iterdir())


def test_write_artifact_writes_single_artifact_set(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.chdir(tmp_path)
    program = OfflineProgram(OfflineLM())
    lm = OfflineLM()

    artifact = optimize.write_artifact(
        "should_decompose",
        program,
        lm,
        {"gate_passed": True, "eval_mean": 1.0},
    )

    assert artifact == Path("optimized/should_decompose")
    assert (artifact / "program.json").is_file()
    assert (artifact / "report.json").is_file()
    assert json.loads((artifact / "program.json").read_text())
    _assert_single_artifact_set(artifact)


def test_second_write_replaces_artifact_set_in_place(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.chdir(tmp_path)

    optimize.write_artifact(
        "should_decompose",
        OfflineProgram(OfflineLM()),
        OfflineLM(),
        {"gate_passed": True},
    )

    artifact = optimize.write_artifact(
        "should_decompose",
        OfflineProgram(OfflineLM()),
        OfflineLM(),
        {"gate_passed": False, "eval_mean": 0.5},
    )

    assert artifact == Path("optimized/should_decompose")
    assert json.loads((artifact / "report.json").read_text()) == {
        "gate_passed": False,
        "eval_mean": 0.5,
    }
    _assert_single_artifact_set(artifact)


def test_main_budget_exhausted_run_writes_report_into_artifact_set(
    tmp_path: Path, monkeypatch
) -> None:
    monkeypatch.chdir(tmp_path)
    manifest = SimpleNamespace(module_name="should_decompose")
    loader = object()

    monkeypatch.setattr(optimize, "_load_manifest", lambda _name: manifest)
    monkeypatch.setattr(optimize, "load_program_class", lambda _manifest: OfflineProgram)
    monkeypatch.setattr(optimize, "_load_dataset_loader", lambda _manifest: loader)
    monkeypatch.setattr(optimize, "_baseline_means", lambda _manifest: {
        "train": 1.0,
        "eval": 1.0,
        "canaries": 1.0,
    })
    monkeypatch.setattr(optimize, "_construct_lm", lambda *_args: OfflineLM())
    monkeypatch.setattr(optimize, "build_trainsets", lambda _loader, seed: ([], []))

    def exhaust_budget(*_args, **_kwargs):
        raise optimize._BudgetExhausted("offline budget exhausted")

    monkeypatch.setattr(optimize, "run_stage_zero", exhaust_budget)

    artifact = optimize.write_artifact(
        "should_decompose",
        OfflineProgram(OfflineLM()),
        OfflineLM(),
        {"gate_passed": True},
    )

    assert optimize.main(["should_decompose", "--budget-usd", "0"]) == 1
    report = json.loads((artifact / "report.json").read_text())
    assert report["gate_passed"] is False
    assert report["budget_exhausted"] is True
    _assert_single_artifact_set(artifact)


def test_missing_transcript_candidates_fail_only_when_opted_in(tmp_path: Path) -> None:
    source = (
        Path(__file__).resolve().parents[2]
        / "src"
        / "cambium"
        / "modules"
        / "example"
        / "datasets"
    )
    datasets = tmp_path / "datasets"
    shutil.copytree(source, datasets)
    (datasets / "transcript_candidates.jsonl").unlink()
    loader = optimize._import_target("cambium.modules.example.dataset").ExampleDatasetLoader(
        datasets
    )

    train, validation = optimize.build_trainsets(loader, seed=17)
    assert len(train) == 160
    assert len(validation) == 40

    with pytest.raises(optimize.OptimizeError, match="transcript candidate file is missing"):
        optimize._augment_training_pool(loader, train, train + validation)


def test_transcript_candidates_are_deduplicated_and_frozen_splits_are_unchanged(
    tmp_path: Path,
) -> None:
    source = (
        Path(__file__).resolve().parents[2]
        / "src"
        / "cambium"
        / "modules"
        / "example"
        / "datasets"
    )
    datasets = tmp_path / "datasets"
    shutil.copytree(source, datasets)
    candidate_path = datasets / "transcript_candidates.jsonl"
    approved_records = []
    for line in candidate_path.read_text(encoding="utf-8").splitlines():
        record = json.loads(line)
        record["review_status"] = "approved"
        record["notes"] = "reviewed; approved_for_training"
        approved_records.append(record)
    extra = {
        "id": "test-candidate-unique",
        "candidate": True,
        "review_status": "approved",
        "redacted": True,
        "input": {
            "task": "Synthetic candidate only",
            "context": "candidate context",
        },
        "expected": {"decompose": False, "reason": "test candidate"},
    }
    approved_records.extend(
        [
            extra,
            {**extra, "id": "test-candidate-duplicate"},
        ]
    )
    candidate_path.write_text(
        "".join(json.dumps(record) + "\n" for record in approved_records),
        encoding="utf-8",
    )

    loader = optimize._import_target(
        "cambium.modules.example.dataset"
    ).ExampleDatasetLoader(datasets)
    frozen = (
        loader.load_split(Split.TRAIN)
        + loader.load_split(Split.EVAL)
        + loader.load_split(Split.CANARIES)
    )
    train, validation = optimize.build_trainsets(loader, seed=17)

    augmented, counts = optimize._augment_training_pool(loader, train, frozen)

    assert counts == {
        "loaded": 25,
        "included": 13,
        "excluded": 12,
        "excluded_frozen": 11,
        "excluded_duplicates": 1,
    }
    assert len(augmented) == len(train) + counts["included"]
    assert len(validation) == 40
    frozen_pairs = {(item.input.task, item.input.context) for item in frozen}
    augmented_pairs = {(item.input.task, item.input.context) for item in augmented}
    assert augmented_pairs.isdisjoint(
        {
            (item.input.task, item.input.context)
            for item in loader.load_split(Split.EVAL)
        }
    )
    assert augmented_pairs.isdisjoint(
        {
            (item.input.task, item.input.context)
            for item in loader.load_split(Split.CANARIES)
        }
    )
    assert sum(
        item.input.task == "Synthetic candidate only" for item in augmented
    ) == 1
    assert all(
        (item.input.task, item.input.context) in frozen_pairs
        for item in validation
    )

def test_baseline_means_reads_all_three_splits() -> None:
    manifest = SimpleNamespace(
        package_dir=Path("src/cambium/modules/example"),
    )

    means = optimize._baseline_means(manifest)

    assert set(means) == {"train", "eval", "canaries"}
    assert all(0.0 <= value <= 1.0 for value in means.values())


def test_baseline_means_rejects_dataset_digest_drift(tmp_path: Path) -> None:
    source = Path(__file__).resolve().parents[2] / "src" / "cambium" / "modules" / "example"
    package_dir = tmp_path / "example"
    shutil.copytree(source, package_dir)
    (package_dir / "datasets" / "train.jsonl").write_text(
        (package_dir / "datasets" / "train.jsonl").read_text(encoding="utf-8")
        + "\n",
        encoding="utf-8",
    )
    manifest = SimpleNamespace(
        package_dir=package_dir,
        module_name="should_decompose",
        dataset_schema_version=1,
    )

    with pytest.raises(optimize.OptimizeError, match="split_digests.train"):
        optimize._baseline_means(manifest)


def test_tracking_diffundo_uses_remaining_budget() -> None:
    class Delegate:
        def __init__(self) -> None:
            self.budgets: list[float] = []

        async def call(self, *_args, **kwargs):
            self.budgets.append(kwargs["budget_usd"])
            return SimpleNamespace(estimated_cost_usd=0.4)

    delegate = Delegate()
    ledger = optimize._CostLedger(1.0)
    tracked = optimize._TrackingDiffundo(delegate, ledger)

    asyncio.run(tracked.call(optimize.ProviderTier.FAST, {}, budget_usd=1.0))
    asyncio.run(tracked.call(optimize.ProviderTier.FAST, {}, budget_usd=1.0))

    assert delegate.budgets == [1.0, 0.6]
    assert ledger.spent_usd == 0.8


def test_load_dataset_loader_uses_module_datasets_directory() -> None:
    package_dir = Path(__file__).resolve().parents[2] / "src" / "cambium" / "modules" / "example"
    manifest = optimize.load_module_manifest(package_dir)

    loader = cast(Any, optimize._load_dataset_loader(manifest))

    assert loader.path == package_dir / "datasets"
    assert loader.load_split(Split.TRAIN)


def test_anti_reward_gap_rewards_honest_candidates() -> None:
    final = {"eval_mean": 1.0, "train_mean": 1.0}
    canaries = {"mean": 1.0}
    baseline = {"train": 1.0, "eval": 1.0, "canaries": 1.0}

    assert optimize._anti_reward_gap(final, canaries, baseline) == 0.0
    assert optimize._anti_reward_gap(final, canaries, None) is None
    assert optimize._anti_reward_gap(None, canaries, baseline) is None
    assert optimize._anti_reward_gap(final, None, baseline) is None


def test_main_dry_run_does_not_construct_an_lm(monkeypatch) -> None:
    def fail_constructor(*args, **kwargs):
        raise AssertionError("dry-run constructed an LM")

    monkeypatch.setattr(optimize, "CambiumLM", fail_constructor)
    assert (
        optimize.main(
            [
                "--dry-run",
                "should_decompose",
                "--optimizer",
                "bootstrap",
                "--budget-usd",
                "1.00",
            ]
        )
        == 0
    )


def test_main_tiny_budget_fails_without_crashing() -> None:
    result = optimize.main(
        ["should_decompose", "--optimizer", "zero", "--budget-usd", "0.000001"]
    )
    assert result != 0
