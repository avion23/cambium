"""Nuntius IPC: NDJSON framing + message helpers (M1).

Transport is JSON-Lines over stdio (one JSON object per ``\\n``-terminated
line, UTF-8). This module is the framing layer only: it carries bytes and
never interprets message payloads (architecture §4, §8.2).

Framing rules implemented here (docs/architecture/architecture.md §5.1):
- Partial lines are buffered across reads; a message boundary is the newline.
- Blank and whitespace-only lines are skipped.
- Lines that are not a single JSON object are logged and skipped; the stream
  is not corrupted (arch §5.1.4).
- A line longer than ``MAX_LINE_BYTES`` raises ``MessageTooLong`` after the
  receiver resyncs to the next newline (draft §1.4).
- A partial line at EOF is discarded; the receiver returns ``None`` (draft
  §1.3). EOF is never a protocol message.
"""

from __future__ import annotations

import asyncio
import json
import logging
import uuid
from typing import Any, Protocol, cast

logger = logging.getLogger(__name__)

MAX_LINE_BYTES = 1_048_576  # 1 MiB line cap; enforced before admission.


class MessageTooLong(ValueError):
    """A wire line exceeded the line-length cap before its terminating newline.

    Raised only after the reader has consumed the remainder of the oversized
    line (resynced to the next newline), so the stream stays usable.
    """

    def __init__(self, length: int) -> None:
        super().__init__(f"line exceeds the message length cap ({length} bytes)")
        self.length = length


class _FrameWriter(Protocol):
    def write(self, frame: bytes, /) -> None: ...


class _ReaderWithLimit(Protocol):
    _limit: int


def make_request_id(prefix: str = "req") -> str:
    """Return a globally unique request id: ``<prefix>-<uuid4 hex>``."""
    return f"{prefix}-{uuid.uuid4().hex}"


def encode_message(msg: dict[str, Any]) -> bytes | None:
    """Encode ``msg`` as one newline-terminated JSON line (``MAX_LINE_BYTES``-checked).

    Returns ``None`` when the encoded frame would exceed ``MAX_LINE_BYTES``
    so callers can pre-check the return before queueing and raise
    :class:`MessageTooLong` (or otherwise handle it) themselves.
    """
    content = json.dumps(msg).encode("utf-8")
    if len(content) > MAX_LINE_BYTES:
        return None
    return content + b"\n"


def write_frame(writer: _FrameWriter, frame: bytes) -> None:
    """Queue one pre-encoded newline-terminated wire frame on ``writer``."""
    writer.write(frame)


def write_message(writer: _FrameWriter, msg: dict[str, Any]) -> None:
    """Encode ``msg`` as one newline-terminated JSON line and queue it.

    The caller is responsible for draining the writer (``await
    writer.drain()``) so the message is actually flushed to the pipe.
    Raises :class:`MessageTooLong` when the encoded frame would exceed
    ``MAX_LINE_BYTES``; the caller must handle the oversized message instead
    of letting an over-cap frame on the wire force the receiver to resync.
    """
    content = json.dumps(msg).encode("utf-8")
    if len(content) > MAX_LINE_BYTES:
        raise MessageTooLong(len(content))
    write_frame(writer, content + b"\n")


async def _discard_to_newline(reader: asyncio.StreamReader) -> None:
    """Consume bytes up to and including the next newline (or EOF), discarding.

    Reads one byte at a time so bytes past the newline (the next line) are
    never consumed.
    """
    while True:
        byte = await reader.read(1)
        if not byte or byte == b"\n":
            return


async def _read_line(reader: asyncio.StreamReader, limit: int) -> bytes | None:
    """Read exactly one newline-terminated line, bounded by ``limit`` bytes.

    Returns the line bytes including its trailing newline, or ``None`` when
    EOF is reached. ``readuntil`` preserves bytes after the first newline and
    avoids one coroutine step per byte. A reader constructed with a smaller
    limit is temporarily raised for this call and restored afterwards.
    Oversized input is resynchronized to the next newline before
    ``MessageTooLong`` is raised.

    The temporary reader limit preserves the public ``limit`` argument even
    for in-memory readers created with asyncio's smaller default.
    """
    reader_with_limit = cast(_ReaderWithLimit, reader)
    original_limit = reader_with_limit._limit
    reader_with_limit._limit = limit
    try:
        try:
            return await reader.readuntil(b"\n")
        except asyncio.IncompleteReadError as exc:
            return exc.partial or None
        except asyncio.LimitOverrunError as exc:
            await reader.read(exc.consumed)
            await _discard_to_newline(reader)
            raise MessageTooLong(exc.consumed) from exc
    finally:
        reader_with_limit._limit = original_limit


async def read_message(
    reader: asyncio.StreamReader, *, limit: int = MAX_LINE_BYTES
) -> dict[str, Any] | None:
    """Read the next protocol message from the NDJSON stream.

    Buffers partial lines across reads. Blank lines and lines that are not a
    single JSON object are logged and skipped (arch §5.1.4). Returns ``None``
    at EOF (a partial line at EOF is discarded per draft §1.3). Raises
    ``MessageTooLong`` when a line exceeds ``limit`` bytes, after resyncing to
    the next newline.
    """
    while True:
        line = await _read_line(reader, limit)
        if line is None:
            return None
        if not line.endswith(b"\n"):
            # Partial line at EOF: discard (draft §1.3).
            return None
        content = line[:-1]
        if not content.strip():
            continue
        try:
            msg = json.loads(content.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError, RecursionError) as exc:
            logger.debug("skipping unparseable line: %s", exc)
            continue
        if not isinstance(msg, dict):
            logger.debug("skipping non-object JSON line: %r", msg)
            continue
        return msg
