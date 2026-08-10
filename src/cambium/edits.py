"""Deterministic, hash-anchored text edits.

An anchor is an exact line in a UTF-8 file.  Its digest combines that line
with the file's normalized (LF) text, so a digest becomes stale when the file
changes.  The full normalized text is deliberate: this primitive must reject
edits based on a file snapshot that was subsequently changed, not guess which
nearby lines are still safe to use.

``apply_anchored_edit`` accepts either the literal anchor line or a digest
returned by :func:`anchor_of`.  The latter makes a read/plan/apply/verify
cycle explicit while retaining a convenient literal-line API for callers that
already hold the current file contents.
"""

from __future__ import annotations

import hashlib
import os
import stat
import tempfile
from dataclasses import dataclass
from pathlib import Path

_DIGEST_LENGTH = hashlib.sha256().digest_size * 2


class EditError(ValueError):
    """Base class for deterministic edit validation failures."""


class AnchorNotFoundError(EditError):
    """The requested anchor does not identify a line in the current file."""


class AmbiguousAnchorError(EditError):
    """The requested anchor identifies more than one line."""


class AmbiguousEditError(EditError):
    """The old text has more than one replacement location."""


@dataclass(frozen=True, slots=True)
class EditResult:
    """The result of one successful anchored edit."""

    applied: bool
    anchor_matched: bool
    occurrences: int
    new_anchor: str | None


@dataclass(frozen=True, slots=True)
class _Line:
    content: str
    start: int
    end: int
    number: int


def _confined_path(path: Path) -> Path:
    """Resolve ``path`` and reject targets outside the current directory."""
    root = Path.cwd().resolve()
    resolved = Path(path).resolve()
    if not resolved.is_relative_to(root):
        raise EditError(
            f"path {resolved} is outside current working directory {root}"
        )
    return resolved


def _read_utf8(path: Path) -> str:
    """Read exact UTF-8 bytes without newline translation."""
    return path.read_bytes().decode("utf-8")


def _normalized_context(text: str) -> str:
    """Normalize only line endings; preserve all other Unicode/code bytes."""
    return text.replace("\r\n", "\n").replace("\r", "\n")


def _lines(text: str) -> list[_Line]:
    """Return logical lines with offsets into the original text."""
    if not text:
        return []

    result: list[_Line] = []
    start = 0
    index = 0
    while index < len(text):
        if text[index] not in "\r\n":
            index += 1
            continue

        result.append(_Line(text[start:index], start, index, len(result) + 1))
        if text[index] == "\r" and index + 1 < len(text) and text[index + 1] == "\n":
            index += 2
        else:
            index += 1
        start = index

    if start < len(text):
        result.append(_Line(text[start:], start, len(text), len(result) + 1))
    return result


def _digest_for_line(text: str, line: str, context: bytes | None = None) -> str:
    """Hash an exact anchor line plus normalized file context."""
    if context is None:
        context = _normalized_context(text).encode("utf-8")
    digest = hashlib.sha256()
    digest.update(line.encode("utf-8"))
    digest.update(b"\0")
    digest.update(context)
    return digest.hexdigest()


def _digest_for_empty_file(text: str) -> str:
    """Hash the no-line result needed when an edit deletes the whole file."""
    digest = hashlib.sha256()
    digest.update(b"\0")
    digest.update(_normalized_context(text).encode("utf-8"))
    return digest.hexdigest()


def _is_digest(value: str) -> bool:
    return len(value) == _DIGEST_LENGTH and all(
        character in "0123456789abcdefABCDEF" for character in value
    )


def _line_context(lines: list[_Line], *, limit: int = 12) -> str:
    """Format bounded line context for actionable validation errors."""
    if not lines:
        return "file is empty"
    if len(lines) <= limit:
        selected = lines
    else:
        edge = limit // 2
        selected = [*lines[:edge], *lines[-edge:]]
    rendered = "; ".join(f"line {line.number}: {line.content!r}" for line in selected)
    if len(lines) > limit:
        rendered = rendered.replace(
            f"line {lines[-edge].number}:", f"...; line {lines[-edge].number}:", 1
        )
    return rendered


def _anchor_not_found(anchor: str, lines: list[_Line]) -> AnchorNotFoundError:
    return AnchorNotFoundError(
        f"anchor {anchor!r} not found; line context: {_line_context(lines)}"
    )


def _resolve_anchor(text: str, anchor: str) -> tuple[int | None, list[_Line]]:
    """Return one matching line index, or raise a precise anchor error."""
    lines = _lines(text)
    literal_matches = [index for index, line in enumerate(lines) if line.content == anchor]
    if literal_matches:
        if len(literal_matches) > 1:
            line_numbers = ", ".join(str(lines[index].number) for index in literal_matches)
            raise AmbiguousAnchorError(
                f"anchor {anchor!r} is ambiguous: matched {len(literal_matches)} "
                f"lines ({line_numbers}); line context: {_line_context(lines)}"
            )
        return literal_matches[0], lines

    if _is_digest(anchor):
        expected = anchor.lower()
        context = _normalized_context(text).encode("utf-8")
        digest_matches = [
            index
            for index, line in enumerate(lines)
            if _digest_for_line(text, line.content, context) == expected
        ]
        if digest_matches:
            if len(digest_matches) > 1:
                line_numbers = ", ".join(str(lines[index].number) for index in digest_matches)
                raise AmbiguousAnchorError(
                    f"anchor {anchor!r} is ambiguous: matched {len(digest_matches)} "
                    f"lines ({line_numbers}); line context: {_line_context(lines)}"
                )
            return digest_matches[0], lines

    raise _anchor_not_found(anchor, lines)


def anchor_of(text: str, anchor: str) -> str:
    """Return a stable SHA-256 digest for one exact anchor line.

    The digest includes the exact anchor line and the complete file after
    normalizing CRLF/CR line endings to LF.  Duplicate anchor lines are
    rejected because a later verifier could not distinguish them.
    """
    if not isinstance(text, str):
        raise TypeError("text must be a string")
    if not isinstance(anchor, str):
        raise TypeError("anchor must be a string")

    line_index, lines = _resolve_anchor(text, anchor)
    if line_index is None:  # pragma: no cover - _resolve_anchor always returns or raises
        raise _anchor_not_found(anchor, lines)
    return _digest_for_line(text, lines[line_index].content)


def _find_occurrences(text: str, old: str) -> list[int]:
    starts: list[int] = []
    offset = 0
    while True:
        start = text.find(old, offset)
        if start == -1:
            return starts
        starts.append(start)
        offset = start + 1


def _line_at_offset(lines: list[_Line], offset: int) -> int | None:
    """Return the line at ``offset`` or the next line after a separator."""
    for index, line in enumerate(lines):
        if line.start <= offset < line.end:
            return index
        if offset == line.start:
            return index
        if offset < line.start:
            return index
    return len(lines) - 1 if lines else None


def _replace_occurrences(text: str, starts: list[int], old: str, new: str) -> str:
    pieces: list[str] = []
    previous = 0
    for start in starts:
        pieces.append(text[previous:start])
        pieces.append(new)
        previous = start + len(old)
    pieces.append(text[previous:])
    return "".join(pieces)


def _map_offset(offset: int, starts: list[int], old_length: int, new_length: int) -> int:
    mapped = offset
    for start in starts:
        if offset < start:
            break
        if start <= offset < start + old_length:
            return start + (mapped - start)
        mapped += new_length - old_length
    return mapped


def _new_anchor(
    text: str, original_anchor_offset: int, starts: list[int], old: str, new: str
) -> str:
    if not text:
        return _digest_for_empty_file(text)
    mapped_offset = _map_offset(original_anchor_offset, starts, len(old), len(new))
    lines = _lines(text)
    line_index = _line_at_offset(lines, min(mapped_offset, len(text) - 1))
    if line_index is None:  # pragma: no cover - non-empty text always has a line
        return _digest_for_empty_file(text)
    return _digest_for_line(text, lines[line_index].content)


def _write_atomically(path: Path, text: str) -> None:
    """Write UTF-8 bytes through a same-directory temporary file."""
    mode = stat.S_IMODE(path.stat().st_mode)
    fd, temporary_name = tempfile.mkstemp(prefix=f".{path.name}.", suffix=".tmp", dir=path.parent)
    temporary_path = Path(temporary_name)
    try:
        with os.fdopen(fd, "wb") as temporary:
            fd = -1
            temporary.write(text.encode("utf-8"))
            temporary.flush()
            os.fsync(temporary.fileno())
        os.chmod(temporary_path, mode)
        os.replace(temporary_path, path)
    finally:
        if fd != -1:
            os.close(fd)
        try:
            temporary_path.unlink()
        except FileNotFoundError:
            pass


def apply_anchored_edit(
    path: Path,
    anchor: str,
    old: str,
    new: str,
    *,
    allow_multiple: bool = False,
) -> EditResult:
    """Apply one exact, anchored replacement atomically.

    ``old`` must occur exactly in the current file after the anchor is matched.
    Multiple exact occurrences are rejected unless ``allow_multiple`` is true,
    in which case every non-overlapping occurrence is replaced.  All validation
    occurs before the atomic write.
    """
    if not isinstance(anchor, str):
        raise TypeError("anchor must be a string")
    if not isinstance(old, str):
        raise TypeError("old must be a string")
    if not isinstance(new, str):
        raise TypeError("new must be a string")
    if not old:
        raise EditError("old text must not be empty")

    target = _confined_path(path)
    text = _read_utf8(target)
    anchor_index, lines = _resolve_anchor(text, anchor)
    if anchor_index is None:  # pragma: no cover - _resolve_anchor always returns or raises
        raise _anchor_not_found(anchor, lines)

    starts = _find_occurrences(text, old)
    occurrences = len(starts)
    if occurrences == 0:
        raise EditError(
            f"old text was not found in the edit region for anchor {anchor!r}; "
            f"line context: {_line_context(lines)}"
        )
    if occurrences > 1 and not allow_multiple:
        raise AmbiguousEditError(
            f"ambiguous edit: old text occurs {occurrences} times; "
            "pass allow_multiple=True to replace all occurrences"
        )

    selected = starts if allow_multiple else starts[:1]
    if any(
        current < previous + len(old)
        for previous, current in zip(selected, selected[1:], strict=False)
    ):
        raise AmbiguousEditError("ambiguous edit: old text occurrences overlap")

    changed = _replace_occurrences(text, selected, old, new)
    new_anchor = _new_anchor(changed, lines[anchor_index].start, selected, old, new)
    _write_atomically(target, changed)
    return EditResult(
        applied=True,
        anchor_matched=True,
        occurrences=occurrences,
        new_anchor=new_anchor,
    )


def verify_anchor(path: Path, anchor: str) -> bool:
    """Return whether ``anchor`` still matches one line in the current file."""
    if not isinstance(anchor, str):
        raise TypeError("anchor must be a string")
    target = _confined_path(path)
    try:
        text = _read_utf8(target)
    except (OSError, UnicodeError):
        return False

    expected = anchor.lower()
    if not _is_digest(expected):
        return False
    lines = _lines(text)
    if not lines:
        return _digest_for_empty_file(text) == expected
    context = _normalized_context(text).encode("utf-8")
    return any(_digest_for_line(text, line.content, context) == expected for line in lines)


__all__ = [
    "AmbiguousAnchorError",
    "AmbiguousEditError",
    "AnchorNotFoundError",
    "EditError",
    "EditResult",
    "anchor_of",
    "apply_anchored_edit",
    "verify_anchor",
]
