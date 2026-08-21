"""DSPy hill-climbing driver for Cambium decision modules.

This is a harness boundary.  Decision packages are discovered from their
manifest and their DSPy program is imported from the manifest's dotted module
path.  The first spike has two stages: a zero-shot measurement and
``BootstrapFewShot`` compilation.
"""

from __future__ import annotations

import argparse
import asyncio
import importlib
import json
import math
import os
import random
import statistics
import sys
import tempfile
import uuid
from collections.abc import Callable, Mapping
from pathlib import Path
from types import MethodType
from typing import Any, cast

import dspy

from cambium import module_conformance
from cambium.diffundo import Diffundo, DiffundoError, ProviderTier
from cambium.jlens import JlenClient, JlenError, render_messages
from cambium.lm import CambiumLM
from cambium.modules.base import (
    DatasetError,
    Example,
    ModuleContractError,
    load_module_manifest,
)

_DSPY_EXCEPTIONS: Any = importlib.import_module("dspy.utils.exceptions")
AdapterParseError = cast(type[Exception], _DSPY_EXCEPTIONS.__dict__["AdapterParseError"])
DSPyError = cast(type[Exception], _DSPY_EXCEPTIONS.__dict__["DSPyError"])

MODULES_DIR = module_conformance.MODULES_DIR

_MISSING = object()
_MODULES_PREFIX = ".".join(("cambium", "modules"))
_EXAMPLE_DATASET_TARGET = ".".join((_MODULES_PREFIX, "example", "dataset"))
_EXAMPLE_DECIDE_TARGET = ".".join((_MODULES_PREFIX, "example", "decide"))
_EXAMPLE_METRIC_TARGET = ".".join((_MODULES_PREFIX, "example", "metric"))
_TRANSCRIPT_CANDIDATES_FILENAME = "transcript_candidates.jsonl"


class OptimizeError(ValueError):
    """Raised when an optimization run cannot satisfy its harness contract."""


class _BudgetExhausted(OptimizeError):
    """Raised before a provider call when the cumulative budget is spent."""


_MIN_CALL_BUDGET_USD = 0.01


class _CostLedger:
    """Small per-run cost ledger used by the Diffundo adapter."""

    def __init__(self, budget_usd: float) -> None:
        self.budget_usd = budget_usd
        self.spent_usd = 0.0

    @property
    def remaining_usd(self) -> float:
        return max(0.0, self.budget_usd - self.spent_usd)

    def check_available(self) -> None:
        if self.budget_usd < _MIN_CALL_BUDGET_USD:
            raise _BudgetExhausted(
                f"optimization budget ${self.budget_usd:.6f} is below the "
                f"${_MIN_CALL_BUDGET_USD:.2f} minimum for one provider call"
            )
        if self.spent_usd >= self.budget_usd:
            raise _BudgetExhausted(
                f"optimization budget exhausted: spent ${self.spent_usd:.6f} "
                f"of ${self.budget_usd:.6f}"
            )

    def record(self, value: object) -> None:
        if isinstance(value, bool) or not isinstance(value, (int, float)):
            return
        if math.isfinite(value) and value >= 0:
            self.spent_usd += float(value)


def _import_target(target: str) -> Any:
    """Import a dotted target without presenting a static package import."""
    importer = importlib.import_module
    return importer(target)


class _TrackingDiffundo(Diffundo):
    """Delegate Diffundo calls while recording ``CallResult`` costs.

    The subclass keeps :class:`CambiumLM`'s state serializer on its normal
    ``Diffundo`` path.  The delegated object remains the owner of provider
    state and credentials.
    """

    def __init__(self, delegate: Diffundo, ledger: _CostLedger) -> None:
        self._delegate = delegate
        self._ledger = ledger

    def __getattr__(self, name: str) -> Any:
        return getattr(self._delegate, name)

    def __copy__(self) -> _TrackingDiffundo:
        return self

    def __deepcopy__(self, memo: dict[int, Any]) -> _TrackingDiffundo:
        del memo
        return self

    async def call(
        self,
        tier: ProviderTier,
        prompt: dict[str, Any],
        *,
        model: str | None = None,
        budget_usd: float | None = None,
    ) -> Any:
        self._ledger.check_available()
        result = await self._delegate.call(
            tier,
            prompt,
            model=model,
            budget_usd=budget_usd,
        )
        self._ledger.record(getattr(result, "estimated_cost_usd", 0.0))
        return result


def _program_class_name(module_name: str) -> str:
    """Return the conventional DSPy class name for a logical module name."""
    if not isinstance(module_name, str) or not module_name.strip():
        raise OptimizeError("manifest module_name must be a non-empty string")
    pieces = [piece for piece in module_name.split("_") if piece]
    if not pieces:
        raise OptimizeError(f"cannot derive a DSPy class name from {module_name!r}")
    return "".join(piece[:1].upper() + piece[1:] for piece in pieces) + "ModuleDSPy"


def load_program_class(manifest) -> type:
    """Load the manifest-selected DSPy program class.

    The manifest value is data, not a Python import statement.  Keeping it in
    ``target`` before calling :func:`importlib.import_module` also preserves
    the module conformance boundary.
    """
    target = getattr(manifest, "dspy_program", "")
    if not isinstance(target, str) or not target.strip():
        raise OptimizeError(
            f"module {getattr(manifest, 'package_name', '<unknown>')!r}: "
            "manifest field 'dspy_program' is required for optimization"
        )
    target = target.strip()
    try:
        mod = _import_target(target)
    except ImportError as exc:
        raise OptimizeError(
            f"cannot import DSPy program module {target!r}: {type(exc).__name__}: {exc}"
        ) from exc

    class_name = _program_class_name(getattr(manifest, "module_name", ""))
    program_class = getattr(mod, class_name, None)
    if not isinstance(program_class, type):
        raise OptimizeError(
            f"DSPy program module {target!r} has no class {class_name!r}"
        )
    return program_class


def _read_field(value: object, name: str) -> object:
    if isinstance(value, Mapping):
        return value.get(name, _MISSING)
    getter = getattr(value, "get", None)
    if callable(getter):
        result = getter(name, _MISSING)
        if result is not _MISSING:
            return result
    try:
        return getattr(value, name)
    except AttributeError:
        return _MISSING


def _program_package(program: object) -> str:
    module_name = getattr(type(program), "__module__", "")
    if isinstance(module_name, str) and module_name.startswith(f"{_MODULES_PREFIX}."):
        return module_name.rsplit(".", 1)[0]
    return ".".join((_MODULES_PREFIX, "example"))


def _domain_module(program: object):
    target = f"{_program_package(program)}.decide"
    try:
        return _import_target(target)
    except ImportError:
        target = _EXAMPLE_DECIDE_TARGET
        return _import_target(target)


def _metric_function(program: object) -> Callable[[Example], float]:
    metric = getattr(program, "metric", None)
    if callable(metric):
        return metric

    target = f"{_program_package(program)}.metric"
    try:
        mod = _import_target(target)
    except ImportError:
        target = _EXAMPLE_METRIC_TARGET
        mod = _import_target(target)
    metric = getattr(mod, "should_decompose_metric", None)
    if not callable(metric):
        raise OptimizeError(f"metric module {target!r} has no usable metric")
    return metric


def _parse_decision(raw: object, decision_type: type) -> object | None:
    members = tuple(decision_type)
    if isinstance(raw, decision_type):
        return raw
    if isinstance(raw, bool):
        for member in members:
            if getattr(member, "value", None) == ("decompose" if raw else "do_not_decompose"):
                return member
        return None
    if not isinstance(raw, str):
        return None
    normalized = raw.strip().lower().replace("-", "_").replace(" ", "_")
    for member in members:
        value = getattr(member, "value", None)
        name = getattr(member, "name", "").lower()
        if normalized == value or normalized == name:
            return member
    return None


def _task_input(value: object, task_input_type: type) -> object | None:
    if isinstance(value, task_input_type):
        return value
    task = _read_field(value, "task")
    context = _read_field(value, "context")
    if not isinstance(task, str):
        return None
    if context is _MISSING:
        context = ""
    if not isinstance(context, str):
        return None
    return task_input_type(task=task, context=context)


def _prediction_output(raw: object, program: object) -> object | None:
    domain = _domain_module(program)
    decision_type = getattr(domain, "Decision", None)
    output_type = getattr(domain, "DecomposeOutput", None)
    if not isinstance(decision_type, type) or not isinstance(output_type, type):
        return None

    if isinstance(raw, output_type):
        value = raw
        decision = _parse_decision(getattr(value, "decision", _MISSING), decision_type)
        reason = getattr(value, "reason", _MISSING)
        confidence = getattr(value, "confidence", 1.0)
    else:
        if isinstance(raw, str):
            try:
                raw = json.loads(raw)
            except json.JSONDecodeError:
                return None
        if isinstance(raw, list) and len(raw) == 1:
            raw = raw[0]
        decision = _parse_decision(
            _read_field(raw, "decision"), decision_type
        )
        if decision is None:
            decision = _parse_decision(_read_field(raw, "decompose"), decision_type)
        reason = _read_field(raw, "reason")
        confidence = _read_field(raw, "confidence")
        if confidence is _MISSING:
            confidence = 1.0

    if decision is None:
        return None
    if reason is _MISSING:
        reason = ""
    if not isinstance(reason, str):
        return None
    if isinstance(confidence, bool) or not isinstance(confidence, (int, float)):
        return None
    if not math.isfinite(confidence) or not 0.0 <= confidence <= 1.0:
        return None
    return output_type(decision=decision, reason=reason, confidence=float(confidence))


def _fallback_prediction(program: object) -> object:
    domain = _domain_module(program)
    decision_type = getattr(domain, "Decision", None)
    output_type = getattr(domain, "DecomposeOutput", None)
    if not isinstance(decision_type, type) or not isinstance(output_type, type):
        raise OptimizeError("decision module does not expose Decision and DecomposeOutput")
    decision = _parse_decision("do_not_decompose", decision_type)
    if decision is None:
        raise OptimizeError("decision module has no DO_NOT_DECOMPOSE decision")
    return output_type(
        decision=decision,
        reason="unparseable model output",
        confidence=0.0,
    )


def _gold_parts(gold: object, program: object) -> tuple[object, dict[str, object]] | None:
    domain = _domain_module(program)
    task_input_type = getattr(domain, "TaskInput", None)
    decision_type = getattr(domain, "Decision", None)
    if not isinstance(task_input_type, type) or not isinstance(decision_type, type):
        return None

    if isinstance(gold, Example):
        input_value = _task_input(gold.input, task_input_type)
        expected_source = gold.expected
        raw_decision = expected_source.get("decompose", _MISSING)
        reason = expected_source.get("reason", "")
    else:
        input_value = _task_input(gold, task_input_type)
        raw_decision = _read_field(gold, "decision")
        if raw_decision is _MISSING:
            raw_decision = _read_field(gold, "decompose")
        reason = _read_field(gold, "reason")
        if reason is _MISSING:
            reason = ""

    decision = _parse_decision(raw_decision, decision_type)
    if input_value is None or decision is None or not isinstance(reason, str):
        return None
    return input_value, {"decompose": decision, "reason": reason}


def _jlens_client_from_env() -> JlenClient | None:
    """Build a jlens score client from CAMBIUM_JLENS_URL, or None if unset."""
    base_url = os.environ.get("CAMBIUM_JLENS_URL", "").strip()
    if not base_url:
        return None
    layers = None
    raw_layers = os.environ.get("CAMBIUM_JLENS_LAYERS", "").strip()
    if raw_layers:
        parsed = []
        for part in raw_layers.split(","):
            part = part.strip()
            if not part.isdigit():
                parsed = []
                break
            parsed.append(int(part))
        layers = parsed or None
    return JlenClient(base_url, layers=layers)


def _decision_strings(expected: Mapping[str, object]) -> tuple[list[str], list[str]]:
    """Render the expected decision values and their enum siblings as strings."""
    expected_strings: list[str] = []
    for value in expected.values():
        if isinstance(value, str):
            expected_strings.append(value)
            continue
        rendered = getattr(value, "value", None)
        if isinstance(rendered, str):
            expected_strings.append(rendered)
    alt_strings: list[str] = []
    for value in expected.values():
        decision_type = type(value)
        members = getattr(decision_type, "__members__", None)
        if not isinstance(members, Mapping):
            continue
        for member in members.values():
            rendered = getattr(member, "value", None)
            if (
                isinstance(rendered, str)
                and rendered not in expected_strings
                and rendered not in alt_strings
            ):
                alt_strings.append(rendered)
    return expected_strings, alt_strings


def _fuse_jlens(
    jlens_client: JlenClient,
    expected: Mapping[str, object],
    trace: object,
    score: float,
    weight: float,
) -> float:
    """Blend the exact-match score with the jlens readout signal.

    The trace records every LM call as ``(predictor, inputs, pred)``; the
    predictor's signature and current demos plus the call inputs reconstruct
    the exact messages the LM saw.  The jlens signal is the normalized rank of
    the expected decision token in the model's internal readout at the last
    prompt position, averaged over the requested layers.  Any jlens failure
    leaves the exact-match score unchanged.
    """
    if not isinstance(trace, (list, tuple)) or not trace:
        return score
    expected_strings, alt_strings = _decision_strings(expected)
    if not expected_strings:
        return score
    for predictor, inputs, _ in reversed(trace):
        if not hasattr(predictor, "signature"):
            continue
        try:
            messages = render_messages(predictor, inputs)
            result = jlens_client.score(messages, expected_strings, alt_strings)
            jlens_score = jlens_client.signal(result, expected_strings)
        except (JlenError, TypeError, ValueError, OSError):
            return score
        if not isinstance(jlens_score, (int, float)) or not math.isfinite(jlens_score):
            return score
        return weight * max(0.0, min(1.0, jlens_score)) + (1.0 - weight) * score
    return score


def make_dspy_metric(program, jlens_client: JlenClient | None = None) -> Callable:
    """Adapt a DSPy metric call to the Cambium metric.

    The adapter returns a fraction in ``[0, 1]``.  DSPy's ``Evaluate`` displays
    that value as a percentage in its report, so the driver compares the
    adapter's normalized fraction rather than the displayed percentage.

    When ``jlens_client`` is given (or ``CAMBIUM_JLENS_URL`` is set), the
    exact-match score is blended with the jlens readout signal on the LM call
    trace so that internally committed but textually loose predictions still
    score above zero.
    """
    scorer = _metric_function(program)
    if jlens_client is None:
        jlens_client = _jlens_client_from_env()
    jlens_weight = 0.5
    raw_weight = os.environ.get("CAMBIUM_JLENS_WEIGHT", "").strip()
    if raw_weight:
        try:
            jlens_weight = float(raw_weight)
        except ValueError:
            jlens_weight = 0.5
    if not math.isfinite(jlens_weight) or not 0.0 <= jlens_weight <= 1.0:
        jlens_weight = 0.5

    def metric(
        gold: object,
        pred: object,
        trace: object = None,
        pred_name: object = None,
        pred_trace: object = None,
        program_trace: object = None,
    ) -> float:
        del pred_name, pred_trace, program_trace
        try:
            parts = _gold_parts(gold, program)
            prediction = _prediction_output(pred, program)
            if parts is None or prediction is None:
                return 0.0
            input_value, expected = parts
            example = Example(
                input=input_value,
                expected=expected,
                prediction=prediction,
            )
            score = scorer(example)
            if isinstance(score, bool) or not isinstance(score, (int, float)):
                return 0.0
            if not math.isfinite(score) or not 0.0 <= score <= 1.0:
                return 0.0
            score = float(score)
            if jlens_client is not None:
                score = _fuse_jlens(jlens_client, expected, trace, score, jlens_weight)
            return score
        except (ImportError, KeyError, ValueError):
            return 0.0

    return metric


def _loader_split(loader: object, name: str) -> object:
    module_name = getattr(type(loader), "__module__", "")
    split_type = None
    if isinstance(module_name, str) and module_name:
        try:
            target = module_name
            split_type = getattr(_import_target(target), "Split", None)
        except ImportError:
            split_type = None
    if not isinstance(split_type, type):
        target = _EXAMPLE_DATASET_TARGET
        split_type = getattr(_import_target(target), "Split", None)
    split = getattr(split_type, name, None)
    if split is None:
        raise OptimizeError(f"dataset loader has no Split.{name}")
    return split


def _load_split(loader: object, name: str) -> list[Example]:
    load_split = getattr(loader, "load_split", None)
    if not callable(load_split):
        raise OptimizeError("dataset loader does not provide load_split")
    try:
        return list(load_split(_loader_split(loader, name)))
    except OptimizeError:
        raise
    except (DatasetError, ImportError, OSError, KeyError, json.JSONDecodeError) as exc:
        raise OptimizeError(f"could not load the {name.lower()} split: {exc}") from exc


def _loader_datasets_dir(loader: object) -> Path:
    """Return the directory containing a loader's dataset files."""
    datasets_dir = getattr(loader, "datasets_dir", _MISSING)
    if datasets_dir is not _MISSING:
        try:
            return Path(datasets_dir)
        except TypeError as exc:
            raise OptimizeError("dataset loader has an invalid datasets_dir") from exc

    path = getattr(loader, "path", _MISSING)
    if path is _MISSING:
        raise OptimizeError("dataset loader does not expose a dataset path")
    try:
        dataset_path = Path(path)
    except TypeError as exc:
        raise OptimizeError("dataset loader has an invalid dataset path") from exc
    return dataset_path if dataset_path.is_dir() else dataset_path.parent


def _load_transcript_candidates(loader: object) -> list[Example]:
    """Load optional transcript candidates with the module's DatasetLoader."""
    candidate_path = _loader_datasets_dir(loader) / _TRANSCRIPT_CANDIDATES_FILENAME
    if not candidate_path.is_file():
        raise OptimizeError(
            f"transcript candidate file is missing for this module: {candidate_path}"
        )

    loader_class = type(loader)
    try:
        candidate_loader = loader_class(candidate_path)
    except (OSError, ValueError) as exc:
        raise OptimizeError(
            f"could not construct the dataset loader for transcript candidates: {exc}"
        ) from exc
    load = getattr(candidate_loader, "load", None)
    if not callable(load):
        raise OptimizeError("dataset loader does not provide load for transcript candidates")
    try:
        candidates = list(load())
    except (DatasetError, ImportError, OSError, KeyError, json.JSONDecodeError) as exc:
        raise OptimizeError(f"could not load transcript candidates: {exc}") from exc
    if any(getattr(candidate, "canary", False) for candidate in candidates):
        raise OptimizeError("transcript candidate file contains a canary record")
    return candidates


def _canonical_input_pair(example: object) -> tuple[str, str]:
    """Return the exact canonical ``(task, context)`` pair for one example."""
    input_value = getattr(example, "input", _MISSING)
    task = _read_field(input_value, "task")
    context = _read_field(input_value, "context")
    if not isinstance(task, str) or not isinstance(context, str):
        raise OptimizeError("dataset example input must contain task and context strings")
    return task, context


def _augment_training_pool(
    loader: object,
    train_examples: list[Example],
    frozen_examples: list[Example],
    candidates: list[Example] | None = None,
) -> tuple[list[Example], dict[str, int]]:
    """Add non-overlapping transcript candidates to the training pool only."""
    frozen_pairs = {_canonical_input_pair(example) for example in frozen_examples}
    if candidates is None:
        candidates = _load_transcript_candidates(loader)
    candidate_pairs: set[tuple[str, str]] = set()
    included: list[Example] = []
    excluded_frozen = 0
    excluded_duplicates = 0
    for candidate in candidates:
        pair = _canonical_input_pair(candidate)
        if pair in frozen_pairs:
            excluded_frozen += 1
            continue
        if pair in candidate_pairs:
            excluded_duplicates += 1
            continue
        candidate_pairs.add(pair)
        included.append(candidate)

    counts = {
        "loaded": len(candidates),
        "included": len(included),
        "excluded": excluded_frozen + excluded_duplicates,
        "excluded_frozen": excluded_frozen,
        "excluded_duplicates": excluded_duplicates,
    }
    return [*train_examples, *included], counts


def build_trainsets(loader, seed=0, val_fraction=0.2) -> tuple[list, list]:
    """Load train records and deterministically carve a validation subset."""
    if isinstance(val_fraction, bool) or not isinstance(val_fraction, (int, float)):
        raise OptimizeError("val_fraction must be a number in [0, 1)")
    if not math.isfinite(val_fraction) or not 0.0 <= val_fraction < 1.0:
        raise OptimizeError("val_fraction must be a number in [0, 1)")
    load_split = getattr(loader, "load_split", None)
    if not callable(load_split):
        raise OptimizeError("dataset loader does not provide load_split")
    try:
        examples = list(load_split(_loader_split(loader, "TRAIN")))
    except OptimizeError:
        raise
    except (DatasetError, ImportError, OSError, KeyError, json.JSONDecodeError) as exc:
        raise OptimizeError(f"could not load the train split: {exc}") from exc
    if any(getattr(example, "canary", False) for example in examples):
        raise OptimizeError("train split contains a canary record")

    shuffled = list(examples)
    random.Random(seed).shuffle(shuffled)
    if not shuffled or val_fraction == 0:
        return shuffled, []
    requested = max(1, math.ceil(len(shuffled) * val_fraction))
    val_count = min(requested, max(0, len(shuffled) - 1))
    return shuffled[val_count:], shuffled[:val_count]


def _is_parse_failure(exc: Exception) -> bool:
    name = type(exc).__name__.lower()
    detail = str(exc).lower()
    return "parse" in name or "jsondecode" in name or "parse" in detail


async def _score_examples_async(program: object, examples: list[Example]) -> list[float]:
    scorer = _metric_function(program)
    scores: list[float] = []
    for example in examples:
        try:
            raw_prediction = await program.decide(example.input)
        except _BudgetExhausted:
            raise
        except (AdapterParseError, ValueError) as exc:
            if not _is_parse_failure(exc):
                raise
            raw_prediction = None
        prediction = _prediction_output(raw_prediction, program)
        if prediction is None:
            prediction = _fallback_prediction(program)
        try:
            score = scorer(example.with_prediction(prediction))
        except (KeyError, ValueError):
            score = 0.0
        if isinstance(score, bool) or not isinstance(score, (int, float)):
            score = 0.0
        if not math.isfinite(score) or not 0.0 <= score <= 1.0:
            score = 0.0
        scores.append(float(score))
    return scores


def score_split(program, examples) -> dict:
    """Run and score one split through the program's async decision port."""
    records = list(examples)
    scores = asyncio.run(_score_examples_async(program, records))
    if not scores:
        return {"mean": 0.0, "std": 0.0, "count": 0}
    return {
        "mean": float(statistics.fmean(scores)),
        "std": float(statistics.pstdev(scores)),
        "count": len(scores),
    }


def run_stage_zero(program, train_examples, val_examples, seed=0) -> tuple[object, dict]:
    """Measure the supplied program without adding demonstrations."""
    del seed
    eval_score = score_split(program, list(val_examples))
    train_score = score_split(program, list(train_examples))
    return program, {
        "eval_mean": eval_score["mean"],
        "train_mean": train_score["mean"],
    }


def _to_dspy_example(example: Example, program: object) -> dspy.Example:
    domain = _domain_module(program)
    decision_type = getattr(domain, "Decision", None)
    if not isinstance(decision_type, type):
        raise OptimizeError("decision module does not expose Decision")
    input_value = example.input
    task = _read_field(input_value, "task")
    context = _read_field(input_value, "context")
    if not isinstance(task, str) or not isinstance(context, str):
        raise OptimizeError("train example input must contain task and context strings")
    raw_decision = example.expected.get("decompose", _MISSING)
    decision = _parse_decision(raw_decision, decision_type)
    reason = example.expected.get("reason", "")
    if decision is None or not isinstance(reason, str):
        raise OptimizeError("train example expected value is not parseable")
    return dspy.Example(
        task=task,
        context=context,
        decision=decision.value,
        reason=reason,
    ).with_inputs("task", "context")


def _ensure_bootstrap_forward(program: object) -> None:
    """Give a decide-only DSPy program the synchronous optimizer seam."""
    if callable(getattr(program, "forward", None)):
        return
    predictor_name: str | None = None
    for name, value in vars(program).items():
        if isinstance(value, dspy.Predict):
            predictor_name = name
            break
    if predictor_name is None:
        raise OptimizeError("DSPy program has no forward method or Predict field")

    def forward(instance: object, **kwargs: object) -> object:
        predictor = getattr(instance, predictor_name)
        lm = getattr(instance, "_lm", None)
        with dspy.context(lm=lm):
            return predictor(**kwargs)

    program.forward = MethodType(forward, program)


def run_stage_bootstrap(program, train_examples, val_examples, seed=0) -> tuple[object, dict]:
    """Compile up to four bootstrapped and eight labeled demonstrations."""
    _ensure_bootstrap_forward(program)
    try:
        optimizer = dspy.BootstrapFewShot(
            metric=make_dspy_metric(program),
            max_bootstrapped_demos=4,
            max_labeled_demos=8,
            max_rounds=1,
        )
    except (DiffundoError, DSPyError) as exc:
        raise OptimizeError(f"could not create BootstrapFewShot: {exc}") from exc

    trainset = [_to_dspy_example(example, program) for example in train_examples]
    state = random.getstate()
    random.seed(seed)
    try:
        try:
            compiled = optimizer.compile(program, trainset=trainset)
        except _BudgetExhausted:
            raise
        except (DiffundoError, DSPyError) as exc:
            raise OptimizeError(f"BootstrapFewShot compilation failed: {exc}") from exc
    finally:
        random.setstate(state)

    if compiled is None:
        raise OptimizeError("BootstrapFewShot returned no compiled program")
    eval_score = score_split(compiled, list(val_examples))
    train_score = score_split(compiled, list(train_examples))
    return compiled, {
        "eval_mean": eval_score["mean"],
        "train_mean": train_score["mean"],
    }


def _atomic_json_write(path: Path, value: object) -> None:
    temporary_path: Path | None = None
    try:
        with tempfile.NamedTemporaryFile(
            mode="w",
            encoding="utf-8",
            dir=path.parent,
            prefix=f".{path.name}-",
            suffix=".tmp",
            delete=False,
        ) as temporary:
            temporary_path = Path(temporary.name)
            json.dump(value, temporary, ensure_ascii=False, indent=2, sort_keys=True)
            temporary.write("\n")
            temporary.flush()
            os.fsync(temporary.fileno())
        os.replace(temporary_path, path)
    except BaseException:
        if temporary_path is not None:
            temporary_path.unlink(missing_ok=True)
        raise


def _safe_component(value: object, label: str) -> str:
    text = str(value)
    if not text or text in {".", ".."} or Path(text).name != text:
        raise OptimizeError(f"{label} must be one path component")
    return text


def write_artifact(
    module_name,
    version,
    program,
    lm,
    report,
    *,
    promote: bool = False,
) -> Path:
    """Persist program state and report, optionally promoting the artifact."""
    if not isinstance(promote, bool):
        raise OptimizeError("promote must be a boolean")
    module_component = _safe_component(module_name, "module_name")
    version_component = _safe_component(version, "version")
    module_root = Path("optimized") / module_component
    version_dir = module_root / f"v{version_component}"
    version_dir.mkdir(parents=True, exist_ok=True)

    program_dump = getattr(program, "dump_state", None)
    lm_dump = getattr(lm, "dump_state", None)
    if not callable(program_dump) or not callable(lm_dump):
        raise OptimizeError("program and LM must provide dump_state()")
    _atomic_json_write(version_dir / "program.json", program_dump())
    _atomic_json_write(version_dir / "lm.json", lm_dump())
    _atomic_json_write(version_dir / "report.json", report)

    approved = promote and isinstance(report, Mapping) and report.get("gate_passed") is True
    if not approved:
        return version_dir

    current = module_root / "current"
    temporary_link = module_root / f".current-{uuid.uuid4().hex}"
    try:
        os.symlink(version_dir.name, temporary_link)
        os.replace(temporary_link, current)
    except BaseException:
        temporary_link.unlink(missing_ok=True)
        raise
    return version_dir


def _load_dataset_loader(manifest: object) -> object:
    package_target = getattr(manifest, "cli_module", "")
    if not isinstance(package_target, str) or not package_target:
        raise OptimizeError("manifest cli_module is required to load the dataset")
    target = f"{package_target}.dataset"
    try:
        dataset_module = _import_target(target)
    except ImportError as exc:
        raise OptimizeError(f"cannot import dataset module {target!r}: {exc}") from exc

    candidates: list[type] = []
    for name in dir(dataset_module):
        candidate = getattr(dataset_module, name, None)
        if isinstance(candidate, type) and name.endswith("DatasetLoader"):
            candidates.append(candidate)
    if not candidates:
        raise OptimizeError(f"dataset module {target!r} has no DatasetLoader class")
    loader_class = next(
        (candidate for candidate in candidates if candidate.__name__ == "ExampleDatasetLoader"),
        candidates[0],
    )
    try:
        return loader_class(Path(manifest.package_dir) / "datasets")
    except (OSError, ValueError) as exc:
        raise OptimizeError(f"could not construct dataset loader: {exc}") from exc


def _load_manifest(module_name: str) -> object:
    """Load by package directory, then by the manifest's logical name."""
    package_dir = MODULES_DIR / module_name
    try:
        return load_module_manifest(package_dir)
    except (ModuleContractError, OSError) as first_error:
        if not MODULES_DIR.is_dir():
            raise first_error
        for candidate in sorted(MODULES_DIR.iterdir(), key=lambda path: path.name):
            if not candidate.is_dir() or candidate == package_dir:
                continue
            manifest_path = candidate / "module.json"
            if not manifest_path.is_file():
                continue
            try:
                manifest = load_module_manifest(candidate)
            except (ModuleContractError, OSError):
                continue
            if manifest.module_name == module_name:
                return manifest
        raise first_error


def _baseline_means(manifest: object) -> dict[str, float]:
    path = Path(manifest.package_dir) / "tests" / "baselines" / "baseline.json"
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, KeyError, TypeError, ValueError, json.JSONDecodeError) as exc:
        raise OptimizeError(f"cannot read baseline {path}: {exc}") from exc
    means: dict[str, float] = {}
    for split in ("train", "eval", "canaries"):
        try:
            value = data["metric"][split]["mean"]
        except (KeyError, TypeError) as exc:
            raise OptimizeError(f"baseline {path} has no metric.{split}.mean") from exc
        if isinstance(value, bool) or not isinstance(value, (int, float)):
            raise OptimizeError(f"baseline {path} metric.{split}.mean is not numeric")
        if not math.isfinite(value) or not 0.0 <= value <= 1.0:
            raise OptimizeError(f"baseline {path} metric.{split}.mean is outside [0, 1]")
        means[split] = float(value)
    return means


def _next_version(module_name: str) -> int:
    root = Path("optimized") / module_name
    versions: list[int] = []
    if root.is_dir():
        for path in root.iterdir():
            if path.is_dir() and path.name.startswith("v"):
                try:
                    versions.append(int(path.name[1:]))
                except ValueError:
                    continue
    return max(versions, default=0) + 1


def _construct_lm(tier_name: str, budget_usd: float, ledger: _CostLedger) -> Any:
    from cambium.diffundo import CredentialSource
    from cambium.provider_config import AuthMode, load_providers, select_provider

    try:
        tier = ProviderTier(tier_name)
    except ValueError as exc:
        raise OptimizeError(f"unsupported LM tier {tier_name!r}") from exc
    try:
        providers = load_providers()
        selected = select_provider(providers, tier=tier)
    except (OSError, ValueError) as exc:
        raise OptimizeError(f"provider selection failed: {exc}") from exc

    options: dict[str, Any] = {"primary_provider": selected.name}
    if selected.auth is AuthMode.CODEX_CHATGPT:
        from cambium.oauth import OAuthStore

        document = OAuthStore().read_document(selected.name)
        if document is None:
            raise OptimizeError(f"provider {selected.name!r} has no stored OAuth session")
        options["credential_source"] = CredentialSource(
            access_token=document.access_token,
            account_id=document.account_id,
        )

    router = Diffundo(providers, **options)
    tracked = _TrackingDiffundo(router, ledger)
    return CambiumLM(
        tracked,
        tier,
        model=selected.model or None,
        budget_usd=budget_usd,
    )


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="python -m cambium.optimize",
        description="Run the Cambium DSPy optimizer spike.",
    )
    parser.add_argument("module_name")
    parser.add_argument("--optimizer", choices=("zero", "bootstrap"), default="zero")
    parser.add_argument("--budget-usd", type=float, default=2.0)
    parser.add_argument("--seed", type=int, default=0)
    parser.add_argument("--tier", default="fast")
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument(
        "--include-transcript-candidates",
        action="store_true",
        help="explicitly add reviewed transcript candidates to the training pool",
    )
    return parser


def _partial_report(
    manifest: object,
    args: argparse.Namespace,
    ledger: _CostLedger,
    *,
    baseline_means: dict[str, float] | None = None,
    stage_zero: dict | None = None,
    stage_bootstrap: dict | None = None,
    final: dict | None = None,
    canaries: dict | None = None,
    budget_exhausted: bool = False,
    transcript_candidates: dict[str, int] | None = None,
) -> dict[str, Any]:
    report = {
        "module": getattr(manifest, "module_name", args.module_name),
        "optimizer": args.optimizer,
        "seed": args.seed,
        "tier": args.tier,
        "budget_usd": args.budget_usd,
        "spent_usd": ledger.spent_usd,
        "baseline": baseline_means,
        "stage_zero": stage_zero,
        "stage_bootstrap": stage_bootstrap,
        "final": final,
        "canaries": canaries,
        "budget_exhausted": budget_exhausted,
        "gate_passed": False,
    }
    if transcript_candidates is not None:
        report["transcript_candidates"] = transcript_candidates
    return report


def _anti_reward_gap(
    final: dict | None,
    canaries: dict | None,
    baseline_means: dict[str, float] | None,
) -> float | None:
    if final is None or canaries is None or baseline_means is None:
        return None
    try:
        train_gain = final["train_mean"] - baseline_means["train"]
        canary_gain = canaries["mean"] - baseline_means["canaries"]
    except (KeyError, TypeError):
        return None
    return float(train_gain - canary_gain)


def _run(argv=None) -> int:
    """Run one optimization plan and return its process exit code."""
    args = _parser().parse_args(sys.argv[1:] if argv is None else argv)
    if isinstance(args.budget_usd, bool) or not math.isfinite(args.budget_usd):
        print("cambium optimize: --budget-usd must be finite", file=sys.stderr)
        return 2
    if args.budget_usd < 0:
        print("cambium optimize: --budget-usd must be non-negative", file=sys.stderr)
        return 2

    try:
        manifest = _load_manifest(args.module_name)
    except (ModuleContractError, OSError) as exc:
        print(f"cambium optimize: manifest load failed: {exc}", file=sys.stderr)
        return 1

    if args.dry_run:
        target = getattr(manifest, "dspy_program", "")
        print(
            f"cambium optimize: plan module={args.module_name} optimizer={args.optimizer} "
            f"budget_usd={args.budget_usd:.2f} tier={args.tier} seed={args.seed}",
            file=sys.stderr,
        )
        print(
            f"cambium optimize: dspy_program={target or '<manifest field unavailable>'}; "
            "dry-run; no LM constructed",
            file=sys.stderr,
        )
        return 0

    program: object | None = None
    ledger = _CostLedger(args.budget_usd)
    baseline_means: dict[str, float] | None = None
    stage_zero: dict | None = None
    stage_bootstrap: dict | None = None
    final: dict | None = None
    canaries: dict | None = None
    transcript_candidates: dict[str, int] | None = None
    try:
        candidate_records: list[Example] | None = None
        if args.include_transcript_candidates:
            loader = _load_dataset_loader(manifest)
            candidate_records = _load_transcript_candidates(loader)
        program_class = load_program_class(manifest)
        if not args.include_transcript_candidates:
            loader = _load_dataset_loader(manifest)
        baseline_means = _baseline_means(manifest)
        lm = _construct_lm(args.tier, args.budget_usd, ledger)
        program = program_class(lm)
        train_examples, val_examples = build_trainsets(loader, seed=args.seed)
        if args.include_transcript_candidates:
            frozen_examples = [
                *train_examples,
                *val_examples,
                *_load_split(loader, "EVAL"),
                *_load_split(loader, "CANARIES"),
            ]
            train_examples, transcript_candidates = _augment_training_pool(
                loader,
                train_examples,
                frozen_examples,
                candidates=candidate_records,
            )
        program, stage_zero = run_stage_zero(
            program,
            train_examples,
            val_examples,
            seed=args.seed,
        )
        final = stage_zero
        if args.optimizer == "bootstrap":
            program, stage_bootstrap = run_stage_bootstrap(
                program,
                train_examples,
                val_examples,
                seed=args.seed,
            )
            final = stage_bootstrap
        canaries = score_split(program, _load_split(loader, "CANARIES"))
        gate_passed = (
            final["eval_mean"] >= 0.85
            and final["eval_mean"] >= baseline_means["eval"] - 0.05
            and canaries["count"] > 0
            and canaries["mean"] == 1.0
        )
        report = _partial_report(
            manifest,
            args,
            ledger,
            baseline_means=baseline_means,
            stage_zero=stage_zero,
            stage_bootstrap=stage_bootstrap,
            final=final,
            canaries=canaries,
            transcript_candidates=transcript_candidates,
        )
        report["gate_passed"] = gate_passed
        report["anti_reward_gap"] = _anti_reward_gap(
            final,
            canaries,
            baseline_means,
        )
        artifact = write_artifact(
            manifest.module_name,
            _next_version(manifest.module_name),
            program,
            lm,
            report,
            promote=gate_passed,
        )
        print(
            f"cambium optimize: wrote {artifact}; gate_passed={gate_passed} "
            f"spent_usd={ledger.spent_usd:.6f}",
            file=sys.stderr,
        )
        return 0 if gate_passed else 1
    except _BudgetExhausted as exc:
        print(f"cambium optimize: {exc}", file=sys.stderr)
        if program is not None:
            report = _partial_report(
                manifest,
                args,
                ledger,
                baseline_means=baseline_means,
                stage_zero=stage_zero,
                stage_bootstrap=stage_bootstrap,
                final=final,
                canaries=canaries,
                budget_exhausted=True,
                transcript_candidates=transcript_candidates,
            )
            report["anti_reward_gap"] = _anti_reward_gap(
                final,
                canaries,
                baseline_means,
            )
            try:
                artifact = write_artifact(
                    manifest.module_name,
                    _next_version(manifest.module_name),
                    program,
                    lm,
                    report,
                    promote=False,
                )
            except (OptimizeError, OSError) as artifact_error:
                print(
                    f"cambium optimize: could not write budget report artifact: {artifact_error}",
                    file=sys.stderr,
                )
            else:
                print(f"cambium optimize: wrote {artifact}", file=sys.stderr)
        return 1


def main(argv=None) -> int:
    try:
        return _run(argv)
    except Exception as exc:
        print(f"cambium optimize: ERROR {type(exc).__name__}: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
