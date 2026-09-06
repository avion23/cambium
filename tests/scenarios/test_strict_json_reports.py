"""Experiment report writers emit strict JSON (no nan/inf literals).

Guards the serialization seam: ``benchmark.write_json_report`` and the
``prompt_optimize`` dump sites sanitize non-finite floats to 0.0 before a
``allow_nan=False`` dump, so a provider-reported NaN cost cannot poison
``report.json`` (which Python re-reads silently but strict consumers reject).
"""

from __future__ import annotations

import json

import pytest

from cambium.benchmark import json_finite, write_json_report
from cambium.prompt_optimize import grounded_feedback


def _reject_constant(name: str) -> None:
    raise AssertionError(f"non-finite JSON constant in strict document: {name}")


def strict_loads(text: str) -> object:
    """Loader that rejects NaN/Infinity/-Infinity literals."""
    return json.loads(text, parse_constant=_reject_constant)


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        (float("nan"), 0.0),
        (float("inf"), 0.0),
        (float("-inf"), 0.0),
        (
            {"cost": [float("nan")], "nested": {"score": float("inf")}},
            {"cost": [0.0], "nested": {"score": 0.0}},
        ),
        ("nan", "nan"),
        (
            {"f": 1.5, "i": 3, "b": True, "none": None, "s": "1e400"},
            {"f": 1.5, "i": 3, "b": True, "none": None, "s": "1e400"},
        ),
    ],
)
def test_json_finite_table(raw: object, expected: object) -> None:
    assert json_finite(raw) == expected


def test_json_finite_preserves_finite_float_and_int_types() -> None:
    assert json_finite(2.5) == 2.5
    assert type(json_finite(2.5)) is float
    assert json_finite(3) == 3
    assert type(json_finite(3)) is int


def test_strict_loader_rejects_nan_literals() -> None:
    with pytest.raises(AssertionError, match="NaN"):
        strict_loads(json.dumps({"x": float("nan")}))


# Fixed finite fixture in the shape run_case builds; the byte-stability test
# pins writer output to the pre-change expression ``json.dumps(row, indent=2)``
# + newline so the report shape cannot drift.
FIXTURE_ROW = {
    "id": "case-1",
    "split": "val",
    "passed": True,
    "score": 0.95,
    "elapsed_s": 12.345,
    "calls": 7,
    "tokens": 4321,
    "cost_usd": 0.0123,
    "head": "a" * 40,
    "base": "b" * 40,
    "changed": ["src/x.py"],
    "providers": ["p1", "p2"],
    "children": 2,
    "peak_pending_children": 2,
    "directory": "/tmp/unused",
    "turn_heads": ["c" * 40],
    "source": None,
    "family": "utility-join",
    "rollovers": 0,
    "summary_calls": 1,
    "failed_provider_calls": 0,
    "malformed_actions": 0,
    "tool_failures": 0,
    "output_tokens": 2100,
    "cached_tokens": 100,
    "task_providers": {"case-1": ["p1"]},
    "child_policies": [{"child_task_id": "t2", "resolved_context_mode": "isolated"}],
    "feedback": "exit=0; check=True\n",
}


def test_write_json_report_byte_identical_for_finite_rows(tmp_path) -> None:
    expected = json.dumps(FIXTURE_ROW, indent=2) + "\n"
    path = tmp_path / "report.json"
    write_json_report(path, FIXTURE_ROW)
    assert path.read_text(encoding="utf-8") == expected


def test_write_json_report_survives_nan_poisoned_row(tmp_path) -> None:
    row = {**FIXTURE_ROW, "cost_usd": float("nan"), "score": float("inf")}
    row["child_policies"] = [{"weight": float("-inf")}]
    path = tmp_path / "report.json"
    write_json_report(path, row)
    text = path.read_text(encoding="utf-8")
    assert "NaN" not in text and "Infinity" not in text
    loaded = strict_loads(text)
    assert loaded["cost_usd"] == 0.0
    assert loaded["score"] == 0.0
    assert loaded["child_policies"][0]["weight"] == 0.0


def test_grounded_feedback_survives_nan_poisoned_row() -> None:
    row = {**FIXTURE_ROW, "score": float("nan"), "elapsed_s": float("inf")}
    feedback = grounded_feedback("coding", {"coding": "p", "summary": "s"}, row)
    assert "<raw-row>" in feedback
    raw_text = feedback.split("<raw-row>\n", 1)[1]
    loaded = strict_loads(raw_text)
    assert loaded["score"] == 0.0
    assert loaded["elapsed_s"] == 0.0
