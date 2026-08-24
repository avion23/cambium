"""DSPy hill-climbing driver for Cambium decision modules.

This is a harness boundary.  Decision packages are discovered from their
manifest and their DSPy program is imported from the manifest's dotted module
path.  The first spike has two stages: a zero-shot measurement and
``BootstrapFewShot`` compilation.
"""

from __future__ import annotations

import argparse
import asyncio
import hashlib
import importlib
import json
import math
import os
import random
import statistics
import sys
import tempfile
from collections.abc import Callable, Iterable, Mapping
from pathlib import Path
from types import MethodType
from typing import Any, cast

import dspy  # type: ignore[import-untyped]

from cambium import module_conformance
from cambium.diffundo import Diffundo, DiffundoError, ProviderTier
from cambium.jlens import JlenClient, JlenError, render_messages
from cambium.lm import CambiumLM
from cambium.modules.base import (
    DatasetError,
    Example,
    ModuleContractError,
    ModuleManifest,
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
        if self.remaining_usd < _MIN_CALL_BUDGET_USD:
            raise _BudgetExhausted(
                f"optimization budget exhausted: spent ${self.spent_usd:.6f} "
                f"of ${self.budget_usd:.6f}; only ${self.remaining_usd:.6f} remains"
            )

    def record(self, value: object) -> None:
        if isinstance(value, bool) or not isinstance(value, int | float):
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
        allow_model_substitution: bool = False,
        requirements: Mapping[str, Any] | None = None,
    ) -> Any:
        self._ledger.check_available()
        call_budget = self._ledger.remaining_usd
        if budget_usd is not None:
            call_budget = min(call_budget, budget_usd)
        result = await self._delegate.call(
            tier,
            prompt,
            model=model,
            budget_usd=call_budget,
            allow_model_substitution=allow_model_substitution,
            requirements=requirements,
        )
        self._ledger.record(getattr(result, "estimated_cost_usd", 0.0))
        if self._ledger.spent_usd > self._ledger.budget_usd:
            raise _BudgetExhausted(
                f"optimization budget exceeded: spent ${self._ledger.spent_usd:.6f} "
                f"of ${self._ledger.budget_usd:.6f}"
            )
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
        raise OptimizeError(f"DSPy program module {target!r} has no class {class_name!r}")
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


def _output_type(domain: object) -> type | None:
    """Find the domain's ``*Output`` type without naming a module variant."""
    candidates = [
        value
        for name, value in vars(domain).items()
        if name.endswith("Output") and isinstance(value, type)
    ]
    return candidates[0] if len(candidates) == 1 else None


def _label_field(program: object, domain: object | None = None) -> str:
    """Return the module-declared expected-label key.

    A DSPy program may expose the field directly, while packaged modules
    declare it in ``module.json``.  The output compatibility property is a
    final metadata fallback for lightweight test programs that do not have a
    manifest.  The default keeps the v1 example module's behavior unchanged.
    """
    if domain is None:
        domain = _domain_module(program)
    for owner in (program, type(program), domain):
        value = getattr(owner, "label_field", _MISSING)
        if isinstance(value, str) and value:
            return value

    domain_path = getattr(domain, "__file__", None)
    if isinstance(domain_path, str):
        try:
            manifest = load_module_manifest(Path(domain_path).parent)
        except (ModuleContractError, OSError):
            pass
        else:
            if isinstance(manifest.label_field, str) and manifest.label_field:
                return manifest.label_field

    output_type = _output_type(domain)
    if output_type is not None:
        properties = [
            name
            for name, value in vars(output_type).items()
            if isinstance(value, property) and name not in {"decision", "reason", "confidence"}
        ]
        if len(properties) == 1:
            return properties[0]
    return "decompose"


def _metric_function(program: object) -> Callable[[Example], float]:
    metric = getattr(program, "metric", None)
    if callable(metric):
        return cast(Callable[[Example], float], metric)

    target = f"{_program_package(program)}.metric"
    try:
        mod = _import_target(target)
    except ImportError:
        target = _EXAMPLE_METRIC_TARGET
        mod = _import_target(target)
    metric = getattr(mod, f"should_{_label_field(program)}_metric", None)
    if not callable(metric):
        raise OptimizeError(f"metric module {target!r} has no usable metric")
    return cast(Callable[[Example], float], metric)


def _parse_decision(raw: object, decision_type: type, label_field: str = "decompose") -> object | None:
    members = tuple(cast(Iterable[object], decision_type))
    if isinstance(raw, decision_type):
        return raw
    if isinstance(raw, bool):
        for member in members:
            if getattr(member, "value", None) == (
                label_field if raw else f"do_not_{label_field}"
            ):
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
    output_type = _output_type(domain)
    if not isinstance(decision_type, type) or not isinstance(output_type, type):
        return None
    label_field = _label_field(program, domain)

    if isinstance(raw, output_type):
        value = raw
        decision = _parse_decision(
            getattr(value, "decision", _MISSING), decision_type, label_field
        )
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
        decision = None
        for field in dict.fromkeys(("decision", label_field, "decompose")):
            decision = _parse_decision(_read_field(raw, field), decision_type, label_field)
            if decision is not None:
                break
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
    if isinstance(confidence, bool) or not isinstance(confidence, int | float):
        return None
    if not math.isfinite(confidence) or not 0.0 <= confidence <= 1.0:
        return None
    return output_type(decision=decision, reason=reason, confidence=float(confidence))


def _fallback_prediction(program: object) -> object:
    domain = _domain_module(program)
    decision_type = getattr(domain, "Decision", None)
    output_type = _output_type(domain)
    if not isinstance(decision_type, type) or not isinstance(output_type, type):
        raise OptimizeError("decision module does not expose Decision and an output type")
    label_field = _label_field(program, domain)
    fallback = getattr(program, "fallback_decision", _MISSING)
    if fallback is _MISSING:
        fallback = f"do_not_{label_field}"
    decision = _parse_decision(fallback, decision_type, label_field)
    if decision is None:
        fallback_value = getattr(fallback, "value", fallback)
        raise OptimizeError(f"decision module has no {str(fallback_value).upper()} decision")
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
    label_field = _label_field(program, domain)

    if isinstance(gold, Example):
        input_value = _task_input(gold.input, task_input_type)
        expected_source = gold.expected
        raw_decision = expected_source.get(label_field, _MISSING)
        reason = expected_source.get("reason", "")
    else:
        input_value = _task_input(gold, task_input_type)
        raw_decision = _read_field(gold, "decision")
        if raw_decision is _MISSING:
            for field in dict.fromkeys((label_field, "decompose")):
                raw_decision = _read_field(gold, field)
                if raw_decision is not _MISSING:
                    break
        reason = _read_field(gold, "reason")
        if reason is _MISSING:
            reason = ""

    decision = _parse_decision(raw_decision, decision_type, label_field)
    if input_value is None or decision is None or not isinstance(reason, str):
        return None
    return input_value, {label_field: decision, "reason": reason}


def _jlens_client_from_env() -> JlenClient | None:
    """Build a jlens score client from CAMBIUM_JLENS_URL, or None if unset."""
    base_url = os.environ.get("CAMBIUM_JLENS_URL", "").strip()
    if not base_url:
        return None
    layers = None
    raw_layers = os.environ.get("CAMBIUM_JLENS_LAYERS", "").strip()
    if raw_layers:
        parsed: list[int] = []
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
    if not isinstance(trace, list | tuple) or not trace:
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
        if not isinstance(jlens_score, int | float) or not math.isfinite(jlens_score):
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
            if isinstance(score, bool) or not isinstance(score, int | float):
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
    load_split = cast(Callable[[object], Iterable[Example]], load_split)
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
            return Path(cast(str | os.PathLike[str], datasets_dir))
        except TypeError as exc:
            raise OptimizeError("dataset loader has an invalid datasets_dir") from exc

    path = getattr(loader, "path", _MISSING)
    if path is _MISSING:
        raise OptimizeError("dataset loader does not expose a dataset path")
    try:
        dataset_path = Path(cast(str | os.PathLike[str], path))
    except TypeError as exc:
        raise OptimizeError("dataset loader has an invalid dataset path") from exc
    return dataset_path if dataset_path.is_dir() else dataset_path.parent


def _reviewed_transcript_records(candidate_path: Path) -> list[dict[str, Any]]:
    """Load only explicitly approved, redacted candidate records.

    Rejected/excluded rows are ignored. Any pending or unknown review status
    fails closed so an optimizer flag cannot silently promote raw transcripts.
    """

    approved: list[dict[str, Any]] = []
    pending = 0
    try:
        lines = candidate_path.read_text(encoding="utf-8").splitlines()
    except OSError as exc:
        raise OptimizeError(
            f"could not read transcript candidates {candidate_path}: {exc}"
        ) from exc
    for line_no, line in enumerate(lines, start=1):
        if not line.strip():
            continue
        try:
            record = json.loads(line)
        except json.JSONDecodeError as exc:
            raise OptimizeError(
                f"transcript candidate {candidate_path}:{line_no} is invalid JSON"
            ) from exc
        if not isinstance(record, dict):
            raise OptimizeError(f"transcript candidate {candidate_path}:{line_no} is not an object")
        status = record.get("review_status")
        if status in {"rejected", "excluded"}:
            continue
        if status != "approved":
            pending += 1
            continue
        if record.get("candidate") is not True or record.get("redacted") is not True:
            raise OptimizeError(
                f"approved transcript candidate {candidate_path}:{line_no} "
                "must be candidate=true and redacted=true"
            )
        approved.append(record)
    if pending:
        raise OptimizeError(
            f"{pending} transcript candidate(s) still need review; "
            "approve or reject every record before optimization"
        )
    if not approved:
        raise OptimizeError("no approved transcript candidates are available")
    return approved


def _load_transcript_candidates(
    loader: object, candidate_path: Path | None = None
) -> list[Example]:
    """Load explicitly reviewed transcript candidates with the module loader."""

    if candidate_path is None:
        candidate_path = _loader_datasets_dir(loader) / _TRANSCRIPT_CANDIDATES_FILENAME
    candidate_path = Path(candidate_path)
    if not candidate_path.is_file():
        raise OptimizeError(
            f"transcript candidate file is missing for this module: {candidate_path}"
        )
    approved = _reviewed_transcript_records(candidate_path)

    loader_class = cast(Callable[[Path], object], type(loader))
    temporary_path: Path | None = None
    try:
        with tempfile.NamedTemporaryFile(
            mode="w",
            encoding="utf-8",
            prefix=".approved-transcript-",
            suffix=".jsonl",
            dir=candidate_path.parent,
            delete=False,
        ) as stream:
            temporary_path = Path(stream.name)
            for record in approved:
                stream.write(
                    json.dumps(
                        record,
                        ensure_ascii=False,
                        sort_keys=True,
                        separators=(",", ":"),
                    )
                    + "\n"
                )
        try:
            candidate_loader = loader_class(temporary_path)
        except (OSError, ValueError) as exc:
            raise OptimizeError(
                f"could not construct the dataset loader for transcript candidates: {exc}"
            ) from exc
        load = getattr(candidate_loader, "load", None)
        if not callable(load):
            raise OptimizeError("dataset loader does not provide load for transcript candidates")
        load = cast(Callable[[], Iterable[Example]], load)
        try:
            candidates = list(load())
        except (
            DatasetError,
            ImportError,
            OSError,
            KeyError,
            json.JSONDecodeError,
        ) as exc:
            raise OptimizeError(f"could not load transcript candidates: {exc}") from exc
    finally:
        if temporary_path is not None:
            try:
                temporary_path.unlink()
            except FileNotFoundError:
                pass
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
    if isinstance(val_fraction, bool) or not isinstance(val_fraction, int | float):
        raise OptimizeError("val_fraction must be a number in [0, 1)")
    if not math.isfinite(val_fraction) or not 0.0 <= val_fraction < 1.0:
        raise OptimizeError("val_fraction must be a number in [0, 1)")
    load_split = getattr(loader, "load_split", None)
    if not callable(load_split):
        raise OptimizeError("dataset loader does not provide load_split")
    load_split = cast(Callable[[object], Iterable[Example]], load_split)
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
    outcomes = await _evaluate_examples_async(program, examples)
    return [cast(float, outcome["score"]) for outcome in outcomes]


async def _evaluate_examples_async(
    program: object, examples: list[Example]
) -> list[dict[str, float | int]]:
    """Run the program and retain one normalized metric outcome per example."""
    scorer = _metric_function(program)
    outcomes: list[dict[str, float | int]] = []
    for index, example in enumerate(examples):
        try:
            raw_prediction = await cast(Callable[[Any], Any], cast(Any, program).decide)(
                example.input
            )
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
        if isinstance(score, bool) or not isinstance(score, int | float):
            score = 0.0
        if not math.isfinite(score) or not 0.0 <= score <= 1.0:
            score = 0.0
        outcomes.append({"index": index, "score": float(score)})
    return outcomes


async def _evaluate_splits_async(
    program: object, split_examples: Mapping[str, list[Example]]
) -> dict[str, list[dict[str, float | int]]]:
    """Evaluate each named split without changing the program or metric seam."""
    return {
        split: await _evaluate_examples_async(program, examples)
        for split, examples in split_examples.items()
    }


def _score_outcomes(outcomes: list[dict[str, float | int]]) -> dict[str, Any]:
    scores = [cast(float, outcome["score"]) for outcome in outcomes]
    if not scores:
        return {"mean": 0.0, "std": 0.0, "count": 0, "records": outcomes}
    return {
        "mean": float(statistics.fmean(scores)),
        "std": float(statistics.pstdev(scores)),
        "count": len(scores),
        "records": outcomes,
    }


def evaluate_dataset(program: object, loader: object) -> dict[str, dict[str, Any]]:
    """Evaluate all three reviewed dataset splits with the program's metric."""
    split_examples = {
        split: _load_split(loader, split.upper())
        for split in ("train", "eval", "canaries")
    }
    outcomes = asyncio.run(_evaluate_splits_async(program, split_examples))
    return {split: _score_outcomes(outcomes[split]) for split in split_examples}


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
    label_field = _label_field(program, domain)
    input_value = example.input
    task = _read_field(input_value, "task")
    context = _read_field(input_value, "context")
    if not isinstance(task, str) or not isinstance(context, str):
        raise OptimizeError("train example input must contain task and context strings")
    raw_decision = example.expected.get(label_field, _MISSING)
    decision = _parse_decision(raw_decision, decision_type, label_field)
    reason = example.expected.get("reason", "")
    if decision is None or not isinstance(reason, str):
        raise OptimizeError("train example expected value is not parseable")
    return dspy.Example(
        task=task,
        context=context,
        decision=cast(Any, decision).value,
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

    cast(Any, program).forward = MethodType(forward, program)


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


_GEPA_MIN_DATASET_SIZE = 4
_GEPA_VAL_FRACTION = 0.3


def _gepa_metric_call_budget(
    train_examples: Iterable[Example],
    val_examples: Iterable[Example],
    *,
    budget_usd: float | None,
    ledger: _CostLedger | None,
) -> int:
    """Translate the dollar budget into GEPA's metric-call budget."""
    if ledger is not None:
        remaining_usd = ledger.remaining_usd
    elif budget_usd is not None:
        remaining_usd = budget_usd
    else:
        remaining_usd = None

    if remaining_usd is None:
        return max(1, len(list(train_examples)) + len(list(val_examples)))
    if remaining_usd < _MIN_CALL_BUDGET_USD:
        if ledger is not None:
            ledger.check_available()
        raise _BudgetExhausted(
            f"optimization budget exhausted: only ${remaining_usd:.6f} remains for GEPA "
            f"(minimum provider call is ${_MIN_CALL_BUDGET_USD:.2f})"
        )
    return max(1, math.floor(remaining_usd / _MIN_CALL_BUDGET_USD))


def _gepa_report_details(compiled: object) -> dict[str, int]:
    """Extract optional GEPA run counters without depending on one version."""
    details = getattr(compiled, "detailed_results", None)
    if details is None:
        return {}
    report: dict[str, int] = {}
    total_calls = getattr(details, "total_metric_calls", None)
    if isinstance(total_calls, int) and not isinstance(total_calls, bool) and total_calls >= 0:
        report["calls"] = total_calls
    candidates = getattr(details, "candidates", None)
    if isinstance(candidates, list) and candidates:
        report["iterations"] = max(0, len(candidates) - 1)
    full_evals = getattr(details, "num_full_val_evals", None)
    if isinstance(full_evals, int) and not isinstance(full_evals, bool) and full_evals >= 0:
        report["full_evals"] = full_evals
    return report


def run_stage_gepa(
    program,
    train_examples,
    val_examples,
    seed=0,
    *,
    budget_usd: float | None = None,
    ledger: _CostLedger | None = None,
    reflection_lm: object | None = None,
) -> tuple[object, dict]:
    """Reflectively optimize a DSPy program with a deterministic held-out set."""
    train_examples = list(train_examples)
    val_examples = list(val_examples)
    if len(train_examples) + len(val_examples) < _GEPA_MIN_DATASET_SIZE:
        raise OptimizeError(
            "GEPA requires more reviewed data: at least 4 non-canary records are "
            "required for a train/validation split"
        )
    if not train_examples or not val_examples:
        raise OptimizeError(
            "GEPA requires more reviewed data: both train and held-out validation "
            "records are required"
        )

    _ensure_bootstrap_forward(program)
    if reflection_lm is None:
        reflection_lm = getattr(program, "_lm", None)
    if reflection_lm is None or not callable(reflection_lm):
        raise OptimizeError(
            "GEPA requires a reflection LM; the program must expose the "
            "Diffundo-backed CambiumLM"
        )
    gepa_class = getattr(dspy, "GEPA", None)
    if not callable(gepa_class):
        raise OptimizeError("installed DSPy does not expose dspy.GEPA")

    max_metric_calls = _gepa_metric_call_budget(
        train_examples,
        val_examples,
        budget_usd=budget_usd,
        ledger=ledger,
    )
    try:
        optimizer = gepa_class(
            metric=make_dspy_metric(program),
            max_metric_calls=max_metric_calls,
            reflection_lm=reflection_lm,
            seed=seed,
            track_stats=True,
        )
    except _BudgetExhausted:
        raise
    except (DiffundoError, DSPyError, AssertionError, TypeError, ValueError) as exc:
        raise OptimizeError(f"could not create GEPA: {exc}") from exc

    trainset = [_to_dspy_example(example, program) for example in train_examples]
    valset = [_to_dspy_example(example, program) for example in val_examples]
    try:
        compiled = optimizer.compile(program, trainset=trainset, valset=valset)
    except _BudgetExhausted:
        raise
    except (DiffundoError, DSPyError, AssertionError, RuntimeError, TypeError, ValueError) as exc:
        raise OptimizeError(f"GEPA compilation failed: {exc}") from exc

    if compiled is None:
        raise OptimizeError("GEPA returned no compiled program")
    eval_score = score_split(compiled, val_examples)
    train_score = score_split(compiled, train_examples)
    report = {
        "eval_mean": eval_score["mean"],
        "train_mean": train_score["mean"],
    }
    report.update(_gepa_report_details(compiled))
    return compiled, report


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


def write_artifact(module_name, program, lm, report) -> Path:
    """Persist the module's single artifact set, replacing any previous set.

    Each member is atomically replaced, but the directory is not transactional.
    ``program.json`` and ``lm.json`` are written before ``report.json``; the
    report is the commit marker that readers should require before consuming
    the other two files after an interrupted write.
    """
    module_component = _safe_component(module_name, "module_name")
    module_dir = Path("optimized") / module_component
    module_dir.mkdir(parents=True, exist_ok=True)

    program_dump = getattr(program, "dump_state", None)
    lm_dump = getattr(lm, "dump_state", None)
    if not callable(program_dump) or not callable(lm_dump):
        raise OptimizeError("program and LM must provide dump_state()")
    _atomic_json_write(module_dir / "program.json", program_dump())
    _atomic_json_write(module_dir / "lm.json", lm_dump())
    # Keep report last: it commits the preceding pair for readers.
    _atomic_json_write(module_dir / "report.json", report)
    return module_dir


class _SingleFileDatasetLoader:
    """Expose a reviewed JSONL queue as the module loader's split interface."""

    def __init__(self, loader: object, path: Path) -> None:
        self.path = path
        self.datasets_dir = path.parent
        load = getattr(loader, "load", None)
        if not callable(load):
            raise OptimizeError("explicit dataset loader does not provide load()")
        examples = list(load())
        try:
            records = [
                json.loads(line)
                for line in path.read_text(encoding="utf-8").splitlines()
                if line.strip()
            ]
        except (OSError, ValueError) as exc:
            raise OptimizeError(f"could not read explicit dataset {path}: {exc}") from exc
        if len(records) != len(examples):
            raise OptimizeError(f"explicit dataset {path} changed while it was being read")
        self._splits: dict[str, list[Example]] = {"train": [], "eval": [], "canaries": []}
        for record, example in zip(records, examples, strict=True):
            if not isinstance(record, dict):
                raise OptimizeError(f"explicit dataset {path} contains a non-object record")
            split = record.get("split", "train")
            if not isinstance(split, str):
                raise OptimizeError(f"explicit dataset {path} has a non-string split")
            split = split.casefold()
            if split == "val":
                split = "eval"
            if split not in self._splits:
                raise OptimizeError(f"explicit dataset {path} has unknown split {split!r}")
            if getattr(example, "canary", False):
                split = "canaries"
            self._splits[split].append(example)

    def load_split(self, split: object) -> list[Example]:
        name = getattr(split, "value", getattr(split, "name", ""))
        name = str(name).casefold()
        if name not in self._splits:
            raise DatasetError(f"explicit dataset loader has no split {name!r}")
        return list(self._splits[name])


def _load_dataset_loader(manifest: object, dataset_path: Path | None = None) -> object:
    """Construct the module loader for its packaged or explicit dataset."""
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
    if dataset_path is None:
        dataset_path = Path(cast(ModuleManifest, manifest).package_dir) / "datasets"
    try:
        loader = loader_class(dataset_path)
        if dataset_path.is_file():
            return _SingleFileDatasetLoader(loader, dataset_path)
        return loader
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
    package_dir = Path(cast(ModuleManifest, manifest).package_dir)
    path = package_dir / "tests" / "baselines" / "baseline.json"
    datasets_dir = package_dir / "datasets"
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, TypeError, ValueError, json.JSONDecodeError) as exc:
        raise OptimizeError(f"cannot read baseline {path}: {exc}") from exc
    if not isinstance(data, dict):
        raise OptimizeError(f"baseline {path} must contain an object")
    schema_version = data.get("schema_version")
    if (
        isinstance(schema_version, bool)
        or not isinstance(schema_version, int)
        or schema_version != 1
    ):
        raise OptimizeError(f"baseline {path} has unsupported schema_version")
    module_name = getattr(manifest, "module_name", None)
    if not isinstance(data.get("module"), str):
        raise OptimizeError(f"baseline {path} has no module name")
    if module_name is not None and data["module"] != module_name:
        raise OptimizeError(
            f"baseline {path} module {data['module']!r} does not match {module_name!r}"
        )

    meta_path = datasets_dir / "meta.json"
    try:
        meta = json.loads(meta_path.read_text(encoding="utf-8"))
    except (OSError, TypeError, ValueError, json.JSONDecodeError) as exc:
        raise OptimizeError(f"cannot read dataset metadata {meta_path}: {exc}") from exc
    if not isinstance(meta, dict):
        raise OptimizeError(f"dataset metadata {meta_path} must contain an object")
    expected_schema = getattr(manifest, "dataset_schema_version", None)
    current_schema = meta.get("schema_version")
    if expected_schema is not None:
        if isinstance(expected_schema, bool) or not isinstance(expected_schema, int):
            raise OptimizeError("manifest dataset_schema_version is not an integer")
        if isinstance(current_schema, bool) or not isinstance(current_schema, int):
            raise OptimizeError(f"dataset metadata {meta_path} schema_version is not an integer")
        if current_schema != expected_schema:
            raise OptimizeError(
                f"dataset metadata {meta_path} schema_version {current_schema!r} "
                f"does not match manifest {expected_schema!r}"
            )
    dataset_version = meta.get("dataset_version")
    if not isinstance(dataset_version, str) or not dataset_version:
        raise OptimizeError(f"dataset metadata {meta_path} has no dataset_version")
    if data.get("dataset_version") != dataset_version:
        raise OptimizeError(
            f"baseline {path} dataset_version {data.get('dataset_version')!r} "
            f"does not match current dataset {dataset_version!r}"
        )

    baseline_digests = data.get("split_digests")
    meta_digests = meta.get("split_digests")
    if not isinstance(baseline_digests, Mapping) or not isinstance(meta_digests, Mapping):
        raise OptimizeError(f"baseline {path} has no complete split_digests")
    for split in ("train", "eval", "canaries"):
        baseline_digest = baseline_digests.get(split)
        meta_digest = meta_digests.get(split)
        if not isinstance(baseline_digest, str) or not isinstance(meta_digest, str):
            raise OptimizeError(f"baseline {path} has no split_digests.{split}")
        if baseline_digest != meta_digest:
            raise OptimizeError(
                f"baseline {path} split_digests.{split} does not match dataset metadata"
            )
        split_path = datasets_dir / f"{split}.jsonl"
        try:
            actual_digest = hashlib.sha256(split_path.read_bytes()).hexdigest()
        except OSError as exc:
            raise OptimizeError(f"cannot read dataset split {split_path}: {exc}") from exc
        if baseline_digest != actual_digest:
            raise OptimizeError(
                f"baseline {path} split_digests.{split} does not match current dataset"
            )

    means: dict[str, float] = {}
    for split in ("train", "eval", "canaries"):
        try:
            value = data["metric"][split]["mean"]
        except (KeyError, TypeError) as exc:
            raise OptimizeError(f"baseline {path} has no metric.{split}.mean") from exc
        if isinstance(value, bool) or not isinstance(value, int | float):
            raise OptimizeError(f"baseline {path} metric.{split}.mean is not numeric")
        if not math.isfinite(value) or not 0.0 <= value <= 1.0:
            raise OptimizeError(f"baseline {path} metric.{split}.mean is outside [0, 1]")
        means[split] = float(value)
    return means


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
    # Deliberately NO model pin on the LM: pinning selected.model collapses
    # Diffundo candidates to exactly that provider (strict-model match), so a
    # dead primary kills optimization even when same-tier alternatives are
    # healthy. Selection still drives the primary hint and OAuth credentials.
    if selected.auth is AuthMode.CODEX_CHATGPT:
        from cambium.oauth import OAuthError, TokenManager

        try:
            access_token, account_id = TokenManager(selected.name).ensure_fresh()
        except OAuthError as exc:
            raise OptimizeError(
                f"provider {selected.name!r} OAuth session is unavailable: {exc}"
            ) from exc
        options["credential_source"] = CredentialSource(
            access_token=access_token,
            account_id=account_id,
        )

    router = Diffundo(providers, **options)
    tracked = _TrackingDiffundo(router, ledger)
    # No model pin: pinning selected.model collapses Diffundo candidates to
    # exactly that provider (strict-model match), so a dead primary kills
    # optimization even when same-tier alternatives are healthy.
    return CambiumLM(
        tracked,
        tier,
        budget_usd=budget_usd,
    )


def _load_program_state(program: object, program_dir: Path, *, required: bool = False) -> bool:
    """Load ``program.json`` into a fresh program, if an artifact is present."""
    if not program_dir.is_dir():
        if required:
            raise OptimizeError(f"program directory is missing: {program_dir}")
        return False
    state_path = program_dir / "program.json"
    if not state_path.is_file():
        if required:
            raise OptimizeError(f"program state is missing: {state_path}")
        return False
    try:
        state = json.loads(state_path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        raise OptimizeError(f"could not read program state {state_path}: {exc}") from exc
    if not isinstance(state, dict):
        raise OptimizeError(f"program state {state_path} must contain an object")

    load_state = getattr(program, "load_state", None)
    if not callable(load_state):
        raise OptimizeError("DSPy program does not provide load_state()")
    try:
        load_state(state)
    except (AttributeError, KeyError, OSError, RuntimeError, TypeError, ValueError) as exc:
        raise OptimizeError(f"could not load program state {state_path}: {exc}") from exc
    return True


def _eval_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="cambium optimize eval",
        description="Evaluate a fresh or optimized DSPy program on every dataset split.",
    )
    parser.add_argument("module_name", metavar="MODULE")
    parser.add_argument("--dataset", type=Path, required=True, metavar="PATH")
    parser.add_argument("--program-dir", type=Path, metavar="PATH")
    parser.add_argument("--budget-usd", type=float, default=2.0)
    parser.add_argument("--tier", default="fast")
    parser.add_argument("--json", action="store_true", help="emit one JSON report")
    return parser


def _run_eval(args: argparse.Namespace) -> int:
    if isinstance(args.budget_usd, bool) or not math.isfinite(args.budget_usd):
        print("cambium optimize eval: --budget-usd must be finite", file=sys.stderr)
        return 2
    if args.budget_usd < 0:
        print("cambium optimize eval: --budget-usd must be non-negative", file=sys.stderr)
        return 2

    manifest = _load_manifest(args.module_name)
    program_class = load_program_class(manifest)
    loader = _load_dataset_loader(manifest, args.dataset)
    ledger = _CostLedger(args.budget_usd)
    lm = _construct_lm(args.tier, args.budget_usd, ledger)
    program = program_class(lm)

    module_name = getattr(manifest, "module_name", args.module_name)
    if args.program_dir is None:
        program_dir = Path("optimized") / _safe_component(module_name, "module_name")
        required_state = False
    else:
        program_dir = args.program_dir
        required_state = True
    optimized = _load_program_state(program, program_dir, required=required_state)
    report = {
        "module": module_name,
        "program": "optimized" if optimized else "fresh",
        "dataset": str(args.dataset),
        "splits": evaluate_dataset(program, loader),
    }
    if args.json:
        print(json.dumps(report, ensure_ascii=True, sort_keys=True))
    else:
        print(f"module={report['module']} program={report['program']} dataset={report['dataset']}")
        for split, summary in report["splits"].items():
            print(f"{split}: mean={summary['mean']:.6f} count={summary['count']}")
            for outcome in summary["records"]:
                print(f"  record={outcome['index']} score={outcome['score']:.6f}")
    return 0


def eval_main(argv: list[str] | None = None) -> int:
    """Evaluate a fresh or saved DSPy program against all reviewed splits."""
    args = _eval_parser().parse_args(argv)
    try:
        return _run_eval(args)
    except (ModuleContractError, OptimizeError, OSError) as exc:
        print(f"cambium optimize eval: {exc}", file=sys.stderr)
        return 1


def extract_candidates(*args: Any, **kwargs: Any) -> Any:
    """Load the packaged OpenCode extractor without duplicating its policy."""
    from cambium.opencode import extract_candidates as extractor

    return extractor(*args, **kwargs)


def extract_main(argv: list[str] | None = None) -> int:
    """Run the end-to-end trajectory extraction command."""
    from cambium.opencode import extract_main as runner

    return runner(argv)


def stats_main(argv: list[str] | None = None) -> int:
    """Run the extracted-dataset report command."""
    from cambium.opencode import stats_main as runner

    return runner(argv)


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="python -m cambium.optimize",
        description="Run the Cambium DSPy optimizer spike.",
    )
    parser.add_argument("module_name")
    parser.add_argument("--optimizer", choices=("zero", "bootstrap", "gepa"), default="zero")
    parser.add_argument("--budget-usd", type=float, default=2.0)
    parser.add_argument("--seed", type=int, default=0)
    parser.add_argument("--tier", default="fast")
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--dataset", type=Path, metavar="PATH")
    candidate_source = parser.add_mutually_exclusive_group()
    candidate_source.add_argument(
        "--include-transcript-candidates",
        action="store_true",
        help="add the module-local approved transcript candidates",
    )
    candidate_source.add_argument(
        "--transcript-candidates",
        type=Path,
        metavar="PATH",
        help="add an explicit approved/redacted transcript-candidate JSONL file",
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
    stage_gepa: dict | None = None,
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
        "stage_gepa": stage_gepa,
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
    loader: object = _MISSING
    lm: object = _MISSING
    ledger = _CostLedger(args.budget_usd)
    baseline_means: dict[str, float] | None = None
    stage_zero: dict | None = None
    stage_bootstrap: dict | None = None
    stage_gepa: dict | None = None
    final: dict | None = None
    canaries: dict | None = None
    transcript_candidates: dict[str, int] | None = None
    try:
        candidate_records: list[Example] | None = None
        use_transcript_candidates = (
            args.include_transcript_candidates or args.transcript_candidates is not None
        )
        if use_transcript_candidates:
            loader = _load_dataset_loader(manifest)
            candidate_records = _load_transcript_candidates(
                loader,
                args.transcript_candidates,
            )
        program_class = load_program_class(manifest)
        if not use_transcript_candidates:
            if args.dataset is None:
                loader = _load_dataset_loader(manifest)
            else:
                loader = _load_dataset_loader(manifest, args.dataset)
        baseline_means = _baseline_means(manifest)
        lm = _construct_lm(args.tier, args.budget_usd, ledger)
        program = program_class(lm)
        if args.dataset is not None:
            train_examples = _load_split(loader, "TRAIN")
            val_examples = _load_split(loader, "EVAL")
        else:
            if args.optimizer == "gepa":
                train_examples, val_examples = build_trainsets(
                    loader,
                    seed=args.seed,
                    val_fraction=_GEPA_VAL_FRACTION,
                )
                if len(train_examples) + len(val_examples) < _GEPA_MIN_DATASET_SIZE:
                    raise OptimizeError(
                        "GEPA requires more reviewed data: at least 4 non-canary records "
                        "are required for a train/validation split"
                    )
            else:
                train_examples, val_examples = build_trainsets(loader, seed=args.seed)
        if use_transcript_candidates:
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
        elif args.optimizer == "gepa":
            program, stage_gepa = run_stage_gepa(
                program,
                train_examples,
                val_examples,
                seed=args.seed,
                budget_usd=args.budget_usd,
                ledger=ledger,
                reflection_lm=lm,
            )
            final = stage_gepa
        canaries = score_split(program, _load_split(loader, "CANARIES"))
        gate_passed = (
            ledger.spent_usd <= ledger.budget_usd
            and final["eval_mean"] >= 0.85
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
            stage_gepa=stage_gepa,
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
            cast(ModuleManifest, manifest).module_name,
            program,
            lm,
            report,
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
                stage_gepa=stage_gepa,
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
                    cast(ModuleManifest, manifest).module_name,
                    program,
                    lm,
                    report,
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
    command_line = sys.argv[1:] if argv is None else argv
    if command_line and command_line[0] == "extract":
        return extract_main(command_line[1:])
    if command_line and command_line[0] in {"stats", "report"}:
        return stats_main(command_line[1:])
    try:
        if command_line and command_line[0] == "eval":
            return eval_main(command_line[1:])
        return _run(command_line)
    except Exception as exc:
        print(f"cambium optimize: ERROR {type(exc).__name__}: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
