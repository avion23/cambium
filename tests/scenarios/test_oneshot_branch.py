"""Regression tests for per-session one-shot branches."""

from __future__ import annotations

import asyncio
import subprocess
from pathlib import Path
from typing import Any

from cambium import oneshot
from cambium.supervisor import PlanResult, TaskResult


def _repo(path: Path) -> Path:
    path.mkdir()
    subprocess.run(
        ["git", "init", "-b", "main", str(path)], check=True, capture_output=True
    )
    subprocess.run(["git", "-C", str(path), "config", "user.name", "oneshot-test"], check=True)
    subprocess.run(
        ["git", "-C", str(path), "config", "user.email", "oneshot@test"], check=True
    )
    (path / "file.txt").write_text("file\n", encoding="utf-8")
    subprocess.run(["git", "-C", str(path), "add", "file.txt"], check=True, capture_output=True)
    subprocess.run(
        ["git", "-C", str(path), "commit", "-m", "initial"],
        check=True,
        capture_output=True,
    )
    return path


def test_default_sessions_get_distinct_branches_and_succeed(monkeypatch, tmp_path: Path) -> None:
    repo = _repo(tmp_path / "repo")
    runs: list[tuple[Path, str]] = []

    async def fake_run_plan(session_dir, plan, on_event=None, **kwargs):
        task = plan["tasks"][0]
        worktree = Path(task["worktree_path"])
        subprocess.run(
            [
                "git",
                "-C",
                str(repo),
                "worktree",
                "add",
                "-b",
                task["branch"],
                str(worktree),
                "main",
            ],
            check=True,
            capture_output=True,
            text=True,
        )
        target = worktree / task["target_file"]
        target.write_text(
            target.read_text(encoding="utf-8").rstrip("\n") + "\n" + task["marker"] + "\n",
            encoding="utf-8",
        )
        subprocess.run(
            ["git", "-C", str(worktree), "add", task["target_file"]],
            check=True,
            capture_output=True,
        )
        subprocess.run(
            ["git", "-C", str(worktree), "commit", "-m", "marker"],
            check=True,
            capture_output=True,
        )
        runs.append((Path(session_dir), task["branch"]))
        return PlanResult(
            (TaskResult(task_id=task["task_id"], status="succeeded", exit_code=0),)
        )

    monkeypatch.setattr(oneshot.supervisor, "run_plan", fake_run_plan)
    config = oneshot.OneShotConfig(
        prompt="append marker",
        repo=repo,
        target_file="file.txt",
        marker="// marker",
    )

    first = asyncio.run(oneshot.run_oneshot(config))
    second = asyncio.run(oneshot.run_oneshot(config))

    assert first.exit_code == second.exit_code == 0
    assert runs[0][0] != runs[1][0]
    assert runs[0][1] != runs[1][1]
    assert all(
        (session_dir / "wt" / "file.txt").read_text(encoding="utf-8").endswith("// marker\n")
        for session_dir, _branch in runs
    )


def test_default_branch_is_stable_and_explicit_branch_is_preserved(tmp_path: Path) -> None:
    repo = _repo(tmp_path / "repo")
    session_dir = tmp_path / "session-provider-secret"
    config = oneshot.OneShotConfig(prompt="prompt", repo=repo)

    first = oneshot.build_plan(config, repo, session_dir)
    second = oneshot.build_plan(config, repo, session_dir)
    other = oneshot.build_plan(config, repo, tmp_path / "other-session")
    first_branch = first["tasks"][0]["branch"]

    assert first_branch == second["tasks"][0]["branch"]
    assert first_branch != other["tasks"][0]["branch"]
    assert first_branch.startswith("cambium-oneshot-")
    assert "provider-secret" not in first_branch
    assert (
        subprocess.run(
            ["git", "check-ref-format", f"refs/heads/{first_branch}"], check=False
        ).returncode
        == 0
    )

    explicit = oneshot.build_plan(
        oneshot.OneShotConfig(prompt="prompt", repo=repo, branch="release/topic"),
        repo,
        session_dir,
    )
    assert explicit["tasks"][0]["branch"] == "release/topic"


# --------------------------------------------------------------------------- #
# --auto (routing mode, solution C): candidates from enabled providers with
# stored credentials; supervisor resolves the (provider, model, tier).
# --------------------------------------------------------------------------- #


def _write_providers(path: Path) -> Path:
    """Two enabled providers serving different models."""
    import json

    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(
            {
                "providers": [
                    {
                        "name": "pa",
                        "tier": "strong",
                        "base_url": "http://127.0.0.1:1",
                        "api_key_env": "CAMBIUM_PROVIDER_PA_API_KEY",
                        "model": "model-a",
                        "priority": 0,
                        "enabled": True,
                    },
                    {
                        "name": "pb",
                        "tier": "strong",
                        "base_url": "http://127.0.0.1:1",
                        "api_key_env": "CAMBIUM_PROVIDER_PB_API_KEY",
                        "model": "model-b",
                        "priority": 0,
                        "enabled": True,
                    },
                ]
            }
        ),
        encoding="utf-8",
    )
    return path


def test_auto_mode_candidates_and_plan_shape(monkeypatch, tmp_path: Path) -> None:
    """--auto builds model_candidates from providers with stored credentials
    and leaves the (provider, model, tier) to the supervisor resolution."""
    repo = _repo(tmp_path / "repo")
    config_path = _write_providers(tmp_path / "providers.json")
    # only provider pa has a stored credential
    stored = {"CAMBIUM_PROVIDER_PA_API_KEY": "secret-a"}

    def _stored(name: str) -> dict[str, str]:
        if name not in stored:
            raise ValueError("provider credential is not configured")
        return {name: stored[name]}

    monkeypatch.setattr(oneshot, "_stored_provider_environment", _stored)

    captured: dict[str, Any] = {}

    async def fake_run_plan(session_dir, plan, on_event=None, **kwargs):
        captured.update(plan)
        return PlanResult(
            (TaskResult(task_id="oneshot", status="succeeded", exit_code=0),)
        )

    monkeypatch.setattr(oneshot.supervisor, "run_plan", fake_run_plan)
    config = oneshot.OneShotConfig(
        prompt="run one auto task",
        repo=repo,
        auto=True,
        provider_config_path=config_path,
    )
    asyncio.run(oneshot.run_oneshot(config))

    spec = captured["tasks"][0]
    assert spec["model_candidates"] == ["model-a"]  # pb has no stored key
    assert spec["fanout_config"] == {}  # resolution fills model + tier
    assert spec["provider_env_keys"] == ["CAMBIUM_PROVIDER_PA_API_KEY"]


def test_auto_mode_rejects_pinned_provider_or_model(monkeypatch, tmp_path: Path) -> None:
    repo = _repo(tmp_path / "repo")
    config = oneshot.OneShotConfig(
        prompt="p", repo=repo, auto=True, provider="pa",
        provider_config_path=_write_providers(tmp_path / "providers.json"),
    )
    try:
        asyncio.run(oneshot.run_oneshot(config))
    except ValueError as exc:
        assert "cannot be combined" in str(exc)
    else:
        raise AssertionError("auto + provider must be rejected")


def test_auto_mode_requires_stored_credential(monkeypatch, tmp_path: Path) -> None:
    repo = _repo(tmp_path / "repo")
    config_path = _write_providers(tmp_path / "providers.json")
    monkeypatch.setattr(
        oneshot, "_stored_provider_environment",
        lambda name: (_ for _ in ()).throw(ValueError("not configured")),
    )
    config = oneshot.OneShotConfig(
        prompt="p", repo=repo, auto=True, provider_config_path=config_path,
    )
    try:
        asyncio.run(oneshot.run_oneshot(config))
    except ValueError as exc:
        assert "stored credentials" in str(exc)
    else:
        raise AssertionError("auto with no stored credentials must fail closed")
