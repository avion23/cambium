"""Worker agent-loop behavior: direct actions, transcript bounding,
lint feedback visibility, read_batch exposure, and the heartbeat drain fix.

The provider-backed loop is driven in-process with a scripted fake router
(no network, no subprocess): a real worktree, real tool dispatch, and real
``Diffundo.call``-shaped responses.
"""

from __future__ import annotations

import asyncio
import copy
import json
import os
import subprocess
import sys
import threading
import time
from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace
from typing import Any, cast

import pytest

from cambium import tools, worker
from cambium.branch_history import query_branch_history
from cambium.diffundo import (
    ProviderError,
    ProviderOutcome,
    ProviderTier,
    prompt_prefix_bytes,
    validate_prompt_structure,
)
from cambium.fencing import write_generation
from cambium.state_view import state_text


class _FakeWriter:
    def __init__(self) -> None:
        self.lines: list[bytes] = []

    def write(self, data: bytes) -> None:
        self.lines.append(data)

    async def drain(self) -> None:
        pass

    def messages(self) -> list[dict[str, Any]]:
        return [json.loads(line) for line in self.lines if line.strip()]


class _FakeCallResult:
    def __init__(
        self,
        content: str,
        *,
        model: str = "loopback-model",
        usage: dict[str, int] | None = None,
        provider: str = "loopback-provider",
        latency_s: float = 0.01,
        fell_back_from: str | None = None,
    ) -> None:
        self.content = content
        self.model = model
        self.usage = usage or {"prompt_tokens": 7, "completion_tokens": 3, "total_tokens": 10}
        self.provider = provider
        self.latency_s = latency_s
        self.estimated_cost_usd = 0.0
        self.retry_after_s: float | None = None
        self.request_rate_status: str | None = None
        self.account_quota_owner: str | None = None
        self.prompt_prefix_bytes: int | None = None
        self.provider_cache_hit: bool | None = None
        self.fell_back_from = fell_back_from


class _ScriptedRouter:
    def declared_model(self, name: str) -> str:
        return ""

    def __init__(self, responses: list[str]) -> None:
        self.responses = list(responses)
        self.prompts: list[dict[str, Any]] = []

    async def call(
        self,
        tier: ProviderTier,
        prompt: dict[str, Any],
        *,
        model: str | None = None,
        budget_usd: float | None = None,
        allow_model_substitution: bool = False,
    ) -> _FakeCallResult:
        self.prompts.append(prompt)
        if not self.responses:
            raise AssertionError("router call with no scripted response")
        return _FakeCallResult(self.responses.pop(0))


class _StreamingScriptedRouter(_ScriptedRouter):
    def __init__(
        self,
        responses: list[str],
        deltas: list[tuple[str, str]] | None = None,
        *,
        delta_delay_s: float = 0.0,
        hold_s: float = 1.1,
    ) -> None:
        super().__init__(responses)
        self.deltas = list(deltas or [])
        self.delta_delay_s = delta_delay_s
        self.hold_s = hold_s

    async def call(
        self,
        tier: ProviderTier,
        prompt: dict[str, Any],
        *,
        model: str | None = None,
        budget_usd: float | None = None,
        allow_model_substitution: bool = False,
        on_delta: Any = None,
        on_status: Any = None,
    ) -> _FakeCallResult:
        del tier, model, budget_usd, allow_model_substitution
        self.prompts.append(prompt)
        if not self.responses:
            raise AssertionError("router call with no scripted response")
        if on_status is not None:
            on_status(
                {
                    "kind": "provider_attempt",
                    "provider": "loopback-provider",
                    "model": "loopback-model",
                }
            )
        if on_delta is not None:
            for kind, fragment in self.deltas:
                if self.delta_delay_s:
                    await asyncio.sleep(self.delta_delay_s)
                on_delta(kind, fragment)
        await asyncio.sleep(self.hold_s)
        if on_status is not None:
            on_status(
                {
                    "kind": "provider_succeeded",
                    "provider": "loopback-provider",
                    "model": "loopback-model",
                    "provider_cache_hit": False,
                }
            )
        return _FakeCallResult(self.responses.pop(0))


class _UsageScriptedRouter(_ScriptedRouter):
    def __init__(self, responses: list[str], usages: list[dict[str, Any]]) -> None:
        super().__init__(responses)
        self.usages = list(usages)

    async def call(
        self,
        tier: ProviderTier,
        prompt: dict[str, Any],
        *,
        model: str | None = None,
        budget_usd: float | None = None,
        allow_model_substitution: bool = False,
    ) -> _FakeCallResult:
        del tier, model, budget_usd, allow_model_substitution
        self.prompts.append(prompt)
        if not self.responses or not self.usages:
            raise AssertionError("router call with no scripted response")
        return _FakeCallResult(self.responses.pop(0), usage=self.usages.pop(0))


class _SummaryFlushRouter:
    """Router double that requires substitution authorization for summaries."""

    def __init__(
        self,
        *,
        all_providers_dead: bool = False,
        malformed_summaries: int = 0,
        responses: list[str] | None = None,
    ) -> None:
        self.all_providers_dead = all_providers_dead
        self.malformed_summaries = malformed_summaries
        self.responses = (
            list(responses)
            if responses is not None
            else ['{"type":"finish","summary":"done","objective_met":true}']
        )
        self.prompts: list[dict[str, Any]] = []
        self.allow_model_substitution: list[bool] = []
        self.max_call_budget_s: list[float | None] = []

    def declared_model(self, name: str) -> str:
        return ""

    async def call(
        self,
        tier: ProviderTier,
        prompt: dict[str, Any],
        *,
        model: str | None = None,
        budget_usd: float | None = None,
        allow_model_substitution: bool = False,
        max_call_budget_s: float | None = None,
    ) -> _FakeCallResult:
        del tier, model, budget_usd
        self.prompts.append(prompt)
        self.allow_model_substitution.append(allow_model_substitution)
        self.max_call_budget_s.append(max_call_budget_s)
        messages = prompt.get("messages")
        control_content = None
        if isinstance(messages, list):
            for message in reversed(messages):
                if (
                    isinstance(message, dict)
                    and isinstance(message.get("content"), str)
                    and message["content"].startswith("<cambium-summary-control>\n")
                ):
                    control_content = message["content"]
                    break
        if control_content is not None:
            if not allow_model_substitution:
                raise AssertionError("summary calls must authorize model substitution")
            if self.all_providers_dead:
                raise RuntimeError("all summary providers failed")
            if self.malformed_summaries:
                self.malformed_summaries -= 1
                return _FakeCallResult(
                    "{}{}",
                    model="healthy-model",
                    provider="healthy-substitute",
                    fell_back_from="dead-primary",
                )
            control = json.loads(
                control_content.removeprefix("<cambium-summary-control>\n").removesuffix(
                    "\n</cambium-summary-control>"
                )
            )
            summary = {
                "type": "summary_entry",
                "sequence": control["sequence"],
                "source_sha256": control["source_sha256"],
                "source_message_count": control["source_message_count"],
                "through_turn": control["through_turn"],
                "objective": "preserve the current coding objective",
                "outcome": "captured the completed work segment",
                "decisions_added": [],
                "decisions_superseded": [],
                "facts_added": [],
                "facts_invalidated": [],
                "files_and_symbols_changed": [],
                "verification_results": [],
                "relevant_failed_approaches": [],
                "open_items": [],
            }
            return _FakeCallResult(
                json.dumps(summary, sort_keys=True, separators=(",", ":")),
                model="healthy-model",
                provider="healthy-substitute",
                fell_back_from="dead-primary",
            )
        if not self.responses:
            raise AssertionError("router call with no scripted response")
        return _FakeCallResult(
            self.responses.pop(0),
            model="dead-model",
            provider="dead-primary",
        )


class _StickySummaryFlushRouter(_SummaryFlushRouter):
    """Summary double that exposes coding-lease binding and call provenance."""

    def __init__(self, **kwargs: Any) -> None:
        super().__init__(**kwargs)
        self.bind_calls: list[tuple[str, str]] = []
        self.call_kinds: list[tuple[str, str, str]] = []
        self._lease: SimpleNamespace | None = None

    @property
    def provider_lease(self) -> SimpleNamespace | None:
        return self._lease

    def bind_provider(
        self,
        provider: str,
        model: str,
        *,
        root_task_id: str = "task",
    ) -> None:
        del root_task_id
        self.bind_calls.append((provider, model))
        if self._lease is None:
            self._lease = SimpleNamespace(provider=provider, model=model)
            return
        if (self._lease.provider, self._lease.model) != (provider, model):
            raise AssertionError("coding provider lease moved")

    async def call(
        self,
        tier: ProviderTier,
        prompt: dict[str, Any],
        *,
        model: str | None = None,
        budget_usd: float | None = None,
        allow_model_substitution: bool = False,
        max_call_budget_s: float | None = None,
    ) -> _FakeCallResult:
        result = await super().call(
            tier,
            prompt,
            model=model,
            budget_usd=budget_usd,
            allow_model_substitution=allow_model_substitution,
            max_call_budget_s=max_call_budget_s,
        )
        kind = "summary" if allow_model_substitution else "agent"
        self.call_kinds.append((kind, result.provider, result.model))
        return result


@pytest.mark.parametrize("value", ["nan", "inf", "-inf"])
def test_env_float_rejects_non_finite_values(monkeypatch: pytest.MonkeyPatch, value: str) -> None:
    default = 17.5
    monkeypatch.setenv("CAMBIUM_TEST_FLOAT", value)

    with pytest.raises(ValueError, match="finite"):
        worker._env_float("CAMBIUM_TEST_FLOAT", default)

    monkeypatch.delenv("CAMBIUM_TEST_FLOAT")
    assert worker._env_float("CAMBIUM_TEST_FLOAT", default) is default


def _make_worktree(repo: Path, branch: str = "agent-loop") -> Path:
    repo.mkdir(parents=True)
    subprocess.run(["git", "init", "-b", "main", str(repo)], check=True, capture_output=True)
    subprocess.run(["git", "-C", str(repo), "config", "user.name", "agent-loop-test"], check=True)
    subprocess.run(["git", "-C", str(repo), "config", "user.email", "agent-loop@test"], check=True)
    (repo / "alpha.txt").write_text("alpha-content\n", encoding="utf-8")
    (repo / "beta.txt").write_text("beta-content\n", encoding="utf-8")
    subprocess.run(["git", "-C", str(repo), "add", "-A"], check=True, capture_output=True)
    subprocess.run(
        ["git", "-C", str(repo), "commit", "-m", "initial"], check=True, capture_output=True
    )
    worktree = repo.parent / "wt"
    subprocess.run(
        ["git", "-C", str(repo), "worktree", "add", "-b", branch, str(worktree), "main"],
        check=True,
        capture_output=True,
    )
    write_generation(worktree, 1)
    return worktree


def _agent_config(worktree: Path, **overrides: Any) -> worker.AgentConfig:
    values: dict[str, Any] = dict(
        task_id="loop-agent",
        generation=1,
        task="read the files and finish",
        worktree=worktree,
        base_commit=None,
        fanout_config={},
        max_turns=10,
        max_tokens=200_000,
        shell_permission=True,
        network_permission=False,
        heartbeat_interval_s=0.05,
        max_wall_s=60.0,
        checkpoint_root=None,
    )
    values.update(overrides)
    return worker.AgentConfig(**values)


async def _drive_loop(
    config: worker.AgentConfig,
    worktree: Path,
    router: _ScriptedRouter,
    writer: _FakeWriter | None = None,
    run_request_id: str | None = None,
) -> dict[str, Any]:
    return await worker._run_agent_loop(
        config=config,
        router=router,  # type: ignore[arg-type]  # duck-typed Diffundo
        tier=ProviderTier.FAST,
        model="loopback-model",
        worktree=worktree,
        writer=writer,  # type: ignore[arg-type]
        stop=threading.Event(),
        progress=worker.AgentProgress(),
        run_request_id=run_request_id,
    )


def test_provider_boundary_degradation_is_emitted_and_fails_on_three_consecutive_errors(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    worktree = _make_worktree(tmp_path / "repo")
    config = _agent_config(worktree, fanout_config={"tier": "fast", "model": "loopback-model"})
    writer = _FakeWriter()
    providers = [
        SimpleNamespace(
            name=f"provider-{index}",
            protocol=SimpleNamespace(value="loopback"),
            reasoning_effort=None,
        )
        for index in range(3)
    ]
    monkeypatch.setattr(
        worker,
        "_provider_router",
        lambda *_args, **_kwargs: (object(), ProviderTier.FAST, "loopback-model", "identity"),
    )
    monkeypatch.setattr(worker, "_provider_path", lambda: tmp_path / "providers.json")
    monkeypatch.setattr(worker, "load_providers", lambda _path: providers)

    def fail_boundary(*_args: Any, **_kwargs: Any) -> dict[str, Any]:
        raise RuntimeError("boundary unavailable")

    monkeypatch.setattr(
        worker,
        "_provider_boundary",
        fail_boundary,
    )

    outcome = asyncio.run(
        worker._do_provider_work(
            {
                "scratch_repo": str(tmp_path / "repo"),
                "worktree_path": str(worktree),
                "request_id": "run-boundary",
            },
            config,
            threading.Event(),
            writer,  # type: ignore[arg-type]
            worker.AgentProgress(),
        )
    )

    assert outcome["status"] == "failed"
    assert outcome["failure_reason"] == "provider boundary degraded too many times"
    degraded = [
        message for message in writer.messages() if message["type"] == "provider_boundary_degraded"
    ]
    assert len(degraded) == 3
    assert all(message["error_type"] == "RuntimeError" for message in degraded)


async def _drive_loop_with_heartbeats(
    config: worker.AgentConfig,
    worktree: Path,
    router: _ScriptedRouter,
) -> tuple[dict[str, Any], list[dict[str, Any]]]:
    writer = _FakeWriter()
    stop = threading.Event()
    progress = worker.AgentProgress()
    heartbeat = asyncio.create_task(
        worker._heartbeat_loop(
            cast(asyncio.StreamWriter, writer),
            config.task_id,
            config.generation,
            stop,
            progress,
            config.heartbeat_interval_s,
        )
    )
    try:
        outcome = await worker._run_agent_loop(
            config=config,
            router=router,  # type: ignore[arg-type]  # duck-typed Diffundo
            tier=ProviderTier.FAST,
            model="loopback-model",
            worktree=worktree,
            writer=writer,  # type: ignore[arg-type]
            stop=stop,
            progress=progress,
        )
    finally:
        stop.set()
        await heartbeat
    return outcome, writer.messages()


@pytest.mark.parametrize("all_dead", [False, True])
def test_semantic_child_summary_substitution_and_failure(tmp_path: Path, all_dead: bool) -> None:
    worktree = _make_worktree(tmp_path / "repo")
    config = _agent_config(
        worktree,
        context_reuse=True,
        checkpoint_root=tmp_path / "checkpoints",
        max_turns=4,
    )
    router = _SummaryFlushRouter(
        all_providers_dead=all_dead,
        responses=[
            json.dumps(
                {
                    "name": "delegate",
                    "arguments": {
                        "child_task_id": "review",
                        "kind": "investigation",
                        "spec": {
                            "task": "Review alpha.txt",
                            "context_mode": "semantic",
                            "placement": "spread",
                        },
                    },
                }
            )
        ],
    )
    outcome = asyncio.run(_drive_loop(config, worktree, router))
    assert router.allow_model_substitution == [False, True]
    assert len(router.max_call_budget_s) == 2
    assert all(
        isinstance(value, float) and 0.0 < value <= config.max_wall_s
        for value in router.max_call_budget_s
    )
    if all_dead:
        assert outcome["status"] == "failed"
        assert "summary provider call failed" in outcome["failure_reason"]
    else:
        assert outcome["status"] == "suspended"
        assert outcome["provider"] == "dead-primary"
        assert "fell_back_from" not in outcome


def test_summary_fallback_does_not_move_coding_lease_for_later_agent_call(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    async def direct_to_thread(function: Any, *args: Any, **kwargs: Any) -> Any:
        return function(*args, **kwargs)

    monkeypatch.setattr(worker.asyncio, "to_thread", direct_to_thread)
    worktree = _make_worktree(tmp_path / "repo")
    summary_config = _agent_config(
        worktree,
        context_reuse=True,
        checkpoint_root=tmp_path / "checkpoints",
        max_turns=4,
    )
    router = _StickySummaryFlushRouter(
        responses=[
            json.dumps(
                {
                    "name": "delegate",
                    "arguments": {
                        "child_task_id": "review",
                        "kind": "investigation",
                        "spec": {
                            "task": "Review alpha.txt",
                            "context_mode": "semantic",
                            "placement": "spread",
                        },
                    },
                }
            )
        ]
    )

    suspended = asyncio.run(_drive_loop(summary_config, worktree, router))
    assert suspended["status"] == "suspended"
    assert router.call_kinds == [
        ("agent", "dead-primary", "dead-model"),
        ("summary", "healthy-substitute", "healthy-model"),
    ]
    assert router.bind_calls == [("dead-primary", "dead-model")]
    assert suspended["provider"] == "dead-primary"
    assert "fell_back_from" not in suspended
    checkpoint = worker._load_epoch_checkpoint(
        summary_config, suspended["checkpoint_ref"], expect_task_id=True
    )
    assert checkpoint.cache_key.provider == "dead-primary"
    assert checkpoint.cache_key.model == "dead-model"

    router.responses.append('{"type":"finish","summary":"done","objective_met":true}')
    later_config = _agent_config(worktree, context_reuse=False, max_turns=1)
    completed = asyncio.run(_drive_loop(later_config, worktree, router))

    assert completed["status"] == "succeeded"
    assert completed["provider"] == "dead-primary"
    assert router.call_kinds[-1] == ("agent", "dead-primary", "dead-model")
    assert router.bind_calls == [
        ("dead-primary", "dead-model"),
        ("dead-primary", "dead-model"),
    ]


def test_attempt_failure_usage_event_preserves_provider_state() -> None:
    class _Router:
        def declared_model(self, name: str) -> str:
            assert name == "dead-provider"
            return "dead-model"

        def status(self, name: str) -> SimpleNamespace:
            assert name == "dead-provider"
            return SimpleNamespace(value="cooldown")

    failures = [
        ProviderError(
            "dead-provider",
            ProviderOutcome.QUOTA,
            "HTTP 429 rate limit",
            retry_after_s=60.0,
            request_rate_status="cooldown",
        )
    ]

    events = worker._attempt_failure_usage_events(
        failures,
        turn=3,
        router=cast(Any, _Router()),
        prompt={"messages": [{"role": "system", "content": "stable"}]},
        call_kind="summary",
    )

    assert len(events) == 1
    event = events[0]
    assert event["provider"] == "dead-provider"
    assert event["model"] == "dead-model"
    assert event["call_kind"] == "summary"
    assert event["failure_reason"].startswith("quota:")
    assert event["retry_after_s"] == 60.0
    assert event["request_rate_status"] == "cooldown"


def test_agent_call_receives_remaining_wall_cap(tmp_path: Path) -> None:
    worktree = _make_worktree(tmp_path / "repo")
    config = _agent_config(worktree, context_reuse=False, max_wall_s=1.0)
    router = _SummaryFlushRouter(
        responses=['{"type":"finish","summary":"done","objective_met":true}']
    )

    outcome = asyncio.run(_drive_loop(config, worktree, router))

    assert outcome["status"] == "succeeded"
    assert len(router.max_call_budget_s) == 1
    budget = router.max_call_budget_s[0]
    assert isinstance(budget, float) and 0.0 < budget <= config.max_wall_s


def test_turn_resume_fallback_note_survives_delegate_checkpoint(tmp_path: Path) -> None:
    worktree = _make_worktree(tmp_path / "repo")
    checkpoint_root = tmp_path / "checkpoints"
    initial = _agent_config(
        worktree,
        context_reuse=True,
        checkpoint_root=checkpoint_root,
        max_turns=4,
    )
    state_ref = worker._write_checkpoint_file(
        initial,
        1,
        [{"role": "user", "content": "prior turn evidence"}],
        {},
        [],
        no_progress_actions=0,
    )
    assert state_ref is not None
    resume = worker._validate_resume(
        {
            "checkpoint_ref": "loop-agent/turn-001.json",
            "epoch": 1,
            "child_results": [],
            "child_results_truncated": False,
            "workspace_changed": False,
            "rejection_feedback": None,
        }
    )
    config = _agent_config(
        worktree,
        context_reuse=True,
        checkpoint_root=checkpoint_root,
        resume=resume,
        max_turns=4,
    )

    class _FallbackDelegateRouter(_ScriptedRouter):
        async def call(
            self,
            tier: ProviderTier,
            prompt: dict[str, Any],
            *,
            model: str | None = None,
            budget_usd: float | None = None,
            allow_model_substitution: bool = False,
            max_call_budget_s: float | None = None,
        ) -> _FakeCallResult:
            del tier, model, budget_usd, allow_model_substitution, max_call_budget_s
            self.prompts.append(prompt)
            return _FakeCallResult(
                self.responses.pop(0),
                provider="healthy-substitute",
                model="healthy-model",
                fell_back_from="dead-primary",
            )

    router = _FallbackDelegateRouter(
        [
            json.dumps(
                {
                    "name": "delegate",
                    "arguments": {
                        "child_task_id": "review",
                        "kind": "investigation",
                        "spec": {
                            "task": "Review alpha.txt",
                            "context_mode": "trunk",
                            "placement": "inherit",
                        },
                    },
                }
            )
        ]
    )
    writer = _FakeWriter()

    outcome = asyncio.run(_drive_loop(config, worktree, router, writer, "resume-fallback"))

    assert outcome["status"] == "suspended"
    event = next(
        message for message in writer.messages() if message["type"] == "context_checkpoint"
    )
    checkpoint = worker._load_epoch_checkpoint(config, event["checkpoint_ref"], expect_task_id=True)
    rendered = json.dumps(checkpoint.full_messages)
    assert "healthy-substitute/healthy-model" in rendered
    assert "assigned provider was unavailable" in rendered


def test_finish_keeps_raw_evidence_without_summary_call(tmp_path: Path) -> None:
    worktree = _make_worktree(tmp_path / "repo")
    config = _agent_config(
        worktree,
        context_reuse=True,
        checkpoint_root=tmp_path / "checkpoints",
        max_turns=4,
    )
    router = _SummaryFlushRouter(
        all_providers_dead=True,
        responses=[
            '{"name":"read_batch","arguments":{"paths":["alpha.txt"]}}',
            '{"type":"finish","summary":"read alpha","objective_met":true}',
        ],
    )
    writer = _FakeWriter()
    outcome = asyncio.run(_drive_loop(config, worktree, router, writer))
    assert outcome["status"] == "succeeded"
    assert router.allow_model_substitution == [False, False]
    event = next(e for e in writer.messages() if e["type"] == "context_checkpoint")
    checkpoint = worker._load_epoch_checkpoint(config, event["checkpoint_ref"], expect_task_id=True)
    text = json.dumps(checkpoint.full_messages)
    assert "alpha-content" in text and "read alpha" in text


def test_malformed_summary_defers_and_task_completes(tmp_path: Path) -> None:
    repo = tmp_path / "repo"
    worktree = _make_worktree(repo)
    config = _agent_config(
        worktree,
        context_reuse=True,
        rolling_compact=True,
        rolling_compact_threshold_high=1,
        checkpoint_root=tmp_path / "checkpoints",
        max_turns=10,
    )
    writer = _FakeWriter()
    router = _SummaryFlushRouter(
        malformed_summaries=2,
        responses=[
            '{"type":"plan","steps":["continue"]}',
            '{"type":"finish","summary":"done","objective_met":true}',
        ],
    )

    outcome = asyncio.run(
        _drive_loop(config, worktree, router, writer, "deferred-once")  # type: ignore[arg-type]
    )

    assert outcome["status"] == "succeeded"
    deferred = [
        message for message in writer.messages() if message["type"] == "compaction_deferred"
    ]
    assert deferred == [
        {
            "type": "compaction_deferred",
            "request_id": "deferred-once",
            "task_id": "loop-agent",
            "generation": 1,
            "epoch": 1,
            "reason": "summary response must be exactly one JSON object",
        }
    ]
    assert not any(message["type"] == "compaction_failed" for message in writer.messages())


def test_semantic_delegate_fold_failure_does_not_suspend_stale_checkpoint(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A deferred forced fold cannot publish the pre-fold checkpoint."""
    async def direct_to_thread(function: Any, *args: Any, **kwargs: Any) -> Any:
        return function(*args, **kwargs)

    monkeypatch.setattr(worker.asyncio, "to_thread", direct_to_thread)
    worktree = _make_worktree(tmp_path / "repo")
    checkpoint_root = tmp_path / "checkpoints"
    seed_config = _agent_config(
        worktree,
        context_reuse=True,
        checkpoint_root=checkpoint_root,
    )
    prior_checkpoint = worker._write_epoch_checkpoint(
        seed_config,
        turn=1,
        epoch=1,
        messages=[
            {"role": "system", "content": "You are the agent."},
            {"role": "user", "content": "<cambium-task>prior task</cambium-task>"},
        ],
        provider="loopback-provider",
        model="loopback-model",
        tools_sha256=worker._sha256_hex(
            json.dumps(worker._exposed_tool_schemas(seed_config), sort_keys=True).encode("utf-8")
        ),
        provider_compat={"loopback-provider": ("loopback", None)},
    )
    assert prior_checkpoint is not None
    prior_bytes = (checkpoint_root / prior_checkpoint.checkpoint_ref).read_bytes()
    config = _agent_config(
        worktree,
        context_reuse=True,
        rolling_compact=True,
        rolling_compact_threshold_high=1,
        checkpoint_root=checkpoint_root,
        resume={
            "checkpoint_ref": prior_checkpoint.checkpoint_ref,
            "epoch": prior_checkpoint.epoch,
            "child_results": [],
            "child_results_truncated": False,
            "rejection_feedback": None,
            "workspace_changed": False,
        },
        max_turns=4,
    )
    delegate = json.dumps(
        {
            "type": "tool_call",
            "name": "delegate",
            "arguments": {
                "child_task_id": "review",
                "kind": "investigation",
                "spec": {
                    "task": "Review alpha.txt",
                    "context_mode": "semantic",
                    "placement": "spread",
                },
            },
        }
    )
    writer = _FakeWriter()
    router = _SummaryFlushRouter(malformed_summaries=2, responses=[delegate])

    outcome = asyncio.run(_drive_loop(config, worktree, router, writer, "forced-fold-race"))

    assert outcome["status"] == "failed"
    assert "compaction" in (outcome["failure_reason"] or "")
    assert not any(message["type"] == "context_checkpoint" for message in writer.messages())
    assert (checkpoint_root / prior_checkpoint.checkpoint_ref).read_bytes() == prior_bytes
    assert len(
        [message for message in writer.messages() if message["type"] == "compaction_deferred"]
    ) == 1


def test_two_malformed_summaries_fail_on_the_third_fold_attempt(tmp_path: Path) -> None:
    repo = tmp_path / "repo"
    worktree = _make_worktree(repo)
    config = _agent_config(
        worktree,
        context_reuse=True,
        rolling_compact=True,
        rolling_compact_threshold_high=1,
        checkpoint_root=tmp_path / "checkpoints",
        max_turns=10,
    )
    writer = _FakeWriter()
    router = _SummaryFlushRouter(
        malformed_summaries=6,
        responses=[
            '{"type":"plan","steps":["first"]}',
            '{"type":"plan","steps":["second"]}',
            '{"type":"plan","steps":["third"]}',
        ],
    )

    outcome = asyncio.run(
        _drive_loop(config, worktree, router, writer, "deferred-twice")  # type: ignore[arg-type]
    )

    assert outcome["status"] == "failed"
    assert outcome["failure_reason"] == (
        "compaction_failed: summary response must be exactly one JSON object"
    )
    messages = writer.messages()
    assert len([message for message in messages if message["type"] == "compaction_deferred"]) == 2
    assert len([message for message in messages if message["type"] == "compaction_failed"]) == 1
    summary_prompts = [
        p
        for p in router.prompts
        if p["messages"][-1]["content"].startswith("<cambium-summary-control>\n")
    ]
    assert len(summary_prompts) == 6  # One ordinary repair per attempted fold.
    assert all(
        "Previous summary was invalid" in p["messages"][-1]["content"]
        for p in summary_prompts[1::2]
    )


# ---------------------------------------------------------------------------
# Plan-before-act: plan action parses, is stored, and the loop proceeds
# ---------------------------------------------------------------------------


def test_build_agent_prompt_last_message_is_always_user() -> None:
    """Payloads must not end on a system/assistant message (ZAI/GLM 1214)."""
    prompt = worker._build_agent_prompt("edit a.txt", [{"name": "read_batch"}], [])
    messages = prompt["messages"]
    assert messages[0]["role"] == "system"
    assert "native control functions named plan and finish are present" in messages[0]["content"]
    assert "never serialize an action into assistant text" in messages[0]["content"]
    assert messages[-1]["role"] == "user"
    # A plan action leaves the transcript ending with an assistant message;
    # the builder appends a neutral user continuation.
    plan_transcript = [
        {"role": "user", "content": "Begin."},
        {"role": "assistant", "content": '{"type": "plan", "steps": []}'},
    ]
    prompt2 = worker._build_agent_prompt("edit a.txt", [{"name": "read_batch"}], plan_transcript)
    assert prompt2["messages"][-1]["role"] == "user"
    assert prompt2["messages"][-1]["content"] == "Continue."


def test_build_agent_prompt_static_head_is_byte_stable_across_tasks() -> None:
    """§9.1.6: the system message (directive + sorted tool schemas) is
    byte-identical across tasks and transcripts; the dynamic task text rides
    as delimited user-role data in the tail (provider exact-prefix caching
    keys on the stable system head)."""
    tools = [{"name": "read_batch", "parameters": {"type": "object", "properties": {}}}]
    identity = "codex/gpt-5.6-luna"
    task_a = "task alpha"
    task_b = "task bravo longer"
    prompt_a = worker._build_agent_prompt(task_a, tools, [], model_identity=identity)
    prompt_b = worker._build_agent_prompt(task_b, tools, [], model_identity=identity)
    content_a = prompt_a["messages"][0]["content"]
    content_b = prompt_b["messages"][0]["content"]
    assert content_a == content_b
    assert task_a not in content_a
    assert task_b not in content_b
    assert prompt_a["messages"][1] == {
        "role": "user",
        "content": "<cambium-task>\nTask: task alpha\n</cambium-task>",
    }
    assert prompt_b["messages"][1] == {
        "role": "user",
        "content": "<cambium-task>\nTask: task bravo longer\n</cambium-task>",
    }
    # prompt_prefix_bytes mirrors the system-message byte length exactly.
    assert prompt_prefix_bytes(prompt_a) == len(content_a.encode("utf-8"))
    assert prompt_prefix_bytes(prompt_b) == len(content_b.encode("utf-8"))
    # A task carrying volatile tokens stays in the user tail: the header
    # validator does not flag it and the system prefix does not move.
    volatile = "fix the deploy from 2026-08-20T12:34:56Z (request_id=req-123)"
    prompt_v = worker._build_agent_prompt(volatile, tools, [], model_identity=identity)
    assert prompt_v["messages"][0]["content"] == content_a
    assert volatile in prompt_v["messages"][1]["content"]
    validate_prompt_structure(prompt_v)

    grown = worker._build_agent_prompt(
        task_a,
        tools,
        [
            {"role": "user", "content": "Begin."},
            {"role": "assistant", "content": '{"type": "tool_call", "name": "read_batch"}'},
            {"role": "user", "content": "tool read_batch ok=true"},
        ],
        model_identity=identity,
    )
    assert grown["messages"][0]["content"] == content_a
    assert prompt_prefix_bytes(grown) == prompt_prefix_bytes(prompt_a)


def test_agent_status_bar_is_last_context_tail_message(tmp_path: Path) -> None:
    repo = tmp_path / "repo"
    worktree = _make_worktree(repo)
    config = _agent_config(
        worktree,
        context_reuse=True,
        checkpoint_root=tmp_path / "checkpoints",
        max_tokens=100,
    )
    router = _SummaryFlushRouter(
        responses=[
            '{"type":"plan","steps":["continue"]}',
            '{"type":"finish","summary":"done","objective_met":true}',
        ]
    )

    outcome = asyncio.run(_drive_loop(config, worktree, router))  # type: ignore[arg-type]

    assert outcome["status"] == "succeeded"
    action_prompts = [
        prompt
        for prompt in router.prompts
        if not str(prompt["messages"][-1].get("content", "")).startswith(
            "<cambium-summary-control>"
        )
    ]
    assert len(action_prompts) == 2
    first_messages = action_prompts[0]["messages"]
    second_messages = action_prompts[1]["messages"]
    assert first_messages[:2] == second_messages[:2]
    assert first_messages[-1]["role"] == "user"
    assert "<cambium-situation " in first_messages[-1]["content"]
    assert worker._strip_situation_frame_content(first_messages[-1]["content"]) == (
        "<cambium-loop-state>budget=100% turn=1 epoch=0 code_changed=false "
        "verified_after_change=false verification_failed=false no_progress=0 "
        "budget_new_tokens=0 previous_prompt_tokens=0"
        "</cambium-loop-state>"
    )
    assert "budget=90%" in second_messages[-1]["content"]
    assert "turn=2" in second_messages[-1]["content"]
    assert "epoch=0" in second_messages[-1]["content"]
    assert "code_changed=false" in second_messages[-1]["content"]
    assert "verified_after_change=false" in second_messages[-1]["content"]


def test_usage_budget_charge_uses_uncached_baseline_and_safe_fallback() -> None:
    cached = {
        "prompt_tokens": 100,
        "cached_tokens": 90,
        "completion_tokens": 5,
        "total_tokens": 105,
    }
    assert worker._usage_budget_charge(cached, 0) == (15, 10)
    assert worker._usage_budget_charge(
        {**cached, "prompt_tokens": 120, "cached_tokens": 110, "total_tokens": 125},
        10,
    ) == (5, 10)

    missing_cache = {
        "prompt_tokens": 120,
        "completion_tokens": 5,
        "total_tokens": 125,
    }
    assert worker._usage_budget_charge(missing_cache, 100) == (25, 120)


def test_cached_heavy_turn_uses_paid_tokens_not_gross_prompt(tmp_path: Path) -> None:
    repo = tmp_path / "repo"
    worktree = _make_worktree(repo)
    config = _agent_config(worktree, max_tokens=20)
    router = _UsageScriptedRouter(
        [
            '{"type":"tool_call","name":"read_batch","arguments":{"paths":["alpha.txt"]}}',
            '{"type":"finish","summary":"read the file","objective_met":true}',
        ],
        [
            {
                "prompt_tokens": 100,
                "cached_tokens": 90,
                "completion_tokens": 5,
                "total_tokens": 105,
            },
            {
                "prompt_tokens": 120,
                "cached_tokens": 110,
                "completion_tokens": 5,
                "total_tokens": 125,
            },
        ],
    )

    outcome = asyncio.run(_drive_loop(config, worktree, router))

    assert outcome["status"] == "succeeded"
    assert outcome["failure_reason"] is None
    assert len(router.prompts) == 2
    assert worker.FINAL_SYNTHESIS_DIRECTIVE not in json.dumps(router.prompts)


def test_soft_cap_injects_one_forced_finalization(tmp_path: Path) -> None:
    repo = tmp_path / "repo"
    worktree = _make_worktree(repo)
    config = _agent_config(worktree, max_tokens=100)
    router = _UsageScriptedRouter(
        [
            '{"type":"tool_call","name":"read_batch","arguments":{"paths":["alpha.txt"]}}',
            '{"type":"finish","summary":"read the file","objective_met":false}',
        ],
        [
            {"prompt_tokens": 90, "completion_tokens": 0, "total_tokens": 90},
            {"prompt_tokens": 100, "completion_tokens": 0, "total_tokens": 100},
        ],
    )

    outcome = asyncio.run(_drive_loop(config, worktree, router))

    assert outcome["status"] == "failed"
    assert outcome["failure_reason"] == (
        "forced finalization: investigation incomplete, no changes made"
    )
    assert len(router.prompts) == 2
    injected = [
        message
        for message in router.prompts[1]["messages"]
        if message.get("content") == worker.FINAL_SYNTHESIS_DIRECTIVE
    ]
    assert len(injected) == 1


def test_finalization_may_use_scaled_headroom_past_hard_cap(tmp_path: Path) -> None:
    repo = tmp_path / "repo"
    worktree = _make_worktree(repo)
    config = _agent_config(worktree, max_tokens=100)
    router = _UsageScriptedRouter(
        [
            '{"type":"tool_call","name":"read_batch","arguments":{"paths":["alpha.txt"]}}',
            '{"type":"finish","summary":"best available result","objective_met":false}',
        ],
        [
            {"prompt_tokens": 95, "completion_tokens": 0, "total_tokens": 95},
            {"prompt_tokens": 4_000, "completion_tokens": 0, "total_tokens": 4_000},
        ],
    )

    outcome = asyncio.run(_drive_loop(config, worktree, router))

    assert outcome["status"] == "failed"
    assert outcome["failure_reason"] == (
        "forced finalization: investigation incomplete, no changes made"
    )


def test_three_turn_budget_allows_edit_verify_finish(tmp_path: Path) -> None:
    repo = tmp_path / "repo"
    worktree = _make_worktree(repo)
    config = _agent_config(worktree, max_turns=3)
    router = _ScriptedRouter(
        [
            json.dumps(
                {
                    "type": "tool_call",
                    "name": "write_file",
                    "arguments": {"path": "alpha.txt", "content": "changed"},
                }
            ),
            json.dumps(
                {
                    "type": "tool_call",
                    "name": "run_shell",
                    "arguments": {
                        "cmd": [
                            "python3",
                            "-c",
                            "from pathlib import Path; "
                            "assert Path('alpha.txt').read_text() == 'changed'",
                        ]
                    },
                }
            ),
            '{"type":"finish","summary":"changed and verified alpha.txt","objective_met":true}',
        ]
    )

    outcome = asyncio.run(_drive_loop(config, worktree, router))

    assert outcome["status"] == "succeeded"
    assert outcome["failure_reason"] is None
    assert len(router.prompts) == 3
    assert not any(
        message.get("content") == worker.FINAL_SYNTHESIS_DIRECTIVE
        for prompt in router.prompts[:2]
        for message in prompt["messages"]
    )
    assert any(
        message.get("content") == worker.FINAL_SYNTHESIS_DIRECTIVE
        for message in router.prompts[2]["messages"]
    )


def test_build_agent_prompt_renders_bounded_parent_envelope() -> None:
    """Design C: a child receives the parent's summary, changed files, and
    commits as a delimited user-role data block after the transcript, never
    inside the system message and never the parent's raw transcript."""
    tools = [{"name": "read_batch", "parameters": {"type": "object", "properties": {}}}]
    envelope = {
        "parent_task_id": "parent-1",
        "summary": "added the token budget",
        "files_changed": ["src/a.py", "src/b.py"],
        "commits": ["abc123"],
        "status": "succeeded",
    }
    prompt = worker._build_agent_prompt("continue the work", tools, [], parent_envelope=envelope)
    system_content = prompt["messages"][0]["content"]
    assert "Task:" not in system_content
    assert "Parent task context:" not in system_content
    assert prompt["messages"][1] == {
        "role": "user",
        "content": "<cambium-task>\nTask: continue the work\n</cambium-task>",
    }
    block = prompt["messages"][-1]
    assert block["role"] == "user"
    assert block["content"].startswith("<cambium-parent-context>\nParent task context:")
    assert block["content"].endswith("</cambium-parent-context>")
    assert "parent summary: added the token budget" in block["content"]
    assert "parent files changed: src/a.py, src/b.py" in block["content"]
    assert "parent commits: abc123" in block["content"]
    assert "parent status: succeeded" in block["content"]


def test_parent_envelope_rejects_oversized_and_incomplete_fields() -> None:
    """Strict parent envelopes reject malformed or oversized payloads."""
    tools = [{"name": "read_batch", "parameters": {"type": "object", "properties": {}}}]
    with pytest.raises(worker.ParentEnvelopeError, match="summary.*field cap"):
        worker._validate_parent_envelope(
            {
                "parent_task_id": "parent",
                "unified_diff": "",
                "diff_truncated": False,
                "summary": "x" * 100_000,
                "metric_score": None,
                "metric_breakdown": {},
                "files_changed": [],
                "commits": [],
                "status": "succeeded",
            }
        )
    with pytest.raises(worker.ParentEnvelopeError, match="must be an object"):
        worker._validate_parent_envelope("not a dict")
    with pytest.raises(worker.ParentEnvelopeError, match="unknown keys"):
        worker._validate_parent_envelope({"unknown_key": 1})
    content = worker._build_agent_prompt("task", tools, [], parent_envelope=None)["messages"][0][
        "content"
    ]
    assert "Parent task context:" not in content


def test_parent_envelope_rejects_non_string_list_items() -> None:
    """Strict parent envelopes reject non-string list items."""
    with pytest.raises(worker.ParentEnvelopeError, match="only strings"):
        worker._validate_parent_envelope(
            {
                "parent_task_id": "parent",
                "unified_diff": "",
                "diff_truncated": False,
                "summary": "ok",
                "metric_score": None,
                "metric_breakdown": {},
                "files_changed": ["a.py", {"path": "b.py"}],
                "commits": ["abc"],
                "status": "succeeded",
            }
        )


def test_plan_before_act_plan_read_batch_finish(tmp_path: Path) -> None:
    repo = tmp_path / "repo"
    worktree = _make_worktree(repo)
    config = _agent_config(worktree)
    router = _ScriptedRouter(
        [
            '{"type":"plan","steps":["read both files","finish"]}',
            '{"type":"tool_call","name":"read_batch","arguments":'
            '{"paths":["alpha.txt","beta.txt"]}}',
            '{"type":"finish","summary":"read both files","objective_met":true}',
        ]
    )

    outcome = asyncio.run(_drive_loop(config, worktree, router))

    assert outcome["status"] == "succeeded"
    assert outcome["summary"] == "read both files"
    assert outcome["turn"] == 3
    assert len(router.prompts) == 3

    transcript = outcome["transcript"]
    plan_message = worker._plan_message(transcript)
    assert plan_message is not None
    assert json.loads(plan_message["content"]) == {
        "type": "plan",
        "steps": ["read both files", "finish"],
    }
    observation = transcript[-2]["content"]
    assert "tool read_batch ok=True" in observation
    assert "alpha-content" in observation
    assert "beta-content" in observation
    final_action = json.loads(transcript[-1]["content"])
    assert final_action["type"] == "finish"
    assert "thought" not in final_action


def test_batched_tool_calls_keep_order_and_deny_atomically(tmp_path: Path) -> None:
    repo = tmp_path / "read-repo"
    worktree = _make_worktree(repo)
    config = _agent_config(worktree)
    router = _ScriptedRouter(
        [
            json.dumps(
                {
                    "type": "tool_call",
                    "calls": [
                        {"name": "read_batch", "arguments": {"paths": ["alpha.txt"]}},
                        {"name": "read_batch", "arguments": {"paths": ["beta.txt"]}},
                    ],
                }
            ),
            '{"type":"finish","summary":"read both files","objective_met":true}',
        ]
    )

    outcome = asyncio.run(_drive_loop(config, worktree, router))

    assert outcome["status"] == "succeeded"
    tool_actions = [
        json.loads(message["content"])
        for message in outcome["transcript"]
        if message["role"] == "assistant"
        and json.loads(message["content"]).get("type") == "tool_call"
    ]
    assert tool_actions == [
        {
            "type": "tool_call",
            "calls": [
                {"name": "read_batch", "arguments": {"paths": ["alpha.txt"]}},
                {"name": "read_batch", "arguments": {"paths": ["beta.txt"]}},
            ],
        }
    ]
    observations = [
        message["content"]
        for message in outcome["transcript"]
        if message["role"] == "user" and message["content"].startswith("tool read_batch")
    ]
    assert len(observations) == 2
    assert "alpha-content" in observations[0]
    assert "beta-content" in observations[1]

    denied_repo = tmp_path / "denied" / "repo"
    denied_worktree = _make_worktree(denied_repo)
    denied_config = _agent_config(denied_worktree)
    denied_router = _ScriptedRouter(
        [
            json.dumps(
                {
                    "type": "tool_call",
                    "calls": [
                        {
                            "name": "git_op",
                            "arguments": {"op": "commit", "args": "blocked"},
                        },
                        {
                            "name": "write_file",
                            "arguments": {"path": "blocked.txt", "content": "nope\n"},
                        },
                    ],
                }
            ),
            '{"type":"finish","summary":"denial handled","objective_met":true}',
        ]
    )

    denied_outcome = asyncio.run(_drive_loop(denied_config, denied_worktree, denied_router))

    assert denied_outcome["status"] == "succeeded"
    assert not (denied_worktree / "blocked.txt").exists()
    denied_transcript = "\n".join(message["content"] for message in denied_outcome["transcript"])
    assert "tool git_op ok=False" in denied_transcript
    assert "git_op is restricted" in denied_transcript
    assert "not executed: batch contained a denied action" in denied_transcript
    assert worker._parse_agent_action(
        '{"type":"tool_call","name":"read_batch","arguments":{"paths":["legacy.txt"]}}'
    ) == {
        "type": "tool_call",
        "calls": [{"name": "read_batch", "arguments": {"paths": ["legacy.txt"]}}],
    }
    assert worker._native_tool_action(
        SimpleNamespace(
            tool_calls=(
                {"function": {"name": "read_batch", "arguments": '{"paths":["a"]}'}},
                {"function": {"name": "read_batch", "arguments": '{"paths":["b"]}'}},
            )
        )
    ) == {
        "type": "tool_call",
        "calls": [
            {"name": "read_batch", "arguments": {"paths": ["a"]}},
            {"name": "read_batch", "arguments": {"paths": ["b"]}},
        ],
    }
    with pytest.raises(ValueError, match="missing 'paths'"):
        worker._native_tool_action(
            SimpleNamespace(
                tool_calls=(
                    {"function": {"name": "read_batch", "arguments": "{}"}},
                )
            )
        )


def test_native_plan_and_finish_controls_use_canonical_validation() -> None:
    assert worker._native_tool_action(
        SimpleNamespace(
            tool_calls=(
                {"function": {"name": "plan", "arguments": '{"steps":["inspect","edit"]}'}},
            )
        )
    ) == {"type": "plan", "steps": ["inspect", "edit"]}
    assert worker._native_tool_action(
        SimpleNamespace(
            tool_calls=(
                {
                    "function": {
                        "name": "finish",
                        "arguments": '{"summary":"done","objective_met":true}',
                    }
                },
            )
        )
    ) == {"type": "finish", "summary": "done", "objective_met": True}

    with pytest.raises(ValueError, match="provider native control action cannot be mixed"):
        worker._native_tool_action(
            SimpleNamespace(
                tool_calls=(
                    {"function": {"name": "plan", "arguments": '{"steps":["inspect"]}'}},
                    {"function": {"name": "read_batch", "arguments": '{"paths":["a.py"]}'}},
                )
            )
        )
    with pytest.raises(ValueError, match="at least 1 item"):
        worker._native_tool_action(
            SimpleNamespace(
                tool_calls=(
                    {"function": {"name": "plan", "arguments": '{"steps":[]}'}},
                )
            )
        )
    with pytest.raises(ValueError, match="unknown argument 'extra'"):
        worker._native_tool_action(
            SimpleNamespace(
                tool_calls=(
                    {
                        "function": {
                            "name": "finish",
                            "arguments": '{"summary":"done","objective_met":true,"extra":1}',
                        }
                    },
                )
            )
        )


def test_cancellation_mid_batch_persists_remaining_calls_as_unexecuted(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    repo = tmp_path / "repo"
    worktree = _make_worktree(repo)
    checkpoint_root = tmp_path / "checkpoints"
    config = _agent_config(worktree, checkpoint_root=checkpoint_root)
    stop = threading.Event()
    calls: list[str] = []

    async def run_tool(name: str, _arguments: dict[str, Any], _ctx: Any) -> worker.ToolResult:
        calls.append(name)
        stop.set()
        return worker.ToolResult(ok=True, output="first result", duration_ms=1)

    monkeypatch.setattr(worker, "run_tool", run_tool)
    router = _ScriptedRouter(
        [
            json.dumps(
                {
                    "type": "tool_call",
                    "calls": [
                        {"name": "write_file", "arguments": {"path": "one", "content": "1"}},
                        {"name": "write_file", "arguments": {"path": "two", "content": "2"}},
                        {"name": "run_shell", "arguments": {"cmd": ["echo", "three"]}},
                    ],
                }
            )
        ]
    )
    writer = _FakeWriter()

    outcome = asyncio.run(
        worker._run_agent_loop(
            config=config,
            router=router,  # type: ignore[arg-type]
            tier=ProviderTier.FAST,
            model="loopback-model",
            worktree=worktree,
            writer=writer,  # type: ignore[arg-type]
            stop=stop,
            progress=worker.AgentProgress(),
        )
    )

    assert outcome["status"] == "cancelled"
    assert outcome["turn"] == 0
    assert calls == ["write_file"]
    assert [message["content"] for message in outcome["transcript"][-3:]] == [
        "tool write_file ok=True\nfirst result",
        "not executed: batch cancelled",
        "not executed: batch cancelled",
    ]
    checkpoint = checkpoint_root / "loop-agent" / "turn-001.json"
    assert checkpoint.exists()
    persisted = json.loads(checkpoint.read_text(encoding="utf-8"))
    assert persisted["transcript"] == outcome["transcript"]
    tool_events = [message for message in writer.messages() if message["type"] == "tool_event"]
    assert len(tool_events) == 1
    assert tool_events[0]["batch_index"] == 0


def test_repeated_evidence_gets_one_finish_directive_before_stall(tmp_path: Path) -> None:
    worktree = _make_worktree(tmp_path / "repo")
    config = _agent_config(
        worktree,
        max_no_progress_actions=2,
        progress_window=3,
    )
    action = json.dumps(
        {
            "type": "tool_call",
            "calls": [{"name": "read_batch", "arguments": {"paths": ["alpha.txt"]}}],
        }
    )
    router = _ScriptedRouter(
        [action, action, '{"type":"finish","summary":"evidence complete","objective_met":true}']
    )

    outcome = asyncio.run(_drive_loop(config, worktree, router))

    assert outcome["status"] == "succeeded"
    assert outcome["summary"] == "evidence complete"
    assert len(router.prompts) == 3
    final_prompt_messages = router.prompts[-1]["messages"]
    assert sum(
        message.get("content") == worker.REPEATED_EVIDENCE_DIRECTIVE
        for message in final_prompt_messages
        if isinstance(message, dict)
    ) == 1


def _write_progress_session(session: Path, events: list[dict[str, Any]]) -> None:
    event_dir = session / ".cambium"
    event_dir.mkdir(parents=True, exist_ok=True)
    (event_dir / "events.db").write_text(
        "".join(f"{json.dumps(event, sort_keys=True)}\n" for event in events),
        encoding="utf-8",
    )


def _progress_session_events() -> list[dict[str, Any]]:
    return [
        {
            "seq": 1,
            "kind": "task_assigned",
            "task_id": "loop-agent",
            "generation": 1,
            "payload": {
                "task_id": "loop-agent",
                "session_id": "progress-session",
                "task": "inspect existing evidence",
                "repo": "/repo",
                "worktree": "/worktree",
                "branch": "cambium/loop-agent",
            },
        },
        {
            "seq": 2,
            "kind": "tool_event",
            "task_id": "loop-agent",
            "generation": 1,
            "payload": {
                "task_id": "loop-agent",
                "generation": 1,
                "turn": 1,
                "batch_index": 0,
                "tool": "read_batch",
                "cmd": 'read_batch {"paths":["alpha.txt"]}',
                "ok": True,
                "duration_ms": 1,
            },
        },
    ]


def _append_progress_tool_event(
    events: list[dict[str, Any]],
    name: str,
    *,
    turn: int,
) -> None:
    events.append(
        {
            "seq": len(events) + 1,
            "kind": "tool_event",
            "task_id": "loop-agent",
            "generation": 1,
            "payload": {
                "task_id": "loop-agent",
                "generation": 1,
                "turn": turn,
                "batch_index": 0,
                "tool": name,
                "cmd": "",
                "ok": True,
                "duration_ms": 1,
            },
        }
    )


def _run_supervised_read(
    session: Path,
    name: str,
    arguments: dict[str, Any],
) -> str:
    if name == "branch_history":
        return query_branch_history(session, arguments)
    assert name == "inspect_state"
    return state_text(session, arguments["task_id"])


@pytest.mark.parametrize(
    ("name", "arguments"),
    [
        ("branch_history", {"action": "tools", "task_id": "loop-agent"}),
        ("branch_history", {"action": "branches"}),
        ("inspect_state", {"task_id": "loop-agent"}),
    ],
)
def test_repeated_bookkeeping_only_evidence_is_not_progress(
    tmp_path: Path,
    name: str,
    arguments: dict[str, Any],
) -> None:
    session = tmp_path / "session"
    events = _progress_session_events()
    action = {
        "type": "tool_call",
        "calls": [{"name": name, "arguments": arguments}],
    }
    outputs: list[str] = []
    for turn in range(2, 5):
        _write_progress_session(session, events)
        outputs.append(_run_supervised_read(session, name, arguments))
        _append_progress_tool_event(events, name, turn=turn)

    assert len(set(outputs)) == 3
    detector = worker._ProgressDetector(max_no_progress_actions=2, progress_window=3)
    assert not detector.observe(action=action, result_content=outputs[0])
    assert detector.no_progress_actions == 0
    assert not detector.observe(action=action, result_content=outputs[1])
    assert detector.no_progress_actions == 1
    assert detector.observe(action=action, result_content=outputs[2])
    assert detector.no_progress_actions == 2


@pytest.mark.parametrize(
    ("name", "arguments"),
    [
        ("branch_history", {"action": "tools", "task_id": "loop-agent"}),
        ("branch_history", {"action": "branches"}),
        ("inspect_state", {"task_id": "loop-agent"}),
    ],
)
def test_material_evidence_change_remains_progress_after_bookkeeping_normalization(
    tmp_path: Path,
    name: str,
    arguments: dict[str, Any],
) -> None:
    session = tmp_path / "session"
    events = _progress_session_events()
    _write_progress_session(session, events)
    first = _run_supervised_read(session, name, arguments)
    _append_progress_tool_event(events, name, turn=2)
    _append_progress_tool_event(events, "run_shell", turn=3)
    _write_progress_session(session, events)
    changed = _run_supervised_read(session, name, arguments)
    action = {
        "type": "tool_call",
        "calls": [{"name": name, "arguments": arguments}],
    }
    detector = worker._ProgressDetector(max_no_progress_actions=1, progress_window=1)

    assert first != changed
    assert not detector.observe(action=action, result_content=first)
    assert not detector.observe(action=action, result_content=changed)
    assert detector.no_progress_actions == 0


def test_different_real_evidence_queries_remain_progress(tmp_path: Path) -> None:
    session = tmp_path / "session"
    events = _progress_session_events()
    _write_progress_session(session, events)
    calls = [
        {
            "name": "branch_history",
            "arguments": {"action": "tools", "task_id": "loop-agent"},
        },
        {
            "name": "branch_history",
            "arguments": {"action": "branches"},
        },
    ]
    detector = worker._ProgressDetector(max_no_progress_actions=1, progress_window=1)

    for call in calls:
        output = _run_supervised_read(session, call["name"], call["arguments"])
        assert not detector.observe(
            action={"type": "tool_call", "calls": [call]},
            result_content=output,
        )
        assert detector.no_progress_actions == 0


@pytest.mark.parametrize(
    ("name", "events"),
    [
        ("inspect_state", _progress_session_events()[:1]),
        (
            "inspect_state",
            [
                *_progress_session_events(),
                {
                    "seq": 3,
                    "kind": "tool_event",
                    "task_id": "loop-agent",
                    "generation": 1,
                    "payload": {
                        "task_id": "loop-agent",
                        "generation": 1,
                        "turn": 2,
                        "batch_index": 0,
                        "tool": "run_shell",
                        "cmd": "",
                        "ok": True,
                        "duration_ms": 1,
                    },
                },
            ],
        ),
    ],
)
def test_first_real_inspect_read_preserves_prior_state(
    tmp_path: Path,
    name: str,
    events: list[dict[str, Any]],
) -> None:
    session = tmp_path / "session"
    arguments = {"task_id": "loop-agent"}
    action = {
        "type": "tool_call",
        "calls": [{"name": name, "arguments": arguments}],
    }
    _write_progress_session(session, events)
    first = _run_supervised_read(session, name, arguments)
    _append_progress_tool_event(events, name, turn=3)
    _write_progress_session(session, events)
    second = _run_supervised_read(session, name, arguments)
    detector = worker._ProgressDetector(max_no_progress_actions=2, progress_window=3)

    assert first != second
    assert not detector.observe(action=action, result_content=first)
    assert not detector.observe(action=action, result_content=second)
    assert detector.no_progress_actions == 1


@pytest.mark.parametrize(
    ("name", "arguments"),
    [
        ("branch_history", {"action": "tools", "task_id": "loop-agent"}),
        ("branch_history", {"action": "branches"}),
        ("inspect_state", {"task_id": "loop-agent"}),
    ],
)
def test_restore_real_evidence_bookkeeping_identity(
    tmp_path: Path,
    name: str,
    arguments: dict[str, Any],
) -> None:
    session = tmp_path / "session"
    events = _progress_session_events()
    _write_progress_session(session, events)
    first = _run_supervised_read(session, name, arguments)
    _append_progress_tool_event(events, name, turn=2)
    _write_progress_session(session, events)
    second = _run_supervised_read(session, name, arguments)
    action = {
        "type": "tool_call",
        "calls": [{"name": name, "arguments": arguments}],
    }
    restored = worker._ProgressDetector(max_no_progress_actions=1, progress_window=1)
    restored.restore(
        [
            worker._canonical_action_message(action),
            {"role": "user", "content": f"tool {name} ok=True\n{first}"},
        ]
    )

    assert restored.observe(action=action, result_content=second)


def test_repeated_real_evidence_gets_one_finish_directive(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    async def direct_to_thread(function: Any, *args: Any, **kwargs: Any) -> Any:
        return function(*args, **kwargs)

    monkeypatch.setattr(worker.asyncio, "to_thread", direct_to_thread)
    worktree = _make_worktree(tmp_path / "repo")
    session = tmp_path / "session"
    events = _progress_session_events()
    _write_progress_session(session, events)
    monkeypatch.setenv("CAMBIUM_SESSION_ID", str(session))

    async def run_and_record(
        name: str, arguments: dict[str, Any], ctx: tools.ToolContext
    ) -> worker.ToolResult:
        del ctx
        result = worker.ToolResult(
            ok=True,
            output=_run_supervised_read(session, name, arguments),
            duration_ms=1,
        )
        _append_progress_tool_event(events, name, turn=len(events))
        _write_progress_session(session, events)
        return result

    monkeypatch.setattr(worker, "run_tool", run_and_record)
    action = json.dumps(
        {
            "type": "tool_call",
            "calls": [
                {
                    "name": "branch_history",
                    "arguments": {"action": "tools", "task_id": "loop-agent"},
                }
            ],
        }
    )
    router = _ScriptedRouter(
        [action, action, '{"type":"finish","summary":"evidence complete","objective_met":true}']
    )
    config = _agent_config(
        worktree,
        max_no_progress_actions=2,
        progress_window=3,
    )

    outcome = asyncio.run(_drive_loop(config, worktree, router))

    assert outcome["status"] == "succeeded"
    final_messages = router.prompts[-1]["messages"]
    assert (
        sum(
            message.get("content") == worker.REPEATED_EVIDENCE_DIRECTIVE
            for message in final_messages
            if isinstance(message, dict)
        )
        == 1
    )


def test_repeated_read_failure_persists_causal_tool_event_and_checkpoint(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    async def direct_to_thread(function: Any, *args: Any, **kwargs: Any) -> Any:
        return function(*args, **kwargs)

    monkeypatch.setattr(worker.asyncio, "to_thread", direct_to_thread)
    worktree = _make_worktree(tmp_path / "repo")
    checkpoint_root = tmp_path / "checkpoints"
    config = _agent_config(
        worktree,
        checkpoint_root=checkpoint_root,
        max_no_progress_actions=2,
        progress_window=3,
    )
    action = json.dumps(
        {
            "type": "tool_call",
            "calls": [{"name": "read_batch", "arguments": {"paths": ["alpha.txt"]}}],
        }
    )
    router = _ScriptedRouter([action, action, action])
    writer = _FakeWriter()

    outcome = asyncio.run(_drive_loop(config, worktree, router, writer))

    assert outcome["status"] == "failed"
    assert "no progress" in outcome["failure_reason"]
    assert outcome["turn"] == 3
    assert len(router.prompts) == 3
    messages = writer.messages()
    tool_events = [message for message in messages if message["type"] == "tool_event"]
    assert [message["turn"] for message in tool_events] == [1, 2, 3]
    checkpoints = [message for message in messages if message["type"] == "checkpoint"]
    assert [message["turn"] for message in checkpoints] == [1, 2, 3]
    checkpoint_path = Path(checkpoints[-1]["state_ref"])
    assert checkpoint_path == checkpoint_root / "loop-agent" / "turn-003.json"
    persisted = json.loads(checkpoint_path.read_text(encoding="utf-8"))
    assert persisted["transcript"] == outcome["transcript"]
    assert persisted["no_progress_actions"] == 2
    assert persisted["transcript"][-1]["content"].startswith("tool read_batch ok=True\n")


@pytest.mark.parametrize(
    ("name", "arguments"),
    [
        ("branch_history", {"action": "tools", "task_id": "child"}),
        ("inspect_state", {"task_id": "child"}),
        ("git_op", {"op": "status", "args": "--short"}),
    ],
)
def test_changed_observable_read_evidence_prevents_pre_execution_stall(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    name: str,
    arguments: dict[str, Any],
) -> None:
    worktree = _make_worktree(tmp_path / "repo")
    config = _agent_config(
        worktree,
        max_no_progress_actions=1,
        progress_window=1,
    )
    action = json.dumps(
        {
            "type": "tool_call",
            "calls": [{"name": name, "arguments": arguments}],
        }
    )
    router = _ScriptedRouter(
        [action, action, '{"type":"finish","summary":"evidence changed","objective_met":true}']
    )
    results = iter(("evidence=1", "evidence=2"))

    async def execute_read(
        tool_name: str, _arguments: dict[str, Any], _ctx: Any
    ) -> worker.ToolResult:
        assert tool_name == name
        return worker.ToolResult(ok=True, output=next(results), duration_ms=1)

    monkeypatch.setattr(worker, "run_tool", execute_read)

    outcome = asyncio.run(_drive_loop(config, worktree, router))

    assert outcome["status"] == "succeeded"
    assert outcome["summary"] == "evidence changed"
    assert len(router.prompts) == 3


def test_restore_hashes_a_read_batch_as_one_joined_result() -> None:
    action = {
        "type": "tool_call",
        "calls": [
            {"name": "read_batch", "arguments": {"paths": ["alpha.txt"]}},
            {"name": "read_batch", "arguments": {"paths": ["beta.txt"]}},
        ],
    }
    result_contents = ["--- alpha.txt ---\nalpha", "--- beta.txt ---\nbeta"]
    live = worker._ProgressDetector(max_no_progress_actions=2, progress_window=3)
    live.observe(action=action, result_content="\n".join(result_contents))

    restored = worker._ProgressDetector(max_no_progress_actions=2, progress_window=3)
    restored.restore(
        [
            worker._canonical_action_message(action),
            *(
                {
                    "role": "user",
                    "content": f"tool read_batch ok=True\n{result_content}",
                }
                for result_content in result_contents
            ),
        ]
    )

    assert len(live._recent_content_hashes) == 1
    assert list(restored._recent_content_hashes) == list(live._recent_content_hashes)


@pytest.mark.parametrize(
    ("name", "arguments"),
    [
        ("branch_history", {"action": "tools", "task_id": "child"}),
        ("inspect_state", {"task_id": "child"}),
        ("git_op", {"op": "status", "args": "--short"}),
    ],
)
def test_restore_recognizes_observable_read_evidence(
    name: str,
    arguments: dict[str, Any],
) -> None:
    action = {
        "type": "tool_call",
        "calls": [{"name": name, "arguments": arguments}],
    }
    result = "watermark=7\ncheckpoint_ref=turn-007"
    restored = worker._ProgressDetector(max_no_progress_actions=1, progress_window=1)

    restored.restore(
        [
            worker._canonical_action_message(action),
            {"role": "user", "content": f"tool {name} ok=True\n{result}"},
        ]
    )

    assert restored.observe(action=action, result_content=result)


def test_turn_checkpoint_restart_preserves_no_progress_streak(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    async def direct_to_thread(function: Any, *args: Any, **kwargs: Any) -> Any:
        return function(*args, **kwargs)

    monkeypatch.setattr(worker.asyncio, "to_thread", direct_to_thread)
    worktree = _make_worktree(tmp_path / "repo")
    checkpoint_root = tmp_path / "checkpoints"
    config = _agent_config(
        worktree,
        checkpoint_root=checkpoint_root,
        max_no_progress_actions=2,
        progress_window=3,
    )
    action = json.dumps(
        {
            "type": "tool_call",
            "calls": [{"name": "read_batch", "arguments": {"paths": ["alpha.txt"]}}],
        }
    )
    writer = _FakeWriter()
    first_router = _ScriptedRouter(
        [action, action, '{"type":"finish","summary":"first run","objective_met":true}']
    )

    first_outcome = asyncio.run(_drive_loop(config, worktree, first_router, writer))

    assert first_outcome["status"] == "succeeded"
    checkpoint_path = checkpoint_root / "loop-agent" / "turn-002.json"
    persisted = json.loads(checkpoint_path.read_text(encoding="utf-8"))
    assert persisted["no_progress_actions"] == 1
    resume = worker._validate_resume(
        {
            "checkpoint_ref": "loop-agent/turn-002.json",
            "epoch": 2,
            "child_results": [],
            "child_results_truncated": False,
            "workspace_changed": False,
            "rejection_feedback": None,
        }
    )
    resumed_config = replace(config, resume=resume)
    resumed_router = _ScriptedRouter([action])

    resumed_outcome = asyncio.run(_drive_loop(resumed_config, worktree, resumed_router))

    assert resumed_outcome["status"] == "failed"
    assert resumed_outcome["turn"] == 3
    assert "no progress" in (resumed_outcome["failure_reason"] or "")


def test_plan_checkpoint_restart_preserves_no_progress_streak(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    async def direct_to_thread(function: Any, *args: Any, **kwargs: Any) -> Any:
        return function(*args, **kwargs)

    monkeypatch.setattr(worker.asyncio, "to_thread", direct_to_thread)
    worktree = _make_worktree(tmp_path / "repo")
    checkpoint_root = tmp_path / "checkpoints"
    config = _agent_config(
        worktree,
        checkpoint_root=checkpoint_root,
        max_no_progress_actions=2,
        progress_window=3,
    )
    action = '{"type":"plan","steps":["repeat"]}'
    writer = _FakeWriter()
    first_router = _ScriptedRouter(
        [action, action, '{"type":"finish","summary":"first run","objective_met":true}']
    )

    first_outcome = asyncio.run(_drive_loop(config, worktree, first_router, writer))

    assert first_outcome["status"] == "succeeded"
    checkpoint_path = checkpoint_root / "loop-agent" / "turn-002.json"
    persisted = json.loads(checkpoint_path.read_text(encoding="utf-8"))
    assert persisted["no_progress_actions"] == 1
    resume = worker._validate_resume(
        {
            "checkpoint_ref": "loop-agent/turn-002.json",
            "epoch": 2,
            "child_results": [],
            "child_results_truncated": False,
            "workspace_changed": False,
            "rejection_feedback": None,
        }
    )
    resumed_config = replace(config, resume=resume)
    resumed_router = _ScriptedRouter([action])

    resumed_outcome = asyncio.run(_drive_loop(resumed_config, worktree, resumed_router))

    assert resumed_outcome["status"] == "failed"
    assert resumed_outcome["turn"] == 3
    assert "no progress" in (resumed_outcome["failure_reason"] or "")


@pytest.mark.parametrize("invalid", [True, -1, "1", None])
def test_turn_checkpoint_rejects_invalid_no_progress_count(tmp_path: Path, invalid: Any) -> None:
    worktree = _make_worktree(tmp_path / "repo")
    checkpoint_root = tmp_path / "checkpoints"
    config = _agent_config(worktree, checkpoint_root=checkpoint_root)
    path = worker._write_checkpoint_file(
        config,
        1,
        [{"role": "user", "content": "evidence"}],
        {},
        [],
        no_progress_actions=0,
    )
    assert path is not None
    payload = json.loads(path.read_text(encoding="utf-8"))
    payload["no_progress_actions"] = invalid
    path.write_text(json.dumps(payload), encoding="utf-8")

    with pytest.raises(worker.ContextForkError, match="no_progress"):
        worker._load_turn_checkpoint(config, "loop-agent/turn-001.json")


def test_turn_checkpoint_requires_no_progress_count(tmp_path: Path) -> None:
    worktree = _make_worktree(tmp_path / "repo")
    checkpoint_root = tmp_path / "checkpoints"
    config = _agent_config(worktree, checkpoint_root=checkpoint_root)
    path = worker._write_checkpoint_file(
        config,
        1,
        [{"role": "user", "content": "evidence"}],
        {},
        [],
        no_progress_actions=0,
    )
    assert path is not None
    payload = json.loads(path.read_text(encoding="utf-8"))
    del payload["no_progress_actions"]
    path.write_text(json.dumps(payload), encoding="utf-8")

    with pytest.raises(worker.ContextForkError, match="invalid key set"):
        worker._load_turn_checkpoint(config, "loop-agent/turn-001.json")


def test_tool_call_batch_cap_rejects_text_and_native_actions(tmp_path: Path) -> None:
    calls = [
        {"name": "read_batch", "arguments": {"paths": [f"file-{index}.txt"]}}
        for index in range(worker.MAX_TOOL_CALLS_PER_BATCH + 4)
    ]
    action_text = json.dumps({"type": "tool_call", "calls": calls})
    with pytest.raises(
        ValueError,
        match=r"tool_call calls\[20\] exceeds the maximum of 16 calls per batch",
    ):
        worker._parse_agent_action(action_text)

    native_result = _FakeCallResult("")
    native_result.tool_calls = [
        {"function": {"name": call["name"], "arguments": json.dumps(call["arguments"])}}
        for call in calls
    ]
    native_results = [native_result] * 3

    async def native_call(*_args: Any, **_kwargs: Any) -> _FakeCallResult:
        return native_results.pop(0)

    router = SimpleNamespace(call=native_call, declared_model=lambda _name: "")
    worktree = _make_worktree(tmp_path / "repo")
    outcome = asyncio.run(
        worker._run_agent_loop(
            config=_agent_config(worktree),
            router=router,
            tier=ProviderTier.FAST,
            model="loopback-model",
            worktree=worktree,
            writer=None,
            stop=threading.Event(),
            progress=worker.AgentProgress(),
        )
    )

    assert outcome["status"] == "failed"
    assert outcome["failure_reason"] == "agent emitted 3 consecutive invalid actions"
    assert "tool_call calls[20] exceeds the maximum of 16 calls per batch" in "\n".join(
        message["content"] for message in outcome["transcript"]
    )


@pytest.mark.parametrize("shell_check", [False, True])
def test_finish_does_not_require_a_ritual_shell_command(tmp_path: Path, shell_check: bool) -> None:
    worktree = _make_worktree(tmp_path / "repo")
    actions = [
        '{"name":"write_file","arguments":{"path":"note.txt","content":"hello\\n"}}',
    ]
    if shell_check:
        actions.append('{"type":"tool_call","name":"run_shell","arguments":{"cmd":["false"]}}')
    actions.append('{"type":"finish","summary":"updated note","objective_met":true}')
    router = _ScriptedRouter(actions)
    outcome = asyncio.run(_drive_loop(_agent_config(worktree), worktree, router))
    assert outcome["status"] == "succeeded"
    assert (worktree / "note.txt").read_text() == "hello\n"
    assert len(router.prompts) == 2 + int(shell_check)
    evidence = "\n".join(message["content"] for message in outcome["transcript"])
    assert "finish rejected" not in evidence
    if shell_check:
        assert "tool run_shell ok=False" in evidence


def test_finish_after_verified_change_succeeds(tmp_path: Path) -> None:
    repo = tmp_path / "repo"
    worktree = _make_worktree(repo)
    config = _agent_config(worktree)
    router = _ScriptedRouter(
        [
            '{"type":"plan","steps":["edit alpha.txt"]}',
            '{"type":"tool_call","name":"edit_file","arguments":'
            '{"path":"alpha.txt","old_string":"alpha-content","new_string":"ALPHA"}}',
            '{"type":"tool_call","name":"run_shell","arguments":{"cmd":["true"]}}',
            '{"type":"finish","summary":"verified edit","objective_met":true}',
        ]
    )

    outcome = asyncio.run(_drive_loop(config, worktree, router))

    assert outcome["status"] == "succeeded"
    assert outcome["summary"] == "verified edit"
    assert not any("finish rejected" in message["content"] for message in outcome["transcript"])


def test_plan_and_thought_round_trip_through_parser() -> None:
    assert worker._parse_agent_action('{"type":"plan","steps":["a","b"]}') == {
        "type": "plan",
        "steps": ["a", "b"],
    }
    assert worker._parse_agent_action('{"type":"plan","steps":["a"],"thought":"reasoning"}') == {
        "type": "plan",
        "steps": ["a"],
    }
    assert worker._parse_agent_action(
        '{"type":"tool_call","name":"read_batch","arguments":{"paths":["a.py"]},'
        '"thought":"need context"}'
    ) == {
        "type": "tool_call",
        "calls": [{"name": "read_batch", "arguments": {"paths": ["a.py"]}}],
    }
    assert worker._parse_agent_action(
        '{"type":"finish","summary":"done","objective_met":true,"thought":"verified"}'
    ) == {"type": "finish", "summary": "done", "objective_met": True}
    verbose = worker._parse_agent_action(
        json.dumps(
            {
                "type": "finish",
                "summary": "Useful result. " + "routine tool narration " * 80,
                "objective_met": True,
            }
        )
    )["summary"]
    assert len(verbose.encode("utf-8")) <= worker.MAX_SUMMARY_CHARS
    assert verbose == ("Useful result. " + "routine tool narration " * 80).strip()
    for bad, match in (
        ('{"type":"tool_call","calls":[]}', "non-empty array"),
        ('{"type":"tool_call","calls":[null]}', "calls\\[0\\] must be an object"),
        (
            '{"type":"tool_call","calls":[{"name":"nope","arguments":{}},'
            '{"name":"read_batch","arguments":3}]}',
            "calls\\[0\\].*calls\\[1\\]",
        ),
    ):
        with pytest.raises(ValueError, match=match):
            worker._parse_agent_action(bad)

    # Concatenated actions are rejected: exactly one top-level JSON object
    # is the contract; trailing content raises.
    with pytest.raises(ValueError, match="no trailing content"):
        worker._parse_agent_action(
            '{"type":"finish","summary":"done","objective_met":true}'
            '{"type":"tool_call","name":"read_batch","arguments":{"paths":["a.py"]}}'
        )
    # ZAI has repeatedly emitted this exact closer typo after otherwise valid
    # single-call batches. Normalize it only for read-only/inspection actions.
    assert worker._parse_agent_action(
        '{"type":"tool_call","calls":[{"name":"read_batch","arguments":'
        '{"paths":["a.py"]}]}]}'
    ) == {
        "type": "tool_call",
        "calls": [{"name": "read_batch", "arguments": {"paths": ["a.py"]}}],
    }
    with pytest.raises(ValueError, match="action is not valid JSON"):
        worker._parse_agent_action(
            '{"type":"tool_call","calls":[{"name":"run_shell","arguments":'
            '{"cmd":["python","-c","assert 2 + 3 == 5"],"timeout_s":30}]}]}'
        )

    for bad in (
        '{"type":"plan"}',
        '{"type":"plan","steps":[]}',
        '{"type":"plan","steps":["ok", 3]}',
        '{"type":"plan","steps":["ok"],"extra":1}',
        '{"type":"tool_call","name":"read_batch","arguments":{},"extra":1}',
        '{"type":"finish","summary":"done","objective_met":true,"extra":1}',
        '{"type":"finish","summary":"done"}',
        '{"type":"finish","summary":"done","objective_met":"yes"}',
    ):
        with pytest.raises(ValueError):
            worker._parse_agent_action(bad)


def test_parse_agent_action_normalizes_observed_provider_shapes() -> None:
    assert worker._parse_agent_action(
        '[{"calls":[{"name":"repo_query","action":"tree","limit":100}]}]'
    ) == {
        "type": "tool_call",
        "calls": [
            {"name": "repo_query", "arguments": {"action": "tree", "limit": 100}}
        ],
    }

    with pytest.raises(ValueError, match="no trailing content"):
        worker._parse_agent_action(
            '[{"calls":[{"name":"repo_query","action":"tree"}]}]'
            '{"type":"finish","summary":"done","objective_met":true}'
        )
    with pytest.raises(ValueError, match="exactly one JSON object"):
        worker._parse_agent_action(
            '[{"type":"plan","steps":["a"]},{"type":"plan","steps":["b"]}]'
        )
    with pytest.raises(ValueError, match="must carry exactly name/arguments"):
        worker._parse_agent_action('[{"calls":[{"name":"not_a_tool","action":"tree"}]}]')
    with pytest.raises(ValueError, match="unknown tool"):
        worker._parse_agent_action(
            '[{"calls":[{"name":"not_a_tool","arguments":{"action":"tree"}}]}]'
        )
    with pytest.raises(ValueError, match="must carry exactly name/arguments"):
        worker._parse_agent_action(
            '{"type":"tool_call","calls":[{"name":"repo_query",'
            '"arguments":{"action":"tree"},"action":"tree"}]}'
        )
    with pytest.raises(ValueError, match="arguments must be an object"):
        worker._parse_agent_action(
            '[{"calls":[{"name":"repo_query","arguments":3}]}]'
        )

    assert worker._parse_agent_action(
        '{"calls":['
        '{"name":"read_batch","paths":["a.py"]},'
        '{"name":"git_op","op":"status","args":"--short"}'
        "]}"
    ) == {
        "type": "tool_call",
        "calls": [
            {"name": "read_batch", "arguments": {"paths": ["a.py"]}},
            {"name": "git_op", "arguments": {"op": "status", "args": "--short"}},
        ],
    }
    for mutating in (
        '{"calls":[{"name":"edit_file","path":"a.py",'
        '"old_string":"old","new_string":"new"}]}',
        '{"calls":[{"name":"run_shell","cmd":["true"]}]}',
        '{"calls":[{"name":"git_op","op":"add","args":"."}]}',
    ):
        with pytest.raises(ValueError, match="must carry exactly name/arguments"):
            worker._parse_agent_action(mutating)


def test_parse_agent_action_accepts_fenced_tool_call() -> None:
    fenced = '```json\n{"type":"tool_call","name":"read_batch","arguments":{"paths":["a.py"]}}\n```'
    assert worker._parse_agent_action(fenced) == {
        "type": "tool_call",
        "calls": [{"name": "read_batch", "arguments": {"paths": ["a.py"]}}],
    }


def test_parse_agent_action_accepts_fenced_finish_with_backticks_in_body() -> None:
    fenced = '```\n{"type":"finish","summary":"kept ``` inline","objective_met":true}\n```'
    assert worker._parse_agent_action(fenced) == {
        "type": "finish",
        "summary": "kept ``` inline",
        "objective_met": True,
    }


def test_parse_agent_action_rejects_fenced_with_prose_or_unclosed_fence() -> None:
    prose = 'Here is the action:\n```json\n{"type":"plan","steps":["a"]}\n```'
    with pytest.raises(ValueError, match="not valid JSON"):
        worker._parse_agent_action(prose)
    unclosed = '```json\n{"type":"plan","steps":["a"]}\n'
    with pytest.raises(ValueError, match="not valid JSON"):
        worker._parse_agent_action(unclosed)
    two_fences = (
        '```json\n{"type":"plan","steps":["a"]}\n```\n```json\n{"type":"plan","steps":["b"]}\n```'
    )
    with pytest.raises(ValueError):
        worker._parse_agent_action(two_fences)


def test_lenient_parse_accepts_raw_control_characters_in_strings() -> None:
    action = (
        '{"type":"tool_call","name":"write_file","arguments":'
        '{"path":"hello.py","content":"print(\'hello world\')\n\t"}}'
    )
    assert worker._parse_agent_action(action) == {
        "type": "tool_call",
        "calls": [
            {
                "name": "write_file",
                "arguments": {"path": "hello.py", "content": "print('hello world')\n\t"},
            }
        ],
    }
    with pytest.raises(ValueError):
        worker._parse_agent_action('{"type":"finish","summary":"broken\n-oops}')


# ---------------------------------------------------------------------------
# Transcript summarization (pure function)
# ---------------------------------------------------------------------------


def test_summarize_transcript_large_trimmed_keeps_plan_and_marker(tmp_path: Path) -> None:
    plan_message = {
        "role": "assistant",
        "content": '{"type":"plan","steps":["first","second"]}',
    }
    transcript = [plan_message]
    for index in range(8):
        transcript.append(
            {
                "role": "assistant",
                "content": (
                    '{"type":"tool_call","name":"read_batch",'
                    f'"arguments":{{"paths":["f{index}.py"]}}}}'
                ),
            }
        )
        transcript.append({"role": "user", "content": "x" * 2_000})
    budget = 5_000
    snapshot = copy.deepcopy(transcript)

    result = worker._summarize_transcript(transcript, budget, keep_turns=6)

    assert worker._transcript_chars(result) <= budget
    assert result != transcript
    assert transcript == snapshot  # the input transcript is never mutated
    # the plan survives intact at the front
    assert result[0] == plan_message
    assert json.loads(result[0]["content"])["type"] == "plan"
    # a synthetic dropped-message marker reports what was removed
    assert any("prior context" in message.get("content", "") for message in result)
    marker = next(message for message in result if "prior context" in message.get("content", ""))
    # turn-atomic dropping: 4 turn pairs fall to the keep_turns window and
    # 4 more whole turns drop to fit the budget (12 messages total)
    assert "12 earlier message(s) dropped" in marker["content"]
    # exactly the newest 2 whole turns (4 messages) survive untruncated
    tail = result[2:]
    assert len(tail) == 4
    assert [message["role"] for message in tail] == (["assistant", "user"] * 2)
    assert "f7.py" in tail[-2]["content"]
    assert tail[-1]["content"] == "x" * 2_000  # whole turns, never sliced


def test_summarize_transcript_bounds_oversized_observation_inside_wrapper(
    tmp_path: Path,
) -> None:
    """A single oversized observation keeps its wrapper header; only the body
    is truncated, with a counted omitted-chars suffix (plan §9.1.1)."""
    body = "y" * 20_000
    transcript = [
        {"role": "assistant", "content": '{"type":"plan","steps":["read"]}'},
        {
            "role": "assistant",
            "content": (
                '{"type":"tool_call","name":"read_batch","arguments":{"paths":["big.txt"]}}'
            ),
        },
        {"role": "user", "content": f"tool read_batch ok=True\n--- big.txt ---\n{body}"},
    ]
    snapshot = copy.deepcopy(transcript)
    budget = 2_000

    result = worker._summarize_transcript(transcript, budget, keep_turns=6)

    assert transcript == snapshot
    assert worker._transcript_chars(result) <= budget
    observation = result[-1]
    assert observation["content"].startswith("tool read_batch ok=True\n")
    assert "--- big.txt ---\n" in observation["content"]
    assert "observation char(s) omitted]" in observation["content"]
    # the header and wrapper survive; the body carried the cut
    assert len(observation["content"]) < len(transcript[-1]["content"])


def test_render_rolling_compaction_wrapper_always_closed_and_parseable() -> None:
    """The rolling fold reserves wrapper overhead: the closing tag is never
    cut and the embedded JSON always parses, even at degenerate budgets."""
    continuation = [
        {"role": "user", "content": "child result " + "z" * 500},
        {"role": "assistant", "content": '{"type":"plan","steps":["go"]}'},
        {"role": "user", "content": "Continue."},
    ]
    snapshot = copy.deepcopy(continuation)
    for budget in (1, 50, 200, 5_000):
        rendered = worker._render_rolling_compaction(continuation, budget)
        assert len(rendered) == 1
        content = rendered[0]["content"]
        assert rendered[0]["role"] == "user"
        assert content.startswith("<cambium-rolling-context>\n")
        assert content.endswith("\n</cambium-rolling-context>")
        inner = content[len("<cambium-rolling-context>\n") : -len("\n</cambium-rolling-context>")]
        parsed = json.loads(inner)
        assert isinstance(parsed, list)
        # Degenerate budgets below the wrapper size still close cleanly.
        wrapper_floor = len("<cambium-rolling-context>\n") + 2 + len("\n</cambium-rolling-context>")
        if len(content) > wrapper_floor:
            assert len(content) <= budget
    assert continuation == snapshot


def test_agent_loop_bounds_transcript_before_every_provider_call(tmp_path: Path) -> None:
    repo = tmp_path / "repo"
    worktree = _make_worktree(repo)
    (worktree / "large.txt").write_text("x" * 20_000, encoding="utf-8")
    budget = 5_000
    config = replace(_agent_config(worktree, max_turns=20), max_transcript_chars=budget)
    # Distinct contents keep every action novel while still growing the
    # transcript past the budget.
    for index in range(8):
        (worktree / f"large{index}.txt").write_text(
            f"file-{index}\n" + "x" * 20_000, encoding="utf-8"
        )
    router = _ScriptedRouter(
        ['{"type":"plan","steps":["inspect repeatedly","finish"]}']
        + [
            '{"type":"tool_call","name":"read_batch","arguments":{"paths":'
            f'["large{index}.txt"]}}}}'
            for index in range(7)
        ]
        + ['{"type":"finish","summary":"bounded transcript","objective_met":true}']
    )

    outcome = asyncio.run(_drive_loop(config, worktree, router))

    assert outcome["status"] == "succeeded"
    assert len(router.prompts) == 9
    for prompt in router.prompts:
        transcript = prompt["messages"][1:]
        # The task text is fixed user-role data added at build time; the
        # budget bounds the growing transcript, not the static task block.
        if transcript and transcript[0].get("content", "").startswith("<cambium-task>"):
            transcript = transcript[1:]
        if transcript and transcript[-1].get("content") in {"Begin.", "Continue."}:
            transcript = transcript[:-1]
        if transcript and str(transcript[-1].get("content", "")).startswith("<cambium-loop-state>"):
            transcript = transcript[:-1]
        assert worker._transcript_chars(transcript) <= budget


def test_strip_for_fold_drops_obsolete_reads_and_superseded_passes() -> None:
    """Tier-1 stripping (§9.1.7): obsolete read bodies and superseded passing
    run_shell outputs collapse to one-line markers; edits, failures, the
    latest verification, identifiers, and the plan survive; idempotent."""

    def call(name: str, args: dict[str, Any]) -> dict[str, str]:
        return {
            "role": "assistant",
            "content": json.dumps(
                {
                    "type": "tool_call",
                    "name": name,
                    "arguments": args,
                }
            ),
        }

    def obs(name: str, ok: bool, body: str) -> dict[str, str]:
        return {"role": "user", "content": f"tool {name} ok={ok}\n{body}"}

    continuation = [
        {"role": "assistant", "content": '{"type":"plan","steps":["work"]}'},
        call("read_batch", {"paths": ["a.py", "b.py"]}),
        obs("read_batch", True, "--- a.py ---\nold a\n\n--- b.py ---\nold b"),
        call("edit_file", {"path": "a.py", "old_string": "old", "new_string": "new"}),
        obs("edit_file", True, "edited a.py"),
        call("run_shell", {"cmd": ["pytest", "-q"]}),
        obs("run_shell", True, "1 passed"),
        call("read_batch", {"paths": ["a.py"]}),
        obs("read_batch", True, "--- a.py ---\nnew a"),
        call("run_shell", {"cmd": ["pytest", "-q"]}),
        obs("run_shell", False, "2 failed"),
        call("run_shell", {"cmd": ["pytest", "-q"]}),
        obs("run_shell", True, "2 passed"),
    ]
    snapshot = copy.deepcopy(continuation)

    stripped = worker._strip_for_fold(continuation)

    assert continuation == snapshot  # pure: input never mutated
    assert len(stripped) == len(continuation)  # whole messages, none dropped
    # the plan and every identifier survive
    assert stripped[0] == continuation[0]
    # the first read is obsolete (a.py edited + re-read, b.py never re-read...
    # b.py has no later touch, so this read is NOT obsolete and stays whole)
    assert stripped[2] == continuation[2]
    # the passing run before the later failure+pass is superseded
    assert stripped[6]["content"] == (
        "tool run_shell ok=True\n[run_shell: passed (output omitted - superseded by a later run)]"
    )
    # the edit, the failure, and the latest passing verification stay whole
    assert stripped[4] == continuation[4]
    assert stripped[9] == continuation[9]
    assert stripped[11] == continuation[11]
    # idempotent
    assert worker._strip_for_fold(stripped) == stripped


def test_strip_for_fold_drops_fully_superseded_read_body() -> None:
    """A read whose every path is later edited or re-read collapses to the
    on-disk pointer with its paths (identifiers) preserved."""
    continuation = [
        {
            "role": "assistant",
            "content": json.dumps(
                {
                    "type": "tool_call",
                    "name": "read_batch",
                    "arguments": {"paths": ["a.py", "b.py"]},
                }
            ),
        },
        {"role": "user", "content": "tool read_batch ok=True\n--- a.py ---\nx\n\n--- b.py ---\ny"},
        {
            "role": "assistant",
            "content": json.dumps(
                {
                    "type": "tool_call",
                    "name": "edit_file",
                    "arguments": {"path": "a.py", "old_string": "x", "new_string": "z"},
                }
            ),
        },
        {"role": "user", "content": "tool edit_file ok=True\nedited"},
        {
            "role": "assistant",
            "content": json.dumps(
                {
                    "type": "tool_call",
                    "name": "read_batch",
                    "arguments": {"paths": ["b.py"]},
                }
            ),
        },
        {"role": "user", "content": "tool read_batch ok=True\n--- b.py ---\ny"},
    ]

    stripped = worker._strip_for_fold(continuation)

    assert stripped[1]["content"] == (
        "tool read_batch ok=True\n[read_batch: a.py, b.py (omitted - file on disk)]"
    )
    # the re-read of b.py is the latest read of that path: body kept
    assert stripped[5] == continuation[5]
    assert worker._strip_for_fold(stripped) == stripped


# ---------------------------------------------------------------------------
# Feedback loop: lint diagnostics from write_file reach the transcript
# ---------------------------------------------------------------------------


def test_lint_feedback_visible_in_transcript(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    repo = tmp_path / "repo"
    worktree = _make_worktree(repo)
    fake_bin = tmp_path / "bin"
    fake_bin.mkdir()
    fake_ruff = fake_bin / "ruff"
    fake_ruff.write_text(
        "#!" + sys.executable + "\n"
        "import json\n"
        "import sys\n"
        "print(json.dumps([{'filename': sys.argv[-1], 'code': 'invalid-syntax', "
        "'location': {'row': 1, 'column': 1}, 'message': 'fixture syntax error'}]))\n",
        encoding="utf-8",
    )
    fake_ruff.chmod(0o755)
    monkeypatch.setenv("PATH", os.pathsep.join((str(fake_bin), os.defpath)))
    config = _agent_config(worktree)
    router = _ScriptedRouter(
        [
            '{"type":"plan","steps":["write a file"]}',
            '{"type":"tool_call","name":"write_file","arguments":'
            '{"path":"broken.py","content":"broken(:\\n"}}',
            '{"type":"tool_call","name":"run_shell","arguments":{"cmd":["true"]}}',
            '{"type":"finish","summary":"wrote file","objective_met":true}',
        ]
    )

    outcome = asyncio.run(_drive_loop(config, worktree, router))

    assert outcome["status"] == "succeeded"
    observations = [
        message["content"]
        for message in outcome["transcript"]
        if "tool write_file ok=True" in message["content"]
    ]
    assert observations
    assert "Lint diagnostics:" in observations[0]
    assert "E999" in observations[0]


# ---------------------------------------------------------------------------
# Heartbeat drain: _run_task does not block on a heartbeat that sleeps long
# ---------------------------------------------------------------------------


def test_heartbeats_publish_visible_provider_transitions_without_waiting_for_cadence(
    tmp_path: Path,
) -> None:
    repo = tmp_path / "repo"
    worktree = _make_worktree(repo)
    config = _agent_config(worktree, heartbeat_interval_s=15.0)
    router = _StreamingScriptedRouter(
        ['{"type":"finish","summary":"done","objective_met":true}'],
        [("thinking", "consider"), ("text", "answer fragment")],
        delta_delay_s=0.08,
        hold_s=0.05,
    )

    outcome, messages = asyncio.run(_drive_loop_with_heartbeats(config, worktree, router))

    assert outcome["status"] == "succeeded"
    heartbeats = [message for message in messages if message["type"] == "heartbeat"]
    phases = [heartbeat.get("phase") for heartbeat in heartbeats]
    assert phases[:3] == ["waiting", "thinking", "streaming"]
    assert any(
        heartbeat.get("provider") == "loopback-provider"
        and heartbeat.get("model") == "loopback-model"
        for heartbeat in heartbeats
    )
    # Provider responses are internal JSON actions; the timeline shows stream
    # state/rate, not protocol fragments.
    assert not any(
        heartbeat.get("phase") == "streaming" and heartbeat.get("tail") for heartbeat in heartbeats
    )


def test_run_task_drain_uses_config_heartbeat_interval(tmp_path: Path) -> None:
    run = {
        "request_id": "run-drain",
        "task_id": "drain",
        "scratch_repo": str(tmp_path),
        "worktree_path": str(tmp_path / "wt"),
        "generation": "invalid",
    }
    config = worker.AgentConfig(
        task_id="drain",
        generation=1,
        task="",
        worktree=Path(run["worktree_path"]),
        base_commit=None,
        fanout_config=None,
        max_turns=1,
        max_tokens=200_000,
        shell_permission=False,
        network_permission=False,
        heartbeat_interval_s=3.0,
        max_wall_s=60.0,
        checkpoint_root=None,
    )
    writer = _FakeWriter()
    stop = threading.Event()

    async def _run() -> dict[str, Any]:
        return await worker._run_task(
            cast(asyncio.StreamWriter, writer), run, "drain", 1, stop, config
        )

    started = time.monotonic()
    outcome = asyncio.run(_run())
    elapsed = time.monotonic() - started

    assert outcome["status"] == "failed"  # fail-closed tasks fail fast
    assert outcome["failure_reason"] == (
        "task has no provider configuration (fanout_config); "
        "the deterministic marker worker was removed"
    )
    # the old code waited HEARTBEAT_INTERVAL_S + 1.0 == 2.0s; the fixed code
    # drains as soon as the heartbeat observes the stop flag (~50ms).
    assert elapsed < 1.5


# ---------------------------------------------------------------------------
# Plan-spin guard: consecutive plan actions without a tool call fail fast
# ---------------------------------------------------------------------------


def test_consecutive_plan_actions_fail_fast_with_no_progress_reason(
    tmp_path: Path,
) -> None:
    repo = tmp_path / "repo"
    worktree = _make_worktree(repo)
    config = _agent_config(worktree)
    router = _ScriptedRouter(
        [
            '{"type":"plan","steps":["a"]}',
            '{"type":"plan","steps":["a"]}',
            '{"type":"plan","steps":["a"]}',
            '{"type":"plan","steps":["a"]}',
            '{"type":"plan","steps":["e"]}',
            '{"type":"finish","summary":"must never be reached","objective_met":true}',
        ]
    )

    outcome = asyncio.run(_drive_loop(config, worktree, router))

    assert outcome["status"] == "failed"
    assert "no progress" in outcome["failure_reason"]
    assert outcome["turn"] == 3  # failed on the 3rd consecutive plan
    assert len(router.prompts) == 3  # no further router calls
    assert not any(
        "must never be reached" in message["content"] for message in outcome["transcript"]
    )


def test_plan_then_tool_resets_consecutive_plan_counter(tmp_path: Path) -> None:
    repo = tmp_path / "repo"
    worktree = _make_worktree(repo)
    config = _agent_config(worktree)
    router = _ScriptedRouter(
        [
            '{"type":"plan","steps":["read alpha"]}',
            '{"type":"plan","steps":["read alpha again"]}',
            '{"type":"tool_call","name":"read_batch","arguments":{"paths":["alpha.txt"]}}',
            '{"type":"plan","steps":["one more plan before finishing"]}',
            '{"type":"finish","summary":"read the file","objective_met":true}',
        ]
    )

    outcome = asyncio.run(_drive_loop(config, worktree, router))

    assert outcome["status"] == "succeeded"
    assert outcome["summary"] == "read the file"
    assert outcome["turn"] == 5
    assert len(router.prompts) == 5


def test_concatenated_actions_are_rejected(tmp_path: Path) -> None:
    """A response carrying several concatenated JSON actions is invalid:
    exactly one top-level object is the action contract, so the loop treats
    it as an invalid action and the model is told so."""
    repo = tmp_path / "repo"
    worktree = _make_worktree(repo)
    config = _agent_config(worktree)
    router = _ScriptedRouter(
        [
            '{"type":"tool_call","name":"read_batch","arguments":'
            '{"paths":["alpha.txt"]}}'
            '{"type":"tool_call","name":"read_batch","arguments":'
            '{"paths":["beta.txt"]}}',
            '{"type":"finish","summary":"read both files","objective_met":true}',
        ]
    )

    outcome = asyncio.run(_drive_loop(config, worktree, router))

    assert outcome["status"] == "succeeded"
    assert outcome["summary"] == "read both files"
    assert outcome["turn"] == 2
    assert any("no trailing content" in message["content"] for message in outcome["transcript"])


def test_three_invalid_actions_fail_fast_with_no_progress(tmp_path: Path) -> None:
    """Malformed responses remain inspectable without spending more model calls."""
    repo = tmp_path / "repo"
    worktree = _make_worktree(repo)
    config = _agent_config(worktree, checkpoint_root=tmp_path / "checkpoints")
    writer = _FakeWriter()
    router = _ScriptedRouter(
        [
            "not-json-one",
            "not-json-two",
            "not-json-three",
            '{"type":"finish","summary":"must never be reached","objective_met":true}',
        ]
    )

    outcome = asyncio.run(_drive_loop(config, worktree, router, writer))

    assert outcome["status"] == "failed"
    assert outcome["failure_reason"] == "agent emitted 3 consecutive invalid actions"
    assert "max turns exceeded" not in outcome["failure_reason"]
    assert outcome["turn"] == 3  # failed on the 3rd consecutive invalid action
    assert len(router.prompts) == 3  # no further router calls
    checkpoints = [m for m in writer.messages() if m["type"] == "checkpoint"]
    assert [m["turn"] for m in checkpoints] == [1, 2, 3]
    recorded = json.loads(Path(checkpoints[-1]["state_ref"]).read_text())["transcript"]
    assert recorded[-2] == {"role": "assistant", "content": "not-json-three"}
    correction = recorded[-1]["content"]
    assert "invalid action: action is not valid JSON" in correction
    assert "native function tools are present" in correction
    assert "native tool channel" in correction
    assert '{"calls":[{"name":"TOOL","arguments":{}}]}' in correction
    # exactly one occurrence: nested inside calls, never as a bare top-level shape
    assert correction.count('{"name":"TOOL"') == 1


def test_valid_action_resets_consecutive_invalid_action_bound(tmp_path: Path) -> None:
    repo = tmp_path / "repo"
    worktree = _make_worktree(repo)
    config = _agent_config(worktree)
    router = _ScriptedRouter(
        [
            "malformed-before-reset",
            '{"type":"plan","steps":["continue"]}',
            "malformed-after-reset-one",
            "malformed-after-reset-two",
            "malformed-after-reset-three",
        ]
    )

    outcome = asyncio.run(_drive_loop(config, worktree, router))

    assert outcome["status"] == "failed"
    assert outcome["failure_reason"] == "agent emitted 3 consecutive invalid actions"
    assert outcome["turn"] == 5
    assert len(router.prompts) == 5


def test_parse_repair_near_budget_can_still_use_tools(tmp_path: Path) -> None:
    worktree = _make_worktree(tmp_path / "repo")
    config = _agent_config(worktree, max_turns=3)
    router = _ScriptedRouter(
        [
            '{"type":"plan","steps":["read alpha"]}',
            '{"name":"read_batch","arguments":[]}',
            '{"name":"read_batch","arguments":{"paths":["alpha.txt"]}}',
            '{"type":"finish","summary":"read alpha","objective_met":true}',
        ]
    )

    outcome = asyncio.run(_drive_loop(config, worktree, router))

    assert outcome["status"] == "succeeded"
    assert len(router.prompts) == 4
    assert any(
        message.get("role") == "user" and "alpha-content" in message.get("content", "")
        for message in router.prompts[-1]["messages"]
    )


def test_tool_schema_failure_does_not_increment_invalid_action_bound(tmp_path: Path) -> None:
    repo = tmp_path / "repo"
    worktree = _make_worktree(repo)
    config = _agent_config(worktree)
    router = _ScriptedRouter(
        [
            '{"type":"tool_call","name":"read_batch","arguments":{}}',
            "malformed-one",
            "malformed-two",
            '{"type":"finish","summary":"tool feedback handled","objective_met":true}',
        ]
    )

    outcome = asyncio.run(_drive_loop(config, worktree, router))

    assert outcome["status"] == "succeeded"
    assert outcome["summary"] == "tool feedback handled"
    assert len(router.prompts) == 4


# ---------------------------------------------------------------------------
# Publish scan: incidental cache/build artifacts never block or enter the commit
# ---------------------------------------------------------------------------


def _base_commit(worktree: Path) -> str:
    return subprocess.run(
        ["git", "-C", str(worktree), "rev-parse", "HEAD"],
        check=True,
        capture_output=True,
        text=True,
    ).stdout.strip()


def _finalize_worktree_outcome(
    worktree: Path, config: worker.AgentConfig, run: dict[str, Any]
) -> dict[str, Any]:
    return worker._finalize_worktree(
        run=run,
        config=config,
        worktree=worktree,
        generation=config.generation,
        worker_identity="test-worker",
        stop=threading.Event(),
        loop_outcome={
            "status": "succeeded",
            "summary": "verified edit",
            "turn": 3,
            "usage": {"prompt_tokens": 7, "completion_tokens": 3, "total_tokens": 10},
            "provider": "loopback-provider",
            "model": "loopback-model",
            "latency_s": 0.01,
            "transcript": [],
            "commits_so_far": [],
        },
    )


def test_finalize_worktree_excludes_cache_artifacts_from_commit(tmp_path: Path) -> None:
    repo = tmp_path / "repo"
    worktree = _make_worktree(repo)
    (worktree / "main.py").write_text("def add(a, b):\n    return a + b\n", encoding="utf-8")
    subprocess.run(["git", "-C", str(worktree), "add", "main.py"], check=True, capture_output=True)
    subprocess.run(
        ["git", "-C", str(worktree), "commit", "-m", "add main.py"],
        check=True,
        capture_output=True,
    )
    base_commit = _base_commit(worktree)
    config = replace(_agent_config(worktree), base_commit=base_commit)

    # The agent's real change, left uncommitted in the worktree.
    (worktree / "main.py").write_text("def add(a, b):\n    return a - b\n", encoding="utf-8")
    # Incidental artifacts of the agent's verification tool use.
    pytest_cache = worktree / ".pytest_cache"
    pytest_cache.mkdir()
    (pytest_cache / ".gitignore").write_text("*\n", encoding="utf-8")
    (pytest_cache / "CACHEDIR.TAG").write_text("", encoding="utf-8")
    (pytest_cache / "README.md").write_text("", encoding="utf-8")
    pycache = worktree / "src" / "__pycache__"
    pycache.mkdir(parents=True)
    (pycache / "x.cpython-312.pyc").write_bytes(b"\x00")

    run = {"request_id": "test", "scratch_repo": str(repo)}
    outcome = _finalize_worktree_outcome(worktree, config, run)

    assert outcome["status"] == "succeeded"
    assert outcome["failure_reason"] is None
    assert outcome["requires_commit"] is False
    assert outcome["files_changed"] == ["main.py"]
    assert len(outcome["commits"]) == 1
    sha = outcome["commits"][0]
    committed = subprocess.run(
        [
            "git",
            "-C",
            str(worktree),
            "diff-tree",
            "--no-commit-id",
            "--name-only",
            "-r",
            sha,
        ],
        check=True,
        capture_output=True,
        text=True,
    ).stdout.splitlines()
    assert committed == ["main.py"]
    assert "main.py" in outcome["diff"]
    assert not any(
        ".pyc" in name or "__pycache__" in name or ".pytest_cache" in name for name in committed
    )


def test_finalize_worktree_only_cache_artifacts_is_true_noop(tmp_path: Path) -> None:
    repo = tmp_path / "repo"
    worktree = _make_worktree(repo)
    base_commit = _base_commit(worktree)
    config = replace(_agent_config(worktree), base_commit=base_commit)

    pytest_cache = worktree / ".pytest_cache"
    pytest_cache.mkdir()
    (pytest_cache / ".gitignore").write_text("*\n", encoding="utf-8")
    (pytest_cache / "CACHEDIR.TAG").write_text("", encoding="utf-8")

    run = {"request_id": "test", "scratch_repo": str(repo)}
    outcome = _finalize_worktree_outcome(worktree, config, run)

    assert outcome["status"] == "succeeded"
    assert outcome["failure_reason"] is None
    assert outcome["commits"] == []
    assert outcome["files_changed"] == []
    assert _base_commit(worktree) == base_commit


def test_requires_commit_defaults_and_passes_through_run_task() -> None:
    init = {"task_id": "requires-commit"}
    config = worker.AgentConfig.from_init(init)

    assert config.requires_commit is False
    assert (
        worker._merge_task_config(config, init, {"requires_commit": True}).requires_commit is True
    )
    with pytest.raises(ValueError, match="requires_commit"):
        worker.AgentConfig.from_init({**init, "requires_commit": "yes"})


def test_provider_router_explicit_empty_authorization_fails_closed() -> None:
    assert (
        worker.AgentConfig.from_init(
            {"task_id": "legacy", "authorized_providers": None}
        ).authorized_providers_explicit
        is False
    )
    with pytest.raises(worker.AllProvidersFailed) as raised:
        worker._provider_router(
            {"tier": "fast", "model": "loopback-model"},
            authorized_providers=(),
            authorized_providers_explicit=True,
        )

    failure = raised.value
    assert failure.providers_tried == ()
    assert failure.last_error is not None
    assert str(failure.last_error) == "authorized_providers explicitly empty"


def test_finalize_worktree_requires_commit_when_dirty_commit_is_not_produced(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    repo = tmp_path / "repo"
    worktree = _make_worktree(repo)
    base_commit = _base_commit(worktree)
    config = replace(_agent_config(worktree), base_commit=base_commit, requires_commit=True)
    (worktree / "alpha.txt").write_text("dirty content\n", encoding="utf-8")

    def no_commit(
        _worktree: Path,
        _generation: int,
        *args: str,
        cwd: str | Path | None = None,
    ) -> tuple[int, str, str]:
        del cwd
        return 0, "", ""

    monkeypatch.setattr(worker, "_fenced_git", no_commit)
    outcome = _finalize_worktree_outcome(
        worktree, config, {"request_id": "test", "scratch_repo": str(repo)}
    )

    assert outcome["status"] == "failed"
    assert outcome["failure_reason"] == "requires_commit unmet"
    assert outcome["commits"] == []


def test_requires_commit_doc_only_finish_publishes_commit(
    tmp_path: Path,
) -> None:
    repo = tmp_path / "repo"
    worktree = _make_worktree(repo)
    base_commit = _base_commit(worktree)
    config = replace(_agent_config(worktree), base_commit=base_commit, requires_commit=True)
    router = _ScriptedRouter(
        [
            '{"type":"plan","steps":["write the release notes","finish"]}',
            '{"type":"tool_call","name":"run_shell","arguments":{"cmd":['
            '"sh","-c","mkdir -p docs && printf \'%s\\n\' \'release notes\' '
            '> docs/release.md"]}}',
            '{"type":"finish","summary":"wrote release notes","objective_met":true}',
        ]
    )

    loop_outcome = asyncio.run(_drive_loop(config, worktree, router))
    assert loop_outcome["status"] == "succeeded"
    outcome = worker._finalize_worktree(
        run={"request_id": "test", "scratch_repo": str(repo)},
        config=config,
        worktree=worktree,
        generation=config.generation,
        worker_identity="test-worker",
        stop=threading.Event(),
        loop_outcome=loop_outcome,
    )
    outcome.update(request_id="test", task_id=config.task_id, generation=config.generation)
    writer = _FakeWriter()

    asyncio.run(worker._emit_result_envelope(cast(asyncio.StreamWriter, writer), outcome))

    envelope = writer.messages()[0]
    assert envelope["status"] == "succeeded"
    assert envelope["requires_commit"] is True
    assert len(envelope["commits"]) == 1
    assert envelope["files_changed"] == ["docs/release.md"]
    assert _base_commit(worktree) == envelope["commits"][0]


def test_requires_commit_clean_finish_fails(tmp_path: Path) -> None:
    repo = tmp_path / "repo"
    worktree = _make_worktree(repo)
    base_commit = _base_commit(worktree)
    config = replace(_agent_config(worktree), base_commit=base_commit, requires_commit=True)
    router = _ScriptedRouter(
        [
            '{"type":"plan","steps":["finish"]}',
            '{"type":"finish","summary":"nothing changed","objective_met":true}',
        ]
    )

    loop_outcome = asyncio.run(_drive_loop(config, worktree, router))
    assert loop_outcome["status"] == "succeeded"
    outcome = worker._finalize_worktree(
        run={"request_id": "test", "scratch_repo": str(repo)},
        config=config,
        worktree=worktree,
        generation=config.generation,
        worker_identity="test-worker",
        stop=threading.Event(),
        loop_outcome=loop_outcome,
    )

    assert outcome["status"] == "failed"
    assert outcome["failure_reason"] == "requires_commit unmet: no changes"
    assert outcome["commits"] == []


def test_clean_noop_envelope_reports_requires_commit_false(tmp_path: Path) -> None:
    repo = tmp_path / "repo"
    worktree = _make_worktree(repo)
    base_commit = _base_commit(worktree)
    config = replace(_agent_config(worktree), base_commit=base_commit)
    outcome = _finalize_worktree_outcome(
        worktree, config, {"request_id": "test", "scratch_repo": str(repo)}
    )
    outcome.update(
        request_id="test",
        task_id=config.task_id,
        generation=config.generation,
    )
    writer = _FakeWriter()

    asyncio.run(worker._emit_result_envelope(cast(asyncio.StreamWriter, writer), outcome))

    envelope = writer.messages()[0]
    assert envelope["status"] == "succeeded"
    assert envelope["commits"] == []
    assert envelope["requires_commit"] is False


# ---------------------------------------------------------------------------
# Tool progress delivery: a backpressured consumer never blocks or fails
# the tool, and the newest tail is always the last emitted delta
# ---------------------------------------------------------------------------


class _BackpressuredWriter(_FakeWriter):
    """A writer whose transport never accepts bytes: drain() blocks forever."""

    def __init__(self) -> None:
        super().__init__()
        self._blocked = asyncio.Event()

    async def drain(self) -> None:
        await self._blocked.wait()


def test_tool_progress_callback_delivers_pending_tail_before_completion(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        worker,
        "time",
        SimpleNamespace(
            monotonic=lambda: 1_000.0,
            monotonic_ns=lambda: 1_000_000_000_000,
            time=lambda: 1_000.0,
        ),
    )
    writer = _BackpressuredWriter()
    config = SimpleNamespace(task_id="t", generation=1)
    sink = worker._tool_progress_callback(
        cast(asyncio.StreamWriter, writer),
        config,  # type: ignore[arg-type]
        "run_shell",
        1,
    )

    sink("stdout", "first")
    sink("stdout", "FINAL ERROR")  # inside the 100ms throttle window

    deltas = [m for m in writer.messages() if m["type"] == "tool_output_delta"]
    assert [m["delta"] for m in deltas] == ["first"]

    asyncio.run(sink.flush())  # the loop's flush before the completion event

    deltas = [m for m in writer.messages() if m["type"] == "tool_output_delta"]
    assert [m["delta"] for m in deltas] == ["first", "FINAL ERROR"]


class _FrozenClockEventLoop(asyncio.SelectorEventLoop):
    """Keep asyncio timeout scheduling off the real clock for this scenario."""

    def time(self) -> float:
        return 1_000.0


def test_run_shell_delivery_survives_backpressure_and_reports_final_tail(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    fixed_clock = SimpleNamespace(
        monotonic=lambda: 1_000.0,
        monotonic_ns=lambda: 1_000_000_000_000,
        time=lambda: 1_000.0,
    )
    monkeypatch.setattr(
        worker,
        "time",
        fixed_clock,
    )
    monkeypatch.setattr(tools, "time", fixed_clock)
    repo = tmp_path / "repo"
    worktree = _make_worktree(repo)
    config = _agent_config(worktree)
    command = [
        sys.executable,
        "-u",
        "-c",
        "import os; os.write(1, b'first\\n'); os.write(1, b'FINAL ERROR')",
    ]
    router = _ScriptedRouter(
        [
            json.dumps({"type": "tool_call", "name": "run_shell", "arguments": {"cmd": command}}),
            '{"type":"finish","summary":"done","objective_met":true}',
        ]
    )
    writer = _BackpressuredWriter()

    async def _run() -> dict[str, Any]:
        # A drain() that never completes must not stall the loop: the tool's
        # inline delivery path never awaits the writer.
        return await asyncio.wait_for(
            worker._run_agent_loop(
                config=config,
                router=router,  # type: ignore[arg-type]
                tier=ProviderTier.FAST,
                model="loopback-model",
                worktree=worktree,
                writer=writer,  # type: ignore[arg-type]
                stop=threading.Event(),
                progress=worker.AgentProgress(),
            ),
            timeout=30.0,
        )

    outcome = asyncio.run(_run(), loop_factory=_FrozenClockEventLoop)

    assert outcome["status"] == "succeeded"
    deltas = [m for m in writer.messages() if m["type"] == "tool_output_delta"]
    assert deltas
    assert "first" in deltas[0]["delta"]
    # "first" + "FINAL ERROR" arrived well inside the throttle window: the
    # final emission still contains FINAL ERROR as the last delta.
    assert "FINAL ERROR" in deltas[-1]["delta"]
    tool_events = [m for m in writer.messages() if m["type"] == "tool_event"]
    assert tool_events
    assert tool_events[0]["tool"] == "run_shell"
    assert tool_events[0]["ok"] is True


def test_run_shell_still_times_out_with_backpressured_writer(tmp_path: Path) -> None:
    repo = tmp_path / "repo"
    worktree = _make_worktree(repo)
    config = _agent_config(worktree)
    command = [sys.executable, "-c", "import time; time.sleep(5)"]
    router = _ScriptedRouter(
        [
            json.dumps(
                {
                    "type": "tool_call",
                    "name": "run_shell",
                    "arguments": {"cmd": command, "timeout_s": 1},
                }
            ),
            '{"type":"finish","summary":"done","objective_met":true}',
        ]
    )
    writer = _BackpressuredWriter()

    async def _run() -> dict[str, Any]:
        return await asyncio.wait_for(
            worker._run_agent_loop(
                config=config,
                router=router,  # type: ignore[arg-type]
                tier=ProviderTier.FAST,
                model="loopback-model",
                worktree=worktree,
                writer=writer,  # type: ignore[arg-type]
                stop=threading.Event(),
                progress=worker.AgentProgress(),
            ),
            timeout=30.0,
        )

    started = time.monotonic()
    outcome = asyncio.run(_run())
    elapsed = time.monotonic() - started

    # The 1s tool timeout fired on schedule despite the blocked transport,
    # and the loop continued to the scripted finish.
    assert outcome["status"] == "succeeded"
    assert elapsed < 4.5
    tool_events = [m for m in writer.messages() if m["type"] == "tool_event"]
    assert tool_events
    assert tool_events[0]["tool"] == "run_shell"
    assert tool_events[0]["ok"] is False
    observations = [
        message["content"]
        for message in outcome["transcript"]
        if "timed out after 1s" in str(message.get("content", ""))
    ]
    assert observations
