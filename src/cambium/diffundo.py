"""Diffundo — stateless, tiered, multi-provider LLM router.

Implements the provider-cascade contract of docs/architecture/architecture.md §9
as normatively extended by docs/research/cascade-design.md (fall-through classes
§1.2, race semantics §1.3, health state machine §2.4) and
docs/research/design-deltas.md:

- **D1 — no local cache.** ``Diffundo`` is a stateless router; the only state is
  per-provider cooldown timers, circuit-breaker health, and token buckets
  (architecture §8.1, §9.2). There is no response store anywhere in the module.
- **D8c — prompt prefix layout.** ``validate_prompt_structure`` lints the prompt
  head (first 3 lines of the leading message) for volatile tokens — timestamps
  and ``request_id`` — that would churn a provider's exact-prefix cache key.
- **D8f — token bucket + pause-on-exhaustion.** Each provider carries a token
  bucket refilled at ``rpm`` tokens/minute; an empty bucket marks the provider
  ``RATE_LIMITED`` and the cascade skips it. When every provider in a tier is
  unavailable, ``call`` pauses the dispatch on an ``asyncio.Event`` and a
  per-tier recovery monitor wakes it when any provider's bucket/cooldown/breaker
  recovers.

Failure semantics follow cascade-design §2.4 exactly: UNKNOWN -> HEALTHY on
first success; UNKNOWN/HEALTHY -> COOLDOWN on retryable failure; COOLDOWN
(probe) -> OPEN on probe failure; OPEN -> HALF_OPEN after the open interval;
HALF_OPEN -> HEALTHY on probe success / OPEN on probe failure; any state ->
DISABLED on a non-retryable auth/config error, first call included. Refusals
are request-level fall-throughs that never drive health transitions. The
**probe path is the primary OPEN trip** (a failed probe after a cooldown or on
a half-open probe); the sliding-window failure-rate escalation is a secondary
safety net that only fires once the window is full.

Every ``call``/``call_race`` is bounded by a wall-clock deadline
(``call_budget_s`` / ``race_timeout_s``): the per-attempt HTTP timeout is capped
at the remaining budget, retries are skipped when the backoff no longer fits,
and the cascade aborts (``AllProvidersFailed``) once the budget is spent — the
deadline is not just a candidate-waiting bound.

Stdlib only. HTTP calls use urllib against an OpenAI-compatible
``/chat/completions`` endpoint; the API key is read from the environment (name
from ``ProviderConfig.api_key_env``) at call time. All blocking I/O runs off
the event loop via ``asyncio.to_thread``.
"""

from __future__ import annotations

import asyncio
import json
import os
import random
import re
import time
import urllib.error
import urllib.request
from collections import deque
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from enum import Enum
from typing import Any
from urllib.parse import urlparse

from . import __version__
from .provider_config import is_loopback_host

_TIMESTAMP_RE = re.compile(
    r"\d{4}-\d{2}-\d{2}[T ]\d{2}:\d{2}(?::\d{2}(?:\.\d+)?)?(?:Z|[+-]\d{2}:?\d{2})?"
)
_VOLATILE_MARKERS = ("request_id", "request-id")
_REFUSAL_MARKERS = re.compile(r"content.?filter|refus|moderat|safety", re.IGNORECASE)
_URL_CREDENTIALS_RE = re.compile(
    r"(?P<scheme>\b[a-z][a-z0-9+.-]*://)[^/\s?#@]*@",
    re.IGNORECASE,
)
_REDACTED = "[REDACTED]"
_CLOUDFLARE_1010_RE = re.compile(
    r"(?=.*\b1010\b)(?=.*(?:cloudflare|cf[- ]error|"
    r"error\s*(?:code\s*)?[:#-]?\s*1010|browser(?:['’]s)?\s+signature))",
    re.IGNORECASE | re.DOTALL,
)
USER_AGENT = f"cambium/{__version__}"
# Light content scan for model refusals returned as a 200 completion (issue 4).
# Documented heuristic: exact refusal phrases in the completion text are treated
# as a REFUSAL fall-through so a refusing model never wins the cascade.
_CONTENT_REFUSAL_RE = re.compile(
    r"\b(?:i can'?t|cannot|can'?t)\s+(?:assist|help|comply|complete|answer)\b"
    r"|\b(?:refus(?:e|es|ed|ing|al)|sorry)\b",
    re.IGNORECASE,
)


class ProviderTier(Enum):
    """Capability tier; the cascade's primary routing key (arch §9.2)."""

    FAST = "fast"
    BALANCED = "balanced"
    STRONG = "strong"
    REASONING = "reasoning"


class HealthState(Enum):
    """Per-provider circuit-breaker health (cascade-design §2.4)."""

    UNKNOWN = "unknown"
    HEALTHY = "healthy"
    COOLDOWN = "cooldown"
    OPEN = "open"
    HALF_OPEN = "half_open"
    DISABLED = "disabled"


class ProviderStatus(Enum):
    """Current selection-filter status of a provider (for observability)."""

    UNKNOWN = "unknown"
    AVAILABLE = "available"
    RATE_LIMITED = "rate_limited"
    COOLDOWN = "cooldown"
    OPEN = "open"
    HALF_OPEN = "half_open"
    DISABLED = "disabled"


class ProviderOutcome(Enum):
    """Outcome classes that decide fall-through and health transitions (§1.2)."""

    TIMEOUT = "timeout"
    ERROR = "error"
    QUOTA = "quota"
    REFUSAL = "refusal"
    AUTH_ERROR = "auth_error"


@dataclass(frozen=True, slots=True)
class ProviderConfig:
    """Static provider description. ``api_key_env`` is an env-var NAME, never a
    key value; the value is resolved from the environment at call time (D7)."""

    name: str
    tier: ProviderTier
    base_url: str
    api_key_env: str
    timeout_s: float = 30.0
    max_retries: int = 2
    rpm: int = 60
    enabled: bool = True
    model: str = ""
    priority: int = 0
    cooldown_s: float = 60.0
    price_per_1m_in: float = 0.0
    price_per_1m_out: float = 0.0


@dataclass(frozen=True, slots=True)
class CallResult:
    """A successful completion plus provenance.

    ``tool_calls`` carries the raw OpenAI-shaped tool-call dicts when the
    provider returned them; a text completion leaves it ``None``.
    """

    provider: str
    model: str
    tier: ProviderTier
    content: str
    latency_s: float
    usage: dict[str, Any] | None = None
    estimated_cost_usd: float = 0.0
    tool_calls: tuple[dict[str, Any], ...] | None = None


class DiffundoError(Exception):
    """Base class for Diffundo failures."""


class AllProvidersFailed(DiffundoError):
    """Every tier candidate failed (arch §9.2, cascade-design §5.1)."""

    def __init__(
        self,
        providers_tried: Sequence[str],
        last_error: BaseException | None,
    ) -> None:
        super().__init__(
            f"all providers failed: tried {list(providers_tried)}; last error: {last_error!r}"
        )
        self.providers_tried = tuple(providers_tried)
        self.last_error = last_error


class ProviderError(DiffundoError):
    """One provider attempt failed; the outcome class drives fall-through."""

    def __init__(
        self,
        provider: str,
        outcome: ProviderOutcome,
        message: str = "",
        cause: BaseException | None = None,
        *,
        budget_exhausted: bool = False,
    ) -> None:
        super().__init__(f"provider {provider!r} {outcome.value}: {message}".rstrip())
        self.provider = provider
        self.outcome = outcome
        self.message = message
        self.cause = cause
        self.budget_exhausted = budget_exhausted


class CostBudgetExceeded(DiffundoError):
    """The winning response exceeded the per-call ``budget_usd``."""

    def __init__(self, provider: str, cost_usd: float, budget_usd: float) -> None:
        super().__init__(
            f"provider {provider!r} cost {cost_usd:.6f} USD exceeds budget {budget_usd} USD"
        )
        self.provider = provider
        self.cost_usd = cost_usd
        self.budget_usd = budget_usd


class PromptStructureError(ValueError):
    """A volatile token (timestamp / request_id) sits in the static prompt head."""


# --------------------------------------------------------------------------- #
# Prompt-structure lint (D8c)
# --------------------------------------------------------------------------- #


def _prompt_head(prompt: dict[str, Any]) -> str:
    """Leading static text of an OpenAI-compatible prompt dict."""
    messages = prompt.get("messages")
    if isinstance(messages, list) and messages:
        first = messages[0]
        if isinstance(first, dict):
            content = first.get("content")
            if isinstance(content, str):
                return content
    if isinstance(prompt.get("prompt"), str):
        return prompt["prompt"]
    return ""


def validate_prompt_structure(prompt: dict[str, Any]) -> None:
    """Raise when volatile tokens appear in the first 3 lines (D8c).

    Provider-side prefix caches are exact-prefix content-addressed; timestamps
    and ``request_id`` values at the top churn the prefix key. Static,
    byte-stable content must sit at the top and dynamic content at the bottom,
    so only the first 3 lines of the leading message are linted.
    """
    head = _prompt_head(prompt)
    for idx, line in enumerate(head.splitlines()[:3], start=1):
        if _TIMESTAMP_RE.search(line):
            raise PromptStructureError(
                f"line {idx}: volatile timestamp token in the static prefix; "
                "timestamps belong below the first 3 lines (D8c)"
            )
        lowered = line.lower()
        for marker in _VOLATILE_MARKERS:
            if marker in lowered:
                raise PromptStructureError(
                    f"line {idx}: volatile {marker!r} token in the static prefix; "
                    "request ids belong below the first 3 lines (D8c)"
                )


# --------------------------------------------------------------------------- #
# Per-provider runtime state
# --------------------------------------------------------------------------- #


class _TokenBucket:
    """Leaky token bucket refilled at ``rpm`` tokens per minute (D8f)."""

    __slots__ = ("capacity", "_tokens_per_s", "_tokens", "_last_refill")

    def __init__(self, rpm: int) -> None:
        self.capacity = max(int(rpm), 1)
        self._tokens_per_s = max(int(rpm), 1) / 60.0
        self._tokens = float(self.capacity)
        self._last_refill = time.monotonic()

    def _refill(self) -> None:
        now = time.monotonic()
        elapsed = now - self._last_refill
        if elapsed > 0:
            self._tokens = min(self.capacity, self._tokens + elapsed * self._tokens_per_s)
            self._last_refill = now

    def has_token(self) -> bool:
        self._refill()
        return self._tokens >= 1.0

    def try_take(self) -> bool:
        self._refill()
        if self._tokens >= 1.0:
            self._tokens -= 1.0
            return True
        return False


class _ProviderRuntime:
    """Mutable per-provider state: health, timers, bucket, probe guard."""

    __slots__ = (
        "provider",
        "health",
        "cooldown_until",
        "open_until",
        "bucket",
        "lock",
        "outcomes",
        "probe_in_flight",
    )

    def __init__(self, provider: ProviderConfig, window_size: int) -> None:
        self.provider = provider
        self.health = HealthState.UNKNOWN
        self.cooldown_until = 0.0
        self.open_until = 0.0
        self.bucket = _TokenBucket(provider.rpm)
        self.lock = asyncio.Lock()
        self.outcomes: deque[bool] = deque(maxlen=window_size)
        self.probe_in_flight = False


class _PauseTracker:
    """Per-tier pause state: the dispatch-pause Event plus its waiter count."""

    __slots__ = ("event", "waiters", "monitor")

    def __init__(self) -> None:
        self.event = asyncio.Event()
        self.waiters = 0
        self.monitor: asyncio.Task | None = None


class _RawResponse:
    """Parsed /chat/completions response before it becomes a CallResult."""

    __slots__ = ("payload", "latency_s")

    def __init__(self, payload: dict[str, Any], latency_s: float) -> None:
        self.payload = payload
        self.latency_s = latency_s

    def to_result(self, provider: ProviderConfig) -> CallResult:
        try:
            choices = self.payload["choices"]
            message = choices[0]["message"]
        except (KeyError, IndexError, TypeError) as exc:
            raise ProviderError(
                provider.name, ProviderOutcome.ERROR, "malformed response: no choices"
            ) from exc
        if message.get("refusal"):
            raise ProviderError(
                provider.name, ProviderOutcome.REFUSAL, "model refusal marker in response"
            )
        content = message.get("content")
        raw_tool_calls = message.get("tool_calls")
        tool_calls: tuple[dict[str, Any], ...] | None = None
        if isinstance(raw_tool_calls, list) and raw_tool_calls:
            for tool_call in raw_tool_calls:
                if _tool_call_name(tool_call) is None:
                    raise ProviderError(
                        provider.name,
                        ProviderOutcome.ERROR,
                        "malformed response: tool call without a function name",
                    )
                try:
                    _tool_call_arguments(tool_call)
                except ValueError as exc:
                    raise ProviderError(
                        provider.name,
                        ProviderOutcome.ERROR,
                        f"malformed response: {exc}",
                    ) from exc
            tool_calls = tuple(raw_tool_calls)
        if not isinstance(content, str):
            if tool_calls is None:
                raise ProviderError(
                    provider.name, ProviderOutcome.ERROR, "malformed response: content missing"
                )
            content = ""
        if _CONTENT_REFUSAL_RE.search(content):
            # A 200 completion whose text is a model refusal: fall through to the
            # next provider (documented heuristic, see module docstring). Like any
            # refusal it never drives a health transition.
            raise ProviderError(
                provider.name,
                ProviderOutcome.REFUSAL,
                f"completion content carries refusal markers: {content[:80]!r}",
            )
        usage = self.payload.get("usage")
        if not isinstance(usage, dict):
            usage = None
        return CallResult(
            provider=provider.name,
            model=self.payload.get("model") or provider.model,
            tier=provider.tier,
            content=content,
            latency_s=self.latency_s,
            usage=usage,
            estimated_cost_usd=_estimate_cost(provider, usage),
            tool_calls=tool_calls,
        )


def _estimate_cost(provider: ProviderConfig, usage: dict[str, Any] | None) -> float:
    if not usage:
        return 0.0
    prompt_tokens = float(usage.get("prompt_tokens") or 0)
    completion_tokens = float(usage.get("completion_tokens") or 0)
    return (
        prompt_tokens / 1_000_000 * provider.price_per_1m_in
        + completion_tokens / 1_000_000 * provider.price_per_1m_out
    )


def _tool_call_name(tool_call: Any) -> str | None:
    """Return the function name of an OpenAI-shaped tool call, else None."""
    if not isinstance(tool_call, dict):
        return None
    function = tool_call.get("function")
    if isinstance(function, dict) and type(function.get("name")) is str:
        name = function["name"]
    elif type(tool_call.get("name")) is str:
        name = tool_call["name"]
    else:
        return None
    return name if name.strip() else None


def _tool_call_arguments(tool_call: Any) -> dict[str, Any] | None:
    """Return parsed tool-call arguments, or None when empty or missing.

    Only genuinely empty/missing arguments map to ``{}`` at the DSPy boundary;
    present-but-malformed arguments raise ``ValueError`` so the caller rejects
    the tool call fail-closed instead of silently dropping them.
    """
    if not isinstance(tool_call, Mapping):
        raise ValueError("tool call must be an object")
    function = tool_call.get("function")
    function = function if isinstance(function, Mapping) else {}
    raw = function.get("arguments", tool_call.get("arguments", ""))
    if raw is None or (isinstance(raw, str) and not raw.strip()):
        return None
    if isinstance(raw, str):
        try:
            parsed = json.loads(raw)
        except json.JSONDecodeError as exc:
            raise ValueError(
                f"tool call arguments are not valid JSON: {raw[:80]!r}"
            ) from exc
    elif isinstance(raw, Mapping):
        parsed = dict(raw)
    else:
        raise ValueError(
            f"tool call arguments must be a JSON object, got {type(raw).__name__}"
        )
    if not isinstance(parsed, dict):
        raise ValueError(
            f"tool call arguments must be a JSON object, got {type(parsed).__name__}"
        )
    return parsed


def _default_quality_gate(result: CallResult) -> bool:
    return bool((result.content or "").strip() or result.tool_calls)


def _default_score(result: CallResult) -> float:
    return float(len(result.content or "") + len(result.tool_calls or ()))


def _redact_error_text(message: str, api_key: str) -> str:
    """Remove credentials while retaining safe provider diagnostics."""
    redacted = message.replace(api_key, _REDACTED)
    return _URL_CREDENTIALS_RE.sub(r"\g<scheme>" + _REDACTED + "@", redacted)


class _SanitizedHTTPError(Exception):
    """HTTP failure cause without a request URL or response body."""

    def __init__(self, status: int, reason: str) -> None:
        super().__init__(f"HTTP Error {status}: {reason}")
        self.status = status


class _NoRedirectHandler(urllib.request.HTTPRedirectHandler):
    """Fail-closed: a provider completion endpoint must never redirect.

    urllib would otherwise replay the original request headers — including the
    Authorization Bearer — against the redirect target, bypassing the
    loopback/https transport guard. ``redirect_request`` raises an ``HTTPError``
    carrying the 3xx status so the caller classifies it as a ``ProviderError``
    and no follow-up request is ever made.
    """

    def redirect_request(
        self,
        req: urllib.request.Request,
        fp: Any,
        code: int,
        msg: str,
        headers: Any,
        newurl: str,
    ) -> urllib.request.Request:
        raise urllib.error.HTTPError(
            req.full_url,
            code,
            "provider completion endpoints must not redirect",
            headers,
            fp,
        )


# --------------------------------------------------------------------------- #
# Diffundo router
# --------------------------------------------------------------------------- #


class Diffundo:
    """Stateless tiered provider router (no local cache — D1).

    Per-instance state is limited to per-provider cooldown timers, circuit
    breaker health, token buckets, and per-tier pause events (architecture
    §8.1/§9, D8f). No attribute is a mutable mapping; there is no response
    store anywhere.
    """

    def __init__(
        self,
        providers: Sequence[ProviderConfig],
        *,
        call_budget_s: float = 60.0,
        pause_timeout_s: float = 0.5,
        breaker_window_size: int = 20,
        breaker_failure_threshold: float = 0.5,
        open_backoff_base: float = 2.0,
        retry_base_delay_s: float = 0.05,
    ) -> None:
        self._providers = tuple(providers)
        self._runtimes = tuple(
            _ProviderRuntime(provider, breaker_window_size) for provider in self._providers
        )
        self._pauses = tuple(_PauseTracker() for _ in ProviderTier)
        self._call_budget_s = call_budget_s
        self._pause_timeout_s = pause_timeout_s
        self._breaker_window = breaker_window_size
        self._breaker_threshold = breaker_failure_threshold
        self._open_backoff_base = open_backoff_base
        self._retry_base_delay_s = retry_base_delay_s

    # -- public API --------------------------------------------------------- #

    async def call(
        self,
        tier: ProviderTier,
        prompt: dict[str, Any],
        *,
        model: str | None = None,
        budget_usd: float | None = None,
    ) -> CallResult:
        """Ordered cascade over tier-matching providers (arch §9.2).

        Falls through on timeout/error/quota/refusal; providers in cooldown,
        OPEN, DISABLED, or rate-limited are skipped by the selection filter.
        When every tier provider is unavailable the dispatch pauses on an
        ``asyncio.Event`` and a recovery monitor wakes it (D8f); if nothing
        recovers within the bounded pause window, raises ``AllProvidersFailed``.
        """
        validate_prompt_structure(prompt)
        deadline = time.monotonic() + self._call_budget_s
        tried: list[str] = []
        last_error: BaseException | None = None
        while True:
            candidates = await self._await_candidates(tier, model, deadline)
            if not candidates:
                raise AllProvidersFailed(tried, last_error)
            for provider in candidates:
                try:
                    result = await self._attempt(provider, prompt, deadline=deadline)
                except ProviderError as exc:
                    tried.append(provider.name)
                    last_error = exc
                    if exc.budget_exhausted:
                        raise AllProvidersFailed(tried, last_error) from exc
                    continue
                if budget_usd is not None and result.estimated_cost_usd > budget_usd:
                    raise CostBudgetExceeded(
                        result.provider, result.estimated_cost_usd, budget_usd
                    )
                return result
            raise AllProvidersFailed(tried, last_error)

    async def call_race(
        self,
        tier: ProviderTier,
        prompt: dict[str, Any],
        *,
        model: str | None = None,
        budget_usd: float | None = None,
        n: int = 2,
        race_timeout_s: float = 30.0,
        quality_gate: Callable[[CallResult], bool] | None = None,
        score: Callable[[CallResult], float] | None = None,
    ) -> CallResult:
        """Opt-in race over the first ``n`` tier candidates (cascade-design §1.3).

        First-completed wins only if ``quality_gate`` passes; otherwise the race
        keeps waiting and, if nothing passes by the deadline, returns the
        best-by-``score`` completed result — a superior result is never
        discarded. Exceptions are read as values, so a crashed provider can
        neither win nor kill the race (LLM-M6 hygiene).
        """
        validate_prompt_structure(prompt)
        gate = quality_gate or _default_quality_gate
        scorer = score or _default_score
        deadline = time.monotonic() + race_timeout_s
        candidates = await self._await_candidates(tier, model, deadline)
        if not candidates:
            raise AllProvidersFailed([], None)
        selected = candidates[: max(int(n), 1)]
        tasks: dict[asyncio.Task[Any], str] = {
            asyncio.create_task(self._attempt(provider, prompt, deadline=deadline)): provider.name
            for provider in selected
        }
        results: dict[str, BaseException | CallResult] = {}
        best: tuple[str, CallResult] | None = None
        gated_winner: tuple[str, CallResult] | None = None
        budget_exhausted = False
        last_error: BaseException | None = None
        try:
            while tasks:
                now = time.monotonic()
                if now >= deadline:
                    break
                done, pending = await asyncio.wait(
                    list(tasks),
                    timeout=deadline - now,
                    return_when=asyncio.FIRST_COMPLETED,
                )
                for task in done:
                    provider_name = tasks[task]
                    try:
                        value: BaseException | CallResult = task.result()
                    except asyncio.CancelledError:
                        continue
                    except Exception as exc:
                        value = exc
                    results[provider_name] = value
                    last_error = value
                    if isinstance(value, Exception):
                        if getattr(value, "budget_exhausted", False):
                            budget_exhausted = True
                            break
                        continue
                    if gate(value):
                        gated_winner = (provider_name, value)
                        break
                    if best is None or scorer(value) > scorer(best[1]):
                        best = (provider_name, value)
                if gated_winner is not None or budget_exhausted:
                    break
                tasks = {t: p for t, p in tasks.items() if t in pending}
        finally:
            for task in tasks:
                if not task.done():
                    task.cancel()
        if budget_exhausted:
            raise AllProvidersFailed(list(results), last_error)
        if gated_winner is not None:
            result = gated_winner[1]
        elif best is not None:
            result = best[1]
        else:
            raise AllProvidersFailed(list(results), last_error)
        if budget_usd is not None and result.estimated_cost_usd > budget_usd:
            raise CostBudgetExceeded(result.provider, result.estimated_cost_usd, budget_usd)
        return result

    def health(self, name: str) -> HealthState:
        """Current circuit-breaker health state for a provider."""
        return self._runtime(name).health

    def status(self, name: str) -> ProviderStatus:
        """Current selection-filter status for a provider."""
        runtime = self._runtime(name)
        provider = runtime.provider
        if not provider.enabled or runtime.health is HealthState.DISABLED:
            return ProviderStatus.DISABLED
        if runtime.health is HealthState.OPEN:
            if runtime.open_until <= time.monotonic():
                return ProviderStatus.HALF_OPEN
            return ProviderStatus.OPEN
        if runtime.health is HealthState.COOLDOWN:
            if runtime.cooldown_until <= time.monotonic():
                return ProviderStatus.AVAILABLE
            return ProviderStatus.COOLDOWN
        if runtime.health is HealthState.HALF_OPEN:
            return ProviderStatus.HALF_OPEN
        if runtime.health is HealthState.UNKNOWN:
            if runtime.bucket.has_token():
                return ProviderStatus.AVAILABLE
            return ProviderStatus.RATE_LIMITED
        if not runtime.bucket.has_token():
            return ProviderStatus.RATE_LIMITED
        return ProviderStatus.AVAILABLE

    # -- candidate selection ------------------------------------------------- #

    def _candidates(self, tier: ProviderTier, model: str | None) -> list[ProviderConfig]:
        """Tier-matching, capability-filtered, health/bucket-eligible providers,
        sorted by priority ascending (arch §9.1/§9.2 step 1)."""
        now = time.monotonic()
        out: list[ProviderConfig] = []
        for runtime in self._runtimes:
            provider = runtime.provider
            if provider.tier is not tier or not provider.enabled:
                continue
            if model is not None and provider.model != model:
                continue
            if runtime.health is HealthState.DISABLED:
                continue
            if runtime.health is HealthState.OPEN:
                if runtime.open_until > now:
                    continue
                runtime.health = HealthState.HALF_OPEN
            if runtime.health is HealthState.COOLDOWN and runtime.cooldown_until > now:
                continue
            probing = runtime.health in (HealthState.HALF_OPEN, HealthState.COOLDOWN)
            if probing and runtime.probe_in_flight:
                continue
            if not runtime.bucket.has_token():
                continue
            out.append(provider)
        out.sort(key=lambda provider: provider.priority)
        return out

    async def _await_candidates(
        self,
        tier: ProviderTier,
        model: str | None,
        deadline: float,
    ) -> list[ProviderConfig]:
        """Return candidates, pausing on exhaustion (D8f) until a provider
        recovers or the pause window / deadline is spent."""
        paused_total = 0.0
        while True:
            now = time.monotonic()
            if now >= deadline:
                return []
            candidates = self._candidates(tier, model)
            if candidates:
                return candidates
            if paused_total >= self._pause_timeout_s:
                return []
            max_wait = min(self._pause_timeout_s - paused_total, deadline - now)
            paused_total += await self._pause_for(tier, max_wait)

    def _is_available(self, name: str) -> bool:
        """True when a provider could serve a dispatch right now."""
        runtime = self._runtime(name)
        provider = runtime.provider
        now = time.monotonic()
        if not provider.enabled or runtime.health is HealthState.DISABLED:
            return False
        if runtime.health is HealthState.OPEN and runtime.open_until > now:
            return False
        if runtime.health is HealthState.COOLDOWN and runtime.cooldown_until > now:
            return False
        if runtime.health in (HealthState.HALF_OPEN, HealthState.COOLDOWN):
            if runtime.probe_in_flight:
                return False
        return runtime.bucket.has_token()

    # -- pause / recovery monitor (D8f) -------------------------------------- #

    @staticmethod
    def _tier_index(tier: ProviderTier) -> int:
        return list(ProviderTier).index(tier)

    async def _pause_for(self, tier: ProviderTier, max_wait: float) -> float:
        """Arm the recovery monitor and block up to ``max_wait`` for a provider
        to recover. Returns the time actually spent paused.

        Waiters block on the tracker's ``asyncio.Event``; the event is set ONLY
        by the recovery monitor (a provider recovered). A stale wake signal from
        an earlier recovery is consumed by ``clear()`` before each wait, so a
        waiter that wakes to an already-consumed recovery re-blocks instead of
        busy-spinning.
        """
        start = time.monotonic()
        tracker = self._pauses[self._tier_index(tier)]
        tracker.waiters += 1
        try:
            if tracker.monitor is None or tracker.monitor.done():
                tracker.monitor = asyncio.create_task(self._recovery_monitor(tier))
            try:
                tracker.event.clear()
                await asyncio.wait_for(tracker.event.wait(), timeout=max(0.0, max_wait))
            except TimeoutError:
                pass
        finally:
            tracker.waiters -= 1
            if tracker.waiters == 0:
                tracker.event.clear()
                monitor = tracker.monitor
                if monitor is not None and not monitor.done():
                    monitor.cancel()
                tracker.monitor = None
        return time.monotonic() - start

    async def _recovery_monitor(self, tier: ProviderTier) -> None:
        """Wake the paused dispatch of ``tier`` when any provider recovers."""
        while True:
            tracker = self._pauses[self._tier_index(tier)]
            if tracker.waiters == 0:
                return
            if any(
                self._is_available(runtime.provider.name)
                for runtime in self._runtimes
                if runtime.provider.tier is tier and runtime.provider.enabled
            ):
                tracker.event.set()
                return
            await asyncio.sleep(0.05)

    # -- provider attempt ---------------------------------------------------- #

    async def _attempt(
        self,
        provider: ProviderConfig,
        prompt: dict[str, Any],
        *,
        deadline: float | None = None,
    ) -> CallResult:
        """One provider attempt: the full retry sequence, then health bookkeeping.

        Returns a ``CallResult`` on success; raises ``ProviderError`` otherwise.
        Health transitions follow cascade-design §2.4: retryable exhaustion moves
        the provider to COOLDOWN (or OPEN for a failed probe), auth/config errors
        disable it, refusals leave it untouched.

        When ``deadline`` is given it bounds the whole attempt: the per-attempt
        HTTP timeout is capped at the remaining budget, retry backoff is skipped
        when it no longer fits, and a spent budget raises a ``budget_exhausted``
        ``ProviderError`` so the cascade aborts (cascade-design §2.2).
        """
        runtime = self._runtime(provider.name)
        async with runtime.lock:
            probing = runtime.health in (HealthState.HALF_OPEN, HealthState.COOLDOWN)
            if probing and runtime.probe_in_flight:
                raise ProviderError(provider.name, ProviderOutcome.ERROR, "probe already in flight")
            if not runtime.bucket.try_take():
                raise ProviderError(provider.name, ProviderOutcome.QUOTA, "token bucket empty")
            if probing:
                runtime.probe_in_flight = True
            try:
                last_exc: ProviderError | None = None
                for attempt_no in range(provider.max_retries + 1):
                    remaining = self._remaining(deadline)
                    if remaining is not None and remaining <= 0:
                        raise ProviderError(
                            provider.name,
                            ProviderOutcome.TIMEOUT,
                            "call budget exhausted",
                            budget_exhausted=True,
                        )
                    timeout_s = provider.timeout_s
                    if remaining is not None:
                        timeout_s = min(timeout_s, remaining)
                    try:
                        raw = await self._post(provider, prompt, timeout_s=timeout_s)
                        result = raw.to_result(provider)
                    except ProviderError as exc:
                        last_exc = exc
                        if exc.outcome is ProviderOutcome.REFUSAL:
                            break
                        if exc.outcome is ProviderOutcome.AUTH_ERROR:
                            self._record_disable(provider)
                            break
                        if attempt_no >= provider.max_retries:
                            break
                        delay = self._retry_delay(attempt_no)
                        remaining = self._remaining(deadline)
                        if remaining is not None and remaining <= delay:
                            break
                        await asyncio.sleep(delay)
                        continue
                    self._record_success(provider)
                    return result
                assert last_exc is not None
                if last_exc.outcome in (ProviderOutcome.REFUSAL, ProviderOutcome.AUTH_ERROR):
                    raise ProviderError(
                        provider.name,
                        last_exc.outcome,
                        last_exc.message,
                        last_exc.cause,
                        budget_exhausted=last_exc.budget_exhausted,
                    ) from last_exc
                self._record_failure(provider)
                raise ProviderError(
                    provider.name,
                    last_exc.outcome,
                    last_exc.message,
                    last_exc.cause,
                    budget_exhausted=last_exc.budget_exhausted,
                ) from last_exc
            finally:
                runtime.probe_in_flight = False

    def _retry_delay(self, attempt_no: int) -> float:
        # Full jitter (mirrors arch §7.4): uniform(0, base * backoff ** n).
        return random.uniform(0.0, self._retry_base_delay_s * (2.0 ** attempt_no))

    @staticmethod
    def _remaining(deadline: float | None) -> float | None:
        """Seconds left before ``deadline`` (None when unbounded)."""
        if deadline is None:
            return None
        return deadline - time.monotonic()

    async def _post(
        self,
        provider: ProviderConfig,
        prompt: dict[str, Any],
        *,
        timeout_s: float,
    ) -> _RawResponse:
        return await asyncio.to_thread(self._post_sync, provider, prompt, timeout_s)

    def _post_sync(
        self, provider: ProviderConfig, prompt: dict[str, Any], timeout_s: float
    ) -> _RawResponse:
        # Defensive transport guard: a ProviderConfig constructed without going
        # through the config loader must still never send the Authorization
        # header over plaintext http to a non-loopback host (security audit).
        parsed = urlparse(provider.base_url)
        scheme = parsed.scheme.lower()
        if scheme == "http" and not is_loopback_host(parsed.hostname or ""):
            raise ProviderError(
                provider.name,
                ProviderOutcome.AUTH_ERROR,
                "http transport is allowed only for loopback hosts; "
                "remote providers require https",
            )
        api_key = os.environ.get(provider.api_key_env)
        if not api_key:
            raise ProviderError(
                provider.name,
                ProviderOutcome.AUTH_ERROR,
                f"env var {provider.api_key_env!r} not set",
            )
        url = f"{provider.base_url.rstrip('/')}/chat/completions"
        body = {**prompt, "model": provider.model}
        data = json.dumps(body).encode("utf-8")
        request = urllib.request.Request(
            url,
            data=data,
            method="POST",
            headers={
                "Content-Type": "application/json",
                "Authorization": f"Bearer {api_key}",
                "User-Agent": USER_AGENT,
            },
        )
        # Fail-closed transport: never follow a provider redirect (urllib would
        # replay the Authorization header against the redirect target), and
        # never route loopback http through a proxy (HTTP_PROXY would capture
        # the Authorization Bearer). https remote providers keep normal proxy
        # behavior.
        handlers: list[urllib.request.BaseHandler] = [_NoRedirectHandler()]
        if scheme == "http":
            handlers.append(urllib.request.ProxyHandler({}))
        opener = urllib.request.build_opener(*handlers)
        start = time.monotonic()
        http_error: ProviderError | None = None
        http_cause: _SanitizedHTTPError | None = None
        try:
            with opener.open(request, timeout=timeout_s) as response:
                payload = json.loads(response.read().decode("utf-8"))
        except urllib.error.HTTPError as exc:
            status = exc.code
            try:
                error_body = exc.read().decode("utf-8", errors="replace")
            except Exception:
                error_body = ""
            safe_body = _redact_error_text(error_body, api_key)[:500]
            http_cause = _SanitizedHTTPError(
                status, _redact_error_text(str(exc.reason), api_key)
            )
            http_error = self._classify_http(
                provider, status, safe_body, cause=http_cause
            )
        except urllib.error.URLError as exc:
            reason = exc.reason
            if isinstance(reason, TimeoutError):
                outcome = ProviderOutcome.TIMEOUT
            else:
                outcome = ProviderOutcome.ERROR
            raise ProviderError(
                provider.name, outcome, f"transport error: {reason}", exc
            ) from exc
        except TimeoutError as exc:
            raise ProviderError(
                provider.name,
                ProviderOutcome.TIMEOUT,
                f"timeout after {timeout_s}s",
                exc,
            ) from exc
        except (OSError, ValueError) as exc:
            raise ProviderError(
                provider.name, ProviderOutcome.ERROR, f"request failed: {exc}", exc
            ) from exc
        if http_error is not None:
            assert http_cause is not None
            raise http_error from http_cause
        if not isinstance(payload, dict):
            raise ProviderError(
                provider.name, ProviderOutcome.ERROR, "malformed response: not a JSON object"
            )
        return _RawResponse(payload, time.monotonic() - start)

    def _classify_http(
        self,
        provider: ProviderConfig,
        status: int,
        message: str,
        *,
        cause: BaseException | None = None,
    ) -> ProviderError:
        if status in (301, 302, 303, 307, 308):
            # Reached only via _NoRedirectHandler: a completion endpoint that
            # redirects is a contract violation that could replay the
            # Authorization header elsewhere; disable the provider fail-closed.
            return ProviderError(
                provider.name,
                ProviderOutcome.AUTH_ERROR,
                f"HTTP {status} redirect: provider completion endpoints must not redirect",
                cause,
            )
        if status == 429:
            return ProviderError(
                provider.name, ProviderOutcome.QUOTA, f"HTTP 429: {message}", cause
            )
        # Cloudflare's browser-signature block is a provider/network error, not
        # evidence that the configured API credential is invalid.
        if status == 403 and _CLOUDFLARE_1010_RE.search(message):
            return ProviderError(
                provider.name,
                ProviderOutcome.ERROR,
                f"HTTP 403 Cloudflare 1010: {message}",
                cause,
            )
        if status in (401, 403):
            return ProviderError(
                provider.name, ProviderOutcome.AUTH_ERROR, f"HTTP {status}: {message}", cause
            )
        if status == 400 and _REFUSAL_MARKERS.search(message):
            return ProviderError(
                provider.name, ProviderOutcome.REFUSAL, f"HTTP 400: {message}", cause
            )
        return ProviderError(
            provider.name, ProviderOutcome.ERROR, f"HTTP {status}: {message}", cause
        )

    # -- health bookkeeping -------------------------------------------------- #

    def _record_success(self, provider: ProviderConfig) -> None:
        runtime = self._runtime(provider.name)
        runtime.outcomes.append(True)
        if runtime.health in (HealthState.UNKNOWN, HealthState.COOLDOWN, HealthState.HALF_OPEN):
            runtime.health = HealthState.HEALTHY

    def _record_failure(self, provider: ProviderConfig) -> None:
        runtime = self._runtime(provider.name)
        runtime.outcomes.append(False)
        now = time.monotonic()
        if runtime.health in (HealthState.UNKNOWN, HealthState.HEALTHY):
            runtime.health = HealthState.COOLDOWN
            runtime.cooldown_until = now + provider.cooldown_s
        elif runtime.health in (HealthState.COOLDOWN, HealthState.HALF_OPEN):
            # PRIMARY OPEN trip: a failed probe. A provider in COOLDOWN is only
            # ever dispatched as one probe once its cooldown elapsed; HALF_OPEN
            # is itself a probe state. A failure here means persistence -> OPEN.
            runtime.health = HealthState.OPEN
            runtime.open_until = now + provider.cooldown_s * self._open_backoff_base
        # SECONDARY safety net (cascade-design §2.3): the sliding-window failure
        # rate can escalate COOLDOWN -> OPEN before the probe fires. It is almost
        # unreachable in practice — a full window of failures implies probes that
        # already tripped OPEN via the branch above — but it bounds the case
        # where probes never get admitted.
        if (
            runtime.health is HealthState.COOLDOWN
            and len(runtime.outcomes) == runtime.outcomes.maxlen
            and sum(not ok for ok in runtime.outcomes) / len(runtime.outcomes)
            >= self._breaker_threshold
        ):
            runtime.health = HealthState.OPEN
            runtime.open_until = now + provider.cooldown_s * self._open_backoff_base

    def _record_disable(self, provider: ProviderConfig) -> None:
        self._runtime(provider.name).health = HealthState.DISABLED

    # -- helpers ------------------------------------------------------------- #

    def _runtime(self, name: str) -> _ProviderRuntime:
        for runtime in self._runtimes:
            if runtime.provider.name == name:
                return runtime
        raise KeyError(f"unknown provider: {name!r}")
