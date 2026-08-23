from __future__ import annotations

import asyncio
import hashlib
import json
import os
import subprocess
import sys
from pathlib import Path
from typing import Any, cast

from cambium.redact import (
    EVENT_RECORD_STRUCTURAL_FIELDS,
    WORKER_RESULT_STRUCTURAL_FIELDS,
    _oauth_account_fingerprint,
    build_session_redactor,
)
from cambium.supervisor import _Runtime
from cambium.worker import _emit_result_envelope


class _EventStore:
    def __init__(self) -> None:
        self.records: list[dict[str, object]] = []

    def append(self, record: dict[str, object]) -> None:
        self.records.append(record)


class _Writer:
    def __init__(self) -> None:
        self.frames: list[bytes] = []

    def write(self, frame: bytes) -> None:
        self.frames.append(frame)

    async def drain(self) -> None:
        return None


def _event_record(secret: str) -> dict[str, object]:
    store = _EventStore()

    async def emit() -> None:
        runtime = _Runtime(Path("."), store)
        await runtime.emit(
            "result",
            task_id="event-task",
            generation=7,
            request_id="event-request",
            api_key=secret,
            nested=[
                {"password": secret},
                {9: secret, "message": f"prefix-{secret}"},
            ],
        )

    asyncio.run(emit())
    return store.records[0]


def _worker_result() -> dict[str, object]:
    writer = _Writer()
    outcome = {
        "request_id": "worker-request",
        "task_id": "worker-task",
        "generation": 4,
        "status": "suspended",
        "commits": ["commit-opaque-secret-value"],
        "files_changed": ["file-opaque-secret-value"],
        "diff": "diff contains opaque-secret-value",
        "diff_truncated": False,
        "summary": "summary contains opaque-secret-value",
        "failure_reason": "failure contains opaque-secret-value",
        "started_at": 1.0,
        "ended_at": 2.0,
        "epoch": 3,
        "checkpoint_ref": "checkpoint-ref",
        "provider_metadata": {
            "api_key": "opaque-secret-value",
            "nested": [{"refresh_token": "opaque-secret-value"}],
        },
    }
    asyncio.run(_emit_result_envelope(cast(asyncio.StreamWriter, writer), outcome))
    return json.loads(writer.frames[0])


def _add_constant_shape(
    record: dict[str, object], fields: frozenset[str], prefix: str
) -> dict[str, object]:
    shaped = dict(record)
    for field in fields:
        shaped.setdefault(field, f"{prefix}-{field}")
    return shaped


def _structural_strings(record: dict[str, object], fields: frozenset[str]) -> set[str]:
    return {value for key, value in record.items() if key in fields and isinstance(value, str)}


def test_emitted_protocol_shapes_preserve_structure_and_redact_payloads() -> None:
    secret = "opaque-secret-value"
    raw_event = _event_record(secret)
    assert {
        "kind",
        "task_id",
        "worker_id",
        "generation",
        "request_id",
        "ts",
        "monotonic_ms",
        "payload",
    } <= set(raw_event)
    assert {
        "kind",
        "task_id",
        "worker_id",
        "generation",
        "request_id",
        "ts",
        "monotonic_ms",
        "payload",
        "seq",
    } <= EVENT_RECORD_STRUCTURAL_FIELDS
    event = _add_constant_shape(raw_event, EVENT_RECORD_STRUCTURAL_FIELDS, "event")
    event_redactor = build_session_redactor(
        _structural_strings(event, EVENT_RECORD_STRUCTURAL_FIELDS) | {secret}
    )
    redacted_event = event_redactor.redact_protocol_record(
        event, structural_fields=EVENT_RECORD_STRUCTURAL_FIELDS
    )

    for key, value in event.items():
        if key in EVENT_RECORD_STRUCTURAL_FIELDS and isinstance(value, str):
            assert redacted_event[key] == value
    assert redacted_event["payload"] == {
        "***": "***",
        "nested": [
            {"***": "***"},
            {9: "***", "message": "prefix-***"},
        ],
    }
    assert secret not in repr(redacted_event)
    assert (
        event_redactor.redact_protocol_record(
            cast(dict[str, Any], redacted_event),
            structural_fields=EVENT_RECORD_STRUCTURAL_FIELDS,
        )
        == redacted_event
    )

    raw_worker = _worker_result()
    assert set(raw_worker) == {
        "type",
        "request_id",
        "task_id",
        "generation",
        "status",
        "exit_code",
        "commits",
        "files_changed",
        "diff",
        "diff_truncated",
        "summary",
        "failure_reason",
        "started_at",
        "ended_at",
        "epoch",
        "checkpoint_ref",
        "provider_metadata",
    }
    assert {
        "type",
        "request_id",
        "task_id",
        "generation",
        "status",
        "exit_code",
        "diff_truncated",
        "epoch",
        "checkpoint_ref",
    } <= WORKER_RESULT_STRUCTURAL_FIELDS
    worker = _add_constant_shape(raw_worker, WORKER_RESULT_STRUCTURAL_FIELDS, "worker")
    worker_redactor = build_session_redactor(
        _structural_strings(worker, WORKER_RESULT_STRUCTURAL_FIELDS) | {secret}
    )
    redacted_worker = worker_redactor.redact_protocol_record(
        worker, structural_fields=WORKER_RESULT_STRUCTURAL_FIELDS
    )

    for key, value in worker.items():
        if key in WORKER_RESULT_STRUCTURAL_FIELDS and isinstance(value, str):
            assert redacted_worker[key] == value
    assert secret not in repr(redacted_worker)
    assert redacted_worker["provider_metadata"] == {
        "***": "***",
        "nested": [{"***": "***"}],
    }
    assert (
        worker_redactor.redact_protocol_record(
            cast(dict[str, Any], redacted_worker),
            structural_fields=WORKER_RESULT_STRUCTURAL_FIELDS,
        )
        == redacted_worker
    )


def test_oauth_account_fingerprint_is_stable_distinct_and_bounded() -> None:
    unicode_value = "\u8d26\u53f7-\U0001f510"
    huge_value = "x" * 1_000_000
    values = ("", unicode_value, huge_value)

    for value in values:
        expected = hashlib.sha256(value.encode("utf-8")).hexdigest()[:8]
        assert _oauth_account_fingerprint(value) == expected
        assert _oauth_account_fingerprint(value) == expected

    similar = (
        "account-0001",
        "account-0002",
        "account-0001x",
        "account-0001 ",
    )
    assert len({_oauth_account_fingerprint(value) for value in similar}) == len(similar)

    root = Path(__file__).resolve().parents[2]
    script = (
        "from cambium.redact import _oauth_account_fingerprint; "
        f"print(_oauth_account_fingerprint({unicode_value!r}))"
    )
    outputs = []
    for seed in ("1", "987654321"):
        environment = os.environ.copy()
        environment["PYTHONHASHSEED"] = seed
        environment["PYTHONPATH"] = str(root / "src")
        completed = subprocess.run(
            [sys.executable, "-c", script],
            check=True,
            capture_output=True,
            text=True,
            env=environment,
        )
        outputs.append(completed.stdout.strip())
    assert outputs == [
        _oauth_account_fingerprint(unicode_value),
        _oauth_account_fingerprint(unicode_value),
    ]


def test_session_redactor_handles_lists_keys_and_recursion_limit() -> None:
    secret = "opaque-deep-secret"
    payload: dict[object, object] = {
        13: secret,
        "items": [{"password": secret}, {"note": f"prefix-{secret}"}],
    }
    cursor: dict[object, object] = payload
    depth = sys.getrecursionlimit() + 100
    for index in range(depth):
        child: dict[object, object] = {"level": index}
        cursor["child"] = child
        cursor = child
    cursor["value"] = secret

    redactor = build_session_redactor([secret])
    redacted = cast(dict[object, object], redactor.redact_mapping(payload))

    assert redacted[13] == "***"
    assert redacted["items"] == [
        {"***": "***"},
        {"note": "prefix-***"},
    ]
    output_cursor: dict[object, object] = redacted
    for _ in range(depth):
        output_cursor = cast(dict[object, object], output_cursor["child"])
    assert output_cursor["value"] == "***"
    assert payload[13] == secret
    items = cast(list[dict[object, object]], payload["items"])
    assert items[0]["password"] == secret
