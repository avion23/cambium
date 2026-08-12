"""Worker runtime (Opifex seed) — ``python -m cambium.worker``.

Speaks the Nuntius JSON-Lines wire protocol over stdio
(docs/architecture.md §5, docs/research/ipc-protocol-draft.md). By default
one worker executes one task and then exits; when the init carries
``worker_reuse: true`` the worker stays alive after the task and waits for a
rebind init on stdin (eval-3 ADOPT warm pool):

    init                        ->  ready (echoes the init request_id and the
                                    generation fencing token)
    run_task                    ->  heartbeat(s) every ~1s while working
                                ->  result_envelope (echoes the run_task
                                    request_id) -> exit_message (connection
                                    level; carries NO request_id)
                                ->  with ``worker_reuse``: result_envelope
                                    then reuse_ready (keeps the process alive)
    init (rebind, reuse only)   ->  clears ALL per-task state (agent loop,
                                    transcript, tool state, LM clients), chdir
                                    to the new worktree, then ready; the
                                    single-init exit behavior is unchanged
                                    when ``worker_reuse`` is absent
    check_health                ->  ok (echoes the request_id, generation)
    steer                       ->  {"action": "cancel"} aborts the current
                                    task (status cancelled); anything else is
                                    logged and ignored (v2.1 hook)
    propose_child (outbound)    ->  after the task body, one message per
                                    entry in the run payload's
                                    ``proposed_children`` list, or during the
                                    agent loop, one message per successful
                                    ``delegate`` tool call; both shapes are
                                    {request_id, parent_task_id,
                                    child_task_id, kind, spec}. The
                                    supervisor validates the revision and
                                    replies with durable ``child_admitted`` /
                                    ``child_rejected`` events (no wire ack)
    cancel                      ->  ok (ack) then abort the current task with
                                    status "cancelled"
    shutdown                    ->  ok (ack), abort the current task, then
                                    exit_message (reason "shutdown") + exit 0

A reuse-enabled worker exits cleanly (code 0) when stdin closes while it is
idle between tasks.

Defensive timeouts (worker self-protection if the supervisor dies):
    - init deadline: no init message within ``INIT_TIMEOUT_S`` (default 30 s,
      env ``CAMBIUM_INIT_TIMEOUT_S``) -> ``fatal_error`` + exit 1.
    - idle deadline: no message from the supervisor within ``IDLE_TIMEOUT_S``
      (default 300 s, env ``CAMBIUM_IDLE_TIMEOUT_S``) after ``ready`` -> the
      worker aborts any current task and exits gracefully (``exit_message``
      reason "idle", exit 0). No ``result_envelope`` is emitted for the
      aborted task — the supervisor is presumed gone.

Task spec (the ``run_task`` body) is compatible with
``scripts/fake_worker.py``'s task spec:

    task_id         stable task id (echoed everywhere)
    scratch_repo    git repo the throwaway worktree is branched from
    worktree_path   where the throwaway worktree is created (must stay under
                    the scratch repo's parent — path safety)
    branch          name of the throwaway branch
    target_file     file inside the worktree to edit (deterministic fallback;
                    must not escape it)
    marker          line appended to the target file (deterministic fallback)
    write_marker    bool; false forces the task to fail
    work_delay_s    optional float; pause before the edit (test hook so
                    cancellation is observable)

When ``init.fanout_config`` is present, the worker runs the provider-backed
agent loop instead: it loads the provider file named by the worker's absolute
``CAMBIUM_PROVIDERS`` environment variable and iterates bounded
``Diffundo.call`` turns, each accepting exactly one JSON action:

    {"type": "plan", "steps": [<non-empty strings>]}
    {"type": "tool_call", "name": <schema name>, "arguments": {...}}
    {"type": "finish", "summary": <non-empty summary>}

The agent is instructed to emit a short ``plan`` action before any
``tool_call``; the plan is kept in the transcript. The transcript is
summarized (truncation plus a synthetic dropped-message marker, no LLM call)
when it exceeds a character budget, so it stays bounded across turns.

Tool calls execute inside the worktree (with shell/git permissions from
``init.permissions``), emit ``tool_event`` messages, and persist
``checkpoint`` state under ``$CAMBIUM_SESSION_ID/.cambium/checkpoints/``.
Every router call also emits one redacted ``usage_event`` (implementation
plan step 3): provider/model/turn, token fields, estimated cost, latency,
Retry-After, request-rate status, account-quota owner, stable prompt-prefix
bytes, provider-reported cache-hit, and failure reason; fields the provider
did not report are omitted, never an error. Every tool result, including lint
feedback from ``write_file``, is appended to the transcript so the agent sees
success or failure.
The worker owns at most one fenced commit of the resulting changes; a
successful provider loop that leaves no non-``.cambium`` changes owns none.
A true no-op succeeds only while the worktree HEAD still resolves to the
base commit and writes no final transcript checkpoint; an advanced HEAD is
reported as a failure so no unfenced commit is ever merged.

Malformed wire input is fatal: the worker emits ``fatal_error``, then
``exit_message`` (reason "fatal"), and exits nonzero (let-it-crash). The
process exit code is 0 when the worker delivered a terminal result envelope
(the task outcome lives in the envelope ``status``) or when the exit is a
graceful supervisor- or worker-initiated close (shutdown, idle).
"""

from __future__ import annotations

import asyncio
import copy
import json
import logging
import math
import os
import re
import secrets
import subprocess
import sys
import tempfile
import threading
import time
import zlib
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from cambium.auth import scrub_environment
from cambium.diffundo import (
    AllProvidersFailed,
    CallResult,
    CredentialSource,
    Diffundo,
    ProviderError,
    ProviderTier,
    prompt_prefix_bytes,
)
from cambium.fencing import is_cache_artifact_path, validate_worker_generation
from cambium.ipc import (
    MAX_LINE_BYTES,
    MessageTooLong,
    make_request_id,
    read_message,
    write_message,
)
from cambium.lint_diag import LintDiag
from cambium.provider_config import AuthMode, load_providers
from cambium.redact import Redactor, build_session_redactor
from cambium.schemas import TOOL_SCHEMAS
from cambium.tools import ToolContext, ToolResult, run_tool

PROTO = 1
HEARTBEAT_INTERVAL_S = 1.0
INIT_TIMEOUT_S = 30.0
IDLE_TIMEOUT_S = 300.0
MAX_SUMMARY_CHARS = 2_000
# Consecutive non-progress actions (valid plans AND invalid/unparseable
# actions) before the agent loop fails fast.
MAX_CONSECUTIVE_PLANS = 2
MAX_DIFF_BYTES = 64 * 1024  # 64 KiB diff cap (ipc-protocol-draft.md §3)
EXIT_CODES = {"succeeded": 0, "failed": 1, "cancelled": 4}
DEFAULT_MAX_TURNS = 50
DEFAULT_MAX_TOKENS = 200_000
DEFAULT_MAX_WALL_S = 3600.0
CHECKPOINT_SCHEMA = 1
MAX_ACTION_CONTENT_BYTES = 16 * 1024
MAX_OBSERVATION_BYTES = 64 * 1024
MAX_CMD_BYTES = 512
MAX_TRANSCRIPT_CHARS = 120_000
TRANSCRIPT_KEEP_TURNS = 6
INSPECTION_GIT_OPS = frozenset({"status", "diff", "log"})
_USAGE_COUNT_FIELDS = frozenset(
    {
        "prompt_tokens",
        "completion_tokens",
        "total_tokens",
        "input_tokens",
        "output_tokens",
        "cached_tokens",
    }
)
_DIFFUNDO_OPTIONS = frozenset(
    {
        "call_budget_s",
        "pause_timeout_s",
        "breaker_window_size",
        "breaker_failure_threshold",
        "open_backoff_base",
         "retry_base_delay_s",
     }
 )

logger = logging.getLogger(__name__)


class GenerationFenceError(RuntimeError):
    """The worker no longer owns the persisted worktree generation."""


def _monotonic_ms() -> int:
    return time.monotonic_ns() // 1_000_000


def _env_float(name: str, default: float) -> float:
    value = os.environ.get(name)
    if value is None:
        return default
    try:
        return float(value)
    except ValueError:
        return default


async def send(writer: asyncio.StreamWriter, msg: dict[str, Any]) -> None:
    write_message(writer, msg)
    await writer.drain()


def git(*args: str, cwd: str | Path | None = None) -> tuple[int, str, str]:
    proc = subprocess.run(
        ["git", *args], cwd=cwd, capture_output=True, text=True, env=scrub_environment()
    )
    return proc.returncode, proc.stdout.strip(), proc.stderr.strip()


def _fenced_git(
    worktree: Path,
    generation: int,
    *args: str,
    cwd: str | Path | None = None,
) -> tuple[int, str, str]:
    """Run mutating git while continuously enforcing the generation fence."""
    _require_generation(worktree, generation)
    proc = subprocess.Popen(
        ["git", *args],
        cwd=cwd,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        env=scrub_environment(),
        start_new_session=True,
    )
    while proc.poll() is None:
        if validate_worker_generation(worktree, generation):
            time.sleep(0.001)
            continue
        try:
            os.killpg(proc.pid, 9)
        except ProcessLookupError:
            pass
        stdout, stderr = proc.communicate()
        raise GenerationFenceError(
            f"generation mismatch for {worktree}: worker={generation}, "
            "persisted generation is different or missing"
        )
    stdout, stderr = proc.communicate()
    _require_generation(worktree, generation)
    return proc.returncode, stdout.strip(), stderr.strip()


def _require_generation(worktree: Path, generation: int) -> None:
    if not validate_worker_generation(worktree, generation):
        raise GenerationFenceError(
            f"generation mismatch for {worktree}: worker={generation}, "
            "persisted generation is different or missing"
        )


def _write_worktree_state(
    worktree: Path, generation: int, path: Path, content: str
) -> None:
    """Write worker state only while this process owns the current fence."""
    _require_generation(worktree, generation)
    path.write_text(content)


def cap_diff(diff: str) -> tuple[str, bool]:
    """Cap ``diff`` to ``MAX_DIFF_BYTES`` UTF-8 bytes, never splitting a
    codepoint; returns ``(diff, truncated)``."""
    raw = diff.encode("utf-8")
    if len(raw) <= MAX_DIFF_BYTES:
        return diff, False
    truncated = raw[:MAX_DIFF_BYTES].decode("utf-8", errors="ignore")
    return truncated + "\n... [diff truncated]", True


def _provider_fanout_config(run: dict[str, Any]) -> dict[str, Any] | None:
    config = run.get("fanout_config")
    if not isinstance(config, dict) or not config:
        return None
    return config


def _provider_env_keys(value: Any) -> tuple[str, ...]:
    if not isinstance(value, (list, tuple)):
        return ()
    return tuple(key for key in value if isinstance(key, str))


def _checkpoint_redactor(
    provider_env_keys: tuple[str, ...], credentials: Any = None
) -> Redactor:
    """Build the worker's immutable redactor from its authorized credentials."""
    secret_values = [
        value
        for key in provider_env_keys
        if isinstance(value := os.environ.get(key), str) and value
    ]
    if isinstance(credentials, dict):
        secret_values.extend(
            value
            for key, value in credentials.items()
            if key in provider_env_keys and isinstance(value, str) and value
        )
    return build_session_redactor(secret_values)


def _provider_path() -> Path:
    configured = os.environ.get("CAMBIUM_PROVIDERS")
    if not configured:
        raise RuntimeError("provider configuration is not set in CAMBIUM_PROVIDERS")
    path = Path(configured).expanduser()
    if not path.is_absolute():
        path = Path.cwd() / path
    return path.resolve()


def _fanout_section(config: dict[str, Any]) -> dict[str, Any]:
    for key in ("diffundo", "router"):
        section = config.get(key)
        if isinstance(section, dict):
            return section
    return config


def _fanout_value(config: dict[str, Any], section: dict[str, Any], key: str) -> Any:
    value = config.get(key)
    if value is not None:
        return value
    return section.get(key)


def _model_identity(
    providers: list[Any],
    tier: ProviderTier,
    model: str,
    *,
    assigned_provider: str | None,
) -> str:
    """Resolve the truthful model identity for the agent system prompt.

    ``assigned_provider`` (supervisor admission balancing) is authoritative;
    without one, the provider name is included only when exactly one configured
    provider serves the resolved tier+model, so the identity never invents a
    provider. The model name always comes from the resolved fanout config.
    """
    provider = assigned_provider
    if provider is None:
        serving = [
            candidate.name
            for candidate in providers
            if candidate.enabled and candidate.tier is tier and candidate.model == model
        ]
        if len(serving) == 1:
            provider = serving[0]
    if provider:
        return f"{provider}/{model}"
    return model


def _oauth_env_suffix(provider: str) -> str:
    """Normalize a provider id into its ``CAMBIUM_OAUTH_*`` env suffix.

    Mirrors ``supervisor._oauth_env_suffix`` so the supervisor's injected
    env vars and the worker's lookup agree byte-for-byte.
    """
    return re.sub(r"[._-]+", "_", provider.upper())


def _provider_router(
    config: dict[str, Any], *, assigned_provider: str | None = None
) -> tuple[Diffundo, ProviderTier, str, str]:
    providers = load_providers(_provider_path())
    section = _fanout_section(config)
    tier_value = _fanout_value(config, section, "tier")
    model = _fanout_value(config, section, "model")
    if not isinstance(tier_value, str) or not tier_value:
        raise ValueError("fanout_config requires a provider tier")
    if not isinstance(model, str) or not model:
        raise ValueError("fanout_config requires a provider model")
    try:
        tier = ProviderTier(tier_value)
    except ValueError as exc:
        raise ValueError(f"unsupported provider tier {tier_value!r}") from exc

    options: dict[str, Any] = {}
    for key in _DIFFUNDO_OPTIONS:
        value = _fanout_value(config, section, key)
        if value is not None:
            options[key] = value
    # Seed the per-subagent sticky primary from the task id: separate worker
    # processes (separate Diffundo instances, one task each) pick different
    # primary providers, spreading requests across providers at task
    # granularity while each task keeps its context on one provider (per-
    # provider prompt-prefix caching preserved).
    task_id = config.get("task_id") if isinstance(config, dict) else None
    if isinstance(task_id, str) and task_id:
        options.setdefault("rotation_seed", zlib.crc32(task_id.encode("utf-8")))
    if assigned_provider is not None:
        if any(provider.name == assigned_provider for provider in providers):
            # Supervisor-level admission balancing (solution C): preset the
            # per-subagent sticky primary to the task's assigned provider.
            options["primary_provider"] = assigned_provider
        else:
            logger.warning(
                "assigned_provider %r is not in the loaded providers; "
                "falling back to the seeded primary pick",
                assigned_provider,
            )
    codex_providers = [
        provider for provider in providers
        if getattr(provider, "auth", None) is AuthMode.CODEX_CHATGPT
    ]
    if codex_providers:
        # The supervisor injected CAMBIUM_OAUTH_ACCESS_/ACCOUNT_<SUFFIX> into
        # the worker env at spawn (access token ONLY — the refresh token never
        # leaves the supervisor). Diffundo carries one CredentialSource, so
        # more than one codex provider is unsupported until a per-provider
        # mapping exists: fail closed rather than silently share a credential.
        if len(codex_providers) > 1:
            raise ValueError(
                "multiple codex_chatgpt providers require per-provider "
                "credential sources (unsupported)"
            )
        codex = codex_providers[0]
        suffix = _oauth_env_suffix(codex.name)
        access = os.environ.get(f"CAMBIUM_OAUTH_ACCESS_{suffix}")
        account = os.environ.get(f"CAMBIUM_OAUTH_ACCOUNT_{suffix}")
        if not access:
            raise ValueError(
                f"codex provider {codex.name!r}: CAMBIUM_OAUTH_ACCESS_{suffix} "
                "is not set in the worker environment"
            )
        options["credential_source"] = CredentialSource(
            access_token=access, account_id=account or None
        )
    return Diffundo(providers, **options), tier, model, _model_identity(
        providers, tier, model, assigned_provider=assigned_provider
    )


def _positive_int(value: Any, name: str, default: int) -> int:
    if value is None:
        return default
    if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
        raise ValueError(f"{name} must be a positive integer")
    return value


def _positive_float(value: Any, name: str, default: float) -> float:
    if value is None:
        return default
    if isinstance(value, bool) or not isinstance(value, (int, float)) or value <= 0:
        raise ValueError(f"{name} must be a positive number")
    return float(value)


@dataclass(frozen=True, slots=True)
class AgentConfig:
    """Immutable per-task agent configuration parsed from init (init is authoritative)."""

    task_id: str
    generation: int
    task: str
    worktree: Path | None
    base_commit: str | None
    fanout_config: dict[str, Any] | None
    max_turns: int
    max_tokens: int
    shell_permission: bool
    network_permission: bool
    heartbeat_interval_s: float
    max_wall_s: float
    checkpoint_root: Path | None
    max_transcript_chars: int = MAX_TRANSCRIPT_CHARS
    # Supervisor-level admission balancing (solution C): the provider this
    # task was assigned at admission; presets Diffundo's sticky primary.
    assigned_provider: str | None = None
    provider_env_keys: tuple[str, ...] = ()
    redactor: Redactor | None = None

    @classmethod
    def from_init(cls, init: dict[str, Any]) -> AgentConfig:
        """Parse and validate the init message; raises ``ValueError`` on bad input."""
        permissions = init.get("permissions")
        if not isinstance(permissions, dict):
            permissions = {}
        shell_permission = permissions.get("shell", False)
        network_permission = permissions.get("network", False)
        if not isinstance(shell_permission, bool) or not isinstance(network_permission, bool):
            raise ValueError("init permissions.shell/network must be strict booleans")
        heartbeat = init.get("heartbeat")
        heartbeat_interval_s = (
            heartbeat.get("interval_s") if isinstance(heartbeat, dict) else None
        )
        budget = init.get("budget")
        max_wall_s = budget.get("max_wall_s") if isinstance(budget, dict) else None
        worktree = init.get("worktree")
        base_commit = init.get("base_commit")
        task = init.get("spec")
        assigned_provider = init.get("assigned_provider")
        if assigned_provider is not None and not isinstance(assigned_provider, str):
            raise ValueError("init assigned_provider must be a string")
        provider_env_keys = _provider_env_keys(init.get("provider_env_keys"))
        session_id = os.environ.get("CAMBIUM_SESSION_ID")
        checkpoint_root = (
            Path(session_id).resolve() / ".cambium" / "checkpoints" if session_id else None
        )
        return cls(
            task_id=str(init.get("task_id", "unknown")),
            generation=_positive_int(init.get("generation"), "init generation", 1),
            task=task if isinstance(task, str) else "",
            worktree=Path(worktree) if isinstance(worktree, str) else None,
            base_commit=base_commit if isinstance(base_commit, str) else None,
            fanout_config=_provider_fanout_config(init),
            assigned_provider=assigned_provider,
            max_turns=_positive_int(init.get("max_turns"), "init max_turns", DEFAULT_MAX_TURNS),
            max_tokens=_positive_int(
                init.get("max_tokens"), "init max_tokens", DEFAULT_MAX_TOKENS
            ),
            shell_permission=shell_permission,
            network_permission=network_permission,
            heartbeat_interval_s=_positive_float(
                heartbeat_interval_s, "init heartbeat.interval_s", HEARTBEAT_INTERVAL_S
            ),
            max_wall_s=_positive_float(
                max_wall_s, "init budget.max_wall_s", DEFAULT_MAX_WALL_S
            ),
            checkpoint_root=checkpoint_root,
            max_transcript_chars=_positive_int(
                init.get("max_transcript_chars"),
                "init max_transcript_chars",
                MAX_TRANSCRIPT_CHARS,
            ),
            provider_env_keys=provider_env_keys,
            redactor=_checkpoint_redactor(provider_env_keys, init.get("credentials")),
        )


def _merge_task_config(
    config: AgentConfig, init: dict[str, Any], run: dict[str, Any]
) -> AgentConfig:
    """Fill execution fields from run_task only when init omitted them (init authoritative)."""
    max_turns = config.max_turns
    if "max_turns" not in init:
        max_turns = _positive_int(run.get("max_turns"), "run_task max_turns", DEFAULT_MAX_TURNS)
    max_tokens = config.max_tokens
    if "max_tokens" not in init:
        max_tokens = _positive_int(
            run.get("max_tokens"), "run_task max_tokens", DEFAULT_MAX_TOKENS
        )
    max_wall_s = config.max_wall_s
    init_budget = init.get("budget")
    init_provided_wall = isinstance(init_budget, dict) and "max_wall_s" in init_budget
    if not init_provided_wall:
        max_wall_s = _positive_float(
            run.get("max_wall_s"), "run_task max_wall_s", DEFAULT_MAX_WALL_S
        )
    worktree = config.worktree
    if worktree is None and isinstance(run.get("worktree_path"), str):
        worktree = Path(run["worktree_path"])
    base_commit = config.base_commit or run.get("base_commit")
    task = config.task if config.task.strip() else str(run.get("task", ""))
    fanout_config = config.fanout_config or _provider_fanout_config(run)
    return AgentConfig(
        task_id=config.task_id,
        generation=config.generation,
        task=task,
        worktree=worktree,
        base_commit=base_commit if isinstance(base_commit, str) else None,
        fanout_config=fanout_config,
        assigned_provider=config.assigned_provider,
        max_turns=max_turns,
        max_tokens=max_tokens,
        shell_permission=config.shell_permission,
        network_permission=config.network_permission,
        heartbeat_interval_s=config.heartbeat_interval_s,
        max_wall_s=max_wall_s,
        checkpoint_root=config.checkpoint_root,
        max_transcript_chars=config.max_transcript_chars,
        provider_env_keys=config.provider_env_keys,
        redactor=config.redactor,
    )


def _config_from_run(run: dict[str, Any]) -> AgentConfig:
    """Fallback config when do_work is invoked directly (no init message)."""
    provider_env_keys = _provider_env_keys(run.get("provider_env_keys"))
    return AgentConfig(
        task_id=str(run.get("task_id", "unknown")),
        generation=_positive_int(run.get("generation"), "run_task generation", 1),
        task=str(run.get("task", "")),
        worktree=(
            Path(run["worktree_path"]) if isinstance(run.get("worktree_path"), str) else None
        ),
        base_commit=run.get("base_commit"),
        fanout_config=_provider_fanout_config(run),
        assigned_provider=None,
        max_turns=_positive_int(run.get("max_turns"), "run_task max_turns", DEFAULT_MAX_TURNS),
        max_tokens=_positive_int(run.get("max_tokens"), "run_task max_tokens", DEFAULT_MAX_TOKENS),
        shell_permission=False,
        network_permission=False,
        heartbeat_interval_s=HEARTBEAT_INTERVAL_S,
        max_wall_s=_positive_float(
            run.get("max_wall_s"), "run_task max_wall_s", DEFAULT_MAX_WALL_S
        ),
        checkpoint_root=None,
        provider_env_keys=provider_env_keys,
        redactor=_checkpoint_redactor(provider_env_keys, run.get("credentials")),
    )


class AgentProgress:
    """Current turn/tool/status shared with the heartbeat loop."""

    __slots__ = ("turn", "tool", "status")

    def __init__(self) -> None:
        self.turn = 0
        self.tool: str | None = None
        self.status = "working"


def _cap_utf8(text: str, limit: int) -> str:
    raw = text.encode("utf-8")
    if len(raw) <= limit:
        return text
    return raw[:limit].decode("utf-8", errors="ignore")


def _bounded_text(text: str, limit: int) -> str:
    raw = text.encode("utf-8")
    if len(raw) <= limit:
        return text
    return raw[:limit].decode("utf-8", errors="ignore") + "\n... [truncated]"


def _safe_task_id(task_id: str) -> str:
    safe = re.sub(r"[^A-Za-z0-9_.-]", "_", task_id)
    return safe or "task"


def _atomic_json_write(path: Path, content: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    descriptor, temporary_name = tempfile.mkstemp(
        prefix=f".{path.name}.", suffix=".tmp", dir=path.parent
    )
    temporary_path = Path(temporary_name)
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8", newline="\n") as handle:
            handle.write(content)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary_path, path)
        temporary_path = None
    finally:
        if temporary_path is not None:
            try:
                temporary_path.unlink()
            except FileNotFoundError:
                pass


def _valid_usage_count(value: Any) -> bool:
    """Return whether a provider token count is finite and non-negative."""
    if isinstance(value, bool):
        return False
    if isinstance(value, int):
        return value >= 0
    return isinstance(value, float) and math.isfinite(value) and value >= 0


def _invalid_usage_fields(usage: dict[str, Any] | None) -> tuple[str, ...]:
    """Return recognized provider usage fields with unsafe numeric values.

    Unknown provider usage fields remain omitted as before; the worker only
    accepts the scalar token-count fields in ``_USAGE_COUNT_FIELDS``.
    """
    if not isinstance(usage, dict):
        return ()
    return tuple(sorted(
        key for key, value in usage.items()
        if key in _USAGE_COUNT_FIELDS and not _valid_usage_count(value)
    ))


def _usage_counts(usage: dict[str, Any] | None) -> dict[str, int | float]:
    if not isinstance(usage, dict):
        return {}
    return {
        key: value
        for key, value in usage.items()
        if key in _USAGE_COUNT_FIELDS
        and _valid_usage_count(value)
    }


_ALL_TOOL_NAMES = frozenset(schema["name"] for schema in TOOL_SCHEMAS)


def _exposed_tool_schemas(config: AgentConfig) -> list[dict[str, Any]]:
    """Schemas offered to the model; shell and mutating git are permission-filtered."""
    schemas: list[dict[str, Any]] = []
    for schema in TOOL_SCHEMAS:
        name = schema["name"]
        if name == "run_shell":
            if config.shell_permission:
                schemas.append(schema)
            continue
        if name == "git_op":
            restricted = copy.deepcopy(schema)
            op_property = restricted["parameters"]["properties"]["op"]
            op_property["enum"] = [
                value for value in op_property.get("enum", ()) if value in INSPECTION_GIT_OPS
            ]
            schemas.append(restricted)
            continue
        schemas.append(schema)
    return schemas


def _permission_denied(name: str, args: dict[str, Any], config: AgentConfig) -> str | None:
    if name == "run_shell" and not config.shell_permission:
        return "run_shell is not permitted by this worker's permissions"
    if name == "git_op":
        op = args.get("op")
        if op not in INSPECTION_GIT_OPS:
            return f"git_op is restricted to inspection operations {sorted(INSPECTION_GIT_OPS)!r}"
    return None


def _action_keys(parsed: dict[str, Any], required: frozenset[str]) -> bool:
    """Exactly ``required`` keys plus at most an optional ``thought`` field."""
    allowed = required | {"thought"}
    return required <= parsed.keys() and parsed.keys() <= allowed


def _parse_agent_action(content: str) -> dict[str, Any]:
    """Strictly parse ONE agent action; a response may carry several
    concatenated JSON actions, in which case the first complete action is
    used (trailing actions are ignored and surfaced to the loop).  Raises
    ``ValueError`` on any deviation.

    Accepted shapes (each may optionally carry a ``thought`` field for
    reasoning; the action fields themselves must be exact):
        {"type": "plan", "steps": [<non-empty strings>]}
        {"type": "tool_call", "name": <schema name>, "arguments": {...}}
        {"type": "finish", "summary": <non-empty str>}
    """
    text = content.strip()
    if not text:
        raise ValueError("empty agent action")
    try:
        parsed, _end = json.JSONDecoder().raw_decode(text)
    except (json.JSONDecodeError, UnicodeDecodeError, RecursionError) as exc:
        raise ValueError(f"action is not valid JSON: {exc}") from None
    if not isinstance(parsed, dict):
        raise ValueError("agent action must be exactly one JSON object")
    action_type = parsed.get("type")
    if action_type == "plan":
        if not _action_keys(parsed, frozenset({"type", "steps"})):
            raise ValueError("plan must carry exactly type/steps (plus optional thought)")
        steps = parsed.get("steps")
        if not isinstance(steps, list) or not steps or not all(
            isinstance(step, str) and step.strip() for step in steps
        ):
            raise ValueError("plan steps must be a non-empty array of non-empty strings")
        return {"type": "plan", "steps": list(steps)}
    if action_type == "tool_call":
        if not _action_keys(parsed, frozenset({"type", "name", "arguments"})):
            raise ValueError(
                "tool_call must carry exactly type/name/arguments (plus optional thought)")
        name = parsed.get("name")
        arguments = parsed.get("arguments")
        if not isinstance(name, str) or not name:
            raise ValueError("tool_call name must be a non-empty string")
        if not isinstance(arguments, dict):
            raise ValueError("tool_call arguments must be an object")
        if name not in _ALL_TOOL_NAMES:
            raise ValueError(f"unknown tool: {name!r}")
        return {"type": "tool_call", "name": name, "arguments": arguments}
    if action_type == "finish":
        if not _action_keys(parsed, frozenset({"type", "summary"})):
            raise ValueError("finish must carry exactly type/summary (plus optional thought)")
        summary = parsed.get("summary")
        if not isinstance(summary, str) or not summary.strip():
            raise ValueError("finish summary must be a non-empty string")
        return {"type": "finish", "summary": summary}
    raise ValueError(f"unknown agent action type: {action_type!r}")


_TRAILING_ACTION_NOTE = (
    "only the first action was executed; trailing JSON was ignored — "
    "emit exactly one JSON action per turn"
)


def _action_trailing(content: str) -> str:
    """Return the non-whitespace content AFTER the first complete JSON object,
    or "" when there is none or the content cannot be parsed at all."""
    text = content.strip()
    if not text:
        return ""
    try:
        _obj, end = json.JSONDecoder().raw_decode(text)
    except (json.JSONDecodeError, UnicodeDecodeError, RecursionError):
        return ""
    return text[end:].strip()


def _usage_total(usage: dict[str, Any] | None) -> int | None:
    """Return one completion's usable token total, or ``None`` (fail closed).

    A recognized invalid count rejects the whole completion instead of being
    silently omitted.  This is the strict ingestion choice: the caller fails
    the attempt before emitting an event or updating cumulative usage.
    """
    if not isinstance(usage, dict):
        return None
    if _invalid_usage_fields(usage):
        return None
    total = usage.get("total_tokens")
    if _valid_usage_count(total):
        return int(total)
    inputs = usage.get("input_tokens", usage.get("prompt_tokens"))
    outputs = usage.get("output_tokens", usage.get("completion_tokens"))
    if (
        _valid_usage_count(inputs)
        and _valid_usage_count(outputs)
    ):
        return int(inputs) + int(outputs)
    return None


def _accumulate_usage(cumulative: dict[str, int], usage: dict[str, Any] | None) -> dict[str, int]:
    """Add one validated completion without letting a stale total poison it.

    ``total_tokens`` in the cumulative snapshot is a normalized running total:
    each call contributes its explicit total when present, otherwise its
    input+output sum.  It is not a sum of only the calls that reported an
    explicit ``total_tokens`` field.
    """
    if _invalid_usage_fields(usage):
        return cumulative
    for key, value in _usage_counts(usage).items():
        if key == "total_tokens":
            continue
        cumulative[key] = cumulative.get(key, 0) + int(value)
    total = _usage_total(usage)
    if total is not None:
        previous_total = cumulative.get("total_tokens", 0)
        if not _valid_usage_count(previous_total):
            previous_total = 0
        cumulative["total_tokens"] = int(previous_total) + total
    return cumulative


def _cumulative_total(cumulative: dict[str, int]) -> int:
    total = cumulative.get("total_tokens")
    if _valid_usage_count(total):
        return int(total)
    inputs = cumulative.get("input_tokens", cumulative.get("prompt_tokens"))
    outputs = cumulative.get("output_tokens", cumulative.get("completion_tokens"))
    if _valid_usage_count(inputs) and _valid_usage_count(outputs):
        return int(inputs) + int(outputs)
    return 0


def _transcript_chars(transcript: list[dict[str, Any]]) -> int:
    total = 0
    for message in transcript:
        content = message.get("content")
        if isinstance(content, str):
            total += len(content)
    return total


def _plan_message(transcript: list[dict[str, Any]]) -> dict[str, Any] | None:
    """Return the transcript entry carrying a valid ``plan`` action, or ``None``."""
    for message in transcript:
        if message.get("role") != "assistant":
            continue
        content = message.get("content")
        if not isinstance(content, str):
            continue
        try:
            parsed = json.loads(content)
        except (json.JSONDecodeError, UnicodeDecodeError, RecursionError):
            continue
        if isinstance(parsed, dict) and parsed.get("type") == "plan":
            return message
    return None


def _fit_transcript_to_budget(
    messages: list[dict[str, Any]], budget: int
) -> list[dict[str, Any]]:
    """Trim retained user observations so ``messages`` fits ``budget`` chars.

    The plan (if any) and the synthetic marker keep their full content; only
    the retained tail observations are proportionally truncated (plain cuts,
    no extra marker) to reach an exact fit.
    """
    truncatable = [
        message for message in messages[2:] if message.get("role") == "user"
    ]
    if not truncatable:
        return messages
    fixed = _transcript_chars(messages) - _transcript_chars(truncatable)
    target = max(0, budget - fixed)
    current = _transcript_chars(truncatable)
    if current <= target:
        return messages
    scale = target / current
    for message in truncatable:
        content = message.get("content")
        if not isinstance(content, str):
            continue
        message["content"] = _cap_utf8(content, int(len(content) * scale))
    return messages


def _summarize_transcript(
    transcript: list[dict[str, Any]],
    budget: int,
    keep_turns: int = TRANSCRIPT_KEEP_TURNS,
) -> list[dict[str, Any]]:
    """Bound the transcript to ``budget`` characters without calling the LLM.

    Returns the transcript unchanged when it fits within the budget. When it
    does not, keeps the plan message (if any), drops everything older than the
    most recent ``keep_turns`` turns, inserts one synthetic "prior context
    summary" marker, and proportionally truncates the retained observations so
    the result stays within the budget.
    """
    if _transcript_chars(transcript) <= budget:
        return list(transcript)
    tail = transcript[-(keep_turns * 2):] if keep_turns > 0 else []
    plan = _plan_message(transcript)
    tail = [dict(message) for message in tail if message is not plan]
    dropped = len(transcript) - len(tail) - (1 if plan is not None else 0)
    marker = {
        "role": "user",
        "content": (
            f"[prior context: {dropped} earlier message(s) dropped to bound the "
            "transcript; only the plan and the most recent turns are retained]"
        ),
    }
    kept: list[dict[str, Any]] = []
    if plan is not None:
        kept.append(dict(plan))
    kept.append(marker)
    kept.extend(tail)
    return _fit_transcript_to_budget(kept, budget)


def _build_agent_prompt(
    task: str,
    tools: list[dict[str, Any]],
    transcript: list[dict[str, Any]],
    model_identity: str = "",
) -> dict[str, Any]:
    system_lines = [
        "You are Cambium's autonomous coding agent.",
        "You act inside a disposable git worktree and must complete the task.",
    ]
    if model_identity:
        system_lines.append(
            f"You are running as the configured model {model_identity}. When "
            "asked what model or provider you are, answer truthfully from this "
            "identity and never guess."
        )
    system_lines.extend([
        "Return exactly one JSON object per turn; your reply must be one action:",
        '  plan:      {"type": "plan", "steps": ["...", "..."]}',
        '  tool_call: {"type": "tool_call", "name": <tool name>, "arguments": {...}}',
        '  finish:    {"type": "finish", "summary": <non-empty summary>}',
        'An optional "thought" field may be added to the same object to record your '
        "reasoning; the action fields above must remain exact.",
        "Your FIRST action must be a short plan: list the concrete steps before any "
        "tool_call.",
        "Approach:",
        "- Reading uses only the batch read tool (read_batch); individual file "
        "reads are unavailable, so read all needed files in one batch call.",
        "- Read the relevant files before editing; verify each change before moving on.",
        "- If a tool call fails, diagnose the error and retry with a corrected call.",
        "- When the task changes code, run the relevant tests via run_shell; only emit "
        "finish after the change is verified and the tests pass. If tests fail, iterate.",
        "- Emit finish only when the task is complete and verified.",
        "Examples:",
        '  {"type": "plan", "steps": ["read src/a.py and src/b.py", "edit src/a.py", '
        '"run tests"]}',
        '  {"type": "tool_call", "name": "read_batch", '
        '"arguments": {"paths": ["src/a.py", "src/b.py"]}}',
        '  {"type": "finish", "summary": "implemented and verified the change"}',
        "Available tools:",
        json.dumps(tools, sort_keys=True),
        f"Task: {task}",
    ])
    messages = [{"role": "system", "content": "\n".join(system_lines)}]
    messages.extend(transcript)
    if not messages or messages[-1].get("role") != "user":
        # Some providers (e.g. ZAI/GLM) reject payloads whose last message is
        # not a user message: a fresh transcript has no non-system message
        # (1214 on turn one), and a plan action leaves the transcript ending
        # with an assistant message (1214 on the next turn). A neutral user
        # message keeps every payload valid without changing the static
        # system prefix (plan step 3 caching).
        messages.append({
            "role": "user",
            "content": "Begin." if len(messages) == 1 else "Continue.",
        })
    return {"messages": messages}


def _tool_observation(name: str, result: ToolResult) -> str:
    body = result.output if result.ok else (result.error or result.output or "")
    return _bounded_text(f"tool {name} ok={result.ok}\n{body}", MAX_OBSERVATION_BYTES)


def _safe_cmd(name: str, args: dict[str, Any]) -> str:
    if name == "run_shell":
        cmd = args.get("cmd")
        if isinstance(cmd, list):
            return _cap_utf8(" ".join(str(token) for token in cmd), MAX_CMD_BYTES)
    return _cap_utf8(f"{name} {json.dumps(args, sort_keys=True)}", MAX_CMD_BYTES)


async def _emit_tool_event(
    writer: asyncio.StreamWriter,
    config: AgentConfig,
    name: str,
    args: dict[str, Any],
    turn: int,
    tool_result: ToolResult,
) -> None:
    await send(writer, {
        "type": "tool_event",
        "task_id": config.task_id,
        "generation": config.generation,
        "tool": name,
        "cmd": _safe_cmd(name, args),
        "turn": turn,
        "ok": bool(tool_result.ok),
        "duration_ms": int(tool_result.duration_ms),
    })


def _success_usage_event(result: CallResult, turn: int) -> dict[str, Any]:
    """One redacted durable usage event for a completed router call.

    Fields the provider did not report are omitted; a missing field never
    breaks the event or the session (implementation plan step 3).
    """
    event: dict[str, Any] = {
        "turn": turn,
        "provider": result.provider,
        "model": result.model,
        "estimated_cost_usd": max(0.0, float(result.estimated_cost_usd)),
        "latency_s": max(0.0, float(result.latency_s)),
    }
    usage = _usage_counts(result.usage)
    if usage:
        event["usage"] = usage
    if result.retry_after_s is not None:
        event["retry_after_s"] = max(0.0, float(result.retry_after_s))
    if result.request_rate_status is not None:
        event["request_rate_status"] = result.request_rate_status
    if result.account_quota_owner is not None:
        event["account_quota_owner"] = result.account_quota_owner
    if result.prompt_prefix_bytes is not None:
        event["prompt_prefix_bytes"] = result.prompt_prefix_bytes
    if result.provider_cache_hit is not None:
        event["provider_cache_hit"] = result.provider_cache_hit
    return event


def _failure_usage_event(
    exc: BaseException,
    *,
    turn: int,
    model: str | None,
    router: Diffundo,
    prompt: dict[str, Any],
) -> dict[str, Any]:
    """One redacted durable usage event for a failed router call.

    Carries the terminal failure's provider evidence and the redacted failure
    reason; fields that are unavailable are omitted, never an error.
    """
    event: dict[str, Any] = {"turn": turn}
    if isinstance(model, str) and model:
        event["model"] = model
    failure_reason = exc.__class__.__name__
    provider: str | None = None
    retry_after_s: float | None = None
    request_rate_status: str | None = None
    account_quota_owner: str | None = None
    if isinstance(exc, AllProvidersFailed) and isinstance(exc.last_error, ProviderError):
        error = exc.last_error
        provider = error.provider
        retry_after_s = error.retry_after_s
        request_rate_status = error.request_rate_status
        account_quota_owner = error.account_quota_owner
        failure_reason = f"{error.outcome.value}: {error.message}"
    if provider is not None:
        event["provider"] = provider
        if request_rate_status is None:
            try:
                request_rate_status = router.status(provider).value
            except Exception:
                request_rate_status = None
    if retry_after_s is not None:
        event["retry_after_s"] = max(0.0, float(retry_after_s))
    if request_rate_status is not None:
        event["request_rate_status"] = request_rate_status
    if account_quota_owner is not None:
        event["account_quota_owner"] = account_quota_owner
    prefix_bytes = prompt_prefix_bytes(prompt)
    if prefix_bytes is not None:
        event["prompt_prefix_bytes"] = prefix_bytes
    event["failure_reason"] = _cap_utf8(failure_reason, 512)
    return event


async def _emit_usage_event(
    writer: asyncio.StreamWriter,
    config: AgentConfig,
    event: dict[str, Any],
) -> None:
    await send(writer, {
        "type": "usage_event",
        "task_id": config.task_id,
        "generation": config.generation,
        **event,
    })


def _write_checkpoint_file(
    config: AgentConfig,
    turn: int,
    transcript: list[dict[str, Any]],
    usage: dict[str, int],
    commits_so_far: list[str],
) -> Path | None:
    if config.checkpoint_root is None:
        return None
    directory = config.checkpoint_root / _safe_task_id(config.task_id)
    path = directory / f"turn-{turn:03d}.json"
    payload = {
        "schema": CHECKPOINT_SCHEMA,
        "task": config.task,
        "generation": config.generation,
        "turn": turn,
        "transcript": transcript,
        "usage": usage,
        "commits_so_far": commits_so_far,
    }
    redactor = config.redactor or _checkpoint_redactor(config.provider_env_keys)
    payload = redactor.redact_mapping(payload)
    _atomic_json_write(path, json.dumps(payload, sort_keys=True, indent=2))
    return path


async def _emit_checkpoint(
    writer: asyncio.StreamWriter,
    config: AgentConfig,
    turn: int,
    state_ref: Path,
    commits_so_far: list[str],
) -> None:
    await send(writer, {
        "type": "checkpoint",
        "task_id": config.task_id,
        "generation": config.generation,
        "turn": turn,
        "state_ref": str(state_ref),
        "commits_so_far": commits_so_far,
    })


async def _persist_checkpoint(
    writer: asyncio.StreamWriter,
    config: AgentConfig,
    turn: int,
    transcript: list[dict[str, Any]],
    usage: dict[str, int],
    commits_so_far: list[str],
) -> None:
    path = await asyncio.to_thread(
        _write_checkpoint_file, config, turn, transcript, usage, commits_so_far
    )
    if path is not None:
        await _emit_checkpoint(writer, config, turn, path, commits_so_far)


def _loop_failure_outcome(loop_outcome: dict[str, Any]) -> dict[str, Any]:
    return {
        "status": loop_outcome.get("status", "failed"),
        "failure_reason": loop_outcome.get("failure_reason"),
        "commits": [],
        "files_changed": [],
        "diff": "",
        "diff_truncated": False,
        "summary": loop_outcome.get("summary", "")[:MAX_SUMMARY_CHARS],
    }


def _cumulative_provider_metadata(loop_outcome: dict[str, Any]) -> dict[str, Any] | None:
    provider = loop_outcome.get("provider")
    model = loop_outcome.get("model")
    usage = loop_outcome.get("usage")
    if not isinstance(provider, str) or not isinstance(model, str) or not isinstance(usage, dict):
        return None
    return {
        "provider": provider,
        "model": model,
        "usage": usage,
        "latency_s": max(0.0, float(loop_outcome.get("latency_s", 0.0))),
    }


def _do_work_marker(run: dict[str, Any], stop: threading.Event) -> dict[str, Any]:
    """Execute one task: throwaway worktree, one-file edit, commit.

    Returns the outcome dict:

        status          "succeeded" | "failed" | "cancelled"
        failure_reason  str | None (set when status != "succeeded")
        commits         list[str] of SHAs produced
        files_changed   list[str] of paths changed
        diff            ``git diff <base_commit>..HEAD`` in the worktree,
                        capped at ``MAX_DIFF_BYTES`` UTF-8 bytes
        diff_truncated  bool; true when the diff was capped
        summary         worker-authored, <= ``MAX_SUMMARY_CHARS``

    Cooperative cancellation via ``stop``: the worker checks it between git
    steps and reports status "cancelled" if it was set.
    """
    outcome: dict[str, Any] = {
        "status": "failed",
        "failure_reason": None,
        "commits": [],
        "files_changed": [],
        "diff": "",
        "diff_truncated": False,
        "summary": "",
    }
    try:
        scratch = Path(run["scratch_repo"]).resolve()
        worktree = Path(run["worktree_path"]).resolve()
        generation = run.get("generation")
        if (
            isinstance(generation, bool)
            or not isinstance(generation, int)
            or generation <= 0
        ):
            outcome["failure_reason"] = "invalid worker generation"
            return outcome
        write_marker = bool(run.get("write_marker", True))
        provider_metadata: dict[str, Any] | None = None

        target_file = run.get("target_file")
        marker = run.get("marker")
        if (
            not isinstance(target_file, str)
            or not target_file
            or not isinstance(marker, str)
            or not marker
        ):
            outcome["failure_reason"] = "marker task requires target_file and marker"
            return outcome

        session_root = scratch.parent
        if not worktree.is_relative_to(session_root):
            outcome["failure_reason"] = (
                f"worktree_path {worktree} outside session scratch root {session_root}")
            return outcome
        target = (worktree / target_file).resolve()
        if not target.is_relative_to(worktree):
            outcome["failure_reason"] = f"target_file {target_file!r} escapes the worktree"
            return outcome

        def guarded_git(*args: str, cwd: str | Path | None = None) -> tuple[int, str, str]:
            _require_generation(worktree, generation)
            if args and args[0] in {"add", "commit"}:
                return _fenced_git(worktree, generation, *args, cwd=cwd)
            return git(*args, cwd=cwd)

        _require_generation(worktree, generation)
        rc, _out, err = guarded_git("rev-parse", "main", cwd=scratch)
        if rc != 0:
            outcome["failure_reason"] = f"no main branch in scratch repo: {err}"
            return outcome
        base_commit = _out

        if not worktree.exists():
            outcome["failure_reason"] = f"worker worktree is missing: {worktree}"
            return outcome
        rc, _out, err = guarded_git("rev-parse", "HEAD", cwd=worktree)
        if rc != 0:
            outcome["failure_reason"] = f"cannot resolve worktree HEAD: {err}"
            return outcome
        worker_identity = secrets.token_hex(16)

        # Optional work_delay_s pauses before the edit (testing hook); the
        # pause polls ``stop`` so cancellation stays responsive.
        delay = float(run.get("work_delay_s", 0.0) or 0.0)
        deadline = time.monotonic() + delay
        while time.monotonic() < deadline:
            if stop.is_set():
                outcome["status"] = "cancelled"
                return outcome
            time.sleep(min(0.05, deadline - time.monotonic()))

        if stop.is_set():
            outcome["status"] = "cancelled"
            return outcome
        _require_generation(worktree, generation)
        if not write_marker:
            outcome["failure_reason"] = "marker not written (write_marker=false)"
            return outcome
        if not target.exists():
            outcome["failure_reason"] = f"target file missing: {target_file}"
            return outcome
        _require_generation(worktree, generation)
        _write_worktree_state(
            worktree,
            generation,
            target,
            target.read_text().rstrip("\n") + "\n" + marker + "\n",
        )
        _require_generation(worktree, generation)
        if marker not in target.read_text():
            outcome["failure_reason"] = "edit missing: marker not present after write"
            return outcome
        if stop.is_set():
            outcome["status"] = "cancelled"
            return outcome

        guarded_git("add", target_file, cwd=worktree)
        rc, _out, err = guarded_git(
            "commit",
            "-m",
            f"cambium-ipc: {run['task_id']}",
            "-m",
            f"Cambium-Worker-Generation: {generation}\n"
            f"Cambium-Worker-Identity: {worker_identity}",
            cwd=worktree,
        )
        if rc != 0:
            outcome["failure_reason"] = f"commit failed: {err}"
            return outcome
        _rc, sha, _err = guarded_git("rev-parse", "HEAD", cwd=worktree)
        _rc, diff, _err = guarded_git("diff", f"{base_commit}..HEAD", cwd=worktree)
        diff, diff_truncated = cap_diff(diff)
        _require_generation(worktree, generation)
        outcome.update(
            status="succeeded",
            failure_reason=None,
            commits=[sha],
            files_changed=[target_file],
            diff=diff,
            diff_truncated=diff_truncated,
            summary=f"appended marker to {target_file}"[:MAX_SUMMARY_CHARS],
            provider_metadata=provider_metadata,
        )
        return outcome
    except GenerationFenceError as exc:
        outcome["failure_reason"] = str(exc)
        return outcome
    except Exception as exc:  # let-it-crash: report as a failure, not a hang
        outcome["failure_reason"] = f"task crashed: {exc}"
        return outcome


async def do_work(
    run: dict[str, Any],
    stop: threading.Event,
    *,
    config: AgentConfig | None = None,
    writer: asyncio.StreamWriter | None = None,
    progress: AgentProgress | None = None,
) -> dict[str, Any]:
    """Execute one task and return the outcome dict (result-envelope shape).

    With no ``fanout_config`` this is the deterministic marker path
    (``_do_work_marker``); provider-backed tasks run the bounded agent loop
    and then ``_finalize_worktree``.
    """
    if _provider_fanout_config(run) is None:
        return await asyncio.to_thread(_do_work_marker, run, stop)
    if config is None:
        config = _config_from_run(run)
    if progress is None:
        progress = AgentProgress()
    return await _do_provider_work(run, config, stop, writer, progress)


def _fanout_budget_usd(config: dict[str, Any] | None) -> float | None:
    if not isinstance(config, dict):
        return None
    value = config.get("budget_usd")
    if isinstance(value, bool) or not isinstance(value, (int, float)) or value < 0:
        return None
    return float(value)


def _loop_result(
    outcome: dict[str, Any],
    status: str,
    failure_reason: str | None,
    turn: int,
    cumulative_usage: dict[str, int],
    transcript: list[dict[str, Any]],
) -> dict[str, Any]:
    return {
        **outcome,
        "status": status,
        "failure_reason": failure_reason,
        "turn": max(0, int(turn)),
        "usage": dict(cumulative_usage),
        "transcript": transcript,
    }


async def _run_agent_loop(
    *,
    config: AgentConfig,
    router: Diffundo,
    tier: ProviderTier,
    model: str,
    model_identity: str = "",
    worktree: Path,
    writer: asyncio.StreamWriter | None,
    stop: threading.Event,
    progress: AgentProgress,
) -> dict[str, Any]:
    """Bounded provider-backed tool loop: one router call per turn, strict
    action parsing, permission checks, tool dispatch, tool_event + checkpoint.

    Returns a loop outcome dict: status / failure_reason / summary / turn /
    cumulative usage / provider / latency_s / bounded transcript.
    """
    outcome: dict[str, Any] = {
        "status": "failed",
        "failure_reason": None,
        "summary": "",
        "turn": 0,
        "usage": {},
        "provider": None,
        "model": model,
        "latency_s": 0.0,
        "transcript": [],
        "commits_so_far": [],
    }
    wall_deadline = time.monotonic() + config.max_wall_s
    cumulative_usage: dict[str, int] = {}
    transcript: list[dict[str, Any]] = []
    tools = _exposed_tool_schemas(config)
    lint_diag = LintDiag()
    budget_usd = _fanout_budget_usd(config.fanout_config)
    no_progress_actions = 0
    verified_after_change = False
    verification_failed = False
    code_changed = False
    try:
        for turn in range(1, config.max_turns + 1):
            progress.turn = turn
            progress.status = "working"
            if stop.is_set():
                return _loop_result(
                    outcome, "cancelled", None, turn - 1, cumulative_usage, transcript
                )
            if time.monotonic() >= wall_deadline:
                return _loop_result(
                    outcome, "failed", "wall budget exceeded", turn - 1,
                    cumulative_usage, transcript,
                )
            _require_generation(worktree, config.generation)
            transcript = _summarize_transcript(transcript, config.max_transcript_chars)
            prompt = _build_agent_prompt(config.task, tools, transcript, model_identity)
            try:
                result = await router.call(tier, prompt, model=model, budget_usd=budget_usd)
            except Exception as exc:
                # Provider errors may contain response text or request details.
                # Only the exception class is safe to carry into the envelope.
                # The usage event carries the redacted failure evidence; the
                # envelope failure reason stays class-name only.
                if writer is not None:
                    await _emit_usage_event(
                        writer,
                        config,
                        _failure_usage_event(
                            exc, turn=turn, model=model, router=router, prompt=prompt
                        ),
                    )
                return _loop_result(
                    outcome, "failed", f"provider call failed: {exc.__class__.__name__}",
                    turn - 1, cumulative_usage, transcript,
                )
            if time.monotonic() >= wall_deadline:
                return _loop_result(
                    outcome, "failed", "wall budget exceeded",
                    turn, cumulative_usage, transcript,
                )
            if result.model != model:
                # An untrusted response model never enters durable artifacts;
                # no usage event is emitted for it.
                return _loop_result(
                    outcome, "failed", "provider response model mismatch",
                    turn - 1, cumulative_usage, transcript,
                )
            invalid_usage_fields = _invalid_usage_fields(result.usage)
            if invalid_usage_fields:
                # Strict choice: reject the attempt rather than omit a bad
                # provider count and continue with a misleading budget/debt.
                return _loop_result(
                    outcome, "failed", "provider usage contains invalid token counts",
                    turn - 1, cumulative_usage, transcript,
                )
            total = _usage_total(result.usage)
            if total is None:
                return _loop_result(
                    outcome, "failed", "provider usage missing usable token counts",
                    turn - 1, cumulative_usage, transcript,
                )
            if writer is not None:
                await _emit_usage_event(writer, config, _success_usage_event(result, turn))
            cumulative_usage = _accumulate_usage(cumulative_usage, result.usage)
            if _cumulative_total(cumulative_usage) > config.max_tokens:
                return _loop_result(
                    outcome, "failed", "token budget exceeded",
                    turn, cumulative_usage, transcript,
                )
            action_content = _bounded_text(result.content, MAX_ACTION_CONTENT_BYTES)
            try:
                action = _parse_agent_action(result.content)
            except ValueError as exc:
                transcript.append({"role": "assistant", "content": action_content})
                transcript.append({"role": "user", "content": f"invalid action: {exc}"})
                no_progress_actions += 1
                if no_progress_actions > MAX_CONSECUTIVE_PLANS:
                    return _loop_result(
                        outcome, "failed",
                        f"agent made no progress: {no_progress_actions} consecutive "
                        "actions without a tool call",
                        turn, cumulative_usage, transcript,
                    )
                continue
            trailing = _action_trailing(result.content)
            if action["type"] == "plan":
                no_progress_actions += 1
                if no_progress_actions > MAX_CONSECUTIVE_PLANS:
                    return _loop_result(
                        outcome, "failed",
                        f"agent made no progress: {no_progress_actions} consecutive "
                        "actions without a tool call",
                        turn, cumulative_usage, transcript,
                    )
                transcript.append({"role": "assistant", "content": action_content})
                if trailing:
                    transcript.append({"role": "user", "content": _TRAILING_ACTION_NOTE})
                progress.tool = "plan"
                continue
            no_progress_actions = 0
            if action["type"] == "finish":
                transcript.append({"role": "assistant", "content": action_content})
                if code_changed and verification_failed and not verified_after_change:
                    transcript.append({
                        "role": "user",
                        "content": (
                            "finish rejected: you changed code but did not run a "
                            "successful verification command; run the tests (e.g. "
                            "run_shell) before finishing"
                        ),
                    })
                    progress.tool = "finish"
                    continue
                return {
                    **outcome,
                    "status": "succeeded",
                    "summary": action["summary"],
                    "turn": turn,
                    "usage": cumulative_usage,
                    "provider": result.provider,
                    "latency_s": max(0.0, float(result.latency_s)),
                    "transcript": transcript,
                }
            name, arguments = action["name"], action["arguments"]
            denial = _permission_denied(name, arguments, config)
            if denial is not None:
                transcript.append({"role": "assistant", "content": action_content})
                transcript.append({"role": "user", "content": f"action rejected: {denial}"})
                progress.tool = name
                continue
            if stop.is_set():
                return _loop_result(
                    outcome, "cancelled", None, turn - 1, cumulative_usage, transcript
                )
            progress.tool = name
            with ToolContext(worktree, lint=lint_diag) as ctx:
                tool_result = await run_tool(name, arguments, ctx)
            if name == "delegate" and tool_result.ok and writer is not None:
                # Model-triggered child proposal: emit now; the supervisor
                # validates and admits at this task's terminal envelope
                # (implementation-plan step 2's dynamic admission).
                await _emit_delegated_child(writer, config, arguments)
            if tool_result.ok:
                if name in ("write_file", "edit_file"):
                    code_changed = True
                    verified_after_change = False
                    verification_failed = False
                elif name == "run_shell":
                    verified_after_change = True
                    verification_failed = False
            elif name == "run_shell":
                verification_failed = True
                verified_after_change = False
            transcript.append({"role": "assistant", "content": action_content})
            if trailing:
                transcript.append({"role": "user", "content": _TRAILING_ACTION_NOTE})
            transcript.append({"role": "user", "content": _tool_observation(name, tool_result)})
            if writer is not None:
                await _emit_tool_event(writer, config, name, arguments, turn, tool_result)
                await _persist_checkpoint(writer, config, turn, transcript, cumulative_usage, [])
        return _loop_result(
            outcome, "failed", f"max turns exceeded ({config.max_turns})",
            config.max_turns, cumulative_usage, transcript,
        )
    except GenerationFenceError as exc:
        return _loop_result(
            outcome, "failed", str(exc), progress.turn, cumulative_usage, transcript
        )


async def _do_provider_work(
    run: dict[str, Any],
    config: AgentConfig,
    stop: threading.Event,
    writer: asyncio.StreamWriter | None,
    progress: AgentProgress,
) -> dict[str, Any]:
    worktree = Path(run["worktree_path"]).resolve()
    session_root = Path(run["scratch_repo"]).resolve().parent
    if not worktree.is_relative_to(session_root):
        return _loop_failure_outcome({
            "status": "failed",
            "failure_reason": (
                f"worktree_path {worktree} outside session scratch root {session_root}"),
        })
    if not worktree.exists():
        return _loop_failure_outcome({
            "status": "failed",
            "failure_reason": f"worker worktree is missing: {worktree}",
        })
    try:
        router, tier, model, model_identity = _provider_router(
            config.fanout_config, assigned_provider=config.assigned_provider
        )
    except Exception as exc:
        return _loop_failure_outcome({
            "status": "failed",
            "failure_reason": f"provider routing failed: {exc.__class__.__name__}",
        })
    worker_identity = secrets.token_hex(16)
    loop_outcome = await _run_agent_loop(
        config=config,
        router=router,
        tier=tier,
        model=model,
        model_identity=model_identity,
        worktree=worktree,
        writer=writer,
        stop=stop,
        progress=progress,
    )
    if loop_outcome["status"] != "succeeded":
        return _loop_failure_outcome(loop_outcome)
    outcome = await asyncio.to_thread(
        _finalize_worktree,
        run=run,
        config=config,
        worktree=worktree,
        generation=config.generation,
        worker_identity=worker_identity,
        stop=stop,
        loop_outcome=loop_outcome,
    )
    final_checkpoint = outcome.pop("_checkpoint_path", None)
    if writer is not None and final_checkpoint is not None:
        await _emit_checkpoint(
            writer, config, loop_outcome.get("turn", 0),
            Path(final_checkpoint), outcome.get("commits", []),
        )
    return outcome


def _finalize_worktree(
    *,
    run: dict[str, Any],
    config: AgentConfig,
    worktree: Path,
    generation: int,
    worker_identity: str,
    stop: threading.Event,
    loop_outcome: dict[str, Any],
) -> dict[str, Any]:
    """Stage the agent's changed files (excluding ``.cambium/``) and make at
    most ONE fenced worker-owned commit with generation + identity trailers.
    A clean worktree succeeds as a no-op only while HEAD still resolves to
    the base commit; such a true no-op receives no empty commit and writes
    no final checkpoint. Returns the result-envelope shape: model summary +
    cumulative safe provider metadata.

    The commit message, envelope, state paths, and provider metadata are all
    worker-authored; no model-controlled value reaches any of them.
    """
    outcome: dict[str, Any] = {
        "status": "failed",
        "failure_reason": None,
        "commits": [],
        "files_changed": [],
        "diff": "",
        "diff_truncated": False,
        "summary": loop_outcome.get("summary", "")[:MAX_SUMMARY_CHARS],
    }
    provider_metadata = _cumulative_provider_metadata(loop_outcome)
    if provider_metadata is not None:
        outcome["provider_metadata"] = provider_metadata
    try:
        if stop.is_set():
            outcome["status"] = "cancelled"
            return outcome
        _require_generation(worktree, generation)
        scratch = Path(run["scratch_repo"]).resolve()
        base_commit = config.base_commit
        if not base_commit:
            rc, base, err = git("rev-parse", "main", cwd=scratch)
            if rc != 0:
                outcome["failure_reason"] = f"no main branch in scratch repo: {err}"
                return outcome
            base_commit = base
        rc, head_sha, err = git("rev-parse", "HEAD", cwd=worktree)
        if rc != 0:
            outcome["failure_reason"] = f"cannot resolve worktree HEAD: {err}"
            return outcome
        _require_generation(worktree, generation)
        status_proc = subprocess.run(
            [
                "git",
                "status",
                "--porcelain=v1",
                "--untracked-files=all",
                "--ignored=matching",
            ],
            cwd=worktree,
            capture_output=True,
            text=True,
            env=scrub_environment(),
        )
        if status_proc.returncode != 0:
            outcome["failure_reason"] = (
                f"git status failed: {status_proc.stderr.strip()}"
            )
            return outcome
        changed: list[str] = []
        ignored: list[str] = []
        for line in status_proc.stdout.splitlines():
            path = line[3:].strip() if len(line) > 3 else line.strip()
            if " -> " in path:
                path = path.split(" -> ", 1)[1]
            if not path or path == ".cambium" or path.startswith(".cambium/"):
                continue
            if (
                is_cache_artifact_path(path)
            ):
                continue
            if line[:2] == "!!":
                ignored.append(path)
                continue
            changed.append(path)
        # A provider can commit directly (e.g. via permitted shell): HEAD then
        # no longer matches the base commit, whether or not the worktree is
        # dirty. Never publish such unfenced commits — the fenced-commit path
        # would stack a fenced commit on top of them and the supervisor would
        # merge both. Resolve the base once and require worktree HEAD to equal
        # it on every publish path.
        rc, resolved_base, err = git(
            "rev-parse", "--verify", f"{base_commit}^{{commit}}", cwd=worktree
        )
        if rc != 0:
            outcome["failure_reason"] = (
                f"cannot resolve base_commit {base_commit}: {err}"
            )
            return outcome
        if head_sha != resolved_base:
            outcome["failure_reason"] = (
                f"worktree HEAD {head_sha} advanced beyond base_commit "
                f"{resolved_base}; refusing to publish unverified changes"
            )
            return outcome
        if ignored:
            outcome["failure_reason"] = (
                f"worktree contains ignored changes {ignored!r}; refusing to publish "
                "unverified changes"
            )
            return outcome
        if not changed:
            _require_generation(worktree, generation)
            # True no-op: no final checkpoint is written. The summary stays in
            # the envelope.
            outcome.update(
                status="succeeded",
                failure_reason=None,
                commits=[],
                files_changed=[],
                diff="",
                diff_truncated=False,
                summary=(
                    loop_outcome.get("summary") or f"completed {config.task_id}"
                )[:MAX_SUMMARY_CHARS],
            )
            return outcome
        for path in changed:
            _require_generation(worktree, generation)
            rc, _out, err = _fenced_git(worktree, generation, "add", "--", path, cwd=worktree)
            if rc != 0:
                outcome["failure_reason"] = f"git add failed for {path}: {err}"
                return outcome
        _require_generation(worktree, generation)
        rc, _out, err = _fenced_git(
            worktree,
            generation,
            "commit",
            "-m",
            f"cambium-agent: {config.task_id}",
            "-m",
            f"Cambium-Worker-Generation: {generation}\n"
            f"Cambium-Worker-Identity: {worker_identity}",
            cwd=worktree,
        )
        if rc != 0:
            outcome["failure_reason"] = f"commit failed: {err}"
            return outcome
        _rc, sha, _err = git("rev-parse", "HEAD", cwd=worktree)
        _rc, diff, _err = git("diff", f"{resolved_base}..HEAD", cwd=worktree)
        diff, diff_truncated = cap_diff(diff)
        _require_generation(worktree, generation)
        final_checkpoint = _write_checkpoint_file(
            config,
            loop_outcome.get("turn", 0),
            loop_outcome.get("transcript", []),
            loop_outcome.get("usage", {}),
            [sha],
        )
        outcome.update(
            status="succeeded",
            failure_reason=None,
            commits=[sha],
            files_changed=changed,
            diff=diff,
            diff_truncated=diff_truncated,
            summary=(
                loop_outcome.get("summary") or f"completed {config.task_id}"
            )[:MAX_SUMMARY_CHARS],
        )
        if final_checkpoint is not None:
            outcome["_checkpoint_path"] = str(final_checkpoint)
        return outcome
    except GenerationFenceError as exc:
        outcome["failure_reason"] = str(exc)
        return outcome
    except Exception as exc:  # let-it-crash: report as a failure, not a hang
        outcome["failure_reason"] = f"task crashed: {exc}"
        return outcome


async def _heartbeat_loop(
    writer: asyncio.StreamWriter,
    task_id: str,
    generation: int,
    stop: threading.Event,
    progress: AgentProgress | None = None,
    interval_s: float = HEARTBEAT_INTERVAL_S,
) -> None:
    while not stop.is_set():
        turn = progress.turn if progress is not None else 0
        tool = progress.tool if progress is not None else None
        status = progress.status if progress is not None else "working"
        await send(writer, {
            "type": "heartbeat",
            "task_id": task_id,
            "generation": generation,
            "turn": turn,
            "tool": tool,
            "status": status,
            "monotonic_ms": _monotonic_ms(),
        })
        if stop.is_set():
            # Observed the stop flag right after this send: exit at the safe
            # point (between iterations) instead of starting another send.
            break
        # Sleep in short slices so stop is observed promptly even when the
        # configured interval is large; the send cadence stays ~interval_s.
        remaining = interval_s
        while remaining > 0 and not stop.is_set():
            step = min(0.05, remaining)
            await asyncio.sleep(step)
            remaining -= step


async def _emit_proposed_children(
    writer: asyncio.StreamWriter, run: dict[str, Any], task_id: str
) -> None:
    """Emit one ``propose_child`` wire message per declared child proposal.

    The proposal set is read from the run payload's ``proposed_children``
    (declared by the caller in the plan spec; deterministic, no model
    autonomy): each entry is ``{"child_task_id", "kind", "spec"}``. Every
    message carries a fresh ``request_id`` so the supervisor can correlate the
    resulting ``child_admitted`` / ``child_rejected`` events. Emitted after
    the task body finishes and before the result envelope, so the supervisor
    can admit children as soon as the parent's terminal envelope is known.
    """
    proposals = run.get("proposed_children")
    if not isinstance(proposals, list):
        return
    for proposal in proposals:
        if not isinstance(proposal, dict):
            continue
        child_task_id = proposal.get("child_task_id")
        kind = proposal.get("kind")
        child_spec = proposal.get("spec")
        if not isinstance(child_task_id, str) or not child_task_id:
            continue
        if not isinstance(kind, str) or not kind:
            continue
        if not isinstance(child_spec, dict):
            continue
        await send(writer, {
            "type": "propose_child",
            "request_id": make_request_id("propose"),
            "parent_task_id": task_id,
            "child_task_id": child_task_id,
            "kind": kind,
            "spec": child_spec,
        })


async def _emit_delegated_child(
    writer: asyncio.StreamWriter, config: AgentConfig, arguments: dict[str, Any]
) -> None:
    """Emit the ``propose_child`` wire message for one model ``delegate`` call.

    Mirrors ``_emit_proposed_children``'s message shape exactly (type,
    fresh ``request_id``, parent_task_id, child_task_id, kind, spec). The
    supervisor buffers the proposal per parent and validates/admits it at
    this task's terminal envelope; the tool result is emitted before this
    message so the model's observation stays bounded and independent.
    """
    await send(writer, {
        "type": "propose_child",
        "request_id": make_request_id("propose"),
        "parent_task_id": config.task_id,
        "child_task_id": arguments["child_task_id"],
        "kind": arguments["kind"],
        "spec": arguments["spec"],
    })


async def _run_task(
    writer: asyncio.StreamWriter,
    run: dict[str, Any],
    task_id: str,
    generation: int,
    stop: threading.Event,
    config: AgentConfig,
) -> dict[str, Any]:
    """Run the task body with heartbeats; returns the terminal outcome."""
    started_at = time.time()
    run_rid = run["request_id"]
    progress = AgentProgress()

    hb = asyncio.create_task(
        _heartbeat_loop(
            writer, task_id, generation, stop, progress, config.heartbeat_interval_s
        )
    )
    try:
        outcome = await do_work(run, stop, config=config, writer=writer, progress=progress)
    finally:
        stop.set()
        # Heartbeat stop: the write is enqueued synchronously and atomically,
        # but cancel() between the write and its drain could leave the next
        # (result) message written against a mid-drain heartbeat. Never cancel
        # mid-send: set the stop flag and let the loop observe it at its safe
        # point (after the in-flight send completes, before the next one).
        # A hard cancel is only a fallback if the loop fails to drain promptly.
        try:
            await asyncio.wait_for(hb, timeout=config.heartbeat_interval_s + 1.0)
        except (TimeoutError, asyncio.CancelledError):
            hb.cancel()
            try:
                await hb
            except asyncio.CancelledError:
                pass
    outcome["request_id"] = run_rid
    outcome["task_id"] = task_id
    outcome["generation"] = generation
    outcome["started_at"] = started_at
    outcome["ended_at"] = time.time()
    await _emit_proposed_children(writer, run, task_id)
    return outcome


def _exit_reason(status: str) -> str:
    return {"succeeded": "done", "failed": "failed", "cancelled": "cancelled"}.get(
        status, "failed")


async def _emit_result_envelope(writer: asyncio.StreamWriter, outcome: dict[str, Any]) -> None:
    status = outcome["status"]
    envelope = {
        "type": "result_envelope",
        "request_id": outcome["request_id"],
        "task_id": outcome["task_id"],
        "generation": outcome["generation"],
        "status": status,
        "exit_code": EXIT_CODES.get(status, 1),
        "commits": outcome.get("commits", []),
        "files_changed": outcome.get("files_changed", []),
        "diff": outcome.get("diff", ""),
        "diff_truncated": bool(outcome.get("diff_truncated", False)),
        "summary": (outcome.get("summary") or "")[:MAX_SUMMARY_CHARS],
        "failure_reason": outcome.get("failure_reason"),
        "started_at": outcome.get("started_at"),
        "ended_at": outcome.get("ended_at"),
    }
    provider_metadata = outcome.get("provider_metadata")
    if isinstance(provider_metadata, dict):
        envelope["provider_metadata"] = provider_metadata
    await send(writer, envelope)


async def _emit_result(writer: asyncio.StreamWriter, outcome: dict[str, Any]) -> None:
    """Emit result_envelope + the authoritative exit_message (normal completion)."""
    await _emit_result_envelope(writer, outcome)
    await send(writer, {
        "type": "exit_message",
        "task_id": outcome["task_id"],
        "generation": outcome["generation"],
        "reason": _exit_reason(outcome["status"]),
        "monotonic_ms": _monotonic_ms(),
    })


async def _send_ok(
    writer: asyncio.StreamWriter,
    msg: dict[str, Any],
    task_id: str,
    generation: int,
) -> None:
    await send(writer, {
        "type": "ok",
        "request_id": msg.get("request_id") if isinstance(msg, dict) else None,
        "task_id": task_id,
        "generation": generation,
        "monotonic_ms": _monotonic_ms(),
    })


async def _send_pong(
    writer: asyncio.StreamWriter,
    msg: dict[str, Any],
    task_id: str,
    generation: int,
) -> None:
    await send(writer, {
        "type": "pong",
        "request_id": msg.get("request_id"),
        "task_id": task_id,
        "generation": generation,
        "monotonic_ms": _monotonic_ms(),
    })


async def _fatal(writer: asyncio.StreamWriter, msg: Any, message: str) -> int:
    context = msg if isinstance(msg, dict) else {}
    await send(writer, {
        "type": "fatal_error",
        "request_id": context.get("request_id"),
        "task_id": context.get("task_id"),
        "generation": context.get("generation"),
        "error_type": "invalid_message",
        "message": message[:500],
        "recoverable": False,
    })
    await send(writer, {
        "type": "exit_message",
        "task_id": context.get("task_id"),
        "generation": context.get("generation"),
        "reason": "fatal",
        "monotonic_ms": _monotonic_ms(),
    })
    return 1


async def run(reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> int:
    """The worker wire loop. Returns the process exit code."""
    init_timeout = _env_float("CAMBIUM_INIT_TIMEOUT_S", INIT_TIMEOUT_S)
    idle_timeout = _env_float("CAMBIUM_IDLE_TIMEOUT_S", IDLE_TIMEOUT_S)

    try:
        first = await asyncio.wait_for(read_message(reader), timeout=init_timeout)
    except TimeoutError:
        return await _fatal(writer, {}, "init timeout: no init message within deadline")
    except MessageTooLong:
        return await _fatal(writer, {}, "wire line exceeded the length cap")
    if first is None:
        return 1
    if not isinstance(first, dict) or first.get("type") != "init" or "request_id" not in first:
        return await _fatal(writer, first, "expected init as the first message")

    init_rid = first["request_id"]
    task_id = first.get("task_id", "unknown")
    generation = first.get("generation", 1)
    init_fanout_config = first.get("fanout_config")
    if isinstance(generation, bool) or not isinstance(generation, int) or generation <= 0:
        return await _fatal(writer, first, "init generation must be a positive integer")
    try:
        init_config = AgentConfig.from_init(first)
    except ValueError as exc:
        return await _fatal(writer, first, f"invalid init config: {exc}")
    await send(writer, {
        "type": "ready",
        "request_id": init_rid,
        "task_id": task_id,
        "pid": os.getpid(),
        "generation": generation,
        "proto": first.get("proto", PROTO),
        "monotonic_ms": _monotonic_ms(),
    })
    # Worker-reuse opt-in (eval-3 ADOPT): when the init asks for it, the
    # worker stays alive after the task and waits for a rebind init on stdin
    # instead of exiting. The single-init exit behavior below is unchanged
    # when this flag is absent.
    worker_reuse = bool(first.get("worker_reuse"))

    current: asyncio.Task[dict[str, Any]] | None = None
    stop = threading.Event()

    while True:
        # The idle deadline only applies BETWEEN tasks: while a task runs the
        # supervisor sends no steering messages, and a slow model (e.g. max-
        # reasoning luna) can legitimately run far past the idle timeout. A
        # timeout mid-task would abort the run and exit "idle" with no result
        # envelope. With a task in flight the read simply waits.
        read_timeout = None if current is not None else idle_timeout
        read_task = asyncio.create_task(
            asyncio.wait_for(read_message(reader), timeout=read_timeout))
        pending = {read_task}
        if current is not None:
            pending.add(current)
        done, _ = await asyncio.wait(pending, return_when=asyncio.FIRST_COMPLETED)

        if current is not None and current in done:
            task = current
            current = None
            read_task.cancel()
            try:
                await read_task
            except BaseException:
                pass
            try:
                outcome = task.result()
            except Exception as exc:
                return await _fatal(writer, {}, f"task crashed: {exc}")
            await _emit_result_envelope(writer, outcome)
            if not worker_reuse:
                await send(writer, {
                    "type": "exit_message",
                    "task_id": outcome["task_id"],
                    "generation": outcome["generation"],
                    "reason": _exit_reason(outcome["status"]),
                    "monotonic_ms": _monotonic_ms(),
                })
                return 0
            # Reuse path: report ready-for-reuse and loop back to read the
            # next init instead of exiting. All per-task state (agent loop,
            # transcript, tool state, LM clients) is rebuilt from the new
            # init's config on rebind.
            await send(writer, {
                "type": "reuse_ready",
                "task_id": outcome["task_id"],
                "generation": outcome["generation"],
                "pid": os.getpid(),
                "monotonic_ms": _monotonic_ms(),
            })
            continue

        try:
            msg = read_task.result()
        except TimeoutError:
            # No message from the supervisor within the idle deadline: the
            # supervisor is presumed gone. Abort any current task and exit
            # gracefully (documented in the module docstring).
            stop.set()
            if current is not None:
                task = current
                current = None
                try:
                    await task
                except BaseException:
                    pass
            await send(writer, {
                "type": "exit_message",
                "task_id": task_id,
                "generation": generation,
                "reason": "idle",
                "monotonic_ms": _monotonic_ms(),
            })
            return 0
        except MessageTooLong:
            return await _fatal(writer, {}, "wire line exceeded the length cap")
        except Exception as exc:
            return await _fatal(writer, {}, f"wire read failed: {exc}")

        if msg is None:
            if worker_reuse and current is None:
                # stdin closed while idle between tasks (supervisor closed the
                # pipe or exited): a pooled worker exits cleanly.
                return 0
            # stdin closed: no further requests can arrive.
            await send(writer, {
                "type": "exit_message",
                "task_id": task_id,
                "generation": generation,
                "reason": "crash",
                "monotonic_ms": _monotonic_ms(),
            })
            return 1

        mtype = msg.get("type") if isinstance(msg, dict) else None
        if mtype == "init":
            # Rebind: only a reuse-enabled worker accepts a second init. The
            # rebind re-sends the FULL init (worktree, spec, fanout_config,
            # assigned_provider, budgets, permissions), so every per-task
            # client is rebuilt from the new config; nothing is carried over.
            if not worker_reuse:
                return await _fatal(writer, msg, "init after init (reuse not enabled)")
            if current is not None:
                return await _fatal(writer, msg, "init while a task is already running")
            if "request_id" not in msg:
                return await _fatal(writer, msg, "init without a request_id")
            new_generation = msg.get("generation", 1)
            if (
                isinstance(new_generation, bool)
                or not isinstance(new_generation, int)
                or new_generation <= 0
            ):
                return await _fatal(writer, msg, "init generation must be a positive integer")
            try:
                new_config = AgentConfig.from_init(msg)
            except ValueError as exc:
                return await _fatal(writer, msg, f"invalid init config: {exc}")
            if new_config.worktree is not None:
                os.chdir(new_config.worktree)
            first = msg
            init_rid = msg["request_id"]
            task_id = msg.get("task_id", "unknown")
            generation = new_generation
            init_fanout_config = msg.get("fanout_config")
            init_config = new_config
            worker_reuse = bool(msg.get("worker_reuse"))
            stop = threading.Event()
            await send(writer, {
                "type": "ready",
                "request_id": init_rid,
                "task_id": task_id,
                "pid": os.getpid(),
                "generation": generation,
                "proto": msg.get("proto", PROTO),
                "monotonic_ms": _monotonic_ms(),
            })
            continue
        if mtype == "run_task":
            if current is not None:
                return await _fatal(writer, msg, "run_task while a task is already running")
            if "request_id" not in msg:
                return await _fatal(writer, msg, "run_task without a request_id")
            claimed_generation = msg.get("generation", generation)
            if claimed_generation != generation:
                return await _fatal(writer, msg, "run_task generation does not match init")
            msg = {**msg, "generation": generation}
            stop = threading.Event()
            task_run = dict(msg)
            if init_fanout_config is not None:
                # Provider configuration belongs to init. It is kept in the
                # worker's local task context and never sent back over IPC.
                task_run["fanout_config"] = init_fanout_config
            task_config = _merge_task_config(init_config, first, task_run)
            current = asyncio.create_task(
                _run_task(writer, task_run, task_id, generation, stop, task_config))
        elif mtype == "check_health":
            await _send_ok(writer, msg, task_id, generation)
        elif mtype == "ping":
            await _send_pong(writer, msg, task_id, generation)
        elif mtype == "steer":
            payload = msg.get("payload") or {}
            # Structured parse: only an exact {"action": "cancel"} aborts.
            # Free text containing the word "cancel" must NOT abort.
            if isinstance(payload, dict) and payload.get("action") == "cancel":
                logger.info("steer: cancel requested")
                stop.set()
            else:
                logger.info("steer (v2.1 hook; continuing): %s",
                            json.dumps(payload)[:200])
        elif mtype == "cancel":
            logger.info("cancel: aborting current task")
            await _send_ok(writer, msg, task_id, generation)
            stop.set()
        elif mtype == "shutdown":
            await _send_ok(writer, msg, task_id, generation)
            if current is not None:
                stop.set()
                task = current
                current = None
                try:
                    outcome = await task
                    await _emit_result_envelope(writer, outcome)
                except BaseException:
                    pass
            await send(writer, {
                "type": "exit_message",
                "task_id": task_id,
                "generation": generation,
                "reason": "shutdown",
                "monotonic_ms": _monotonic_ms(),
            })
            return 0
        else:
            return await _fatal(writer, msg, f"unknown message type {mtype!r}")


class _WriterProtocol(asyncio.streams.FlowControlMixin):
    """Flow-control protocol for the stdout write transport.

    ``StreamWriter.wait_closed`` resolves the protocol's ``_closed`` future,
    so it must be tied to THIS transport's ``connection_lost`` — not the
    reader's (whose stdin pipe stays open for the worker's lifetime).
    """

    def __init__(self) -> None:
        super().__init__()
        self._closed = asyncio.get_running_loop().create_future()

    def connection_lost(self, exc: BaseException | None) -> None:
        if not self._closed.done():
            self._closed.set_result(exc)
        super().connection_lost(exc)

    def _get_close_waiter(self, stream: asyncio.StreamWriter) -> asyncio.Future:
        return self._closed


async def _open_stdio() -> tuple[asyncio.StreamReader, asyncio.StreamWriter]:
    """Wrap stdin/stdout in asyncio streams (protocol stream = stdout)."""
    loop = asyncio.get_running_loop()
    reader = asyncio.StreamReader(limit=MAX_LINE_BYTES)
    read_protocol = asyncio.StreamReaderProtocol(reader)
    await loop.connect_read_pipe(lambda: read_protocol, sys.stdin.buffer)
    write_protocol = _WriterProtocol()
    transport, _ = await loop.connect_write_pipe(
        lambda: write_protocol, sys.stdout.buffer)
    writer = asyncio.StreamWriter(transport, write_protocol, reader, loop)
    return reader, writer


async def _amain() -> int:
    reader, writer = await _open_stdio()
    try:
        return await run(reader, writer)
    finally:
        writer.close()
        await writer.wait_closed()


def main() -> int:
    logging.basicConfig(
        stream=sys.stderr,
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )
    try:
        return asyncio.run(_amain())
    except KeyboardInterrupt:
        return 1


if __name__ == "__main__":
    sys.exit(main())
