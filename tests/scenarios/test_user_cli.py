"""Focused end-to-end contracts for the user-facing Cambium CLI."""

from __future__ import annotations

import asyncio
import json
import os
import stat
import subprocess
from io import StringIO
from pathlib import Path

import pytest

from cambium import cli, oneshot, repl, session, tui
from cambium.auth import AuthStore, derived_env_name
from cambium.render import render_json_result, render_text_result
from cambium.results import Result, write_result
from cambium.supervisor import PlanResult, TaskResult


def _repo(path: Path) -> Path:
    subprocess.run(
        ["git", "init", "-b", "main", str(path)], check=True, capture_output=True
    )
    subprocess.run(["git", "-C", str(path), "config", "user.name", "cli-test"], check=True)
    subprocess.run(["git", "-C", str(path), "config", "user.email", "cli@test"], check=True)
    (path / "file.txt").write_text("file\n", encoding="utf-8")
    subprocess.run(["git", "-C", str(path), "add", "-A"], check=True, capture_output=True)
    subprocess.run(
        ["git", "-C", str(path), "commit", "-m", "initial"],
        check=True,
        capture_output=True,
    )
    return path


def _plan_result() -> PlanResult:
    return PlanResult((TaskResult(task_id="oneshot", status="succeeded", exit_code=0),))


def test_parser_maps_run_options_to_one_shot_config(monkeypatch, capsys, tmp_path: Path) -> None:
    captured: list[oneshot.OneShotConfig] = []

    async def fake_run(config: oneshot.OneShotConfig) -> PlanResult:
        captured.append(config)
        return _plan_result()

    monkeypatch.setattr(oneshot, "run_oneshot", fake_run)

    assert cli.main(
        [
            "run",
            "--repo",
            str(tmp_path / "repo"),
            "--session-dir",
            str(tmp_path / "session"),
            "--provider",
            "demo",
            "--model",
            "demo-model",
            "--json",
            "fix the bug",
        ]
    ) == 0

    assert captured[0].prompt == "fix the bug"
    assert captured[0].repo == str(tmp_path / "repo")
    assert captured[0].session_root == str(tmp_path / "session")
    assert captured[0].provider == "demo"
    assert captured[0].model == "demo-model"
    assert json.loads(capsys.readouterr().out)["results"][0]["task_id"] == "oneshot"


def test_unknown_single_token_is_not_reinterpreted_as_a_prompt(capsys) -> None:
    with pytest.raises(SystemExit) as raised:
        cli.main(["not-a-command"])
    assert raised.value.code == 2
    assert "invalid command arguments" in capsys.readouterr().err


def test_bare_prompt_dispatches_multiple_words(monkeypatch, capsys) -> None:
    captured: list[oneshot.OneShotConfig] = []

    async def fake_run(config: oneshot.OneShotConfig) -> PlanResult:
        captured.append(config)
        return _plan_result()

    monkeypatch.setattr(oneshot, "run_oneshot", fake_run)

    assert cli.main(["make", "the", "change"]) == 0
    assert captured[0].prompt == "make the change"
    assert "plan=tasks:1" in capsys.readouterr().out


def test_run_oneshot_delegates_async_at_supervisor_boundary(monkeypatch, tmp_path: Path) -> None:
    repo = _repo(tmp_path / "repo")
    captured: dict[str, object] = {}

    async def fake_run_plan(session_dir, plan, on_event=None, **kwargs):
        captured.update(session_dir=session_dir, plan=plan, on_event=on_event, kwargs=kwargs)
        return _plan_result()

    monkeypatch.setattr(oneshot.supervisor, "run_plan", fake_run_plan)
    config = oneshot.OneShotConfig(prompt="make the change", repo=repo)

    result = asyncio.run(oneshot.run_oneshot(config))

    assert result == _plan_result()
    session_dir = Path(captured["session_dir"])
    assert session_dir.parent == repo / ".cambium" / "sessions"
    assert captured["plan"]["tasks"][0]["task"] == "make the change"
    assert "gate" not in captured["plan"]["tasks"][0]


def test_default_runs_allocate_distinct_session_leaves(monkeypatch, tmp_path: Path) -> None:
    repo = _repo(tmp_path / "repo")
    sessions: list[Path] = []

    async def fake_run_plan(session_dir, plan, on_event=None, **kwargs):
        sessions.append(Path(session_dir))
        return _plan_result()

    monkeypatch.setattr(oneshot.supervisor, "run_plan", fake_run_plan)
    config = oneshot.OneShotConfig(prompt="repeat", repo=repo)

    asyncio.run(oneshot.run_oneshot(config))
    asyncio.run(oneshot.run_oneshot(config))

    assert sessions[0] != sessions[1]
    assert all(path.parent == repo / ".cambium" / "sessions" for path in sessions)


def test_render_accepts_plan_result() -> None:
    result = _plan_result()

    assert "plan=tasks:1" in render_text_result(result)
    assert json.loads(render_json_result(result))["results"][0]["status"] == "succeeded"


def test_repl_and_tui_make_a_new_config_per_prompt(monkeypatch, tmp_path: Path) -> None:
    configs: list[oneshot.OneShotConfig] = []

    async def fake_run(config: oneshot.OneShotConfig) -> PlanResult:
        configs.append(config)
        return _plan_result()

    monkeypatch.setattr(oneshot, "run_oneshot", fake_run)
    base = oneshot.OneShotConfig(repo=tmp_path / "repo", provider="demo")
    repl_out = StringIO()
    assert repl.run_repl(
        base,
        input_stream=StringIO("first\n/exit\n"),
        output_stream=repl_out,
        error_stream=StringIO(),
    ) == 0
    tui_out = StringIO()
    assert tui.run_tui(
        base,
        input_stream=StringIO("second\n"),
        output_stream=tui_out,
        error_stream=StringIO(),
    ) == 0

    assert [config.prompt for config in configs] == ["first", "second"]
    assert all(config.repo == base.repo and config.provider == base.provider for config in configs)
    assert "plan=tasks:1" in repl_out.getvalue()
    assert "plan=tasks:1" in tui_out.getvalue()


def _write_result(path: Path, ended_at: float) -> None:
    state = path / ".cambium"
    result = Result(
        status="done",
        exit_code=0,
        commits=(),
        files_changed=(),
        unified_diff="",
        diff_truncated=False,
        summary=path.name,
        metric_score=0.0,
        metric_breakdown={},
        parent_task_id=None,
        event_log_ref=f"sqlite:{state / 'events.db'}",
        session_id=str(path.resolve()),
        started_at=ended_at - 1,
        ended_at=ended_at,
        failure_reason=None,
    )
    write_result(result, path, session_id=str(path.resolve()))


def test_session_readers_and_cli_expose_paths_and_result_data(
    capsys, tmp_path: Path
) -> None:
    root = tmp_path / "sessions"
    _write_result(root / "old", 1.0)
    _write_result(root / "new", 2.0)

    assert session.list_sessions(root) == [(root / "old").resolve(), (root / "new").resolve()]
    assert session.latest_session(root) == (root / "new").resolve()
    view = session.show_session(root / "new")
    assert view.path == (root / "new").resolve()
    assert view.result["summary"] == "new"

    assert cli.main(["session", "list", "--session-dir", str(root)]) == 0
    assert capsys.readouterr().out.splitlines() == [
        str((root / "old").resolve()),
        str((root / "new").resolve()),
    ]
    assert cli.main(["session", "show", "--session-dir", str(root), "new"]) == 0
    assert json.loads(capsys.readouterr().out)["summary"] == "new"


def test_stored_auth_is_handed_to_provider_worker_without_plan_leak(
    monkeypatch, tmp_path: Path
) -> None:
    repo = _repo(tmp_path / "repo")
    provider = "demo"
    env_name = derived_env_name(provider)
    config_path = repo / ".cambium" / "providers.json"
    config_path.parent.mkdir(parents=True)
    config_path.write_text(
        json.dumps(
            {
                "providers": [
                    {
                        "name": provider,
                        "tier": "fast",
                        "base_url": "http://127.0.0.1:8080/v1",
                        "api_key_env": env_name,
                        "model": "demo-model",
                    }
                ]
            }
        ),
        encoding="utf-8",
    )
    secret = "stored-secret-never-in-plan"
    auth_path = tmp_path / "home" / ".local" / "share" / "cambium" / "auth.json"
    store = AuthStore(auth_path)
    store.set_provider(provider, secret)
    monkeypatch.setattr(oneshot, "AuthStore", lambda: store)
    monkeypatch.delenv(env_name, raising=False)
    captured: dict[str, object] = {}

    async def fake_run_plan(session_dir, plan, on_event=None, **kwargs):
        captured.update(plan=plan, kwargs=kwargs)
        return _plan_result()

    monkeypatch.setattr(oneshot.supervisor, "run_plan", fake_run_plan)
    asyncio.run(
        oneshot.run_oneshot(
            oneshot.OneShotConfig(prompt="use provider", repo=repo, provider=provider)
        )
    )

    plan_text = json.dumps(captured["plan"])
    assert secret not in plan_text
    assert captured["plan"]["tasks"][0]["provider_env_keys"] == [env_name]
    assert captured["plan"]["tasks"][0]["fanout_config"] == {
        "tier": "fast",
        "model": "demo-model",
    }
    assert captured["plan"]["tasks"][0]["provider_config_path"] == str(
        config_path.resolve()
    )
    assert captured["kwargs"]["provider_environment"] == {env_name: secret}
    assert secret not in repr(captured["plan"])
    assert env_name not in os.environ


def test_plan_file_is_private_and_contains_no_credential(tmp_path: Path) -> None:
    session_dir = tmp_path / "session"
    session_dir.mkdir()
    secret = "plan-secret-must-not-appear"
    from cambium.supervisor import _write_plan

    path = _write_plan(
        session_dir,
        {"tasks": [{"task_id": "one", "provider_env_keys": [derived_env_name("demo")]}]},
    )

    assert stat.S_IMODE(path.stat().st_mode) == 0o600
    assert secret not in path.read_text(encoding="utf-8")
