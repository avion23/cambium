"""One-shot execution boundary: a direct prompt run through the supervisor.

Turns one user prompt plus a target repository into a supervised one-task
plan.  ``run_oneshot`` resolves the repository and session root, runs
:func:`preflight`, builds the plan with :func:`build_plan`, and delegates
execution to the existing ``cambium.supervisor.run_plan``.  Session state,
worker supervision, gating, and ref-only publication therefore follow the
supervisor contract unchanged; this module supplies only the mapping and the
edge checks around it.
"""

from __future__ import annotations

import subprocess
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from cambium.supervisor import (
    DEFAULT_WALL_BUDGET_S,
    EventSink,
    PlanResult,
    run_plan,
)

__all__ = [
    "EventSink",
    "OneShotConfig",
    "build_plan",
    "default_session_root",
    "preflight",
    "resolve_repo",
    "run_oneshot",
]


@dataclass(frozen=True, slots=True)
class OneShotConfig:
    """One direct prompt run against a repository.

    ``prompt``, ``repo``, and ``task_id`` are required.  ``session_root``
    defaults to :func:`default_session_root`; ``worktree_path`` defaults to
    ``session_root/wt`` and ``branch`` to ``wt-<task_id>``.  The remaining
    fields map directly onto the supervisor task spec.
    """

    prompt: str
    repo: str | Path
    task_id: str
    session_root: str | Path | None = None
    worktree_path: str | Path | None = None
    branch: str | None = None
    gate: str = "true"
    worker: str = "cambium.worker"
    provider_env_keys: tuple[str, ...] = ()
    fanout_config: Mapping[str, Any] | None = None
    base_commit: str | None = None
    max_wall_s: float = DEFAULT_WALL_BUDGET_S
    max_restarts: int = 0
    target_file: str | None = None
    marker: str | None = None


def resolve_repo(repo: str | Path) -> Path:
    """Resolve a repository argument to an absolute filesystem path."""
    return Path(repo).expanduser().resolve()


def default_session_root() -> Path:
    """Return the default session root for one-shot runs."""
    return Path.cwd() / "sessions"


def _git_stdout(repo: Path, *args: str) -> str | None:
    result = subprocess.run(
        ["git", "-C", str(repo), *args],
        capture_output=True,
        text=True,
    )
    if result.returncode != 0:
        return None
    return result.stdout.strip() or None


def preflight(config: OneShotConfig, repo: Path, session_root: Path) -> None:
    """Raise ``ValueError`` when the one-shot run cannot start.

    Checks the request (non-empty prompt and task id) and the repository
    (exists, is a git repository, and has ``refs/heads/main`` to publish
    onto).  The worktree must stay under the session root, matching the
    supervisor's plan validation.  Remaining plan validation is delegated to
    ``run_plan``.
    """
    if not isinstance(config.prompt, str) or not config.prompt.strip():
        raise ValueError("one-shot prompt must be a non-empty string")
    if not isinstance(config.task_id, str) or not config.task_id.strip():
        raise ValueError("one-shot task_id must be a non-empty string")
    if not repo.exists():
        raise ValueError(f"one-shot repository does not exist: {repo}")
    if not repo.is_dir():
        raise ValueError(f"one-shot repository is not a directory: {repo}")
    if not (repo / ".git").exists():
        raise ValueError(f"one-shot repository is not a git repository: {repo}")
    if _git_stdout(repo, "rev-parse", "--verify", "refs/heads/main") is None:
        raise ValueError(f"one-shot repository has no refs/heads/main: {repo}")
    worktree = (
        Path(config.worktree_path).expanduser().resolve()
        if config.worktree_path is not None
        else Path(session_root).resolve() / "wt"
    )
    if not worktree.is_relative_to(Path(session_root).resolve()):
        raise ValueError(
            f"one-shot worktree_path must stay under the session root: {worktree}"
        )


def build_plan(
    config: OneShotConfig, repo: Path, session_root: Path
) -> dict[str, Any]:
    """Map one one-shot config to the supervisor's one-task plan.

    Pure mapping: no filesystem access.  ``worktree_path`` defaults to
    ``session_root/wt`` and ``branch`` to ``wt-<task_id>``; the supervisor
    resolves and validates the resulting paths inside ``run_plan``.
    """
    worktree = (
        Path(config.worktree_path)
        if config.worktree_path is not None
        else Path(session_root) / "wt"
    )
    branch = config.branch if config.branch is not None else f"wt-{config.task_id}"
    spec: dict[str, Any] = {
        "task_id": config.task_id,
        "task": config.prompt,
        "repo": str(repo),
        "worktree_path": str(worktree),
        "branch": branch,
        "gate": config.gate,
        "worker": config.worker,
        "provider_env_keys": list(config.provider_env_keys),
        "max_wall_s": config.max_wall_s,
        "max_restarts": config.max_restarts,
    }
    if config.fanout_config is not None:
        spec["fanout_config"] = dict(config.fanout_config)
    if config.base_commit is not None:
        spec["base_commit"] = config.base_commit
    if config.target_file is not None:
        spec["target_file"] = config.target_file
    if config.marker is not None:
        spec["marker"] = config.marker
    return {"tasks": [spec]}


async def run_oneshot(
    config: OneShotConfig, on_event: EventSink | None = None
) -> PlanResult:
    """Resolve, preflight, and execute one direct prompt against a repository."""
    repo = resolve_repo(config.repo)
    session_root = (
        Path(config.session_root).expanduser().resolve()
        if config.session_root is not None
        else default_session_root()
    )
    preflight(config, repo, session_root)
    plan = build_plan(config, repo, session_root)
    return await run_plan(session_root, plan, on_event=on_event)
