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


def _generation_state() -> SimpleNamespace:
    """Return the mutable state fields used by generation event handlers."""
    return SimpleNamespace(
        task_id="task",
        generation=3,
        turn=0,
        loop=asyncio.get_running_loop(),
        heartbeat_phase=None,
        last_heartbeat=None,
        handle=SimpleNamespace(last_heartbeat=None),
    )


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

    handled = asyncio.run(
        runtime._handle_generation_message(
            state,
            {"type": "synthetic_external_probe", "task_id": "other", "generation": 2},
        )
    )
    assert handled is False
    assert runtime.records[-1] == (
        "protocol",
        {
            "task_id": "task",
            "generation": 3,
            "note": "synthetic_external_probe rejected: identity mismatch",
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


def test_ready_identity_fence_runs_after_request_correlation(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    runtime = _DispatchProbe()
    state = SimpleNamespace(task_id="task", generation=3, init_rid="init", proc=None)
    killed: list[Any] = []

    async def kill(proc: Any) -> None:
        killed.append(proc)

    monkeypatch.setattr(supervisor, "_kill_worker", kill)
    handled = asyncio.run(
        runtime._handle_generation_message(
            state,
            {
                "type": "ready",
                "request_id": "init",
                "task_id": "other",
                "generation": 2,
                "proto": 1,
            },
        )
    )

    assert handled is True
    assert state.protocol_reason == "ready_identity_mismatch"
    assert killed == [None]
    assert runtime.records == [
        (
            "protocol",
            {
                "task_id": "task",
                "generation": 3,
                "request_id": "init",
                "note": "ready identity mismatch",
                "expected_task_id": "task",
                "expected_generation": 3,
            },
        )
    ]


def test_eof_probe_rejects_mismatched_pong_identity(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    runtime = _DispatchProbe()
    runtime._next_rid = lambda: "pong-rid"  # type: ignore[method-assign]

    async def write_json(*_args: Any, **_kwargs: Any) -> bool:
        return True

    monkeypatch.setattr(supervisor, "_write_json", write_json)
    async def scenario() -> bool:
        loop = asyncio.get_running_loop()
        state = SimpleNamespace(
            task_id="task",
            generation=3,
            proc=SimpleNamespace(returncode=None),
            loop=loop,
            wall_deadline=loop.time() + 1.0,
            messages=asyncio.Queue(),
        )
        state.messages.put_nowait(
            {
                "type": "pong",
                "request_id": "pong-rid",
                "task_id": "other",
                "generation": 2,
            }
        )
        state.messages.put_nowait(None)
        return await runtime._probe_after_eof(state)

    assert asyncio.run(scenario()) is False
    assert [kind for kind, _payload in runtime.records] == ["ping", "protocol", "protocol"]
    assert runtime.records[1][1]["note"] == "pong rejected: identity mismatch"
    assert runtime.records[2][1]["note"] == "missing correlated pong after EOF"


@pytest.mark.parametrize(
    ("message_type", "message"),
    [
        (
            "tool_event",
            {
                "type": "tool_event",
                "task_id": "other",
                "generation": 3,
                "tool": "run_shell",
                "turn": 7,
                "ok": True,
                "duration_ms": 1,
            },
        ),
        (
            "tool_event",
            {
                "type": "tool_event",
                "task_id": "task",
                "generation": 2,
                "tool": "run_shell",
                "turn": 7,
                "ok": True,
                "duration_ms": 1,
            },
        ),
        (
            "heartbeat",
            {
                "type": "heartbeat",
                "task_id": "other",
                "generation": 3,
                "turn": 7,
                "status": "working",
            },
        ),
        (
            "heartbeat",
            {
                "type": "heartbeat",
                "task_id": "task",
                "generation": 2,
                "turn": 7,
                "status": "working",
            },
        ),
        (
            "heartbeat",
            {
                "type": "heartbeat",
                "task_id": "task",
                "generation": True,
                "turn": 7,
                "status": "working",
            },
        ),
    ],
)
def test_stale_generation_events_cannot_mutate_or_persist_as_current(
    message_type: str, message: dict[str, Any]
) -> None:
    runtime = _DispatchProbe()

    async def scenario() -> SimpleNamespace:
        state = _generation_state()
        handled = await runtime._handle_generation_message(state, message)
        assert handled is False
        return state

    state = asyncio.run(scenario())

    assert state.turn == 0
    assert state.last_heartbeat is None
    assert state.handle.last_heartbeat is None
    assert [kind for kind, _payload in runtime.records] == ["protocol"]
    kind, payload = runtime.records[0]
    assert kind == "protocol"
    assert payload["task_id"] == "task"
    assert payload["generation"] == 3
    assert payload["note"] == f"{message_type} rejected: identity mismatch"


@pytest.mark.parametrize(
    ("message_type", "message"),
    [
        (
            "tool_event",
            {
                "type": "tool_event",
                "task_id": "task",
                "generation": 3,
                "tool": "run_shell",
                "turn": 7,
                "ok": True,
                "duration_ms": 1,
            },
        ),
        (
            "heartbeat",
            {
                "type": "heartbeat",
                "task_id": "task",
                "generation": 3,
                "turn": 7,
                "status": "working",
            },
        ),
    ],
)
def test_current_generation_events_mutate_and_persist(
    message_type: str, message: dict[str, Any]
) -> None:
    runtime = _DispatchProbe()

    async def scenario() -> SimpleNamespace:
        state = _generation_state()
        handled = await runtime._handle_generation_message(state, message)
        assert handled is False
        return state

    state = asyncio.run(scenario())

    assert state.turn == 7
    if message_type == "heartbeat":
        assert state.last_heartbeat is not None
        assert state.handle.last_heartbeat == state.last_heartbeat
    else:
        assert state.last_heartbeat is None
    assert [kind for kind, _payload in runtime.records] == [message_type]
    kind, payload = runtime.records[0]
    assert kind == message_type
    assert payload["task_id"] == "task"
    assert payload["generation"] == 3
    assert payload["turn"] == 7


@pytest.mark.parametrize(
    ("message_type", "message"),
    [
        (
            "tool_event",
            {"type": "tool_event", "tool": "run_shell", "turn": 7, "ok": True},
        ),
        ("heartbeat", {"type": "heartbeat", "turn": 7, "status": "working"}),
    ],
)
def test_generation_events_without_identity_remain_compatible(
    message_type: str, message: dict[str, Any]
) -> None:
    runtime = _DispatchProbe()

    async def scenario() -> SimpleNamespace:
        state = _generation_state()
        handled = await runtime._handle_generation_message(state, message)
        assert handled is False
        return state

    state = asyncio.run(scenario())

    assert state.turn == 7
    assert [kind for kind, _payload in runtime.records] == [message_type]


def test_duplicate_ready_is_terminal_protocol_failure(monkeypatch: pytest.MonkeyPatch) -> None:
    runtime = _DispatchProbe()
    killed: list[Any] = []

    async def kill(proc: Any) -> None:
        killed.append(proc)

    monkeypatch.setattr(supervisor, "_kill_worker", kill)
    state = SimpleNamespace(
        task_id="task",
        generation=3,
        init_rid="init",
        phase="run",
        protocol_reason=None,
        proc="worker",
    )

    handled = asyncio.run(
        runtime._handle_generation_message(
            state,
            {
                "type": "ready",
                "request_id": "init",
                "task_id": "task",
                "generation": 3,
                "proto": 1,
            },
        )
    )

    assert handled is True
    assert state.protocol_reason == "duplicate_ready"
    assert killed == ["worker"]
    assert runtime.records[-1][1]["note"] == "ready received outside ready phase"


def _result_state() -> SimpleNamespace:
    return SimpleNamespace(
        task_id="task",
        generation=3,
        turn=0,
        run_rid="run-1",
        correlated=False,
        envelope=None,
        sandbox_failure_reason=None,
        protocol_failure=None,
        proc=None,
    )


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("status", "mystery"),
        ("summary", ["not", "text"]),
        ("diff", {"bad": "shape"}),
        ("diff_truncated", 1),
        ("requires_commit", "yes"),
        ("commits", [1]),
        ("files_changed", "a.py"),
        ("failure_reason", {"bad": "shape"}),
    ],
)
def test_malformed_result_envelope_fails_at_wire_boundary(
    field: str, value: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    runtime = _DispatchProbe()

    async def kill(_proc: Any) -> None:
        return None

    monkeypatch.setattr(supervisor, "_kill_worker", kill)
    message: dict[str, Any] = {
        "type": "result_envelope",
        "request_id": "run-1",
        "task_id": "task",
        "generation": 3,
        "status": "succeeded",
        "summary": "done",
        "diff": "",
        "diff_truncated": False,
        "commits": [],
        "files_changed": [],
    }
    message[field] = value
    state = _result_state()

    handled = asyncio.run(runtime._handle_generation_message(state, message))

    assert handled is True
    assert state.envelope is None
    assert state.protocol_failure == "INVALID_RESULT_ENVELOPE"
    rejection = next(
        payload
        for kind, payload in runtime.records
        if kind == "protocol" and payload.get("note") == "result rejected: invalid field(s)"
    )
    assert field in rejection["fields"]


def test_fatal_error_cannot_be_superseded_by_late_success(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    runtime = _DispatchProbe()
    state = _result_state()

    async def kill(_proc: Any) -> None:
        return None

    monkeypatch.setattr(supervisor, "_kill_worker", kill)

    fatal = asyncio.run(
        runtime._handle_generation_message(
            state,
            {
                "type": "fatal_error",
                "task_id": "task",
                "generation": 3,
                "error_type": {"malformed": True},
            },
        )
    )
    assert fatal is True
    assert state.protocol_failure == "fatal_error"

    late = asyncio.run(
        runtime._handle_generation_message(
            state,
            {
                "type": "result_envelope",
                "request_id": "run-1",
                "task_id": "task",
                "generation": 3,
                "status": "succeeded",
                "summary": "late success",
                "diff": "",
                "diff_truncated": False,
                "commits": [],
                "files_changed": [],
            },
        )
    )
    assert late is True
    assert state.envelope is None
    assert state.protocol_failure == "fatal_error"
