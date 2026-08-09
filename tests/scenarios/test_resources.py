"""Scenarios for supervisor-owned compile resources and task budgets."""

from __future__ import annotations

import asyncio

from cambium.resources import CompileGate, ResourceBudget


def test_is_heavy_uses_exact_command_token_prefixes() -> None:
    gate = CompileGate(max_concurrent=1)

    assert gate.is_heavy(["make"])
    assert gate.is_heavy(["make", "all"])
    assert gate.is_heavy(["cargo", "build"])
    assert gate.is_heavy(["pytest", "-q"])
    assert gate.is_heavy(["npm", "install", "pytest"])
    assert gate.is_heavy(["pip", "install", "-e", "."])

    assert not gate.is_heavy(["echo", "make"])
    assert not gate.is_heavy(["git", "status"])
    assert not gate.is_heavy(["makefile"])
    assert not gate.is_heavy(["python", "-m", "pytest"])
    assert not gate.is_heavy([])


def test_heavy_acquires_are_serialized_and_waiters_proceed() -> None:
    async def scenario() -> None:
        gate = CompileGate(max_concurrent=1, timeout_s=1.0)
        command = ["make"]

        assert await gate.acquire(command)
        second = asyncio.create_task(gate.acquire(["cargo", "build"]))
        await asyncio.sleep(0)
        assert not second.done()
        assert gate.stats() == {
            "current": 1,
            "heavy": 1,
            "max": 1,
            "waits": 1,
            "timeouts": 0,
        }

        gate.release(command)
        assert await second
        gate.release(["cargo", "build"])
        assert gate.stats()["current"] == 0

    asyncio.run(scenario())


def test_heavy_acquire_timeout_returns_false() -> None:
    async def scenario() -> None:
        gate = CompileGate(max_concurrent=1, timeout_s=0.01)
        command = ["cargo", "build"]

        assert await gate.acquire(command)
        assert await gate.acquire(["pytest", "-q"]) is False
        assert gate.stats()["waits"] == 1
        assert gate.stats()["timeouts"] == 1
        gate.release(command)

    asyncio.run(scenario())


def test_non_heavy_commands_do_not_wait_or_change_gate_stats() -> None:
    async def scenario() -> None:
        gate = CompileGate(max_concurrent=1, timeout_s=0.01)
        assert await gate.acquire(["make"])
        before = gate.stats()

        assert await gate.acquire(["git", "status"])
        assert gate.stats() == before

        gate.release(["git", "status"])
        gate.release(["make"])

    asyncio.run(scenario())


def test_compile_gate_stats_record_capacity_and_successful_heavy_ops() -> None:
    async def scenario() -> None:
        gate = CompileGate(max_concurrent=2)
        assert gate.stats() == {
            "current": 0,
            "heavy": 0,
            "max": 2,
            "waits": 0,
            "timeouts": 0,
        }
        assert await gate.acquire(["gcc", "-c", "main.c"])
        assert await gate.acquire(["echo", "ok"])
        assert gate.stats() == {
            "current": 1,
            "heavy": 1,
            "max": 2,
            "waits": 0,
            "timeouts": 0,
        }
        gate.release(["gcc", "-c", "main.c"])

    asyncio.run(scenario())


def test_resource_budget_exhausts_heavy_operation_allowance() -> None:
    budget = ResourceBudget(max_wall_s=10.0, max_heavy_ops=2)

    assert budget.can_start_heavy()
    assert budget.consume_heavy_op()
    assert budget.consume_heavy_op()
    assert budget.heavy_ops == 2
    assert budget.can_start_heavy() is False
    assert budget.consume_heavy_op() is False


def test_resource_budget_wall_limit_blocks_operations() -> None:
    budget = ResourceBudget(max_wall_s=0.0, max_heavy_ops=1)

    assert budget.wall_remaining_s == 0.0
    assert budget.can_start_heavy() is False
    assert budget.consume_heavy_op() is False
