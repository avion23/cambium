"""Scenarios for supervisor-owned compile resources."""

from __future__ import annotations

import asyncio

import pytest

from cambium.resources import CompileGate


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

        first_token = await gate.acquire(command)
        assert first_token is not None
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

        gate.release(first_token)
        second_token = await second
        assert second_token is not None
        gate.release(second_token)
        assert gate.stats()["current"] == 0

    asyncio.run(scenario())


def test_heavy_acquire_timeout_returns_false() -> None:
    async def scenario() -> None:
        gate = CompileGate(max_concurrent=1, timeout_s=0.01)
        command = ["cargo", "build"]

        token = await gate.acquire(command)
        assert token is not None
        assert await gate.acquire(["pytest", "-q"]) is False
        assert gate.stats()["waits"] == 1
        assert gate.stats()["timeouts"] == 1
        gate.release(token)

    asyncio.run(scenario())


def test_non_heavy_commands_do_not_wait_or_change_gate_stats() -> None:
    async def scenario() -> None:
        gate = CompileGate(max_concurrent=1, timeout_s=0.01)
        token = await gate.acquire(["make"])
        assert token is not None
        before = gate.stats()

        assert await gate.acquire(["git", "status"]) is None
        assert gate.stats() == before

        gate.release(None)
        gate.release(token)

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
        token = await gate.acquire(["gcc", "-c", "main.c"])
        assert token is not None
        assert await gate.acquire(["echo", "ok"]) is None
        assert gate.stats() == {
            "current": 1,
            "heavy": 1,
            "max": 2,
            "waits": 0,
            "timeouts": 0,
        }
        gate.release(token)

    asyncio.run(scenario())


def test_release_rejects_unknown_duplicate_and_command_tokens() -> None:
    async def scenario() -> None:
        gate = CompileGate(max_concurrent=1)
        token = await gate.acquire(["make", "all"])
        assert token is not None

        with pytest.raises(ValueError, match="unknown or duplicate"):
            gate.release(object())
        with pytest.raises(ValueError, match="unknown or duplicate"):
            gate.release(["make"])
        assert gate.stats()["current"] == 1

        gate.release(token)
        with pytest.raises(ValueError, match="unknown or duplicate"):
            gate.release(token)

    asyncio.run(scenario())
