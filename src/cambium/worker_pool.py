"""Pure worker-pool lifecycle state machine.

The state machine models pool admission and worker retirement without doing
any I/O.  A caller owns subprocesses, protocol messages, and the background
refill runner; this module only returns a new immutable state and intents.
This is a seed for the pool boundary, not M7 acceptance evidence.
"""

from __future__ import annotations

from dataclasses import dataclass, field, replace
from enum import StrEnum


class WorkerState(StrEnum):
    """Lifecycle state of one reusable worker slot."""

    STARTING = "STARTING"
    READY = "READY"
    BUSY = "BUSY"
    RESETTING = "RESETTING"
    RETIRING = "RETIRING"
    DEAD = "DEAD"


class PoolIntent(StrEnum):
    """Side-effect request returned by a pure state transition."""

    REFILL = "REFILL"


class PoolError(RuntimeError):
    """Base error for invalid pool operations."""


class PoolConfigurationError(ValueError):
    """A pool configuration violates a production policy."""


class PoolClosedError(PoolError):
    """An operation was attempted after pool shutdown."""


class PoolDisabledError(PoolError):
    """A pool operation was attempted while the pool is disabled."""


class NoReadyWorkerError(PoolError):
    """Admission was requested but no worker is READY."""


class InvalidTransitionError(PoolError):
    """A worker did not have the state required by a transition."""


def _require_non_empty_string(value: object, name: str) -> str:
    if not isinstance(value, str):
        raise TypeError(f"{name} must be a string")
    if not value:
        raise ValueError(f"{name} must be non-empty")
    return value


def validate_pool_config(
    max_width: int,
    pool_enabled: bool,
    *,
    production: bool = True,
    development_override: bool = False,
) -> None:
    """Validate the width/pool policy at the production configuration edge.

    A disabled pool is allowed for narrow sessions.  In production, a
    session with ``max_width >= 4`` must use the reusable pool unless the
    caller explicitly opts into the development override.
    """
    if isinstance(max_width, bool) or not isinstance(max_width, int):
        raise TypeError("max_width must be an integer")
    if max_width < 1:
        raise ValueError("max_width must be at least 1")
    if not isinstance(pool_enabled, bool):
        raise TypeError("pool_enabled must be a boolean")
    if not isinstance(production, bool):
        raise TypeError("production must be a boolean")
    if not isinstance(development_override, bool):
        raise TypeError("development_override must be a boolean")

    if production and not pool_enabled and max_width >= 4 and not development_override:
        raise PoolConfigurationError(
            "production requires the worker pool when max_width >= 4; "
            "set development_override=True only for development"
        )


@dataclass(frozen=True, slots=True)
class PoolConfig:
    """Immutable policy used by one pool state machine."""

    max_width: int = 8
    pool_enabled: bool = True
    production: bool = True
    development_override: bool = False
    target_idle_ready: int = 3

    def __post_init__(self) -> None:
        validate_pool_config(
            self.max_width,
            self.pool_enabled,
            production=self.production,
            development_override=self.development_override,
        )
        if isinstance(self.target_idle_ready, bool) or not isinstance(self.target_idle_ready, int):
            raise TypeError("target_idle_ready must be an integer")
        if self.target_idle_ready < 1:
            raise ValueError("target_idle_ready must be at least 1")


@dataclass(frozen=True, slots=True)
class WorkerSlot:
    """Immutable state for one worker identity."""

    worker_id: str
    state: WorkerState = WorkerState.STARTING
    task_id: str | None = None

    def __post_init__(self) -> None:
        _require_non_empty_string(self.worker_id, "worker_id")
        if not isinstance(self.state, WorkerState):
            raise TypeError("state must be a WorkerState")
        if self.task_id is not None:
            _require_non_empty_string(self.task_id, "task_id")
        if self.state is WorkerState.BUSY and self.task_id is None:
            raise ValueError("BUSY worker must have a task_id")
        if self.state is not WorkerState.BUSY and self.task_id is not None:
            raise ValueError("only a BUSY worker may have a task_id")


@dataclass(frozen=True, slots=True)
class PoolTransition:
    """Result of one pure operation.

    ``state`` is always a new immutable state.  ``worker_id`` is populated by
    ``acquire`` and is otherwise ``None``.  Intents are advisory effects for
    an owner outside this module; the state machine never executes them.
    """

    state: PoolState
    intents: tuple[PoolIntent, ...] = ()
    worker_id: str | None = None

    def __post_init__(self) -> None:
        if not isinstance(self.state, PoolState):
            raise TypeError("state must be a PoolState")
        if not isinstance(self.intents, tuple):
            raise TypeError("intents must be a tuple")
        if not all(isinstance(intent, PoolIntent) for intent in self.intents):
            raise TypeError("intents must contain PoolIntent values")
        if self.worker_id is not None:
            _require_non_empty_string(self.worker_id, "worker_id")

    @property
    def refill_requested(self) -> bool:
        """Whether this transition asks an owner to refill in the background."""
        return PoolIntent.REFILL in self.intents


@dataclass(frozen=True, slots=True)
class PoolState:
    """Immutable pool state with pure worker lifecycle transitions."""

    config: PoolConfig = field(default_factory=PoolConfig)
    workers: tuple[WorkerSlot, ...] = ()
    shutting_down: bool = False

    def __post_init__(self) -> None:
        if not isinstance(self.config, PoolConfig):
            raise TypeError("config must be a PoolConfig")
        if not isinstance(self.workers, tuple):
            raise TypeError("workers must be a tuple")
        if not all(isinstance(worker, WorkerSlot) for worker in self.workers):
            raise TypeError("workers must contain WorkerSlot values")
        worker_ids = [worker.worker_id for worker in self.workers]
        if len(worker_ids) != len(set(worker_ids)):
            raise ValueError("worker_id values must be unique")
        if not isinstance(self.shutting_down, bool):
            raise TypeError("shutting_down must be a boolean")

    @property
    def target_idle_ready(self) -> int:
        """Configured number of idle READY workers to keep available."""
        return self.config.target_idle_ready

    @property
    def ready_count(self) -> int:
        """Number of workers currently available for admission."""
        return sum(worker.state is WorkerState.READY for worker in self.workers)

    @property
    def idle_ready_count(self) -> int:
        """Alias for the READY count used by the refill policy."""
        return self.ready_count

    def worker(self, worker_id: str) -> WorkerSlot:
        """Return one slot by identity."""
        index = self._worker_index(worker_id)
        return self.workers[index]

    def start_worker(self, worker_id: str) -> PoolTransition:
        """Add a worker in ``STARTING`` state."""
        if self.shutting_down:
            raise PoolClosedError("cannot start a worker after shutdown")
        if not self.config.pool_enabled:
            raise PoolDisabledError("cannot start a worker while the pool is disabled")
        worker_id = _require_non_empty_string(worker_id, "worker_id")
        if any(worker.worker_id == worker_id for worker in self.workers):
            raise ValueError(f"worker_id {worker_id!r} already exists")
        next_state = replace(self, workers=(*self.workers, WorkerSlot(worker_id)))
        return PoolTransition(next_state, worker_id=worker_id)

    def worker_ready(self, worker_id: str) -> PoolTransition:
        """Move one ``STARTING`` worker to ``READY``."""
        return self._move_worker(worker_id, WorkerState.STARTING, WorkerState.READY)

    def acquire(self, task_id: str) -> PoolTransition:
        """Assign the first READY worker and request an asynchronous refill.

        Admission never starts a worker inline.  When the assignment leaves
        fewer than ``target_idle_ready`` READY workers, the returned intent
        asks the pool owner to refill in the background.
        """
        if self.shutting_down:
            raise PoolClosedError("cannot acquire a worker after shutdown")
        if not self.config.pool_enabled:
            raise PoolDisabledError("cannot acquire from a disabled pool")
        task_id = _require_non_empty_string(task_id, "task_id")

        index = next(
            (index for index, worker in enumerate(self.workers)
             if worker.state is WorkerState.READY),
            None,
        )
        if index is None:
            raise NoReadyWorkerError("no READY worker is available")

        worker = self.workers[index]
        assigned = WorkerSlot(worker.worker_id, WorkerState.BUSY, task_id)
        workers = (*self.workers[:index], assigned, *self.workers[index + 1 :])
        next_state = replace(self, workers=workers)
        intents = (
            (PoolIntent.REFILL,)
            if next_state.idle_ready_count < next_state.target_idle_ready
            else ()
        )
        return PoolTransition(next_state, intents=intents, worker_id=worker.worker_id)

    def release(self, worker_id: str) -> PoolTransition:
        """Move a BUSY worker to RESETTING after its task completes."""
        return self._move_worker(worker_id, WorkerState.BUSY, WorkerState.RESETTING)

    def reset_succeeded(self, worker_id: str) -> PoolTransition:
        """Return a successfully reset worker to READY."""
        return self._move_worker(worker_id, WorkerState.RESETTING, WorkerState.READY)

    def reset_failed(self, worker_id: str) -> PoolTransition:
        """Retire a worker when reset verification fails."""
        return self._move_worker(worker_id, WorkerState.RESETTING, WorkerState.RETIRING)

    def retire_complete(self, worker_id: str) -> PoolTransition:
        """Mark a retired worker DEAD after its owner stops it."""
        return self._move_worker(worker_id, WorkerState.RETIRING, WorkerState.DEAD)

    def shutdown(self) -> PoolTransition:
        """Close admission and retire every non-DEAD worker.

        Shutdown returns no refill intent.  A subsequent acquire is rejected,
        so shutdown cannot create new background refill work.
        """
        if self.shutting_down:
            return PoolTransition(self)
        workers = tuple(
            worker
            if worker.state is WorkerState.DEAD
            else WorkerSlot(worker.worker_id, WorkerState.RETIRING)
            for worker in self.workers
        )
        next_state = replace(self, workers=workers, shutting_down=True)
        return PoolTransition(next_state)

    def _worker_index(self, worker_id: str) -> int:
        worker_id = _require_non_empty_string(worker_id, "worker_id")
        for index, worker in enumerate(self.workers):
            if worker.worker_id == worker_id:
                return index
        raise KeyError(f"unknown worker_id {worker_id!r}")

    def _move_worker(
        self,
        worker_id: str,
        expected: WorkerState,
        replacement: WorkerState,
    ) -> PoolTransition:
        index = self._worker_index(worker_id)
        worker = self.workers[index]
        if worker.state is not expected:
            raise InvalidTransitionError(
                f"worker {worker.worker_id!r} is {worker.state}, expected {expected}"
            )
        moved = WorkerSlot(worker.worker_id, replacement)
        workers = (*self.workers[:index], moved, *self.workers[index + 1 :])
        return PoolTransition(replace(self, workers=workers), worker_id=worker.worker_id)
