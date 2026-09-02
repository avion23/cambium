"""Adversarial redaction coverage for the pi-session extractor."""

from __future__ import annotations

import base64
import random
from collections.abc import Callable, Iterator

import pytest

from cambium.redact import Redactor
from scripts.extract_pi import _redact_text

FUZZ_SEED = 20260824
FUZZ_CASES = 64
BASE64_MARKER = "<redacted:base64>"
_ALPHABET = "ABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789"

_HOMOGLYPHS = {
    "s": "ѕ",
    "k": "κ",
    "p": "р",
    "r": "г",
    "o": "ο",
    "j": "ј",
    "A": "А",
    "P": "Ρ",
}


def _synthetic_keys() -> Iterator[str]:
    generator = random.Random(FUZZ_SEED)
    for index in range(FUZZ_CASES):
        length = 24 + index % 29
        suffix = "".join(generator.choice(_ALPHABET) for _ in range(length))
        yield "sk-proj-" + suffix


def _zero_width(value: str) -> str:
    controls = ("\u200b", "\u2066", "\ufeff")
    return "".join(
        character + controls[index % len(controls)] for index, character in enumerate(value)
    )


def _homoglyph(value: str) -> str:
    return "".join(_HOMOGLYPHS.get(character, character) for character in value)


def _html_entities(value: str) -> str:
    return "".join(f"&#x{ord(character):x};" for character in value)


def _split_lines(value: str) -> str:
    pieces: list[str] = []
    position = 0
    width = 1
    while position < len(value):
        pieces.append(value[position : position + width])
        position += width
        width = width % 5 + 1
    return "\n".join(pieces)


def _base64(value: str) -> str:
    return base64.b64encode(value.encode("ascii")).decode("ascii")


def _survived(kind: str, source: str, key: str, output: str) -> bool:
    """Check both the wire form and the decoded/compact form for survival."""

    if source in output or key in output:
        return True
    if kind == "split_lines":
        return "".join(source.split()) in "".join(output.split())
    if kind == "base64":
        return BASE64_MARKER not in output
    return False


def _assert_zero_survival(
    kind: str, transform: Callable[[str], str], *, require_base64_marker: bool = False
) -> None:
    redactor = Redactor(replacement="[REDACTED]")
    survivors = 0
    for key in _synthetic_keys():
        source = transform(key)
        output = _redact_text(source, redactor)
        survivors += _survived(kind, source, key, output)
        if require_base64_marker:
            assert BASE64_MARKER in output
    assert survivors == 0


@pytest.mark.parametrize(
    ("kind", "transform", "require_base64_marker"),
    [
        ("zero_width", _zero_width, False),
        ("homoglyph", _homoglyph, False),
        ("html_entities", _html_entities, False),
        ("split_lines", _split_lines, False),
        ("base64", _base64, True),
    ],
)
def test_key_fuzz_corpus_has_zero_survival(
    kind: str, transform: Callable[[str], str], require_base64_marker: bool
) -> None:
    _assert_zero_survival(kind, transform, require_base64_marker=require_base64_marker)
