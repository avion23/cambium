"""Weighted provider ordering in the Diffundo cascade (measured quality).

The cascade's provider order within a tier used to be static config priority.
Measured durable usage evidence (``.cambium/sessions/*/.cambium/events.db``,
kind ``usage_event``) shows huge provider differences:

- opencode-go: 110 calls, 99.1% cache-hit, latency p50 2.93s
- zai: 49 calls, 98.0% cache-hit, latency p50 4.58s
- codex: 56 calls, 12.5% cache-hit, latency p50 7.21s

``Diffundo`` now accepts a usage-debt snapshot (``ProviderDebt`` counters as
recorded by ``routing.DebtStore``) and refines order WITHIN an equal-priority
run by measured quality (``diffundo._debt_quality_score``): a higher
cache-hit rate and a lower mean latency win. Config priority stays the
primary ordering key; a provider with no fresh debt scores a neutral weight
so it keeps its config-priority position instead of being pinned to the
bottom permanently. Health states are untouched: a cooldown/quarantined
provider is skipped exactly as before.

No mocks, no network: each scenario drives real ``Diffundo.call`` against
fake OpenAI-compatible ``http.server`` backends in background threads,
reusing the loopback FakeServer pattern from tests/scenarios/test_diffundo.py.
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
    _debt_quality_score,
)
from cambium.routing import ProviderDebt

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
        behaviors: list[tuple[int, dict[str, Any], float]],
    ) -> None:
        self.behaviors = list(behaviors)
        self.calls: list[dict[str, Any]] = []
        self._lock = threading.Lock()
        self._httpd = HTTPServer(("127.0.0.1", 0), _Handler)
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
            self.calls.append(body)
            return len(self.calls) - 1

    def behavior_at(self, index: int) -> tuple[int, dict[str, Any], float]:
        behavior = self.behaviors[index] if index < len(self.behaviors) else self.behaviors[-1]
        return behavior

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
        index = server.record(body)
        status, payload, delay = server.behavior_at(index)
        if delay:
            time.sleep(delay)
        encoded = json.dumps(payload).encode("utf-8")
        try:
            self.send_response(status)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(encoded)))
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
# Measured durable-usage fixture (see module docstring)
# --------------------------------------------------------------------------- #


def _measured_debt() -> dict[str, ProviderDebt]:
    """Debt snapshot folded from the measured durable usage events.

    Cache-hit counts round the measured rates (99.1% of 110, 98.0% of 49,
    12.5% of 56); latency totals use the measured p50 as the mean latency.
    ``last_seen`` is fresh so the entries are not stale.
    """
    now = time.time()
    return {
        "opencode-go": ProviderDebt(
            requests=110,
            cache_hit_count=109,
            latency_total_s=2.93 * 110,
            latency_count=110,
            last_seen=now,
        ),
        "zai": ProviderDebt(
            requests=49,
            cache_hit_count=48,
            latency_total_s=4.58 * 49,
            latency_count=49,
            last_seen=now,
        ),
        "codex": ProviderDebt(
            requests=56,
            cache_hit_count=7,
            latency_total_s=7.21 * 56,
            latency_count=56,
            last_seen=now,
        ),
    }


def _three_servers() -> tuple[FakeServer, FakeServer, FakeServer]:
    return (
        FakeServer([(200, _ok_payload("codex answer"), 0.0)]),
        FakeServer([(200, _ok_payload("zai answer"), 0.0)]),
        FakeServer([(200, _ok_payload("opencode-go answer"), 0.0)]),
    )


# --------------------------------------------------------------------------- #
# 1. pure score function
# --------------------------------------------------------------------------- #


def test_debt_quality_score_ranks_measured_data_and_neutral_defaults() -> None:
    now = time.time()
    debt = _measured_debt()
    scores = {name: _debt_quality_score(entry, now=now) for name, entry in debt.items()}
    # codex sorts below opencode-go and zai when measured data exists
    assert scores["opencode-go"] > scores["zai"] > scores["codex"]

    # no data / empty entry -> neutral 0.0 (never a penalty)
    assert _debt_quality_score(None, now=now) == 0.0
    assert _debt_quality_score(ProviderDebt(), now=now) == 0.0

    # stale data (last seen older than a week) -> neutral 0.0
    stale_now = now + 8 * 24 * 3600
    assert _debt_quality_score(debt["codex"], now=stale_now) == 0.0

    # a raw mapping entry works too (not just ProviderDebt)
    mapping = {
        "requests": 10,
        "cache_hit_count": 5,
        "latency_total_s": 10.0,
        "latency_count": 5,
        "last_seen": now,
    }
    assert _debt_quality_score(mapping, now=now) > 0.0


# --------------------------------------------------------------------------- #
# 2. candidate order with measured data vs. config priority
# --------------------------------------------------------------------------- #


def test_measured_debt_reorders_equal_priority_candidates(monkeypatch) -> None:
    codex, zai, opencode_go = _three_servers()
    _set_keys(monkeypatch, "K_CODEX", "K_ZAI", "K_OPG")
    try:
        # config order carries codex FIRST, but all three are equal priority 0
        router = Diffundo(
            (
                _config("codex", codex, "K_CODEX"),
                _config("zai", zai, "K_ZAI"),
                _config("opencode-go", opencode_go, "K_OPG"),
            ),
            debt=_measured_debt(),
        )
        names = [provider.name for provider in router._candidates(ProviderTier.FAST, None)]
        assert names == ["opencode-go", "zai", "codex"]
    finally:
        codex.close()
        zai.close()
        opencode_go.close()


def test_config_priority_order_preserved_without_debt(monkeypatch) -> None:
    first = FakeServer([(200, _ok_payload("first"), 0.0)])
    second = FakeServer([(200, _ok_payload("second"), 0.0)])
    _set_keys(monkeypatch, "K_1", "K_2")
    try:
        for debt in (None, {}):
            router = Diffundo(
                (
                    _config("p_second", second, "K_2", priority=5),
                    _config("p_first", first, "K_1", priority=0),
                ),
                debt=debt,
            )
            names = [provider.name for provider in router._candidates(ProviderTier.FAST, None)]
            assert names == ["p_first", "p_second"]
    finally:
        first.close()
        second.close()


def test_unknown_provider_keeps_priority_position_not_bottom(monkeypatch) -> None:
    # A provider with NO recorded data must not be penalized to the bottom:
    # within an equal-priority run it sits above measured-bad providers, and
    # it is never reordered below a worse-priority provider.
    unknown = FakeServer([(200, _ok_payload("unknown"), 0.0)])
    bad = FakeServer([(200, _ok_payload("bad"), 0.0)])
    later = FakeServer([(200, _ok_payload("later"), 0.0)])
    _set_keys(monkeypatch, "K_UNKNOWN", "K_BAD", "K_LATER")
    now = time.time()
    debt = {
        "bad": ProviderDebt(
            requests=56,
            cache_hit_count=7,
            latency_total_s=7.21 * 56,
            latency_count=56,
            last_seen=now,
        ),
    }
    try:
        router = Diffundo(
            (
                _config("unknown", unknown, "K_UNKNOWN", priority=0),
                _config("bad", bad, "K_BAD", priority=0),
                _config("later", later, "K_LATER", priority=5),
            ),
            debt=debt,
        )
        names = [provider.name for provider in router._candidates(ProviderTier.FAST, None)]
        assert names == ["unknown", "bad", "later"]
    finally:
        unknown.close()
        bad.close()
        later.close()


# --------------------------------------------------------------------------- #
# 3. cascade behavior (real calls against loopback servers)
# --------------------------------------------------------------------------- #


def test_cascade_prefers_best_measured_provider_then_next_best(monkeypatch) -> None:
    # codex sits FIRST in config order (all priority 0); measured quality must
    # move it below opencode-go and zai in the dispatch order.
    codex = FakeServer([(200, _ok_payload("codex answer"), 0.0)])
    zai = FakeServer(
        [(200, _ok_payload("zai answer"), 0.0), (500, _error_payload("zai down"), 0.0)]
    )
    opencode_go = FakeServer(
        [(200, _ok_payload("opencode-go answer"), 0.0), (500, _error_payload("opg down"), 0.0)]
    )
    _set_keys(monkeypatch, "K_CODEX", "K_ZAI", "K_OPG")
    router = Diffundo(
        (
            _config("codex", codex, "K_CODEX"),
            _config("zai", zai, "K_ZAI"),
            _config("opencode-go", opencode_go, "K_OPG"),
        ),
        debt=_measured_debt(),
    )
    try:
        # call 1: opencode-go wins on measured quality despite codex leading
        # config order
        result = asyncio.run(router.call(ProviderTier.FAST, PROMPT))
        assert isinstance(result, CallResult)
        assert result.provider == "opencode-go"
        assert len(opencode_go.calls) == 1
        assert len(codex.calls) == 0

        # call 2: opencode-go is in cooldown; zai serves (codex still last)
        result2 = asyncio.run(router.call(ProviderTier.FAST, PROMPT))
        assert result2.provider == "zai"
        assert len(opencode_go.calls) == 2
        assert len(codex.calls) == 0

        # call 3: opencode-go and zai both in cooldown; codex is the last resort
        result3 = asyncio.run(router.call(ProviderTier.FAST, PROMPT))
        assert result3.provider == "codex"
        assert len(opencode_go.calls) == 2
        assert len(zai.calls) == 2
        assert len(codex.calls) == 1
        assert router.health("opencode-go") is HealthState.COOLDOWN
        assert router.health("zai") is HealthState.COOLDOWN
        assert router.health("codex") is HealthState.HEALTHY
    finally:
        codex.close()
        zai.close()
        opencode_go.close()


def test_cooldown_skips_best_quality_provider(monkeypatch) -> None:
    # The weight only affects ORDER among healthy available providers: a
    # cooldown provider keeps being skipped even with the best measured
    # quality.
    good = FakeServer([(500, _error_payload("good down"), 0.0)])
    bad = FakeServer([(200, _ok_payload("bad answer"), 0.0)])
    _set_keys(monkeypatch, "K_GOOD", "K_BAD")
    now = time.time()
    debt = {
        "p_good": ProviderDebt(
            requests=110,
            cache_hit_count=109,
            latency_total_s=2.93 * 110,
            latency_count=110,
            last_seen=now,
        ),
        "p_bad": ProviderDebt(
            requests=56,
            cache_hit_count=7,
            latency_total_s=7.21 * 56,
            latency_count=56,
            last_seen=now,
        ),
    }
    router = Diffundo(
        (
            _config("p_good", good, "K_GOOD", cooldown_s=60.0),
            _config("p_bad", bad, "K_BAD"),
        ),
        debt=debt,
    )
    try:
        # call 1: p_good wins the order, fails, and goes to COOLDOWN; the
        # cascade falls through to p_bad despite its worse measured quality
        result = asyncio.run(router.call(ProviderTier.FAST, PROMPT))
        assert result.provider == "p_bad"
        assert router.status("p_good") is ProviderStatus.COOLDOWN

        # call 2: p_good is skipped entirely while in cooldown
        result2 = asyncio.run(router.call(ProviderTier.FAST, PROMPT))
        assert result2.provider == "p_bad"
        assert len(good.calls) == 1
        assert len(bad.calls) == 2
    finally:
        good.close()
        bad.close()


def test_primary_association_leads_despite_worse_quality(monkeypatch) -> None:
    # Supervisor-level admission balancing presets a per-task sticky primary;
    # the quality weight refines order among the remaining candidates and must
    # never override the task's associated provider (prompt-prefix caching).
    codex, zai, opencode_go = _three_servers()
    _set_keys(monkeypatch, "K_CODEX", "K_ZAI", "K_OPG")
    router = Diffundo(
        (
            _config("codex", codex, "K_CODEX"),
            _config("zai", zai, "K_ZAI"),
            _config("opencode-go", opencode_go, "K_OPG"),
        ),
        primary_provider="codex",
        debt=_measured_debt(),
    )
    try:
        result = asyncio.run(router.call(ProviderTier.FAST, PROMPT))
        assert result.provider == "codex"
        assert len(codex.calls) == 1
        assert len(opencode_go.calls) == 0
        assert len(zai.calls) == 0
    finally:
        codex.close()
        zai.close()
        opencode_go.close()
