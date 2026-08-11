"""Focused regressions for the interactive and session result interfaces."""

from __future__ import annotations

import io
import json
from pathlib import Path

from cambium import cli, oneshot, repl, session, tui
from cambium.render import render_json_result, render_text_result
from cambium.store import EventStore
from cambium.supervisor import PlanResult, TaskResult


class _FlushStream(io.StringIO):
    def __init__(self) -> None:
        super().__init__()
        self.flushes = 0

    def flush(self) -> None:
        self.flushes += 1


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
    async def run(_config: oneshot.OneShotConfig) -> PlanResult:
        return _no_change_result()

    monkeypatch.setattr(oneshot, "run_oneshot", run)
    tui_out = _FlushStream()
    assert tui.run_tui(
        oneshot.OneShotConfig(repo=tmp_path),
        input_stream=io.StringIO("hi\n"),
        output_stream=tui_out,
        error_stream=io.StringIO(),
    ) == 0
    assert "plan_status={succeeded}" in tui_out.getvalue()
    assert "I reviewed the repository but changed nothing." in tui_out.getvalue()

    assert repl.run_repl(
        oneshot.OneShotConfig(repo=tmp_path),
        input_stream=io.StringIO("hi\n/exit\n"),
        output_stream=io.StringIO(),
        error_stream=io.StringIO(),
    ) == 0
    assert cli.main(["run", "hi", "--repo", str(tmp_path)]) == 0


def test_tui_backend_exception_then_eof_returns_failure(monkeypatch, tmp_path):
    async def run(_config: oneshot.OneShotConfig):
        raise RuntimeError("backend unavailable")

    monkeypatch.setattr(oneshot, "run_oneshot", run)
    out = _FlushStream()
    err = _FlushStream()
    code = tui.run_tui(
        oneshot.OneShotConfig(repo=tmp_path),
        input_stream=io.StringIO("hi\n"),
        output_stream=out,
        error_stream=err,
    )

    assert code == 1
    assert err.getvalue() == "cambium: backend unavailable\n"
    assert err.flushes == 1


def test_repl_backend_exception_flushes_and_returns_failure(monkeypatch, tmp_path):
    async def run(config: oneshot.OneShotConfig) -> PlanResult:
        if config.prompt == "bad":
            raise RuntimeError("backend unavailable")
        return PlanResult((TaskResult(task_id="oneshot", status="succeeded", exit_code=0),))

    monkeypatch.setattr(oneshot, "run_oneshot", run)
    out = _FlushStream()
    err = _FlushStream()
    code = repl.run_repl(
        oneshot.OneShotConfig(repo=tmp_path),
        input_stream=io.StringIO("bad\nok\n"),
        output_stream=out,
        error_stream=err,
    )

    assert code == 1
    assert err.getvalue() == "repl: backend unavailable\n"
    assert err.flushes == 1
    assert out.flushes == 1


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
    assert view.events == ()


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
