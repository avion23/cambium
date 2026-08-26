"""Deterministic redaction for text, event payloads, and worker environments.

Redaction is deliberately conservative about *names* and deliberate about
*values*.  A value with an unambiguous provider shape is scrubbed wherever it
appears.  A short or punctuation-heavy value is scrubbed when it follows a
secret-bearing field or header name.  Bare hashes, commit identifiers, token
counts, signatures, and author metadata stay intact.

The module has no I/O and does not stringify arbitrary objects.  Structured
redaction walks mappings and sequences, preserves ordinary scalar objects,
does not mutate its input, and keeps the common mapping/list cycles finite.
Worker environment construction uses a deterministic runtime base, and named
provider variables are added only when the caller names them explicitly.
"""

from __future__ import annotations

import hashlib
import os
import re
import threading
from collections.abc import Iterable, Iterator, Mapping, Sequence
from enum import Enum
from os import PathLike
from typing import Any, cast

__all__ = [
    "DEFAULT_PATTERNS",
    "EVENT_RECORD_STRUCTURAL_FIELDS",
    "NON_SECRET_BASICS",
    "REDACT_KEYS",
    "REDACT_VALUES",
    "Redactor",
    "WORKER_RESULT_STRUCTURAL_FIELDS",
    "build_session_redactor",
    "build_worker_env",
    "is_secret_name",
    "sanitize_oauth_document",
]

_DEFAULT_REPLACEMENT = "***"

EVENT_RECORD_STRUCTURAL_FIELDS = frozenset(
    {
        "event_id",
        "generation",
        "kind",
        "monotonic_ms",
        "payload",
        "request_id",
        "schema_version",
        "seq",
        "task_id",
        "ts",
        "worker_id",
    }
)
WORKER_RESULT_STRUCTURAL_FIELDS = frozenset(
    {
        "checkpoint_ref",
        "child_task_id",
        "diff_truncated",
        "epoch",
        "exit_code",
        "generation",
        "parent_task_id",
        "proto",
        "request_id",
        "schema_version",
        "status",
        "task_id",
        "type",
    }
)

# Keep this expression useful to callers that need the architecture's named
# key rule, but use _secret_name_kind below for the intentional metric and
# metadata exceptions.  In particular, ``author`` must not match ``auth``.
REDACT_KEYS = re.compile(
    r"(?i)(?<![a-z0-9])(?:api[_-]?key|token|secret|password|passwd|"
    r"passphrase|credential|credentials|authorization|proxy-authorization|"
    r"auth|cookie|set-cookie|session|sessionid|sid|csrf|xsrf|jwt|oauth|"
    r"bearer|private[_-]?key|access[_-]?key|client[_-]?secret|"
    r"code_verifier|user_code)(?![a-z0-9])"
)

# The value patterns are intentionally shape-based.  Do not add a generic
# long-hex/base64 expression: that would turn public git SHAs and metric
# signatures into ``***``.  Ambiguous values are handled by contextual fields.
_MAX_PEM_SCAN_CHARS = 1 << 20
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
    rf"(?:(?!-----BEGIN)[\s\S]){{0,{_MAX_PEM_SCAN_CHARS}}}?"
    r"-----END (?:[A-Z0-9][A-Z0-9 ]* )?PRIVATE KEY(?: BLOCK)?-----",
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
        "session_status",
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
        "authorization_code",
        "code_verifier",
        "device_auth_id",
        "user_code",
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

# OAuth document fields whose values are credentials.  The token-bearing
# names are exact structured matches only: ``code`` or ``verifier`` alone
# are never secret parts, so ``status_code`` and ``country_code`` stay
# benign while ``code_verifier`` and ``user_code`` redact.
_OAUTH_FIELD_NAMES = frozenset(
    {
        "access_token",
        "refresh_token",
        "id_token",
        "authorization_code",
        "code_verifier",
        "device_auth_id",
        "user_code",
    }
)
_OAUTH_ACCOUNT_NAMES = frozenset({"account_id", "accountid"})
# Distinct from the default ``***`` marker so sanitized OAuth documents
# read as field redactions rather than pattern hits.
_OAUTH_TOKEN_REPLACEMENT = "<redacted>"


def _oauth_account_fingerprint(value: str) -> str:
    """Return a stable account identifier: the first 8 hex of SHA-256."""

    return hashlib.sha256(value.encode("utf-8")).hexdigest()[:8]


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
_CONTEXT_CANDIDATE_RE = re.compile(
    r"(?ix)"
    r"(?<![a-z0-9_.-])"
    r"(?P<key_quote>[\"']?)"
    r"(?P<name>[a-z][a-z0-9_.-]*)"
    r"(?P=key_quote)"
    r"[ \t]*[:=][ \t]*"
)


def _replacement_sub(pattern: re.Pattern[str], text: str, replacement: str) -> str:
    """Use a callable replacement so replacement text is always literal."""

    return pattern.sub(lambda _match: replacement, text)


def _exact_value_pattern(values: frozenset[str]) -> re.Pattern[str] | None:
    if not values:
        return None
    # Longest-first makes overlapping registrations deterministic and prevents
    # a short value from partially consuming a longer registered value.
    alternatives = sorted(values, key=lambda value: (-len(value), value))
    return re.compile("|".join(re.escape(value) for value in alternatives))


_ESCAPE_CHARACTERS = {
    "b": "\b",
    "f": "\f",
    "n": "\n",
    "r": "\r",
    "t": "\t",
    "a": "\a",
    "v": "\v",
}


def _decode_escape_char(text: str, position: int) -> tuple[str, int]:
    """Decode one character at *position* in wire text, handling escapes.

    JSON serialization and Python ``repr`` rewrite a single character into
    ``\\n``-style escapes, ``\\uXXXX``, ``\\xHH``, or octal escapes before
    redaction sees the output, so matching the decoded character (not the wire
    bytes) catches every valid encoding.  A backslash that does not begin a
    valid escape is treated as a literal backslash so ordinary diagnostics
    are not corrupted.
    """
    character = text[position]
    if character != "\\" or position + 1 >= len(text):
        return character, position + 1
    escaped = text[position + 1]
    if escaped in _ESCAPE_CHARACTERS:
        return _ESCAPE_CHARACTERS[escaped], position + 2
    if escaped in {'"', "'", "/"}:
        return escaped, position + 2
    if escaped == "\\":
        return "\\", position + 2
    if escaped in "xXuU":
        digits = 2 if escaped == "x" else (4 if escaped == "u" else 8)
        start = position + 2
        end = start + digits
        if end <= len(text):
            try:
                codepoint = int(text[start:end], 16)
            except ValueError:
                codepoint = -1
            if 0 <= codepoint <= 0x10FFFF:
                return chr(codepoint), end
        return "\\", position + 1
    if escaped in "01234567":
        end = position + 2
        while end < min(len(text), position + 4) and text[end] in "01234567":
            end += 1
        return chr(int(text[position + 1 : end], 8)), end
    return "\\", position + 1


def _decoded_characters(text: str) -> list[tuple[str, int, int]]:
    """Return ``(character, start, end)`` for each decoded character of *text*.

    A valid escape sequence is exactly one decoded character, so a registered
    value can never match characters inside a ``\\uXXXX`` escape that belongs
    to a longer credential.  A backslash that does not begin a valid escape
    stays a literal backslash and the following character is decoded on its
    own.
    """
    characters: list[tuple[str, int, int]] = []
    position = 0
    while position < len(text):
        start = position
        character, position = _decode_escape_char(text, position)
        characters.append((character, start, position))
    return characters


def _escaped_value_spans(text: str, value: str) -> list[tuple[int, int]]:
    """Return spans of *text* whose decoded form is exactly *value*.

    Matching operates on decoded characters, so a credential whose wire form
    mixes raw characters, short escapes, and ``\\uXXXX``/hex escapes (either
    hex case) is matched as one span, while a short registered value never
    matches a character inside an unrelated escape sequence.  A match advances
    past its own span; decoding is deterministic per decoded character.
    """
    target = len(value)
    if target == 0:
        return []
    decoded = _decoded_characters(text)
    spans: list[tuple[int, int]] = []
    index = 0
    while index < len(decoded):
        if len(decoded) - index < target:
            break
        characters: list[str] = []
        position = index
        while len(characters) < target:
            characters.append(decoded[position][0])
            position += 1
        if "".join(characters) == value:
            spans.append((decoded[index][1], decoded[position - 1][2]))
            index = position
        else:
            index += 1
    return spans


def _decodes_to(text: str, value: str) -> bool:
    """Return whether the entire *text* decodes to exactly *value*."""
    if not text:
        return False
    decoded: list[str] = []
    position = 0
    while len(decoded) < len(value) and position < len(text):
        character, position = _decode_escape_char(text, position)
        decoded.append(character)
    return len(decoded) == len(value) and "".join(decoded) == value and position == len(text)


def _replace_spans(text: str, spans: list[tuple[int, int]], replacement: str) -> str:
    """Replace the merged, non-overlapping spans of *text* with *replacement*."""
    merged: list[tuple[int, int]] = []
    for start, end in sorted(spans):
        if not merged or start > merged[-1][1]:
            merged.append((start, end))
            continue
        merged[-1] = (merged[-1][0], max(merged[-1][1], end))
    parts: list[str] = []
    position = 0
    for start, end in merged:
        parts.append(text[position:start])
        parts.append(replacement)
        position = end
    parts.append(text[position:])
    return "".join(parts)


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
        positions: Iterable[int]
        if markers:
            positions = (
                position for marker in markers for position in _marker_positions(text, marker)
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


def _escaped_literal_spans(text: str) -> list[tuple[int, int]]:
    """Return raw spans matching provider shapes in decoded wire text."""
    decoded = _decoded_characters(text)
    if not decoded:
        return []
    logical = "".join(character for character, _start, _end in decoded)
    spans: list[tuple[int, int]] = []
    for start, end in _literal_spans(logical):
        if start < end:
            spans.append((decoded[start][1], decoded[end - 1][2]))
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


_FIELD_NAME_CHARS = frozenset("ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789_.-")
_VALUE_STOP_CHARS = frozenset(" \t\r\n,;{}[])")
_HEADER_VALUE_STOP_CHARS = frozenset("\r\n,;{}[])")
_COOKIE_VALUE_STOP_CHARS = frozenset("\r\n,{}[])")
_DELIMITED_VALUE_ENDS = {"{": "}", "[": "]"}


def _record_stop(text: str, start: int, stop_chars: frozenset[str]) -> int:
    position = start
    while position < len(text) and text[position] not in stop_chars:
        position += 1
    return position


def _delimited_value_end(text: str, start: int) -> int:
    stack: list[str] = []
    quote: str | None = None
    escaped = False
    position = start
    while position < len(text):
        character = text[position]
        if quote is not None:
            if escaped:
                escaped = False
            elif character == "\\":
                escaped = True
            elif character == quote:
                quote = None
        elif character in "\"'":
            quote = character
        elif character in _DELIMITED_VALUE_ENDS:
            stack.append(_DELIMITED_VALUE_ENDS[character])
        elif stack and character == stack[-1]:
            stack.pop()
            if not stack:
                return position + 1
        position += 1
    return len(text)


def _field_follows_separator(text: str, start: int) -> bool:
    position = start
    while position < len(text) and text[position] in " \t":
        position += 1
    return _CONTEXT_CANDIDATE_RE.match(text, position) is not None


def _separator_gap_is_boundary(gap: str) -> bool:
    if not gap:
        return True
    last_boundary = -1
    for index, character in enumerate(gap):
        if character in "\r\n,;{}[]":
            last_boundary = index
    if last_boundary >= 0:
        gap = gap[last_boundary + 1 :]
    return not gap.strip(" \t")


def _header_value_end(text: str, start: int) -> int:
    position = start
    while position < len(text):
        character = text[position]
        if character in _HEADER_VALUE_STOP_CHARS:
            return position
        if ("a" <= character.lower() <= "z" or character in "\"'") and (
            position == start or text[position - 1] not in _FIELD_NAME_CHARS
        ):
            candidate = _CONTEXT_CANDIDATE_RE.match(text, position)
            if candidate is not None:
                end = position
                while end > start and text[end - 1] in " \t":
                    end -= 1
                return end
        position += 1
    return len(text)


def _context_edits(text: str, replacement: str) -> list[tuple[int, int, str]]:
    """Find contextual value spans with one forward pass through *text*."""

    edits: list[tuple[int, int, str]] = []
    position = 0
    field_chain = False
    while position < len(text):
        candidate = _CONTEXT_CANDIDATE_RE.search(text, position)
        if candidate is None:
            break

        name = candidate.group("name")
        normalised = _normalise_name(name)
        cookie_field = normalised in _COOKIE_FIELD_NAMES
        multiword_field = normalised in _MULTIWORD_CONTEXT_NAMES
        secret_field = is_secret_name(name)
        relevant = cookie_field or multiword_field or secret_field

        gap = text[position : candidate.start()]
        field_boundary = _separator_gap_is_boundary(gap)
        record_boundary = any(character in "\r\n,;{}[]" for character in gap)
        if multiword_field and not (
            field_boundary and (field_chain or candidate.start() == 0 or record_boundary)
        ):
            position = candidate.end()
            field_chain = False
            continue

        value_start = candidate.end()
        if value_start >= len(text):
            edits.append((value_start, value_start, replacement))
            position = value_start
            field_chain = True
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
            if relevant:
                edits.append((body_start, body_end, replacement_text))
            position = min(value_end + 1, len(text))
            field_chain = True
            continue

        bearer_prefix = text[value_start : value_start + 7].casefold() in {
            "bearer ",
            "basic ",
        }
        if cookie_field:
            stop_chars = _COOKIE_VALUE_STOP_CHARS
        elif multiword_field or bearer_prefix:
            stop_chars = _HEADER_VALUE_STOP_CHARS
        else:
            stop_chars = _VALUE_STOP_CHARS

        if text[value_start] in _DELIMITED_VALUE_ENDS:
            value_end = _delimited_value_end(text, value_start)
        elif cookie_field:
            value_end = _record_stop(text, value_start, stop_chars)
        elif multiword_field or bearer_prefix:
            value_end = _header_value_end(text, value_start)
        else:
            value_end = value_start
            while value_end < len(text):
                character = text[value_end]
                if character not in stop_chars:
                    value_end += 1
                    continue
                if character == ";" and not _field_follows_separator(text, value_end + 1):
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
        if relevant:
            edits.append((value_start, value_end, replacement_text))
        position = value_end
        field_chain = True
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


class _FieldRole(Enum):
    NORMAL = "normal"
    COOKIE_VALUE = "cookie_value"
    COOKIE_ATTRIBUTE = "cookie_attribute"


class _TuplePlaceholder:
    __slots__ = ("value",)

    def __init__(self) -> None:
        self.value: tuple[Any, ...] | None = None


_MISSING = object()


class _CloneState:
    __slots__ = ("memo", "patterns")

    def __init__(self, patterns: tuple[re.Pattern[str], ...]) -> None:
        self.patterns = patterns
        self.memo: dict[
            tuple[int, tuple[re.Pattern[str], ...], _StructuredContext, _FieldRole], object
        ] = {}


def _cached(
    state: _CloneState,
    node: object,
    context: _StructuredContext,
    role: _FieldRole,
) -> object:
    key = (id(node), state.patterns, context, role)
    return state.memo.get(key, _MISSING)


def _memoise(
    state: _CloneState,
    node: object,
    context: _StructuredContext,
    role: _FieldRole,
    value: object,
) -> None:
    key = (id(node), state.patterns, context, role)
    state.memo[key] = value


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
    return not isinstance(value, Mapping | Sequence) or isinstance(value, str | bytes | bytearray)


def _child_context(key: object, value: object, context: _StructuredContext) -> _StructuredContext:
    if not isinstance(key, str):
        return context
    normalised = _normalise_name(key)
    if normalised in {"headers", "header"}:
        return _StructuredContext.HEADERS
    if normalised in {"cookies", "cookie_jar", "cookiejar"}:
        return _StructuredContext.COOKIES
    if context is _StructuredContext.COOKIES and isinstance(value, Mapping | Sequence):
        return _StructuredContext.COOKIES
    return context


def _child_role(key: object, value: object, context: _StructuredContext) -> _FieldRole:
    if context is _StructuredContext.COOKIES and isinstance(key, str):
        if _cookie_name_is_attribute(key):
            return _FieldRole.COOKIE_ATTRIBUTE
        if _cookie_context_value_key(key, value):
            return _FieldRole.COOKIE_VALUE
    return _FieldRole.NORMAL


def _redact_mapping_key(
    key: object,
    redactor: Redactor,
    structural_fields: frozenset[str] | None = None,
) -> object:
    if not isinstance(key, str):
        return key
    if is_secret_name(key):
        return redactor.replacement
    if structural_fields is not None and _normalise_name(key) in structural_fields:
        return redactor._redact_structural(key)
    return redactor.redact(key)


def _normalise_structural_fields(fields: Iterable[str]) -> frozenset[str]:
    if isinstance(fields, str | bytes):
        raise TypeError("structural_fields must be an iterable of field names")
    normalised: set[str] = set()
    for field in fields:
        if not isinstance(field, str):
            raise TypeError("structural_fields must contain strings")
        normalised.add(_normalise_name(field))
    return frozenset(normalised)


def _clone_structured(
    node: object,
    redactor: Redactor,
    state: _CloneState,
    context: _StructuredContext,
    role: _FieldRole,
) -> object:
    frames: list[
        tuple[
            str,
            object,
            dict[object, object] | list[object],
            Iterator[object],
            _TuplePlaceholder | None,
            _StructuredContext,
            _FieldRole,
        ]
    ] = []

    def prepare(
        current: object,
        current_context: _StructuredContext,
        current_role: _FieldRole,
    ) -> object:
        cached = _cached(state, current, current_context, current_role)
        if cached is not _MISSING:
            return cached

        if isinstance(current, str):
            if (
                current_context is _StructuredContext.COOKIES
                and current_role is not _FieldRole.COOKIE_ATTRIBUTE
            ):
                result_string = redactor.replacement
            else:
                result_string = redactor.redact(current)
            _memoise(state, current, current_context, current_role, result_string)
            return result_string

        if isinstance(current, Mapping):
            result: dict[object, object] = {}
            _memoise(state, current, current_context, current_role, result)
            mapping = cast(dict[object, object], result)
            frames.append(
                (
                    "mapping",
                    current,
                    mapping,
                    iter(current.items()),
                    None,
                    current_context,
                    current_role,
                )
            )
            return result

        if isinstance(current, list):
            result_list: list[object] = []
            _memoise(state, current, current_context, current_role, result_list)
            frames.append(
                (
                    "sequence",
                    current,
                    result_list,
                    iter(current),
                    None,
                    current_context,
                    current_role,
                )
            )
            return result_list

        if isinstance(current, tuple):
            placeholder = _TuplePlaceholder()
            items: list[object] = []
            _memoise(state, current, current_context, current_role, placeholder)
            frames.append(
                (
                    "tuple",
                    current,
                    items,
                    iter(current),
                    placeholder,
                    current_context,
                    current_role,
                )
            )
            return placeholder

        if isinstance(current, Sequence) and not isinstance(current, bytes | bytearray):
            result_sequence: list[object] = []
            _memoise(state, current, current_context, current_role, result_sequence)
            frames.append(
                (
                    "sequence",
                    current,
                    result_sequence,
                    iter(current),
                    None,
                    current_context,
                    current_role,
                )
            )
            return result_sequence

        return current

    copied = prepare(node, context, role)
    while frames:
        (
            frame_kind,
            source,
            target,
            iterator,
            auxiliary,
            frame_context,
            frame_role,
        ) = frames[-1]
        try:
            if frame_kind == "mapping":
                key, value = cast(tuple[object, object], next(iterator))
                key_name = key if isinstance(key, str) else None
                result_key = _redact_mapping_key(key, redactor)
                cookie_value = (
                    frame_context is _StructuredContext.COOKIES
                    and _cookie_context_value_key(key, value)
                )
                mapping = cast(dict[object, object], target)
                if cookie_value or (key_name is not None and is_secret_name(key_name)):
                    mapping[result_key] = redactor.replacement
                    continue
                mapping[result_key] = prepare(
                    value,
                    _child_context(key, value, frame_context),
                    _child_role(key, value, frame_context),
                )
                continue

            item = next(iterator)
            cast(list[object], target).append(prepare(item, frame_context, frame_role))
        except StopIteration:
            frames.pop()
            if frame_kind != "tuple":
                continue
            result_tuple: tuple[object, ...] = tuple(cast(list[object], target))
            cast(_TuplePlaceholder, auxiliary).value = result_tuple
            _memoise(state, source, frame_context, frame_role, result_tuple)

    return copied


def _resolve_tuple_placeholders(node: object, seen: set[int]) -> object:
    root: list[object] = [node]
    tasks: list[
        tuple[
            str,
            object,
            dict[object, object] | list[object],
            object,
            list[object] | None,
        ]
    ] = [("visit", node, root, 0, None)]

    def assign(parent: dict[object, object] | list[object], slot: object, value: object) -> None:
        if isinstance(parent, dict):
            parent[slot] = value
        else:
            parent[cast(int, slot)] = value

    while tasks:
        task, current, parent, slot, auxiliary = tasks.pop()
        if task == "tuple_done":
            original = cast(tuple[object, ...], current)
            completed_items = cast(list[object], auxiliary)
            resolved_tuple: tuple[object, ...] = tuple(completed_items)
            if all(
                resolved is original_item
                for resolved, original_item in zip(resolved_tuple, original, strict=True)
            ):
                resolved_tuple = original
            assign(parent, slot, resolved_tuple)
            continue

        if isinstance(current, _TuplePlaceholder):
            resolved = current.value if current.value is not None else None
            assign(parent, slot, resolved)
            if resolved is not None:
                tasks.append(("visit", resolved, parent, slot, None))
            continue

        if isinstance(current, dict):
            mapping = cast(dict[object, object], current)
            identity = id(mapping)
            if identity in seen:
                assign(parent, slot, mapping)
                continue
            seen.add(identity)
            assign(parent, slot, mapping)
            mapping_items = tuple(mapping.items())
            for key, value in reversed(mapping_items):
                tasks.append(("visit", value, mapping, key, None))
            continue

        if isinstance(current, list):
            sequence = cast(list[object], current)
            identity = id(sequence)
            if identity in seen:
                assign(parent, slot, sequence)
                continue
            seen.add(identity)
            assign(parent, slot, sequence)
            for index in range(len(sequence) - 1, -1, -1):
                tasks.append(("visit", sequence[index], sequence, index, None))
            continue

        if isinstance(current, tuple):
            original = cast(tuple[object, ...], current)
            identity = id(original)
            if identity in seen:
                assign(parent, slot, original)
                continue
            seen.add(identity)
            tuple_items: list[object] = list(original)
            tasks.append(("tuple_done", original, parent, slot, tuple_items))
            for index in range(len(tuple_items) - 1, -1, -1):
                tasks.append(("visit", tuple_items[index], tuple_items, index, None))
            continue

        assign(parent, slot, current)

    return root[0]


class Redactor:
    """A deterministic text and structured-payload redactor.

    ``patterns`` defaults to :data:`DEFAULT_PATTERNS`.  A caller-supplied
    pattern collection is authoritative: only those patterns run for string
    values, while secret-named structured keys remain whole-value redaction
    boundaries.  ``secret_values`` seeds the exact-value registry with
    case-sensitive values applied additively as unbounded substrings before the
    configured pattern behavior; more values can be registered at any time with
    :meth:`register_secret`, which covers credentials rotated after
    construction.  ``whole_values`` is an immutable set of exact values that is
    replaced only when a whole string equals one of them, so a prose-like
    credential never corrupts a larger benign diagnostic. Use
    :meth:`redact_protocol_record` when a caller owns a protocol envelope and
    must explicitly allow exact values in selected top-level fields.
    ``replacement`` is inserted literally, even when it contains backslashes
    or digits.
    """

    def __init__(
        self,
        patterns: Iterable[re.Pattern[str]] | None = None,
        *,
        replacement: str = _DEFAULT_REPLACEMENT,
        secret_values: Iterable[str] | None = None,
        whole_values: Iterable[str] | None = None,
    ) -> None:
        if not isinstance(replacement, str):
            raise TypeError("replacement must be a string")
        self._patterns = DEFAULT_PATTERNS if patterns is None else tuple(patterns)
        if any(not isinstance(pattern, re.Pattern) for pattern in self._patterns):
            raise TypeError("patterns must contain compiled regular expressions")
        if isinstance(secret_values, str | bytes):
            raise TypeError("secret_values must be an iterable of values, not a string")
        registered = frozenset() if secret_values is None else frozenset(secret_values)
        if any(not isinstance(value, str) for value in registered):
            raise TypeError("secret_values must contain strings")
        if any(not value for value in registered):
            raise ValueError("secret_values must not contain empty strings")
        if isinstance(whole_values, str | bytes):
            raise TypeError("whole_values must be an iterable of values, not a string")
        whole = frozenset() if whole_values is None else frozenset(whole_values)
        if any(not isinstance(value, str) for value in whole):
            raise TypeError("whole_values must contain strings")
        if any(not value for value in whole):
            raise ValueError("whole_values must not contain empty strings")
        self._lock = threading.Lock()
        self._secret_values = registered
        self._whole_values = whole
        self._exact_value_pattern = _exact_value_pattern(registered)
        self.replacement = replacement

    @property
    def secret_values(self) -> frozenset[str]:
        """Return a snapshot of the registered exact values.

        The returned frozenset is immutable; :meth:`register_secret` installs a
        new snapshot under the internal lock, so a concurrent reader never sees
        a half-updated registry.
        """

        return self._secret_values

    def register_secret(self, value: str) -> None:
        """Register *value* for exact substring redaction at any time.

        Thread-safe and idempotent: the value joins the exact-value replacement
        set immediately, so a credential rotated after the redactor was built
        (for example a session-scoped OAuth token) is redacted by later
        :meth:`redact`, :meth:`redact_escaped`, and :meth:`redact_mapping`
        calls.  Registering a value that is already present is a no-op.
        """

        if not isinstance(value, str):
            raise TypeError("secret value must be a string")
        if not value:
            raise ValueError("secret value must not be empty")
        with self._lock:
            if value in self._secret_values:
                return
            updated = self._secret_values | {value}
            self._secret_values = updated
            self._exact_value_pattern = _exact_value_pattern(updated)

    def _redact_exact_values(self, text: str) -> str:
        if self._exact_value_pattern is None:
            return text
        return _replacement_sub(self._exact_value_pattern, text, self.replacement)

    def _redact_patterns(self, text: str) -> str:
        """Apply the configured pattern and contextual passes to *text*."""
        if self._patterns == DEFAULT_PATTERNS:
            return _redact_default(text, self.replacement)
        for pattern in self._patterns:
            text = _replacement_sub(pattern, text, self.replacement)
        return text

    def _redact_structural(self, text: str) -> str:
        """Apply configured patterns without matching registered exact values."""
        if not isinstance(text, str):
            raise TypeError("text must be a string")
        return self._redact_patterns(text)

    def redact(self, text: str) -> str:
        """Redact secrets and email addresses from *text*.

        A whole-value registration is honored first: a string that exactly
        equals one is replaced outright, so prose-like credentials never
        corrupt a larger benign diagnostic.
        """

        if not isinstance(text, str):
            raise TypeError("text must be a string")
        if text in self._whole_values:
            return self.replacement
        text = self._redact_exact_values(text)
        return self._redact_patterns(text)

    def redact_escaped(self, text: str) -> str:
        """Redact *text* after decoding JSON/repr escape sequences in place.

        Registered values are matched against the decoded wire form first, so
        a credential whose wire form was rewritten (any hex case, any mix of
        escapes) is replaced as one span and a short registered value never
        matches a character inside an unrelated ``\\uXXXX`` escape.  The
        pattern and contextual passes then run over the remaining text.
        Whole-value registrations stay whole: a string that decodes to one is
        replaced outright, never corrupted into a larger span.
        """

        if not isinstance(text, str):
            raise TypeError("text must be a string")
        if "\\" not in text:
            return self.redact(text)
        if text in self._whole_values:
            return self.replacement
        for value in self._whole_values:
            if _decodes_to(text, value):
                return self.replacement
        spans = _escaped_literal_spans(text)
        for value in self._secret_values:
            spans.extend(_escaped_value_spans(text, value))
        if spans:
            text = _replace_spans(text, spans, self.replacement)
        return self._redact_patterns(text)

    def redact_mapping(self, mapping: object) -> object:
        """Return a redacted copy of a mapping or sequence.

        Secret-named mapping values are replaced without inspection, which
        covers short and non-string secrets and prevents hostile objects from
        being serialized.  Lists, tuples, generic sequences, nested mappings,
        shared references, and mapping/list cycles are handled without calling
        ``repr`` on any input value.
        """

        state = _CloneState(self._patterns)
        copied = _clone_structured(
            mapping, self, state, _StructuredContext.NORMAL, _FieldRole.NORMAL
        )
        return cast(dict[object, object], _resolve_tuple_placeholders(copied, set()))

    def redact_protocol_record(
        self,
        record: Mapping[str, Any],
        *,
        structural_fields: Iterable[str],
    ) -> dict[object, object]:
        """Redact a protocol record with an explicit top-level structure allowlist.

        Only scalar values under the caller-supplied top-level field names use
        pattern-only redaction. All nested mappings and sequences take the
        generic redaction path, so field names such as ``status`` cannot grant
        an exemption to untrusted payload content.
        """
        if not isinstance(record, Mapping):
            raise TypeError("record must be a mapping")
        fields = _normalise_structural_fields(structural_fields)
        state = _CloneState(self._patterns)
        copied: dict[object, object] = {}
        _memoise(state, record, _StructuredContext.NORMAL, _FieldRole.NORMAL, copied)
        for key, value in record.items():
            key_name = key if isinstance(key, str) else None
            result_key = _redact_mapping_key(key, self, fields)
            if key_name is not None and is_secret_name(key_name):
                copied[result_key] = self.replacement
                continue
            if (
                key_name is not None
                and _normalise_name(key_name) in fields
                and isinstance(value, str)
            ):
                copied[result_key] = self._redact_structural(value)
                continue
            copied[result_key] = _clone_structured(
                value,
                self,
                state,
                _StructuredContext.NORMAL,
                _FieldRole.NORMAL,
            )
        return cast(dict[object, object], _resolve_tuple_placeholders(copied, set()))

    def is_secret_name(self, name: str) -> bool:
        """Convenience wrapper around :func:`is_secret_name`."""

        return is_secret_name(name)


def build_session_redactor(
    secret_values: Iterable[str] | None = None,
    *,
    whole_values: Iterable[str] | None = None,
) -> Redactor:
    """Build one session-scoped Redactor from a secret-value snapshot.

    The snapshot is captured at construction.  A session builds a single
    instance here and hands it to its EventStore so persisted event records
    redact the same exact values; credentials rotated mid-session (for example
    OAuth tokens) are registered on the same instance with ``register_secret``
    and are redacted by every later call.

    ``secret_values`` are registered for substring redaction in free-text
    values; structured protocol fields keep their exact values and use only
    configured patterns. ``whole_values`` are replaced only when a whole
    string equals one of them, so prose-like declared values never corrupt a
    larger benign diagnostic.
    """

    return Redactor(secret_values=secret_values, whole_values=whole_values)


def sanitize_oauth_document(
    doc: Mapping, *, redactor: Redactor | None = None
) -> dict[object, object]:
    """Return a redacted copy of an OAuth document.

    Token-bearing OAuth fields (``access_token``, ``refresh_token``,
    ``id_token``, ``authorization_code``, ``code_verifier``,
    ``device_auth_id``, ``user_code``) are replaced outright with
    ``<redacted>`` regardless of value shape; ``account_id`` is replaced by a
    stable fingerprint (the first 8 hex characters of its SHA-256).  Nested
    mappings and sequences are walked, and every remaining string value passes
    through the supplied *redactor* — or the default shape patterns when none
    is given — so a rotated token never survives in request objects, headers,
    response bodies, or exception text.  Keys and the input mapping itself are
    left untouched.
    """

    if not isinstance(doc, Mapping):
        raise TypeError("doc must be a mapping")
    effective = redactor if redactor is not None else Redactor()
    return cast(dict[object, object], _sanitize_oauth_node(doc, effective))


def _sanitize_oauth_node(node: object, redactor: Redactor) -> object:
    if isinstance(node, str):
        return redactor.redact(node)
    if isinstance(node, Mapping):
        result: dict[object, object] = {}
        for key, value in node.items():
            name = key if isinstance(key, str) else None
            if name is not None:
                normalised = _normalise_name(name)
                if normalised in _OAUTH_FIELD_NAMES:
                    result[key] = _OAUTH_TOKEN_REPLACEMENT
                    continue
                if normalised in _OAUTH_ACCOUNT_NAMES and isinstance(value, str):
                    result[key] = _oauth_account_fingerprint(value)
                    continue
            result[key] = _sanitize_oauth_node(value, redactor)
        return result
    if isinstance(node, list):
        return [_sanitize_oauth_node(item, redactor) for item in node]
    if isinstance(node, tuple):
        return tuple(_sanitize_oauth_node(item, redactor) for item in node)
    return node


# Runtime names controlled or reserved by build_worker_env are not copied from
# the host environment. Provider variables are never inherited merely because
# they do not look secret; they must be named in ``allowlist``.
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

_WORKER_ID_NAMES = frozenset(
    {
        "CAMBIUM_TASK_ID",
        "CAMBIUM_GENERATION",
        "CAMBIUM_SESSION_ID",
    }
)


def build_worker_env(
    base: Mapping[str, str],
    allowlist: Iterable[str] | None = None,
    *,
    worktree: str | PathLike[str] | None = None,
    overrides: Mapping[str, str] | None = None,
) -> dict[str, str]:
    """Build a strict worker environment from *base*.

    ``base`` supplies values for explicitly allowlisted provider names only.
    The runtime base is deterministic: ``PATH`` uses :data:`os.defpath` and
    ``PYTHONUNBUFFERED`` is ``"1"``. Cambium task, generation, and session IDs
    may be set explicitly with ``overrides``. Host runtime values are never
    copied.
    """

    if not isinstance(base, Mapping):
        raise TypeError("base must be a mapping")
    if isinstance(allowlist, str | bytes):
        raise TypeError("allowlist must be an iterable of names, not a string")
    if overrides is not None and not isinstance(overrides, Mapping):
        raise TypeError("overrides must be a mapping")

    requested_values = () if allowlist is None else tuple(allowlist)
    if any(not isinstance(name, str) for name in requested_values):
        raise TypeError("allowlist must contain strings")
    requested = frozenset(requested_values)

    if overrides is not None:
        invalid = tuple(name for name in overrides if name not in _WORKER_ID_NAMES)
        if invalid:
            raise ValueError(f"environment override is not allowlisted: {invalid!r}")
        if any(not isinstance(value, str) for value in overrides.values()):
            raise TypeError("environment overrides must contain strings")

    env = {
        "PATH": os.defpath,
        "PYTHONUNBUFFERED": "1",
    }

    if overrides is not None:
        env.update(overrides)

    # Reserved runtime names are omitted even if a caller includes them in the
    # provider allowlist.
    for name in requested - NON_SECRET_BASICS:
        if name not in base:
            continue
        value = base[name]
        if not isinstance(value, str):
            raise TypeError(f"environment value for {name!r} must be a string")
        env[name] = value
    return env
