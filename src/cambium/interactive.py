"""Persistent interactive-session coordination for REPL and TUI frontends.

The supervisor still owns one immutable worker session per submitted prompt.
This module links those leaves into one long semantic branch by carrying the
latest immutable context checkpoint forward.  A cache-compatible continuation
uses the exact ``context_fork`` descriptor and provider/model lease; the same
checkpoint is also supplied as ``summary_trunk_ref`` so an incompatible provider
can still recover the provider-neutral semantic trunk without pretending that
its KV cache is warm.

The coordinator is deliberately small and single-writer.  Frontends call
``prepare_turn`` -> ``observe_event`` -> ``complete_turn`` serially.  No worker
or renderer mutates the branch head directly.
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
from .oneshot import OneShotConfig, RoutingMode, SessionMode
from .store import EventStore, StoreError, read_events_file
from .summary_trunk import (
    SummaryTrunkError,
    is_k0_entry,
    partition_summary_trunk,
    rollover_summary_trunk,
    summary_entries,
)

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
    candidate = (root / relative).resolve()
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
    except (OSError, UnicodeDecodeError, json.JSONDecodeError):
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
    except (OSError, UnicodeDecodeError, json.JSONDecodeError):
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
            and 1 <= turn <= max_listed_turn + _MANIFEST_TURN_MARGIN
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
        if provider_preference is not None and model_preference is not None:
            self._model_preferences.setdefault(provider_preference, model_preference)
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

    def _load_durable_head(self) -> None:
        """Read the newest durable checkpoint for reconnect diagnostics."""
        if self._seed is not None:
            self._last_epoch = self._seed.epoch
            self._last_checkpoint = self._seed.checkpoint_ref
        for turn_dir in self.active_turn_dirs():
            event_db = turn_dir / ".cambium" / "events.db"
            if not event_db.is_file():
                continue
            try:
                events = read_events_file(event_db)
            except (OSError, StoreError, ValueError, sqlite3.Error):
                continue
            for event in events:
                if event.get("kind") not in _CONTEXT_KINDS:
                    continue
                payload = _payload(event)
                checkpoint_ref = payload.get("checkpoint_ref")
                epoch = payload.get("epoch")
                if not isinstance(checkpoint_ref, str) or type(epoch) is not int or epoch < 0:
                    continue
                try:
                    checkpoint = _checkpoint_path(turn_dir, checkpoint_ref)
                except InteractiveSessionError:
                    continue
                if not checkpoint.is_file() or epoch < self._last_epoch:
                    continue
                self._last_epoch = epoch
                self._last_checkpoint = checkpoint_ref

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
        """Replay turn event stores and return their latest checkpoint heads."""
        heads: list[BranchHead] = []
        for turn, turn_dir in self._listed_turn_dirs(self.root):
            event_db = turn_dir / ".cambium" / "events.db"
            if not event_db.is_file():
                continue
            latest: tuple[int, str] | None = None
            try:
                events = read_events_file(event_db)
            except (OSError, ValueError, StoreError):
                continue
            for event in events:
                if event.get("kind") not in {"context_checkpoint", "context_epoch_advanced"}:
                    continue
                payload = event.get("payload")
                if not isinstance(payload, Mapping):
                    continue
                checkpoint_ref = payload.get("checkpoint_ref")
                epoch = payload.get("epoch")
                if (
                    not isinstance(checkpoint_ref, str)
                    or not checkpoint_ref
                    or type(epoch) is not int
                    or epoch < 0
                ):
                    continue
                latest = (epoch, checkpoint_ref)
            if latest is None:
                continue
            current = (
                self._seed is not None
                and turn == self._turn
                and self._seed.source_session == turn_dir.resolve()
                and self._seed.checkpoint_ref == latest[1]
            )
            heads.append(
                BranchHead(
                    turn=turn,
                    epoch=latest[0],
                    checkpoint_ref=latest[1],
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

    def _set_serving_preference(self, provider: str, model: str | None) -> None:
        """Persist the pair that actually served, without erasing /model history."""
        if not isinstance(provider, str) or not provider:
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
            self._set_serving_preference(*selected)

    def _record_serving_preference(
        self, turn: InteractiveTurn, provider: str, model: str | None
    ) -> None:
        """Record a provider/model that actually served this turn."""
        self._serving_turn = turn.number
        declared_model = self._configured_model(provider)
        if declared_model is not None:
            model = declared_model
        elif not isinstance(model, str) or not model:
            model = None
        self._set_serving_preference(provider, model)

    def observe_result(self, turn: InteractiveTurn, result: Any) -> None:
        """Record a terminal serving pair, including router fallback provenance."""
        results = getattr(result, "results", None)
        item = results[0] if isinstance(results, tuple | list) and results else result
        provider = getattr(item, "provider", None)
        if not isinstance(provider, str) or not provider:
            return
        model = getattr(item, "model", None)
        if provider != self.provider or (isinstance(model, str) and model != self.model):
            self._record_serving_preference(turn, provider, model)

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
            if self._model_preferences.get(requested_provider) != requested_model:
                self._model_preferences[requested_provider] = requested_model
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

        Normal semantic summary flushing is performed by the provider-backed
        worker at a successful turn boundary.  This operator path therefore
        refuses a checkpoint that still has a raw tail rather than inventing a
        model-free summary, then performs the local K0 rollover check.
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
                max_wall_s=self._base_config.max_wall_s,
                checkpoint_root=checkpoint_root,
                provider_env_keys=self._base_config.provider_env_keys,
            )
            checkpoint = _load_epoch_checkpoint(config, seed.checkpoint_ref, expect_task_id=False)
            trunk, raw_tail = partition_summary_trunk(checkpoint.full_messages)
            if raw_tail:
                return (
                    "compact: semantic flush is pending; raw tail remains and "
                    "requires a provider turn"
                )
            entries = summary_entries(trunk)
            if not entries:
                return "compact: no semantic summary segments are available"
            if len(entries) == 1 and is_k0_entry(entries[0]):
                return f"compact: already at K0 epoch={checkpoint.epoch}"
            rolled_messages, _projection, _history = rollover_summary_trunk(trunk)
            cache_key = checkpoint.cache_key
            provider = cache_key.provider
            provider_compat = {provider: (cache_key.protocol, cache_key.reasoning_effort)}
            rolled = _write_epoch_checkpoint(
                config,
                turn=checkpoint.turn,
                epoch=checkpoint.epoch + 1,
                provider_messages=rolled_messages,
                continuation_suffix=[],
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
                previous_prompt_tokens=checkpoint.previous_prompt_tokens,
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
        while session_dir.exists():
            number += 1
            session_dir = self._turn_dir(number)
        session_dir.mkdir(parents=True, exist_ok=False, mode=0o700)
        config = replace(
            self._base_config,
            prompt=prompt,
            session_root=session_dir,
            task_id="interactive-main",
            worktree_path=session_dir / "wt",
            branch=None,
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
                changes["routing_mode"] = RoutingMode.CASCADE
                changes["auto"] = False
            if self._seed.model is not None:
                changes["model"] = self._seed.model
            if changes:
                config = replace(config, **changes)
        changes: dict[str, Any] = {}
        if self._provider_preference is not None:
            changes.update(
                {
                    "provider": self._provider_preference,
                    "assigned_provider": self._provider_preference,
                    "routing_mode": RoutingMode.CASCADE,
                    "auto": False,
                }
            )
        if self._model_preference is not None:
            changes["model"] = self._model_preference
        if changes:
            config = replace(config, **changes)
        self._pending_seed = None
        return InteractiveTurn(
            number=number,
            session_dir=session_dir,
            config=config,
            context_fork=context_fork,
            summary_trunk_ref=summary_trunk_ref,
        )

    async def run_turn(self, turn: InteractiveTurn, *, on_event=None):
        """Run one prepared leaf through the canonical supervisor runtime.

        This mirrors :func:`oneshot.run_oneshot` only at the frontend adapter
        boundary, then adds the two context-link fields that ordinary one-shot
        callers intentionally do not expose. Provider resolution, credentials,
        admission, workers, events, merge publication, and result construction
        remain owned by the existing oneshot/supervisor path.
        """
        config = turn.config
        repo = self.repo
        oneshot.preflight(config, repo, turn.session_dir)
        oneshot.admit_session(config, turn.session_dir)
        resolved, provider_environment = oneshot._resolve_provider(config, repo)
        plan = oneshot.build_plan(resolved, repo, turn.session_dir)
        task = plan["tasks"][0]
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
        if provider_environment:
            kwargs["provider_environment"] = provider_environment
        result = await supervisor.run_plan(turn.session_dir, plan, **kwargs)
        self.observe_result(turn, result)
        return result

    def observe_event(self, turn: InteractiveTurn, event: Mapping[str, Any]) -> None:
        """Capture serving provenance and the newest durable checkpoint."""
        kind = event.get("kind")
        payload = _payload(event)
        if kind in {"usage_event", "result"}:
            serving = payload.get("provider_metadata") if kind == "result" else payload
            if not isinstance(serving, Mapping):
                serving = payload
            provider = serving.get("provider")
            model = serving.get("model")
            if isinstance(provider, str) and provider:
                self._record_serving_preference(
                    turn,
                    provider,
                    model if isinstance(model, str) and model else None,
                )
            return
        if kind not in _CONTEXT_KINDS:
            return
        checkpoint_ref = payload.get("checkpoint_ref")
        cache_key = payload.get("cache_key")
        if not isinstance(checkpoint_ref, str) or not checkpoint_ref:
            return
        if not isinstance(cache_key, Mapping):
            return
        try:
            checkpoint = _checkpoint_path(turn.session_dir, checkpoint_ref)
        except InteractiveSessionError:
            return
        if not checkpoint.is_file() or checkpoint.is_symlink():
            return
        descriptor = _fork_descriptor(checkpoint_ref, cache_key)
        provider = cache_key.get("provider")
        model = cache_key.get("model")
        epoch = payload.get("epoch", 0)
        self._pending_seed = ContextSeed(
            source_session=turn.session_dir,
            checkpoint_ref=checkpoint_ref,
            descriptor={} if descriptor is None else descriptor,
            provider=provider if isinstance(provider, str) and provider else None,
            model=model if isinstance(model, str) and model else None,
            epoch=epoch if type(epoch) is int and epoch >= 0 else 0,
        )
        if self._serving_turn == turn.number:
            self._pending_seed = replace(
                self._pending_seed,
                provider=self.provider,
                model=self.model,
            )
        if (
            self._serving_turn != turn.number
            and isinstance(provider, str)
            and provider
            and isinstance(model, str)
            and model
        ):
            self._set_serving_preference(provider, model)
        if self._pending_seed.epoch >= self._last_epoch:
            self._last_epoch = self._pending_seed.epoch
            self._last_checkpoint = checkpoint_ref

    def complete_turn(self, turn: InteractiveTurn, *, succeeded: bool) -> None:
        """Publish the captured checkpoint as the next branch head."""
        if turn.number <= self._turn:
            raise InteractiveSessionError("interactive turns must complete in order")
        self._turn = turn.number
        if succeeded and self._pending_seed is not None:
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
