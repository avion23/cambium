"""Regression tests for per-session one-shot branches."""

from __future__ import annotations

import asyncio
import subprocess
from pathlib import Path

import pytest

from cambium import oneshot
from cambium.supervisor import PlanResult, TaskResult


def _repo(path: Path) -> Path:
    path.mkdir()
    subprocess.run(["git", "init", "-b", "main", str(path)], check=True, capture_output=True)
    subprocess.run(["git", "-C", str(path), "config", "user.name", "oneshot-test"], check=True)
    subprocess.run(["git", "-C", str(path), "config", "user.email", "oneshot@test"], check=True)
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
        return PlanResult((TaskResult(task_id=task["task_id"], status="succeeded", exit_code=0),))

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


def _write_providers(path: Path, *, pa_key: str = "secret-a", pb_key: str = "") -> Path:
    """Two providers serving different models, with keys stored in the file."""
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
                        "api_key": pa_key,
                        "model": "model-a",
                        "priority": 0,
                        "enabled": True,
                    },
                    {
                        "name": "pb",
                        "tier": "strong",
                        "base_url": "http://127.0.0.1:1",
                        "api_key_env": "CAMBIUM_PROVIDER_PB_API_KEY",
                        "api_key": pb_key,
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


def test_auto_mode_candidates_and_plan_shape(tmp_path: Path) -> None:
    """--auto builds model_candidates from providers with file-backed credentials
    and leaves the (provider, model, tier) to the supervisor resolution."""
    from cambium.oneshot import _resolve_provider

    repo = _repo(tmp_path / "repo")
    config_path = _write_providers(tmp_path / "providers.json")
    config = oneshot.OneShotConfig(
        prompt="run one auto task",
        repo=repo,
        auto=True,
        provider_config_path=config_path,
    )
    resolved, environment = _resolve_provider(config, repo)

    assert resolved.model_candidates == ("model-a",)  # pb has no stored key
    assert resolved.fanout_config == {}  # resolution fills model + tier
    assert resolved.provider_env_keys == ("CAMBIUM_PROVIDER_PA_API_KEY",)
    assert environment == {"CAMBIUM_PROVIDER_PA_API_KEY": "secret-a"}
    # the plan the supervisor sees carries the candidates for resolution
    plan = oneshot.build_plan(resolved, repo, tmp_path / "session")
    spec = plan["tasks"][0]
    assert spec["model_candidates"] == ["model-a"]
    assert spec["fanout_config"] == {}


def test_auto_mode_rejects_pinned_provider_or_model(tmp_path: Path) -> None:
    from cambium.oneshot import _resolve_provider

    repo = _repo(tmp_path / "repo")
    config = oneshot.OneShotConfig(
        prompt="p",
        repo=repo,
        auto=True,
        provider="pa",
        provider_config_path=_write_providers(tmp_path / "providers.json"),
    )
    try:
        _resolve_provider(config, repo)
    except ValueError as exc:
        assert "cannot be combined" in str(exc)
    else:
        raise AssertionError("auto + provider must be rejected")


def test_auto_mode_requires_file_credential(tmp_path: Path) -> None:
    from cambium.oneshot import _resolve_provider

    repo = _repo(tmp_path / "repo")
    config_path = _write_providers(tmp_path / "providers.json", pa_key="", pb_key="")
    config = oneshot.OneShotConfig(
        prompt="p",
        repo=repo,
        auto=True,
        provider_config_path=config_path,
    )
    try:
        _resolve_provider(config, repo)
    except ValueError as exc:
        assert "stored credentials" in str(exc)
    else:
        raise AssertionError("auto with no file-backed credentials must fail closed")


def test_explicit_provider_requires_usable_credential_and_key_in_plan(
    tmp_path: Path,
) -> None:
    from cambium.oneshot import _resolve_provider

    repo = _repo(tmp_path / "repo")
    config_path = _write_providers(tmp_path / "providers.json")
    config = oneshot.OneShotConfig(
        prompt="p",
        repo=repo,
        provider="pb",
        provider_config_path=config_path,
    )

    with pytest.raises(ValueError, match="not authorized"):
        _resolve_provider(config, repo)

    _write_providers(config_path, pb_key="secret-b")
    resolved, environment = _resolve_provider(config, repo)
    assert "CAMBIUM_PROVIDER_PB_API_KEY" in resolved.provider_env_keys
    assert environment["CAMBIUM_PROVIDER_PB_API_KEY"] == "secret-b"


def test_pinned_provider_router_keeps_ready_siblings_for_terminal_fallback(
    monkeypatch, tmp_path: Path
) -> None:
    from cambium import worker
    from cambium.oneshot import _resolve_provider

    repo = _repo(tmp_path / "repo")
    config_path = _write_providers(tmp_path / "providers.json")
    _write_providers(config_path, pb_key="secret-b")
    config = oneshot.OneShotConfig(
        prompt="p",
        repo=repo,
        provider="pa",
        provider_config_path=config_path,
    )

    resolved, _environment = _resolve_provider(config, repo)
    monkeypatch.setattr(worker, "_provider_path", lambda: config_path)

    router, _tier, _model, _identity = worker._provider_router(
        dict(resolved.fanout_config or {}),
        assigned_provider=resolved.assigned_provider,
        authorized_providers=resolved.authorized_providers,
        authorized_providers_explicit=True,
        task_id="tui-turn",
    )

    assert tuple(provider.name for provider in router._providers) == ("pa", "pb")
    assert router._primary_provider == "pa"
    assert resolved.model_candidates == ("model-a", "model-b")


def test_resume_requires_existing_session_artifact(tmp_path: Path) -> None:
    config = oneshot.OneShotConfig(
        prompt="resume",
        repo=tmp_path,
        session_mode=oneshot.SessionMode.RESUME,
    )
    session_dir = tmp_path / "session"

    with pytest.raises(ValueError, match="requires an existing session"):
        oneshot.admit_session(config, session_dir)
    with pytest.raises(ValueError, match="requires an existing session"):
        oneshot.build_plan(config, tmp_path, session_dir)

    session_dir.mkdir()
    with pytest.raises(ValueError, match="requires an existing session"):
        oneshot.admit_session(config, session_dir)

    (session_dir / "plan.json").write_text("{}", encoding="utf-8")
    oneshot.admit_session(config, session_dir)
    assert oneshot.build_plan(config, tmp_path, session_dir)["tasks"][0]["session_mode"] == "resume"


def test_resume_without_explicit_session_does_not_allocate_one(tmp_path: Path) -> None:
    repo = _repo(tmp_path / "repo")
    config = oneshot.OneShotConfig(
        prompt="resume",
        repo=repo,
        target_file="file.txt",
        marker="// marker",
        session_mode=oneshot.SessionMode.RESUME,
    )

    with pytest.raises(ValueError, match="explicit existing session"):
        asyncio.run(oneshot.run_oneshot(config))
    sessions_root = repo / ".cambium" / "sessions"
    assert not sessions_root.exists()
