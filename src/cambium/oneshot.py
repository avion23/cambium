"""The one-prompt boundary for the user-facing Cambium CLI.

This module maps one prompt to the existing supervisor plan contract.  It does
not run a provider itself and it does not change the parent process
environment.  Provider credentials loaded from :class:`AuthStore` are passed
to the supervisor as an in-memory, per-run mapping; the supervisor forwards
only the selected environment name to the worker process.
"""

from __future__ import annotations

import os
import subprocess
import tempfile
from collections.abc import Mapping
from dataclasses import dataclass, replace
from hashlib import sha256
from pathlib import Path
from typing import Any

from . import supervisor
from .auth import (
    AuthError,
    AuthStore,
    effective_home,
    is_provider_env_name,
    scrub_environment,
)
from .ipc import MAX_LINE_BYTES
from .provider_config import ProviderSelectionError, load_providers, select_provider
from .session import session_root
from .supervisor import (
    DEFAULT_MAX_TOKENS,
    DEFAULT_WALL_BUDGET_S,
    EventSink,
    PlanResult,
    _reject_reused_session,
)

# User-facing agent-loop budget. The supervisor's own DEFAULT_MAX_TURNS (20)
# stays the fallback for plans that omit the field; build_plan always writes an
# explicit max_turns, so this is the effective default for CLI/REPL/TUI runs.
# 50 accommodates real multi-step work (read/diagnose/fix/verify plus a full
# test suite) while wall/token budgets and --max-turns still bound the loop.
DEFAULT_MAX_TURNS = 50

__all__ = [
    "EventSink",
    "OneShotConfig",
    "allocate_session_dir",
    "build_plan",
    "default_session_root",
    "preflight",
    "resolve_repo",
    "run_oneshot",
]


@dataclass(frozen=True, slots=True)
class OneShotConfig:
    """Configuration for one direct prompt run.

    ``prompt`` and ``repo`` have usable defaults so callers that build a
    context for the REPL or TUI do not need to invent an internal task id.
    ``session_root`` is the concrete session directory for an explicit run;
    when it is ``None``, :func:`run_oneshot` allocates a fresh leaf under the
    repository's default session root.

    ``provider_env_keys`` and ``fanout_config`` are internal, non-secret plan
    fields.  Normal callers set ``provider`` and optionally ``model``; the
    runner resolves those fields against the existing provider configuration.

    ``auto`` is the routing mode (solution C): the supervisor selects the
    (provider, model, tier) from ``model_candidates`` against the usage-debt
    ledger instead of pinning one provider here. ``model_candidates`` is
    internal (filled from the enabled configured providers when ``auto``).
    """

    prompt: str = ""
    repo: str | Path = "."
    session_root: str | Path | None = None
    provider: str | None = None
    model: str | None = None
    auto: bool = False
    model_candidates: tuple[str, ...] = ()
    routing_state_path: str | Path | None = None
    provider_config_path: str | Path | None = None
    task_id: str | None = None
    worktree_path: str | Path | None = None
    branch: str | None = None
    worker: str = "cambium.worker"
    provider_env_keys: tuple[str, ...] = ()
    fanout_config: Mapping[str, Any] | None = None
    base_commit: str | None = None
    max_wall_s: float = DEFAULT_WALL_BUDGET_S
    max_tokens: int = DEFAULT_MAX_TOKENS
    max_turns: int = DEFAULT_MAX_TURNS
    max_restarts: int = 0
    target_file: str | None = None
    marker: str | None = None

    @property
    def task(self) -> str:
        """Return the prompt under the supervisor's task terminology."""
        return self.prompt

    @property
    def session_dir(self) -> str | Path | None:
        """Return the explicit concrete session leaf, when supplied."""
        return self.session_root


def resolve_repo(repo: str | Path) -> Path:
    """Resolve a non-empty repository argument to an absolute path."""
    if not isinstance(repo, (str, Path)) or (isinstance(repo, str) and not repo.strip()):
        raise ValueError("one-shot repository must be a non-empty path")
    return Path(repo).expanduser().resolve()


def default_session_root(repo: str | Path | None = None) -> Path:
    """Return the repository-local root that contains user sessions."""
    target = resolve_repo("." if repo is None else repo)
    return session_root(target)


def _git_stdout(repo: Path, *args: str) -> str | None:
    result = subprocess.run(
        ["git", "-C", str(repo), *args],
        capture_output=True,
        text=True,
        env=scrub_environment(),
    )
    if result.returncode != 0:
        return None
    return result.stdout.strip() or None


def preflight(
    config: OneShotConfig,
    repo: Path | None = None,
    session_dir: Path | None = None,
) -> None:
    """Reject a prompt or repository that cannot reach the supervisor."""
    if not isinstance(config.prompt, str) or not config.prompt.strip():
        raise ValueError("one-shot prompt must be a non-empty string")
    encoded_len = len(config.prompt.encode("utf-8"))
    if encoded_len > MAX_LINE_BYTES:
        raise ValueError(
            f"one-shot prompt exceeds the supervisor frame limit "
            f"({encoded_len} > {MAX_LINE_BYTES} bytes)"
        )
    if config.task_id is not None and (
        not isinstance(config.task_id, str) or not config.task_id.strip()
    ):
        raise ValueError("one-shot task_id must be a non-empty string")

    target_repo = resolve_repo(config.repo) if repo is None else Path(repo).resolve()
    if not target_repo.exists():
        raise ValueError(f"one-shot repository does not exist: {target_repo}")
    if not target_repo.is_dir():
        raise ValueError(f"one-shot repository is not a directory: {target_repo}")
    if not (target_repo / ".git").exists():
        raise ValueError(f"one-shot repository is not a git repository: {target_repo}")
    if _git_stdout(target_repo, "rev-parse", "--verify", "refs/heads/main") is None:
        raise ValueError(f"one-shot repository has no refs/heads/main: {target_repo}")

    if session_dir is None:
        return
    session_path = Path(session_dir).expanduser().resolve()
    worktree = (
        Path(config.worktree_path).expanduser().resolve()
        if config.worktree_path is not None
        else session_path / "wt"
    )
    if not worktree.is_relative_to(session_path):
        raise ValueError(
            f"one-shot worktree_path must stay under the session directory: {worktree}"
        )


def _provider_config_path(config: OneShotConfig, repo: Path) -> Path:
    """Return a trusted provider path without consulting the target repository."""
    if config.provider_config_path is not None:
        path = Path(config.provider_config_path).expanduser()
    else:
        configured = os.environ.get("CAMBIUM_PROVIDERS")
        path = (
            Path(configured).expanduser()
            if configured
            else effective_home() / ".config" / "cambium" / "providers.json"
        )
    if not path.is_absolute():
        path = Path.cwd() / path
    return path.resolve()


def _stored_provider_environment(
    env_name: str, auth_store: AuthStore | None = None
) -> dict[str, str]:
    """Return one selected credential without changing ``os.environ``."""
    if not is_provider_env_name(env_name):
        raise ValueError("provider credential is not configured")
    value = os.environ.get(env_name)
    if value:
        return {env_name: value}
    store = auth_store if auth_store is not None else AuthStore()
    try:
        launch_environment = store.launch_environment(base={})
    except AuthError as exc:
        raise ValueError("provider credential is unavailable") from exc
    value = launch_environment.get(env_name)
    if not value:
        raise ValueError("provider credential is not configured")
    return {env_name: value}


def _is_codex_oauth_provider(provider: Any) -> bool:
    """True when a provider authenticates via the Codex ChatGPT OAuth profile.

    Such providers carry an empty ``api_key_env`` (the config slice pins the
    profile and forbids env keys) and their credential lives in the OAuth
    store, never in the worker env — so the oneshot resolver must not try to
    look up an API key for them.
    """
    from cambium.provider_config import AuthMode

    return getattr(provider, "auth", None) is AuthMode.CODEX_CHATGPT


def _oauth_doc_present(provider_name: str) -> bool:
    """Local-only check that the OAuth store holds a document for the provider."""
    from cambium.oauth import OAuthStore

    try:
        store = OAuthStore()
        document = store.read_document(provider_name)
    except Exception:
        return False
    return document is not None


def _resolve_provider(
    config: OneShotConfig,
    repo: Path,
    auth_store: AuthStore | None = None,
) -> tuple[OneShotConfig, dict[str, str]]:
    """Resolve one configured provider and prepare a non-global credential handoff.

    ``auth_store`` is injected for callers that own their credential store
    (tests, sandboxes); the production default is the real ``AuthStore``.
    """
    marker_mode = (
        config.provider is None
        and config.model is None
        and config.fanout_config is None
        and config.target_file is not None
        and config.marker is not None
    )
    if marker_mode:
        return config, {}

    if config.auto:
        if config.provider is not None or config.model is not None:
            raise ValueError("auto mode cannot be combined with --provider or --model")
        config_path = _provider_config_path(config, repo)
        try:
            providers = load_providers(config_path)
        except (OSError, ProviderSelectionError, ValueError) as exc:
            raise ValueError(f"provider selection failed: {exc}") from exc
        environment: dict[str, str] = {}
        stored: list[Any] = []
        for candidate in providers:
            if not candidate.enabled:
                continue
            if _is_codex_oauth_provider(candidate):
                # OAuth-authenticated: eligible when the local store holds a
                # document; the credential is never an env key.
                if _oauth_doc_present(candidate.name):
                    stored.append(candidate)
                continue
            try:
                environment.update(_stored_provider_environment(candidate.api_key_env, auth_store))
            except ValueError:
                continue
            stored.append(candidate)
        if not stored:
            raise ValueError(
                "auto mode requires at least one enabled provider with stored "
                "credentials; run `cambium auth set <provider> --stdin` first"
            )
        resolved = replace(
            config,
            provider=None,
            model=None,
            provider_config_path=config_path,
            # The fanout model/tier are left to the supervisor's routing
            # resolution (solution C) so the ledger balances the pick.
            fanout_config={},
            provider_env_keys=tuple(
                candidate.api_key_env for candidate in stored if candidate.api_key_env
            ),
            model_candidates=tuple(
                sorted({candidate.model for candidate in stored})
            ),
        )
        return resolved, environment

    if (
        config.fanout_config is not None
        and config.provider is None
        and not config.provider_env_keys
    ):
        raise ValueError("provider mode requires a selected provider")

    if config.provider is None and config.provider_env_keys:
        environment: dict[str, str] = {}
        for env_name in config.provider_env_keys:
            environment.update(_stored_provider_environment(env_name, auth_store))
        return config, environment

    config_path = _provider_config_path(config, repo)
    try:
        providers = load_providers(config_path)
        if config.provider is None and config.model is not None:
            matching = [
                candidate
                for candidate in providers
                if candidate.enabled and candidate.model == config.model
            ]
            selected = select_provider(matching)
        else:
            selected = select_provider(providers, name=config.provider)
    except (OSError, ProviderSelectionError, ValueError) as exc:
        raise ValueError(f"provider selection failed: {exc}") from exc

    effective_model = config.model if config.model is not None else selected.model
    if not isinstance(effective_model, str) or not effective_model:
        raise ValueError(f"provider {selected.name!r} has no configured model")
    if selected.model != effective_model:
        raise ValueError(
            f"model {effective_model!r} is not configured for provider {selected.name!r}"
        )

    fanout_config = dict(config.fanout_config or {})
    fanout_config["tier"] = selected.tier.value
    fanout_config["model"] = effective_model
    resolved = replace(
        config,
        provider=selected.name,
        model=effective_model,
        provider_config_path=config_path,
        provider_env_keys=() if _is_codex_oauth_provider(selected) else (selected.api_key_env,),
        fanout_config=fanout_config,
    )
    if _is_codex_oauth_provider(selected):
        # Credential comes from the OAuth store at spawn; preflight validates.
        return resolved, {}
    return resolved, _stored_provider_environment(selected.api_key_env, auth_store)


def allocate_session_dir(repo: str | Path) -> Path:
    """Allocate a fresh session leaf under the repository's session root."""
    root = default_session_root(repo)
    root.mkdir(parents=True, exist_ok=True, mode=0o700)
    try:
        os.chmod(root, 0o700)
    except OSError:
        pass
    return Path(tempfile.mkdtemp(prefix="run-", dir=root))


def _default_branch(session_dir: Path) -> str:
    """Return a stable, private branch name for one concrete session leaf."""
    suffix = sha256(str(session_dir).encode("utf-8")).hexdigest()[:16]
    return f"cambium-oneshot-{suffix}"


def build_plan(
    config: OneShotConfig,
    repo: Path | None = None,
    session_dir: Path | None = None,
) -> dict[str, Any]:
    """Map one resolved config to the supervisor's one-task plan."""
    target_repo = resolve_repo(config.repo) if repo is None else Path(repo).resolve()
    target_session = (
        Path(session_dir).expanduser().resolve()
        if session_dir is not None
        else (
            Path(config.session_root).expanduser().resolve()
            if config.session_root is not None
            else default_session_root(target_repo)
        )
    )
    task_id = config.task_id or "oneshot"
    worktree = (
        Path(config.worktree_path).expanduser().resolve()
        if config.worktree_path is not None
        else target_session / "wt"
    )
    branch = config.branch if config.branch is not None else _default_branch(target_session)
    spec: dict[str, Any] = {
        "task_id": task_id,
        "task": config.prompt,
        "repo": str(target_repo),
        "worktree_path": str(worktree),
        "branch": branch,
        "worker": config.worker,
        "provider_env_keys": list(config.provider_env_keys),
        "max_wall_s": config.max_wall_s,
        "max_tokens": int(config.max_tokens),
        "max_turns": int(config.max_turns),
        "max_restarts": config.max_restarts,
    }
    if config.model_candidates:
        spec["model_candidates"] = list(config.model_candidates)
    if config.fanout_config is not None:
        spec["fanout_config"] = dict(config.fanout_config)
    if config.provider_config_path is not None and config.fanout_config is not None:
        spec["provider_config_path"] = str(Path(config.provider_config_path).resolve())
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
    """Run one prompt through exactly one supervisor plan."""
    repo = resolve_repo(config.repo)
    preflight(config, repo)
    explicit_session_dir = (
        Path(config.session_root).expanduser().resolve()
        if config.session_root is not None
        else None
    )
    if explicit_session_dir is not None:
        preflight(config, repo, explicit_session_dir)
        _reject_reused_session(explicit_session_dir)

    resolved, provider_environment = _resolve_provider(config, repo)
    session_dir = (
        explicit_session_dir
        if explicit_session_dir is not None
        else allocate_session_dir(repo)
    )
    preflight(resolved, repo, session_dir)
    plan = build_plan(resolved, repo, session_dir)
    # The usage-debt ledger is repo-scoped by default so real runs keep
    # cross-session burn per repository without touching a user-global file;
    # tests against temporary repos are isolated automatically.
    routing_state_path = (
        resolved.routing_state_path
        if resolved.routing_state_path is not None
        else repo / ".cambium" / "routing-state.json"
    )
    if provider_environment:
        return await supervisor.run_plan(
            session_dir,
            plan,
            on_event=on_event,
            provider_environment=provider_environment,
            routing_state_path=routing_state_path,
            reject_reused_session=True,
        )
    return await supervisor.run_plan(
        session_dir,
        plan,
        on_event=on_event,
        routing_state_path=routing_state_path,
        reject_reused_session=True,
    )
