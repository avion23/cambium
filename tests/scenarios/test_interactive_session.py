"""Persistent interactive branch and TUI input scenarios."""

from __future__ import annotations

import asyncio
import io
from pathlib import Path

from cambium import tui
from cambium.interactive import InteractiveSession
from cambium.oneshot import OneShotConfig
from cambium.supervisor import PlanResult, TaskResult


class _Tty(io.StringIO):
    def isatty(self) -> bool:
        return True


def _cache_key() -> dict[str, object]:
    digest = "a" * 64
    return {
        "provider": "provider-a",
        "model": "model-a",
        "protocol": "chat_completions",
        "reasoning_effort": None,
        "system_sha256": digest,
        "tools_sha256": digest,
        "prefix_sha256": digest,
        "suffix_sha256": digest,
        "full_sha256": digest,
        "prefix_bytes": 1024,
        "message_count": 3,
        "redacted": False,
        "provider_boundary": {},
    }


def _checkpoint_event(ref: str) -> dict[str, object]:
    return {
        "seq": 1,
        "kind": "context_checkpoint",
        "task_id": "interactive-main",
        "payload": {
            "checkpoint_ref": ref,
            "epoch": 3,
            "cache_key": _cache_key(),
        },
    }


def test_interactive_session_carries_exact_and_semantic_seed(tmp_path: Path) -> None:
    root = tmp_path / "interactive"
    session = InteractiveSession(OneShotConfig(repo=tmp_path, session_root=root))
    first = session.prepare_turn("inspect")
    checkpoint_ref = "interactive-main/epoch-3-" + "b" * 64 + ".json"
    checkpoint = first.session_dir / ".cambium" / "checkpoints" / checkpoint_ref
    checkpoint.parent.mkdir(parents=True)
    checkpoint.write_text("{}", encoding="utf-8")

    session.observe_event(first, _checkpoint_event(checkpoint_ref))
    session.complete_turn(first, succeeded=True)
    second = session.prepare_turn("continue")

    copied = second.session_dir / ".cambium" / "checkpoints" / checkpoint_ref
    assert copied.read_text(encoding="utf-8") == "{}"
    assert second.summary_trunk_ref == checkpoint_ref
    assert second.context_fork is not None
    assert second.context_fork["provider"] == "provider-a"
    assert second.config.provider == "provider-a"
    assert second.config.model == "model-a"
    assert second.config.task_id == "interactive-main"

    reloaded = InteractiveSession(OneShotConfig(repo=tmp_path, session_root=root))
    assert reloaded.turn == 1
    assert reloaded.provider == "provider-a"
    assert reloaded.seed is not None
    assert reloaded.seed.checkpoint_ref == checkpoint_ref


def test_interactive_reset_starts_fresh_branch(tmp_path: Path) -> None:
    session = InteractiveSession(
        OneShotConfig(repo=tmp_path, session_root=tmp_path / "interactive")
    )
    session.reset()
    assert session.seed is None
    assert "branch=2" in session.describe()


def test_tui_multiline_input() -> None:
    source = io.StringIO("<<<\nline one\nline two\n>>>\n")
    out = io.StringIO()
    assert tui._read_prompt(source, out) == "line one\nline two"
    assert out.getvalue() == "cambium> ... ... ... "


def test_tty_tui_reuses_checkpoint_on_second_prompt(monkeypatch, tmp_path: Path) -> None:
    seen_context_forks: list[dict[str, object] | None] = []

    async def fake_run(self, turn, *, on_event=None):
        seen_context_forks.append(turn.context_fork)
        checkpoint_ref = f"interactive-main/epoch-{turn.number}-" + f"{turn.number:064x}" + ".json"
        checkpoint = turn.session_dir / ".cambium" / "checkpoints" / checkpoint_ref
        checkpoint.parent.mkdir(parents=True, exist_ok=True)
        checkpoint.write_text("{}", encoding="utf-8")
        if on_event is not None:
            on_event(_checkpoint_event(checkpoint_ref))
            on_event(
                {
                    "seq": 2,
                    "kind": "usage_event",
                    "task_id": "interactive-main",
                    "payload": {
                        "provider": "provider-a",
                        "model": "model-a",
                        "turn": turn.number,
                        "latency_s": 1.0,
                        "usage": {
                            "input_tokens": 100,
                            "output_tokens": 25,
                            "total_tokens": 125,
                        },
                    },
                }
            )
        return PlanResult(
            results=(TaskResult(task_id=f"task-{turn.number}", status="succeeded", exit_code=0),)
        )

    monkeypatch.setattr(InteractiveSession, "run_turn", fake_run)
    source = _Tty("one\ntwo\n/usage\n/exit\n")
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

    assert code == 0
    assert error.getvalue() == ""
    assert seen_context_forks[0] is None
    assert seen_context_forks[1] is not None
    assert "tokens=250" in output.getvalue()
    assert "provider=provider-a model=model-a" in output.getvalue()


def test_reset_excludes_prior_turns_from_restored_branch(tmp_path: Path) -> None:
    session = InteractiveSession(
        OneShotConfig(repo=tmp_path, session_root=tmp_path / "interactive")
    )
    first = session.prepare_turn("one")
    session.complete_turn(first, succeeded=False)
    assert session.active_turn_dirs() == (first.session_dir,)

    session.reset()

    assert session.active_turn_dirs() == ()
    second = session.prepare_turn("two")
    assert second.number == 2


def test_restore_history_folds_completed_current_branch(monkeypatch, tmp_path: Path) -> None:
    session = InteractiveSession(
        OneShotConfig(repo=tmp_path, session_root=tmp_path / "interactive")
    )
    first = session.prepare_turn("one")
    event_db = first.session_dir / ".cambium" / "events.db"
    event_db.parent.mkdir(parents=True, exist_ok=True)
    event_db.touch()
    session.complete_turn(first, succeeded=False)

    monkeypatch.setattr(
        tui,
        "read_events_file",
        lambda _path: [
            {
                "seq": 1,
                "kind": "usage_event",
                "task_id": "interactive-main",
                "payload": {
                    "provider": "provider-a",
                    "model": "model-a",
                    "latency_s": 1.0,
                    "usage": {
                        "input_tokens": 10,
                        "output_tokens": 5,
                        "total_tokens": 15,
                    },
                },
            }
        ],
    )

    cumulative, _snapshot = tui._restore_history(session)

    assert cumulative.calls == 1
    assert cumulative.total_tokens == 15
