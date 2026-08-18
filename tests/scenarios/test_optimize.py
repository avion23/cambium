"""Offline fast-tier scenarios for the DSPy optimizer spike."""

from __future__ import annotations

import asyncio
import importlib
import json
from pathlib import Path
from types import SimpleNamespace

import dspy

from cambium import optimize
from cambium.modules.base import Example

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

    def __call__(self, *args, **kwargs):
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

    async def decide(self, input: TaskInput) -> DecomposeOutput:
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

    def load_split(self, split: Split) -> list[Example]:
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
    output = asyncio.run(compiled.decide(TaskInput(task="new task", context="")))
    assert isinstance(output, DecomposeOutput)
    assert output.decision is Decision.DO_NOT_DECOMPOSE


def test_write_artifact_writes_state_and_current_link(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.chdir(tmp_path)
    program = OfflineProgram(OfflineLM())
    lm = OfflineLM()

    version_dir = optimize.write_artifact(
        "should_decompose",
        1,
        program,
        lm,
        {"gate_passed": False, "eval_mean": 0.0},
    )

    assert version_dir == Path("optimized/should_decompose/v1")
    assert (version_dir / "program.json").is_file()
    assert (version_dir / "lm.json").is_file()
    assert (version_dir / "report.json").is_file()
    assert json.loads((version_dir / "program.json").read_text())
    current = Path("optimized/should_decompose/current")
    assert current.is_symlink()
    assert current.resolve() == version_dir.resolve()


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
