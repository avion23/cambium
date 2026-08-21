"""Regression contracts for rolling-fold prompt placement and fork ordering."""

from __future__ import annotations

import asyncio
import json
import subprocess
import threading
from pathlib import Path
from typing import Any

from cambium import worker
from cambium.diffundo import ProviderTier
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
    ) -> _FakeCallResult:
        self.prompts.append(prompt)
        if not self.responses:
            raise AssertionError("router call with no scripted response")
        return _FakeCallResult(self.responses.pop(0))


def _make_worktree(repo: Path) -> Path:
    repo.mkdir(parents=True)
    subprocess.run(
        ["git", "init", "-b", "main", str(repo)],
        check=True,
        capture_output=True,
    )
    subprocess.run(
        ["git", "-C", str(repo), "config", "user.name", "context-cache-test"],
        check=True,
    )
    subprocess.run(
        ["git", "-C", str(repo), "config", "user.email", "context-cache@test"],
        check=True,
    )
    (repo / "alpha.txt").write_text("alpha-content\n", encoding="utf-8")
    subprocess.run(["git", "-C", str(repo), "add", "-A"], check=True, capture_output=True)
    subprocess.run(
        ["git", "-C", str(repo), "commit", "-m", "initial"],
        check=True,
        capture_output=True,
    )
    worktree = repo.parent / "wt"
    subprocess.run(
        ["git", "-C", str(repo), "worktree", "add", "-b", "context-cache", str(worktree), "main"],
        check=True,
        capture_output=True,
    )
    write_generation(worktree, 1)
    return worktree


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


def _message_chars(messages: list[dict[str, Any]]) -> int:
    return sum(
        len(content)
        for message in messages
        if isinstance(content := message.get("content"), str)
    )


def _messages_bytes(messages: list[dict[str, Any]]) -> bytes:
    return json.dumps(messages, ensure_ascii=False, separators=(",", ":")).encode("utf-8")


def _rolling_indices(messages: list[dict[str, Any]]) -> list[int]:
    return [
        index
        for index, message in enumerate(messages)
        if "<cambium-rolling-context>" in str(message.get("content", ""))
    ]


def test_rolling_fold_keeps_prefix_and_places_marker_at_base_length(
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
        },
        max_turns=3,
    )
    router = _ScriptedRouter(['{"type":"plan","steps":["continue"]}'])

    asyncio.run(_drive_loop(config, worktree, router, _FakeWriter()))

    assert len(router.prompts) == 2
    pre_fold = router.prompts[0]["messages"]
    post_fold = router.prompts[1]["messages"]
    immutable_base = checkpoint.full_messages
    folded_indices = _rolling_indices(post_fold)
    assert len(folded_indices) == 1
    folded_index = folded_indices[0]
    assert folded_index == len(immutable_base)
    assert pre_fold[:folded_index] == immutable_base
    assert post_fold[:folded_index] == pre_fold[:folded_index]
    assert _messages_bytes(post_fold[:folded_index]) == _messages_bytes(
        pre_fold[:folded_index]
    )
    assert post_fold[folded_index]["role"] == "user"


def test_rolling_fold_refolds_once_after_full_hysteresis_width(
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
    threshold_high = 130
    threshold_low = 80
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
        },
        max_turns=4,
    )
    plan = '{"type":"plan","steps":["continue"]}'
    router = _ScriptedRouter([plan, plan, plan])
    writer = _FakeWriter()

    asyncio.run(_drive_loop(config, worktree, router, writer))

    advanced = [
        message
        for message in writer.messages()
        if message["type"] == "context_epoch_advanced"
    ]
    assert [message["turn"] for message in advanced] == [
        checkpoint.turn + 1,
        checkpoint.turn + 3,
    ]
    assert len([message["turn"] for message in advanced]) == len({
        message["turn"] for message in advanced
    })
    assert len(router.prompts) == 3
    base_length = len(checkpoint.full_messages)
    prompt_before_growth = router.prompts[0]["messages"]
    prompt_after_one_growth = router.prompts[1]["messages"]
    assert all(
        len(_rolling_indices(prompt["messages"])) == 1 for prompt in router.prompts
    )
    first_suffix = prompt_before_growth[base_length:]
    grown_suffix = prompt_after_one_growth[base_length:]
    assert len(first_suffix) == 1
    assert len(grown_suffix) == 3
    # The fold reserves wrapper overhead: the closing tag is never cut and
    # the embedded JSON parses (plan §9.1.1); the payload stays within the
    # low threshold unless the wrapper alone exceeds it.
    folded_content = first_suffix[0]["content"]
    assert folded_content.startswith("<cambium-rolling-context>\n")
    assert folded_content.endswith("\n</cambium-rolling-context>")
    inner = folded_content[
        len("<cambium-rolling-context>\n"): -len("\n</cambium-rolling-context>")
    ]
    assert isinstance(json.loads(inner), list)
    assert len(folded_content) <= threshold_low
    assert first_suffix[0] == grown_suffix[0]

    one_plan_growth = _message_chars(grown_suffix) - _message_chars(first_suffix)
    hysteresis_width = threshold_high - threshold_low
    assert 0 < one_plan_growth < hysteresis_width
    assert _message_chars(grown_suffix) <= threshold_high
    assert _message_chars(first_suffix) + (2 * one_plan_growth) > threshold_high


def test_fork_prompt_appends_continuation_after_immutable_base() -> None:
    base = [
        {"role": "system", "content": "immutable system prompt"},
        {"role": "user", "content": "immutable tool schemas"},
    ]
    continuation = [
        {"role": "user", "content": "folded continuation"},
        {"role": "user", "content": "child continuation"},
    ]
    original_base = [dict(message) for message in base]

    prompt = worker._fork_prompt(tuple(base), continuation)

    assert prompt["messages"] == [*base, *continuation]
    assert prompt["messages"][: len(base)] == base
    assert prompt["messages"][len(base):] == continuation
    assert base == original_base
