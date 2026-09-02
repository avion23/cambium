"""Fault-injection scenarios for provider 429 storms.

The fake loopback transport drives the real :class:`Diffundo` retry,
classification, health, deadline, and quota-accounting paths.  The scenarios
also keep the prompt byte-identical across retries: a completion request is
safe to replay, while a policy refusal is terminal for that request and does
not quarantine the provider.
"""

from __future__ import annotations

import asyncio
import json
import random
import sqlite3
import threading
import time
from dataclasses import dataclass, field
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any, cast

import pytest

from cambium.diffundo import (
    AllProvidersFailed,
    Diffundo,
    HealthState,
    ProviderConfig,
    ProviderError,
    ProviderOutcome,
    ProviderStatus,
    ProviderTier,
)
from cambium.provider_scheduler import QuotaWindowSpec

PROMPT = {"messages": [{"role": "user", "content": "read-only provider probe"}]}


@dataclass(frozen=True, slots=True)
class _Behavior:
    status: int
    payload: dict[str, Any]
    delay_s: float = 0.0
    headers: dict[str, str] = field(default_factory=dict)


class _StormServer:
    """Scripted OpenAI-compatible transport on a loopback HTTP server."""

    def __init__(self, behaviors: list[_Behavior]) -> None:
        if not behaviors:
            raise ValueError("a storm server needs at least one behavior")
        self._behaviors = tuple(behaviors)
        self.requests: list[dict[str, Any]] = []
        self.statuses: list[int] = []
        self._lock = threading.Lock()
        self._httpd = cast(_FakeHTTPServer, _FakeHTTPServer(("127.0.0.1", 0), _Handler))
        self._httpd.fake = self
        self._thread = threading.Thread(
            target=self._httpd.serve_forever,
            kwargs={"poll_interval": 0.001},
            daemon=True,
        )
        self._thread.start()
        self.base_url = f"http://127.0.0.1:{self._httpd.server_port}"

    def record(self, body: dict[str, Any]) -> int:
        with self._lock:
            self.requests.append(body)
            index = len(self.requests) - 1
            self.statuses.append(self.behavior_at(index).status)
            return index

    def behavior_at(self, index: int) -> _Behavior:
        return self._behaviors[min(index, len(self._behaviors) - 1)]

    def close(self) -> None:
        self._httpd.shutdown()
        self._httpd.server_close()
        self._thread.join()


class _FakeHTTPServer(ThreadingHTTPServer):
    daemon_threads = True
    fake: _StormServer


class _Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.0"

    def do_POST(self) -> None:  # noqa: N802 (http.server API)
        length = int(self.headers.get("Content-Length") or 0)
        raw = self.rfile.read(length) if length else b""
        try:
            body = json.loads(raw.decode("utf-8") or "{}")
        except json.JSONDecodeError:
            body = {}
        server = cast(_FakeHTTPServer, self.server).fake
        index = server.record(body)
        behavior = server.behavior_at(index)
        if behavior.delay_s > 0:
            time.sleep(behavior.delay_s)
        encoded = json.dumps(behavior.payload).encode("utf-8")
        try:
            self.send_response(behavior.status)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(encoded)))
            self.send_header("Connection", "close")
            for name, value in behavior.headers.items():
                self.send_header(name, value)
            self.end_headers()
            self.wfile.write(encoded)
        except OSError:
            # The router's deadline can close the socket while this scripted
            # handler is still sleeping to model a network timeout.
            pass

    def log_message(self, format: str, *args: object) -> None:
        pass


def _ok_payload(content: str) -> dict[str, Any]:
    return {
        "id": "chatcmpl-storm",
        "object": "chat.completion",
        "model": "storm-model",
        "choices": [
            {
                "index": 0,
                "message": {"role": "assistant", "content": content},
                "finish_reason": "stop",
            }
        ],
        "usage": {"prompt_tokens": 5, "completion_tokens": 3, "total_tokens": 8},
    }


def _error_payload(message: str) -> dict[str, Any]:
    return {"error": {"message": message, "type": "storm_error", "code": "storm"}}


def _config(
    name: str,
    server: _StormServer,
    env_name: str,
    *,
    max_retries: int = 0,
    # Leave incidental loopback attempts above sub-second xdist scheduling
    # noise; timeout-focused scenarios override this explicitly below.
    timeout_s: float = 1.0,
    cooldown_s: float = 1.0,
    rpm: int = 600,
    quota_windows: tuple[QuotaWindowSpec, ...] = (),
) -> ProviderConfig:
    return ProviderConfig(
        name=name,
        tier=ProviderTier.FAST,
        base_url=server.base_url,
        api_key_env=env_name,
        api_key=f"sk-storm-{name}",
        timeout_s=timeout_s,
        max_retries=max_retries,
        rpm=rpm,
        enabled=True,
        model="storm-model",
        cooldown_s=cooldown_s,
        quota_windows=quota_windows,
    )


def _quota_state(path: str, provider: str) -> tuple[int, int, int]:
    """Return pending reservations, reconciled reservations, used requests."""
    with sqlite3.connect(path) as connection:
        pending, reconciled = connection.execute(
            "SELECT "
            "COALESCE(SUM(reconciled=0), 0), "
            "COALESCE(SUM(reconciled=1), 0) "
            "FROM quota_reservations WHERE provider=?",
            (provider,),
        ).fetchone()
        used_requests = connection.execute(
            "SELECT COALESCE(used_requests, 0) FROM quota_windows "
            "WHERE provider=? AND name='storm-window'",
            (provider,),
        ).fetchone()
    return int(pending), int(reconciled), int(used_requests[0] if used_requests else 0)


def _quota_window() -> tuple[QuotaWindowSpec, ...]:
    return (QuotaWindowSpec("storm-window", 3600.0, request_allowance=10),)


def test_429_storm_cools_lane_releases_quota_and_replays_idempotently(
    tmp_path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A failed 429 run cools the lane, then a recovery can reserve again.

    The first logical call has exactly ``N`` wire attempts (one initial call
    plus its retry budget).  Its reservation reconciles to zero despite the
    failure, so a later recovery call cannot be blocked by a leaked request
    reservation.  A later transient retry exercises the full-jitter path.
    """
    retry_count = 3
    server = _StormServer(
        [
            *(
                _Behavior(
                    429,
                    _error_payload("rate limit exceeded"),
                    headers={"Retry-After": "1"},
                )
                for _ in range(retry_count + 1)
            ),
            _Behavior(200, _ok_payload("recovered")),
            _Behavior(500, _error_payload("transient failure")),
            _Behavior(200, _ok_payload("jitter recovered")),
        ]
    )
    quota_db = tmp_path / "quota.db"
    monkeypatch.setenv("CAMBIUM_QUOTA_DB", str(quota_db))
    provider = _config(
        "storm-provider",
        server,
        "K_STORM",
        max_retries=retry_count,
        cooldown_s=30.0,
        quota_windows=_quota_window(),
    )
    router = Diffundo(
        (provider,),
        # This scenario checks retry/quota bookkeeping, not wall-budget
        # exhaustion; give all four wire attempts generous headroom.
        call_budget_s=30.0,
        pause_timeout_s=0.0,
        retry_base_delay_s=0.25,
    )
    sleeps: list[float] = []

    async def record_sleep(delay: float) -> None:
        sleeps.append(delay)

    monkeypatch.setattr(asyncio, "sleep", record_sleep)
    try:
        with pytest.raises(AllProvidersFailed) as raised:
            asyncio.run(router.call(ProviderTier.FAST, PROMPT))
        failure = cast(AllProvidersFailed, raised.value)
        error = cast(ProviderError, failure.last_error)

        # The HTTP classifier reports quota pressure and the completed storm
        # transitions the provider lane into its cooldown state.
        assert error.outcome is ProviderOutcome.QUOTA
        assert error.retry_after_s == 1.0
        assert error.request_rate_status == "cooldown"
        assert router.health("storm-provider") is HealthState.COOLDOWN
        assert router.status("storm-provider") is ProviderStatus.COOLDOWN
        assert server.statuses == [429] * (retry_count + 1)
        assert sleeps == [1.0] * retry_count

        # The failed logical attempt reconciled its reservation, rather than
        # leaving a pending reservation for every retry.  The request counter
        # remains one because the provider did receive that HTTP request.
        assert _quota_state(str(quota_db), "storm-provider") == (0, 1, 1)

        # Simulate the provider cooldown elapsing without making this scenario
        # sleep for 30 seconds.  A request allowance of ten is intentionally
        # larger than the storm but the assertion above proves the failed
        # reservation itself was released.
        # Backdate well beyond the cooldown so scheduler jitter cannot make
        # this recovery probe appear unavailable.
        router._runtime("storm-provider").cooldown_until = time.monotonic() - 60.0
        recovered = asyncio.run(router.call(ProviderTier.FAST, PROMPT))
        assert recovered.content == "recovered"
        assert router.health("storm-provider") is HealthState.HEALTHY
        assert _quota_state(str(quota_db), "storm-provider") == (0, 2, 2)

        # No Retry-After is present on this transient error, so the retry uses
        # bounded full jitter: uniform(0, base * 2**attempt_no).
        jitter_bounds: list[tuple[float, float]] = []

        def deterministic_uniform(lower: float, upper: float) -> float:
            jitter_bounds.append((lower, upper))
            return upper * 0.75

        monkeypatch.setattr(random, "uniform", deterministic_uniform)
        jittered = asyncio.run(router.call(ProviderTier.FAST, PROMPT))
        assert jittered.content == "jitter recovered"
        assert jitter_bounds == [(0.0, 0.25)]
        assert sleeps[-1] == pytest.approx(0.1875)
        assert server.statuses == [429] * (retry_count + 1) + [200, 500, 200]
        assert _quota_state(str(quota_db), "storm-provider") == (0, 3, 3)

        # Retries are idempotent completion replays: the transport sees the
        # same request body for every attempt and no prompt mutation occurs.
        assert server.requests
        assert all(request == server.requests[0] for request in server.requests)
    finally:
        server.close()


def test_429_storm_respects_wall_and_retry_budgets(
    monkeypatch: pytest.MonkeyPatch, tmp_path
) -> None:
    """A large provider reset hint cannot turn one bounded turn into a hang."""
    server = _StormServer(
        [_Behavior(429, _error_payload("quota pressure"), headers={"Retry-After": "60"})]
    )
    quota_db = tmp_path / "quota.db"
    monkeypatch.setenv("CAMBIUM_QUOTA_DB", str(quota_db))
    max_retries = 50
    router = Diffundo(
        (
            _config(
                "bounded-storm",
                server,
                "K_BOUNDED_STORM",
                max_retries=max_retries,
                cooldown_s=30.0,
                quota_windows=_quota_window(),
            ),
        ),
        # Generous wall budget: the 60s reset hint stays far beyond it (so
        # Retry-After skips retries), and the margin survives xdist CPU
        # contention where a tight budget expired during the first attempt.
        call_budget_s=5.0,
        pause_timeout_s=0.0,
    )

    async def fail_if_sleep_called(delay: float) -> None:
        raise AssertionError(f"Retry-After beyond the wall budget slept for {delay}s")

    monkeypatch.setattr(asyncio, "sleep", fail_if_sleep_called)
    started = time.monotonic()
    try:
        with pytest.raises(AllProvidersFailed) as raised:
            asyncio.run(router.call(ProviderTier.FAST, PROMPT))
        elapsed = time.monotonic() - started
        error = cast(ProviderError, cast(AllProvidersFailed, raised.value).last_error)

        # The tight budget and one-request assertion prove no Retry-After
        # retry; allow generous worker scheduling jitter in this wall bound.
        assert elapsed < 6.0
        assert len(server.requests) == 1
        assert error.outcome is ProviderOutcome.QUOTA
        assert error.retry_after_s == 60.0
        assert router.health("bounded-storm") is HealthState.COOLDOWN
        assert _quota_state(str(quota_db), "bounded-storm") == (0, 1, 1)
    finally:
        server.close()


def test_mixed_storm_policy_refusal_is_terminal_but_health_neutral(monkeypatch, tmp_path) -> None:
    """429 retries, policy refusal, and timeout keep distinct outcomes."""
    server = _StormServer(
        [
            _Behavior(429, _error_payload("rate limit exceeded"), headers={"Retry-After": "0"}),
            _Behavior(403, _error_payload("content_policy_violation: blocked by policy")),
            _Behavior(429, _error_payload("rate limit exceeded"), headers={"Retry-After": "0"}),
            _Behavior(200, _ok_payload("recovered after policy turn")),
            # The delayed response models a network timeout.  Diffundo
            # deliberately does not re-POST a tarpitted endpoint; the next
            # cascade turn is the retry opportunity.
            _Behavior(200, _ok_payload("late timeout response"), delay_s=1.0),
            _Behavior(429, _error_payload("rate limit exceeded"), headers={"Retry-After": "0"}),
            _Behavior(200, _ok_payload("recovered after timeout")),
        ]
    )
    quota_db = tmp_path / "quota.db"
    monkeypatch.setenv("CAMBIUM_QUOTA_DB", str(quota_db))
    provider = _config(
        "mixed-storm",
        server,
        "K_MIXED_STORM",
        max_retries=2,
        # The scripted 1s response must exceed this timeout; TIMEOUT is
        # intentional here rather than incidental transport starvation.
        timeout_s=0.5,
        cooldown_s=0.0,
        quota_windows=_quota_window(),
    )
    # call_budget_s must stay far above the per-attempt timeouts: under xdist
    # load a tight wall budget fires the pre-attempt deadline check and turns
    # the expected REFUSAL into budget-exhaustion TIMEOUT.  The refusal, not
    # the budget, is the behavior under test.
    router = Diffundo((provider,), call_budget_s=30.0, pause_timeout_s=0.0)
    sleeps: list[float] = []

    async def record_sleep(delay: float) -> None:
        sleeps.append(delay)

    monkeypatch.setattr(asyncio, "sleep", record_sleep)
    try:
        # First call: the 429 is retried, then the 403 policy refusal stops
        # this request.  Refusals are request-level fall-throughs and must not
        # damage provider health.
        with pytest.raises(AllProvidersFailed) as first:
            asyncio.run(router.call(ProviderTier.FAST, PROMPT))
        first_error = cast(ProviderError, cast(AllProvidersFailed, first.value).last_error)
        assert first_error.outcome is ProviderOutcome.REFUSAL
        assert len(server.requests) == 2
        assert sleeps == [0.0]
        assert router.health("mixed-storm") is HealthState.UNKNOWN
        assert _quota_state(str(quota_db), "mixed-storm") == (0, 1, 1)

        # The next 429 is retried on the same provider and recovers.
        recovered = asyncio.run(router.call(ProviderTier.FAST, PROMPT))
        assert recovered.content == "recovered after policy turn"
        assert server.statuses[:4] == [429, 403, 429, 200]
        assert router.health("mixed-storm") is HealthState.HEALTHY

        # A network timeout is typed separately and is not retried against the
        # tarpitted endpoint.  It does cool the lane, unlike a policy refusal.
        with pytest.raises(AllProvidersFailed) as timed_out:
            asyncio.run(router.call(ProviderTier.FAST, PROMPT))
        timeout_error = cast(ProviderError, cast(AllProvidersFailed, timed_out.value).last_error)
        assert timeout_error.outcome is ProviderOutcome.TIMEOUT
        assert len(server.requests) == 5
        assert router.health("mixed-storm") is HealthState.COOLDOWN
        assert _quota_state(str(quota_db), "mixed-storm") == (0, 3, 3)

        # Cooldown zero is used only to avoid a real wait; the next logical
        # turn probes the lane, retries its 429, and recovers.
        recovered_again = asyncio.run(router.call(ProviderTier.FAST, PROMPT))
        assert recovered_again.content == "recovered after timeout"
        assert server.statuses == [429, 403, 429, 200, 200, 429, 200]
        assert router.health("mixed-storm") is HealthState.HEALTHY
        assert _quota_state(str(quota_db), "mixed-storm") == (0, 4, 4)

        # Every retry, including retries around the mixed failures, preserves
        # the same idempotent completion request.
        assert all(request == server.requests[0] for request in server.requests)
    finally:
        server.close()


def test_terminal_dead_lane_does_not_starve_optimizer_style_burst() -> None:
    """A dead incumbent is not retried ahead of a healthy burst lane."""
    dead = _StormServer([_Behavior(503, _error_payload("endpoint unavailable"))])
    healthy = _StormServer([_Behavior(200, _ok_payload("healthy"))])
    dead_provider = _config(
        "burst-dead",
        dead,
        "K_BURST_DEAD",
        cooldown_s=0.0,
        rpm=3,
    )
    healthy_provider = _config("burst-healthy", healthy, "K_BURST_HEALTHY", rpm=60)
    router = Diffundo(
        (dead_provider, healthy_provider),
        primary_provider="burst-dead",
        pause_timeout_s=0.0,
        call_budget_s=2.0,
    )
    try:

        async def run_burst() -> list[str]:
            first = await router.call(ProviderTier.FAST, PROMPT)
            assert first.provider == "burst-healthy"

            # The captured production failure had a dead sticky incumbent. A
            # zero cooldown keeps this probe on the live candidate path while
            # the small dead bucket makes repeated probes observable.
            router._primary_provider = "burst-dead"
            names = [first.provider]
            for _ in range(23):
                result = await router.call(ProviderTier.FAST, PROMPT)
                names.append(result.provider)
            return names

        names = asyncio.run(run_burst())
        assert names == ["burst-healthy"] * 24
        assert len(dead.requests) == 1
        assert len(healthy.requests) == 24
        assert router.status("burst-healthy") is ProviderStatus.AVAILABLE
    finally:
        dead.close()
        healthy.close()
