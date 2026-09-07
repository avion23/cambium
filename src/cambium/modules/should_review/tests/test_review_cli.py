"""Module-specific checks for the should_review JSON adapter.

The shared stdin/stdout schema, validation, and error mapping are exercised by
the reference example module. These tests keep only should_review's label and
rule wiring.
"""

from __future__ import annotations

import contextlib
import io
import json
import subprocess
import sys

from cambium.modules.should_review.__main__ import main as _cli_main


class _FakeStdin:
    def __init__(self, payload: str) -> None:
        self.buffer = io.BytesIO(payload.encode("utf-8"))


def _run_cli(payload: object) -> subprocess.CompletedProcess[str]:
    stdout = io.StringIO()
    stderr = io.StringIO()
    previous_stdin = sys.stdin
    sys.stdin = _FakeStdin(json.dumps(payload))
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


def _output(result: subprocess.CompletedProcess[str]) -> dict:
    assert result.returncode == 0, result.stderr
    assert result.stderr == ""
    value = json.loads(result.stdout)
    assert isinstance(value, dict)
    return value


def test_cli_wires_review_output() -> None:
    output = _output(_run_cli({"task": "I cannot complete the payment migration."}))

    assert set(output) == {"confidence", "reason", "review"}
    assert output["review"] is True


def test_cli_preserves_should_review_context_rule() -> None:
    output = _output(
        _run_cli(
            {
                "task": "I cannot finish the migration.",
                "context": "already reviewed",
            }
        )
    )

    assert output["review"] is False


def test_cli_evaluate_uses_review_metric() -> None:
    output = _output(
        _run_cli(
            {
                "operation": "evaluate",
                "records": [
                    {
                        "input": {"task": "I cannot complete the payment migration."},
                        "expected": {"review": True, "reason": "refusal"},
                    },
                    {
                        "input": {"task": "Rename one function."},
                        "expected": {"review": False, "reason": "atomic"},
                    },
                ],
            }
        )
    )

    results = output["results"]
    assert [row["prediction"]["review"] for row in results] == [True, False]
    assert [row["score"] for row in results] == [1.0, 1.0]
