"""Pure worker-pool state-machine scenarios.

These tests cover the seed state boundary only.  They do not start
subprocesses, speak the worker protocol, or establish M7 acceptance.
"""

from __future__ import annotations

from dataclasses import FrozenInstanceError

import pytest

from cambium.worker_pool import (
    InvalidTransitionError,
    NoReadyWorkerError,
    PoolClosedError,
    PoolConfig,
    PoolConfigurationError,
    PoolIntent,
    PoolState,
    WorkerState,
)


def _ready_pool(worker_count: int = 3) -> PoolState:
    state = PoolState(config=PoolConfig(max_width=8))
    for index in range(worker_count):
        worker_id = f"worker-{index}"
        state = state.start_worker(worker_id).state
        state = state.worker_ready(worker_id).state
    return state


def test_worker_states_are_explicit_and_pool_target_defaults_to_three() -> None:
    assert {state.name for state in WorkerState} == {
        "STARTING",
        "READY",
        "BUSY",
        "RESETTING",
        "RETIRING",
        "DEAD",
    }
    assert PoolConfig().target_idle_ready == 3
    assert _ready_pool().ready_count == 3


def test_acquire_is_pure_and_returns_background_refill_intent() -> None:
    before = _ready_pool()

    transition = before.acquire("task-1")

    assert before.worker("worker-0").state is WorkerState.READY
    assert transition.worker_id == "worker-0"
    assert transition.state.worker("worker-0").state is WorkerState.BUSY
    assert transition.state.worker("worker-0").task_id == "task-1"
    assert transition.intents == (PoolIntent.REFILL,)
    assert transition.refill_requested
    assert transition.state.ready_count == 2


def test_reset_failure_moves_busy_worker_through_retiring_to_dead() -> None:
    acquired = _ready_pool().acquire("task-1")
    resetting = acquired.state.release(acquired.worker_id).state
    retiring = resetting.reset_failed(acquired.worker_id).state

    assert resetting.worker(acquired.worker_id).state is WorkerState.RESETTING
    assert retiring.worker(acquired.worker_id).state is WorkerState.RETIRING
    assert retiring.retire_complete(acquired.worker_id).state.worker(
        acquired.worker_id
    ).state is WorkerState.DEAD


def test_successful_reset_returns_worker_to_ready() -> None:
    acquired = _ready_pool().acquire("task-1")
    resetting = acquired.state.release(acquired.worker_id).state

    ready = resetting.reset_succeeded(acquired.worker_id).state

    assert ready.worker(acquired.worker_id).state is WorkerState.READY
    assert ready.ready_count == 3


def test_shutdown_prevents_admission_and_refill() -> None:
    closed = _ready_pool().shutdown()

    assert closed.intents == ()
    assert closed.state.shutting_down
    assert all(worker.state is WorkerState.RETIRING for worker in closed.state.workers)
    with pytest.raises(PoolClosedError):
        closed.state.acquire("task-after-shutdown")


def test_production_rejects_wide_disabled_pool_without_override() -> None:
    with pytest.raises(PoolConfigurationError, match="max_width >= 4"):
        PoolConfig(max_width=4, pool_enabled=False)

    assert PoolConfig(
        max_width=4,
        pool_enabled=False,
        development_override=True,
    ).pool_enabled is False
    assert PoolConfig(
        max_width=4,
        pool_enabled=False,
        production=False,
    ).pool_enabled is False


def test_invalid_lifecycle_events_do_not_get_silently_accepted() -> None:
    state = _ready_pool()
    for index in range(3):
        state = state.acquire(f"task-{index}").state

    with pytest.raises(NoReadyWorkerError):
        state.acquire("task-after-capacity")

    with pytest.raises(InvalidTransitionError):
        state.reset_failed("worker-0")


def test_state_dataclasses_are_frozen_and_slot_based() -> None:
    state = _ready_pool()

    assert not hasattr(state, "__dict__")
    with pytest.raises(FrozenInstanceError):
        state.shutting_down = True
