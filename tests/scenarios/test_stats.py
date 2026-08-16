"""Provider usage aggregation and its compact renderer line."""

from __future__ import annotations

import json
import sqlite3

import pytest

from cambium.render import render_usage_breakdown, render_usage_stats_line
from cambium.stats import (
    session_usage_breakdown,
    session_usage_stats,
    usage_breakdown_from_events,
    usage_stats_from_events,
)

_EVENTS_SCHEMA = """CREATE TABLE events (
    seq          INTEGER PRIMARY KEY AUTOINCREMENT,
    kind         TEXT    NOT NULL,
    payload      TEXT    NOT NULL,
    ts           TEXT,
    monotonic_ms INTEGER,
    task_id      TEXT,
    worker_id    TEXT,
    generation   INTEGER,
    request_id   TEXT
)"""


def _event(kind: str, payload: object, **extra: object) -> dict[str, object]:
    record: dict[str, object] = {"kind": kind, "payload": payload}
    record.update(extra)
    return record


def _insert_event(
    connection: sqlite3.Connection, kind: str, payload: dict[str, object], seq: int
) -> None:
    connection.execute(
        "INSERT INTO events(seq, kind, payload) VALUES(?, ?, ?)",
        (seq, kind, json.dumps(payload, sort_keys=True)),
    )


def _usage_payload(**values: object) -> dict[str, object]:
    payload: dict[str, object] = {"turn": 1}
    payload.update(values)
    return payload


def test_usage_stats_from_events_empty_sequence_is_none() -> None:
    assert usage_stats_from_events([]) is None


def test_usage_stats_from_events_only_non_usage_records_is_none() -> None:
    events = [
        _event("result", {"status": "succeeded"}),
        _event("worker_exit", {"exit_code": 0}),
    ]
    assert usage_stats_from_events(events) is None


def test_usage_stats_from_events_single_success_row() -> None:
    events = [
        _event(
            "usage_event",
            {
                "turn": 1,
                "provider": "p",
                "model": "m",
                "usage": {"input_tokens": 100, "output_tokens": 50, "total_tokens": 150},
            },
        ),
    ]
    stats = usage_stats_from_events(events)
    assert stats is not None
    assert stats.calls == 1
    assert stats.turns == 1
    assert stats.input_tokens == 100
    assert stats.output_tokens == 50
    assert stats.cached_tokens == 0
    assert stats.total_tokens == 150
    assert stats.last_turn_tokens == 150
    assert stats.model == "m"
    assert stats.provider == "p"


def test_usage_stats_from_events_multiple_rows_across_turns() -> None:
    events = [
        _event(
            "usage_event",
            {
                "turn": 1,
                "provider": "p1",
                "model": "m1",
                "usage": {"input_tokens": 100, "output_tokens": 50, "total_tokens": 150},
            },
            seq=1,
        ),
        _event(
            "usage_event",
            {
                "turn": 2,
                "provider": "p2",
                "model": "m2",
                "usage": {"input_tokens": 200, "output_tokens": 80, "total_tokens": 280},
            },
            seq=2,
        ),
        _event(
            "usage_event",
            {
                "turn": 2,
                "provider": "p3",
                "model": "m3",
                "usage": {"input_tokens": 300, "output_tokens": 120, "total_tokens": 420},
            },
            seq=3,
        ),
    ]
    stats = usage_stats_from_events(events)
    assert stats is not None
    assert stats.calls == 3
    assert stats.turns == 2
    assert stats.input_tokens == 600
    assert stats.output_tokens == 250
    assert stats.cached_tokens == 0
    assert stats.total_tokens == 850
    assert stats.last_turn_tokens == 700
    assert stats.model == "m3"
    assert stats.provider == "p3"


def test_usage_stats_from_events_prompt_and_completion_aliases() -> None:
    events = [
        _event(
            "usage_event",
            {"turn": 1, "usage": {"prompt_tokens": 10, "completion_tokens": 20}},
        ),
    ]
    stats = usage_stats_from_events(events)
    assert stats is not None
    assert stats.input_tokens == 10
    assert stats.output_tokens == 20
    assert stats.total_tokens == 30


def test_usage_stats_from_events_total_falls_back_to_input_plus_output() -> None:
    events = [
        _event(
            "usage_event",
            {"turn": 1, "usage": {"input_tokens": 5, "output_tokens": 7}},
        ),
    ]
    stats = usage_stats_from_events(events)
    assert stats is not None
    assert stats.total_tokens == 12


def test_usage_stats_from_events_failure_row_contributes_no_tokens() -> None:
    events = [
        _event(
            "usage_event",
            {
                "turn": 1,
                "provider": "p",
                "model": "m",
                "usage": {"input_tokens": 100, "output_tokens": 50, "total_tokens": 150},
            },
            seq=1,
        ),
        _event("usage_event", {"turn": 2, "failure_reason": "rate_limited: 429"}, seq=2),
    ]
    stats = usage_stats_from_events(events)
    assert stats is not None
    assert stats.calls == 2
    assert stats.turns == 2
    assert stats.input_tokens == 100
    assert stats.output_tokens == 50
    assert stats.total_tokens == 150
    assert stats.last_turn_tokens == 0
    assert stats.model == "m"
    assert stats.provider == "p"


def test_usage_stats_from_events_rows_without_turn_have_no_turn_info() -> None:
    events = [
        _event(
            "usage_event",
            {
                "provider": "p",
                "model": "m",
                "usage": {"input_tokens": 100, "output_tokens": 50, "total_tokens": 150},
            },
        ),
        _event("usage_event", {"usage": {"input_tokens": 10, "output_tokens": 5}}),
    ]
    stats = usage_stats_from_events(events)
    assert stats is not None
    assert stats.turns is None
    assert stats.last_turn_tokens == 0
    assert stats.total_tokens == 165
    assert stats.model == "m"
    assert stats.provider == "p"


def test_usage_stats_from_events_tolerates_garbage_rows() -> None:
    events = [
        _event("usage_event", {"turn": "not-an-int", "usage": "not a mapping"}),
        _event("usage_event", "not a mapping"),
        _event(
            "usage_event",
            {
                "turn": 1,
                "usage": {
                    "input_tokens": "oops",
                    "output_tokens": 50,
                    "total_tokens": -5,
                    "cached_tokens": "nope",
                },
            },
        ),
    ]
    stats = usage_stats_from_events(events)
    assert stats is not None
    assert stats.calls == 2
    assert stats.turns == 1
    assert stats.input_tokens == 0
    assert stats.output_tokens == 50
    assert stats.cached_tokens == 0
    assert stats.total_tokens == 50
    assert stats.last_turn_tokens == 50
    assert stats.model is None
    assert stats.provider is None


def test_session_usage_stats_missing_db_is_none_and_creates_nothing(tmp_path) -> None:
    session_dir = tmp_path / "session"
    session_dir.mkdir()
    assert session_usage_stats(session_dir) is None
    assert not (session_dir / ".cambium" / "events.db").exists()


def test_session_usage_stats_missing_table_is_none(tmp_path) -> None:
    session_dir = tmp_path / "session"
    db = session_dir / ".cambium" / "events.db"
    db.parent.mkdir(parents=True)
    with sqlite3.connect(db) as connection:
        connection.execute("CREATE TABLE other (id INTEGER PRIMARY KEY)")
    assert session_usage_stats(session_dir) is None


def test_session_usage_stats_corrupt_database_raises(tmp_path) -> None:
    session_dir = tmp_path / "session"
    db = session_dir / ".cambium" / "events.db"
    db.parent.mkdir(parents=True)
    db.write_bytes(b"not a sqlite database")

    with pytest.raises(sqlite3.DatabaseError):
        session_usage_stats(session_dir)


def test_session_usage_stats_aggregates_durable_log(tmp_path) -> None:
    session_dir = tmp_path / "session"
    db = session_dir / ".cambium" / "events.db"
    db.parent.mkdir(parents=True)
    with sqlite3.connect(db) as connection:
        connection.execute(_EVENTS_SCHEMA)
        _insert_event(
            connection,
            "usage_event",
            {
                "turn": 1,
                "provider": "p",
                "model": "m",
                "usage": {"input_tokens": 100, "output_tokens": 50, "total_tokens": 150},
            },
            seq=10,
        )
        _insert_event(
            connection,
            "usage_event",
            {
                "turn": 2,
                "provider": "p",
                "model": "m2",
                "usage": {"input_tokens": 200, "output_tokens": 80, "total_tokens": 280},
            },
            seq=20,
        )
        _insert_event(connection, "usage_event", {"turn": 3, "failure_reason": "cancelled"}, seq=30)
    stats = session_usage_stats(session_dir)
    assert stats is not None
    assert stats.calls == 3
    assert stats.turns == 3
    assert stats.input_tokens == 300
    assert stats.output_tokens == 130
    assert stats.total_tokens == 430
    assert stats.last_turn_tokens == 0
    assert stats.model == "m2"
    assert stats.provider == "p"
    with sqlite3.connect(db) as connection:
        assert connection.execute("SELECT COUNT(*) FROM events").fetchone()[0] == 3
    db.unlink()
    assert not db.exists()


def test_session_usage_stats_ignores_non_usage_kinds(tmp_path) -> None:
    session_dir = tmp_path / "session"
    db = session_dir / ".cambium" / "events.db"
    db.parent.mkdir(parents=True)
    with sqlite3.connect(db) as connection:
        connection.execute(_EVENTS_SCHEMA)
        _insert_event(connection, "result", {"status": "succeeded"}, seq=1)
        _insert_event(
            connection,
            "usage_event",
            {
                "turn": 1,
                "provider": "p",
                "model": "m",
                "usage": {"input_tokens": 10, "output_tokens": 5, "total_tokens": 15},
            },
            seq=2,
        )
        _insert_event(connection, "worker_exit", {"exit_code": 0}, seq=3)
    stats = session_usage_stats(session_dir)
    assert stats is not None
    assert stats.calls == 1
    assert stats.turns == 1
    assert stats.total_tokens == 15


def test_session_usage_stats_rejects_undecodable_payloads(tmp_path) -> None:
    session_dir = tmp_path / "session"
    db = session_dir / ".cambium" / "events.db"
    db.parent.mkdir(parents=True)
    with sqlite3.connect(db) as connection:
        connection.execute(_EVENTS_SCHEMA)
        connection.execute(
            "INSERT INTO events(seq, kind, payload) VALUES(?, ?, ?)",
            (1, "usage_event", "{not json"),
        )
        _insert_event(
            connection,
            "usage_event",
            {"turn": 1, "usage": {"input_tokens": 10, "output_tokens": 5, "total_tokens": 15}},
            seq=2,
        )
    with pytest.raises(json.JSONDecodeError):
        session_usage_stats(session_dir)


def test_session_usage_stats_rejects_non_object_payload(tmp_path) -> None:
    session_dir = tmp_path / "session"
    db = session_dir / ".cambium" / "events.db"
    db.parent.mkdir(parents=True)
    with sqlite3.connect(db) as connection:
        connection.execute(_EVENTS_SCHEMA)
        connection.execute(
            "INSERT INTO events(seq, kind, payload) VALUES(?, ?, ?)",
            (1, "usage_event", "[]"),
        )

    with pytest.raises(ValueError, match="not a JSON object"):
        session_usage_stats(session_dir)


def test_render_usage_stats_line_none_is_empty() -> None:
    assert render_usage_stats_line(None) == ""


def test_usage_stats_from_events_accumulates_estimated_cost() -> None:
    events = [
        _event(
            "usage_event",
            {
                "turn": 1,
                "usage": {"prompt_tokens": 10, "completion_tokens": 20, "total_tokens": 30},
                "model": "m",
                "provider": "p",
                "estimated_cost_usd": 0.0015,
            },
        ),
        _event(
            "usage_event",
            {
                "turn": 2,
                "usage": {"prompt_tokens": 5, "completion_tokens": 5, "total_tokens": 10},
                "model": "m",
                "provider": "p",
                "estimated_cost_usd": 0.0005,
            },
        ),
        _event(
            "usage_event",
            {
                "turn": 3,
                "usage": {"prompt_tokens": 1, "completion_tokens": 1, "total_tokens": 2},
                "model": "m",
                "provider": "p",
                "estimated_cost_usd": "garbage",
            },
        ),
    ]
    stats = usage_stats_from_events(events)
    assert stats is not None
    assert stats.estimated_cost_usd == pytest.approx(0.002)
    assert stats.calls == 3


def test_usage_stats_from_events_ignores_negative_cost() -> None:
    stats = usage_stats_from_events(
        [
            _event(
                "usage_event",
                {
                    "turn": 1,
                    "usage": {"total_tokens": 5},
                    "provider": "p",
                    "estimated_cost_usd": -0.5,
                },
            )
        ]
    )
    assert stats is not None
    assert stats.estimated_cost_usd == 0.0


def test_usage_breakdown_from_events_groups_by_task_and_provider() -> None:
    events = [
        _event(
            "usage_event",
            _usage_payload(
                usage={"total_tokens": 100}, provider="p1", model="m", estimated_cost_usd=0.001
            ),
            task_id="t1",
        ),
        _event(
            "usage_event",
            _usage_payload(
                usage={"total_tokens": 50}, provider="p2", model="m", estimated_cost_usd=0.0005
            ),
            task_id="t1",
        ),
        _event(
            "usage_event",
            _usage_payload(
                usage={"total_tokens": 25}, provider="p1", model="m", estimated_cost_usd=0.00025
            ),
            task_id="t2",
        ),
        _event("checkpoint", {"t": 1}),
    ]
    breakdown = usage_breakdown_from_events(events)
    assert breakdown is not None
    assert [name for name, _ in breakdown.by_task] == ["t1", "t2"]
    assert [name for name, _ in breakdown.by_provider] == ["p1", "p2"]
    by_task = dict(breakdown.by_task)
    by_provider = dict(breakdown.by_provider)
    assert by_task["t1"].total_tokens == 150
    assert by_task["t1"].estimated_cost_usd == pytest.approx(0.0015)
    assert by_task["t2"].total_tokens == 25
    assert by_task["t2"].estimated_cost_usd == pytest.approx(0.00025)
    assert by_provider["p1"].total_tokens == 125
    assert by_provider["p1"].estimated_cost_usd == pytest.approx(0.00125)
    assert by_provider["p2"].total_tokens == 50
    assert breakdown.total.calls == 3
    assert breakdown.total.total_tokens == 175
    assert breakdown.total.estimated_cost_usd == pytest.approx(0.00175)


def test_usage_breakdown_from_events_rows_without_task_or_provider_contribute_to_total() -> None:
    events = [
        _event(
            "usage_event",
            _usage_payload(usage={"total_tokens": 10}, provider="p1", estimated_cost_usd=0.001),
            task_id="t1",
        ),
        _event(
            "usage_event",
            _usage_payload(usage={"total_tokens": 10}, estimated_cost_usd=0.001),
        ),
    ]
    breakdown = usage_breakdown_from_events(events)
    assert breakdown is not None
    assert len(breakdown.by_task) == 1
    assert dict(breakdown.by_task)["t1"].total_tokens == 10
    assert breakdown.total.calls == 2
    assert breakdown.total.total_tokens == 20


def test_usage_breakdown_from_events_no_usage_is_none() -> None:
    assert usage_breakdown_from_events([_event("checkpoint", {"t": 1})]) is None
    assert usage_breakdown_from_events([]) is None


def test_session_usage_breakdown_aggregates_durable_log(tmp_path) -> None:
    db = tmp_path / ".cambium" / "events.db"
    db.parent.mkdir()
    connection = sqlite3.connect(db)
    connection.execute(_EVENTS_SCHEMA)
    _insert_event(
        connection,
        "usage_event",
        _usage_payload(usage={"total_tokens": 100}, provider="p1", estimated_cost_usd=0.001),
        1,
    )
    _insert_event(
        connection,
        "usage_event",
        _usage_payload(usage={"total_tokens": 50}, provider="p2", estimated_cost_usd=0.0005),
        2,
    )
    connection.commit()
    connection.close()
    breakdown = session_usage_breakdown(tmp_path)
    assert breakdown is not None
    assert [name for name, _ in breakdown.by_provider] == ["p1", "p2"]
    assert breakdown.total.total_tokens == 150
    assert breakdown.total.estimated_cost_usd == pytest.approx(0.0015)


def test_session_usage_breakdown_missing_db_is_none(tmp_path) -> None:
    assert session_usage_breakdown(tmp_path) is None


def test_session_usage_breakdown_missing_table_is_none(tmp_path) -> None:
    db = tmp_path / ".cambium" / "events.db"
    db.parent.mkdir()
    connection = sqlite3.connect(db)
    connection.execute("CREATE TABLE other (x TEXT)")
    connection.commit()
    connection.close()
    assert session_usage_breakdown(tmp_path) is None


def test_render_usage_breakdown_none_is_empty() -> None:
    assert render_usage_breakdown(None) == ""
