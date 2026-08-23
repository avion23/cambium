"""Focused regressions for the interactive and session result interfaces."""

from __future__ import annotations

import asyncio
import io
import json
import sqlite3
from pathlib import Path

import pytest

from cambium import cli, oneshot, repl, session, stats, tui
from cambium.render import render_json_result, render_text_result
from cambium.store import EventStore
from cambium.supervisor import PlanResult, TaskResult


class _FlushStream(io.StringIO):
    def __init__(self) -> None:
        super().__init__()
        self.flushes = 0

    def flush(self) -> None:
        self.flushes += 1


def _succeeded_run(_config: oneshot.OneShotConfig) -> PlanResult:
    return PlanResult((TaskResult(task_id="oneshot", status="succeeded", exit_code=0),))


def _no_change_result() -> PlanResult:
    return PlanResult(
        (
            TaskResult(
                task_id="oneshot",
                status="succeeded",
                exit_code=0,
                summary="I reviewed the repository but changed nothing.",
            ),
        )
    )


def test_no_change_completion_returns_success_across_user_interfaces(monkeypatch, tmp_path):
    async def run(_config: oneshot.OneShotConfig, on_event=None) -> PlanResult:
        return _no_change_result()

    monkeypatch.setattr(oneshot, "run_oneshot", run)
    tui_out = _FlushStream()
    assert (
        asyncio.run(
            tui.run_tui(
                oneshot.OneShotConfig(repo=tmp_path),
                input_stream=io.StringIO("hi\n"),
                output_stream=tui_out,
                error_stream=io.StringIO(),
            )
        )
        == 0
    )
    assert "plan_status={succeeded}" in tui_out.getvalue()
    assert "I reviewed the repository but changed nothing." in tui_out.getvalue()

    assert (
        asyncio.run(
            repl.run_repl(
                oneshot.OneShotConfig(repo=tmp_path),
                input_stream=io.StringIO("hi\n/exit\n"),
                output_stream=io.StringIO(),
                error_stream=io.StringIO(),
            )
        )
        == 0
    )
    assert cli.main(["run", "hi", "--repo", str(tmp_path)]) == 0


def test_tui_programming_error_terminates(monkeypatch, tmp_path):
    async def run(_config: oneshot.OneShotConfig, on_event=None):
        raise RuntimeError("backend unavailable")

    monkeypatch.setattr(oneshot, "run_oneshot", run)
    out = _FlushStream()
    err = _FlushStream()
    with pytest.raises(RuntimeError, match="backend unavailable"):
        asyncio.run(
            tui.run_tui(
                oneshot.OneShotConfig(repo=tmp_path),
                input_stream=io.StringIO("hi\n"),
                output_stream=out,
                error_stream=err,
            )
        )


def test_repl_programming_error_terminates(monkeypatch, tmp_path):
    async def run(config: oneshot.OneShotConfig, on_event=None) -> PlanResult:
        if config.prompt == "bad":
            raise RuntimeError("backend unavailable")
        return PlanResult((TaskResult(task_id="oneshot", status="succeeded", exit_code=0),))

    monkeypatch.setattr(oneshot, "run_oneshot", run)
    out = _FlushStream()
    err = _FlushStream()
    with pytest.raises(RuntimeError, match="backend unavailable"):
        asyncio.run(
            repl.run_repl(
                oneshot.OneShotConfig(repo=tmp_path),
                input_stream=io.StringIO("bad\nok\n"),
                output_stream=out,
                error_stream=err,
            )
        )


def test_nested_plan_result_renders_reason_summary_and_safe_json():
    result = _no_change_result()

    text = render_text_result(result)
    assert "plan_summaries={oneshot:'I reviewed the repository but changed nothing.'}" in text
    rendered = json.loads(render_json_result(result))
    assert rendered["results"][0]["status"] == "succeeded"
    assert rendered["results"][0]["reason"] is None
    assert rendered["results"][0]["summary"].startswith("I reviewed")


def _valid_event_log(path: Path) -> None:
    store = EventStore(path, fsync_interval_s=60.0)
    try:
        store.append({"kind": "result", "payload": {"ok": True}})
    finally:
        store.close()


def test_session_event_uri_encodes_query_and_fragment_chars(tmp_path):
    session_dir = tmp_path / "explicit?session#one"
    state = session_dir / ".cambium"
    state.mkdir(parents=True)
    (state / "result.json").write_text(json.dumps({"status": "done"}), encoding="utf-8")
    _valid_event_log(state / "events.db")

    view = session.show_session(session_dir)

    assert view.path == session_dir.resolve()
    assert view.result == {"status": "done"}


def test_session_listing_surfaces_invalid_results(tmp_path):
    root = tmp_path / "sessions"
    valid = root / "valid" / ".cambium"
    invalid = root / "invalid" / ".cambium"
    valid.mkdir(parents=True)
    invalid.mkdir(parents=True)
    (valid / "result.json").write_text('{"ended_at": 1}')
    (invalid / "result.json").write_text("{not json")

    entries = session.list_session_entries(root)

    assert [entry.path.name for entry in entries] == ["valid", "invalid"]
    assert entries[0].valid is True
    assert entries[1].valid is False
    assert entries[1].reason is not None
    with pytest.raises(session.InvalidSessionError):
        session.list_sessions(root)


@pytest.mark.parametrize(
    "value",
    [float("nan"), float("inf"), float("-inf"), 10**10_000],
    ids=["nan", "positive-inf", "negative-inf", "huge-int"],
)
def test_session_timestamp_uses_negative_infinity_for_nonfinite_values(value) -> None:
    assert session._timestamp(value) == float("-inf")


def test_cli_session_show_renderer_failure_is_clean(capsys, tmp_path):
    root = tmp_path / "sessions"
    session_dir = root / "nan"
    state = session_dir / ".cambium"
    state.mkdir(parents=True)
    _valid_event_log(state / "events.db")
    (state / "result.json").write_text(
        json.dumps(
            {
                "status": "done",
                "exit_code": 0,
                "metric_score": float("nan"),
                "api_key": "secret-must-not-be-printed",
            }
        ),
        encoding="utf-8",
    )

    code = cli.main(["session", "show", "--session-dir", str(root), "nan"])

    captured = capsys.readouterr()
    assert code == 1
    assert captured.out == ""
    assert "cambium session:" in captured.err
    assert "Traceback" not in captured.err
    assert "secret-must-not-be-printed" not in captured.err


def test_tui_prints_usage_stats_line(monkeypatch, tmp_path):
    async def run(_config: oneshot.OneShotConfig, on_event=None) -> PlanResult:
        return _succeeded_run(_config)

    monkeypatch.setattr(oneshot, "run_oneshot", run)
    session_dir = oneshot.allocate_session_dir(oneshot.resolve_repo(tmp_path))
    db = session_dir / ".cambium" / "events.db"
    db.parent.mkdir(parents=True)
    store = EventStore(db)
    try:
        for payload in (
            {
                "turn": 1,
                "provider": "p1",
                "model": "opencode-go/deepseek-v4-flash",
                "usage": {"input_tokens": 100, "output_tokens": 50, "total_tokens": 150},
            },
            {
                "turn": 2,
                "provider": "p1",
                "model": "opencode-go/deepseek-v4-flash",
                "usage": {"input_tokens": 30, "output_tokens": 20, "total_tokens": 50},
            },
        ):
            store.append({"kind": "usage_event", "payload": payload})
    finally:
        store.close()
    out = _FlushStream()
    err = _FlushStream()
    code = asyncio.run(
        tui.run_tui(
            oneshot.OneShotConfig(repo=tmp_path, session_root=session_dir),
            input_stream=io.StringIO("hi\n"),
            output_stream=out,
            error_stream=err,
        )
    )
    value = out.getvalue()
    assert code == 0
    assert err.getvalue() == ""
    assert "plan_status={succeeded}" in value
    assert "· tokens=200 (in=130 out=70 cached=0) ·" in value
    assert "stats: calls=2 ·" in value
    assert "last_turn=+50" in value
    assert "model=opencode-go/deepseek-v4-flash" in value
    assert f"worktree=…/{session_dir.name}/wt" in value
    assert str(session_dir) not in value


def test_tui_stats_failure_does_not_break_loop(monkeypatch, tmp_path):
    async def run(_config: oneshot.OneShotConfig, on_event=None) -> PlanResult:
        return _succeeded_run(_config)

    def _fail_stats(_session_dir):
        raise sqlite3.DatabaseError("stats backend unavailable")

    monkeypatch.setattr(oneshot, "run_oneshot", run)
    monkeypatch.setattr(stats, "session_usage_stats", _fail_stats)
    out = _FlushStream()
    err = _FlushStream()
    code = asyncio.run(
        tui.run_tui(
            oneshot.OneShotConfig(repo=tmp_path),
            input_stream=io.StringIO("hi\n"),
            output_stream=out,
            error_stream=err,
        )
    )
    value = out.getvalue()
    assert code == 0
    assert err.getvalue() == ("cambium tui: usage stats unavailable: stats backend unavailable\n")
    assert "plan_status={succeeded}" in value
    assert "stats:" not in value


def test_oneshot_allocate_session_dir(tmp_path):
    first = oneshot.allocate_session_dir(tmp_path)
    second = oneshot.allocate_session_dir(tmp_path)
    assert first.parent == oneshot.default_session_root(tmp_path)
    assert first.is_dir()
    assert second.is_dir()
    assert first != second
