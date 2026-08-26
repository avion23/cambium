"""Operator-facing TUI usability scenarios."""

from __future__ import annotations

import asyncio
import io
import json
import os
import time
from pathlib import Path

from cambium import tui
from cambium.interactive import InteractiveSession
from cambium.oneshot import OneShotConfig
from cambium.provider_scheduler import QuotaLedger
from cambium.supervisor import PlanResult, TaskResult


class _Tty(io.StringIO):
    def isatty(self) -> bool:
        return True


class _History:
    def __init__(self) -> None:
        self.read: Path | None = None
        self.written: Path | None = None
        self.length: int | None = None

    def read_history_file(self, path) -> None:
        self.read = Path(path)

    def write_history_file(self, path) -> None:
        self.written = Path(path)
        Path(path).write_text("history\n", encoding="utf-8")

    def set_history_length(self, length: int) -> None:
        self.length = length


def _provider_config(path: Path) -> Path:
    path.write_text(
        json.dumps(
            {
                "providers": [
                    {
                        "name": "ready-a",
                        "tier": "balanced",
                        "base_url": "http://127.0.0.1:9999/v1",
                        "api_key_env": "CAMBIUM_PROVIDER_READY_A_API_KEY",
                        "model": "model-a",
                    },
                    {
                        "name": "ready-b",
                        "tier": "strong",
                        "base_url": "http://127.0.0.1:9999/v1",
                        "api_key_env": "CAMBIUM_PROVIDER_READY_B_API_KEY",
                        "model": "model-b",
                    },
                    {
                        "name": "missing",
                        "tier": "fast",
                        "base_url": "http://127.0.0.1:9999/v1",
                        "api_key_env": "CAMBIUM_PROVIDER_MISSING_API_KEY",
                        "model": "model-missing",
                    },
                    {
                        "name": "disabled",
                        "tier": "fast",
                        "base_url": "http://127.0.0.1:9999/v1",
                        "api_key_env": "CAMBIUM_PROVIDER_DISABLED_API_KEY",
                        "model": "model-disabled",
                        "enabled": False,
                    },
                ]
            }
        ),
        encoding="utf-8",
    )
    return path


def _model_config(tmp_path: Path, provider_config: Path) -> OneShotConfig:
    return OneShotConfig(
        repo=tmp_path,
        session_root=tmp_path / "interactive",
        provider="ready-a",
        model="model-a",
        provider_config_path=provider_config,
    )


def test_tui_operator_commands_render_without_provider_calls(tmp_path: Path) -> None:
    source = _Tty("/dashboard\n/events\n/branches\n/fork\n/compact\n/model\n/cancel\n/exit\n")
    output = _Tty()
    error = io.StringIO()

    code = asyncio.run(
        tui.run_tui(
            OneShotConfig(repo=tmp_path, session_root=tmp_path / "interactive"),
            input_stream=source,
            output_stream=output,
            error_stream=error,
        )
    )

    text = output.getvalue()
    assert code == 0
    assert error.getvalue() == ""
    assert "Cambium interactive session" in text
    assert "events: none" in text
    assert "branches: none" in text
    assert "cannot fork: no successful checkpoint" in text
    assert "compact: no successful checkpoint" in text
    assert "auto/auto" in text
    assert "press Ctrl-C while a turn is running" in text
    assert "┌ Cambium" in text


def test_status_command_keeps_dropped_context_fields_available(tmp_path: Path) -> None:
    output = _Tty()
    error = io.StringIO()

    code = asyncio.run(
        tui.run_tui(
            OneShotConfig(repo=tmp_path, session_root=tmp_path / "interactive"),
            input_stream=_Tty("/status\n/exit\n"),
            output_stream=output,
            error_stream=error,
        )
    )

    text = output.getvalue()
    assert code == 0
    assert error.getvalue() == ""
    assert "session=" in text
    assert "branch=" in text and "generation=" not in text.split("SYSTEM ▸", 1)[0]
    assert "epoch=" in text
    assert "checkpoint=" in text


def test_tui_quota_command_renders_seeded_ledger_rows(monkeypatch, tmp_path: Path) -> None:
    quota_db = tmp_path / "provider-quota.db"
    monkeypatch.setenv("CAMBIUM_QUOTA_DB", str(quota_db))
    QuotaLedger(quota_db).observe(
        "zai",
        "five-hour",
        reset_at=time.time() + 3600,
        allowance_tokens=1000,
        remaining_tokens=700,
        allowance_requests=10,
        remaining_requests=8,
    )

    output = _Tty()
    error = io.StringIO()
    code = asyncio.run(
        tui.run_tui(
            OneShotConfig(repo=tmp_path, session_root=tmp_path / "interactive"),
            input_stream=_Tty("/quota\n/exit\n"),
            output_stream=output,
            error_stream=error,
        )
    )

    text = output.getvalue()
    assert code == 0
    assert error.getvalue() == ""
    assert "Unknown command: /quota" not in text
    assert "quota:" in text
    assert "zai/five-hour: 700/1000 tok, 8/10 req" in text


def test_model_lists_ready_targets_and_marks_current(monkeypatch, tmp_path: Path) -> None:
    provider_config = _provider_config(tmp_path / "providers.json")
    monkeypatch.setenv("CAMBIUM_PROVIDER_READY_A_API_KEY", "test-key-a-not-output")
    monkeypatch.setenv("CAMBIUM_PROVIDER_READY_B_API_KEY", "test-key-b-not-output")
    monkeypatch.delenv("CAMBIUM_PROVIDER_MISSING_API_KEY", raising=False)
    monkeypatch.delenv("CAMBIUM_PROVIDER_DISABLED_API_KEY", raising=False)
    output = _Tty()
    error = io.StringIO()

    code = asyncio.run(
        tui.run_tui(
            _model_config(tmp_path, provider_config),
            input_stream=_Tty("/model\n/exit\n"),
            output_stream=output,
            error_stream=error,
        )
    )

    text = output.getvalue()
    assert code == 0
    assert error.getvalue() == ""
    assert "eligible provider/model targets (enabled + credential-ready):" in text
    assert "ready-a:model-a (current)" in text
    assert "ready-b:model-b" in text
    assert "model-missing" not in text
    assert "model-disabled" not in text
    assert "test-key-" not in text


def test_model_switch_persists_for_subsequent_turns(monkeypatch, tmp_path: Path) -> None:
    provider_config = _provider_config(tmp_path / "providers.json")
    monkeypatch.setenv("CAMBIUM_PROVIDER_READY_A_API_KEY", "test-key-a-not-output")
    monkeypatch.setenv("CAMBIUM_PROVIDER_READY_B_API_KEY", "test-key-b-not-output")
    config = _model_config(tmp_path, provider_config)

    output = _Tty()
    error = io.StringIO()
    code = asyncio.run(
        tui.run_tui(
            config,
            input_stream=_Tty("/model ready-b:model-b\n/exit\n"),
            output_stream=output,
            error_stream=error,
        )
    )

    assert code == 0
    assert error.getvalue() == ""
    assert "model preference set: provider=ready-b model=model-b" in output.getvalue()

    reloaded = InteractiveSession(config)
    assert reloaded.provider == "ready-b"
    assert reloaded.model == "model-b"
    manifest = json.loads(
        (tmp_path / "interactive" / ".cambium" / "interactive.json").read_text(encoding="utf-8")
    )
    assert manifest["provider_preference"] == "ready-b"
    assert manifest["model_preference"] == "model-b"

    turn = reloaded.prepare_turn("continue")
    assert turn.config.provider == "ready-b"
    assert turn.config.model == "model-b"
    assert turn.config.assigned_provider == "ready-b"


def test_trimmed_q_exits_without_submitting_a_turn(tmp_path: Path) -> None:
    output = _Tty()
    error = io.StringIO()

    code = asyncio.run(
        tui.run_tui(
            OneShotConfig(repo=tmp_path, session_root=tmp_path / "interactive"),
            input_stream=_Tty("  q  \n"),
            output_stream=output,
            error_stream=error,
        )
    )

    assert code == 0
    assert error.getvalue() == ""
    assert "Unknown command" not in output.getvalue()
    assert not tuple((tmp_path / "interactive").glob("turn-*"))


def test_exit_command_still_exits_without_submitting_a_turn(tmp_path: Path) -> None:
    output = _Tty()
    error = io.StringIO()

    code = asyncio.run(
        tui.run_tui(
            OneShotConfig(repo=tmp_path, session_root=tmp_path / "interactive"),
            input_stream=_Tty("/exit\n"),
            output_stream=output,
            error_stream=error,
        )
    )

    assert code == 0
    assert error.getvalue() == ""
    assert not tuple((tmp_path / "interactive").glob("turn-*"))


def test_new_command_starts_a_fresh_branch_without_exiting(tmp_path: Path) -> None:
    output = _Tty()
    error = io.StringIO()

    code = asyncio.run(
        tui.run_tui(
            OneShotConfig(repo=tmp_path, session_root=tmp_path / "interactive"),
            input_stream=_Tty("/new\n/session\n/exit\n"),
            output_stream=output,
            error_stream=error,
        )
    )

    assert code == 0
    assert error.getvalue() == ""
    assert "Started a fresh semantic branch" in output.getvalue()
    assert "branch=2" in output.getvalue()


def test_tui_history_is_private_and_bounded(monkeypatch, tmp_path: Path) -> None:
    history = _History()
    monkeypatch.setattr(tui, "_readline", history)
    path = tmp_path / ".cambium" / "tui_history"
    path.parent.mkdir(parents=True)
    path.write_text("old\n", encoding="utf-8")

    tui._load_history(path)
    tui._save_history(path)

    assert history.read == path
    assert history.written == path
    assert history.length == 1000
    assert os.stat(path).st_mode & 0o777 == 0o600


def test_help_documents_turn_cancellation() -> None:
    assert "Ctrl-C cancels" in tui._HELP
    assert "toggle full command/output details" in tui._HELP
    assert "/dashboard" in tui._HELP
    assert "/events" in tui._HELP
    assert "blank line submits" in tui._HELP


def test_v_toggles_tool_details_without_submitting_a_prompt(monkeypatch, tmp_path: Path) -> None:
    toggles: list[bool] = []

    def toggle(self) -> bool:
        toggles.append(self.tool_details_expanded)
        return not self.tool_details_expanded

    monkeypatch.setattr(tui.Transcript, "toggle_tool_details", toggle)
    source = _Tty("v\n/exit\n")

    assert (
        asyncio.run(
            tui.run_tui(
                OneShotConfig(repo=tmp_path, session_root=tmp_path / "interactive"),
                input_stream=source,
                output_stream=_Tty(),
                error_stream=io.StringIO(),
            )
        )
        == 0
    )
    assert toggles == [False]


def test_tty_bracketed_paste_preserves_embedded_newlines() -> None:
    source = _Tty("\x1b[200~line one\nline two\x1b[201~\n")
    output = _Tty()

    assert tui._read_prompt(source, output) == "line one\nline two"
    assert tui._BRACKETED_PASTE_ENABLE in output.getvalue()
    assert tui._BRACKETED_PASTE_DISABLE in output.getvalue()


def test_tty_trailing_backslash_continues_until_next_line() -> None:
    source = _Tty("line one\\\nline two\n")
    output = _Tty()

    assert tui._read_prompt(source, output) == "line one\nline two"


def test_ctrl_c_cancels_active_turn_and_returns_to_prompt(monkeypatch, tmp_path: Path) -> None:
    from cambium.interactive import InteractiveSession

    class _Turn:
        def __init__(self, number: int, session_dir: Path) -> None:
            self.number = number
            self.session_dir = session_dir

    def prepare_turn(self, _prompt):
        self._turn += 1
        session_dir = self.root / f"turn-{self._turn:04d}"
        session_dir.mkdir(parents=True)
        return _Turn(self._turn, session_dir)

    async def fake_run(self, _turn, *, on_event=None):
        del on_event
        await asyncio.Event().wait()

    def complete_turn(self, _turn, *, succeeded):
        assert succeeded is False

    monkeypatch.setattr(InteractiveSession, "prepare_turn", prepare_turn, raising=False)
    monkeypatch.setattr(InteractiveSession, "run_turn", fake_run, raising=False)
    monkeypatch.setattr(InteractiveSession, "complete_turn", complete_turn, raising=False)

    source = _Tty("work\n/exit\n")
    output = _Tty()

    async def scenario() -> int:
        loop = asyncio.get_running_loop()
        monkeypatch.setattr(
            loop,
            "add_signal_handler",
            lambda _signal, callback: loop.call_soon(callback),
        )
        monkeypatch.setattr(loop, "remove_signal_handler", lambda _signal: True)
        return await tui.run_tui(
            OneShotConfig(repo=tmp_path, session_root=tmp_path / "interactive"),
            input_stream=source,
            output_stream=output,
            error_stream=io.StringIO(),
        )

    assert asyncio.run(scenario()) == 0
    assert "turn cancelled" in output.getvalue()


def test_tty_input_during_turn_is_queued_and_runs_after(monkeypatch, tmp_path: Path) -> None:
    from cambium.interactive import InteractiveSession

    prompts: list[str] = []

    async def fake_run(self, turn, *, on_event=None):
        del on_event
        prompts.append(turn.config.prompt)
        if turn.number == 1:
            await asyncio.sleep(0.05)
        return PlanResult(
            (TaskResult(task_id=f"task-{turn.number}", status="succeeded", exit_code=0),)
        )

    monkeypatch.setattr(InteractiveSession, "run_turn", fake_run)
    source = _Tty("first\nfollow-up\n/exit\n")
    output = _Tty()

    code = asyncio.run(
        tui.run_tui(
            OneShotConfig(repo=tmp_path, session_root=tmp_path / "interactive"),
            input_stream=source,
            output_stream=output,
            error_stream=io.StringIO(),
        )
    )

    assert code == 0
    assert prompts == ["first", "follow-up"]
    assert "queued: follow-up" in output.getvalue()


def test_bang_cancel_cancels_active_turn(monkeypatch, tmp_path: Path) -> None:
    from cambium.interactive import InteractiveSession

    prompts: list[str] = []
    completions: list[bool] = []

    async def fake_run(self, turn, *, on_event=None):
        del on_event
        prompts.append(turn.config.prompt)
        await asyncio.Event().wait()

    def complete_turn(self, _turn, *, succeeded):
        completions.append(succeeded)

    monkeypatch.setattr(InteractiveSession, "run_turn", fake_run)
    monkeypatch.setattr(InteractiveSession, "complete_turn", complete_turn)
    source = _Tty("work\n!cancel\n/exit\n")
    output = _Tty()

    code = asyncio.run(
        tui.run_tui(
            OneShotConfig(repo=tmp_path, session_root=tmp_path / "interactive"),
            input_stream=source,
            output_stream=output,
            error_stream=io.StringIO(),
        )
    )

    assert code == 0
    assert prompts == ["work"]
    assert completions == [False]
    assert "turn cancelled" in output.getvalue()
