"""Fast worker-boundary regressions for the SituationFrame wiring."""

from __future__ import annotations

import asyncio
import copy
import hashlib
import json
import subprocess
import threading
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from cambium import worker
from cambium.diffundo import ProviderTier
from cambium.fencing import write_generation
from cambium.situation import SECTION_ORDER, SituationFrameLimits


class _Writer:
    def __init__(self) -> None:
        self.lines: list[bytes] = []

    def write(self, data: bytes) -> None:
        self.lines.append(data)

    async def drain(self) -> None:
        pass

    def messages(self) -> list[dict[str, Any]]:
        return [json.loads(line) for line in self.lines if line.strip()]


class _Router:
    def __init__(self, responses: list[str]) -> None:
        self.responses = list(responses)
        self.prompts: list[dict[str, Any]] = []

    def declared_model(self, _provider: str) -> str:
        return ""

    async def call(
        self,
        _tier: ProviderTier,
        prompt: dict[str, Any],
        *,
        model: str | None = None,
        budget_usd: float | None = None,
        allow_model_substitution: bool = False,
    ) -> SimpleNamespace:
        del model, budget_usd, allow_model_substitution
        self.prompts.append(copy.deepcopy(prompt))
        if not self.responses:
            raise AssertionError("router call with no scripted response")
        return SimpleNamespace(
            content=self.responses.pop(0),
            model="scenario-model",
            usage={"prompt_tokens": 7, "completion_tokens": 3, "total_tokens": 10},
            provider="scenario-provider",
            latency_s=0.01,
            estimated_cost_usd=0.0,
            retry_after_s=None,
            request_rate_status=None,
            account_quota_owner=None,
            prompt_prefix_bytes=None,
            provider_cache_hit=None,
            fell_back_from=None,
        )


def _make_repo(repo: Path) -> None:
    repo.mkdir(parents=True)
    subprocess.run(["git", "init", "-b", "main", str(repo)], check=True, capture_output=True)
    subprocess.run(
        ["git", "-C", str(repo), "config", "user.name", "situation-events-test"],
        check=True,
        capture_output=True,
    )
    subprocess.run(
        ["git", "-C", str(repo), "config", "user.email", "situation-events@test"],
        check=True,
        capture_output=True,
    )
    (repo / ".gitignore").write_text(".cambium/\n", encoding="utf-8")
    (repo / "fixture.txt").write_text("fixture\n", encoding="utf-8")
    subprocess.run(["git", "-C", str(repo), "add", "-A"], check=True, capture_output=True)
    subprocess.run(
        ["git", "-C", str(repo), "commit", "-m", "initial"],
        check=True,
        capture_output=True,
    )
    write_generation(repo, 1)


def _config(worktree: Path, **overrides: Any) -> worker.AgentConfig:
    values: dict[str, Any] = {
        "task_id": "situation-events",
        "generation": 1,
        "task": "inspect the worktree",
        "worktree": worktree,
        "base_commit": None,
        "fanout_config": {},
        "max_turns": 3,
        "max_tokens": 200_000,
        "shell_permission": False,
        "network_permission": False,
        "heartbeat_interval_s": 0.05,
        "max_wall_s": 60.0,
        "checkpoint_root": None,
        "context_reuse": False,
    }
    values.update(overrides)
    return worker.AgentConfig(**values)


def _frame(prompt: dict[str, Any]) -> str:
    content = prompt["messages"][-1]["content"]
    start = content.index("<cambium-situation ")
    end = content.index("</cambium-situation>", start) + len("</cambium-situation>")
    return content[start:end]


def _run(
    config: worker.AgentConfig,
    worktree: Path,
    router: _Router,
    writer: _Writer | None = None,
) -> dict[str, Any]:
    return asyncio.run(
        worker._run_agent_loop(
            config=config,
            router=router,  # type: ignore[arg-type]  # duck-typed Diffundo
            tier=ProviderTier.FAST,
            model="scenario-model",
            worktree=worktree,
            writer=writer,  # type: ignore[arg-type]  # duck-typed StreamWriter
            stop=threading.Event(),
            progress=worker.AgentProgress(),
        )
    )


def test_epoch_checkpoint_preserves_exact_provider_request(tmp_path: Path) -> None:
    worktree = tmp_path / "worktree"
    checkpoint_root = tmp_path / "checkpoints"
    config = _config(worktree, checkpoint_root=checkpoint_root)
    state_message = worker._context_state_message(
        code_changed=False,
        verified_after_change=False,
        verification_failed=False,
        no_progress_actions=0,
        budget_new_tokens=0,
        previous_prompt_tokens=0,
        turn=1,
        situation_frame=(
            '<cambium-situation version="1" source_watermark="9" '
            'frame_sha256="frame">\nMISSION\n</cambium-situation>'
        ),
    )
    original_request = [
        {"role": "system", "content": "system prefix é"},
        state_message,
    ]
    suffix = [{"role": "user", "content": "tool output"}]

    checkpoint = worker._write_epoch_checkpoint(
        config,
        turn=1,
        epoch=1,
        provider_messages=copy.deepcopy(original_request),
        continuation_suffix=copy.deepcopy(suffix),
        provider="scenario-provider",
        model="scenario-model",
        tools_sha256="a" * 64,
        provider_compat={"scenario-provider": ("loopback", None)},
    )
    assert checkpoint is not None

    path = checkpoint_root / checkpoint.checkpoint_ref
    persisted = json.loads(path.read_text(encoding="utf-8"))
    assert persisted["content"]["provider_messages"] == original_request
    assert persisted["content"]["continuation_suffix"] == suffix
    cache_key = persisted["meta"]["cache_key"]
    assert cache_key["prefix_sha256"] == worker._messages_sha256(original_request)
    assert cache_key["suffix_sha256"] == worker._messages_sha256(suffix)
    assert cache_key["full_sha256"] == worker._messages_sha256([*original_request, *suffix])
    assert cache_key["prefix_bytes"] == worker.prompt_prefix_bytes({"messages": original_request})


def test_single_tool_event_reaches_next_situation_frame(tmp_path: Path) -> None:
    worktree = tmp_path / "repo"
    _make_repo(worktree)
    router = _Router(
        [
            '{"type":"tool_call","name":"read_batch","arguments":{"paths":["fixture.txt"]}}',
            '{"type":"finish","summary":"read it","objective_met":true}',
        ]
    )

    outcome = _run(_config(worktree), worktree, router)

    assert outcome["status"] == "succeeded", outcome
    assert "current_tool: read_batch" in _frame(router.prompts[1])


def test_untracked_write_marks_situation_dirty(tmp_path: Path) -> None:
    worktree = tmp_path / "repo"
    _make_repo(worktree)
    router = _Router(
        [
            '{"type":"tool_call","name":"write_file","arguments":{"path":"new.txt","content":"new\\n"}}',
            '{"type":"tool_call","name":"run_shell","arguments":{"cmd":["true"]}}',
            '{"type":"finish","summary":"wrote it","objective_met":true}',
        ]
    )

    outcome = _run(_config(worktree, shell_permission=True, max_turns=5), worktree, router)

    assert outcome["status"] == "succeeded", outcome
    assert "  dirty: true" in _frame(router.prompts[1])


def test_resume_child_result_reaches_situation_frame(tmp_path: Path) -> None:
    worktree = tmp_path / "repo"
    _make_repo(worktree)
    checkpoint_root = tmp_path / "checkpoints"
    base_config = _config(worktree, checkpoint_root=checkpoint_root)
    delegate = worker._canonical_action_message(
        {
            "type": "tool_call",
            "calls": [
                {
                    "name": "delegate",
                    "arguments": {"child_task_id": "child-1", "kind": "review"},
                }
            ],
        }
    )
    checkpoint = worker._write_epoch_checkpoint(
        base_config,
        turn=1,
        epoch=1,
        provider_messages=[
            {"role": "system", "content": "system"},
            delegate,
        ],
        provider="scenario-provider",
        model="scenario-model",
        tools_sha256="a" * 64,
        provider_compat={"scenario-provider": ("loopback", None)},
    )
    assert checkpoint is not None
    config = _config(
        worktree,
        checkpoint_root=checkpoint_root,
        resume={
            "checkpoint_ref": checkpoint.checkpoint_ref,
            "epoch": checkpoint.epoch,
            "child_results": [
                {
                    "parent_task_id": "situation-events",
                    "unified_diff": "",
                    "diff_truncated": False,
                    "summary": "child completed review",
                    "metric_score": None,
                    "metric_breakdown": {},
                    "commits": [],
                    "files_changed": ["review.md"],
                    "status": "succeeded",
                }
            ],
            "child_results_truncated": False,
            "workspace_changed": False,
        },
    )
    router = _Router(['{"type":"finish","summary":"resumed","objective_met":true}'])

    outcome = _run(config, worktree, router)

    assert outcome["status"] == "succeeded"
    frame = _frame(router.prompts[0])
    assert "CHILDREN" in frame
    assert "branch_id=child-1" in frame
    assert "lifecycle=succeeded" in frame
    assert "result=succeeded" in frame


def test_usage_event_carries_situation_frame_provenance(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    worktree = tmp_path / "repo"
    _make_repo(worktree)
    limits = SituationFrameLimits(
        max_frame_bytes=2_048,
        max_section_bytes=180,
        max_section_items=1,
    )
    monkeypatch.setattr(worker, "SITUATION_FRAME_LIMITS", limits)
    router = _Router(['{"type":"finish","summary":"done","objective_met":true}'])
    writer = _Writer()

    outcome = _run(_config(worktree), worktree, router, writer)

    assert outcome["status"] == "succeeded"
    usage = next(message for message in writer.messages() if message["type"] == "usage_event")
    frame = _frame(router.prompts[0])
    payload_lines = frame.splitlines()[1:-1]
    truncated = [
        section
        for section in SECTION_ORDER
        if any(line.startswith(f"  [truncated {section};") for line in payload_lines)
    ]
    assert usage["situation_frame_version"] == 1
    assert usage["situation_frame_source_watermark"] == 2
    assert usage["situation_frame_sha256"] == hashlib.sha256(
        "\n".join(payload_lines).encode("utf-8")
    ).hexdigest()
    assert usage["situation_frame_bytes"] == len(frame.encode("utf-8"))
    assert usage["situation_frame_truncated_sections"] == truncated
