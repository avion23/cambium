"""Integration scenarios for the optional DSPy-to-Diffundo boundary."""

from __future__ import annotations

import asyncio
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

dspy = pytest.importorskip("dspy")


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


def test_identical_calls_are_not_cached_and_do_not_write_dspy_cache(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("HOME", str(tmp_path))
    diffundo = FakeDiffundo()
    lm = CambiumLM(diffundo, ProviderTier.FAST, temperature=0.0)  # type: ignore[arg-type]
    assert _call(lm) == ["completion text"]
    assert _call(lm) == ["completion text"]
    assert len(diffundo.calls) == 2
    assert lm.cache is False
    assert lm.num_retries == 0
    assert lm.history == []
    assert not (tmp_path / ".dspy_cache").exists()
    assert all("cache" not in call["prompt"] for call in diffundo.calls)


def test_same_prompt_to_two_provider_endpoints_reaches_each_once() -> None:
    first = FakeDiffundo("https://provider-a.invalid")
    second = FakeDiffundo("https://provider-b.invalid")
    assert _call(CambiumLM(first, ProviderTier.FAST)) == ["completion text"]  # type: ignore[arg-type]
    assert _call(CambiumLM(second, ProviderTier.FAST)) == ["completion text"]  # type: ignore[arg-type]
    assert len(first.calls) == len(second.calls) == 1
    assert first.calls[0]["endpoint"] != second.calls[0]["endpoint"]


def test_session_context_is_isolated_and_retains_no_prompt() -> None:
    diffundo = FakeDiffundo()
    lm = CambiumLM(diffundo, ProviderTier.FAST)  # type: ignore[arg-type]
    original_marker = object()
    with dspy.context(cambium_session=original_marker):
        assert _call(lm, "PROMPT-CANARY") == ["completion text"]
        assert dspy.settings.cambium_session is original_marker
    assert not hasattr(dspy.settings, "cambium_session")
    assert diffundo.session_markers[0] is not original_marker
    assert lm.history == []


def test_architectus_real_decide_port_uses_cambium_lm() -> None:
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


def test_free_threaded_build_is_rejected_before_dspy_use(monkeypatch: pytest.MonkeyPatch) -> None:
    import cambium.lm as lm_module

    monkeypatch.setattr(
        lm_module.sysconfig,
        "get_config_var",
        lambda name: int(name == "Py_GIL_DISABLED"),
    )
    with pytest.raises(RuntimeError, match="free-threaded CPython"):
        CambiumLM(FakeDiffundo(), ProviderTier.FAST)  # type: ignore[arg-type]
