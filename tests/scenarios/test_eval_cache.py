"""Scenario tests for the eval-harness-only LLM response cache."""

from __future__ import annotations

import os
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

from cambium.eval_cache import EvalCache


def test_key_is_deterministic_and_model_sensitive() -> None:
    prompt = {
        "Task": {"id": "task-1", "instruction": "inspect the patch"},
        "Context": {"files": ["src/app.py"], "revision": 7},
        "Tool Outputs": [{"name": "git diff", "output": "clean"}],
    }
    reordered_prompt = {
        "Tool Outputs": prompt["Tool Outputs"],
        "Context": prompt["Context"],
        "Task": prompt["Task"],
    }

    first = EvalCache.key(prompt, "model-a")

    assert first == EvalCache.key(reordered_prompt, "model-a")
    assert first != EvalCache.key(prompt, "model-b")


def test_put_get_roundtrip(tmp_path: Path) -> None:
    cache = EvalCache(tmp_path / "cache", enabled=True)
    key = cache.key({"task": "roundtrip"}, "model-a")
    result = {"answer": "accepted", "score": 0.97}

    cache.put(key, result)

    assert cache.get(key) == result
    assert (tmp_path / "cache" / key[:2] / f"{key}.json").is_file()


def test_disabled_cache_does_not_read_or_write(tmp_path: Path) -> None:
    cache = EvalCache(tmp_path / "cache")
    key = cache.key({"task": "disabled"}, "model-a")

    cache.put(key, {"answer": "not stored"})

    assert cache.get(key) is None
    assert not (tmp_path / "cache").exists()


def test_corrupt_file_is_a_cache_miss(tmp_path: Path) -> None:
    cache = EvalCache(tmp_path / "cache", enabled=True)
    key = cache.key({"task": "corrupt"}, "model-a")
    path = tmp_path / "cache" / key[:2] / f"{key}.json"
    path.parent.mkdir(parents=True)
    path.write_text("{not valid json", encoding="utf-8")

    assert cache.get(key) is None


def test_pruning_keeps_newest_entries(tmp_path: Path) -> None:
    cache = EvalCache(tmp_path / "cache", max_entries=2, enabled=True)
    keys = [cache.key({"task": index}, "model-a") for index in range(3)]

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
        (cache.key({"task": index}, "model-a"), {"task": index})
        for index in range(32)
    ]

    with ThreadPoolExecutor(max_workers=8) as executor:
        list(executor.map(lambda item: cache.put(*item), entries))

    assert [cache.get(key) for key, _ in entries] == [result for _, result in entries]
