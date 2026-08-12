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
from dataclasses import replace
from pathlib import Path
from typing import Any

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
        {"role": "assistant", "content": "{\"type\": \"plan\", \"steps\": []}"},
    ]
    prompt2 = worker._build_agent_prompt("edit a.txt", [{"name": "read_batch"}], plan_transcript)
    assert prompt2["messages"][-1]["role"] == "user"
    assert prompt2["messages"][-1]["content"] == "Continue."
    # The static system prefix is unchanged across transcripts.
    assert prompt2["messages"][0]["content"] == messages[0]["content"]


def test_build_agent_prompt_static_head_is_byte_stable_across_tasks() -> None:
    """D8c: the system-prompt head must be byte-identical across tasks and
    transcripts; only the trailing ``Task:`` line may vary (provider
    exact-prefix caching keys on the stable head)."""
    tools = [{"name": "read_batch", "parameters": {"type": "object", "properties": {}}}]
    identity = "codex/gpt-5.6-luna"
    task_a = "task alpha"
    task_b = "task bravo longer"
    prompt_a = worker._build_agent_prompt(task_a, tools, [], model_identity=identity)
    prompt_b = worker._build_agent_prompt(task_b, tools, [], model_identity=identity)
    content_a = prompt_a["messages"][0]["content"]
    content_b = prompt_b["messages"][0]["content"]
    head_a, _, tail_a = content_a.rpartition("Task: ")
    head_b, _, tail_b = content_b.rpartition("Task: ")
    assert head_a == head_b
    assert tail_a == task_a
    assert tail_b == task_b
    # The byte-length difference is exactly the task-text byte-length
    # difference (the 5385-vs-5387 cross-session observation is this, by
    # design, never a volatile token in the head).
    bytes_a = len(content_a.encode("utf-8"))
    bytes_b = len(content_b.encode("utf-8"))
    assert bytes_b - bytes_a == len(task_b.encode("utf-8")) - len(task_a.encode("utf-8"))
    # prompt_prefix_bytes mirrors the system-message byte length exactly.
    assert prompt_prefix_bytes(prompt_a) == bytes_a
    assert prompt_prefix_bytes(prompt_b) == bytes_b


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


def test_build_agent_prompt_head_passes_d8c_lint() -> None:
    """The static head (first 3 lines) carries no volatile timestamp or
    request_id token; dynamic content sits at the bottom (D8c)."""
    tools = [{"name": "read_batch", "parameters": {"type": "object", "properties": {}}}]
    prompt = worker._build_agent_prompt(
        "a task", tools, [], model_identity="codex/gpt-5.6-luna"
    )
    validate_prompt_structure(prompt)  # raises PromptStructureError on churn
    head = prompt["messages"][0]["content"]
    assert "\nTask: " in head  # dynamic content is the final line, not the head



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
            '{"type":"finish","summary":"tests failed anyway"}',
            '{"type":"finish","summary":"still unverified"}',
            '{"type":"tool_call","name":"read_batch","arguments":'
            '{"paths":["note.txt"]}}',
            '{"type":"tool_call","name":"run_shell","arguments":{"cmd":["true"]}}',
            '{"type":"finish","summary":"verified"}',
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
    assert "did not run a successful verification command" in rejected[0]


def test_finish_after_edit_without_verification_attempt_succeeds(
    tmp_path: Path,
) -> None:
    repo = tmp_path / "repo"
    worktree = _make_worktree(repo)
    config = _agent_config(worktree)
    router = _ScriptedRouter(
        [
            '{"type":"plan","steps":["edit alpha.txt"]}',
            '{"type":"tool_call","name":"edit_file","arguments":'
            '{"path":"alpha.txt","old_string":"alpha-content","new_string":"ALPHA"}}',
            '{"type":"finish","summary":"edited, no tests available"}',
        ]
    )

    outcome = asyncio.run(_drive_loop(config, worktree, router))

    assert outcome["status"] == "succeeded"
    assert outcome["summary"] == "edited, no tests available"
    assert outcome["turn"] == 3
    assert len(router.prompts) == 3
    assert not any(
        "finish rejected" in message["content"]
        for message in outcome["transcript"]
    )


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
            '{"type":"finish","summary":"verified edit"}',
        ]
    )

    outcome = asyncio.run(_drive_loop(config, worktree, router))

    assert outcome["status"] == "succeeded"
    assert outcome["summary"] == "verified edit"
    assert not any(
        "finish rejected" in message["content"]
        for message in outcome["transcript"]
    )


def test_plan_and_thought_round_trip_through_parser() -> None:
    assert worker._parse_agent_action('{"type":"plan","steps":["a","b"]}') == {
        "type": "plan",
        "steps": ["a", "b"],
    }
    assert worker._parse_agent_action(
        '{"type":"plan","steps":["a"],"thought":"reasoning"}'
    ) == {"type": "plan", "steps": ["a"]}
    assert worker._parse_agent_action(
        '{"type":"tool_call","name":"read_batch","arguments":{"paths":["a.py"]},'
        '"thought":"need context"}'
    ) == {"type": "tool_call", "name": "read_batch", "arguments": {"paths": ["a.py"]}}
    assert worker._parse_agent_action(
        '{"type":"finish","summary":"done","thought":"verified"}'
    ) == {"type": "finish", "summary": "done"}

    # Concatenated actions: the FIRST complete object is parsed; the rest is
    # surfaced via _action_trailing.
    assert worker._parse_agent_action(
        '{"type":"finish","summary":"done"}'
        '{"type":"tool_call","name":"read_batch","arguments":{"paths":["a.py"]}}'
    ) == {"type": "finish", "summary": "done"}
    assert worker._action_trailing(
        '{"type":"finish","summary":"done"}'
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
        '{"type":"finish","summary":"done","extra":1}',
    ):
        with pytest.raises(ValueError):
            worker._parse_agent_action(bad)


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
            '{"type":"tool_call","name":"run_shell","arguments":{"cmd":["true"]}}',
            '{"type":"finish","summary":"wrote file"}',
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
            '{"type":"plan","steps":["b"]}',
            '{"type":"plan","steps":["c"]}',
            '{"type":"plan","steps":["d"]}',
            '{"type":"plan","steps":["e"]}',
            '{"type":"finish","summary":"must never be reached"}',
        ]
    )

    outcome = asyncio.run(_drive_loop(config, worktree, router))

    assert outcome["status"] == "failed"
    assert "no progress" in outcome["failure_reason"]
    assert outcome["turn"] == 3  # failed on the 3rd consecutive plan
    assert len(router.prompts) == 3  # no further router calls
    assert not any(
        "must never be reached" in message["content"]
        for message in outcome["transcript"]
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
            '{"type":"finish","summary":"read the file"}',
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
            '{"type":"finish","summary":"read both files"}',
        ]
    )

    outcome = asyncio.run(_drive_loop(config, worktree, router))

    assert outcome["status"] == "succeeded"
    assert outcome["summary"] == "read both files"
    assert outcome["turn"] == 2
    assert len(router.prompts) == 2
    assert any(
        "trailing JSON was ignored" in message["content"]
        for message in outcome["transcript"]
    )


def test_three_invalid_actions_fail_fast_with_no_progress(tmp_path: Path) -> None:
    """Invalid (unparseable) actions count toward the no-progress guard and
    fail fast at the 3rd consecutive one instead of burning the turn budget."""
    repo = tmp_path / "repo"
    worktree = _make_worktree(repo)
    config = _agent_config(worktree)
    router = _ScriptedRouter(
        [
            '{"type":"plan"',
            '{"type":"plan"',
            '{"type":"plan"',
            '{"type":"finish","summary":"must never be reached"}',
        ]
    )

    outcome = asyncio.run(_drive_loop(config, worktree, router))

    assert outcome["status"] == "failed"
    assert "no progress" in outcome["failure_reason"]
    assert "max turns exceeded" not in outcome["failure_reason"]
    assert outcome["turn"] == 3  # failed on the 3rd consecutive invalid action
    assert len(router.prompts) == 3  # no further router calls


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
    (worktree / "main.py").write_text(
        "def add(a, b):\n    return a + b\n", encoding="utf-8"
    )
    subprocess.run(
        ["git", "-C", str(worktree), "add", "main.py"], check=True, capture_output=True
    )
    subprocess.run(
        ["git", "-C", str(worktree), "commit", "-m", "add main.py"],
        check=True,
        capture_output=True,
    )
    base_commit = _base_commit(worktree)
    config = replace(_agent_config(worktree), base_commit=base_commit)

    # The agent's real change, left uncommitted in the worktree.
    (worktree / "main.py").write_text(
        "def add(a, b):\n    return a - b\n", encoding="utf-8"
    )
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
        ".pyc" in name or "__pycache__" in name or ".pytest_cache" in name
        for name in committed
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
