"""Shared git fixtures for group 13 scenario tests."""

from __future__ import annotations

import subprocess
from pathlib import Path

from cambium.fencing import write_generation


def init_repo(
    path: Path,
    *,
    user_name: str,
    user_email: str,
    filename: str,
    content: str,
) -> tuple[Path, str]:
    path.mkdir(parents=True)
    subprocess.run(["git", "init", "-b", "main", str(path)], check=True, capture_output=True)
    subprocess.run(["git", "-C", str(path), "config", "user.name", user_name], check=True)
    subprocess.run(["git", "-C", str(path), "config", "user.email", user_email], check=True)
    (path / filename).write_text(content, encoding="utf-8")
    subprocess.run(["git", "-C", str(path), "add", filename], check=True, capture_output=True)
    subprocess.run(
        ["git", "-C", str(path), "commit", "-m", "initial"],
        check=True,
        capture_output=True,
    )
    base = subprocess.run(
        ["git", "-C", str(path), "rev-parse", "HEAD"],
        check=True,
        capture_output=True,
        text=True,
    ).stdout.strip()
    return path, base


def init_worktree(
    repo: Path,
    *,
    user_name: str,
    user_email: str,
    filename: str,
    content: str,
    branch: str,
    worktree_name: str,
) -> Path:
    repo, _ = init_repo(
        repo,
        user_name=user_name,
        user_email=user_email,
        filename=filename,
        content=content,
    )
    worktree = repo.parent / worktree_name
    subprocess.run(
        ["git", "-C", str(repo), "worktree", "add", "-b", branch, str(worktree), "main"],
        check=True,
        capture_output=True,
    )
    write_generation(worktree, 1)
    return worktree
