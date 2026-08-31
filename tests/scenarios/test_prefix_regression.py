"""Trajectory-prefix regression over a synthetic immutable checkpoint."""

from __future__ import annotations

import json
import sqlite3
from pathlib import Path
from typing import Any

from scripts.prefix_regression import assert_prefix_regression, run_prefix_regression

_EVENTS_SCHEMA = """CREATE TABLE events (
    seq INTEGER PRIMARY KEY,
    kind TEXT NOT NULL,
    payload TEXT NOT NULL,
    ts TEXT,
    monotonic_ms INTEGER,
    task_id TEXT,
    worker_id TEXT,
    generation INTEGER,
    request_id TEXT
)"""


def _synthetic_session(root: Path) -> Path:
    session = root / "session"
    checkpoint_root = session / ".cambium" / "checkpoints" / "task-1"
    checkpoint_root.mkdir(parents=True)
    reference = "task-1/epoch-001-aaaaaaaaaaaaaaaa-bbbbbbbbbbbbbbbb.json"
    (checkpoint_root / "epoch-001-aaaaaaaaaaaaaaaa-bbbbbbbbbbbbbbbb.json").write_text(
        json.dumps(
            {
                "schema": 5,
                "content": {
                    "provider_messages": [
                        {"role": "system", "content": "static agent instructions"},
                        {"role": "user", "content": "tool output before the error"},
                    ],
                    "continuation_suffix": [],
                },
                "meta": {
                    "task_id": "task-1",
                    "turn": 1,
                    "cache_key": {"provider": "recorded", "model": "test-model"},
                },
            }
        ),
        encoding="utf-8",
    )
    db = session / ".cambium" / "events.db"
    with sqlite3.connect(db) as connection:
        connection.execute(_EVENTS_SCHEMA)
        connection.executemany(
            "INSERT INTO events VALUES(?, ?, ?, ?, ?, ?, ?, ?, ?)",
            [
                (
                    1,
                    "task_assigned",
                    json.dumps({"task": "repair the error"}),
                    None,
                    None,
                    "task-1",
                    "task-1:1",
                    1,
                    None,
                ),
                (
                    2,
                    "context_checkpoint",
                    json.dumps({"turn": 1, "checkpoint_ref": reference}),
                    None,
                    None,
                    "task-1",
                    "task-1:1",
                    1,
                    None,
                ),
            ],
        )
    return session


def test_replays_one_turn_from_frozen_prefix(tmp_path: Path) -> None:
    session = _synthetic_session(tmp_path)
    prompts: list[dict[str, Any]] = []

    def transport(prompt: dict[str, Any]) -> str:
        prompts.append(prompt)
        return '{"type":"plan","steps":["inspect the failing path"]}'

    result = assert_prefix_regression(session, 1, "plan", transport=transport)

    assert result["passed"] is True
    assert result["action_class"] == "plan"
    assert result["replay_turn"] == 2
    assert len(prompts) == 1
    assert prompts[0]["messages"][-1] == {
        "role": "user",
        "content": "tool output before the error",
    }


def test_replays_production_turn_checkpoint_layout(tmp_path: Path) -> None:
    session = tmp_path / "session"
    checkpoint_dir = session / ".cambium" / "checkpoints" / "task-1"
    checkpoint_dir.mkdir(parents=True)
    checkpoint = checkpoint_dir / "turn-001.json"
    checkpoint.write_text(
        json.dumps(
            {
                "schema": 1,
                "task": "repair the error",
                "generation": 1,
                "turn": 1,
                "transcript": [
                    {"role": "assistant", "content": '{"type":"plan","steps":["inspect"]}'},
                ],
                "usage": {},
                "commits_so_far": [],
                "workspace_hash": "a" * 64,
            }
        ),
        encoding="utf-8",
    )
    db = session / ".cambium" / "events.db"
    with sqlite3.connect(db) as connection:
        connection.execute(_EVENTS_SCHEMA)
        connection.executemany(
            "INSERT INTO events VALUES(?, ?, ?, ?, ?, ?, ?, ?, ?)",
            [
                (
                    1,
                    "task_assigned",
                    json.dumps({"task": "repair the error"}),
                    None,
                    None,
                    "task-1",
                    "task-1:1",
                    1,
                    None,
                ),
                (
                    2,
                    "checkpoint",
                    json.dumps({"turn": 1, "state_ref": str(checkpoint)}),
                    None,
                    None,
                    "task-1",
                    "task-1:1",
                    1,
                    None,
                ),
            ],
        )

    prompts: list[dict[str, Any]] = []
    result = run_prefix_regression(
        session,
        1,
        "finish",
        transport=lambda prompt: (
            prompts.append(prompt) or '{"type":"finish","summary":"done","objective_met":true}'
        ),
    )

    assert result["passed"] is True
    assert result["checkpoint_kind"] == "turn"
    assert any("<cambium-task>" in message["content"] for message in prompts[0]["messages"])


def test_action_set_accepts_any_allowed_class(tmp_path: Path) -> None:
    session = _synthetic_session(tmp_path)
    result = run_prefix_regression(
        session,
        1,
        ("plan", "tool_call"),
        transport=lambda _prompt: '{"type":"tool_call","name":"read_batch",'
        '"arguments":{"path":"README.md"}}',
    )
    assert result["passed"] is True


def test_native_multi_tool_call_response_parses_as_batch(tmp_path: Path) -> None:
    """A provider response with two native tool calls replays as one batch turn."""

    session = _synthetic_session(tmp_path)
    response = {
        "choices": [
            {
                "message": {
                    "tool_calls": [
                        {
                            "function": {
                                "name": "git_op",
                                "arguments": '{"op": "status"}',
                            }
                        },
                        {
                            "function": {
                                "name": "read_batch",
                                "arguments": '{"paths": ["pyproject.toml"]}',
                            }
                        },
                    ]
                }
            }
        ]
    }
    result = run_prefix_regression(
        session,
        1,
        "tool_call",
        transport=lambda _prompt: response,
    )
    assert result["passed"] is True
    assert result["action_class"] == "tool_call"
