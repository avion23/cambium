"""Cache-first context reuse (plan phase 1): epoch checkpoints, suspend,
resume, and cache-compatible fork pinning.

Fast tests drive the real worker agent loop in-process against a scripted
fake router with a real worktree and real tool dispatch (no network, no
subprocess), plus pure unit tests of the strict validators. Slow tests run
the full supervisor with subprocess fake workers for suspend -> resume
orchestration and for fork pinning across the wire boundary.
"""

from __future__ import annotations

import asyncio
import json
import subprocess
import threading
from dataclasses import asdict
from pathlib import Path
from typing import Any, cast

import pytest

from cambium import worker
from cambium.context_policy import CastPolicy
from cambium.diffundo import ProviderTier
from cambium.fencing import write_generation
from cambium.redact import Redactor
from cambium.summary_trunk import is_k0_entry, summary_entries
from cambium.supervisor import (
    TaskResult,
    _bounded_resume_envelope,
    _invalid_context_checkpoint_fields,
    _invalid_usage_event_fields,
    _Runtime,
    read_events,
    run_plan,
)
from cambium.worker import ContextForkError

ROOT = Path(__file__).resolve().parents[2]
TEST_RESOURCE_THRESHOLDS = {
    "mem_available_frac": 0.0,
    "load1_per_cpu": 1_000_000.0,
    "disk_free": 0,
}

_STRICT_ENVELOPE_KEYS = {
    "parent_task_id",
    "unified_diff",
    "diff_truncated",
    "summary",
    "metric_score",
    "metric_breakdown",
    "commits",
    "files_changed",
    "status",
}


# ---------------------------------------------------------------------------
# Shared in-process harness
# ---------------------------------------------------------------------------


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
        self.model = "loopback-model"
        self.usage = {"prompt_tokens": 7, "completion_tokens": 3, "total_tokens": 10}
        self.provider = "loopback-provider"
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
    repo.mkdir(parents=True)
    subprocess.run(["git", "init", "-b", "main", str(repo)], check=True, capture_output=True)
    subprocess.run(["git", "-C", str(repo), "config", "user.name", "epochs-test"], check=True)
    subprocess.run(["git", "-C", str(repo), "config", "user.email", "epochs@test"], check=True)
    (repo / "alpha.txt").write_text("alpha-content\n", encoding="utf-8")
    subprocess.run(["git", "-C", str(repo), "add", "-A"], check=True, capture_output=True)
    subprocess.run(
        ["git", "-C", str(repo), "commit", "-m", "initial"], check=True, capture_output=True
    )
    worktree = repo.parent / "wt"
    subprocess.run(
        ["git", "-C", str(repo), "worktree", "add", "-b", "epochs", str(worktree), "main"],
        check=True,
        capture_output=True,
    )
    write_generation(worktree, 1)
    return worktree


def _agent_config(worktree: Path, **overrides: Any) -> worker.AgentConfig:
    fields: dict[str, Any] = {
        "task_id": "epoch-agent",
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


def _delegate_action(child_task_id: str) -> str:
    return (
        '{"type":"tool_call","name":"delegate","arguments":'
        + json.dumps(
            {
                "child_task_id": child_task_id,
                "kind": "test",
                "spec": {"task": "child task", "child_only": True},
            }
        )
        + "}"
    )


async def _drive_loop(
    config: worker.AgentConfig,
    worktree: Path,
    router: _ScriptedRouter,
    writer: _FakeWriter,
    run_request_id: str | None = None,
) -> dict[str, Any]:
    return await worker._run_agent_loop(
        config=config,
        router=router,  # type: ignore[arg-type]  # duck-typed Diffundo
        tier=ProviderTier.FAST,
        model="loopback-model",
        worktree=worktree,
        writer=writer,  # type: ignore[arg-type]  # duck-typed StreamWriter
        stop=threading.Event(),
        progress=worker.AgentProgress(),
        run_request_id=run_request_id,
    )


def _strict_child_envelope(
    status: str = "succeeded", summary: str = "child did the work"
) -> dict[str, Any]:
    return {
        "parent_task_id": "epoch-agent",
        "unified_diff": "diff",
        "diff_truncated": False,
        "summary": summary,
        "metric_score": None,
        "metric_breakdown": {},
        "commits": ["c1"],
        "files_changed": ["b.txt"],
        "status": status,
    }


def _write_epoch(
    config: worker.AgentConfig,
    *,
    epoch: int = 1,
    messages: list[dict[str, Any]] | None = None,
    provider: str = "loopback-provider",
    model: str = "loopback-model",
) -> worker.ContextCheckpoint:
    checkpoint = worker._write_epoch_checkpoint(
        config,
        turn=1,
        epoch=epoch,
        messages=messages
        or [
            {"role": "system", "content": "You are the agent."},
            {"role": "user", "content": "observe tool output"},
        ],
        provider=provider,
        model=model,
        tools_sha256=worker._sha256_hex(
            json.dumps(worker._exposed_tool_schemas(config), sort_keys=True).encode("utf-8")
        ),
        provider_compat={provider: ("loopback", None)},
    )
    assert checkpoint is not None
    return checkpoint


_DIGEST = "a" * 64
_REF = f"epoch-agent/epoch-001-{'a' * 16}-{'b' * 16}.json"


def _provider_boundary(provider: str = "p1", model: str = "m1") -> dict[str, Any]:
    return {
        "provider": provider,
        "endpoint": "https://api.example",
        "authmode": "api_key",
        "api_key_env": "PROVIDER_KEY",
        "provider_env_keys": ["PROVIDER_KEY"],
        "authorized_providers": [provider],
        "authorized_providers_explicit": True,
        "protocol": "openai",
        "model": model,
        "tier": "fast",
        "reasoning_effort": None,
        "provider_config_path": "/opt/cambium/providers.json",
    }


# ---------------------------------------------------------------------------
# Strict validators (worker side)
# ---------------------------------------------------------------------------


def test_validate_context_fork_strict_keys() -> None:
    digest = _DIGEST
    valid = {
        "checkpoint_ref": _REF,
        "provider": "p1",
        "model": "m1",
        "system_sha256": digest,
        "tools_sha256": digest,
        "prefix_sha256": digest,
        "suffix_sha256": digest,
        "full_sha256": digest,
        "prefix_bytes": 123,
        "provider_boundary": _provider_boundary(),
    }
    assert worker._validate_context_fork(valid) == valid
    assert worker._validate_context_fork(None) is None

    with pytest.raises(ContextForkError, match="unknown keys"):
        worker._validate_context_fork({**valid, "sneaky": 1})
    with pytest.raises(ContextForkError, match="checkpoint_ref"):
        worker._validate_context_fork({**valid, "checkpoint_ref": ""})
    with pytest.raises(ContextForkError, match="checkpoint_ref"):
        worker._validate_context_fork({**valid, "checkpoint_ref": "epoch-agent/../evil.json"})
    with pytest.raises(ContextForkError, match="model"):
        worker._validate_context_fork({**valid, "model": ""})
    with pytest.raises(ContextForkError, match="sha256"):
        worker._validate_context_fork({**valid, "system_sha256": "not-hex"})
    with pytest.raises(ContextForkError, match="prefix_bytes"):
        worker._validate_context_fork({**valid, "prefix_bytes": -1})
    with pytest.raises(ContextForkError, match="provider_boundary"):
        worker._validate_context_fork({**valid, "provider_boundary": {"provider": "p1"}})
    with pytest.raises(ContextForkError, match="object"):
        worker._validate_context_fork("bogus")  # type: ignore[arg-type]


def test_validate_resume_strict() -> None:
    payload = {
        "checkpoint_ref": _REF,
        "epoch": 1,
        "child_results": [_strict_child_envelope()],
        "child_results_truncated": False,
        "workspace_changed": False,
    }
    assert worker._validate_resume(payload) == payload
    assert worker._validate_resume(None) is None

    with pytest.raises(ContextForkError, match="unknown keys"):
        worker._validate_resume({**payload, "transcript": []})
    with pytest.raises(ContextForkError, match="epoch"):
        worker._validate_resume({**payload, "epoch": 0})
    with pytest.raises(ContextForkError, match="epoch"):
        worker._validate_resume({**payload, "epoch": True})
    with pytest.raises(ContextForkError, match="child_results"):
        worker._validate_resume({**payload, "child_results": "x"})
    with pytest.raises(ContextForkError, match="item cap"):
        worker._validate_resume({**payload, "child_results": [{}] * 100})
    with pytest.raises(ContextForkError, match="not a strict envelope"):
        worker._validate_resume({**payload, "child_results": [{"status": "succeeded"}]})
    with pytest.raises(ContextForkError, match="child_results_truncated"):
        worker._validate_resume({**payload, "child_results_truncated": "yes"})


def test_epoch_checkpoint_roundtrip_and_tamper(tmp_path: Path) -> None:
    config = _agent_config(tmp_path / "wt", checkpoint_root=tmp_path / "ckpts")
    checkpoint = _write_epoch(config)

    loaded = worker._load_epoch_checkpoint(config, checkpoint.checkpoint_ref, expect_task_id=True)
    assert loaded.epoch == checkpoint.epoch
    assert loaded.cache_key == checkpoint.cache_key
    assert loaded.checkpoint_ref == checkpoint.checkpoint_ref

    with pytest.raises(ContextForkError, match="task_id mismatch|invalid checkpoint_ref path"):
        worker._load_epoch_checkpoint(
            _agent_config(tmp_path / "wt", task_id="other", checkpoint_root=tmp_path / "ckpts"),
            checkpoint.checkpoint_ref,
            expect_task_id=True,
        )

    with pytest.raises(ContextForkError, match="generation mismatch"):
        worker._load_epoch_checkpoint(
            _agent_config(
                tmp_path / "wt",
                checkpoint_root=tmp_path / "ckpts",
                generation=2,
            ),
            checkpoint.checkpoint_ref,
            expect_task_id=True,
        )

    path = tmp_path / "ckpts" / checkpoint.checkpoint_ref
    data = json.loads(path.read_text(encoding="utf-8"))
    data["content"]["provider_messages"][1]["content"] = "tampered"
    path.write_text(json.dumps(data, sort_keys=True), encoding="utf-8")
    with pytest.raises(ContextForkError, match="persisted-address mismatch|prefix_sha256 mismatch"):
        worker._load_epoch_checkpoint(config, checkpoint.checkpoint_ref, expect_task_id=True)

    with pytest.raises(ContextForkError, match="unreadable"):
        worker._load_epoch_checkpoint(
            config,
            f"epoch-agent/epoch-001-{'c' * 16}-{'d' * 16}.json",
            expect_task_id=True,
        )
    with pytest.raises(ContextForkError, match="invalid checkpoint_ref path"):
        worker._load_epoch_checkpoint(config, "../evil.json", expect_task_id=True)


def test_redacted_epoch_checkpoint_roundtrip(tmp_path: Path) -> None:
    config = _agent_config(
        tmp_path / "wt",
        checkpoint_root=tmp_path / "ckpts",
        redactor=Redactor(secret_values={"SECRETXYZ"}),
    )
    checkpoint = _write_epoch(
        config,
        messages=[
            {"role": "system", "content": "You are the agent SECRETXYZ."},
            {"role": "user", "content": "observe SECRETXYZ"},
        ],
    )

    loaded = worker._load_epoch_checkpoint(config, checkpoint.checkpoint_ref, expect_task_id=True)
    assert checkpoint.cache_key.redacted is True
    assert loaded.cache_key.redacted is True
    assert loaded.cache_key == checkpoint.cache_key
    assert all(
        "SECRETXYZ" not in message["content"]
        for message in [*loaded.provider_messages, *loaded.continuation_suffix]
    )

    compatible, reason = worker._fork_cache_compatible(
        {"fanout_config": {"model": checkpoint.cache_key.model}},
        {"cache_key": asdict(checkpoint.cache_key)},
        frozenset({"loopback-provider"}),
    )
    assert not compatible
    assert reason == "checkpoint redacted"


def test_child_result_lines_and_fork_prompt() -> None:
    lines = worker._child_result_lines(_strict_child_envelope())
    assert "Child task result:" in lines
    assert "succeeded" in lines
    assert "child did the work" in lines
    assert "b.txt" in lines

    prompt = worker._fork_prompt(
        cast(Any, {"role": "system", "content": "sys"}),
        [{"role": "user", "content": "obs"}],
    )
    assert prompt["messages"][-1]["role"] == "user"
    assert prompt["messages"][-1]["content"] != "Continue."
    plan_tail = worker._fork_prompt(
        cast(Any, {"role": "system", "content": "sys"}),
        [
            {"role": "user", "content": "obs"},
            {"role": "assistant", "content": '{"type": "plan", "steps": []}'},
        ],
    )
    assert plan_tail["messages"][-1]["content"] == "Continue."


def test_fork_cache_compatible_matrix() -> None:
    tools_sha = worker._provider_task_tools_hash()
    epoch: dict[str, Any] = {
        "epoch": 1,
        "checkpoint_ref": _REF,
        "cache_key": {
            "provider": "fake-provider",
            "model": "fake-model",
            "protocol": "loopback",
            "reasoning_effort": None,
            "tools_sha256": tools_sha,
            "redacted": False,
            "provider_boundary": _provider_boundary("fake-provider", "fake-model"),
        },
    }
    child = {
        "fanout_config": {"model": "fake-model"},
        "authorized_providers": ["fake-provider"],
    }
    compatible, reason = worker._fork_cache_compatible(child, epoch, frozenset({"fake-provider"}))
    assert compatible and reason is None

    incompatible, reason = worker._fork_cache_compatible(
        child, epoch, frozenset({"other-provider"})
    )
    assert not incompatible and "not authorized" in (reason or "")

    # An empty authorized set is the "unrestricted" wire value (worker.py
    # routing semantics), so it must not reject an otherwise-matching fork.
    unrestricted, reason = worker._fork_cache_compatible(child, epoch, frozenset())
    assert unrestricted and reason is None

    incompatible, reason = worker._fork_cache_compatible(
        {**child, "fanout_config": {"model": "other-model"}},
        epoch,
        frozenset({"fake-provider"}),
    )
    assert not incompatible and "model differs" in (reason or "")

    incompatible, reason = worker._fork_cache_compatible(
        {**child, "fanout_config": None},
        epoch,
        frozenset({"fake-provider"}),
    )
    assert not incompatible and "model differs" in (reason or "")

    incompatible, reason = worker._fork_cache_compatible(
        {**child, "fanout_config": {"model": "fake-model", "protocol": "other"}},
        epoch,
        frozenset({"fake-provider"}),
    )
    assert not incompatible and "protocol differs" in (reason or "")

    incompatible, reason = worker._fork_cache_compatible(
        {**child, "fanout_config": {"model": "fake-model", "reasoning_effort": "high"}},
        epoch,
        frozenset({"fake-provider"}),
    )
    assert not incompatible and "reasoning" in (reason or "")

    incompatible, reason = worker._fork_cache_compatible(
        child,
        {**epoch, "cache_key": {**epoch["cache_key"], "redacted": True}},
        frozenset({"fake-provider"}),
    )
    assert not incompatible and "redacted" in (reason or "")

    incompatible, reason = worker._fork_cache_compatible(
        child,
        {**epoch, "cache_key": {**epoch["cache_key"], "tools_sha256": "b" * 64}},
        frozenset({"fake-provider"}),
    )
    assert not incompatible and "tool schema" in (reason or "")

    incompatible, reason = worker._fork_cache_compatible(
        child, {"epoch": 1, "cache_key": None}, frozenset()
    )
    assert not incompatible and reason is not None


# ---------------------------------------------------------------------------
# In-process loop: suspend at the delegate boundary
# ---------------------------------------------------------------------------


def test_suspend_cuts_epoch_at_delegate_boundary(tmp_path: Path) -> None:
    worktree = _make_worktree(tmp_path / "repo")
    config = _agent_config(worktree, checkpoint_root=tmp_path / "ckpts", context_reuse=True)
    writer = _FakeWriter()
    router = _ScriptedRouter([_delegate_action("child-1")])

    outcome = asyncio.run(_drive_loop(config, worktree, router, writer))

    assert outcome["status"] == "suspended"
    assert outcome["epoch"] == 1
    assert isinstance(outcome["checkpoint_ref"], str) and outcome["checkpoint_ref"]
    assert outcome["transcript"]

    checkpoint = worker._load_epoch_checkpoint(
        config, outcome["checkpoint_ref"], expect_task_id=True
    )
    assert checkpoint.epoch == 1
    assert checkpoint.cache_key.model == "loopback-model"

    messages = writer.messages()
    kinds = [m["type"] for m in messages]
    assert "context_checkpoint" in kinds
    assert "propose_child" in kinds
    checkpoint_msg = next(m for m in messages if m["type"] == "context_checkpoint")
    assert checkpoint_msg["checkpoint_ref"] == outcome["checkpoint_ref"]
    usage = [m for m in messages if m["type"] == "usage_event"]
    assert usage and "epoch" not in usage[0]  # pre-epoch turns carry no epoch


def test_finish_cuts_terminal_epoch_when_context_reuse_enabled(
    tmp_path: Path,
) -> None:
    worktree = _make_worktree(tmp_path / "repo")
    config = _agent_config(worktree, checkpoint_root=tmp_path / "ckpts", context_reuse=True)
    writer = _FakeWriter()
    router = _ScriptedRouter(['{"type":"finish","summary":"done"}'])

    outcome = asyncio.run(_drive_loop(config, worktree, router, writer))

    assert outcome["status"] == "succeeded"
    checkpoints = [
        message for message in writer.messages() if message["type"] == "context_checkpoint"
    ]
    assert len(checkpoints) == 1
    assert checkpoints[0]["epoch"] == 1
    checkpoint_ref = checkpoints[0]["checkpoint_ref"]
    assert isinstance(checkpoint_ref, str) and checkpoint_ref
    checkpoint = worker._load_epoch_checkpoint(config, checkpoint_ref, expect_task_id=True)
    assert checkpoint.epoch == 1
    assert checkpoint.continuation_suffix == []
    assert "<cambium-summary-entry>" in checkpoint.provider_messages[-1]["content"]
    usage = [message for message in writer.messages() if message["type"] == "usage_event"]
    assert usage and all("epoch" not in message for message in usage)


def test_cast_rollover_is_durable_before_epoch_publication(tmp_path: Path) -> None:
    worktree = _make_worktree(tmp_path / "repo")
    checkpoint_root = tmp_path / "ckpts"
    config = _agent_config(
        worktree,
        checkpoint_root=checkpoint_root,
        context_reuse=True,
        rolling_compact_threshold_high=1,
        rolling_compact_threshold_low=1,
        cast_policy=CastPolicy(max_segments=1),
    )
    writer = _FakeWriter()
    router = _ScriptedRouter(
        [
            '{"type":"plan","steps":["inspect"]}',
            '{"type":"finish","summary":"done"}',
        ]
    )

    outcome = asyncio.run(_drive_loop(config, worktree, router, writer))

    assert outcome["status"] == "succeeded"
    advanced = [
        message
        for message in writer.messages()
        if message["type"] == "context_epoch_advanced"
        and message.get("reason") == "cast_k0_rollover"
    ]
    assert advanced
    checkpoint = worker._load_epoch_checkpoint(
        config,
        advanced[-1]["checkpoint_ref"],
        expect_task_id=True,
    )
    entries = summary_entries(checkpoint.provider_messages)
    assert len(entries) == 1
    assert is_k0_entry(entries[0])
    manifests = list((checkpoint_root / config.task_id / "rollovers").glob("k0-*.json"))
    assert len(manifests) == 1
    manifest = json.loads(manifests[0].read_text(encoding="utf-8"))
    assert manifest["schema"] == "cambium.cast-rollover.v1"
    assert manifest["source_sha256"] == entries[0].source_sha256
    assert len(manifest["entries"]) == 2


def test_no_suspend_when_context_reuse_disabled(tmp_path: Path) -> None:
    worktree = _make_worktree(tmp_path / "repo")
    config = _agent_config(worktree, checkpoint_root=tmp_path / "ckpts", context_reuse=False)
    writer = _FakeWriter()
    router = _ScriptedRouter(
        [
            _delegate_action("child-1"),
            '{"type":"finish","summary":"done"}',
        ]
    )

    outcome = asyncio.run(_drive_loop(config, worktree, router, writer))

    assert outcome["status"] == "succeeded"
    kinds = [m["type"] for m in writer.messages()]
    assert "context_checkpoint" not in kinds
    assert not list((tmp_path / "ckpts").glob("*/epoch-*.json"))


# ---------------------------------------------------------------------------
# In-process loop: resume re-seeds the transcript from the checkpoint
# ---------------------------------------------------------------------------


def test_resume_seeds_transcript_and_usage_epoch(tmp_path: Path) -> None:
    worktree = _make_worktree(tmp_path / "repo")
    config = _agent_config(worktree, checkpoint_root=tmp_path / "ckpts")
    checkpoint = _write_epoch(config)
    resume_config = _agent_config(
        worktree,
        checkpoint_root=tmp_path / "ckpts",
        context_reuse=True,
        resume={
            "checkpoint_ref": checkpoint.checkpoint_ref,
            "epoch": checkpoint.epoch,
            "child_results": [_strict_child_envelope()],
            "child_results_truncated": False,
            "workspace_changed": False,
        },
    )
    writer = _FakeWriter()
    router = _ScriptedRouter(['{"type":"finish","summary":"resumed and done"}'])

    outcome = asyncio.run(_drive_loop(resume_config, worktree, router, writer))

    assert outcome["status"] == "succeeded"
    first = router.prompts[0]["messages"]
    assert first[0]["role"] == "system"
    contents = " ".join(m.get("content", "") for m in first if isinstance(m.get("content"), str))
    assert "observe tool output" in contents
    assert "Child task result:" in contents
    assert "child did the work" in contents
    usage = [m for m in writer.messages() if m["type"] == "usage_event"]
    assert usage and usage[0]["epoch"] == checkpoint.epoch
    assert "fork_of" not in usage[0]


def test_resume_continuation_guard_preserves_checkpoint_prefix(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    worktree = _make_worktree(tmp_path / "repo")
    config = _agent_config(worktree, checkpoint_root=tmp_path / "ckpts")
    checkpoint = _write_epoch(config)
    prefix = checkpoint.full_messages
    original_checkpoint_bytes = (tmp_path / "ckpts" / checkpoint.checkpoint_ref).read_bytes()
    monkeypatch.setattr(worker, "MAX_CONTEXT_MESSAGES", 4)
    resume_config = _agent_config(
        worktree,
        checkpoint_root=tmp_path / "ckpts",
        context_reuse=True,
        max_transcript_chars=100_000,
        resume={
            "checkpoint_ref": checkpoint.checkpoint_ref,
            "epoch": checkpoint.epoch,
            "child_results": [_strict_child_envelope()],
            "child_results_truncated": False,
            "workspace_changed": False,
        },
    )
    writer = _FakeWriter()
    router = _ScriptedRouter(
        [
            '{"type":"tool_call","name":"read_batch","arguments":{"paths":["alpha.txt"]}}',
            '{"type":"tool_call","name":"read_batch","arguments":{"paths":["alpha.txt"]}}',
            '{"type":"finish","summary":"done"}',
        ]
    )

    outcome = asyncio.run(_drive_loop(resume_config, worktree, router, writer))

    assert outcome["status"] == "succeeded"
    action_prompts = [
        prompt
        for prompt in router.prompts
        if not str(prompt["messages"][-1].get("content", "")).startswith(
            "<cambium-summary-control>"
        )
    ]
    summary_prompts = [
        prompt
        for prompt in router.prompts
        if str(prompt["messages"][-1].get("content", "")).startswith("<cambium-summary-control>")
    ]
    assert len(action_prompts) == 3
    assert len(summary_prompts) == 2
    assert all(prompt["messages"][: len(prefix)] == prefix for prompt in action_prompts)
    assert all(
        len(prompt["messages"]) - len(prefix) <= worker.MAX_CONTEXT_MESSAGES
        for prompt in action_prompts
    )
    assert (
        tmp_path / "ckpts" / checkpoint.checkpoint_ref
    ).read_bytes() == original_checkpoint_bytes
    usage = [message for message in writer.messages() if message["type"] == "usage_event"]
    assert usage[0]["epoch"] == checkpoint.epoch
    assert usage[-1]["epoch"] == checkpoint.epoch + 1
    assert any(message["type"] == "context_epoch_advanced" for message in writer.messages())
    terminal = [message for message in writer.messages() if message["type"] == "context_checkpoint"]
    assert len(terminal) == 1
    assert terminal[0]["epoch"] == checkpoint.epoch + 3


def test_resume_missing_checkpoint_fails_closed(tmp_path: Path) -> None:
    worktree = _make_worktree(tmp_path / "repo")
    config = _agent_config(
        worktree,
        checkpoint_root=tmp_path / "ckpts",
        context_reuse=True,
        resume={
            "checkpoint_ref": "epoch-agent/epoch-001-missing.json",
            "epoch": 1,
            "child_results": [],
            "child_results_truncated": False,
            "workspace_changed": False,
        },
    )
    writer = _FakeWriter()
    router = _ScriptedRouter([])

    outcome = asyncio.run(_drive_loop(config, worktree, router, writer))

    assert outcome["status"] == "failed"
    assert "context_resume_failed" in (outcome["failure_reason"] or "")


def test_rolling_compact_config_defaults_on() -> None:
    init = {
        "task_id": "epoch-agent",
        "context_reuse": True,
        "rolling_compact": True,
        "max_transcript_chars": 200,
        "rolling_compact_threshold_high": 100,
    }
    config = worker.AgentConfig.from_init(init)
    assert config.rolling_compact is True
    assert config.rolling_compact_threshold_high == 100
    assert config.rolling_compact_threshold_low == 50

    defaulted = worker.AgentConfig.from_init(
        {
            "task_id": "default-rolling",
            "context_reuse": True,
        }
    )
    assert defaulted.rolling_compact is True


def test_rolling_compact_fold_advances_epoch_and_preserves_head(
    tmp_path: Path,
) -> None:
    worktree = _make_worktree(tmp_path / "repo")
    base_config = _agent_config(worktree, checkpoint_root=tmp_path / "ckpts")
    checkpoint = _write_epoch(base_config)
    old_bytes = (tmp_path / "ckpts" / checkpoint.checkpoint_ref).read_bytes()
    resume = {
        "checkpoint_ref": checkpoint.checkpoint_ref,
        "epoch": checkpoint.epoch,
        "child_results": [
            _strict_child_envelope(summary="a" * 140),
            _strict_child_envelope(summary="b" * 140),
        ],
        "child_results_truncated": False,
        "workspace_changed": False,
    }
    config = _agent_config(
        worktree,
        checkpoint_root=tmp_path / "ckpts",
        context_reuse=True,
        rolling_compact=True,
        rolling_compact_threshold_high=100,
        rolling_compact_threshold_low=50,
        resume=resume,
        max_turns=2,
    )
    writer = _FakeWriter()
    outcome = asyncio.run(
        _drive_loop(
            config,
            worktree,
            _ScriptedRouter(['{"type":"plan","steps":["continue"]}']),
            writer,
            run_request_id="run-compact",
        )
    )

    assert outcome["status"] == "failed"
    advanced = [
        message for message in writer.messages() if message["type"] == "context_epoch_advanced"
    ]
    assert len(advanced) == 1
    assert advanced[0] == {
        "type": "context_epoch_advanced",
        "request_id": "run-compact",
        "task_id": "epoch-agent",
        "generation": 1,
        "epoch": 2,
        "turn": 2,
        "checkpoint_ref": advanced[0]["checkpoint_ref"],
        "cache_key": advanced[0]["cache_key"],
        "folded_from_epoch": 1,
        "reason": None,
    }
    folded = worker._load_epoch_checkpoint(
        config, advanced[0]["checkpoint_ref"], expect_task_id=True
    )
    assert folded.epoch == 2
    assert folded.provider_messages[: len(checkpoint.provider_messages)] == (
        checkpoint.provider_messages
    )
    assert len(folded.provider_messages) == len(checkpoint.provider_messages) + 1
    assert "<cambium-summary-entry>" in folded.provider_messages[-1]["content"]
    assert folded.continuation_suffix == []
    assert (tmp_path / "ckpts" / checkpoint.checkpoint_ref).read_bytes() == old_bytes
    assert len(list((tmp_path / "ckpts" / "epoch-agent").glob("epoch-*.json"))) == 2

    resumed = _agent_config(
        worktree,
        checkpoint_root=tmp_path / "ckpts",
        context_reuse=True,
        rolling_compact=False,
        resume={
            "checkpoint_ref": folded.checkpoint_ref,
            "epoch": folded.epoch,
            "child_results": [],
            "child_results_truncated": False,
            "workspace_changed": False,
        },
        max_turns=4,
    )
    resume_router = _ScriptedRouter(['{"type":"finish","summary":"resumed"}'])
    resume_outcome = asyncio.run(_drive_loop(resumed, worktree, resume_router, _FakeWriter()))
    assert resume_outcome["status"] == "succeeded"
    assert resume_router.prompts[0]["messages"][: len(folded.full_messages)] == (
        folded.full_messages
    )


def test_rolling_compact_rewrites_active_context_before_publication(
    tmp_path: Path,
) -> None:
    worktree = _make_worktree(tmp_path / "repo")
    base_config = _agent_config(worktree, checkpoint_root=tmp_path / "ckpts")
    checkpoint = _write_epoch(base_config)
    resume = {
        "checkpoint_ref": checkpoint.checkpoint_ref,
        "epoch": checkpoint.epoch,
        "child_results": [_strict_child_envelope(summary="x" * 300)] * 2,
        "child_results_truncated": False,
        "workspace_changed": False,
    }
    config = _agent_config(
        worktree,
        checkpoint_root=tmp_path / "ckpts",
        context_reuse=True,
        rolling_compact=True,
        rolling_compact_threshold_high=100,
        rolling_compact_threshold_low=50,
        resume=resume,
        max_turns=2,
    )
    writer = _FakeWriter()
    asyncio.run(
        _drive_loop(
            config,
            worktree,
            _ScriptedRouter(['{"type":"plan","steps":["continue"]}']),
            writer,
            run_request_id="run-unpublished",
        )
    )

    kinds = [message["type"] for message in writer.messages()]
    assert "context_epoch_advanced" in kinds
    assert "compaction_failed" not in kinds
    assert len(list((tmp_path / "ckpts" / "epoch-agent").glob("epoch-*.json"))) == 2


def test_rolling_compact_hysteresis_does_not_refold_below_low(
    tmp_path: Path,
) -> None:
    worktree = _make_worktree(tmp_path / "repo")
    base_config = _agent_config(worktree, checkpoint_root=tmp_path / "ckpts")
    checkpoint = _write_epoch(base_config)
    resume = {
        "checkpoint_ref": checkpoint.checkpoint_ref,
        "epoch": checkpoint.epoch,
        "child_results": [
            _strict_child_envelope(summary="a" * 500),
            _strict_child_envelope(summary="b" * 500),
        ],
        "child_results_truncated": False,
        "workspace_changed": False,
    }
    config = _agent_config(
        worktree,
        checkpoint_root=tmp_path / "ckpts",
        context_reuse=True,
        rolling_compact=True,
        rolling_compact_threshold_high=1_000,
        rolling_compact_threshold_low=900,
        resume=resume,
        max_turns=3,
    )
    writer = _FakeWriter()
    outcome = asyncio.run(
        _drive_loop(
            config,
            worktree,
            _ScriptedRouter(
                [
                    '{"type":"plan","steps":["continue"]}',
                    '{"type":"plan","steps":["continue"]}',
                ]
            ),
            writer,
            run_request_id="run-hysteresis",
        )
    )

    assert outcome["status"] == "failed"
    advanced = [
        message for message in writer.messages() if message["type"] == "context_epoch_advanced"
    ]
    assert len(advanced) == 1
    assert len(list((tmp_path / "ckpts" / "epoch-agent").glob("epoch-*.json"))) == 2


def test_rolling_compact_failure_is_fail_closed_and_preserves_checkpoint(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    worktree = _make_worktree(tmp_path / "repo")
    base_config = _agent_config(worktree, checkpoint_root=tmp_path / "ckpts")
    checkpoint = _write_epoch(base_config)
    old_bytes = (tmp_path / "ckpts" / checkpoint.checkpoint_ref).read_bytes()
    old_file_count = len(list((tmp_path / "ckpts" / "epoch-agent").glob("epoch-*.json")))
    resume = {
        "checkpoint_ref": checkpoint.checkpoint_ref,
        "epoch": checkpoint.epoch,
        "child_results": [_strict_child_envelope(summary="x" * 300)] * 2,
        "child_results_truncated": False,
        "workspace_changed": False,
    }
    config = _agent_config(
        worktree,
        checkpoint_root=tmp_path / "ckpts",
        context_reuse=True,
        rolling_compact=True,
        rolling_compact_threshold_high=100,
        rolling_compact_threshold_low=50,
        resume=resume,
        max_turns=2,
    )

    def fail_checkpoint(*args: Any, **kwargs: Any) -> worker.ContextCheckpoint:
        raise RuntimeError("checkpoint write failed")

    monkeypatch.setattr(worker, "_write_epoch_checkpoint", fail_checkpoint)
    writer = _FakeWriter()
    router = _ScriptedRouter([])
    outcome = asyncio.run(
        _drive_loop(
            config,
            worktree,
            router,
            writer,
            run_request_id="run-failure",
        )
    )

    assert outcome["status"] == "failed"
    assert len(router.prompts) == 1
    assert "<cambium-summary-control>" in (router.prompts[0]["messages"][-1]["content"])
    failed = [message for message in writer.messages() if message["type"] == "compaction_failed"]
    assert len(failed) == 1
    assert failed[0] == {
        "type": "compaction_failed",
        "request_id": "run-failure",
        "task_id": "epoch-agent",
        "generation": 1,
        "epoch": 1,
        "reason": "checkpoint write failed",
    }
    assert (tmp_path / "ckpts" / checkpoint.checkpoint_ref).read_bytes() == old_bytes
    assert len(list((tmp_path / "ckpts" / "epoch-agent").glob("epoch-*.json"))) == old_file_count


def test_rolling_compact_internal_opt_out_keeps_existing_epoch_path(
    tmp_path: Path,
) -> None:
    worktree = _make_worktree(tmp_path / "repo")
    base_config = _agent_config(worktree, checkpoint_root=tmp_path / "ckpts")
    checkpoint = _write_epoch(base_config)
    resume = {
        "checkpoint_ref": checkpoint.checkpoint_ref,
        "epoch": checkpoint.epoch,
        "child_results": [_strict_child_envelope(summary="x" * 300)] * 2,
        "child_results_truncated": False,
        "workspace_changed": False,
    }
    config = _agent_config(
        worktree,
        checkpoint_root=tmp_path / "ckpts",
        context_reuse=True,
        rolling_compact=False,
        resume=resume,
        max_turns=2,
    )
    writer = _FakeWriter()
    router = _ScriptedRouter(['{"type":"plan","steps":["continue"]}'])
    asyncio.run(_drive_loop(config, worktree, router, writer, run_request_id="run-legacy"))

    assert router.prompts[0]["messages"] == [
        *checkpoint.full_messages,
        {
            "role": "user",
            "content": worker._child_result_lines(_strict_child_envelope(summary="x" * 300)),
        },
        {
            "role": "user",
            "content": worker._child_result_lines(_strict_child_envelope(summary="x" * 300)),
        },
    ]
    kinds = [message["type"] for message in writer.messages()]
    assert "context_epoch_advanced" not in kinds
    assert "compaction_failed" not in kinds
    assert len(list((tmp_path / "ckpts" / "epoch-agent").glob("epoch-*.json"))) == 1


# ---------------------------------------------------------------------------
# In-process loop: fork reuses the checkpointed prefix
# ---------------------------------------------------------------------------


def test_fork_reuses_epoch_prefix(tmp_path: Path) -> None:
    worktree = _make_worktree(tmp_path / "repo")
    config = _agent_config(worktree, checkpoint_root=tmp_path / "ckpts")
    checkpoint = _write_epoch(config)
    cache_key = checkpoint.cache_key
    fork_config = _agent_config(
        worktree,
        checkpoint_root=tmp_path / "ckpts",
        context_reuse=True,
        context_fork={
            "checkpoint_ref": checkpoint.checkpoint_ref,
            "provider": cache_key.provider,
            "model": cache_key.model,
            "system_sha256": cache_key.system_sha256,
            "tools_sha256": cache_key.tools_sha256,
            "prefix_sha256": cache_key.prefix_sha256,
            "suffix_sha256": cache_key.suffix_sha256,
            "full_sha256": cache_key.full_sha256,
            "prefix_bytes": cache_key.prefix_bytes,
            "provider_boundary": cache_key.provider_boundary,
        },
    )
    writer = _FakeWriter()
    router = _ScriptedRouter(['{"type":"finish","summary":"forked and done"}'])

    outcome = asyncio.run(_drive_loop(fork_config, worktree, router, writer))

    assert outcome["status"] == "succeeded"
    first = router.prompts[0]["messages"]
    assert first[0] == checkpoint.system_message
    assert first[: len(checkpoint.full_messages)] == checkpoint.full_messages
    assert first[1:] == [
        *checkpoint.transcript,
        {
            "role": "user",
            "content": "Child task: read the files and finish",
        },
    ]
    kinds = [m["type"] for m in writer.messages()]
    assert "context_fork_skipped" not in kinds
    usage = [m for m in writer.messages() if m["type"] == "usage_event"]
    assert usage and usage[0]["fork_of"] == checkpoint.checkpoint_ref


def test_fork_fallback_reports_skip(tmp_path: Path) -> None:
    missing_ref = f"epoch-agent/epoch-001-{'c' * 16}-{'d' * 16}.json"
    worktree = _make_worktree(tmp_path / "repo")
    config = _agent_config(
        worktree,
        checkpoint_root=tmp_path / "ckpts",
        context_reuse=True,
        context_fork={
            "checkpoint_ref": missing_ref,
            "provider": "loopback-provider",
            "model": "loopback-model",
            "system_sha256": _DIGEST,
            "tools_sha256": _DIGEST,
            "prefix_sha256": _DIGEST,
            "suffix_sha256": _DIGEST,
            "full_sha256": _DIGEST,
            "prefix_bytes": 0,
            "provider_boundary": _provider_boundary("loopback-provider", "loopback-model"),
        },
    )
    writer = _FakeWriter()
    router = _ScriptedRouter(['{"type":"finish","summary":"legacy path"}'])

    outcome = asyncio.run(_drive_loop(config, worktree, router, writer))

    assert outcome["status"] == "succeeded"
    kinds = [m["type"] for m in writer.messages()]
    assert "context_fork_skipped" in kinds
    skipped = next(m for m in writer.messages() if m["type"] == "context_fork_skipped")
    assert skipped["reason"]
    usage = [m for m in writer.messages() if m["type"] == "usage_event"]
    assert usage and "fork_of" not in usage[0]


def test_fork_descriptor_artifact_mismatch_falls_back(tmp_path: Path) -> None:
    worktree = _make_worktree(tmp_path / "repo")
    config = _agent_config(worktree, checkpoint_root=tmp_path / "ckpts")
    checkpoint = _write_epoch(config)
    cache_key = checkpoint.cache_key
    fork_descriptor = {
        "checkpoint_ref": checkpoint.checkpoint_ref,
        "provider": cache_key.provider,
        "model": cache_key.model,
        "system_sha256": "f" * 64,
        "tools_sha256": cache_key.tools_sha256,
        "prefix_sha256": cache_key.prefix_sha256,
        "suffix_sha256": cache_key.suffix_sha256,
        "full_sha256": cache_key.full_sha256,
        "prefix_bytes": cache_key.prefix_bytes,
        "provider_boundary": cache_key.provider_boundary,
    }
    fork_config = _agent_config(
        worktree,
        checkpoint_root=tmp_path / "ckpts",
        context_reuse=True,
        context_fork=fork_descriptor,
    )
    writer = _FakeWriter()
    router = _ScriptedRouter(['{"type":"finish","summary":"legacy path"}'])

    outcome = asyncio.run(_drive_loop(fork_config, worktree, router, writer))

    assert outcome["status"] == "succeeded"
    skipped = [
        message for message in writer.messages() if message["type"] == "context_fork_skipped"
    ]
    assert len(skipped) == 1
    assert "mismatch" in skipped[0]["reason"]
    usage = [message for message in writer.messages() if message["type"] == "usage_event"]
    assert usage and "fork_of" not in usage[0]


def test_redacted_resume_fails_without_seeding_transcript(tmp_path: Path) -> None:
    worktree = _make_worktree(tmp_path / "repo")
    config = _agent_config(
        worktree,
        checkpoint_root=tmp_path / "ckpts",
        redactor=Redactor(secret_values={"SECRETXYZ"}),
    )
    checkpoint = _write_epoch(
        config,
        messages=[
            {"role": "system", "content": "You are the agent SECRETXYZ."},
            {"role": "user", "content": "observe SECRETXYZ"},
        ],
    )
    resume_config = _agent_config(
        worktree,
        checkpoint_root=tmp_path / "ckpts",
        context_reuse=True,
        resume={
            "checkpoint_ref": checkpoint.checkpoint_ref,
            "epoch": checkpoint.epoch,
            "child_results": [_strict_child_envelope()],
            "child_results_truncated": False,
            "workspace_changed": False,
        },
    )
    writer = _FakeWriter()
    router = _ScriptedRouter([])

    outcome = asyncio.run(_drive_loop(resume_config, worktree, router, writer))

    assert outcome["status"] == "failed"
    assert "context_resume_failed" in (outcome["failure_reason"] or "")
    assert "redacted" in (outcome["failure_reason"] or "")
    assert router.prompts == []
    assert outcome["transcript"] == []
    assert not any(message["type"] == "context_checkpoint" for message in writer.messages())


def test_invalid_context_checkpoint_fields_matrix() -> None:
    digest = "a" * 64
    valid: dict[str, Any] = {
        "type": "context_checkpoint",
        "task_id": "t",
        "generation": 1,
        "epoch": 1,
        "turn": 1,
        "checkpoint_ref": f"t/epoch-001-{'a' * 16}-{'b' * 16}.json",
        "cache_key": {
            "provider": "p",
            "model": "m",
            "protocol": "loopback",
            "reasoning_effort": None,
            "system_sha256": digest,
            "tools_sha256": digest,
            "prefix_sha256": digest,
            "suffix_sha256": digest,
            "full_sha256": digest,
            "prefix_bytes": 0,
            "message_count": 1,
            "redacted": False,
            "provider_boundary": _provider_boundary("p", "m"),
        },
    }
    assert _invalid_context_checkpoint_fields(valid) == []

    cases: dict[str, Any] = {
        "epoch_zero": {**valid, "epoch": 0},
        "epoch_bool": {**valid, "epoch": True},
        "ref_empty": {**valid, "checkpoint_ref": ""},
        "sha_bad": {
            **valid,
            "cache_key": {**valid["cache_key"], "tools_sha256": "xyz"},
        },
        "prefix_neg": {
            **valid,
            "cache_key": {**valid["cache_key"], "prefix_bytes": -1},
        },
        "prefix_sha_missing": {
            **valid,
            "cache_key": {
                key: value for key, value in valid["cache_key"].items() if key != "prefix_sha256"
            },
        },
        "suffix_sha_bad": {
            **valid,
            "cache_key": {**valid["cache_key"], "suffix_sha256": 12},
        },
        "redacted_str": {
            **valid,
            "cache_key": {**valid["cache_key"], "redacted": "no"},
        },
        "provider_int": {
            **valid,
            "cache_key": {**valid["cache_key"], "provider": 3},
        },
        "no_cache_key": {k: v for k, v in valid.items() if k != "cache_key"},
    }
    for name, msg in cases.items():
        assert _invalid_context_checkpoint_fields(msg), f"{name} not rejected"


def test_invalid_usage_event_fields_epoch_fork_of() -> None:
    valid = {
        "type": "usage_event",
        "task_id": "t",
        "generation": 1,
        "turn": 1,
        "provider": "p",
        "model": "m",
        "usage": {"prompt_tokens": 1, "completion_tokens": 1, "total_tokens": 2},
        "epoch": 2,
        "fork_of": "t/epoch-001-abc.json",
    }
    assert _invalid_usage_event_fields(valid) == []

    assert "epoch" in _invalid_usage_event_fields({**valid, "epoch": -1})
    assert "epoch" in _invalid_usage_event_fields({**valid, "epoch": "x"})
    assert "fork_of" in _invalid_usage_event_fields({**valid, "fork_of": ""})
    assert "fork_of" in _invalid_usage_event_fields({**valid, "fork_of": 5})


def test_bounded_resume_envelope_caps() -> None:
    huge = "x" * 20_000
    envelope = {
        "parent_task_id": "t-root",
        "unified_diff": huge,
        "diff_truncated": False,
        "summary": huge,
        "metric_score": 3.5,
        "metric_breakdown": {"lint": 1},
        "commits": [f"c{i}" for i in range(100)],
        "files_changed": ["a.txt", "b.txt"],
        "status": "succeeded",
        "sneaky": "dropped",
    }
    bounded = _bounded_resume_envelope(envelope)
    assert set(bounded) == _STRICT_ENVELOPE_KEYS
    assert len(bounded["summary"].encode("utf-8")) <= worker.MAX_ENVELOPE_FIELD_CHARS
    assert len(bounded["unified_diff"].encode("utf-8")) <= worker.MAX_ENVELOPE_FIELD_CHARS
    assert len(bounded["commits"]) <= worker.MAX_ENVELOPE_ITEMS
    assert bounded["metric_score"] == 3.5
    assert bounded["status"] == "succeeded"


def test_child_results_for_resume_order_and_synthesis(tmp_path: Path) -> None:
    runtime = _Runtime(session_dir=tmp_path, store=None)
    runtime._results["c1"] = TaskResult(task_id="c1", status="failed", exit_code=1, reason="boom")
    runtime._child_result_by_task["c2"] = _strict_child_envelope()

    payload = runtime._child_results_for_resume(
        "t-root", ["c1", "c2"], checkpoint_ref="ref", epoch=2
    )
    assert payload["checkpoint_ref"] == "ref"
    assert payload["epoch"] == 2
    assert payload["child_results_truncated"] is False
    assert len(payload["child_results"]) == 2
    assert payload["child_results"][0]["status"] == "failed"
    assert payload["child_results"][0]["summary"] == "boom"
    assert payload["child_results"][1] == _strict_child_envelope()

    many = runtime._child_results_for_resume(
        "t-root",
        [f"c{i}" for i in range(worker.MAX_ENVELOPE_ITEMS + 5)],
        checkpoint_ref="ref",
        epoch=2,
    )
    assert many["child_results_truncated"] is True
    assert len(many["child_results"]) == worker.MAX_ENVELOPE_ITEMS


# ---------------------------------------------------------------------------
# Slow end-to-end: suspend -> resume orchestration over the wire
# ---------------------------------------------------------------------------


def _make_repo(repo: Path, files: dict[str, str]) -> str:
    subprocess.run(["git", "init", "-b", "main", str(repo)], check=True, capture_output=True)
    subprocess.run(["git", "-C", str(repo), "config", "user.name", "ctx-test"], check=True)
    subprocess.run(["git", "-C", str(repo), "config", "user.email", "ctx@test"], check=True)
    subprocess.run(["git", "-C", str(repo), "config", "gc.auto", "0"], check=True)
    for name, content in files.items():
        (repo / name).write_text(content)
    subprocess.run(["git", "-C", str(repo), "add", "-A"], check=True, capture_output=True)
    subprocess.run(
        ["git", "-C", str(repo), "commit", "-m", "initial"], check=True, capture_output=True
    )
    return subprocess.run(
        ["git", "-C", str(repo), "rev-parse", "HEAD"], check=True, capture_output=True, text=True
    ).stdout.strip()


def _task(
    session_dir: Path,
    repo: Path,
    base: str,
    task_id: str,
    *,
    worktree: str,
    branch: str,
    target_file: str,
    marker: str,
    worker_path: str = "cambium.worker",
    **extra,
) -> dict:
    spec = {
        "task_id": task_id,
        "task": f"edit {target_file}",
        "repo": str(repo),
        "worktree_path": str(session_dir / worktree),
        "branch": branch,
        "worker": worker_path,
        "target_file": target_file,
        "marker": marker,
        "write_marker": True,
        "base_commit": base,
        "provider_env_keys": ["FAKE_MODE"],
        "resource_thresholds": TEST_RESOURCE_THRESHOLDS,
    }
    spec.update(extra)
    return spec


def _child_proposal(spec: dict) -> dict:
    return {
        "child_task_id": spec["task_id"],
        "kind": spec.get("kind", "test"),
        "spec": spec,
    }


def _kinds(events: list[dict], kind: str) -> list[dict]:
    return [e for e in events if e["kind"] == kind]


def _write_suspend_worker(path: Path) -> None:
    """Parent worker: drive 1 proposes a child, emits an epoch checkpoint,
    then suspends; the resume drive (init carries ``resume``) dumps the init
    and completes via ``do_work``. Every init is appended to the dump file.
    """
    path.write_text(
        "#!/usr/bin/env python3\n"
        "import json\n"
        "import os\n"
        "import sys\n"
        "from pathlib import Path\n"
        f"ROOT = Path({str(ROOT)!r})\n"
        "sys.path.insert(0, str(ROOT / 'scripts'))\n"
        "from fake_worker import do_work, read_msg, send  # noqa: E402\n"
        "def main() -> int:\n"
        "    init = read_msg()\n"
        "    if init is None or init.get('type') != 'init':\n"
        "        return 1\n"
        "    task_id = init['task_id']\n"
        "    dump_path = Path(os.environ['CONTEXT_DUMP_PATH'])\n"
        "    dump_path.parent.mkdir(parents=True, exist_ok=True)\n"
        "    with open(dump_path, 'a', encoding='utf-8') as f:\n"
        "        f.write(json.dumps(init) + '\\n')\n"
        "    init_rid = init['request_id']\n"
        "    send({'type': 'ready', 'request_id': init_rid, 'task_id': task_id,\n"
        "          'pid': os.getpid(), 'generation': init.get('generation', 1),\n"
        "          'proto': 1})\n"
        "    run = read_msg()\n"
        "    if run is None or run.get('type') != 'run_task':\n"
        "        send({'type': 'exit_message', 'task_id': task_id,\n"
        "              'generation': init.get('generation', 1), 'reason': 'crash'})\n"
        "        return 1\n"
        "    run_rid = run['request_id']\n"
        "    if init.get('resume') is not None:\n"
        "        status, failure_reason, commits, files_changed, diff = do_work(run)\n"
        "        send({'type': 'result_envelope', 'request_id': run_rid,\n"
        "              'task_id': task_id, 'generation': init.get('generation', 1),\n"
        "              'status': status, 'commits': commits,\n"
        "              'files_changed': files_changed, 'diff': diff,\n"
        "              'summary': status, 'failure_reason': failure_reason})\n"
        "        send({'type': 'exit_message', 'task_id': task_id,\n"
        "              'generation': init.get('generation', 1), 'reason': 'done'})\n"
        "        return 0\n"
        "    proposals = run.get('proposed_children') or []\n"
        "    if not proposals:\n"
        "        send({'type': 'exit_message', 'task_id': task_id,\n"
        "              'generation': init.get('generation', 1), 'reason': 'crash'})\n"
        "        return 1\n"
        "    for proposal in proposals:\n"
        "        send({'type': 'propose_child', 'request_id': run_rid,\n"
        "              'parent_task_id': task_id,\n"
        "              'child_task_id': proposal['child_task_id'],\n"
        "              'kind': proposal['kind'], 'spec': proposal['spec']})\n"
        "    checkpoint_ref = os.environ.get(\n"
        "        'FAKE_CHECKPOINT_REF', task_id + '/epoch-001-"
        + ("a" * 16)
        + "-"
        + ("b" * 16)
        + ".json')\n"
        "    boundary = {\n"
        "        'provider': os.environ.get('FAKE_EPOCH_PROVIDER', 'fake-provider'),\n"
        "        'endpoint': 'https://api.example',\n"
        "        'authmode': 'api_key',\n"
        "        'api_key_env': 'FAKE_KEY',\n"
        "        'provider_env_keys': [],\n"
        "        'authorized_providers': None,\n"
        "        'authorized_providers_explicit': False,\n"
        "        'protocol': 'loopback',\n"
        "        'model': os.environ.get('FAKE_EPOCH_MODEL', 'fake-model'),\n"
        "        'tier': 'fast',\n"
        "        'reasoning_effort': None,\n"
        "        'provider_config_path': '/opt/cambium/providers.json',\n"
        "    }\n"
        "    send({'type': 'context_checkpoint', 'task_id': task_id,\n"
        "          'generation': init.get('generation', 1), 'epoch': 1, 'turn': 1,\n"
        "          'checkpoint_ref': checkpoint_ref,\n"
        "          'cache_key': {\n"
        "              'provider': os.environ.get('FAKE_EPOCH_PROVIDER', 'fake-provider'),\n"
        "              'model': os.environ.get('FAKE_EPOCH_MODEL', 'fake-model'),\n"
        "              'protocol': 'loopback',\n"
        "              'reasoning_effort': None,\n"
        "              'system_sha256': os.environ.get('FAKE_SYSTEM_SHA', 'a' * 64),\n"
        "              'tools_sha256': os.environ.get('FAKE_TOOLS_SHA', 'b' * 64),\n"
        "              'prefix_sha256': 'd' * 64,\n"
        "              'suffix_sha256': 'e' * 64,\n"
        "              'full_sha256': 'f' * 64,\n"
        "              'prefix_bytes': 0,\n"
        "              'message_count': 1,\n"
        "              'redacted': False,\n"
        "              'provider_boundary': boundary,\n"
        "          }})\n"
        "    send({'type': 'result_envelope', 'request_id': run_rid,\n"
        "          'task_id': task_id, 'generation': init.get('generation', 1),\n"
        "          'status': 'suspended', 'epoch': 1, 'checkpoint_ref': checkpoint_ref,\n"
        "          'commits': [], 'files_changed': [], 'diff': '',\n"
        "          'summary': 'suspended for children', 'failure_reason': None})\n"
        "    send({'type': 'exit_message', 'task_id': task_id,\n"
        "          'generation': init.get('generation', 1), 'reason': 'suspended'})\n"
        "    return 0\n"
        "if __name__ == '__main__':\n"
        "    sys.exit(main())\n",
        encoding="utf-8",
    )
    path.chmod(0o755)


def _write_dump_worker(path: Path) -> None:
    """Child worker: dump the full init (fork descriptor, provider, model),
    then complete via ``do_work``."""
    path.write_text(
        "#!/usr/bin/env python3\n"
        "import json\n"
        "import os\n"
        "import sys\n"
        "from pathlib import Path\n"
        f"ROOT = Path({str(ROOT)!r})\n"
        "sys.path.insert(0, str(ROOT / 'scripts'))\n"
        "from fake_worker import do_work, read_msg, send  # noqa: E402\n"
        "def main() -> int:\n"
        "    init = read_msg()\n"
        "    if init is None or init.get('type') != 'init':\n"
        "        return 1\n"
        "    task_id = init['task_id']\n"
        "    dump_path = Path(os.environ['CHILD_DUMP_PATH'])\n"
        "    dump_path.parent.mkdir(parents=True, exist_ok=True)\n"
        "    dump_path.write_text(json.dumps(init))\n"
        "    init_rid = init['request_id']\n"
        "    send({'type': 'ready', 'request_id': init_rid, 'task_id': task_id,\n"
        "          'pid': os.getpid(), 'generation': init.get('generation', 1),\n"
        "          'proto': 1})\n"
        "    run = read_msg()\n"
        "    if run is None or run.get('type') != 'run_task':\n"
        "        send({'type': 'exit_message', 'task_id': task_id,\n"
        "              'generation': init.get('generation', 1), 'reason': 'crash'})\n"
        "        return 1\n"
        "    run_rid = run['request_id']\n"
        "    if 'target_file' in run:\n"
        "        status, failure_reason, commits, files_changed, diff = do_work(run)\n"
        "    else:\n"
        "        status, failure_reason, commits, files_changed, diff = (\n"
        "            'succeeded', None, [], [], '')\n"
        "    send({'type': 'result_envelope', 'request_id': run_rid,\n"
        "          'task_id': task_id, 'generation': init.get('generation', 1),\n"
        "          'status': status, 'commits': commits,\n"
        "          'files_changed': files_changed, 'diff': diff,\n"
        "          'summary': status, 'failure_reason': failure_reason})\n"
        "    send({'type': 'exit_message', 'task_id': task_id,\n"
        "          'generation': init.get('generation', 1), 'reason': 'done'})\n"
        "    return 0\n"
        "if __name__ == '__main__':\n"
        "    sys.exit(main())\n",
        encoding="utf-8",
    )
    path.chmod(0o755)


def _wire_epoch_setup(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    *,
    fork_pin: bool,
) -> tuple[Path, Path, Path, dict[str, Any], str | None]:
    session_dir = tmp_path / "session"
    repo = session_dir / "repo"
    base = _make_repo(repo, {"a.txt": "file a\n", "b.txt": "file b\n"})

    suspend_worker = tmp_path / "suspend_worker.py"
    dump_worker = tmp_path / "dump_worker.py"
    _write_suspend_worker(suspend_worker)
    _write_dump_worker(dump_worker)

    context_dump = tmp_path / "parent-inits.jsonl"
    child_dump = tmp_path / "child-init.json"
    monkeypatch.setenv("CONTEXT_DUMP_PATH", str(context_dump))
    monkeypatch.setenv("CHILD_DUMP_PATH", str(child_dump))

    child_options: dict[str, Any] = {}
    tools_sha: str | None = None
    if fork_pin:
        tools_sha = worker._provider_task_tools_hash()
        monkeypatch.setenv("FAKE_TOOLS_SHA", tools_sha)
        monkeypatch.setenv("FAKE_EPOCH_PROVIDER", "fake-provider")
        monkeypatch.setenv("FAKE_EPOCH_MODEL", "fake-model")
        child_options = {
            "authorized_providers": ["fake-provider"],
            "fanout_config": {"model": "fake-model"},
        }

    child = _task(
        session_dir,
        repo,
        base,
        "c1",
        worktree="wt-c1",
        branch="wt-c1",
        target_file="b.txt",
        marker="// child-marker",
        worker_path=str(dump_worker),
        provider_env_keys=["FAKE_MODE", "CHILD_DUMP_PATH"],
        **child_options,
    )
    root = _task(
        session_dir,
        repo,
        base,
        "t-root",
        worktree="wt-root",
        branch="wt-root",
        target_file="a.txt",
        marker="// parent-marker",
        worker_path=str(suspend_worker),
        provider_env_keys=[
            "FAKE_MODE",
            "CONTEXT_DUMP_PATH",
            "CHILD_DUMP_PATH",
            "FAKE_CHECKPOINT_REF",
            "FAKE_EPOCH_PROVIDER",
            "FAKE_EPOCH_MODEL",
            "FAKE_TOOLS_SHA",
            "FAKE_SYSTEM_SHA",
            "FAKE_MESSAGES_SHA",
        ],
        proposed_children=[_child_proposal(child)],
    )
    return session_dir, context_dump, child_dump, root, tools_sha


@pytest.mark.slow
def test_suspend_resume_end_to_end(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    session_dir, context_dump, _child_dump, root, _tools_sha = _wire_epoch_setup(
        tmp_path, monkeypatch, fork_pin=False
    )

    result = asyncio.run(run_plan(session_dir, {"tasks": [root]}, context_reuse=True))

    assert result.exit_code == 0, result.results
    assert {r.task_id for r in result.results} == {"t-root", "c1"}
    assert all(r.status == "succeeded" for r in result.results)

    events = read_events(session_dir)
    resumes = _kinds(events, "context_resume")
    assert len(resumes) == 1
    assert resumes[0]["payload"]["epoch"] == 1
    assert resumes[0]["payload"]["checkpoint_ref"]
    assert resumes[0]["payload"]["child_count"] == 1
    assert _kinds(events, "context_checkpoint")
    child_results = _kinds(events, "child_result")
    assert child_results and set(child_results[0]["payload"]) == _STRICT_ENVELOPE_KEYS

    lines = context_dump.read_text(encoding="utf-8").strip().splitlines()
    assert len(lines) == 2
    first_init = json.loads(lines[0])
    resume_init = json.loads(lines[1])
    assert "resume" not in first_init
    assert first_init["rolling_compact"] is True
    assert resume_init["resume"]["epoch"] == 1
    assert resume_init["rolling_compact"] is True
    assert resume_init["resume"]["checkpoint_ref"]
    assert resume_init["resume"]["child_results"]
    assert resume_init["resume"]["child_results"][0]["status"] == "succeeded"
    assert resume_init["resume"]["child_results"][0]["files_changed"] == ["b.txt"]


@pytest.mark.slow
def test_fork_pin_end_to_end(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    session_dir, _context_dump, child_dump, root, tools_sha = _wire_epoch_setup(
        tmp_path, monkeypatch, fork_pin=True
    )

    result = asyncio.run(run_plan(session_dir, {"tasks": [root]}, context_reuse=True))

    assert result.exit_code == 0, result.results
    assert {r.task_id for r in result.results} == {"t-root", "c1"}
    assert all(r.status == "succeeded" for r in result.results)

    events = read_events(session_dir)
    fork_events = _kinds(events, "context_fork")
    assert fork_events
    assert fork_events[0]["payload"]["compatible"] is True
    assert fork_events[0]["payload"]["epoch"] == 1

    child_init = json.loads(child_dump.read_text(encoding="utf-8"))
    descriptor = child_init["context_fork"]
    assert descriptor["checkpoint_ref"]
    assert descriptor["provider"] == "fake-provider"
    assert descriptor["model"] == "fake-model"
    assert descriptor["tools_sha256"] == tools_sha
    assert child_init["assigned_provider"] == "fake-provider"
    assert child_init["fanout_config"]["model"] == "fake-model"
    assert child_init["context_reuse"] is True
    assert child_init["rolling_compact"] is True


@pytest.mark.slow
def test_suspend_resume_two_children_concurrency_one(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Suspend/resume under max_concurrent_tasks=1 must not deadlock.

    The parent releases its admission slot while awaiting children, both
    children run serialized in that slot, and the parent resumes with both
    bounded child results on the same worktree and generation.
    """
    session_dir = tmp_path / "session"
    repo = session_dir / "repo"
    base = _make_repo(repo, {"a.txt": "file a\n", "b.txt": "file b\n", "c.txt": "file c\n"})

    suspend_worker = tmp_path / "suspend_worker.py"
    dump_worker = tmp_path / "dump_worker.py"
    _write_suspend_worker(suspend_worker)
    _write_dump_worker(dump_worker)

    context_dump = tmp_path / "parent-inits.jsonl"
    child_dump = tmp_path / "child-init.json"
    monkeypatch.setenv("CONTEXT_DUMP_PATH", str(context_dump))
    monkeypatch.setenv("CHILD_DUMP_PATH", str(child_dump))

    def _child(task_id: str, target: str) -> dict:
        return _task(
            session_dir,
            repo,
            base,
            task_id,
            worktree=f"wt-{task_id}",
            branch=f"wt-{task_id}",
            target_file=target,
            marker=f"// {task_id}-marker",
            worker_path=str(dump_worker),
            provider_env_keys=["FAKE_MODE", "CHILD_DUMP_PATH"],
        )

    root = _task(
        session_dir,
        repo,
        base,
        "t-root",
        worktree="wt-root",
        branch="wt-root",
        target_file="a.txt",
        marker="// parent-marker",
        worker_path=str(suspend_worker),
        provider_env_keys=[
            "FAKE_MODE",
            "CONTEXT_DUMP_PATH",
            "CHILD_DUMP_PATH",
            "FAKE_CHECKPOINT_REF",
            "FAKE_EPOCH_PROVIDER",
            "FAKE_EPOCH_MODEL",
            "FAKE_TOOLS_SHA",
            "FAKE_SYSTEM_SHA",
            "FAKE_MESSAGES_SHA",
        ],
        proposed_children=[
            _child_proposal(_child("c1", "b.txt")),
            _child_proposal(_child("c2", "c.txt")),
        ],
    )

    result = asyncio.run(
        run_plan(
            session_dir,
            {"tasks": [root]},
            context_reuse=True,
            max_concurrent_tasks=1,
        )
    )

    assert result.exit_code == 0, result.results
    assert {r.task_id for r in result.results} == {"t-root", "c1", "c2"}
    assert all(r.status == "succeeded" for r in result.results)

    events = read_events(session_dir)
    resumes = _kinds(events, "context_resume")
    assert len(resumes) == 1
    assert resumes[0]["payload"]["child_count"] == 2

    lines = context_dump.read_text(encoding="utf-8").strip().splitlines()
    assert len(lines) == 2
    resume_init = json.loads(lines[1])
    child_results = resume_init["resume"]["child_results"]
    assert len(child_results) == 2
    assert {r["files_changed"][0] for r in child_results if r["files_changed"]} == {
        "b.txt",
        "c.txt",
    }
    # Same worktree and generation across suspend/resume (no restart).
    first_init = json.loads(lines[0])
    assert first_init["generation"] == resume_init["generation"]


def test_result_identity_note_matrix() -> None:
    from cambium.supervisor import _result_identity_note

    assert _result_identity_note({"task_id": "t", "generation": 1}, "t", 1) is None
    assert (
        _result_identity_note({"task_id": "other", "generation": 1}, "t", 1)
        == "result task_id mismatch"
    )
    assert _result_identity_note({"generation": 2}, "t", 1) == "result generation mismatch"
    assert _result_identity_note({"generation": True}, "t", 1) == "result generation mismatch"
    # Absent identity fields are tolerated (older workers omit them).
    assert _result_identity_note({}, "t", 1) is None


@pytest.mark.slow
def test_suspended_envelope_without_flag_fails_closed(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Without context_reuse a suspended verdict is a failure, not a resume."""
    session_dir = tmp_path / "session"
    repo = session_dir / "repo"
    base = _make_repo(repo, {"a.txt": "file a\n", "b.txt": "file b\n"})

    suspend_worker = tmp_path / "suspend_worker.py"
    dump_worker = tmp_path / "dump_worker.py"
    _write_suspend_worker(suspend_worker)
    _write_dump_worker(dump_worker)
    monkeypatch.setenv("CONTEXT_DUMP_PATH", str(tmp_path / "parent-inits.jsonl"))
    monkeypatch.setenv("CHILD_DUMP_PATH", str(tmp_path / "child-init.json"))

    child = _task(
        session_dir,
        repo,
        base,
        "c1",
        worktree="wt-c1",
        branch="wt-c1",
        target_file="b.txt",
        marker="// child-marker",
        worker_path=str(dump_worker),
        provider_env_keys=["FAKE_MODE", "CHILD_DUMP_PATH"],
    )
    root = _task(
        session_dir,
        repo,
        base,
        "t-root",
        worktree="wt-root",
        branch="wt-root",
        target_file="a.txt",
        marker="// parent-marker",
        worker_path=str(suspend_worker),
        provider_env_keys=[
            "FAKE_MODE",
            "CONTEXT_DUMP_PATH",
            "CHILD_DUMP_PATH",
            "FAKE_CHECKPOINT_REF",
            "FAKE_EPOCH_PROVIDER",
            "FAKE_EPOCH_MODEL",
            "FAKE_TOOLS_SHA",
            "FAKE_SYSTEM_SHA",
            "FAKE_MESSAGES_SHA",
        ],
        proposed_children=[_child_proposal(child)],
    )

    result = asyncio.run(run_plan(session_dir, {"tasks": [root]}, context_reuse=False))

    root_result = next(r for r in result.results if r.task_id == "t-root")
    assert root_result.status == "failed"
    events = read_events(session_dir)
    assert not _kinds(events, "context_resume")
    assert not _kinds(events, "context_fork")


def test_legacy_flat_epoch_checkpoint_still_loads(tmp_path: Path) -> None:
    """Pre-split flat checkpoints (schema 4) resume transparently."""
    checkpoint_root = tmp_path / "ckpts"
    config = _agent_config(tmp_path / "wt", checkpoint_root=checkpoint_root)
    checkpoint = _write_epoch(config)

    legacy_path = checkpoint_root / checkpoint.checkpoint_ref
    legacy = json.loads(legacy_path.read_text(encoding="utf-8"))
    assert "content" in legacy, "new writer must emit the nested layout"

    flat = worker._join_checkpoint_payload(legacy)
    task_component = checkpoint.checkpoint_ref.split("/")[0]
    flat4 = {**flat, "schema": 4}
    address_pre = worker._checkpoint_address({**flat4, "checkpoint_ref": ""})
    placeholder = f"{task_component}/epoch-{checkpoint.epoch:03d}-{address_pre}-{'0' * 16}.json"
    flat4["checkpoint_ref"] = placeholder
    address_persisted = worker._checkpoint_address(flat4)
    legacy_ref = (
        f"{task_component}/epoch-{checkpoint.epoch:03d}-{address_pre}-{address_persisted}.json"
    )
    flat4["checkpoint_ref"] = legacy_ref
    legacy_path = checkpoint_root / legacy_ref
    legacy_path.write_text(json.dumps(flat4, sort_keys=True), encoding="utf-8")

    loaded = worker._load_epoch_checkpoint(config, legacy_ref, expect_task_id=True)
    assert loaded.provider_messages == checkpoint.provider_messages
    assert loaded.epoch == checkpoint.epoch
