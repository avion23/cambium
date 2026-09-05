"""Focused deterministic and bounded SituationFrame scenarios."""

from __future__ import annotations

import asyncio
import hashlib
import json
import subprocess
import threading
from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from cambium import worker
from cambium.branch_state import BranchState, inspect_state
from cambium.diffundo import ProviderTier
from cambium.fencing import write_generation
from cambium.situation import SECTION_ORDER, SituationFrameLimits, render_situation_frame


def _events() -> list[dict]:
    return [
        {
            "seq": 1,
            "kind": "task_assigned",
            "task_id": "root",
            "payload": {
                "session_id": "session-1",
                "task": "repair the parser",
                "repo": "/repo",
                "worktree": "/session/wt",
                "branch": "cambium/root",
                "constraints": ["keep the public API stable"],
                "done_when": ["focused test passes"],
                "verification": ["python -m pytest tests/test_parser.py -q"],
                "writable_scope": ["src/parser.py", "tests/test_parser.py"],
                "tools": ["read_batch", "edit_file", "run_shell"],
            },
        },
        {
            "seq": 2,
            "kind": "child_admitted",
            "task_id": "root",
            "payload": {
                "child_task_id": "review-1",
                "parent_task_id": "root",
                "child_kind": "review",
                "context_mode": "fresh",
                "placement": "spread",
                "critical": True,
            },
        },
        {
            "seq": 3,
            "kind": "context_checkpoint",
            "task_id": "root",
            "payload": {
                "epoch": 2,
                "checkpoint_ref": "root/epoch-002.json",
                "cache_key": {"provider": "provider-a", "model": "model-a"},
            },
        },
        {
            "seq": 4,
            "kind": "tool_event",
            "task_id": "root",
            "payload": {
                "tool": "read_batch",
                "turn": 1,
                "batch_index": 0,
                "ok": True,
            },
        },
    ]


def test_same_replayed_state_renders_byte_identically() -> None:
    state = inspect_state(_events())
    replayed = BranchState.from_json(state.to_json())

    first = render_situation_frame(state)
    second = render_situation_frame(replayed)

    assert first == second
    assert [line for line in first.splitlines() if line in SECTION_ORDER] == list(SECTION_ORDER)
    assert 'frame_sha256="' in first.splitlines()[0]


def test_section_and_whole_caps_name_omissions_and_anchor_inspection() -> None:
    state = inspect_state(
        [
            *_events(),
            {
                "seq": 5,
                "kind": "child_admitted",
                "task_id": "root",
                "payload": {
                    "child_task_id": "review-2",
                    "parent_task_id": "root",
                    "child_kind": "review",
                    "context_mode": "fresh",
                    "placement": "spread",
                },
            },
            {
                "seq": 6,
                "kind": "child_admitted",
                "task_id": "root",
                "payload": {
                    "child_task_id": "review-3",
                    "parent_task_id": "root",
                    "child_kind": "review",
                    "context_mode": "fresh",
                    "placement": "spread",
                },
            },
        ]
    )
    limits = SituationFrameLimits(
        max_frame_bytes=2_048,
        max_section_bytes=180,
        max_section_items=1,
        section_items={"CHILDREN": 1},
    )

    frame = render_situation_frame(state, limits)

    assert len(frame.encode("utf-8")) <= limits.max_frame_bytes
    assert [line for line in frame.splitlines() if line in SECTION_ORDER] == list(SECTION_ORDER)
    assert "truncated MISSION" in frame
    assert "truncated CHILDREN" in frame
    assert "inspect_state(section=MISSION, source_watermark=6)" in frame
    assert "inspect_state(section=CHILDREN, source_watermark=6)" in frame


def test_equivalent_arrival_orders_render_byte_identical_under_truncation() -> None:
    base = inspect_state(_events())
    obligations = ("obligation-z", "obligation-a", "obligation-m")
    blockers = ("blocker-z", "blocker-a", "blocker-m")
    anchors = ("anchor-z", "anchor-a", "anchor-m")
    first_arrival = replace(
        base,
        control=replace(
            base.control,
            open_obligations=obligations,
            blockers=blockers,
        ),
        anchors=anchors,
    )
    second_arrival = replace(
        base,
        control=replace(
            base.control,
            open_obligations=tuple(reversed(obligations)),
            blockers=tuple(reversed(blockers)),
        ),
        anchors=tuple(reversed(anchors)),
    )
    limits = SituationFrameLimits(
        max_frame_bytes=8_192,
        max_section_bytes=2_048,
        max_section_items=12,
        section_items={"OPEN": 4, "ANCHORS": 2},
    )

    first = render_situation_frame(first_arrival, limits)
    second = render_situation_frame(second_arrival, limits)

    assert first == second
    assert "  obligation[0]: obligation-a" in first
    assert "  obligation[1]: obligation-m" in first
    assert "  obligation[2]: obligation-z" in first
    assert "  blocker[0]: blocker-a" in first
    assert "truncated OPEN" in first
    assert "  anchor[0]: anchor-a" in first
    assert "  anchor[1]: anchor-m" in first
    assert "truncated ANCHORS" in first


class _RecordingRouter:
    def __init__(self) -> None:
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
        self.prompts.append(json.loads(json.dumps(prompt)))
        return SimpleNamespace(
            content='{"type":"finish","summary":"frame observed","objective_met":true}',
            model="scenario-model",
            usage={"prompt_tokens": 7, "completion_tokens": 3, "total_tokens": 10},
            provider="scenario-provider",
            latency_s=0.0,
            estimated_cost_usd=0.0,
            retry_after_s=None,
            request_rate_status=None,
            account_quota_owner=None,
            prompt_prefix_bytes=None,
            provider_cache_hit=None,
            fell_back_from=None,
        )


def _make_live_repo(repo: Path) -> None:
    repo.mkdir()
    subprocess.run(["git", "init", "-b", "main", str(repo)], check=True, capture_output=True)
    subprocess.run(
        ["git", "-C", str(repo), "config", "user.name", "situation-frame-test"],
        check=True,
        capture_output=True,
    )
    subprocess.run(
        ["git", "-C", str(repo), "config", "user.email", "situation-frame@test"],
        check=True,
        capture_output=True,
    )
    (repo / "fixture.txt").write_text("fixture\n", encoding="utf-8")
    subprocess.run(["git", "-C", str(repo), "add", "fixture.txt"], check=True, capture_output=True)
    subprocess.run(
        ["git", "-C", str(repo), "commit", "-m", "initial"],
        check=True,
        capture_output=True,
    )
    write_generation(repo, 1)


def _frame_from_message(message: dict[str, Any]) -> str:
    content = message.get("content")
    assert isinstance(content, str)
    start = content.index("<cambium-situation ")
    end = content.index("</cambium-situation>", start) + len("</cambium-situation>")
    return content[start:end]


def test_worker_loop_injects_bounded_ordered_situation_frame(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The real provider boundary receives the bounded frame as its last message."""
    repo = tmp_path / "repo"
    _make_live_repo(repo)
    limits = SituationFrameLimits(
        max_frame_bytes=2_048,
        max_section_bytes=180,
        max_section_items=1,
    )
    monkeypatch.setattr(worker, "SITUATION_FRAME_LIMITS", limits)
    config = worker.AgentConfig(
        task_id="situation-live",
        generation=1,
        task="observe the bounded operating picture",
        worktree=repo,
        base_commit=None,
        fanout_config={},
        max_turns=1,
        max_tokens=100,
        shell_permission=False,
        network_permission=False,
        heartbeat_interval_s=0.05,
        max_wall_s=60.0,
        checkpoint_root=None,
        context_reuse=False,
    )
    router = _RecordingRouter()

    outcome = asyncio.run(
        worker._run_agent_loop(
            config=config,
            router=router,  # type: ignore[arg-type]  # duck-typed Diffundo
            tier=ProviderTier.FAST,
            model="scenario-model",
            worktree=repo,
            writer=None,
            stop=threading.Event(),
            progress=worker.AgentProgress(),
        )
    )

    assert outcome["status"] == "succeeded"
    assert len(router.prompts) == 1
    model_message = router.prompts[0]["messages"][-1]
    assert model_message["role"] == "user"
    frame = _frame_from_message(model_message)
    lines = frame.splitlines()
    assert lines[0].startswith('<cambium-situation version="1"')
    assert lines[-1] == "</cambium-situation>"
    assert [line for line in lines if line in SECTION_ORDER] == list(SECTION_ORDER)
    assert len(frame.encode("utf-8")) <= limits.max_frame_bytes

    section_starts = [lines.index(section) for section in SECTION_ORDER]
    for index, section in enumerate(SECTION_ORDER):
        start = section_starts[index]
        end = section_starts[index + 1] if index + 1 < len(section_starts) else len(lines) - 1
        section_lines = lines[start:end]
        assert len(("\n".join(section_lines) + "\n").encode("utf-8")) <= limits.bytes_for(section)
        content_lines = [line for line in section_lines[1:] if not line.startswith("  [truncated ")]
        assert len(content_lines) <= limits.items_for(section)

    assert "truncated AUTHORITY" in frame
    assert "inspect_state(section=AUTHORITY, source_watermark=2)" in frame
    payload = "\n".join(lines[1:-1])
    digest = hashlib.sha256(payload.encode("utf-8")).hexdigest()
    assert f'frame_sha256="{digest}"' in lines[0]
