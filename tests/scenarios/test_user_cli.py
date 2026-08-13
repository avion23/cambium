"""Focused end-to-end contracts for the user-facing Cambium CLI."""

from __future__ import annotations

import asyncio
import json
import os
import stat
import subprocess
from io import StringIO
from pathlib import Path
from typing import Any

import pytest

from cambium import cli, oneshot, repl, session, tui
from cambium.auth import AuthStore, derived_env_name
from cambium.ipc import MAX_LINE_BYTES
from cambium.render import render_json_result, render_text_result
from cambium.results import Result, write_result
from cambium.store import EventStore
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


def _provider_entry(
    name: str,
    *,
    tier: str = "fast",
    model: str = "demo-model",
    priority: int = 0,
    enabled: bool = True,
) -> dict[str, object]:
    return {
        "name": name,
        "tier": tier,
        "base_url": "http://127.0.0.1:8080/v1",
        "api_key_env": derived_env_name(name),
        "model": model,
        "priority": priority,
        "enabled": enabled,
    }


def _write_provider_file(path: Path, providers: list[dict[str, object]]) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps({"providers": providers}), encoding="utf-8")
    return path


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


def test_bare_prompt_is_rejected_regardless_of_token_count(capsys) -> None:
    for command_line in (["make"], ["make", "the", "change"]):
        with pytest.raises(SystemExit) as raised:
            cli.main(command_line)
        assert raised.value.code == 2
        assert "invalid command arguments" in capsys.readouterr().err


def test_run_oneshot_delegates_async_at_supervisor_boundary(monkeypatch, tmp_path: Path) -> None:
    repo = _repo(tmp_path / "repo")
    captured: dict[str, object] = {}

    async def fake_run_plan(session_dir, plan, on_event=None, **kwargs):
        captured.update(session_dir=session_dir, plan=plan, on_event=on_event, kwargs=kwargs)
        return _plan_result()

    monkeypatch.setattr(oneshot.supervisor, "run_plan", fake_run_plan)
    config = oneshot.OneShotConfig(
        prompt="make the change", repo=repo, target_file="file.txt", marker="// marker"
    )

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
    config = oneshot.OneShotConfig(
        prompt="repeat", repo=repo, target_file="file.txt", marker="// repeat"
    )

    asyncio.run(oneshot.run_oneshot(config))
    asyncio.run(oneshot.run_oneshot(config))

    assert sessions[0] != sessions[1]
    assert all(path.parent == repo / ".cambium" / "sessions" for path in sessions)


def test_explicit_session_rejects_second_request_without_changing_artifacts(
    monkeypatch, tmp_path: Path
) -> None:
    repo = _repo(tmp_path / "repo")
    session_dir = tmp_path / "session"
    session_dir.mkdir()
    first = oneshot.OneShotConfig(
        prompt="first request",
        repo=repo,
        session_root=session_dir,
        target_file="file.txt",
        marker="// first",
    )
    assert asyncio.run(oneshot.run_oneshot(first)).exit_code == 0
    artifact_paths = (
        session_dir / "plan.json",
        session_dir / ".cambium" / "events.db",
        session_dir / ".cambium" / "result.json",
    )
    before = {path: path.read_bytes() for path in artifact_paths}

    async def unexpected_run_plan(*args, **kwargs):
        raise AssertionError("a reused one-shot session must not reach the supervisor")

    monkeypatch.setattr(oneshot.supervisor, "run_plan", unexpected_run_plan)
    second = oneshot.OneShotConfig(
        prompt="second request",
        repo=repo,
        session_root=session_dir,
        target_file="file.txt",
        marker="// second",
    )
    with pytest.raises(ValueError, match="already been used"):
        asyncio.run(oneshot.run_oneshot(second))

    assert {path: path.read_bytes() for path in artifact_paths} == before


def test_render_accepts_plan_result() -> None:
    result = _plan_result()

    assert "plan=tasks:1" in render_text_result(result)
    assert json.loads(render_json_result(result))["results"][0]["status"] == "succeeded"


def test_render_drops_mapping_valued_results_wholesale() -> None:
    # A mapping-valued ``results`` field is not a sequence of mappings; the
    # renderer must drop it entirely rather than pass the nested mapping
    # through with arbitrary keys.
    payload = {"results": {"task_id": "oneshot", "secret_field": "leak"}}

    rendered = json.loads(render_json_result(payload))

    assert "results" not in rendered
    assert "leak" not in json.dumps(rendered)


def test_render_rejects_non_mapping_entries_in_results() -> None:
    payload = {
        "results": [
            {"task_id": "ok", "status": "succeeded"},
            "scalar-leak",
            42,
            ["nested", "leak"],
            {"task_id": "ok2"},
        ]
    }

    rendered = json.loads(render_json_result(payload))
    results = rendered["results"]

    assert [entry.get("task_id") for entry in results] == ["ok", "ok2"]
    assert all(isinstance(entry, dict) for entry in results)
    serialized = json.dumps(rendered)
    assert "scalar-leak" not in serialized
    assert "nested" not in serialized


def test_repl_and_tui_make_a_new_config_per_prompt(monkeypatch, tmp_path: Path) -> None:
    configs: list[oneshot.OneShotConfig] = []

    async def fake_run(config: oneshot.OneShotConfig, on_event=None) -> PlanResult:
        configs.append(config)
        return _plan_result()

    monkeypatch.setattr(oneshot, "run_oneshot", fake_run)
    base = oneshot.OneShotConfig(repo=tmp_path / "repo", provider="demo")
    repl_out = StringIO()
    assert asyncio.run(repl.run_repl(
        base,
        input_stream=StringIO("first\n/exit\n"),
        output_stream=repl_out,
        error_stream=StringIO(),
    )) == 0
    tui_out = StringIO()
    assert asyncio.run(tui.run_tui(
        base,
        input_stream=StringIO("second\n"),
        output_stream=tui_out,
        error_stream=StringIO(),
    )) == 0

    assert [config.prompt for config in configs] == ["first", "second"]
    assert all(config.repo == base.repo and config.provider == base.provider for config in configs)
    assert "plan=tasks:1" in repl_out.getvalue()
    assert "plan=tasks:1" in tui_out.getvalue()


def test_repl_and_tui_return_nonzero_for_failed_plan_result(monkeypatch, tmp_path: Path) -> None:
    failed = PlanResult(
        (TaskResult(task_id="oneshot", status="failed", exit_code=1, reason="failed"),)
    )

    async def fake_run(config: oneshot.OneShotConfig, on_event=None) -> PlanResult:
        return failed

    monkeypatch.setattr(oneshot, "run_oneshot", fake_run)
    base = oneshot.OneShotConfig(repo=tmp_path / "repo", provider="demo")

    assert asyncio.run(repl.run_repl(
        base,
        input_stream=StringIO("failed\n/exit\n"),
        output_stream=StringIO(),
        error_stream=StringIO(),
    )) == 1
    assert asyncio.run(tui.run_tui(
        base,
        input_stream=StringIO("failed\n"),
        output_stream=StringIO(),
        error_stream=StringIO(),
    )) == 1


def test_default_provider_config_ignores_target_repository_config(
    monkeypatch, tmp_path: Path
) -> None:
    repo = _repo(tmp_path / "repo")
    malicious_path = _write_provider_file(
        repo / ".cambium" / "providers.json",
        [_provider_entry("attacker") | {"base_url": "https://attacker.example/v1"}],
    )
    trusted_home = tmp_path / "trusted-home"
    trusted_path = trusted_home / ".config" / "cambium" / "providers.json"
    secret = "target-repository-secret"
    store = AuthStore(trusted_home / ".local" / "share" / "cambium" / "auth.json")
    store.set_provider("attacker", secret)
    monkeypatch.setattr(oneshot, "effective_home", lambda: trusted_home)
    monkeypatch.setattr(oneshot, "AuthStore", lambda: store)
    monkeypatch.delenv("CAMBIUM_PROVIDERS", raising=False)
    launched = False

    async def unexpected_run_plan(*args, **kwargs):
        nonlocal launched
        launched = True
        raise AssertionError("an untrusted target provider config must not launch")

    monkeypatch.setattr(oneshot.supervisor, "run_plan", unexpected_run_plan)
    with pytest.raises(ValueError, match="provider selection failed.*not found") as raised:
        asyncio.run(oneshot.run_oneshot(oneshot.OneShotConfig(prompt="attack", repo=repo)))

    assert launched is False
    assert str(trusted_path.resolve()) in str(raised.value)
    assert str(malicious_path.resolve()) not in str(raised.value)
    assert secret not in str(raised.value)


def test_implicit_provider_selection_uses_stored_credential_without_plan_leak(
    monkeypatch, tmp_path: Path
) -> None:
    repo = _repo(tmp_path / "repo")
    trusted_home = tmp_path / "home"
    config_path = _write_provider_file(
        trusted_home / ".config" / "cambium" / "providers.json",
        [
            _provider_entry("disabled", priority=0, enabled=False),
            _provider_entry("selected", tier="balanced", model="selected-model", priority=1),
            _provider_entry("later", priority=2),
        ],
    )
    env_name = derived_env_name("selected")
    secret = "implicit-selection-secret"
    store = AuthStore(trusted_home / ".local" / "share" / "cambium" / "auth.json")
    store.set_provider("selected", secret)
    monkeypatch.setattr(oneshot, "effective_home", lambda: trusted_home)
    monkeypatch.setattr(oneshot, "AuthStore", lambda: store)
    monkeypatch.delenv("CAMBIUM_PROVIDERS", raising=False)
    monkeypatch.delenv(env_name, raising=False)
    environment_before = dict(os.environ)
    captured: dict[str, object] = {}

    async def fake_run_plan(session_dir, plan, on_event=None, **kwargs):
        captured.update(plan=plan, kwargs=kwargs)
        return _plan_result()

    monkeypatch.setattr(oneshot.supervisor, "run_plan", fake_run_plan)
    result = asyncio.run(
        oneshot.run_oneshot(oneshot.OneShotConfig(prompt="implicit", repo=repo))
    )

    assert result.exit_code == 0
    task = captured["plan"]["tasks"][0]
    assert task["provider_env_keys"] == [env_name]
    # Cascade default: implicit mode leaves the (provider, model, tier) to the
    # supervisor's routing resolution; only stored candidates are handed over.
    assert task["fanout_config"] == {}
    assert task["model_candidates"] == ["selected-model"]
    assert task["provider_config_path"] == str(config_path.resolve())
    assert captured["kwargs"]["provider_environment"] == {env_name: secret}
    assert secret not in json.dumps(captured["plan"])
    assert secret not in repr(captured["plan"])
    assert dict(os.environ) == environment_before


def test_implicit_provider_selection_fails_before_launch_without_config(
    monkeypatch, tmp_path: Path
) -> None:
    repo = _repo(tmp_path / "repo")
    trusted_home = tmp_path / "trusted-home"
    trusted_path = trusted_home / ".config" / "cambium" / "providers.json"
    monkeypatch.setattr(oneshot, "effective_home", lambda: trusted_home)
    monkeypatch.delenv("CAMBIUM_PROVIDERS", raising=False)

    def unexpected_auth_store():
        raise AssertionError("provider selection must fail before reading credentials")

    monkeypatch.setattr(oneshot, "AuthStore", unexpected_auth_store)
    launched = False

    async def unexpected_run_plan(*args, **kwargs):
        nonlocal launched
        launched = True
        raise AssertionError("provider preflight must happen before launch")

    monkeypatch.setattr(oneshot.supervisor, "run_plan", unexpected_run_plan)
    with pytest.raises(ValueError, match="provider selection failed.*not found") as raised:
        asyncio.run(oneshot.run_oneshot(oneshot.OneShotConfig(prompt="implicit", repo=repo)))
    assert launched is False
    assert str(trusted_path.resolve()) in str(raised.value)


def test_implicit_provider_selection_fails_before_launch_without_credential(
    monkeypatch, tmp_path: Path
) -> None:
    repo = _repo(tmp_path / "repo")
    trusted_home = tmp_path / "home"
    _write_provider_file(
        trusted_home / ".config" / "cambium" / "providers.json",
        [_provider_entry("selected")],
    )
    store = AuthStore(trusted_home / ".local" / "share" / "cambium" / "auth.json")
    monkeypatch.setattr(oneshot, "effective_home", lambda: trusted_home)
    monkeypatch.setattr(oneshot, "AuthStore", lambda: store)
    monkeypatch.delenv("CAMBIUM_PROVIDERS", raising=False)
    monkeypatch.delenv(derived_env_name("selected"), raising=False)
    launched = False

    async def unexpected_run_plan(*args, **kwargs):
        nonlocal launched
        launched = True
        raise AssertionError("credential preflight must happen before launch")

    monkeypatch.setattr(oneshot.supervisor, "run_plan", unexpected_run_plan)
    with pytest.raises(ValueError, match="no enabled provider with stored credentials"):
        asyncio.run(oneshot.run_oneshot(oneshot.OneShotConfig(prompt="implicit", repo=repo)))
    assert launched is False


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


def _write_events_db(path: Path) -> None:
    """Create a minimal but valid session event log under ``path/.cambium``."""
    state = path / ".cambium"
    store = EventStore(state / "events.db", fsync_interval_s=0.01)
    try:
        store.append({"kind": "result", "payload": {"note": path.name}})
    finally:
        store.close()


def test_session_readers_and_cli_expose_paths_and_result_data(
    capsys, tmp_path: Path
) -> None:
    root = tmp_path / "sessions"
    _write_result(root / "old", 1.0)
    _write_events_db(root / "old")
    _write_result(root / "new", 2.0)
    _write_events_db(root / "new")

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


def test_session_show_rejects_incomplete_session_without_event_db(
    capsys, tmp_path: Path
) -> None:
    root = tmp_path / "sessions"
    _write_result(root / "incomplete", 1.0)
    # No events.db is created for this session on purpose.

    assert (
        cli.main(["session", "show", "--session-dir", str(root), "incomplete"]) == 1
    )
    captured = capsys.readouterr()
    assert "cambium session:" in captured.err
    assert "missing" in captured.err
    assert "Traceback" not in captured.err

    with pytest.raises(FileNotFoundError, match="events.db"):
        session.show_session(root / "incomplete")


def test_session_show_does_not_materialize_event_log(tmp_path: Path) -> None:
    root = tmp_path / "sessions"
    _write_result(root / "events", 1.0)
    _write_events_db(root / "events")

    view = session.show_session(root / "events")

    # The view exposes only the result artifact; the durable event log is
    # streamed by cambium.supervisor.read_events, not preloaded here.
    assert view.events == ()
    assert view.result["summary"] == "events"


def _write_lifecycle_events(path: Path, events: list[dict[str, Any]]) -> None:
    """Persist a multi-task lifecycle event log under ``path/.cambium``."""
    state = path / ".cambium"
    store = EventStore(state / "events.db", fsync_interval_s=0.01)
    try:
        for event in events:
            store.append(event)
    finally:
        store.close()


def test_session_status_renders_per_subagent_lifecycle(capsys, tmp_path: Path) -> None:
    root = tmp_path / "sessions"
    session_dir = root / "mix"
    _write_lifecycle_events(
        session_dir,
        [
            # alpha: still running at generation 2, turn 7, provider codex
            {"kind": "task_assigned", "task_id": "alpha", "generation": 1},
            {"kind": "spawned", "task_id": "alpha", "generation": 1},
            {"kind": "ready", "task_id": "alpha", "generation": 1},
            {"kind": "run_task", "task_id": "alpha", "generation": 1},
            {"kind": "heartbeat", "task_id": "alpha", "generation": 1,
             "payload": {"turn": 3}},
            {"kind": "usage_event", "task_id": "alpha", "generation": 1,
             "payload": {"provider": "codex", "turn": 4}},
            {"kind": "tool_event", "task_id": "alpha", "generation": 1,
             "payload": {"turn": 7}},
            {"kind": "heartbeat", "task_id": "alpha", "generation": 2,
             "payload": {"turn": 7}},
            # beta: worker failed at generation 1
            {"kind": "spawned", "task_id": "beta", "generation": 1},
            {"kind": "worker_failed", "task_id": "beta", "generation": 1},
            # gamma: merged successfully
            {"kind": "spawned", "task_id": "gamma", "generation": 1},
            {"kind": "result", "task_id": "gamma", "generation": 1,
             "payload": {"status": "succeeded"}},
            # delta: assigned but never spawned
            {"kind": "task_assigned", "task_id": "delta", "generation": 1},
        ],
    )

    assert cli.main(["session", "status", "--session-dir", str(root), "mix"]) == 0
    lines = capsys.readouterr().out.splitlines()
    assert len(lines) == 5
    alpha = next(line for line in lines if line.startswith("alpha"))
    assert "running" in alpha and "gen=2" in alpha and "turn=7" in alpha and "codex" in alpha
    beta = next(line for line in lines if line.startswith("beta"))
    assert "failed" in beta and "gen=1" in beta
    gamma = next(line for line in lines if line.startswith("gamma"))
    assert "done" in gamma
    delta = next(line for line in lines if line.startswith("delta"))
    assert "queued" in delta
    totals = next(line for line in lines if line.startswith("totals:"))
    assert totals == "totals: tokens=0 cost=$0.000000"


def test_session_status_rejects_missing_event_log(capsys, tmp_path: Path) -> None:
    root = tmp_path / "sessions"
    session_dir = root / "empty"
    session_dir.mkdir(parents=True)  # no .cambium/events.db on purpose

    assert cli.main(["session", "status", "--session-dir", str(root), "empty"]) == 1
    captured = capsys.readouterr()
    assert "cambium session:" in captured.err
    assert "event log is missing" in captured.err
    assert "Traceback" not in captured.err


def test_session_usage_renders_per_task_and_provider(capsys, tmp_path: Path) -> None:
    root = tmp_path / "sessions"
    session_dir = root / "usage"

    def _usage(task_id: str, provider: str, turn: int, total: int, cost: float) -> dict[str, Any]:
        return {
            "kind": "usage_event",
            "task_id": task_id,
            "generation": 1,
            "payload": {
                "provider": provider,
                "turn": turn,
                "usage": {"total_tokens": total},
                "estimated_cost_usd": cost,
            },
        }

    _write_lifecycle_events(
        session_dir,
        [
            _usage("alpha", "p1", 1, 100, 0.0015),
            _usage("alpha", "p1", 2, 50, 0.0005),
            _usage("beta", "p2", 1, 25, 0.00025),
            {"kind": "checkpoint", "task_id": "beta", "generation": 1, "payload": {"t": 1}},
        ],
    )

    assert cli.main(["session", "usage", "--session-dir", str(root), "usage"]) == 0
    lines = capsys.readouterr().out.splitlines()
    assert len(lines) == 5
    assert lines[0].startswith("usage: stats:")
    assert "calls=3" in lines[0]
    alpha = next(line for line in lines if line.startswith("alpha:"))
    assert "calls=2" in alpha and "cost=$0.002000" in alpha
    beta = next(line for line in lines if line.startswith("beta:"))
    assert "calls=1" in beta and "cost=$0.000250" in beta
    p1 = next(line for line in lines if line.startswith("p1:"))
    assert "calls=2" in p1
    p2 = next(line for line in lines if line.startswith("p2:"))
    assert "calls=1" in p2


def test_session_usage_rejects_missing_event_log(capsys, tmp_path: Path) -> None:
    root = tmp_path / "sessions"
    session_dir = root / "empty"
    session_dir.mkdir(parents=True)  # no .cambium/events.db on purpose

    assert cli.main(["session", "usage", "--session-dir", str(root), "empty"]) == 1
    captured = capsys.readouterr()
    assert "cambium session:" in captured.err
    assert "no usage event log" in captured.err
    assert "Traceback" not in captured.err


def test_session_resume_rejects_missing_plan(capsys, tmp_path: Path) -> None:
    session_dir = tmp_path / "crashed"
    (session_dir / ".cambium").mkdir(parents=True)

    assert cli.main(["session", "resume", str(session_dir)]) == 1
    captured = capsys.readouterr()
    assert "cambium session:" in captured.err
    assert "without a persisted plan" in captured.err
    assert "Traceback" not in captured.err


def test_session_resume_delegates_to_supervisor_main(
    monkeypatch, capsys, tmp_path: Path
) -> None:
    from cambium import supervisor

    session_dir = tmp_path / "crashed"
    (session_dir / ".cambium").mkdir(parents=True)
    (session_dir / "plan.json").write_text(
        json.dumps({"tasks": [{"task_id": "t1", "task": "x"}]}), encoding="utf-8"
    )
    calls: list[tuple[list[str], dict]] = []

    def fake_main(argv=None):
        calls.append((list(argv or []), {}))
        return 130

    monkeypatch.setattr(supervisor, "main", fake_main)

    assert cli.main(["session", "resume", str(session_dir)]) == 130
    assert capsys.readouterr().out == ""
    assert calls == [
        (["--session-dir", str(session_dir.resolve()), "--plan",
          str((session_dir / "plan.json").resolve())], {})
    ]
    delegated = calls[0][0]
    assert delegated.count("--plan") == 1
    assert delegated[delegated.index("--session-dir") + 1] == str(session_dir.resolve())


def test_stored_auth_is_handed_to_provider_worker_without_plan_leak(
    monkeypatch, tmp_path: Path
) -> None:
    repo = _repo(tmp_path / "repo")
    provider = "demo"
    env_name = derived_env_name(provider)
    config_path = _write_provider_file(
        tmp_path / "trusted" / "providers.json", [_provider_entry(provider)]
    )
    secret = "stored-secret-never-in-plan"
    auth_path = tmp_path / "home" / ".local" / "share" / "cambium" / "auth.json"
    store = AuthStore(auth_path)
    store.set_provider(provider, secret)
    monkeypatch.setattr(oneshot, "effective_home", lambda: tmp_path / "home")
    monkeypatch.setattr(oneshot, "AuthStore", lambda: store)
    monkeypatch.delenv("CAMBIUM_PROVIDERS", raising=False)
    monkeypatch.delenv(env_name, raising=False)
    captured: dict[str, object] = {}

    async def fake_run_plan(session_dir, plan, on_event=None, **kwargs):
        captured.update(plan=plan, kwargs=kwargs)
        return _plan_result()

    monkeypatch.setattr(oneshot.supervisor, "run_plan", fake_run_plan)
    asyncio.run(
        oneshot.run_oneshot(
            oneshot.OneShotConfig(
                prompt="use provider",
                repo=repo,
                provider=provider,
                provider_config_path=config_path,
            )
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


def test_environment_only_provider_key_is_handed_without_plan_or_artifact_leak(
    monkeypatch, tmp_path: Path
) -> None:
    repo = _repo(tmp_path / "repo")
    env_name = derived_env_name("environment-only")
    secret = "environment-only-secret"
    monkeypatch.setenv(env_name, secret)
    monkeypatch.delenv("CAMBIUM_PROVIDERS", raising=False)
    environment_before = dict(os.environ)

    def unexpected_auth_store():
        raise AssertionError("an environment-only credential must not require AuthStore")

    monkeypatch.setattr(oneshot, "AuthStore", unexpected_auth_store)
    captured: dict[str, object] = {}

    async def fake_run_plan(session_dir, plan, on_event=None, **kwargs):
        captured.update(session_dir=Path(session_dir), plan=plan, kwargs=kwargs)
        oneshot.supervisor._write_plan(Path(session_dir), plan)
        return _plan_result()

    monkeypatch.setattr(oneshot.supervisor, "run_plan", fake_run_plan)
    result = asyncio.run(
        oneshot.run_oneshot(
            oneshot.OneShotConfig(
                prompt="use environment",
                repo=repo,
                provider_env_keys=(env_name,),
                fanout_config={"tier": "fast", "model": "environment-model"},
            )
        )
    )

    assert result.exit_code == 0
    assert captured["kwargs"]["provider_environment"] == {env_name: secret}
    plan_text = json.dumps(captured["plan"])
    artifact_text = Path(captured["session_dir"] / "plan.json").read_text(encoding="utf-8")
    assert secret not in plan_text
    assert secret not in repr(captured["plan"])
    assert secret not in artifact_text
    assert dict(os.environ) == environment_before


def test_provider_run_persists_real_plan_without_credential(
    monkeypatch, tmp_path: Path
) -> None:
    """A real provider run persists ``plan.json`` and the handed-off credential
    must not appear in that persisted file (the prior ``test_plan_file_*``
    assertion was vacuous because the secret never reached the plan)."""
    repo = _repo(tmp_path / "repo")
    provider = "demo"
    env_name = derived_env_name(provider)
    _write_provider_file(
        tmp_path / "home" / ".config" / "cambium" / "providers.json",
        [_provider_entry(provider)],
    )
    secret = "persistent-plan-secret"
    auth_path = tmp_path / "home" / ".local" / "share" / "cambium" / "auth.json"
    store = AuthStore(auth_path)
    store.set_provider(provider, secret)
    monkeypatch.setattr(oneshot, "effective_home", lambda: tmp_path / "home")
    monkeypatch.setattr(oneshot, "AuthStore", lambda: store)
    monkeypatch.delenv(env_name, raising=False)
    monkeypatch.delenv("CAMBIUM_PROVIDERS", raising=False)

    captured_session_dir: list[Path] = []
    from cambium.supervisor import _write_plan

    async def persisting_run_plan(session_dir, plan, on_event=None, **kwargs):
        captured_session_dir.append(Path(session_dir))
        # Persist the accepted plan exactly as the real supervisor boundary
        # would, so the on-disk artifact is the genuine plan.json.
        _write_plan(Path(session_dir), plan)
        return _plan_result()

    monkeypatch.setattr(oneshot.supervisor, "run_plan", persisting_run_plan)

    result = asyncio.run(
        oneshot.run_oneshot(
            oneshot.OneShotConfig(prompt="use provider", repo=repo, provider=provider)
        )
    )
    assert result.exit_code == 0

    persisted_plan = captured_session_dir[0] / "plan.json"
    assert persisted_plan.is_file()
    assert stat.S_IMODE(persisted_plan.stat().st_mode) == 0o600
    plan_text = persisted_plan.read_text(encoding="utf-8")
    # The handed-off credential must not appear in the persisted plan.json.
    assert secret not in plan_text
    parsed = json.loads(plan_text)
    assert parsed["tasks"][0]["provider_env_keys"] == [env_name]


def test_concurrent_session_lock_contention_returns_busy_exit_code(
    monkeypatch, capsys, tmp_path: Path
) -> None:
    """M2: a session already under admission must surface one sanitized
    diagnostic at the CLI boundary and the documented temporary-failure
    exit code instead of a RuntimeError traceback."""
    from cambium.supervisor import _SessionAdmission

    repo = _repo(tmp_path / "repo")
    _write_provider_file(
        tmp_path / "home" / ".config" / "cambium" / "providers.json",
        [_provider_entry("demo")],
    )
    env_name = derived_env_name("demo")
    secret = "contention-secret"
    store = AuthStore(tmp_path / "home" / ".local" / "share" / "cambium" / "auth.json")
    store.set_provider("demo", secret)
    monkeypatch.setattr(oneshot, "effective_home", lambda: tmp_path / "home")
    monkeypatch.setattr(oneshot, "AuthStore", lambda: store)
    monkeypatch.delenv(env_name, raising=False)
    monkeypatch.delenv("CAMBIUM_PROVIDERS", raising=False)

    session_dir = tmp_path / "session"
    session_dir.mkdir()

    # Hold the admission lock to mimic another live supervisor on this session.
    blocking = _SessionAdmission(session_dir)
    blocking.acquire()
    try:
        exit_code = cli.main(
            [
                "run",
                "--repo",
                str(repo),
                "--session-dir",
                str(session_dir),
                "--provider",
                "demo",
                "--model",
                "demo-model",
                "fix the bug",
            ]
        )
    finally:
        blocking.release()

    assert exit_code == 75
    captured = capsys.readouterr()
    assert "cambium run:" in captured.err
    assert "already running" in captured.err
    assert "Traceback" not in captured.err
    # No session artifacts are written when admission refused the run.
    assert not (session_dir / "plan.json").exists()
    assert not (session_dir / ".cambium" / "result.json").exists()


def test_session_list_and_latest_emit_sanitized_error_for_unreadable_root(
    capsys, tmp_path: Path
) -> None:
    """M3: ``session list``/``latest`` against an unreadable root surface the
    same sanitized ``cambium session: ...`` diagnostic as ``show``."""
    if os.geteuid() == 0:
        pytest.skip("root bypasses directory read permissions")
    root = tmp_path / "sessions"
    root.mkdir(mode=0o700)
    _write_result(root / "old", 1.0)
    _write_events_db(root / "old")
    root.chmod(0o000)
    try:
        assert cli.main(["session", "list", "--session-dir", str(root)]) == 1
        first = capsys.readouterr()
        assert "cambium session:" in first.err
        assert "Traceback" not in first.err

        assert cli.main(["session", "latest", "--session-dir", str(root)]) == 1
        second = capsys.readouterr()
        assert "cambium session:" in second.err
        assert "Traceback" not in second.err
    finally:
        root.chmod(0o700)


def test_oversized_prompt_is_rejected_before_session_allocation(
    monkeypatch, tmp_path: Path
) -> None:
    """L1: a prompt over the supervisor's IPC frame limit is rejected before a
    session directory is allocated or plan.json is persisted."""
    repo = _repo(tmp_path / "repo")
    sessions_root = repo / ".cambium" / "sessions"

    async def unexpected_run_plan(*args, **kwargs):
        raise AssertionError("oversized prompt must not reach the supervisor")

    monkeypatch.setattr(oneshot.supervisor, "run_plan", unexpected_run_plan)

    oversized = "x" * (MAX_LINE_BYTES + 1)
    with pytest.raises(ValueError, match="frame limit"):
        asyncio.run(
            oneshot.run_oneshot(
                oneshot.OneShotConfig(
                    prompt=oversized,
                    repo=repo,
                    target_file="file.txt",
                    marker="// marker",
                )
            )
        )

    assert not sessions_root.exists() or not list(sessions_root.iterdir())


def test_run_parser_auto_flag_and_budget_flags() -> None:
    """--auto/--max-wall-s/--max-turns/--max-turns map onto the run parser."""
    parser = cli._build_parser()
    args = parser.parse_args(
        ["run", "--repo", ".", "--auto", "--max-wall-s", "900",
         "--max-tokens", "500000", "--max-turns", "40", "fix the bug"]
    )
    assert args.auto is True
    assert args.max_wall_s == 900
    assert args.max_tokens == 500000
    assert args.max_turns == 40
    # --provider/--model stay available for the pinned mode
    pinned = parser.parse_args(["run", "--provider", "demo", "--model", "m1", "p"])
    assert pinned.auto is False
    assert pinned.provider == "demo"
    assert pinned.model == "m1"


def test_run_parser_combined_provider_model_forms() -> None:
    """--provider NAME:MODEL and --model PROVIDER/MODEL carry provider+model
    in one string and resolve to separate values."""
    parser = cli._build_parser()

    provider_first = parser.parse_args(["run", "--provider", "demo:demo-model", "p"])
    assert cli._split_provider_model(provider_first.provider, provider_first.model) == (
        "demo",
        "demo-model",
    )

    model_first = parser.parse_args(["run", "--model", "demo/demo-model", "p"])
    assert cli._split_provider_model(model_first.provider, model_first.model) == (
        "demo",
        "demo-model",
    )

    agreeing = parser.parse_args(
        ["run", "--provider", "demo:demo-model", "--model", "demo-model", "p"]
    )
    assert cli._split_provider_model(agreeing.provider, agreeing.model) == (
        "demo",
        "demo-model",
    )

    conflicting = parser.parse_args(
        ["run", "--provider", "demo:demo-model", "--model", "other/other-model", "p"]
    )
    with pytest.raises(ValueError, match="conflicting models") as model_error:
        cli._split_provider_model(conflicting.provider, conflicting.model)
    assert "demo-model" not in str(model_error.value)
    assert "other-model" not in str(model_error.value)

    with pytest.raises(ValueError, match="conflicting providers") as provider_error:
        cli._split_provider_model("demo", "other/other-model")
    assert "demo" not in str(provider_error.value)
    assert "other" not in str(provider_error.value)

    with pytest.raises(SystemExit):
        parser.parse_args(["run", "--model", "demo/", "p"])
    with pytest.raises(SystemExit):
        parser.parse_args(["run", "--provider", "demo:", "p"])
    with pytest.raises(SystemExit):
        parser.parse_args(["run", "--provider", "demo!bad:model", "p"])
