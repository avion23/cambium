"""Supervisor acceptance of usage-event provenance.

The worker emits bounded situation provenance and fallback provenance on ``usage_event`` records.
(``situation_frame_version``, ``situation_frame_source_watermark``,
``situation_frame_sha256``, ``situation_frame_bytes``,
``situation_frame_truncated_sections``).  The supervisor whitelist must
forward them with the same type/bound discipline as the neighboring fields;
an unknown field fails the whole event, so a stale supervisor would drop
every post-frame usage event from both the durable log and the routing-debt
ledger.  These tests drive a worker-shaped event through the real
``_Runtime`` handler with a real ``DebtStore`` (no network, no subprocess).
"""

from __future__ import annotations

import asyncio
import hashlib
from pathlib import Path
from typing import Any

from cambium.routing import DebtStore
from cambium.supervisor import (
    _GenerationState,
    _invalid_usage_event_fields,
    _Runtime,
)


def _worker_sha256(payload_lines: list[str]) -> str:
    return hashlib.sha256("\n".join(payload_lines).encode("utf-8")).hexdigest()


def _worker_usage_event(task_id: str = "parent", generation: int = 3) -> dict[str, Any]:
    """Shape of the worker's usage_event emission (worker.py, situation frame)."""
    payload_lines = ["## OPEN", "  [truncated OPEN; 120/2048 bytes]", "## CHILDREN"]
    return {
        "type": "usage_event",
        "task_id": task_id,
        "generation": generation,
        "turn": 4,
        "provider": "loopback-provider",
        "fell_back_from": "primary-provider",
        "model": "loopback-model",
        "usage": {"input_tokens": 7, "output_tokens": 3, "total_tokens": 10},
        "estimated_cost_usd": 0.0025,
        "latency_s": 0.75,
        "epoch": 1,
        "situation_frame_version": 1,
        "situation_frame_source_watermark": 2,
        "situation_frame_sha256": _worker_sha256(payload_lines),
        "situation_frame_bytes": 2048,
        "situation_frame_truncated_sections": ["OPEN", "CHILDREN"],
    }


def _generation_state(task_id: str, generation: int) -> _GenerationState:
    return _GenerationState(
        task_id=task_id,
        spec={},
        handle=None,  # type: ignore[arg-type]
        worktree=Path("."),
        generation=generation,
        loop=asyncio.get_running_loop(),
        wall_deadline=0.0,
        cmd=[],
        env={},
        init_rid="rid-init",
        init_msg={},
        proc=None,  # type: ignore[arg-type]
        messages=asyncio.Queue(),
        heartbeat_timeout=30.0,
    )


def _runtime(tmp_path: Path, events: list[dict[str, Any]]) -> _Runtime:
    runtime = _Runtime(tmp_path / "session", None, debt_store=DebtStore(tmp_path / "routing.json"))

    async def emit(kind: str, **payload: Any) -> None:
        events.append({"kind": kind, **payload})

    runtime.emit = emit  # type: ignore[method-assign]
    return runtime


def _handle(tmp_path: Path, events: list[dict[str, Any]], msg: dict[str, Any]) -> _Runtime:
    """Drive one worker-shaped message through the real handler on a live loop."""
    runtime = _runtime(tmp_path, events)

    async def scenario() -> None:
        await runtime._handle_usage_event_message(_generation_state("parent", 3), msg)

    asyncio.run(scenario())
    return runtime


def test_usage_event_with_frame_provenance_is_forwarded_and_folded(
    tmp_path: Path,
) -> None:
    msg = _worker_usage_event()
    assert _invalid_usage_event_fields(msg) == []
    events: list[dict[str, Any]] = []
    runtime = _handle(tmp_path, events, msg)

    assert [event["kind"] for event in events] == ["usage_event"]
    emitted = events[0]
    assert emitted["task_id"] == "parent"
    assert emitted["generation"] == 3
    # Provenance survives the whitelist untouched.
    assert emitted["situation_frame_version"] == 1
    assert emitted["situation_frame_source_watermark"] == 2
    assert emitted["situation_frame_sha256"] == msg["situation_frame_sha256"]
    assert emitted["situation_frame_bytes"] == 2048
    assert emitted["situation_frame_truncated_sections"] == ["OPEN", "CHILDREN"]
    # Accounting fields still forwarded next to provenance.
    assert emitted["provider"] == "loopback-provider"
    assert emitted["fell_back_from"] == "primary-provider"
    assert emitted["usage"] == {"input_tokens": 7, "output_tokens": 3, "total_tokens": 10}
    assert emitted["estimated_cost_usd"] == 0.0025
    assert emitted["latency_s"] == 0.75
    # Accounting intact: the same event folded into the debt ledger.
    debt = runtime._debt_store.as_mapping()["loopback-provider"]
    assert debt.requests == 1
    assert debt.tokens == 10
    assert debt.cost == 0.0025


def test_rejected_provenance_drops_event_and_skips_ledger(tmp_path: Path) -> None:
    events: list[dict[str, Any]] = []
    runtime = _runtime(tmp_path, events)
    bad_values = [
        {"situation_frame_version": "1"},
        {"situation_frame_version": True},
        {"situation_frame_source_watermark": -1},
        {"situation_frame_source_watermark": "w-2"},
        {"situation_frame_sha256": "A" * 64},
        {"situation_frame_sha256": "a" * 63},
        {"situation_frame_bytes": -1},
        {"situation_frame_bytes": 1.5},
        {"situation_frame_truncated_sections": "OPEN"},
        {"situation_frame_truncated_sections": [""]},
        {"situation_frame_truncated_sections": [7]},
        {"situation_frame_truncated_sections": ["BOGUS_SECTION"]},
        {"situation_frame_truncated_sections": ["OPEN", "OPEN"]},
        {"situation_frame_truncated_sections": ["OPEN" * 100]},
        {"fell_back_from": ""},
        {"fell_back_from": 7},
        {"fell_back_from": True},
    ]

    async def scenario() -> None:
        state = _generation_state("parent", 3)
        for bad in bad_values:
            await runtime._handle_usage_event_message(state, {**_worker_usage_event(), **bad})

    asyncio.run(scenario())

    assert [event["kind"] for event in events] == ["protocol"] * len(bad_values)
    rejected_fields = [event["fields"] for event in events]
    assert list(bad_values[0]) == rejected_fields[0]
    assert rejected_fields[0] == ["situation_frame_version"]
    assert rejected_fields[1] == ["situation_frame_version"]
    assert rejected_fields[2] == ["situation_frame_source_watermark"]
    assert rejected_fields[4] == ["situation_frame_sha256"]
    assert rejected_fields[6] == ["situation_frame_bytes"]
    assert rejected_fields[8] == ["situation_frame_truncated_sections"]
    assert rejected_fields[9] == ["situation_frame_truncated_sections"]
    assert rejected_fields[10] == ["situation_frame_truncated_sections"]
    assert rejected_fields[11] == ["situation_frame_truncated_sections"]
    assert rejected_fields[-3:] == [["fell_back_from"]] * 3
    # No durable usage_event and no ledger fold for any rejected variant.
    assert runtime._debt_store.as_mapping() == {}


def test_unknown_and_identity_failures_still_precede_provenance(tmp_path: Path) -> None:
    events: list[dict[str, Any]] = []
    runtime = _runtime(tmp_path, events)

    async def scenario() -> None:
        state = _generation_state("parent", 3)
        drifted = {**_worker_usage_event(), "situation_frame_model": "gpt-x"}
        await runtime._handle_usage_event_message(state, drifted)
        await runtime._handle_usage_event_message(
            state, {**_worker_usage_event(), "generation": 99}
        )

    asyncio.run(scenario())

    assert [event["kind"] for event in events] == ["protocol", "protocol"]
    assert events[0]["fields"] == ["situation_frame_model"]
    assert "identity mismatch" in events[1]["note"]
    assert runtime._debt_store.as_mapping() == {}


def test_provenance_fields_are_optional_and_empty_sections_valid(tmp_path: Path) -> None:
    msg = {key: value for key, value in _worker_usage_event().items()}
    del msg["situation_frame_version"]
    msg["situation_frame_truncated_sections"] = []
    assert _invalid_usage_event_fields(msg) == []
    events: list[dict[str, Any]] = []
    runtime = _handle(tmp_path, events, msg)

    assert [event["kind"] for event in events] == ["usage_event"]
    emitted = events[0]
    assert "situation_frame_version" not in emitted
    assert emitted["situation_frame_truncated_sections"] == []
    debt = runtime._debt_store.as_mapping()["loopback-provider"]
    assert debt.tokens == 10
