"""In-process tests for the should_review module's JSON CLI wire contract.

Every test feeds one JSON document to the module's ``main()`` entry point
in-process (no interpreter spawn) and exchanges one JSON document over the
module's stdin/stdout streams.  The ``python -m`` subprocess boundary itself
is exercised by the bench harness and the module-conformance scenarios, so
these tests do not pay the interpreter-spawn cost per case.  Coverage
mirrors the reference module and extends it with empty/max/unicode input and
determinism probes.
"""

from __future__ import annotations

import contextlib
import io
import json
import subprocess
import sys

import pytest

from cambium.modules.should_review.__main__ import main as _cli_main


class _FakeStdin:
    """Present one CLI payload as ``sys.stdin.buffer`` in the calling process."""

    def __init__(self, payload: str) -> None:
        self.buffer = io.BytesIO(payload.encode("utf-8"))


def _run_cli(payload: str) -> subprocess.CompletedProcess[str]:
    stdout = io.StringIO()
    stderr = io.StringIO()
    previous_stdin = sys.stdin
    sys.stdin = _FakeStdin(payload)
    try:
        with contextlib.redirect_stdout(stdout), contextlib.redirect_stderr(stderr):
            returncode = _cli_main()
    finally:
        sys.stdin = previous_stdin
    return subprocess.CompletedProcess(
        args=["cambium.modules.should_review.__main__"],
        returncode=returncode,
        stdout=stdout.getvalue(),
        stderr=stderr.getvalue(),
    )


def _one_json_object(stdout: str) -> dict:
    lines = stdout.splitlines()
    assert len(lines) == 1
    value = json.loads(lines[0])
    assert isinstance(value, dict)
    return value


def test_cli_returns_one_wire_object_and_no_diagnostics_on_success() -> None:
    result = _run_cli(
        json.dumps(
            {
                "task": "I cannot complete the payment migration.",
            }
        )
    )

    assert result.returncode == 0, result.stderr
    assert result.stderr == ""
    output = _one_json_object(result.stdout)
    assert set(output) == {"confidence", "reason", "review"}
    assert output["review"] is True
    assert isinstance(output["reason"], str)
    assert isinstance(output["confidence"], float)


def test_cli_applies_optional_context_default() -> None:
    result = _run_cli(json.dumps({"task": "Rename one function."}))

    assert result.returncode == 0, result.stderr
    output = _one_json_object(result.stdout)
    assert output["review"] is False


def test_cli_context_suppression() -> None:
    result = _run_cli(
        json.dumps({"task": "I cannot finish the migration.", "context": "already reviewed"})
    )

    assert result.returncode == 0, result.stderr
    output = _one_json_object(result.stdout)
    assert output["review"] is False


@pytest.mark.parametrize(
    ("payload", "field"),
    [
        ('{"task": 42, "task": "Rename one function."}', "task"),
        (
            '{"task": "Rename one function.", "context": "first", "context": "second"}',
            "context",
        ),
        (
            '{"task": "Rename one function.", "context": {"note": "first", "note": "second"}}',
            "note",
        ),
    ],
)
def test_cli_rejects_duplicate_task_fields(payload: str, field: str) -> None:
    result = _run_cli(payload)

    assert result.returncode != 0
    response = _one_json_object(result.stdout)
    assert response["error"]["type"] == "JSONDecodeError"
    assert f"duplicate JSON object field: '{field}'" in response["error"]["message"]
    assert response["error"]["message"] in result.stderr


@pytest.mark.parametrize(
    ("payload", "message"),
    [
        ({"task": "Do the thing", "extra": "reject me"}, "unknown input field"),
        ({"context": "missing task"}, "input.task is required"),
        ({"task": 42}, "input.task must be a string"),
        ({"task": "   "}, "input.task must not be empty"),
        ({"task": "", "context": ""}, "input.task must not be empty"),
        ({"task": "Do the thing", "context": None}, "input.context must be a string"),
        ({"task": "Do the thing", "context": 7}, "input.context must be a string"),
    ],
)
def test_cli_rejects_invalid_typed_input(payload: dict, message: str) -> None:
    result = _run_cli(json.dumps(payload))

    assert result.returncode != 0
    error = _one_json_object(result.stdout)["error"]
    assert isinstance(error, dict)
    assert message in error["message"]
    assert message in result.stderr


@pytest.mark.parametrize("payload", ["", "[]", '{"task": "one"} {"task": "two"}'])
def test_cli_returns_json_error_for_invalid_input_document(payload: str) -> None:
    result = _run_cli(payload)

    assert result.returncode != 0
    response = _one_json_object(result.stdout)
    assert set(response) == {"error"}
    assert isinstance(response["error"]["message"], str)
    assert result.stderr


def test_cli_handles_unicode_task() -> None:
    result = _run_cli(json.dumps({"task": "Fix the 'café' encoding in the Ünïcode handler."}))

    assert result.returncode == 0, result.stderr
    output = _one_json_object(result.stdout)
    assert set(output) == {"confidence", "reason", "review"}
    assert output["review"] is False


def test_cli_handles_max_length_task() -> None:
    long_task = "Update the webhook dispatcher " + ("word " * 120).strip()
    result = _run_cli(json.dumps({"task": long_task}))

    assert result.returncode == 0, result.stderr
    output = _one_json_object(result.stdout)
    assert set(output) == {"confidence", "reason", "review"}


def test_cli_output_is_deterministic() -> None:
    payload = json.dumps({"task": "The diff leaves a FIXME in auth.py."})
    first = _run_cli(payload)
    second = _run_cli(payload)

    assert first.returncode == second.returncode == 0
    assert first.stdout == second.stdout
    assert _one_json_object(first.stdout)["review"] is True


def test_cli_decide_operation_returns_results_array() -> None:
    result = _run_cli(
        json.dumps(
            {
                "operation": "decide",
                "inputs": [
                    {"task": "I cannot complete the payment migration."},
                    {"task": "Rename one function."},
                ],
            }
        )
    )

    assert result.returncode == 0, result.stderr
    assert result.stderr == ""
    output = _one_json_object(result.stdout)
    assert set(output) == {"results"}
    decisions = output["results"]
    assert isinstance(decisions, list)
    assert len(decisions) == 2
    assert set(decisions[0]) == {"confidence", "reason", "review"}
    assert decisions[0]["review"] is True
    assert decisions[1]["review"] is False


def test_cli_decide_operation_requires_inputs_array() -> None:
    result = _run_cli(json.dumps({"operation": "decide", "inputs": {"task": "Split the work."}}))

    assert result.returncode != 0
    error = _one_json_object(result.stdout)["error"]
    assert error["type"] == "InputValidationError"
    assert "decide.inputs must be a JSON array" in error["message"]
    assert "code" not in error


def test_cli_evaluate_operation_returns_scores() -> None:
    result = _run_cli(
        json.dumps(
            {
                "operation": "evaluate",
                "records": [
                    {
                        "input": {
                            "task": "I cannot complete the payment migration.",
                            "context": "",
                        },
                        "expected": {
                            "review": True,
                            "decompose": True,
                            "reason": "refusal",
                        },
                    },
                    {
                        "input": {"task": "Rename one function.", "context": ""},
                        "expected": {
                            "review": False,
                            "decompose": False,
                            "reason": "atomic",
                        },
                    },
                ],
            }
        )
    )

    assert result.returncode == 0, result.stderr
    output = _one_json_object(result.stdout)
    results = output["results"]
    assert isinstance(results, list)
    assert len(results) == 2
    assert results[0]["prediction"]["review"] is True
    assert results[0]["score"] == 1.0
    assert results[1]["prediction"]["review"] is False
    assert results[1]["score"] == 1.0


def test_cli_rejects_unknown_operation() -> None:
    result = _run_cli(json.dumps({"operation": "publish", "inputs": []}))

    assert result.returncode != 0
    error = _one_json_object(result.stdout)["error"]
    assert error["type"] == "InputValidationError"
    assert "unknown operation: 'publish'" in error["message"]
    assert error["message"] in result.stderr
    assert "code" not in error


def test_cli_evaluate_record_schema_error_emits_split_code() -> None:
    result = _run_cli(
        json.dumps(
            {
                "operation": "evaluate",
                "records": [
                    {
                        "input": {"task": "Do the thing"},
                        "expected": {"review": "yes", "decompose": True, "reason": "r"},
                    }
                ],
            }
        )
    )

    assert result.returncode != 0
    error = _one_json_object(result.stdout)["error"]
    assert error["code"] == "SCHEMA_INVALID"
    assert error["type"] == "SchemaInvalidError"
    assert "expected.review must be a boolean" in error["message"]


def test_cli_evaluate_record_input_error_emits_split_code() -> None:
    result = _run_cli(
        json.dumps(
            {
                "operation": "evaluate",
                "records": [
                    {
                        "input": {"task": 42},
                        "expected": {"review": False, "decompose": False, "reason": "r"},
                    }
                ],
            }
        )
    )

    assert result.returncode != 0
    error = _one_json_object(result.stdout)["error"]
    assert error["code"] == "SCHEMA_INVALID"
    assert "input.task must be a string" in error["message"]
