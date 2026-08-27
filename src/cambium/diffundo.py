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
non-retryable auth/config error, first call included. Refusals and content
flags are request-level fall-throughs that never drive health transitions. A
content flag is separately surfaced so the caller can transform context once
before allowing normal cascade failover. The **probe path is the primary OPEN
trip** (a failed probe after a cooldown or on a half-open probe);
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
import errno
import hashlib
import json
import math
import os
import random
import re
import socket
import ssl
import threading
import time
import urllib.error
import urllib.request
import uuid
import weakref
from collections import deque
from collections.abc import Callable, Iterator, Mapping, Sequence
from dataclasses import dataclass, field, replace
from datetime import UTC
from email.utils import parsedate_to_datetime
from enum import Enum
from typing import Any, cast
from urllib.parse import urlparse

from . import __version__
from .provider_config import CODEX_CHATGPT_PROFILE, AuthMode, Protocol, is_loopback_host
from .provider_scheduler import (
    BillingMode,
    CacheCapability,
    ProviderLease,
    QuotaLedger,
    QuotaWindowSpec,
    quota_snapshot_json,
)
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
DEFAULT_SUMMARY_CALL_BUDGET_S = 120.0
_CLOUDFLARE_1010_RE = re.compile(
    r"(?=.*\b1010\b)(?=.*(?:cloudflare|cf[- ]error|"
    r"error\s*(?:code\s*)?[:#-]?\s*1010|browser(?:['’]s)?\s+signature))",
    re.IGNORECASE | re.DOTALL,
)
_WAF_403_RE = re.compile(
    r"(?:cloudflare|cf[- ]?(?:ray|error)|web application firewall|\bwaf\b|"
    r"akamai|imperva|sucuri|bot (?:detected|detection|protection)|"
    r"automated (?:traffic|request)|browser(?:['’]s)? signature|"
    r"browser integrity|captcha|security (?:challenge|rule)|"
    r"\b(?:1006|1009|1010|1015|1020)\b)",
    re.IGNORECASE,
)
_HTTP_403_AUTH_MARKERS = (
    "invalid_api_key",
    "invalid api key",
    "invalid_api_token",
    "invalid api token",
    "invalid token",
    "token expired",
    "token has expired",
    "expired token",
    "credential revoked",
    "credentials revoked",
    "revoked credential",
    "invalid credential",
    "authentication failed",
    "authentication error",
    "auth failed",
    "not authenticated",
    "api key is invalid",
    "api key not valid",
)
_HTTP_403_QUOTA_MARKERS = (
    "insufficient_quota",
    "insufficient quota",
    "quota_exceeded",
    "quota exceeded",
    "exceeded your current quota",
    "rate_limit",
    "rate limit",
    "billing_hard_limit",
    "billing hard limit",
    "billing",
    "payment_required",
    "payment required",
    "credit balance",
    "out of credits",
    "credits exhausted",
    "spend limit",
    "usage limit",
    "subscription limit",
)
_HTTP_403_MODEL_MARKERS = (
    "model_not_found",
    "model not found",
    "model_not_allowed",
    "model not allowed",
    "model_not_available",
    "model not available",
    "model_not_enabled",
    "model not enabled",
    "model_access_denied",
    "model access denied",
    "model_not_entitled",
    "model not entitled",
    "unsupported model",
    "unknown model",
    "not entitled",
    "entitlement",
    "does not have access to model",
    "do not have access to model",
    "no access to model",
    "permission to use model",
    "model is not permitted",
    "model forbidden",
)
_HTTP_403_REFUSAL_MARKERS = (
    "content_policy",
    "content policy",
    "content_policy_violation",
    "content filter",
    "content_filter",
    "policy violation",
    "blocked by policy",
    "due to policy",
    "safety violation",
    "safety filter",
    "blocked by safety",
    "prompt violates",
    "disallowed content",
    "moderation",
    "responsible ai",
    "acceptable use",
)
_REAL_DEATH_ENDPOINT_RE = re.compile(
    r"(?:endpoint\s*(?:is\s*)?unavailable|service\s*unavailable|"
    r"server[_ -]?error|temporarily\s+unavailable)",
    re.IGNORECASE,
)
_REAL_DEATH_TRANSPORT_RE = re.compile(
    r"(?:connection\s+refused|name\s+or\s+service\s+not\s+known|"
    r"temporary\s+failure\s+in\s+name\s+resolution|"
    r"nodename\s+nor\s+servname|certificate\s+verify\s+failed|"
    r"tls\s+(?:handshake|error)|ssl\s+(?:error|handshake))",
    re.IGNORECASE,
)
USER_AGENT = f"cambium/{__version__}"
# Codex-ChatGPT transport identity headers, matching the codex CLI wire shape:
# the backend expects the CLI originator tag and a codex-shaped User-Agent.
CODEX_ORIGINATOR = "codex_cli_rs"
CODEX_USER_AGENT = "codex_cli_rs/1.0 (cambium; cambium)"


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

    ``CONTENT_FLAGGED`` is a request-level, caller-recoverable policy signal;
    unlike ``REFUSAL`` it is not a terminal semantic refusal and never changes
    provider health. ``CONFIG_ERROR`` is the non-retryable configuration class:
    an unsupported
    model/parameter or a machine-readable model/parameter 400 quarantines the
    provider exactly like ``AUTH_ERROR`` (disable, never retry), it just names
    the cause (codex responses adapter).
    """

    TIMEOUT = "timeout"
    ERROR = "error"
    QUOTA = "quota"
    REFUSAL = "refusal"
    CONTENT_FLAGGED = "content_flagged"
    AUTH_ERROR = "auth_error"
    CONFIG_ERROR = "config_error"


_STRUCTURED_CONTENT_FLAG_CODES = frozenset(
    {
        "invalid_prompt",
        "invalid_prompt_error",
        "prompt_flagged",
        "prompt_blocked",
        "blocked_prompt",
        "content_policy_violation",
        "content_filter_violation",
    }
)
_STRUCTURED_POLICY_REFUSAL_CODES = frozenset(
    {
        "content_policy",
        "content_policy_error",
        "content_policy_violation",
        "content_filter",
        "content_filter_error",
        "content_filter_violation",
    }
)
_INVALID_PROMPT_POLICY_TYPES = frozenset(
    {
        "invalid_request_error",
        "invalid_request",
        "content_policy",
        "content_policy_error",
        "content_filter",
        "content_filter_error",
        "policy_error",
        "policy_violation",
        "prompt_policy_error",
        "safety_error",
    }
)
_INVALID_PROMPT_POLICY_MARKERS = frozenset(
    {
        "usage policy",
        "usage policies",
        "policy",
        "policies",
        "disallowed",
        "safety",
        "system",
        "moderation",
        "content filter",
        "content filtering",
        "content_filter",
        "blocked",
        "violation",
    }
)
_STRUCTURED_POLICY_FLAG_CODES = frozenset(
    {"content_policy_violation", "content_filter_violation"}
)
_STRUCTURED_CODEX_OUTCOMES = {
    "model_not_found": ProviderOutcome.CONFIG_ERROR,
    "unsupported_model": ProviderOutcome.CONFIG_ERROR,
    "invalid_parameter": ProviderOutcome.CONFIG_ERROR,
    "unsupported_parameter": ProviderOutcome.CONFIG_ERROR,
    "service_unavailable_error": ProviderOutcome.ERROR,
    "server_is_overloaded": ProviderOutcome.ERROR,
    "server_error": ProviderOutcome.ERROR,
}
_CODEX_CONFIG_CODES = frozenset(
    {
        "model_not_found",
        "unsupported_model",
        "invalid_parameter",
        "unsupported_parameter",
    }
)
_CODEX_CONFIG_FIELD_MARKERS = frozenset({"unsupported", "parameter", "not_found"})
_STRUCTURED_HTTP_OUTCOMES = {
    "invalid_api_key": ProviderOutcome.AUTH_ERROR,
    "invalid_api_token": ProviderOutcome.AUTH_ERROR,
    "invalid_token": ProviderOutcome.AUTH_ERROR,
    "authentication_error": ProviderOutcome.AUTH_ERROR,
    "unauthorized": ProviderOutcome.AUTH_ERROR,
    "insufficient_quota": ProviderOutcome.QUOTA,
    "quota_exceeded": ProviderOutcome.QUOTA,
    "rate_limit_error": ProviderOutcome.QUOTA,
    "billing_hard_limit": ProviderOutcome.QUOTA,
    "payment_required": ProviderOutcome.QUOTA,
    "model_not_found": ProviderOutcome.CONFIG_ERROR,
    "model_not_allowed": ProviderOutcome.CONFIG_ERROR,
    "model_not_available": ProviderOutcome.CONFIG_ERROR,
    "model_not_enabled": ProviderOutcome.CONFIG_ERROR,
    "model_access_denied": ProviderOutcome.CONFIG_ERROR,
    "model_not_entitled": ProviderOutcome.CONFIG_ERROR,
}


def _error_body_objects(body: str | Mapping[str, Any]) -> tuple[Mapping[str, Any], ...] | None:
    """Return the JSON error envelope and nested error object, if present."""
    if isinstance(body, Mapping):
        payload: Any = body
    else:
        try:
            payload = json.loads(body)
        except (json.JSONDecodeError, TypeError, ValueError):
            return None
    if not isinstance(payload, Mapping):
        return None
    nested = payload.get("error")
    if isinstance(nested, Mapping):
        return (payload, nested)
    return (payload,)


def _error_tokens(body: str | Mapping[str, Any]) -> tuple[str, ...] | None:
    """Extract normalized type/code/status values without scanning messages."""
    objects = _error_body_objects(body)
    if objects is None:
        return None
    tokens: list[str] = []
    for field_name in ("code", "type", "status"):
        for item in objects:
            value = item.get(field_name)
            if isinstance(value, str):
                token = _ERROR_TOKEN_RE.sub("_", value.casefold()).strip("_")
            elif type(value) is int:
                token = str(value)
            else:
                token = ""
            if token:
                tokens.append(token)
    return tuple(tokens)


def _error_message_text(body: str | Mapping[str, Any]) -> str:
    """Return freeform error text for the legacy marker fallback."""
    objects = _error_body_objects(body)
    if objects is None:
        return str(body)
    messages = [
        value
        for item in objects
        for key in ("message", "detail", "error")
        if isinstance(value := item.get(key), str)
    ]
    if messages:
        return " ".join(messages)
    return json.dumps(body, default=str) if isinstance(body, Mapping) else str(body)


def _codex_prompt_flagged(error: str | Mapping[str, Any]) -> bool:
    """Require the narrow policy evidence for a recoverable codex prompt flag."""
    objects = _error_body_objects(error)
    if objects is None:
        return False
    codes = {
        _ERROR_TOKEN_RE.sub("_", value.casefold()).strip("_")
        for item in objects
        for value in (item.get("code"),)
        if isinstance(value, str)
    }
    types = {
        _ERROR_TOKEN_RE.sub("_", value.casefold()).strip("_")
        for item in objects
        for value in (item.get("type"),)
        if isinstance(value, str)
    }
    types.discard("error")
    if not types.issubset(_INVALID_PROMPT_POLICY_TYPES):
        return False
    if "invalid_prompt" not in codes and "invalid_request_error" not in types:
        return False
    message = _error_message_text(error).casefold()
    return any(marker in message for marker in _INVALID_PROMPT_POLICY_MARKERS)


def _structured_error_outcome(
    body: str | Mapping[str, Any],
    *,
    policy_outcome: ProviderOutcome,
    strict_prompt_flag: bool = False,
) -> ProviderOutcome | None:
    """Classify known structured fields before looking at freeform text."""
    tokens = _error_tokens(body)
    if tokens is None:
        return None
    objects = _error_body_objects(body)
    prompt_flagged = (
        objects is not None if not strict_prompt_flag else _codex_prompt_flagged(body)
    )
    if any(token in _CODEX_CONFIG_CODES for token in tokens):
        return ProviderOutcome.CONFIG_ERROR
    if strict_prompt_flag and prompt_flagged:
        return ProviderOutcome.CONTENT_FLAGGED
    for token in tokens:
        if token in _STRUCTURED_CONTENT_FLAG_CODES:
            if not strict_prompt_flag and prompt_flagged:
                return ProviderOutcome.CONTENT_FLAGGED
            if token == "invalid_prompt" and prompt_flagged:
                return ProviderOutcome.CONTENT_FLAGGED
            if token in _STRUCTURED_POLICY_FLAG_CODES:
                return ProviderOutcome.CONTENT_FLAGGED
            if token in _STRUCTURED_POLICY_REFUSAL_CODES:
                return policy_outcome
            continue
        outcome = _STRUCTURED_CODEX_OUTCOMES.get(token)
        if outcome is not None:
            return outcome
        if token in _STRUCTURED_POLICY_REFUSAL_CODES:
            return policy_outcome
    return None


def _structured_http_outcome(body: str | Mapping[str, Any]) -> ProviderOutcome | None:
    """Map known HTTP error codes without searching serialized JSON text."""
    tokens = _error_tokens(body)
    if tokens is None:
        return None
    return next(
        (
            outcome
            for token in tokens
            if (outcome := _STRUCTURED_HTTP_OUTCOMES.get(token)) is not None
        ),
        None,
    )


def _keyword_codex_outcome(text: str) -> ProviderOutcome:
    """Apply legacy markers only to unstructured/freeform error text."""
    lowered = text.casefold()
    if any(marker in lowered for marker in _RETRYABLE_CODEX_ERROR_MARKERS):
        return ProviderOutcome.ERROR
    if any(marker in lowered for marker in _CONFIG_CODEX_ERROR_MARKERS):
        return ProviderOutcome.CONFIG_ERROR
    if any(marker in lowered for marker in _REFUSAL_CODEX_ERROR_MARKERS):
        return ProviderOutcome.REFUSAL
    return ProviderOutcome.ERROR


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
    ``requests_per_minute`` and ``max_in_flight`` are independent admission
    dimensions; ``rpm`` and ``max_concurrency`` remain accepted aliases for
    older provider files. ``tokens_per_s`` is only a configured hint—measured
    throughput comes from routing.ProviderDebt usage evidence.
    ``throughput_hint_tps`` and ``interactive_wall_budget_s`` are optional
    operator hints used by the interactive frontend when it chooses a turn
    deadline; they never alter provider transport timeouts.
    """

    name: str
    tier: ProviderTier
    base_url: str
    api_key_env: str
    timeout_s: float = 30.0
    max_retries: int = 2
    # ``rpm`` is the legacy transport-rate spelling.  New routing code uses
    # ``requests_per_minute`` so request rate cannot be mistaken for a
    # concurrency limit.  ``__post_init__`` keeps the two values in sync for
    # the transport while accepting either spelling from callers.
    rpm: int = 60
    requests_per_minute: int | None = None
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
    # ``max_concurrency`` is the historical spelling.  ``max_in_flight`` is
    # the independent admission capacity; legacy configurations derive it
    # conservatively from the old field (normally one slot).
    max_concurrency: int = 1
    max_in_flight: int | None = None
    billing_mode: BillingMode = BillingMode.METERED
    quota_windows: tuple[QuotaWindowSpec, ...] = ()
    price_per_1m_cached_in: float = 0.0
    cache_capability: CacheCapability = field(default_factory=CacheCapability)
    pricing_known: bool = False
    throughput_hint_tps: float = 0.0
    # Optional configured throughput hint. Measured throughput is folded from
    # usage events into routing.ProviderDebt and takes precedence over this
    # hint during quality ordering.
    tokens_per_s: float | None = None
    interactive_wall_budget_s: float | None = None
    supports_native_tools: bool = True
    supports_python_tool: bool = True
    allow_model_substitution: bool = False
    # This marker lets routing distinguish an explicitly separated capacity
    # declaration from an rpm-only legacy provider without changing the
    # public dataclass shape or equality of existing provider values.
    _independent_capacity_model: bool = field(default=False, init=False, repr=False, compare=False)

    def __post_init__(self) -> None:
        """Normalize legacy aliases without coupling rate to concurrency.

        A provider file that only declares ``rpm`` remains valid.  Its
        effective in-flight capacity is conservatively one (or the explicitly
        supplied legacy ``max_concurrency``), while a provider that declares
        either new capacity field opts into the independent lane model.
        """
        explicit_rate = self.requests_per_minute is not None
        rate = self.rpm if self.requests_per_minute is None else self.requests_per_minute
        if isinstance(rate, bool) or not isinstance(rate, int) or rate <= 0:
            raise ValueError("requests_per_minute must be a positive integer")
        object.__setattr__(self, "requests_per_minute", rate)
        # The HTTP token bucket still reads ``rpm``.  Make its rate agree with
        # the canonical field when a new-style config is used.
        object.__setattr__(self, "rpm", rate)

        explicit_in_flight = self.max_in_flight is not None
        in_flight = self.max_in_flight
        if in_flight is None:
            legacy = self.max_concurrency
            if isinstance(legacy, bool) or not isinstance(legacy, int) or legacy <= 0:
                raise ValueError("max_in_flight must be a positive integer")
            in_flight = max(1, legacy)
        elif isinstance(in_flight, bool) or not isinstance(in_flight, int) or in_flight <= 0:
            raise ValueError("max_in_flight must be a positive integer")
        object.__setattr__(self, "max_in_flight", in_flight)
        # Keep the old attribute useful to integrations that still inspect it.
        object.__setattr__(self, "max_concurrency", in_flight)

        configured_tps = self.tokens_per_s
        if configured_tps is None:
            configured_tps = self.throughput_hint_tps
        elif (
            isinstance(self.throughput_hint_tps, int | float)
            and not isinstance(self.throughput_hint_tps, bool)
            and self.throughput_hint_tps not in (0, configured_tps)
        ):
            raise ValueError("tokens_per_s and throughput_hint_tps disagree")
        if (
            isinstance(configured_tps, bool)
            or not isinstance(configured_tps, int | float)
            or not math.isfinite(float(configured_tps))
            or configured_tps < 0
        ):
            raise ValueError("tokens_per_s must be a finite non-negative number")
        object.__setattr__(self, "tokens_per_s", float(configured_tps))
        object.__setattr__(self, "throughput_hint_tps", float(configured_tps))
        object.__setattr__(
            self,
            "_independent_capacity_model",
            explicit_rate or explicit_in_flight or self.max_concurrency != 1,
        )


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
    quota_windows: tuple[dict[str, Any], ...] | None = None
    fell_back_from: str | None = None


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
        probe_already_in_flight: bool = False,
        http_status: int | None = None,
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
        if http_status is None:
            match = re.search(r"\bHTTP\s+(\d{3})\b", message, re.IGNORECASE)
            http_status = int(match.group(1)) if match is not None else None
        self.http_status = http_status
        # A stale candidate list can race with another HALF_OPEN probe. This
        # rejection is admission control, not provider health evidence.
        self.probe_already_in_flight = probe_already_in_flight

    @property
    def is_real_death(self) -> bool:
        """Whether this run has terminal evidence that the endpoint is dead."""
        status = self.http_status
        cause: BaseException | None = self.cause
        seen: set[int] = set()
        while cause is not None and id(cause) not in seen:
            seen.add(id(cause))
            if status is None:
                candidate_status = getattr(cause, "status", getattr(cause, "code", None))
                if type(candidate_status) is int:
                    status = candidate_status
            if isinstance(cause, urllib.error.URLError):
                reason = cause.reason
                if isinstance(reason, BaseException):
                    cause = reason
                    continue
            cause = cause.__cause__
        if self.outcome is ProviderOutcome.AUTH_ERROR and status in (401, 403):
            return True
        if type(status) is int and 500 <= status <= 599:
            return True
        if self.outcome is not ProviderOutcome.ERROR:
            return False
        if "malformed response" in self.message.casefold():
            return False
        if _REAL_DEATH_ENDPOINT_RE.search(self.message):
            return True
        cause = self.cause
        seen.clear()
        while cause is not None and id(cause) not in seen:
            seen.add(id(cause))
            if isinstance(cause, ConnectionRefusedError | socket.gaierror | ssl.SSLError):
                return True
            if isinstance(cause, OSError) and getattr(cause, "errno", None) in {
                errno.ECONNREFUSED,
                errno.ENETUNREACH,
                errno.EHOSTUNREACH,
            }:
                return True
            if isinstance(cause, urllib.error.URLError):
                reason = cause.reason
                if isinstance(reason, BaseException):
                    cause = reason
                    continue
            cause = cause.__cause__
        return bool(_REAL_DEATH_TRANSPORT_RE.search(self.message))


def is_real_death(error: BaseException | None) -> bool:
    """Return the narrow endpoint-death verdict for a provider failure."""
    return isinstance(error, ProviderError) and error.is_real_death


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


class _LoopLocal:
    """Keep an asyncio primitive separate for every running event loop.

    ``CambiumLM.forward`` is a synchronous DSPy seam and uses ``asyncio.run``
    when GEPA invokes it.  A single ``Diffundo`` can therefore serve many
    short-lived loops, including loops running concurrently in GEPA worker
    threads.  asyncio synchronization primitives cannot be shared between
    those loops once they have waiters, so the lookup itself is protected by a
    small thread lock while the returned primitive remains loop-local.
    """

    __slots__ = ("_factory", "_guard", "_values")

    def __init__(self, factory: Callable[[], Any]) -> None:
        self._factory = factory
        self._guard = threading.Lock()
        self._values: weakref.WeakKeyDictionary[asyncio.AbstractEventLoop, Any] = (
            weakref.WeakKeyDictionary()
        )

    def get(self) -> Any:
        loop = asyncio.get_running_loop()
        with self._guard:
            primitive = self._values.get(loop)
            if primitive is None:
                primitive = self._factory()
                self._values[loop] = primitive
            return primitive


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
        "auth_quarantine_fingerprint",
    )

    def __init__(self, provider: ProviderConfig, window_size: int) -> None:
        self.provider = provider
        self.health = HealthState.UNKNOWN
        self.cooldown_until = 0.0
        self.open_until = 0.0
        self.bucket = _TokenBucket(provider.rpm)
        self.lock = _LoopLocal(asyncio.Lock)
        self.outcomes: deque[bool] = deque(maxlen=window_size)
        self.probe_in_flight = False
        # An auth failure is quarantined for this credential identity. A
        # replacement credential is allowed to probe the provider again; a
        # config error has no fingerprint and remains disabled.
        self.auth_quarantine_fingerprint: str | None = None


class _PauseState:
    """One loop's dispatch-pause event and recovery monitor state."""

    __slots__ = ("event", "monitor", "waiters")

    def __init__(self) -> None:
        self.event = asyncio.Event()
        self.waiters = 0
        self.monitor: asyncio.Task | None = None


class _PauseTracker:
    """Per-tier pause state, isolated by the loop that owns each Event."""

    __slots__ = ("_state",)

    def __init__(self) -> None:
        self._state = _LoopLocal(_PauseState)

    def current(self) -> _PauseState:
        return cast(_PauseState, self._state.get())


# Providers without structural refusal fields pass refusal-like prose through as valid content.
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
            choice = choices[0]
            if not isinstance(choice, Mapping):
                raise TypeError("choice must be an object")
            if choice.get("finish_reason") == "content_filter":
                raise ProviderError(
                    provider.name, ProviderOutcome.REFUSAL, "provider content filter refusal"
                )
            message = choice["message"]
            if not isinstance(message, Mapping):
                raise TypeError("message must be an object")
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
            usage = self.payload.get("usage")
            if not isinstance(usage, dict):
                usage = None
            else:
                cached_tokens = _cached_tokens(usage)
                usage = dict(usage)
                usage.pop("cached_tokens", None)
                if cached_tokens is not None:
                    usage["cached_tokens"] = cached_tokens
            model = self.payload.get("model") or provider.model
            if not isinstance(model, str):
                raise TypeError("model must be a string")
            return CallResult(
                provider=provider.name,
                model=model,
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
        except ProviderError:
            raise
        except (AttributeError, IndexError, KeyError, TypeError, ValueError) as exc:
            raise ProviderError(
                provider.name,
                ProviderOutcome.ERROR,
                "malformed response: invalid response fields",
            ) from exc


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
        try:
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
        except ProviderError:
            raise
        except (AttributeError, IndexError, KeyError, TypeError, ValueError) as exc:
            raise ProviderError(
                provider.name,
                ProviderOutcome.ERROR,
                "malformed response: invalid response fields",
            ) from exc


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
    values: list[float] = []
    for key in ("prompt_tokens", "completion_tokens"):
        value = usage.get(key, 0)
        if value is None:
            value = 0
        if isinstance(value, bool) or not isinstance(value, int | float):
            raise ValueError(f"usage.{key} must be numeric")
        if not math.isfinite(value) or value < 0:
            raise ValueError(f"usage.{key} must be finite and non-negative")
        values.append(float(value))
    return (
        values[0] / 1_000_000 * provider.price_per_1m_in
        + values[1] / 1_000_000 * provider.price_per_1m_out
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
            raise ValueError(f"tool call arguments are not valid JSON: {raw[:80]!r}") from exc
    elif isinstance(raw, Mapping):
        parsed = dict(raw)
    else:
        raise ValueError(f"tool call arguments must be a JSON object, got {type(raw).__name__}")
    if not isinstance(parsed, dict):
        raise ValueError(f"tool call arguments must be a JSON object, got {type(parsed).__name__}")
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


def _body_retry_after(message: str) -> float | None:
    """Read a bounded reset delay from a provider's structured error body."""
    try:
        payload = json.loads(message)
    except (json.JSONDecodeError, ValueError):
        return None

    pending: list[Any] = [payload]
    while pending:
        value = pending.pop()
        if isinstance(value, Mapping):
            for key in ("retry_after", "retry_after_s", "reset_after", "reset_after_s"):
                candidate = value.get(key)
                if isinstance(candidate, bool):
                    continue
                if isinstance(candidate, int | float):
                    delay = _finite_nonnegative_float(candidate)
                    if delay is not None:
                        return delay
                elif isinstance(candidate, str) and candidate.strip():
                    try:
                        delay = float(candidate)
                    except ValueError:
                        continue
                    if math.isfinite(delay) and delay >= 0:
                        return delay
            for key in ("reset_at", "resetAt", "reset_time", "resetTime"):
                candidate = value.get(key)
                if isinstance(candidate, bool) or not isinstance(candidate, int | float):
                    continue
                reset_at = _finite_nonnegative_float(candidate)
                if reset_at is not None and reset_at > 0:
                    return max(0.0, reset_at - time.time())
            pending.extend(value.values())
        elif isinstance(value, list):
            pending.extend(value)
    return None


def _finite_nonnegative_float(value: Any) -> float | None:
    try:
        parsed = float(value)
    except (TypeError, ValueError, OverflowError):
        return None
    if not math.isfinite(parsed) or parsed < 0:
        return None
    return parsed


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
# permanent config errors (disable the provider, never retry); a narrowly
# evidenced invalid_prompt policy flag is caller-recoverable, while ordinary
# content refusals fall through like any other refusal.
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
    "model not found",
    "not found",
    "unsupported_model",
    "unsupported model",
    "unknown model",
    "unsupported_parameter",
    "unsupported parameter",
    "invalid_parameter",
    "invalid parameter",
)
_CODEX_CONFIG_TEXT_MARKERS = (
    "model_not_found",
    "model not found",
    "not found",
    "model does not exist",
    "unsupported model",
    "unknown model",
    "unsupported parameter",
    "invalid parameter",
)
_REFUSAL_CODEX_ERROR_MARKERS = ("content_policy", "refus")
_ERROR_TOKEN_RE = re.compile(r"[^a-z0-9]+")


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
        normalized["prompt_tokens_details"] = dict(input_details)
        if cached_tokens is not None:
            normalized["prompt_tokens_details"]["cached_tokens"] = cached_tokens
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
    text = json.dumps(error, default=str)
    if _codex_config_400(error):
        outcome = ProviderOutcome.CONFIG_ERROR
    else:
        outcome = _structured_error_outcome(
            error,
            policy_outcome=ProviderOutcome.REFUSAL,
            strict_prompt_flag=True,
        )
    if outcome is None:
        outcome = _keyword_codex_outcome(_error_message_text(error))
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
        data = line[len("data:") :].strip()
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
                stream_error = stream_error or _codex_stream_error(provider, error, access_token)
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
        return (
            {},
            text,
            ProviderError(
                provider.name,
                ProviderOutcome.ERROR,
                "malformed codex stream: no response.completed event",
            ),
        )
    return completed, text, None


def _codex_config_400(message: str | Mapping[str, Any]) -> bool:
    """True when a codex error body names a model/parameter problem.

    Configuration indicators in structured ``code``, ``model``, or ``param``
    fields take precedence over policy-flag evidence. Freeform text retains
    only the established model/parameter phrases.
    """
    tokens = _error_tokens(message)
    if tokens is None:
        return False
    if any(token in _CODEX_CONFIG_CODES for token in tokens):
        return True
    objects = _error_body_objects(message)
    if objects is not None and any(
        isinstance(value, str) and bool(value.strip())
        for item in objects
        for value in (item.get("param"),)
    ):
        return True
    if objects is not None and any(
        isinstance(value, str)
        and any(
            marker in _ERROR_TOKEN_RE.sub("_", value.casefold()).strip("_")
            for marker in _CODEX_CONFIG_FIELD_MARKERS
        )
        for item in objects
        for field_name in ("code", "model", "param")
        for value in (item.get(field_name),)
    ):
        return True
    lowered = _error_message_text(message).casefold()
    return any(marker in lowered for marker in _CODEX_CONFIG_TEXT_MARKERS)


def _classify_http_403(
    provider: ProviderConfig,
    message: str,
    *,
    cause: BaseException | None,
    retry_after_s: float | None,
    account_quota_owner: str | None,
    structured_http: ProviderOutcome | None,
    content_flagged: bool,
) -> ProviderError:
    """Classify the provider-specific meanings carried by HTTP 403."""
    if retry_after_s is None:
        retry_after_s = _body_retry_after(message)
    if _WAF_403_RE.search(message) or _CLOUDFLARE_1010_RE.search(message):
        # A WAF/browser block says nothing about the provider credential. Keep
        # it retryable with bounded backoff.
        return ProviderError(
            provider.name,
            ProviderOutcome.ERROR,
            f"HTTP 403 WAF/network block: {message}",
            cause,
            retry_after_s=retry_after_s,
        )
    lowered = _error_message_text(message).casefold()
    if structured_http is ProviderOutcome.QUOTA or any(
        marker in lowered for marker in _HTTP_403_QUOTA_MARKERS
    ):
        return ProviderError(
            provider.name,
            ProviderOutcome.QUOTA,
            f"HTTP 403 quota/billing: {message}",
            cause,
            retry_after_s=retry_after_s,
            account_quota_owner=account_quota_owner,
        )
    if structured_http is ProviderOutcome.CONFIG_ERROR or any(
        marker in lowered for marker in _HTTP_403_MODEL_MARKERS
    ):
        return ProviderError(
            provider.name,
            ProviderOutcome.CONFIG_ERROR,
            f"HTTP 403 model entitlement: {message}",
            cause,
        )
    if content_flagged:
        return ProviderError(
            provider.name,
            ProviderOutcome.CONTENT_FLAGGED,
            f"HTTP 403 policy/content flag: {message}",
            cause,
        )
    if structured_http is ProviderOutcome.AUTH_ERROR:
        return ProviderError(
            provider.name,
            ProviderOutcome.AUTH_ERROR,
            f"HTTP 403 credential rejected: {message}",
            cause,
        )
    if any(marker in lowered for marker in _HTTP_403_REFUSAL_MARKERS):
        return ProviderError(
            provider.name,
            ProviderOutcome.REFUSAL,
            f"HTTP 403 policy/content refusal: {message}",
            cause,
        )
    if any(marker in lowered for marker in _HTTP_403_AUTH_MARKERS):
        return ProviderError(
            provider.name,
            ProviderOutcome.AUTH_ERROR,
            f"HTTP 403 credential rejected: {message}",
            cause,
        )
    # An unlabelled 403 remains fail-closed as an auth failure. Known
    # provider/WAF, entitlement, quota, and policy shapes above avoid
    # damaging provider health for their respective non-auth causes.
    return ProviderError(
        provider.name, ProviderOutcome.AUTH_ERROR, f"HTTP 403: {message}", cause
    )


# --------------------------------------------------------------------------- #
# Diffundo router
# --------------------------------------------------------------------------- #


class Diffundo:
    """Tiered provider router with a per-subagent primary association (D1).

    Per-instance state is limited to per-provider cooldown timers, circuit
    breaker health, token buckets, terminal-death routing memory, per-tier
    pause events (architecture §8.1/§9, D8f), and the task's primary provider
    association: one worker process runs one task, so the router binds to one
    provider and keeps sending the task's growing context to it, preserving
    per-provider prompt-prefix caching. The association moves only when that
    provider fails and a fallback serves. No attribute is a mutable mapping;
    there is no response store anywhere.
    """

    def __init__(
        self,
        providers: Sequence[ProviderConfig],
        *,
        call_budget_s: float = 180.0,
        pause_timeout_s: float = 0.5,
        breaker_window_size: int = 20,
        breaker_failure_threshold: float = 0.5,
        open_backoff_base: float = 2.0,
        retry_base_delay_s: float = 0.05,
        summary_call_budget_s: float | None = None,
        rotation_seed: int = 0,
        primary_provider: str | None = None,
        credential_source: CredentialSource | None = None,
        codex_profile: Mapping[str, object] | None = None,
        debt: Mapping[str, Any] | None = None,
        task_id: str = "task",
        requirements: Mapping[str, Any] | None = None,
    ) -> None:
        self._providers = tuple(providers)
        self._task_id = task_id
        # Keep task requirements immutable on the router. Validation happens
        # at the routing boundary so malformed requirements fail before any
        # provider transport is attempted.
        self._requirements: tuple[tuple[str, Any], ...] = (
            tuple(requirements.items()) if requirements else ()
        )
        self._provider_lease: ProviderLease | None = None
        self._quota_ledger = (
            QuotaLedger() if any(provider.quota_windows for provider in self._providers) else None
        )
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
        self._pinned_provider = self._primary_provider
        self._fallback_origin: str | None = None
        self._active_tier: ProviderTier | None = None
        # Endpoint-death evidence is stronger than ordinary cooldown state for
        # routing order. Keep it local to this router/process: a fresh Diffundo
        # instance is the explicit recovery/probe boundary.
        self._terminal_death_providers: frozenset[str] = frozenset()
        self._call_budget_s = call_budget_s
        if summary_call_budget_s is None:
            self._summary_call_budget_s = max(
                DEFAULT_SUMMARY_CALL_BUDGET_S,
                float(call_budget_s) * 2.0,
            )
        elif (
            isinstance(summary_call_budget_s, bool)
            or not isinstance(summary_call_budget_s, int | float)
            or not math.isfinite(float(summary_call_budget_s))
            or summary_call_budget_s <= 0
        ):
            raise ValueError("summary_call_budget_s must be a finite positive number")
        else:
            self._summary_call_budget_s = float(summary_call_budget_s)
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
        self._debt: tuple[tuple[str, Any], ...] | None = tuple(debt.items()) if debt else None

    # -- public API --------------------------------------------------------- #

    async def call(
        self,
        tier: ProviderTier,
        prompt: dict[str, Any],
        *,
        model: str | None = None,
        budget_usd: float | None = None,
        allow_model_substitution: bool = False,
        requirements: Mapping[str, Any] | None = None,
        call_budget_s: float | None = None,
    ) -> CallResult:
        """Ordered cascade over tier-matching providers (arch §9.2).

        Falls through on timeout/error/quota/refusal/content-flagged outcomes;
        content flags remain request-level signals for the caller and do not
        alter provider health. Providers in cooldown, OPEN, DISABLED, or
        rate-limited are skipped by the selection filter.
        A pinned ``model`` is strict unless this request explicitly sets
        ``allow_model_substitution``; provider configuration alone never
        authorizes a task to switch models. When every tier provider is
        unavailable the dispatch pauses on an ``asyncio.Event`` and a recovery
        monitor wakes it (D8f); if nothing recovers within the bounded pause
        window, raises ``AllProvidersFailed``.
        """
        validate_prompt_structure(prompt)
        effective_call_budget_s = self._call_budget_s if call_budget_s is None else call_budget_s
        if (
            isinstance(effective_call_budget_s, bool)
            or not isinstance(effective_call_budget_s, int | float)
            or not math.isfinite(float(effective_call_budget_s))
            or effective_call_budget_s <= 0
        ):
            raise ValueError("call_budget_s must be a finite positive number")
        routing_model = None if self._fallback_origin is not None else model
        request = self._routing_request(
            prompt,
            routing_model,
            allow_model_substitution=allow_model_substitution,
            requirements=requirements,
        )
        deadline = time.monotonic() + float(effective_call_budget_s)
        tried: list[str] = []
        last_error: BaseException | None = None
        selection_tier = self._active_tier or tier
        selection_model = None if self._fallback_origin is not None else model
        fallback_triggered = False
        fallback_origin: str | None = None
        while True:
            (
                candidates,
                selection_model,
                request,
                terminal_origin,
            ) = await self._await_candidates_with_fallback(
                selection_tier,
                selection_model,
                deadline,
                allow_model_substitution=allow_model_substitution,
                request=request,
            )
            fallback_origin = terminal_origin or fallback_origin
            fallback_triggered = fallback_triggered or terminal_origin is not None
            if not candidates:
                raise AllProvidersFailed(tried, last_error)
            probe_rejected = False
            pending = list(candidates)
            while pending:
                provider = pending.pop(0)
                try:
                    result = await self._attempt(provider, prompt, deadline=deadline)
                except ProviderError as exc:
                    if exc.probe_already_in_flight:
                        probe_rejected = True
                        continue
                    if exc.is_real_death:
                        self._terminal_death_providers = self._terminal_death_providers | {
                            provider.name
                        }
                    lease = self._provider_lease
                    if (
                        lease is not None
                        and lease.provider == provider.name
                        and lease.model == provider.model
                        and (
                            exc.is_real_death
                            or (
                                exc.outcome is ProviderOutcome.TIMEOUT
                                and provider.name == self._pinned_provider
                            )
                        )
                    ):
                        # A lease keeps a healthy incumbent sticky, but a
                        # terminally dead holder no longer owns the semantic
                        # branch. Release only this lease state; the
                        # pinned/fallback history remains needed for
                        # provenance and dead-provider avoidance.
                        self._provider_lease = None
                    tried.append(provider.name)
                    last_error = exc
                    pinned_fallback = (
                        self._pinned_provider is not None
                        and provider.name == self._pinned_provider
                        and (exc.is_real_death or exc.outcome is ProviderOutcome.TIMEOUT)
                    )
                    if pinned_fallback:
                        fallback_origin = self._pinned_provider
                        existing = {item.name for item in pending}
                        if existing:
                            fallback_triggered = True
                        fallback_candidates = self._real_death_fallback_candidates(
                            selection_tier,
                            request=request,
                            excluded={*tried, *existing},
                        )
                        if fallback_candidates:
                            pending.extend(fallback_candidates)
                            fallback_triggered = True
                    if exc.budget_exhausted and not fallback_triggered:
                        raise AllProvidersFailed(tried, last_error) from exc
                    continue
                if budget_usd is not None and result.estimated_cost_usd > budget_usd:
                    raise CostBudgetExceeded(result.provider, result.estimated_cost_usd, budget_usd)
                self._primary_provider = provider.name
                if fallback_triggered:
                    self._fallback_origin = fallback_origin
                    self._active_tier = provider.tier
                if self._fallback_origin is not None:
                    result = replace(result, fell_back_from=self._fallback_origin)
                return result
            if probe_rejected and not tried:
                continue
            raise AllProvidersFailed(tried, last_error)

    async def summary_call(
        self,
        tier: ProviderTier,
        prompt: dict[str, Any],
        *,
        model: str | None = None,
        budget_usd: float | None = None,
        allow_model_substitution: bool = False,
        requirements: Mapping[str, Any] | None = None,
    ) -> CallResult:
        """Run a semantic summary with extra provider response headroom.

        Summary prompts contain the complete raw execution tail and the model
        must produce a structured entry, so they can take materially longer
        than the short action calls that precede them.  This remains the same
        router (and therefore the same provider lease/cascade); only its
        bounded transport deadline is extended for the summary call.
        """
        return await self.call(
            tier,
            prompt,
            model=model,
            budget_usd=budget_usd,
            allow_model_substitution=allow_model_substitution,
            requirements=requirements,
            call_budget_s=self._summary_call_budget_s,
        )

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
        if not provider.enabled:
            return ProviderStatus.DISABLED
        if runtime.health is HealthState.DISABLED:
            self._release_auth_quarantine(runtime)
            if runtime.health is HealthState.DISABLED:
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

    @property
    def provider_lease(self) -> ProviderLease | None:
        """Current strict semantic-branch lease, if the first call has succeeded."""

        return self._provider_lease

    def bind_provider(
        self,
        provider: str,
        model: str,
        *,
        root_task_id: str = "task",
        cache_identity: str = "",
        lease: ProviderLease | None = None,
    ) -> None:
        """Pin every later call on this router to one provider/model branch.

        ``lease`` is accepted for child/context inheritance. Reusing the
        immutable object preserves its root and cache identity instead of
        silently creating a fresh cache namespace for the child.
        """

        if lease is not None:
            if lease.provider != provider or lease.model != model:
                raise ValueError("inherited provider lease does not match provider/model")
            root_task_id = lease.root_task_id
            cache_identity = lease.cache_identity
        if not provider or not model:
            raise ValueError("provider lease requires provider and model")
        existing = self._provider_lease
        if existing is not None:
            if existing.provider != provider or existing.model != model:
                raise RuntimeError(
                    "provider continuity violation: attempted to move a live semantic branch"
                )
            # Existing ownership is authoritative. In particular, an
            # inherited non-empty cache identity must never be replaced by a
            # later bind with the child's default empty identity.
            return
        configured = next(
            (
                item
                for item in self._providers
                if item.name == provider and item.model == model and item.enabled
            ),
            None,
        )
        if configured is None:
            raise ValueError("provider lease does not match an enabled configured lane")
        if self._pinned_provider is None:
            # A caller that binds after an unassigned first call still makes
            # this incumbent the origin for terminal-death fallback.
            self._pinned_provider = provider
        self._provider_lease = (
            lease
            if lease is not None
            else ProviderLease(
                provider,
                model,
                root_task_id,
                cache_identity=cache_identity,
            )
        )

    def clear_provider_lease(self) -> None:
        """Clear task-local state when a warm worker is rebound to another task."""

        self._provider_lease = None
        self._primary_provider = None
        self._pinned_provider = None
        self._fallback_origin = None
        self._active_tier = None
        self._terminal_death_providers = frozenset()

    def _routing_request(
        self,
        prompt: Mapping[str, Any],
        model: str | None,
        *,
        allow_model_substitution: bool,
        requirements: Mapping[str, Any] | None,
    ) -> Any:
        """Build the immutable hard-admission request for one live call.

        The pure request predicates live in :mod:`cambium.routing`; importing
        them lazily avoids a module cycle because routing names ProviderTier.
        Tool-bearing prompts require native tool support unless the task
        explicitly opts out, while task requirements supply the remaining
        context, billing, and quality boundaries.
        """
        from .routing import RoutingRequest, validate_requirements

        raw = dict(requirements) if requirements is not None else dict(self._requirements)
        validated = validate_requirements(raw)
        has_native_tools = isinstance(prompt.get("tools"), list) and bool(prompt["tools"])
        return RoutingRequest(
            model=model or "",
            required_context_tokens=validated.get("min_context_window", 0),
            needs_native_tools=validated.get("needs_native_tools", has_native_tools),
            needs_python_tool=validated.get("needs_python_tool", False),
            allow_model_substitution=allow_model_substitution,
            allow_paid=validated.get("allow_paid", True),
            allow_free=validated.get("allow_free", True),
            quality=validated.get("quality"),
            lease=self._provider_lease,
        )

    def _candidates(
        self,
        tier: ProviderTier,
        model: str | None,
        allow_model_substitution: bool = False,
        *,
        request: Any | None = None,
    ) -> list[ProviderConfig]:
        candidates = list(self._candidates_unleased(tier, model, request=request))
        lease = self._provider_lease
        if lease is not None:
            candidates = [
                provider
                for provider in candidates
                if provider.name == lease.provider and provider.model == lease.model
            ]
        elif self._fallback_origin is not None:
            candidates = [
                provider for provider in candidates if provider.name != self._fallback_origin
            ]
        requested_model = model
        if isinstance(requested_model, str) and requested_model:
            exact = [provider for provider in candidates if provider.model == requested_model]
            substitutes = [
                provider
                for provider in candidates
                if allow_model_substitution
                and provider.model != requested_model
                and provider.allow_model_substitution
            ]
            candidates = [*exact, *substitutes]
        return candidates

    def _candidates_unleased(
        self,
        tier: ProviderTier,
        model: str | None,
        *,
        request: Any | None = None,
    ) -> list[ProviderConfig]:
        """Tier-matching, capability-filtered, health/bucket-eligible providers,
        sorted by priority ascending, refined by measured quality within an
        equal-priority run (arch §9.1/§9.2 step 1, weighted routing).

        When ``model`` is pinned, providers in the same tier that declare a
        different model are kept as ordered fallback candidates behind the
        strict model matches. ``_candidates`` admits those substitutes only
        when the request explicitly authorizes substitution and the sibling's
        provider-global opt-in is true. If no provider declares the pinned
        model, the pin is a configuration error and no candidate is returned.
        """
        now = time.monotonic()
        eligible: list[ProviderConfig] = []
        model_declared = model is None
        provider_satisfies_request = None
        if request is not None:
            from .routing import provider_satisfies_request as satisfies_request

            provider_satisfies_request = satisfies_request
        for runtime in self._runtimes:
            provider = runtime.provider
            if provider.tier is not tier or not provider.enabled:
                continue
            if model is not None and provider.model == model:
                model_declared = True
            if (
                request is not None
                and provider_satisfies_request is not None
                and not provider_satisfies_request(provider, request)
            ):
                continue
            if runtime.health is HealthState.DISABLED:
                self._release_auth_quarantine(runtime)
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
        live = [
            provider for provider in eligible if provider.name not in self._terminal_death_providers
        ]
        if live:
            eligible = live
        if model is None:
            return self._order_candidates(eligible)
        # A terminally-dead lane must never anchor a pinned model partition:
        # a 404-dead strict match would keep winning selection (and burning
        # the call) even though its death is already proven. Drop dead lanes
        # from the strict set so an empty strict list triggers exactly-pinned
        # relaxation instead of re-dialing a corpse.
        strict_live = [
            provider for provider in eligible if provider.name not in self._terminal_death_providers
        ]
        strict = [provider for provider in strict_live if provider.model == model]
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

    def _real_death_fallback_candidates(
        self,
        tier: ProviderTier,
        *,
        request: Any,
        excluded: set[str],
    ) -> list[ProviderConfig]:
        """Return enabled substitutes, same tier first, then other tiers."""
        tiers = [tier, *(candidate for candidate in ProviderTier if candidate is not tier)]
        result: list[ProviderConfig] = []
        seen = set(excluded)
        candidate_request = self._relaxed_request(request)
        for candidate_tier in tiers:
            for provider in self._candidates_unleased(
                candidate_tier, None, request=candidate_request
            ):
                if provider.name in seen:
                    continue
                seen.add(provider.name)
                result.append(provider)
        return result

    def _terminal_pin_origin(
        self,
        tier: ProviderTier,
        model: str | None,
        request: Any | None,
    ) -> str | None:
        """Return a dead strict lane's provider when model relaxation is safe."""
        if model is None:
            return None
        pinned = [
            provider
            for provider in self._providers
            if provider.tier is tier and provider.enabled and provider.model == model
        ]
        if request is not None:
            from .routing import provider_satisfies_request

            pinned = [
                provider for provider in pinned if provider_satisfies_request(provider, request)
            ]
        if not pinned or not all(
            provider.name in self._terminal_death_providers for provider in pinned
        ):
            return None
        names = {provider.name for provider in pinned}
        if self._pinned_provider in names:
            return self._pinned_provider
        return pinned[0].name

    @staticmethod
    def _relaxed_request(request: Any | None) -> Any | None:
        if request is None:
            return None
        try:
            return replace(request, model="", allow_model_substitution=True, lease=None)
        except TypeError:
            return request

    async def _await_candidates_with_fallback(
        self,
        tier: ProviderTier,
        model: str | None,
        deadline: float,
        *,
        allow_model_substitution: bool,
        request: Any | None,
    ) -> tuple[list[ProviderConfig], str | None, Any | None, str | None]:
        candidates = await self._await_candidates(
            tier,
            model,
            deadline,
            allow_model_substitution=allow_model_substitution,
            request=request,
        )
        if candidates or model is None:
            return candidates, model, request, None
        origin = self._terminal_pin_origin(tier, model, request)
        if origin is None:
            return candidates, model, request, None
        relaxed_request = self._relaxed_request(request)
        candidates = await self._await_candidates(
            tier,
            None,
            deadline,
            allow_model_substitution=allow_model_substitution,
            request=relaxed_request,
        )
        return candidates, None, relaxed_request, origin

    async def _await_candidates(
        self,
        tier: ProviderTier,
        model: str | None,
        deadline: float,
        *,
        allow_model_substitution: bool = False,
        request: Any | None = None,
    ) -> list[ProviderConfig]:
        """Return candidates, pausing on exhaustion (D8f) until a provider
        recovers or the pause window / deadline is spent."""
        paused_total = 0.0
        while True:
            now = time.monotonic()
            if now >= deadline:
                return []
            candidates = self._candidates(
                tier,
                model,
                allow_model_substitution=allow_model_substitution,
                request=request,
            )
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
        if not provider.enabled:
            return False
        if runtime.health is HealthState.DISABLED:
            self._release_auth_quarantine(runtime)
            if runtime.health is HealthState.DISABLED:
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
        state = self._pauses[self._tier_index(tier)].current()
        state.waiters += 1
        try:
            if state.monitor is None or state.monitor.done():
                state.monitor = asyncio.create_task(self._recovery_monitor(tier))
            try:
                state.event.clear()
                await asyncio.wait_for(state.event.wait(), timeout=max(0.0, max_wait))
            except TimeoutError:
                pass
        finally:
            state.waiters -= 1
            if state.waiters == 0:
                state.event.clear()
                monitor = state.monitor
                if monitor is not None and not monitor.done():
                    monitor.cancel()
                state.monitor = None
        return time.monotonic() - start

    async def _recovery_monitor(self, tier: ProviderTier) -> None:
        """Wake the paused dispatch of ``tier`` when any provider recovers."""
        while True:
            state = self._pauses[self._tier_index(tier)].current()
            if state.waiters == 0:
                return
            if any(
                self._is_available(runtime.provider.name)
                for runtime in self._runtimes
                if runtime.provider.tier is tier and runtime.provider.enabled
            ):
                state.event.set()
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
        policy = provider
        ledger = self._quota_ledger
        reservation = None
        estimated_tokens = 0
        if ledger is not None and policy.quota_windows:
            messages = prompt.get("messages", []) if isinstance(prompt, dict) else []
            estimated_tokens = max(
                1,
                sum(len(str(message.get("content", "")).encode("utf-8")) for message in messages)
                // 4
                + 4096,
            )
            reserve_task = asyncio.create_task(
                asyncio.to_thread(
                    ledger.reserve, policy.name, policy.quota_windows, estimated_tokens
                )
            )
            try:
                # ``to_thread`` cannot be stopped once the SQLite transaction has
                # started. Shield the worker so cancellation can still clean up a
                # reservation that commits after the caller is cancelled.
                reservation = await asyncio.shield(reserve_task)
            except asyncio.CancelledError:
                try:
                    reservation = await asyncio.shield(reserve_task)
                except BaseException:
                    reservation = None
                if reservation is not None:
                    await asyncio.to_thread(ledger.reconcile, reservation, policy.quota_windows, 0)
                raise
            if reservation is None:
                raise ProviderError(
                    policy.name,
                    ProviderOutcome.QUOTA,
                    "configured subscription quota window is exhausted",
                )
        try:
            result = await self._quota_wrapped_attempt(provider, prompt, deadline=deadline)
        except BaseException:
            if reservation is not None and ledger is not None:
                await asyncio.to_thread(ledger.reconcile, reservation, policy.quota_windows, 0)
            raise
        if reservation is not None and ledger is not None:
            usage = result.usage if isinstance(result.usage, dict) else {}
            total = usage.get("total_tokens")
            if isinstance(total, bool) or not isinstance(total, int | float) or total < 0:
                total = estimated_tokens
            await asyncio.to_thread(ledger.reconcile, reservation, policy.quota_windows, int(total))
            snapshots = await asyncio.to_thread(ledger.snapshots, policy.name)
            result = replace(
                result,
                quota_windows=tuple(quota_snapshot_json(snapshot) for snapshot in snapshots),
            )
        return result

    async def _quota_wrapped_attempt(
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
        disable it, and refusals/content flags leave it untouched. A content
        flag is not retried here; the caller owns its one context-transform
        retry before normal cascade failover.

        When ``deadline`` is given it bounds the whole attempt: the per-attempt
        HTTP timeout is capped at the remaining budget, retry backoff is skipped
        when it no longer fits, and a spent budget raises a ``budget_exhausted``
        ``ProviderError`` so the cascade aborts (cascade-design §2.2).
        """
        runtime = self._runtime(provider.name)
        async with runtime.lock.get():
            probing = runtime.health in (HealthState.HALF_OPEN, HealthState.COOLDOWN)
            if probing and runtime.probe_in_flight:
                raise ProviderError(
                    provider.name,
                    ProviderOutcome.ERROR,
                    "probe already in flight",
                    probe_already_in_flight=True,
                )
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
                        if last_exc is not None:
                            # A real provider response was observed before the
                            # budget ran out; preserve its outcome and reset
                            # evidence (e.g. Retry-After) for scheduler use.
                            raise ProviderError(
                                provider.name,
                                last_exc.outcome,
                                "call budget exhausted",
                                last_exc.cause,
                                budget_exhausted=True,
                                retry_after_s=last_retry_after,
                                account_quota_owner=last_quota_owner,
                            ) from last_exc
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
                        raw = await self._post_with_deadline(
                            provider,
                            prompt,
                            timeout_s=timeout_s,
                            deadline=deadline,
                        )
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
                        if exc.outcome is ProviderOutcome.CONTENT_FLAGGED:
                            # The caller may shrink/transform the context once;
                            # this outcome is request-level and must not alter
                            # provider health or consume retry backoff.
                            break
                        if exc.outcome is ProviderOutcome.REFUSAL:
                            break
                        if exc.outcome in (
                            ProviderOutcome.AUTH_ERROR,
                            ProviderOutcome.CONFIG_ERROR,
                        ):
                            self._record_disable(
                                provider,
                                auth_quarantine=exc.outcome is ProviderOutcome.AUTH_ERROR,
                            )
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
                    return replace(result, request_rate_status=request_rate_status)
                assert last_exc is not None
                if last_exc.outcome in (
                    ProviderOutcome.CONTENT_FLAGGED,
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
                request_rate_status = self._record_failure(
                    provider, retry_after_s=last_exc.retry_after_s
                )
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
        return random.uniform(0.0, self._retry_base_delay_s * (2.0**attempt_no))

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

    @staticmethod
    def _consume_post_task(task: asyncio.Task[Any]) -> None:
        """Retrieve a timed-out worker task's eventual exception.

        ``asyncio.to_thread`` cannot interrupt a blocking urllib call. The
        router must still return at its wall deadline, so the timed-out task is
        allowed to finish in the executor and its result is consumed here.
        """
        if task.cancelled():
            return
        try:
            task.exception()
        except BaseException:
            # Retrieving the exception is best-effort cleanup only.
            pass

    async def _post_with_deadline(
        self,
        provider: ProviderConfig,
        prompt: dict[str, Any],
        *,
        timeout_s: float,
        deadline: float | None,
    ) -> _RawResponse:
        """Run one threaded HTTP operation under the call's wall deadline."""
        if deadline is None:
            return await self._post(provider, prompt, timeout_s=timeout_s)
        remaining = self._remaining(deadline)
        if remaining is None or remaining <= 0:
            raise ProviderError(
                provider.name,
                ProviderOutcome.TIMEOUT,
                "call budget exhausted",
                budget_exhausted=True,
            )
        post_task = asyncio.create_task(self._post(provider, prompt, timeout_s=timeout_s))
        try:
            # Shield the task so wait_for returns at the deadline even though
            # cancellation cannot stop the underlying executor thread.
            result = await asyncio.wait_for(asyncio.shield(post_task), timeout=remaining)
        except TimeoutError as exc:
            post_task.add_done_callback(self._consume_post_task)
            raise ProviderError(
                provider.name,
                ProviderOutcome.TIMEOUT,
                "call budget exhausted",
                exc,
                budget_exhausted=True,
            ) from exc
        remaining = self._remaining(deadline)
        if remaining is not None and remaining <= 0:
            raise ProviderError(
                provider.name,
                ProviderOutcome.TIMEOUT,
                "call budget exhausted",
                budget_exhausted=True,
            )
        return result

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
        if scheme not in ("http", "https"):
            raise ProviderError(
                provider.name,
                ProviderOutcome.AUTH_ERROR,
                "provider URL scheme must be http or https",
            )
        if scheme == "http" and not is_loopback_host(parsed.hostname or ""):
            raise ProviderError(
                provider.name,
                ProviderOutcome.AUTH_ERROR,
                "http transport is allowed only for loopback hosts; remote providers require https",
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
        if isinstance(body.get("tools"), list):
            # Internal tool schemas carry {name, description, parameters}; the
            # chat-completions wire format requires the function wrapper. The
            # codex responses path has its own converter (_codex_tools); this
            # is its chat counterpart.
            wire_tools: list[dict[str, Any]] = []
            for tool in body["tools"]:
                if not isinstance(tool, dict) or not isinstance(tool.get("name"), str):
                    continue
                wire_tools.append(
                    {
                        "type": "function",
                        "function": {
                            "name": tool["name"],
                            "description": tool.get("description", ""),
                            "parameters": tool.get(
                                "parameters", {"type": "object", "properties": {}}
                            ),
                        },
                    }
                )
            body["tools"] = wire_tools
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
            http_cause = _SanitizedHTTPError(status, _redact_error_text(str(exc.reason), api_key))
            http_error = self._classify_http(
                provider,
                status,
                safe_body,
                cause=http_cause,
                retry_after_s=(_parse_retry_after(exc.headers) if status in (403, 429) else None),
                account_quota_owner=_account_quota_owner(error_body, api_key),
            )
        except urllib.error.URLError as exc:
            reason = exc.reason
            if isinstance(reason, TimeoutError):
                outcome = ProviderOutcome.TIMEOUT
            else:
                outcome = ProviderOutcome.ERROR
            raise ProviderError(provider.name, outcome, f"transport error: {reason}", exc) from exc
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
                "http transport is allowed only for loopback hosts; remote providers require https",
            )
        credential = self._credential_source
        if credential is None:
            raise ProviderError(
                provider.name,
                ProviderOutcome.AUTH_ERROR,
                "provider requires auth 'codex_chatgpt' but no credential source is injected",
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
                retry_after_s=(_parse_retry_after(exc.headers) if status in (403, 429) else None),
                account_quota_owner=_account_quota_owner(error_body, access_token),
            )
        except urllib.error.URLError as exc:
            reason = exc.reason
            if isinstance(reason, TimeoutError):
                outcome = ProviderOutcome.TIMEOUT
            else:
                outcome = ProviderOutcome.ERROR
            raise ProviderError(provider.name, outcome, f"transport error: {reason}", exc) from exc
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
        config_400 = (
            status == 400
            and provider.protocol is Protocol.CODEX_RESPONSES
            and _codex_config_400(message)
        )
        structured = _structured_error_outcome(
            message,
            policy_outcome=ProviderOutcome.CONTENT_FLAGGED,
            strict_prompt_flag=True,
        )
        structured_http = _structured_http_outcome(message)
        content_flagged = structured is ProviderOutcome.CONTENT_FLAGGED
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
        if status in (401, 429) and content_flagged:
            return ProviderError(
                provider.name,
                ProviderOutcome.CONTENT_FLAGGED,
                f"HTTP {status} policy/content flag: {message}",
                cause,
            )
        if status == 429:
            if retry_after_s is None:
                retry_after_s = _body_retry_after(message)
            return ProviderError(
                provider.name,
                ProviderOutcome.QUOTA,
                f"HTTP 429: {message}",
                cause,
                retry_after_s=retry_after_s,
                account_quota_owner=account_quota_owner,
            )
        if status == 403:
            return _classify_http_403(
                provider,
                message,
                cause=cause,
                retry_after_s=retry_after_s,
                account_quota_owner=account_quota_owner,
                structured_http=structured_http,
                content_flagged=content_flagged,
            )
        if status in (401, 403):
            return ProviderError(
                provider.name, ProviderOutcome.AUTH_ERROR, f"HTTP {status}: {message}", cause
            )
        if status == 400:
            if config_400:
                return ProviderError(
                    provider.name,
                    ProviderOutcome.CONFIG_ERROR,
                    f"HTTP 400: {message}",
                    cause,
                )
            if content_flagged:
                return ProviderError(
                    provider.name,
                    ProviderOutcome.CONTENT_FLAGGED,
                    f"HTTP 400 prompt/content flag: {message}",
                    cause,
                )
            # Codex split (review requirement): a machine-readable 400 naming a
            # model/parameter problem is a permanent CONFIG error that
            # quarantines the provider, NOT a content refusal. The generic
            # all-400 -> REFUSAL rule below is unchanged for chat_completions
            # providers.
            # Deterministic HTTP 400s are permanent request-level rejections
            # (verified live: zai 1214 'messages illegal' was retried then
            # cooled down). A generic 400 used to fall to the retryable ERROR
            # class; classify it as REFUSAL so it is never retried and never
            # drives a health transition.
            return ProviderError(
                provider.name, ProviderOutcome.REFUSAL, f"HTTP 400: {message}", cause
            )
        if content_flagged:
            return ProviderError(
                provider.name,
                ProviderOutcome.CONTENT_FLAGGED,
                f"HTTP {status} prompt/content flag: {message}",
                cause,
            )
        return ProviderError(
            provider.name, ProviderOutcome.ERROR, f"HTTP {status}: {message}", cause
        )

    # -- health bookkeeping -------------------------------------------------- #

    def _record_success(self, provider: ProviderConfig) -> str:
        """Record one success; returns the provider's request-rate status."""
        runtime = self._runtime(provider.name)
        runtime.outcomes.append(True)
        runtime.auth_quarantine_fingerprint = None
        if runtime.health in (HealthState.UNKNOWN, HealthState.COOLDOWN, HealthState.HALF_OPEN):
            runtime.health = HealthState.HEALTHY
        return self.status(provider.name).value

    def _record_failure(
        self, provider: ProviderConfig, *, retry_after_s: float | None = None
    ) -> str:
        """Record one failure; returns the provider's request-rate status."""
        runtime = self._runtime(provider.name)
        runtime.outcomes.append(False)
        now = time.monotonic()
        cooldown_s = provider.cooldown_s
        if retry_after_s is not None and math.isfinite(retry_after_s) and retry_after_s >= 0:
            # A provider reset is stronger evidence than the local default;
            # do not probe a quota/billing failure before that reset.
            cooldown_s = max(cooldown_s, retry_after_s)
        if runtime.health in (HealthState.UNKNOWN, HealthState.HEALTHY):
            runtime.health = HealthState.COOLDOWN
            runtime.cooldown_until = now + cooldown_s
        elif runtime.health in (HealthState.COOLDOWN, HealthState.HALF_OPEN):
            # PRIMARY OPEN trip: a failed probe. A provider in COOLDOWN is only
            # ever dispatched as one probe once its cooldown elapsed; HALF_OPEN
            # is itself a probe state. A failure here means persistence -> OPEN.
            runtime.health = HealthState.OPEN
            runtime.open_until = now + cooldown_s * self._open_backoff_base
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
            runtime.open_until = now + cooldown_s * self._open_backoff_base
        return self.status(provider.name).value

    def _record_disable(self, provider: ProviderConfig, *, auth_quarantine: bool = False) -> None:
        runtime = self._runtime(provider.name)
        runtime.health = HealthState.DISABLED
        runtime.auth_quarantine_fingerprint = (
            self._credential_fingerprint(provider) if auth_quarantine else None
        )

    # -- helpers ------------------------------------------------------------- #

    def _credential_fingerprint(self, provider: ProviderConfig) -> str:
        """Return a non-secret identity for the credential currently in use."""
        if provider.protocol is Protocol.CODEX_RESPONSES:
            credential = self._credential_source
            if credential is None:
                material = b"codex:missing"
            else:
                material = (
                    b"codex:"
                    + credential.access_token.encode("utf-8")
                    + b"\0"
                    + (credential.account_id or "").encode("utf-8")
                )
        else:
            api_key = os.environ.get(provider.api_key_env)
            material = (
                b"api-key:missing" if api_key is None else b"api-key:" + api_key.encode("utf-8")
            )
        return hashlib.sha256(material).hexdigest()

    def _release_auth_quarantine(self, runtime: _ProviderRuntime) -> None:
        """Re-admit a disabled provider only after its credential changes."""
        fingerprint = runtime.auth_quarantine_fingerprint
        if fingerprint is None or self._credential_fingerprint(runtime.provider) == fingerprint:
            return
        runtime.auth_quarantine_fingerprint = None
        runtime.health = HealthState.UNKNOWN
        runtime.cooldown_until = 0.0
        runtime.open_until = 0.0
        runtime.probe_in_flight = False

    def _runtime(self, name: str) -> _ProviderRuntime:
        for runtime in self._runtimes:
            if runtime.provider.name == name:
                return runtime
        raise KeyError(f"unknown provider: {name!r}")
