"""Contract tests for the opt-in supervisor warm-worker pool."""

from __future__ import annotations

import asyncio
from typing import Any, cast

from cambium import supervisor as supervisor_module


class _FakeProcess:
    returncode: int | None = None


def test_positive_pool_bound_rebinds_and_zero_bound_does_not() -> None:
    command = ["python", "-m", "cambium.worker"]
    environment = {"CAMBIUM_TEST": "same"}

    enabled = object.__new__(supervisor_module._Runtime)
    enabled._warm_pool_size = 1
    enabled._pool = []
    process = _FakeProcess()
    asyncio.run(enabled._pool_return(cast(Any, process), command, environment))

    assert enabled._pool_pop(command, environment) is process

    disabled = object.__new__(supervisor_module._Runtime)
    disabled._warm_pool_size = 0
    disabled._pool = []
    killed: list[_FakeProcess] = []

    async def record_kill(proc: Any) -> None:
        killed.append(cast(_FakeProcess, proc))

    cast(Any, disabled)._kill_pooled = record_kill
    process = _FakeProcess()
    asyncio.run(disabled._pool_return(cast(Any, process), command, environment))

    assert killed == [process]
    assert disabled._pool == []
    assert disabled._pool_pop(command, environment) is None
