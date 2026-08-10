"""Subprocess tests for the example module's JSON CLI wire contract.

Every test spawns ``python -m cambium.modules.example`` and exchanges one
JSON document over stdin/stdout.  Coverage is ported from the superseded
``wt-module-cli`` branch and extended to the current decide/evaluate
operation envelope.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[5]
SRC_DIR = str(REPO_ROOT / "src")


def _run_cli(payload: str) -> subprocess.CompletedProcess[str]:
    env = dict(os.environ)
    env["PYTHONPATH"] = os.pathsep.join(filter(None, [SRC_DIR, env.get("PYTHONPATH")]))
    return subprocess.run(
        [sys.executable, "-m", "cambium.modules.example"],
        cwd=REPO_ROOT,
        input=payload,
        capture_output=True,
        text=True,
        env=env,
        timeout=120,
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
                "task": "Add the API change, update the worker, build the tests.",
            }
        )
    )

    assert result.returncode == 0, result.stderr
    assert result.stderr == ""
    output = _one_json_object(result.stdout)
    assert set(output) == {"confidence", "decompose", "reason"}
    assert output["decompose"] is True
    assert isinstance(output["reason"], str)
    assert isinstance(output["confidence"], float)


def test_cli_applies_optional_context_default() -> None:
    result = _run_cli(json.dumps({"task": "Rename one function."}))

    assert result.returncode == 0, result.stderr
    output = _one_json_object(result.stdout)
    assert output["decompose"] is False


def test_cli_rejects_duplicate_task_fields() -> None:
    result = _run_cli('{"task": 42, "task": "Rename one function."}')

    assert result.returncode != 0
    response = _one_json_object(result.stdout)
    assert response["error"]["type"] == "JSONDecodeError"
    assert "duplicate JSON object field: 'task'" in response["error"]["message"]
    assert response["error"]["message"] in result.stderr


def test_cli_rejects_duplicate_context_fields() -> None:
    result = _run_cli(
        '{"task": "Rename one function.", "context": "first", "context": "second"}'
    )

    assert result.returncode != 0
    response = _one_json_object(result.stdout)
    assert response["error"]["type"] == "JSONDecodeError"
    assert "duplicate JSON object field: 'context'" in response["error"]["message"]
    assert response["error"]["message"] in result.stderr


def test_cli_rejects_duplicate_fields_inside_nested_object() -> None:
    result = _run_cli(
        '{"task": "Rename one function.", "context": {"note": "first", "note": "second"}}'
    )

    assert result.returncode != 0
    response = _one_json_object(result.stdout)
    assert response["error"]["type"] == "JSONDecodeError"
    assert "duplicate JSON object field: 'note'" in response["error"]["message"]
    assert response["error"]["message"] in result.stderr


@pytest.mark.parametrize(
    ("payload", "message"),
    [
        ({"task": "Do the thing", "extra": "reject me"}, "unknown input field"),
        ({"context": "missing task"}, "input.task is required"),
        ({"task": 42}, "input.task must be a string"),
        ({"task": "   "}, "input.task must not be empty"),
        ({"task": "Do the thing", "context": None}, "input.context must be a string"),
    ],
)
def test_cli_rejects_invalid_typed_input(payload: dict, message: str) -> None:
    result = _run_cli(json.dumps(payload))

    assert result.returncode != 0
    error = _one_json_object(result.stdout)["error"]
    assert isinstance(error, dict)
    assert message in error["message"]
    assert message in result.stderr


@pytest.mark.parametrize("payload", ["", "[]", "{\"task\": \"one\"} {\"task\": \"two\"}"])
def test_cli_returns_json_error_for_invalid_input_document(payload: str) -> None:
    result = _run_cli(payload)

    assert result.returncode != 0
    response = _one_json_object(result.stdout)
    assert set(response) == {"error"}
    assert isinstance(response["error"]["message"], str)
    assert result.stderr


def test_cli_decide_operation_returns_results_array() -> None:
    result = _run_cli(
        json.dumps(
            {
                "operation": "decide",
                "inputs": [
                    {"task": "Add the API change, update the worker, build the tests."},
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
    assert set(decisions[0]) == {"confidence", "decompose", "reason"}
    assert decisions[0]["decompose"] is True
    assert decisions[1]["decompose"] is False


def test_cli_decide_operation_requires_inputs_array() -> None:
    result = _run_cli(
        json.dumps({"operation": "decide", "inputs": {"task": "Split the work."}})
    )

    assert result.returncode != 0
    error = _one_json_object(result.stdout)["error"]
    assert error["type"] == "InputValidationError"
    assert "decide.inputs must be a JSON array" in error["message"]


def test_cli_evaluate_operation_returns_scores() -> None:
    result = _run_cli(
        json.dumps(
            {
                "operation": "evaluate",
                "records": [
                    {
                        "input": {
                            "task": "Add the API change, update the worker, build the tests."
                        },
                        "expected": {"decompose": True, "reason": "parallel work"},
                    },
                    {
                        "input": {"task": "Rename one function."},
                        "expected": {"decompose": False, "reason": "atomic"},
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
    assert results[0]["prediction"]["decompose"] is True
    assert results[0]["score"] == 1.0
    assert results[1]["prediction"]["decompose"] is False
    assert results[1]["score"] == 1.0


def test_cli_rejects_unknown_operation() -> None:
    result = _run_cli(json.dumps({"operation": "publish", "inputs": []}))

    assert result.returncode != 0
    error = _one_json_object(result.stdout)["error"]
    assert error["type"] == "InputValidationError"
    assert "unknown operation: 'publish'" in error["message"]
    assert error["message"] in result.stderr
