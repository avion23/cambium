"""Regression coverage for sandbox-restricted provider failures."""

from __future__ import annotations

import asyncio
import errno
from pathlib import Path
from typing import cast

import pytest
from diffundo_helpers import FakeServer, _config, _error_payload, _ok_payload

from cambium.diffundo import (
    AllProvidersFailed,
    Diffundo,
    HealthState,
    ProviderConfig,
    ProviderError,
    ProviderOutcome,
    ProviderTier,
    _codex_stream_error,
)
from cambium.supervisor import WorkerHandle, _Runtime
from cambium.worker import _failure_usage_event


def _provider(name: str = "p_sandbox") -> ProviderConfig:
    return ProviderConfig(
        name=name,
        tier=ProviderTier.FAST,
        base_url="https://provider.example",
        api_key_env="CAMBIUM_PROVIDER_KEY",
        api_key="sk-test-sandbox",
        model="test-model",
    )


@pytest.mark.parametrize(
    "signature",
    [
        "sandbox blocked tool execution",
        "permission denied",
        "Operation not permitted",
        "EACCES",
        "provider exited with code 126",
        "provider exited with status 127",
        "network access restricted",
    ],
)
def test_sandbox_signatures_are_distinct_and_not_endpoint_death(signature: str) -> None:
    error = Diffundo(())._classify_http(_provider(), 500, signature)

    assert error.outcome is ProviderOutcome.SANDBOX_RESTRICTED
    assert "sandbox_restricted" in str(error)
    assert error.is_real_death is False


def test_codex_stream_sandbox_error_is_distinct() -> None:
    error = _codex_stream_error(
        _provider(),
        {"type": "sandbox_error", "message": "tool execution blocked"},
        "access-token",
    )

    assert error.outcome is ProviderOutcome.SANDBOX_RESTRICTED


@pytest.mark.parametrize(
    "cause",
    [
        PermissionError(errno.EACCES, "Permission denied"),
        type("ExitedProcess", (RuntimeError,), {"returncode": 127})(),
    ],
)
def test_os_restriction_causes_are_classified(cause: BaseException) -> None:
    error = ProviderError("p_sandbox", ProviderOutcome.ERROR, "transport failed", cause)

    assert error.outcome is ProviderOutcome.SANDBOX_RESTRICTED


def test_sandbox_failure_cascades_without_health_damage(monkeypatch: pytest.MonkeyPatch) -> None:
    blocked = FakeServer([(500, _error_payload("sandbox blocked network access"), 0.0)])
    good = FakeServer([(200, _ok_payload("fallback"), 0.0)])
    router = Diffundo(
        (
            _config("p_sandbox", blocked, "K_SANDBOX", max_retries=2),
            _config("p_good", good, "K_GOOD"),
        )
    )
    try:
        result = asyncio.run(
            router.call(ProviderTier.FAST, {"messages": [{"role": "user", "content": "x"}]})
        )

        assert result.provider == "p_good"
        assert len(blocked.calls) == 1
        assert router.health("p_sandbox") is HealthState.UNKNOWN
    finally:
        blocked.close()
        good.close()


def test_failure_usage_event_names_the_sandbox_restriction() -> None:
    error = ProviderError("p_sandbox", ProviderOutcome.ERROR, "sandbox blocked tool execution")
    event = _failure_usage_event(
        AllProvidersFailed(("p_sandbox",), error),
        turn=1,
        model="test-model",
        router=Diffundo(()),
        prompt={"messages": [{"role": "user", "content": "x"}]},
    )

    assert cast(str, event["failure_reason"]).startswith("sandbox_restricted:")


def test_supervisor_carries_sandbox_usage_reason_into_failed_envelope(
    tmp_path: Path,
) -> None:
    worktree = tmp_path / "worktree"
    worktree.mkdir()
    worker = tmp_path / "worker.py"
    worker.write_text(
        "import json, os, sys\n"
        "def send(message):\n"
        "    print(json.dumps(message), flush=True)\n"
        "init = json.loads(sys.stdin.readline())\n"
        "send({'type': 'ready', 'request_id': init['request_id'],\n"
        "      'task_id': init['task_id'], 'generation': init['generation'],\n"
        "      'pid': os.getpid(), 'proto': 1})\n"
        "run = json.loads(sys.stdin.readline())\n"
        "send({'type': 'usage_event', 'task_id': init['task_id'],\n"
        "      'generation': init['generation'],\n"
        "      'failure_reason': 'sandbox_restricted: permission denied'})\n"
        "send({'type': 'result_envelope', 'request_id': run['request_id'],\n"
        "      'task_id': init['task_id'], 'generation': init['generation'],\n"
        "      'status': 'failed',\n"
        "      'failure_reason': 'provider call failed: AllProvidersFailed'})\n"
        "send({'type': 'exit_message', 'task_id': init['task_id'],\n"
        "      'generation': init['generation'], 'reason': 'done'})\n",
        encoding="utf-8",
    )
    spec = {
        "task_id": "sandbox-task",
        "task": "finish",
        "repo": str(tmp_path),
        "worktree_path": str(worktree),
        "branch": "sandbox-task",
        "base_commit": "not-used-by-drive",
        "worker": str(worker),
        "provider_env_keys": [],
        "max_turns": 1,
        "max_tokens": 100,
    }
    outcome = asyncio.run(
        _Runtime(tmp_path / "session", None)._drive_generation(
            spec,
            WorkerHandle(task_id="sandbox-task", generation=1),
            ready_timeout=2.0,
            heartbeat_interval=0.1,
            heartbeat_timeout=2.0,
            wall_budget=10.0,
        )
    )

    assert outcome.clean is True
    assert outcome.envelope is not None
    assert outcome.envelope["failure_reason"] == "sandbox_restricted: permission denied"
