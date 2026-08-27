"""Adversarial seam reproducers for the current main branch.

The intentionally failing assertions document bugs found without changing
production code.  Passing probes cover the nearby command/tool contracts.
"""

from __future__ import annotations

import asyncio
import io
import json
import subprocess
import threading
from pathlib import Path
from types import SimpleNamespace
from typing import Any, cast

import pytest
from diffundo_helpers import PROMPT, FakeServer, _config, _ok_payload, _set_keys

from cambium import tui, worker
from cambium.diffundo import Diffundo, ProviderTier
from cambium.observability import snapshot_from_events
from cambium.oneshot import OneShotConfig
from cambium.supervisor import run_plan
from cambium.tools import MAX_OUTPUT_BYTES, OUTPUT_TRUNCATION_MARKER, ToolContext, run_tool
from cambium.tui_screen import ActivityState, _char_width


class _Tty(io.StringIO):
    def isatty(self) -> bool:
        return True


class _Writer:
    def __init__(self) -> None:
        self.lines: list[bytes] = []

    def write(self, value: bytes) -> None:
        self.lines.append(value)

    async def drain(self) -> None:
        return None

    def messages(self) -> list[dict[str, Any]]:
        return [json.loads(line) for line in self.lines]


class _Call:
    def __init__(self, content: str) -> None:
        self.content = content
        self.provider = "loopback-provider"
        self.model = "loopback-model"
        self.usage = {"prompt_tokens": 7, "completion_tokens": 3, "total_tokens": 10}
        self.latency_s = 0.01
        self.estimated_cost_usd = 0.0
        self.retry_after_s = None
        self.request_rate_status = None
        self.account_quota_owner = None
        self.prompt_prefix_bytes = None
        self.provider_cache_hit = None


class _ScriptedRouter:
    def __init__(self, responses: list[str]) -> None:
        self.responses = list(responses)
        self.prompts: list[dict[str, Any]] = []

    def declared_model(self, _provider: str) -> str:
        return ""

    async def call(self, _tier: ProviderTier, prompt: dict[str, Any], **_kwargs: Any) -> _Call:
        self.prompts.append(prompt)
        if not self.responses:
            raise AssertionError("no scripted response")
        return _Call(self.responses.pop(0))


def _event(
    seq: int, kind: str, *, task_id: str = "task", generation: int = 1, **payload: Any
) -> dict[str, Any]:
    return {
        "seq": seq,
        "kind": kind,
        "task_id": task_id,
        "generation": generation,
        "monotonic_ms": seq * 100,
        "payload": payload,
    }


def _repo_with_worktree(root: Path) -> tuple[Path, Path, str]:
    repo = root / "repo"
    repo.mkdir()
    subprocess.run(["git", "init", "-b", "main", str(repo)], check=True, capture_output=True)
    subprocess.run(["git", "-C", str(repo), "config", "user.name", "bughunt"], check=True)
    subprocess.run(
        ["git", "-C", str(repo), "config", "user.email", "bughunt@example.test"], check=True
    )
    (repo / "a.txt").write_text("base\n", encoding="utf-8")
    subprocess.run(["git", "-C", str(repo), "add", "-A"], check=True, capture_output=True)
    subprocess.run(
        ["git", "-C", str(repo), "commit", "-m", "base"], check=True, capture_output=True
    )
    base = subprocess.run(
        ["git", "-C", str(repo), "rev-parse", "main"], check=True, capture_output=True, text=True
    ).stdout.strip()
    worktree = root / "wt"
    subprocess.run(
        ["git", "-C", str(repo), "worktree", "add", "-b", "bughunt", str(worktree), base],
        check=True,
        capture_output=True,
    )
    from cambium.fencing import write_generation

    write_generation(worktree, 1)
    return repo, worktree, base


def test_observability_reopens_a_lane_after_an_intermediate_failed_result() -> None:
    snapshot = snapshot_from_events(
        [
            _event(1, "spawned"),
            _event(2, "result", status="failed"),
            _event(3, "exit", reason="crash"),
            _event(4, "restart_scheduled", restart_count=1, max_restarts=2),
            _event(5, "spawned", generation=2),
            _event(6, "ready", generation=2),
            _event(7, "result", generation=2, status="succeeded"),
            _event(8, "exit", generation=2, reason="done"),
        ]
    )

    assert snapshot.agents[0].state == "succeeded"


def test_activity_stays_live_while_a_suspended_generation_waits_for_resume() -> None:
    activity = ActivityState()
    activity.start(now=0.0)
    activity.observe_event(_event(1, "result", status="suspended"), now=1.0)

    assert activity.active
    assert activity.state != "DONE"


def test_resume_at_turn_two_restores_no_progress_history(tmp_path: Path) -> None:
    _repo, worktree, _base = _repo_with_worktree(tmp_path)
    checkpoint_root = tmp_path / "checkpoints"
    config = worker.AgentConfig(
        task_id="resume-agent",
        generation=1,
        task="continue",
        worktree=worktree,
        base_commit=None,
        fanout_config={},
        max_turns=10,
        max_tokens=200_000,
        shell_permission=True,
        network_permission=False,
        heartbeat_interval_s=0.05,
        max_wall_s=60.0,
        checkpoint_root=checkpoint_root,
        max_no_progress_actions=1,
        progress_window=1,
    )
    repeated = '{"type":"plan","steps":["inspect"]}'
    tools_sha = worker._sha256_hex(
        json.dumps(worker._exposed_tool_schemas(config), sort_keys=True).encode()
    )
    checkpoint = worker._write_epoch_checkpoint(
        config,
        turn=2,
        epoch=1,
        messages=[
            {"role": "system", "content": "You are the agent."},
            {"role": "user", "content": "prior"},
            {"role": "assistant", "content": repeated},
        ],
        provider="loopback-provider",
        model="loopback-model",
        tools_sha256=tools_sha,
        provider_compat={"loopback-provider": ("loopback", None)},
    )
    assert checkpoint is not None
    # AgentConfig uses slots; construct the resumed copy explicitly.
    resumed = worker.AgentConfig(
        task_id=config.task_id,
        generation=config.generation,
        task=config.task,
        worktree=config.worktree,
        base_commit=config.base_commit,
        fanout_config=config.fanout_config,
        max_turns=config.max_turns,
        max_tokens=config.max_tokens,
        shell_permission=config.shell_permission,
        network_permission=config.network_permission,
        heartbeat_interval_s=config.heartbeat_interval_s,
        max_wall_s=config.max_wall_s,
        checkpoint_root=config.checkpoint_root,
        max_no_progress_actions=config.max_no_progress_actions,
        progress_window=config.progress_window,
        resume={
            "checkpoint_ref": checkpoint.checkpoint_ref,
            "epoch": checkpoint.epoch,
            "child_results": [],
            "child_results_truncated": False,
            "workspace_changed": False,
        },
    )
    router = _ScriptedRouter([repeated])
    outcome = asyncio.run(
        worker._run_agent_loop(
            config=resumed,
            router=router,  # type: ignore[arg-type]
            tier=ProviderTier.FAST,
            model="loopback-model",
            worktree=worktree,
            writer=None,
            stop=threading.Event(),
            progress=worker.AgentProgress(),
        )
    )

    assert "no progress" in (outcome["failure_reason"] or "")
    assert len(router.prompts) == 1


def test_bound_provider_lease_rotates_on_timeout_failover(monkeypatch: pytest.MonkeyPatch) -> None:
    primary = FakeServer(
        [
            (200, _ok_payload("first", model="m-a"), 0.0),
            (200, _ok_payload("late", model="m-a"), 0.1),
        ]
    )
    sibling = FakeServer([(200, _ok_payload("fallback", model="m-b"), 0.0)])
    _set_keys(monkeypatch, "BUGHUNT_A", "BUGHUNT_B")
    router = Diffundo(
        (
            _config("a", primary, "BUGHUNT_A", model="m-a", timeout_s=0.03, priority=0),
            _config("b", sibling, "BUGHUNT_B", model="m-b", priority=1),
        ),
        primary_provider="a",
        call_budget_s=1.0,
        pause_timeout_s=0.01,
    )
    try:
        first = asyncio.run(router.call(ProviderTier.FAST, PROMPT, model="m-a"))
        router.bind_provider(first.provider, first.model)
        fallback = asyncio.run(router.call(ProviderTier.FAST, PROMPT, model="m-a"))
        assert fallback.provider == "b"
        router.bind_provider(fallback.provider, fallback.model)
    finally:
        primary.close()
        sibling.close()


def test_timeout_fallback_does_not_reclaim_a_recovered_old_provider(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    primary = FakeServer(
        [
            (200, _ok_payload("first", model="m-a"), 0.0),
            (200, _ok_payload("late", model="m-a"), 0.1),
            (200, _ok_payload("recovered", model="m-a"), 0.0),
        ]
    )
    sibling = FakeServer(
        [
            (200, _ok_payload("fallback", model="m-b"), 0.0),
            (200, _ok_payload("stay-on-sibling", model="m-b"), 0.0),
        ]
    )
    _set_keys(monkeypatch, "BUGHUNT_A2", "BUGHUNT_B2")
    router = Diffundo(
        (
            _config("a", primary, "BUGHUNT_A2", model="m-a", timeout_s=0.03, priority=0),
            _config("b", sibling, "BUGHUNT_B2", model="m-b", priority=1),
        ),
        primary_provider="a",
        call_budget_s=1.0,
        pause_timeout_s=0.01,
    )
    try:
        first = asyncio.run(router.call(ProviderTier.FAST, PROMPT, model="m-a"))
        router.bind_provider(first.provider, first.model)
        fallback = asyncio.run(router.call(ProviderTier.FAST, PROMPT, model="m-a"))
        assert fallback.provider == "b"
        router._runtime("a").cooldown_until = __import__("time").monotonic() - 1
        next_call = asyncio.run(router.call(ProviderTier.FAST, PROMPT, model="m-a"))
        assert next_call.provider == "b"
    finally:
        primary.close()
        sibling.close()


def test_empty_fanout_config_does_not_silently_select_marker_worker(tmp_path: Path) -> None:
    repo, _unused_worktree, base = _repo_with_worktree(tmp_path)
    session_dir = tmp_path / "session"
    result = asyncio.run(
        run_plan(
            session_dir,
            {
                "tasks": [
                    {
                        "task_id": "empty-fanout",
                        "task": "provider mode",
                        "repo": str(repo),
                        "worktree_path": str(session_dir / "wt"),
                        "branch": "empty-fanout",
                        "base_commit": base,
                        "fanout_config": {},
                        "target_file": "a.txt",
                        "marker": "// must not be marker mode",
                    }
                ]
            },
        )
    )

    assert result.results[0].status == "failed"


def test_rejected_child_is_not_left_as_a_queued_observability_lane() -> None:
    snapshot = snapshot_from_events(
        [
            _event(1, "task_assigned", task_id="parent"),
            _event(2, "spawned", task_id="parent"),
            _event(3, "result", task_id="parent", status="succeeded"),
            _event(
                4,
                "child_rejected",
                task_id="parent",
                parent_task_id="parent",
                child_task_id="bad-child",
                child_kind="not-a-kind",
                reason="TaskPlanError",
            ),
            _event(5, "session_ended", task_id="", session_status="ended"),
        ]
    )

    rejected = next(agent for agent in snapshot.agents if agent.task_id == "bad-child")
    assert rejected.state != "queued"
    assert snapshot.queued_agents == 0


def test_delegated_child_proposals_get_distinct_request_ids() -> None:
    writer = _Writer()
    config = SimpleNamespace(task_id="parent")
    arguments = {"child_task_id": "child", "kind": "test", "spec": {"task": "x"}}

    async def scenario() -> None:
        await worker._emit_delegated_child(
            cast(Any, writer), cast(Any, config), arguments, request_id="run-1"
        )
        await worker._emit_delegated_child(
            cast(Any, writer), cast(Any, config), arguments, request_id="run-1"
        )

    asyncio.run(scenario())
    messages = writer.messages()
    assert len({message["request_id"] for message in messages}) == 2


def test_command_and_tool_edge_probes_are_stable(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    prompts: list[str] = []

    async def fake_run(self: Any, turn: Any, *, on_event: Any = None) -> Any:
        del self, on_event
        prompts.append(turn.config.prompt)
        from cambium.supervisor import PlanResult, TaskResult

        return PlanResult((TaskResult(task_id="task", status="succeeded", exit_code=0),))

    monkeypatch.setattr("cambium.interactive.InteractiveSession.run_turn", fake_run)
    output = _Tty()
    code = asyncio.run(
        tui.run_tui(
            OneShotConfig(repo=tmp_path, session_root=tmp_path / "interactive"),
            input_stream=_Tty("work\n\n/unknown-command\n/exit\n"),
            output_stream=output,
            error_stream=_Tty(),
        )
    )
    assert code == 0
    assert prompts == ["work"]
    assert "Unknown command: /unknown-command" in output.getvalue()

    stderr_only = asyncio.run(
        run_tool(
            "run_shell",
            {"cmd": ["/bin/sh", "-c", "printf stderr-only >&2"]},
            ToolContext(tmp_path),
        )
    )
    assert stderr_only.ok and stderr_only.output == "stderr-only"
    failed = asyncio.run(
        run_tool(
            "run_shell",
            {"cmd": ["/bin/sh", "-c", "printf out; printf err >&2; exit 7"]},
            ToolContext(tmp_path),
        )
    )
    assert not failed.ok and failed.error == "run_shell exited with status 7"
    huge = asyncio.run(
        run_tool(
            "run_shell",
            {"cmd": ["/bin/sh", "-c", "printf '%*s' 70000 x"]},
            ToolContext(tmp_path),
        )
    )
    assert huge.ok
    assert huge.output.endswith(OUTPUT_TRUNCATION_MARKER)
    assert len(huge.output.encode()) <= MAX_OUTPUT_BYTES
    assert _char_width("界") == 2
    assert _char_width("\u0301") == 0
