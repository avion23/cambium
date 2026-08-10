"""Integration scenarios for the optional DSPy-to-Diffundo boundary."""

from __future__ import annotations

import asyncio
import importlib.util
import os
import subprocess
import sys
from pathlib import Path
from typing import Any

import pytest

from cambium.architectus import ArchitectusCore
from cambium.diffundo import CallResult, ProviderTier
from cambium.lm import ArchitectusLM, CambiumLM
from cambium.tasktree import build_tree


def _require_dspy() -> None:
    if importlib.util.find_spec("dspy") is None:
        pytest.skip("dspy extra is not installed")


def _tree(root: Path) -> tuple[str, ...]:
    if not root.exists():
        return ()
    return tuple(sorted(str(path.relative_to(root)) for path in root.rglob("*")))


class FakeDiffundo:
    def __init__(self, endpoint: str = "https://fake.invalid") -> None:
        self.endpoint = endpoint
        self.calls: list[dict[str, Any]] = []
        self.session_markers: list[object] = []

    async def call(
        self,
        tier: ProviderTier,
        prompt: dict[str, Any],
        *,
        model: str | None = None,
        budget_usd: float | None = None,
    ) -> CallResult:
        import dspy

        self.calls.append(
            {"endpoint": self.endpoint, "tier": tier, "prompt": prompt, "model": model}
        )
        self.session_markers.append(dspy.settings.cambium_session)
        content = "completion text"
        if prompt["messages"][0]["role"] == "system":
            content = '[{"action":"spawn","task_id":"root"}]'
        return CallResult(
            provider=self.endpoint,
            model=model or "fake-model",
            tier=tier,
            content=content,
            latency_s=0.01,
            usage={"prompt_tokens": 2, "completion_tokens": 2},
        )


def _call(lm: CambiumLM, prompt: str = "same prompt") -> list[str]:
    output = lm(messages=[{"role": "user", "content": prompt}])
    assert isinstance(output, list)
    return output


def test_dspy_import_and_construction_do_not_write_home(tmp_path: Path) -> None:
    _require_dspy()
    source = Path(__file__).resolve().parents[2] / "src"
    real_cache = Path.home() / ".dspy_cache"
    real_cache_before_exists = real_cache.exists()
    real_cache_before = _tree(real_cache)
    script = """
import sys
from cambium.diffundo import ProviderTier
from cambium.lm import CambiumLM

fake_diffundo = type("FakeDiffundo", (), {"call": lambda *args, **kwargs: None})()
CambiumLM(fake_diffundo, ProviderTier.FAST)
import dspy
assert dspy.cache.enable_disk_cache is False
assert dspy.cache.enable_memory_cache is False
"""
    env = os.environ.copy()
    env.pop("DSPY_CACHEDIR", None)
    env.pop("DSPY_CACHE_LIMIT", None)
    env.pop("Py_GIL_DISABLED", None)
    env["HOME"] = str(tmp_path)
    env["PYTHONPATH"] = str(source)
    completed = subprocess.run(
        [sys.executable, "-c", script],
        env=env,
        text=True,
        capture_output=True,
        check=False,
    )
    assert completed.returncode == 0, completed.stderr
    assert completed.stderr == ""
    assert _tree(tmp_path) == ()
    assert real_cache.exists() is real_cache_before_exists
    assert _tree(real_cache) == real_cache_before


def test_identical_calls_are_not_cached() -> None:
    _require_dspy()
    diffundo = FakeDiffundo()
    lm = CambiumLM(diffundo, ProviderTier.FAST, temperature=0.0)  # type: ignore[arg-type]
    assert _call(lm) == ["completion text"]
    assert _call(lm) == ["completion text"]
    assert len(diffundo.calls) == 2
    assert lm.cache is False
    assert lm.num_retries == 0
    assert lm.history == []
    assert all("cache" not in call["prompt"] for call in diffundo.calls)


def test_same_prompt_to_two_provider_endpoints_reaches_each_once() -> None:
    _require_dspy()
    first = FakeDiffundo("https://provider-a.invalid")
    second = FakeDiffundo("https://provider-b.invalid")
    assert _call(CambiumLM(first, ProviderTier.FAST)) == ["completion text"]  # type: ignore[arg-type]
    assert _call(CambiumLM(second, ProviderTier.FAST)) == ["completion text"]  # type: ignore[arg-type]
    assert len(first.calls) == len(second.calls) == 1
    assert first.calls[0]["endpoint"] != second.calls[0]["endpoint"]


def test_session_context_is_isolated_and_retains_no_prompt() -> None:
    _require_dspy()
    diffundo = FakeDiffundo()
    lm = CambiumLM(diffundo, ProviderTier.FAST)  # type: ignore[arg-type]
    dspy = sys.modules["dspy"]
    original_marker = object()
    with dspy.context(cambium_session=original_marker):
        assert _call(lm, "PROMPT-CANARY") == ["completion text"]
        assert dspy.settings.cambium_session is original_marker
    assert not hasattr(dspy.settings, "cambium_session")
    assert diffundo.session_markers[0] is not original_marker
    assert lm.history == []


def test_architectus_real_decide_port_uses_cambium_lm() -> None:
    _require_dspy()
    diffundo = FakeDiffundo()
    lm = CambiumLM(diffundo, ProviderTier.FAST)  # type: ignore[arg-type]
    tree = build_tree(
        {"tasks": [{"task_id": "root", "kind": "FEATURE", "depends_on": [], "spec": {}}]}
    )
    actions = asyncio.run(ArchitectusCore(ArchitectusLM(lm), tree=tree).step([{"kind": "tick"}]))
    assert actions == [{"action": "spawn", "task_id": "root"}]
    assert len(diffundo.calls) == 1


def test_core_module_imports_never_import_dspy() -> None:
    source = Path(__file__).resolve().parents[2] / "src"
    script = """
import sys
import builtins

real_import = builtins.__import__
def reject_dspy(name, *args, **kwargs):
    if name == "dspy" or name.startswith("dspy."):
        raise AssertionError("core imports dspy eagerly")
    return real_import(name, *args, **kwargs)

builtins.__import__ = reject_dspy
sys.path.insert(0, sys.argv[1])
import cambium
import cambium.architectus
import cambium.diffundo
import cambium.lm
import cambium.orchestrator
assert 'dspy' not in sys.modules
"""
    completed = subprocess.run(
        [sys.executable, "-I", "-c", script, str(source)],
        env=os.environ.copy(),
        text=True,
        capture_output=True,
        check=False,
    )
    assert completed.returncode == 0, completed.stderr


def test_free_threaded_build_is_rejected_at_lm_import() -> None:
    source = Path(__file__).resolve().parents[2] / "src"
    script = "import sys; sys.path.insert(0, sys.argv[1]); import cambium.lm"
    env = os.environ.copy()
    env["Py_GIL_DISABLED"] = "1"
    completed = subprocess.run(
        [sys.executable, "-c", script, str(source)],
        env=env,
        text=True,
        capture_output=True,
        check=False,
    )
    assert completed.returncode != 0
    assert "free-threaded CPython" in completed.stderr


@pytest.mark.parametrize("key", ["apiKey", "API-Key", "api_key"])
def test_secret_marker_variants_are_rejected(key: str) -> None:
    _require_dspy()
    with pytest.raises(ValueError, match="provider credentials"):
        CambiumLM(FakeDiffundo(), ProviderTier.FAST, **{key: "secret"})  # type: ignore[arg-type]


@pytest.mark.parametrize(
    "key",
    [
        "credential",
        "CREDENTIAL",
        "api.key",
        "client_secret",
        "client-secret",
        "CLIENT-SECRET",
        "access_token",
        "ACCESS-TOKEN",
        "refresh_token",
        "refresh-token",
        "private_key",
        "PRIVATE-KEY",
        "session_key",
        "SESSION-KEY",
        "auth_token",
        "AUTH-TOKEN",
    ],
)
def test_credential_marker_variants_cannot_reach_dump_state(key: str) -> None:
    _require_dspy()
    with pytest.raises(ValueError, match="provider credentials"):
        CambiumLM(
            FakeDiffundo(),
            ProviderTier.FAST,
            **{key: "SENSITIVE_CANARY"},
        )  # type: ignore[arg-type]


def test_per_call_max_tokens_reaches_diffundo() -> None:
    _require_dspy()
    diffundo = FakeDiffundo()
    lm = CambiumLM(diffundo, ProviderTier.FAST)  # type: ignore[arg-type]

    assert lm(messages=[{"role": "user", "content": "hello"}], max_tokens=1) == [
        "completion text"
    ]
    assert diffundo.calls[0]["prompt"]["max_tokens"] == 1


def test_concurrent_dspy_loads_preserve_cache_environment() -> None:
    _require_dspy()
    source = Path(__file__).resolve().parents[2] / "src"
    script = """
import os
import sys
import threading
import time
from concurrent.futures import ThreadPoolExecutor

sys.path.insert(0, sys.argv[1])
import cambium.lm as lm

assert lm._DSPY is None
assert "dspy" not in sys.modules

sentinel = "/tmp/cambium-dspy-cache-sentinel"
os.environ["DSPY_CACHEDIR"] = sentinel
cold_load_count = 0
cold_load_count_lock = threading.Lock()
start = threading.Barrier(16)


real_temporary_directory = lm.tempfile.TemporaryDirectory


def tracked_temporary_directory(*args, **kwargs):
    global cold_load_count
    with cold_load_count_lock:
        cold_load_count += 1
        first_load = cold_load_count == 1
    if first_load:
        time.sleep(0.1)
    return real_temporary_directory(*args, **kwargs)


class TempfileProxy:
    TemporaryDirectory = staticmethod(tracked_temporary_directory)


lm.tempfile = TempfileProxy()


def load_repeatedly(_: int) -> list[object]:
    start.wait()
    return [lm._load_dspy() for _ in range(2000)]


with ThreadPoolExecutor(max_workers=16) as executor:
    loads = list(executor.map(load_repeatedly, range(16)))
results = [value for load in loads for value in load]
assert len(results) == 16 * 2000
assert len({id(value) for value in results}) == 1
assert lm._DSPY is results[0]
assert cold_load_count == 1, cold_load_count
assert os.environ["DSPY_CACHEDIR"] == sentinel
"""
    env = os.environ.copy()
    env.pop("Py_GIL_DISABLED", None)
    completed = subprocess.run(
        [sys.executable, "-c", script, str(source)],
        env=env,
        text=True,
        capture_output=True,
        check=False,
    )
    assert completed.returncode == 0, completed.stderr
    assert completed.stderr == ""
