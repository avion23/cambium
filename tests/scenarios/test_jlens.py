from __future__ import annotations

import json
import math

import pytest

from cambium.jlens import JlenClient, JlenError


class _Response:
    def __init__(self, value: object) -> None:
        self.value = value

    def __enter__(self) -> _Response:
        return self

    def __exit__(self, *args: object) -> None:
        return None

    def read(self) -> bytes:
        return json.dumps(self.value).encode("utf-8")


def test_nonfinite_commitment_is_not_clamped_to_one() -> None:
    client = JlenClient("http://example.test")

    assert client.signal({"commitment": math.nan}, []) == 0.0
    assert client.signal({"commitment": math.inf}, []) == 0.0


def test_nonfinite_commitment_falls_back_to_valid_layer_rank() -> None:
    client = JlenClient("http://example.test")

    assert client.signal(
        {"commitment": math.nan, "layers": {"29": {"expected_rank": 1}}}, []
    ) == 1.0


@pytest.mark.parametrize("value", [None, 1, [], "score"])
def test_score_rejects_non_object_json(monkeypatch, value: object) -> None:
    client = JlenClient("http://example.test")

    def fake_urlopen(*args: object, **kwargs: object) -> _Response:
        return _Response(value)

    monkeypatch.setattr("urllib.request.urlopen", fake_urlopen)
    with pytest.raises(JlenError, match="non-object"):
        client.score([], [])


@pytest.mark.parametrize("value", [None, 1, []])
def test_signal_rejects_non_object_result(value: object) -> None:
    client = JlenClient("http://example.test")

    with pytest.raises(JlenError, match="non-object"):
        client.signal(value, [])  # type: ignore[arg-type]
