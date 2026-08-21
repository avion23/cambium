import json
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


@pytest.mark.parametrize(
    ("missing", "match"),
    [
        ("name", r"providers\[0\]: missing required field\(s\): name"),
        ("tier", r"providers\[0\]: missing required field\(s\): tier"),
        ("base_url", r"providers\[0\]: missing required field\(s\): base_url"),
        ("api_key_env", r"providers\[0\]: missing required field\(s\): api_key_env"),
    ],
)
def test_missing_required_provider_fields_fail_closed(
    tmp_path: Path, missing: str, match: str
) -> None:
    value = _provider()
    del value[missing]

    _assert_config_error(_write(tmp_path / "providers.json", {"providers": [value]}), match)


def test_missing_root_providers_field_fails_closed(tmp_path: Path) -> None:
    _assert_config_error(
        _write(tmp_path / "providers.json", {}),
        r"root: missing required field\(s\): providers",
    )


def test_unknown_auth_mode_fails_closed(tmp_path: Path) -> None:
    _assert_config_error(
        _write(tmp_path / "providers.json", {"providers": [_provider(auth="unknown")]}),
        r"providers\[0\]\.auth: invalid auth mode 'unknown'; expected api_key, codex_chatgpt",
    )


def test_duplicate_provider_names_fail_closed(tmp_path: Path) -> None:
    _assert_config_error(
        _write(
            tmp_path / "providers.json",
            {"providers": [_provider(), _provider()]},
        ),
        r"providers\[1\]\.name: duplicate provider name 'openai'",
    )


@pytest.mark.parametrize("document", [None, [], "providers", 1])
def test_non_object_root_fails_closed(tmp_path: Path, document: object) -> None:
    _assert_config_error(
        _write(tmp_path / "providers.json", document),
        r"root: must be an object with a 'providers' field",
    )


def test_empty_provider_name_fails_closed(tmp_path: Path) -> None:
    value = _provider()
    value["name"] = ""

    _assert_config_error(
        _write(tmp_path / "providers.json", {"providers": [value]}),
        r"providers\[0\]\.name: must be a valid provider id",
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
def test_bad_base_url_shapes_fail_closed(tmp_path: Path, base_url: str) -> None:
    _assert_config_error(
        _write(
            tmp_path / "providers.json",
            {"providers": [_provider(base_url=base_url)]},
        ),
        r"providers\[0\]\.base_url: must be an absolute http\(s\) URL",
    )


@pytest.mark.parametrize(
    "base_url",
    [
        "https://api.example.test/v1?key=value",
        "https://api.example.test/v1#fragment",
    ],
)
def test_base_url_query_and_fragment_fail_closed(tmp_path: Path, base_url: str) -> None:
    _assert_config_error(
        _write(
            tmp_path / "providers.json",
            {"providers": [_provider(base_url=base_url)]},
        ),
        r"providers\[0\]\.base_url: must not contain query parameters or a fragment",
    )


def test_unknown_provider_fields_are_rejected(tmp_path: Path) -> None:
    _assert_config_error(
        _write(
            tmp_path / "providers.json",
            {"providers": [_provider(unexpected="reject-me")]},
        ),
        r"providers\[0\]: unknown field\(s\): 'unexpected'",
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
