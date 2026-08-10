from __future__ import annotations

import shlex
import subprocess
from pathlib import Path

import pytest

from cambium import worker


def test_worker_git_worktree_hook_does_not_receive_provider_key(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    repo = tmp_path / "repo"
    subprocess.run(["git", "init", "-b", "main", str(repo)], check=True, capture_output=True)
    subprocess.run(["git", "config", "user.email", "test@example.com"], cwd=repo, check=True)
    subprocess.run(["git", "config", "user.name", "Test"], cwd=repo, check=True)
    (repo / "tracked.txt").write_text("base\n", encoding="utf-8")
    subprocess.run(["git", "add", "tracked.txt"], cwd=repo, check=True)
    subprocess.run(["git", "commit", "-m", "base"], cwd=repo, check=True, capture_output=True)

    record = tmp_path / "hook-environment"
    hook = repo / ".git" / "hooks" / "post-checkout"
    hook.write_text(f"#!/bin/sh\nenv > {shlex.quote(str(record))}\n", encoding="utf-8")
    hook.chmod(0o700)
    provider_name = "CAMBIUM_PROVIDER_OPENAI_API_KEY"
    monkeypatch.setenv(provider_name, "provider-secret")

    worktree = tmp_path / "worktree"
    returncode, _stdout, stderr = worker.git(
        "worktree", "add", "-b", "worker-test", str(worktree), "main", cwd=repo
    )

    assert returncode == 0, stderr
    assert record.exists(), "the post-checkout hook never ran"
    hook_environment = record.read_text(encoding="utf-8").splitlines()
    assert not any(line.startswith(f"{provider_name}=") for line in hook_environment)
