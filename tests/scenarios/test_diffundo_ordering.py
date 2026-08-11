"""Canaries pinning Diffundo's priority-ascending cascade contract.

Review Claim 5 (docs/research/v2-1-review.md §E) proposed weighted
round-robin/LRU rotation among eligible providers; root REJECTED that change.
The adopted normative contract is priority ascending: within a tier the lower
``ProviderConfig.priority`` is tried first (architecture.md §9.1/§9.2 step 2,
cascade-design.md §1.1), equal-priority providers round-robin their start position per call
(diffundo.py:_candidates, per-instance rotation cursor seeded by the caller —
the worker seeds it from the task id so concurrent subagents interleave),
priority ordering across runs is preserved, and selection stays stateless
otherwise — provider outcomes change eligibility (health / token bucket),
never the rotation.

No mocks, no network: each scenario drives real ``Diffundo.call`` against fake
OpenAI-compatible ``http.server`` backends in background threads, reusing the
loopback FakeServer pattern from tests/scenarios/test_diffundo.py. These
canaries are GREEN on current main.
"""

from __future__ import annotations

import asyncio
import json
import threading
import time
from http.server import BaseHTTPRequestHandler, HTTPServer
from typing import Any

import pytest

from cambium.diffundo import (
    CallResult,
    Diffundo,
    HealthState,
    ProviderConfig,
    ProviderStatus,
    ProviderTier,
)

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
        echo_authorization_in_body: bool = False,
        host: str = "127.0.0.1",
    ) -> None:
        self.behaviors = list(behaviors)
        self.echo_authorization_in_body = echo_authorization_in_body
        self.calls: list[dict[str, Any]] = []
        self.request_headers: list[dict[str, str | None]] = []
        self._lock = threading.Lock()
        self._httpd = HTTPServer((host, 0), _Handler)
        self._httpd.fake = self
        self._thread = threading.Thread(
            target=self._httpd.serve_forever,
            kwargs={"poll_interval": 0.05},
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


def _ok_payload(content: str, *, model: str | None = None) -> dict[str, Any]:
    return {
        "id": "chatcmpl-test",
        "object": "chat.completion",
        "model": model or "m",
        "choices": [
            {
                "index": 0,
                "message": {"role": "assistant", "content": content},
                "finish_reason": "stop",
            }
        ],
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
# 1. distinct priorities -> lower priority serves every call
# --------------------------------------------------------------------------- #


def test_two_healthy_providers_distinct_priorities_try_priority_order(monkeypatch) -> None:
    # p_high sits FIRST in config order but carries the HIGHER priority; the
    # priority sort, not config order, must decide the cascade winner.
    low = FakeServer([(200, _ok_payload("low"), 0.0)])
    high = FakeServer([(200, _ok_payload("high"), 0.0)])
    _set_keys(monkeypatch, "K_LOW", "K_HIGH")
    router = Diffundo(
        (
            _config("p_high", high, "K_HIGH", priority=5),
            _config("p_low", low, "K_LOW", priority=0),
        )
    )
    try:
        for _ in range(10):
            result = asyncio.run(router.call(ProviderTier.FAST, PROMPT))
            assert isinstance(result, CallResult)
            assert result.provider == "p_low"
        assert len(low.calls) == 10
        assert len(high.calls) == 0  # never even dispatched
    finally:
        low.close()
        high.close()


# --------------------------------------------------------------------------- #
# 2. equal priorities -> round-robin within the priority run, priority order
#    across runs preserved
# --------------------------------------------------------------------------- #


def test_equal_priority_providers_round_robin_within_run(monkeypatch) -> None:
    first = FakeServer([(200, _ok_payload("first"), 0.0)])
    second = FakeServer([(200, _ok_payload("second"), 0.0)])
    _set_keys(monkeypatch, "K_FIRST", "K_SECOND")
    router = Diffundo(
        (
            _config("p_first", first, "K_FIRST", priority=0),
            _config("p_second", second, "K_SECOND", priority=0),
        )
    )
    try:
        # equal-priority providers rotate their start position per call, so
        # requests interleave across them (per-subagent token throughput).
        served = [asyncio.run(router.call(ProviderTier.FAST, PROMPT)).provider
                  for _ in range(6)]
        assert served == ["p_first", "p_second"] * 3
        assert len(first.calls) == 3
        assert len(second.calls) == 3
    finally:
        first.close()
        second.close()


def test_round_robin_seed_shifts_start_and_priority_groups_keep_order(monkeypatch) -> None:
    first = FakeServer([(200, _ok_payload("first"), 0.0)])
    second = FakeServer([(200, _ok_payload("second"), 0.0)])
    low = FakeServer([(200, _ok_payload("low"), 0.0)])
    _set_keys(monkeypatch, "K_FIRST", "K_SECOND", "K_LOW")
    seeded = Diffundo(
        (
            _config("p_first", first, "K_FIRST", priority=0),
            _config("p_second", second, "K_SECOND", priority=0),
            _config("p_low", low, "K_LOW", priority=5),
        ),
        rotation_seed=1,
    )
    unseeded = Diffundo(
        (
            _config("p_first", first, "K_FIRST", priority=0),
            _config("p_second", second, "K_SECOND", priority=0),
            _config("p_low", low, "K_LOW", priority=5),
        ),
    )
    try:
        # seed 1 starts the equal-priority run at the second provider.
        assert asyncio.run(seeded.call(ProviderTier.FAST, PROMPT)).provider == "p_second"
        assert asyncio.run(seeded.call(ProviderTier.FAST, PROMPT)).provider == "p_first"
        # priority order across runs is preserved: p_low never precedes p_first.
        assert asyncio.run(unseeded.call(ProviderTier.FAST, PROMPT)).provider == "p_first"
        assert len(low.calls) == 0
    finally:
        first.close()
        second.close()
        low.close()


# --------------------------------------------------------------------------- #
# 4. rate-limited priority provider falls through to the next
# --------------------------------------------------------------------------- #


def test_rate_limited_priority_provider_falls_through_to_next(monkeypatch) -> None:
    first = FakeServer([(200, _ok_payload("first"), 0.0)])
    second = FakeServer([(200, _ok_payload("second"), 0.0)])
    _set_keys(monkeypatch, "K_1", "K_2")
    router = Diffundo(
        (
            _config("p_first", first, "K_1", rpm=1, priority=0),
            _config("p_second", second, "K_2", priority=5),
        )
    )
    try:
        # priority 0 is healthy and wins the first call by priority
        result = asyncio.run(router.call(ProviderTier.FAST, PROMPT))
        assert result.provider == "p_first"
        assert len(first.calls) == 1 and len(second.calls) == 0
        assert router.status("p_first") is ProviderStatus.RATE_LIMITED

        # priority 0's bucket is empty -> skipped; priority 1 serves
        result2 = asyncio.run(router.call(ProviderTier.FAST, PROMPT))
        assert result2.provider == "p_second"
        assert len(first.calls) == 1  # not re-dispatched
        assert len(second.calls) == 1
    finally:
        first.close()
        second.close()


# --------------------------------------------------------------------------- #
# 5. provider outcome changes eligibility, never selection order
# --------------------------------------------------------------------------- #


def test_provider_outcome_does_not_change_selection_order(monkeypatch) -> None:
    flaky = FakeServer([(500, _error_payload("boom"), 0.0)])
    good = FakeServer([(200, _ok_payload("good"), 0.0)])
    _set_keys(monkeypatch, "K_FLAKY", "K_GOOD")
    router = Diffundo(
        (
            _config("p_flaky", flaky, "K_FLAKY", priority=0),
            _config("p_good", good, "K_GOOD", priority=5),
        )
    )
    try:
        # p_flaky is tried first (priority 0) exactly once; a retryable 500
        # drives it to COOLDOWN (cascade-design §2.4) and the cascade falls
        # through to p_good — no infinite retry on the failing provider
        result = asyncio.run(router.call(ProviderTier.FAST, PROMPT))
        assert result.provider == "p_good"
        assert len(flaky.calls) == 1
        assert len(good.calls) == 1
        assert router.health("p_flaky") is HealthState.COOLDOWN
        assert router.status("p_flaky") is ProviderStatus.COOLDOWN

        # the failing provider is skipped, not re-ordered: the remaining
        # candidates still serve in priority-ascending order
        result2 = asyncio.run(router.call(ProviderTier.FAST, PROMPT))
        assert result2.provider == "p_good"
        assert len(flaky.calls) == 1  # never re-dispatched
        assert len(good.calls) == 2
    finally:
        flaky.close()
        good.close()
