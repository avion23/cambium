#!/usr/bin/env python3
"""Repeatable measurements for Cambium's non-provider overhead.

The harness deliberately measures the existing production seams rather than
reimplementing them.  It uses ``perf_counter`` for wall-clock micro-benchmarks
and a small, separate ``cProfile`` pass for the in-process CPU paths.  No
provider or network access is used.  Git and SQLite measurements use temporary
repositories/databases, and the prompt/TUI fixtures mirror the shapes in
``tests/scenarios/test_diffundo_codex.py`` and
``tests/scenarios/test_tui_screen.py``.

Run from the repository root with::

    PYTHONPATH=src python3 scripts/profile_overhead.py

The default sample count is intentionally modest so this can be run as a
repeatable baseline check.  ``--iterations`` and ``--warmups`` are available
when a longer run is useful.  Import measurements launch fresh interpreters
with ``python -X importtime``; all other measurements run in this process.
"""

from __future__ import annotations

import argparse
import asyncio
import cProfile
import io
import json
import os
import pstats
import re
import subprocess
import sys
import tempfile
import time
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from types import SimpleNamespace
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
SRC = ROOT / "src"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))

import cambium.diffundo as diffundo_module  # noqa: E402
from cambium.diffundo import (  # noqa: E402
    AuthMode,
    CredentialSource,
    Diffundo,
    Protocol,
    ProviderConfig,
    ProviderTier,
    _codex_request_body,
)
from cambium.merge import MergeSequencer  # noqa: E402
from cambium.prompts import CODING_AGENT  # noqa: E402
from cambium.schemas import TOOL_SCHEMAS, validate_tool_call  # noqa: E402
from cambium.store import EventStore  # noqa: E402
from cambium.tui_screen import Transcript, render_cockpit  # noqa: E402
from cambium.worker import (  # noqa: E402
    CHECKPOINT_EPOCH_SCHEMA,
    AgentConfig,
    _build_agent_prompt,
    _canonical_json_bytes,
    _write_epoch_checkpoint,
)

DEFAULT_ITERATIONS = 12
DEFAULT_WARMUPS = 2
IMPORT_TARGET = "cambium.supervisor"
IMPORT_CODE = f"import {IMPORT_TARGET}"
IMPORT_LINE = re.compile(r"^import time:\s+(\d+)\s+\|\s+(\d+)\s+\|\s+(.+?)\s*$")


def _parse_decimal(value: str) -> int:
    """Parse the digit-only importtime capture without a permissive cast."""
    if not value or not value.isdecimal():
        raise ValueError(f"expected decimal importtime value, got {value!r}")
    parsed = 0
    for character in value:
        parsed = parsed * 10 + ord(character) - ord("0")
    return parsed


@dataclass(frozen=True, slots=True)
class Measurement:
    """One set of wall-clock samples, all represented in milliseconds."""

    name: str
    samples_ms: tuple[float, ...]
    load: str
    unit: str = "ms/op"

    def _ordered_samples(self) -> list[float]:
        if not self.samples_ms:
            raise ValueError(f"measurement {self.name!r} has no samples")
        return sorted(self.samples_ms)

    @property
    def median_ms(self) -> float:
        ordered = self._ordered_samples()
        middle = len(ordered) // 2
        if len(ordered) % 2:
            return ordered[middle]
        return (ordered[middle - 1] + ordered[middle]) / 2.0

    @property
    def mean_ms(self) -> float:
        samples = self._ordered_samples()
        return sum(samples) / len(samples)

    @property
    def p95_ms(self) -> float:
        ordered = self._ordered_samples()
        rank = max(1, (len(ordered) * 95 + 99) // 100)
        return ordered[rank - 1]

    def as_json(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "samples": len(self.samples_ms),
            "median_ms": self.median_ms,
            "p95_ms": self.p95_ms,
            "mean_ms": self.mean_ms,
            "unit": self.unit,
            "load": self.load,
        }


def _measure(
    name: str,
    operation: Callable[[], Any],
    *,
    iterations: int,
    warmups: int,
    load: str,
    unit: str = "ms/op",
) -> Measurement:
    """Warm one operation and collect ``perf_counter`` samples."""
    for _ in range(warmups):
        operation()
    samples: list[float] = []
    for _ in range(iterations):
        start = time.perf_counter()
        operation()
        samples.append((time.perf_counter() - start) * 1000.0)
    return Measurement(name, tuple(samples), load, unit)


class _RequestConstructed(RuntimeError):
    """Stops the real transport immediately after it constructs a request."""


class _NoNetworkOpener:
    """urllib opener used to time request construction without making a call."""

    def open(self, *_args: Any, **_kwargs: Any) -> Any:
        raise _RequestConstructed


def _representative_prompt() -> dict[str, Any]:
    """Build the same prompt/tool shape used by the worker-loop scenarios."""
    tools = [
        schema
        for schema in TOOL_SCHEMAS
        if schema.get("name") in {"read_batch", "write_file", "edit_file", "run_shell"}
    ]
    transcript = [
        {
            "role": "assistant",
            "content": '{"type":"plan","steps":["inspect","edit","verify"]}',
        },
        {"role": "user", "content": "tool read_batch: 8 files loaded"},
        {
            "role": "assistant",
            "content": (
                '{"type":"tool_call","name":"edit_file","arguments":'
                '{"path":"src/cambium/store.py"}}'
            ),
        },
        {"role": "user", "content": "tool edit_file: replacement applied"},
        {
            "role": "assistant",
            "content": (
                '{"type":"tool_call","name":"run_shell","arguments":'
                '{"cmd":["python","-m","pytest"]}}'
            ),
        },
        {"role": "user", "content": "tool run_shell: tests passed"},
    ]
    prompt = _build_agent_prompt(
        "Measure the existing overhead paths without changing production behavior.",
        tools,
        transcript,
        model_identity="codex/gpt-5.6-luna",
    )
    # Keep the large fixed prompt visible in the fixture metadata and ensure
    # this remains representative even if the worker prompt changes later.
    if not prompt["messages"][0]["content"].startswith(CODING_AGENT.splitlines()[0]):
        raise RuntimeError("worker prompt fixture no longer has its coding-agent header")
    return prompt


def _provider_request_measurements(
    prompt: dict[str, Any], *, iterations: int, warmups: int
) -> list[Measurement]:
    """Time both real transport request-build paths, stopping before I/O."""
    chat_provider = ProviderConfig(
        name="profile-chat",
        tier=ProviderTier.STRONG,
        base_url="https://api.example.test/v1",
        api_key_env="CAMBIUM_PROVIDER_PROFILE_API_KEY",
        model="example-model",
        max_retries=0,
    )
    codex_provider = ProviderConfig(
        name="profile-codex",
        tier=ProviderTier.STRONG,
        base_url="",
        api_key_env="",
        model="gpt-5.6-luna",
        auth=AuthMode.CODEX_CHATGPT,
        protocol=Protocol.CODEX_RESPONSES,
        reasoning_effort="max",
        max_retries=0,
    )
    chat_router = Diffundo([chat_provider], task_id="profile-chat")
    codex_router = Diffundo(
        [codex_provider],
        task_id="profile-codex",
        credential_source=CredentialSource("profile-token", "profile-account"),
        codex_profile={
            "api_origin": "https://chatgpt.com",
            "api_path": "/backend-api/codex/responses",
        },
    )
    previous_opener = diffundo_module.urllib.request.build_opener
    diffundo_module.urllib.request.build_opener = lambda *_args, **_kwargs: _NoNetworkOpener()
    previous_key = os.environ.get(chat_provider.api_key_env)
    os.environ[chat_provider.api_key_env] = "profile-key"

    def chat_request() -> None:
        try:
            chat_router._post_sync(chat_provider, prompt, timeout_s=1.0)
        except _RequestConstructed:
            return
        raise AssertionError("chat transport did not reach the request-construction boundary")

    def codex_request() -> None:
        try:
            codex_router._post_sync(codex_provider, prompt, timeout_s=1.0)
        except _RequestConstructed:
            return
        raise AssertionError("codex transport did not reach the request-construction boundary")

    load = f"{len(prompt['messages'])} messages, {len(prompt['tools'])} tools; no network"
    try:
        return [
            _measure(
                "provider request construction (chat)",
                chat_request,
                iterations=iterations,
                warmups=warmups,
                load=load,
            ),
            _measure(
                "provider request construction (codex)",
                codex_request,
                iterations=iterations,
                warmups=warmups,
                load=load,
            ),
        ]
    finally:
        diffundo_module.urllib.request.build_opener = previous_opener
        if previous_key is None:
            os.environ.pop(chat_provider.api_key_env, None)
        else:
            os.environ[chat_provider.api_key_env] = previous_key


def _event_persistence_measurement(*, iterations: int, warmups: int) -> Measurement:
    """Measure a critical EventStore append through SQLite WAL + fsync."""
    with tempfile.TemporaryDirectory(prefix="cambium-profile-store-") as temporary:
        store = EventStore(
            Path(temporary) / "events.db",
            fsync_interval_s=60.0,
            max_queue_size=256,
            critical_timeout_s=10.0,
        )
        event = {
            "kind": "result",
            "payload": {
                "status": "succeeded",
                "summary": "Representative worker result with a bounded payload.",
                "files_changed": ["src/cambium/store.py", "scripts/profile_overhead.py"],
                "commits": ["a" * 40],
                "diff": "@@ -1,4 +1,4 @@\n- old\n+ new\n" * 12,
            },
            "task_id": "profile-task",
            "worker_id": "profile-task#1",
            "generation": 1,
            "request_id": "profile-request",
        }
        try:
            measurement = _measure(
                "event persistence (EventStore critical write)",
                lambda: _assert_int(store.append(event)),
                iterations=iterations,
                warmups=warmups,
                load="SQLite WAL result row; INSERT + checkpoint + fsync",
            )
        finally:
            store.close()
    return measurement


def _assert_int(value: Any) -> int:
    if not isinstance(value, int) or isinstance(value, bool):
        raise AssertionError(f"expected an accepted sequence, got {value!r}")
    return value


def _checkpoint_fixture(prompt: dict[str, Any]) -> dict[str, Any]:
    messages = [
        {"role": message["role"], "content": str(message["content"])}
        for message in prompt["messages"]
    ]
    cache_key = {
        "provider": "profile-chat",
        "model": "example-model",
        "protocol": "chat_completions",
        "reasoning_effort": None,
        "system_sha256": "a" * 64,
        "tools_sha256": "b" * 64,
        "prefix_sha256": "c" * 64,
        "suffix_sha256": "d" * 64,
        "full_sha256": "e" * 64,
        "prefix_bytes": len(messages[0]["content"].encode("utf-8")),
        "message_count": len(messages),
        "redacted": False,
        "provider_boundary": {
            "provider": "profile-chat",
            "endpoint": "https://api.example.test/v1",
            "authmode": "api_key",
            "api_key_env": "CAMBIUM_PROVIDER_PROFILE_API_KEY",
            "provider_env_keys": ["CAMBIUM_PROVIDER_PROFILE_API_KEY"],
            "authorized_providers": None,
            "authorized_providers_explicit": False,
            "protocol": "chat_completions",
            "model": "example-model",
            "tier": "strong",
            "reasoning_effort": None,
            "provider_config_path": "profile/providers.json",
        },
    }
    return {
        "schema": CHECKPOINT_EPOCH_SCHEMA,
        "task_id": "profile-task",
        "generation": 1,
        "epoch": 4,
        "turn": 7,
        "created_at": 1_755_000_000.0,
        "cache_key": cache_key,
        "provider_messages": messages,
        "continuation_suffix": messages[-2:],
        "checkpoint_ref": "profile-task/epoch-004-aaaaaaaaaaaaaaaa-bbbbbbbbbbbbbbbb.json",
        "code_changed": True,
        "verified_after_change": True,
        "verification_failed": False,
        "no_progress_actions": 0,
        "budget_new_tokens": 512,
        "previous_prompt_tokens": 12_500,
        "cumulative_usage": {"prompt_tokens": 12_500, "completion_tokens": 1_200},
        "wall_deadline": 1_755_003_600.0,
    }


def _checkpoint_measurements(
    prompt: dict[str, Any], *, iterations: int, warmups: int
) -> list[Measurement]:
    """Measure pure canonical encoding and the immutable epoch-file path."""
    payload = _checkpoint_fixture(prompt)
    encoded_size = len(_canonical_json_bytes(payload))

    serialization = _measure(
        "checkpoint serialization (canonical JSON)",
        lambda: _canonical_json_bytes(payload),
        iterations=iterations,
        warmups=warmups,
        load=f"{encoded_size:,} canonical bytes; {len(payload['provider_messages'])} messages",
    )

    with tempfile.TemporaryDirectory(prefix="cambium-profile-checkpoint-") as temporary:
        config = AgentConfig(
            task_id="profile-task",
            generation=1,
            task="Measure checkpoint overhead without changing runtime behavior.",
            worktree=None,
            base_commit=None,
            fanout_config=None,
            max_turns=8,
            max_tokens=20_000,
            shell_permission=True,
            network_permission=False,
            heartbeat_interval_s=1.0,
            max_wall_s=3_600.0,
            checkpoint_root=Path(temporary),
        )
        provider_messages = payload["provider_messages"]
        suffix = payload["continuation_suffix"]

        next_turn = [1]

        def write_checkpoint() -> None:
            turn = next_turn[0]
            next_turn[0] += 1
            checkpoint = _write_epoch_checkpoint(
                config,
                turn=turn,
                epoch=turn,
                provider_messages=provider_messages,
                continuation_suffix=suffix,
                provider="profile-chat",
                model="example-model",
                tools_sha256="b" * 64,
                provider_compat={"profile-chat": ("chat_completions", None)},
                cumulative_usage={"prompt_tokens": 12_500, "completion_tokens": 1_200},
                wall_deadline=1_755_003_600.0,
                created_at=1_755_000_000.0 + turn,
            )
            if checkpoint is None:
                raise AssertionError("checkpoint root unexpectedly disabled")

        persistence = _measure(
            "checkpoint epoch serialization + fsync",
            write_checkpoint,
            iterations=iterations,
            warmups=warmups,
            load=f"immutable epoch file; {encoded_size:,} canonical bytes",
        )
    return [serialization, persistence]


def _git_run(repo: Path, *args: str, input_text: str | None = None) -> str:
    result = subprocess.run(
        ["git", *args],
        cwd=repo,
        input=input_text,
        capture_output=True,
        text=True,
        check=True,
    )
    return result.stdout.strip()


def _init_profile_repo(repo: Path, branches: int) -> tuple[str, list[str], list[Path]]:
    """Create test-style base/worker commits before timing the merge pipeline."""
    subprocess.run(
        ["git", "init", "--quiet", "--initial-branch=main", str(repo)], check=True
    )
    _git_run(repo, "config", "user.name", "cambium-profile")
    _git_run(repo, "config", "user.email", "cambium-profile@example.test")
    _git_run(repo, "config", "gc.auto", "0")
    (repo / "base.txt").write_text("base\n", encoding="utf-8")
    _git_run(repo, "add", "base.txt")
    _git_run(repo, "commit", "--quiet", "-m", "profile base")
    base = _git_run(repo, "rev-parse", "HEAD")
    worker_branches: list[str] = []
    staging_paths: list[Path] = []
    base_tree = _git_run(repo, "rev-parse", f"{base}^{{tree}}")
    base_entries = _git_run(repo, "ls-tree", base)
    for index in range(branches):
        name = f"profile-worker-{index:03d}"
        filename = f"profile-{index:03d}.txt"
        blob = _git_run(repo, "hash-object", "-w", "--stdin", input_text=f"worker {index}\n")
        tree = _git_run(
            repo,
            "mktree",
            input_text=f"{base_entries}\n100644 blob {blob}\t{filename}\n",
        )
        commit = _git_run(
            repo,
            "commit-tree",
            tree,
            "-p",
            base,
            input_text=f"profile worker {index}\n",
        )
        _git_run(repo, "update-ref", f"refs/heads/{name}", commit)
        worker_branches.append(name)
        staging_paths.append(repo.parent / f"profile-staging-{index:03d}")
    if not base_tree:
        raise AssertionError("profile base tree was not created")
    return base, worker_branches, staging_paths


def _git_merge_measurement(*, iterations: int, warmups: int) -> Measurement:
    total = iterations + warmups
    with tempfile.TemporaryDirectory(prefix="cambium-profile-git-") as temporary:
        repo = Path(temporary) / "repo"
        repo.mkdir()
        base, branches, staging_paths = _init_profile_repo(repo, total)
        state = {"expected_old": base, "index": 0}

        def merge_one() -> None:
            index = state["index"]
            state["index"] += 1
            sequencer = MergeSequencer(task_id=f"profile-merge-{index:03d}")
            staged = sequencer.prepare_staging(
                repo,
                staging_paths[index],
                branches[index],
                "main",
            )
            sequencer.publish_merge(repo, staged, state["expected_old"])
            state["expected_old"] = staged
            sequencer.cleanup_staging(repo)

        return _measure(
            "git merge pipeline (stage + rebase + publish + cleanup)",
            merge_one,
            iterations=iterations,
            warmups=warmups,
            load="real git repo; one clean worker commit per fast-forward",
        )


def _tui_snapshot() -> SimpleNamespace:
    agents = tuple(
        SimpleNamespace(
            task_id=f"profile-task-{index}",
            role="main" if index == 0 else "subagent",
            state="active" if index < 3 else "succeeded",
            provider="codex" if index % 2 == 0 else "zai",
            model="gpt-5.6-luna" if index % 2 == 0 else "glm-5.3",
            tool="read_batch" if index < 3 else None,
            total_tokens=12_345 + index * 1_111,
            output_tokens_per_s=47.5 + index,
        )
        for index in range(4)
    )
    context = SimpleNamespace(
        epoch=4,
        summary_segments=3,
        approximate=True,
        estimated_trunk_tokens=9_000,
        summary_trunk_bytes=32_000,
        estimated_raw_tail_tokens=800,
        checkpoint_ref="profile-task/epoch-004-aaaaaaaaaaaaaaaa-bbbbbbbbbbbbbbbb.json",
    )
    recent_events = tuple(
        SimpleNamespace(kind="tool_event", detail=f"read_batch: ok · {20 + index}ms")
        for index in range(8)
    )
    return SimpleNamespace(
        session_status="running",
        agents=agents,
        active_agents=3,
        total_tokens=16_789,
        output_tokens_per_s=51.2,
        context=context,
        recent_events=recent_events,
    )


def _tui_measurements(*, iterations: int, warmups: int) -> list[Measurement]:
    snapshot = _tui_snapshot()
    transcript = Transcript(max_entries=160)
    event_batch = [
        {
            "kind": "tool_event" if index % 3 else "context_checkpoint",
            "payload": (
                {"tool": "read_batch", "ok": True, "duration_ms": 20 + index}
                if index % 3
                else {"epoch": 4, "summary_segments": 3}
            ),
        }
        for index in range(32)
    ]
    for index, event in enumerate(event_batch):
        transcript.observe_event(event)
        if index % 8 == 0:
            transcript.assistant(
                "# Inspection result\n- Found the bounded event path\n"
                "```python\nreturn measured_latency\n```"
            )

    def render() -> int:
        frame = render_cockpit(
            snapshot,
            transcript,
            session_description="session=/tmp/cambium-profile/session-0001",
            branch_line="branch: turn=4 provider=codex model=gpt-5.6-luna epoch=4",
            cumulative_line="usage: calls=12 tokens=16789 out/s=51.2",
            width=120,
            height=40,
            color=False,
        )
        if len(frame) != 40:
            raise AssertionError(f"TUI frame changed size: {len(frame)}")
        return len(frame)

    batch = _measure(
        "TUI render (prepared event batch)",
        render,
        iterations=iterations,
        warmups=warmups,
        load="32-event transcript state; 120x40 cockpit; color disabled",
    )
    per_event = Measurement(
        "TUI render (per event in batch)",
        tuple(sample / len(event_batch) for sample in batch.samples_ms),
        "derived from the prepared 32-event batch",
        "ms/event",
    )
    return [batch, per_event]


def _import_measurements(*, iterations: int, warmups: int) -> list[Measurement]:
    """Measure fresh-process import wall time and ``-X importtime`` cumulative time."""
    env = dict(os.environ)
    env["PYTHONPATH"] = str(SRC)

    def once() -> tuple[float, float]:
        start = time.perf_counter()
        result = subprocess.run(
            [sys.executable, "-X", "importtime", "-c", IMPORT_CODE],
            cwd=ROOT,
            env=env,
            capture_output=True,
            text=True,
            check=True,
        )
        wall_ms = (time.perf_counter() - start) * 1000.0
        cumulative_us: int | None = None
        for line in result.stderr.splitlines():
            match = IMPORT_LINE.match(line)
            if match is not None and match.group(3).strip() == IMPORT_TARGET:
                cumulative_us = _parse_decimal(match.group(2))
        if cumulative_us is None:
            raise RuntimeError(
                f"python -X importtime did not report {IMPORT_TARGET}; "
                f"stderr tail={result.stderr[-500:]!r}"
            )
        return wall_ms, cumulative_us / 1000.0

    for _ in range(warmups):
        once()
    wall: list[float] = []
    cumulative: list[float] = []
    for _ in range(iterations):
        wall_ms, cumulative_ms = once()
        wall.append(wall_ms)
        cumulative.append(cumulative_ms)
    load = f"fresh {sys.executable} process; -X importtime {IMPORT_TARGET}"
    return [
        Measurement("module startup (fresh process wall)", tuple(wall), load),
        Measurement("module import (importtime cumulative)", tuple(cumulative), load),
    ]


def _schema_measurement(*, iterations: int, warmups: int) -> Measurement:
    schema = next(schema for schema in TOOL_SCHEMAS if schema.get("name") == "read_batch")
    call = {"paths": [f"src/cambium/module_{index:02d}.py" for index in range(16)]}
    if validate_tool_call(schema, call):
        raise AssertionError("schema fixture was rejected before timing")

    def validate() -> int:
        errors = validate_tool_call(schema, call)
        if errors:
            raise AssertionError(f"schema fixture became invalid: {errors}")
        return len(call["paths"])

    return _measure(
        "schema validation (read_batch tool call)",
        validate,
        iterations=iterations,
        warmups=warmups,
        load="16 paths; real TOOL_SCHEMAS + validate_tool_call",
    )


async def _mailbox_samples(iterations: int, warmups: int) -> list[float]:
    """Measure a blocked supervisor-style Queue.get across one scheduler turn."""
    payload = {
        "type": "tool_event",
        "task_id": "profile-task",
        "generation": 1,
        "tool": "read_batch",
        "ok": True,
        "duration_ms": 24,
        "batch_index": 3,
        "batch_size": 16,
    }

    async def one() -> float:
        mailbox: asyncio.Queue[dict[str, Any]] = asyncio.Queue(maxsize=256)

        async def producer() -> None:
            await asyncio.sleep(0)
            await mailbox.put(payload)

        producer_task = asyncio.create_task(producer())
        start = time.perf_counter()
        received = await asyncio.wait_for(mailbox.get(), timeout=1.0)
        elapsed_ms = (time.perf_counter() - start) * 1000.0
        await producer_task
        if received is not payload:
            raise AssertionError("mailbox returned the wrong message")
        return elapsed_ms

    for _ in range(warmups):
        await one()
    return [await one() for _ in range(iterations)]


def _mailbox_measurement(*, iterations: int, warmups: int) -> Measurement:
    samples = asyncio.run(_mailbox_samples(iterations, warmups))
    return Measurement(
        "mailbox wait (blocked asyncio.Queue.get)",
        tuple(samples),
        "empty queue; producer publishes after one scheduler yield",
    )


def _cprofile_hot_paths(prompt: dict[str, Any], schema: dict[str, Any], snapshot: Any) -> None:
    """Run CPU-only paths for a compact cProfile view after wall timing."""
    transcript = Transcript(max_entries=160)
    transcript.assistant("# Profile\n- representative transcript\n" * 8)
    provider = ProviderConfig(
        name="profile-codex",
        tier=ProviderTier.STRONG,
        base_url="",
        api_key_env="",
        model="gpt-5.6-luna",
        auth=AuthMode.CODEX_CHATGPT,
        protocol=Protocol.CODEX_RESPONSES,
        reasoning_effort="max",
    )
    call = {"paths": [f"src/cambium/module_{index:02d}.py" for index in range(16)]}
    profile = cProfile.Profile()
    profile.enable()
    for _ in range(100):
        _codex_request_body(provider, prompt)
        _canonical_json_bytes(_checkpoint_fixture(prompt))
        validate_tool_call(schema, call)
        render_cockpit(
            snapshot,
            transcript,
            session_description="session=/tmp/profile",
            branch_line="branch: turn=4",
            cumulative_line="usage: calls=12 tokens=16789 out/s=51.2",
            width=120,
            height=40,
            color=False,
        )
    profile.disable()
    output = io.StringIO()
    pstats.Stats(profile, stream=output).strip_dirs().sort_stats("cumulative").print_stats(5)
    print("\ncProfile top cumulative functions (100 CPU-path rounds):")
    print(output.getvalue().rstrip())


def _format_measurement(measurement: Measurement) -> str:
    return (
        f"{measurement.name:<52} {len(measurement.samples_ms):>3}  "
        f"{measurement.median_ms:>9.3f}  {measurement.p95_ms:>9.3f}  "
        f"{measurement.mean_ms:>9.3f}  {measurement.unit:<8} {measurement.load}"
    )


def _print_text(measurements: list[Measurement], *, iterations: int, warmups: int) -> None:
    print("Cambium overhead profile (measurement only; no production changes)")
    print(f"date_utc: {time.strftime('%Y-%m-%dT%H:%M:%SZ', time.gmtime())}")
    print(f"python: {sys.version.split()[0]}  platform: {sys.platform}")
    print(f"iterations: {iterations}  warmups: {warmups}")
    print()
    print(f"{'measurement':<52}  {'n':>3}  {'median':>9}  {'p95':>9}  {'mean':>9}  unit      load")
    print("-" * 150)
    for measurement in measurements:
        print(_format_measurement(measurement))


def _parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--iterations",
        type=int,
        default=DEFAULT_ITERATIONS,
        help=f"timed samples per operation (default: {DEFAULT_ITERATIONS})",
    )
    parser.add_argument(
        "--warmups",
        type=int,
        default=DEFAULT_WARMUPS,
        help=f"discarded warmup rounds per operation (default: {DEFAULT_WARMUPS})",
    )
    parser.add_argument(
        "--json",
        action="store_true",
        help="emit the measurements as JSON instead of the text table",
    )
    parser.add_argument(
        "--no-cprofile",
        action="store_true",
        help="skip the compact CPU-path cProfile report",
    )
    args = parser.parse_args(argv)
    if args.iterations < 1:
        parser.error("--iterations must be >= 1")
    if args.warmups < 0:
        parser.error("--warmups must be >= 0")
    return args


def main(argv: list[str] | None = None) -> int:
    args = _parse_args(argv)
    prompt = _representative_prompt()
    measurements: list[Measurement] = []
    measurements.extend(
        _provider_request_measurements(prompt, iterations=args.iterations, warmups=args.warmups)
    )
    measurements.append(
        _event_persistence_measurement(iterations=args.iterations, warmups=args.warmups)
    )
    measurements.extend(
        _checkpoint_measurements(prompt, iterations=args.iterations, warmups=args.warmups)
    )
    measurements.append(_git_merge_measurement(iterations=args.iterations, warmups=args.warmups))
    measurements.extend(_tui_measurements(iterations=args.iterations, warmups=args.warmups))
    measurements.extend(_import_measurements(iterations=args.iterations, warmups=args.warmups))
    measurements.append(_schema_measurement(iterations=args.iterations, warmups=args.warmups))
    measurements.append(_mailbox_measurement(iterations=args.iterations, warmups=args.warmups))

    if args.json:
        print(json.dumps({"measurements": [item.as_json() for item in measurements]}, indent=2))
    else:
        _print_text(measurements, iterations=args.iterations, warmups=args.warmups)
        if not args.no_cprofile:
            schema = next(schema for schema in TOOL_SCHEMAS if schema.get("name") == "read_batch")
            _cprofile_hot_paths(prompt, schema, _tui_snapshot())
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
