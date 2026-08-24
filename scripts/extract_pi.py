"""Extract decision-training candidates from pi agent session JSONL logs.

Pi sessions are interaction logs rather than labelled datasets.  This script
therefore only emits substantial, user-authored tasks for which the task text
or the observed session contains a strong decomposition signal.  The label is
an inferred, review-required label; it is never presented as an explicit
model decision.

The extractor intentionally keeps outcome evidence numeric and synthetic.  It
does not copy assistant/tool output into the dataset, which both keeps the
training examples focused and gives the redaction pass a small, auditable
surface.

Run from the repository root, for example::

    PYTHONPATH=src python3 scripts/extract_pi.py \
        --sessions-dir /home/ubuntu/.pi/agent/sessions \
        --output artifacts/optimization/first-real-extraction/candidates-pi.jsonl
"""

from __future__ import annotations

import argparse
import hashlib
import html
import json
import re
import sys
import unicodedata
from collections.abc import Iterator, Mapping, Sequence
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
SRC = ROOT / "src"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))

from cambium.redact import Redactor  # noqa: E402

try:  # Keep the script usable even when the optional module package is absent.
    from cambium.modules.example.decide import should_decompose as _module_decide  # noqa: E402
except ImportError:  # pragma: no cover - exercised only in a partial checkout.
    _module_decide = None


SCHEMA_VERSION = 1
DATASET_VERSION = "1.2.0"
SOURCE = "pi-session"
ADDED_BY = "script:extract_pi"
DEFAULT_SESSIONS_DIR = Path("/home/ubuntu/.pi/agent/sessions")
DEFAULT_OUTPUT = ROOT / "artifacts/optimization/first-real-extraction/candidates-pi.jsonl"
REVIEW_QUEUE = ROOT / "artifacts/optimization/first-real-extraction/review_queue.jsonl"

MAX_TASK_LENGTH = 600
MAX_CONTEXT_LENGTH = 600
MAX_REASON_LENGTH = 240

ACTION_VERBS = frozenset(
    {
        "add",
        "update",
        "refactor",
        "implement",
        "migrate",
        "build",
        "fix",
        "create",
        "remove",
        "rewrite",
        "backfill",
        "introduce",
        "restructure",
        "split",
        "port",
        "restore",
        "merge",
        "verify",
        "audit",
        "classify",
        "investigate",
        "land",
        "reconcile",
        "consolidate",
    }
)

PARALLEL_RE = re.compile(
    r"\b(?:independent(?:ly)?|in parallel|separately|workstreams?|subtasks?|"
    r"decompos\w*|for each|each needs?|each of|multiple independent)\b",
    re.IGNORECASE,
)
ATOMIC_SCOPE_RE = re.compile(
    r"\b(?:single (?:file|module|feature|change|task|coherent)|one (?:file|module|"
    r"feature|coherent change|focused change)|one-file|one coherent|focused single|"
    r"atomic|do not (?:split|decompose)|no need to (?:split|decompose)|only one)\b",
    re.IGNORECASE,
)
NOISE_RE = re.compile(
    r"\b(?:test[- ]fixture|fixture[- ]only|synthetic(?: test)?|lorem ipsum|dummy input)\b",
    re.IGNORECASE,
)
FILE_RE = re.compile(
    r"(?<![A-Za-z0-9_])(?:[A-Za-z0-9_.~$-]+[/\\])*[A-Za-z0-9_.-]+\."
    r"(?:py|rs|ts|tsx|js|jsx|go|java|kt|toml|json|yaml|yml|md|sh|sql|css|html)"
    r"\b",
    re.IGNORECASE,
)
ITEM_RE = re.compile(r"(?m)(?:^\s*[-*+]\s+|\b\d+[).]\s+)")

# Redactor handles provider-shaped credentials and PII.  These additional
# passes cover secrets commonly pasted into shell commands and the explicit
# privacy requirements for this extraction.
HOME_PATH_RE = re.compile(
    r"(?<!\w)/(?:home|Users|private|root)/[^\s<>(){}\[\]\"'`,;]+",
    re.IGNORECASE,
)
HOME_ALIAS_RE = re.compile(
    r"(?<!\w)(?:\$HOME|~)(?:[/\\][^\s<>(){}\[\]\"'`,;]+)+",
    re.IGNORECASE,
)
ABSOLUTE_PATH_RE = re.compile(
    r"(?<![A-Za-z0-9_])/(?:[^\s<>(){}\[\]\"'`,;]+/)+[^\s<>(){}\[\]\"'`,;]+",
)
WINDOWS_PATH_RE = re.compile(
    r"(?<!\w)[A-Za-z]:[\\/][^\s<>(){}\[\]\"'`,;]+",
)
PRIVATE_KEY_RE = re.compile(
    r"-----BEGIN [^-]*PRIVATE KEY-----.*?-----END [^-]*PRIVATE KEY-----",
    re.IGNORECASE | re.DOTALL,
)
KNOWN_TOKEN_RE = re.compile(
    r"\b(?:sk-[A-Za-z0-9_-]{8,}|gh[pousr]_[A-Za-z0-9_]{8,}|"
    r"github_pat_[A-Za-z0-9_]{8,}|AIza[A-Za-z0-9_-]{8,}|"
    r"A(?:KIA|SIA)[A-Z0-9]{16,}|xox[baprs]-[A-Za-z0-9-]{8,}|"
    r"npm_[A-Za-z0-9]{8,}|pypi-[A-Za-z0-9_-]{8,}|"
    r"eyJ[A-Za-z0-9_-]+\.[A-Za-z0-9_-]+\.[A-Za-z0-9_-]+)\b",
    re.IGNORECASE,
)
LONG_ALNUM_RE = re.compile(r"\b[A-Za-z0-9]{60,}\b")
LONG_TOKEN_RE = re.compile(r"\b[A-Za-z0-9_-]{40,}\b")
CREDENTIAL_ASSIGNMENT_RE = re.compile(
    r"(?i)(?<!\w)(?:api[_-]?key|access[_-]?token|refresh[_-]?token|"
    r"id[_-]?token|auth(?:orization)?|bearer|password|passwd|pwd|secret(?:[_-]?key)?|"
    r"private[_-]?key|client[_-]?secret|cookie|session[_-]?id|credential|username|"
    r"email)\s*[:=]\s*(?!\[REDACTED[^\]]*\]|<redacted:base64>)"
    r"(?:\"[^\"]*\"|'[^']*'|[^\s,;)}\]]+)",
)
HEADER_SECRET_RE = re.compile(
    r"(?i)\b(?:authorization|proxy-authorization)\s*:\s*\S+(?:\s+\S+)?|"
    r"\b(?:cookie|set-cookie)\s*:\s*[^\n\r]+",
)
USER_AT_HOST_RE = re.compile(r"(?<![\w./%+-])[A-Za-z0-9_.%+-]+@[A-Za-z0-9.-]+")
UUID_RE = re.compile(
    r"\b[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}\b",
    re.IGNORECASE,
)
BASE64_RE = re.compile(r"(?<![A-Za-z0-9+/_-])[A-Za-z0-9+/_-]{20,}={0,}(?![A-Za-z0-9+/_-])")

_REDACTION_CONTROL_RE = re.compile(r"[\u200b-\u200f\u2060-\u206f\ufeff]")
_CONFUSABLE_TRANSLATION = str.maketrans(
    {
        # Common Cyrillic lookalikes.
        "А": "A",
        "В": "B",
        "С": "C",
        "Е": "E",
        "Н": "H",
        "І": "I",
        "Ј": "J",
        "К": "K",
        "М": "M",
        "О": "O",
        "Р": "P",
        "Ѕ": "S",
        "Т": "T",
        "Х": "X",
        "У": "Y",
        "а": "a",
        "в": "b",
        "с": "c",
        "е": "e",
        "н": "h",
        "і": "i",
        "ј": "j",
        "к": "k",
        "м": "m",
        "о": "o",
        "р": "p",
        "ѕ": "s",
        "т": "t",
        "х": "x",
        "у": "y",
        # Common Greek lookalikes.
        "Α": "A",
        "Β": "B",
        "Ε": "E",
        "Ζ": "Z",
        "Η": "H",
        "Ι": "I",
        "Κ": "K",
        "Μ": "M",
        "Ν": "N",
        "Ο": "O",
        "Ρ": "P",
        "Τ": "T",
        "Χ": "X",
        "Υ": "Y",
        "α": "a",
        "β": "b",
        "ε": "e",
        "ζ": "z",
        "η": "h",
        "ι": "i",
        "κ": "k",
        "μ": "m",
        "ν": "n",
        "ο": "o",
        "ρ": "p",
        "τ": "t",
        "υ": "y",
        "ϲ": "c",
        "ϱ": "p",
        "σ": "s",
        "ς": "s",
    }
)
_BASE64_REPLACEMENT = "<redacted:base64>"
_COMPACT_REDACTION_PASSES = (
    (PRIVATE_KEY_RE, "[REDACTED_PRIVATE_KEY]", False),
    (KNOWN_TOKEN_RE, "[REDACTED_TOKEN]", False),
    (BASE64_RE, _BASE64_REPLACEMENT, True),
    (CREDENTIAL_ASSIGNMENT_RE, "[REDACTED_CREDENTIAL]", False),
)

UNSAFE_OUTPUT_PATTERNS = (
    PRIVATE_KEY_RE,
    re.compile(r"(?i)\b(?:sk-|gh[pousr]_|github_pat_|AKIA|ASIA)"),
    re.compile(r"(?i)\b[A-Za-z0-9]{60,}\b"),
    re.compile(r"(?i)(?<!\w)/(?:home|Users|private|root)/"),
    re.compile(r"\b[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}\b"),
)


def _normalise_text(value: str) -> str:
    """Collapse whitespace and Unicode variants for stable comparisons."""

    return " ".join(unicodedata.normalize("NFKC", value).split()).strip()


def _canonical(value: str) -> str:
    return _normalise_text(value).casefold()


def _redaction_normalise(value: str) -> str:
    """Return a comparison-safe form before any secret pattern is applied.

    Session text can contain wire-format entities, invisible directionality
    controls, or visually confusable Unicode characters.  The extractor is a
    training-data boundary, so it prefers a normalized copy (and some
    deliberate over-redaction) over preserving an ambiguous original form.
    """

    text = value
    # A second pass handles the common ``&amp;#x73;`` representation without
    # making entity expansion an unbounded operation.
    for _ in range(3):
        unfolded = html.unescape(text)
        if unfolded == text:
            break
        text = unfolded
    text = _REDACTION_CONTROL_RE.sub("", text)
    text = unicodedata.normalize("NFKC", text).translate(_CONFUSABLE_TRANSLATION)
    return text.strip()


def _replace_compact_spans(
    text: str,
    pattern: re.Pattern[str],
    replacement: str,
    *,
    require_newline: bool = False,
) -> str:
    """Apply a pattern after removing whitespace, mapping spans back to text.

    A key copied from a wrapped terminal line may have a newline between any
    two characters.  Matching a whitespace-free normalized copy catches that
    form; the index list puts the replacement over the corresponding span in
    the readable normalized text, including the intervening whitespace.
    """

    compact_characters: list[str] = []
    compact_to_text: list[int] = []
    for index, character in enumerate(text):
        if character.isspace():
            continue
        compact_characters.append(character)
        compact_to_text.append(index)
    compact = "".join(compact_characters)
    matches = list(pattern.finditer(compact))
    if not matches:
        return text

    spans: list[tuple[int, int]] = []
    for match in matches:
        if match.start() >= len(compact_to_text) or match.end() <= match.start():
            continue
        start = compact_to_text[match.start()]
        end = compact_to_text[match.end() - 1] + 1
        if require_newline and not any(character in "\r\n" for character in text[start:end]):
            continue
        spans.append((start, end))
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


def _redact_compact_key_runs(value: str) -> str:
    """Redact known key-shaped runs that were split by whitespace/newlines."""

    text = value
    for pattern, replacement, require_newline in _COMPACT_REDACTION_PASSES:
        text = _replace_compact_spans(text, pattern, replacement, require_newline=require_newline)
    return text


def _digest(value: str, length: int = 16) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()[:length]


def _timestamp_ms(value: object) -> int | None:
    """Parse pi's numeric or ISO timestamps without exposing raw values."""

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
        numeric = float(text)
    except (TypeError, ValueError, OverflowError):
        numeric = None
    if numeric is not None:
        try:
            return _timestamp_ms(numeric)
        except (TypeError, ValueError, OverflowError):
            pass
    try:
        parsed = datetime.fromisoformat(text.replace("Z", "+00:00"))
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=UTC)
    try:
        return int(parsed.timestamp() * 1000)
    except (OverflowError, OSError, ValueError):
        return None


def _message_text(message: Mapping[str, Any]) -> str:
    """Return only user-visible text parts, never tool/image payloads."""

    content = message.get("content")
    if isinstance(content, str):
        return content
    if not isinstance(content, list):
        return ""
    parts: list[str] = []
    for part in content:
        if not isinstance(part, Mapping) or part.get("type") != "text":
            continue
        text = part.get("text")
        if isinstance(text, str):
            parts.append(text)
    return "\n".join(parts)


def _iter_strings(value: object, *, _depth: int = 0) -> Iterator[str]:
    """Yield bounded strings from tool arguments for count-only inspection."""

    if _depth > 8:
        return
    if isinstance(value, str):
        if len(value) <= 200_000:
            yield value
        return
    if isinstance(value, Mapping):
        for child in value.values():
            yield from _iter_strings(child, _depth=_depth + 1)
        return
    if isinstance(value, list | tuple):
        for child in value:
            yield from _iter_strings(child, _depth=_depth + 1)


def _redact_text(value: str, redactor: Redactor) -> str:
    """Apply normalized secret, path, token, and base64 sweeps to free text."""

    # Match only after decoding/normalizing.  We emit this conservative
    # normalized copy rather than risk carrying an obfuscated original into a
    # training record.
    text = _redaction_normalise(value)
    text = redactor.redact(text)
    text = _redact_compact_key_runs(text)
    text = PRIVATE_KEY_RE.sub("[REDACTED_PRIVATE_KEY]", text)
    text = HEADER_SECRET_RE.sub("[REDACTED_HEADER]", text)
    text = CREDENTIAL_ASSIGNMENT_RE.sub("[REDACTED_CREDENTIAL]", text)
    text = KNOWN_TOKEN_RE.sub("[REDACTED_TOKEN]", text)
    # Run this before the broad legacy token expressions so a generic base64
    # run receives its explicit marker instead of a less informative token
    # marker.  False positives are intentional at this data-extraction gate.
    text = BASE64_RE.sub(_BASE64_REPLACEMENT, text)
    text = LONG_ALNUM_RE.sub("[REDACTED_TOKEN]", text)
    text = LONG_TOKEN_RE.sub("[REDACTED_TOKEN]", text)
    text = USER_AT_HOST_RE.sub("[REDACTED_IDENTITY]", text)
    text = HOME_PATH_RE.sub("[REDACTED_PATH]", text)
    text = HOME_ALIAS_RE.sub("[REDACTED_PATH]", text)
    text = WINDOWS_PATH_RE.sub("[REDACTED_PATH]", text)
    text = ABSOLUTE_PATH_RE.sub("[REDACTED_PATH]", text)
    text = UUID_RE.sub("[REDACTED_ID]", text)
    return _normalise_text(text)


def _safe_task(value: str, redactor: Redactor) -> str:
    """Redact and bound one user task while preserving its useful beginning."""

    text = _redact_text(value, redactor)
    if len(text) <= MAX_TASK_LENGTH:
        return text
    return text[: MAX_TASK_LENGTH - 14].rstrip() + " [TRUNCATED]"


def _safe_source_path(path: Path) -> str:
    """Keep the source filename while hiding directory/user information."""

    return f"[REDACTED_SESSIONS]/{path.name}"


def _safe_repo(cwd: str) -> str:
    """Return a repository label without emitting a home/worktree username."""

    if not cwd:
        return "unknown"
    parts = [part for part in re.split(r"[/\\]+", cwd) if part]
    if not parts:
        return "unknown"
    label = parts[-1]
    if len(parts) <= 2 and parts[0].casefold() in {"home", "users", "root"}:
        return "unknown"
    if not re.fullmatch(r"[A-Za-z0-9_.-]{1,64}", label):
        return "unknown"
    if UUID_RE.fullmatch(label) or label.casefold() in {"ubuntu", "root", "home", "tmp"}:
        return "unknown"
    return label


@dataclass(slots=True)
class SessionStats:
    """Count-only evidence collected while reading one session."""

    session_id: str = ""
    cwd: str = ""
    session_timestamp_ms: int | None = None
    assistant_turns: int = 0
    visible_assistant_turns: int = 0
    tool_calls: int = 0
    tool_results: int = 0
    tool_errors: int = 0
    assistant_errors: int = 0
    agent_tasks: int = 0
    custom_agent_events: int = 0
    touched_files: set[str] = field(default_factory=set)
    modified_files: set[str] = field(default_factory=set)
    tool_names: set[str] = field(default_factory=set)
    user_messages: list[tuple[int, str, int | None]] = field(default_factory=list)

    @property
    def independent_tasks(self) -> int:
        """Use explicit Agent calls, falling back to custom task events."""

        return self.agent_tasks or self.custom_agent_events


@dataclass(frozen=True, slots=True)
class TaskFeatures:
    """Structural signals from a user task."""

    clauses: int
    items: int
    file_refs: int
    action_clauses: int
    parallel_terms: int
    explicit_atomic: bool
    explicit_multi: bool
    signal_score: int


@dataclass(frozen=True, slots=True)
class CandidateDraft:
    """A candidate plus an internal ranking score used before deduplication."""

    record: dict[str, object]
    task_key: str
    rank: tuple[int, int, int, int]


def _record_file_ref(ref: str) -> str:
    """Hash a file reference for count-only evidence."""

    return _digest(ref.casefold(), length=24)


def _add_file_refs(stats: SessionStats, value: str, *, modified: bool = False) -> None:
    for match in FILE_RE.finditer(value):
        ref = _record_file_ref(match.group(0))
        stats.touched_files.add(ref)
        if modified:
            stats.modified_files.add(ref)


def _named_argument_paths(arguments: object) -> Iterator[str]:
    """Yield path-like argument values without retaining their contents."""

    if isinstance(arguments, Mapping):
        for key, value in arguments.items():
            key_name = str(key).casefold()
            if key_name in {"path", "filepath", "filename", "file", "files", "paths"}:
                yield from _iter_strings(value)
            else:
                yield from _named_argument_paths(value)
    elif isinstance(arguments, list | tuple):
        for value in arguments:
            yield from _named_argument_paths(value)


def _tool_call(stats: SessionStats, part: Mapping[str, Any]) -> None:
    name = part.get("name")
    if not isinstance(name, str):
        name = "unknown"
    name_key = name.casefold()
    stats.tool_calls += 1
    stats.tool_names.add(name_key)
    arguments = part.get("arguments")

    if name_key in {"agent", "subagent", "spawn_agent", "task"}:
        stats.agent_tasks += 1
    for string in _iter_strings(arguments):
        _add_file_refs(stats, string)

    named_paths = list(_named_argument_paths(arguments))
    if name_key in {"edit", "write", "apply_patch", "ast_grep_replace", "multiedit"}:
        for path in named_paths:
            _add_file_refs(stats, path, modified=True)
        # apply_patch carries its file names in one patch string rather than
        # under a path key.  Counting these names is still count-only.
        if isinstance(arguments, Mapping):
            for string in _iter_strings(arguments):
                for match in re.finditer(
                    r"\*\*\*\s+(?:Update|Add|Delete)\s+File:\s*([^\s]+)", string
                ):
                    ref = _record_file_ref(match.group(1))
                    stats.touched_files.add(ref)
                    stats.modified_files.add(ref)


def _task_features(task: str) -> TaskFeatures:
    clauses = len([part for part in re.split(r"[.;]\s+", task.strip()) if part.strip()])
    items = len(ITEM_RE.findall(task))
    file_refs = len({_record_file_ref(match.group(0)) for match in FILE_RE.finditer(task)})
    action_clauses = 0
    for clause in re.split(r"[,;]\s+", task):
        words = clause.strip().split()
        if words and words[0].casefold().rstrip(".") in ACTION_VERBS:
            action_clauses += 1
    parallel_terms = len(set(match.group(0).casefold() for match in PARALLEL_RE.finditer(task)))
    explicit_atomic = bool(ATOMIC_SCOPE_RE.search(task))
    explicit_multi = bool(PARALLEL_RE.search(task))

    score = 0
    if clauses >= 3:
        score += 1
    if len(task) > 220:
        score += 1
    if items >= 3:
        score += 2
    if file_refs >= 3:
        score += 1
    if action_clauses >= 3:
        score += 2
    elif action_clauses == 2:
        score += 1
    if parallel_terms:
        score += 1
    return TaskFeatures(
        clauses=clauses,
        items=items,
        file_refs=file_refs,
        action_clauses=action_clauses,
        parallel_terms=parallel_terms,
        explicit_atomic=explicit_atomic,
        explicit_multi=explicit_multi,
        signal_score=score,
    )


def _module_decision(task: str) -> tuple[bool, str]:
    """Use the owning module's neutral rules when available."""

    if _module_decide is not None:
        result = _module_decide(task, "")
        return bool(result.decompose), str(result.reason)
    features = _task_features(task)
    return features.signal_score >= 2, "evidence threshold met"


def _is_noise(task: str) -> bool:
    lowered = task.casefold()
    if NOISE_RE.search(task):
        return True
    if len(task) < 80:
        return True
    if re.fullmatch(r"(?:hi|hello|hey|ok|okay|thanks|continue|proceed|yes|no)[!. ]*", lowered):
        return True
    if lowered.startswith(("context only:", "background only:", "example only:")):
        return True
    return False


def _candidate_label(
    task: str, features: TaskFeatures, stats: SessionStats
) -> tuple[bool, str, float, list[str]] | None:
    """Return a review label only when its evidence is strong enough."""

    if _is_noise(task):
        return None
    module_label, module_reason = _module_decision(task)
    observed_multi = stats.independent_tasks >= 2 or len(stats.modified_files) >= 3
    strong_multi = (
        features.items >= 3
        or features.action_clauses >= 3
        or features.file_refs >= 3
        or features.explicit_multi
        or observed_multi
    )

    # A phrase such as "one feature" is a deliberate calibration cue: do not
    # turn it into a positive merely because it names several surfaces.
    one_feature_scope = bool(re.search(r"\bone feature\b|\bone coherent change\b", task, re.I))
    if (
        module_label
        and strong_multi
        and not (
            features.explicit_atomic and not (features.items >= 3 or features.action_clauses >= 3)
        )
        and not (one_feature_scope and not observed_multi and features.action_clauses < 3)
    ):
        keywords: list[str] = ["inferred_from_task"]
        if features.items >= 3:
            keywords.append("itemized_workstreams")
        if features.action_clauses >= 2:
            keywords.append("multi_step_work")
        if features.file_refs >= 3 or len(stats.touched_files) >= 3:
            keywords.append("multiple_files")
        if features.explicit_multi or stats.independent_tasks:
            keywords.append("independent_subtasks")
        if stats.modified_files:
            keywords.append("observed_changes")
        if stats.independent_tasks or len(stats.modified_files) >= 3:
            confidence = 0.9
        else:
            confidence = 0.8
        reason_parts: list[str] = []
        if features.items >= 3:
            reason_parts.append("explicit workstream list")
        if features.action_clauses >= 3:
            reason_parts.append("three or more action workstreams")
        elif features.action_clauses == 2:
            reason_parts.append("multiple action workstreams")
        if features.file_refs >= 3:
            reason_parts.append("multiple file surfaces")
        if features.explicit_multi:
            reason_parts.append("independent or parallel work is named")
        if stats.independent_tasks:
            reason_parts.append(f"observed {stats.independent_tasks} independent agent tasks")
        if not reason_parts:
            reason_parts.append(module_reason)
        return True, "; ".join(reason_parts)[:MAX_REASON_LENGTH], confidence, keywords

    if not module_label and features.explicit_atomic and len(task) >= 100 and not strong_multi:
        keywords = ["inferred_from_task", "explicit_single_scope", "observed_outcome"]
        reason = "explicitly scoped single task; " + module_reason
        return False, reason[:MAX_REASON_LENGTH], 0.8, keywords
    return None


def _outcome_context(stats: SessionStats) -> str:
    if stats.tool_errors or stats.assistant_errors:
        status = "errors were observed"
    elif stats.tool_calls or stats.assistant_turns:
        status = "assistant/tool activity was observed"
    else:
        status = "no assistant outcome was recorded"
    return _normalise_text(
        "Observed session outcome: "
        f"{stats.assistant_turns} assistant turns, {stats.tool_calls} tool calls, "
        f"{len(stats.touched_files)} files referenced, {len(stats.modified_files)} files "
        f"modified, {stats.independent_tasks} independent agent tasks; {status}."
    )[:MAX_CONTEXT_LENGTH]


def _load_existing_tasks(path: Path) -> set[str]:
    """Load canonical task keys from the approved queue for cross-deduping."""

    result: set[str] = set()
    if not path.is_file():
        return result
    try:
        with path.open(encoding="utf-8", errors="replace") as stream:
            for line in stream:
                try:
                    record = json.loads(line)
                except json.JSONDecodeError:
                    continue
                if not isinstance(record, Mapping):
                    continue
                input_value = record.get("input")
                if isinstance(input_value, Mapping) and isinstance(input_value.get("task"), str):
                    result.add(_canonical(input_value["task"]))
    except OSError:
        return result
    return result


def _sweep_safe(value: object) -> bool:
    """Return false if any emitted string still matches a forbidden shape."""

    if isinstance(value, str):
        return not any(pattern.search(value) for pattern in UNSAFE_OUTPUT_PATTERNS)
    if isinstance(value, Mapping):
        return all(_sweep_safe(key) and _sweep_safe(child) for key, child in value.items())
    if isinstance(value, list | tuple):
        return all(_sweep_safe(child) for child in value)
    return True


def _record(
    *,
    task: str,
    task_key: str,
    label: bool,
    reason: str,
    confidence: float,
    keywords: list[str],
    stats: SessionStats,
    source_path: Path,
    message_index: int,
    message_timestamp_ms: int | None,
    extracted_at: str,
    redactor: Redactor,
) -> dict[str, object]:
    safe_task = _safe_task(task, redactor)
    context = _redact_text(_outcome_context(stats), redactor)[:MAX_CONTEXT_LENGTH]
    source_digest = _digest(str(source_path))
    session_digest = _digest(stats.session_id or str(source_path))
    record_digest = _digest(json.dumps([safe_task, context, label], ensure_ascii=False))
    repo = _safe_repo(stats.cwd)
    created_ms = message_timestamp_ms or stats.session_timestamp_ms
    provenance: dict[str, object] = {
        "channel": SOURCE,
        "source_path": _safe_source_path(source_path),
        "source_path_digest": source_digest,
        "session": session_digest,
        "message_index": message_index,
        "extracted_at": extracted_at,
        "repo": repo,
        "outcome": {
            "assistant_turns": stats.assistant_turns,
            "tool_calls": stats.tool_calls,
            "tool_errors": stats.tool_errors + stats.assistant_errors,
            "files_referenced": len(stats.touched_files),
            "files_modified": len(stats.modified_files),
            "independent_agent_tasks": stats.independent_tasks,
        },
    }
    return {
        "id": f"should_decompose-pi-{record_digest}",
        "schema_version": SCHEMA_VERSION,
        "dataset_version": DATASET_VERSION,
        "split": "candidate",
        "added_at": extracted_at[:10],
        "added_by": ADDED_BY,
        "source": SOURCE,
        "license": "internal",
        "redacted": True,
        "input": {"task": safe_task, "context": context},
        "expected": {"decompose": label, "reason": reason[:MAX_REASON_LENGTH]},
        "expected_confidence": confidence,
        "rationale_keywords": sorted(set(keywords + ["observed_outcome"])),
        "notes": "candidate; review_required; not_train; inferred_from_pi_session",
        "candidate": True,
        "review_status": "needs_review",
        "repo": repo,
        "tool": "pi",
        "time_created_ms": created_ms,
        "provenance": provenance,
    }


def _read_session(path: Path) -> tuple[SessionStats, int]:
    """Read one JSONL session and return count-only stats plus bad-line count."""

    stats = SessionStats()
    malformed = 0
    try:
        stream = path.open(encoding="utf-8", errors="replace")
    except OSError:
        return stats, 1
    with stream:
        for line_number, line in enumerate(stream, 1):
            try:
                event = json.loads(line)
            except (json.JSONDecodeError, UnicodeDecodeError):
                malformed += 1
                continue
            if not isinstance(event, Mapping):
                continue
            event_type = event.get("type")
            if event_type == "session":
                if isinstance(event.get("id"), str):
                    stats.session_id = event["id"]
                if isinstance(event.get("cwd"), str):
                    stats.cwd = event["cwd"]
                stats.session_timestamp_ms = _timestamp_ms(event.get("timestamp"))
                continue
            if event_type == "compaction":
                details = event.get("details")
                if isinstance(details, Mapping):
                    modified = details.get("modifiedFiles")
                    if isinstance(modified, list):
                        for item in modified:
                            if isinstance(item, str):
                                _add_file_refs(stats, item, modified=True)
                continue
            if event_type == "custom" or event_type == "custom_message":
                custom_type = str(event.get("customType", "")).casefold()
                details = event.get("data") or event.get("details")
                if "agent" in custom_type or "subagent" in custom_type:
                    stats.custom_agent_events += 1
                elif isinstance(details, Mapping) and (
                    "toolUses" in details or "turnCount" in details
                ):
                    stats.custom_agent_events += 1
                continue
            if event_type != "message":
                continue
            message = event.get("message")
            if not isinstance(message, Mapping):
                continue
            role = message.get("role")
            if role == "user":
                text = _message_text(message).strip()
                if text:
                    stats.user_messages.append(
                        (line_number, text, _timestamp_ms(message.get("timestamp")))
                    )
                continue
            if role == "assistant":
                stats.assistant_turns += 1
                content = message.get("content")
                if isinstance(content, list):
                    for part in content:
                        if not isinstance(part, Mapping):
                            continue
                        if part.get("type") == "text" and isinstance(part.get("text"), str):
                            if part["text"].strip():
                                stats.visible_assistant_turns += 1
                        elif part.get("type") == "toolCall":
                            _tool_call(stats, part)
                if str(message.get("stopReason", "")).casefold() in {
                    "error",
                    "aborted",
                    "length_error",
                }:
                    stats.assistant_errors += 1
                continue
            if role == "toolResult":
                stats.tool_results += 1
                if bool(message.get("isError")):
                    stats.tool_errors += 1
    return stats, malformed


def extract(
    sessions_dir: Path,
    *,
    existing_queue: Path = REVIEW_QUEUE,
    limit: int = 0,
    extracted_at: str | None = None,
) -> tuple[list[dict[str, object]], dict[str, int]]:
    """Extract and deduplicate candidates from every JSONL below *sessions_dir*."""

    if not sessions_dir.is_dir():
        raise FileNotFoundError(f"sessions directory does not exist: {sessions_dir}")
    now = extracted_at or datetime.now(UTC).isoformat()
    redactor = Redactor(replacement="[REDACTED]")
    existing_tasks = _load_existing_tasks(existing_queue)
    drafts: list[CandidateDraft] = []
    files = sorted(path for path in sessions_dir.rglob("*.jsonl") if path.is_file())
    malformed_lines = 0
    user_messages = 0
    considered_tasks = 0
    excluded_noise = 0
    excluded_weak = 0
    duplicate_tasks = 0
    unsafe_records = 0

    for source_path in files:
        stats, malformed = _read_session(source_path)
        malformed_lines += malformed
        for message_index, (_, raw_task, message_timestamp_ms) in enumerate(stats.user_messages, 1):
            user_messages += 1
            normalised_task = _normalise_text(raw_task)
            if _is_noise(normalised_task):
                excluded_noise += 1
                continue
            considered_tasks += 1
            features = _task_features(normalised_task)
            selected = _candidate_label(normalised_task, features, stats)
            if selected is None:
                excluded_weak += 1
                continue
            label, reason, confidence, keywords = selected
            record = _record(
                task=normalised_task,
                task_key=_canonical(normalised_task),
                label=label,
                reason=reason,
                confidence=confidence,
                keywords=keywords,
                stats=stats,
                source_path=source_path.relative_to(sessions_dir),
                message_index=message_index,
                message_timestamp_ms=message_timestamp_ms,
                extracted_at=now,
                redactor=redactor,
            )
            if not _sweep_safe(record):
                unsafe_records += 1
                continue
            # Rank direct task evidence first, then observed independent work,
            # then observed file changes, then task length.  This makes
            # duplicate selection deterministic without padding the dataset.
            rank = (
                features.signal_score,
                stats.independent_tasks,
                len(stats.modified_files),
                min(len(normalised_task), MAX_TASK_LENGTH),
            )
            drafts.append(CandidateDraft(record, _canonical(normalised_task), rank))

    chosen: dict[str, CandidateDraft] = {}
    for draft in drafts:
        if draft.task_key in existing_tasks:
            duplicate_tasks += 1
            continue
        previous = chosen.get(draft.task_key)
        if previous is None or draft.rank > previous.rank:
            if previous is not None:
                duplicate_tasks += 1
            chosen[draft.task_key] = draft
        else:
            duplicate_tasks += 1

    records = [draft.record for draft in chosen.values()]
    records.sort(key=lambda record: str(record.get("id", "")))
    if limit:
        records = records[:limit]
    summary = {
        "session_files_scanned": len(files),
        "user_messages_seen": user_messages,
        "tasks_considered": considered_tasks,
        "excluded_noise": excluded_noise,
        "excluded_weak": excluded_weak,
        "duplicate_tasks": duplicate_tasks,
        "unsafe_records_dropped": unsafe_records,
        "candidates_produced": len(records),
        "malformed_lines": malformed_lines,
    }
    return records, summary


def _write_records(path: Path, records: Sequence[Mapping[str, object]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.name}.tmp-{_digest(str(path), 8)}")
    text = "".join(
        json.dumps(record, ensure_ascii=True, sort_keys=True) + "\n" for record in records
    )
    temporary.write_text(text, encoding="utf-8")
    temporary.replace(path)


def _positive_limit(value: str) -> int:
    try:
        parsed = int(value)
    except ValueError as exc:
        raise argparse.ArgumentTypeError("limit must be a non-negative integer") from exc
    if parsed < 0:
        raise argparse.ArgumentTypeError("limit must be a non-negative integer")
    return parsed


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--sessions-dir",
        type=Path,
        default=DEFAULT_SESSIONS_DIR,
        help="pi session tree to scan recursively",
    )
    parser.add_argument(
        "--output",
        type=Path,
        default=DEFAULT_OUTPUT,
        help="JSONL candidate output path",
    )
    parser.add_argument(
        "--limit",
        type=_positive_limit,
        default=0,
        help="maximum candidates to write; 0 means unlimited",
    )
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        records, summary = extract(args.sessions_dir, limit=args.limit)
        _write_records(args.output, records)
    except (FileNotFoundError, OSError, ValueError) as exc:
        print(f"extract_pi: {exc}", file=sys.stderr)
        return 2
    summary = dict(summary)
    summary["output_records"] = len(records)
    print(json.dumps(summary, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
