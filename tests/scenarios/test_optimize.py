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
from cambium.modules.should_review.decide import should_review

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


class ReviewRuleLM(dspy.LM):
    """Offline LM that mirrors the packaged review labels without network I/O."""

    def __init__(self) -> None:
        super().__init__("offline/review-rule", cache=False, num_retries=0)
        self.calls = 0

    def __call__(self, *args, **kwargs) -> list[dict[str, Any] | str]:
        del args
        self.calls += 1
        content = kwargs["messages"][-1]["content"]
        task = content.split("[[ ## task ## ]]\n", 1)[1].split(
            "\n\n[[ ## context ## ]]", 1
        )[0]
        context = content.split("[[ ## context ## ]]\n", 1)[1].split(
            "\n\nRespond", 1
        )[0]
        output = should_review(task, context)
        return [
            json.dumps(
                {
                    "decision": output.decision.value,
                    "reason": output.reason,
                }
            )
        ]

    async def acall(self, *args, **kwargs) -> list[dict[str, Any] | str]:
        return self(*args, **kwargs)


def test_parser_defaults_to_fast_tier() -> None:
    args = optimize._parser().parse_args(["should_decompose"])

    assert args.tier == "fast"
    assert not args.include_transcript_candidates
    assert args.transcript_candidates is None


def test_parser_can_opt_in_to_transcript_candidates() -> None:
    args = optimize._parser().parse_args(["should_decompose", "--include-transcript-candidates"])

    assert args.include_transcript_candidates
    assert args.transcript_candidates is None


class OfflineProgram(dspy.Module):
    """Tiny real DSPy program used to exercise both optimizer stages."""

    name = "should_decompose"

    def __init__(self, lm: dspy.LM) -> None:
        super().__init__()
        self._lm = lm
        self.predict = dspy.Predict("task: str, context: str -> decision: str, reason: str")

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


class ParseFailureProgram(OfflineProgram):
    def __init__(
        self, lm: dspy.LM, parse_failure_tasks: set[str] | None = None
    ) -> None:
        super().__init__(lm)
        self.parse_failure_tasks = parse_failure_tasks

    async def decide(self, input: TaskInputType) -> DecomposeOutputType:
        if (
            self.parse_failure_tasks is not None
            and input.task not in self.parse_failure_tasks
        ):
            return await super().decide(input)
        return DecomposeOutput(
            decision=Decision.DO_NOT_DECOMPOSE,
            reason=optimize.PARSE_FAILURE_REASON,
            confidence=0.0,
        )


class MemoryLoader:
    def __init__(
        self,
        train: list[Example],
        canaries: list[Example] | None = None,
        eval_examples: list[Example] | None = None,
    ) -> None:
        self._splits = {
            Split.TRAIN: list(train),
            Split.EVAL: list(eval_examples or []),
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


def _eval_manifest() -> SimpleNamespace:
    return SimpleNamespace(
        package_name="example",
        module_name="should_decompose",
        dspy_program="cambium.modules.example.dspy_program",
    )


def _patch_eval(
    monkeypatch, loader: MemoryLoader, artifact_root: Path | None = None
) -> None:
    if artifact_root is not None:
        monkeypatch.setattr(optimize, "_ARTIFACT_ROOT", artifact_root)
    monkeypatch.setattr(optimize, "_load_manifest", lambda _name: _eval_manifest())
    monkeypatch.setattr(optimize, "load_program_class", lambda _manifest: OfflineProgram)
    monkeypatch.setattr(
        optimize,
        "_load_dataset_loader",
        lambda _manifest, _dataset_path: loader,
    )
    monkeypatch.setattr(optimize, "_construct_lm", lambda *_args: OfflineLM())


def test_eval_fresh_module_scores_every_split(monkeypatch, tmp_path: Path, capsys) -> None:
    monkeypatch.chdir(tmp_path)
    loader = MemoryLoader(_examples(2), _examples(1), _examples(3))
    _patch_eval(monkeypatch, loader, tmp_path / "optimized")

    assert (
        optimize.main(
            [
                "eval",
                "should_decompose",
                "--dataset",
                str(tmp_path / "reviewed-dataset"),
                "--json",
            ]
        )
        == 0
    )

    report = json.loads(capsys.readouterr().out)
    assert report["program"] == "fresh"
    assert set(report["splits"]) == {"train", "eval", "canaries"}
    assert [report["splits"][split]["count"] for split in ("train", "eval", "canaries")] == [
        2,
        3,
        1,
    ]
    assert all(
        outcome["score"] == 1.0
        for split in report["splits"].values()
        for outcome in split["records"]
    )


def test_eval_loads_saved_program_state(monkeypatch, tmp_path: Path, capsys) -> None:
    loader = MemoryLoader(_examples(1), _examples(1), _examples(1))
    _patch_eval(monkeypatch, loader, tmp_path / "optimized")
    program_dir = tmp_path / "optimized" / "should_decompose"
    program_dir.mkdir(parents=True)
    program_dir.joinpath("program.json").write_text(
        json.dumps(OfflineProgram(OfflineLM()).dump_state()),
        encoding="utf-8",
    )
    loaded_states: list[dict] = []
    original_load_state = OfflineProgram.load_state

    def observe_load_state(program, state):
        loaded_states.append(state)
        return original_load_state(program, state)

    monkeypatch.setattr(OfflineProgram, "load_state", observe_load_state)

    assert (
        optimize.main(
            [
                "eval",
                "should_decompose",
                "--dataset",
                str(tmp_path / "reviewed-dataset"),
                "--program-dir",
                str(program_dir),
                "--json",
            ]
        )
        == 0
    )

    report = json.loads(capsys.readouterr().out)
    assert report["program"] == "optimized"
    assert loaded_states and loaded_states[0]


def test_eval_json_shape_is_stable(monkeypatch, tmp_path: Path, capsys) -> None:
    monkeypatch.chdir(tmp_path)
    _patch_eval(
        monkeypatch,
        MemoryLoader(_examples(1), _examples(1), _examples(1)),
        tmp_path / "optimized",
    )

    assert (
        optimize.main(
            [
                "eval",
                "should_decompose",
                "--dataset",
                str(tmp_path / "reviewed-dataset"),
                "--json",
            ]
        )
        == 0
    )

    report = json.loads(capsys.readouterr().out)
    assert set(report) == {"dataset", "module", "parse_failures", "program", "splits"}
    assert all(
        set(summary) == {
            "count",
            "mean",
            "parse_failures",
            "records",
            "scored_count",
            "std",
        }
        for summary in report["splits"].values()
    )
    assert set(report["splits"]["train"]["records"][0]) == {
        "index",
        "parse_failure",
        "score",
    }


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
    assert set(report) == {
        "eval_mean",
        "eval_parse_failures",
        "train_mean",
        "train_parse_failures",
    }
    assert report["eval_mean"] == 1.0
    assert report["train_mean"] == 1.0


def test_run_stage_bootstrap_returns_working_compiled_program() -> None:
    program = OfflineProgram(OfflineLM())
    train = _examples(4)
    val = _examples(2)

    compiled, report = optimize.run_stage_bootstrap(program, train, val, seed=0)

    assert compiled is not None
    assert set(report) == {
        "eval_mean",
        "eval_parse_failures",
        "train_mean",
        "train_parse_failures",
    }
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
    monkeypatch.setattr(optimize, "_ARTIFACT_ROOT", tmp_path / "optimized")
    program = OfflineProgram(OfflineLM())
    lm = OfflineLM()

    artifact = optimize.write_artifact(
        "should_decompose",
        program,
        lm,
        {"gate_passed": True, "eval_mean": 1.0},
    )

    assert artifact == tmp_path / "optimized" / "should_decompose"
    assert (artifact / "program.json").is_file()
    assert (artifact / "report.json").is_file()
    assert json.loads((artifact / "program.json").read_text())
    _assert_single_artifact_set(artifact)


def test_second_write_replaces_artifact_set_in_place(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(optimize, "_ARTIFACT_ROOT", tmp_path / "optimized")

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

    assert artifact == tmp_path / "optimized" / "should_decompose"
    assert json.loads((artifact / "report.json").read_text()) == {
        "gate_passed": False,
        "eval_mean": 0.5,
    }
    _assert_single_artifact_set(artifact)


def test_main_budget_exhausted_run_writes_report_into_artifact_set(
    tmp_path: Path, monkeypatch
) -> None:
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(optimize, "_ARTIFACT_ROOT", tmp_path / "optimized")
    manifest = SimpleNamespace(module_name="should_decompose")
    loader = object()

    monkeypatch.setattr(optimize, "_load_manifest", lambda _name: manifest)
    monkeypatch.setattr(optimize, "load_program_class", lambda _manifest: OfflineProgram)
    monkeypatch.setattr(optimize, "_load_dataset_loader", lambda _manifest: loader)
    monkeypatch.setattr(
        optimize,
        "_baseline_means",
        lambda _manifest: {
            "train": 1.0,
            "eval": 1.0,
            "canaries": 1.0,
        },
    )
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
        Path(__file__).resolve().parents[2] / "src" / "cambium" / "modules" / "example" / "datasets"
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
        Path(__file__).resolve().parents[2] / "src" / "cambium" / "modules" / "example" / "datasets"
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

    loader = optimize._import_target("cambium.modules.example.dataset").ExampleDatasetLoader(
        datasets
    )
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
        {(item.input.task, item.input.context) for item in loader.load_split(Split.EVAL)}
    )
    assert augmented_pairs.isdisjoint(
        {(item.input.task, item.input.context) for item in loader.load_split(Split.CANARIES)}
    )
    assert sum(item.input.task == "Synthetic candidate only" for item in augmented) == 1
    assert all((item.input.task, item.input.context) in frozen_pairs for item in validation)


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
        (package_dir / "datasets" / "train.jsonl").read_text(encoding="utf-8") + "\n",
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
    tracked = optimize._TrackingDiffundo(cast(optimize.Diffundo, delegate), ledger)

    asyncio.run(tracked.call(optimize.ProviderTier.FAST, {}, budget_usd=1.0))
    asyncio.run(tracked.call(optimize.ProviderTier.FAST, {}, budget_usd=1.0))

    assert delegate.budgets == [1.0, 0.6]
    assert ledger.spent_usd == 0.8


def test_tracking_diffundo_reports_usage_when_subscription_cost_is_zero() -> None:
    class Delegate:
        _providers = (
            SimpleNamespace(
                name="subscription",
                billing_mode=SimpleNamespace(value="subscription"),
                pricing_known=True,
            ),
        )

        async def call(self, *_args, **_kwargs):
            return SimpleNamespace(
                provider="subscription",
                estimated_cost_usd=0.0,
                usage={
                    "prompt_tokens": 12,
                    "completion_tokens": 5,
                    "cached_tokens": 3,
                },
            )

    ledger = optimize._CostLedger(1.0)
    tracked = optimize._TrackingDiffundo(cast(optimize.Diffundo, Delegate()), ledger)

    asyncio.run(tracked.call(optimize.ProviderTier.FAST, {}))

    assert ledger.spent_usd == 0.0
    assert ledger.usage == {
        "calls": 1,
        "prompt_tokens": 12,
        "completion_tokens": 5,
        "cached_tokens": 3,
        "total_tokens": 17,
    }
    assert ledger.price_source == "subscription"


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


def test_should_review_zero_optimizer_reports_rule_baseline_without_promoting(
    monkeypatch, tmp_path: Path
) -> None:
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(optimize, "_ARTIFACT_ROOT", tmp_path / "optimized")
    lm = ReviewRuleLM()
    monkeypatch.setattr(optimize, "_construct_lm", lambda *_args: lm)

    result = optimize.main(["should_review", "--optimizer", "zero", "--budget-usd", "0.10"])

    assert result == 1
    assert lm.calls > 0
    report = json.loads((tmp_path / "optimized" / "should_review" / "report.json").read_text())
    assert set(report["stage_zero"]) == {
        "eval_mean",
        "eval_parse_failures",
        "train_mean",
        "train_parse_failures",
    }
    assert 0.0 <= report["stage_zero"]["train_mean"] <= 1.0
    assert 0.0 <= report["stage_zero"]["eval_mean"] < 0.85
    assert report["stage_zero"]["train_parse_failures"] == 0
    assert report["stage_zero"]["eval_parse_failures"] == 0
    canaries = report["canaries"]
    assert canaries["count"] == 6
    assert canaries["mean"] == pytest.approx(0.5)
    assert canaries["parse_failures"] == 0
    assert canaries["scored_count"] == 6
    assert report["gate_passed"] is False


def test_parse_failures_are_excluded_from_aggregate_means() -> None:
    summary = optimize.score_split(ParseFailureProgram(OfflineLM()), _examples(1))

    assert summary == {
        "count": 1,
        "mean": 0.0,
        "parse_failures": 1,
        "scored_count": 0,
        "std": 0.0,
    }


def test_eval_mean_excludes_parse_failures_and_reports_bucket() -> None:
    summary = optimize.score_split(
        ParseFailureProgram(OfflineLM(), {"Atomic task 1"}), _examples(2)
    )

    assert summary["mean"] == 1.0
    assert summary["count"] == 2
    assert summary["scored_count"] == 1
    assert summary["parse_failures"] == 1


def test_main_tiny_budget_fails_without_crashing() -> None:
    result = optimize.main(["should_decompose", "--optimizer", "zero", "--budget-usd", "0.000001"])
    assert result != 0


def test_gepa_is_available_in_parser_and_dry_run(capsys) -> None:
    args = optimize._parser().parse_args(["should_decompose", "--optimizer", "gepa"])

    assert args.optimizer == "gepa"
    assert optimize.main(
        ["--dry-run", "should_decompose", "--optimizer", "gepa", "--seed", "23"]
    ) == 0
    assert "optimizer=gepa" in capsys.readouterr().err


def test_run_stage_gepa_requires_four_reviewed_records() -> None:
    with pytest.raises(optimize.OptimizeError, match="GEPA requires more reviewed data"):
        optimize.run_stage_gepa(OfflineProgram(OfflineLM()), _examples(2), [], seed=7)


def test_run_stage_gepa_uses_same_reflection_lm_and_reports_scores(monkeypatch) -> None:
    seen: dict[str, Any] = {}

    class FakeGEPA:
        def __init__(self, **kwargs: Any) -> None:
            seen.update(kwargs)

        def compile(self, student, *, trainset, valset):
            assert len(trainset) == 4
            assert len(valset) == 2
            student.detailed_results = SimpleNamespace(
                total_metric_calls=9,
                candidates=[object(), object(), object()],
                num_full_val_evals=2,
            )
            return student

    monkeypatch.setattr(optimize.dspy, "GEPA", FakeGEPA)
    lm = OfflineLM()
    compiled, report = optimize.run_stage_gepa(
        OfflineProgram(lm),
        _examples(4),
        _examples(2),
        seed=19,
        budget_usd=0.20,
    )

    assert compiled is not None
    assert seen["reflection_lm"] is lm
    assert seen["max_metric_calls"] == 20
    assert report["eval_mean"] == 1.0
    assert report["train_mean"] == 1.0
    assert report["calls"] == 9
    assert report["iterations"] == 2
    assert report["full_evals"] == 2


def test_gepa_report_schema_records_stage_and_optimizer() -> None:
    args = SimpleNamespace(
        module_name="should_decompose",
        optimizer="gepa",
        seed=3,
        tier="fast",
        budget_usd=1.0,
    )
    report = optimize._partial_report(
        SimpleNamespace(module_name="should_decompose"),
        cast(Any, args),
        optimize._CostLedger(1.0),
        stage_gepa={"eval_mean": 0.9, "train_mean": 1.0},
    )

    assert report["optimizer"] == "gepa"
    assert report["stage_gepa"] == {"eval_mean": 0.9, "train_mean": 1.0}


def test_gepa_budget_enforcement_raises_before_optimizer() -> None:
    ledger = optimize._CostLedger(0.10)
    ledger.spent_usd = 0.10

    with pytest.raises(optimize._BudgetExhausted, match="optimization budget exhausted"):
        optimize.run_stage_gepa(
            OfflineProgram(OfflineLM()),
            _examples(4),
            _examples(2),
            seed=0,
            budget_usd=0.10,
            ledger=ledger,
            reflection_lm=OfflineLM(),
        )
