"""Deterministic robustness fuzzing for the NDJSON IPC framing layer."""

from __future__ import annotations

import asyncio
import json
import random
from typing import Any

import pytest

from cambium.ipc import MAX_LINE_BYTES, MessageTooLong, read_message

# 128 cases keep every byte-pattern mode (case % 8) sampled 16x; the line-length
# framing boundary itself is covered by test_read_message_one_mib_plus_one_byte_resyncs.
FUZZ_CASES = 128
MAX_RANDOM_BYTES = 8 * 1024
FUZZ_SEED = 0xC0FFEE
# Byte-at-a-time reads make this scenario machine-load sensitive; keep only a
# generous async timeout so it still catches a true hang without wall-time flake.
FUZZ_TIMEOUT_SECONDS = 180.0


def _reader_with(data: bytes) -> asyncio.StreamReader:
    reader = asyncio.StreamReader()
    reader.feed_data(data)
    reader.feed_eof()
    return reader


async def _read_fuzz_case(reader: asyncio.StreamReader) -> dict[str, Any] | None:
    try:
        result = await read_message(reader)
    except MessageTooLong:
        raise
    except Exception as exc:
        pytest.fail(f"read_message raised an unexpected {type(exc).__name__}: {exc}")
    assert result is None or isinstance(result, dict)
    return result


def _utf8_fragments(rng: random.Random, length: int) -> bytes:
    fragments = (
        "é".encode(),
        "雪".encode(),
        "🌙".encode(),
        b"\xc3",
        b"\xe2\x82",
        b"\xf0\x9f\x8c",
        b"\x80",
        b"\xff",
    )
    data = bytearray()
    while len(data) < length:
        data.extend(rng.choice(fragments))
    return bytes(data[:length])


def _random_payload(rng: random.Random, case: int) -> bytes:
    # Keep most cases short so deterministic payload generation stays bounded;
    # mode 4 still supplies near-cap single-line inputs.
    length = int(rng.random() ** 2 * (MAX_RANDOM_BYTES + 1))
    mode = case % 8
    if mode == 0:
        return rng.randbytes(length)
    if mode == 1:
        return b"\n" * length
    if mode == 2:
        return b"\r" * length
    if mode == 3:
        return bytes(rng.choice((0x00, 0x0A, 0x0D, 0x20)) for _ in range(length))
    if mode == 4:
        length = max(length, MAX_RANDOM_BYTES - 1024)
        return bytes(value if value != 0x0A else 0x0B for value in rng.randbytes(length))
    if mode == 5:
        return _utf8_fragments(rng, length)
    if mode == 6:
        data = bytearray(rng.randbytes(length))
        for offset in range(0, length, 31):
            data[offset] = 0
        return bytes(data)
    data = bytearray(rng.randbytes(length))
    for offset in range(0, length, 257):
        data[offset] = 0x0A
    return bytes(data)


def test_read_message_deterministic_random_bytes_never_escapes_or_hangs() -> None:
    async def scenario() -> None:
        rng = random.Random(FUZZ_SEED)
        for case in range(FUZZ_CASES):
            payload = _random_payload(rng, case)
            await _read_fuzz_case(_reader_with(payload))

    asyncio.run(asyncio.wait_for(scenario(), timeout=FUZZ_TIMEOUT_SECONDS))


def test_read_message_one_mib_plus_one_byte_resyncs() -> None:
    async def scenario() -> None:
        reader = _reader_with(b"x" * (MAX_LINE_BYTES + 1) + b"\n" + b'{"type":"after-too-long"}\n')
        with pytest.raises(MessageTooLong) as raised:
            await asyncio.wait_for(read_message(reader), timeout=5.0)
        assert raised.value.length == MAX_LINE_BYTES + 1
        assert await asyncio.wait_for(read_message(reader), timeout=5.0) == {
            "type": "after-too-long"
        }

    asyncio.run(scenario())


def test_read_message_skips_one_thousand_blank_lines() -> None:
    async def scenario() -> None:
        reader = _reader_with(b"\n" * 1000 + b'{"type":"after-blanks"}\n')
        assert await _read_fuzz_case(reader) == {"type": "after-blanks"}

    asyncio.run(scenario())


def test_read_message_skips_deep_json_garbage(monkeypatch) -> None:
    async def scenario() -> None:
        deep_array = b"[" * 1000 + b"0" + b"]" * 1000
        reader = _reader_with(deep_array + b'\n{"type":"after-deep"}\n')
        assert await _read_fuzz_case(reader) == {"type": "after-deep"}

    real_loads = json.loads
    deep_text = (b"[" * 1000 + b"0" + b"]" * 1000).decode()

    def loads(value: str, *args: Any, **kwargs: Any) -> Any:
        if value == deep_text:
            raise RecursionError("synthetic parser depth limit")
        return real_loads(value, *args, **kwargs)

    monkeypatch.setattr(json, "loads", loads)
    asyncio.run(scenario())


def test_read_message_accepts_trailing_crlf() -> None:
    async def scenario() -> None:
        reader = _reader_with(b'{"type":"crlf"}\r\n')
        assert await _read_fuzz_case(reader) == {"type": "crlf"}
        assert await _read_fuzz_case(reader) is None

    asyncio.run(scenario())


def test_read_message_discards_unterminated_final_line() -> None:
    async def scenario() -> None:
        reader = _reader_with(b'{"type":"unterminated"}')
        assert await _read_fuzz_case(reader) is None

    asyncio.run(scenario())
