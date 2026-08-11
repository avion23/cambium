"""Worker agent-loop improvements: plan-before-act, transcript bounding,
lint feedback visibility, read_batch exposure, and the heartbeat drain fix.

The provider-backed loop is driven in-process with a scripted fake router
(no network, no subprocess): a real worktree, real tool dispatch, and real
``Diffundo.call``-shaped responses.
"""

from __future__ import annotations

import asyncio
import json
import subprocess
import threading
import time
from pathlib import Path
from typing import Any

import pytest

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


class _FakeCallResult:
    def __init__(
        self,
        content: str,
        *,
        model: str = "loopback-model",
        usage: dict[str, int] | None = None,
        provider: str = "loopback-provider",
        latency_s: float = 0.01,
    ) -> None:
        self.content = content
        self.model = model
        self.usage = usage or {"prompt_tokens": 7, "completion_tokens": 3, "total_tokens": 10}
        self.provider = provider
        self.latency_s = latency_s


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


def _make_worktree(repo: Path, branch: str = "agent-loop") -> Path:
    repo.mkdir(parents=True)
    subprocess.run(["git", "init", "-b", "main", str(repo)], check=True, capture_output=True)
    subprocess.run(
        ["git", "-C", str(repo), "config", "user.name", "agent-loop-test"], check=True
    )
    subprocess.run(
        ["git", "-C", str(repo), "config", "user.email", "agent-loop@test"], check=True
    )
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
    return worker.AgentConfig(
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
        **overrides,
    )


async def _drive_loop(
    config: worker.AgentConfig, worktree: Path, router: _ScriptedRouter
) -> dict[str, Any]:
    return await worker._run_agent_loop(
        config=config,
        router=router,  # type: ignore[arg-type]  # duck-typed Diffundo
        tier=ProviderTier.FAST,
        model="loopback-model",
        worktree=worktree,
        writer=None,
        stop=threading.Event(),
        progress=worker.AgentProgress(),
    )


# ---------------------------------------------------------------------------
# Plan-before-act: plan action parses, is stored, and the loop proceeds
# ---------------------------------------------------------------------------

def test_build_agent_prompt_turn_one_has_non_system_message() -> None:
    """Turn-one payloads must contain a non-system message (ZAI/GLM 1214)."""
    prompt = worker._build_agent_prompt("edit a.txt", [{"name": "read_file"}], [])
    messages = prompt["messages"]
    assert messages[0]["role"] == "system"
    assert any(message.get("role") != "system" for message in messages)
    # The static system prefix is unchanged when a transcript already exists.
    with_transcript = worker._build_agent_prompt(
        "edit a.txt",
        [{"name": "read_file"}],
        [{"role": "assistant", "content": "{\"type\": \"plan\", \"steps\": []}"}],
    )
    assert with_transcript["messages"][0]["content"] == messages[0]["content"]



def test_plan_before_act_plan_read_batch_finish(tmp_path: Path) -> None:
    repo = tmp_path / "repo"
    worktree = _make_worktree(repo)
    config = _agent_config(worktree)
    router = _ScriptedRouter(
        [
            '{"type":"plan","steps":["read both files","finish"]}',
            '{"type":"tool_call","name":"read_batch","arguments":'
            '{"paths":["alpha.txt","beta.txt"]}}',
            '{"type":"finish","summary":"read both files"}',
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
    assert transcript[-1]["content"].startswith('{"type":"finish"')

    tool_names = [schema["name"] for schema in worker._exposed_tool_schemas(config)]
    assert "read_batch" in tool_names


def test_plan_and_thought_round_trip_through_parser() -> None:
    assert worker._parse_agent_action('{"type":"plan","steps":["a","b"]}') == {
        "type": "plan",
        "steps": ["a", "b"],
    }
    assert worker._parse_agent_action(
        '{"type":"plan","steps":["a"],"thought":"reasoning"}'
    ) == {"type": "plan", "steps": ["a"]}
    assert worker._parse_agent_action(
        '{"type":"tool_call","name":"read_file","arguments":{"path":"a.py"},'
        '"thought":"need context"}'
    ) == {"type": "tool_call", "name": "read_file", "arguments": {"path": "a.py"}}
    assert worker._parse_agent_action(
        '{"type":"finish","summary":"done","thought":"verified"}'
    ) == {"type": "finish", "summary": "done"}

    for bad in (
        '{"type":"plan"}',
        '{"type":"plan","steps":[]}',
        '{"type":"plan","steps":["ok", 3]}',
        '{"type":"plan","steps":["ok"],"extra":1}',
        '{"type":"tool_call","name":"read_file","arguments":{},"extra":1}',
        '{"type":"finish","summary":"done","extra":1}',
    ):
        with pytest.raises(ValueError):
            worker._parse_agent_action(bad)


# ---------------------------------------------------------------------------
# Transcript summarization (pure function)
# ---------------------------------------------------------------------------


def test_summarize_transcript_small_untouched() -> None:
    transcript = [
        {"role": "assistant", "content": '{"type":"plan","steps":["one"]}'},
        {"role": "user", "content": "small observation"},
    ]
    result = worker._summarize_transcript(transcript, 120_000)
    assert result == transcript
    assert worker._transcript_chars(result) == worker._transcript_chars(transcript)


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
                    '{"type":"tool_call","name":"read_file",'
                    f'"arguments":{{"path":"f{index}.py"}}}}'
                ),
            }
        )
        transcript.append({"role": "user", "content": "x" * 2_000})
    budget = 5_000

    result = worker._summarize_transcript(transcript, budget, keep_turns=6)

    assert worker._transcript_chars(result) <= budget
    assert result != transcript
    # the plan survives intact at the front
    assert result[0] == plan_message
    assert json.loads(result[0]["content"])["type"] == "plan"
    # a synthetic dropped-message marker reports what was removed
    assert any(
        "prior context" in message.get("content", "") for message in result
    )
    marker = next(
        message for message in result if "prior context" in message.get("content", "")
    )
    assert "4 earlier message(s) dropped" in marker["content"]
    # exactly the last 6 turns (12 messages) of tail are retained
    tail = result[2:]
    assert len(tail) == 12
    assert tail[-1]["content"].startswith("x")
    # every tail turn has an assistant action and a user observation
    assert [message["role"] for message in tail] == (
        ["assistant", "user"] * 6
    )
    assert "f7.py" in tail[-2]["content"]


# ---------------------------------------------------------------------------
# Feedback loop: lint diagnostics from write_file reach the transcript
# ---------------------------------------------------------------------------


def test_lint_feedback_visible_in_transcript(tmp_path: Path) -> None:
    repo = tmp_path / "repo"
    worktree = _make_worktree(repo)
    config = _agent_config(worktree)
    router = _ScriptedRouter(
        [
            '{"type":"plan","steps":["write a file"]}',
            '{"type":"tool_call","name":"write_file","arguments":'
            '{"path":"broken.py","content":"broken(:\\n"}}',
            '{"type":"finish","summary":"wrote file"}',
        ]
    )

    outcome = asyncio.run(_drive_loop(config, worktree, router))

    assert outcome["status"] == "succeeded"
    observation = outcome["transcript"][-2]["content"]
    assert "tool write_file ok=True" in observation
    assert "Lint diagnostics:" in observation
    assert "E999" in observation


# ---------------------------------------------------------------------------
# Heartbeat drain: _run_task does not block on a heartbeat that sleeps long
# ---------------------------------------------------------------------------


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
        return await worker._run_task(writer, run, "drain", 1, stop, config)

    started = time.monotonic()
    outcome = asyncio.run(_run())
    elapsed = time.monotonic() - started

    assert outcome["status"] == "failed"  # invalid generation fails fast
    assert outcome["failure_reason"] == "invalid worker generation"
    # the old code waited HEARTBEAT_INTERVAL_S + 1.0 == 2.0s; the fixed code
    # drains as soon as the heartbeat observes the stop flag (~50ms).
    assert elapsed < 1.5
