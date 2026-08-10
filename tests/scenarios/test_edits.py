"""Scenario tests for deterministic hash-anchored file edits."""

from __future__ import annotations

from pathlib import Path

import pytest

from cambium.edits import EditResult, anchor_of, apply_anchored_edit, verify_anchor


def _file(tmp_path: Path, name: str = "fixture.txt") -> Path:
    path = tmp_path / name
    path.write_text("before\nanchor\nold value\nafter\n", encoding="utf-8")
    return path


def test_happy_path_returns_verifiable_new_anchor(tmp_path, monkeypatch) -> None:
    monkeypatch.chdir(tmp_path)
    path = _file(tmp_path)

    result = apply_anchored_edit(path, "anchor", "old value", "new value")

    assert result == EditResult(True, True, 1, result.new_anchor)
    assert result.new_anchor is not None
    assert path.read_text(encoding="utf-8") == "before\nanchor\nnew value\nafter\n"
    assert verify_anchor(path, result.new_anchor)


def test_multiple_occurrences_are_rejected_without_writing(tmp_path, monkeypatch) -> None:
    monkeypatch.chdir(tmp_path)
    path = tmp_path / "ambiguous.txt"
    original = "anchor\nold\nold\n"
    path.write_text(original, encoding="utf-8")

    with pytest.raises(ValueError, match=r"2"):
        apply_anchored_edit(path, "anchor", "old", "new")

    assert path.read_text(encoding="utf-8") == original


def test_allow_multiple_replaces_all_exact_occurrences(tmp_path, monkeypatch) -> None:
    monkeypatch.chdir(tmp_path)
    path = tmp_path / "multiple.txt"
    path.write_text("anchor\nold\nold\n", encoding="utf-8")

    result = apply_anchored_edit(path, "anchor", "old", "new", allow_multiple=True)

    assert result.occurrences == 2
    assert path.read_text(encoding="utf-8") == "anchor\nnew\nnew\n"


def test_anchor_not_found_includes_line_context(tmp_path, monkeypatch) -> None:
    monkeypatch.chdir(tmp_path)
    path = _file(tmp_path)

    with pytest.raises(ValueError, match=r"anchor.*missing.*line 2.*anchor"):
        apply_anchored_edit(path, "missing", "old value", "new value")


def test_verify_anchor_detects_a_later_clobbering_edit(tmp_path, monkeypatch) -> None:
    monkeypatch.chdir(tmp_path)
    path = _file(tmp_path)

    result = apply_anchored_edit(path, "anchor", "old value", "new value")
    assert result.new_anchor is not None
    assert verify_anchor(path, result.new_anchor)

    path.write_bytes(path.read_bytes().replace(b"new value", b"clobbered"))

    assert not verify_anchor(path, result.new_anchor)


def test_duplicate_anchor_lines_are_rejected_as_ambiguous(tmp_path, monkeypatch) -> None:
    monkeypatch.chdir(tmp_path)
    path = tmp_path / "duplicate-anchor.txt"
    path.write_text("anchor\nold\nanchor\nold\n", encoding="utf-8")

    with pytest.raises(ValueError, match=r"anchor.*ambiguous.*2"):
        apply_anchored_edit(path, "anchor", "old", "new", allow_multiple=True)


def test_hash_anchor_can_be_reused_and_multiple_mode_is_explicit(
    tmp_path, monkeypatch
) -> None:
    monkeypatch.chdir(tmp_path)
    path = _file(tmp_path)
    expected_anchor = anchor_of(path.read_text(encoding="utf-8"), "anchor")

    result = apply_anchored_edit(path, expected_anchor, "old value", "new value")

    assert result.anchor_matched
    assert result.applied
    assert result.occurrences == 1


def test_unicode_content_and_non_ascii_path_are_byte_safe(tmp_path, monkeypatch) -> None:
    monkeypatch.chdir(tmp_path)
    path = _file(tmp_path, "日本語-é.txt")
    path.write_bytes("προοίμιο\n🔒 アンカー\n旧値 café\n終端\n".encode())

    result = apply_anchored_edit(path, "🔒 アンカー", "旧値 café", "新値 ☕")

    assert result.new_anchor is not None
    assert path.read_bytes() == "προοίμιο\n🔒 アンカー\n新値 ☕\n終端\n".encode()
    assert verify_anchor(path, result.new_anchor)


def test_old_must_be_present_exactly(tmp_path, monkeypatch) -> None:
    monkeypatch.chdir(tmp_path)
    path = _file(tmp_path)

    with pytest.raises(ValueError, match="old text was not found"):
        apply_anchored_edit(path, "anchor", "missing", "changed")


def test_path_outside_cwd_is_rejected(tmp_path, monkeypatch) -> None:
    monkeypatch.chdir(tmp_path)
    outside = tmp_path.parent / "outside.txt"
    outside.write_text("anchor\nold\n", encoding="utf-8")

    with pytest.raises(ValueError, match="outside current working directory"):
        apply_anchored_edit(outside, "anchor", "old", "new")
