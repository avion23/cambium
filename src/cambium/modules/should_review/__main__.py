"""Neutral JSON stdin/stdout adapter for the should_review decision module.

The harness and repository scripts call this entry point instead of importing
the implementation.  A direct input object returns a decision.  Tooling may
send ``{"operation": "decide", "inputs": [...]}`` or
``{"operation": "evaluate", "records": [...]}`` to keep a dataset run in
one subprocess.
"""

from __future__ import annotations

import asyncio
import json
import math
import sys
from typing import Any

from cambium.modules.base import Example

from .decide import Decision, ReviewOutput, ShouldReviewModule, TaskInput

_INPUT_FIELDS = frozenset({"task", "context"})


class InputValidationError(ValueError):
    """Raised when a JSON request does not match the module wire schema."""


class SchemaInvalidError(ValueError):
    """Raised when a dataset record does not match the module dataset schema."""


def _reject_duplicate_fields(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    """Reject duplicate JSON object member names instead of keeping the last value."""
    fields: dict[str, Any] = {}
    for name, value in pairs:
        if name in fields:
            raise json.JSONDecodeError(f"duplicate JSON object field: {name!r}", "", 0)
        fields[name] = value
    return fields


def _parse_input(payload: Any) -> TaskInput:
    """Validate one decoded JSON value and build the typed module input."""
    if not isinstance(payload, dict):
        raise InputValidationError("input must be a JSON object")

    unknown_fields = sorted(set(payload) - _INPUT_FIELDS)
    if unknown_fields:
        names = ", ".join(repr(field) for field in unknown_fields)
        raise InputValidationError(f"unknown input field(s): {names}")
    if "task" not in payload:
        raise InputValidationError("input.task is required")
    task = payload["task"]
    if not isinstance(task, str):
        raise InputValidationError("input.task must be a string")
    if not task.strip():
        raise InputValidationError("input.task must not be empty")

    context = payload.get("context", "")
    if not isinstance(context, str):
        raise InputValidationError("input.context must be a string")

    return TaskInput(task=task, context=context)


def _serialize_output(output: ReviewOutput) -> dict[str, bool | float | str]:
    """Convert the typed domain output to its stable JSON wire shape."""
    if not isinstance(output, ReviewOutput):
        raise TypeError("module returned an invalid output type")
    if not isinstance(output.decision, Decision):
        raise TypeError("module returned an invalid decision")
    if not isinstance(output.reason, str):
        raise TypeError("module returned an invalid reason")
    if isinstance(output.confidence, bool) or not isinstance(output.confidence, int | float):
        raise TypeError("module returned an invalid confidence")
    if not math.isfinite(output.confidence) or not 0.0 <= output.confidence <= 1.0:
        raise ValueError("module returned confidence outside [0.0, 1.0]")
    return {
        "confidence": output.confidence,
        "review": output.review,
        "reason": output.reason,
    }


async def _decide(module: ShouldReviewModule, inputs: list[TaskInput]) -> list[dict]:
    return [_serialize_output(await module.decide(task_input)) for task_input in inputs]


async def _evaluate(
    module: ShouldReviewModule, records: list[dict[str, Any]]
) -> list[dict[str, Any]]:
    results: list[dict[str, Any]] = []
    for index, record in enumerate(records):
        if not isinstance(record, dict):
            raise SchemaInvalidError(f"record {index} must be a JSON object")
        try:
            task_input = _parse_input(record.get("input"))
        except InputValidationError as exc:
            raise SchemaInvalidError(f"record {index}.input: {exc}") from exc
        expected = record.get("expected")
        if not isinstance(expected, dict):
            raise SchemaInvalidError(f"record {index}.expected must be a JSON object")
        expected_review = expected.get("review")
        if not isinstance(expected_review, bool):
            raise SchemaInvalidError(f"record {index}.expected.review must be a boolean")
        if not isinstance(expected.get("reason"), str):
            raise SchemaInvalidError(f"record {index}.expected.reason must be a string")
        canary = record.get("canary", False)
        if not isinstance(canary, bool):
            raise SchemaInvalidError(f"record {index}.canary must be a boolean")
        prediction = await module.decide(task_input)
        prediction_wire = _serialize_output(prediction)
        expected_typed = dict(expected)
        expected_typed["review"] = Decision.REVIEW if expected_review else Decision.DO_NOT_REVIEW
        score = module.metric(
            Example(
                input=task_input,
                expected=expected_typed,
                prediction=prediction,
                canary=canary,
            )
        )
        if isinstance(score, bool) or not isinstance(score, int | float):
            raise TypeError(f"record {index}: module metric is not numeric")
        if not math.isfinite(score) or not 0.0 <= score <= 1.0:
            raise ValueError(f"record {index}: module metric is outside [0.0, 1.0]")
        results.append({"prediction": prediction_wire, "score": score})
    return results


def _write_json(payload: object) -> None:
    sys.stdout.write(json.dumps(payload, sort_keys=True) + "\n")


def _write_error(exc: Exception) -> int:
    error: dict[str, Any] = {
        "message": str(exc) or "module CLI failed",
        "type": type(exc).__name__,
    }
    if isinstance(exc, SchemaInvalidError):
        error["code"] = "SCHEMA_INVALID"
    _write_json({"error": error})
    print(f"cambium.modules.should_review: {type(exc).__name__}: {exc}", file=sys.stderr)
    return 1


def main() -> int:
    """Read one JSON request and emit one JSON response."""
    try:
        payload = json.loads(sys.stdin.buffer.read(), object_pairs_hook=_reject_duplicate_fields)
        module = ShouldReviewModule()
        if isinstance(payload, dict) and "operation" in payload:
            operation = payload.get("operation")
            if operation == "decide":
                inputs = payload.get("inputs")
                if not isinstance(inputs, list):
                    raise InputValidationError("decide.inputs must be a JSON array")
                decisions = asyncio.run(_decide(module, [_parse_input(item) for item in inputs]))
                result = {"results": decisions}
            elif operation == "evaluate":
                records = payload.get("records")
                if not isinstance(records, list):
                    raise InputValidationError("evaluate.records must be a JSON array")
                result = {"results": asyncio.run(_evaluate(module, records))}
            else:
                raise InputValidationError(f"unknown operation: {operation!r}")
        else:
            result = asyncio.run(_decide(module, [_parse_input(payload)]))[0]
        _write_json(result)
    except Exception as exc:
        return _write_error(exc)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
