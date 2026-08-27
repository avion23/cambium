"""CLI routing-budget flag scenarios."""

from __future__ import annotations

from pathlib import Path

from cambium import cli, oneshot
from cambium.supervisor import PlanResult, TaskResult


def _plan(config: oneshot.OneShotConfig, tmp_path: Path) -> dict:
    return oneshot.build_plan(config, repo=tmp_path, session_dir=tmp_path / "session")


def test_run_default_max_restarts_resolves_to_one(tmp_path: Path) -> None:
    args = cli._build_parser().parse_args(["run", "prompt"])
    assert args.max_restarts is None
    assert (
        _plan(
            oneshot.OneShotConfig(
                prompt=args.prompt, repo=tmp_path, max_restarts=args.max_restarts
            ),
            tmp_path,
        )["tasks"][0]["max_restarts"]
        == 1
    )


def test_run_max_restarts_zero_is_forwarded(monkeypatch, tmp_path: Path) -> None:
    captured: list[oneshot.OneShotConfig] = []

    async def fake_run(config: oneshot.OneShotConfig) -> PlanResult:
        captured.append(config)
        return PlanResult((TaskResult(task_id="oneshot", status="succeeded", exit_code=0),))

    monkeypatch.setattr(oneshot, "run_oneshot", fake_run)

    assert cli.main(["run", "prompt", "--repo", str(tmp_path), "--max-restarts", "0"]) == 0
    assert captured[0].max_restarts == 0
    assert _plan(captured[0], tmp_path)["tasks"][0]["max_restarts"] == 0


def test_interactive_default_max_restarts_stays_one(tmp_path: Path) -> None:
    config = oneshot.OneShotConfig(prompt="prompt", repo=tmp_path, interactive=True)
    assert _plan(config, tmp_path)["tasks"][0]["max_restarts"] == 1
