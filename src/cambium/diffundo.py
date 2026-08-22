"""Diffundo — stateless, tiered, multi-provider LLM router.

Implements the provider-cascade contract of docs/architecture/architecture.md §9.
Current cascade behavior is defined by this module and
tests/scenarios/test_diffundo.py; research drafts provide historical context
only:

- **D1 — no local cache.** ``Diffundo`` is a stateless router; the only state is
  per-provider cooldown timers, circuit-breaker health, and token buckets
  (architecture §8.1, §9.2). There is no response store anywhere in the module.
- **D8c — prompt prefix layout.** ``validate_prompt_structure`` lints the
  immutable message header for volatile timestamps, epoch stamps, request or
  trace IDs, and UUIDs that would churn a provider's exact-prefix cache key.
- **D8f — token bucket + pause-on-exhaustion.** Each provider carries a token
  bucket refilled at ``rpm`` tokens/minute; an empty bucket marks the provider
  ``RATE_LIMITED`` and the cascade skips it. When every provider in a tier is
  unavailable, ``call`` pauses the dispatch on an ``asyncio.Event`` and a
  per-tier recovery monitor wakes it when any provider's bucket/cooldown/breaker
  recovers.
- **D11 — durable usage events (implementation plan step 3).** Every router
  call surfaces per-call usage evidence on the ``CallResult`` (success) or the
  terminal ``ProviderError`` (failure): provider, model, token fields,
  estimated cost, latency, the honored ``Retry-After``, the provider's
  request-rate status after the call, the provider-reported account-quota
  owner, the stable prompt-prefix byte length, the provider-reported
  cache-hit flag, and the failure reason. The worker persists these as
  redacted ``usage_event`` records through the EventStore. When a provider
  does not report a value the field is omitted from the event — a missing
  field never breaks the event or the session. These are metrics, not
  evidence of a local response cache (D1): the router always requests
  ``cache=False`` and ``provider_cache_hit`` records only what the provider
  reports.
- **Measured-quality ordering.** A ``Diffundo`` may be given a
  usage-debt snapshot (``ProviderDebt`` counters as folded by
  ``routing.DebtStore``). Config priority stays the primary cascade ordering
  key; ``selection.order_candidates`` uses success confidence, latency-SLO
  compliance, expected cost per successful turn, then a normalized tie-break
  to refine order only WITHIN an equal-priority run,
  so the measured evidence (e.g. codex's 12.5% cache-hit / p50 7.21s latency)
  moves it below better providers. A provider with no fresh debt scores a
  neutral weight and keeps its config-priority position instead of being
  pinned to the bottom permanently. Health states are untouched: the weight
  affects only ORDER among healthy available providers.

Provider health transitions implemented here are: UNKNOWN -> HEALTHY on first
success; UNKNOWN/HEALTHY -> COOLDOWN on retryable failure; COOLDOWN (probe) ->
OPEN on probe failure; OPEN -> HALF_OPEN after the open interval; HALF_OPEN ->
HEALTHY on probe success / OPEN on probe failure; any state -> DISABLED on a
non-retryable auth/config error, first call included. Refusals are request-level
fall-throughs that never drive health transitions. The **probe path is the
primary OPEN trip** (a failed probe after a cooldown or on a half-open probe);
the sliding-window failure-rate escalation is a secondary safety net that only
fires once the window is full.

Every ``call`` is bounded by a wall-clock deadline (``call_budget_s``): the
per-attempt HTTP timeout is capped
at the remaining budget, retries are skipped when the backoff no longer fits,
and the cascade aborts (``AllProvidersFailed``) once the budget is spent — the
deadline is not just a candidate-waiting bound.

Stdlib only. HTTP calls use urllib against an OpenAI-compatible
``/chat/completions`` endpoint; the API key is read from the environment (name
from ``ProviderConfig.api_key_env``) at call time. A provider tagged protocol
``codex_responses`` (codex_chatgpt auth) instead targets the pinned
``CODEX_CHATGPT_PROFILE`` endpoint with the OpenAI Responses-API shape over
SSE; its bearer token and optional ChatGPT account id come only from an
injected ``CredentialSource`` (a codex provider without one fails closed).
All blocking I/O runs off the event loop via ``asyncio.to_thread``.

**Codex prefix caching is provider-side — no in-repo churn source exists.**
Measured across the harness the codex responses endpoint reports
``cached_tokens`` on only 7/56 calls (12.5%), with sparse, non-monotonic
per-turn hits (e.g. 4/24 in one session), while the same byte-stable
in-session prompt prefix hits on essentially every call after the first on
the chat providers (opencode-go 109/110, zai 48/49). The prefix Cambium sends
to codex is byte-stable in-session: ``prompt_prefix_bytes`` is constant per
session (e.g. 24/24 calls at 5385), ``_codex_request_body`` emits a fixed
field order with no per-call timestamps or ids, and the leading ``developer``
item stays byte-identical as the transcript grows. The parse path is proven
correct (the reported hits decode to ``provider_cache_hit=True``); the sparse
``cached_tokens`` is the codex backend's own behavior, not a prefix churn or
a parse failure.

**Cache-control fields are rejected by this endpoint (live-probed).** The
GPT-5.6 caching controls from the standard Responses API are not accepted by
the consumer ``/backend-api/codex/responses`` endpoint: ``prompt_cache_options``
returns ``400 Unsupported parameter: prompt_cache_options`` and
``prompt_cache_breakpoint`` returns ``400 prompt_cache_breakpoint is not
supported on this model``. ``prompt_cache_key`` is accepted but reports
``cached_tokens: 0`` even for a byte-identical 24k-token prefix re-sent three
times, so it does not enable caching here either. Do not emit these fields:
the endpoint's sparse caching is its own behavior and cannot be controlled
from the request shape. The worker bounds transcript growth directly instead.
"""

from __future__ import annotations

import asyncio
import json
import math
import os
import random
import re
import time
import urllib.error
import urllib.request
import uuid
from collections import deque
from collections.abc import Iterator, Mapping, Sequence
from dataclasses import dataclass, replace
from datetime import UTC
from email.utils import parsedate_to_datetime
from enum import Enum
from typing import Any, cast
from urllib.parse import urlparse

from . import __version__
from .provider_config import CODEX_CHATGPT_PROFILE, AuthMode, Protocol, is_loopback_host
from .selection import Candidate, order_candidates

_TIMESTAMP_PATTERN = (
    r"\d{4}-\d{2}-\d{2}[T ]\d{2}:\d{2}"
    r"(?::\d{2}(?:\.\d+)?)?(?:Z|[+-]\d{2}:?\d{2})?"
)
_EPOCH_PATTERN = r"(?<!\d)(?:\d{10}|\d{13}|\d{16}|\d{19})(?:\.\d+)?(?!\d)"
_REQUEST_TRACE_ID_PATTERN = r"\b(?:request|trace)[ _-]?id\b"
_UUID_PATTERN = (
    r"(?<![0-9a-f])"
    r"[0-9a-f]{8}-?[0-9a-f]{4}-?[0-9a-f]{4}-?"
    r"[0-9a-f]{4}-?[0-9a-f]{12}"
    r"(?![0-9a-f])"
)
_VOLATILE_TOKEN_RE = re.compile(
    rf"(?P<timestamp>{_TIMESTAMP_PATTERN})"
    rf"|(?P<epoch>{_EPOCH_PATTERN})"
    rf"|(?P<request_trace_id>{_REQUEST_TRACE_ID_PATTERN})"
    rf"|(?P<uuid>{_UUID_PATTERN})",
    re.IGNORECASE,
)
_LINE_BREAK_RE = re.compile(r"\r\n|\r|\n")
_URL_CREDENTIALS_RE = re.compile(
    r"(?P<scheme>\b[a-z][a-z0-9+.-]*://)[^/\s?#@]*@",
    re.IGNORECASE,
)
_REDACTED = "[REDACTED]"
MAX_PROVIDER_RESPONSE_BYTES = 4 * 1024 * 1024
_CLOUDFLARE_1010_RE = re.compile(
    r"(?=.*\b1010\b)(?=.*(?:cloudflare|cf[- ]error|"
    r"error\s*(?:code\s*)?[:#-]?\s*1010|browser(?:['’]s)?\s+signature))",
    re.IGNORECASE | re.DOTALL,
)
USER_AGENT = f"cambium/{__version__}"
# Codex-ChatGPT transport identity headers, matching the codex CLI wire shape:
# the backend expects the CLI originator tag and a codex-shaped User-Agent.
CODEX_ORIGINATOR = "codex_cli_rs"
CODEX_USER_AGENT = "codex_cli_rs/1.0 (cambium; cambium)"
# Light content scan for model refusals returned as a 200 completion (issue 4).
# Documented heuristic: exact refusal phrases in the completion text are treated
# as a REFUSAL fall-through so a refusing model never wins the cascade.
# Bare "sorry" is deliberately NOT a refusal: legitimate coding responses
# ("sorry for the confusion, the fix is...") would otherwise be discarded.
# Refusal requires an explicit refusal verb; "sorry" alone is not one.
_CONTENT_REFUSAL_RE = re.compile(
    r"\b(?:i can'?t|cannot|can'?t)\s+(?:assist|help|comply|complete|answer)\b"
    r"|\b(?:refus(?:e|es|ed|ing|al))\b",
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
    """Outcome classes that decide fall-through and health transitions (§1.2).

    ``CONFIG_ERROR`` is the non-retryable configuration class: an unsupported
    model/parameter or a machine-readable model/parameter 400 quarantines the
    provider exactly like ``AUTH_ERROR`` (disable, never retry), it just names
    the cause (codex responses adapter).
    """

    TIMEOUT = "timeout"
    ERROR = "error"
    QUOTA = "quota"
    REFUSAL = "refusal"
    AUTH_ERROR = "auth_error"
    CONFIG_ERROR = "config_error"


@dataclass(frozen=True, slots=True)
class ProviderConfig:
    """Static provider description. ``api_key_env`` is an env-var NAME, never a
    key value; the value is resolved from the environment at call time (D7).

    ``auth``/``protocol`` tag the provider mode: the legacy ``API_KEY`` +
    ``CHAT_COMPLETIONS`` pair is unchanged; a ``CODEX_CHATGPT`` provider is
    pinned to ``CODEX_CHATGPT_PROFILE`` and carries empty ``base_url``/
    ``api_key_env`` (the transport derives the endpoint from the profile).
    ``reasoning_effort`` is a normal (non-secret) config field emitted as the
    Responses-API ``reasoning: {effort}`` body field on the codex path.
    """

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
    # Optional per-provider admission-balancing window (solution C): token
    # allowance for one balancing window; 0/absent falls back to the
    # routing.DEFAULT_TOKEN_WINDOW_ALLOWANCE placeholder.
    token_window_allowance: float = 0.0
    auth: AuthMode = AuthMode.API_KEY
    protocol: Protocol = Protocol.CHAT_COMPLETIONS
    # Optional provider context-window capacity in tokens (H2 capability
    # boundary): 0/absent means the provider declares no capacity, so a task
    # that requires ``min_context_window`` is never assigned to it.
    context_window: int = 0
    # Optional Responses-API reasoning effort (codex_responses providers):
    # absent -> the request body carries no reasoning field; the pinned codex
    # provider entry sets "max".
    reasoning_effort: str | None = None


@dataclass(frozen=True, slots=True)
class CredentialSource:
    """Injected bearer credential for a ``codex_chatgpt`` provider.

    The adapter never reads a token itself: the caller injects the access
    token (and the optional ChatGPT account id) at construction. ``account_id``
    is sent as the ``ChatGPT-Account-Id`` header only when set. A
    ``codex_chatgpt`` provider without an injected source fails closed with
    ``ProviderOutcome.AUTH_ERROR``.
    """

    access_token: str
    account_id: str | None = None


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
    retry_after_s: float | None = None
    request_rate_status: str | None = None
    account_quota_owner: str | None = None
    prompt_prefix_bytes: int | None = None
    prompt_prefix_tokens_estimate: int | None = None
    provider_cache_hit: bool | None = None


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
        retry_after_s: float | None = None,
        request_rate_status: str | None = None,
        account_quota_owner: str | None = None,
    ) -> None:
        super().__init__(f"provider {provider!r} {outcome.value}: {message}".rstrip())
        self.provider = provider
        self.outcome = outcome
        self.message = message
        self.cause = cause
        self.budget_exhausted = budget_exhausted
        self.retry_after_s = retry_after_s
        self.request_rate_status = request_rate_status
        self.account_quota_owner = account_quota_owner


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
    """A volatile token sits in the immutable prompt header."""


# --------------------------------------------------------------------------- #
# Prompt-structure lint (D8c)
# --------------------------------------------------------------------------- #


def _prompt_header(prompt: dict[str, Any]) -> Iterator[tuple[int, str]]:
    """Yield string content in messages before the first user-role tail."""
    messages = prompt.get("messages")
    if isinstance(messages, list) and messages:
        for index, message in enumerate(messages):
            if not isinstance(message, dict):
                continue
            if message.get("role") == "user":
                return
            content = message.get("content")
            if isinstance(content, str):
                yield index, content
        return
    content = prompt.get("prompt")
    if isinstance(content, str):
        yield 0, content


def validate_prompt_structure(prompt: dict[str, Any]) -> None:
    """Raise when volatile tokens appear in the immutable prompt header (D8c).

    Provider-side prefix caches are exact-prefix content-addressed; timestamps
    and request or trace IDs at the top churn the prefix key. Static, byte-stable
    content must sit at the top and dynamic user content at the bottom.
    """
    offending_indexes: list[int] = []
    first_detail: tuple[int, str] | None = None
    for message_index, content in _prompt_header(prompt):
        match = _VOLATILE_TOKEN_RE.search(content)
        if match is None:
            continue
        offending_indexes.append(message_index)
        if first_detail is None:
            line = len(_LINE_BREAK_RE.findall(content, 0, match.start())) + 1
            if match.lastgroup == "timestamp":
                token_description = "timestamp"
            elif match.lastgroup == "epoch":
                token_description = "epoch stamp"
            elif match.lastgroup == "request_trace_id":
                token_description = repr(match.group())
            else:
                token_description = "UUID"
            first_detail = line, token_description
    if first_detail is None:
        return
    line, token_description = first_detail
    raise PromptStructureError(
        f"message indexes {offending_indexes}; line {line}: volatile "
        f"{token_description} token in the static prefix; "
        "dynamic content belongs after the first user message (D8c)"
    )


def prompt_prefix_bytes(prompt: dict[str, Any]) -> int | None:
    """Stable byte prefix length of the leading system message (plan step 3).

    The byte length of the leading ``role: system`` message content, the
    fixed prefix that provider exact-prefix caches address. Returns ``None``
    when the prompt has no leading system message; a missing prefix is omitted
    from usage events, never an error. The metric is evidence for routing
    decisions, not evidence that a local response cache exists (D1).
    """
    messages = prompt.get("messages")
    if isinstance(messages, list) and messages:
        first = messages[0]
        if isinstance(first, dict) and first.get("role") == "system":
            content = first.get("content")
            if isinstance(content, str):
                return len(content.encode("utf-8"))
    return None


def prompt_prefix_estimate_tokens(prompt: dict[str, Any]) -> int | None:
    """Estimated tokens in the leading system prefix using UTF-8 bytes / 4.

    This standard heuristic is routing evidence, not tokenizer output.
    """
    prefix_bytes = prompt_prefix_bytes(prompt)
    if prefix_bytes is None:
        return None
    return prefix_bytes // 4


def _read_provider_response(response: Any, provider: str) -> bytes:
    body = response.read(MAX_PROVIDER_RESPONSE_BYTES + 1)
    if len(body) > MAX_PROVIDER_RESPONSE_BYTES:
        raise ProviderError(
            provider,
            ProviderOutcome.ERROR,
            f"response exceeds {MAX_PROVIDER_RESPONSE_BYTES} byte limit",
        )
    return body


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

    def to_result(
        self,
        provider: ProviderConfig,
        prompt: dict[str, Any],
        *,
        retry_after_s: float | None = None,
        account_quota_owner: str | None = None,
    ) -> CallResult:
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
        else:
            cached_tokens = _cached_tokens(usage)
            usage = dict(usage)
            usage.pop("cached_tokens", None)
            if cached_tokens is not None:
                usage["cached_tokens"] = cached_tokens
        return CallResult(
            provider=provider.name,
            model=self.payload.get("model") or provider.model,
            tier=provider.tier,
            content=content,
            latency_s=self.latency_s,
            usage=usage,
            estimated_cost_usd=_estimate_cost(provider, usage),
            tool_calls=tool_calls,
            retry_after_s=retry_after_s,
            account_quota_owner=account_quota_owner,
            prompt_prefix_bytes=prompt_prefix_bytes(prompt),
            prompt_prefix_tokens_estimate=prompt_prefix_estimate_tokens(prompt),
            provider_cache_hit=_provider_cache_hit(usage),
        )


class _CodexRawResponse(_RawResponse):
    """Parsed codex responses stream: the completed event plus the
    delta-assembled output text (``response.output_text.delta`` events)."""

    __slots__ = ("text",)

    def __init__(self, payload: dict[str, Any], latency_s: float, text: str) -> None:
        super().__init__(payload, latency_s)
        self.text = text

    def to_result(
        self,
        provider: ProviderConfig,
        prompt: dict[str, Any],
        *,
        retry_after_s: float | None = None,
        account_quota_owner: str | None = None,
    ) -> CallResult:
        if _CONTENT_REFUSAL_RE.search(self.text):
            # A completed stream whose assembled text is a model refusal:
            # fall through to the next provider, never a health transition
            # (mirrors the chat path's 200-completion heuristic).
            raise ProviderError(
                provider.name,
                ProviderOutcome.REFUSAL,
                f"completion content carries refusal markers: {self.text[:80]!r}",
            )
        usage = _codex_usage(self.payload)
        response = self.payload.get("response")
        model = provider.model
        if isinstance(response, dict) and isinstance(response.get("model"), str):
            model = response["model"]
        return CallResult(
            provider=provider.name,
            model=model,
            tier=provider.tier,
            content=self.text,
            latency_s=self.latency_s,
            usage=usage,
            estimated_cost_usd=_estimate_cost(provider, usage),
            retry_after_s=retry_after_s,
            account_quota_owner=account_quota_owner,
            prompt_prefix_bytes=prompt_prefix_bytes(prompt),
            prompt_prefix_tokens_estimate=prompt_prefix_estimate_tokens(prompt),
            provider_cache_hit=_provider_cache_hit(usage),
        )


def _provider_cache_hit(usage: dict[str, Any] | None) -> bool | None:
    """Provider-reported cache-hit for one completion, or None when unknown.

    True when the normalized cached-token count is positive; False when usage
    is present without a positive count; None when usage is absent. This records
    what the provider reports, the router always requests
    ``cache=False`` and never serves from a local response cache (D1).
    """
    if not isinstance(usage, dict):
        return None
    cached_tokens = _cached_tokens(usage)
    return cached_tokens is not None and cached_tokens > 0


def _cached_tokens(usage: dict[str, Any] | None) -> int | None:
    """Return the first valid cached-token count in provider-shape order."""
    if not isinstance(usage, dict):
        return None
    for details_key in ("prompt_tokens_details", "input_tokens_details"):
        details = usage.get(details_key)
        if isinstance(details, dict):
            cached_tokens = details.get("cached_tokens")
            if (
                isinstance(cached_tokens, int)
                and not isinstance(cached_tokens, bool)
                and cached_tokens >= 0
            ):
                return cached_tokens
    for key in ("cache_read_input_tokens", "cached_tokens"):
        cached_tokens = usage.get(key)
        if (
            isinstance(cached_tokens, int)
            and not isinstance(cached_tokens, bool)
            and cached_tokens >= 0
        ):
            return cached_tokens
    return None


_ACCOUNT_QUOTA_OWNER_KEYS = ("quota_owner", "account_quota_owner", "account_quota")


def _account_quota_owner(body: str, api_key: str) -> str | None:
    """Provider-reported account-quota owner from an error body, else None.

    Parses an OpenAI-compatible error payload for allowlisted quota-owner
    fields at the top level, under ``error``, or under ``error.rate_limit``
    (``quota_owner`` / ``account_quota_owner`` / ``account_quota``, plus
    ``rate_limit.scope``). The extracted value is redacted like any other
    provider text. Returns ``None`` when the provider does not report one; a
    missing owner never breaks the call or the usage event (plan step 3).
    """
    try:
        payload = json.loads(body)
    except (json.JSONDecodeError, ValueError):
        return None
    if not isinstance(payload, dict):
        return None
    candidates: list[dict[str, Any]] = [payload]
    error = payload.get("error")
    if isinstance(error, dict):
        candidates.append(error)
        rate_limit = error.get("rate_limit")
        if isinstance(rate_limit, dict):
            candidates.append(rate_limit)
    for candidate in candidates:
        keys: tuple[str, ...] = _ACCOUNT_QUOTA_OWNER_KEYS
        if candidate is not error and candidate is not payload:
            keys = (*keys, "scope")
        for key in keys:
            value = candidate.get(key)
            if isinstance(value, str) and value.strip():
                owner = _redact_error_text(value.strip(), api_key)
                return owner if owner != _REDACTED else None
    return None


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


def _redact_error_text(message: str, api_key: str) -> str:
    """Remove credentials while retaining safe provider diagnostics."""
    redacted = message.replace(api_key, _REDACTED)
    return _URL_CREDENTIALS_RE.sub(r"\g<scheme>" + _REDACTED + "@", redacted)


def _parse_retry_after(headers: Any) -> float | None:
    """Parse one provider Retry-After value into a nonnegative delay."""
    values: Sequence[Any] | None
    get_all = getattr(headers, "get_all", None)
    if callable(get_all):
        values = cast(Sequence[Any] | None, get_all("Retry-After"))
    elif isinstance(headers, Mapping):
        value = headers.get("Retry-After")
        values = [value] if value is not None else None
    else:
        values = None
    if not values or len(values) != 1:
        return None
    value = values[0]
    if not isinstance(value, str):
        return None
    value = value.strip()
    if not value:
        return None
    if value.isascii() and value.isdecimal():
        delay = float(value)
        if delay >= 0:
            return delay
        return None
    comma_count = value.count(",")
    if comma_count > 1 or (comma_count == 1 and not re.match(r"^[A-Za-z]+,\s", value)):
        return None
    try:
        retry_at = parsedate_to_datetime(value)
    except (IndexError, OverflowError, TypeError, ValueError):
        return None
    if retry_at.tzinfo is None:
        retry_at = retry_at.replace(tzinfo=UTC)
    delay = retry_at.timestamp() - time.time()
    if not math.isfinite(delay):
        return None
    return max(0.0, delay)


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
# Codex-ChatGPT responses adapter (protocol CODEX_RESPONSES)
# --------------------------------------------------------------------------- #

# In-stream error-event classification (probed live against
# https://chatgpt.com/backend-api/codex/responses): service outages are
# retryable (existing cooldown machinery); model/parameter problems are
# permanent config errors (disable the provider, never retry); content
# refusals fall through like any other refusal.
_RETRYABLE_CODEX_ERROR_MARKERS = (
    "service_unavailable",
    "server_is_overloaded",
    "overloaded",
    "unavailable",
    "rate_limit",
    "429",
)
_CONFIG_CODEX_ERROR_MARKERS = (
    "model_not_found",
    "not found",
    "unsupported",
    "invalid",
    "parameter",
)
_REFUSAL_CODEX_ERROR_MARKERS = ("content_policy", "refus")


def _codex_input_item(message: Mapping[str, Any]) -> dict[str, Any]:
    """One chat message -> one Responses-API input item (input_text parts).

    The system role maps to ``developer``; string content becomes an
    ``input_text`` part; list content (already OpenAI-shaped parts) passes
    through unchanged.
    """
    role = message.get("role", "user")
    if role == "system":
        role = "developer"
    content = message.get("content")
    if isinstance(content, str):
        part_type = "output_text" if role == "assistant" else "input_text"
        parts = [{"type": part_type, "text": content}]
    elif isinstance(content, list):
        # Normalize chat-shaped parts: the responses endpoint requires
        # ``input_text`` on user/developer turns and ``output_text`` on
        # assistant turns (live-verified: the backend rejects input_text on
        # assistant items). Non-dict parts are dropped so a malformed item
        # can never poison the request.
        part_type = "output_text" if role == "assistant" else "input_text"
        parts = []
        for part in content:
            if not isinstance(part, Mapping):
                continue
            if part.get("type") in ("text", "output_text", "input_text"):
                parts.append({"type": part_type, "text": part.get("text", "")})
            else:
                parts.append(dict(part))
    else:
        parts = []
    return {"role": role, "content": parts}


def _codex_tools(tools: Any) -> list[dict[str, Any]]:
    """Flatten chat tool entries to the Responses-API function-tool shape.

    Chat ``{"type": "function", "function": {name, description, parameters}}``
    becomes ``{"type": "function", name, description, parameters}``; non-function
    tool types are dropped (the responses endpoint accepts function tools only).
    """
    if not isinstance(tools, list):
        return []
    converted: list[dict[str, Any]] = []
    for tool in tools:
        if not isinstance(tool, Mapping) or tool.get("type") != "function":
            continue
        function = tool.get("function")
        if not isinstance(function, Mapping):
            continue
        item: dict[str, Any] = {"type": "function"}
        for key in ("name", "description", "parameters"):
            value = function.get(key)
            if value is not None:
                item[key] = value
        converted.append(item)
    return converted


def _codex_request_body(provider: ProviderConfig, prompt: dict[str, Any]) -> dict[str, Any]:
    """Convert a chat-completions prompt to the codex Responses-API request body.

    The endpoint requires the Responses shape — ``input`` as a list of
    ``{role, content: [{type: "input_text", text}]}`` items, ``store: false``,
    ``stream: true`` — and rejects chat extras (``max_output_tokens``, bare
    string input). Only the documented fields are emitted: prompt extras
    (``max_tokens`` etc.) never leak into the body.

    The body serializes deterministically: fixed insertion order, no per-call
    timestamps or request ids, and a byte-stable leading ``developer`` item
    for the system prompt, so the request head cannot churn the provider's
    exact-prefix cache key (D8c). The codex backend's sparse
    ``cached_tokens`` is provider-side (see module docstring).
    """
    body: dict[str, Any] = {
        "model": provider.model,
        "input": [],
        "store": False,
        "stream": True,
    }
    messages = prompt.get("messages")
    if isinstance(messages, list):
        body["input"] = [
            _codex_input_item(message) for message in messages if isinstance(message, Mapping)
        ]
    tools = _codex_tools(prompt.get("tools"))
    if tools:
        body["tools"] = tools
    tool_choice = prompt.get("tool_choice")
    if tool_choice is not None:
        body["tool_choice"] = tool_choice
    if provider.reasoning_effort:
        body["reasoning"] = {"effort": provider.reasoning_effort}
    return body


def _codex_usage(completed: dict[str, Any]) -> dict[str, Any] | None:
    """Normalize Responses-API usage to the chat-shape usage used downstream.

    The ``response.completed`` payload reports ``input_tokens``/``output_tokens``
    with ``input_tokens_details``/``output_tokens_details``; the cost estimate
    and cache-hit extraction read the chat shape (``prompt_tokens``/
    ``completion_tokens``, ``prompt_tokens_details.cached_tokens``), so the
    normalized dict carries both plus top-level ``cached_tokens``.
    """
    response = completed.get("response")
    usage = response.get("usage") if isinstance(response, dict) else completed.get("usage")
    if not isinstance(usage, dict):
        return None
    cached_tokens = _cached_tokens(usage)
    normalized: dict[str, Any] = {
        "prompt_tokens": usage.get("input_tokens") or 0,
        "completion_tokens": usage.get("output_tokens") or 0,
    }
    input_details = usage.get("input_tokens_details")
    if isinstance(input_details, dict):
        normalized["prompt_tokens_details"] = {
            "cached_tokens": cached_tokens if cached_tokens is not None else 0
        }
        normalized["input_tokens_details"] = dict(input_details)
    output_details = usage.get("output_tokens_details")
    if isinstance(output_details, dict):
        normalized["output_tokens_details"] = dict(output_details)
    if usage.get("total_tokens") is not None:
        normalized["total_tokens"] = usage["total_tokens"]
    if cached_tokens is not None:
        normalized["cached_tokens"] = cached_tokens
    return normalized


def _codex_stream_error(
    provider: ProviderConfig, error: Mapping[str, Any], access_token: str
) -> ProviderError:
    """Classify one in-stream codex error object into a ProviderError."""
    text = json.dumps(error)
    lowered = text.lower()
    if any(marker in lowered for marker in _RETRYABLE_CODEX_ERROR_MARKERS):
        outcome = ProviderOutcome.ERROR
    elif any(marker in lowered for marker in _CONFIG_CODEX_ERROR_MARKERS):
        outcome = ProviderOutcome.CONFIG_ERROR
    elif any(marker in lowered for marker in _REFUSAL_CODEX_ERROR_MARKERS):
        outcome = ProviderOutcome.REFUSAL
    else:
        outcome = ProviderOutcome.ERROR
    return ProviderError(
        provider.name,
        outcome,
        f"codex stream error: {_redact_error_text(text[:300], access_token)}",
    )


def _parse_codex_sse(
    provider: ProviderConfig, stream: str, access_token: str
) -> tuple[dict[str, Any], str, ProviderError | None]:
    """Parse a codex SSE stream into (completed event, assembled text, error).

    Text is assembled from ``response.output_text.delta`` events; the final
    ``response.completed`` event carries the full response incl. usage. An
    in-stream ``error`` or ``response.failed`` event short-circuits with a
    classified ``ProviderError``; a stream that ends without completion is
    malformed.
    """
    text_parts: list[str] = []
    completed: dict[str, Any] | None = None
    stream_error: ProviderError | None = None
    for line in stream.splitlines():
        if not line.startswith("data:"):
            continue
        data = line[len("data:"):].strip()
        if not data or data == "[DONE]":
            continue
        try:
            event = json.loads(data)
        except json.JSONDecodeError:
            continue
        if not isinstance(event, dict):
            continue
        event_type = event.get("type")
        if event_type == "response.output_text.delta":
            delta = event.get("delta")
            if isinstance(delta, str):
                text_parts.append(delta)
        elif event_type == "response.completed":
            completed = event
        elif event_type == "error":
            error = event.get("error")
            if isinstance(error, dict):
                stream_error = stream_error or _codex_stream_error(
                    provider, error, access_token
                )
        elif event_type == "response.failed":
            response = event.get("response")
            if isinstance(response, dict):
                error = response.get("error")
                if isinstance(error, dict):
                    stream_error = stream_error or _codex_stream_error(
                        provider, error, access_token
                    )
    text = "".join(text_parts)
    if stream_error is not None:
        return {}, text, stream_error
    if completed is None:
        return {}, text, ProviderError(
            provider.name,
            ProviderOutcome.ERROR,
            "malformed codex stream: no response.completed event",
        )
    return completed, text, None


def _codex_config_400(message: str) -> bool:
    """True when a codex HTTP 400 body is machine-readable and names a
    model/parameter problem.

    The live endpoint returns both ``{"error": {...}}`` and ``{"detail": "..."}``
    envelopes, so the whole payload is scanned. Only narrow markers count:
    model-not-found/unsupported-model, and explicit parameter rejections. Bare
    "invalid" is deliberately NOT a marker — generic request-shape errors
    (``{"detail": "Stream must be set to true"}``) are request-level, not
    provider config problems, and keep the generic content-refusal
    fall-through.
    """
    try:
        payload = json.loads(message)
    except (json.JSONDecodeError, ValueError):
        return False
    if not isinstance(payload, dict):
        return False
    lowered = json.dumps(payload).lower()
    return any(
        marker in lowered
        for marker in (
            "model_not_found",
            "model not found",
            "model does not exist",
            "unsupported model",
            "unknown model",
            "unsupported parameter",
            "invalid parameter",
        )
    )


# --------------------------------------------------------------------------- #
# Diffundo router
# --------------------------------------------------------------------------- #


class Diffundo:
    """Tiered provider router with a per-subagent primary association (D1).

    Per-instance state is limited to per-provider cooldown timers, circuit
    breaker health, token buckets, per-tier pause events (architecture
    §8.1/§9, D8f), and the task's primary provider association: one worker
    process runs one task, so the router binds to one provider and keeps
    sending the task's growing context to it, preserving per-provider
    prompt-prefix caching. The association moves only when that provider
    fails and a fallback serves. No attribute is a mutable mapping; there is
    no response store anywhere.
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
        rotation_seed: int = 0,
        primary_provider: str | None = None,
        credential_source: CredentialSource | None = None,
        codex_profile: Mapping[str, object] | None = None,
        debt: Mapping[str, Any] | None = None,
    ) -> None:
        self._providers = tuple(providers)
        self._runtimes = tuple(
            _ProviderRuntime(provider, breaker_window_size) for provider in self._providers
        )
        self._pauses = tuple(_PauseTracker() for _ in ProviderTier)
        # Per-subagent primary association: the provider this task is bound
        # to. The first pick comes from the lowest-priority eligible run,
        # rotated by ``rotation_seed`` (the worker seeds it from the task id)
        # so concurrent subagents spread across providers at task granularity.
        # The binding leads every subsequent candidate list while eligible and
        # moves to whichever provider actually serves, so a task's context
        # stays on one provider (prompt-prefix caching) and never bounces
        # back to a recovered provider.
        self._rotation = rotation_seed
        self._primary_provider: str | None = None
        if primary_provider is not None:
            # Supervisor-level admission balancing (solution C) presets the
            # per-subagent sticky primary from the task's assigned provider;
            # an absent name falls back to the seeded first pick below.
            for provider in self._providers:
                if provider.name == primary_provider:
                    self._primary_provider = provider.name
                    break
        self._call_budget_s = call_budget_s
        self._pause_timeout_s = pause_timeout_s
        self._breaker_window = breaker_window_size
        self._breaker_threshold = breaker_failure_threshold
        self._open_backoff_base = open_backoff_base
        self._retry_base_delay_s = retry_base_delay_s
        # Codex-ChatGPT responses adapter: the bearer credential is injected
        # (never read from the environment or config) and the endpoint profile
        # is pinned by default — the constructor override is a test/DI seam
        # only, providers.json can never set it.
        self._credential_source = credential_source
        self._codex_profile = (
            dict(CODEX_CHATGPT_PROFILE) if codex_profile is None else dict(codex_profile)
        )
        # Stable per-instance session identity for the codex ``session-id``
        # header: one worker process runs one task, so a per-instance UUID is
        # a per-session id and must not rotate per request.
        self._codex_session_id = str(uuid.uuid4())
        # Measured-usage debt snapshot (weighted routing): provider name ->
        # ProviderDebt-like counters (requests, cache_hit_count,
        # latency_total_s/latency_count, last_seen) used to order the cascade
        # within an equal-priority run by measured quality. None/empty keeps
        # the pure config-priority order. Stored as an immutable item tuple so
        # no attribute is a mutable mapping (D1).
        self._debt: tuple[tuple[str, Any], ...] | None = (
            tuple(debt.items()) if debt else None
        )

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
                # The provider that served owns the task's context from here
                # on (prompt-prefix caching locality).
                self._primary_provider = provider.name
                return result
            raise AllProvidersFailed(tried, last_error)

    def health(self, name: str) -> HealthState:
        """Current circuit-breaker health state for a provider."""
        return self._runtime(name).health

    def declared_model(self, name: str) -> str:
        """The model id the named provider is configured to serve.

        Trust reference for response validation: a CallResult from this
        router must report exactly this id for its serving provider. This
        — not the caller's pinned model — is what sibling fallback must
        satisfy when providers in one tier declare different models.
        """
        return self._runtime(name).provider.model

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
        sorted by priority ascending, refined by measured quality within an
        equal-priority run (arch §9.1/§9.2 step 1, weighted routing).

        When ``model`` is pinned, providers in the same tier that declare a
        different model are kept as ordered fallback candidates behind the
        strict model matches, so a quota/rate-limit failure on the pinned
        provider can cascade to a sibling instead of surfacing as
        ``AllProvidersFailed``. If no provider declares the pinned model, the
        pin is a configuration error and no candidate is returned.
        """
        now = time.monotonic()
        eligible: list[ProviderConfig] = []
        model_declared = model is None
        for runtime in self._runtimes:
            provider = runtime.provider
            if provider.tier is not tier or not provider.enabled:
                continue
            if model is not None and provider.model == model:
                model_declared = True
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
            eligible.append(provider)
        if model is None:
            return self._order_candidates(eligible)
        strict = [provider for provider in eligible if provider.model == model]
        fallback = [provider for provider in eligible if provider.model != model]
        if strict:
            return self._order_candidates(strict) + self._order_candidates(fallback)
        if model_declared:
            return self._order_candidates(fallback)
        return []

    def _order_candidates(self, candidates: list[ProviderConfig]) -> list[ProviderConfig]:
        ordered = order_candidates(
            cast(Sequence[Candidate], candidates),
            debt=dict(self._debt) if self._debt is not None else None,
            incumbent=self._primary_provider,
            rotation_offset=self._rotation,
            now=time.time(),
        )
        return cast(list[ProviderConfig], ordered)

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
                last_retry_after: float | None = None
                last_quota_owner: str | None = None
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
                        result = raw.to_result(
                            provider,
                            prompt,
                            retry_after_s=last_retry_after,
                            account_quota_owner=last_quota_owner,
                        )
                    except ProviderError as exc:
                        last_exc = exc
                        if exc.retry_after_s is not None:
                            last_retry_after = exc.retry_after_s
                        if exc.account_quota_owner is not None:
                            last_quota_owner = exc.account_quota_owner
                        if exc.outcome is ProviderOutcome.REFUSAL:
                            break
                        if exc.outcome in (
                            ProviderOutcome.AUTH_ERROR,
                            ProviderOutcome.CONFIG_ERROR,
                        ):
                            self._record_disable(provider)
                            break
                        # A transport timeout means the endpoint (or a CDN in
                        # front of it) is tarpitting this client; re-POSTing
                        # into the same black hole at backoff scale burns the
                        # call budget that sibling candidates still need. The
                        # cascade IS the retry: fall through immediately.
                        if exc.outcome is ProviderOutcome.TIMEOUT:
                            break
                        if attempt_no >= provider.max_retries:
                            break
                        delay = (
                            exc.retry_after_s
                            if exc.retry_after_s is not None
                            else self._retry_delay(attempt_no)
                        )
                        # Keep even an arbitrarily large provider delay: the
                        # call deadline check below skips it without jitter.
                        remaining = self._remaining(deadline)
                        if remaining is not None and remaining <= delay:
                            break
                        await asyncio.sleep(delay)
                        continue
                    request_rate_status = self._record_success(provider)
                    return replace(
                        result, request_rate_status=request_rate_status
                    )
                assert last_exc is not None
                if last_exc.outcome in (
                    ProviderOutcome.REFUSAL,
                    ProviderOutcome.AUTH_ERROR,
                    ProviderOutcome.CONFIG_ERROR,
                ):
                    request_rate_status = self.status(provider.name).value
                    raise ProviderError(
                        provider.name,
                        last_exc.outcome,
                        last_exc.message,
                        last_exc.cause,
                        budget_exhausted=last_exc.budget_exhausted,
                        retry_after_s=last_exc.retry_after_s,
                        request_rate_status=request_rate_status,
                        account_quota_owner=last_exc.account_quota_owner,
                    ) from last_exc
                request_rate_status = self._record_failure(provider)
                raise ProviderError(
                    provider.name,
                    last_exc.outcome,
                    last_exc.message,
                    last_exc.cause,
                    budget_exhausted=last_exc.budget_exhausted,
                    retry_after_s=last_exc.retry_after_s,
                    request_rate_status=request_rate_status,
                    account_quota_owner=last_exc.account_quota_owner,
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
        if provider.protocol is Protocol.CODEX_RESPONSES:
            return self._codex_post_sync(provider, prompt, timeout_s)
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
        payload: Any = None
        try:
            with opener.open(request, timeout=timeout_s) as response:
                response_body = _read_provider_response(response, provider.name)
                payload = json.loads(response_body.decode("utf-8"))
        except urllib.error.HTTPError as exc:
            status = exc.code
            try:
                error_body = _read_provider_response(exc, provider.name).decode(
                    "utf-8", errors="replace"
                )
            except ProviderError:
                raise
            except Exception:
                error_body = ""
            safe_body = _redact_error_text(error_body, api_key)[:500]
            http_cause = _SanitizedHTTPError(
                status, _redact_error_text(str(exc.reason), api_key)
            )
            http_error = self._classify_http(
                provider,
                status,
                safe_body,
                cause=http_cause,
                retry_after_s=_parse_retry_after(exc.headers) if status == 429 else None,
                account_quota_owner=_account_quota_owner(error_body, api_key),
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

    def _codex_post_sync(
        self, provider: ProviderConfig, prompt: dict[str, Any], timeout_s: float
    ) -> _RawResponse:
        """Codex-ChatGPT ``/backend-api/codex/responses`` transport (SSE).

        The endpoint is pinned to ``CODEX_CHATGPT_PROFILE`` (constructor-
        injectable for tests only; providers.json can never set it). The bearer
        token and optional account id come from the injected
        ``CredentialSource`` only — without one a codex provider fails closed
        with ``AUTH_ERROR``. The same fail-closed transport guards apply as on
        the chat path: no redirects, no plaintext http off loopback, no proxy
        on loopback.
        """
        profile = self._codex_profile
        origin = str(profile.get("api_origin") or "").rstrip("/")
        path = str(profile.get("api_path") or "")
        parsed = urlparse(origin)
        scheme = parsed.scheme.lower()
        if scheme not in ("https", "http"):
            raise ProviderError(
                provider.name,
                ProviderOutcome.AUTH_ERROR,
                "codex responses endpoint origin must be an absolute http(s) URL",
            )
        if scheme == "http" and not is_loopback_host(parsed.hostname or ""):
            raise ProviderError(
                provider.name,
                ProviderOutcome.AUTH_ERROR,
                "http transport is allowed only for loopback hosts; "
                "remote providers require https",
            )
        credential = self._credential_source
        if credential is None:
            raise ProviderError(
                provider.name,
                ProviderOutcome.AUTH_ERROR,
                "provider requires auth 'codex_chatgpt' but no credential "
                "source is injected",
            )
        if not credential.access_token:
            raise ProviderError(
                provider.name,
                ProviderOutcome.AUTH_ERROR,
                "injected credential source carries an empty access token",
            )
        access_token = credential.access_token
        url = f"{origin}{path}"
        body = _codex_request_body(provider, prompt)
        data = json.dumps(body).encode("utf-8")
        headers: dict[str, str] = {
            "Content-Type": "application/json",
            "Authorization": f"Bearer {access_token}",
            "User-Agent": CODEX_USER_AGENT,
            "originator": CODEX_ORIGINATOR,
            "session-id": self._codex_session_id,
        }
        if credential.account_id:
            headers["ChatGPT-Account-Id"] = credential.account_id
        request = urllib.request.Request(url, data=data, method="POST", headers=headers)
        # Fail-closed transport, same rationale as the chat path.
        handlers: list[urllib.request.BaseHandler] = [_NoRedirectHandler()]
        if scheme == "http":
            handlers.append(urllib.request.ProxyHandler({}))
        opener = urllib.request.build_opener(*handlers)
        start = time.monotonic()
        http_error: ProviderError | None = None
        http_cause: _SanitizedHTTPError | None = None
        stream = ""
        try:
            with opener.open(request, timeout=timeout_s) as response:
                stream = _read_provider_response(response, provider.name).decode("utf-8")
        except urllib.error.HTTPError as exc:
            status = exc.code
            try:
                error_body = _read_provider_response(exc, provider.name).decode(
                    "utf-8", errors="replace"
                )
            except ProviderError:
                raise
            except Exception:
                error_body = ""
            safe_body = _redact_error_text(error_body, access_token)[:500]
            http_cause = _SanitizedHTTPError(
                status, _redact_error_text(str(exc.reason), access_token)
            )
            http_error = self._classify_http(
                provider,
                status,
                safe_body,
                cause=http_cause,
                retry_after_s=_parse_retry_after(exc.headers) if status == 429 else None,
                account_quota_owner=_account_quota_owner(error_body, access_token),
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
        payload, text, stream_error = _parse_codex_sse(provider, stream, access_token)
        if stream_error is not None:
            raise stream_error
        return _CodexRawResponse(payload, time.monotonic() - start, text)

    def _classify_http(
        self,
        provider: ProviderConfig,
        status: int,
        message: str,
        *,
        cause: BaseException | None = None,
        retry_after_s: float | None = None,
        account_quota_owner: str | None = None,
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
                provider.name,
                ProviderOutcome.QUOTA,
                f"HTTP 429: {message}",
                cause,
                retry_after_s=retry_after_s,
                account_quota_owner=account_quota_owner,
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
        if status == 400:
            # Codex split (review requirement): a machine-readable 400 naming a
            # model/parameter problem is a permanent CONFIG error that
            # quarantines the provider, NOT a content refusal. The generic
            # all-400 -> REFUSAL rule below is unchanged for chat_completions
            # providers.
            if (
                provider.protocol is Protocol.CODEX_RESPONSES
                and _codex_config_400(message)
            ):
                return ProviderError(
                    provider.name,
                    ProviderOutcome.CONFIG_ERROR,
                    f"HTTP 400: {message}",
                    cause,
                )
            # Deterministic HTTP 400s are permanent request-level rejections
            # (verified live: zai 1214 'messages illegal' was retried then
            # cooled down). A generic 400 used to fall to the retryable ERROR
            # class; classify it as REFUSAL so it is never retried and never
            # drives a health transition.
            return ProviderError(
                provider.name, ProviderOutcome.REFUSAL, f"HTTP 400: {message}", cause
            )
        return ProviderError(
            provider.name, ProviderOutcome.ERROR, f"HTTP {status}: {message}", cause
        )

    # -- health bookkeeping -------------------------------------------------- #

    def _record_success(self, provider: ProviderConfig) -> str:
        """Record one success; returns the provider's request-rate status."""
        runtime = self._runtime(provider.name)
        runtime.outcomes.append(True)
        if runtime.health in (HealthState.UNKNOWN, HealthState.COOLDOWN, HealthState.HALF_OPEN):
            runtime.health = HealthState.HEALTHY
        return self.status(provider.name).value

    def _record_failure(self, provider: ProviderConfig) -> str:
        """Record one failure; returns the provider's request-rate status."""
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
        return self.status(provider.name).value

    def _record_disable(self, provider: ProviderConfig) -> None:
        self._runtime(provider.name).health = HealthState.DISABLED

    # -- helpers ------------------------------------------------------------- #

    def _runtime(self, name: str) -> _ProviderRuntime:
        for runtime in self._runtimes:
            if runtime.provider.name == name:
                return runtime
        raise KeyError(f"unknown provider: {name!r}")
