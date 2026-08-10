"""Scenario tests for the eval-harness-only LLM response cache."""

from __future__ import annotations

import os
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

from cambium.eval_cache import EvalCache

PROMPT = {
    "Task": {"id": "task-1", "instruction": "inspect the patch"},
    "Context": {"files": ["src/app.py"], "revision": 7},
    "Tool Outputs": [{"name": "git diff", "output": "clean"}],
}


def test_key_is_deterministic_and_model_sensitive() -> None:
    reordered_prompt = {
        "Tool Outputs": PROMPT["Tool Outputs"],
        "Context": PROMPT["Context"],
        "Task": PROMPT["Task"],
    }

    first = EvalCache.key(PROMPT, provider="provider-a", model="model-a")

    assert first == EvalCache.key(
        reordered_prompt, provider="provider-a", model="model-a"
    )
    assert first != EvalCache.key(PROMPT, provider="provider-a", model="model-b")


def test_different_provider_yields_different_key() -> None:
    key_a = EvalCache.key(PROMPT, provider="provider-a", model="model-a")
    key_b = EvalCache.key(PROMPT, provider="provider-b", model="model-a")

    assert key_a != key_b


def test_different_params_yield_different_key() -> None:
    key_cold = EvalCache.key(
        PROMPT, provider="provider-a", model="model-a", params={"temperature": 0.0}
    )
    key_hot = EvalCache.key(
        PROMPT, provider="provider-a", model="model-a", params={"temperature": 1.0}
    )

    assert key_cold != key_hot
    assert key_cold == EvalCache.key(
        PROMPT, provider="provider-a", model="model-a", params={"temperature": 0.0}
    )


def test_different_prompt_content_yields_different_key() -> None:
    other_prompt = dict(PROMPT)
    other_prompt["Task"] = {"id": "task-2", "instruction": "inspect the patch"}

    assert EvalCache.key(
        PROMPT, provider="provider-a", model="model-a"
    ) != EvalCache.key(other_prompt, provider="provider-a", model="model-a")


def test_key_is_independent_of_dict_key_order() -> None:
    params_in_order = {"temperature": 0.0, "max_tokens": 128}
    params_reordered = {"max_tokens": 128, "temperature": 0.0}

    assert EvalCache.key(
        {"a": 1, "b": 2}, provider="provider-a", model="model-a"
    ) == EvalCache.key({"b": 2, "a": 1}, provider="provider-a", model="model-a")
    assert EvalCache.key(
        PROMPT, provider="provider-a", model="model-a", params=params_in_order
    ) == EvalCache.key(
        PROMPT, provider="provider-a", model="model-a", params=params_reordered
    )


def test_put_get_roundtrip(tmp_path: Path) -> None:
    cache = EvalCache(tmp_path / "cache", enabled=True)
    key = cache.key({"task": "roundtrip"}, provider="provider-a", model="model-a")
    result = {"answer": "accepted", "score": 0.97}

    cache.put(key, result)

    assert cache.get(key) == result
    assert (tmp_path / "cache" / key[:2] / f"{key}.json").is_file()


def test_disabled_cache_does_not_read_or_write(tmp_path: Path) -> None:
    cache = EvalCache(tmp_path / "cache")
    key = cache.key({"task": "disabled"}, provider="provider-a", model="model-a")

    cache.put(key, {"answer": "not stored"})

    assert cache.get(key) is None
    assert not (tmp_path / "cache").exists()


def test_corrupt_file_is_a_cache_miss(tmp_path: Path) -> None:
    cache = EvalCache(tmp_path / "cache", enabled=True)
    key = cache.key({"task": "corrupt"}, provider="provider-a", model="model-a")
    path = tmp_path / "cache" / key[:2] / f"{key}.json"
    path.parent.mkdir(parents=True)
    path.write_text("{not valid json", encoding="utf-8")

    assert cache.get(key) is None


def test_pruning_keeps_newest_entries(tmp_path: Path) -> None:
    cache = EvalCache(tmp_path / "cache", max_entries=2, enabled=True)
    keys = [
        cache.key({"task": index}, provider="provider-a", model="model-a")
        for index in range(3)
    ]

    for index, key in enumerate(keys):
        cache.put(key, {"task": index})
        os.utime(tmp_path / "cache" / key[:2] / f"{key}.json", (index + 1, index + 1))

    assert len(list((tmp_path / "cache").rglob("*.json"))) == 2
    assert cache.get(keys[0]) is None
    assert cache.get(keys[1]) == {"task": 1}
    assert cache.get(keys[2]) == {"task": 2}


def test_concurrent_puts_are_readable(tmp_path: Path) -> None:
    cache = EvalCache(tmp_path / "cache", max_entries=64, enabled=True)
    entries = [
        (
            cache.key({"task": index}, provider="provider-a", model="model-a"),
            {"task": index},
        )
        for index in range(32)
    ]

    with ThreadPoolExecutor(max_workers=8) as executor:
        list(executor.map(lambda item: cache.put(*item), entries))

    assert [cache.get(key) for key, _ in entries] == [result for _, result in entries]
