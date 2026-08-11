"""Provider usage aggregation and its compact renderer line."""

from __future__ import annotations

import json
import sqlite3
from dataclasses import asdict

import pytest

from cambium.render import _human_count, render_usage_stats_line
from cambium.stats import UsageStats, session_usage_stats, usage_stats_from_events

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
    assert stats.total_tokens == -5
    assert stats.last_turn_tokens == -5
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


def test_session_usage_stats_skips_undecodable_payloads(tmp_path) -> None:
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
    stats = session_usage_stats(session_dir)
    assert stats is not None
    assert stats.calls == 1
    assert stats.total_tokens == 15


def test_render_usage_stats_line_from_dataclass() -> None:
    stats = UsageStats(2, 2, 2870, 347, 0, 3217, 1606, "p/m", "p")
    assert render_usage_stats_line(stats, worktree="/wt") == (
        "stats: calls=2 · tokens=3.2k (in=2.9k out=347 cached=0) · last_turn=+1.6k "
        "· model=p/m · worktree=/wt"
    )


def test_render_usage_stats_line_from_mapping() -> None:
    stats = asdict(UsageStats(2, 2, 2870, 347, 0, 3217, 1606, "p/m", "p"))
    assert render_usage_stats_line(stats, worktree="/wt") == (
        "stats: calls=2 · tokens=3.2k (in=2.9k out=347 cached=0) · last_turn=+1.6k "
        "· model=p/m · worktree=/wt"
    )


def test_render_usage_stats_line_mapping_worktree_value() -> None:
    stats = {
        "calls": 1,
        "turns": 1,
        "input_tokens": 1000,
        "output_tokens": 2000,
        "cached_tokens": 0,
        "total_tokens": 3000,
        "last_turn_tokens": 3000,
        "model": "m",
        "provider": "p",
        "worktree": "/map/wt",
    }
    assert render_usage_stats_line(stats).endswith("worktree=…/map/wt")


def test_render_usage_stats_line_none_is_empty() -> None:
    assert render_usage_stats_line(None) == ""


def test_render_usage_stats_line_omits_missing_model_and_worktree() -> None:
    stats = UsageStats(1, 1, 1000, 500, 0, 1500, 1500, None, None)
    assert (
        render_usage_stats_line(stats)
        == "stats: calls=1 · tokens=1.5k (in=1k out=500 cached=0) · last_turn=+1.5k"
    )


def test_render_usage_stats_line_omits_last_turn_without_turns() -> None:
    stats = UsageStats(1, None, 1000, 500, 0, 1500, 0, None, None)
    assert (
        render_usage_stats_line(stats) == "stats: calls=1 · tokens=1.5k (in=1k out=500 cached=0)"
    )


def test_render_usage_stats_line_skips_invalid_mapping_value_types() -> None:
    stats = {
        "calls": True,
        "turns": 1,
        "input_tokens": "x",
        "output_tokens": 20000,
        "cached_tokens": 0,
        "total_tokens": 10000,
        "last_turn_tokens": 5000,
        "model": 42,
        "provider": None,
    }
    assert render_usage_stats_line(stats) == "stats: tokens=10k (out=20k cached=0) · last_turn=+5k"


def test_render_usage_stats_line_human_counts() -> None:
    for raw, expected in (
        (3217, "3.2k"),
        (347, "347"),
        (1000, "1k"),
        (12345, "12.3k"),
        (0, "0"),
    ):
        assert _human_count(raw) == expected
    stats = UsageStats(2, 2, 2870, 347, 0, 3217, 1606, "m", "p")
    assert render_usage_stats_line(stats, worktree="/wt") == (
        "stats: calls=2 · tokens=3.2k (in=2.9k out=347 cached=0) · last_turn=+1.6k "
        "· model=m · worktree=/wt"
    )


def test_render_usage_stats_line_plain_counts_below_thousand() -> None:
    stats = UsageStats(2, 2, 130, 70, 0, 200, 50, "m", "p")
    assert render_usage_stats_line(stats) == (
        "stats: calls=2 · tokens=200 (in=130 out=70 cached=0) · last_turn=+50 · model=m"
    )


def test_render_usage_stats_line_grouping_and_separators() -> None:
    stats = UsageStats(2, 2, 2870, 347, 0, 3217, 1606, "m", "p")
    line = render_usage_stats_line(stats, worktree="/wt")
    assert " · " in line
    assert "tokens=3.2k (in=2.9k out=347 cached=0)" in line
    assert line.count(" · ") == 4
    assert line.startswith("stats: calls=2")


def test_render_usage_stats_line_shortens_worktree() -> None:
    stats = UsageStats(1, 1, 1000, 500, 0, 1500, 1500, "m", "p")
    assert render_usage_stats_line(stats, worktree="/tmp/x/.cambium/sessions/run-abc123/wt") == (
        "stats: calls=1 · tokens=1.5k (in=1k out=500 cached=0) · last_turn=+1.5k · model=m "
        "· worktree=…/run-abc123/wt"
    )
    assert render_usage_stats_line(stats, worktree="wt").endswith("worktree=wt")
    assert not render_usage_stats_line(stats, worktree=None).endswith("worktree=")


def test_render_usage_stats_line_mapping_matches_dataclass_line() -> None:
    dataclass_line = render_usage_stats_line(
        UsageStats(2, 2, 2870, 347, 0, 3217, 1606, "p/m", "p"), worktree="/wt"
    )
    mapping_line = render_usage_stats_line(
        asdict(UsageStats(2, 2, 2870, 347, 0, 3217, 1606, "p/m", "p")), worktree="/wt"
    )
    assert mapping_line == dataclass_line


def test_render_usage_stats_line_raises_type_error_for_other_input() -> None:
    with pytest.raises(TypeError):
        render_usage_stats_line(42)
