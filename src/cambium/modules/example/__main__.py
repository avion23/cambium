"""JSON stdin/stdout adapter for the ``should_decompose`` module."""

from __future__ import annotations

import asyncio
import json
import math
import sys
from typing import Any

from .decide import Decision, DecomposeOutput, ShouldDecomposeModule, TaskInput

_INPUT_FIELDS = frozenset({"task", "context"})


class InputValidationError(ValueError):
    """Raised when the CLI input does not match :class:`TaskInput`."""


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


def _serialize_output(output: DecomposeOutput) -> dict[str, bool | float | str]:
    """Convert the typed domain output to its stable JSON wire shape."""
    if not isinstance(output, DecomposeOutput):
        raise TypeError("module returned an invalid output type")
    if not isinstance(output.decision, Decision):
        raise TypeError("module returned an invalid decision")
    if not isinstance(output.reason, str):
        raise TypeError("module returned an invalid reason")
    if isinstance(output.confidence, bool) or not isinstance(output.confidence, (int, float)):
        raise TypeError("module returned an invalid confidence")
    if not math.isfinite(output.confidence) or not 0.0 <= output.confidence <= 1.0:
        raise ValueError("module returned confidence outside [0.0, 1.0]")

    return {
        "confidence": output.confidence,
        "decompose": output.decompose,
        "reason": output.reason,
    }


def _write_json(payload: object) -> None:
    sys.stdout.write(json.dumps(payload, sort_keys=True) + "\n")


def _write_error(exc: Exception) -> int:
    _write_json(
        {
            "error": {
                "message": str(exc) or "module CLI failed",
                "type": type(exc).__name__,
            }
        }
    )
    print(f"cambium.modules.example: {type(exc).__name__}: {exc}", file=sys.stderr)
    return 1


def main() -> int:
    """Run one typed module call from stdin and emit one JSON object."""
    try:
        payload = json.loads(
            sys.stdin.buffer.read(), object_pairs_hook=_reject_duplicate_fields
        )
        task_input = _parse_input(payload)
        output = asyncio.run(ShouldDecomposeModule().decide(task_input))
        _write_json(_serialize_output(output))
    except Exception as exc:
        return _write_error(exc)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
