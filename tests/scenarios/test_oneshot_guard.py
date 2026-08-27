"""Nested ephemeral one-shot admission guards."""

from __future__ import annotations

from pathlib import Path

import pytest

from cambium import oneshot


def _plan(repo: Path) -> dict:
    return oneshot.build_plan(
        oneshot.OneShotConfig(prompt="prompt", repo=repo),
        repo=repo,
        session_dir=repo.parent / "child-session",
    )


def test_nested_ephemeral_guard_rejects_parent_session_worktree(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    parent = tmp_path / "parent-session"
    monkeypatch.setenv("CAMBIUM_SESSION_ID", str(parent))

    with pytest.raises(
        ValueError,
        match=r"refusing nested-ephemeral run against a session worktree",
    ):
        _plan(parent / "repo")


def test_nested_ephemeral_guard_honors_opt_in(monkeypatch, tmp_path: Path) -> None:
    parent = tmp_path / "parent-session"
    monkeypatch.setenv("CAMBIUM_SESSION_ID", str(parent))
    monkeypatch.setenv("CAMBIUM_ALLOW_NESTED_EPHEMERAL", "1")

    assert _plan(parent / "repo")["tasks"][0]["repo"] == str((parent / "repo").resolve())


def test_nested_ephemeral_guard_is_silent_without_parent_marker(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    parent = tmp_path / "parent-session"
    monkeypatch.delenv("CAMBIUM_SESSION_ID", raising=False)
    monkeypatch.setenv("CAMBIUM_SESSION_DIR", str(parent))

    assert _plan(parent / "repo")["tasks"][0]["repo"] == str((parent / "repo").resolve())
