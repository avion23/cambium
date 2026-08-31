"""Regression contracts for rolling-fold prompt placement and fork ordering."""

from __future__ import annotations

import asyncio
import json
import threading
from pathlib import Path
from typing import Any

from _helpers_g13 import init_worktree  # type: ignore[reportMissingImports]

from cambium import worker
from cambium.diffundo import ProviderTier
from cambium.summary_trunk import summary_entries


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
    def __init__(self, content: str) -> None:
        self.content = content
        self.model = "fake-model"
        self.usage = {"prompt_tokens": 7, "completion_tokens": 3, "total_tokens": 10}
        self.provider = "fake-provider"
        self.latency_s = 0.01
        self.estimated_cost_usd = 0.0
        self.retry_after_s: float | None = None
        self.request_rate_status: str | None = None
        self.account_quota_owner: str | None = None
        self.prompt_prefix_bytes: int | None = None
        self.provider_cache_hit: bool | None = None


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
        messages = prompt.get("messages")
        last_content = (
            messages[-1].get("content")
            if isinstance(messages, list) and messages and isinstance(messages[-1], dict)
            else None
        )
        if isinstance(last_content, str) and last_content.startswith("<cambium-summary-control>\n"):
            payload = last_content.removeprefix("<cambium-summary-control>\n").removesuffix(
                "\n</cambium-summary-control>"
            )
            control = json.loads(payload)
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
            return _FakeCallResult(json.dumps(summary, sort_keys=True, separators=(",", ":")))
        if not self.responses:
            raise AssertionError("router call with no scripted response")
        return _FakeCallResult(self.responses.pop(0))


def _make_worktree(repo: Path) -> Path:
    return init_worktree(
        repo,
        user_name="context-cache-test",
        user_email="context-cache@test",
        filename="alpha.txt",
        content="alpha-content\n",
        branch="context-cache",
        worktree_name="wt",
    )


def _agent_config(worktree: Path, **overrides: Any) -> worker.AgentConfig:
    fields: dict[str, Any] = {
        "task_id": "context-cache-agent",
        "generation": 1,
        "task": "read the files and finish",
        "worktree": worktree,
        "base_commit": None,
        "fanout_config": {},
        "max_turns": 10,
        "max_tokens": 200_000,
        "shell_permission": True,
        "network_permission": False,
        "heartbeat_interval_s": 0.05,
        "max_wall_s": 60.0,
    }
    fields.update(overrides)
    return worker.AgentConfig(**fields)


def _strict_child_envelope(summary: str) -> dict[str, Any]:
    return {
        "parent_task_id": "context-cache-agent",
        "unified_diff": "diff",
        "diff_truncated": False,
        "summary": summary,
        "metric_score": None,
        "metric_breakdown": {},
        "commits": ["c1"],
        "files_changed": ["b.txt"],
        "status": "succeeded",
    }


def _write_epoch(
    config: worker.AgentConfig,
    messages: list[dict[str, Any]],
) -> worker.ContextCheckpoint:
    checkpoint = worker._write_epoch_checkpoint(
        config,
        turn=1,
        epoch=1,
        messages=messages,
        provider="fake-provider",
        model="fake-model",
        tools_sha256=worker._sha256_hex(
            json.dumps(worker._exposed_tool_schemas(config), sort_keys=True).encode("utf-8")
        ),
        provider_compat={"fake-provider": ("loopback", None)},
    )
    assert checkpoint is not None
    return checkpoint


async def _drive_loop(
    config: worker.AgentConfig,
    worktree: Path,
    router: _ScriptedRouter,
    writer: _FakeWriter,
) -> dict[str, Any]:
    return await worker._run_agent_loop(
        config=config,
        router=router,  # type: ignore[arg-type]
        tier=ProviderTier.FAST,
        model="fake-model",
        worktree=worktree,
        writer=writer,  # type: ignore[arg-type]
        stop=threading.Event(),
        progress=worker.AgentProgress(),
        run_request_id="context-cache-test",
    )


def _messages_bytes(messages: list[dict[str, Any]]) -> bytes:
    return json.dumps(messages, ensure_ascii=False, separators=(",", ":")).encode("utf-8")


def _summary_indices(messages: list[dict[str, Any]]) -> list[int]:
    return [
        index
        for index, message in enumerate(messages)
        if str(message.get("content", "")).startswith("<cambium-summary-entry>\n")
    ]


def _is_summary_prompt(prompt: dict[str, Any]) -> bool:
    messages = prompt["messages"]
    return bool(messages) and str(messages[-1].get("content", "")).startswith(
        "<cambium-summary-control>\n"
    )


def test_summary_flush_keeps_head_and_appends_entry_at_head_length(
    tmp_path: Path,
) -> None:
    worktree = _make_worktree(tmp_path / "repo")
    checkpoint_root = tmp_path / "ckpts"
    base_config = _agent_config(worktree, checkpoint_root=checkpoint_root)
    checkpoint = _write_epoch(
        base_config,
        [
            {"role": "system", "content": "immutable system prompt"},
            {"role": "user", "content": "immutable tool schemas"},
        ],
    )
    original_checkpoint_bytes = (checkpoint_root / checkpoint.checkpoint_ref).read_bytes()
    child = _strict_child_envelope("seed")
    seed_chars = len(worker._child_result_lines(child))
    threshold_high = seed_chars + 1
    config = _agent_config(
        worktree,
        checkpoint_root=checkpoint_root,
        context_reuse=True,
        rolling_compact=True,
        rolling_compact_threshold_high=threshold_high,
        rolling_compact_threshold_low=max(1, threshold_high // 2),
        resume={
            "checkpoint_ref": checkpoint.checkpoint_ref,
            "epoch": checkpoint.epoch,
            "child_results": [child],
            "child_results_truncated": False,
            "workspace_changed": False,
        },
        max_turns=3,
    )
    plan = '{"type":"plan","steps":["continue"]}'
    router = _ScriptedRouter([plan, plan])
    writer = _FakeWriter()

    outcome = asyncio.run(_drive_loop(config, worktree, router, writer))

    assert outcome["status"] == "failed"
    action_prompts = [prompt for prompt in router.prompts if not _is_summary_prompt(prompt)]
    summary_prompts = [prompt for prompt in router.prompts if _is_summary_prompt(prompt)]
    assert len(action_prompts) == 2
    assert len(summary_prompts) == 1

    immutable_head = checkpoint.full_messages
    pre_flush = action_prompts[0]["messages"]
    post_flush = action_prompts[1]["messages"]
    assert pre_flush[: len(immutable_head)] == immutable_head
    assert post_flush[: len(immutable_head)] == immutable_head
    assert _messages_bytes(post_flush[: len(immutable_head)]) == _messages_bytes(
        pre_flush[: len(immutable_head)]
    )
    assert _summary_indices(post_flush) == [len(immutable_head)]
    assert len(post_flush) == len(immutable_head) + 2
    assert post_flush[-1]["role"] == "user"
    assert post_flush[-1]["content"].startswith("<cambium-loop-state>budget=")
    assert not any(
        "<cambium-rolling-context>" in str(message.get("content", "")) for message in post_flush
    )

    advanced = [
        message for message in writer.messages() if message["type"] == "context_epoch_advanced"
    ]
    assert [message["turn"] for message in advanced] == [checkpoint.turn + 2]
    folded = worker._load_epoch_checkpoint(
        config, advanced[0]["checkpoint_ref"], expect_task_id=True
    )
    assert folded.full_messages == post_flush[:-1]
    entries = summary_entries(folded.provider_messages)
    assert len(entries) == 1
    assert entries[0].sequence == 1
    assert entries[0].source_message_count == 3
    assert entries[0].through_turn == checkpoint.turn + 2
    assert (checkpoint_root / checkpoint.checkpoint_ref).read_bytes() == original_checkpoint_bytes


def test_summary_flush_appends_second_entry_after_raw_tail_crosses_threshold(
    tmp_path: Path,
) -> None:
    worktree = _make_worktree(tmp_path / "repo")
    checkpoint_root = tmp_path / "ckpts"
    base_config = _agent_config(worktree, checkpoint_root=checkpoint_root)
    checkpoint = _write_epoch(
        base_config,
        [
            {"role": "system", "content": "immutable system prompt"},
            {"role": "user", "content": "immutable tool schemas"},
        ],
    )
    threshold_high = 100
    threshold_low = 50
    config = _agent_config(
        worktree,
        checkpoint_root=checkpoint_root,
        context_reuse=True,
        rolling_compact=True,
        rolling_compact_threshold_high=threshold_high,
        rolling_compact_threshold_low=threshold_low,
        resume={
            "checkpoint_ref": checkpoint.checkpoint_ref,
            "epoch": checkpoint.epoch,
            "child_results": [
                _strict_child_envelope("x" * 300),
                _strict_child_envelope("y" * 300),
            ],
            "child_results_truncated": False,
            "workspace_changed": False,
        },
        max_turns=3,
    )
    read_call = '{"type":"tool_call","name":"read_batch","arguments":{"paths":["alpha.txt"]}}'
    plan = '{"type":"plan","steps":["continue"]}'
    router = _ScriptedRouter([read_call, plan])
    writer = _FakeWriter()

    outcome = asyncio.run(_drive_loop(config, worktree, router, writer))

    assert outcome["status"] == "failed"
    advanced = [
        message for message in writer.messages() if message["type"] == "context_epoch_advanced"
    ]
    assert [message["turn"] for message in advanced] == [
        checkpoint.turn + 1,
        checkpoint.turn + 2,
    ]
    assert [message["epoch"] for message in advanced] == [2, 3]

    action_prompts = [prompt for prompt in router.prompts if not _is_summary_prompt(prompt)]
    summary_prompts = [prompt for prompt in router.prompts if _is_summary_prompt(prompt)]
    assert len(action_prompts) == 2
    assert len(summary_prompts) == 2

    first_folded = worker._load_epoch_checkpoint(
        config, advanced[0]["checkpoint_ref"], expect_task_id=True
    )
    second_folded = worker._load_epoch_checkpoint(
        config, advanced[1]["checkpoint_ref"], expect_task_id=True
    )
    immutable_head = checkpoint.full_messages
    assert first_folded.provider_messages[: len(immutable_head)] == immutable_head
    assert second_folded.provider_messages[: len(first_folded.provider_messages)] == (
        first_folded.provider_messages
    )
    assert _messages_bytes(
        second_folded.provider_messages[: len(first_folded.provider_messages)]
    ) == _messages_bytes(first_folded.provider_messages)
    assert first_folded.continuation_suffix == []
    assert second_folded.continuation_suffix == []
    assert action_prompts[0]["messages"][:-1] == first_folded.full_messages
    assert action_prompts[1]["messages"][:-1] == second_folded.full_messages
    assert action_prompts[0]["messages"][-1]["content"].startswith(
        "<cambium-loop-state>budget="
    )
    assert action_prompts[1]["messages"][-1]["content"].startswith(
        "<cambium-loop-state>budget="
    )
    assert summary_prompts[1]["messages"][: len(first_folded.full_messages)] == (
        first_folded.full_messages
    )

    first_entries = summary_entries(first_folded.provider_messages)
    second_entries = summary_entries(second_folded.provider_messages)
    assert [entry.sequence for entry in first_entries] == [1]
    assert [entry.sequence for entry in second_entries] == [1, 2]
    assert second_entries[0] == first_entries[0]
    assert first_entries[0].source_message_count == 2
    assert second_entries[1].source_message_count == 2
    assert first_entries[0].through_turn == checkpoint.turn + 1
    assert second_entries[1].through_turn == checkpoint.turn + 2
    assert first_entries[0].source_sha256 != second_entries[1].source_sha256
