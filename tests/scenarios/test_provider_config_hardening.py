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
    """Assert an invalid provider entry is dropped, recorded, and warned about."""

    caplog.set_level(logging.WARNING, logger="cambium.provider_config")
    assert load_providers(path) == []
    sidecar = path.with_name(path.name + ".quarantine")
    records = json.loads(sidecar.read_text(encoding="utf-8"))
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


@pytest.mark.parametrize(
    ("missing", "match"),
    [
        ("name", r"providers\[0\]: missing required field\(s\): name"),
        ("tier", r"providers\[0\]: missing required field\(s\): tier"),
        ("base_url", r"providers\[0\]: missing required field\(s\): base_url"),
        ("api_key_env", r"providers\[0\]: missing required field\(s\): api_key_env"),
    ],
)
def test_missing_required_provider_fields_are_quarantined(
    tmp_path: Path, missing: str, match: str, caplog: pytest.LogCaptureFixture
) -> None:
    value = _provider()
    del value[missing]

    _assert_quarantined(_write(tmp_path / "providers.json", {"providers": [value]}), match, caplog)


def test_missing_root_providers_field_fails_closed(tmp_path: Path) -> None:
    _assert_config_error(
        _write(tmp_path / "providers.json", {}),
        r"root: missing required field\(s\): providers",
    )


@pytest.mark.parametrize("providers", [None, {}, "not-a-list", 1])
def test_providers_field_not_list_remains_structural_failure(
    tmp_path: Path, providers: object
) -> None:
    _assert_config_error(
        _write(tmp_path / "providers.json", {"providers": providers}),
        r"providers: must be a list",
    )


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
        r"providers\[0\]\.auth: invalid auth mode 'unknown'; expected api_key, codex_chatgpt",
        caplog,
    )


def test_duplicate_provider_names_fail_closed(tmp_path: Path) -> None:
    _assert_config_error(
        _write(
            tmp_path / "providers.json",
            {"providers": [_provider(), _provider()]},
        ),
        r"providers\[1\]\.name: duplicate provider name 'openai'",
    )


def test_provider_env_name_collisions_fail_closed(tmp_path: Path) -> None:
    first = _provider("first.one")
    second = _provider("first-one")

    _assert_config_error(
        _write(tmp_path / "providers.json", {"providers": [first, second]}),
        r"providers\[1\]\.name: provider mapping collides with provider 'first.one'",
    )


@pytest.mark.parametrize("document", [None, [], "providers", 1])
def test_non_object_root_fails_closed(tmp_path: Path, document: object) -> None:
    _assert_config_error(
        _write(tmp_path / "providers.json", document),
        r"root: must be an object with a 'providers' field",
    )


def test_empty_provider_name_is_quarantined(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    value = _provider()
    value["name"] = ""

    _assert_quarantined(
        _write(tmp_path / "providers.json", {"providers": [value]}),
        r"providers\[0\]\.name: must be a valid provider id",
        caplog,
    )


@pytest.mark.parametrize(
    "base_url",
    [
        "",
        "api.example.test/v1",
        "ftp://api.example.test/v1",
        "https://",
        "https://:443/v1",
        "https://api.example.test:bad/v1",
        "https://api.example.test:99999/v1",
        "https://[::1",
    ],
)
def test_bad_base_url_shapes_are_quarantined(
    tmp_path: Path, base_url: str, caplog: pytest.LogCaptureFixture
) -> None:
    _assert_quarantined(
        _write(
            tmp_path / "providers.json",
            {"providers": [_provider(base_url=base_url)]},
        ),
        r"providers\[0\]\.base_url: must be an absolute http\(s\) URL",
        caplog,
    )


@pytest.mark.parametrize(
    "base_url",
    [
        "https://api.example.test/v1?key=value",
        "https://api.example.test/v1#fragment",
    ],
)
def test_base_url_query_and_fragment_are_quarantined(
    tmp_path: Path, base_url: str, caplog: pytest.LogCaptureFixture
) -> None:
    _assert_quarantined(
        _write(
            tmp_path / "providers.json",
            {"providers": [_provider(base_url=base_url)]},
        ),
        r"providers\[0\]\.base_url: must not contain query parameters or a fragment",
        caplog,
    )


def test_unknown_provider_fields_are_quarantined(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    _assert_quarantined(
        _write(
            tmp_path / "providers.json",
            {"providers": [_provider(unexpected="reject-me")]},
        ),
        r"providers\[0\]: unknown field\(s\): 'unexpected'",
        caplog,
    )


@pytest.mark.parametrize("constant", ["NaN", "Infinity", "-Infinity"])
@pytest.mark.parametrize("field", ["timeout_s", "cooldown_s", "price", "token_window_allowance"])
def test_non_finite_numeric_constants_fail_closed(
    tmp_path: Path, field: str, constant: str
) -> None:
    value = _provider()
    constants = {
        "NaN": float("nan"),
        "Infinity": float("inf"),
        "-Infinity": float("-inf"),
    }
    value[field] = constants[constant]

    _assert_config_error(
        _write(tmp_path / "providers.json", {"providers": [value]}),
        r"root: non-standard JSON constant",
    )
