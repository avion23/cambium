"""IPC edge-case scenario tests."""

from __future__ import annotations

import asyncio

import pytest

from cambium.ipc import (
    MAX_LINE_BYTES,
    MessageTooLong,
    encode_message,
    read_message,
    write_frame,
    write_message,
)


class _RecordingWriter:
    def __init__(self) -> None:
        self.frames: list[bytes] = []

    def write(self, frame: bytes) -> None:
        self.frames.append(frame)


class _PartialErrorWriter:
    def __init__(self, written_bytes: int, error: type[OSError]) -> None:
        self.data = bytearray()
        self.written_bytes = written_bytes
        self.error = error

    def write(self, frame: bytes) -> None:
        self.data.extend(frame[: self.written_bytes])
        raise self.error("pipe write failed")


def _reader(data: bytes) -> asyncio.StreamReader:
    reader = asyncio.StreamReader()
    reader.feed_data(data)
    reader.feed_eof()
    return reader


@pytest.mark.parametrize("error", (BlockingIOError, BrokenPipeError))
def test_partial_frame_followed_by_frame_desynchronizes_line_reader(
    error: type[OSError],
) -> None:
    async def scenario() -> None:
        first = encode_message({"type": "first", "value": "payload"})
        second = encode_message({"type": "second"})
        assert first is not None
        assert second is not None
        writer = _PartialErrorWriter(len(first) // 2, error)

        with pytest.raises(error):
            write_frame(writer, first)

        reader = _reader(bytes(writer.data) + second)
        assert await read_message(reader) is None

    asyncio.run(scenario())


def test_partial_frame_at_eof_is_discarded() -> None:
    async def scenario() -> None:
        frame = encode_message({"type": "partial", "value": "payload"})
        assert frame is not None
        writer = _PartialErrorWriter(len(frame) // 2, BrokenPipeError)

        with pytest.raises(BrokenPipeError):
            write_frame(writer, frame)

        assert await read_message(_reader(bytes(writer.data))) is None

    asyncio.run(scenario())


def test_oversized_message_is_rejected_before_writer_write() -> None:
    message = {"value": "x" * MAX_LINE_BYTES}
    assert encode_message(message) is None
    writer = _RecordingWriter()

    with pytest.raises(MessageTooLong):
        write_message(writer, message)

    assert writer.frames == []


def test_control_characters_are_escaped_inside_one_wire_line() -> None:
    message = {"value": "line\nreturn\rtab\tbackspace\bformfeed\fnull\x00unit\x1f"}
    frame = encode_message(message)
    assert frame is not None
    assert frame.count(b"\n") == 1
    assert all(byte >= 0x20 or byte == 0x0A for byte in frame)

    async def scenario() -> None:
        assert await read_message(_reader(frame)) == message

    asyncio.run(scenario())


def test_non_utf8_line_is_skipped_and_next_frame_is_read() -> None:
    async def scenario() -> None:
        reader = _reader(b'{"type":"invalid","value":"\xff"}\n{"type":"valid"}\n')
        assert await read_message(reader) == {"type": "valid"}
        assert await read_message(reader) is None

    asyncio.run(scenario())
