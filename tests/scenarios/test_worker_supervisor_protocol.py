from __future__ import annotations

import asyncio
from types import SimpleNamespace
from typing import Any

import pytest

from cambium import supervisor


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
    assert runtime.records[-1][0] == "protocol"
    assert runtime.records[-1][1]["type"] == "synthetic_external_probe"

    handled = asyncio.run(
        runtime._handle_generation_message(
            state,
            {"type": "synthetic_external_probe", "task_id": "other", "generation": 2},
        )
    )
    assert handled is False
    assert [kind for kind, _payload in runtime.records] == ["protocol", "protocol"]


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
    assert [kind for kind, _payload in runtime.records] == ["protocol"]


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
    assert [kind for kind, _payload in runtime.records] == ["protocol"]


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
    assert runtime.records[-1][0] == "protocol"


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
    assert any(kind == "protocol" for kind, _payload in runtime.records)


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
