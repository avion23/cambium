from __future__ import annotations

import ast
import asyncio
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from cambium import supervisor

ROOT = Path(__file__).resolve().parents[2]
WORKER_PATH = ROOT / "src" / "cambium" / "worker.py"
SUPERVISOR_PATH = ROOT / "src" / "cambium" / "supervisor.py"
_AGENT_ACTION_TYPES = frozenset({"plan", "tool_call", "finish"})
_COMPATIBILITY_INPUT_TYPES = frozenset({"result", "exit", "error", "log"})
_DISPATCH_FUNCTIONS = frozenset(
    {
        "_handle_generation_protocol_message",
        "_handle_generation_lifecycle_message",
        "_handle_generation_event_message",
    }
)


def _literal_worker_message_types() -> set[str]:
    """Return literal built-in worker wire types, excluding model actions."""
    tree = ast.parse(WORKER_PATH.read_text(encoding="utf-8"))
    message_types: set[str] = set()
    for node in ast.walk(tree):
        if not isinstance(node, ast.Dict):
            continue
        for key, value in zip(node.keys, node.values, strict=True):
            if (
                isinstance(key, ast.Constant)
                and key.value == "type"
                and isinstance(value, ast.Constant)
                and isinstance(value.value, str)
            ):
                message_types.add(value.value)
    return message_types - _AGENT_ACTION_TYPES


def _dispatch_message_types() -> set[str]:
    """Return message types named by the built-in generation dispatcher."""
    tree = ast.parse(SUPERVISOR_PATH.read_text(encoding="utf-8"))
    message_types: set[str] = set()
    for function in ast.walk(tree):
        if (
            not isinstance(function, ast.AsyncFunctionDef)
            or function.name not in _DISPATCH_FUNCTIONS
        ):
            continue
        for node in ast.walk(function):
            if not isinstance(node, ast.Compare) or not isinstance(node.left, ast.Name):
                continue
            if node.left.id != "mtype" or len(node.ops) != 1 or len(node.comparators) != 1:
                continue
            comparator = node.comparators[0]
            if isinstance(node.ops[0], ast.Eq) and isinstance(comparator, ast.Constant):
                if isinstance(comparator.value, str):
                    message_types.add(comparator.value)
            elif isinstance(node.ops[0], ast.In) and isinstance(comparator, ast.Tuple | ast.Set):
                for element in comparator.elts:
                    if isinstance(element, ast.Constant) and isinstance(element.value, str):
                        message_types.add(element.value)
    return message_types


def _assert_builtin_protocol_complete(
    worker_types: set[str], supervisor_types: set[str]
) -> None:
    missing = worker_types - supervisor_types
    assert not missing, f"unhandled built-in worker message types: {sorted(missing)!r}"


def test_builtin_worker_messages_have_supervisor_dispatch_obligations() -> None:
    worker_types = _literal_worker_message_types()
    supervisor_types = _dispatch_message_types()

    _assert_builtin_protocol_complete(worker_types, supervisor_types)
    assert supervisor_types - worker_types == _COMPATIBILITY_INPUT_TYPES


def test_synthetic_builtin_message_fails_completeness_contract() -> None:
    worker_types = _literal_worker_message_types() | {"synthetic_protocol_probe"}

    with pytest.raises(AssertionError, match="synthetic_protocol_probe"):
        _assert_builtin_protocol_complete(worker_types, _dispatch_message_types())


class _DispatchProbe(supervisor._Runtime):
    def __init__(self) -> None:
        self.records: list[tuple[str, dict[str, Any]]] = []

    async def emit(self, kind: str, **payload: Any) -> None:
        self.records.append((kind, payload))


def test_ok_ack_is_known_but_unknown_wire_type_stays_visible() -> None:
    runtime = _DispatchProbe()
    state = SimpleNamespace(task_id="task", generation=3)

    handled = asyncio.run(
        runtime._handle_generation_message(
            state,
            {"type": "ok", "request_id": "cancel-1", "task_id": "task", "generation": 3},
        )
    )
    assert handled is False
    assert runtime.records == []

    handled = asyncio.run(
        runtime._handle_generation_message(
            state,
            {"type": "synthetic_external_probe", "task_id": "task", "generation": 3},
        )
    )
    assert handled is False
    assert runtime.records[-1] == (
        "protocol",
        {
            "task_id": "task",
            "type": "synthetic_external_probe",
            "note": "unhandled message type 'synthetic_external_probe'",
            "generation": 3,
        },
    )


def test_ok_ack_rejects_stale_worker_identity() -> None:
    runtime = _DispatchProbe()
    state = SimpleNamespace(task_id="task", generation=3)

    handled = asyncio.run(
        runtime._handle_generation_message(
            state,
            {"type": "ok", "request_id": "cancel-1", "task_id": "other", "generation": 2},
        )
    )
    assert handled is False
    assert runtime.records == [
        (
            "protocol",
            {
                "task_id": "task",
                "generation": 3,
                "note": "ok rejected: identity mismatch",
            },
        )
    ]
