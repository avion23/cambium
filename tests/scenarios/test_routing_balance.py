"""Supervisor-level task admission balancing (solution C) scenarios.

The model-selector engine balances (model, provider) at task admission from a
usage-debt ledger, before the model filter partitions the provider pool:

1. debt-aware selection: with two fake providers serving two different
   candidate models and a ledger favouring provider B, the first
   ``model_candidates`` task is assigned provider A; after its usage feeds the
   ledger, the next admission is assigned B. The chosen provider/model appear
   in the ``task_assigned`` events and in the worker requests, and the durable
   ledger reflects the folded usage.
2. sticky assignment end-to-end: the worker init carries ``assigned_provider``
   and the multi-turn task's usage events all show the assigned provider (no
   fallback while healthy), even though the seeded primary would pick the
   other provider.
3. generic HTTP 400 (no refusal marker) is a permanent REFUSAL: never
   retried, no health transition, cascade falls through.
4. the DebtStore ledger round-trips save/load and tolerates a corrupt file.

Unit-level selection/ledger scenarios run in the fast tier; worker and
supervisor subprocess scenarios carry the ``slow`` marker like the other
process-boundary suites.
"""

from __future__ import annotations

import asyncio
import json
import os
import signal
import subprocess
import sys
import threading
import time
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path
from typing import Any

import pytest

from cambium.diffundo import (
    AllProvidersFailed,
    Diffundo,
    HealthState,
    ProviderConfig,
    ProviderOutcome,
    ProviderTier,
)
from cambium.fencing import write_generation
from cambium.ipc import MAX_LINE_BYTES, read_message
from cambium.routing import DebtStore, ProviderDebt, select_primary
from cambium.supervisor import read_events, run_plan

ROOT = Path(__file__).resolve().parents[2]
WORKER = "cambium.worker"
PROVIDER_KEY_A = "CAMBIUM_PROVIDER_PROVIDER_A_API_KEY"
PROVIDER_KEY_B = "CAMBIUM_PROVIDER_PROVIDER_B_API_KEY"
PROVIDER_SECRET = "routing-balance-secret"
TASK_TEXT = "Append a single marker line starting with '// balance-' to target.txt."
DEFAULT_USAGE = {"prompt_tokens": 17, "completion_tokens": 9, "total_tokens": 26}


# --------------------------------------------------------------------------- #
# Fake provider server (http.server in a thread — no network)
# --------------------------------------------------------------------------- #


class FakeServer:
    """OpenAI-compatible /chat/completions server on an ephemeral port.

    ``behaviors`` is a list of ``(status, payload, delay_s)`` consumed in order;
    the last behavior repeats for any further request. Requests are recorded.
    """

    def __init__(
        self,
        behaviors: list[
            tuple[int, dict[str, Any], float]
            | tuple[int, dict[str, Any], float, dict[str, str]]
        ],
        *,
        host: str = "127.0.0.1",
    ) -> None:
        self.behaviors = list(behaviors)
        self.calls: list[dict[str, Any]] = []
        self.request_headers: list[dict[str, str | None]] = []
        self._lock = threading.Lock()
        self._httpd = HTTPServer((host, 0), _Handler)
        self._httpd.fake = self
        self._thread = threading.Thread(
            target=self._httpd.serve_forever,
            kwargs={"poll_interval": 0.001},
            daemon=True,
        )
        self._thread.start()
        self.base_url = f"http://{host}:{self._httpd.server_port}"

    def record(self, body: dict[str, Any], headers: dict[str, str | None]) -> int:
        with self._lock:
            self.calls.append(body)
            self.request_headers.append(headers)
            return len(self.calls) - 1

    def behavior_at(
        self, index: int
    ) -> tuple[int, dict[str, Any], float, dict[str, str]]:
        behavior = self.behaviors[index] if index < len(self.behaviors) else self.behaviors[-1]
        if len(behavior) == 3:
            status, payload, delay = behavior
            return status, payload, delay, {}
        status, payload, delay, headers = behavior
        return status, payload, delay, headers

    def close(self) -> None:
        self._httpd.shutdown()
        self._httpd.server_close()
        self._thread.join()


class _Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def do_POST(self) -> None:  # noqa: N802 (http.server API)
        length = int(self.headers.get("Content-Length") or 0)
        raw = self.rfile.read(length) if length else b""
        try:
            body = json.loads(raw.decode("utf-8") or "{}")
        except json.JSONDecodeError:
            body = {}
        server: FakeServer = self.server.fake  # type: ignore[attr-defined]
        index = server.record(
            body,
            {
                "User-Agent": self.headers.get("User-Agent"),
                "Authorization": self.headers.get("Authorization"),
            },
        )
        status, payload, delay, extra_headers = server.behavior_at(index)
        if delay:
            time.sleep(delay)
        encoded = json.dumps(payload).encode("utf-8")
        try:
            self.send_response(status)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(encoded)))
            for name, value in extra_headers.items():
                self.send_header(name, value)
            self.end_headers()
            self.wfile.write(encoded)
        except OSError:
            pass  # the client timed out (budget-capped attempt) and closed first

    def log_message(self, *args: object) -> None:
        pass


def _finish_payload(
    summary: str, *, model: str, total_tokens: int
) -> dict[str, Any]:
    """One strict ``finish`` agent action; ``total_tokens`` sizes the usage so
    the task's single call dominates the provider's debt utilization."""
    content = json.dumps({"type": "finish", "summary": summary})
    return {
        "id": "chatcmpl-balance",
        "object": "chat.completion",
        "model": model,
        "choices": [
            {
                "index": 0,
                "message": {"role": "assistant", "content": content},
                "finish_reason": "stop",
            }
        ],
        "usage": {
            "prompt_tokens": max(1, total_tokens - 5),
            "completion_tokens": 5,
            "total_tokens": total_tokens,
        },
    }


def _ok_payload(content: str, *, model: str) -> dict[str, Any]:
    return {
        "id": "chatcmpl-balance",
        "object": "chat.completion",
        "model": model,
        "choices": [
            {
                "index": 0,
                "message": {"role": "assistant", "content": content},
                "finish_reason": "stop",
            }
        ],
        "usage": dict(DEFAULT_USAGE),
    }


def _error_payload(message: str) -> dict[str, Any]:
    return {"error": {"message": message, "type": "test_error", "code": "test"}}


PROMPT = {"messages": [{"role": "user", "content": "hello"}]}


def _config(
    name: str,
    server: FakeServer,
    env: str,
    *,
    tier: ProviderTier = ProviderTier.FAST,
    model: str = "",
    **overrides: Any,
) -> ProviderConfig:
    base = dict(timeout_s=5.0, max_retries=0, rpm=60, enabled=True, model=model)
    base.update(overrides)
    return ProviderConfig(name=name, tier=tier, base_url=server.base_url, api_key_env=env, **base)


def _set_keys(monkeypatch: pytest.MonkeyPatch, *names: str) -> None:
    for name in names:
        monkeypatch.setenv(name, f"sk-test-{name}")


# --------------------------------------------------------------------------- #
# 1. select_primary: max-min utilization, tie-breaks, per-provider allowance
# --------------------------------------------------------------------------- #


def _pc(name: str, model: str, **overrides: Any) -> ProviderConfig:
    base = dict(
        tier=ProviderTier.FAST,
        base_url="http://127.0.0.1:1",
        api_key_env=f"K_{name.upper()}",
        model=model,
    )
    base.update(overrides)
    return ProviderConfig(name=name, **base)


def test_select_primary_max_min_utilization_tie_break_and_allowance() -> None:
    providers = [_pc("a", "m1"), _pc("b", "m2"), _pc("c", "m1")]
    debt = {
        "a": ProviderDebt(tokens=5_000_000),
        "b": ProviderDebt(tokens=1_000_000),
        "c": ProviderDebt(tokens=2_000_000, requests=1),
    }
    # b has the lowest normalized utilization (1M/20M) across both candidates
    assert select_primary(providers, ["m1", "m2"], debt) == ("b", "m2")
    # only m1 candidates: a (25%) vs c (10%) -> c
    assert select_primary(providers, ["m1"], debt) == ("c", "m1")
    # equal utilization -> fewer requests -> config order
    tied = {
        "a": ProviderDebt(tokens=100),
        "c": ProviderDebt(tokens=100, requests=2),
    }
    assert select_primary(providers, ["m1"], tied) == ("a", "m1")
    # a per-provider token_window_allowance scales that provider's window:
    # b's 1M tokens fill a 1M window (100%) while a sits at 25% of the default
    scaled = [_pc("a", "m1"), _pc("b", "m2", token_window_allowance=1_000_000)]
    assert select_primary(scaled, ["m1", "m2"], debt) == ("a", "m1")
    # no enabled provider serves a candidate -> ValueError
    with pytest.raises(ValueError):
        select_primary(providers, ["nope"], {})
    with pytest.raises(ValueError):
        select_primary(providers, [], {})


# --------------------------------------------------------------------------- #
# 2. DebtStore: save/load round-trip and corrupt-file tolerance
# --------------------------------------------------------------------------- #


def test_debt_store_round_trip_and_corrupt_tolerance(tmp_path) -> None:
    path = tmp_path / "routing-state.json"
    store = DebtStore(path)
    store.record({
        "provider": "p1",
        "usage": {"prompt_tokens": 10, "completion_tokens": 5, "total_tokens": 15},
        "estimated_cost_usd": 0.01,
    })
    store.record({
        "provider": "p1",
        "failure_reason": "quota: HTTP 429: rate limited",
        "request_rate_status": "cooldown",
    })
    store.record({"provider": "p2", "usage": {"total_tokens": 7}})
    assert store.dirty is True
    store.save()

    loaded = DebtStore(path)
    loaded.load()
    p1 = loaded.as_mapping()["p1"]
    assert p1.tokens == 15  # total_tokens wins over prompt+completion
    assert p1.requests == 2
    assert p1.failed_requests == 1
    assert p1.retry_after_count == 1  # 429 counted once
    assert p1.cost == 0.01
    assert loaded.as_mapping()["p2"].tokens == 7
    assert loaded.dirty is False

    # a corrupt file loads as an empty ledger without raising, and the store
    # keeps working
    path.write_text("{not-json!!!", encoding="utf-8")
    corrupt = DebtStore(path)
    corrupt.load()
    assert corrupt.as_mapping() == {}
    corrupt.record({"provider": "p3", "usage": {"total_tokens": 3}})
    corrupt.save()
    assert json.loads(path.read_text(encoding="utf-8"))["providers"]["p3"]["tokens"] == 3

    # a missing file is an empty ledger
    missing = DebtStore(tmp_path / "missing.json")
    missing.load()
    assert missing.as_mapping() == {}


# --------------------------------------------------------------------------- #
# 3. generic HTTP 400 is a permanent REFUSAL (never retried, no health change)
# --------------------------------------------------------------------------- #


def test_generic_400_is_refusal_not_retried_and_cascades(monkeypatch) -> None:
    bad = FakeServer([(400, _error_payload("messages illegal"), 0.0)])
    good = FakeServer([(200, _ok_payload("good", model="m"), 0.0)])
    _set_keys(monkeypatch, "K_BAD", "K_GOOD")
    router = Diffundo(
        (
            _config("p_bad", bad, "K_BAD", max_retries=2),
            _config("p_good", good, "K_GOOD", priority=5),
        )
    )
    try:
        result = asyncio.run(router.call(ProviderTier.FAST, PROMPT))
        assert result.provider == "p_good"
        # a retryable class would have attempted 3 times (max_retries=2)
        assert len(bad.calls) == 1
        assert len(good.calls) == 1
        # refusal never drives a health transition
        assert router.health("p_bad") is HealthState.UNKNOWN
    finally:
        bad.close()
        good.close()


def test_generic_400_single_provider_raises_refusal_outcome(monkeypatch) -> None:
    bad = FakeServer([(400, _error_payload("deterministic bad request"), 0.0)])
    _set_keys(monkeypatch, "K_BAD")
    router = Diffundo((_config("p_bad", bad, "K_BAD", max_retries=2),))
    try:
        # every candidate refused -> AllProvidersFailed wrapping the terminal
        # REFUSAL; a retryable class would have attempted 3 times first
        with pytest.raises(AllProvidersFailed) as exc_info:
            asyncio.run(router.call(ProviderTier.FAST, PROMPT))
        assert exc_info.value.last_error.outcome is ProviderOutcome.REFUSAL
        assert len(bad.calls) == 1
        assert router.health("p_bad") is HealthState.UNKNOWN
    finally:
        bad.close()


def test_primary_provider_kwarg_presets_sticky_binding(monkeypatch) -> None:
    first = FakeServer([(200, _ok_payload("first", model="m"), 0.0)])
    second = FakeServer([(200, _ok_payload("second", model="m"), 0.0)])
    _set_keys(monkeypatch, "K_FIRST", "K_SECOND")
    router = Diffundo(
        (
            _config("p_first", first, "K_FIRST", priority=0),
            _config("p_second", second, "K_SECOND", priority=0),
        ),
        primary_provider="p_second",
    )
    try:
        # the preset binding wins over the seeded first pick: every call of
        # this task's router stays on the assigned provider
        for _ in range(3):
            assert asyncio.run(router.call(ProviderTier.FAST, PROMPT)).provider == "p_second"
        assert len(second.calls) == 3
        assert len(first.calls) == 0
    finally:
        first.close()
        second.close()


def test_primary_provider_kwarg_absent_name_falls_back_to_seeded_pick(
    monkeypatch,
) -> None:
    first = FakeServer([(200, _ok_payload("first", model="m"), 0.0)])
    second = FakeServer([(200, _ok_payload("second", model="m"), 0.0)])
    _set_keys(monkeypatch, "K_FIRST", "K_SECOND")
    router = Diffundo(
        (
            _config("p_first", first, "K_FIRST", priority=0),
            _config("p_second", second, "K_SECOND", priority=0),
        ),
        primary_provider="p_missing",
    )
    try:
        result = asyncio.run(router.call(ProviderTier.FAST, PROMPT))
        assert result.provider == "p_first"  # seeded first pick
    finally:
        first.close()
        second.close()


# --------------------------------------------------------------------------- #
# 4. end-to-end: debt-aware selection through the real supervisor + worker
# --------------------------------------------------------------------------- #


def _make_repo(repo: Path) -> str:
    repo.mkdir(parents=True)
    subprocess.run(["git", "init", "-b", "main", str(repo)], check=True, capture_output=True)
    subprocess.run(
        ["git", "-C", str(repo), "config", "user.name", "routing-balance-test"], check=True
    )
    subprocess.run(
        ["git", "-C", str(repo), "config", "user.email", "routing@test"], check=True
    )
    subprocess.run(["git", "-C", str(repo), "config", "gc.auto", "0"], check=True)
    (repo / "target.txt").write_text("fixture\n", encoding="utf-8")
    subprocess.run(["git", "-C", str(repo), "add", "-A"], check=True, capture_output=True)
    subprocess.run(
        ["git", "-C", str(repo), "commit", "-m", "initial"], check=True, capture_output=True
    )
    return subprocess.run(
        ["git", "-C", str(repo), "rev-parse", "refs/heads/main"],
        check=True,
        capture_output=True,
        text=True,
    ).stdout.strip()


def _provider_config_file(
    path: Path, servers: list[tuple[str, FakeServer, str, str]]
) -> Path:
    """Write a provider config; entries are (name, server, env_key, model)."""
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(
            {
                "providers": [
                    {
                        "name": name,
                        "tier": "fast",
                        "base_url": server.base_url,
                        "api_key_env": env_key,
                        "timeout_s": 2.0,
                        "max_retries": 0,
                        "rpm": 120,
                        "enabled": True,
                        "model": model,
                        "priority": 0,
                        "cooldown_s": 1.0,
                        "price": 0.0,
                    }
                    for name, server, env_key, model in servers
                ]
            }
        ),
        encoding="utf-8",
    )
    return path


def _set_provider_env(monkeypatch: pytest.MonkeyPatch, config_path: Path) -> None:
    monkeypatch.setenv("CAMBIUM_PROVIDERS", str(config_path.resolve()))
    monkeypatch.setenv(PROVIDER_KEY_A, PROVIDER_SECRET)
    monkeypatch.setenv(PROVIDER_KEY_B, PROVIDER_SECRET)
    monkeypatch.setenv("NO_PROXY", "127.0.0.1,localhost")
    monkeypatch.setenv("no_proxy", "127.0.0.1,localhost")


def _provider_task(
    session_dir: Path,
    repo: Path,
    base: str,
    task_id: str,
    *,
    worktree: str,
    branch: str,
) -> dict[str, Any]:
    return {
        "task_id": task_id,
        "task": TASK_TEXT,
        "repo": str(repo),
        "worktree_path": str(session_dir / worktree),
        "branch": branch,
        "worker": WORKER,
        "base_commit": base,
        "fanout_config": {
            "tier": "fast",
            "call_budget_s": 5.0,
            "pause_timeout_s": 0.1,
        },
        "model_candidates": ["m1", "m2"],
        "provider_env_keys": [PROVIDER_KEY_A, PROVIDER_KEY_B, "NO_PROXY", "no_proxy"],
        "ready_timeout_s": 5.0,
        "max_wall_s": 20.0,
        "max_tokens": 5_000_000,
        "max_restarts": 0,
        "heartbeat_interval_s": 0.05,
    }


@pytest.mark.slow
def test_debt_aware_selection_balances_across_tasks_and_feeds_ledger(
    tmp_path, monkeypatch
) -> None:
    """Two providers serve two candidate models; the ledger favours B, so the
    batch pre-assignment pass (H1) assigns every task in the wave to A (the
    lowest-utilization provider) in one pass from the persisted snapshot, and
    the workers' usage still folds into the durable ledger."""
    server_a = FakeServer(
        [(200, _finish_payload("done on A", model="m1", total_tokens=2_000_000), 0.0)]
    )
    server_b = FakeServer(
        [(200, _finish_payload("done on B", model="m2", total_tokens=1_000), 0.0)]
    )
    try:
        config_path = _provider_config_file(
            tmp_path / "providers.json",
            [
                ("provider-a", server_a, PROVIDER_KEY_A, "m1"),
                ("provider-b", server_b, PROVIDER_KEY_B, "m2"),
            ],
        )
        state_path = tmp_path / "routing-state.json"
        # ledger favours B: B already consumed 1M tokens (5% of the default
        # 20M window) while A has none
        state_path.write_text(
            json.dumps(
                {
                    "version": 1,
                    "providers": {
                        "provider-b": {
                            "tokens": 1_000_000,
                            "requests": 10,
                            "failed_requests": 0,
                            "cost": 0.0,
                            "retry_after_count": 0,
                            # fresh timestamp: the P3 time-decay (24h half-life)
                            # must not zero a seed that represents live debt
                            "last_seen": time.time(),
                        }
                    },
                }
            ),
            encoding="utf-8",
        )
        _set_provider_env(monkeypatch, config_path)

        session_dir = tmp_path / "session"
        repo = session_dir / "repo"
        base = _make_repo(repo)
        plan = {
            "tasks": [
                _provider_task(
                    session_dir, repo, base, "t-balance-1",
                    worktree="wt-b1", branch="wt-b1",
                ),
                _provider_task(
                    session_dir, repo, base, "t-balance-2",
                    worktree="wt-b2", branch="wt-b2",
                ),
            ]
        }
        # the batch pass resolves the whole wave against the persisted
        # snapshot in one go (H1): B's seeded 1M tokens make it 5% utilized,
        # so every task in the wave is pre-assigned to A
        result = asyncio.run(
            run_plan(
                session_dir, plan, max_concurrent_tasks=1,
                routing_state_path=state_path,
            )
        )
        events = read_events(session_dir)

        assert result.exit_code == 0
        assert {r.task_id for r in result.results} == {"t-balance-1", "t-balance-2"}
        assert all(r.status == "succeeded" for r in result.results)

        assigned = [
            event["payload"]
            for event in events
            if event["kind"] == "task_assigned" and "assigned_provider" in event["payload"]
        ]
        assert [payload["assigned_provider"] for payload in assigned] == [
            "provider-a", "provider-a",
        ]
        assert {payload["model"] for payload in assigned} == {"m1"}

        # both workers call the assigned provider with the assigned model
        assert len(server_a.calls) == 2
        assert len(server_b.calls) == 0
        assert all(call["model"] == "m1" for call in server_a.calls)

        # usage fed the durable ledger: A folded both tasks' 2M tokens; B kept
        # its seeded 1M untouched
        ledger = DebtStore(state_path)
        ledger.load()
        debts = ledger.as_mapping()
        assert debts["provider-a"].tokens == 4_000_000
        assert debts["provider-a"].requests == 2
        assert debts["provider-b"].tokens == 1_000_000
        assert debts["provider-b"].requests == 10
    finally:
        server_a.close()
        server_b.close()


# --------------------------------------------------------------------------- #
# 5. end-to-end: sticky assigned_provider binding through the real worker
# --------------------------------------------------------------------------- #


class _WorkerRunner:
    """Direct worker spawn for the sticky-binding scenario."""

    def __init__(self, env: dict[str, str]) -> None:
        self.proc: asyncio.subprocess.Process | None = None
        self.stderr_lines: list[str] = []
        self._stderr_task: asyncio.Task | None = None
        self._env = env

    async def start(self) -> None:
        self.proc = await asyncio.create_subprocess_exec(
            sys.executable, "-u", "-m", "cambium.worker",
            stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            env={**os.environ, "PYTHONUNBUFFERED": "1", **self._env},
            start_new_session=True,
        )
        self._stderr_task = asyncio.create_task(self._drain_stderr())

    async def _drain_stderr(self) -> None:
        assert self.proc is not None
        while True:
            raw = await self.proc.stderr.readline()
            if not raw:
                break
            self.stderr_lines.append(raw.decode("utf-8", "replace").rstrip())

    async def send(self, msg: dict[str, Any]) -> None:
        assert self.proc is not None
        self.proc.stdin.write((json.dumps(msg) + "\n").encode("utf-8"))
        await self.proc.stdin.drain()

    async def recv(self, timeout: float = 30.0) -> dict[str, Any] | None:
        assert self.proc is not None
        return await asyncio.wait_for(
            read_message(self.proc.stdout, limit=MAX_LINE_BYTES), timeout
        )

    async def stop(self) -> None:
        if self.proc is not None and self.proc.returncode is None:
            try:
                os.killpg(self.proc.pid, signal.SIGKILL)
            except (ProcessLookupError, PermissionError):
                pass
            await self.proc.wait()
        if self._stderr_task is not None:
            try:
                await asyncio.wait_for(self._stderr_task, 5.0)
            except (TimeoutError, asyncio.CancelledError):
                pass


def _worker_env(config_path: Path, session_dir: Path) -> dict[str, str]:
    env = dict(os.environ)
    env["CAMBIUM_PROVIDERS"] = str(config_path.resolve())
    env[PROVIDER_KEY_A] = PROVIDER_SECRET
    env[PROVIDER_KEY_B] = PROVIDER_SECRET
    env["NO_PROXY"] = "127.0.0.1,localhost"
    env["no_proxy"] = "127.0.0.1,localhost"
    env["PYTHONPATH"] = os.pathsep.join(
        filter(None, [str(ROOT / "src"), os.environ.get("PYTHONPATH")])
    )
    env["CAMBIUM_SESSION_ID"] = str(session_dir.resolve())
    return env


@pytest.mark.slow
def test_sticky_assigned_provider_binding_all_usage_events_on_assigned_provider(
    tmp_path, monkeypatch
) -> None:
    """Worker init carries ``assigned_provider``; the multi-turn task's usage
    events all show the assigned provider (no fallback while healthy), even
    though the seeded primary would pick the other provider."""
    server_a = FakeServer([(200, _ok_payload("a", model="m1"), 0.0)])
    server_b = FakeServer(
        [
            (200, _ok_payload('{"type":"plan","steps":["read"]}', model="m1"), 0.0),
            (
                200,
                _ok_payload(
                    '{"type":"tool_call","name":"read_file",'
                    '"arguments":{"path":"target.txt"}}',
                    model="m1",
                ),
                0.0,
            ),
            (200, _ok_payload('{"type":"finish","summary":"read target.txt"}', model="m1"), 0.0),
        ]
    )
    try:
        config_path = _provider_config_file(
            tmp_path / "providers.json",
            [
                ("provider-a", server_a, PROVIDER_KEY_A, "m1"),
                ("provider-b", server_b, PROVIDER_KEY_B, "m1"),
            ],
        )
        _set_provider_env(monkeypatch, config_path)

        session_dir = tmp_path / "session"
        repo = session_dir / "repo"
        _make_repo(repo)
        worktree = session_dir / "wt"
        subprocess.run(
            ["git", "-C", str(repo), "worktree", "add", "-b", "sticky", str(worktree), "main"],
            check=True,
            capture_output=True,
        )
        write_generation(worktree, 1)

        async def drive() -> tuple[dict[str, Any], list[dict[str, Any]], int]:
            runner = _WorkerRunner(_worker_env(config_path, session_dir))
            await runner.start()
            try:
                await runner.send({
                    "type": "init", "request_id": "init-1", "task_id": "agent-sticky",
                    "generation": 1,
                    "fanout_config": {"tier": "fast", "model": "m1"},
                    "assigned_provider": "provider-b",
                    "heartbeat": {"interval_s": 0.05},
                    "spec": TASK_TEXT,
                })
                ready = await runner.recv()
                assert ready is not None and ready["type"] == "ready", (
                    f"stderr={runner.stderr_lines!r}"
                )
                await runner.send({
                    "type": "run_task", "request_id": "run-1", "task_id": "agent-sticky",
                    "scratch_repo": str(repo), "worktree_path": str(worktree),
                    "branch": "sticky", "generation": 1, "task": TASK_TEXT,
                })
                messages: list[dict[str, Any]] = []
                while True:
                    msg = await runner.recv()
                    if msg is None:
                        raise AssertionError(
                            f"EOF before exit_message; stderr={runner.stderr_lines!r}"
                        )
                    messages.append(msg)
                    if msg["type"] == "exit_message":
                        break
                rc = await runner.proc.wait()
                result = next(m for m in messages if m["type"] == "result_envelope")
                return result, messages, rc
            finally:
                await runner.stop()

        result, messages, rc = asyncio.run(drive())

        assert rc == 0
        assert result["status"] == "succeeded"

        usage_events = [m for m in messages if m["type"] == "usage_event"]
        assert len(usage_events) == 3
        assert all(event["provider"] == "provider-b" for event in usage_events)
        assert all(event["model"] == "m1" for event in usage_events)
        assert len(server_b.calls) == 3
        assert len(server_a.calls) == 0  # no fallback while healthy
    finally:
        server_a.close()
        server_b.close()


# --------------------------------------------------------------------------- #
# 7. review findings P1/P3: env-isolated default path; aged debt decays
# --------------------------------------------------------------------------- #


def test_debt_store_decays_aged_entries_on_load(tmp_path) -> None:
    import time

    path = tmp_path / "routing-state.json"
    store = DebtStore(path)
    store.record({"provider": "p1", "usage": {"total_tokens": 400}})
    store.save()
    # age the entry 48h (two 24h half-lives) and reload: 400 -> 100
    raw = json.loads(path.read_text(encoding="utf-8"))
    raw["providers"]["p1"]["last_seen"] = time.time() - 48 * 3600
    path.write_text(json.dumps(raw), encoding="utf-8")
    loaded = DebtStore(path)
    loaded.load()
    assert loaded.as_mapping()["p1"].tokens == 100


# --------------------------------------------------------------------------- #
# 8. review finding P2: a ledger save failure must not discard the result
# --------------------------------------------------------------------------- #


@pytest.mark.slow
def test_ledger_save_failure_does_not_block_session_result(tmp_path, monkeypatch) -> None:
    """If the routing-state save raises (disk full, permissions), the session
    result must still be written and the failure reported as a log event —
    never propagated past the supervisor's result boundary.

    The failure is induced with a real condition: the ledger path's parent is
    a regular file, so ``save``'s ``mkdir(parents=True)`` raises
    ``NotADirectoryError`` (an OSError) — no stubbing.
    """
    server = FakeServer([(200, _finish_payload("done", model="m1", total_tokens=26), 0.0)])
    try:
        config_path = _provider_config_file(
            tmp_path / "providers.json",
            [("provider-a", server, PROVIDER_KEY_A, "m1")],
        )
        _set_provider_env(monkeypatch, config_path)
        # a regular file where the ledger's parent directory would need to be
        blocker = tmp_path / "blocker"
        blocker.write_text("not a directory", encoding="utf-8")
        blocked_state = blocker / "routing-state.json"

        session_dir = tmp_path / "session"
        repo = session_dir / "repo"
        base = _make_repo(repo)
        plan = {
            "tasks": [
                _provider_task(
                    session_dir, repo, base, "t-save-fail",
                    worktree="wt-sf", branch="wt-sf",
                )
            ]
        }
        result = asyncio.run(
            run_plan(
                session_dir, plan, max_concurrent_tasks=1,
                routing_state_path=blocked_state,
            )
        )
        events = read_events(session_dir)

        assert result.exit_code == 0
        assert result.results[0].status == "succeeded"
        # canonical result artifact is present despite the save failure
        assert (session_dir / ".cambium" / "result.json").exists()
        logged = [
            e["payload"]["message"]
            for e in events
            if e["kind"] == "log" and "routing-state save failed" in e["payload"].get("message", "")
        ]
        assert logged and "routing-state save failed" in logged[0]
    finally:
        server.close()
