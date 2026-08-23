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
import shutil
from dataclasses import dataclass, replace
from pathlib import Path
from typing import Any, Mapping

from . import oneshot, supervisor
from .oneshot import OneShotConfig, RoutingMode, SessionMode

_INTERACTIVE_SCHEMA = 1
_MANIFEST_NAME = "interactive.json"
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


class InteractiveSession:
    """Single-writer semantic branch spanning many one-shot supervisor leaves."""

    def __init__(self, config: OneShotConfig) -> None:
        self._base_config = config
        self.repo = oneshot.resolve_repo(config.repo)
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
        self._turn = 0
        self._branch_generation = 1
        self._branch_start_turn = 0
        self._seed: ContextSeed | None = None
        self._pending_seed: ContextSeed | None = None
        self._load_manifest()

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
            self._turn_dir(number)
            for number in range(self._branch_start_turn + 1, self._turn + 1)
            if self._turn_dir(number).is_dir()
        )

    @property
    def provider(self) -> str | None:
        return self._seed.provider if self._seed is not None else self._base_config.provider

    @property
    def model(self) -> str | None:
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
        }

    def _write_manifest(self) -> None:
        _atomic_json(self._manifest_path, self._manifest_document())

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
        if type(generation) is not int or generation < 1:
            raise InteractiveSessionError("interactive manifest generation is invalid")
        if type(branch_start) is not int or not 0 <= branch_start <= turn:
            raise InteractiveSessionError("interactive manifest branch start is invalid")
        self._turn = turn
        self._branch_generation = generation
        self._branch_start_turn = branch_start
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

    def reset(self) -> None:
        """Start a fresh semantic branch while retaining old turn artifacts."""
        self._seed = None
        self._pending_seed = None
        self._branch_generation += 1
        self._branch_start_turn = self._turn
        self._write_manifest()

    def _turn_dir(self, number: int) -> Path:
        return self.root / f"turn-{number:04d}"

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
        return await supervisor.run_plan(turn.session_dir, plan, **kwargs)

    def observe_event(self, turn: InteractiveTurn, event: Mapping[str, Any]) -> None:
        """Capture the newest durable checkpoint event for the current turn."""
        if event.get("kind") not in _CONTEXT_KINDS:
            return
        payload = _payload(event)
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
    "ContextSeed",
    "InteractiveSession",
    "InteractiveSessionError",
    "InteractiveTurn",
]
