from __future__ import annotations

import json
import sqlite3
from pathlib import Path

from scripts.extract_opencode_transcript_candidates import extract_candidates


def _database(path: Path) -> sqlite3.Connection:
    connection = sqlite3.connect(path)
    connection.executescript(
        """
        CREATE TABLE project (
            id TEXT PRIMARY KEY,
            name TEXT,
            worktree TEXT
        );
        CREATE TABLE session (
            id TEXT PRIMARY KEY,
            project_id TEXT,
            title TEXT,
            slug TEXT,
            directory TEXT,
            path TEXT,
            metadata TEXT
        );
        CREATE TABLE message (
            id TEXT PRIMARY KEY,
            session_id TEXT,
            time_created INTEGER,
            data TEXT
        );
        CREATE TABLE part (
            id TEXT PRIMARY KEY,
            message_id TEXT,
            session_id TEXT,
            time_created INTEGER,
            data TEXT
        );
        """
    )
    connection.execute(
        "INSERT INTO project(id, name, worktree) VALUES (?, ?, ?)",
        ("p-cambium", "example", "/workspace/cambium"),
    )
    connection.execute(
        "INSERT INTO project(id, name, worktree) VALUES (?, ?, ?)",
        ("p-other", "other", "/workspace/other"),
    )
    return connection


def _session(
    connection: sqlite3.Connection,
    session_id: str,
    *,
    project_id: str,
    directory: str,
    title: str,
) -> None:
    connection.execute(
        "INSERT INTO session(id, project_id, title, slug, directory, path, metadata) "
        "VALUES (?, ?, ?, ?, ?, ?, ?)",
        (session_id, project_id, title, session_id, directory, "", None),
    )


def _part(
    connection: sqlite3.Connection,
    session_id: str,
    number: int,
    *,
    role: str,
    part_type: str,
    text: str,
    status: str = "completed",
) -> None:
    message_id = f"m-{session_id}-{number}"
    part_id = f"p-{session_id}-{number}"
    connection.execute(
        "INSERT INTO message(id, session_id, time_created, data) VALUES (?, ?, ?, ?)",
        (message_id, session_id, number, json.dumps({"role": role})),
    )
    if part_type == "tool":
        data = {"type": "tool", "state": {"status": status, "output": text}}
    else:
        data = {"type": part_type, "text": text}
    connection.execute(
        "INSERT INTO part(id, message_id, session_id, time_created, data) VALUES (?, ?, ?, ?, ?)",
        (part_id, message_id, session_id, number, json.dumps(data)),
    )


def test_extracts_visible_structured_and_labeled_records(tmp_path: Path) -> None:
    path = tmp_path / "synthetic.db"
    connection = _database(path)
    _session(
        connection,
        "s-field",
        project_id="p-cambium",
        directory="/workspace/cambium",
        title="review",
    )
    _session(
        connection,
        "s-content",
        project_id="p-other",
        directory="/workspace/other",
        title="unrelated",
    )
    _session(
        connection,
        "s-ignored",
        project_id="p-other",
        directory="/workspace/other",
        title="unrelated",
    )
    _part(
        connection,
        "s-field",
        1,
        role="assistant",
        part_type="reasoning",
        text=json.dumps(
            {
                "input": {"task": "hidden task", "context": ""},
                "expected": {"decompose": False, "reason": "hidden"},
            }
        ),
    )
    _part(
        connection,
        "s-field",
        2,
        role="assistant",
        part_type="tool",
        text=json.dumps(
            {
                "input": {
                    "task": "Add the billing export and update the worker.",
                    "context": (
                        "api_key=FAKE_VALUE_1234567890 "
                        "OPENAI_API_KEY=ANOTHER_FAKE_VALUE "
                        "```env\nOPENAI_API_KEY=BLOCK_FAKE_VALUE\n``` "
                        "https://alice:password@example.invalid/x "
                        "alice@example.invalid 555-123-4567 /home/alice/project"
                    ),
                },
                "expected": {"decompose": True, "reason": "two independent workstreams"},
            }
        ),
    )
    _part(
        connection,
        "s-field",
        3,
        role="assistant",
        part_type="text",
        text=(
            "Task: Rename one local helper.\n"
            "Context: The change is already scoped.\n"
            "Decision: do_not_decompose\n"
            "Rationale: one atomic edit"
        ),
    )
    _part(
        connection,
        "s-field",
        4,
        role="assistant",
        part_type="tool",
        status="error",
        text=json.dumps(
            {
                "input": {"task": "Error task", "context": ""},
                "expected": {"decompose": True, "reason": "error"},
            }
        ),
    )
    _part(
        connection,
        "s-content",
        1,
        role="user",
        part_type="text",
        text="Cambium should_decompose review follows.",
    )
    _part(
        connection,
        "s-content",
        2,
        role="assistant",
        part_type="tool",
        text=json.dumps(
            {
                "input": {"task": "Fix one focused parser guard.", "context": ""},
                "expected": {"decompose": False, "reason": "single atomic fix"},
            }
        ),
    )
    _part(
        connection,
        "s-ignored",
        1,
        role="assistant",
        part_type="tool",
        text=json.dumps(
            {
                "input": {"task": "Ignored task", "context": ""},
                "expected": {"decompose": True, "reason": "unrelated result"},
            }
        ),
    )
    connection.commit()
    connection.close()

    result = extract_candidates([path])

    assert len(result.candidates) == 3
    assert {candidate.decompose for candidate in result.candidates} == {False, True}
    serialized = json.dumps(
        [{"task": candidate.task, "context": candidate.context} for candidate in result.candidates]
    )
    assert "FAKE_VALUE_1234567890" not in serialized
    assert "ANOTHER_FAKE_VALUE" not in serialized
    assert "BLOCK_FAKE_VALUE" not in serialized
    assert "alice@example.invalid" not in serialized
    assert "[REDACTED_" in serialized
    assert result.summaries[0].selected_sessions == 2
    assert result.summaries[0].explicit_records == 3


def test_conflicting_explicit_labels_are_rejected(tmp_path: Path) -> None:
    path = tmp_path / "conflict.db"
    connection = _database(path)
    _session(
        connection,
        "s-conflict",
        project_id="p-cambium",
        directory="/workspace/cambium",
        title="conflict",
    )
    for number, label in ((1, True), (2, False)):
        _part(
            connection,
            "s-conflict",
            number,
            role="tool",
            part_type="tool",
            text=json.dumps(
                {
                    "input": {"task": "Same task", "context": ""},
                    "expected": {"decompose": label, "reason": "explicit result"},
                }
            ),
        )
    connection.commit()
    connection.close()

    result = extract_candidates([path])

    assert result.candidates == ()
    assert result.conflicting_records == 2


def test_unsafe_candidate_is_skipped_and_counted(tmp_path: Path) -> None:
    path = tmp_path / "unsafe.db"
    connection = _database(path)
    _session(
        connection,
        "s-unsafe",
        project_id="p-cambium",
        directory="/workspace/cambium",
        title="unsafe",
    )
    _part(
        connection,
        "s-unsafe",
        1,
        role="tool",
        part_type="tool",
        text=json.dumps(
            {
                "input": {"task": "Bad\u0000task", "context": ""},
                "expected": {"decompose": True, "reason": "explicit result"},
            }
        ),
    )
    connection.commit()
    connection.close()

    result = extract_candidates([path])

    assert result.candidates == ()
    assert result.unsafe_records == 1
    assert result.summaries[0].safe_records == 0
