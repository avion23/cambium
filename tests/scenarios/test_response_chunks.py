"""Bounded worker-to-supervisor final-response transport."""

from __future__ import annotations

import asyncio
import json
import threading
from types import SimpleNamespace
from typing import Any

import pytest

from cambium import supervisor, worker
from cambium.ipc import encode_message
from cambium.redact import Redactor


class _RuntimeProbe(supervisor._Runtime):
    def __init__(self, *, redactor: Redactor | None = None) -> None:
        self.records: list[tuple[str, dict[str, Any]]] = []
        self._response_session_bytes = 0
        self._redactor = redactor

    async def emit(self, kind: str, **payload: Any) -> None:
        self.records.append((kind, payload))


def _state() -> SimpleNamespace:
    return SimpleNamespace(
        task_id="task",
        generation=3,
        run_rid="run-1",
        turn=0,
        correlated=False,
        envelope=None,
        sandbox_failure_reason=None,
        protocol_failure=None,
        proc=None,
        response_chunks={},
        response_next_index=0,
        response_bytes=0,
        response_final_index=None,
        response_expected_count=None,
        response_expected_bytes=None,
        response_durable_chunks={},
        response_durable_next_index=0,
        response_durable_bytes=0,
        response_durable_final_index=None,
        response_redaction_done=False,
    )


def _chunk(index: int, text: str, *, final: bool = False, **changes: Any) -> dict[str, Any]:
    return {
        "type": "response_chunk",
        "task_id": "task",
        "generation": 3,
        "request_id": "run-1",
        "chunk_index": index,
        "text": text,
        "final": final,
        **changes,
    }


def _event(index: int, text: str, *, final: bool = False, **changes: Any) -> dict[str, Any]:
    message = _chunk(index, text, final=final, **changes)
    return {
        "kind": "response_chunk",
        "task_id": message.pop("task_id"),
        "generation": message.pop("generation"),
        "request_id": message.pop("request_id"),
        "payload": {
            "chunk_index": message["chunk_index"],
            "text": message["text"],
            "final": message["final"],
        },
    }


def _result(
    status: str = "succeeded", *, count: int = 2, response_bytes: int = 11
) -> dict[str, Any]:
    return {
        "kind": "result",
        "task_id": "task",
        "generation": 3,
        "request_id": "run-1",
        "payload": {
            "status": status,
            "response_chunk_count": count,
            "response_bytes": response_bytes,
        },
    }


def test_worker_splits_long_utf8_response_into_bounded_frames() -> None:
    response = ("α🙂" * 9_000) + ("z" * 13_000)

    chunks = worker._split_response_chunks(response)

    assert "".join(chunks) == response
    assert len(response.encode()) > 12 * 1024
    assert len(chunks) > 1
    assert all(len(chunk.encode()) <= worker.MAX_RESPONSE_CHUNK_BYTES for chunk in chunks)
    for index, text in enumerate(chunks):
        frame = _chunk(index, text, final=index == len(chunks) - 1)
        assert encode_message(frame) is not None


def test_finish_parser_preserves_response_larger_than_old_ceiling() -> None:
    response = "answer-" + ("x" * (12 * 1024 + 1))

    action = worker._parse_agent_action(
        json.dumps({"type": "finish", "summary": response, "objective_met": True})
    )

    assert action["summary"] == response


def test_worker_emits_chunks_then_bounded_result_without_response(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    sent: list[dict[str, Any]] = []

    async def capture(_writer: Any, message: dict[str, Any]) -> None:
        sent.append(message)

    monkeypatch.setattr(worker, "send", capture)
    response = "🙂" * 20_000
    outcome = {
        "request_id": "run-1",
        "task_id": "task",
        "generation": 3,
        "status": "succeeded",
        "response": response,
        "summary": "compact",
    }

    async def scenario() -> None:
        await worker._emit_response_chunks(None, outcome)
        await worker._emit_result_envelope(None, outcome)

    asyncio.run(scenario())

    result = sent[-1]
    assert "".join(frame["text"] for frame in sent[:-1]) == response
    assert result["type"] == "result_envelope"
    assert "response" not in result
    assert result["response_chunk_count"] == len(sent) - 1
    assert result["response_bytes"] == len(response.encode())
    assert encode_message(result) is not None
    assert len(json.dumps(result).encode()) < worker.MAX_RESPONSE_CHUNK_BYTES


def test_terminal_outcome_cancellation_keeps_prefix_and_never_inlines_response(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    sent: list[dict[str, Any]] = []

    async def capture(_writer: Any, message: dict[str, Any]) -> None:
        sent.append(message)

    monkeypatch.setattr(worker, "send", capture)
    response = "cancel-prefix-" + ("x" * (worker.MAX_RESPONSE_CHUNK_BYTES + 10))
    outcome = {
        "request_id": "run-1",
        "task_id": "task",
        "generation": 3,
        "status": "succeeded",
        "response": response,
        "summary": "compact",
    }
    stop = threading.Event()
    stop.set()

    asyncio.run(worker._emit_terminal_outcome(None, outcome, stop=stop))

    assert "".join(frame["text"] for frame in sent[:-1]) == response
    result = sent[-1]
    assert result["status"] == "cancelled"
    assert "response" not in result
    assert result["response_chunk_count"] == len(sent) - 1


def test_supervisor_accepts_sequential_chunks_and_exact_duplicate() -> None:
    runtime = _RuntimeProbe()
    state = _state()

    async def scenario() -> None:
        assert not await runtime._handle_response_chunk_message(state, _chunk(0, "hello "))
        assert not await runtime._handle_response_chunk_message(state, _chunk(0, "hello "))
        assert not await runtime._handle_response_chunk_message(
            state, _chunk(1, "world", final=True)
        )

    asyncio.run(scenario())

    durable = [payload for kind, payload in runtime.records if kind == "response_chunk"]
    assert [item["text"] for item in durable] == ["hello world"]
    assert not [payload for kind, payload in runtime.records if kind == "response"]
    assert state.response_next_index == 2
    assert state.response_bytes == 11
    assert state.response_final_index == 1
    assert any(
        payload.get("note") == "duplicate response_chunk ignored"
        for kind, payload in runtime.records
        if kind == "protocol"
    )


@pytest.mark.parametrize(
    "message",
    [
        _chunk(0, "wrong request", request_id="other"),
        _chunk(0, "wrong task", task_id="other"),
        _chunk(0, "stale", generation=2),
        _chunk(1, "gap"),
        _chunk(0, ""),
        _chunk(0, "bad final", final=1),
        _chunk(0, "x" * (worker.MAX_RESPONSE_CHUNK_BYTES + 1)),
    ],
)
def test_supervisor_rejects_stale_mismatched_malformed_and_gap(
    message: dict[str, Any],
) -> None:
    runtime = _RuntimeProbe()
    state = _state()

    handled = asyncio.run(runtime._handle_response_chunk_message(state, message))

    assert handled is True
    assert state.protocol_failure == "INVALID_RESPONSE_CHUNK"
    assert not [record for record in runtime.records if record[0] == "response_chunk"]


def test_supervisor_rejects_conflicting_duplicate_and_incomplete_result() -> None:
    runtime = _RuntimeProbe()
    state = _state()

    async def scenario() -> None:
        assert not await runtime._handle_response_chunk_message(state, _chunk(0, "hello"))
        assert await runtime._handle_response_chunk_message(state, _chunk(0, "changed"))

    asyncio.run(scenario())
    assert state.protocol_failure == "INVALID_RESPONSE_CHUNK"

    runtime = _RuntimeProbe()
    state = _state()
    asyncio.run(runtime._handle_response_chunk_message(state, _chunk(0, "prefix")))
    asyncio.run(
        runtime._handle_result_message(
            state,
            {
                "type": "result_envelope",
                "task_id": "task",
                "generation": 3,
                "request_id": "run-1",
                "status": "succeeded",
                "summary": "compact",
                "response_chunk_count": 1,
                "response_bytes": 6,
            },
        )
    )
    assert state.envelope is None
    assert state.protocol_failure == "INCOMPLETE_RESPONSE"


def test_supervisor_rejects_inline_response_in_result_control_envelope() -> None:
    runtime = _RuntimeProbe()
    state = _state()

    asyncio.run(
        runtime._handle_result_message(
            state,
            {
                "type": "result_envelope",
                "task_id": "task",
                "generation": 3,
                "request_id": "run-1",
                "status": "succeeded",
                "summary": "compact",
                "response": "must use response_chunk",
            },
        )
    )

    assert state.envelope is None
    assert state.protocol_failure == "INVALID_RESULT_ENVELOPE"
    assert not [record for record in runtime.records if record[0] == "response_chunk"]
    result = next(payload for kind, payload in runtime.records if kind == "result")
    assert result["response_valid"] is False


def test_supervisor_session_response_resource_limit_rejects_before_persist(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    runtime = _RuntimeProbe()
    state = _state()
    monkeypatch.setattr(supervisor, "MAX_RESPONSE_SESSION_BYTES", 5)

    assert not asyncio.run(runtime._handle_response_chunk_message(state, _chunk(0, "1234")))
    assert asyncio.run(runtime._handle_response_chunk_message(state, _chunk(1, "12")))

    durable = [payload for kind, payload in runtime.records if kind == "response_chunk"]
    assert durable == []
    assert state.response_bytes == 4
    assert state.protocol_failure == "INVALID_RESPONSE_CHUNK"


def test_supervisor_redacts_secret_across_raw_chunk_boundary_and_replays_redacted_text() -> None:
    secret = "registered-secret-" + ("s" * 64)
    response = ("p" * (worker.MAX_RESPONSE_CHUNK_BYTES - 5)) + secret + "-tail"
    first = response[: worker.MAX_RESPONSE_CHUNK_BYTES]
    second = response[worker.MAX_RESPONSE_CHUNK_BYTES :]
    runtime = _RuntimeProbe(redactor=Redactor(secret_values=[secret]))
    state = _state()

    async def scenario() -> None:
        assert not await runtime._handle_response_chunk_message(state, _chunk(0, first))
        assert not await runtime._handle_response_chunk_message(
            state, _chunk(1, second, final=True)
        )
        await runtime._handle_result_message(
            state,
            {
                "type": "result_envelope",
                "task_id": "task",
                "generation": 3,
                "request_id": "run-1",
                "status": "succeeded",
                "summary": "compact",
                "response_chunk_count": 2,
                "response_bytes": len(response.encode()),
            },
        )

    asyncio.run(scenario())

    redacted = runtime._redactor.redact(response)
    durable = [payload for kind, payload in runtime.records if kind == "response_chunk"]
    assert secret not in "".join(item["text"] for item in durable)
    assert "".join(item["text"] for item in durable) == redacted
    assert not [payload for kind, payload in runtime.records if kind == "response"]
    result = next(payload for kind, payload in runtime.records if kind == "result")
    assert result["response_chunk_count"] == len(durable)
    assert result["response_bytes"] == len(redacted.encode())
    events = [
        {
            "kind": "response_chunk",
            "task_id": item["task_id"],
            "generation": item["generation"],
            "request_id": item["request_id"],
            "payload": {
                "chunk_index": item["chunk_index"],
                "text": item["text"],
                "final": item["final"],
            },
        }
        for item in durable
    ]
    events.append(
        {
            "kind": "result",
            "task_id": result["task_id"],
            "generation": result["generation"],
            "request_id": result["request_id"],
            "payload": {
                "status": result["status"],
                "response_chunk_count": result["response_chunk_count"],
                "response_bytes": result["response_bytes"],
            },
        }
    )
    assert supervisor.reconstruct_response(events, "task", 3, "run-1") == (redacted, True)


def test_cancelled_partial_raw_response_flushes_only_a_redacted_prefix() -> None:
    secret = "cancel-secret-" + ("c" * 64)
    runtime = _RuntimeProbe(redactor=Redactor(secret_values=[secret]))
    state = _state()
    prefix = "safe-" + secret

    async def scenario() -> None:
        assert not await runtime._handle_response_chunk_message(state, _chunk(0, prefix))
        assert not [record for record in runtime.records if record[0] == "response_chunk"]
        await runtime._handle_result_message(
            state,
            {
                "type": "result_envelope",
                "task_id": "task",
                "generation": 3,
                "request_id": "run-1",
                "status": "cancelled",
                "summary": "compact",
            },
        )

    asyncio.run(scenario())

    durable = [payload for kind, payload in runtime.records if kind == "response_chunk"]
    assert "".join(item["text"] for item in durable) == "safe-***"
    assert durable[-1]["final"] is False
    assert secret not in "".join(item["text"] for item in durable)
    result = next(payload for kind, payload in runtime.records if kind == "result")
    assert result["status"] == "cancelled"


def test_reconstruct_response_returns_valid_prefix_and_full_completion() -> None:
    chunks = [_event(0, "hello "), _event(1, "world", final=True)]

    assert supervisor.reconstruct_response(chunks, "task", 3, "run-1") == (
        "hello world",
        False,
    )
    assert supervisor.reconstruct_response(
        [chunks[0], chunks[0], chunks[1], _result()], "task", 3, "run-1"
    ) == ("hello world", True)


def test_replay_ignores_stale_but_blocks_matching_gap_or_malformed_chunk() -> None:
    stale = _event(0, "inject", generation=2)
    mismatch = _event(0, "inject", request_id="other")
    valid = _event(0, "safe")
    gap = _event(2, "unsafe", final=True)

    text, complete = supervisor.reconstruct_response(
        [stale, mismatch, valid, gap, _result(count=2, response_bytes=10)],
        "task",
        3,
        "run-1",
    )

    assert text == "safe"
    assert complete is False


def test_replay_blocks_matching_malformed_result_before_later_success() -> None:
    events = [
        _event(0, "prefix"),
        {
            "kind": "result",
            "task_id": "task",
            "generation": 3,
            "request_id": "run-1",
            "payload": {"status": "not-a-terminal-status"},
        },
        _event(1, "suffix", final=True),
        _result(count=2, response_bytes=12),
    ]

    assert supervisor.reconstruct_response(events, "task", 3, "run-1") == (
        "prefix",
        False,
    )


@pytest.mark.parametrize("status", ["cancelled", "failed"])
def test_cancel_or_crash_replay_never_reports_prefix_as_complete(status: str) -> None:
    events = [
        _event(0, "durable prefix"),
        _result(status, count=1, response_bytes=len("durable prefix")),
    ]

    assert supervisor.reconstruct_response(events, "task", 3, "run-1") == (
        "durable prefix",
        False,
    )
