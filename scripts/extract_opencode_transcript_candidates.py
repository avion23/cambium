"""Extract review-only should_decompose candidates from OpenCode SQLite data."""

from __future__ import annotations

import argparse
import hashlib
import json
import re
import sqlite3
import unicodedata
from collections import defaultdict
from collections.abc import Iterable, Iterator, Sequence
from dataclasses import dataclass
from pathlib import Path

SOURCE = "opencode-transcript"
DATASET_VERSION = "1.1.0"
SCHEMA_VERSION = 1
ADDED_AT = "2026-08-18"
ADDED_BY = "script:extract_opencode_transcript_candidates"
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


@dataclass(frozen=True, slots=True)
class Candidate:
    task: str
    context: str
    decompose: bool
    reason: str
    kind: str
    source: str


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


@dataclass(frozen=True, slots=True)
class ExtractionResult:
    candidates: tuple[Candidate, ...]
    summaries: tuple[DatabaseSummary, ...]
    duplicate_records: int
    conflicting_records: int
    excluded_records: int
    unsafe_records: int


def _normalise_text(value: str) -> str:
    return " ".join(unicodedata.normalize("NFKC", value).split()).strip()


def _canonical_pair(task: str, context: str) -> tuple[str, str]:
    return (_normalise_text(task).casefold(), _normalise_text(context).casefold())


def _as_text(value: object) -> str:
    return value if isinstance(value, str) else ""


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


def _iter_segments(connection: sqlite3.Connection) -> Iterator[tuple[str, str, str]]:
    query = """
        SELECT p.session_id, m.data, p.data
        FROM part AS p
        JOIN message AS m ON m.id = p.message_id
        ORDER BY m.time_created, p.time_created, p.id
    """
    for session_id, message_data, part_data in connection.execute(query):
        try:
            message = json.loads(message_data)
            part = json.loads(part_data)
        except (TypeError, json.JSONDecodeError):
            continue
        if not isinstance(message, dict) or not isinstance(part, dict):
            continue
        part_type = part.get("type")
        if part_type == "text":
            role = message.get("role")
            source = "user" if role == "user" else "assistant" if role == "assistant" else ""
            text = part.get("text")
            if not source or not isinstance(text, str) or not text.strip():
                continue
            yield session_id, source, text
            continue
        if part_type != "tool":
            continue
        state = part.get("state")
        if not isinstance(state, dict) or state.get("status") != "completed":
            continue
        text = state.get("output")
        if isinstance(text, str) and text.strip():
            yield session_id, "tool", text


def _load_sessions(connection: sqlite3.Connection) -> tuple[dict[str, bool], int]:
    query = """
        SELECT s.id, s.title, s.slug, s.directory, s.path, s.metadata,
               COALESCE(pr.name, ''), COALESCE(pr.worktree, '')
        FROM session AS s
        LEFT JOIN project AS pr ON pr.id = s.project_id
    """
    sessions: dict[str, bool] = {}
    total = 0
    for row in connection.execute(query):
        session_id = row[0]
        field_text = " ".join(_as_text(value) for value in row[1:])
        sessions[session_id] = bool(RELEVANCE_RE.search(field_text))
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


def _read_database(path: Path) -> tuple[list[Candidate], DatabaseSummary]:
    uri = f"file:{path.resolve()}?mode=ro"
    with sqlite3.connect(uri, uri=True) as connection:
        sessions, total_sessions = _load_sessions(connection)
        field_relevant = {session_id for session_id, relevant in sessions.items() if relevant}
        content_relevant: set[str] = set()
        for session_id, _source, text in _iter_segments(connection):
            if RELEVANCE_RE.search(text):
                content_relevant.add(session_id)
        selected = field_relevant | content_relevant
        raw_candidates: list[RawCandidate] = []
        for session_id, source, text in _iter_segments(connection):
            if session_id not in selected:
                continue
            raw_candidates.extend(_extract_segment(text, source=source))
    candidates = [
        candidate
        for raw in raw_candidates
        if (candidate := _redact_candidate(raw)) is not None
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
    )
    return candidates, summary


def extract_candidates(
    database_paths: Sequence[Path], *, exclude_paths: Iterable[Path] = ()
) -> ExtractionResult:
    excluded = _load_exclusions(exclude_paths)
    all_candidates: list[Candidate] = []
    summaries: list[DatabaseSummary] = []
    for path in database_paths:
        candidates, summary = _read_database(path)
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


def _record(candidate: Candidate) -> dict[str, object]:
    digest = hashlib.sha256(
        json.dumps(
            [candidate.task, candidate.context, candidate.decompose],
            ensure_ascii=False,
            separators=(",", ":"),
        ).encode("utf-8")
    ).hexdigest()[:16]
    return {
        "id": f"should_decompose-transcript-{digest}",
        "schema_version": SCHEMA_VERSION,
        "dataset_version": DATASET_VERSION,
        "split": "candidate",
        "added_at": ADDED_AT,
        "added_by": ADDED_BY,
        "source": SOURCE,
        "license": "internal",
        "redacted": True,
        "input": {"task": candidate.task, "context": candidate.context},
        "expected": {"decompose": candidate.decompose, "reason": candidate.reason},
        "expected_confidence": 0.95,
        "rationale_keywords": ["explicit_decision", "explicit_rationale", "visible_transcript"],
        "notes": "candidate; review_required; not_train; explicit_visible_decision",
        "candidate": True,
        "review_status": "needs_review",
    }


def write_records(path: Path, result: ExtractionResult) -> None:
    records = sorted(
        (_record(candidate) for candidate in result.candidates), key=lambda record: record["id"]
    )
    path.write_text(
        "".join(json.dumps(record, ensure_ascii=True, sort_keys=True) + "\n" for record in records),
        encoding="utf-8",
    )


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--database", action="append", type=Path, required=True)
    parser.add_argument("--exclude", action="append", type=Path, default=[])
    parser.add_argument("--output", type=Path, required=True)
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    result = extract_candidates(args.database, exclude_paths=args.exclude)
    write_records(args.output, result)
    for summary in result.summaries:
        print(
            f"database={summary.database} sessions={summary.sessions} "
            f"field_relevant={summary.field_relevant_sessions} "
            f"content_relevant={summary.content_relevant_sessions} "
            f"selected={summary.selected_sessions} "
            f"explicit_records={summary.explicit_records}"
        )
    print(
        f"candidates={len(result.candidates)} duplicates={result.duplicate_records} "
        f"conflicts={result.conflicting_records} excluded={result.excluded_records} "
        f"unsafe={result.unsafe_records}"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
