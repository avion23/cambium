"""Parallel dispatch coverage for queued interactive TUI prompts."""

from __future__ import annotations

import asyncio
import io
from pathlib import Path

from cambium import oneshot, tui
from cambium.oneshot import OneShotConfig
from cambium.supervisor import PlanResult, TaskResult, _Runtime


class _Tty(io.StringIO):
    def isatty(self) -> bool:
        return True


def test_admission_is_unlimited_by_default_and_honors_explicit_cap(tmp_path: Path) -> None:
    assert _Runtime(tmp_path / "unlimited", None)._admission_semaphore is None
    capped = _Runtime(tmp_path / "capped", None, max_concurrent_tasks=2)
    assert capped._admission_semaphore is not None
    assert capped._admission_semaphore._value == 2


def test_queued_tui_prompts_use_one_flat_plan(
    monkeypatch, tmp_path: Path
) -> None:
    monkeypatch.setattr(oneshot, "preflight", lambda *_args, **_kwargs: None)
    monkeypatch.setattr(oneshot, "admit_session", lambda *_args, **_kwargs: None)
    monkeypatch.setattr(
        oneshot,
        "_resolve_provider",
        lambda config, _repo: (config, {}),
    )
    calls: list[tuple[Path, dict, dict]] = []

    async def fake_run_plan(session_dir, plan, on_event=None, **kwargs):
        del on_event
        calls.append((Path(session_dir), plan, kwargs))
        if len(calls) == 1:
            await asyncio.sleep(0.1)
        return PlanResult(
            tuple(
                TaskResult(task_id=task["task_id"], status="succeeded", exit_code=0)
                for task in plan["tasks"]
            )
        )

    monkeypatch.setattr(oneshot.supervisor, "run_plan", fake_run_plan)
    source = _Tty("first\nsecond\nthird\n/exit\n")
    output = _Tty()

    code = asyncio.run(
        tui.run_tui(
            OneShotConfig(repo=tmp_path, session_root=tmp_path / "interactive", provider="test"),
            input_stream=source,
            output_stream=output,
            error_stream=io.StringIO(),
            max_workers=2,
        )
    )

    assert code == 0
    assert [len(plan["tasks"]) for _session, plan, _kwargs in calls] == [1, 2]
    assert [task["task"] for task in calls[1][1]["tasks"]] == ["second", "third"]
    assert all(kwargs["max_concurrent_tasks"] == 2 for _session, _plan, kwargs in calls)
