"""Persistent interactive-session coordination for REPL and TUI frontends.

The supervisor still owns one immutable worker session per submitted prompt.
This module links those leaves into one long semantic branch by carrying the
latest immutable context checkpoint forward.  A cache-compatible continuation
uses the exact ``context_fork`` descriptor and provider/model lease; the same
checkpoint is also supplied as ``summary_trunk_ref`` so an incompatible provider
can still recover the provider-neutral semantic trunk without pretending that
its KV cache is warm.

The coordinator is deliberately small and single-writer.  Frontends call
``prepare_turn`` -> ``observe_event`` -> ``complete_turn``.
No worker or renderer mutates the branch head directly.
"""

from __future__ import annotations

import copy
import json
import os
import re
import shutil
import sqlite3
import time
from collections.abc import Mapping
from dataclasses import asdict, dataclass, replace
from pathlib import Path
from typing import Any

from . import oneshot, supervisor
from .oneshot import OneShotConfig, SessionMode
from .results import ROOT_RESULT_KEYS, Result
from .store import EventStore, StoreError, iter_event_pages
from .summary_trunk import (
    SummaryTrunkError,
    is_k0_entry,
    partition_summary_trunk,
    rollover_summary_trunk,
    summary_entries,
)

fcntl: Any
try:
    import fcntl
except ImportError:  # pragma: no cover - exercised on Windows
    fcntl = None

_INTERACTIVE_SCHEMA = 1
_MANIFEST_NAME = "interactive.json"
_LOCK_NAME = "session.lock"
_TURN_DIR_RE = re.compile(r"^turn-(\d+)$")
_MANIFEST_TURN_MARGIN = 1
_CONTEXT_KINDS = frozenset({"context_checkpoint", "context_epoch_advanced"})
_BRANCH_GENERATION_FIELD = "interactive_branch_generation"
_BRANCH_START_TURN_FIELD = "interactive_branch_start_turn"
_INTERACTIVE_TASK_ID = "interactive-main"
_SUCCESS_EXIT_REASONS = frozenset({"done"})
_FAILURE_EVENT_KINDS = frozenset(
    {
        "error",
        "fatal_error",
        "protocol",
        "worker_failed",
        "task_failed",
        "worker_terminated",
        "join_invariant_failed",
        "merge_failed",
        "resolver_failed",
        "context_resume_failed",
        "timeout",
    }
)
_FORK_FIELDS = (
    "provider",
    "model",
    "system_sha256",
    "tools_sha256",
    "prefix_sha256",
    "suffix_sha256",
    "full_sha256",
    "prefix_bytes",
    "provider_boundary",
)


class InteractiveSessionError(ValueError):
    """An interactive branch manifest or checkpoint seed is invalid."""


class InteractiveSessionBusyError(InteractiveSessionError):
    """Another frontend currently owns the interactive session lock."""


@dataclass(frozen=True, slots=True)
class ContextSeed:
    """One immutable context checkpoint that can seed the next prompt."""

    source_session: Path
    checkpoint_ref: str
    descriptor: dict[str, Any]
    provider: str | None
    model: str | None
    epoch: int


@dataclass(frozen=True, slots=True)
class BranchHead:
    """One durable checkpoint head discovered in an interactive turn log."""

    turn: int
    epoch: int
    checkpoint_ref: str
    source_session: Path
    current: bool


@dataclass(frozen=True, slots=True)
class InteractiveTurn:
    """Prepared one-shot leaf belonging to one long interactive branch."""

    number: int
    session_dir: Path
    config: OneShotConfig
    context_fork: dict[str, Any] | None
    summary_trunk_ref: str | None
    branch_generation: int
    branch_start_turn: int


def _payload(event: Mapping[str, Any]) -> Mapping[str, Any]:
    value = event.get("payload")
    return value if isinstance(value, Mapping) else {}


def _safe_relative(value: str) -> Path:
    relative = Path(value)
    if relative.is_absolute() or not relative.parts or ".." in relative.parts:
        raise InteractiveSessionError("checkpoint_ref must be a confined relative path")
    return relative


def _checkpoint_path(session_dir: Path, checkpoint_ref: str) -> Path:
    relative = _safe_relative(checkpoint_ref)
    session_root = session_dir.resolve()
    root = (session_root / ".cambium" / "checkpoints").resolve()
    try:
        root.relative_to(session_root)
    except ValueError as exc:
        raise InteractiveSessionError("checkpoint root escapes the session") from exc
    candidate = root / relative
    current = root
    for component in relative.parts:
        current /= component
        if current.is_symlink():
            raise InteractiveSessionError("checkpoint path is a symlink")
    candidate = candidate.resolve()
    try:
        candidate.relative_to(root)
    except ValueError as exc:
        raise InteractiveSessionError("checkpoint_ref escapes the checkpoint root") from exc
    return candidate


def _fork_descriptor(checkpoint_ref: str, cache_key: Mapping[str, Any]) -> dict[str, Any] | None:
    descriptor: dict[str, Any] = {"checkpoint_ref": checkpoint_ref}
    for field in _FORK_FIELDS:
        if field not in cache_key:
            return None
        descriptor[field] = copy.deepcopy(cache_key[field])
    provider = descriptor.get("provider")
    model = descriptor.get("model")
    if not isinstance(provider, str) or not provider:
        return None
    if not isinstance(model, str) or not model:
        return None
    return descriptor


def _atomic_json(path: Path, document: Mapping[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    temporary = path.with_name(f".{path.name}.{os.getpid()}.tmp")
    data = json.dumps(
        document,
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
        allow_nan=False,
    )
    try:
        with open(temporary, "w", encoding="utf-8", newline="") as stream:
            stream.write(data)
            stream.flush()
            os.fsync(stream.fileno())
        os.chmod(temporary, 0o600)
        os.replace(temporary, path)
    finally:
        try:
            temporary.unlink()
        except FileNotFoundError:
            pass


def _lock_document(path: Path) -> dict[str, Any] | None:
    """Read lock metadata without treating an unreadable file as ownership."""
    try:
        raw = path.read_bytes()
        if not raw or len(raw) > 4096:
            return None
        document = json.loads(raw)
    except (OSError, UnicodeDecodeError, json.JSONDecodeError, RecursionError):
        return None
    return dict(document) if isinstance(document, Mapping) else None


def _pid_alive(pid: Any) -> bool:
    if isinstance(pid, bool) or not isinstance(pid, int) or pid <= 0:
        return False
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    except OSError:
        return False
    return True


class _InteractiveSessionLock:
    """A flock-backed frontend lock whose metadata makes stale owners visible.

    ``flock`` is the authority: the kernel releases it when a frontend is
    killed, so a lock file left behind by a crash is safe to reclaim.  The
    small metadata document is only diagnostic.  It lets operators distinguish
    an active owner from a killed process and gives tests a deterministic way
    to exercise stale-file recovery without relying on process timing.
    """

    def __init__(self, path: Path) -> None:
        self.path = path
        self._fd: int | None = None
        self._recovered_stale = False

    @property
    def recovered_stale(self) -> bool:
        return self._recovered_stale

    @staticmethod
    def _metadata(*, released: bool) -> dict[str, Any]:
        return {
            "pid": os.getpid(),
            "started_at": time.time(),
            "released": released,
        }

    def _write_metadata(self, document: Mapping[str, Any]) -> None:
        fd = self._fd
        if fd is None:
            return
        encoded = json.dumps(document, sort_keys=True, separators=(",", ":")).encode("utf-8")
        os.lseek(fd, 0, os.SEEK_SET)
        os.ftruncate(fd, 0)
        remaining = memoryview(encoded)
        while remaining:
            written = os.write(fd, remaining)
            if written <= 0:  # pragma: no cover - defensive for unusual filesystems
                raise OSError("interactive session lock write made no progress")
            remaining = remaining[written:]
        os.fsync(fd)

    def acquire(self) -> None:
        if self._fd is not None:
            return
        self.path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        try:
            os.chmod(self.path.parent, 0o700)
        except OSError:
            pass
        existed = self.path.is_file()
        previous = _lock_document(self.path) if existed else None
        flags = os.O_RDWR | os.O_CREAT | getattr(os, "O_CLOEXEC", 0)
        flags |= getattr(os, "O_NOFOLLOW", 0)
        fd = os.open(self.path, flags, 0o600)
        try:
            os.fchmod(fd, 0o600)
            if fcntl is not None:
                try:
                    fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
                except BlockingIOError as exc:
                    owner = previous.get("pid") if previous is not None else None
                    os.close(fd)
                    detail = f"session is already running: {self.path.parent.parent}"
                    if isinstance(owner, int) and not _pid_alive(owner):
                        detail += f" (stale owner pid={owner})"
                    raise InteractiveSessionBusyError(detail) from exc
            elif previous is not None and not previous.get("released"):
                owner = previous.get("pid")
                if _pid_alive(owner):
                    os.close(fd)
                    raise InteractiveSessionBusyError(
                        f"session is already running: {self.path.parent.parent}"
                    )
            self._fd = fd
            self._recovered_stale = existed and (
                previous is None
                or (not bool(previous.get("released")) and not _pid_alive(previous.get("pid")))
            )
            self._write_metadata(self._metadata(released=False))
        except BaseException:
            if self._fd is None:
                try:
                    os.close(fd)
                except OSError:
                    pass
            raise

    def release(self) -> None:
        fd = self._fd
        if fd is None:
            return
        self._fd = None
        try:
            self._fd = fd
            try:
                self._write_metadata(self._metadata(released=True))
            except OSError:
                pass
            if fcntl is not None:
                fcntl.flock(fd, fcntl.LOCK_UN)
        finally:
            self._fd = None
            os.close(fd)

    def status(self) -> str:
        """Return ``missing``, ``available``, ``active``, or ``stale``."""
        if not self.path.exists():
            return "missing"
        metadata = _lock_document(self.path)
        if fcntl is not None:
            flags = os.O_RDWR | getattr(os, "O_CLOEXEC", 0)
            flags |= getattr(os, "O_NOFOLLOW", 0)
            try:
                fd = os.open(self.path, flags)
            except OSError:
                return "available"
            try:
                try:
                    fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
                except BlockingIOError:
                    return "active"
                finally:
                    try:
                        fcntl.flock(fd, fcntl.LOCK_UN)
                    except OSError:
                        pass
            finally:
                os.close(fd)
        if metadata is None or (
            not bool(metadata.get("released")) and not _pid_alive(metadata.get("pid"))
        ):
            return "stale"
        return "available"

    def __enter__(self) -> _InteractiveSessionLock:
        self.acquire()
        return self

    def __exit__(self, _exc_type: Any, _exc: Any, _traceback: Any) -> None:
        self.release()


def _read_manifest_document(path: Path) -> dict[str, Any] | None:
    try:
        raw = path.read_bytes()
        if len(raw) > 1024 * 1024:
            return None
        document = json.loads(raw)
    except (OSError, UnicodeDecodeError, json.JSONDecodeError, RecursionError):
        return None
    return dict(document) if isinstance(document, Mapping) else None


def _durable_mtime(root: Path) -> int:
    """Return a monotonic-ish activity key for reconnect candidate ordering."""
    newest = 0
    paths = [root / ".cambium" / _MANIFEST_NAME]
    paths.extend(root.glob("turn-*/.cambium/events.db"))
    paths.extend(root.glob("turn-*/.cambium/checkpoints/**/*"))
    for path in paths:
        try:
            newest = max(newest, path.stat().st_mtime_ns)
        except OSError:
            continue
    return newest


class InteractiveSession:
    """Single-writer semantic branch spanning many one-shot supervisor leaves."""

    def __init__(self, config: OneShotConfig) -> None:
        # Keep the interactive marker at the frontend boundary.  A caller may
        # construct ``InteractiveSession(OneShotConfig())`` directly (without
        # going through the CLI), and those turns must still receive the
        # throughput-aware default instead of the one-shot fallback.
        from .prompts import load_policy, validate_policy

        self._base_config = replace(config, interactive=True)
        self.repo = oneshot.resolve_repo(config.repo)
        self._reconnected = False
        if config.session_root is None:
            self.root = oneshot.allocate_session_dir(self.repo)
        else:
            self.root = Path(config.session_root).expanduser().resolve()
            self.root.mkdir(parents=True, exist_ok=True, mode=0o700)
            try:
                os.chmod(self.root, 0o700)
            except OSError:
                pass
        self._manifest_path = self.root / ".cambium" / _MANIFEST_NAME
        if self._manifest_path.is_file() and self._has_durable_state(self.root):
            self._reconnected = True
        self._lock = _InteractiveSessionLock(self.root / ".cambium" / _LOCK_NAME)
        self._turn = 0
        self._branch_generation = 1
        self._branch_start_turn = 0
        self._seed: ContextSeed | None = None
        self._pending_seed: ContextSeed | None = None
        self._last_epoch = 0
        self._last_checkpoint: str | None = None
        self._provider_preference: str | None = None
        self._model_preference: str | None = None
        self._model_preferences: dict[str, str] = {}
        self._serving_turn: int | None = None
        self._lock_acquired = False
        self._load_manifest()
        selected = self._base_config.prompt_policy
        self._base_config = replace(
            self._base_config,
            prompt_policy=validate_policy(selected) if selected is not None else load_policy(),
        )
        self._load_durable_head()
        self._reconcile_provider_preference()

    @classmethod
    def latest_for_repo(cls, repo: Path) -> Path | None:
        """Return the newest reconnectable interactive root for ``repo``.

        Ordinary one-shot leaves share the repository session root, so the
        interactive manifest is the type marker.  A manifest alone is not
        enough to resume: at least one durable event database or checkpoint
        must exist, which avoids reopening an abandoned empty allocation.
        """
        repo = Path(repo).expanduser().resolve()
        sessions = oneshot.default_session_root(repo)
        if not sessions.is_dir():
            return None
        candidates: list[tuple[tuple[int, int, str], Path]] = []
        for child in sessions.iterdir():
            if not cls._is_reconnectable(child, repo):
                continue
            document = _read_manifest_document(child / ".cambium" / _MANIFEST_NAME)
            if document is None:
                continue
            turn = document["turn"]
            candidates.append(((_durable_mtime(child), turn, child.name), child.resolve()))
        if not candidates:
            return None
        candidates.sort(key=lambda item: item[0])
        return candidates[-1][1]

    @classmethod
    def resolve_continue_session(cls, repo: str | Path, value: str | Path | None) -> Path:
        """Resolve an explicit continuation target without allocating a session."""
        repo_path = oneshot.resolve_repo(repo)
        if value is None or not str(value).strip():
            latest = cls.latest_for_repo(repo_path)
            if latest is None:
                raise InteractiveSessionError(
                    "no previous interactive session is available to continue"
                )
            return latest

        requested = Path(value).expanduser()
        value_text = os.fspath(value)
        if (
            not requested.is_absolute()
            and requested.parent == Path(".")
            and not value_text.startswith(".")
            and not requested.is_dir()
        ):
            requested = oneshot.default_session_root(repo_path) / requested
        sessions_root = oneshot.default_session_root(repo_path)
        if sessions_root.parent.is_symlink() or sessions_root.is_symlink():
            raise InteractiveSessionError(
                "repository session root contains a symlink; refusing continuation"
            )
        sessions_root = sessions_root.resolve()
        lexical = Path(os.path.abspath(os.fspath(requested)))
        try:
            relative = lexical.relative_to(sessions_root)
        except ValueError as exc:
            raise InteractiveSessionError(
                "interactive session path must stay under the repository session root"
            ) from exc
        current = sessions_root
        for component in relative.parts:
            current /= component
            if current.is_symlink():
                raise InteractiveSessionError(
                    "interactive session path must not contain symlinked components"
                )
        candidate = lexical.resolve()
        try:
            candidate.relative_to(sessions_root)
        except ValueError as exc:
            raise InteractiveSessionError(
                "interactive session path must stay under the repository session root"
            ) from exc
        if not cls._is_reconnectable(candidate, repo_path):
            raise InteractiveSessionError(f"no resumable interactive session found at {candidate}")
        return candidate

    @classmethod
    def _is_reconnectable(cls, root: Path, repo: Path) -> bool:
        if not root.is_dir():
            return False
        sessions_root = oneshot.default_session_root(repo)
        if sessions_root.parent.is_symlink() or sessions_root.is_symlink():
            return False
        sessions_root = sessions_root.resolve()
        lexical = Path(os.path.abspath(os.fspath(root)))
        try:
            relative = lexical.relative_to(sessions_root)
        except ValueError:
            return False
        current = sessions_root
        for component in relative.parts:
            current /= component
            if current.is_symlink():
                return False
        try:
            root = lexical.resolve()
            root.relative_to(sessions_root)
        except (OSError, ValueError):
            return False
        document = _read_manifest_document(root / ".cambium" / _MANIFEST_NAME)
        if document is None or document.get("schema") != _INTERACTIVE_SCHEMA:
            return False
        if document.get("repo") != str(repo):
            return False
        turn = document.get("turn")
        max_listed_turn = max(
            (number for number, _turn_dir in cls._listed_turn_dirs(root)),
            default=0,
        )
        return (
            type(turn) is int
            and 0 <= turn <= max_listed_turn + _MANIFEST_TURN_MARGIN
            and cls._has_durable_state(root)
        )

    @staticmethod
    def _has_durable_state(root: Path) -> bool:
        for _number, turn_dir in InteractiveSession._listed_turn_dirs(root):
            state_dir = turn_dir / ".cambium"
            if (state_dir / "events.db").is_file():
                return True
            checkpoints = state_dir / "checkpoints"
            if checkpoints.is_dir() and any(path.is_file() for path in checkpoints.rglob("*")):
                return True
        return False

    def acquire(self) -> None:
        """Own the interactive root until :meth:`release` is called."""
        if self._lock_acquired:
            return
        self._lock.acquire()
        try:
            self._reload_durable_state()
        except BaseException:
            self._lock.release()
            raise
        self._lock_acquired = True

    def release(self) -> None:
        """Release the interactive root lock, including after normal exit."""
        self._lock.release()
        self._lock_acquired = False

    @property
    def lock_path(self) -> Path:
        return self._lock.path

    @property
    def lock_status(self) -> str:
        return self._lock.status()

    @property
    def recovered_stale_lock(self) -> bool:
        return self._lock.recovered_stale

    @property
    def reconnected(self) -> bool:
        return self._reconnected

    @property
    def last_epoch(self) -> int:
        return self._last_epoch

    @property
    def last_checkpoint(self) -> str | None:
        return self._last_checkpoint

    def __enter__(self) -> InteractiveSession:
        self.acquire()
        return self

    def __exit__(self, _exc_type: Any, _exc: Any, _traceback: Any) -> None:
        self.release()

    @property
    def turn(self) -> int:
        return self._turn

    @property
    def seed(self) -> ContextSeed | None:
        return self._seed

    @property
    def branch_generation(self) -> int:
        return self._branch_generation

    @property
    def branch_start_turn(self) -> int:
        return self._branch_start_turn

    def active_turn_dirs(self) -> tuple[Path, ...]:
        """Completed turn leaves belonging to the current semantic branch."""
        return tuple(
            turn_dir
            for number, turn_dir in self._listed_turn_dirs(self.root)
            if self._branch_start_turn < number <= self._turn
        )

    @property
    def provider(self) -> str | None:
        if self._provider_preference is not None:
            return self._provider_preference
        return self._seed.provider if self._seed is not None else self._base_config.provider

    @property
    def model(self) -> str | None:
        if self._model_preference is not None:
            return self._model_preference
        return self._seed.model if self._seed is not None else self._base_config.model

    def _manifest_document(self) -> dict[str, Any]:
        seed: dict[str, Any] | None = None
        if self._seed is not None:
            seed = {
                "source_session": str(self._seed.source_session),
                "checkpoint_ref": self._seed.checkpoint_ref,
                "descriptor": self._seed.descriptor,
                "provider": self._seed.provider,
                "model": self._seed.model,
                "epoch": self._seed.epoch,
            }
        return {
            "schema": _INTERACTIVE_SCHEMA,
            "repo": str(self.repo),
            "turn": self._turn,
            "branch_generation": self._branch_generation,
            "branch_start_turn": self._branch_start_turn,
            "prompt_policy": self._base_config.prompt_policy,
            "seed": seed,
            "provider_preference": self._provider_preference,
            "model_preference": self._model_preference,
            "model_preferences": dict(self._model_preferences),
        }

    def _write_manifest(self) -> None:
        _atomic_json(self._manifest_path, self._manifest_document())

    def _reload_durable_state(self) -> None:
        """Refresh state after taking ownership of the frontend lock."""
        self._turn = 0
        self._branch_generation = 1
        self._branch_start_turn = 0
        self._seed = None
        self._pending_seed = None
        self._last_epoch = 0
        self._last_checkpoint = None
        self._provider_preference = None
        self._model_preference = None
        self._model_preferences = {}
        self._serving_turn = None
        self._reconnected = self._manifest_path.is_file() and self._has_durable_state(self.root)
        self._load_manifest()
        self._reconcile_successful_orphans()
        self._load_durable_head()
        self._reconcile_provider_preference()

    def _load_manifest(self) -> None:
        if not self._manifest_path.is_file():
            return
        try:
            raw = self._manifest_path.read_bytes()
            if len(raw) > 1024 * 1024:
                raise InteractiveSessionError("interactive manifest exceeds the size cap")
            document = json.loads(raw)
        except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise InteractiveSessionError("interactive manifest is unreadable") from exc
        if not isinstance(document, Mapping) or document.get("schema") != _INTERACTIVE_SCHEMA:
            raise InteractiveSessionError("interactive manifest schema is invalid")
        if document.get("repo") != str(self.repo):
            raise InteractiveSessionError("interactive manifest belongs to another repository")
        turn = document.get("turn")
        generation = document.get("branch_generation", 1)
        branch_start = document.get("branch_start_turn", 0)
        if type(turn) is not int or turn < 0:
            raise InteractiveSessionError("interactive manifest turn is invalid")
        max_listed_turn = max(
            (number for number, _turn_dir in self._listed_turn_dirs(self.root)),
            default=0,
        )
        if turn > max_listed_turn + _MANIFEST_TURN_MARGIN:
            raise InteractiveSessionError(
                "interactive manifest turn is implausibly ahead of durable turn directories"
            )
        if type(generation) is not int or generation < 1:
            raise InteractiveSessionError("interactive manifest generation is invalid")
        if type(branch_start) is not int or not 0 <= branch_start <= turn:
            raise InteractiveSessionError("interactive manifest branch start is invalid")
        self._turn = turn
        self._branch_generation = generation
        self._branch_start_turn = branch_start
        if "prompt_policy" in document:
            from .prompts import validate_policy

            self._base_config = replace(
                self._base_config, prompt_policy=validate_policy(document["prompt_policy"])
            )
        provider_preference = document.get("provider_preference")
        if provider_preference is not None and (
            not isinstance(provider_preference, str) or not provider_preference.strip()
        ):
            raise InteractiveSessionError("interactive provider preference is invalid")
        self._provider_preference = provider_preference
        model_preference = document.get("model_preference")
        if model_preference is not None and (
            not isinstance(model_preference, str) or not model_preference.strip()
        ):
            raise InteractiveSessionError("interactive model preference is invalid")
        self._model_preference = model_preference
        model_preferences = document.get("model_preferences", {})
        if not isinstance(model_preferences, Mapping):
            raise InteractiveSessionError("interactive model preferences are invalid")
        parsed_model_preferences: dict[str, str] = {}
        for provider, model in model_preferences.items():
            if (
                not isinstance(provider, str)
                or not provider.strip()
                or not isinstance(model, str)
                or not model.strip()
            ):
                raise InteractiveSessionError("interactive model preferences are invalid")
            parsed_model_preferences[provider] = model
        self._model_preferences = parsed_model_preferences
        seed = document.get("seed")
        if seed is None:
            return
        if not isinstance(seed, Mapping):
            raise InteractiveSessionError("interactive manifest seed is invalid")
        source = seed.get("source_session")
        checkpoint_ref = seed.get("checkpoint_ref")
        descriptor = seed.get("descriptor")
        epoch = seed.get("epoch", 0)
        if not isinstance(source, str) or not source:
            raise InteractiveSessionError("interactive seed source is invalid")
        if not isinstance(checkpoint_ref, str) or not checkpoint_ref:
            raise InteractiveSessionError("interactive seed checkpoint_ref is invalid")
        if not isinstance(descriptor, Mapping):
            raise InteractiveSessionError("interactive seed descriptor is invalid")
        if type(epoch) is not int or epoch < 0:
            raise InteractiveSessionError("interactive seed epoch is invalid")
        source_session = Path(source).expanduser().resolve()
        checkpoint = _checkpoint_path(source_session, checkpoint_ref)
        if not checkpoint.is_file():
            raise InteractiveSessionError("interactive seed checkpoint is missing")
        provider = seed.get("provider")
        model = seed.get("model")
        self._seed = ContextSeed(
            source_session=source_session,
            checkpoint_ref=checkpoint_ref,
            descriptor=copy.deepcopy(dict(descriptor)),
            provider=provider if isinstance(provider, str) and provider else None,
            model=model if isinstance(model, str) and model else None,
            epoch=epoch,
        )

    @staticmethod
    def _context_seed_from_event(
        source_session: Path,
        event: Mapping[str, Any],
        *,
        strict: bool = False,
    ) -> ContextSeed | None:
        """Build a reusable seed only from a complete durable context event."""
        payload = _payload(event)
        checkpoint_ref = payload.get("checkpoint_ref")
        cache_key = payload.get("cache_key")
        epoch = payload.get("epoch")
        if (
            not isinstance(checkpoint_ref, str)
            or not checkpoint_ref
            or not isinstance(cache_key, Mapping)
            or type(epoch) is not int
            or epoch < 0
        ):
            return None
        try:
            checkpoint = _checkpoint_path(source_session, checkpoint_ref)
        except InteractiveSessionError:
            return None
        if not checkpoint.is_file() or checkpoint.is_symlink():
            return None
        descriptor_cache_key = cache_key
        if strict:
            try:
                from .ipc import MAX_LINE_BYTES
                from .worker import (
                    _reject_duplicate_pairs,
                    _reject_json_constant,
                    _validate_epoch_checkpoint_data,
                )

                if checkpoint.stat().st_size > MAX_LINE_BYTES * 4:
                    return None
                checkpoint_data = json.loads(
                    checkpoint.read_text(encoding="utf-8"),
                    object_pairs_hook=_reject_duplicate_pairs,
                    parse_constant=_reject_json_constant,
                )
                validated = _validate_epoch_checkpoint_data(
                    checkpoint_data,
                    checkpoint_ref,
                    expected_task_id=_INTERACTIVE_TASK_ID,
                    expected_generation=event.get("generation"),
                )
                if payload.get("epoch") != validated.epoch:
                    return None
                for field in _FORK_FIELDS:
                    if cache_key.get(field) != getattr(validated.cache_key, field):
                        return None
                descriptor_cache_key = {
                    field: getattr(validated.cache_key, field) for field in _FORK_FIELDS
                }
            except (
                OSError,
                UnicodeDecodeError,
                ValueError,
                TypeError,
                KeyError,
                RecursionError,
            ):
                return None
        descriptor = _fork_descriptor(checkpoint_ref, descriptor_cache_key)
        provider = cache_key.get("provider")
        model = cache_key.get("model")
        return ContextSeed(
            source_session=source_session.resolve(),
            checkpoint_ref=checkpoint_ref,
            descriptor={} if descriptor is None else descriptor,
            provider=provider if isinstance(provider, str) and provider else None,
            model=model if isinstance(model, str) and model else None,
            epoch=epoch,
        )

    @staticmethod
    def _interactive_plan_task(
        plan_path: Path,
        *,
        branch_generation: int | None = None,
        branch_start_turn: int | None = None,
        turn: int | None = None,
    ) -> tuple[dict[str, Any], bool] | None:
        """Return the one interactive task and whether it needs context."""
        plan = _read_manifest_document(plan_path)
        tasks = plan.get("tasks") if isinstance(plan, Mapping) else None
        if not isinstance(tasks, list) or len(tasks) != 1:
            return None
        task = tasks[0]
        if not isinstance(task, Mapping) or task.get("task_id") != _INTERACTIVE_TASK_ID:
            return None
        generation = task.get(_BRANCH_GENERATION_FIELD)
        start_turn = task.get(_BRANCH_START_TURN_FIELD)
        if type(generation) is not int or generation < 1:
            return None
        if type(start_turn) is not int or start_turn < 0:
            return None
        if branch_generation is not None and generation != branch_generation:
            return None
        if branch_start_turn is not None and start_turn != branch_start_turn:
            return None
        if turn is not None and start_turn > turn:
            return None
        for field in ("repo", "worktree_path", "branch", "task"):
            value = task.get(field)
            if not isinstance(value, str) or not value.strip():
                return None
        context_fork = task.get("context_fork")
        summary_ref = task.get("summary_trunk_ref")
        if context_fork is not None and not isinstance(context_fork, Mapping):
            return None
        if summary_ref is not None and (
            not isinstance(summary_ref, str) or not summary_ref
        ):
            return None
        context_reuse = task.get("context_reuse")
        if type(context_reuse) is not bool:
            return None
        return dict(task), context_reuse

    @staticmethod
    def _assignment_matches_plan(task: Mapping[str, Any], event: Mapping[str, Any]) -> bool:
        """Check the durable assignment against the persisted interactive task."""
        envelope_task_id = event.get("task_id")
        if envelope_task_id is not None and envelope_task_id != _INTERACTIVE_TASK_ID:
            return False
        payload = _payload(event)
        payload_task_id = payload.get("task_id")
        if payload_task_id is not None and payload_task_id != _INTERACTIVE_TASK_ID:
            return False
        if envelope_task_id is None and payload_task_id is None:
            return False
        for field in ("repo", "worktree_path", "branch", "task"):
            expected = task.get(field)
            actual = payload.get(field)
            if expected is not None and actual != expected:
                return False
            if expected is None and actual is not None:
                return False
        parent_task_id = payload.get("parent_task_id")
        return parent_task_id is None

    @staticmethod
    def _durable_turn_evidence(
        turn_dir: Path,
        *,
        branch_generation: int | None = None,
        branch_start_turn: int | None = None,
        turn: int | None = None,
        require_context: bool = False,
        expected_repo: Path | None = None,
        allow_post_terminal_context: bool = False,
    ) -> tuple[bool, ContextSeed | None]:
        """Validate one turn from its durable plan, events, and checkpoints.

        A result file is only a corroborating record.  Adoption requires the
        event stream to prove assignment, a successful terminal result, a
        clean exit, and a successful session end.  A later target failure
        clears a provisional success, while a new generation may subsequently
        prove success again.  A context checkpoint is usable only when it was
        emitted by that successful generation; a failed generation's
        checkpoint must never seed a continuation.
        """
        state_dir = turn_dir / ".cambium"
        event_db = state_dir / "events.db"
        result_path = state_dir / "result.json"
        plan_path = turn_dir / "plan.json"
        if any(path.is_symlink() for path in (state_dir, event_db, result_path, plan_path)):
            return False, None
        if not event_db.is_file() or not plan_path.is_file():
            return False, None

        parsed_plan = InteractiveSession._interactive_plan_task(
            plan_path,
            branch_generation=branch_generation,
            branch_start_turn=branch_start_turn,
            turn=turn,
        )
        if parsed_plan is None:
            return False, None
        task, plan_requires_context = parsed_plan
        context_required = require_context or plan_requires_context
        if expected_repo is not None:
            try:
                if Path(task["repo"]).resolve() != expected_repo.resolve():
                    return False, None
                if Path(task["worktree_path"]).resolve() != (turn_dir / "wt").resolve():
                    return False, None
                if task["branch"] != oneshot._default_branch(turn_dir):
                    return False, None
            except (OSError, TypeError, ValueError):
                return False, None

        # ``result.json`` is useful corroboration, but events remain the
        # authority.  If a result exists, reject malformed or contradictory
        # data instead of allowing it to override the event stream.
        if result_path.exists():
            document = _read_manifest_document(result_path)
            if document is None or set(document) != set(ROOT_RESULT_KEYS):
                return False, None
            try:
                result = Result(**document)
            except (TypeError, ValueError):
                return False, None
            expected_session = str(turn_dir.resolve())
            expected_event_log = f"sqlite:{turn_dir.resolve() / '.cambium' / 'events.db'}"
            if (
                result.status != "done"
                or result.exit_code != 0
                or result.session_id != expected_session
                or result.event_log_ref != expected_event_log
                or result.parent_task_id is not None
                or result.failure_reason is not None
            ):
                return False, None

        assigned_seq: int | None = None
        successful_result: tuple[int, int] | None = None
        clean_exit: tuple[int, int] | None = None
        session_success_seq: int | None = None
        session_ended_seen = False
        latest_context: ContextSeed | None = None
        latest_context_generation: int | None = None
        latest_context_seq: int | None = None
        failed_generations: set[int] = set()
        result_generations: set[int] = set()
        exit_generations: set[int] = set()
        context_tainted = False

        def clear_provisional(failed_generation: int | None = None) -> None:
            nonlocal successful_result, clean_exit, session_success_seq
            nonlocal latest_context, latest_context_generation, latest_context_seq, context_tainted
            successful_result = None
            clean_exit = None
            session_success_seq = None
            if failed_generation is None:
                latest_context = None
                latest_context_generation = None
                latest_context_seq = None
                context_tainted = True
                return
            failed_generations.add(failed_generation)
            if latest_context_generation == failed_generation:
                latest_context = None
                latest_context_generation = None
                latest_context_seq = None

        try:
            pages = iter_event_pages(event_db)
            for page in pages:
                if not isinstance(page, list):
                    return False, None
                for event in page:
                    if not isinstance(event, Mapping):
                        return False, None
                    seq = event.get("seq")
                    if type(seq) is not int or seq <= 0:
                        return False, None
                    kind = event.get("kind")
                    if not isinstance(kind, str) or not kind:
                        return False, None

                    if kind in _CONTEXT_KINDS and event.get("task_id") == _INTERACTIVE_TASK_ID:
                        if session_ended_seen and not allow_post_terminal_context:
                            context_tainted = True
                            latest_context = None
                            latest_context_generation = None
                            latest_context_seq = None
                            continue
                        generation = event.get("generation")
                        if type(generation) is not int or generation <= 0:
                            context_tainted = True
                            latest_context = None
                            latest_context_generation = None
                            latest_context_seq = None
                            continue
                        payload_task_id = _payload(event).get("task_id")
                        if payload_task_id is not None and payload_task_id != _INTERACTIVE_TASK_ID:
                            context_tainted = True
                            latest_context = None
                            latest_context_generation = None
                            latest_context_seq = None
                            continue
                        if generation in failed_generations:
                            continue
                        candidate = InteractiveSession._context_seed_from_event(
                            turn_dir,
                            event,
                            strict=True,
                        )
                        latest_context = candidate
                        latest_context_generation = generation if candidate is not None else None
                        latest_context_seq = seq if candidate is not None else None
                        context_tainted = candidate is None
                        continue

                    if kind == "task_assigned":
                        if not InteractiveSession._assignment_matches_plan(task, event):
                            if event.get("task_id") == _INTERACTIVE_TASK_ID:
                                return False, None
                            continue
                        if assigned_seq is not None:
                            return False, None
                        assigned_seq = seq
                        continue

                    target = event.get("task_id") == _INTERACTIVE_TASK_ID
                    if kind == "result" and target:
                        payload = _payload(event)
                        payload_task_id = payload.get("task_id")
                        payload_generation = payload.get("generation")
                        if payload_task_id is not None and payload_task_id != _INTERACTIVE_TASK_ID:
                            return False, None
                        status = payload.get("status", event.get("status"))
                        generation = event.get("generation")
                        if type(generation) is not int or generation <= 0:
                            return False, None
                        if payload_generation is not None and payload_generation != generation:
                            return False, None
                        if generation in result_generations:
                            return False, None
                        result_generations.add(generation)
                        if session_ended_seen:
                            return False, None
                        response_valid = payload.get("response_valid", event.get("response_valid"))
                        if status == "succeeded" and (
                            response_valid is None or response_valid is True
                        ):
                            successful_result = (seq, generation)
                            clean_exit = None
                            session_success_seq = None
                        else:
                            clear_provisional(generation)
                        continue

                    if kind == "exit" and target:
                        payload = _payload(event)
                        payload_task_id = payload.get("task_id")
                        payload_generation = payload.get("generation")
                        if payload_task_id is not None and payload_task_id != _INTERACTIVE_TASK_ID:
                            return False, None
                        reason = payload.get("reason", event.get("reason"))
                        generation = event.get("generation")
                        if type(generation) is not int or generation <= 0:
                            return False, None
                        if payload_generation is not None and payload_generation != generation:
                            return False, None
                        if generation in exit_generations:
                            return False, None
                        exit_generations.add(generation)
                        if (
                            reason in _SUCCESS_EXIT_REASONS
                            and successful_result is not None
                            and generation == successful_result[1]
                            and seq > successful_result[0]
                        ):
                            clean_exit = (seq, generation)
                        else:
                            clear_provisional(generation)
                        continue

                    if kind in _FAILURE_EVENT_KINDS and target:
                        # A recoverable merge diagnostic is followed by an
                        # explicit resolver success and is not a terminal
                        # failure for the root turn.
                        payload = _payload(event)
                        payload_task_id = payload.get("task_id")
                        if payload_task_id is not None and payload_task_id != _INTERACTIVE_TASK_ID:
                            return False, None
                        recoverable_merge = (
                            kind == "merge_failed"
                            and payload.get("internal") is True
                            and payload.get("recoverable") is True
                        )
                        if not recoverable_merge:
                            generation = event.get("generation")
                            clear_provisional(generation if type(generation) is int else None)
                        continue

                    if kind != "session_ended":
                        continue
                    if event.get("task_id") is not None:
                        return False, None
                    if session_ended_seen:
                        return False, None
                    session_ended_seen = True
                    payload = _payload(event)
                    statuses = payload.get("results")
                    successful_session = (
                        payload.get("session_status") == "ended"
                        and isinstance(statuses, Mapping)
                        and statuses.get(_INTERACTIVE_TASK_ID) == "succeeded"
                    )
                    if successful_session:
                        if (
                            successful_result is None
                            or clean_exit is None
                            or not (
                                assigned_seq is not None
                                and assigned_seq < successful_result[0] < clean_exit[0] < seq
                            )
                        ):
                            clear_provisional()
                            continue
                        session_success_seq = seq
                    else:
                        clear_provisional()
        except (OSError, StoreError, ValueError, sqlite3.Error, TypeError, RecursionError):
            return False, None

        if (
            assigned_seq is None
            or successful_result is None
            or clean_exit is None
            or session_success_seq is None
            or context_tainted
            or (
                latest_context is not None
                and latest_context_generation != successful_result[1]
            )
            or (
                latest_context is not None
                and assigned_seq is not None
                and (latest_context_seq is None or latest_context_seq <= assigned_seq)
            )
            or (context_required and latest_context is None)
        ):
            return False, None
        return True, latest_context

    @staticmethod
    def _successful_orphan_seed(
        turn_dir: Path,
        *,
        branch_generation: int,
        branch_start_turn: int,
        require_context: bool = False,
        expected_repo: Path | None = None,
    ) -> tuple[bool, ContextSeed | None]:
        """Validate one crashed-after-success turn using existing durable facts."""
        return InteractiveSession._durable_turn_evidence(
            turn_dir,
            branch_generation=branch_generation,
            branch_start_turn=branch_start_turn,
            require_context=require_context,
            expected_repo=expected_repo,
        )

    def _reconcile_successful_orphans(self) -> None:
        """Adopt only contiguous turns proven successful before frontend death."""
        adopted = False
        while True:
            turn_dir = self._turn_dir(self._turn + 1)
            if not turn_dir.exists():
                break
            successful, seed = self._successful_orphan_seed(
                turn_dir,
                branch_generation=self._branch_generation,
                branch_start_turn=self._branch_start_turn,
                require_context=False,
                expected_repo=self.repo,
            )
            if not successful:
                break
            self._turn += 1
            self._seed = seed
            adopted = True
        if adopted:
            self._pending_seed = None
            self._reconnected = True
            self._write_manifest()

    def _load_durable_head(self) -> None:
        """Recover a continuation head only from corroborated durable context events."""
        manifest_seed = self._seed
        self._seed = None
        self._last_epoch = 0
        self._last_checkpoint = None

        turn_dirs = list(self.active_turn_dirs())
        listed_sources = {
            turn_dir.resolve() for _number, turn_dir in self._listed_turn_dirs(self.root)
        }
        source_resolved = manifest_seed.source_session.resolve() if manifest_seed else None
        if manifest_seed is not None and source_resolved not in listed_sources:
            return
        if manifest_seed is not None and all(
            turn_dir.resolve() != source_resolved for turn_dir in turn_dirs
        ):
            turn_dirs.append(manifest_seed.source_session)

        latest: tuple[tuple[int, int], ContextSeed] | None = None
        manifest_anchor: ContextSeed | None = None
        manifest_anchor_seen = manifest_seed is None
        for turn_dir in turn_dirs:
            event_db = turn_dir / ".cambium" / "events.db"
            if not event_db.is_file() or event_db.is_symlink():
                continue
            try:
                pages = iter_event_pages(event_db)
            except (OSError, StoreError, ValueError, sqlite3.Error):
                continue
            source_matches_manifest = (
                manifest_seed is not None and turn_dir.resolve() == source_resolved
            )
            try:
                for page in pages:
                    for event in page:
                        if (
                            event.get("kind") not in _CONTEXT_KINDS
                            or event.get("task_id") != _INTERACTIVE_TASK_ID
                        ):
                            continue
                        payload = _payload(event)
                        payload_task_id = payload.get("task_id")
                        if payload_task_id is not None and payload_task_id != _INTERACTIVE_TASK_ID:
                            continue
                        generation = event.get("generation")
                        if (
                            isinstance(generation, bool)
                            or not isinstance(generation, int)
                            or generation <= 0
                        ):
                            continue
                        candidate = self._context_seed_from_event(turn_dir, event, strict=True)
                        if candidate is None:
                            continue
                        match = _TURN_DIR_RE.fullmatch(turn_dir.name)
                        turn_number = int(match.group(1)) if match is not None else -1
                        order = (turn_number, int(event["seq"]))
                        if latest is None or order > latest[0]:
                            latest = (order, candidate)
                        if not source_matches_manifest or manifest_seed is None:
                            continue
                        if not manifest_anchor_seen:
                            if (
                                candidate.checkpoint_ref == manifest_seed.checkpoint_ref
                                and candidate.epoch == manifest_seed.epoch
                                and candidate.descriptor == manifest_seed.descriptor
                                and candidate.provider == manifest_seed.provider
                                and candidate.model == manifest_seed.model
                            ):
                                manifest_anchor = candidate
                                manifest_anchor_seen = True
                            continue
                        if (
                            event.get("kind") == "context_epoch_advanced"
                            and manifest_anchor is not None
                            and payload.get("folded_from_epoch") == manifest_anchor.epoch
                            and candidate.epoch == manifest_anchor.epoch + 1
                        ):
                            manifest_anchor = candidate
            except (OSError, StoreError, ValueError, sqlite3.Error, TypeError, RecursionError):
                continue

        if manifest_seed is not None:
            self._seed = manifest_anchor
        if self._seed is not None:
            self._last_epoch = self._seed.epoch
            self._last_checkpoint = self._seed.checkpoint_ref
        elif latest is not None:
            # Keep diagnostics useful without promoting an unreferenced event
            # into a continuation seed.
            self._last_epoch = latest[1].epoch
            self._last_checkpoint = latest[1].checkpoint_ref

    def resume_summary(self) -> str:
        """Describe the durable state that will be attached on startup."""
        checkpoint = self._last_checkpoint or "none"
        return (
            "Detected prior interactive session; resuming durable state: "
            f"turns={self._turn} last_epoch={self._last_epoch} "
            f"last_checkpoint={checkpoint}. {self.describe()}"
        )

    def reset(self) -> None:
        """Start a fresh semantic branch while retaining old turn artifacts."""
        from .prompts import load_policy

        self._base_config = replace(self._base_config, prompt_policy=load_policy())
        self._seed = None
        self._pending_seed = None
        self._last_epoch = 0
        self._last_checkpoint = None
        self._branch_generation += 1
        self._branch_start_turn = self._turn
        self._write_manifest()

    def fork(self) -> str:
        """Start a new branch whose first turn reuses the current checkpoint."""
        if self._seed is None:
            raise InteractiveSessionError("cannot fork: no successful checkpoint is available")
        self._pending_seed = None
        self._branch_generation += 1
        self._branch_start_turn = self._turn
        self._write_manifest()
        return (
            f"forked branch generation={self._branch_generation} from "
            f"epoch={self._seed.epoch} checkpoint={self._seed.checkpoint_ref}"
        )

    def branch_heads(self) -> tuple[BranchHead, ...]:
        """Return durable checkpoint heads for every completed branch turn."""
        heads: list[BranchHead] = []
        for turn, turn_dir in self._listed_turn_dirs(self.root):
            plan_path = turn_dir / "plan.json"
            parsed_plan = InteractiveSession._interactive_plan_task(
                plan_path,
                turn=turn,
            )
            if parsed_plan is None:
                continue
            task, _requires_context = parsed_plan
            generation = task[_BRANCH_GENERATION_FIELD]
            branch_start = task[_BRANCH_START_TURN_FIELD]
            valid, seed = InteractiveSession._durable_turn_evidence(
                turn_dir,
                branch_generation=generation,
                branch_start_turn=branch_start,
                turn=turn,
                require_context=True,
                expected_repo=self.repo,
                allow_post_terminal_context=True,
            )
            if not valid or seed is None:
                continue
            current = (
                self._seed is not None
                and turn == self._turn
                and self._seed.source_session == turn_dir.resolve()
                and self._seed.checkpoint_ref == seed.checkpoint_ref
            )
            heads.append(
                BranchHead(
                    turn=turn,
                    epoch=seed.epoch,
                    checkpoint_ref=seed.checkpoint_ref,
                    source_session=turn_dir,
                    current=current,
                )
            )
        return tuple(heads)

    def eligible_provider_models(self) -> tuple[tuple[str, str], ...]:
        """Return enabled, credential-ready provider/model pairs.

        Credential readiness delegates to the same helper used by
        :func:`oneshot._resolve_provider`. This method only exposes provider
        names and configured model ids, never credential values or environment
        variable names.
        """
        from .provider_config import load_providers

        provider_path = oneshot._provider_config_path(self._base_config, self.repo)
        providers = load_providers(provider_path)
        authorized = oneshot._authorized_provider_names(providers, oneshot.AuthStore())
        return tuple(
            (candidate.name, candidate.model)
            for candidate in authorized
            if isinstance(candidate.name, str)
            and candidate.name
            and isinstance(candidate.model, str)
            and candidate.model
        )

    def _configured_model(self, provider: str) -> str | None:
        """Return a provider's declared model without checking credentials."""
        try:
            from .provider_config import load_providers

            provider_path = oneshot._provider_config_path(self._base_config, self.repo)
            for candidate in load_providers(provider_path):
                if candidate.name == provider and isinstance(candidate.model, str):
                    return candidate.model or None
        except (OSError, ValueError):
            pass
        return None

    def _has_explicit_model_preference(self) -> bool:
        """Return whether the current pair came from an interactive ``/model`` command."""
        provider = self.provider
        model = self.model
        return (
            provider is not None
            and model is not None
            and self._model_preferences.get(provider) == model
        )

    def _set_serving_preference(
        self, provider: str, model: str | None, *, force: bool = False
    ) -> None:
        """Persist an actual serving pair unless ``/model`` explicitly pinned one."""
        if not isinstance(provider, str) or not provider:
            return
        if not force and self._has_explicit_model_preference():
            if self._pending_seed is not None:
                self._pending_seed = replace(
                    self._pending_seed,
                    provider=provider,
                    model=model,
                )
            return
        changed = self._provider_preference != provider or self._model_preference != model
        self._provider_preference = provider
        self._model_preference = model
        if self._pending_seed is not None:
            self._pending_seed = replace(
                self._pending_seed,
                provider=provider,
                model=model,
            )
        if changed:
            self._write_manifest()

    def _reconcile_provider_preference(self) -> None:
        """Drop an unavailable or incompatible persisted provider/model pin."""
        provider = self.provider
        if provider is None:
            return
        try:
            from .provider_config import load_providers

            provider_path = oneshot._provider_config_path(self._base_config, self.repo)
            configured = load_providers(provider_path)
            configured_names = {
                candidate.name
                for candidate in configured
                if isinstance(candidate.name, str) and candidate.name
            }
            # Checkpoint fixtures and custom callers may carry provider names
            # that are not in the local provider file.  There is no declared
            # replacement model for those names, so leave the pair alone.
            if provider not in configured_names:
                return
            options = self.eligible_provider_models()
        except (OSError, ValueError):
            return
        if not options:
            return
        selected = next(
            ((name, model) for name, model in options if name == provider),
            options[0],
        )
        if selected[0] != provider or selected[1] != self.model:
            self._set_serving_preference(*selected, force=True)

    def _record_serving_preference(
        self,
        turn: InteractiveTurn,
        provider: str,
        model: str | None,
        *,
        force: bool = False,
    ) -> None:
        """Record a provider/model that actually served this turn."""
        self._serving_turn = turn.number
        declared_model = self._configured_model(provider)
        if declared_model is not None:
            model = declared_model
        elif not isinstance(model, str) or not model:
            model = None
        self._set_serving_preference(provider, model, force=force)

    def _serving_observation_allowed(
        self,
        provider: str,
        *,
        call_kind: Any = None,
        failure_reason: Any = None,
        fell_back_from: Any = None,
    ) -> tuple[bool, bool]:
        """Return whether serving evidence may move coding ownership and whether to force it."""
        if failure_reason is not None or call_kind == "summary":
            return False, False
        incumbent = self.provider
        if incumbent is None or provider == incumbent:
            return True, False
        genuine_fallback = (
            call_kind == "agent"
            and isinstance(fell_back_from, str)
            and fell_back_from == incumbent
        )
        return genuine_fallback, genuine_fallback

    def observe_result(self, turn: InteractiveTurn, result: Any) -> None:
        """Record only successful root coding service, including genuine fallback."""
        if not isinstance(turn, InteractiveTurn):
            raise InteractiveSessionError("interactive result requires a prepared turn")
        results = getattr(result, "results", None)
        item = results[0] if isinstance(results, tuple | list) and results else result
        if getattr(item, "task_id", None) != turn.config.task_id:
            return
        if getattr(item, "status", None) != "succeeded":
            return
        provider = getattr(item, "provider", None)
        if not isinstance(provider, str) or not provider:
            return
        model = getattr(item, "model", None)
        allowed, force = self._serving_observation_allowed(
            provider,
            call_kind="agent",
            fell_back_from=getattr(item, "fell_back_from", None),
        )
        if allowed and (
            provider != self.provider or (isinstance(model, str) and model != self.model)
        ):
            self._record_serving_preference(turn, provider, model, force=force)

    def set_model_preference(self, value: str) -> str:
        """Validate and persist a provider/model preference for later turns."""
        target = value.strip()
        if not target or any(character.isspace() for character in target):
            return "model: expected PROVIDER or PROVIDER:MODEL"

        requested_provider: str | None = None
        requested_model: str | None = None
        if ":" in target:
            requested_provider, requested_model = target.split(":", 1)
            if not requested_provider or not requested_model:
                return "model: expected PROVIDER or PROVIDER:MODEL"
        else:
            requested_provider = target

        try:
            options = self.eligible_provider_models()
            from .provider_config import load_providers

            provider_path = oneshot._provider_config_path(self._base_config, self.repo)
            configured = tuple(
                (candidate.name, candidate.model)
                for candidate in load_providers(provider_path)
                if candidate.enabled
                and isinstance(candidate.name, str)
                and candidate.name
                and isinstance(candidate.model, str)
                and candidate.model
            )
        except (OSError, ValueError) as exc:
            return f"model: provider config/auth unavailable ({exc})"

        provider_config_path = "~/.config/cambium/providers.json"
        try:
            provider_config_path = str(oneshot._provider_config_path(self._base_config, self.repo))
        except Exception:  # noqa: BLE001 - refusal guidance must never raise
            pass

        if requested_model is None:
            provider_options = [
                (provider, model) for provider, model in options if provider == requested_provider
            ]
            if provider_options:
                stored_model = self._model_preferences.get(requested_provider)
                requested_model = (
                    stored_model
                    if (requested_provider, stored_model) in provider_options
                    else provider_options[0][1]
                )
            else:
                current_provider = self.provider
                if current_provider is None:
                    if any(provider == requested_provider for provider, _model in configured):
                        return (
                            f"model: provider {requested_provider!r} is not eligible "
                            f"(disabled or credential unavailable); add/change the entry in "
                            f"{provider_config_path} then rerun /model"
                        )
                    return (
                        "model: expected an eligible provider or PROVIDER:MODEL "
                        "(routing is currently automatic)"
                    )
                requested_provider = current_provider
                requested_model = target

                if (requested_provider, requested_model) in configured:
                    options = configured

        if requested_provider is None or requested_model is None:
            return "model: expected PROVIDER or PROVIDER:MODEL"
        if (requested_provider, requested_model) not in options:
            if not any(provider == requested_provider for provider, _model in options):
                return (
                    f"model: provider {requested_provider!r} is not eligible "
                    f"(disabled or credential unavailable); add/change the entry in "
                    f"{provider_config_path} then rerun /model"
                )
            return (
                f"model: {requested_model!r} is not configured for provider "
                f"{requested_provider!r}; add/change the entry in {provider_config_path} "
                "then rerun /model"
            )

        if self.provider == requested_provider and self.model == requested_model:
            changed = (
                self._provider_preference != requested_provider
                or self._model_preference != requested_model
                or self._model_preferences.get(requested_provider) != requested_model
            )
            self._provider_preference = requested_provider
            self._model_preference = requested_model
            self._model_preferences[requested_provider] = requested_model
            if changed:
                self._write_manifest()
            return (
                f"model preference unchanged: provider={requested_provider} model={requested_model}"
            )

        self._provider_preference = requested_provider
        self._model_preference = requested_model
        self._model_preferences[requested_provider] = requested_model
        self._write_manifest()
        return (
            f"model preference set: provider={requested_provider} model={requested_model} "
            "(subsequent turns; existing context may use the semantic-trunk fallback)"
        )

    def compact(self) -> str:
        """Roll the current summary-only checkpoint into a CAST K0 checkpoint.

        This deterministic operation materializes semantic entries and retains
        the recent raw tail unchanged. It never invents a model-free summary
        or requires a paid terminal flush merely to save a usable checkpoint.
        """
        if self._seed is None:
            return "compact: no successful checkpoint is available"
        seed = self._seed
        try:
            from .worker import AgentConfig, _load_epoch_checkpoint, _write_epoch_checkpoint

            checkpoint_root = seed.source_session / ".cambium" / "checkpoints"
            config = AgentConfig(
                task_id="interactive-main",
                generation=1,
                task="interactive compaction",
                worktree=None,
                base_commit=None,
                fanout_config=None,
                max_turns=self._base_config.max_turns,
                max_tokens=self._base_config.max_tokens,
                shell_permission=True,
                network_permission=False,
                heartbeat_interval_s=1.0,
                max_wall_s=(
                    self._base_config.max_wall_s or oneshot.DEFAULT_INTERACTIVE_WALL_BUDGET_S
                ),
                checkpoint_root=checkpoint_root,
                provider_env_keys=self._base_config.provider_env_keys,
            )
            checkpoint = _load_epoch_checkpoint(config, seed.checkpoint_ref, expect_task_id=False)
            trunk, raw_tail = partition_summary_trunk(checkpoint.full_messages)
            entries = summary_entries(trunk)
            if not entries:
                return "compact: no semantic summary segments are available"
            if len(entries) == 1 and is_k0_entry(entries[0]):
                return f"compact: already at K0 epoch={checkpoint.epoch}"
            rolled_messages, _projection, _history = rollover_summary_trunk(trunk)
            cache_key = checkpoint.cache_key
            provider = cache_key.provider
            if not isinstance(provider, str) or not provider:
                return "compact: checkpoint provider is unavailable"
            provider_compat = {provider: (cache_key.protocol, cache_key.reasoning_effort)}
            rolled = _write_epoch_checkpoint(
                config,
                turn=checkpoint.turn,
                epoch=checkpoint.epoch + 1,
                provider_messages=rolled_messages,
                continuation_suffix=raw_tail,
                provider=provider,
                model=cache_key.model,
                tools_sha256=cache_key.tools_sha256,
                provider_compat=provider_compat,
                provider_boundary=cache_key.provider_boundary,
                code_changed=checkpoint.code_changed,
                verified_after_change=checkpoint.verified_after_change,
                verification_failed=checkpoint.verification_failed,
                no_progress_actions=checkpoint.no_progress_actions,
                budget_new_tokens=checkpoint.budget_new_tokens,
                previous_prompt_tokens=0,
                cumulative_usage=checkpoint.cumulative_usage,
                wall_deadline=checkpoint.wall_deadline,
            )
            if rolled is None:
                return "compact: checkpoint root is unavailable"
            descriptor = _fork_descriptor(rolled.checkpoint_ref, asdict(rolled.cache_key))
            new_seed = ContextSeed(
                source_session=seed.source_session,
                checkpoint_ref=rolled.checkpoint_ref,
                descriptor={} if descriptor is None else descriptor,
                provider=rolled.cache_key.provider,
                model=rolled.cache_key.model,
                epoch=rolled.epoch,
            )
            event_store = EventStore(seed.source_session / ".cambium" / "events.db")
            try:
                event_store.append(
                    {
                        "kind": "context_epoch_advanced",
                        "ts": time.time(),
                        "task_id": checkpoint.task_id,
                        "generation": checkpoint.generation,
                        "request_id": f"tui-compact-{time.time_ns():x}",
                        "payload": {
                            "checkpoint_ref": rolled.checkpoint_ref,
                            "epoch": rolled.epoch,
                            "turn": rolled.turn,
                            "folded_from_epoch": checkpoint.epoch,
                            "reason": "manual K0 rollover",
                            "cache_key": asdict(rolled.cache_key),
                        },
                    }
                )
            finally:
                event_store.close()
            self._seed = new_seed
            self._write_manifest()
            return (
                f"compacted: K0 rollover epoch={checkpoint.epoch}->{rolled.epoch} "
                f"checkpoint={rolled.checkpoint_ref}"
            )
        except (OSError, StoreError, SummaryTrunkError, ValueError) as exc:
            return f"compact: unavailable ({exc})"

    def _turn_dir(self, number: int) -> Path:
        return self.root / f"turn-{number:04d}"

    @staticmethod
    def _listed_turn_dirs(root: Path) -> tuple[tuple[int, Path], ...]:
        """List actual, strictly named turn directories without probing gaps."""
        try:
            children = tuple(root.iterdir())
        except OSError:
            return ()
        listed: list[tuple[int, Path]] = []
        for path in children:
            if path.is_symlink() or not path.is_dir():
                continue
            match = _TURN_DIR_RE.fullmatch(path.name)
            if match is None:
                continue
            try:
                number = int(match.group(1))
            except ValueError:
                continue
            if path.name != f"turn-{number:04d}":
                continue
            listed.append((number, path))
        listed.sort(key=lambda item: (item[0], item[1].name))
        return tuple(listed)

    def _copy_seed(self, seed: ContextSeed, session_dir: Path) -> None:
        source = _checkpoint_path(seed.source_session, seed.checkpoint_ref)
        if not source.is_file() or source.is_symlink():
            raise InteractiveSessionError("context seed checkpoint is unavailable")
        destination = _checkpoint_path(session_dir, seed.checkpoint_ref)
        destination.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        if destination.exists():
            raise InteractiveSessionError("turn checkpoint destination already exists")
        shutil.copyfile(source, destination)
        os.chmod(destination, 0o600)

    def prepare_turn(self, prompt: str) -> InteractiveTurn:
        """Allocate one new supervisor leaf and attach the latest context seed."""
        if not isinstance(prompt, str) or not prompt.strip():
            raise InteractiveSessionError("interactive prompt must be non-empty")
        self._reconcile_provider_preference()
        number = self._turn + 1
        session_dir = self._turn_dir(number)
        if session_dir.exists():
            raise InteractiveSessionError(
                f"cannot prepare turn {number}: an unreconciled durable turn directory exists"
            )
        session_dir.mkdir(parents=True, exist_ok=False, mode=0o700)
        # Publish the interactive type marker before supervisor work starts.
        # The accepted-turn pointer remains unchanged until complete_turn or
        # conservative reconnect reconciliation proves this leaf succeeded.
        self._write_manifest()
        config = replace(
            self._base_config,
            session_root=session_dir,
            session_mode=SessionMode.NEW,
        )
        context_fork: dict[str, Any] | None = None
        summary_trunk_ref: str | None = None
        if self._seed is not None:
            self._copy_seed(self._seed, session_dir)
            summary_trunk_ref = self._seed.checkpoint_ref
            if self._seed.descriptor:
                context_fork = copy.deepcopy(self._seed.descriptor)
            changes: dict[str, Any] = {}
            if self._seed.provider is not None:
                changes["provider"] = self._seed.provider
                changes["assigned_provider"] = self._seed.provider
            if self._seed.model is not None:
                changes["model"] = self._seed.model
            if changes:
                config = replace(config, **changes)
        changes = {}
        if self._provider_preference is not None:
            changes.update(
                {
                    "provider": self._provider_preference,
                    "assigned_provider": self._provider_preference,
                }
            )
        if self._model_preference is not None:
            changes["model"] = self._model_preference
        if changes:
            config = replace(config, **changes)
        if self._seed is not None and (
            self.provider != self._seed.provider or self.model != self._seed.model
        ):
            context_fork = None
        self._pending_seed = None
        return InteractiveTurn(
            number=number,
            session_dir=session_dir,
            config=replace(
                config,
                prompt=prompt,
                task_id="interactive-main",
                worktree_path=session_dir / "wt",
                branch=None,
            ),
            context_fork=copy.deepcopy(context_fork),
            summary_trunk_ref=summary_trunk_ref,
            branch_generation=self._branch_generation,
            branch_start_turn=self._branch_start_turn,
        )

    async def run_turn(
        self,
        turn: InteractiveTurn,
        *,
        on_event=None,
        max_concurrent_tasks: int | None = None,
    ):
        """Run one prepared leaf in a supervisor plan.

        This mirrors :func:`oneshot.run_oneshot` only at the frontend adapter
        boundary, then adds the two context-link fields that ordinary one-shot
        callers intentionally do not expose. Provider resolution, credentials,
        admission, workers, events, merge publication, and result construction
        remain owned by the existing oneshot/supervisor path.
        """
        if not isinstance(turn, InteractiveTurn):
            raise InteractiveSessionError("interactive run requires a prepared turn")
        if (
            turn.branch_generation != self._branch_generation
            or turn.branch_start_turn != self._branch_start_turn
        ):
            raise InteractiveSessionError("prepared turn belongs to another interactive branch")
        session_dir = turn.session_dir
        repo = self.repo
        oneshot.preflight(turn.config, repo, session_dir)
        oneshot.admit_session(turn.config, session_dir)
        resolved, provider_environment = oneshot._resolve_provider(turn.config, repo)
        task = oneshot.build_plan(resolved, repo, session_dir)["tasks"][0]
        task[_BRANCH_GENERATION_FIELD] = turn.branch_generation
        task[_BRANCH_START_TURN_FIELD] = turn.branch_start_turn
        task["context_reuse"] = resolved.context_reuse
        if turn.context_fork is not None:
            task["context_fork"] = copy.deepcopy(turn.context_fork)
        if turn.summary_trunk_ref is not None:
            task["summary_trunk_ref"] = turn.summary_trunk_ref
        routing_state_path = (
            resolved.routing_state_path
            if resolved.routing_state_path is not None
            else repo / ".cambium" / "routing-state.json"
        )
        kwargs: dict[str, Any] = {
            "on_event": on_event,
            "routing_state_path": routing_state_path,
            "reject_reused_session": True,
            "context_reuse": resolved.context_reuse,
        }
        if max_concurrent_tasks is not None:
            kwargs["max_concurrent_tasks"] = max_concurrent_tasks
        if provider_environment:
            kwargs["provider_environment"] = provider_environment
        result = await supervisor.run_plan(session_dir, {"tasks": [task]}, **kwargs)
        self.observe_result(turn, result)
        return result

    def observe_event(self, turn: InteractiveTurn, event: Mapping[str, Any]) -> None:
        """Capture root coding provenance and the newest durable checkpoint."""
        if not isinstance(turn, InteractiveTurn) or event.get("task_id") != _INTERACTIVE_TASK_ID:
            return
        kind = event.get("kind")
        payload = _payload(event)
        if kind in {"usage_event", "result"}:
            serving = payload.get("provider_metadata") if kind == "result" else payload
            if not isinstance(serving, Mapping):
                serving = payload
            if kind == "result" and payload.get("status") != "succeeded":
                return
            provider = serving.get("provider")
            model = serving.get("model")
            if isinstance(provider, str) and provider:
                call_kind = payload.get("call_kind", serving.get("call_kind"))
                failure_reason = serving.get(
                    "failure_reason", payload.get("failure_reason")
                )
                allowed, force = self._serving_observation_allowed(
                    provider,
                    call_kind=call_kind,
                    failure_reason=failure_reason,
                    fell_back_from=serving.get("fell_back_from"),
                )
                model_value = model if isinstance(model, str) and model else None
                if allowed and (provider != self.provider or model_value != self.model):
                    self._record_serving_preference(
                        turn,
                        provider,
                        model_value,
                        force=force,
                    )
            return
        if kind not in _CONTEXT_KINDS:
            return
        payload_task_id = payload.get("task_id")
        if payload_task_id is not None and payload_task_id != _INTERACTIVE_TASK_ID:
            return
        generation = event.get("generation")
        if isinstance(generation, bool) or not isinstance(generation, int) or generation <= 0:
            return
        seed = self._context_seed_from_event(turn.session_dir, event, strict=True)
        if seed is None:
            return
        self._pending_seed = seed
        if (
            self._serving_turn != turn.number
            and seed.provider is not None
            and seed.model is not None
        ):
            self._set_serving_preference(seed.provider, seed.model)
        if seed.epoch >= self._last_epoch:
            self._last_epoch = seed.epoch
            self._last_checkpoint = seed.checkpoint_ref

    def complete_turn(self, turn: InteractiveTurn, *, succeeded: bool) -> None:
        """Publish the captured checkpoint as the next branch head."""
        if not isinstance(turn, InteractiveTurn):
            raise InteractiveSessionError("interactive completion requires a prepared turn")
        number = turn.number
        if number <= self._turn:
            raise InteractiveSessionError("interactive turns must complete in order")
        self._turn = number
        if succeeded:
            self._seed = self._pending_seed
        self._pending_seed = None
        self._write_manifest()

    def describe(self) -> str:
        seed = self._seed
        provider = self.provider or "auto"
        model = self.model or "auto"
        checkpoint = seed.checkpoint_ref if seed is not None else "none"
        epoch = seed.epoch if seed is not None else 0
        return (
            f"session={self.root} turn={self._turn} branch={self._branch_generation} "
            f"provider={provider} model={model} epoch={epoch} checkpoint={checkpoint}"
        )


__all__ = [
    "BranchHead",
    "ContextSeed",
    "InteractiveSession",
    "InteractiveSessionError",
    "InteractiveTurn",
]
