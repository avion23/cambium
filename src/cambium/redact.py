"""Deterministic redaction for text, event payloads, and worker environments.

Redaction is deliberately conservative about *names* and deliberate about
*values*.  A value with an unambiguous provider shape is scrubbed wherever it
appears.  A short or punctuation-heavy value is scrubbed when it follows a
secret-bearing field or header name.  Bare hashes, commit identifiers, token
counts, signatures, and author metadata stay intact.

The module has no I/O and does not stringify arbitrary objects.  Structured
redaction walks mappings and sequences, preserves ordinary scalar objects,
does not mutate its input, and keeps the common mapping/list cycles finite.
Worker environment construction is an allowlist operation: the default is the
small non-secret runtime set, and named provider variables are added only when
the caller names them explicitly.
"""

from __future__ import annotations

import re
from collections.abc import Iterable, Mapping, Sequence
from enum import Enum
from typing import Any

__all__ = [
    "DEFAULT_PATTERNS",
    "NON_SECRET_BASICS",
    "REDACT_KEYS",
    "REDACT_VALUES",
    "Redactor",
    "build_worker_env",
    "is_secret_name",
]

_DEFAULT_REPLACEMENT = "***"

# Keep this expression useful to callers that need the architecture's named
# key rule, but use _secret_name_kind below for the intentional metric and
# metadata exceptions.  In particular, ``author`` must not match ``auth``.
REDACT_KEYS = re.compile(
    r"(?i)(?<![a-z0-9])(?:api[_-]?key|token|secret|password|passwd|"
    r"passphrase|credential|credentials|authorization|proxy-authorization|"
    r"auth|cookie|set-cookie|session|sessionid|sid|csrf|xsrf|jwt|oauth|"
    r"bearer|private[_-]?key|access[_-]?key|client[_-]?secret)(?![a-z0-9])"
)

# The value patterns are intentionally shape-based.  Do not add a generic
# long-hex/base64 expression: that would turn public git SHAs and metric
# signatures into ``***``.  Ambiguous values are handled by contextual fields.
_LITERAL_SOURCES: tuple[str, ...] = (
    # OpenAI, Anthropic, OpenRouter, and compatible ``sk-`` credentials.
    r"(?<![A-Za-z0-9_-])sk-[A-Za-z0-9_-]{20,}(?![A-Za-z0-9_-])",
    # NVIDIA NIM.
    r"(?<![A-Za-z0-9_-])nvapi-[A-Za-z0-9_-]{16,}(?![A-Za-z0-9_-])",
    # Google API keys.
    r"(?<![A-Za-z0-9_-])AIza[A-Za-z0-9_-]{35,}(?![A-Za-z0-9_-])",
    # GitHub classic and fine-grained personal access tokens.
    r"(?<![A-Za-z0-9_])ghp_[A-Za-z0-9]{36,}(?![A-Za-z0-9_])",
    r"(?<![A-Za-z0-9_])github_pat_[A-Za-z0-9_]{82,}(?![A-Za-z0-9_])",
    # AWS access-key IDs.  The length is exact; a longer value is handled by
    # a contextual name rather than by leaking its unmatched suffix.
    r"(?<![A-Za-z0-9_])A(?:KIA|SIA)[0-9A-Z]{16}(?![0-9A-Z])",
    # Other provider prefixes seen in OpenAI-compatible tooling.
    r"(?<![A-Za-z0-9_-])(?:gsk_|xai-|pplx-|r8_|hf_|xoxb-|xapp-)[A-Za-z0-9_-]{20,}"
    r"(?![A-Za-z0-9_-])",
    # Opaque authorization credentials.  Short values are caught by the
    # Authorization header/context rule below; the standalone threshold keeps
    # prose such as "Bearer bonds" and "Basic algebra" intact.
    r"(?i:\bBearer[ \t]+[A-Za-z0-9._~+/\-]{16,})",
    r"(?i:\bBasic[ \t]+[A-Za-z0-9+/]{16,}={0,2})",
    # JWTs: the ``eyJ`` header is a strong discriminator.  The segments may be
    # short in test fixtures and in compact tokens, so do not impose an
    # arbitrary six-character minimum on the payload/signature segments.
    r"(?<![A-Za-z0-9_-])eyJ[A-Za-z0-9_-]{2,}\.[A-Za-z0-9_-]{1,}\."
    r"[A-Za-z0-9_-]{1,}(?![A-Za-z0-9_-])",
    # PEM private keys, including RSA/EC/OPENSSH/PGP and encrypted variants.
    r"-----BEGIN (?:[A-Z0-9][A-Z0-9 ]* )?PRIVATE KEY(?: BLOCK)?-----"
    r"[\s\S]*?-----END (?:[A-Z0-9][A-Z0-9 ]* )?PRIVATE KEY(?: BLOCK)?-----",
    # Credentials embedded in an absolute HTTP(S) URL.  The password can be
    # short and punctuation-heavy; the @ delimiter makes this unambiguous.
    r"(?i:\bhttps?://[^\s/@]+:[^\s/@]+@)",
    # PII is not a secret-key shape, but it is not allowed into event text.
    r"(?<![A-Za-z0-9._%+\-])[A-Za-z0-9._%+\-]+@"
    r"[A-Za-z0-9.-]+\.[A-Za-z]{2,}(?![A-Za-z])",
)

_LITERAL_PATTERNS: tuple[re.Pattern[str], ...] = tuple(
    re.compile(source) for source in _LITERAL_SOURCES
)

_CASE_BEARER_RE = re.compile(r"(?i:Bearer)")
_CASE_BASIC_RE = re.compile(r"(?i:Basic)")
_CASE_HTTP_URL_RE = re.compile(r"(?i:https?://)")
_EMAIL_LOCAL_CHARS = frozenset(
    "ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789._%+-"
)

# Distinctive ASCII prefixes let the default path validate only candidate
# offsets instead of running every long value expression over every character
# in a large log.  The full expressions still enforce boundaries and lengths.
_LITERAL_MARKERS: tuple[tuple[tuple[str, ...], re.Pattern[str]], ...] = (
    (("sk-",), _LITERAL_PATTERNS[0]),
    (("nvapi-",), _LITERAL_PATTERNS[1]),
    (("AIza",), _LITERAL_PATTERNS[2]),
    (("ghp_",), _LITERAL_PATTERNS[3]),
    (("github_pat_",), _LITERAL_PATTERNS[4]),
    (("AKIA", "ASIA"), _LITERAL_PATTERNS[5]),
    (("gsk_", "xai-", "pplx-", "r8_", "hf_", "xoxb-", "xapp-"), _LITERAL_PATTERNS[6]),
    ((), _LITERAL_PATTERNS[7]),
    ((), _LITERAL_PATTERNS[8]),
    (("eyJ",), _LITERAL_PATTERNS[9]),
    (("-----BEGIN",), _LITERAL_PATTERNS[10]),
    ((), _LITERAL_PATTERNS[11]),
    ((), _LITERAL_PATTERNS[12]),
)

# A public combined value expression is useful for logging integrations.  The
# Redactor uses the individual compiled expressions so replacement order is
# explicit and deterministic.
REDACT_VALUES = re.compile("|".join(f"(?:{source})" for source in _LITERAL_SOURCES))

_NAME_SEPARATOR_RE = re.compile(r"[^a-z0-9]+")
_CAMEL_BOUNDARY_RE = re.compile(r"(?<=[a-z0-9])(?=[A-Z])")

_SAFE_METADATA_NAMES = frozenset(
    {
        "api_key_env",
        "api_key_name",
        "api_key_names",
        "api_key_label",
        "api_key_type",
        "api_key_length",
        "token_count",
        "token_counts",
        "token_cost",
        "token_limit",
        "token_metrics",
        "token_rate",
        "token_type",
        "token_usage",
        "tokens",
        "input_tokens",
        "output_tokens",
        "prompt_tokens",
        "completion_tokens",
        "cached_tokens",
        "reasoning_tokens",
        "total_tokens",
        "max_tokens",
        "min_tokens",
        "signature",
        "signatures",
        "git_signature",
        "commit_signature",
        "signature_type",
        "signature_algorithm",
        "signature_length",
        "author",
        "authors",
        "author_name",
        "author_id",
        "author_email",
        "co_author",
        "co_authors",
        "cambium_task_id",
        "cambium_generation",
        "cambium_session_id",
    }
)

_SECRET_EXACT_NAMES = frozenset(
    {
        "api_key",
        "apikey",
        "secret",
        "password",
        "passwd",
        "passphrase",
        "credential",
        "credentials",
        "auth",
        "authorization",
        "proxy_authorization",
        "cookie",
        "set_cookie",
        "session",
        "session_id",
        "sessionid",
        "sid",
        "csrf",
        "csrf_token",
        "xsrf",
        "xsrf_token",
        "jwt",
        "oauth",
        "bearer",
        "access_key",
        "access_key_id",
        "secret_key",
        "secret_access_key",
        "private_key",
        "signing_key",
        "encryption_key",
        "master_key",
        "client_secret",
        "webhook_secret",
        "hmac_secret",
        "access_token",
        "refresh_token",
        "id_token",
        "session_token",
        "bearer_token",
        "oauth_token",
        "oauth_secret",
        "access_tokens",
        "refresh_tokens",
    }
)

_SECRET_NAME_PARTS = frozenset(
    {
        "api",
        "auth",
        "authorization",
        "bearer",
        "cookie",
        "credential",
        "credentials",
        "csrf",
        "jwt",
        "oauth",
        "password",
        "passwd",
        "passphrase",
        "secret",
        "session",
        "sessionid",
        "sid",
        "token",
        "private",
        "hmac",
        "xsrf",
    }
)

_COOKIE_FIELD_NAMES = frozenset({"cookie", "cookies", "set_cookie", "set_cookies"})
_MULTIWORD_CONTEXT_NAMES = frozenset(
    {
        "authorization",
        "proxy_authorization",
        "www_authenticate",
        "x_auth",
        "x_auth_token",
        "x_access_token",
    }
)
_COOKIE_ATTRIBUTE_NAMES = frozenset(
    {
        "domain",
        "expires",
        "httponly",
        "max_age",
        "partitioned",
        "path",
        "priority",
        "samesite",
        "secure",
    }
)


def _normalise_name(name: str) -> str:
    """Return a comparison form without invoking user-defined string methods."""

    # The base ``str`` methods avoid dispatching a surprising override on a
    # str subclass.  Non-string keys never reach this function.
    stripped = str.strip(name).strip("\"'")
    with_boundaries = _CAMEL_BOUNDARY_RE.sub("_", stripped)
    return _NAME_SEPARATOR_RE.sub("_", str.casefold(with_boundaries)).strip("_")


def _secret_name_kind(name: str) -> str | None:
    if not isinstance(name, str):
        return None

    normalised = _normalise_name(name)
    if not normalised or normalised in _SAFE_METADATA_NAMES:
        return None

    # Metadata suffixes are deliberately excluded.  ``api_key_env`` and
    # ``token_type`` describe a credential; they are not the credential.
    if any(
        normalised.endswith(f"_{suffix}")
        for suffix in ("env", "name", "names", "label", "labels", "type", "length")
    ):
        return None

    if normalised in _SECRET_EXACT_NAMES:
        return normalised

    parts = frozenset(part for part in normalised.split("_") if part)
    if "author" in parts or "signature" in parts:
        # A git/event signature and author metadata are intentionally benign.
        # Explicit names such as signing_key and hmac_secret were handled
        # above or by their secret-bearing parts.
        if not parts & (_SECRET_NAME_PARTS - {"auth"}):
            return None

    if "api" in parts and "key" in parts:
        return "api_key"
    if "secret" in parts or "password" in parts or "passwd" in parts:
        return "secret"
    if "credential" in parts or "credentials" in parts:
        return "credential"
    if parts & {"authorization", "auth", "bearer", "cookie"}:
        return "auth"
    if parts & {"session", "sessionid", "sid", "csrf", "xsrf", "jwt", "oauth"}:
        return "session"
    if "token" in parts:
        return "token"
    if "private" in parts and "key" in parts:
        return "private_key"
    if "key" in parts and parts & {"access", "signing", "encryption", "master", "hmac"}:
        return "key"
    return None


def is_secret_name(name: str) -> bool:
    """Return whether a field or environment name carries a secret value.

    Matching is case-insensitive and understands underscore, hyphen, dot, and
    camel-case forms.  Metric fields such as ``token_count`` and metadata such
    as ``signature`` and ``author`` are explicitly not secret names.
    """

    return _secret_name_kind(name) is not None


# The contextual scanner preserves field names and separators while replacing
# the value.  It accepts quoted JSON/YAML-like values and bare log/header
# values.  A bare value ends at a record delimiter; spaces inside an
# Authorization value are therefore covered as one contextual secret.
_CONTEXT_FIELD_RE = re.compile(
    r"(?ix)"
    r"(?<![a-z0-9_.-])"
    r"(?P<prefix>"
    r"(?P<key_quote>[\"']?)"
    r"(?P<name>[a-z][a-z0-9_.-]*)"
    r"(?P=key_quote)"
    r"[ \t]*[:=][ \t]*"
    r")"
    r"(?:"
    r"\"(?P<double>(?:\\.|[^\"\\])*)\""
    r"|'(?P<single>(?:\\.|[^'\\])*)'"
    r"|(?P<bare>[^\r\n,;{}\[\]\)]*)"
    r")"
)

_CONTEXT_TOKEN_RE = re.compile(
    r"(?ix)"
    r"(?<![a-z0-9_.-])"
    r"(?P<prefix>"
    r"(?P<key_quote>[\"']?)"
    r"(?P<name>[a-z][a-z0-9_.-]*)"
    r"(?P=key_quote)"
    r"[ \t]*[:=][ \t]*"
    r")"
    r"(?:"
    r"\"(?P<double>(?:\\.|[^\"\\])*)\""
    r"|'(?P<single>(?:\\.|[^'\\])*)'"
    r"|(?P<bare>[^ \t\r\n,;{}\[\]\)]*)"
    r")"
)

_MULTIWORD_CONTEXT_RE = re.compile(
    r"(?ix)"
    r"(?<![a-z0-9_.-])"
    r"(?P<prefix>"
    r"(?P<key_quote>[\"']?)"
    r"(?P<name>authorization|proxy-authorization|www-authenticate|"
    r"x-auth|x-auth-token|x-access-token|access-token|refresh-token|"
    r"id-token|client-secret|api-key|x-api-key)"
    r"(?P=key_quote)"
    r"[ \t]*[:=][ \t]*"
    r")"
    r"(?:"
    r"\"(?P<double>(?:\\.|[^\"\\])*)\""
    r"|'(?P<single>(?:\\.|[^'\\])*)'"
    r"|(?P<bare>[^\r\n,;{}\[\]\)]*)"
    r")"
)

_COOKIE_CONTEXT_RE = re.compile(
    r"(?ix)"
    r"(?<![a-z0-9_.-])"
    r"(?P<prefix>"
    r"(?P<key_quote>[\"']?)"
    r"(?P<name>cookie|cookies|set-cookie|set_cookies)"
    r"(?P=key_quote)"
    r"[ \t]*[:=][ \t]*"
    r")"
    r"(?:"
    r"\"(?P<double>(?:\\.|[^\"\\])*)\""
    r"|'(?P<single>(?:\\.|[^'\\])*)'"
    r"|(?P<bare>[^\r\n,{}\[\]\)]*)"
    r")"
)

_NEXT_FIELD_RE = re.compile(r"[ \t]+(?=[a-z][a-z0-9_.-]*[ \t]*[:=])", re.I)


def _replacement_sub(pattern: re.Pattern[str], text: str, replacement: str) -> str:
    """Use a callable replacement so replacement text is always literal."""

    return pattern.sub(lambda _match: replacement, text)


def _marker_positions(text: str, marker: str) -> Iterable[int]:
    start = 0
    while True:
        position = text.find(marker, start)
        if position < 0:
            return
        yield position
        start = position + len(marker)


def _literal_spans(text: str) -> list[tuple[int, int]]:
    spans: list[tuple[int, int]] = []
    for index, (markers, pattern) in enumerate(_LITERAL_MARKERS):
        if markers:
            positions = (
                position
                for marker in markers
                for position in _marker_positions(text, marker)
            )
        elif index == 7:
            positions = (match.start() for match in _CASE_BEARER_RE.finditer(text))
        elif index == 8:
            positions = (match.start() for match in _CASE_BASIC_RE.finditer(text))
        elif index == 11:
            positions = (match.start() for match in _CASE_HTTP_URL_RE.finditer(text))
        else:
            positions = _marker_positions(text, "@")

        for position in positions:
            if index == 12:
                start = position
                while start > 0 and text[start - 1] in _EMAIL_LOCAL_CHARS:
                    start -= 1
            else:
                start = position
            match = pattern.match(text, start)
            if match is not None:
                spans.append((match.start(), match.end()))
    return spans


def _redact_literal_values(text: str, replacement: str) -> str:
    spans = _literal_spans(text)
    if not spans:
        return text

    merged: list[tuple[int, int]] = []
    for start, end in sorted(spans):
        if not merged or start > merged[-1][1]:
            merged.append((start, end))
            continue
        previous_start, previous_end = merged[-1]
        merged[-1] = (previous_start, max(previous_end, end))

    parts: list[str] = []
    position = 0
    for start, end in merged:
        parts.append(text[position:start])
        parts.append(replacement)
        position = end
    parts.append(text[position:])
    return "".join(parts)


def _cookie_name_is_attribute(name: str) -> bool:
    return _normalise_name(name) in _COOKIE_ATTRIBUTE_NAMES


def _replace_cookie_value(value: str, replacement: str) -> str:
    leading_length = len(value) - len(value.lstrip(" \t"))
    trailing_length = len(value) - len(value.rstrip(" \t"))
    leading = value[:leading_length]
    trailing = value[len(value) - trailing_length :] if trailing_length else ""
    core_end = len(value) - trailing_length if trailing_length else len(value)
    core = value[leading_length:core_end]
    if len(core) >= 2 and core[0] == core[-1] and core[0] in "\"'":
        return f"{leading}{core[0]}{replacement}{core[-1]}{trailing}"
    return f"{leading}{replacement}{trailing}"


def _redact_cookie_body(body: str, replacement: str, *, set_cookie: bool) -> str:
    """Redact cookie values while retaining Set-Cookie attributes."""

    segments = body.split(";")
    result: list[str] = []
    for index, segment in enumerate(segments):
        equals = segment.find("=")
        if equals <= 0:
            result.append(segment)
            continue
        cookie_name = segment[:equals].strip()
        if not cookie_name:
            result.append(segment)
            continue
        if set_cookie and index > 0 and _cookie_name_is_attribute(cookie_name):
            result.append(segment)
            continue
        redacted_value = _replace_cookie_value(segment[equals + 1 :], replacement)
        result.append(segment[: equals + 1] + redacted_value)
    return ";".join(result)


def _context_replacement(match: re.Match[str], replacement: str) -> str:
    name = match.group("name")
    normalised = _normalise_name(name)
    prefix = match.group("prefix")
    double = match.group("double")
    single = match.group("single")
    bare = match.group("bare")

    if normalised in _COOKIE_FIELD_NAMES:
        if double is not None:
            value = _redact_cookie_body(
                double, replacement, set_cookie=normalised.startswith("set_")
            )
            return f"{prefix}\"{value}\""
        if single is not None:
            value = _redact_cookie_body(
                single, replacement, set_cookie=normalised.startswith("set_")
            )
            return f"{prefix}'{value}'"
        value = _redact_cookie_body(
            bare or "", replacement, set_cookie=normalised.startswith("set_")
        )
        return prefix + value

    if not is_secret_name(name):
        return match.group(0)

    if double is not None:
        return f"{prefix}\"{replacement}\""
    if single is not None:
        return f"{prefix}'{replacement}'"

    value = (bare or "").rstrip(" \t")
    trailing = (bare or "")[len(value) :]
    if not value:
        return prefix + replacement + trailing

    # Keep another same-line field if it is clearly delimited by a key/colon
    # or key/equal pair.  This avoids turning a compact log record into one
    # opaque marker while still replacing the complete first secret value.
    next_field = _NEXT_FIELD_RE.search(value)
    if next_field is None:
        return prefix + replacement + trailing
    return prefix + replacement + value[next_field.start() :] + trailing


_FIELD_NAME_CHARS = frozenset(
    "ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789_.-"
)
_VALUE_STOP_CHARS = frozenset(" \t\r\n,;{}[])")
_HEADER_VALUE_STOP_CHARS = frozenset("\r\n,;{}[])")
_COOKIE_VALUE_STOP_CHARS = frozenset("\r\n,{}[])")


def _context_edits(text: str, replacement: str) -> list[tuple[int, int, str]]:
    """Find contextual value spans by anchoring on the rare separators."""

    separators: list[int] = []
    for separator in (":", "="):
        start = 0
        while True:
            position = text.find(separator, start)
            if position < 0:
                break
            separators.append(position)
            start = position + 1
    separators.sort()

    edits: list[tuple[int, int, str]] = []
    for separator in separators:
        name_end = separator - 1
        while name_end >= 0 and text[name_end] in " \t":
            name_end -= 1
        if name_end < 0:
            continue

        if text[name_end] in "\"'":
            quote = text[name_end]
            name_start = text.rfind(quote, 0, name_end)
            if name_start < 0:
                continue
            name = text[name_start + 1 : name_end]
        else:
            name_start = name_end
            while name_start >= 0 and text[name_start] in _FIELD_NAME_CHARS:
                name_start -= 1
            name = text[name_start + 1 : name_end + 1]

        normalised = _normalise_name(name)
        cookie_field = normalised in _COOKIE_FIELD_NAMES
        if not cookie_field and normalised not in _MULTIWORD_CONTEXT_NAMES:
            if not is_secret_name(name):
                continue

        value_start = separator + 1
        while value_start < len(text) and text[value_start] in " \t":
            value_start += 1
        if value_start >= len(text):
            edits.append((value_start, value_start, replacement))
            continue

        if text[value_start] in "\"'":
            quote = text[value_start]
            value_end = value_start + 1
            while value_end < len(text):
                if text[value_end] == "\\":
                    value_end += 2
                    continue
                if text[value_end] == quote:
                    break
                value_end += 1
            body_start = value_start + 1
            body_end = min(value_end, len(text))
            if cookie_field:
                body = text[body_start:body_end]
                replacement_text = _redact_cookie_body(
                    body, replacement, set_cookie=normalised.startswith("set_")
                )
            else:
                replacement_text = replacement
            edits.append((body_start, body_end, replacement_text))
            continue

        bearer_prefix = text[value_start : value_start + 7].casefold() in {
            "bearer ",
            "basic ",
        }
        if cookie_field:
            stop_chars = _COOKIE_VALUE_STOP_CHARS
        elif normalised in _MULTIWORD_CONTEXT_NAMES or bearer_prefix:
            stop_chars = _HEADER_VALUE_STOP_CHARS
        else:
            stop_chars = _VALUE_STOP_CHARS
        value_end = value_start
        while value_end < len(text):
            character = text[value_end]
            if character not in stop_chars:
                value_end += 1
                continue
            if character == ";" and not cookie_field:
                probe = value_end + 1
                while probe < len(text) and text[probe] in " \t":
                    probe += 1
                field_start = probe
                while probe < len(text) and text[probe] in _FIELD_NAME_CHARS:
                    probe += 1
                while probe < len(text) and text[probe] in " \t":
                    probe += 1
                if field_start < probe and probe < len(text) and text[probe] in ":=":
                    break
                value_end += 1
                continue
            break
        if cookie_field:
            body = text[value_start:value_end]
            replacement_text = _redact_cookie_body(
                body, replacement, set_cookie=normalised.startswith("set_")
            )
        else:
            replacement_text = replacement
        edits.append((value_start, value_end, replacement_text))
    return edits


def _redact_context(text: str, replacement: str) -> str:
    edits = _context_edits(text, replacement)
    if not edits:
        return text

    parts: list[str] = []
    position = 0
    for start, end, replacement_text in sorted(edits):
        if start < position:
            continue
        parts.append(text[position:start])
        parts.append(replacement_text)
        position = end
    parts.append(text[position:])
    return "".join(parts)


def _redact_default(text: str, replacement: str) -> str:
    text = _redact_literal_values(text, replacement)
    return _redact_context(text, replacement)


# Keep the canonical tuple declarative and stable.  Redactor recognizes this
# exact set and uses the context-aware implementation above; custom sets use
# only the patterns supplied by the caller.
DEFAULT_PATTERNS: tuple[re.Pattern[str], ...] = _LITERAL_PATTERNS + (
    _COOKIE_CONTEXT_RE,
    _MULTIWORD_CONTEXT_RE,
    _CONTEXT_TOKEN_RE,
)


class _StructuredContext(Enum):
    NORMAL = "normal"
    HEADERS = "headers"
    COOKIES = "cookies"


class _TuplePlaceholder:
    __slots__ = ("value",)

    def __init__(self) -> None:
        self.value: tuple[Any, ...] | None = None


_MISSING = object()


class _CloneState:
    __slots__ = ("memo",)

    def __init__(self) -> None:
        self.memo: dict[int, object] = {}


def _cached(state: _CloneState, node: object) -> object:
    return state.memo.get(id(node), _MISSING)


def _cookie_context_value_key(key: object, value: object) -> bool:
    if not isinstance(key, str):
        return True
    normalised = _normalise_name(key)
    if normalised in {
        "name",
        "key",
        "path",
        "domain",
        "expires",
        "max_age",
        "samesite",
        "secure",
        "httponly",
        "partitioned",
        "priority",
    }:
        return False
    if normalised == "value":
        return True
    return not isinstance(value, (Mapping, Sequence)) or isinstance(value, (str, bytes, bytearray))


def _child_context(key: object, value: object, context: _StructuredContext) -> _StructuredContext:
    if not isinstance(key, str):
        return context
    normalised = _normalise_name(key)
    if normalised in {"headers", "header"}:
        return _StructuredContext.HEADERS
    if normalised in {"cookies", "cookie_jar", "cookiejar"}:
        return _StructuredContext.COOKIES
    if context is _StructuredContext.COOKIES and isinstance(value, (Mapping, Sequence)):
        return _StructuredContext.COOKIES
    return context


def _clone_structured(
    node: object,
    redactor: Redactor,
    state: _CloneState,
    context: _StructuredContext,
) -> object:
    if isinstance(node, str):
        if context is _StructuredContext.COOKIES:
            return redactor.replacement
        return redactor.redact(node)

    cached = _cached(state, node)
    if cached is not _MISSING:
        if isinstance(cached, _TuplePlaceholder):
            return cached
        return cached

    if isinstance(node, Mapping):
        result: dict[object, object] = {}
        state.memo[id(node)] = result
        for key, value in node.items():
            key_name = key if isinstance(key, str) else None
            if context is _StructuredContext.COOKIES and _cookie_context_value_key(key, value):
                result[key] = redactor.replacement
                continue
            if key_name is not None and is_secret_name(key_name):
                result[key] = redactor.replacement
                continue
            result[key] = _clone_structured(
                value,
                redactor,
                state,
                _child_context(key, value, context),
            )
        return result

    if isinstance(node, list):
        result_list: list[object] = []
        state.memo[id(node)] = result_list
        result_list.extend(
            _clone_structured(item, redactor, state, context) for item in node
        )
        return result_list

    if isinstance(node, tuple):
        placeholder = _TuplePlaceholder()
        state.memo[id(node)] = placeholder
        result_tuple = tuple(
            _clone_structured(item, redactor, state, context) for item in node
        )
        placeholder.value = result_tuple
        state.memo[id(node)] = result_tuple
        return result_tuple

    if isinstance(node, Sequence) and not isinstance(node, (bytes, bytearray)):
        # Unknown sequence implementations are copied to a plain list.  This
        # avoids invoking an arbitrary constructor while still redacting their
        # elements without using repr().
        result_sequence: list[object] = []
        state.memo[id(node)] = result_sequence
        result_sequence.extend(
            _clone_structured(item, redactor, state, context) for item in node
        )
        return result_sequence

    # Non-container values are preserved.  In particular, do not call str(),
    # repr(), deepcopy(), or a user-defined serialization hook here.
    return node


def _resolve_tuple_placeholders(node: object, seen: set[int]) -> object:
    if isinstance(node, _TuplePlaceholder):
        return node.value if node.value is not None else None

    if isinstance(node, dict):
        identity = id(node)
        if identity in seen:
            return node
        seen.add(identity)
        for key, value in tuple(node.items()):
            resolved = _resolve_tuple_placeholders(value, seen)
            if resolved is not value:
                node[key] = resolved
        return node

    if isinstance(node, list):
        identity = id(node)
        if identity in seen:
            return node
        seen.add(identity)
        for index, value in enumerate(node):
            resolved = _resolve_tuple_placeholders(value, seen)
            if resolved is not value:
                node[index] = resolved
        return node

    if isinstance(node, tuple):
        identity = id(node)
        if identity in seen:
            return node
        seen.add(identity)
        resolved_items = tuple(_resolve_tuple_placeholders(item, seen) for item in node)
        if all(
            resolved is original
            for resolved, original in zip(resolved_items, node, strict=True)
        ):
            return node
        return resolved_items

    return node


class Redactor:
    """A deterministic text and structured-payload redactor.

    ``patterns`` defaults to :data:`DEFAULT_PATTERNS`.  A caller-supplied
    pattern collection is authoritative: only those patterns run for string
    values, while secret-named structured keys remain whole-value redaction
    boundaries.  ``replacement`` is inserted literally, even when it contains
    backslashes or digits.
    """

    def __init__(
        self,
        patterns: Iterable[re.Pattern[str]] | None = None,
        *,
        replacement: str = _DEFAULT_REPLACEMENT,
    ) -> None:
        if not isinstance(replacement, str):
            raise TypeError("replacement must be a string")
        self._patterns = DEFAULT_PATTERNS if patterns is None else tuple(patterns)
        if any(not isinstance(pattern, re.Pattern) for pattern in self._patterns):
            raise TypeError("patterns must contain compiled regular expressions")
        self.replacement = replacement

    def redact(self, text: str) -> str:
        """Redact secrets and email addresses from *text*."""

        if not isinstance(text, str):
            raise TypeError("text must be a string")
        if self._patterns == DEFAULT_PATTERNS:
            return _redact_default(text, self.replacement)
        for pattern in self._patterns:
            text = _replacement_sub(pattern, text, self.replacement)
        return text

    def redact_mapping(self, mapping: object) -> object:
        """Return a redacted copy of a mapping or sequence.

        Secret-named mapping values are replaced without inspection, which
        covers short and non-string secrets and prevents hostile objects from
        being serialized.  Lists, tuples, generic sequences, nested mappings,
        shared references, and mapping/list cycles are handled without calling
        ``repr`` on any input value.
        """

        state = _CloneState()
        copied = _clone_structured(mapping, self, state, _StructuredContext.NORMAL)
        return _resolve_tuple_placeholders(copied, set())

    def is_secret_name(self, name: str) -> bool:
        """Convenience wrapper around :func:`is_secret_name`."""

        return is_secret_name(name)


# Explicitly safe runtime variables.  Provider variables are never inherited
# merely because they do not look secret; they must be named in ``allowlist``.
NON_SECRET_BASICS = frozenset(
    {
        "PATH",
        "PYTHONUNBUFFERED",
        "CAMBIUM_TASK_ID",
        "CAMBIUM_GENERATION",
        "CAMBIUM_SESSION_ID",
        "HOME",
    }
)


def build_worker_env(
    base: Mapping[str, str],
    allowlist: Iterable[str] | None = None,
) -> dict[str, str]:
    """Build a strict worker environment from *base*.

    The result contains :data:`NON_SECRET_BASICS` plus only names explicitly
    supplied in ``allowlist``.  ``None`` is a safe basics-only default, not an
    instruction to inherit every non-secret-looking host variable.  Values are
    passed through unchanged and no value is printed or stringified.
    """

    if not isinstance(base, Mapping):
        raise TypeError("base must be a mapping")
    if isinstance(allowlist, str):
        raise TypeError("allowlist must be an iterable of names, not a string")

    requested = frozenset() if allowlist is None else frozenset(allowlist)
    if any(not isinstance(name, str) for name in requested):
        raise TypeError("allowlist must contain strings")
    keep = NON_SECRET_BASICS | requested
    return {
        key: value
        for key, value in base.items()
        if isinstance(key, str) and key in keep
    }
