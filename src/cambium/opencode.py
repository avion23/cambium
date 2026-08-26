"""Extract reviewed ``should_decompose`` trajectories from OpenCode data.

The extractor is deliberately read-only.  It only promotes records that carry
an explicit boolean decision and rationale in the source transcript, then
passes those records through the existing redaction and canonical-pair
deduplication path.  The command-line adapter lives here so the installed
``cambium`` command does not depend on the repository's ``scripts`` directory.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import sqlite3
import unicodedata
from collections import Counter, defaultdict
from collections.abc import Iterable, Iterator, Sequence
from dataclasses import dataclass, field, replace
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

SOURCE = "opencode-transcript"
DATASET_VERSION = "1.2.0"
SCHEMA_VERSION = 1
ADDED_BY = "cambium optimize extract"
MAX_TASK_LENGTH = 600
MAX_CONTEXT_LENGTH = 600
MAX_REASON_LENGTH = 240
MAX_JSON_SEGMENT_LENGTH = 2_000_000
MAX_NATURAL_SEGMENT_LENGTH = 200_000

RELEVANCE_RE = re.compile(
    r"\b(?:cambium|should[_ -]?decompose|dspy|hillclimb(?:ing)?|decomposition)\b",
    re.IGNORECASE,
)
FIELD_RE = re.compile(
    r"^\s*(?:[-*]\s*)?"
    r"(?P<field>input[.]task|input[.]context|expected[.]decompose|"
    r"task|context|decision|label|decompose|should_decompose|reason|rationale)"
    r"\s*[:=]\s*(?P<value>.*?)\s*$",
    re.IGNORECASE,
)
DECISION_VALUE_RE = re.compile(
    r"^(?:decompose|do[_ -]?not[_ -]?decompose|true|false|yes|no)[.!]?$",
    re.IGNORECASE,
)
PRIVATE_KEY_RE = re.compile(
    r"-----BEGIN [^-]*PRIVATE KEY-----.*?-----END [^-]*PRIVATE KEY-----",
    re.IGNORECASE | re.DOTALL,
)
CODE_FENCE_RE = re.compile(r"```[^\n]*\n.*?```", re.DOTALL)
SENSITIVE_FILE_RE = re.compile(
    r"(?:\.env(?:\b|$)|printenv\b|set-cookie\b|authorization\s*:|"
    r"(?:api[_-]?key|access[_-]?token|refresh[_-]?token|password|secret|cookie)\s*[:=])",
    re.IGNORECASE,
)
SENSITIVE_ASSIGNMENT_KEY = (
    r"(?:[A-Za-z][A-Za-z0-9]*[_-])*"
    r"(?:api[_-]?(?:key|secret)|access[_-]?(?:token|key)|refresh[_-]?token|"
    r"id[_-]?token|token|auth(?:entication)?|authorization|bearer|password|passwd|pwd|"
    r"secret(?:[_-]?key)?|private[_-]?key|client[_-]?secret|cookie|session[_-]?id|"
    r"credential|user[_-]?name|email)"
    r"(?:[_-][A-Za-z0-9]+)*"
)
CREDENTIAL_ASSIGNMENT_RE = re.compile(
    rf"(?<!\w){SENSITIVE_ASSIGNMENT_KEY}\s*[:=]\s*"
    r"(?:\"[^\"]*\"|'[^']*'|[^\s,;)}\]]+)",
    re.IGNORECASE,
)
HEADER_SECRET_RE = re.compile(
    r"\b(?:authorization|proxy-authorization)\s*:\s*\S+\s+\S+|"
    r"\b(?:cookie|set-cookie)\s*:\s*[^\n\r]+",
    re.IGNORECASE,
)
CREDENTIAL_URL_RE = re.compile(
    r"\b(?:https?|ssh|ftp|git|postgres(?:ql)?|mysql|redis)://[^\s/@:]+:[^\s/@]+@[^\s<>]+",
    re.IGNORECASE,
)
URL_RE = re.compile(
    r"\b(?:https?|ssh|ftp|git|postgres(?:ql)?|mysql|redis)://[^\s<>\"]+",
    re.IGNORECASE,
)
KNOWN_TOKEN_RE = re.compile(
    r"\b(?:sk-[A-Za-z0-9_-]{16,}|gh[pousr]_[A-Za-z0-9_]{20,}|"
    r"github_pat_[A-Za-z0-9_]{20,}|AIza[A-Za-z0-9_-]{20,}|"
    r"AKIA[A-Z0-9]{16}|ASIA[A-Z0-9]{16}|xox[baprs]-[A-Za-z0-9-]{16,}|"
    r"npm_[A-Za-z0-9]{20,}|pypi-[A-Za-z0-9_-]{20,}|hf_[A-Za-z0-9]{20,}|"
    r"eyJ[A-Za-z0-9_-]{10,}[.][A-Za-z0-9_-]{10,}[.][A-Za-z0-9_-]{10,})\b"
)
LONG_HEX_RE = re.compile(r"\b[0-9A-Fa-f]{32,}\b")
LONG_TOKEN_RE = re.compile(r"\b[A-Za-z0-9_-]{40,}\b")
EMAIL_RE = re.compile(r"\b[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}\b")
PHONE_RE = re.compile(r"(?<!\w)\+?\d[\d(). -]{8,}\d(?!\w)")
IP_RE = re.compile(r"(?<!\w)(?:\d{1,3}[.]){3}\d{1,3}(?!\w)")
UUID_RE = re.compile(
    r"\b[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}\b",
    re.IGNORECASE,
)
HOME_PATH_RE = re.compile(r"(?<!\w)/(?:home|Users|private|root)/[^\s<>)\]}]+")
WINDOWS_HOME_PATH_RE = re.compile(r"(?<!\w)[A-Za-z]:[\\/]Users[\\/][^\s<>)\]}]+")
REMAINING_UNSAFE_RE = re.compile(
    r"-----BEGIN [^-]*PRIVATE KEY-----|"
    r"\b(?:https?|ssh|ftp|git|postgres(?:ql)?|mysql|redis)://[^\s<>\"]*@[^\s<>\"]+|"
    r"\b[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}\b|"
    r"\b(?:authorization|proxy-authorization)\s*:\s*\S+\s+\S+|"
    r"\b(?:cookie|set-cookie)\s*:\s*[^\n\r]+|"
    rf"(?<!\w){SENSITIVE_ASSIGNMENT_KEY}\s*[:=]\s*"
    r"(?!\[REDACTED_)[^\s,;)}\]]+",
    re.IGNORECASE,
)


@dataclass(frozen=True, slots=True)
class RawCandidate:
    task: str
    context: str
    decompose: bool
    reason: str
    kind: str
    source: str
    database: str = ""
    session_id: str = ""
    repo: str = ""
    time_created_ms: int | None = None
    tool: str = ""


@dataclass(frozen=True, slots=True)
class Candidate:
    task: str
    context: str
    decompose: bool
    reason: str
    kind: str
    source: str
    database: str = ""
    session_id: str = ""
    repo: str = ""
    time_created_ms: int | None = None
    tool: str = ""


@dataclass(frozen=True, slots=True)
class DatabaseSummary:
    database: str
    sessions: int
    field_relevant_sessions: int
    content_relevant_sessions: int
    selected_sessions: int
    explicit_records: int
    safe_records: int
    unsafe_records: int
    repo_counts: dict[str, int] = field(default_factory=dict)
    tool_counts: dict[str, int] = field(default_factory=dict)
    time_counts: dict[str, int] = field(default_factory=dict)


@dataclass(frozen=True, slots=True)
class ExtractionResult:
    candidates: tuple[Candidate, ...]
    summaries: tuple[DatabaseSummary, ...]
    duplicate_records: int
    conflicting_records: int
    excluded_records: int
    unsafe_records: int


@dataclass(frozen=True, slots=True)
class _Session:
    """The non-sensitive session fields needed for filtering and provenance."""

    session_id: str
    relevant: bool
    repo: str
    repo_values: tuple[str, ...]


@dataclass(frozen=True, slots=True)
class _Segment:
    session_id: str
    source: str
    text: str
    time_created_ms: int | None
    tool: str


def _normalise_text(value: str) -> str:
    return " ".join(unicodedata.normalize("NFKC", value).split()).strip()


def _canonical_pair(task: str, context: str) -> tuple[str, str]:
    return (_normalise_text(task).casefold(), _normalise_text(context).casefold())


def _as_text(value: object) -> str:
    return value if isinstance(value, str) else ""


def _normalise_repo(value: object) -> str:
    """Return a stable, case-insensitive repository comparison key."""
    text = _as_text(value).strip()
    if not text:
        return ""
    if text.startswith(("/", "./", "../", "~", "\\")) or "/" in text or "\\" in text:
        text = os.path.normpath(text).replace("\\", "/")
    return text.rstrip("/").casefold()


def _repo_label(values: Sequence[object]) -> str:
    """Choose a non-path repository label for record-level statistics."""
    for value in values:
        text = _as_text(value).strip()
        if not text:
            continue
        if "/" not in text and "\\" not in text:
            return text
    for value in values:
        text = _as_text(value).strip()
        if text:
            return Path(text).name or text
    return "unknown"


def _timestamp_ms(value: object) -> int | None:
    """Convert OpenCode's epoch values to milliseconds without guessing dates."""
    if isinstance(value, bool):
        return None
    if isinstance(value, int | float):
        if not value:
            return None
        number = float(value)
        if abs(number) < 100_000_000_000:
            number *= 1000
        return int(number)
    if not isinstance(value, str) or not value.strip():
        return None
    text = value.strip()
    try:
        return _timestamp_ms(float(text))
    except ValueError:
        pass
    try:
        parsed = datetime.fromisoformat(text.replace("Z", "+00:00"))
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=UTC)
    return int(parsed.timestamp() * 1000)


def parse_time_bound(value: str | int | float | None) -> int | None:
    """Parse a CLI time bound as epoch milliseconds.

    Numeric values are accepted in seconds or milliseconds.  ISO-8601 values
    may include ``Z``; a date-only value denotes midnight UTC.
    """
    if value is None:
        return None
    parsed = _timestamp_ms(value)
    if parsed is None:
        raise ValueError(f"invalid time bound {value!r}; use epoch seconds or ISO-8601")
    return parsed


def _time_day(value: int | None) -> str:
    if value is None:
        return "unknown"
    try:
        return datetime.fromtimestamp(value / 1000, tz=UTC).date().isoformat()
    except (OverflowError, OSError, ValueError):
        return "unknown"


def _in_time_range(value: int | None, start_ms: int | None, end_ms: int | None) -> bool:
    if start_ms is None and end_ms is None:
        return True
    if value is None:
        return False
    return (start_ms is None or value >= start_ms) and (end_ms is None or value <= end_ms)


def _repo_matches(session: _Session, filters: Sequence[str]) -> bool:
    if not filters:
        return True
    values = {_normalise_repo(value) for value in session.repo_values}
    values.discard("")
    for raw_filter in filters:
        wanted = _normalise_repo(raw_filter)
        if not wanted:
            continue
        if wanted in values:
            return True
        # A session directory below the requested worktree is still part of
        # that repository.  The reverse relationship is intentionally not
        # accepted so ``/repo`` cannot select ``/repo-other``.
        for value in values:
            if value.startswith(f"{wanted}/"):
                return True
        if Path(wanted).name and Path(wanted).name == _normalise_repo(session.repo):
            return True
    return False


def _looks_like_sensitive_code_block(text: str) -> bool:
    for match in CODE_FENCE_RE.finditer(text):
        block = match.group(0)
        if SENSITIVE_FILE_RE.search(block) or CREDENTIAL_ASSIGNMENT_RE.search(block):
            return True
    return False


def _redact_text(value: str, *, limit: int) -> str | None:
    if not isinstance(value, str) or not value.strip() or len(value) > limit:
        return None
    if any(ord(char) < 32 and char not in "\n\r\t" for char in value):
        return None

    text = unicodedata.normalize("NFKC", value)
    text = PRIVATE_KEY_RE.sub("[REDACTED_PRIVATE_KEY]", text)

    def redact_code_block(match: re.Match[str]) -> str:
        block = match.group(0)
        if SENSITIVE_FILE_RE.search(block) or CREDENTIAL_ASSIGNMENT_RE.search(block):
            return "[REDACTED_FILE_CONTENT]"
        return block

    text = CODE_FENCE_RE.sub(redact_code_block, text)
    text = CREDENTIAL_URL_RE.sub("[REDACTED_URL]", text)
    text = URL_RE.sub("[REDACTED_URL]", text)
    text = HEADER_SECRET_RE.sub("[REDACTED_HEADER]", text)
    text = CREDENTIAL_ASSIGNMENT_RE.sub("[REDACTED_CREDENTIAL]", text)
    text = KNOWN_TOKEN_RE.sub("[REDACTED_TOKEN]", text)
    text = UUID_RE.sub("[REDACTED_ID]", text)
    text = LONG_HEX_RE.sub("[REDACTED_TOKEN]", text)
    text = LONG_TOKEN_RE.sub("[REDACTED_TOKEN]", text)
    text = EMAIL_RE.sub("[REDACTED_PERSON]", text)
    text = PHONE_RE.sub("[REDACTED_PERSON]", text)
    text = IP_RE.sub("[REDACTED_NETWORK_ID]", text)
    text = HOME_PATH_RE.sub("[REDACTED_PATH]", text)
    text = WINDOWS_HOME_PATH_RE.sub("[REDACTED_PATH]", text)
    text = _normalise_text(text)
    if not text or REMAINING_UNSAFE_RE.search(text):
        return None
    return text


def _candidate_from_values(
    task: object,
    context: object,
    decompose: object,
    reason: object,
    *,
    kind: str,
    source: str,
) -> RawCandidate | None:
    if not isinstance(task, str) or not isinstance(context, str):
        return None
    if not isinstance(decompose, bool) or not isinstance(reason, str):
        return None
    if (
        not task.strip()
        or len(task) > MAX_TASK_LENGTH
        or len(context) > MAX_CONTEXT_LENGTH
        or not reason.strip()
        or len(reason) > MAX_REASON_LENGTH
    ):
        return None
    if not any(char.isalpha() for char in task):
        return None
    return RawCandidate(
        task=task.strip(),
        context=context.strip(),
        decompose=decompose,
        reason=reason.strip(),
        kind=kind,
        source=source,
    )


def _redact_candidate(raw: RawCandidate) -> Candidate | None:
    task = _redact_text(raw.task, limit=MAX_TASK_LENGTH)
    context = _redact_text(raw.context, limit=MAX_CONTEXT_LENGTH) if raw.context else ""
    reason = _redact_text(raw.reason, limit=MAX_REASON_LENGTH)
    if task is None or context is None or reason is None:
        return None
    if not any(char.isalpha() for char in task):
        return None
    return Candidate(
        task=task,
        context=context,
        decompose=raw.decompose,
        reason=reason,
        kind=raw.kind,
        source=raw.source,
        database=raw.database,
        session_id=raw.session_id,
        repo=raw.repo,
        time_created_ms=raw.time_created_ms,
        tool=raw.tool,
    )


def _json_values(text: str) -> Iterator[object]:
    if len(text) > MAX_JSON_SEGMENT_LENGTH:
        return
    stripped = text.strip()
    if not stripped:
        return
    try:
        yield json.loads(stripped)
    except json.JSONDecodeError:
        pass
    for line in text.splitlines():
        candidate = line.strip()
        if not candidate or candidate == stripped or candidate.startswith("```"):
            continue
        try:
            yield json.loads(candidate)
        except json.JSONDecodeError:
            continue


def _structured_candidates(value: object, *, source: str, depth: int = 0) -> Iterator[RawCandidate]:
    if depth > 8:
        return
    if isinstance(value, dict):
        input_value = value.get("input")
        expected_value = value.get("expected")
        if isinstance(input_value, dict) and isinstance(expected_value, dict):
            candidate = _candidate_from_values(
                input_value.get("task"),
                input_value.get("context"),
                expected_value.get("decompose"),
                expected_value.get("reason"),
                kind="structured",
                source=source,
            )
            if candidate is not None:
                yield candidate
        candidate = _candidate_from_values(
            value.get("task"),
            value.get("context"),
            value.get("decompose"),
            value.get("reason"),
            kind="structured",
            source=source,
        )
        if candidate is not None:
            yield candidate
        for child in value.values():
            yield from _structured_candidates(child, source=source, depth=depth + 1)
    elif isinstance(value, list):
        for child in value:
            yield from _structured_candidates(child, source=source, depth=depth + 1)


def _decision_value(value: str) -> bool | None:
    if not DECISION_VALUE_RE.fullmatch(value.strip()):
        return None
    normalized = value.strip().rstrip(".").casefold().replace("_", " ")
    normalized = " ".join(normalized.split())
    if normalized in {"decompose", "true", "yes"}:
        return True
    if normalized in {"do not decompose", "false", "no"}:
        return False
    return None


def _natural_candidates(text: str, *, source: str) -> Iterator[RawCandidate]:
    if len(text) > MAX_NATURAL_SEGMENT_LENGTH or _looks_like_sensitive_code_block(text):
        return
    fields: dict[str, str] = {}

    def finish() -> RawCandidate | None:
        decision = _decision_value(fields.get("decision", ""))
        if decision is None:
            decision = _decision_value(fields.get("label", ""))
        if decision is None:
            decision = _decision_value(fields.get("expected.decompose", ""))
        if decision is None:
            decision = _decision_value(fields.get("decompose", ""))
        if decision is None:
            decision = _decision_value(fields.get("should_decompose", ""))
        task = fields.get("task", fields.get("input.task"))
        context = fields.get("context", fields.get("input.context", ""))
        reason = fields.get("reason", fields.get("rationale"))
        if task is None or reason is None or decision is None:
            return None
        return _candidate_from_values(
            task,
            context,
            decision,
            reason,
            kind="labeled-text",
            source=source,
        )

    for line in text.splitlines():
        match = FIELD_RE.match(line)
        if match is None:
            if not line.strip() and fields:
                candidate = finish()
                if candidate is not None:
                    yield candidate
                fields = {}
            continue
        field = match.group("field").casefold()
        value = match.group("value").strip()
        if field in {"task", "input.task"} and ("task" in fields or "input.task" in fields):
            candidate = finish()
            if candidate is not None:
                yield candidate
            fields = {}
        fields[field] = value
    candidate = finish()
    if candidate is not None:
        yield candidate


def _extract_segment(text: str, *, source: str) -> Iterator[RawCandidate]:
    for value in _json_values(text):
        yield from _structured_candidates(value, source=source)
    yield from _natural_candidates(text, source=source)


def _tool_name(part: dict[str, object], state: dict[str, object] | None) -> str:
    for value in (
        part.get("tool"),
        state.get("tool") if state is not None else None,
        state.get("name") if state is not None else None,
    ):
        text = _as_text(value).strip()
        if text:
            return text[:100]
    return "tool"


def _iter_segments(
    connection: sqlite3.Connection,
    *,
    start_ms: int | None = None,
    end_ms: int | None = None,
) -> Iterator[_Segment]:
    query = """
        SELECT p.session_id, m.data, p.data, m.time_created, p.time_created, p.id
        FROM part AS p
        JOIN message AS m ON m.id = p.message_id
        ORDER BY COALESCE(m.time_created, p.time_created), p.time_created, p.id
    """
    rows = connection.execute(query)
    for session_id, message_data, part_data, message_time, part_time, _part_id in rows:
        try:
            message = json.loads(message_data)
            part = json.loads(part_data)
        except (TypeError, json.JSONDecodeError):
            continue
        if not isinstance(message, dict) or not isinstance(part, dict):
            continue
        timestamp = _timestamp_ms(part_time) or _timestamp_ms(message_time)
        if not _in_time_range(timestamp, start_ms, end_ms):
            continue
        part_type = part.get("type")
        if part_type == "text":
            role = message.get("role")
            source = "user" if role == "user" else "assistant" if role == "assistant" else ""
            text = part.get("text")
            if not source or not isinstance(text, str) or not text.strip():
                continue
            yield _Segment(session_id, source, text, timestamp, "text")
            continue
        if part_type != "tool":
            continue
        state = part.get("state")
        if not isinstance(state, dict) or state.get("status") != "completed":
            continue
        text = state.get("output")
        if isinstance(text, str) and text.strip():
            yield _Segment(session_id, "tool", text, timestamp, _tool_name(part, state))


def _load_sessions(connection: sqlite3.Connection) -> tuple[dict[str, _Session], int]:
    query = """
        SELECT s.id, s.title, s.slug, s.directory, s.path, s.metadata,
               COALESCE(pr.name, ''), COALESCE(pr.worktree, '')
        FROM session AS s
        LEFT JOIN project AS pr ON pr.id = s.project_id
    """
    sessions: dict[str, _Session] = {}
    total = 0
    for row in connection.execute(query):
        session_id = row[0]
        field_text = " ".join(_as_text(value) for value in row[1:])
        repo_values = tuple(_as_text(value) for value in (row[2], row[3], row[4], row[6], row[7]))
        sessions[session_id] = _Session(
            session_id=session_id,
            relevant=bool(RELEVANCE_RE.search(field_text)),
            repo=_repo_label((row[6], row[7], row[3], row[4])),
            repo_values=repo_values,
        )
        total += 1
    return sessions, total


def _load_exclusions(paths: Iterable[Path]) -> set[tuple[str, str]]:
    excluded: set[tuple[str, str]] = set()
    for path in paths:
        for line in path.read_text(encoding="utf-8").splitlines():
            if not line.strip():
                continue
            try:
                record = json.loads(line)
            except json.JSONDecodeError:
                continue
            if not isinstance(record, dict):
                continue
            input_value = record.get("input")
            if not isinstance(input_value, dict):
                continue
            task = input_value.get("task")
            context = input_value.get("context")
            if isinstance(task, str) and isinstance(context, str):
                excluded.add(_canonical_pair(task, context))
    return excluded


def _read_database(
    path: Path,
    *,
    repo_filters: Sequence[str] = (),
    start_ms: int | None = None,
    end_ms: int | None = None,
) -> tuple[list[Candidate], DatabaseSummary]:
    uri = f"file:{path.resolve()}?mode=ro"
    with sqlite3.connect(uri, uri=True) as connection:
        sessions, total_sessions = _load_sessions(connection)
        matching_sessions = {
            session_id
            for session_id, session in sessions.items()
            if _repo_matches(session, repo_filters)
        }
        field_relevant = {
            session_id for session_id in matching_sessions if sessions[session_id].relevant
        }
        content_relevant: set[str] = set()
        segments = list(_iter_segments(connection, start_ms=start_ms, end_ms=end_ms))
        for segment in segments:
            if segment.session_id in matching_sessions and RELEVANCE_RE.search(segment.text):
                content_relevant.add(segment.session_id)
        if repo_filters:
            # An explicit repository filter is an intentional narrowing of
            # local data, so it may select sessions whose title has no
            # Cambium keyword.  Without a filter retain the historical
            # relevance gate for broad database scans.
            selected = matching_sessions
        else:
            selected = field_relevant | content_relevant
        raw_candidates: list[RawCandidate] = []
        for segment in segments:
            if segment.session_id not in selected:
                continue
            session = sessions[segment.session_id]
            for raw in _extract_segment(segment.text, source=segment.source):
                raw_candidates.append(
                    replace(
                        raw,
                        database=path.name,
                        session_id=segment.session_id,
                        repo=session.repo,
                        time_created_ms=segment.time_created_ms,
                        tool=segment.tool,
                    )
                )
    candidates = [
        candidate for raw in raw_candidates if (candidate := _redact_candidate(raw)) is not None
    ]
    summary = DatabaseSummary(
        database=path.name,
        sessions=total_sessions,
        field_relevant_sessions=len(field_relevant),
        content_relevant_sessions=len(content_relevant),
        selected_sessions=len(selected),
        explicit_records=len(raw_candidates),
        safe_records=len(candidates),
        unsafe_records=len(raw_candidates) - len(candidates),
        repo_counts=dict(Counter(candidate.repo or "unknown" for candidate in candidates)),
        tool_counts=dict(Counter(candidate.tool or "unknown" for candidate in candidates)),
        time_counts=dict(Counter(_time_day(candidate.time_created_ms) for candidate in candidates)),
    )
    return candidates, summary


def extract_candidates(
    database_paths: Sequence[Path],
    *,
    exclude_paths: Iterable[Path] = (),
    repo_filters: Sequence[str] = (),
    repo_filter: str | None = None,
    start_time: str | int | float | None = None,
    end_time: str | int | float | None = None,
) -> ExtractionResult:
    paths = resolve_database_paths(database_paths)
    filters = [*repo_filters]
    if repo_filter is not None:
        filters.append(repo_filter)
    start_ms = parse_time_bound(start_time)
    end_ms = parse_time_bound(end_time)
    if start_ms is not None and end_ms is not None and start_ms > end_ms:
        raise ValueError("start time must not be after end time")
    excluded = _load_exclusions(exclude_paths)
    all_candidates: list[Candidate] = []
    summaries: list[DatabaseSummary] = []
    for path in paths:
        candidates, summary = _read_database(
            path,
            repo_filters=filters,
            start_ms=start_ms,
            end_ms=end_ms,
        )
        all_candidates.extend(candidates)
        summaries.append(summary)

    grouped: dict[tuple[str, str], list[Candidate]] = defaultdict(list)
    for candidate in all_candidates:
        if _canonical_pair(candidate.task, candidate.context) in excluded:
            continue
        grouped[_canonical_pair(candidate.task, candidate.context)].append(candidate)

    chosen: list[Candidate] = []
    duplicate_records = 0
    conflicting_records = 0
    excluded_records = sum(
        1
        for candidate in all_candidates
        if _canonical_pair(candidate.task, candidate.context) in excluded
    )
    for key in sorted(grouped):
        records = grouped[key]
        labels = {candidate.decompose for candidate in records}
        if len(labels) > 1:
            conflicting_records += len(records)
            continue
        duplicate_records += len(records) - 1
        chosen.append(
            min(
                records,
                key=lambda candidate: (
                    0 if candidate.kind == "structured" else 1,
                    len(candidate.reason),
                    candidate.reason.casefold(),
                    candidate.source,
                ),
            )
        )
    chosen.sort(key=lambda candidate: _canonical_pair(candidate.task, candidate.context))
    return ExtractionResult(
        candidates=tuple(chosen),
        summaries=tuple(summaries),
        duplicate_records=duplicate_records,
        conflicting_records=conflicting_records,
        excluded_records=excluded_records,
        unsafe_records=sum(summary.unsafe_records for summary in summaries),
    )


def resolve_database_paths(paths: Sequence[Path]) -> tuple[Path, ...]:
    """Resolve database files or OpenCode storage directories read-only."""
    found: set[Path] = set()
    suffixes = {".db", ".sqlite", ".sqlite3"}
    for raw_path in paths:
        path = Path(raw_path).expanduser()
        if path.is_file():
            found.add(path.resolve())
            continue
        if not path.is_dir():
            raise FileNotFoundError(f"OpenCode source does not exist: {path}")
        for candidate in path.rglob("*"):
            if not candidate.is_file() or candidate.suffix.casefold() not in suffixes:
                continue
            # OpenCode storage directories also contain auxiliary SQLite files
            # (for example release-check databases) that do not carry session
            # data.  Ignore those discovered files, while preserving the
            # existing error for an explicitly supplied database path.
            try:
                with sqlite3.connect(
                    f"file:{candidate.resolve()}?mode=ro", uri=True, timeout=5
                ) as connection:
                    tables = {
                        row[0]
                        for row in connection.execute(
                            "SELECT name FROM sqlite_master WHERE type = 'table'"
                        )
                    }
            except sqlite3.Error:
                continue
            if {"session", "project", "message", "part"}.issubset(tables):
                found.add(candidate.resolve())
    if not found:
        raise FileNotFoundError(
            "OpenCode source contains no SQLite database (.db, .sqlite, or .sqlite3)"
        )
    return tuple(sorted(found, key=lambda value: str(value)))


def _atomic_write_text(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.name}.tmp-{os.getpid()}")
    try:
        temporary.write_text(text, encoding="utf-8")
        os.replace(temporary, path)
    finally:
        temporary.unlink(missing_ok=True)


def _file_digest(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _session_digest(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()[:16]


def _record(
    candidate: Candidate,
    *,
    review_gate: bool = False,
    extracted_at: str | None = None,
) -> dict[str, object]:
    digest = hashlib.sha256(
        json.dumps(
            [candidate.task, candidate.context, candidate.decompose],
            ensure_ascii=False,
            separators=(",", ":"),
        ).encode("utf-8")
    ).hexdigest()[:16]
    status = "needs_review" if review_gate else "approved"
    split = "review_queue" if review_gate else "accepted"
    extraction_time = extracted_at or datetime.now(UTC).isoformat()
    provenance = {
        "database": candidate.database or "unknown",
        "session": _session_digest(candidate.session_id) if candidate.session_id else "unknown",
        "repo": candidate.repo or "unknown",
        "extracted_at": extraction_time,
        "time_created_ms": candidate.time_created_ms,
        "time_created": (
            datetime.fromtimestamp(candidate.time_created_ms / 1000, tz=UTC).isoformat()
            if candidate.time_created_ms is not None
            else None
        ),
        "tool": candidate.tool or "unknown",
        "channel": candidate.source,
    }
    return {
        "id": f"should_decompose-transcript-{digest}",
        "schema_version": SCHEMA_VERSION,
        "dataset_version": DATASET_VERSION,
        "split": split,
        "added_at": datetime.now(UTC).date().isoformat(),
        "added_by": ADDED_BY,
        "source": SOURCE,
        "license": "internal",
        "redacted": True,
        "input": {"task": candidate.task, "context": candidate.context},
        "expected": {"decompose": candidate.decompose, "reason": candidate.reason},
        "expected_confidence": 0.95,
        "rationale_keywords": ["explicit_decision", "explicit_rationale", "visible_transcript"],
        "notes": (
            "candidate; review_required; not_train; explicit_visible_decision"
            if review_gate
            else "accepted; explicit_visible_decision; extracted_from_opencode"
        ),
        "candidate": True,
        "review_status": status,
        "repo": candidate.repo or "unknown",
        "tool": candidate.tool or "unknown",
        "time_created_ms": candidate.time_created_ms,
        "provenance": provenance,
    }


def _metadata_path(path: Path) -> Path:
    return Path(f"{path}.meta.json")


def _dataset_metadata(
    result: ExtractionResult,
    database_paths: Sequence[Path],
    *,
    repo_filters: Sequence[str],
    start_time: str | int | float | None,
    end_time: str | int | float | None,
    review_gate: bool,
) -> dict[str, object]:
    counts = {
        "records": len(result.candidates),
        "duplicates": result.duplicate_records,
        "conflicts": result.conflicting_records,
        "excluded": result.excluded_records,
        "unsafe": result.unsafe_records,
    }
    return {
        "schema_version": SCHEMA_VERSION,
        "dataset_version": DATASET_VERSION,
        "dataset_kind": "opencode-trajectory",
        "split": "review_queue" if review_gate else "accepted",
        "created_at": datetime.now(UTC).isoformat(),
        "provenance": {
            "extractor": "cambium optimize extract",
            "source": SOURCE,
            "database_files": [
                {"name": path.name, "sha256": _file_digest(path)} for path in database_paths
            ],
            "repo_filter": list(repo_filters),
            "time_range": {"from": start_time, "to": end_time},
            "review_gate": review_gate,
        },
        "counts": counts,
        "database_summaries": [
            {
                "database": summary.database,
                "sessions": summary.sessions,
                "selected_sessions": summary.selected_sessions,
                "explicit_records": summary.explicit_records,
                "safe_records": summary.safe_records,
                "unsafe_records": summary.unsafe_records,
                "repo_counts": summary.repo_counts,
                "tool_counts": summary.tool_counts,
                "time_counts": summary.time_counts,
            }
            for summary in result.summaries
        ],
    }


def write_dataset(
    path: Path,
    result: ExtractionResult,
    database_paths: Sequence[Path],
    *,
    repo_filters: Sequence[str] = (),
    start_time: str | int | float | None = None,
    end_time: str | int | float | None = None,
    review_gate: bool = False,
) -> tuple[Path, Path]:
    """Write JSONL records and a sidecar containing versioned provenance."""
    extracted_at = datetime.now(UTC).isoformat()
    records = sorted(
        (
            _record(candidate, review_gate=review_gate, extracted_at=extracted_at)
            for candidate in result.candidates
        ),
        key=lambda record: str(record["id"]),
    )
    text = "".join(
        json.dumps(record, ensure_ascii=True, sort_keys=True) + "\n" for record in records
    )
    output = Path(path)
    _atomic_write_text(output, text)
    metadata_path = _metadata_path(output)
    metadata = _dataset_metadata(
        result,
        database_paths,
        repo_filters=repo_filters,
        start_time=start_time,
        end_time=end_time,
        review_gate=review_gate,
    )
    _atomic_write_text(metadata_path, json.dumps(metadata, ensure_ascii=True, indent=2) + "\n")
    return output, metadata_path


def write_records(path: Path, result: ExtractionResult) -> None:
    """Legacy script writer: preserve its review-queue default."""
    output = Path(path)
    extracted_at = datetime.now(UTC).isoformat()
    records = sorted(
        (
            _record(candidate, review_gate=True, extracted_at=extracted_at)
            for candidate in result.candidates
        ),
        key=lambda record: str(record["id"]),
    )
    _atomic_write_text(
        output,
        "".join(json.dumps(record, ensure_ascii=True, sort_keys=True) + "\n" for record in records),
    )


def _read_jsonl_records(path: Path) -> list[dict[str, Any]]:
    try:
        lines = path.read_text(encoding="utf-8").splitlines()
    except OSError as exc:
        raise ValueError(f"could not read dataset {path}: {exc}") from exc
    records: list[dict[str, Any]] = []
    for line_no, line in enumerate(lines, start=1):
        if not line.strip():
            continue
        try:
            record = json.loads(line)
        except json.JSONDecodeError as exc:
            raise ValueError(f"dataset {path}:{line_no} is invalid JSON") from exc
        if not isinstance(record, dict):
            raise ValueError(f"dataset {path}:{line_no} is not an object")
        records.append(record)
    return records


def dataset_stats(path: Path) -> dict[str, object]:
    """Return deterministic counts for repository, day, label, and tool."""
    records = _read_jsonl_records(Path(path))
    by_repo: Counter[str] = Counter()
    by_time: Counter[str] = Counter()
    by_tool: Counter[str] = Counter()
    by_label: Counter[str] = Counter()
    by_status: Counter[str] = Counter()
    for record in records:
        provenance = record.get("provenance")
        provenance = provenance if isinstance(provenance, dict) else {}
        repo = record.get("repo") or provenance.get("repo") or "unknown"
        tool = record.get("tool") or provenance.get("tool") or "unknown"
        timestamp = record.get("time_created_ms")
        if timestamp is None:
            timestamp = provenance.get("time_created_ms")
        by_repo[str(repo)] += 1
        by_time[_time_day(_timestamp_ms(timestamp))] += 1
        by_tool[str(tool)] += 1
        expected = record.get("expected")
        label = expected.get("decompose") if isinstance(expected, dict) else None
        by_label[str(label).lower() if isinstance(label, bool) else "unknown"] += 1
        by_status[str(record.get("review_status", "unknown"))] += 1

    metadata: dict[str, Any] = {}
    metadata_path = _metadata_path(Path(path))
    if metadata_path.is_file():
        try:
            loaded = json.loads(metadata_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            raise ValueError(f"dataset metadata {metadata_path} is invalid: {exc}") from exc
        if not isinstance(loaded, dict):
            raise ValueError(f"dataset metadata {metadata_path} must be an object")
        metadata = loaded
    return {
        "schema_version": metadata.get("schema_version", SCHEMA_VERSION),
        "dataset_version": metadata.get("dataset_version", DATASET_VERSION),
        "record_count": len(records),
        "records": len(records),
        "by_repo": dict(sorted(by_repo.items())),
        "by_time": dict(sorted(by_time.items())),
        "tool_vocabulary": dict(sorted(by_tool.items())),
        "by_tool": dict(sorted(by_tool.items())),
        "by_label": dict(sorted(by_label.items())),
        "by_review_status": dict(sorted(by_status.items())),
        "provenance": metadata.get("provenance", {}),
    }


def _source_arguments(args: argparse.Namespace) -> list[Path]:
    values = [*getattr(args, "database", []), *getattr(args, "session_dir", [])]
    source = getattr(args, "source", None)
    if source is not None:
        values.append(source)
    if not values:
        raise ValueError("one OpenCode database or session-directory path is required")
    return [Path(value) for value in values]


def _extract_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="cambium optimize extract",
        description="Extract redacted, deduplicated trajectories from local OpenCode data.",
    )
    parser.add_argument("source", nargs="?", type=Path, metavar="PATH")
    parser.add_argument("--database", action="append", type=Path, default=[])
    parser.add_argument(
        "--session-dir",
        action="append",
        type=Path,
        default=[],
        help="OpenCode storage/session directory (SQLite files are discovered read-only)",
    )
    parser.add_argument("--repo", action="append", default=[], metavar="PATH_OR_NAME")
    parser.add_argument("--from", "--since", dest="start_time", metavar="TIME")
    parser.add_argument("--to", "--until", dest="end_time", metavar="TIME")
    parser.add_argument("--exclude", action="append", type=Path, default=[])
    parser.add_argument("--output", type=Path, required=True, metavar="PATH")
    parser.add_argument(
        "--review-gate",
        action="store_true",
        help="write needs_review candidates to the review queue instead of the accepted set",
    )
    return parser


def _stats_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="cambium optimize stats",
        description="Report counts for an extracted trajectory dataset.",
    )
    parser.add_argument("dataset", nargs="?", type=Path, metavar="DATASET")
    parser.add_argument("--dataset", dest="dataset_option", type=Path, metavar="PATH")
    parser.add_argument("--json", action="store_true", help="emit one JSON report")
    return parser


def _print_extraction_summary(result: ExtractionResult, output: Path, metadata: Path) -> None:
    for summary in result.summaries:
        print(
            f"database={summary.database} sessions={summary.sessions} "
            f"selected={summary.selected_sessions} explicit_records={summary.explicit_records}"
        )
    print(
        f"candidates={len(result.candidates)} duplicates={result.duplicate_records} "
        f"conflicts={result.conflicting_records} excluded={result.excluded_records} "
        f"unsafe={result.unsafe_records}"
    )
    print(f"wrote={output} metadata={metadata}")


def extract_main(argv: Sequence[str] | None = None) -> int:
    args = _extract_parser().parse_args(argv)
    try:
        sources = _source_arguments(args)
        databases = resolve_database_paths(sources)
        result = extract_candidates(
            databases,
            exclude_paths=args.exclude,
            repo_filters=args.repo,
            start_time=args.start_time,
            end_time=args.end_time,
        )
        output, metadata = write_dataset(
            args.output,
            result,
            databases,
            repo_filters=args.repo,
            start_time=args.start_time,
            end_time=args.end_time,
            review_gate=args.review_gate,
        )
    except (OSError, sqlite3.Error, ValueError) as exc:
        print(f"cambium optimize extract: {exc}", file=os.sys.stderr)
        return 1
    _print_extraction_summary(result, output, metadata)
    return 0


def stats_main(argv: Sequence[str] | None = None) -> int:
    args = _stats_parser().parse_args(argv)
    path = args.dataset_option or args.dataset
    if path is None:
        print("cambium optimize stats: DATASET or --dataset is required", file=os.sys.stderr)
        return 2
    try:
        report = dataset_stats(path)
    except (OSError, ValueError) as exc:
        print(f"cambium optimize stats: {exc}", file=os.sys.stderr)
        return 1
    if args.json:
        print(json.dumps(report, ensure_ascii=True, sort_keys=True))
    else:
        print(f"dataset_version={report['dataset_version']} records={report['record_count']}")
        print(f"repos={json.dumps(report['by_repo'], sort_keys=True)}")
        print(f"time={json.dumps(report['by_time'], sort_keys=True)}")
        print(f"tools={json.dumps(report['tool_vocabulary'], sort_keys=True)}")
    return 0


def legacy_main(argv: Sequence[str] | None = None) -> int:
    """Run the historical script interface with its review-gate default."""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--database", action="append", type=Path, required=True)
    parser.add_argument("--exclude", action="append", type=Path, default=[])
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args(argv)
    try:
        result = extract_candidates(args.database, exclude_paths=args.exclude)
        write_records(args.output, result)
    except (OSError, sqlite3.Error, ValueError) as exc:
        print(f"extractor: {exc}", file=os.sys.stderr)
        return 1
    _print_extraction_summary(result, args.output, _metadata_path(args.output))
    return 0


def main(argv: Sequence[str] | None = None) -> int:
    """Alias for the installed extraction subcommand."""
    return extract_main(argv)
