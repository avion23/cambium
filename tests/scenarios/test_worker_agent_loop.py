"""Worker agent-loop improvements: plan-before-act, transcript bounding,
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

from cambium import worker
from cambium.diffundo import ProviderTier, prompt_prefix_bytes, validate_prompt_structure
from cambium.fencing import write_generation


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
    ) -> _FakeCallResult:
        del tier, model, budget_usd, allow_model_substitution
        self.prompts.append(prompt)
        if not self.responses:
            raise AssertionError("router call with no scripted response")
        if self.delta_delay_s:
            await asyncio.sleep(self.delta_delay_s)
        if on_delta is not None:
            for kind, fragment in self.deltas:
                on_delta(kind, fragment)
        await asyncio.sleep(self.hold_s)
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
    ) -> _FakeCallResult:
        del tier, model, budget_usd
        self.prompts.append(prompt)
        self.allow_model_substitution.append(allow_model_substitution)
        messages = prompt.get("messages")
        last_content = (
            messages[-1].get("content")
            if isinstance(messages, list) and messages and isinstance(messages[-1], dict)
            else None
        )
        if isinstance(last_content, str) and last_content.startswith("<cambium-summary-control>\n"):
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
                last_content.removeprefix("<cambium-summary-control>\n").removesuffix(
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


def test_summary_flush_authorizes_substitution_and_preserves_provenance(tmp_path: Path) -> None:
    repo = tmp_path / "repo"
    worktree = _make_worktree(repo)
    config = _agent_config(
        worktree,
        context_reuse=True,
        checkpoint_root=tmp_path / "checkpoints",
        max_turns=1,
    )
    router = _SummaryFlushRouter()

    outcome = asyncio.run(_drive_loop(config, worktree, router))  # type: ignore[arg-type]

    assert outcome["status"] == "succeeded"
    assert outcome["provider"] == "healthy-substitute"
    assert outcome["fell_back_from"] == "dead-primary"
    assert router.allow_model_substitution == [False, True]
    metadata = worker._cumulative_provider_metadata(outcome)
    assert metadata is not None
    assert metadata["provider"] == "healthy-substitute"
    assert metadata["fell_back_from"] == "dead-primary"


def test_summary_flush_all_providers_dead_fails_cleanly(tmp_path: Path) -> None:
    repo = tmp_path / "repo"
    worktree = _make_worktree(repo)
    config = _agent_config(
        worktree,
        context_reuse=True,
        checkpoint_root=tmp_path / "checkpoints",
        max_turns=1,
    )
    router = _SummaryFlushRouter(all_providers_dead=True)

    outcome = asyncio.run(_drive_loop(config, worktree, router))  # type: ignore[arg-type]

    assert outcome["status"] == "failed"
    assert outcome["failure_reason"] == (
        "compaction_failed: summary provider call failed: RuntimeError"
    )
    assert router.allow_model_substitution == [False, True]


def test_malformed_summary_defers_and_task_completes(tmp_path: Path) -> None:
    repo = tmp_path / "repo"
    worktree = _make_worktree(repo)
    config = _agent_config(
        worktree,
        context_reuse=True,
        rolling_compact=True,
        rolling_compact_threshold_high=1,
        rolling_compact_threshold_low=1,
        checkpoint_root=tmp_path / "checkpoints",
        max_turns=10,
    )
    writer = _FakeWriter()
    router = _SummaryFlushRouter(
        malformed_summaries=1,
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


def test_two_malformed_summaries_fail_on_the_third_fold_attempt(tmp_path: Path) -> None:
    repo = tmp_path / "repo"
    worktree = _make_worktree(repo)
    config = _agent_config(
        worktree,
        context_reuse=True,
        rolling_compact=True,
        rolling_compact_threshold_high=1,
        rolling_compact_threshold_low=1,
        checkpoint_root=tmp_path / "checkpoints",
        max_turns=10,
    )
    writer = _FakeWriter()
    router = _SummaryFlushRouter(
        malformed_summaries=3,
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
    assert (
        len(
            [
                prompt
                for prompt in router.prompts
                if str(prompt["messages"][-1].get("content", "")).startswith(
                    "<cambium-summary-control>\n"
                )
            ]
        )
        == 3
    )


# ---------------------------------------------------------------------------
# Plan-before-act: plan action parses, is stored, and the loop proceeds
# ---------------------------------------------------------------------------


def test_build_agent_prompt_last_message_is_always_user() -> None:
    """Payloads must not end on a system/assistant message (ZAI/GLM 1214)."""
    prompt = worker._build_agent_prompt("edit a.txt", [{"name": "read_batch"}], [])
    messages = prompt["messages"]
    assert messages[0]["role"] == "system"
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
    # The static system prefix is unchanged across transcripts.
    assert prompt2["messages"][0]["content"] == messages[0]["content"]


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


def test_build_agent_prompt_head_is_byte_stable_across_transcript_growth() -> None:
    """A growing transcript (tool loop) never changes the leading system
    message, so the in-session prefix stays byte-stable per turn."""
    tools = [{"name": "read_batch", "parameters": {"type": "object", "properties": {}}}]
    identity = "codex/gpt-5.6-luna"
    task = "read the files and finish"
    fresh = worker._build_agent_prompt(task, tools, [], model_identity=identity)
    grown = worker._build_agent_prompt(
        task,
        tools,
        [
            {"role": "user", "content": "Begin."},
            {"role": "assistant", "content": '{"type": "tool_call", "name": "read_batch"}'},
            {"role": "user", "content": "tool read_batch ok=true"},
        ],
        model_identity=identity,
    )
    assert grown["messages"][0]["content"] == fresh["messages"][0]["content"]
    assert prompt_prefix_bytes(grown) == prompt_prefix_bytes(fresh)


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
    assert first_messages[-1]["content"] == (
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
    # A provider/model switch starts a new uncached baseline at the caller.
    assert worker._usage_budget_charge(cached, 0) == (15, 10)


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
    assert worker.FINAL_SYNTHESIS_MIN_HEADROOM_TOKENS == 4_000
    assert 4_000 > config.max_tokens


def test_max_turns_edge_injects_the_same_finalization_directive(tmp_path: Path) -> None:
    repo = tmp_path / "repo"
    worktree = _make_worktree(repo)
    config = _agent_config(worktree, max_turns=3)
    router = _ScriptedRouter(
        [
            '{"type":"tool_call","name":"read_batch","arguments":{"paths":["alpha.txt"]}}',
            '{"type":"finish","summary":"read the file","objective_met":false}',
        ]
    )

    outcome = asyncio.run(_drive_loop(config, worktree, router))

    assert outcome["status"] == "failed"
    assert outcome["failure_reason"] == (
        "forced finalization: investigation incomplete, no changes made"
    )
    assert len(router.prompts) == 2
    assert (
        sum(
            message.get("content") == worker.FINAL_SYNTHESIS_DIRECTIVE
            for message in router.prompts[1]["messages"]
        )
        == 1
    )


def test_build_agent_prompt_head_passes_d8c_lint() -> None:
    """The static head (first 3 lines) carries no volatile timestamp or
    request_id token; dynamic content sits at the bottom (D8c)."""
    tools = [{"name": "read_batch", "parameters": {"type": "object", "properties": {}}}]
    prompt = worker._build_agent_prompt("a task", tools, [], model_identity="codex/gpt-5.6-luna")
    validate_prompt_structure(prompt)  # raises PromptStructureError on churn
    head = prompt["messages"][0]["content"]
    assert "Task:" not in head  # dynamic content is user-role data, not head
    task_message = prompt["messages"][1]
    assert task_message["role"] == "user"
    assert task_message["content"] == "<cambium-task>\nTask: a task\n</cambium-task>"


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

    tool_names = [schema["name"] for schema in worker._exposed_tool_schemas(config)]
    assert "read_batch" in tool_names


def test_exposed_tool_schemas_offer_batch_reading_only(tmp_path: Path) -> None:
    repo = tmp_path / "repo"
    worktree = _make_worktree(repo)
    config = replace(_agent_config(worktree), shell_permission=False)

    tool_names = [schema["name"] for schema in worker._exposed_tool_schemas(config)]

    assert "read_batch" in tool_names
    assert "read_file" not in tool_names


def test_finish_after_failed_verification_is_rejected(tmp_path: Path) -> None:
    repo = tmp_path / "repo"
    worktree = _make_worktree(repo)
    config = _agent_config(worktree)
    router = _ScriptedRouter(
        [
            '{"type":"plan","steps":["write note.txt"]}',
            '{"type":"tool_call","name":"write_file","arguments":'
            '{"path":"note.txt","content":"hello\\n"}}',
            '{"type":"tool_call","name":"run_shell","arguments":{"cmd":["false"]}}',
            '{"type":"finish","summary":"tests failed anyway","objective_met":true}',
            '{"type":"finish","summary":"still unverified","objective_met":true}',
            '{"type":"tool_call","name":"read_batch","arguments":{"paths":["note.txt"]}}',
            '{"type":"tool_call","name":"run_shell","arguments":{"cmd":["true"]}}',
            '{"type":"finish","summary":"verified","objective_met":true}',
        ]
    )

    outcome = asyncio.run(_drive_loop(config, worktree, router))

    assert outcome["status"] == "succeeded"
    assert outcome["summary"] == "verified"
    assert outcome["turn"] == 8
    assert len(router.prompts) == 8
    rejected = [
        message["content"]
        for message in outcome["transcript"]
        if "finish rejected" in message["content"]
    ]
    assert len(rejected) == 2
    assert "verification command failed" in rejected[0]


def test_finish_after_edit_without_verification_is_rejected(tmp_path: Path) -> None:
    repo = tmp_path / "repo"
    worktree = _make_worktree(repo)
    config = _agent_config(worktree)
    router = _ScriptedRouter(
        [
            '{"type":"plan","steps":["edit alpha.txt"]}',
            '{"type":"tool_call","name":"edit_file","arguments":'
            '{"path":"alpha.txt","old_string":"alpha-content","new_string":"ALPHA"}}',
            '{"type":"finish","summary":"edited, no tests available","objective_met":true}',
            '{"type":"tool_call","name":"run_shell","arguments":{"cmd":["true"]}}',
            '{"type":"finish","summary":"edited and verified","objective_met":true}',
        ]
    )

    outcome = asyncio.run(_drive_loop(config, worktree, router))

    assert outcome["status"] == "succeeded"
    assert outcome["summary"] == "edited and verified"
    assert outcome["turn"] == 5
    rejected = [
        message["content"]
        for message in outcome["transcript"]
        if "finish rejected" in message["content"]
    ]
    assert len(rejected) == 1
    assert "did not run a successful verification command" in rejected[0]


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
    ) == {"type": "tool_call", "name": "read_batch", "arguments": {"paths": ["a.py"]}}
    assert worker._parse_agent_action(
        '{"type":"finish","summary":"done","objective_met":true,"thought":"verified"}'
    ) == {"type": "finish", "summary": "done", "objective_met": True}

    # Concatenated actions: the FIRST complete object is parsed; the rest is
    # surfaced via _action_trailing.
    assert worker._parse_agent_action(
        '{"type":"finish","summary":"done","objective_met":true}'
        '{"type":"tool_call","name":"read_batch","arguments":{"paths":["a.py"]}}'
    ) == {"type": "finish", "summary": "done", "objective_met": True}
    assert worker._action_trailing(
        '{"type":"finish","summary":"done","objective_met":true}'
        '{"type":"tool_call","name":"read_batch","arguments":{"paths":["a.py"]}}'
    ).startswith('{"type":"tool_call"')
    assert worker._action_trailing('{"type":"plan","steps":["a"]}') == ""
    assert worker._action_trailing('{"type":"plan"') == ""

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


def test_lenient_parse_accepts_raw_control_characters_in_strings() -> None:
    action = (
        '{"type":"tool_call","name":"write_file","arguments":'
        '{"path":"hello.py","content":"print(\'hello world\')\n\t"}}'
    )
    assert worker._parse_agent_action(action) == {
        "type": "tool_call",
        "name": "write_file",
        "arguments": {"path": "hello.py", "content": "print('hello world')\n\t"},
    }
    assert worker._action_trailing(action) == ""
    assert worker._action_trailing(action + '{"type":"plan","steps":["a"]}').startswith(
        '{"type":"plan"'
    )
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
        if transcript and str(transcript[-1].get("content", "")).startswith(
            "<cambium-loop-state>"
        ):
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


def test_heartbeats_report_waiting_then_streaming_tail(tmp_path: Path) -> None:
    repo = tmp_path / "repo"
    worktree = _make_worktree(repo)
    config = _agent_config(worktree)
    router = _StreamingScriptedRouter(
        ['{"type":"finish","summary":"done","objective_met":true}'],
        [("text", "answer fragment")],
        delta_delay_s=0.15,
    )

    outcome, messages = asyncio.run(_drive_loop_with_heartbeats(config, worktree, router))

    assert outcome["status"] == "succeeded"
    heartbeats = [message for message in messages if message["type"] == "heartbeat"]
    phases = [heartbeat.get("phase") for heartbeat in heartbeats]
    assert "waiting" in phases
    assert "streaming" in phases
    assert phases.index("waiting") < phases.index("streaming")
    assert any(heartbeat.get("tail") == "answer fragment" for heartbeat in heartbeats)


def test_heartbeats_stay_waiting_without_provider_deltas(tmp_path: Path) -> None:
    repo = tmp_path / "repo"
    worktree = _make_worktree(repo)
    config = _agent_config(worktree)
    router = _StreamingScriptedRouter(
        ['{"type":"finish","summary":"done","objective_met":true}']
    )

    outcome, messages = asyncio.run(_drive_loop_with_heartbeats(config, worktree, router))

    assert outcome["status"] == "succeeded"
    heartbeats = [message for message in messages if message["type"] == "heartbeat"]
    assert heartbeats
    assert {heartbeat.get("phase") for heartbeat in heartbeats} == {"waiting"}
    assert all("tail" not in heartbeat for heartbeat in heartbeats)


def test_heartbeat_tail_is_bounded_and_terminally_safe(tmp_path: Path) -> None:
    repo = tmp_path / "repo"
    worktree = _make_worktree(repo)
    config = _agent_config(worktree)
    fragment = "\x1b[31m" + ("x" * 200) + "\x1b[0m\nnext"
    router = _StreamingScriptedRouter(
        ['{"type":"finish","summary":"done","objective_met":true}'],
        [("output_text", fragment)],
        delta_delay_s=0.15,
    )

    outcome, messages = asyncio.run(_drive_loop_with_heartbeats(config, worktree, router))

    assert outcome["status"] == "succeeded"
    tails = [
        heartbeat["tail"]
        for heartbeat in messages
        if heartbeat["type"] == "heartbeat" and "tail" in heartbeat
    ]
    assert tails
    assert all(len(tail) <= 120 for tail in tails)
    assert all("\x1b" not in tail and "\n" not in tail for tail in tails)


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

    assert outcome["status"] == "failed"  # invalid generation fails fast
    assert outcome["failure_reason"] == "invalid worker generation"
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


def test_concatenated_actions_first_action_parsed_trailing_noted(tmp_path: Path) -> None:
    """A response carrying several concatenated JSON actions parses as the
    first action, notes the ignored trailing JSON to the model, and continues
    instead of failing as invalid."""
    repo = tmp_path / "repo"
    worktree = _make_worktree(repo)
    config = _agent_config(worktree)
    router = _ScriptedRouter(
        [
            '{"type":"plan","steps":["read both files"]}'
            '{"type":"tool_call","name":"read_batch","arguments":'
            '{"paths":["alpha.txt","beta.txt"]}}',
            '{"type":"finish","summary":"read both files","objective_met":true}',
        ]
    )

    outcome = asyncio.run(_drive_loop(config, worktree, router))

    assert outcome["status"] == "succeeded"
    assert outcome["summary"] == "read both files"
    assert outcome["turn"] == 2
    assert len(router.prompts) == 2
    assert any(
        "trailing JSON was ignored" in message["content"] for message in outcome["transcript"]
    )


def test_three_invalid_actions_fail_fast_with_no_progress(tmp_path: Path) -> None:
    """Distinct malformed responses hit the dedicated invalid-action bound."""
    repo = tmp_path / "repo"
    worktree = _make_worktree(repo)
    config = _agent_config(worktree)
    router = _ScriptedRouter(
        [
            "not-json-one",
            "not-json-two",
            "not-json-three",
            '{"type":"finish","summary":"must never be reached","objective_met":true}',
        ]
    )

    outcome = asyncio.run(_drive_loop(config, worktree, router))

    assert outcome["status"] == "failed"
    assert outcome["failure_reason"] == "agent emitted 3 consecutive invalid actions"
    assert "max turns exceeded" not in outcome["failure_reason"]
    assert outcome["turn"] == 3  # failed on the 3rd consecutive invalid action
    assert len(router.prompts) == 3  # no further router calls


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
