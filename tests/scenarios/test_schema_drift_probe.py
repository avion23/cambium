"""Schema-vs-implementation drift regression (probe/schema-drift).

Schemas describe; implementations prescribe. Each test asserts both sides
reject the same invalid calls and accept the same valid calls.
"""

from __future__ import annotations

import asyncio
import json
import tempfile
from pathlib import Path
from types import SimpleNamespace

import pytest

from cambium.modules.base import (
    Example,
    _reject_duplicate_module_fields,
    _reject_module_json_constant,
    evaluate_split_async,
)
from cambium.modules.example.decide import Decision, DecomposeOutput, TaskInput
from cambium.schemas import FINISH_ACTION_SCHEMA, TOOL_SCHEMAS, validate_tool_call
from cambium.tools import ToolContext, _git_op, run_tool


def _schema(name: str) -> dict:
    return next(s for s in TOOL_SCHEMAS if s["name"] == name)


def _run(name: str, args: dict, ctx: ToolContext):
    return asyncio.run(run_tool(name, args, ctx))


def test_run_shell_schema_matches_impl_bounds() -> None:
    schema = _schema("run_shell")
    assert validate_tool_call(schema, {"cmd": []})
    assert validate_tool_call(schema, {"cmd": ["ls"], "timeout_s": 0})
    assert validate_tool_call(schema, {"cmd": ["ls"], "timeout_s": -1})
    assert not validate_tool_call(schema, {"cmd": ["ls"]})
    assert not validate_tool_call(schema, {"cmd": ["ls"], "timeout_s": 30})


def test_read_batch_schema_matches_impl_nonempty() -> None:
    schema = _schema("read_batch")
    assert validate_tool_call(schema, {"paths": []})
    assert not validate_tool_call(schema, {"paths": ["a.txt"]})


def test_finish_schema_rejects_whitespace_summary() -> None:
    assert validate_tool_call(
        FINISH_ACTION_SCHEMA,
        {"type": "finish", "summary": " ", "objective_met": True},
    )
    assert not validate_tool_call(
        FINISH_ACTION_SCHEMA,
        {"type": "finish", "summary": "done", "objective_met": True},
    )


def test_delegate_kind_schema_matches_impl() -> None:
    schema = _schema("delegate")
    policy = {"context_mode": "fresh", "placement": "spread"}
    assert validate_tool_call(
        schema,
        {"child_task_id": "c", "kind": "message", "spec": {"task": "t", **policy}},
    )
    assert not validate_tool_call(
        schema,
        {"child_task_id": "c", "kind": "test", "spec": {"task": "t", **policy}},
    )
    assert not validate_tool_call(schema, {"child_task_id": "c", "spec": {"task": "t"}})


def test_git_op_rejects_unsafe_both_layers(tmp_path: Path) -> None:
    ctx = ToolContext(tmp_path)
    for op in ("checkout", "reset"):
        assert validate_tool_call(_schema("git_op"), {"op": op, "args": ""})
        with pytest.raises(Exception, match="not allowlisted"):
            asyncio.run(_git_op({"op": op, "args": ""}, ctx))
        result = _run("git_op", {"op": op, "args": ""}, ctx)
        assert not result.ok
        assert "must be one of" in (result.error or "")


def test_evaluate_split_async_rejects_bool_scores() -> None:
    class BoolMetric:
        name = "bool-metric"

        async def decide(self, inputs):
            return DecomposeOutput(decision=Decision.DECOMPOSE, reason="r")

        def metric(self, example: Example) -> float:
            return True  # type: ignore[return-value]

    class FakeLoader:
        def load_split(self, split):
            return [
                Example(
                    input=TaskInput(task="t", context=""),
                    expected={"decompose": Decision.DECOMPOSE, "reason": "r"},
                )
            ]

    with pytest.raises(TypeError, match="not numeric"):
        asyncio.run(evaluate_split_async(BoolMetric(), FakeLoader(), None))


def test_module_wire_rejects_nonstandard_constants() -> None:
    with pytest.raises(Exception, match="invalid JSON constant"):
        json.loads(
            '{"task": "hi", "context": NaN}',
            object_pairs_hook=_reject_duplicate_module_fields,
            parse_constant=_reject_module_json_constant,
        )


def test_prompt_optimize_metric_rejects_bool() -> None:
    pytest.importorskip("dspy")
    from cambium.prompt_optimize import metric

    assert metric(None, SimpleNamespace(score=True, report="r")).score == 0.0
    assert metric(None, SimpleNamespace(score=False, report="r")).score == 0.0
    assert metric(None, SimpleNamespace(score=0.9, report="r")).score == 0.9


def test_read_only_delegate_defaults_still_fresh_inherit() -> None:
    from cambium.child_policy import complete_child_policy

    single = complete_child_policy({}, siblings=1, read_only=True)
    assert (single["context_mode"], single["placement"]) == ("fresh", "inherit")
    batch = complete_child_policy({}, siblings=4, read_only=True)
    assert (batch["context_mode"], batch["placement"]) == ("fresh", "inherit")


def test_resume_rejection_feedback_round_trip() -> None:
    from cambium.supervisor import _batch_rejection_feedback
    from cambium.worker import _validate_resume

    ref = "t/epoch-001-aaaaaaaaaaaaaaaa-bbbbbbbbbbbbbbbb.json"
    feedback = _batch_rejection_feedback(1, 0, [("ChildPolicyError", "bad")])
    assert feedback
    validated = _validate_resume(
        {
            "checkpoint_ref": ref,
            "epoch": 1,
            "child_results": [],
            "child_results_truncated": False,
            "workspace_changed": False,
            "rejection_feedback": feedback,
        }
    )
    assert validated is not None
    assert validated["rejection_feedback"] == feedback
    with tempfile.TemporaryDirectory():
        pass
