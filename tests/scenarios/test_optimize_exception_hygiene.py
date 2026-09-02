from __future__ import annotations

import asyncio
from dataclasses import dataclass
from enum import StrEnum
from pathlib import Path
from types import SimpleNamespace

import dspy
import pytest

from cambium import optimize
from cambium.diffundo import DiffundoError
from cambium.modules.base import DatasetError, Example, ModuleContractError


class _Decision(StrEnum):
    DECOMPOSE = "decompose"
    DO_NOT_DECOMPOSE = "do_not_decompose"


@dataclass
class _TaskInput:
    task: str
    context: str


def _gold() -> Example:
    return Example(
        input=_TaskInput(task="Atomic task", context=""),
        expected={"decompose": _Decision.DO_NOT_DECOMPOSE, "reason": "atomic"},
    )


def _prediction() -> dict[str, str]:
    return {"decision": "do_not_decompose", "reason": "atomic"}


def test_load_program_class_wraps_import_error_but_propagates_type_error(monkeypatch) -> None:
    manifest = SimpleNamespace(dspy_program="package.program", module_name="example")

    def import_error(_target):
        raise ImportError("missing program")

    monkeypatch.setattr(optimize, "_import_target", import_error)
    with pytest.raises(optimize.OptimizeError, match="cannot import DSPy program"):
        optimize.load_program_class(manifest)

    def type_error(_target):
        raise TypeError("broken importer")

    monkeypatch.setattr(optimize, "_import_target", type_error)
    with pytest.raises(TypeError, match="broken importer"):
        optimize.load_program_class(manifest)


def test_field_and_domain_adapters_propagate_programming_errors(monkeypatch) -> None:
    class BrokenGetter:
        def get(self, _name, _default):
            raise TypeError("broken getter")

    with pytest.raises(TypeError, match="broken getter"):
        optimize._read_field(BrokenGetter(), "task")

    def type_error(_target):
        raise TypeError("broken domain importer")

    monkeypatch.setattr(optimize, "_import_target", type_error)
    with pytest.raises(TypeError, match="broken domain importer"):
        optimize._domain_module(object())

    class Program:
        pass

    with pytest.raises(TypeError, match="broken domain importer"):
        optimize._metric_function(Program())


def test_domain_import_fallbacks_handle_import_errors(monkeypatch) -> None:
    calls: list[str] = []
    fallback = object()

    def import_target(target):
        calls.append(target)
        if len(calls) == 1:
            raise ImportError("module unavailable")
        return fallback

    monkeypatch.setattr(optimize, "_import_target", import_target)
    assert optimize._domain_module(object()) is fallback
    assert calls[-1] == optimize._EXAMPLE_DECIDE_TARGET


def test_task_input_and_output_constructors_propagate_type_errors(monkeypatch) -> None:
    class BrokenTaskInput:
        def __init__(self, **_kwargs):
            raise TypeError("broken task input")

    with pytest.raises(TypeError, match="broken task input"):
        optimize._task_input({"task": "task", "context": ""}, BrokenTaskInput)

    class BrokenOutput:
        def __init__(self, **_kwargs):
            raise TypeError("broken prediction output")

    monkeypatch.setattr(
        optimize,
        "_domain_module",
        lambda _program: SimpleNamespace(Decision=_Decision, DecomposeOutput=BrokenOutput),
    )
    with pytest.raises(TypeError, match="broken prediction output"):
        optimize._prediction_output(_prediction(), object())


def test_prediction_json_failure_is_handled() -> None:
    assert optimize._prediction_output("not json", object()) is None


def test_metric_handles_value_error_but_propagates_type_error() -> None:
    class ValueErrorProgram:
        def metric(self, _example):
            raise ValueError("invalid score")

    metric = optimize.make_dspy_metric(ValueErrorProgram())
    assert metric(_gold(), _prediction()) == 0.0

    class TypeErrorProgram:
        def metric(self, _example):
            raise TypeError("broken metric")

    metric = optimize.make_dspy_metric(TypeErrorProgram())
    with pytest.raises(TypeError, match="broken metric"):
        metric(_gold(), _prediction())


def test_loader_import_and_data_failures_are_handled_but_type_errors_propagate(
    monkeypatch,
) -> None:
    class Loader:
        def load_split(self, _split):
            raise DatasetError("bad dataset")

    with pytest.raises(optimize.OptimizeError, match="could not load the train split"):
        optimize._load_split(Loader(), "TRAIN")
    with pytest.raises(optimize.OptimizeError, match="could not load the train split"):
        optimize.build_trainsets(Loader())

    class TypeErrorLoader:
        def load_split(self, _split):
            raise TypeError("broken dataset loader")

    with pytest.raises(TypeError, match="broken dataset loader"):
        optimize._load_split(TypeErrorLoader(), "TRAIN")
    with pytest.raises(TypeError, match="broken dataset loader"):
        optimize.build_trainsets(TypeErrorLoader())

    def import_type_error(_target):
        raise TypeError("broken split importer")

    monkeypatch.setattr(optimize, "_import_target", import_type_error)
    with pytest.raises(TypeError, match="broken split importer"):
        optimize._loader_split(object(), "TRAIN")


def test_transcript_loader_handles_dataset_error_but_propagates_type_error(tmp_path: Path) -> None:
    candidate_path = tmp_path / optimize._TRANSCRIPT_CANDIDATES_FILENAME
    candidate_path.write_text(
        '{"id": "approved", "candidate": true, "review_status": "approved", "redacted": true}\n',
        encoding="utf-8",
    )

    class DatasetFailureLoader:
        def __init__(self, path):
            self.datasets_dir = Path(path)

        def load(self):
            raise DatasetError("bad transcript dataset")

    with pytest.raises(optimize.OptimizeError, match="could not load transcript candidates"):
        optimize._load_transcript_candidates(DatasetFailureLoader(tmp_path))

    class TypeErrorLoader:
        def __init__(self, path):
            self.datasets_dir = Path(path)

        def load(self):
            raise TypeError("broken transcript loader")

    with pytest.raises(TypeError, match="broken transcript loader"):
        optimize._load_transcript_candidates(TypeErrorLoader(tmp_path))


def test_score_adapter_handles_parse_and_value_failures_but_not_type_errors() -> None:
    class ParseProgram:
        async def decide(self, _input):
            raise ValueError("response parse failed")

        def metric(self, _example):
            return 1.0

    assert asyncio.run(optimize._score_examples_async(ParseProgram(), [_gold()])) == [1.0]

    class UnexpectedValueProgram:
        async def decide(self, _input):
            raise ValueError("unexpected program failure")

        def metric(self, _example):
            return 1.0

    with pytest.raises(ValueError, match="unexpected program failure"):
        asyncio.run(optimize._score_examples_async(UnexpectedValueProgram(), [_gold()]))

    class TypeErrorProgram:
        async def decide(self, _input):
            raise TypeError("broken decision port")

        def metric(self, _example):
            return 1.0

    with pytest.raises(TypeError, match="broken decision port"):
        asyncio.run(optimize._score_examples_async(TypeErrorProgram(), [_gold()]))

    class ValueErrorMetricProgram:
        async def decide(self, _input):
            return {"decision": "do_not_decompose", "reason": "atomic"}

        def metric(self, _example):
            raise ValueError("bad metric input")

    assert asyncio.run(optimize._score_examples_async(ValueErrorMetricProgram(), [_gold()])) == [
        0.0
    ]

    class TypeErrorMetricProgram(ValueErrorMetricProgram):
        def metric(self, _example):
            raise TypeError("broken metric input")

    with pytest.raises(TypeError, match="broken metric input"):
        asyncio.run(optimize._score_examples_async(TypeErrorMetricProgram(), [_gold()]))


def test_bootstrap_handles_provider_errors_but_propagates_type_errors(monkeypatch) -> None:
    class Program:
        def forward(self, **_kwargs):
            return None

        def metric(self, _example):
            return 1.0

    class ProviderFailureOptimizer:
        def __init__(self, **_kwargs):
            pass

        def compile(self, _program, *, trainset):
            del trainset
            raise DiffundoError("provider failed")

    monkeypatch.setattr(optimize.dspy, "BootstrapFewShot", ProviderFailureOptimizer)
    with pytest.raises(optimize.OptimizeError, match="compilation failed"):
        optimize.run_stage_bootstrap(Program(), [], [])

    class TypeErrorOptimizer(ProviderFailureOptimizer):
        def compile(self, _program, *, trainset):
            del trainset
            raise TypeError("broken compiler")

    monkeypatch.setattr(optimize.dspy, "BootstrapFewShot", TypeErrorOptimizer)
    with pytest.raises(TypeError, match="broken compiler"):
        optimize.run_stage_bootstrap(Program(), [], [])


def test_bootstrap_forward_installation_propagates_attribute_error() -> None:
    predictor = dspy.Predict("task: str -> decision: str")

    class LockedProgram:
        def __init__(self):
            object.__setattr__(self, "predict", predictor)

        def __setattr__(self, _name, _value):
            raise AttributeError("locked program")

    with pytest.raises(AttributeError, match="locked program"):
        optimize._ensure_bootstrap_forward(LockedProgram())


def test_dataset_import_and_construction_errors_are_narrowed(monkeypatch, tmp_path: Path) -> None:
    manifest = SimpleNamespace(cli_module="cambium.modules.example", package_dir=tmp_path)

    def import_error(_target):
        raise ImportError("missing dataset")

    monkeypatch.setattr(optimize, "_import_target", import_error)
    with pytest.raises(optimize.OptimizeError, match="cannot import dataset module"):
        optimize._load_dataset_loader(manifest)

    def type_error(_target):
        raise TypeError("broken dataset importer")

    monkeypatch.setattr(optimize, "_import_target", type_error)
    with pytest.raises(TypeError, match="broken dataset importer"):
        optimize._load_dataset_loader(manifest)


def test_manifest_loader_handles_contract_error_but_propagates_type_error(monkeypatch) -> None:
    def contract_error(_path):
        raise ModuleContractError("bad manifest")

    monkeypatch.setattr(optimize, "load_module_manifest", contract_error)
    monkeypatch.setattr(optimize, "MODULES_DIR", Path("/nonexistent/cambium-modules"))
    with pytest.raises(ModuleContractError, match="bad manifest"):
        optimize._load_manifest("example")

    def type_error(_path):
        raise TypeError("broken manifest loader")

    monkeypatch.setattr(optimize, "load_module_manifest", type_error)
    with pytest.raises(TypeError, match="broken manifest loader"):
        optimize._load_manifest("example")


def test_provider_selection_handles_configuration_errors_but_not_type_errors(monkeypatch) -> None:
    import cambium.provider_config as provider_config

    def value_error():
        raise ValueError("bad provider configuration")

    monkeypatch.setattr(provider_config, "load_providers", value_error)
    with pytest.raises(optimize.OptimizeError, match="provider selection failed"):
        optimize._construct_lm("fast", 1.0, optimize._CostLedger(1.0))

    def type_error():
        raise TypeError("broken provider loader")

    monkeypatch.setattr(provider_config, "load_providers", type_error)
    with pytest.raises(TypeError, match="broken provider loader"):
        optimize._construct_lm("fast", 1.0, optimize._CostLedger(1.0))


def test_minimum_budget_gate_remains_fail_closed() -> None:
    with pytest.raises(optimize._BudgetExhausted, match=r"below the \$0.01 minimum"):
        optimize._CostLedger(0.009).check_available()


def test_public_cli_boundary_reports_unexpected_errors(monkeypatch, capsys) -> None:
    def fail(_name):
        raise RuntimeError("unexpected manifest failure")

    monkeypatch.setattr(optimize, "_load_manifest", fail)
    assert optimize.main(["example", "--dry-run"]) == 1
    assert (
        "cambium optimize: ERROR RuntimeError: unexpected manifest failure"
        in capsys.readouterr().err
    )
