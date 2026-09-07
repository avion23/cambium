"""Regression tests for the Phase 2 worker SituationFrame remainder slice.

Covers three worker.py behaviors end to end:
1. a summary-only epoch checkpoint restores the real admitted child branch IDs
   from checkpoint metadata after the delegate actions were summarized away;
2. a resumed non-summary epoch checkpoint projects correct raw-tail
   message/byte metrics from the ``partition_summary_trunk`` remainder;
3. a redacted SituationFrame header digest matches the frame bytes actually
   delivered to the provider.
"""

from __future__ import annotations

import asyncio
import copy
import hashlib
import json
import re
import subprocess
import threading
from pathlib import Path
from types import SimpleNamespace
from typing import Any

from cambium import worker
from cambium.diffundo import ProviderTier
from cambium.fencing import write_generation
from cambium.redact import Redactor
from cambium.summary_trunk import SummaryEntry, render_summary_message


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
        ["git", "-C", str(repo), "config", "user.name", "situation-remainder-test"],
        check=True,
        capture_output=True,
    )
    subprocess.run(
        ["git", "-C", str(repo), "config", "user.email", "situation-remainder@test"],
        check=True,
        capture_output=True,
    )
    (repo / ".gitignore").write_text(".cambium/\n", encoding="utf-8")
    (repo / "fixture.txt").write_text("fixture\n", encoding="utf-8")
    subprocess.run(["git", "-C", str(repo), "add", "-A"], check=True, capture_output=True)
    subprocess.run(
        ["git", "-C", str(repo), "commit", "-m", "initial"], check=True, capture_output=True
    )
    write_generation(repo, 1)


def _config(worktree: Path, **overrides: Any) -> worker.AgentConfig:
    values: dict[str, Any] = {
        "task_id": "situation-remainder",
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


def _child_result(child_task_id: str) -> dict[str, Any]:
    return {
        "parent_task_id": "situation-remainder",
        "child_task_id": child_task_id,
        "unified_diff": "",
        "diff_truncated": False,
        "summary": "child completed review",
        "metric_score": None,
        "metric_breakdown": {},
        "commits": [],
        "files_changed": ["review.md"],
        "status": "succeeded",
    }


def test_summary_only_resume_restores_admitted_child_branch_ids(tmp_path: Path) -> None:
    """Post-delegation summary checkpoint resumes with the real admitted branch ID.

    The delegate action was summarized away, so only the checkpoint metadata
    still carries the admitted child identity. Without it the projection falls
    back to the synthetic ``<task>:resume-child-N`` placeholder.
    """

    worktree = tmp_path / "repo"
    _make_repo(worktree)
    checkpoint_root = tmp_path / "checkpoints"
    base_config = _config(worktree, checkpoint_root=checkpoint_root)
    admitted_branch_id = "branch-7f3a9c1d"
    entry = SummaryEntry(
        type="summary_entry",
        sequence=1,
        source_sha256="b" * 64,
        source_message_count=4,
        through_turn=1,
        objective="delegate the review and collect its result",
        outcome="delegate admitted one child branch and returned its result",
        decisions_added=(),
        decisions_superseded=(),
        facts_added=(),
        facts_invalidated=(),
        files_and_symbols_changed=(),
        verification_results=(),
        relevant_failed_approaches=(),
        open_items=(),
    )
    summary_only_request = [
        {"role": "system", "content": "system prefix"},
        worker._task_message(base_config.task),
        # The delegate action and its transcript were absorbed into this
        # entry; no assistant delegate message survives in the trunk.
        render_summary_message(entry),
    ]

    checkpoint = worker._write_epoch_checkpoint(
        base_config,
        turn=1,
        epoch=1,
        provider_messages=copy.deepcopy(summary_only_request),
        continuation_suffix=[],
        admitted_child_task_ids=[admitted_branch_id],
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
            "child_results": [_child_result(admitted_branch_id)],
            "child_results_truncated": False,
            "rejection_feedback": None,
            "workspace_changed": False,
        },
    )
    router = _Router(['{"type":"finish","summary":"resumed","objective_met":true}'])

    outcome = _run(config, worktree, router)

    assert outcome["status"] == "succeeded", outcome
    frame = _frame(router.prompts[0])
    assert "CHILDREN" in frame
    assert f"branch_id={admitted_branch_id}" in frame
    assert "lifecycle=succeeded" in frame
    assert "result=succeeded" in frame
    assert "resume-child" not in frame


def test_resumed_non_summary_checkpoint_reports_raw_tail_metrics(tmp_path: Path) -> None:
    """Resumed ordinary messages after the stable head count as raw tail."""

    worktree = tmp_path / "repo"
    _make_repo(worktree)
    checkpoint_root = tmp_path / "checkpoints"
    base_config = _config(worktree, checkpoint_root=checkpoint_root)
    ordinary_request = [
        {"role": "system", "content": "system prefix"},
        {"role": "user", "content": "turn-1 instruction"},
        {"role": "assistant", "content": "turn-1 result"},
        {"role": "user", "content": "turn-2 instruction"},
    ]
    suffix = [{"role": "assistant", "content": "turn-2 partial output"}]

    checkpoint = worker._write_epoch_checkpoint(
        base_config,
        turn=2,
        epoch=1,
        provider_messages=copy.deepcopy(ordinary_request),
        continuation_suffix=copy.deepcopy(suffix),
        provider="scenario-provider",
        model="scenario-model",
        tools_sha256="a" * 64,
        provider_compat={"scenario-provider": ("loopback", None)},
    )
    assert checkpoint is not None

    # The same load + projection wiring the epoch-resume branch of
    # _run_agent_loop performs, including the empty child-result continuation.
    loaded = worker._load_epoch_checkpoint(
        base_config, checkpoint.checkpoint_ref, expect_task_id=True
    )
    repository, branch = worker._situation_git_identity(worktree)
    tools = worker._exposed_tool_schemas(base_config)
    state = worker._worker_situation_state(
        config=base_config,
        worktree=worktree,
        repository=repository,
        branch=branch,
        tools=tools,
        events=worker._initial_situation_events(base_config, worktree, repository, branch, tools),
        model="scenario-model",
        last_provider=None,
        turn=loaded.turn + 1,
        epoch_count=loaded.epoch,
        current_epoch_checkpoint=loaded,
        base_messages=tuple(copy.deepcopy(loaded.full_messages)),
        context_continuation=[],
        wall_deadline=worker.time.monotonic() + 60.0,
    )

    expected_tail = [*loaded.full_messages[2:]]
    assert state.context.summary_segments == 0
    assert state.context.raw_tail_messages == len(expected_tail)
    assert state.context.raw_tail_bytes == len(
        json.dumps(
            expected_tail,
            sort_keys=True,
            separators=(",", ":"),
            ensure_ascii=False,
            allow_nan=False,
        ).encode("utf-8")
    )


def test_redacted_mission_yields_frame_digest_matching_delivered_bytes(tmp_path: Path) -> None:
    """Redaction inside the mission re-stamps the header digest."""

    worktree = tmp_path / "repo"
    _make_repo(worktree)
    secret = "sk-" + "a1B2c3D4e5F6g7H8i9J0k1L2"
    config = _config(
        worktree,
        task=f"rotate the leaked key {secret} then report",
        redactor=Redactor(),
    )
    router = _Router(['{"type":"finish","summary":"done","objective_met":true}'])

    outcome = _run(config, worktree, router)

    assert outcome["status"] == "succeeded", outcome
    frame = _frame(router.prompts[0])
    assert secret not in frame
    assert "objective: rotate the leaked key" in frame
    lines = frame.rstrip("\n").splitlines()
    header_match = re.search(r'frame_sha256="([0-9a-f]{64})"', lines[0])
    assert header_match is not None
    payload = "\n".join(lines[1:-1])
    assert header_match.group(1) == hashlib.sha256(payload.encode("utf-8")).hexdigest()
