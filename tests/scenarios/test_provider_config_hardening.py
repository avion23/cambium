import json
import logging
import re
from pathlib import Path

import pytest

from cambium.auth import derived_env_name
from cambium.provider_config import load_providers


def _provider(name: str = "openai", **overrides: object) -> dict[str, object]:
    value: dict[str, object] = {
        "name": name,
        "tier": "strong",
        "base_url": "https://api.example.test/v1",
        "api_key_env": derived_env_name(name),
        "api_key": f"sk-hardening-{name}",
        "model": "example-model",
    }
    value.update(overrides)
    return value


def _write(path: Path, document: object) -> Path:
    path.write_text(json.dumps(document), encoding="utf-8")
    return path


def _assert_config_error(path: Path, match: str) -> None:
    with pytest.raises(ValueError, match=match) as raised:
        load_providers(path)
    assert type(raised.value) is ValueError


def _assert_quarantined(path: Path, match: str, caplog: pytest.LogCaptureFixture) -> None:
    caplog.set_level(logging.WARNING, logger="cambium.provider_config")
    assert load_providers(path) == []
    records = json.loads(path.with_name(path.name + ".quarantine").read_text(encoding="utf-8"))
    assert isinstance(records, list) and len(records) == 1
    record = records[0]
    assert isinstance(record, dict)
    assert {"entry", "reason", "quarantined_at"} <= set(record)
    assert re.search(match, str(record["reason"]))
    assert any(
        item.name == "cambium.provider_config"
        and item.levelno == logging.WARNING
        and getattr(item, "event", None) == "provider_config_quarantined"
        for item in caplog.records
    )


def test_missing_required_provider_fields_are_quarantined(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    for index, (missing, match) in enumerate(
        (
            ("name", r"providers\[0\]: missing required field\(s\): name"),
            ("tier", r"providers\[0\]: missing required field\(s\): tier"),
            ("base_url", r"providers\[0\]: missing required field\(s\): base_url"),
        )
    ):
        value = _provider()
        del value[missing]
        _assert_quarantined(
            _write(tmp_path / f"providers-{index}.json", {"providers": [value]}), match, caplog
        )


def test_structural_root_failures_are_closed(tmp_path: Path) -> None:
    cases = (
        ({}, r"root: missing required field\(s\): providers"),
        ({"providers": None}, r"providers: must be a list"),
        ({"providers": {}}, r"providers: must be a list"),
        ({"providers": "not-a-list"}, r"providers: must be a list"),
        ({"providers": 1}, r"providers: must be a list"),
        (None, r"root: must be an object with a 'providers' field"),
        ([], r"root: must be an object with a 'providers' field"),
        ("providers", r"root: must be an object with a 'providers' field"),
        (1, r"root: must be an object with a 'providers' field"),
    )
    for index, (document, match) in enumerate(cases):
        _assert_config_error(_write(tmp_path / f"providers-{index}.json", document), match)


def test_invalid_json_remains_structural_failure(tmp_path: Path) -> None:
    path = tmp_path / "providers.json"
    path.write_text("{not-json", encoding="utf-8")
    _assert_config_error(path, r"invalid provider config JSON")


def test_unknown_top_level_fields_remain_structural_failure(tmp_path: Path) -> None:
    _assert_config_error(
        _write(tmp_path / "providers.json", {"providers": [_provider()], "unexpected": True}),
        r"root: unknown field\(s\): 'unexpected'",
    )


def test_unknown_auth_mode_is_quarantined(tmp_path: Path, caplog: pytest.LogCaptureFixture) -> None:
    _assert_quarantined(
        _write(tmp_path / "providers.json", {"providers": [_provider(auth="unknown")]}),
        r"providers\[0\]\.auth: invalid auth mode 'unknown'; expected api_key, codex_chatgpt, none",
        caplog,
    )


def test_duplicate_provider_identity_fails_closed(tmp_path: Path) -> None:
    _assert_config_error(
        _write(tmp_path / "duplicate.json", {"providers": [_provider(), _provider()]}),
        r"providers\[1\]\.name: duplicate provider name 'openai'",
    )
    _assert_config_error(
        _write(
            tmp_path / "collision.json",
            {"providers": [_provider("first.one"), _provider("first-one")]},
        ),
        r"providers\[1\]\.name: provider mapping collides with provider 'first.one'",
    )


def test_invalid_provider_shapes_are_quarantined(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    empty_name = _provider()
    empty_name["name"] = ""
    cases = [
        (empty_name, r"providers\[0\]\.name: must be a valid provider id"),
        (_provider(unexpected="reject-me"), r"providers\[0\]: unknown field\(s\): 'unexpected'"),
    ]
    bad_urls = (
        "",
        "api.example.test/v1",
        "ftp://api.example.test/v1",
        "https://",
        "https://:443/v1",
        "https://api.example.test:bad/v1",
        "https://api.example.test:99999/v1",
        "https://[::1",
    )
    cases.extend(
        (_provider(base_url=url), r"providers\[0\]\.base_url: must be an absolute http\(s\) URL")
        for url in bad_urls
    )
    cases.extend(
        (
            _provider(base_url=url),
            r"providers\[0\]\.base_url: must not contain query parameters or a fragment",
        )
        for url in (
            "https://api.example.test/v1?key=value",
            "https://api.example.test/v1#fragment",
        )
    )
    for index, (provider, match) in enumerate(cases):
        _assert_quarantined(
            _write(tmp_path / f"providers-{index}.json", {"providers": [provider]}),
            match,
            caplog,
        )


def test_non_finite_numeric_constants_fail_closed(tmp_path: Path) -> None:
    constants = (float("nan"), float("inf"), float("-inf"))
    fields = ("timeout_s", "cooldown_s", "price", "token_window_allowance")
    cases = ((field, value) for field in fields for value in constants)
    for index, (field, value) in enumerate(cases):
        provider = _provider()
        provider[field] = value
        _assert_config_error(
            _write(tmp_path / f"providers-{index}.json", {"providers": [provider]}),
            r"root: non-standard JSON constant",
        )
